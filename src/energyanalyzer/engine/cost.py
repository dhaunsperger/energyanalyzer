"""Billing simulation engine (ARCHITECTURE.md §6).

Entry points:
    simulate(plan, intervals, tdu, prices=None) -> PlanResult
    rank(plans, intervals, tdu, prices=None) -> list[PlanResult]  (see RankedResults)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..core.models import (
    Buyback,
    BuybackKind,
    EnergyRate,
    EvFreeCharging,
    Plan,
    PlanResult,
    TduTariff,
    add_local_columns,
    validate_intervals,
    describe_billing_window,
)

logger = logging.getLogger(__name__)


def _first_match_rate(
    rates: list[EnergyRate],
    df: pd.DataFrame,
    prices: pd.Series | None,
    active_mask: pd.Series,
    context: str,
) -> tuple[pd.Series, pd.Series, bool]:
    """Resolve a $/kWh rate Series over df.index using first-match window
    semantics: iterate the ordered rate list, assigning wherever an interval
    is still unassigned and the rate's window matches (window=None = catch-all,
    must be last). Returns (rate_series, tdu_exempt_mask, uses_rtw); the
    tdu_exempt mask marks intervals whose matched rate waives TDU volumetric
    charges (only meaningful for energy rates — buyback callers ignore it)."""
    rate = pd.Series(np.nan, index=df.index, dtype=float)
    tdu_exempt = pd.Series(False, index=df.index)
    unassigned = pd.Series(True, index=df.index)
    uses_rtw = False
    for er in rates:
        if not unassigned.any():
            break
        mask = unassigned if er.window is None else (unassigned & er.window.mask(df))
        if not mask.any():
            continue
        if er.rate_ckwh is not None:
            rate.loc[mask] = er.rate_ckwh / 100.0
        else:
            uses_rtw = True
            if prices is None:
                raise ValueError(
                    f"{context}: rate '{er.label or '(unlabeled)'}' is RTW-indexed "
                    "but no price series was supplied"
                )
            aligned = prices.reindex(df.index)
            need = mask & active_mask
            missing = need & aligned.isna()
            if missing.any():
                bad = df.index[missing][0]
                raise ValueError(
                    f"{context}: missing RTW price data for {int(missing.sum())} "
                    f"interval(s) with nonzero usage (e.g. {bad}); cannot bill without prices"
                )
            rate.loc[mask] = er.rtw.usd_kwh(aligned).loc[mask]
        if er.tdu_exempt:
            tdu_exempt |= mask
        unassigned &= ~mask
    if unassigned.any():
        raise ValueError(
            f"{context}: no matching rate for {int(unassigned.sum())} interval(s); "
            "the rate list must end with a catch-all (window: null)"
        )
    return rate, tdu_exempt, uses_rtw


def _apply_ev_free_charging(
    df: pd.DataFrame, ev: EvFreeCharging
) -> tuple[pd.Series, pd.Series]:
    """Waive the energy charge on the first ``ev.monthly_kwh_cap`` import kWh
    inside ``ev.window`` each billing month (chronologically), at those kWh's
    own rate. Returns ``(reduced_energy_charge, freed_kwh)`` per interval.

    Only the energy charge is reduced -- TDU delivery on those kWh still
    applies. Requires df to carry the ``add_local_columns`` helpers and an
    ``energy_charge`` column, and to be time-sorted (the canonical UTC frame is).
    """
    window_kwh = df["import_kwh"].where(ev.window.mask(df), 0.0)
    # Cumulative in-window import kWh BEFORE this interval, within its month, so
    # the cap is spent chronologically across the month.
    cum_before = window_kwh.groupby(df["month"], sort=False).cumsum() - window_kwh
    remaining_cap = (ev.monthly_kwh_cap - cum_before).clip(lower=0.0)
    freed_kwh = pd.concat([remaining_cap, window_kwh], axis=1).min(axis=1)  # per interval
    # Fraction of this interval's import that's freed -> reduce its energy charge
    # by the same fraction (0 for non-window / zero-usage intervals).
    frac = (freed_kwh / df["import_kwh"]).where(df["import_kwh"] > 0, 0.0).clip(0.0, 1.0)
    return df["energy_charge"] * (1.0 - frac), freed_kwh


def _resolve_buyback_rate(
    buyback: Buyback, df: pd.DataFrame, prices: pd.Series | None, plan_id: str
) -> tuple[pd.Series, bool]:
    """Return (buyback_rate_usd_kwh series, uses_rtw) per interval."""
    active_mask = df["export_kwh"] > 0
    if buyback.kind == BuybackKind.none:
        return pd.Series(0.0, index=df.index), False
    if buyback.kind == BuybackKind.fixed:
        return pd.Series(buyback.rate_ckwh / 100.0, index=df.index), False
    if buyback.kind == BuybackKind.rtw:
        if prices is None:
            raise ValueError(
                f"plan {plan_id}: buyback is RTW-indexed but no price series was supplied"
            )
        aligned = prices.reindex(df.index)
        missing = active_mask & aligned.isna()
        if missing.any():
            bad = df.index[missing][0]
            raise ValueError(
                f"plan {plan_id}: missing RTW price data for {int(missing.sum())} "
                f"export interval(s) (e.g. {bad}); cannot bill without prices"
            )
        return buyback.rtw.usd_kwh(aligned), True
    if buyback.kind == BuybackKind.windows:
        rate, _, uses_rtw = _first_match_rate(
            buyback.rates, df, prices, active_mask, f"plan {plan_id} buyback.rates"
        )
        return rate, uses_rtw
    raise AssertionError(f"unknown buyback kind: {buyback.kind!r}")  # pragma: no cover


def simulate(
    plan: Plan,
    intervals: pd.DataFrame,
    tdu: TduTariff,
    prices: pd.Series | None = None,
) -> PlanResult:
    """Simulate one year of monthly bills for `plan` against `intervals`
    (canonical UTC import/export frame, see core.models). Implements the
    algorithm in ARCHITECTURE.md §6 exactly.

    `prices` is a $/kWh Series on a UTC 15-min index, required only if the
    plan (energy or buyback) uses an RTW-indexed rate; if it's needed and
    missing (None, or has gaps over intervals with nonzero usage) a
    ValueError is raised.
    """
    validate_intervals(intervals)

    # Defence in depth: the app trims to a billing window before ranking (and
    # says so), but a direct caller passing 13-14 months -- what two overlapping
    # SmartMeter exports merge into -- would otherwise get those extra months
    # silently summed into a figure labelled "first year".
    span = describe_billing_window(intervals)
    plan_warnings: list[str] = [] if (span.is_reliable and not span.trimmed) else [span.note]

    df = add_local_columns(intervals)

    energy_rate, tdu_exempt, energy_rtw = _first_match_rate(
        plan.energy_rates,
        df,
        prices,
        active_mask=df["import_kwh"] > 0,
        context=f"plan {plan.id} energy_rates",
    )
    buyback_rate, buyback_rtw = _resolve_buyback_rate(plan.buyback, df, prices, plan.id)

    df = df.copy()
    df["energy_charge"] = df["import_kwh"] * energy_rate
    # Free EV charging: waive the energy charge on capped in-window import kWh
    # each month (TDU still applies). Reduces energy_charge before it's summed.
    if plan.ev_free_charging is not None:
        df["energy_charge"], df["ev_free_kwh"] = _apply_ev_free_charging(
            df, plan.ev_free_charging
        )
    else:
        df["ev_free_kwh"] = 0.0
    df["export_credit_raw"] = df["export_kwh"] * buyback_rate
    df["tdu_import_kwh"] = df["import_kwh"].where(~tdu_exempt, 0.0)

    uses_rtw = energy_rtw or buyback_rtw

    rows: list[dict] = []
    rollover = 0.0
    for month, g in df.groupby("month", sort=True):
        import_kwh = float(g["import_kwh"].sum())
        export_kwh = float(g["export_kwh"].sum())
        energy_cost = float(g["energy_charge"].sum())
        ev_free_kwh = float(g["ev_free_kwh"].sum())

        # Monthly FIXED charges (base + TDU's per-month fee) are prorated by how
        # much of the calendar month the data actually covers. A 365-day export
        # that doesn't start on the 1st spans 13 calendar months, and charging
        # all 13 in full invents a 13th month of fixed fees -- worse, the error
        # scales with the plan's base charge ($19.95/mo plans absorb four times
        # the phantom cost of a $4.95/mo plan), which reorders the ranking. With
        # proration the two partial end months sum back to exactly one month.
        # Whole months are unaffected (coverage == 1.0).
        coverage = min(int(g["date"].nunique()) / month.days_in_month, 1.0)
        base = plan.base_charge_usd * coverage
        tdu_charge = (
            tdu.fixed_usd_month * coverage
            + tdu.volumetric_usd_kwh * float(g["tdu_import_kwh"].sum())
            if plan.tdu_passthrough
            else 0.0
        )

        if plan.buyback.kind == BuybackKind.none:
            credit_earned = 0.0
        else:
            credit_earned = float(g["export_credit_raw"].sum())
            if plan.buyback.monthly_credit_cap == "energy_charge":
                credit_earned = min(credit_earned, energy_cost)

        bill_credit = 0.0
        for bc in plan.bill_credits:
            hi_ok = bc.max_kwh is None or import_kwh < bc.max_kwh
            if import_kwh >= bc.min_kwh and hi_ok:
                bill_credit += bc.credit_usd

        pool = credit_earned + rollover

        if plan.buyback.offset_scope == "energy_only":
            offsettable = energy_cost
        else:  # all_charges
            offsettable = energy_cost + base + tdu_charge - bill_credit

        if plan.buyback.cash_out:
            used = pool
            rollover_out = 0.0
        else:
            used = min(pool, max(offsettable, 0.0))
            rollover_out = (pool - used) if plan.buyback.rollover else 0.0

        bill = energy_cost + base + tdu_charge - bill_credit - used

        rows.append(
            {
                "month": str(month),
                "coverage": coverage,
                "import_kwh": import_kwh,
                "export_kwh": export_kwh,
                "energy_cost": energy_cost,
                "ev_free_kwh": ev_free_kwh,
                "base": base,
                "tdu": tdu_charge,
                "bill_credit": bill_credit,
                "credit_earned": credit_earned,
                "credit_used": used,
                "rollover_out": rollover_out,
                "bill": bill,
            }
        )
        rollover = rollover_out

    monthly = pd.DataFrame(
        rows,
        columns=[
            "month",
            "coverage",
            "import_kwh",
            "export_kwh",
            "energy_cost",
            "ev_free_kwh",
            "base",
            "tdu",
            "bill_credit",
            "credit_earned",
            "credit_used",
            "rollover_out",
            "bill",
        ],
    )

    # A mandatory one-off (e.g. a required carbon-offset/setup purchase) is part
    # of what year one costs, so it lands in the total but never in a monthly
    # bill -- the monthly frame stays a faithful picture of the recurring bill.
    first_year_net = float(monthly["bill"].sum()) + float(plan.signup_fee_usd or 0.0)
    total_import = float(monthly["import_kwh"].sum())
    avg_import_price_ckwh = (first_year_net / total_import * 100.0) if total_import else 0.0

    return PlanResult(
        plan_id=plan.id,
        first_year_net=first_year_net,
        final_rollover_balance=rollover,
        avg_import_price_ckwh=avg_import_price_ckwh,
        monthly=monthly,
        uses_rtw=uses_rtw,
        warnings=plan_warnings,
    )


class RankedResults(list):
    """A list[PlanResult] (ascending by first_year_net) with a `.warnings`
    attribute collecting messages for any plan skipped because simulate()
    raised (e.g. a plan needs RTW prices that weren't supplied)."""

    def __init__(self, *args):
        super().__init__(*args)
        self.warnings: list[str] = []


def rank(
    plans: list[Plan],
    intervals: pd.DataFrame,
    tdu: TduTariff,
    prices: pd.Series | None = None,
) -> list[PlanResult]:
    """Simulate every plan and return results sorted ascending by
    first_year_net. Plans whose simulation raises are skipped; the message
    is logged and collected in the returned list's `.warnings` attribute
    (see RankedResults) rather than aborting the whole ranking."""
    results = RankedResults()
    for plan in plans:
        # A plan that charges nothing for energy in EVERY window is not a cheap
        # plan, it is a failed parse. The EFL parser defaults to 0.0 when it can
        # find no Energy Charge -- which happens on usage-tiered plans, whose
        # tiers the schema cannot express -- and 0c/kWh then wins the ranking
        # outright. Free electricity is the one wrong answer that always sorts
        # first, so it is refused here rather than trusted: the confidence gate
        # cannot catch it, because "Promote all drafts" bypasses needs_review.
        # `rtw is None` on every rate first: an RTW-indexed IMPORT rate leaves
        # rate_ckwh unset, and must not be mistaken for a zero.
        if plan.unpriceable_reason:
            results.warnings.append(
                f"skipping plan {plan.id}: {plan.unpriceable_reason}"
            )
            continue
        if all(r.rtw is None for r in plan.energy_rates) and not any(
            r.rate_ckwh for r in plan.energy_rates
        ):
            msg = (
                f"skipping plan {plan.id}: every energy rate is 0c/kWh, which means the "
                "rate could not be read from the EFL (usage-tiered plans parse this way) "
                "-- not that the electricity is free"
            )
            logger.warning(msg)
            results.warnings.append(msg)
            continue
        try:
            results.append(simulate(plan, intervals, tdu, prices))
        except ValueError as exc:
            msg = f"skipping plan {plan.id}: {exc}"
            logger.warning(msg)
            results.warnings.append(msg)
    results.sort(key=lambda r: r.first_year_net)
    return results
