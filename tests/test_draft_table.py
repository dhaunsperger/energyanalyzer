"""Tests for the Plans page's draft review table (the value column).

The confidence table showed field/source/confidence/evidence but not the value
the draft actually assigned, so checking a parse meant ping-ponging between the
table and the YAML below it for every row.
"""

from __future__ import annotations

import pathlib

import pytest

_SRC = pathlib.Path(__file__).parent.parent / "src/energyanalyzer/app/pages/2_Plans.py"
_ns: dict = {}
_text = _SRC.read_text()
exec(_text[_text.index("def _hours_span") : _text.index("st.set_page_config(")], _ns)  # noqa: S102
_hours_span = _ns["_hours_span"]
_window_label = _ns["_window_label"]
draft_field_value = _ns["draft_field_value"]


@pytest.mark.parametrize(
    "hours,expected",
    [
        ([23, 0, 1, 2, 3, 4, 5], "23:00-06:00"),      # free nights, wraps midnight
        ([21, 22, 23, 0, 1, 2, 3, 4, 5], "21:00-06:00"),
        ([22, 23, 0, 1, 2, 3, 4, 5], "22:00-06:00"),  # Octopus off-peak
        ([23, 0, 1, 2, 3, 4], "23:00-05:00"),         # EV window
        ([9, 10, 11, 12, 13, 14, 15], "09:00-16:00"), # free daytime, no wrap
        (list(range(6, 22)), "06:00-22:00"),          # Octopus peak
    ],
)
def test_hours_span_handles_windows_that_wrap_midnight(hours, expected):
    """A night window wraps midnight, so neither min() nor max() bounds it --
    max() rendered {23,0..5} as "23:00-00:00" when it actually runs to 06:00."""
    assert _hours_span(hours) == expected


def test_window_label_covers_weekday_and_month_windows():
    assert _window_label(None) == "all hours"
    assert _window_label({"weekdays": [5, 6]}) == "Sat/Sun"
    assert _window_label({"weekdays": [4, 5, 6]}) == "Fri/Sat/Sun"
    assert "months 3,4,5" in _window_label({"months": [3, 4, 5], "hours": list(range(6, 22))})


def test_draft_field_value_renders_the_stored_value():
    raw = {
        "retailer": "Meter Energy", "name": "Meter Saver Plan", "term_months": 12,
        "base_charge_usd": 0.0, "rate_type": "fixed", "renewable_pct": 100.0,
        "energy_rates": [
            {"rate_ckwh": 0.0, "window": {"hours": [23, 0, 1, 2, 3, 4, 5]}},
            {"rate_ckwh": 6.47, "window": None},
        ],
        "buyback": {"kind": "fixed", "rate_ckwh": 3.0, "offset_scope": "energy_only"},
        "bill_credits": [{"min_kwh": 500.0, "credit_usd": 50.0}],
        "etf_usd": 20.0, "etf_per_month_remaining": True,
    }
    assert draft_field_value(raw, "retailer") == "Meter Energy"
    assert draft_field_value(raw, "term_months") == "12 mo"
    assert draft_field_value(raw, "base_charge") == "$0.0"
    assert draft_field_value(raw, "energy_charge") == "0.0c @ 23:00-06:00 | 6.47c @ all hours"
    assert draft_field_value(raw, "free_window") == "23:00-06:00"
    assert draft_field_value(raw, "buyback") == "fixed 3.0c (energy_only)"
    assert draft_field_value(raw, "bill_credits") == "$50.0 @>=500.0kWh"
    assert draft_field_value(raw, "etf") == "$20.0/mo remaining"
    assert draft_field_value(raw, "renewable_pct") == "100.0%"


def test_draft_field_value_marks_parse_only_fields():
    """tdu_ckwh / tdu_monthly / avg_price_* are read to CHECK a parse but are not
    stored on the Plan (TDU tariffs come from tdu/oncor.yaml), so the column must
    say so rather than invent a value."""
    for field in ("tdu_ckwh", "tdu_monthly", "avg_price_500_ckwh"):
        assert draft_field_value({}, field) == "--"


def test_rtw_buyback_shows_multiplier_and_cap():
    raw = {"buyback": {"kind": "rtw", "rtw": {"multiplier": 1.0, "cap_ckwh": 25.0}}}
    assert draft_field_value(raw, "buyback") == "rtw x1.0, cap 25.0c"
