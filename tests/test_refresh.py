"""Tests for the "Refresh market data" pipeline and staleness helpers
(app/common.py: refresh_market_data, interval_staleness_warning,
price_coverage_warning, tdu_staleness_warning, plan_is_stale, stale_plan_ids).

refresh_market_data is exercised entirely against tmp dirs + monkeypatched
fetch/download/parse internals -- no live network, deterministic outcomes.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd
import pytest
import yaml

from energyanalyzer.app import common as app_common
from energyanalyzer.core.models import EnergyRate, Plan, TduTariff
from energyanalyzer.eflparse import parser as eflparser
from energyanalyzer.fetchers import meterplan as meterplan_module
from energyanalyzer.fetchers import ptc as ptc_module

FIXTURE = Path(__file__).parent / "fixtures" / "ptc_sample.csv"
METERPLAN_FIXTURE = Path(__file__).parent / "fixtures" / "meterplan_sample.md"


def _write_plan_yaml(path: Path, id_: str, source: str) -> None:
    path.write_text(
        f"id: {id_}\n"
        f"retailer: Test Co\n"
        f"name: Test Plan {id_}\n"
        f"term_months: 12\n"
        f"base_charge_usd: 5.0\n"
        f"energy_rates:\n"
        f"  - rate_ckwh: 12.0\n"
        f"source: {source}\n"
    )


@pytest.fixture
def refresh_dirs(tmp_path: Path):
    plans_dir = tmp_path / "plans"
    drafts_dir = plans_dir / "drafts"
    efl_dir = tmp_path / "efl"
    ptc_dir = tmp_path / "ptc"
    meterplan_dir = tmp_path / "meterplan"
    for d in (plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir):
        d.mkdir(parents=True)
    return plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeHttpxClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, *a, **k):
        return _FakeResponse(b"%PDF-1.4 fake efl content")


def _fake_parse_efl(pdf_path) -> eflparser.DraftPlan:
    """Deterministic stand-in for the real static parser: a TXU-branded EFL
    is "confident" (auto-promotable); everything else needs review."""
    pdf_path = Path(pdf_path)
    plan_id = pdf_path.stem.lower()
    confident = "txu" in plan_id
    return eflparser.DraftPlan(
        plan_dict={
            "id": plan_id,
            "retailer": "Test Retailer",
            "name": pdf_path.stem,
            "term_months": 12,
            "base_charge_usd": 4.0,
            "energy_rates": [{"rate_ckwh": 11.0}],
            "buyback": {"kind": "none"},
            "source": f"efl:{pdf_path.name}",
            "needs_review": not confident,
        },
        confidence=(
            {"energy_charge": 0.9, "base_charge": 0.95} if confident else {"energy_charge": 0.5}
        ),
        evidence={},
        unparsed_notes=[],
    )


def test_refresh_market_data_full_pipeline(refresh_dirs, monkeypatch):
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs

    _write_plan_yaml(plans_dir / "manual_plan.yaml", "manual_plan", "manual")
    _write_plan_yaml(plans_dir / "report_plan.yaml", "report_plan", "report-2026-07")
    _write_plan_yaml(plans_dir / "ptc_plan.yaml", "ptc_plan", "ptc")
    _write_plan_yaml(plans_dir / "efl_plan.yaml", "efl_plan", "efl:oldfile.pdf")
    # id-protected even though its source looks importable/replaceable.
    _write_plan_yaml(plans_dir / "pulse_current.yaml", "pulse_current", "ptc")

    (drafts_dir / "stale_draft.yaml").write_text("id: stale_draft\n")
    (efl_dir / "old.pdf").write_bytes(b"old pdf bytes")

    older_csv = ptc_dir / "ptc_old.csv"
    newer_csv = ptc_dir / "ptc_new.csv"
    older_csv.write_text(FIXTURE.read_text())
    newer_csv.write_text(FIXTURE.read_text())
    now = time.time()
    os.utime(older_csv, (now - 1000, now - 1000))
    os.utime(newer_csv, (now, now))

    # Live fetch is blocked in this sandbox -- verify the graceful fallback.
    def _boom_fetch(dest_dir, timeout=30.0):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", _boom_fetch)

    # meterplan.com is a separate site/network call -- also blocked here, and
    # no local snapshot exists, so this test stays scoped to the PTC/EFL path
    # (the dedicated meterplan-stage tests below cover it in isolation).
    def _boom_meterplan_fetch(dest_dir, timeout=30.0):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(meterplan_module, "fetch_meterplan", _boom_meterplan_fetch)

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)
    monkeypatch.setattr(eflparser, "parse_efl", _fake_parse_efl)

    calls: list[tuple[int, int, str]] = []
    summary = app_common.refresh_market_data(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        efl_dir=efl_dir,
        ptc_dir=ptc_dir,
        meterplan_dir=meterplan_dir,
        progress_callback=lambda d, t, n: calls.append((d, t, n)),
    )

    # --- deletion: only ptc/efl:-sourced plans go, manual/report/id-protected stay ---
    remaining_ids = {p.stem for p in plans_dir.glob("*.yaml")}
    assert "manual_plan" in remaining_ids
    assert "report_plan" in remaining_ids
    assert "pulse_current" in remaining_ids
    assert "ptc_plan" not in remaining_ids
    assert "efl_plan" not in remaining_ids
    assert set(summary["deleted_plans"]) == {"ptc_plan", "efl_plan"}
    assert summary["deleted_drafts"] == 1
    assert summary["deleted_efls"] == 1
    assert summary["deleted_snapshots"] == 1  # older csv pruned, newest kept as fallback
    assert not (drafts_dir / "stale_draft.yaml").exists()
    assert not (efl_dir / "old.pdf").exists()
    assert not older_csv.exists()
    assert newer_csv.exists()

    # --- fetch fallback ---
    assert summary["fetched"] is False
    assert summary["snapshot_path"] == str(newer_csv)
    assert any("falling back" in n.lower() for n in summary["notes"])

    # --- download / parse (6 ONCOR rows in the fixture) ---
    assert len(summary["downloaded"]["downloaded"]) == 6
    assert len(summary["parsed"]["parsed"]) == 6

    # --- auto-promote gate: only the TXU draft clears needs_review=False + >=0.8 ---
    assert len(summary["promoted"]) == 1
    promoted_path = next(plans_dir.glob("*txu*.yaml"))
    promoted = Plan.model_validate(yaml.safe_load(promoted_path.read_text()))
    assert promoted.retrieved == dt.date.today()
    assert promoted.source.startswith("efl:")  # already set by the parser -- not clobbered

    assert len(summary["needing_review"]) == 5
    assert len(list(drafts_dir.glob("*.yaml"))) == 5  # rejected drafts stay for manual review

    # --- meterplan.com stage: also blocked/no snapshot, tolerated gracefully ---
    assert summary["meterplan"]["fetched"] is False
    assert summary["meterplan"]["snapshot_path"] is None
    assert summary["meterplan"]["imported"] == []
    assert any("meterplan.com fetch failed" in n.lower() for n in summary["notes"])

    assert calls, "progress_callback should have been invoked"
    assert all(isinstance(c[2], str) and ":" in c[2] for c in calls), "labels should be 'stage: item'"


def test_refresh_market_data_no_snapshot_available_is_graceful(refresh_dirs, monkeypatch):
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs

    def _boom_fetch(dest_dir, timeout=30.0):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", _boom_fetch)

    def _boom_meterplan_fetch(dest_dir, timeout=30.0):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(meterplan_module, "fetch_meterplan", _boom_meterplan_fetch)

    summary = app_common.refresh_market_data(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        efl_dir=efl_dir,
        ptc_dir=ptc_dir,
        meterplan_dir=meterplan_dir,
    )
    assert summary["snapshot_path"] is None
    assert summary["downloaded"] == {"downloaded": [], "skipped": [], "failed": []}
    assert summary["parsed"] == {"parsed": [], "skipped": [], "failed": []}
    assert summary["promoted"] == []
    assert any("no power to choose snapshot available" in n.lower() for n in summary["notes"])
    assert summary["meterplan"]["imported"] == []
    assert any("no meterplan.com snapshot available" in n.lower() for n in summary["notes"])


def test_refresh_market_data_fetch_false_skips_network(refresh_dirs, monkeypatch):
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs
    snapshot = ptc_dir / "ptc_only.csv"
    snapshot.write_text(FIXTURE.read_text())

    def _fail_if_called(*a, **k):
        raise AssertionError("fetch_ptc_csv should not be called when fetch=False")

    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", _fail_if_called)

    def _fail_meterplan_if_called(*a, **k):
        raise AssertionError("fetch_meterplan should not be called when fetch=False")

    monkeypatch.setattr(meterplan_module, "fetch_meterplan", _fail_meterplan_if_called)

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)
    monkeypatch.setattr(eflparser, "parse_efl", _fake_parse_efl)

    summary = app_common.refresh_market_data(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        efl_dir=efl_dir,
        ptc_dir=ptc_dir,
        meterplan_dir=meterplan_dir,
        fetch=False,
    )
    assert summary["fetched"] is False
    assert summary["snapshot_path"] == str(snapshot)
    assert any("fetch=false" in n.lower() for n in summary["notes"])
    assert summary["meterplan"]["fetched"] is False
    assert summary["meterplan"]["snapshot_path"] is None  # no meterplan snapshot on disk either
    assert any("fetch=false" in n.lower() and "meterplan" in n.lower() for n in summary["notes"])


# --------------------------------------------------------------------------- #
# Meterplan.com solar buyback plan index stage (in isolation, monkeypatched
# fetch): dedupe against surviving plans, battery skip, auto-promote of
# "simple" drafts via the existing gate.
# --------------------------------------------------------------------------- #
def test_refresh_market_data_meterplan_stage(refresh_dirs, monkeypatch):
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs

    # PTC is blocked/unavailable here -- isolates this test to the meterplan
    # stage (the full-pipeline test above covers PTC+EFL+meterplan together).
    def _boom_ptc_fetch(dest_dir, timeout=30.0):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", _boom_ptc_fetch)

    def _fake_fetch_meterplan(dest_dir, timeout=30.0):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / "meterplan_snapshot.md"
        dest_path.write_text(METERPLAN_FIXTURE.read_text())
        return dest_path

    monkeypatch.setattr(meterplan_module, "fetch_meterplan", _fake_fetch_meterplan)

    # A manual plan matching one Lubbock row by (retailer, name, term) should
    # dedupe it out via existing_plan_keys, and must survive (source=manual).
    (plans_dir / "manual_saver24.yaml").write_text(
        "id: manual_saver24\n"
        "retailer: Meter Energy\n"
        "name: Saver\n"
        "term_months: 24\n"
        "energy_rates:\n"
        "  - rate_ckwh: 11.0\n"
        "source: manual\n"
    )

    summary = app_common.refresh_market_data(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        efl_dir=efl_dir,
        ptc_dir=ptc_dir,
        meterplan_dir=meterplan_dir,
        tdu="Lubbock",
    )

    mp = summary["meterplan"]
    assert mp["fetched"] is True
    assert mp["snapshot_path"] == str(meterplan_dir / "meterplan_snapshot.md")
    # Fixture has 6 Lubbock rows: 1 "+ Battery" (skipped), 1 Saver/24 (deduped
    # against the manual plan above), 4 remaining simple plans imported.
    assert mp["skipped_battery"] == 1
    assert mp["skipped_existing"] == 1
    assert mp["flagged_for_review"] == 0
    assert len(mp["imported"]) == 4
    assert all(pid.startswith("mp_") for pid in mp["imported"])

    # All 4 are "simple" (fixed import, fixed/none export, non-free-night
    # name) -> needs_review=False + high-confidence load-bearing fields ->
    # auto-promoted by the existing gate, same as confident EFL drafts.
    assert len(summary["promoted"]) == 4
    assert set(summary["promoted"]) == set(mp["imported"])
    for pid in summary["promoted"]:
        promoted = Plan.model_validate(yaml.safe_load((plans_dir / f"{pid}.yaml").read_text()))
        assert promoted.source == "meterplan"
        assert promoted.retrieved == dt.date.today()
        assert promoted.needs_review is False
        assert promoted.tdu == "LUBBOCK"

    assert not list(drafts_dir.glob("mp_*.yaml"))  # promoted drafts are removed
    assert (plans_dir / "manual_saver24.yaml").exists()  # manual plan untouched
    assert summary["needing_review"] == []


# --------------------------------------------------------------------------- #
# Staleness helpers
# --------------------------------------------------------------------------- #
def test_interval_staleness_warning():
    from energyanalyzer.ingest.smt import QualityReport

    quality = QualityReport(source="test")
    quality.end = pd.Timestamp("2026-01-01", tz="UTC")

    stale = app_common.interval_staleness_warning(quality, as_of=dt.date(2026, 3, 1))
    assert stale is not None
    assert "2026-01-01" in stale

    fresh = app_common.interval_staleness_warning(quality, as_of=dt.date(2026, 1, 10))
    assert fresh is None

    empty = app_common.interval_staleness_warning(QualityReport(source="empty"))
    assert empty is None


def test_price_coverage_warning():
    prices = pd.Series(
        [1.0, 2.0], index=pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    )
    gap = app_common.price_coverage_warning(prices, pd.Timestamp("2026-06-01", tz="UTC"))
    assert gap is not None
    assert app_common.price_coverage_warning(prices, pd.Timestamp("2026-01-01", tz="UTC")) is None
    assert app_common.price_coverage_warning(None, pd.Timestamp("2026-01-01", tz="UTC")) is None


def test_tdu_staleness_warning():
    tariff = TduTariff(effective=dt.date(2025, 1, 1), fixed_usd_month=4.06, volumetric_ckwh=6.12)
    assert app_common.tdu_staleness_warning(tariff, as_of=dt.date(2026, 1, 1)) is not None
    assert app_common.tdu_staleness_warning(tariff, as_of=dt.date(2025, 3, 1)) is None


def test_plan_is_stale_and_stale_plan_ids():
    def _plan(id_: str, source: str, retrieved=None) -> Plan:
        return Plan(
            id=id_,
            retailer="R",
            name="N",
            term_months=12,
            energy_rates=[EnergyRate(rate_ckwh=10.0)],
            source=source,
            retrieved=retrieved,
        )

    fresh_plan = _plan("fresh", "ptc", dt.date.today() - dt.timedelta(days=10))
    stale_plan = _plan("stale", "ptc", dt.date.today() - dt.timedelta(days=200))
    unstamped_report = _plan("report_unstamped", "report-2026-07")
    unstamped_manual = _plan("manual_unstamped", "manual")

    assert app_common.plan_is_stale(fresh_plan) is False
    assert app_common.plan_is_stale(stale_plan) is True
    assert app_common.plan_is_stale(unstamped_report) is True
    assert app_common.plan_is_stale(unstamped_manual) is False

    ids = app_common.stale_plan_ids([fresh_plan, stale_plan, unstamped_report, unstamped_manual])
    assert set(ids) == {"stale", "report_unstamped"}
