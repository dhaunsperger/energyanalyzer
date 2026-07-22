"""Tests for the static (no-LLM) EFL parser. ARCHITECTURE.md Sec 8."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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


# --------------------------------------------------------------------------- #
# Real Texas EFL corpus regression tests (tests/fixtures/efl_texts/real/*.txt)
# --------------------------------------------------------------------------- #
# Ground truth below was hand-verified by reading each `pdftotext -layout`
# dump (see the "components" table each EFL discloses). Oncor's standard TDU
# tariff throughout this July-2026 batch is $4.06/month + 6.1196c/kWh, quoted
# by each REP under a variety of labels; that figure is not asserted directly
# here since it is not stored on the Plan schema (see core/models.py -- the
# engine looks up TDU tariffs from tdu/oncor.yaml, not from the EFL parse).
def _rate_pairs(draft: DraftPlan) -> list[tuple[float, Optional[dict]]]:
    return [(r["rate_ckwh"], r["window"]) for r in draft.plan_dict["energy_rates"]]


class TestCorpusAeTexasSmartSecure36:
    """AE Texas Smart Secure 36 (36mo fixed, Oncor). pdftotext -layout mangles
    this particular PDF's font: a handful of glyphs (only B, capital E, s, b,
    y) come out as invisible Unicode Private-Use-Area codepoints instead of
    their real letters, so "Energy Charge" reads as "\\ue001nerg\\ue006e
    Charge" etc. -- but the literal word "Charge" itself is never one of the
    corrupted letters, so the generic '<label>Charge ... per <unit>'
    fallback scan still finds: Energy Charge $0.0649/kWh, Base Charge $0.00,
    TDU Delivery Charge $4.23/mo + 6.1196c/kWh (all pass-through, not
    bundled). Confidence lands below 0.8 (fallback layer) so needs_review is
    correctly True -- a human should double check the corrupted-source
    numbers even though they happen to be right here.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("AE_TEXAS_Smart_Secure_36_34636.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(6.49)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 36

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        # corrupted-font PDF -> low-confidence fallback extraction path
        assert draft.plan_dict["needs_review"] is True

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusApGasTrueClassic11:
    """AP Gas & Electric TrueClassic 11 (11mo fixed, Oncor): numbered-list
    style disclosure -- "1) Energy Rate (c) per kWh: 6.274c", "2) Base
    Charge ($) per month: $0.00", "3) Energy Delivery Charges: 6.1196c per
    kWh and $4.06 per month" (combined TDU line)."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("AP_GAS_ELECTRIC_TX_LLC_TrueClassic_11_33631.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(6.274)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 11

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusApGasTrueClassic36:
    """Same AP Gas & Electric TrueClassic numbered-list style, 36mo term,
    different (higher) rate/ETF."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("AP_GAS_ELECTRIC_TX_LLC_TrueClassic_36_33453.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(7.174)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 36

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusAbundanceConfidentRenter12:
    """Abundance Energy Confident Renter 12 (12mo fixed, Oncor): standard
    'Energy Charge 6.22c Per kWh (c)' / 'Base Charge $0.00 Per Billing Cycle
    ($)' layout. The disclosure-chart line ("Does REP purchase excess
    distributed renewable generation? Yes, for solar buyback plans only...
    please inquire for more details") reads as a marketing disclaimer, not a
    genuinely unresolved rate -- hedge-language detection in
    _extract_buyback() resolves this confidently as "no rate disclosed"
    rather than flagging it as low-confidence."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("Abundance_Energy_Confident_Renter_12_34695.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(6.22)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 12

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        # hedge-language disclaimer confidently resolved as no stated rate
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusBkvDaisy11:
    """BKV Energy Daisy 11 (11mo fixed, Oncor): 'Energy Charge: 7.034c per
    kWh' / 'Base Charge: $0 per month' plain layout."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("BKV_Energy_Daisy_11_34777.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(7.034)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 11

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusBkvDaisy13:
    """BKV Energy Daisy 13 (13mo fixed, Oncor): same layout, different rate."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("BKV_Energy_Daisy_13_34683.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(7.216)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 13

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusBudgetPowerNoGimmicks12:
    """Budget Power No Gimmicks 12 (12mo fixed, Oncor): 'Fixed Energy Charge
    5.512c per kWh' / 'Base Charge $0 per billing cycle'."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("Budget_Power_No_Gimmicks_12_34399.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(5.512)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 12

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusBudgetPowerNoGimmicks24:
    """Budget Power No Gimmicks 24 (24mo fixed, Oncor): same layout, higher
    rate."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("Budget_Power_No_Gimmicks_24_34398.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(6.212)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 24

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusChariotBrightNights12:
    """Chariot Energy Bright Nights 12 (12mo fixed, Oncor): brand-prefixed
    TOU table -- 'Chariot Energy Daytime Energy Charge 6.78c per kWh' +
    'Chariot Energy Bright Nights Energy Charge 0c per kWh' +
    'Chariot Energy Base Monthly Charge $9.95 per billing cycle' + 'Oncor
    Delivery Charges $4.06 per billing cycle' / '6.1196c per kWh'. The EFL
    separately states "Bright Nights hours are 11:00 PM to 06:00 AM." so the
    free-night window is [23,0,1,2,3,4,5] (11pm-6am), not the generic
    assumed 9pm-6am fallback. This is a heuristic multi-tier reconstruction
    (arbitrary brand-prefixed labels, not the standard On/Off-Peak
    vocabulary) so confidence is kept below 0.8 and needs_review is True."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("CHARIOT_ENERGY_Bright_Nights_12_34727.txt")

    def test_energy_rates_two_tier(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 2
        night_rate, day_rate = rates
        assert night_rate[0] == pytest.approx(0.0)
        assert night_rate[1] == {"hours": [23, 0, 1, 2, 3, 4, 5]}
        assert day_rate[0] == pytest.approx(6.78)
        assert day_rate[1] is None  # catch-all default must be last

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(9.95)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 12

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        # heuristic brand-prefixed multi-tier reconstruction -> flagged
        assert draft.plan_dict["needs_review"] is True

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusChariotGridPlus12:
    """Chariot Energy GridPlus 12 (12mo fixed, Oncor): usage-tiered bill
    credit -- 'Chariot Energy Residential Usage Credit $125 per billing
    cycle when usage >=1000 kWh'. This word order (amount before the
    'when usage >=' clause, no 'credit of' preamble) previously fell
    through _extract_bill_credits entirely, silently leaving bill_credits
    empty for a plan whose real economics depend on it."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("CHARIOT_ENERGY_GridPlus_12.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(13.22)
        assert rates[0][1] is None

    def test_bill_credit_tier(self, draft):
        credits = draft.plan_dict["bill_credits"]
        assert len(credits) == 1
        assert credits[0]["min_kwh"] == pytest.approx(1000.0)
        assert credits[0]["max_kwh"] is None
        assert credits[0]["credit_usd"] == pytest.approx(125.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 12

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusConstellationAcProtectPlus2Units:
    """Constellation 12 Month A/C Protect Plus for 2 Units (12mo fixed,
    Oncor): stacking two-tier usage credit -- 'Residential Usage Credit
    35.00 $ per bill month if usage >= 1000kWh' plus a second, additive
    line 'Additional Residential Usage Credit 15.00 $ per bill month if
    usage >= 2000kWh'. Both value/$ order (value before '$') and the 'if'
    (not 'when') connector differ from the other real-corpus phrasings
    already covered. Both tiers are open-ended (max_kwh=None): cost.py
    sums every bill_credits row a month's usage clears, so >=2000kWh
    months correctly get $35+$15=$50, not a replacement of the first
    tier."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("CONSTELLATION_NEWENERGY_INC_12_Month_A_C_Protect_Plus_for_2_Units.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(12.27)
        assert rates[0][1] is None

    def test_bill_credit_tiers_stack(self, draft):
        credits = draft.plan_dict["bill_credits"]
        assert len(credits) == 2
        low, high = credits
        assert low["min_kwh"] == pytest.approx(1000.0)
        assert low["max_kwh"] is None
        assert low["credit_usd"] == pytest.approx(35.0)
        assert high["min_kwh"] == pytest.approx(2000.0)
        assert high["max_kwh"] is None
        assert high["credit_usd"] == pytest.approx(15.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 12

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusEnergyTexasNoBull12:
    """Energy Texas No Bull 12 (12mo fixed, Oncor): 'Energy Charge: 6.024c
    per kWh' / 'Base Charge: $0 per month' plain layout."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("Energy_Texas_No_Bull_12_34904.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(6.024)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 12

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusJustEnergyBasicsPtc24:
    """Just Energy Basics PTC 24 (24mo fixed, Oncor): bullet-form disclosure
    -- '• Energy Charge: 9.4c/kWh' and '• Pass-Through TDSP
    Distribution Charge: 6.1196c/kWh' / '• Pass-Through TDSP Customer
    Charge: $4.06 per month'. Texas EFLs must itemize every price
    component in this list; since it has an Energy Charge line and only
    TDSP-marked lines besides, _extract_base_charge_absent_from_itemized_list
    confidently resolves base_charge_usd to 0.0 rather than flagging it as
    a low-confidence guess."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("JUST_ENERGY_Basics_PTC_24_33606.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(9.4)
        assert rates[0][1] is None

    def test_base_charge_defaults_to_zero(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 24

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusProntoPower:
    """Pronto Power (Summer Energy LLC dba Pronto Power), Power To Choose /
    prepaid variable plan, 1-month term. No labeled 'Energy Charge' line at
    all -- the rate is only stated in prose ("included in variable rate of
    17.9 cents") and via a flat repeated avg-price table row (ONCOR 17.9c
    17.9c 17.9c 17.9c). "Daily Customer Fee (DCF) $0.39 cents per day" is
    converted to an approximate monthly base charge: 0.39 * 365 / 12 =
    11.8625 -> rounded to $11.86. "TDSP recurring (pass-through) charges:
    $0, included in variable rate of 17.9 cents" means delivery is bundled
    into the energy rate, so tdu_passthrough is False."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("PRONTO_POWER_Power_To_Choose_33671.txt")

    def test_energy_rate_variable(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(17.9)
        assert rates[0][1] is None
        assert draft.plan_dict["rate_type"] == "variable"

    def test_base_charge_derived_from_daily_fee(self, draft):
        # $0.39/day * 365 / 12, rounded to cents
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(11.86)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 1

    def test_tdu_bundled(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is False

    def test_needs_review(self, draft):
        # prepaid/variable plan with several low-confidence derived fields
        assert draft.plan_dict["needs_review"] is True

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusThinkEnergyThinkClean12:
    """Think Energy Think Clean 12 (12mo fixed, Oncor): 'Energy Charge 7.8c
    Per kWh (c)' / 'Base Charge $4.95 Per Billing Cycle ($)'."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("Think_Energy_Think_Clean_12_34475.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(7.8)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(4.95)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 12

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusThinkEnergyThinkClean12Thermostat:
    """Think Energy Think Clean 12 with Smart Thermostat Connected variant:
    same base layout plus a 'Think Smart Credit $10.00 Per Billing Cycle'
    line (a flat monthly bill credit, not a usage-tier bill credit, so it's
    intentionally not picked up by the usage-tier bill_credits extractor)."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("Think_Energy_Think_Clean_12_with_Smart_Thermostat_Connected_34706.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(7.8)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(4.95)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 12

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusThinkEnergyThinkClean24:
    """Think Energy Think Clean 24 (24mo fixed, Oncor): same layout, higher
    rate."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("Think_Energy_Think_Clean_24_34491.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(8.4)
        assert rates[0][1] is None

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(4.95)

    def test_term(self, draft):
        assert draft.plan_dict["term_months"] == 24

    def test_tdu_passthrough(self, draft):
        assert draft.plan_dict["tdu_passthrough"] is True

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusSfeRewardsPlus12:
    """SFE RewardsPlus 12 (12mo fixed, Oncor): average-price-table style base
    charge row, e.g. 'Base Charge($ per month) $ 0.00' -- label and value
    share one line with a parenthetical unit descriptor in between."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("sfe_rewardsplus_12.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(6.67)

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_needs_review(self, draft):
        assert draft.plan_dict["needs_review"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusChariotChoice24:
    """Chariot Choice 24 (24mo fixed, Oncor): brand-prefixed table explicitly
    states 'Chariot Energy Base Monthly Charge N/A per billing cycle' --
    an unambiguous statement of no base charge, not a missing value."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("chariot_choice_24.txt")

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(0.0)

    def test_base_charge_confidence_high(self, draft):
        # explicit "N/A" statement, not a silent default-to-zero
        assert draft.confidence["base_charge"] >= 0.8

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusNecCoopPlainAndSimple:
    """NEC Co-op Energy Plain and Simple: pricing given as a 'Charge details
    | Base Charge | Per kWh Charge' table with one row per provider, e.g.
    'NEC Co-op Energy $7.50 9.94c' / 'Delivery Costs - Oncor $4.06
    6.1196c' -- base charge ($) then per-kWh rate (c), retailer row first."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("nec_coop_plain_and_simple.txt")

    def test_energy_rate(self, draft):
        rates = _rate_pairs(draft)
        assert len(rates) == 1
        assert rates[0][0] == pytest.approx(9.94)

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(7.50)

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusHeritagePowerBrightStart24:
    """Heritage Power Bright Start 24 (24mo fixed, two-column disclosure
    chart): 'Do I have a termination fee or any fees\\nYes; $75 One time\\n
    associated with terminating service?' -- the '$75' lands on the line
    *after* the question label, which the old same-line-only ETF regex
    (no newline allowed between label and '$') never crossed, silently
    yielding etf_usd=0 at 0 confidence for a plan that really has a $75
    ETF."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("HERITAGE_POWER_LLC_Bright_Start_24.txt")

    def test_etf(self, draft):
        assert draft.plan_dict["etf_usd"] == pytest.approx(75.0)
        assert draft.plan_dict["etf_per_month_remaining"] is False

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusChariotShine36:
    """Chariot Energy Shine 36 (36mo fixed, Oncor): the disclosure table
    reads 'Chariot Energy Buy Back Rate Real Time Market Pricing per
    kWh' -- a genuine RTW buyback -- but the old _BUYBACK_LABEL regex
    only matched one-word 'Buyback Rate', not this EFL's two-word 'Buy
    Back Rate', so it fell through to the next label match instead:
    'Excess Energy Credit', from an unrelated paragraph describing net
    metering export credits 'capped at 25c per kWh'. The parser then
    misread that 25c cap as a fixed buyback rate. Also verifies the RTW
    detector itself: the old regex required 'real-time' (no space) and
    'market price' (not 'Pricing'), neither of which this EFL's actual
    wording ('Real Time Market Pricing') satisfied."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("CHARIOT_ENERGY_Chariot_Shine_36.txt")

    def test_buyback_is_rtw_not_the_export_credit_cap(self, draft):
        bb = draft.plan_dict["buyback"]
        assert bb["kind"] == "rtw"

    def test_rtw_cap_extracted_from_the_export_credit_paragraph(self, draft):
        # The same "capped at 25c per kWh" sentence that used to be
        # misread as a fixed buyback rate is, correctly, this RTW rate's
        # cap -- extracted from >1000 chars away from the buyback label.
        assert draft.plan_dict["buyback"]["rtw"]["cap_ckwh"] == 25.0

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


def test_corpus_all_real_fixtures_present_and_schema_valid():
    """Sanity check: every PDF-derived .txt fixture under real/ parses to a
    schema-valid Plan (never crashes), regardless of confidence -- this is
    the "genuinely impossible extraction must still be schema-valid +
    needs_review" guarantee from ARCHITECTURE.md Sec 8."""
    real_files = sorted(REAL_FIXTURES.glob("*.txt"))
    assert len(real_files) == 22
    for path in real_files:
        draft = _real_draft(path.name)
        Plan.model_validate(draft.plan_dict)
