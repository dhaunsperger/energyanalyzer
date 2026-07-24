"""Tests for energyanalyzer.fetchers.meterplan -- built entirely from the
committed tests/fixtures/meterplan_sample.md snapshot; no live network access
(meterplan.com is blocked in the sandbox this module was developed in).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from energyanalyzer.core.models import Plan
from energyanalyzer.fetchers import meterplan as mp

FIXTURE = Path(__file__).parent / "fixtures" / "meterplan_sample.md"
PLANS_FIXTURE = Path(__file__).parent / "fixtures" / "meterplan_plans_sample.html"


# --------------------------------------------------------------------------- #
# Meter's own real EFLs (JSON-LD on the /plans page)
# --------------------------------------------------------------------------- #
def test_parse_meterplan_efl_offers_filters_tdu_and_extracts_efl():
    html = PLANS_FIXTURE.read_text()
    offers = mp.parse_meterplan_efl_offers(html, tdu="Oncor")
    # 2 Oncor offers carry an EFL; the third Oncor offer has no EFL property
    # (dropped), and the CenterPoint offer is filtered out by TDU.
    ids = sorted(o["offer_id"] for o in offers)
    assert ids == ["earner-12mo-oncor", "saver-12mo-oncor"]
    saver = next(o for o in offers if o["offer_id"] == "saver-12mo-oncor")
    assert saver["name"] == "Meter Saver — 12 months (Oncor)"
    # json.loads decodes the escaped presigned query string; URL is intact.
    assert saver["efl_url"].startswith("https://light-assets.s3.amazonaws.com/efls/")
    assert "X-Amz-Signature=deadbeef" in saver["efl_url"]


def test_parse_meterplan_efl_offers_tdu_filter_centerpoint():
    html = PLANS_FIXTURE.read_text()
    offers = mp.parse_meterplan_efl_offers(html, tdu="CenterPoint")
    assert [o["offer_id"] for o in offers] == ["saver-12mo-centerpoint"]


def test_fetch_meterplan_efls_downloads_offer_pdfs(tmp_path, monkeypatch):
    html = PLANS_FIXTURE.read_text()

    class _Resp:
        def __init__(self, *, text=None, content=b"", ctype="application/pdf"):
            self.text = text or ""
            self.content = content
            self.headers = {"content-type": ctype}

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def get(self, url, *a, **k):
            if url.startswith(mp.METERPLAN_PLANS_URL):
                return _Resp(text=html)
            return _Resp(content=b"%PDF-1.7 fake meter efl")

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    summary = mp.fetch_meterplan_efls(zip_code="78665", dest=tmp_path, tdu="Oncor")

    assert summary["offers"] == 2
    assert len(summary["downloaded"]) == 2
    assert summary["failed"] == []
    names = sorted(Path(p).name for p in summary["downloaded"])
    assert names == ["Meter_Energy_earner_12mo_oncor.pdf", "Meter_Energy_saver_12mo_oncor.pdf"]
    for p in summary["downloaded"]:
        assert Path(p).read_bytes().startswith(b"%PDF")


def test_fetch_meterplan_efls_page_failure_raises_runtimeerror(tmp_path, monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def get(self, *a, **k):
            raise OSError("network down")

    import httpx

    monkeypatch.setattr(httpx, "Client", _BoomClient)
    with pytest.raises(RuntimeError, match="Meter Energy's plans page"):
        mp.fetch_meterplan_efls(dest=tmp_path)


# --------------------------------------------------------------------------- #
# load_meterplan: table-section selection, row counts, tidy columns
# --------------------------------------------------------------------------- #
def test_load_meterplan_selects_availability_tables_not_top_plans():
    df = mp.load_meterplan(FIXTURE)

    # The fixture's "Source Status" table gives authoritative per-slice
    # counts for the two tables we DO parse (Meter Plan Availability +
    # Competitor Plan Availability): 6+9+10+4+11+6 = 46 meter rows, and
    # 24+24+0+0+0+0 = 48 competitor rows -> 94 total. "Top Plans By TDU For
    # The Default Profile" (30 rows) is a curated re-listing of rows that
    # already appear in those two tables -- if it were accidentally
    # included too, the total would be higher (and TDU counts would exceed
    # the Source Status table's numbers).
    assert len(df) == 94

    for col in (
        "tdu",
        "retailer",
        "plan_name",
        "term_months",
        "import_ckwh",
        "export_kind",
        "export_ckwh",
        "base_usd_month",
        "etf_usd",
        "etf_per_month_remaining",
        "battery_required",
        "source_url",
        "generated",
    ):
        assert col in df.columns, f"missing tidy column {col}"


def test_load_meterplan_tdu_breakdown_matches_source_status():
    df = mp.load_meterplan(FIXTURE)
    counts = df["tdu"].value_counts().to_dict()
    assert counts == {
        "Oncor": 30,  # 6 meter + 24 competitor
        "Centerpoint": 33,  # 9 meter + 24 competitor
        "AEP Central": 10,  # 10 meter + 0 competitor (partial)
        "AEP North": 4,
        "TNMP": 11,
        "Lubbock": 6,
    }


def test_load_meterplan_generated_timestamp():
    df = mp.load_meterplan(FIXTURE)
    generated = df["generated"].iloc[0]
    assert isinstance(generated, dt.datetime)
    assert generated.year == 2026
    assert generated.month == 7
    assert generated.day == 21
    assert generated.hour == 16
    assert generated.minute == 37
    # Every row shares the same document-level Generated: timestamp.
    assert df["generated"].nunique() == 1


def test_load_meterplan_missing_path_raises_filenotfound(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        mp.load_meterplan(tmp_path / "nope.md")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        mp.load_meterplan(empty_dir)


def test_load_meterplan_from_directory_picks_most_recent_md(tmp_path: Path):
    import os
    import time

    older = tmp_path / "meterplan_20250101T000000Z.md"
    newer = tmp_path / "meterplan_20260701T000000Z.md"
    older.write_text(FIXTURE.read_text())
    newer.write_text(FIXTURE.read_text())
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    df = mp.load_meterplan(tmp_path)
    assert len(df) == 94


# --------------------------------------------------------------------------- #
# Rate / fee cell parsing edge cases (spot-checked against real fixture rows)
# --------------------------------------------------------------------------- #
def _row(df, retailer, plan_name, term_months, tdu=None):
    sel = (df["retailer"] == retailer) & (df["plan_name"] == plan_name) & (
        df["term_months"] == term_months
    )
    if tdu is not None:
        sel &= df["tdu"] == tdu
    matches = df[sel]
    assert len(matches) == 1, f"expected exactly 1 match, got {len(matches)}"
    return matches.iloc[0]


def test_import_rate_parses_symbol_and_word_forms():
    df = mp.load_meterplan(FIXTURE)
    # "15.6¢/kWh"
    txu = _row(df, "TXU Energy", "Solar BB System Flex", 1, tdu="Oncor")
    assert txu["import_ckwh"] == pytest.approx(15.6)
    # "13.53 cents/kWh"
    earner_battery = _row(df, "Meter Energy", "Earner + Battery", 12, tdu="Centerpoint")
    assert earner_battery["import_ckwh"] == pytest.approx(13.53)


def test_base_charge_parses_none_and_dollar_variants():
    df = mp.load_meterplan(FIXTURE)
    # "None" -> 0.0
    tesla = _row(df, "Tesla Electric", "Drive Plan", 12, tdu="Oncor")
    assert tesla["base_usd_month"] == pytest.approx(0.0)
    # "$19.95/mo"
    txu = _row(df, "TXU Energy", "Solar BB System Flex", 1, tdu="Oncor")
    assert txu["base_usd_month"] == pytest.approx(19.95)
    # "$14.95/month"
    almika = _row(df, "Almika Solar", "60 Energy Plus Buyback", 60, tdu="Oncor")
    assert almika["base_usd_month"] == pytest.approx(14.95)
    # "$0/month"
    saver = _row(df, "Meter Energy", "Saver", 12, tdu="Oncor")
    assert saver["base_usd_month"] == pytest.approx(0.0)


def test_etf_parses_none_flat_and_per_month_variants():
    df = mp.load_meterplan(FIXTURE)
    # "None"
    tesla = _row(df, "Tesla Electric", "Drive Plan", 12, tdu="Oncor")
    assert tesla["etf_usd"] == pytest.approx(0.0)
    assert bool(tesla["etf_per_month_remaining"]) is False
    # "$150"
    direct = _row(df, "Direct Energy", "Direct Solar Unlimited", 12, tdu="Oncor")
    assert direct["etf_usd"] == pytest.approx(150.0)
    assert bool(direct["etf_per_month_remaining"]) is False
    # "$14.95  per month" (double space in source)
    almika = _row(df, "Almika Solar", "60 Energy Plus Buyback", 60, tdu="Oncor")
    assert almika["etf_usd"] == pytest.approx(14.95)
    assert bool(almika["etf_per_month_remaining"]) is True


def test_export_credit_fixed_rtw_and_none():
    df = mp.load_meterplan(FIXTURE)
    # fixed
    txu = _row(df, "TXU Energy", "Solar BB System Flex", 1, tdu="Oncor")
    assert txu["export_kind"] == "fixed"
    assert txu["export_ckwh"] == pytest.approx(15.6)
    # "Real Time" -> rtw
    chariot = _row(df, "Chariot Energy", "Shine", 36, tdu="Oncor")
    assert chariot["export_kind"] == "rtw"
    assert pd.isna(chariot["export_ckwh"])
    # "0.0¢/kWh" -> none
    reliant = _row(df, "Reliant Energy", "Truly Free Nights", 12, tdu="Oncor")
    assert reliant["export_kind"] == "none"
    # "0 cents/kWh" word-form zero also -> none
    standard = _row(df, "Meter Energy", "Standard", 12, tdu="Oncor")
    assert standard["export_kind"] == "none"


def test_battery_required_flag():
    df = mp.load_meterplan(FIXTURE)
    battery_rows = df[df["plan_name"].str.contains(r"\+ Battery", regex=True)]
    assert len(battery_rows) > 0
    assert battery_rows["battery_required"].all()
    non_battery_rows = df[~df["plan_name"].str.contains(r"\+ Battery", regex=True)]
    assert not non_battery_rows["battery_required"].any()


# --------------------------------------------------------------------------- #
# filter_meterplan
# --------------------------------------------------------------------------- #
def test_filter_meterplan_by_tdu_default_oncor():
    df = mp.load_meterplan(FIXTURE)
    filtered = mp.filter_meterplan(df)  # default tdu="Oncor"
    assert len(filtered) == 30
    assert set(filtered["tdu"]) == {"Oncor"}


def test_filter_meterplan_case_insensitive_and_none():
    df = mp.load_meterplan(FIXTURE)
    assert len(mp.filter_meterplan(df, tdu="oncor")) == 30
    assert len(mp.filter_meterplan(df, tdu="ONCOR")) == 30
    assert len(mp.filter_meterplan(df, tdu=None)) == 94


# --------------------------------------------------------------------------- #
# meterplan_to_drafts
# --------------------------------------------------------------------------- #
def test_meterplan_to_drafts_oncor_counts_and_schema_validity(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    oncor = mp.filter_meterplan(df, tdu="Oncor")

    summary = mp.meterplan_to_drafts(oncor, tmp_path, existing_plan_keys=set())

    assert summary["skipped_battery"] == 0  # no Oncor rows are battery plans in this fixture
    assert summary["skipped_existing"] == 0
    assert len(summary["imported"]) == 30
    # free-night-name rows (Truly Free Nights, All Nighter, Nights Free, Free
    # Nights & Solar Days, Free & Clear Nights, Twelve Hour Power = 6) plus
    # rtw-export rows (Shine, Glow Solar, Solar Payback Match, Solar Max = 4).
    assert summary["flagged_for_review"] == 10

    draft_files = sorted(tmp_path.glob("mp_*.yaml"))
    assert len(draft_files) == 30

    import yaml

    for path in draft_files:
        raw = yaml.safe_load(path.read_text())
        parse_meta = raw.pop("_parse")
        assert "confidence" in parse_meta and "evidence" in parse_meta
        # Every draft plan-shaped dict (minus _parse) must validate.
        Plan.model_validate(raw)


def test_meterplan_to_drafts_battery_rows_skipped(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    aep_central = mp.filter_meterplan(df, tdu="AEP Central")
    battery_rows = aep_central[aep_central["battery_required"]]
    assert len(battery_rows) == 2  # Earner + Battery, Saver + Battery

    summary = mp.meterplan_to_drafts(aep_central, tmp_path, existing_plan_keys=set())
    assert summary["skipped_battery"] == 2
    assert not any("battery" in pid for pid in summary["imported"])
    for path in tmp_path.glob("*.yaml"):
        assert "battery" not in path.stem


def test_meterplan_to_drafts_dedupe_existing(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    oncor = mp.filter_meterplan(df, tdu="Oncor")

    existing = {("tesla electric", "drive plan", 12)}
    summary = mp.meterplan_to_drafts(oncor, tmp_path, existing_plan_keys=existing)
    assert summary["skipped_existing"] == 1
    assert len(summary["imported"]) == 29
    assert not any("tesla" in pid for pid in summary["imported"])


def test_meterplan_to_drafts_free_night_structure_and_needs_review(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    oncor = mp.filter_meterplan(df, tdu="Oncor")
    mp.meterplan_to_drafts(oncor, tmp_path, existing_plan_keys=set())

    import yaml

    path = next(tmp_path.glob("*truly_free_nights*.yaml"))
    raw = yaml.safe_load(path.read_text())
    assert raw["needs_review"] is True

    rates = raw["energy_rates"]
    assert len(rates) == 2
    night, day = rates
    assert night["rate_ckwh"] == 0.0
    assert night["window"]["hours"] == [21, 22, 23, 0, 1, 2, 3, 4, 5]
    assert day["window"] is None
    assert day["rate_ckwh"] == pytest.approx(24.3)  # published "Import rate"

    parse_meta = raw["_parse"]
    assert parse_meta["confidence"]["energy_charge"] == pytest.approx(0.4)
    assert parse_meta["confidence"]["free_window"] == pytest.approx(0.4)
    assert any("verify" in n.lower() for n in parse_meta["unparsed_notes"])

    Plan.model_validate({k: v for k, v in raw.items() if k != "_parse"})


def test_meterplan_to_drafts_rtw_mapping(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    oncor = mp.filter_meterplan(df, tdu="Oncor")
    mp.meterplan_to_drafts(oncor, tmp_path, existing_plan_keys=set())

    import yaml

    path = next(tmp_path.glob("*shine*.yaml"))
    raw = yaml.safe_load(path.read_text())
    buyback = raw["buyback"]
    assert buyback["kind"] == "rtw"
    assert buyback["rtw"] == {"multiplier": 1.0, "adder_ckwh": 0.0}
    assert raw["needs_review"] is True  # rtw export isn't "simple"

    Plan.model_validate({k: v for k, v in raw.items() if k != "_parse"})


def test_meterplan_to_drafts_simple_plan_not_flagged(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    oncor = mp.filter_meterplan(df, tdu="Oncor")
    mp.meterplan_to_drafts(oncor, tmp_path, existing_plan_keys=set())

    import yaml

    path = next(tmp_path.glob("*gexa_energy_solar_buyback*.yaml"))
    raw = yaml.safe_load(path.read_text())
    assert raw["needs_review"] is False
    assert raw["energy_rates"] == [{"rate_ckwh": pytest.approx(9.3), "window": None}]
    assert raw["buyback"]["kind"] == "fixed"
    assert raw["buyback"]["rate_ckwh"] == pytest.approx(3.0)
    for key in ("energy_charge", "base_charge", "buyback"):
        assert raw["_parse"]["confidence"][key] >= 0.8


def test_meterplan_to_drafts_stamps_retrieved_from_generated(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    oncor = mp.filter_meterplan(df, tdu="Oncor")
    summary = mp.meterplan_to_drafts(oncor, tmp_path, existing_plan_keys=set())

    import yaml

    for pid in summary["imported"][:3]:
        raw = yaml.safe_load((tmp_path / f"{pid}.yaml").read_text())
        assert raw["retrieved"] == dt.date(2026, 7, 21)


def test_meterplan_to_drafts_retrieved_override(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    oncor = mp.filter_meterplan(df, tdu="Oncor")
    override = dt.date(2020, 1, 1)
    summary = mp.meterplan_to_drafts(
        oncor, tmp_path, existing_plan_keys=set(), retrieved=override
    )

    import yaml

    pid = summary["imported"][0]
    raw = yaml.safe_load((tmp_path / f"{pid}.yaml").read_text())
    assert raw["retrieved"] == override


def test_meterplan_to_drafts_source_and_id_prefix(tmp_path: Path):
    df = mp.load_meterplan(FIXTURE)
    oncor = mp.filter_meterplan(df, tdu="Oncor")
    summary = mp.meterplan_to_drafts(oncor, tmp_path, existing_plan_keys=set())
    for pid in summary["imported"]:
        assert pid.startswith("mp_")

    import yaml

    raw = yaml.safe_load(next(tmp_path.glob("*.yaml")).read_text())
    assert raw["source"] == "meterplan"
    assert raw["tdu"] == "ONCOR"


# --------------------------------------------------------------------------- #
# fetch_meterplan (network path -- exercised only via monkeypatched httpx)
# --------------------------------------------------------------------------- #
def test_fetch_meterplan_raises_clear_error_when_network_unavailable(tmp_path, monkeypatch):
    import httpx

    class _BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            raise httpx.ConnectError("network blocked in this environment")

    monkeypatch.setattr(httpx, "Client", _BoomClient)

    with pytest.raises(RuntimeError) as excinfo:
        mp.fetch_meterplan(dest_dir=tmp_path)
    msg = str(excinfo.value)
    assert "meterplan.com" in msg.lower()
    assert str(tmp_path) in msg


def test_fetch_meterplan_saves_timestamped_snapshot(tmp_path, monkeypatch):
    class _FakeResponse:
        def __init__(self, content: bytes):
            self.content = content

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, *args, **kwargs):
            return _FakeResponse(FIXTURE.read_bytes())

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    path = mp.fetch_meterplan(dest_dir=tmp_path)
    assert path.exists()
    assert path.suffix == ".md"
    assert path.parent == tmp_path

    # The saved snapshot should itself be loadable.
    df = mp.load_meterplan(path)
    assert len(df) == 94
