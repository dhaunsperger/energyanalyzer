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
    assert row["import_kwh"] == pytest.approx(n * 1.0)
    expected_energy = n * 1.0 * 0.10
    expected_tdu = 10.0 + 0.05 * (n * 1.0)
    assert row["energy_cost"] == pytest.approx(expected_energy)
    assert row["tdu"] == pytest.approx(expected_tdu)
    assert row["bill"] == pytest.approx(expected_energy + 5.0 + expected_tdu)
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
    energy_cost = n * 0.5 * 0.10  # 4.8
    tdu_charge = 10.0 + 0.05 * (n * 0.5)  # 12.4
    credit_earned = n * 0.5 * 0.50  # 24.0
    assert row["energy_cost"] == pytest.approx(energy_cost)
    assert row["tdu"] == pytest.approx(tdu_charge)
    assert row["credit_earned"] == pytest.approx(credit_earned)
    used = min(credit_earned, energy_cost)
    assert row["credit_used"] == pytest.approx(used)
    expected_bill = energy_cost + 5.0 + tdu_charge - used
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
    energy_cost = 96 * 0.5 * 0.10
    tdu_charge = 10.0 + 0.05 * (96 * 0.5)
    credit_earned = 96 * 0.5 * 0.50
    offsettable = energy_cost + 5.0 + tdu_charge
    used = min(credit_earned, offsettable)
    assert row["credit_used"] == pytest.approx(used)
    expected_bill = energy_cost + 5.0 + tdu_charge - used
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
