"""Tests for the static (no-LLM) EFL parser. ARCHITECTURE.md Sec 8."""

from __future__ import annotations

from pathlib import Path

import pytest

from energyanalyzer.core.models import Plan
from energyanalyzer.eflparse.parser import (
    DraftPlan,
    parse_efl_text,
    parse_time_range,
    save_draft,
)

FIXTURES = Path(__file__).parent / "fixtures" / "efl_texts"
REAL_FIXTURES = FIXTURES / "real"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def _draft(name: str) -> DraftPlan:
    return parse_efl_text(_load(name), source_name=name)


def _real_draft(name: str) -> DraftPlan:
    return parse_efl_text((REAL_FIXTURES / name).read_text(), source_name=name)


# --------------------------------------------------------------------------- #
# parse_time_range helper
# --------------------------------------------------------------------------- #
class TestParseTimeRange:
    def test_night_window_exclusive_hour_boundary(self):
        # "9 p.m. to 6 a.m." -- charges resume AT 6am, so 6 is excluded.
        assert parse_time_range("9 p.m. to 6 a.m.") == [21, 22, 23, 0, 1, 2, 3, 4, 5]

    def test_night_window_inclusive_minute_boundary(self):
        # "9 p.m. and 5:59 a.m." -- explicit minute means hour 5 is included.
        assert parse_time_range("9 p.m. and 5:59 a.m.") == [21, 22, 23, 0, 1, 2, 3, 4, 5]

    def test_daytime_window(self):
        assert parse_time_range("6 a.m. to 5 p.m.") == list(range(6, 17))

    def test_no_times_found(self):
        assert parse_time_range("no times here") == []

    def test_single_time_found_returns_empty(self):
        assert parse_time_range("free after 9 p.m.") == []


# --------------------------------------------------------------------------- #
# Real Pulse Power EFL (tests/fixtures/efl_texts/pulse.txt)
# --------------------------------------------------------------------------- #
class TestPulse:
    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _draft("pulse.txt")

    def test_energy_rate(self, draft):
        rates = draft.plan_dict["energy_rates"]
        assert len(rates) == 1
        assert rates[0]["window"] is None
        assert 15.8 <= rates[0]["rate_ckwh"] <= 15.9

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(4.95)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 36

    def test_etf(self, draft):
        assert draft.plan_dict["etf_usd"] == pytest.approx(20.0)
        assert draft.plan_dict["etf_per_month_remaining"] is True

    def test_buyback_1to1(self, draft):
        bb = draft.plan_dict["buyback"]
        assert bb["kind"] == "fixed"
        assert bb["rate_ckwh"] == pytest.approx(15.8)
        # 1:1 detection: buyback == energy charge
        assert bb["rate_ckwh"] == draft.plan_dict["energy_rates"][0]["rate_ckwh"]

    def test_renewable(self, draft):
        assert draft.plan_dict["renewable_pct"] == pytest.approx(100.0)

    def test_retailer(self, draft):
        assert draft.plan_dict["retailer"] == "Pulse Power"

    def test_confidence_recorded_for_load_bearing_fields(self, draft):
        for key in ("energy_charge", "base_charge", "buyback"):
            assert draft.confidence[key] >= 0.8
            assert key in draft.evidence
            assert draft.evidence[key]

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)

    def test_needs_review_false_when_all_confident(self, draft):
        # every load-bearing field parsed with high confidence
        assert draft.plan_dict["needs_review"] is False


# --------------------------------------------------------------------------- #
# Synthetic fixture: TXU-style solar buyback, non-offsettable base
# --------------------------------------------------------------------------- #
class TestTxuStyleSolar:
    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _draft("txu_style_solar.txt")

    def test_energy_rate(self, draft):
        rates = draft.plan_dict["energy_rates"]
        assert rates[-1]["window"] is None
        assert rates[-1]["rate_ckwh"] == pytest.approx(14.5)

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(9.95)

    def test_buyback_not_1to1_and_energy_only(self, draft):
        bb = draft.plan_dict["buyback"]
        assert bb["kind"] == "fixed"
        assert bb["rate_ckwh"] == pytest.approx(3.0)
        assert bb["rate_ckwh"] != draft.plan_dict["energy_rates"][-1]["rate_ckwh"]
        assert bb["offset_scope"] == "energy_only"

    def test_term_and_type(self, draft):
        assert draft.plan_dict["term_months"] == 12
        assert draft.plan_dict["rate_type"] == "fixed"

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


# --------------------------------------------------------------------------- #
# Synthetic fixture: free-nights plan, "9 p.m. and 5:59 a.m." wording
# --------------------------------------------------------------------------- #
class TestFreeNights59:
    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _draft("freenights_59.txt")

    def test_free_window_and_default_rate(self, draft):
        rates = draft.plan_dict["energy_rates"]
        assert len(rates) == 2
        free_rate = rates[0]
        assert free_rate["rate_ckwh"] == 0.0
        assert free_rate["window"]["hours"] == [21, 22, 23, 0, 1, 2, 3, 4, 5]
        default_rate = rates[-1]
        assert default_rate["window"] is None
        assert default_rate["rate_ckwh"] == pytest.approx(20.6)

    def test_base_charge_and_term(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(4.95)
        assert draft.plan_dict["term_months"] == 12

    def test_free_window_confidence_high(self, draft):
        assert draft.confidence["free_window"] >= 0.8

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


# --------------------------------------------------------------------------- #
# Synthetic fixture: tiered usage bill-credit plan
# --------------------------------------------------------------------------- #
class TestTieredCredit:
    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _draft("tiered_credit.txt")

    def test_energy_rate(self, draft):
        rates = draft.plan_dict["energy_rates"]
        assert rates[-1]["window"] is None
        assert rates[-1]["rate_ckwh"] == pytest.approx(13.2)

    def test_bill_credit_tiers(self, draft):
        credits = draft.plan_dict["bill_credits"]
        assert len(credits) == 2
        low, high = credits
        assert low["min_kwh"] == 1000
        assert low["max_kwh"] == 2000
        assert low["credit_usd"] == pytest.approx(50.0)
        assert high["min_kwh"] == 2000
        assert high["max_kwh"] is None
        assert high["credit_usd"] == pytest.approx(100.0)

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


# --------------------------------------------------------------------------- #
# Synthetic fixture: TOU multi-rate plan
# --------------------------------------------------------------------------- #
class TestTouPlan:
    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _draft("tou_plan.txt")

    def test_three_rate_tiers_default_last(self, draft):
        rates = draft.plan_dict["energy_rates"]
        assert len(rates) == 3
        # catch-all must be last per Plan schema
        assert rates[-1]["window"] is None
        labels = [r["label"] for r in rates]
        assert "on-peak" in labels
        assert "mid-peak" in labels
        assert "off-peak" in labels

    def test_on_peak_window(self, draft):
        rates = {r["label"]: r for r in draft.plan_dict["energy_rates"]}
        on_peak = rates["on-peak"]
        assert on_peak["rate_ckwh"] == pytest.approx(35.7)
        assert on_peak["window"]["hours"] == [17, 18, 19, 20]
        assert on_peak["window"]["weekdays"] == [0, 1, 2, 3, 4]

    def test_off_peak_default_rate(self, draft):
        rates = {r["label"]: r for r in draft.plan_dict["energy_rates"]}
        assert rates["off-peak"]["rate_ckwh"] == pytest.approx(4.1)
        assert rates["off-peak"]["window"] is None

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


# --------------------------------------------------------------------------- #
# Synthetic fixture: bundled-TDU plan (delivery folded into Energy Charge)
# --------------------------------------------------------------------------- #
class TestBundledTdu:
    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _draft("bundled_tdu.txt")

    def test_bundled_flag(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is False

    def test_energy_rate_and_base(self, draft):
        rates = draft.plan_dict["energy_rates"]
        assert rates[-1]["rate_ckwh"] == pytest.approx(12.4)
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 6

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


# --------------------------------------------------------------------------- #
# Negative test: garbage text
# --------------------------------------------------------------------------- #
class TestGarbageText:
    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        garbage = (
            "The quick brown fox jumps over the lazy dog. Lorem ipsum dolor "
            "sit amet, consectetur adipiscing elit. This document contains "
            "no electricity information whatsoever."
        )
        return parse_efl_text(garbage, source_name="garbage.txt")

    def test_low_confidence_on_core_fields(self, draft):
        for key in ("energy_charge", "base_charge", "term_months", "retailer"):
            assert draft.confidence[key] < 0.8

    def test_needs_review_flagged(self, draft):
        assert draft.plan_dict["needs_review"] is True

    def test_unparsed_notes_present(self, draft):
        assert draft.unparsed_notes

    def test_still_schema_valid(self, draft):
        # A parser failure mode should never crash on garbage input; it
        # should degrade to a low-confidence, needs_review draft that is
        # still a structurally valid Plan (falls back to sane defaults).
        plan = Plan.model_validate(draft.plan_dict)
        assert plan.needs_review is True


# --------------------------------------------------------------------------- #
# save_draft()
# --------------------------------------------------------------------------- #
def test_save_draft_writes_parse_metadata(tmp_path):
    draft = _draft("pulse.txt")
    out_path = save_draft(draft, drafts_dir=tmp_path)
    assert out_path.exists()
    assert out_path.parent == tmp_path

    import yaml

    data = yaml.safe_load(out_path.read_text())
    assert "_parse" in data
    assert data["_parse"]["confidence"] == draft.confidence
    assert data["_parse"]["evidence"] == draft.evidence
    assert data["_parse"]["unparsed_notes"] == draft.unparsed_notes
    # the plan-shaped part (everything but _parse) must still validate
    plan_only = {k: v for k, v in data.items() if k != "_parse"}
    Plan.model_validate(plan_only)
