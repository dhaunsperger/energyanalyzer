"""Tests for the REP-site discovery stage wired into the refresh pipeline
(app/common.py: `_run_rep_discovery`, and the stage-6.5 block inside
`refresh_market_data`).

Every test monkeypatches `energyanalyzer.fetchers.rep_discovery` functions on
the REAL module object (`_run_rep_discovery` does `from
energyanalyzer.fetchers import rep_discovery as rd` INSIDE the function, so
patching the module attribute is what actually takes effect) -- no test here
ever launches a real Playwright browser or touches the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from energyanalyzer.app import common as app_common
from energyanalyzer.fetchers import rep_discovery as rd_module
from energyanalyzer.fetchers.rep_discovery import DiscoveredPlan, RepConfig


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def discovery_dirs(tmp_path: Path):
    efl_dir = tmp_path / "efl"
    drafts_dir = tmp_path / "drafts"
    plans_dir = tmp_path / "plans"
    snapshot_dir = tmp_path / "rep_discovery"
    for d in (efl_dir, drafts_dir, plans_dir, snapshot_dir):
        d.mkdir(parents=True)
    return efl_dir, drafts_dir, plans_dir, snapshot_dir


def _harvester_config(key: str = "champion", retailer: str = "Champion") -> RepConfig:
    return RepConfig(key=key, retailer=retailer, homepage="https://example.test", harvester=lambda p, z, cfg: [])


def _render_config(key: str = "gexa", retailer: str = "Gexa") -> RepConfig:
    return RepConfig(
        key=key,
        retailer=retailer,
        homepage="https://example.test",
        extractor=lambda h, cfg: [],
        render=lambda p, z: None,
    )


def _manual_config(key: str = "ambit", retailer: str = "Ambit") -> RepConfig:
    return RepConfig(
        key=key,
        retailer=retailer,
        homepage="https://example.test",
        extractor=lambda h, cfg: [],
        render=None,
    )


def _plan(retailer: str, name: str, buyback: bool = True) -> DiscoveredPlan:
    return DiscoveredPlan(
        retailer=retailer,
        plan_name=name,
        efl_url=f"https://example.test/{name}.pdf",
        is_buyback=buyback,
    )


def _fail_if_called(*a, **k):
    raise AssertionError("this should not have been called")


# --------------------------------------------------------------------------- #
# 1. Dispatch + aggregation happy path
# --------------------------------------------------------------------------- #
def test_dispatch_and_aggregation_happy_path(discovery_dirs, monkeypatch):
    efl_dir, drafts_dir, plans_dir, snapshot_dir = discovery_dirs

    harvester_cfg = _harvester_config("champion", "Champion")
    render_cfg = _render_config("gexa", "Gexa")
    manual_cfg = _manual_config("ambit", "Ambit")

    # Manual rep needs a snapshot file on disk to avoid manual-needed.
    (snapshot_dir / "ambit_20260101T000000Z.html").write_text("<html>fixture</html>")

    monkeypatch.setattr(
        rd_module,
        "REP_CONFIGS",
        {"champion": harvester_cfg, "gexa": render_cfg, "ambit": manual_cfg},
    )

    harvester_plan = _plan("Champion", "Champion Buyback")
    render_plan = _plan("Gexa", "Gexa Buyback")
    manual_plan = _plan("Ambit", "Ambit Buyback")

    monkeypatch.setattr(rd_module, "harvest_live", lambda cfg, zip_, headless=True: [harvester_plan])
    monkeypatch.setattr(
        rd_module,
        "fetch_rendered_html",
        lambda cfg, zip_, headless=True, snapshot_dir=None: ("<html>rendered</html>", snapshot_dir / "x.html"),
    )

    discover_calls: list[str] = []

    def _fake_discover(html, cfg):
        discover_calls.append(cfg.key)
        if cfg.key == "gexa":
            return [render_plan]
        if cfg.key == "ambit":
            return [manual_plan]
        return []

    monkeypatch.setattr(rd_module, "discover", _fake_discover)

    download_calls = {}

    def _fake_download_discovered(plans, dest, progress_callback=None):
        download_calls["plans"] = list(plans)
        download_calls["dest"] = dest
        if progress_callback:
            progress_callback(1, 1, "done")
        return {"downloaded": ["a.pdf"], "skipped": [], "failed": [], "filtered_out": 0}

    monkeypatch.setattr(rd_module, "download_discovered", _fake_download_discovered)

    parse_calls = {}

    def _fake_parse_downloaded_efls(pdf_paths, drafts_dir=None, plans_dir=None, progress_callback=None):
        parse_calls["pdf_paths"] = list(pdf_paths)
        return {"parsed": ["x"], "skipped": [], "failed": []}

    monkeypatch.setattr(app_common, "parse_downloaded_efls", _fake_parse_downloaded_efls)

    result = app_common._run_rep_discovery(
        "78665",
        efl_dir=efl_dir,
        drafts_dir=drafts_dir,
        plans_dir=plans_dir,
        snapshot_dir=snapshot_dir,
    )

    for key in ("champion", "gexa", "ambit"):
        rep = result["reps"][key]
        assert rep["status"] == "ok"
        assert rep["plans_found"] == 1
        assert rep["buyback"] == 1

    assert len(download_calls["plans"]) == 3
    assert {p.plan_name for p in download_calls["plans"]} == {
        "Champion Buyback",
        "Gexa Buyback",
        "Ambit Buyback",
    }
    assert download_calls["dest"] == efl_dir

    assert parse_calls["pdf_paths"] == [Path("a.pdf")]
    assert result["downloaded"] == {"downloaded": ["a.pdf"], "skipped": [], "failed": [], "filtered_out": 0}
    assert result["parsed"] == {"parsed": ["x"], "skipped": [], "failed": []}


# --------------------------------------------------------------------------- #
# 2. manual-needed
# --------------------------------------------------------------------------- #
def test_manual_rep_without_snapshot_is_manual_needed(discovery_dirs, monkeypatch):
    efl_dir, drafts_dir, plans_dir, snapshot_dir = discovery_dirs
    manual_cfg = _manual_config("ambit", "Ambit")
    monkeypatch.setattr(rd_module, "REP_CONFIGS", {"ambit": manual_cfg})
    monkeypatch.setattr(rd_module, "harvest_live", _fail_if_called)
    monkeypatch.setattr(rd_module, "fetch_rendered_html", _fail_if_called)
    monkeypatch.setattr(rd_module, "discover", _fail_if_called)
    monkeypatch.setattr(rd_module, "download_discovered", _fail_if_called)
    monkeypatch.setattr(app_common, "parse_downloaded_efls", _fail_if_called)

    # snapshot_dir exists but has no ambit_*.html file in it.
    result = app_common._run_rep_discovery(
        "78665",
        efl_dir=efl_dir,
        drafts_dir=drafts_dir,
        plans_dir=plans_dir,
        snapshot_dir=snapshot_dir,
    )

    rep = result["reps"]["ambit"]
    assert rep["status"] == "manual-needed"
    assert rep["plans_found"] == 0
    assert rep["buyback"] == 0
    assert result["downloaded"] == {"downloaded": [], "skipped": [], "failed": [], "filtered_out": 0}
    assert result["parsed"] == {"parsed": [], "skipped": [], "failed": []}


# --------------------------------------------------------------------------- #
# 3. per-REP error isolation
# --------------------------------------------------------------------------- #
def test_per_rep_error_is_isolated(discovery_dirs, monkeypatch):
    efl_dir, drafts_dir, plans_dir, snapshot_dir = discovery_dirs
    harvester_cfg = _harvester_config("champion", "Champion")
    render_cfg = _render_config("gexa", "Gexa")
    monkeypatch.setattr(rd_module, "REP_CONFIGS", {"champion": harvester_cfg, "gexa": render_cfg})

    def _boom_harvest_live(cfg, zip_, headless=True):
        raise RuntimeError("playwright missing")

    monkeypatch.setattr(rd_module, "harvest_live", _boom_harvest_live)

    render_plan = _plan("Gexa", "Gexa Buyback")
    monkeypatch.setattr(
        rd_module,
        "fetch_rendered_html",
        lambda cfg, zip_, headless=True, snapshot_dir=None: ("<html></html>", snapshot_dir / "x.html"),
    )
    monkeypatch.setattr(rd_module, "discover", lambda html, cfg: [render_plan])
    monkeypatch.setattr(
        rd_module,
        "download_discovered",
        lambda plans, dest, progress_callback=None: {
            "downloaded": ["gexa.pdf"],
            "skipped": [],
            "failed": [],
            "filtered_out": 0,
        },
    )
    monkeypatch.setattr(
        app_common,
        "parse_downloaded_efls",
        lambda pdf_paths, drafts_dir=None, plans_dir=None, progress_callback=None: {
            "parsed": [],
            "skipped": [],
            "failed": [],
        },
    )

    result = app_common._run_rep_discovery(
        "78665",
        efl_dir=efl_dir,
        drafts_dir=drafts_dir,
        plans_dir=plans_dir,
        snapshot_dir=snapshot_dir,
    )

    champion = result["reps"]["champion"]
    assert champion["status"] == "error"
    assert "RuntimeError" in champion["detail"]
    assert "playwright missing" in champion["detail"]

    gexa = result["reps"]["gexa"]
    assert gexa["status"] == "ok"
    assert gexa["plans_found"] == 1
    assert gexa["buyback"] == 1


# --------------------------------------------------------------------------- #
# 4. unknown rep key
# --------------------------------------------------------------------------- #
def test_unknown_rep_key_is_reported_as_error(discovery_dirs, monkeypatch):
    efl_dir, drafts_dir, plans_dir, snapshot_dir = discovery_dirs
    monkeypatch.setattr(rd_module, "REP_CONFIGS", {})
    monkeypatch.setattr(rd_module, "harvest_live", _fail_if_called)
    monkeypatch.setattr(rd_module, "fetch_rendered_html", _fail_if_called)
    monkeypatch.setattr(rd_module, "discover", _fail_if_called)
    monkeypatch.setattr(rd_module, "download_discovered", _fail_if_called)
    monkeypatch.setattr(app_common, "parse_downloaded_efls", _fail_if_called)

    result = app_common._run_rep_discovery(
        "78665",
        efl_dir=efl_dir,
        drafts_dir=drafts_dir,
        plans_dir=plans_dir,
        reps=["nope"],
        snapshot_dir=snapshot_dir,
    )

    rep = result["reps"]["nope"]
    assert rep["status"] == "error"
    assert "unknown" in rep["detail"].lower()


# --------------------------------------------------------------------------- #
# 5. empty result short-circuit
# --------------------------------------------------------------------------- #
def test_no_plans_found_skips_download_and_parse(discovery_dirs, monkeypatch):
    efl_dir, drafts_dir, plans_dir, snapshot_dir = discovery_dirs
    harvester_cfg = _harvester_config("champion", "Champion")
    render_cfg = _render_config("gexa", "Gexa")
    monkeypatch.setattr(rd_module, "REP_CONFIGS", {"champion": harvester_cfg, "gexa": render_cfg})

    monkeypatch.setattr(rd_module, "harvest_live", lambda cfg, zip_, headless=True: [])
    monkeypatch.setattr(
        rd_module,
        "fetch_rendered_html",
        lambda cfg, zip_, headless=True, snapshot_dir=None: ("<html></html>", snapshot_dir / "x.html"),
    )
    monkeypatch.setattr(rd_module, "discover", lambda html, cfg: [])
    monkeypatch.setattr(rd_module, "download_discovered", _fail_if_called)
    monkeypatch.setattr(app_common, "parse_downloaded_efls", _fail_if_called)

    result = app_common._run_rep_discovery(
        "78665",
        efl_dir=efl_dir,
        drafts_dir=drafts_dir,
        plans_dir=plans_dir,
        snapshot_dir=snapshot_dir,
    )

    for key in ("champion", "gexa"):
        assert result["reps"][key]["status"] == "ok"
        assert result["reps"][key]["plans_found"] == 0
    assert result["downloaded"] == {"downloaded": [], "skipped": [], "failed": [], "filtered_out": 0}
    assert result["parsed"] == {"parsed": [], "skipped": [], "failed": []}


# --------------------------------------------------------------------------- #
# 6. progress_callback
# --------------------------------------------------------------------------- #
def test_progress_callback_receives_per_rep_labels(discovery_dirs, monkeypatch):
    efl_dir, drafts_dir, plans_dir, snapshot_dir = discovery_dirs
    harvester_cfg = _harvester_config("champion", "Champion")
    render_cfg = _render_config("gexa", "Gexa")
    monkeypatch.setattr(rd_module, "REP_CONFIGS", {"champion": harvester_cfg, "gexa": render_cfg})

    monkeypatch.setattr(rd_module, "harvest_live", lambda cfg, zip_, headless=True: [])
    monkeypatch.setattr(
        rd_module,
        "fetch_rendered_html",
        lambda cfg, zip_, headless=True, snapshot_dir=None: ("<html></html>", snapshot_dir / "x.html"),
    )
    monkeypatch.setattr(rd_module, "discover", lambda html, cfg: [])
    monkeypatch.setattr(rd_module, "download_discovered", _fail_if_called)
    monkeypatch.setattr(app_common, "parse_downloaded_efls", _fail_if_called)

    events: list[tuple[int, int, str]] = []
    app_common._run_rep_discovery(
        "78665",
        efl_dir=efl_dir,
        drafts_dir=drafts_dir,
        plans_dir=plans_dir,
        snapshot_dir=snapshot_dir,
        progress_callback=lambda d, t, n: events.append((d, t, n)),
    )

    labels = [n for (_d, _t, n) in events]
    assert "discovery: Champion (querying site)" in labels
    assert "discovery: Gexa (querying site)" in labels


# --------------------------------------------------------------------------- #
# 7. refresh_market_data integration
# --------------------------------------------------------------------------- #
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


def test_refresh_market_data_run_discovery_false_does_not_invoke(refresh_dirs, monkeypatch):
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs

    from energyanalyzer.fetchers import meterplan as meterplan_module
    from energyanalyzer.fetchers import ptc as ptc_module

    def _boom(*a, **k):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", _boom)
    monkeypatch.setattr(meterplan_module, "fetch_meterplan", _boom)
    monkeypatch.setattr(app_common, "_run_rep_discovery", _fail_if_called)

    summary = app_common.refresh_market_data(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        efl_dir=efl_dir,
        ptc_dir=ptc_dir,
        meterplan_dir=meterplan_dir,
        fetch=False,
        run_discovery=False,
    )

    assert summary["discovery"]["enabled"] is False
    assert summary["discovery"]["reps"] == {}


def test_refresh_market_data_run_discovery_true_updates_summary(refresh_dirs, monkeypatch):
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs

    from energyanalyzer.fetchers import meterplan as meterplan_module
    from energyanalyzer.fetchers import ptc as ptc_module

    def _boom(*a, **k):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", _boom)
    monkeypatch.setattr(meterplan_module, "fetch_meterplan", _boom)

    canned = {
        "reps": {"gexa": {"retailer": "Gexa", "status": "ok", "plans_found": 1, "buyback": 1, "detail": "rendered live"}},
        "downloaded": {"downloaded": ["gexa.pdf"], "skipped": [], "failed": [], "filtered_out": 0},
        "parsed": {"parsed": ["gexa_plan"], "skipped": [], "failed": []},
    }
    monkeypatch.setattr(app_common, "_run_rep_discovery", lambda **kwargs: canned)

    summary = app_common.refresh_market_data(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        efl_dir=efl_dir,
        ptc_dir=ptc_dir,
        meterplan_dir=meterplan_dir,
        fetch=False,
        run_discovery=True,
        discovery_zip="78665",
    )

    assert summary["discovery"]["enabled"] is True
    assert summary["discovery"]["reps"] == canned["reps"]
    assert summary["discovery"]["downloaded"] == canned["downloaded"]
    assert summary["discovery"]["parsed"] == canned["parsed"]


def test_refresh_market_data_discovery_failure_is_caught_into_notes(refresh_dirs, monkeypatch):
    plans_dir, drafts_dir, efl_dir, ptc_dir, meterplan_dir = refresh_dirs

    from energyanalyzer.fetchers import meterplan as meterplan_module
    from energyanalyzer.fetchers import ptc as ptc_module

    def _boom(*a, **k):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(ptc_module, "fetch_ptc_csv", _boom)
    monkeypatch.setattr(meterplan_module, "fetch_meterplan", _boom)

    def _boom_discovery(**kwargs):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(app_common, "_run_rep_discovery", _boom_discovery)

    summary = app_common.refresh_market_data(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        efl_dir=efl_dir,
        ptc_dir=ptc_dir,
        meterplan_dir=meterplan_dir,
        fetch=False,
        run_discovery=True,
    )

    assert summary["discovery"]["enabled"] is True
    # Failure was swallowed -- the default empty discovery summary is kept.
    assert summary["discovery"]["reps"] == {}
    assert any("rep discovery stage failed" in n.lower() for n in summary["notes"])
    assert any("discovery exploded" in n for n in summary["notes"])
