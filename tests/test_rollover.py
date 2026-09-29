"""Short contracts priced past their term at the retailer's published variable
rate (engine.rollover, rank(variable_rates=...))."""

from __future__ import annotations

import datetime as dt

import pytest

from energyanalyzer.core.models import EnergyRate, RateWindow, VariableRateHistory
from energyanalyzer.core.plans_io import load_variable_rates
from energyanalyzer.engine.cost import rank, simulate
from energyanalyzer.engine.rollover import apply_contract_rollover, rolls_over

from test_engine import base_plan, flat_tdu, make_intervals


def _history(**tdu) -> VariableRateHistory:
    return VariableRateHistory(
        retailers=["Southern Federal Power", "Ranchero Power"],
        tdu={k: [{"effective": d, "energy_ckwh": c} for d, c in v] for k, v in tdu.items()},
    )


# Three whole months, Dec 2023 - Feb 2024: no DST change inside.
def _three_months():
    return make_intervals("2023-12-01", days=31 + 31 + 29, import_kwh=1.0, export_kwh=0.0)


def _monthly_energy(plan, intervals):
    return simulate(plan, intervals, flat_tdu(fixed=0.0, volumetric_ckwh=0.0)).monthly.set_index("month")[
        "energy_cost"
    ]


HIST = _history(
    ONCOR=[
        (dt.date(2023, 11, 15), 12.0),  # in effect mid-Dec and mid-Jan
        (dt.date(2024, 1, 20), 14.0),  # after Jan 15 -> not yet in effect for Jan
        (dt.date(2024, 2, 10), 13.0),  # in effect for Feb
    ]
)


def test_one_month_teaser_then_published_rate():
    """Ranchero's shape: the advertised rate for month one only."""
    iv = _three_months()
    plan = base_plan(retailer="Ranchero Power", term_months=1, tdu="ONCOR", energy_rates=[EnergyRate(rate_ckwh=2.0)])
    rolled, note = apply_contract_rollover(plan, iv, HIST)
    e = _monthly_energy(rolled, iv)
    assert e["2023-12"] == pytest.approx(31 * 96 * 0.02)  # contract month
    assert e["2024-01"] == pytest.approx(31 * 96 * 0.12)  # the Jan 20 change isn't in effect mid-Jan
    assert e["2024-02"] == pytest.approx(29 * 96 * 0.13)
    assert "after month 1" in note and "12.00-13.00" in note


def test_contract_free_window_survives_only_inside_the_contract():
    """A free-nights rule keeps its hours and TDU relief for the contract months,
    and does not follow the customer onto the variable product."""
    iv = _three_months()
    night = RateWindow(hours=[0, 1, 2, 3, 4, 5])
    plan = base_plan(
        retailer="Southern Federal Power LLC",
        term_months=2,
        tdu="ONCOR",
        energy_rates=[EnergyRate(rate_ckwh=0.0, window=night, tdu_exempt=True), EnergyRate(rate_ckwh=8.0)],
    )
    rolled, _ = apply_contract_rollover(plan, iv, HIST)
    free = rolled.energy_rates[0]
    assert free.window.hours == [0, 1, 2, 3, 4, 5] and free.window.months == [1, 12] and free.tdu_exempt
    e = _monthly_energy(rolled, iv)
    assert e["2024-01"] == pytest.approx(31 * 72 * 0.08)  # 18 paid hours/day
    assert e["2024-02"] == pytest.approx(29 * 96 * 0.13)  # every hour at the variable rate


def test_rank_prices_short_plans_and_flags_the_ones_it_cannot():
    iv = _three_months()
    teaser = base_plan(id="ranchero", retailer="Ranchero Power", term_months=1, tdu="ONCOR", energy_rates=[EnergyRate(rate_ckwh=2.0)])
    unknown = base_plan(id="unknown", retailer="Nobody Energy", term_months=1, tdu="ONCOR", energy_rates=[EnergyRate(rate_ckwh=2.0)])
    year = base_plan(id="year", retailer="Ranchero Power", term_months=12, tdu="ONCOR", energy_rates=[EnergyRate(rate_ckwh=8.5)])
    res = {r.plan_id: r for r in rank([teaser, unknown, year], iv, flat_tdu(), variable_rates=[HIST])}
    assert res["ranchero"].rollover == "history"
    assert res["unknown"].rollover == "assumed" and "best case" in res["unknown"].rollover_note
    assert res["year"].rollover is None
    # The teaser no longer out-prices the 12-month plan once month 2+ is real.
    assert res["ranchero"].first_year_net > res["year"].first_year_net
    # ...but the same plan with no histories passed is billed as before.
    plain = rank([teaser], iv, flat_tdu())[0]
    assert plain.rollover is None and plain.first_year_net < res["ranchero"].first_year_net


def test_indexed_energy_is_not_a_teaser():
    from energyanalyzer.core.models import RtwRate

    plan = base_plan(term_months=1, energy_rates=[EnergyRate(rtw=RtwRate(multiplier=1.0))])
    assert not rolls_over(plan)


def test_retailer_matching_ignores_legal_suffixes():
    assert HIST.matches("Southern Federal Power LLC")
    assert HIST.matches("Ranchero Power")
    assert not HIST.matches("Spark Energy LLC")
    assert HIST.rate_on("oncor", dt.date(2020, 1, 1)) == 12.0  # before history: earliest
    assert HIST.rate_on("CENTERPOINT", dt.date(2024, 3, 15)) is None


def test_the_tracked_southern_federal_history_loads():
    histories = load_variable_rates()
    sofed = next(h for h in histories if h.matches("Ranchero Power"))
    # SoFed published no March 2026 row; February's rate stays in effect.
    assert sofed.rate_on("ONCOR", dt.date(2026, 3, 15)) == pytest.approx(13.8873)
    assert sofed.rate_on("ONCOR", dt.date(2026, 9, 15)) == pytest.approx(12.4695)
