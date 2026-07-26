"""Tests for the static (no-LLM) EFL parser. ARCHITECTURE.md Sec 8."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from energyanalyzer.core.models import Plan
from energyanalyzer.eflparse.parser import (
    DraftPlan,
    _defined_weekdays,
    _weekdays_from_snippet,
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
        """A corrupted font is not, by itself, a reason to distrust the numbers.

        This used to assert True: the fallback scan scored 0.6 because the
        LABEL was unreadable, even though the values ("$0.0649 per kWh",
        "$0.00 per billing cycle") are perfectly legible and -- as the other
        assertions here have always checked -- correct. That cost a review on
        every broken-font EFL, and worse, sent them to the LLM, which under the
        assist-only policy pinned them in the queue permanently.

        The label is now matched as a subsequence, so "\\ue001nerg\\ue006 Charge"
        is recognised as an Energy Charge and the read is scored on its merits.
        Audited across all 232 EFLs on disk: the matcher accepts only genuine
        Base/Energy/Monthly Base labels and the PUA-mangled "ae"/"nerg" forms --
        no false matches.
        """
        assert draft.plan_dict["needs_review"] is False
        assert draft.confidence["energy_charge"] >= 0.8
        assert draft.confidence["base_charge"] >= 0.8

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


def test_unresolvable_differing_energy_rows_never_look_confident():
    """Differing energy rows are a schedule; if it can't be resolved, flag it.

    When the qualifiers DO map to day sets the schedule is read properly (see
    test_suffixed_qualifier_rows_become_a_weekend_schedule). When they don't,
    the generic scan takes the first row -- and scoring that confidently would
    auto-promote a flat rate with the other tier silently dropped.
    """
    from energyanalyzer.eflparse.parser import parse_efl_text

    text = (
        "Electricity Facts Label\nAcme Energy\nSome Plan 12\nOncor\n"
        "Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
        "Base Charge $0.00 per billing cycle\n"
        "Energy Charge 17.6000 ¢ per kWh – Tier One\n"
        "Energy Charge 8.0000 ¢ per kWh – Tier Two\n"
        "TDU Delivery Charge $4.06 per billing cycle\n"
        "Contract Term 12 Month(s)\n"
    )
    d = parse_efl_text(text, "acme.pdf")
    assert d.confidence["energy_charge"] < 0.8
    assert d.plan_dict["needs_review"] is True


def test_suffixed_qualifier_rows_become_a_weekend_schedule():
    """A qualifier can TRAIL the rate instead of leading it.

    "Energy Charge 17.6000 ¢ per kWh – Weekdays" / "... 0.0000 ¢ per kWh –
    Weekends" (Frontier, Gexa). The brand-tier reader only sees leading labels,
    so these fell through to the flat-rate reader, which took the first row and
    billed the WEEKDAY rate every day -- the free weekend silently dropped.

    The weekend definition comes from the EFL, not a Sat/Sun assumption: Gexa's
    "Free 3 Day Weekends" defines weekends as Friday to Monday.
    """
    from energyanalyzer.eflparse.parser import parse_efl_text

    def _efl(weekend_defn: str) -> str:
        return (
            "Electricity Facts Label\nAcme Energy\nFree Weekends 12\nOncor\n"
            "Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
            "Base Charge $0.00 per billing cycle\n"
            "Energy Charge 22.9000 ¢ per kWh - Weekdays\n"
            "Energy Charge 0.0000 ¢ per kWh - Weekends\n"
            f"{weekend_defn}\n"
            "TDU Delivery Charge $4.06 per billing cycle\n"
            "Contract Term 12 Month(s)\n"
        )

    d = parse_efl_text(_efl("Weekends is defined as 12:00 AM Saturday to 12:00 AM Monday."), "a.pdf")
    assert d.plan_dict["energy_rates"] == [
        {"label": "Weekends", "rate_ckwh": 0.0, "window": {"weekdays": [5, 6]}},
        {"label": "Weekdays", "rate_ckwh": 22.9, "window": None},
    ]
    assert d.plan_dict["needs_review"] is False

    # A three-day weekend, and the catch-all must remain the PAID rate.
    d3 = parse_efl_text(_efl("Weekends is defined as 12:00 AM Friday to 12:00 AM Monday."), "b.pdf")
    assert d3.plan_dict["energy_rates"][0]["window"] == {"weekdays": [4, 5, 6]}
    assert d3.plan_dict["energy_rates"][-1]["rate_ckwh"] == 22.9


def test_day_definition_does_not_swallow_the_other_keyword():
    """"Weekends" from a rate row must not bind to "Weekdays is defined as".

    These EFLs print the rate rows immediately above the definitions, so the
    text runs "...0.0000 ¢ per kWh - Weekends Weekdays is defined as 12:01 AM
    Monday to 11:59 PM Friday". A permissive gap let the match start at the rate
    row's "Weekends" and return the WEEKDAY range as the weekend definition --
    Frontier's weekend came back as Mon-Fri, Gexa's as Mon-Thu, which would have
    applied the free rate to weekdays and the full rate to the weekend.
    """
    from energyanalyzer.eflparse.parser import _defined_weekdays

    text = (
        "Energy Charge 22.9000 ¢ per kWh - Weekdays "
        "Energy Charge 0.0000 ¢ per kWh - Weekends "
        "Weekdays is defined as 12:01 AM Monday to 11:59 PM Thursday, including holidays. "
        "Weekends is defined as 12:00 AM Friday to 12:00 AM Monday, including holidays."
    )
    assert _defined_weekdays(text, "weekend") == [4, 5, 6]
    assert _defined_weekdays(text, "weekday") == [0, 1, 2, 3]


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
        """Both rates AND the window come from the document, so nothing is
        assumed and there is nothing for a human to resolve.

        This used to assert True at 0.75. The distinction that matters is not
        "was the label brand-prefixed" but "did the EFL state its hours": when
        `_find_night_hours` comes up empty the parser falls back to an ASSUMED
        9pm-6am window and scores 0.5, which still lands in review. Here the EFL
        says "Bright Nights hours are 11:00 PM to 06:00 AM", and the window
        below is exactly that.
        """
        assert draft.plan_dict["needs_review"] is False
        assert draft.confidence["free_window"] >= 0.8

    def test_assumed_window_still_lands_in_review(self, draft):
        """The safety valve: strip the hours sentence and the plan must flag."""
        import re as _re

        text = _re.sub(
            r"Bright Nights hours are[^\n.]*", "", (REAL_FIXTURES / "CHARIOT_ENERGY_Bright_Nights_12_34727.txt").read_text()
        )
        d = parse_efl_text(text, "chariot.txt")
        assert d.confidence["free_window"] < 0.8
        assert d.plan_dict["needs_review"] is True

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
        # This EFL answers "Yes" to "Does the REP purchase excess distributed
        # renewable generation?" but discloses no buyback rate anywhere. The
        # buyback confidence is therefore vetoed down (see
        # _buyback_disclosure_answer) so a human resolves the rate rather than
        # the plan entering the rankings as a confident non-buyback plan.
        assert draft.plan_dict["needs_review"] is True
        assert draft.plan_dict["buyback"]["kind"] == "none"
        assert draft.confidence["buyback"] < 0.8

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


class TestCorpusAmbitSolarBuyback12:
    """Ambit Energy Texas Solar Buyback 12 (Oncor): the price table lists
    'Energy Charge: Per kWh (c) 12.7000c' and 'Buyback Rate: Per kWh (c) 3.5c',
    but the plan TITLE ('...Texas Solar Buyback 12SM') is itself a buyback-label
    match with no rate on its own line, so the old scan fell through to its wide
    context window and read the '17.6c' Average-Price-per-kWh estimate (the 500
    kWh column) as the buyback rate. The fix prefers candidates that disclose a
    rate on the label line itself, so the '3.5c' from the 'Buyback Rate:' line
    wins. Also, the offset-scope prose ('...offset up to 100% of your Energy
    Charges each month (excluding base charge, TDU charges, and all other taxes
    and fees)') sits ~600 chars after the rate, beyond the local window, so
    offset scope is resolved over the full text -> energy_only."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("AMBIT_ENERGY_Texas_Solar_Buyback_12.txt")

    def test_buyback_rate_is_the_labeled_rate_not_the_average_price(self, draft):
        bb = draft.plan_dict["buyback"]
        assert bb["kind"] == "fixed"
        # 3.5c from "Buyback Rate:", NOT 17.6c (the 500 kWh Average Price estimate).
        assert bb["rate_ckwh"] == pytest.approx(3.5)

    def test_buyback_offset_scope_is_energy_only(self, draft):
        # Ambit buyback credits offset Energy Charges only -- not base/TDU/taxes.
        assert draft.plan_dict["buyback"]["offset_scope"] == "energy_only"

    def test_buyback_not_1to1_with_energy_charge(self, draft):
        bb = draft.plan_dict["buyback"]
        energy = draft.plan_dict["energy_rates"][-1]["rate_ckwh"]
        assert bb["rate_ckwh"] != pytest.approx(energy)

    def test_confidence_recorded_for_buyback(self, draft):
        assert draft.confidence["buyback"] >= 0.8
        assert draft.evidence["buyback"]

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusDirectSolarUnlimited12:
    """Direct Energy 'Direct Solar Unlimited 12' (Oncor): a genuine solar
    buyback plan whose credit is labeled 'Solar Grid Credit: 5.3c per kWh' --
    a term the buyback-label vocabulary didn't recognize, so the parser reported
    kind=none. Adding 'Solar Grid Credit' to the labels resolves it to a fixed
    5.3c buyback."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("DIRECT_ENERGY_Direct_Solar_Unlimited_12.txt")

    def test_solar_grid_credit_parsed_as_fixed_buyback(self, draft):
        bb = draft.plan_dict["buyback"]
        assert bb["kind"] == "fixed"
        assert bb["rate_ckwh"] == pytest.approx(5.3)

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestCorpusReliantSolarPaybackMatch12:
    """Reliant 'Solar Payback Match 12' (Oncor): also a 'Solar Grid Credit' plan,
    but its credit is the ERCOT 15-minute Real-Time Settlement Point Price
    (RTSPP), floored at zero -- an RTW buyback, not a fixed rate. The RTSPP
    wording sits ~400 chars after the buyback label, so RTW detection uses a
    wider window than the fixed-rate context (but only specific market signals --
    RTSPP/settlement point/real-time market -- never bare 'real-time' or
    'ERCOT', which appear in unrelated EFL prose/boilerplate)."""

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("RELIANT_Solar_Payback_Match_12.txt")

    def test_buyback_is_rtw_not_fixed(self, draft):
        bb = draft.plan_dict["buyback"]
        assert bb["kind"] == "rtw"
        assert "rate_ckwh" not in bb  # RTW, no fixed rate

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


class TestWeekendDefinition:
    """A plan may DEFINE its own weekend/weekday span (e.g. Gexa 'Free 3 Day
    Weekends': weekends = Fri-Sun, weekdays = Mon-Thu). The parser must read
    that definition rather than assuming the Sat/Sun default -- otherwise
    Friday is billed at the weekday rate instead of free."""

    _DEFN = (
        "Weekdays is defined as 12:01 AM Monday to 11:59 PM Thursday, including holidays. "
        "Weekends is defined as 12:00 AM Friday to 12:00 AM Monday, including holidays."
    )

    def test_weekend_span_from_definition(self):
        # Fri-Sun; the range ends at 12:00 AM Monday, so Monday is excluded.
        assert _defined_weekdays(self._DEFN, "weekend") == [4, 5, 6]

    def test_weekday_span_from_definition(self):
        # Mon-Thu inclusive (ends 11:59 PM Thursday, not midnight).
        assert _defined_weekdays(self._DEFN, "weekday") == [0, 1, 2, 3]

    def test_no_definition_returns_none(self):
        assert _defined_weekdays("no such clause here", "weekend") is None

    def test_snippet_prefers_definition_over_default(self):
        assert _weekdays_from_snippet("free on weekends", self._DEFN) == [4, 5, 6]

    def test_snippet_falls_back_to_sat_sun_without_definition(self):
        assert _weekdays_from_snippet("free on weekends", "") == [5, 6]

    def test_mon_fri_weekday_default_without_definition(self):
        assert _weekdays_from_snippet("weekday rate applies", "") == [0, 1, 2, 3, 4]


def test_corpus_all_real_fixtures_present_and_schema_valid():
    """Sanity check: every PDF-derived .txt fixture under real/ parses to a
    schema-valid Plan (never crashes), regardless of confidence -- this is
    the "genuinely impossible extraction must still be schema-valid +
    needs_review" guarantee from ARCHITECTURE.md Sec 8."""
    real_files = sorted(REAL_FIXTURES.glob("*.txt"))
    assert len(real_files) == 27
    for path in real_files:
        draft = _real_draft(path.name)
        Plan.model_validate(draft.plan_dict)


class TestCorpusGreenMountainRenewableRewards:
    """Green Mountain "Renewable Rewards Solar Credit 12" -- a real silent-wrong
    caught by scripts/audit_plans_llm.py.

    The parser reported `buyback: none` at 0.95 confidence on a plan whose name
    contains "Solar Credit", because Green Mountain brands its export credit
    "Renewable Rewards Credit" and that label wasn't in `_BUYBACK_LABEL`. The
    EFL states it plainly: "You will receive a Renewable Rewards Credit on your
    bill for the excess energy delivered by your eligible renewable energy
    system to the grid ... Renewable Rewards Credit: 6.3c per kWh". At the
    owner's ~9,800 kWh/yr export that is ~$618/yr of credit dropped from the
    ranking, on a promoted (unflagged) plan -- the exact failure mode the
    silent-wrong metric exists to prevent.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def draft() -> DraftPlan:
        return _real_draft("GREEN_MOUNTAIN_Renewable_Rewards_Solar_Credit_12.txt")

    def test_buyback_is_the_renewable_rewards_credit(self, draft):
        assert draft.plan_dict["buyback"]["kind"] == "fixed"
        assert draft.plan_dict["buyback"]["rate_ckwh"] == pytest.approx(6.3)

    def test_buyback_rate_is_not_the_energy_charge(self, draft):
        """Guard against a 1:1 misread -- 6.3c is the credit, 11.3c the rate."""
        rates = _rate_pairs(draft)
        assert rates[0][0] == pytest.approx(11.3)

    def test_base_charge(self, draft):
        assert draft.plan_dict["base_charge_usd"] == pytest.approx(29.95)

    def test_schema_valid(self, draft):
        Plan.model_validate(draft.plan_dict)


def test_usage_credit_table_row_form_is_parsed():
    """Regression: the Gexa / Frontier / Discount Power price-table row states its
    threshold as "for usage (>=N) kWh", not "when usage >= N kWh".

    Missing that silently dropped credits worth $50-$125 PER MONTH from 9 plans,
    5 of which had already auto-promoted -- the rest of those EFLs parse
    confidently, so nothing ever flagged them. Found 2026-07-24 while reviewing
    the draft queue, and the reason bill_credits is worth a dedicated test: it
    is not a load-bearing key, so the needs_review gate does not protect it.
    """
    from energyanalyzer.eflparse.parser import _extract_bill_credits

    table = "Usage Credit $125.00 per billing cycle for usage (>=1000) kWh"
    assert _extract_bill_credits(table) == [
        {"min_kwh": 1000.0, "max_kwh": None, "credit_usd": 125.0}
    ]

    # $ before the amount is optional, and thousands separators appear in the wild
    assert _extract_bill_credits("Usage Credit 50.00 per billing cycle for usage (>=500) kWh") == [
        {"min_kwh": 500.0, "max_kwh": None, "credit_usd": 50.0}
    ]

    prose = (
        "A Usage Credit of $50.00 will be included for each billing cycle when "
        "your usage on this plan is above or equal to 500 kWh."
    )
    assert _extract_bill_credits(prose) == [
        {"min_kwh": 500.0, "max_kwh": None, "credit_usd": 50.0}
    ]


def test_retailer_brand_alias_maps_the_licence_entity_to_the_brand():
    """Meter's EFLs are issued by "Light Energy, LLC" -- a licence-holding entity
    that appears nowhere a shopper would recognise, while the plan names on the
    same document read "Meter Saver Plan".

    Left unaliased it is not just confusing: `_plan_supersedes` compares retailer
    brand tokens, and "Light Energy" shares none with "Meter Energy", so the real
    EFL could never supersede the brand-named synthetic index row for the same
    plan -- the two sat side by side in the rankings.
    """
    from energyanalyzer.eflparse.parser import _apply_retailer_alias

    assert _apply_retailer_alias("Light Energy, LLC") == "Meter Energy"
    assert _apply_retailer_alias("Light Energy LLC") == "Meter Energy"
    # Anything not in the table is returned untouched -- this must stay a small,
    # evidence-based table, not a general rewriter.
    for other in ("Gexa Energy, LP", "Green Mountain Energy Company", "Lightning Power"):
        assert _apply_retailer_alias(other) == other


def test_meter_efl_parses_under_the_meter_brand():
    draft = _real_draft("METER_Saver_Plan_12.txt") if (REAL_FIXTURES / "METER_Saver_Plan_12.txt").exists() else None
    if draft is None:
        pytest.skip("Meter EFL fixture not committed")
    assert draft.plan_dict["retailer"] == "Meter Energy"


def test_etf_is_not_read_as_a_buyback_rate():
    """A dollar amount with no per-kWh unit must not become a per-kWh rate.

    Champion's Free Weekends-24 hedges its buyback ("may be available... please
    contact Customer Care") and, because PDF extraction interleaves the
    two-column disclosure chart, its "$250.00" early termination fee lands
    within the buyback label's context window. The parser read it as the buyback
    rate and reported 25000c/kWh at 0.85 confidence -- a silent-wrong that would
    have ranked the plan first by an absurd margin had its other fields parsed.
    """
    from energyanalyzer.eflparse.parser import _extract_buyback, _rate_ckwh_from_snippet

    # A unit-less dollar amount too large to be a per-kWh price is rejected...
    assert _rate_ckwh_from_snippet("Early Termination Fee: $250.00") is None
    # ...while genuine per-kWh prices still parse, with or without the unit.
    assert _rate_ckwh_from_snippet("Buyback Rate $0.158 per kWh") == 15.8
    assert _rate_ckwh_from_snippet("Buyback Rate: $0.035") == 3.5
    # An explicit unit is trusted even when the value looks implausible.
    assert _rate_ckwh_from_snippet("$2.50 per kWh") == 250.0

    text = (
        "Solar Buyback may be available with this plan. Please contact Customer "
        "Care to further discuss Champion's Solar Buyback program. Disclosure Chart "
        "Type of Product Fixed Rate Contract Term 24 Month(s) Yes, Early "
        "Termination Fee: $250.00 If applicable, Champion will assess the fee."
    )
    buyback, _conf, _ev = _extract_buyback(text, energy_ckwh=10.9)
    assert buyback == {"kind": "none"}


def test_buyback_attachable_to_this_plan_goes_to_review_not_confident_none():
    """"Available WITH THIS PLAN" and "for buyback plans ONLY" mean opposites.

    Champion attaches buyback to any residential plan except Free Nights without
    changing the rate, so a confident kind=none understates every Champion plan
    for a solar owner. TXU and Abundance gate buyback behind switching products,
    where kind=none genuinely describes the plan on the EFL. Both wordings hedge,
    so the hedge alone cannot separate them.
    """
    from energyanalyzer.eflparse.parser import _extract_buyback

    attachable = (
        "Solar Buyback may be available with this plan. Please contact Customer "
        "Care to further discuss Champion's Solar Buyback program."
    )
    bb, conf, ev = _extract_buyback(attachable, energy_ckwh=None)
    assert bb == {"kind": "none"} and conf < 0.8, "must land in review, not assert no-buyback"
    assert "THIS plan" in ev

    gated = (
        "Yes, for homeowners who are enrolled on an eligible TXU Energy solar "
        "buyback plan, and who have executed an Interconnection Agreement."
    )
    bb, conf, _ = _extract_buyback(gated, energy_ckwh=None)
    assert bb == {"kind": "none"} and conf >= 0.8, "switching products is required: none is right"

    # Precedence: when the EFL answers the PUCT disclosure question with a clear
    # "Yes", that veto outranks the gated reading and sends the plan to review
    # anyway -- the document itself says the REP buys excess generation, so an
    # unresolved rate is an unread field rather than an absent one. Abundance
    # only lands at high confidence on the real PDF because column interleaving
    # breaks the question/answer apart; on clean text the veto wins, which is
    # the safer of the two outcomes.
    disclosed_yes = (
        "Does REP purchase excess distributed renewable generation? Yes, for solar "
        "buy-back plans only. Please inquire for more details on solar buyback plans."
    )
    bb, conf, _ = _extract_buyback(disclosed_yes, energy_ckwh=None)
    assert bb == {"kind": "none"} and conf < 0.8


def test_champion_addendum_buyback_applied_to_eligible_plans_only():
    """Champion's buyback is real but lives in an addendum, not the EFL.

    Its EFLs say only "Solar Buyback may be available with this plan", so the
    parser alone can only report kind=none -- which understates every Champion
    plan for a solar owner. The addendum (read 2026-07-25) is a straight ERCOT
    real-time settlement: Excess per 15-min interval x the load zone's real-time
    settlement point price, and "does not contain any other costs, charges, fees,
    or taxes" -- so multiplier 1.0, adder 0.0, no cap.
    """
    from energyanalyzer.eflparse.parser import _attachable_buyback_policy

    attachable = "Solar Buyback may be available with this plan. Please contact Customer Care."
    got = _attachable_buyback_policy(
        "Champion Energy Services, LLC", "Champ Saver-12", attachable, {"kind": "none"}
    )
    assert got is not None
    buyback, conf, _ev = got
    assert buyback["kind"] == "rtw"
    assert buyback["rtw"] == {"multiplier": 1.0, "adder_ckwh": 0.0, "floor_ckwh": 0.0}
    assert buyback["offset_scope"] == "all_charges"
    assert (buyback["rollover"], buyback["cash_out"]) == (True, False)
    assert conf >= 0.8

    # Only Free NIGHTS is excluded. Champion's site states buyback IS available
    # on Free Weekends, so widening this would silently drop ~$215/yr of credit
    # from a plan that qualifies.
    assert _attachable_buyback_policy(
        "Champion Energy Services, LLC", "Free Nights 12", attachable, {"kind": "none"}
    ) is None
    assert _attachable_buyback_policy(
        "Champion Energy Services, LLC", "Free Weekends-24", attachable, {"kind": "none"}
    ) is not None

    # Never overrides a rate the EFL actually publishes...
    assert _attachable_buyback_policy(
        "Champion Energy Services, LLC", "Champ Saver-12", attachable,
        {"kind": "fixed", "rate_ckwh": 3.5},
    ) is None
    # ...never fires on a REP that gates buyback behind switching products...
    gated = "Yes, for solar buy-back plans only. Please inquire for more details."
    assert _attachable_buyback_policy("TXU Energy", "e-Saver 12", gated, {"kind": "none"}) is None
    # ...and never on a REP with no entry in the table.
    assert _attachable_buyback_policy("Gexa Energy, LP", "Gexa Saver 12", attachable,
                                      {"kind": "none"}) is None


# --------------------------------------------------------------------------- #
# Champion's REP/TDU split charge table (N rate columns + base + the TDU's pair)
# --------------------------------------------------------------------------- #
_CHAMPION_PREAMBLE = (
    "Electricity Facts Label\nChampion Energy Services, LLC PUC #10098\n"
    "Residential Service ⇒ {plan}\nOncor Electric Delivery\n7/25/2026\n"
    "Your average price per kilowatt-hour will vary based on your actual usage.\n"
)
_CHAMPION_TAIL = "\nType of Product Fixed Rate\nContract Term {term} Month(s)\nRenewable Content 24.7%\n"


def _champion_text(plan: str, term: int, prose: str, header: str, row: str) -> str:
    return (
        _CHAMPION_PREAMBLE.format(plan=plan)
        + prose
        + "\n"
        + header
        + "\n"
        + row
        + "\nOther Key Terms and Questions\n"
        + "Utility delivery charges include all recurring passed through charges from the "
        "utility without markup.\n"
        + _CHAMPION_TAIL.format(term=term)
    )


def test_champion_split_table_two_rate_columns_tou():
    """Champion prints rate(s), then its base charge, then the TDU's pair -- and
    the TDU pair is ALWAYS last, which is what makes the row readable without
    untangling the interleaved headers.

    The old single-rate reader either matched at the SECOND rate column (reading
    the discounted rate as the flat rate) or bailed on its "Energy Charge" header
    guard, which these variants don't print. Both EV Saver-12 and Free
    Weekends-24 came out as a flat 0.0c/kWh catch-all -- free electricity around
    the clock -- and had to be hand-entered, which any refresh would silently undo.
    """
    ev = parse_efl_text(
        _champion_text(
            "EV Saver-12", 12,
            "EV charging hours are from 10:00 PM to 4:00 AM every night.",
            "Champion Energy Charges Delivery Charges from\nOncor Electric Delivery\n"
            "Daytime Hours EV Charging Hours Base\nper kWh per month\n"
            "Usage Charge Usage Charge Charge",
            "7.4¢/kWh 6.0¢/kWh $0.00 6.1196¢/kWh $4.06",
        ),
        "ev.pdf",
    ).plan_dict
    # Oncor's 6.1196c/kWh and $4.06/mo are the TDU's -- never the plan's.
    assert ev["base_charge_usd"] == 0.0
    assert ev["energy_rates"] == [
        {"label": "EV Charging", "rate_ckwh": 6.0, "window": {"hours": [22, 23, 0, 1, 2, 3]}},
        {"label": "", "rate_ckwh": 7.4, "window": None},
    ]

    fw = parse_efl_text(
        _champion_text(
            "Free Weekends-24", 24,
            "Weekend hours are all day Saturday and Sunday, from 12:01 am on Saturday "
            "morning to 11:59 pm Sunday night.",
            "Delivery Charges from Champion Energy Charges Oncor Electric Delivery\n"
            "Base Weekdays Weekends Charge per kWh per month",
            "10.9¢/kWh 0.0¢/kWh $0.00 6.1196¢/kWh $4.06",
        ),
        "fw.pdf",
    ).plan_dict
    assert fw["base_charge_usd"] == 0.0
    assert fw["energy_rates"] == [
        {"label": "Weekend", "rate_ckwh": 0.0, "window": {"weekdays": [5, 6]}},
        {"label": "", "rate_ckwh": 10.9, "window": None},
    ]
    # The catch-all must be the WEEKDAY rate; a 0.0 catch-all is free power always.
    assert fw["energy_rates"][-1]["rate_ckwh"] > 0


def test_champion_split_table_single_rate_column_unchanged():
    d = parse_efl_text(
        _champion_text(
            "Champ Saver-12", 12, "",
            "Champion Energy Charges Delivery Charges from Energy Charge "
            "Oncor Electric Delivery Base Charge (per kWh) per kWh per month",
            "6.7¢/kWh $0.00 6.1196¢/kWh $4.06",
        ),
        "cs.pdf",
    ).plan_dict
    assert d["base_charge_usd"] == 0.0
    assert d["energy_rates"] == [{"label": "", "rate_ckwh": 6.7, "window": None}]


def test_multi_rate_split_row_needs_a_delivery_header_and_a_known_restricted_column():
    """Guards against firing on an unrelated run of numbers, and against guessing
    a window when the headers don't say which column is time-restricted."""
    from energyanalyzer.eflparse.parser import _extract_multi_rate_charge_row

    row = "7.4¢/kWh 6.0¢/kWh $0.00 6.1196¢/kWh $4.06"
    # No delivery/TDU header above the row -> not this table.
    assert _extract_multi_rate_charge_row("Some other table\n" + row) is None
    # Header present but no restricted/general column labels -> index unknown,
    # so parse_efl_text must not invent a window (it falls back to other readers).
    got = _extract_multi_rate_charge_row(
        "Delivery Charges from Oncor Electric Delivery\nCharge per month\n" + row
    )
    assert got is not None and got["restricted_index"] is None


def test_charge_basis_is_the_first_unit_named_not_a_fixed_precedence():
    """A '... per <unit>' phrase can name more than one unit.

    Heritage Power prints "Minimum Usage Charge: $0 per billing cycle < 0 kWh".
    Testing for "kwh" before "cycle" classified that as a per-kWh charge -- a
    $0.00 ENERGY RATE, i.e. free electricity around the clock. Harmless while
    the generic scan's reads scored too low to promote; a live trap once they
    didn't. The unit immediately after "per" is the real basis.
    """
    from energyanalyzer.eflparse.parser import _classify_unit_kind, _generic_charge_rows

    assert _classify_unit_kind("billing cycle < 0 kWh") == "month"
    assert _classify_unit_kind("kWh") == "kwh"
    assert _classify_unit_kind("day") == "day"
    assert _classify_unit_kind("illing ccle") == "month"  # broken-font spelling

    rows = _generic_charge_rows("Minimum Usage Charge: $0 per billing cycle < 0 kWh\n")
    assert rows and rows[0]["kind"] == "month", "must never be offered as an energy rate"


def test_broken_font_bill_unit_is_a_monthly_basis():
    """"per ill" is "per bill" with the b dropped by a subset font.

    Atlantex prints "ae Charge $19.95 per ill". The month markers knew "illing"
    but not "ill", so the row matched no unit at all and its $19.95 base charge
    was never read -- the plan silently defaulted to $0.00, understating it by
    $239/yr. This was the last wrong value in the ground-truth corpus.
    """
    from energyanalyzer.eflparse.parser import _classify_unit_kind, _generic_charge_rows

    assert _classify_unit_kind("ill") == "month"
    assert _classify_unit_kind("illing ccle") == "month"
    rows = _generic_charge_rows("ae Charge $19.95 per ill\n")
    assert rows and rows[0]["kind"] == "month" and rows[0]["value"] == 19.95


def test_base_charge_amount_may_follow_the_unit():
    """"a monthly Base Electricity Charge per ESI-ID of $0.00" (Constellation).

    Every labelled reader expects "<label> ... $X per <unit>", so an amount that
    trails the unit was never found and the charge defaulted to $0.00 -- right
    by luck here, but unread, and wrong for any REP that charges one.
    """
    from energyanalyzer.eflparse.parser import _extract_base_charge_trailing_amount

    got = _extract_base_charge_trailing_amount(
        "calculated using: (i) a Fixed Energy Charge of 6.27¢ per kWh, (ii) the "
        "applicable TDU tariff, (iii) a monthly Base Electricity Charge per ESI-ID "
        "of $0.00 (NOTE: A Minimum Usage Fee of $ 0 will apply), and (iv) all "
        "recurring charges."
    )
    assert got is not None and got[0] == 0.0 and got[1] >= 0.8

    nonzero = _extract_base_charge_trailing_amount(
        "a monthly Base Electricity Charge per ESI-ID of $9.95 applies."
    )
    assert nonzero is not None and nonzero[0] == 9.95


def test_credited_window_is_read_even_though_the_efl_never_says_free():
    """A window whose energy charge is credited back, not called "free".

    Amigo/Just Energy/Tara "Days Bundle" plans say "Your bill will contain a
    credit for Energy Charges resulting from energy consumed during Day Hours"
    and define it separately as "Day Hours = 9:00 AM - 4:00 PM". The free-window
    reader keys off the word "free", which appears nowhere, so all three billed
    the full rate for seven hours a day that cost nothing.

    Both phrases wrap mid-sentence in the real PDFs, so the match runs against
    whitespace-normalised text; a newline-sensitive pattern saw half of each and
    found nothing on two of the three documents.
    """
    from energyanalyzer.eflparse.parser import _extract_credited_window

    wrapped = (
        "Day Hours = 9:00 AM –\n4:00 PM. Your bill will contain a credit for Energy\n"
        "Charges resulting from energy consumed during Day Hours.\n"
    )
    got = _extract_credited_window(wrapped)
    assert got is not None
    assert got["hours"] == [9, 10, 11, 12, 13, 14, 15]

    # No credit sentence -> nothing to infer, even with an hours definition.
    assert _extract_credited_window("Day Hours = 9:00 AM - 4:00 PM.\n") is None


def test_unmodelled_bonus_credit_is_flagged_but_plain_free_nights_is_not():
    """A bonus the schema can't express must not be promoted silently.

    TXU's Free Nights & Cool Summer 12 pays "an additional 25% credit on all
    Energy Charges" in Jul-Aug-Sep on top of the free-nights window. Nothing
    misparses, so nothing scores low -- the plan just quietly under-ranks,
    because we model it as costing more than it does. That is the one failure
    shape a confidence gate cannot see.

    The discrimination matters: an ordinary free-nights plan says "100% credit
    on all Energy Charges", which IS modelled (a 0.0 rate over the window) and
    must stay unflagged, or every free-nights plan lands in review.
    """
    from energyanalyzer.eflparse.parser import _BONUS_CREDIT_RE

    txu = (
        "Free Nights Savings: From 9:00 p.m. through 4:59 a.m. each day, you will receive "
        "a 100% credit on all Energy Charges. Cool Summer Bonus: You will receive an "
        "additional 25% credit on all Energy Charges during the Free Nights Savings period "
        "during July, August, and September."
    )
    ambit = (
        "Free Nights: You can receive a 100% Discount on all Energy Charges during the "
        "nighttime hours from 9:00 p.m. through 5:59 a.m. each day."
    )
    assert _BONUS_CREDIT_RE.search(txu)
    assert not _BONUS_CREDIT_RE.search(ambit)


def test_solar_exclusion_needs_both_an_exclusion_word_and_a_solar_subject():
    """`excludes_solar` gates eligibility, so a false positive silently hides a
    buyable plan from ranking. Requires BOTH signals in one sentence: "solar"
    alone is everywhere in these documents (renewable content, buyback terms,
    brand names) and would flag much of the corpus.

    Audited over all 243 EFLs on disk: exactly one match, TXU's Free Nights &
    Cool Summer 12.
    """
    from energyanalyzer.eflparse.parser import _SOLAR_EXCLUSION_RE, _SOLAR_SUBJECT_RE

    def flagged(text: str) -> bool:
        return any(
            _SOLAR_SUBJECT_RE.search(m.group(0)) for m in _SOLAR_EXCLUSION_RE.finditer(text)
        )

    assert flagged(
        "Customers with electric vehicles, batteries, and/or solar panels are ineligible "
        "for this plan."
    )
    # Buyback terms mention solar constantly -- never an exclusion.
    assert not flagged(
        "Solar Buyback: excess generation from your rooftop solar panels is credited at "
        "9.7 cents per kWh."
    )
    # An exclusion about something else entirely.
    assert not flagged("Prepaid customers are ineligible for this product.")
    # 100% renewable content is not an eligibility statement.
    assert not flagged("This product is 100% renewable, sourced from Texas wind and solar.")


def test_excludes_solar_defaults_false_and_round_trips():
    from energyanalyzer.core.models import EnergyRate, Plan

    base = dict(
        id="x", retailer="R", name="N", term_months=12,
        energy_rates=[EnergyRate(label="", rate_ckwh=12.0, window=None)],
    )
    assert Plan(**base).excludes_solar is False
    assert Plan(**base, excludes_solar=True).excludes_solar is True
