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
    tdu_passthrough: bool = True
    etf_usd: float = 0.0
    etf_per_month_remaining: bool = False
    rate_type: Literal["fixed", "variable", "indexed"] = "fixed"
    renewable_pct: Optional[float] = None
    source: str = "manual"  # manual | efl:<file> | ptc | report-2026-07
    efl_url: Optional[str] = None
    notes: str = ""
    needs_review: bool = False

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
    out["month"] = local.to_period("M") if hasattr(local, "to_period") else pd.PeriodIndex(local, freq="M")
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
