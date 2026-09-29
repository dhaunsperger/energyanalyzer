"""Price the months a short contract leaves uncovered.

A plan shorter than the 12-month comparison year does not stop costing money
when its term ends. Texas rules roll the customer onto the retailer's
month-to-month product, at whatever the retailer charges that month -- and for
the 1-month promotional plans that top the rankings, the advertised rate is
explicitly the first month's only ("applied to your first month's bill period
and may change on a monthly bill basis at the sole discretion of ..."). Carrying
that rate through twelve months ranked Ranchero's 2.02c first; its parent's own
published history puts the following months at 11.9-14.3c, and the same year
costs about twice as much.

`apply_contract_rollover` rewrites a plan's energy schedule for one billing
window: the contract's own rates for its first `term_months` months, then the
retailer's published variable rate for each month after. It is expressed purely
as month-windowed `EnergyRate`s, so the unmodified engine bills it -- this module
never touches the billing math.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

from energyanalyzer.core.models import (
    EnergyRate,
    Plan,
    RateWindow,
    VariableRateHistory,
    add_local_columns,
)

COMPARISON_MONTHS = 12


def rolls_over(plan: Plan) -> bool:
    """Does this plan's term end inside the comparison year with a FIXED
    schedule that could be mistaken for a year-long price? RTW-indexed energy
    has no promotional rate to run out, so it is left alone."""
    return plan.term_months < COMPARISON_MONTHS and all(
        r.rtw is None for r in plan.energy_rates
    )


def history_for(plan: Plan, histories: list[VariableRateHistory]) -> Optional[VariableRateHistory]:
    for h in histories:
        if h.matches(plan.retailer) and h.tdu.get((plan.tdu or "").upper()):
            return h
    return None


def _billing_months(intervals: pd.DataFrame) -> list[pd.Period]:
    return sorted(add_local_columns(intervals)["month"].unique())


def apply_contract_rollover(
    plan: Plan, intervals: pd.DataFrame, history: VariableRateHistory
) -> tuple[Plan, str]:
    """Return (plan with a rolled-over energy schedule, human-readable note).

    Contract months are the first `plan.term_months` calendar months of the
    billing window; each later month is priced at the variable rate in effect
    mid-month (the 15th) of that same calendar month. The usage year is a past
    year used as a proxy for the next, so each month is paired with what the
    retailer actually charged then -- the same season, the same wholesale
    conditions -- rather than one arbitrary "current" rate.

    Month windows identify months by number (1-12), so this assumes the window
    spans at most 12 distinct months, which `select_billing_window` guarantees.
    Base charge, bill credits and buyback carry over unchanged.
    """
    months = _billing_months(intervals)
    contract = months[: plan.term_months]
    later = months[plan.term_months :]
    if not later:
        return plan, ""
    contract_nums = sorted({m.month for m in contract})

    rates: list[EnergyRate] = []
    for r in plan.energy_rates:
        if r.window is None:
            window = RateWindow(months=contract_nums)
        else:
            m = r.window.months or contract_nums
            m = sorted(set(m) & set(contract_nums))
            if not m:
                continue  # this rule only applies in months past the contract
            window = r.window.model_copy(update={"months": m})
        rates.append(r.model_copy(update={"window": window}))

    tdu = (plan.tdu or "").upper()
    priced = [(m, history.rate_on(tdu, dt.date(m.year, m.month, 15))) for m in later]
    for i, (m, ckwh) in enumerate(priced):
        last = i == len(priced) - 1
        rates.append(
            EnergyRate(
                rate_ckwh=ckwh,
                label=f"month-to-month {m}",
                window=None if last else RateWindow(months=[m.month]),
            )
        )

    values = [c for _, c in priced]
    note = (
        f"after month {plan.term_months}: {history.retailers[0]}'s published variable rate, "
        f"{min(values):.2f}-{max(values):.2f}c/kWh"
        if min(values) != max(values)
        else f"after month {plan.term_months}: {history.retailers[0]}'s published variable rate, "
        f"{values[0]:.2f}c/kWh"
    )
    return plan.model_copy(update={"energy_rates": rates}), note
