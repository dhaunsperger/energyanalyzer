"""Tests for the durable refresh journal + background runner.

These cover the failure that motivated the module: a refresh killed mid-run by a
Streamlit rerun used to leave no record at all. The journal must survive that,
report where it stopped, and let the local finish complete the database.
"""

from __future__ import annotations

import threading
import time

import pytest

from energyanalyzer.app.refresh_state import (
    RefreshJournal,
    RefreshRunner,
    load_state,
    was_interrupted,
)


@pytest.fixture
def journal_path(tmp_path):
    return tmp_path / "refresh_state.json"


def test_journal_records_progress_and_completion(journal_path):
    j = RefreshJournal(journal_path)
    run_id = j.start({"run_discovery": "True"})
    j.progress(1, 10, "download: a.pdf")
    j.finish({"promoted": ["x"], "needing_review": []})

    state = load_state(journal_path)
    assert state["run_id"] == run_id
    assert state["status"] == "completed"
    assert state["summary"]["promoted"] == ["x"]
    assert state["finished_at"]


def test_journal_survives_an_abandoned_run(journal_path):
    """The whole point: a run that never reaches a terminal state still leaves an
    accurate trail of how far it got."""
    j = RefreshJournal(journal_path)
    j.start({})
    j.progress(3, 20, "download: c.pdf")
    j.progress(1, 11, "discovery: green_mountain")
    # ...process dies here; no finish() / fail() call is ever made.

    state = load_state(journal_path)
    assert state["status"] == "running"
    assert state["stage"] == "discovery"
    assert state["item"] == "green_mountain"
    # Stage transitions are always flushed, never throttled away.
    assert [s["stage"] for s in state["stages_seen"]] == ["download", "discovery"]
    # No live thread owns it -> the UI must offer recovery.
    assert was_interrupted(state) is True


def test_completed_and_failed_runs_are_not_interrupted(journal_path):
    j = RefreshJournal(journal_path)
    j.start({})
    j.finish({})
    assert was_interrupted(load_state(journal_path)) is False

    j2 = RefreshJournal(journal_path)
    j2.start({})
    j2.fail("boom")
    state = load_state(journal_path)
    assert state["status"] == "failed" and state["error"] == "boom"
    assert was_interrupted(state) is False


def test_missing_or_corrupt_journal_degrades_quietly(tmp_path):
    assert load_state(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_state(bad) is None
    assert was_interrupted(None) is False


def test_summary_with_non_json_values_is_still_written(journal_path, tmp_path):
    """Refresh summaries carry Paths/dates; the journal must not choke on them."""
    import datetime as dt

    j = RefreshJournal(journal_path)
    j.start({})
    j.finish({"snapshot": tmp_path / "x.csv", "when": dt.date(2026, 7, 24), "n": {1, 2}})
    state = load_state(journal_path)
    assert "x.csv" in state["summary"]["snapshot"]
    assert state["summary"]["when"] == "2026-07-24"


def test_runner_runs_in_background_and_journals_result(journal_path):
    j = RefreshJournal(journal_path)
    runner = RefreshRunner(journal=j, log_path=journal_path.parent / 'log.txt')
    started = threading.Event()

    def fake_refresh(progress_callback=None, **kwargs):
        started.set()
        progress_callback(1, 2, "download: a.pdf")
        return {"promoted": ["p1"], "kwargs_seen": sorted(kwargs)}

    run_id = runner.start(fake_refresh, run_discovery=True)
    assert run_id
    assert started.wait(timeout=5)
    for _ in range(50):
        if not runner.is_running():
            break
        time.sleep(0.1)

    state = load_state(journal_path)
    assert state["status"] == "completed"
    assert state["summary"]["promoted"] == ["p1"]
    assert state["summary"]["kwargs_seen"] == ["run_discovery"]


def test_runner_records_a_crash_instead_of_propagating(journal_path):
    runner = RefreshRunner(journal=RefreshJournal(journal_path), log_path=journal_path.parent / 'log.txt')

    def boom(progress_callback=None, **kwargs):
        raise RuntimeError("network gone")

    runner.start(boom)
    for _ in range(50):
        if not runner.is_running():
            break
        time.sleep(0.1)
    state = load_state(journal_path)
    assert state["status"] == "failed"
    assert "network gone" in state["error"]


def test_runner_refuses_a_concurrent_second_run(journal_path):
    """Two refreshes at once would both be writing plans/ and data/efl/."""
    runner = RefreshRunner(journal=RefreshJournal(journal_path), log_path=journal_path.parent / 'log.txt')
    release = threading.Event()

    def slow(progress_callback=None, **kwargs):
        release.wait(timeout=5)
        return {}

    assert runner.start(slow) is not None
    assert runner.start(slow) is None  # rejected while the first is in flight
    release.set()


def test_a_finished_run_is_not_reported_as_live(journal_path):
    """Regression: the Plans page gates its live progress view on the journal
    saying "running" AND a thread owning it. When the run finishes, BOTH must go
    false, or the page leaves a stale "Refresh running in the background" banner
    above an empty body -- observed 2026-07-24 after a successful 13-minute run.
    """
    runner = RefreshRunner(
        journal=RefreshJournal(journal_path), log_path=journal_path.parent / "log.txt"
    )
    runner.start(lambda progress_callback=None, **kw: {"promoted": []})
    for _ in range(50):
        if not runner.is_running():
            break
        time.sleep(0.1)

    state = load_state(journal_path)
    live = bool(
        state and state.get("status") == "running" and runner.is_running(state.get("run_id"))
    )
    assert state["status"] == "completed"
    assert live is False, "a completed run must not still read as live"
    assert was_interrupted(state) is False, "a completed run must not offer recovery"
