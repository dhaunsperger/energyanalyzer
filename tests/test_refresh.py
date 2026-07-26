"""Tests for the "Refresh market data" pipeline and staleness helpers
(app/common.py: refresh_market_data, interval_staleness_warning,
price_coverage_warning, tdu_staleness_warning, plan_is_stale, stale_plan_ids).

refresh_market_data is exercised entirely against tmp dirs + monkeypatched
fetch/download/parse internals -- no live network, deterministic outcomes.
"""

from __future__ import annotations

import datetime as dt
import json
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


def _boom_meter_efls(*args, **kwargs):
    """Stand-in for the Meter /plans EFL fetch (a live network call). Blocked
    here like the other fetchers -- the refresh stage catches it, notes it, and
    leaves Meter's markdown rows in place (the fetch-unavailable fallback)."""
    raise RuntimeError("network blocked")


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
    monkeypatch.setattr(meterplan_module, "fetch_meterplan_efls", _boom_meter_efls)

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

    # --- quarantine: only ptc/efl:-sourced plans move, manual/report/id-protected stay ---
    remaining_ids = {p.stem for p in plans_dir.glob("*.yaml")}
    assert "manual_plan" in remaining_ids
    assert "report_plan" in remaining_ids
    assert "pulse_current" in remaining_ids
    assert set(summary["deleted_plans"]) == {"ptc_plan", "efl_plan"}
    # ...and both come BACK, because the live PTC fetch failed in this test.
    # Nothing spoke for the market, so nothing may be declared gone: a refresh
    # that cannot reach its sources must cost no data at all.
    assert "ptc_plan" in remaining_ids
    assert "efl_plan" in remaining_ids
    assert set(summary["quarantine"]["restored"]) == {"ptc_plan", "efl_plan"}
    assert summary["quarantine"]["delisted"] == []
    assert summary["deleted_drafts"] == 1
    assert summary["deleted_efls"] == 1
    assert summary["deleted_snapshots"] == 1  # older csv pruned, newest kept as fallback
    assert not (drafts_dir / "stale_draft.yaml").exists()
    # old.pdf is an orphan -- no surviving plan was parsed from it, so it is not
    # resurrected (that would regenerate a draft from it on every future run).
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
    monkeypatch.setattr(meterplan_module, "fetch_meterplan_efls", _boom_meter_efls)

    summary = app_common.refresh_market_data(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        efl_dir=efl_dir,
        ptc_dir=ptc_dir,
        meterplan_dir=meterplan_dir,
    )
    assert summary["snapshot_path"] is None
    assert summary["downloaded"] == {"downloaded": [], "skipped": [], "failed": [], "deferred": []}
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

    def _fail_meter_efls_if_called(*a, **k):
        raise AssertionError("fetch_meterplan_efls should not be called when fetch=False")

    monkeypatch.setattr(meterplan_module, "fetch_meterplan_efls", _fail_meter_efls_if_called)

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
    # Meter's /plans EFL fetch unavailable here -> Meter's markdown rows import
    # normally (this test exercises the markdown fallback path).
    monkeypatch.setattr(meterplan_module, "fetch_meterplan_efls", _boom_meter_efls)

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
def test_efl_pdf_health_flags_non_pdf_files(tmp_path):
    efl_dir = tmp_path / "efl"
    efl_dir.mkdir()
    (efl_dir / "good.pdf").write_bytes(b"%PDF-1.5\n...real content...")
    (efl_dir / "leading_junk.pdf").write_bytes(b"\n\n%PDF-1.4 ok")  # tolerated
    (efl_dir / "html_error.pdf").write_bytes(b"<!DOCTYPE html><html>nope</html>")
    (efl_dir / "captcha.pdf").write_bytes(b"<html><meta http-equiv='refresh'></html>")

    health = app_common.efl_pdf_health(efl_dir)
    assert health["total"] == 4
    assert set(health["invalid"]) == {"html_error.pdf", "captcha.pdf"}


def test_efl_pdf_health_empty_dir(tmp_path):
    assert app_common.efl_pdf_health(tmp_path / "missing") == {"total": 0, "invalid": []}


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


# --------------------------------------------------------------------------- #
# finish_refresh: the recovery path for an interrupted run
# --------------------------------------------------------------------------- #
def _draft(drafts_dir: Path, plan_id: str, *, confident: bool) -> Path:
    """A draft that either clears the auto-promote gate or doesn't."""
    conf = 0.95 if confident else 0.4
    path = drafts_dir / f"{plan_id}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": plan_id,
                "retailer": "Test REP",
                "name": plan_id,
                "term_months": 12,
                "base_charge_usd": 0.0,
                "energy_rates": [{"rate_ckwh": 10.0, "window": None}],
                "buyback": {"kind": "none"},
                "needs_review": not confident,
                "source": "efl:x.pdf",
                "_parse": {"confidence": {"energy_charge": conf, "base_charge": conf}},
            }
        )
    )
    return path


def test_finish_refresh_promotes_confident_drafts_left_by_an_interrupted_run(refresh_dirs):
    """The real-world case: fetch/download/parse completed and wrote drafts, then
    the run was killed before auto-promote. Re-running just this stage must
    complete the database without repeating the sweep."""
    plans_dir, drafts_dir = refresh_dirs[0], refresh_dirs[1]
    _draft(drafts_dir, "confident_plan_12mo", confident=True)
    _draft(drafts_dir, "shaky_plan_12mo", confident=False)

    summary = app_common.finish_refresh(plans_dir=plans_dir, drafts_dir=drafts_dir)

    assert summary["promoted"] == ["confident_plan_12mo"]
    assert summary["needing_review"] == ["shaky_plan_12mo"]
    assert (plans_dir / "confident_plan_12mo.yaml").exists()
    assert not (drafts_dir / "confident_plan_12mo.yaml").exists()  # draft consumed
    assert (drafts_dir / "shaky_plan_12mo.yaml").exists()  # left for review


def test_finish_refresh_is_idempotent(refresh_dirs):
    """Clicking "Finish incomplete refresh" twice must not error or double-write."""
    plans_dir, drafts_dir = refresh_dirs[0], refresh_dirs[1]
    _draft(drafts_dir, "confident_plan_12mo", confident=True)

    first = app_common.finish_refresh(plans_dir=plans_dir, drafts_dir=drafts_dir)
    second = app_common.finish_refresh(plans_dir=plans_dir, drafts_dir=drafts_dir)

    assert first["promoted"] == ["confident_plan_12mo"]
    assert second["promoted"] == []  # nothing left to do
    assert len(list(plans_dir.glob("*.yaml"))) == 1


def test_finish_refresh_on_empty_drafts_dir_is_a_noop(refresh_dirs):
    summary = app_common.finish_refresh(
        plans_dir=refresh_dirs[0], drafts_dir=refresh_dirs[1]
    )
    assert summary == {"promoted": [], "needing_review": [], "meterplan_superseded": [], "notes": []}


# --------------------------------------------------------------------------- #
# promote_all_drafts: the "quick look" bulk promote
# --------------------------------------------------------------------------- #
def test_promote_all_drafts_bypasses_the_confidence_gate_but_keeps_flags(refresh_dirs):
    """Every draft lands in the database, and an unconfident one arrives still
    flagged -- the gate is bypassed, the warning is not."""
    plans_dir, drafts_dir = refresh_dirs[0], refresh_dirs[1]
    _draft(drafts_dir, "confident_plan_12mo", confident=True)
    _draft(drafts_dir, "shaky_plan_12mo", confident=False)

    summary = app_common.promote_all_drafts(plans_dir=plans_dir, drafts_dir=drafts_dir)

    assert sorted(summary["promoted"]) == ["confident_plan_12mo", "shaky_plan_12mo"]
    assert summary["failed"] == []
    assert summary["flagged"] == 1  # only the shaky one

    shaky = Plan.model_validate(yaml.safe_load((plans_dir / "shaky_plan_12mo.yaml").read_text()))
    confident = Plan.model_validate(
        yaml.safe_load((plans_dir / "confident_plan_12mo.yaml").read_text())
    )
    assert shaky.needs_review is True, "an unverified plan must stay badged in the database"
    assert confident.needs_review is False, "the parser's verdict is preserved, not overwritten"
    assert shaky.retrieved == dt.date.today()
    # Move semantics: drafts are consumed.
    assert list(drafts_dir.glob("*.yaml")) == []


def test_promote_all_drafts_leaves_invalid_drafts_in_place(refresh_dirs):
    """A draft that can't validate must not vanish silently -- it stays on disk
    and is reported, so nothing is lost to a bulk click."""
    plans_dir, drafts_dir = refresh_dirs[0], refresh_dirs[1]
    _draft(drafts_dir, "good_plan_12mo", confident=True)
    (drafts_dir / "broken.yaml").write_text(yaml.safe_dump({"id": "broken", "term_months": -5}))

    summary = app_common.promote_all_drafts(plans_dir=plans_dir, drafts_dir=drafts_dir)

    assert summary["promoted"] == ["good_plan_12mo"]
    assert len(summary["failed"]) == 1
    assert summary["failed"][0]["draft"] == "broken.yaml"
    assert (drafts_dir / "broken.yaml").exists()
    assert not (plans_dir / "broken.yaml").exists()


def test_promote_all_drafts_on_empty_dir_is_a_noop(refresh_dirs):
    summary = app_common.promote_all_drafts(
        plans_dir=refresh_dirs[0], drafts_dir=refresh_dirs[1]
    )
    assert summary == {"promoted": [], "failed": [], "flagged": 0}


def test_discovery_falls_back_to_manual_capture_when_a_live_render_fails(tmp_path, monkeypatch):
    """A probabilistic WAF (Ambit) or a nav-flow drift must not cost a REP all of
    its plans when a good capture is sitting on disk."""
    from energyanalyzer.fetchers import rep_discovery as rd

    snapshot_dir = tmp_path / "rep_discovery"
    snapshot_dir.mkdir()
    (snapshot_dir / "ambit_20260101T000000Z.html").write_text("<html>saved capture</html>")

    def _boom(*a, **k):
        raise RuntimeError("Blocked by WAF")

    seen = {}

    def _fake_discover(html, config, **k):
        seen["html"] = html
        return []

    monkeypatch.setattr(rd, "fetch_rendered_html", _boom)
    monkeypatch.setattr(rd, "discover", _fake_discover)
    monkeypatch.setattr(rd, "download_discovered", lambda *a, **k: {"downloaded": [], "skipped": [], "failed": [], "filtered_out": 0})

    out = app_common._run_rep_discovery(
        zip_code="78665",
        efl_dir=tmp_path / "efl",
        drafts_dir=tmp_path / "drafts",
        plans_dir=tmp_path / "plans",
        reps=["ambit"],
        snapshot_dir=snapshot_dir,
    )
    rep = out["reps"]["ambit"]
    # "empty": the capture was read but yielded no plans (the fake discover
    # returns []). What this test pins is the fallback itself -- the saved
    # capture was used instead of the failed live render.
    assert rep["status"] == "empty", rep
    assert "ambit_20260101T000000Z.html" in rep["detail"]
    assert seen["html"] == "<html>saved capture</html>"


def test_discovery_reports_error_when_render_fails_and_no_capture_exists(tmp_path, monkeypatch):
    """Without a capture to fall back on, the failure must surface -- never be
    quietly swallowed into a zero-plan 'ok'."""
    from energyanalyzer.fetchers import rep_discovery as rd

    monkeypatch.setattr(rd, "fetch_rendered_html", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Blocked by WAF")))
    out = app_common._run_rep_discovery(
        zip_code="78665",
        efl_dir=tmp_path / "efl",
        drafts_dir=tmp_path / "drafts",
        plans_dir=tmp_path / "plans",
        reps=["ambit"],
        snapshot_dir=tmp_path / "empty",
    )
    assert out["reps"]["ambit"]["status"] == "error"
    assert "WAF" in out["reps"]["ambit"]["detail"]


def test_refresh_threads_llm_assist_through_to_every_parse_stage(refresh_dirs, monkeypatch):
    """The "Pre-fill unreadable fields with the local LLM" checkbox was wired to
    `parse_downloaded_efls` but NOT to `refresh_market_data`, so ticking it did
    nothing for a full refresh -- the case that matters most, since a refresh
    re-parses every EFL and silently re-breaks the ones with damaged fonts
    (Atlantex's "$19.95" base charge) on every run.
    """
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs
    seen: list[bool] = []

    def _spy(pdf_paths, drafts_dir=None, plans_dir=None, progress_callback=None, llm_assist=False, **kw):
        seen.append(llm_assist)
        return {"parsed": [], "skipped": [], "failed": [], "llm_assisted": []}

    monkeypatch.setattr(app_common, "parse_downloaded_efls", _spy)
    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    monkeypatch.setattr(meterplan_module, "fetch_meterplan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    monkeypatch.setattr(meterplan_module, "fetch_meterplan_efls", _boom_meter_efls)
    (ptc_dir / "snap.csv").write_text(FIXTURE.read_text())

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)
    monkeypatch.setattr(eflparser, "parse_efl", _fake_parse_efl)

    app_common.refresh_market_data(
        plans_dir=plans_dir, drafts_dir=drafts_dir, efl_dir=efl_dir,
        ptc_dir=ptc_dir, meterplan_dir=meterplan_dir, llm_assist=True,
    )
    assert seen, "refresh should have reached a parse stage"
    assert all(seen), "every parse stage must receive llm_assist=True"


def test_refresh_defaults_llm_assist_off(refresh_dirs, monkeypatch):
    """Off by default -- the LLM tier is optional and must never run unasked."""
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs
    seen: list[bool] = []
    monkeypatch.setattr(app_common, "parse_downloaded_efls",
        lambda *a, llm_assist=False, **k: (seen.append(llm_assist),
            {"parsed": [], "skipped": [], "failed": []})[1])
    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    monkeypatch.setattr(meterplan_module, "fetch_meterplan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    monkeypatch.setattr(meterplan_module, "fetch_meterplan_efls", _boom_meter_efls)
    (ptc_dir / "snap.csv").write_text(FIXTURE.read_text())
    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)
    monkeypatch.setattr(eflparser, "parse_efl", _fake_parse_efl)
    app_common.refresh_market_data(
        plans_dir=plans_dir, drafts_dir=drafts_dir, efl_dir=efl_dir,
        ptc_dir=ptc_dir, meterplan_dir=meterplan_dir,
    )
    assert seen and not any(seen)


# --------------------------------------------------------------------------- #
# EFL identity: an EFL is never anonymous
# --------------------------------------------------------------------------- #
def test_known_efl_identities_reads_discovery_manifest_and_ptc(tmp_path):
    """Discovery records retailer/plan per download; PTC's snapshot row supplies
    both and is what the saved filename was built from. Either way the identity
    is known before the parser ever opens the PDF."""
    efl_dir, ptc_dir = tmp_path / "efl", tmp_path / "ptc"
    efl_dir.mkdir()
    ptc_dir.mkdir()
    (efl_dir / "rep_discovery_manifest.jsonl").write_text(
        '{"file": "/abs/path/Atlantex_Power_Solar_Buy_Back_Plan.pdf", '
        '"retailer": "Atlantex Power", "plan_name": "Solar Buy Back Plan"}\n'
        '{"bad json"\n'                                    # tolerated, not fatal
    )
    ident = app_common.known_efl_identities(efl_dir=efl_dir, ptc_dir=ptc_dir)
    assert ident["Atlantex_Power_Solar_Buy_Back_Plan.pdf"] == (
        "Atlantex Power", "Solar Buy Back Plan"
    )


def test_parse_restores_identity_and_rebuilds_the_id(refresh_dirs, monkeypatch):
    """A damaged-font EFL parses as "Unknown Retailer"/"Unnamed Plan", which is
    both useless in the UI and a COLLISION -- several such plans differ only by
    contract term and would overwrite each other's draft file. The download step
    knew the real names, so the draft id must be rebuilt from them."""
    plans_dir, drafts_dir, efl_dir, ptc_dir, _ = refresh_dirs
    pdf = efl_dir / "Atlantex_Power_Solar_Buy_Back_Plan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    (efl_dir / "rep_discovery_manifest.jsonl").write_text(
        f'{{"file": "{pdf}", "retailer": "Atlantex Power", "plan_name": "Solar Buy Back Plan"}}\n'
    )

    def _unreadable(path):
        return eflparser.DraftPlan(
            plan_dict={
                "id": "unknown_retailer_unnamed_plan_12mo",
                "retailer": "Unknown Retailer", "name": "Unnamed Plan",
                "term_months": 12, "base_charge_usd": 0.0,
                "energy_rates": [{"rate_ckwh": 5.59}], "buyback": {"kind": "none"},
                "source": f"efl:{Path(path).name}", "needs_review": True,
            },
            confidence={"energy_charge": 0.6}, evidence={}, unparsed_notes=[],
        )

    monkeypatch.setattr(eflparser, "parse_efl", _unreadable)
    out = app_common.parse_downloaded_efls([pdf], drafts_dir=drafts_dir, plans_dir=plans_dir)

    assert out["parsed"] == ["atlantex_power_solar_buy_back_plan_12mo"]
    assert out["identified"][0]["file"] == "Atlantex_Power_Solar_Buy_Back_Plan.pdf"
    saved = yaml.safe_load((drafts_dir / "atlantex_power_solar_buy_back_plan_12mo.yaml").read_text())
    assert saved["retailer"] == "Atlantex Power"
    assert saved["name"] == "Solar Buy Back Plan"
    assert not (drafts_dir / "unknown_retailer_unnamed_plan_12mo.yaml").exists()


def test_parse_does_not_override_an_identity_the_parser_read(refresh_dirs, monkeypatch):
    """Only the placeholders are replaced -- a retailer the parser read from the
    document itself is more trustworthy than a download-time label."""
    plans_dir, drafts_dir, efl_dir, ptc_dir, _ = refresh_dirs
    pdf = efl_dir / "Some_Retailer_Some_Plan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    (efl_dir / "rep_discovery_manifest.jsonl").write_text(
        f'{{"file": "{pdf}", "retailer": "WRONG", "plan_name": "WRONG"}}\n'
    )
    monkeypatch.setattr(eflparser, "parse_efl", _fake_parse_efl)
    out = app_common.parse_downloaded_efls([pdf], drafts_dir=drafts_dir, plans_dir=plans_dir)
    assert out["identified"] == []
    saved = yaml.safe_load((drafts_dir / f"{out['parsed'][0]}.yaml").read_text())
    assert saved["retailer"] == "Test Retailer"


def test_manual_efls_survive_the_refresh_wipe(tmp_path):
    """Hand-supplied EFLs must outlive a refresh, which deletes what it refetches.

    Regression guard: the wipe's glob("*.pdf") deleted Ambit's hand-saved EFLs
    (added after its WAF began refusing every client), leaving only their
    Zone.Identifier stubs. Nothing could restore them -- that is precisely why
    they were manual.
    """
    efl_dir = tmp_path / "efl"
    manual = efl_dir / "manual"
    manual.mkdir(parents=True)
    fetched = efl_dir / "Fetched_Plan.pdf"
    fetched.write_bytes(b"%PDF-1.4 refetchable")
    by_hand = manual / "Ambit_Texas_Solar_Buyback_12.pdf"
    by_hand.write_bytes(b"%PDF-1.4 saved by hand")

    # What the wipe sees, and what the parse stage sees.
    wiped = sorted(efl_dir.glob("*.pdf"))
    assert wiped == [fetched], "the wipe must not reach data/efl/manual/"
    assert app_common.manual_efl_paths(efl_dir) == [by_hand]

    for p in wiped:
        p.unlink()
    assert by_hand.exists()
    assert app_common.manual_efl_paths(efl_dir) == [by_hand]


# --------------------------------------------------------------------------- #
# Quarantine / reconcile
# --------------------------------------------------------------------------- #
def _q_plan(path, plan_id, retailer, name, term, source="ptc"):
    path.write_text(
        "id: {id}\nretailer: {r}\nname: {n}\nterm_months: {t}\n"
        "base_charge_usd: 0.0\nenergy_rates:\n- label: ''\n  rate_ckwh: 12.0\n  window: null\n"
        "tdu_passthrough: true\nrate_type: fixed\nsource: {s}\n".format(
            id=plan_id, r=retailer, n=name, t=term, s=source
        )
    )


def _setup_quarantine(tmp_path, listings, ptc_ok=True, meterplan_ok=True):
    plans_dir = tmp_path / "plans"
    q = tmp_path / "q"
    plans_dir.mkdir()
    (q / "plans").mkdir(parents=True)
    (q / "efl").mkdir(parents=True)
    (q / app_common.QUARANTINE_AUTHORITY_FILE).write_text(
        json.dumps({"ptc_ok": ptc_ok, "meterplan_ok": meterplan_ok,
                    "coverage_retailers": [], "listings": listings})
    )
    return plans_dir, q


def test_quarantined_plan_still_listed_is_restored_unflagged(tmp_path):
    """The download failed, not the plan. Our copy is the last good one."""
    plans_dir, q = _setup_quarantine(tmp_path, [["Gexa Energy", "Gexa 12", 12]])
    _q_plan(q / "plans" / "gexa_12.yaml", "gexa_12", "Gexa Energy", "Gexa 12", 12)

    out = app_common.reconcile_quarantine(plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q)

    assert out["restored"] == ["gexa_12"]
    assert out["delisted"] == []
    restored = yaml.safe_load((plans_dir / "gexa_12.yaml").read_text())
    assert not restored.get("needs_review")


def test_quarantined_plan_a_live_source_no_longer_lists_is_flagged(tmp_path):
    """PTC was reached and does not carry it -- kept, but marked, not deleted."""
    plans_dir, q = _setup_quarantine(tmp_path, [["Gexa Energy", "Gexa 12", 12]])
    _q_plan(q / "plans" / "gone_24.yaml", "gone_24", "Defunct Power", "Vanished 24", 24)

    out = app_common.reconcile_quarantine(plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q)

    assert out["delisted"] == ["gone_24"]
    kept = yaml.safe_load((plans_dir / "gone_24.yaml").read_text())
    assert kept["needs_review"] is True
    assert "no longer" in kept["notes"].lower() or "not listed" in kept["notes"].lower()


def test_nothing_is_delisted_when_no_source_completed(tmp_path):
    """The whole point: a broken run must cost nothing.

    This is what made the 2026-07-26 refresh expensive -- Direct Energy's
    harvester returned empty and its two real plans had already been deleted.
    """
    plans_dir, q = _setup_quarantine(tmp_path, [], ptc_ok=False, meterplan_ok=False)
    _q_plan(q / "plans" / "solar_12.yaml", "solar_12", "Direct Energy", "Direct Solar Unlimited 12", 12)

    out = app_common.reconcile_quarantine(plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q)

    assert out["restored"] == ["solar_12"]
    assert out["delisted"] == []
    assert not yaml.safe_load((plans_dir / "solar_12.yaml").read_text()).get("needs_review")


def test_rederived_plan_drops_its_quarantine_copy(tmp_path):
    plans_dir, q = _setup_quarantine(tmp_path, [["Gexa Energy", "Gexa 12", 12]])
    _q_plan(q / "plans" / "gexa_12.yaml", "gexa_12", "Gexa Energy", "Gexa 12", 12)
    _q_plan(plans_dir / "gexa_12.yaml", "gexa_12", "Gexa Energy", "Gexa 12", 12, source="efl:new.pdf")

    out = app_common.reconcile_quarantine(plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q)

    assert out["dropped"] == 1
    assert out["restored"] == [] and out["delisted"] == []
    # The freshly-parsed plan wins; the old copy does not overwrite it.
    assert yaml.safe_load((plans_dir / "gexa_12.yaml").read_text())["source"] == "efl:new.pdf"


def test_restored_plans_bring_back_only_their_own_efls(tmp_path):
    plans_dir, q = _setup_quarantine(tmp_path, [], ptc_ok=False, meterplan_ok=False)
    _q_plan(q / "plans" / "s12.yaml", "s12", "Direct Energy", "Direct Solar Unlimited 12", 12,
            source="efl:Direct_Solar_12.pdf")
    (q / "efl" / "Direct_Solar_12.pdf").write_bytes(b"%PDF-1.4 wanted")
    (q / "efl" / "Orphan.pdf").write_bytes(b"%PDF-1.4 nothing points here")
    efl_dir = tmp_path / "efl"
    efl_dir.mkdir()

    out = app_common.reconcile_quarantine(plans_dir=plans_dir, efl_dir=efl_dir, quarantine_dir=q)

    assert out["efls_restored"] == 1
    assert (efl_dir / "Direct_Solar_12.pdf").exists()
    assert not (efl_dir / "Orphan.pdf").exists()


def test_brand_plus_term_names_do_not_cover_a_different_term(tmp_path):
    """The fallback for name-less plans must not make every term interchangeable.

    "Gexa 12" has no distinctive tokens once brand and term are stripped, so
    the strict rule can never match it and the reconciler falls back to
    retailer + term. That fallback has to keep the term strict, or a listing
    for Gexa 12 would vouch for a Gexa 24 that PTC actually dropped.
    """
    plans_dir, q = _setup_quarantine(tmp_path, [["Gexa Energy", "Gexa 12", 12]])
    _q_plan(q / "plans" / "gexa_24.yaml", "gexa_24", "Gexa Energy", "Gexa 24", 24)

    out = app_common.reconcile_quarantine(plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q)

    assert out["delisted"] == ["gexa_24"]
    assert out["restored"] == []


def test_discovery_coverage_listing_without_a_term_still_matches(tmp_path):
    """Discovery coverage publishes plan names only -- no term column.

    A term of None must be read as "this source didn't say", not as a mismatch
    against every plan (which would flag a fully-scraped REP's whole catalogue
    as delisted).
    """
    plans_dir, q = _setup_quarantine(tmp_path, [["Champion Energy", "Champ Saver-24", None]])
    _q_plan(q / "plans" / "champ_24.yaml", "champ_24", "Champion Energy", "Champ Saver 24", 24)

    out = app_common.reconcile_quarantine(plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q)

    assert out["restored"] == ["champ_24"]
    assert out["delisted"] == []


def test_precedence_confident_old_copy_outranks_an_unverified_new_draft(tmp_path):
    """Precedence is confidence first, then freshness.

    A verified reading must not be displaced by an unverified one: it stays
    rankable while its replacement waits in review. The alternative -- dropping
    it because a draft exists -- removes the plan from the ranking entirely and
    loses the value for good, since the quarantine is wiped next run.
    """
    plans_dir, q = _setup_quarantine(tmp_path, [], ptc_ok=True, meterplan_ok=True)
    drafts_dir = tmp_path / "drafts"
    drafts_dir.mkdir()
    _q_plan(q / "plans" / "gexa_12.yaml", "gexa_12", "Gexa Energy", "Gexa 12", 12)  # verified
    _q_plan(drafts_dir / "gexa_12.yaml", "gexa_12", "Gexa Energy", "Gexa 12", 12)   # new, in review

    out = app_common.reconcile_quarantine(
        plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q, drafts_dir=drafts_dir
    )

    assert out["kept_pending_review"] == ["gexa_12"]
    assert out["delisted"] == []
    assert (plans_dir / "gexa_12.yaml").exists(), "the verified copy must stay rankable"
    assert (drafts_dir / "gexa_12.yaml").exists(), "its replacement must stay in review"


def test_precedence_unreviewed_old_copy_loses_to_the_fresher_draft(tmp_path):
    """An old copy that was never verified is just an older guess -- drop it."""
    plans_dir, q = _setup_quarantine(tmp_path, [], ptc_ok=True, meterplan_ok=True)
    drafts_dir = tmp_path / "drafts"
    drafts_dir.mkdir()
    stale = q / "plans" / "gexa_12.yaml"
    _q_plan(stale, "gexa_12", "Gexa Energy", "Gexa 12", 12)
    stale.write_text(stale.read_text() + "needs_review: true\n")
    _q_plan(drafts_dir / "gexa_12.yaml", "gexa_12", "Gexa Energy", "Gexa 12", 12)

    out = app_common.reconcile_quarantine(
        plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q, drafts_dir=drafts_dir
    )

    assert out["dropped"] == 1
    assert out["kept_pending_review"] == [] and out["restored"] == []
    assert not (plans_dir / "gexa_12.yaml").exists()


def test_a_rebuilt_plan_is_never_flagged_delisted(tmp_path):
    """Whatever the run rebuilt is still sold, however the name parses.

    Five Just Energy plans were flagged as gone from a PTC snapshot that still
    listed all five: the parser reads their EFL header as retailer "Just Energy
    - Fixed Rate Product: Basics PTC - 24" and name "For Service Area: Oncor",
    so the name carries no product identity to match a listing with.
    """
    plans_dir, q = _setup_quarantine(tmp_path, [], ptc_ok=True, meterplan_ok=True)
    drafts_dir = tmp_path / "drafts"
    drafts_dir.mkdir()
    stale = q / "plans" / "je_24.yaml"
    _q_plan(stale, "je_24", "Just Energy - Fixed Rate Product: Basics PTC - 24",
            "For Service Area: Oncor", 24)
    stale.write_text(stale.read_text() + "needs_review: true\n")
    _q_plan(drafts_dir / "je_24.yaml", "je_24", "Just Energy", "Basics PTC - 24", 24)

    out = app_common.reconcile_quarantine(
        plans_dir=plans_dir, efl_dir=tmp_path / "efl", quarantine_dir=q, drafts_dir=drafts_dir
    )

    assert out["delisted"] == []
    assert (drafts_dir / "je_24.yaml").exists()
