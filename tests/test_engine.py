"""Tests for engine/cost.py (ARCHITECTURE.md §6)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from energyanalyzer.core.models import (
    BillCredit,
    Buyback,
    BuybackKind,
    EnergyRate,
    EvFreeCharging,
    Plan,
    RateWindow,
    RtwRate,
    TduTariff,
)
from energyanalyzer.engine.cost import rank, simulate

REPO_ROOT = Path(__file__).resolve().parents[1]

# Frozen July-2026 report benchmark archive (see the folder's README). Every
# input the benchmark depends on is pinned here -- the plan YAMLs, the Oncor
# tariff, and the exact interval usage CSV -- so refreshing live usage data,
# adding a new Oncor tariff, or pruning the live plans/ database can't move the
# expected dollar figures. See test_integration_report_benchmarks.
BENCHMARK_DIR = Path(__file__).parent / "fixtures" / "benchmark_2026_07"
BENCHMARK_PLANS_DIR = BENCHMARK_DIR / "plans"
BENCHMARK_TDU_YAML = BENCHMARK_DIR / "oncor.yaml"
# Private usage data (gitignored, same as data/); the test skips when absent.
BENCHMARK_CSV = BENCHMARK_DIR / "IntervalData.csv"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def make_intervals(
    start: str, days: int, import_kwh: float, export_kwh: float
) -> pd.DataFrame:
    """Build a canonical UTC interval frame: `days` days of 15-min intervals
    starting at local (America/Chicago) midnight on `start` (an ISO date
    string), each interval carrying constant import/export kwh. Dates should
    be chosen outside DST transitions so localization is unambiguous."""
    idx_local = pd.date_range(
        start=start, periods=days * 96, freq="15min", tz="America/Chicago"
    )
    idx_utc = idx_local.tz_convert("UTC")
    return pd.DataFrame(
        {"import_kwh": import_kwh, "export_kwh": export_kwh}, index=idx_utc
    ).sort_index()


def month_coverage(start: str, days: int) -> float:
    """Fraction of `start`'s calendar month that `days` days cover.

    Monthly FIXED charges (plan base + the TDU's per-month fee) prorate by
    this, so a 365-day span that straddles 13 calendar months still totals 12
    months of fixed fees. These unit tests deliberately use short spans, so
    their expected fixed charges are fractions of a month -- real 12-month
    datasets have coverage 1.0 everywhere and are unaffected.
    """
    return min(days / pd.Period(start, freq="M").days_in_month, 1.0)


def flat_tdu(fixed=10.0, volumetric_ckwh=5.0) -> TduTariff:
    return TduTariff(
        effective=dt.date(2023, 1, 1),
        fixed_usd_month=fixed,
        volumetric_ckwh=volumetric_ckwh,
    )


def base_plan(**overrides) -> Plan:
    defaults = dict(
        id="test_plan",
        retailer="Test Co",
        name="Test Plan",
        term_months=12,
        base_charge_usd=0.0,
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        buyback=Buyback(kind=BuybackKind.none),
        tdu_passthrough=False,
    )
    defaults.update(overrides)
    return Plan(**defaults)


# --------------------------------------------------------------------------- #
# 1. Flat rate + TDU
# --------------------------------------------------------------------------- #
def test_flat_rate_with_tdu():
    intervals = make_intervals("2024-01-08", days=2, import_kwh=1.0, export_kwh=0.0)
    plan = base_plan(
        base_charge_usd=5.0,
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        tdu_passthrough=True,
    )
    tdu = flat_tdu(fixed=10.0, volumetric_ckwh=5.0)
    result = simulate(plan, intervals, tdu)

    assert len(result.monthly) == 1
    row = result.monthly.iloc[0]
    n = 2 * 96
    cov = month_coverage("2024-01-08", 2)
    assert row["coverage"] == pytest.approx(cov)
    assert row["import_kwh"] == pytest.approx(n * 1.0)
    expected_energy = n * 1.0 * 0.10
    expected_tdu = 10.0 * cov + 0.05 * (n * 1.0)
    assert row["energy_cost"] == pytest.approx(expected_energy)
    assert row["tdu"] == pytest.approx(expected_tdu)
    assert row["bill"] == pytest.approx(expected_energy + 5.0 * cov + expected_tdu)
    assert result.first_year_net == pytest.approx(row["bill"])
    assert not result.uses_rtw


# --------------------------------------------------------------------------- #
# 2. Free-night window
# --------------------------------------------------------------------------- #
def test_free_night_window():
    intervals = make_intervals("2024-01-08", days=2, import_kwh=1.0, export_kwh=0.0)
    free_hours = [21, 22, 23, 0, 1, 2, 3, 4, 5]
    plan = base_plan(
        energy_rates=[
            EnergyRate(rate_ckwh=0.0, window=RateWindow(hours=free_hours)),
            EnergyRate(rate_ckwh=20.0),
        ]
    )
    tdu = flat_tdu()
    result = simulate(plan, intervals, tdu)

    row = result.monthly.iloc[0]
    paid_hours_per_day = 24 - len(free_hours)
    paid_intervals = paid_hours_per_day * 4 * 2
    expected_energy = paid_intervals * 1.0 * 0.20
    assert row["energy_cost"] == pytest.approx(expected_energy)
    assert row["bill"] == pytest.approx(expected_energy)


# --------------------------------------------------------------------------- #
# 2b. Free EV charging: capped, energy-only, does not free whole-home load
# --------------------------------------------------------------------------- #
def test_ev_free_charging_caps_and_leaves_tdu():
    # 2 days, 1 kWh/interval. Charging window = local hours 0-5 (6h x 4 x 2 days
    # = 48 window kWh). Cap 20 kWh/month -> free 20 kWh at the 10c energy rate.
    intervals = make_intervals("2024-01-08", days=2, import_kwh=1.0, export_kwh=0.0)
    window = RateWindow(hours=[0, 1, 2, 3, 4, 5])
    plan = base_plan(
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        tdu_passthrough=True,
        ev_free_charging=EvFreeCharging(window=window, monthly_kwh_cap=20.0),
    )
    tdu = flat_tdu(fixed=10.0, volumetric_ckwh=5.0)
    result = simulate(plan, intervals, tdu)
    row = result.monthly.iloc[0]

    total_kwh = 2 * 96
    # Only 20 kWh are freed (cap), even though 48 kWh fell in the window.
    assert row["ev_free_kwh"] == pytest.approx(20.0)
    # Energy charge = (all kWh - 20 free) x 10c.
    assert row["energy_cost"] == pytest.approx((total_kwh - 20) * 0.10)
    # TDU is charged on ALL import kWh -- the free benefit waives energy only.
    assert row["tdu"] == pytest.approx(
        10.0 * month_coverage("2024-01-08", 2) + 0.05 * total_kwh
    )
    # Sanity: an identical plan without the benefit costs exactly 20 x 10c more.
    plain = base_plan(energy_rates=[EnergyRate(rate_ckwh=10.0)], tdu_passthrough=True)
    plain_row = simulate(plain, intervals, tdu).monthly.iloc[0]
    assert plain_row["bill"] - row["bill"] == pytest.approx(20 * 0.10)


def test_ev_free_charging_cap_exceeds_window_usage_frees_all():
    # Cap larger than the window usage -> frees exactly the window kWh, no more.
    intervals = make_intervals("2024-01-08", days=1, import_kwh=1.0, export_kwh=0.0)
    window = RateWindow(hours=[0, 1])  # 2h x 4 = 8 window kWh in the day
    plan = base_plan(
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        ev_free_charging=EvFreeCharging(window=window, monthly_kwh_cap=500.0),
    )
    row = simulate(plan, intervals, flat_tdu()).monthly.iloc[0]
    assert row["ev_free_kwh"] == pytest.approx(8.0)  # not the full 500 cap
    assert row["energy_cost"] == pytest.approx((96 - 8) * 0.10)


# --------------------------------------------------------------------------- #
# 3. Weekend window
# --------------------------------------------------------------------------- #
def test_weekend_window():
    # 2024-01-08 is a Monday; 7 days covers exactly one Sat+Sun, same month.
    intervals = make_intervals("2024-01-08", days=7, import_kwh=1.0, export_kwh=0.0)
    plan = base_plan(
        energy_rates=[
            EnergyRate(rate_ckwh=0.0, window=RateWindow(weekdays=[5, 6])),
            EnergyRate(rate_ckwh=15.0),
        ]
    )
    tdu = flat_tdu()
    result = simulate(plan, intervals, tdu)

    row = result.monthly.iloc[0]
    weekday_intervals = 5 * 96
    expected_energy = weekday_intervals * 1.0 * 0.15
    assert row["energy_cost"] == pytest.approx(expected_energy)
    assert row["bill"] == pytest.approx(expected_energy)


# --------------------------------------------------------------------------- #
# 4. Fixed buyback, offset_scope=energy_only: credit can't touch base/tdu,
#    excess rolls over.
# --------------------------------------------------------------------------- #
def test_fixed_buyback_energy_only_scope_rolls_over():
    intervals = make_intervals("2024-01-08", days=1, import_kwh=0.5, export_kwh=0.5)
    plan = base_plan(
        base_charge_usd=5.0,
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        tdu_passthrough=True,
        buyback=Buyback(
            kind=BuybackKind.fixed,
            rate_ckwh=50.0,
            offset_scope="energy_only",
            rollover=True,
        ),
    )
    tdu = flat_tdu(fixed=10.0, volumetric_ckwh=5.0)
    result = simulate(plan, intervals, tdu)

    row = result.monthly.iloc[0]
    n = 96
    cov = month_coverage("2024-01-08", 1)
    energy_cost = n * 0.5 * 0.10  # 4.8
    tdu_charge = 10.0 * cov + 0.05 * (n * 0.5)
    credit_earned = n * 0.5 * 0.50  # 24.0
    assert row["energy_cost"] == pytest.approx(energy_cost)
    assert row["tdu"] == pytest.approx(tdu_charge)
    assert row["credit_earned"] == pytest.approx(credit_earned)
    used = min(credit_earned, energy_cost)
    assert row["credit_used"] == pytest.approx(used)
    expected_bill = energy_cost + 5.0 * cov + tdu_charge - used
    assert row["bill"] == pytest.approx(expected_bill)
    assert row["rollover_out"] == pytest.approx(credit_earned - used)
    assert result.final_rollover_balance == pytest.approx(credit_earned - used)


# --------------------------------------------------------------------------- #
# 5. all_charges scope: credit can offset base + TDU too.
# --------------------------------------------------------------------------- #
def test_fixed_buyback_all_charges_scope():
    intervals = make_intervals("2024-01-08", days=1, import_kwh=0.5, export_kwh=0.5)
    plan = base_plan(
        base_charge_usd=5.0,
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        tdu_passthrough=True,
        buyback=Buyback(
            kind=BuybackKind.fixed,
            rate_ckwh=50.0,
            offset_scope="all_charges",
            rollover=True,
        ),
    )
    tdu = flat_tdu(fixed=10.0, volumetric_ckwh=5.0)
    result = simulate(plan, intervals, tdu)

    row = result.monthly.iloc[0]
    cov = month_coverage("2024-01-08", 1)
    energy_cost = 96 * 0.5 * 0.10
    tdu_charge = 10.0 * cov + 0.05 * (96 * 0.5)
    credit_earned = 96 * 0.5 * 0.50
    offsettable = energy_cost + 5.0 * cov + tdu_charge
    used = min(credit_earned, offsettable)
    assert row["credit_used"] == pytest.approx(used)
    expected_bill = energy_cost + 5.0 * cov + tdu_charge - used
    assert row["bill"] == pytest.approx(expected_bill)
    assert expected_bill == pytest.approx(0.0)
    assert row["rollover_out"] == pytest.approx(credit_earned - used)


# --------------------------------------------------------------------------- #
# 6. monthly_credit_cap = energy_charge
# --------------------------------------------------------------------------- #
def test_monthly_credit_cap_energy_charge():
    intervals = make_intervals("2024-01-08", days=1, import_kwh=0.5, export_kwh=0.3)
    plan = base_plan(
        base_charge_usd=5.0,
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        tdu_passthrough=True,
        buyback=Buyback(
            kind=BuybackKind.fixed,
            rate_ckwh=50.0,
            offset_scope="all_charges",
            monthly_credit_cap="energy_charge",
        ),
    )
    tdu = flat_tdu(fixed=10.0, volumetric_ckwh=5.0)
    result = simulate(plan, intervals, tdu)

    row = result.monthly.iloc[0]
    energy_cost = 96 * 0.5 * 0.10  # 4.8
    raw_credit = 96 * 0.3 * 0.50  # 14.4 (would exceed energy_cost uncapped)
    assert raw_credit > energy_cost
    assert row["credit_earned"] == pytest.approx(energy_cost)  # capped


# --------------------------------------------------------------------------- #
# 7. bill_credits tier
# --------------------------------------------------------------------------- #
def test_bill_credits_tier():
    # 96 intervals * 1.25 kwh = 120 kwh -> matches the >=100 tier.
    intervals = make_intervals("2024-01-08", days=1, import_kwh=1.25, export_kwh=0.0)
    plan = base_plan(
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        bill_credits=[
            BillCredit(min_kwh=0, max_kwh=100, credit_usd=0.0),
            BillCredit(min_kwh=100, max_kwh=None, credit_usd=5.0),
        ],
    )
    tdu = flat_tdu()
    result = simulate(plan, intervals, tdu)

    row = result.monthly.iloc[0]
    assert row["import_kwh"] == pytest.approx(120.0)
    assert row["bill_credit"] == pytest.approx(5.0)
    expected_energy = 120.0 * 0.10
    assert row["bill"] == pytest.approx(expected_energy - 5.0)


# --------------------------------------------------------------------------- #
# 8. RTW import rate with cap
# --------------------------------------------------------------------------- #
def test_rtw_import_with_cap():
    # 4 intervals (1 hour), built manually to control per-interval prices.
    idx_local = pd.date_range(
        "2024-01-08", periods=4, freq="15min", tz="America/Chicago"
    )
    idx_utc = idx_local.tz_convert("UTC")
    intervals = pd.DataFrame(
        {"import_kwh": [1.0, 1.0, 1.0, 1.0], "export_kwh": [0.0, 0.0, 0.0, 0.0]},
        index=idx_utc,
    )
    prices = pd.Series([0.10, 0.20, 0.05, 0.30], index=idx_utc)

    plan = base_plan(
        energy_rates=[
            EnergyRate(rtw=RtwRate(multiplier=1.0, adder_ckwh=0.0, cap_ckwh=15.0))
        ]
    )
    tdu = flat_tdu()
    result = simulate(plan, intervals, tdu, prices=prices)

    row = result.monthly.iloc[0]
    expected = 0.10 + 0.15 + 0.05 + 0.15  # 0.20 and 0.30 clipped to 0.15 cap
    assert row["energy_cost"] == pytest.approx(expected)
    assert row["bill"] == pytest.approx(expected)
    assert result.uses_rtw


def test_rtw_buyback_with_cap():
    # Capped RTW *export* credit (e.g. Chariot Shine's "RTW up to 25c/kWh"):
    # intervals where the wholesale price exceeds the cap credit at the cap.
    idx_local = pd.date_range(
        "2024-01-08", periods=4, freq="15min", tz="America/Chicago"
    )
    idx_utc = idx_local.tz_convert("UTC")
    intervals = pd.DataFrame(
        {"import_kwh": [0.0, 0.0, 0.0, 0.0], "export_kwh": [1.0, 1.0, 1.0, 1.0]},
        index=idx_utc,
    )
    prices = pd.Series([0.10, 0.60, 0.05, 3.00], index=idx_utc)  # two price spikes

    plan = base_plan(
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        buyback=Buyback(
            kind=BuybackKind.rtw,
            rtw=RtwRate(multiplier=1.0, adder_ckwh=0.0, cap_ckwh=25.0),
        ),
    )
    tdu = flat_tdu()
    result = simulate(plan, intervals, tdu, prices=prices)

    row = result.monthly.iloc[0]
    expected_credit = 0.10 + 0.25 + 0.05 + 0.25  # 0.60 and 3.00 clipped to 0.25
    assert row["credit_earned"] == pytest.approx(expected_credit)
    assert result.uses_rtw


def test_rtw_missing_prices_raises():
    intervals = make_intervals("2024-01-08", days=1, import_kwh=1.0, export_kwh=0.0)
    plan = base_plan(energy_rates=[EnergyRate(rtw=RtwRate())])
    tdu = flat_tdu()
    with pytest.raises(ValueError):
        simulate(plan, intervals, tdu, prices=None)


# --------------------------------------------------------------------------- #
# 9. Windows buyback (time-of-use export)
# --------------------------------------------------------------------------- #
def test_windows_buyback():
    intervals = make_intervals("2024-01-08", days=1, import_kwh=0.0, export_kwh=1.0)
    plan = base_plan(
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        buyback=Buyback(
            kind=BuybackKind.windows,
            rates=[
                EnergyRate(label="peak", rate_ckwh=20.0, window=RateWindow(hours=[17, 18])),
                EnergyRate(label="offpeak", rate_ckwh=5.0),
            ],
            offset_scope="energy_only",
            rollover=True,
        ),
    )
    tdu = flat_tdu()
    result = simulate(plan, intervals, tdu)

    row = result.monthly.iloc[0]
    peak_intervals = 2 * 4  # hours 17,18 -> 8 intervals
    offpeak_intervals = 96 - peak_intervals
    expected_credit = peak_intervals * 1.0 * 0.20 + offpeak_intervals * 1.0 * 0.05
    assert row["credit_earned"] == pytest.approx(expected_credit)
    # energy_cost is 0 (no import), offset_scope=energy_only -> offsettable=0
    assert row["energy_cost"] == pytest.approx(0.0)
    assert row["credit_used"] == pytest.approx(0.0)
    assert row["bill"] == pytest.approx(0.0)
    assert row["rollover_out"] == pytest.approx(expected_credit)
    assert result.final_rollover_balance == pytest.approx(expected_credit)


# --------------------------------------------------------------------------- #
# 10. cash_out -> negative bill
# --------------------------------------------------------------------------- #
def test_cash_out_negative_bill():
    intervals = make_intervals("2024-01-08", days=1, import_kwh=0.2, export_kwh=1.0)
    plan = base_plan(
        energy_rates=[EnergyRate(rate_ckwh=10.0)],
        buyback=Buyback(kind=BuybackKind.fixed, rate_ckwh=50.0, cash_out=True),
    )
    tdu = flat_tdu()
    result = simulate(plan, intervals, tdu)

    row = result.monthly.iloc[0]
    energy_cost = 96 * 0.2 * 0.10  # 1.92
    credit_earned = 96 * 1.0 * 0.50  # 48.0
    expected_bill = energy_cost - credit_earned
    assert row["bill"] == pytest.approx(expected_bill)
    assert row["bill"] < 0
    assert row["rollover_out"] == pytest.approx(0.0)
    assert result.final_rollover_balance == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# rank()
# --------------------------------------------------------------------------- #
def test_rank_orders_ascending_and_skips_failures():
    intervals = make_intervals("2024-01-08", days=1, import_kwh=1.0, export_kwh=0.0)
    tdu = flat_tdu()
    cheap = base_plan(id="cheap", energy_rates=[EnergyRate(rate_ckwh=5.0)])
    pricey = base_plan(id="pricey", energy_rates=[EnergyRate(rate_ckwh=20.0)])
    broken = base_plan(id="broken", energy_rates=[EnergyRate(rtw=RtwRate())])

    results = rank([pricey, broken, cheap], intervals, tdu, prices=None)

    ids = [r.plan_id for r in results]
    assert ids == ["cheap", "pricey"]
    assert len(results.warnings) == 1
    assert "broken" in results.warnings[0]


# --------------------------------------------------------------------------- #
# Integration test against the real SMT CSV (skips if not present).
# --------------------------------------------------------------------------- #
def _load_smt_csv_minimal(path: Path) -> pd.DataFrame:
    """Minimal inline SMT CSV loader (ARCHITECTURE.md §4), used only if
    energyanalyzer.ingest.smt isn't importable yet.

    The file lists, per day, a chronological block of Consumption rows
    followed by a chronological block of Surplus Generation rows. On the
    fall-back day the 01:00-01:45 local times repeat (100 rows/channel
    instead of 96): the first pass through those naive timestamps is the
    DST (fold=0) instant, the second pass is standard time (fold=1) per
    ARCHITECTURE.md §4. We resolve that per-channel via cumcount on the
    naive timestamp (order-preserving, since the file is chronological)
    rather than pandas' 'infer', which fails once duplicate rows for two
    different channels get mixed.
    """
    raw = pd.read_csv(path, dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    raw["ESIID"] = raw["ESIID"].str.lstrip("'")
    raw["kwh"] = raw["USAGE_KWH"].astype(float)
    raw["_naive"] = pd.to_datetime(
        raw["USAGE_DATE"] + " " + raw["USAGE_START_TIME"], format="%m/%d/%Y %H:%M"
    )

    channel_cols = {"Consumption": "import_kwh", "Surplus Generation": "export_kwh"}
    series_by_col = {}
    for channel, col in channel_cols.items():
        sub = raw[raw["CONSUMPTION_SURPLUSGENERATION"] == channel].copy()
        occurrence = sub.groupby("_naive").cumcount()
        is_dst = (occurrence == 0).to_numpy()  # first pass = DST (fold 0)
        local_idx = pd.DatetimeIndex(sub["_naive"]).tz_localize(
            "America/Chicago", ambiguous=is_dst, nonexistent="shift_forward"
        )
        utc_idx = local_idx.tz_convert("UTC")
        series_by_col[col] = (
            pd.Series(sub["kwh"].to_numpy(), index=utc_idx).groupby(level=0).sum()
        )

    out = pd.DataFrame(series_by_col).fillna(0.0).sort_index()
    out.index.name = "ts"
    return out


def _load_intervals_for_integration_test() -> pd.DataFrame:
    try:
        from energyanalyzer.ingest.smt import load_intervals  # type: ignore

        return load_intervals(BENCHMARK_CSV)
    except Exception:
        return _load_smt_csv_minimal(BENCHMARK_CSV)


def _benchmark_tdu():
    """Oncor tariff pinned in the benchmark archive; use the latest record for
    all 12 forward-looking months (same rule as plans_io.current_tdu, but read
    from the frozen archive so a new live tariff can't move the benchmark)."""
    import yaml

    from energyanalyzer.core.models import TduTariff

    raw = yaml.safe_load(BENCHMARK_TDU_YAML.read_text())
    tariffs = sorted(
        (TduTariff.model_validate(r) for r in raw["tariffs"]), key=lambda t: t.effective
    )
    return tariffs[-1]


@pytest.mark.skipif(
    not BENCHMARK_CSV.exists(),
    reason="benchmark interval CSV not present (private, gitignored)",
)
def test_integration_report_benchmarks():
    """Validate simulate() against the report benchmarks in ARCHITECTURE.md §1,
    using the real SMT interval data.

    Two groups, both hard-checked here, but with different failure handling:

    1. Plans with no time-of-use energy window (flat rate or 1:1/fixed
       buyback): pulse_current, txu_solar_bb_system_flex,
       gexa_solar_buyback_12. These match the report to within a few cents
       to a couple dollars, which strongly validates the engine's core §6
       arithmetic (energy_cost/tdu/buyback/offset math) end-to-end against
       real data -- these are hard assertions.

    2. Free-night plans: reproducing the report requires that some REPs'
       "free" hours waive TDU delivery charges too, not just the REP energy
       charge (Green Mtn / Reliant / Direct), while TXU's free nights still
       incur TDU. The schema expresses this via `EnergyRate.tdu_exempt`
       (set on the free windows of those three seed plans), and §6 charges
       TDU volumetric only on import matched by non-exempt rates. With that
       flag, gmtn computes ~$1264.87 vs report $1265 and reliant ~$1330.15
       vs $1330; direct lands ~$1276.64 vs $1244 (within its wider
       tolerance -- its exact free-window hours are an assumption), and
       txu_free_nights (no flag) ~$1887.67 vs $1907.
    """
    from energyanalyzer.core.plans_io import load_plans

    intervals = _load_intervals_for_integration_test()
    tdu = _benchmark_tdu()
    plans = {p.id: p for p in load_plans(BENCHMARK_PLANS_DIR)}

    strict_benchmarks = {
        "pulse_current": (1031, 15),
        "txu_solar_bb_system_flex": (1211, 15),
        "gexa_solar_buyback_12": (1613, 15),
        "gmtn_pollution_free_nights_24": (1265, 15),
        "reliant_free_overnight_12": (1330, 15),
        "direct_twelve_hour_power_24": (1244, 60),
        "txu_free_nights_cool_summer_12": (1907, 60),
    }
    reported_only_benchmarks: dict[str, tuple[int, int]] = {}

    print("\nplan_id, computed, expected, tolerance, within_tolerance")
    failures = []
    for plan_id, (expected, tol) in strict_benchmarks.items():
        result = simulate(plans[plan_id], intervals, tdu, prices=None)
        ok = abs(result.first_year_net - expected) <= tol
        print(f"{plan_id}: ${result.first_year_net:.2f} vs ${expected} +/-{tol}  {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(
                f"{plan_id}: computed=${result.first_year_net:.2f} expected=${expected} +/-{tol}"
            )

    for plan_id, (expected, tol) in reported_only_benchmarks.items():
        result = simulate(plans[plan_id], intervals, tdu, prices=None)
        ok = abs(result.first_year_net - expected) <= tol
        print(f"{plan_id}: ${result.first_year_net:.2f} vs ${expected} +/-{tol}  {'OK' if ok else 'MISMATCH (see docstring)'}")

    if failures:
        pytest.fail("core engine benchmark mismatch:\n" + "\n".join(failures))


def test_rank_refuses_a_plan_whose_every_rate_is_zero():
    """0c/kWh is a failed parse, and it is the one wrong answer that always
    sorts first.

    The EFL parser defaults to 0.0 when it cannot find an Energy Charge, which
    is what happens on usage-tiered plans (TXU e-Saver, Saver's Choice, Ambit
    Lone Star Plus) since the schema cannot express tiers. Promoted, those took
    the top 6 slots of the ranking as free electricity. needs_review cannot
    catch it -- "Promote all drafts" deliberately bypasses that gate -- so the
    refusal lives here.
    """
    from energyanalyzer.core.models import EnergyRate, Plan
    from energyanalyzer.engine.cost import rank

    def mk(pid, rate):
        return Plan(
            id=pid, retailer="R", name=pid, term_months=12,
            energy_rates=[EnergyRate(label="", rate_ckwh=rate, window=None)],
        )

    intervals = make_intervals("2024-03-01", 2, 0.5, 0.0)
    results = rank([mk("free", 0.0), mk("real", 12.0)], intervals, flat_tdu(), None)

    assert [r.plan_id for r in results] == ["real"]
    assert any("0c/kWh" in w and "free" in w for w in results.warnings)


def test_rank_still_simulates_an_rtw_indexed_import_rate():
    """An RTW import rate leaves rate_ckwh unset, which must not read as zero."""
    from energyanalyzer.core.models import EnergyRate, Plan, RtwRate
    from energyanalyzer.engine.cost import rank

    plan = Plan(
        id="rtw_import", retailer="R", name="RTW", term_months=12,
        energy_rates=[EnergyRate(label="", rtw=RtwRate(multiplier=1.0, adder_ckwh=3.0), window=None)],
    )
    intervals = make_intervals("2024-03-01", 2, 0.5, 0.0)
    prices = pd.Series(0.05, index=intervals.index)
    results = rank([plan], intervals, flat_tdu(), prices)
    assert [r.plan_id for r in results] == ["rtw_import"]


def test_a_mandatory_signup_fee_lands_in_the_first_year_total_once():
    """Just Energy's family sells six 5-month "Sustainable/Bundle" plans whose
    EFL requires a one-time $49.99 GoodBundle carbon-offset purchase to enroll.
    They price energy at 4.9c/kWh and ranked #7 and #8 on that alone, so leaving
    a mandatory fee out flattered them against plans that have none.

    It belongs in the first-year total exactly once, and never in a monthly
    bill -- the monthly frame has to stay a faithful picture of the recurring
    charge.
    """
    intervals = make_intervals("2024-01-08", days=2, import_kwh=1.0, export_kwh=0.0)
    tdu = TduTariff(effective=dt.date(2024, 1, 1), fixed_usd_month=0.0, volumetric_ckwh=0.0)
    base = dict(
        id="p", retailer="R", name="N", term_months=5,
        energy_rates=[EnergyRate(rate_ckwh=10.0)], tdu_passthrough=False,
    )
    free = simulate(Plan(**base), intervals, tdu)
    paid = simulate(Plan(**base, signup_fee_usd=49.99), intervals, tdu)

    assert paid.first_year_net == pytest.approx(free.first_year_net + 49.99)
    assert paid.monthly["bill"].sum() == pytest.approx(free.monthly["bill"].sum()), (
        "a one-off must not be smeared across the monthly bills"
    )


def test_no_signup_fee_changes_nothing():
    intervals = make_intervals("2024-01-08", days=2, import_kwh=1.0, export_kwh=0.0)
    tdu = TduTariff(effective=dt.date(2024, 1, 1), fixed_usd_month=0.0, volumetric_ckwh=0.0)
    plan = Plan(id="p", retailer="R", name="N", term_months=12,
                energy_rates=[EnergyRate(rate_ckwh=10.0)], tdu_passthrough=False)
    assert plan.signup_fee_usd == 0.0
    assert simulate(plan, intervals, tdu).first_year_net == pytest.approx(
        float(simulate(plan, intervals, tdu).monthly["bill"].sum())
    )


def test_an_unpriceable_plan_is_refused_rather_than_guessed_at():
    """A plan whose shape the schema cannot hold must not be ranked at all.

    needs_review is not enough on its own: "Promote all drafts" bypasses it
    deliberately. Direct Apartment 12 is usage-tiered (8.8798c to 1000 kWh,
    10.8798c above) and kept its FIRST tier -- a rate that looks cheap and ranks
    high. Whether the parser happens to grab the cheap tier or the dear one is
    luck; a wrong number that sorts well is worse than no number.
    """
    intervals = make_intervals("2024-01-08", days=2, import_kwh=1.0, export_kwh=0.0)
    tdu = TduTariff(effective=dt.date(2024, 1, 1), fixed_usd_month=0.0, volumetric_ckwh=0.0)
    ok = Plan(id="ok", retailer="R", name="Fine", term_months=12,
              energy_rates=[EnergyRate(rate_ckwh=10.0)], tdu_passthrough=False)
    tiered = Plan(id="tiered", retailer="R", name="Tiered", term_months=12,
                  energy_rates=[EnergyRate(rate_ckwh=8.8798)], tdu_passthrough=False,
                  unpriceable_reason="usage-tiered energy charge (0-1000 kWh @ 8.8798c, "
                                     ">1000 kWh @ 10.8798c)")

    results = rank([ok, tiered], intervals, tdu)

    assert [r.plan_id for r in results] == ["ok"]
    assert any("tiered" in w and "usage-tiered" in w for w in results.warnings)


# --------------------------------------------------------------------------- #
# Monthly fixed charges prorate across partial calendar months
# --------------------------------------------------------------------------- #
def test_fixed_charges_prorate_over_a_non_calendar_aligned_year():
    """365 days that don't start on the 1st span 13 calendar months, but must
    still cost exactly 12 months of base + TDU fixed fees.

    Charging all 13 in full doesn't just inflate the total -- the error scales
    with the plan's base charge, so it reorders the ranking: a $19.95/mo plan
    would absorb four times the phantom cost of a $4.95/mo one.
    """
    intervals = make_intervals("2025-10-15", days=365, import_kwh=0.3, export_kwh=0.0)
    tdu = flat_tdu(fixed=10.0, volumetric_ckwh=5.0)
    plan = base_plan(base_charge_usd=19.95, tdu_passthrough=True)

    result = simulate(plan, intervals, tdu)

    assert len(result.monthly) == 13  # Oct 2025 .. Oct 2026, both ends partial
    assert result.monthly["base"].sum() == pytest.approx(12 * 19.95)
    # TDU's fixed component prorates identically (volumetric is per-kWh, so it
    # is unaffected and excluded here).
    fixed_tdu = result.monthly["tdu"].sum() - 0.05 * result.monthly["import_kwh"].sum()
    assert fixed_tdu == pytest.approx(12 * 10.0)
    # The two partial end months are the only ones scored below a full month.
    assert result.monthly["coverage"].iloc[0] < 1.0
    assert result.monthly["coverage"].iloc[-1] < 1.0
    assert (result.monthly["coverage"].iloc[1:-1] == 1.0).all()


def test_whole_calendar_months_are_charged_in_full():
    """The proration above must be a no-op on calendar-aligned data."""
    intervals = make_intervals("2025-01-01", days=31 + 28, import_kwh=0.3, export_kwh=0.0)
    plan = base_plan(base_charge_usd=9.95, tdu_passthrough=True)

    result = simulate(plan, intervals, flat_tdu(fixed=10.0, volumetric_ckwh=5.0))

    assert (result.monthly["coverage"] == 1.0).all()
    assert result.monthly["base"].sum() == pytest.approx(2 * 9.95)
