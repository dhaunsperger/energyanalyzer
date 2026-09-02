"""Core data contracts for EnergyAnalyzer.

This module is the authoritative schema referenced by ARCHITECTURE.md §4-5.
Plans are stored as YAML (rates in cents/kWh, `_ckwh` suffix); the engine
works in $/kWh via the `usd_kwh` helpers.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

LOCAL_TZ = "America/Chicago"


# --------------------------------------------------------------------------- #
# Rate structure
# --------------------------------------------------------------------------- #
class RateWindow(BaseModel):
    """Time window in LOCAL clock time, matched on interval start.

    Empty lists are wildcards. hours are 0-23 local interval-start hours;
    weekdays 0=Mon .. 6=Sun; months 1-12.
    """

    months: list[int] = Field(default_factory=list)
    weekdays: list[int] = Field(default_factory=list)
    hours: list[int] = Field(default_factory=list)

    @field_validator("months")
    @classmethod
    def _months_ok(cls, v: list[int]) -> list[int]:
        assert all(1 <= m <= 12 for m in v), "months must be 1-12"
        return v

    @field_validator("weekdays")
    @classmethod
    def _weekdays_ok(cls, v: list[int]) -> list[int]:
        assert all(0 <= d <= 6 for d in v), "weekdays must be 0-6 (0=Mon)"
        return v

    @field_validator("hours")
    @classmethod
    def _hours_ok(cls, v: list[int]) -> list[int]:
        assert all(0 <= h <= 23 for h in v), "hours must be 0-23"
        return v

    def mask(self, df: pd.DataFrame) -> pd.Series:
        """Boolean mask over a frame that has month/weekday/hour columns
        (see add_local_columns)."""
        m = pd.Series(True, index=df.index)
        if self.months:
            m &= df["month_num"].isin(self.months)
        if self.weekdays:
            m &= df["weekday"].isin(self.weekdays)
        if self.hours:
            m &= df["hour"].isin(self.hours)
        return m


class RtwRate(BaseModel):
    """Real-time-wholesale indexed rate: price*multiplier + adder, clipped."""

    multiplier: float = 1.0
    adder_ckwh: float = 0.0
    cap_ckwh: Optional[float] = None  # max ¢/kWh applied after adder
    floor_ckwh: float = 0.0

    def usd_kwh(self, price_usd_kwh: pd.Series) -> pd.Series:
        r = price_usd_kwh * self.multiplier + self.adder_ckwh / 100.0
        if self.cap_ckwh is not None:
            r = r.clip(upper=self.cap_ckwh / 100.0)
        return r.clip(lower=self.floor_ckwh / 100.0)


class EnergyRate(BaseModel):
    """One rate rule. Exactly one of rate_ckwh / rtw must be set.
    window=None means catch-all default (must be last in the list)."""

    rate_ckwh: Optional[float] = None
    rtw: Optional[RtwRate] = None
    window: Optional[RateWindow] = None
    label: str = ""  # e.g. "free nights"
    tdu_exempt: bool = False  # import matched by this rate is excluded from TDU
    #   volumetric charges (plans whose "free" hours waive delivery too — e.g.
    #   Green Mtn Pollution Free Nights, Reliant Free Overnight). Only
    #   meaningful on Plan.energy_rates; ignored for buyback rates.

    @model_validator(mode="after")
    def _one_kind(self) -> "EnergyRate":
        assert (self.rate_ckwh is None) != (self.rtw is None), (
            "EnergyRate needs exactly one of rate_ckwh or rtw"
        )
        return self


class BuybackKind(str, Enum):
    none = "none"
    fixed = "fixed"
    rtw = "rtw"
    windows = "windows"  # time-of-use export rates (e.g. Rhythm PowerShift)


class Buyback(BaseModel):
    kind: BuybackKind = BuybackKind.none
    rate_ckwh: Optional[float] = None  # for kind=fixed
    rtw: Optional[RtwRate] = None  # for kind=rtw
    rates: list[EnergyRate] = Field(default_factory=list)  # for kind=windows,
    #   same first-match-wins semantics as Plan.energy_rates (last = default)
    offset_scope: Literal["energy_only", "all_charges"] = "all_charges"
    monthly_credit_cap: Literal[None, "energy_charge"] = None
    rollover: bool = True
    cash_out: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "Buyback":
        if self.kind == BuybackKind.fixed:
            assert self.rate_ckwh is not None, "fixed buyback needs rate_ckwh"
        if self.kind == BuybackKind.rtw:
            assert self.rtw is not None, "rtw buyback needs rtw config"
        if self.kind == BuybackKind.windows:
            assert self.rates and self.rates[-1].window is None, (
                "windows buyback needs rates list ending in a catch-all default"
            )
        return self


class BillCredit(BaseModel):
    """Credit applied when the month's import kWh is in [min_kwh, max_kwh)."""

    min_kwh: float = 0.0
    max_kwh: Optional[float] = None
    credit_usd: float


class EvFreeCharging(BaseModel):
    """Free EV charging during a time window, up to a monthly kWh allowance.

    Models plans (e.g. Tesla) that give free charging *for the car* during
    certain hours. Unlike a free-nights plan -- a 0c energy window that frees
    ALL usage -- this waives the energy charge on only up to ``monthly_kwh_cap``
    import kWh inside ``window`` each billing month (the estimated EV load, e.g.
    ~271 = 3250 kWh/yr / 12), at whatever rate those kWh would otherwise cost.
    Usage beyond the cap, or outside the window, is billed normally. Only the
    energy charge is waived; TDU delivery still applies (a REP can't waive TDU).
    """

    window: RateWindow
    monthly_kwh_cap: float  # free import kWh per billing month inside the window
    label: str = "EV free charging"

    @field_validator("monthly_kwh_cap")
    @classmethod
    def _cap_positive(cls, v: float) -> float:
        assert v > 0, "monthly_kwh_cap must be > 0"
        return v


class Plan(BaseModel):
    id: str  # filename-safe unique id, e.g. "gexa_solar_buyback_12"
    retailer: str
    name: str
    term_months: int
    tdu: str = "ONCOR"
    base_charge_usd: float = 0.0
    energy_rates: list[EnergyRate]
    buyback: Buyback = Field(default_factory=Buyback)
    bill_credits: list[BillCredit] = Field(default_factory=list)
    ev_free_charging: Optional[EvFreeCharging] = None
    tdu_passthrough: bool = True
    etf_usd: float = 0.0
    etf_per_month_remaining: bool = False
    # A one-off charge you cannot avoid if you want the plan -- Just Energy's
    # "One-time GoodBundle set up and carbon offset purchase: $49.99", which its
    # own EFL calls "required to enroll on this product". Counted ONCE in
    # first_year_net, which is what that number means: money out the door in
    # year one. Deliberately not folded into base_charge_usd at 1/12 (the way
    # the EFL amortizes it for its average-price table) -- these are 5-month
    # contracts, so spreading a one-off over twelve months understates it for
    # the term actually signed, and it would quietly distort every monthly view.
    signup_fee_usd: float = 0.0
    rate_type: Literal["fixed", "variable", "indexed"] = "fixed"
    renewable_pct: Optional[float] = None
    source: str = "manual"  # manual | efl:<file> | ptc | report-2026-07
    retrieved: Optional[dt.date] = None  # when the rate data was obtained;
    #   stamped by the EFL parse/promote flow, used for staleness badges
    # SHA-256 of the EFL PDF this reading was taken from, stamped at promote
    # time. It answers one question a refresh cannot otherwise answer: when a
    # re-parse of an already-verified plan lands back in review, is this a NEW
    # reading of a CHANGED document, or the same failed parse of the same
    # document the user already corrected by hand? Without it every refresh
    # re-queues work that was already done (17 such drafts on 2026-07-26).
    source_sha256: Optional[str] = None
    efl_url: Optional[str] = None
    # Where you actually sign up. Several plans are sold ONLY through a Power to
    # Choose referral landing page and are unreachable from the retailer's own
    # navigation -- Just Energy's family sells its six GoodBundle plans at
    # /ptcsl/ (Just Energy's is even /affiliatepartner/ptcsl/), which is why
    # they cannot be found by browsing the site. Taken from the PTC row's
    # enroll_url column, never guessed from the PDF.
    enroll_url: Optional[str] = None
    notes: str = ""
    needs_review: bool = False
    # Set when the EFL describes a structure this schema cannot express, so the
    # plan must not be priced at all. `needs_review` is not enough on its own:
    # "Promote all drafts" bypasses it deliberately, and a usage-tiered plan that
    # kept its first tier (Direct Apartment 12: 8.8798c of 8.8798/10.8798) looks
    # cheap and ranks high. rank() refuses these the same way it refuses an
    # all-zero rate -- a wrong number that sorts well is worse than no number.
    unpriceable_reason: Optional[str] = None
    # The REP will not sell this plan to a home with rooftop solar (TXU's Free
    # Nights & Cool Summer 12: "Customers with electric vehicles, batteries,
    # and/or solar panels are ineligible"). This premise HAS solar, so such a
    # plan is not merely mispriced, it is unbuyable -- and free-nights plans
    # tend to score well against a solar export profile, so it would otherwise
    # rank near the top. Ranking hides these by default rather than deleting
    # them: the exclusion is the REP's current policy, not a permanent fact.
    excludes_solar: bool = False

    @model_validator(mode="after")
    def _default_last(self) -> "Plan":
        assert self.energy_rates, "plan needs at least one energy rate"
        assert self.energy_rates[-1].window is None, (
            "last energy_rate must be the catch-all default (window: null)"
        )
        return self


class TduTariff(BaseModel):
    effective: dt.date
    fixed_usd_month: float
    volumetric_ckwh: float

    @property
    def volumetric_usd_kwh(self) -> float:
        return self.volumetric_ckwh / 100.0


# --------------------------------------------------------------------------- #
# Engine results
# --------------------------------------------------------------------------- #
class PlanResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    plan_id: str
    first_year_net: float
    final_rollover_balance: float = 0.0
    avg_import_price_ckwh: float  # (net bill) / import kWh, informational
    monthly: object  # pd.DataFrame: month, energy_cost, base, tdu, credit_earned,
    #                  credit_used, bill_credit, rollover_out, bill
    uses_rtw: bool = False
    # Share of intervals whose ERCOT price was estimated rather than published
    # (ERCOT's archive trails real time by a day or two). >0 means this plan's
    # figure leans on estimated prices for that slice -- see prices.ercot.
    prices_estimated_fraction: float = 0.0
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Interval-frame helpers
# --------------------------------------------------------------------------- #
def add_local_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Given the canonical UTC-indexed frame, add local-time helper columns:
    local, month (Period), month_num, hour, weekday, date."""
    out = df.copy()
    local = out.index.tz_convert(LOCAL_TZ)
    out["local"] = local
    # A month Period is tz-naive; converting a tz-aware index to Period warns
    # ("...will drop timezone information"). `local` is already local wall time,
    # so drop the tz explicitly first -- same month buckets, no warning.
    out["month"] = local.tz_localize(None).to_period("M")
    out["month_num"] = local.month
    out["hour"] = local.hour
    out["weekday"] = local.weekday
    out["date"] = local.date
    return out


def validate_intervals(df: pd.DataFrame) -> None:
    """Raise AssertionError if the frame violates the canonical contract."""
    assert isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None, (
        "index must be tz-aware DatetimeIndex (UTC)"
    )
    assert str(df.index.tz) == "UTC", "index must be UTC"
    assert df.index.is_monotonic_increasing, "index must be sorted"
    assert not df.index.has_duplicates, "duplicate interval starts"
    for col in ("import_kwh", "export_kwh"):
        assert col in df.columns, f"missing column {col}"
        assert (df[col] >= 0).all(), f"{col} must be non-negative"


# --------------------------------------------------------------------------- #
# Billing window selection
# --------------------------------------------------------------------------- #
BILLING_MONTHS = 12


class BillingWindow(BaseModel):
    """Which calendar months a first-year cost is computed over, and why.

    A "first-year net bill" is only meaningful over ~12 months, but the interval
    frame is whatever the user's exports happen to add up to: SmartMeter Texas
    hands out rolling 12-month windows, so keeping two downloads side by side
    merges into 13-14 distinct calendar months. Billing all of them sums a
    13th and 14th month into the annual figure -- and because the extra months
    are whichever season the two downloads straddle (summer, for a spring and
    an autumn export), the error is seasonal rather than uniform and reorders
    the ranking instead of just inflating it.
    """

    months_available: int
    months_used: int
    start: str  # first billed month, "YYYY-MM"
    end: str  # last billed month
    trimmed: bool  # were whole months dropped to reach months_used?
    partial_ends: bool  # is either end month incomplete?

    @property
    def note(self) -> str:
        if self.trimmed:
            return (
                f"Using the most recent {self.months_used} complete calendar months "
                f"({self.start} to {self.end}) of the {self.months_available} months "
                "loaded. A first-year cost is only comparable over a single year; "
                "the extra months would double-count a season and change the ranking."
            )
        if self.months_used < BILLING_MONTHS:
            return (
                f"Only {self.months_used} calendar months of usage are loaded "
                f"({self.start} to {self.end}). Costs shown are for that span, NOT a "
                "full year, and plans whose value is seasonal will rank unreliably."
            )
        if self.partial_ends:
            return (
                f"Usage spans {self.start} to {self.end}; the first and/or last month "
                "is partial, so their fixed charges are prorated."
            )
        return f"Using {self.months_used} complete calendar months ({self.start} to {self.end})."

    @property
    def is_reliable(self) -> bool:
        return self.months_used >= BILLING_MONTHS


def describe_billing_window(df: pd.DataFrame, months: int = BILLING_MONTHS) -> BillingWindow:
    """Describe the window `select_billing_window` would bill for `df`."""
    local = add_local_columns(df)
    counts = local.groupby("month")["import_kwh"].size()
    periods = list(counts.index)
    complete = [p for p in periods if counts[p] >= p.days_in_month * 96 * 0.99]
    keep = [p for p in periods if p in complete][-months:] if len(complete) >= months else periods
    return BillingWindow(
        months_available=len(periods),
        months_used=len(keep),
        start=str(keep[0]) if keep else "",
        end=str(keep[-1]) if keep else "",
        trimmed=len(keep) < len(periods),
        partial_ends=bool(keep) and (keep[0] not in complete or keep[-1] not in complete),
    )


def select_billing_window(
    df: pd.DataFrame, months: int = BILLING_MONTHS
) -> tuple[pd.DataFrame, BillingWindow]:
    """Trim `df` to the most recent `months` COMPLETE calendar months.

    Returns the frame unchanged when it doesn't hold that many complete months,
    so a short dataset still produces numbers -- the returned BillingWindow says
    what happened and `is_reliable` says whether to trust a first-year total.
    """
    window = describe_billing_window(df, months)
    if not window.trimmed:
        return df, window
    local = add_local_columns(df)
    return df[local["month"].astype(str).between(window.start, window.end)], window
