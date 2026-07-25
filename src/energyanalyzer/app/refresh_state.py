"""Durable record of refresh runs, and a runner that survives Streamlit reruns.

The problem this solves: "Refresh market data" is a long job (10+ minutes with
REP discovery), and Streamlit **kills the running script on any rerun** -- a page
navigation, a widget click, even an accidental one. Previously the whole run
lived inside that script run, with the result only landing in `st.session_state`
at the very end, so an interruption lost both the work in flight AND any record
that it had happened. The user saw a progress bar vanish and no summary, with no
way to tell how far it got.

Two independent mechanisms here:

1. :class:`RefreshJournal` -- an append-as-you-go record on disk
   (`data/refresh_state.json`, gitignored). Every stage transition is written
   atomically, so an interrupted run still leaves an accurate "got this far"
   trail that outlives the script run, the session, and a server restart.

2. :class:`RefreshRunner` -- runs the refresh on a **background thread**, which
   Streamlit does not kill on rerun. The thread never touches `st.*` (it has no
   ScriptRunContext); it writes to the journal, and the UI polls the journal.
   That separation is what makes navigating away safe.

Recovery is the third leg and lives in ``app.common.finish_refresh``: the slow
stages write drafts to disk as they go, so an interrupted run can be completed
locally without repeating the sweep.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_PATH = REPO_ROOT / "data" / "refresh_state.json"
# The discovery console. A background thread has no ScriptRunContext and so
# cannot stream into an `st.empty()` the way the old in-script version did, so
# the fetchers' INFO logs go to a file and the UI tails it. Bonus over the old
# behaviour: the log outlives the run, so you can read what discovery did on a
# sweep that was interrupted.
LOG_PATH = REPO_ROOT / "data" / "refresh_log.txt"
_MAX_LOG_BYTES = 2_000_000

# Progress callbacks fire per-item (hundreds of times); rewriting the journal on
# each would be pointless I/O. Throttle routine progress, but never throttle a
# stage change or a terminal state -- those are the events that matter after a
# crash.
_MIN_WRITE_INTERVAL_S = 1.0


class RefreshJournal:
    """Crash-durable progress record for one refresh run."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state: dict = {}
        self._last_write = 0.0

    # -- writing ---------------------------------------------------------- #
    def start(self, params: dict) -> str:
        run_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._state = {
                "run_id": run_id,
                "status": "running",
                "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "params": params,
                "stage": "starting",
                "stage_done": 0,
                "stage_total": 0,
                "item": "",
                "stages_seen": [],
                "summary": None,
                "error": None,
            }
            self._write_locked()
        return run_id

    def progress(self, done: int, total: int, label: str) -> None:
        """Record one progress tick. `label` is "<stage>: <item>" as emitted by
        the refresh's own `_report` helper."""
        stage, _, item = label.partition(": ")
        with self._lock:
            if not self._state:
                return
            changed_stage = stage != self._state.get("stage")
            self._state.update(
                {"stage": stage, "stage_done": done, "stage_total": total, "item": item}
            )
            if changed_stage:
                seen = self._state.setdefault("stages_seen", [])
                seen.append(
                    {"stage": stage, "at": dt.datetime.now().isoformat(timespec="seconds")}
                )
            self._write_locked(force=changed_stage)

    def finish(self, summary: dict) -> None:
        with self._lock:
            self._state.update(
                {
                    "status": "completed",
                    "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "summary": _jsonable(summary),
                    "stage": "done",
                }
            )
            self._write_locked(force=True)

    def fail(self, error: str) -> None:
        with self._lock:
            self._state.update(
                {
                    "status": "failed",
                    "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "error": error[:2000],
                }
            )
            self._write_locked(force=True)

    def _write_locked(self, force: bool = False) -> None:
        import time

        now = time.time()
        if not force and (now - self._last_write) < _MIN_WRITE_INTERVAL_S:
            return
        self._last_write = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a crash mid-write must never leave truncated JSON
            # that makes the next page load unreadable.
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(self._state, fh, indent=2, default=str)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001 -- journalling must never break a refresh
            logger.debug("Could not write refresh journal: %r", exc)


def _jsonable(obj):
    """Summaries carry Paths/dates/sets; make them survive a JSON round-trip."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class _RefreshLogCapture:
    """Attach a file handler to the `energyanalyzer` logger for one run."""

    def __init__(self, path: Path = LOG_PATH) -> None:
        self.path = Path(path)
        self._handler: Optional[logging.Handler] = None
        self._prev_level: Optional[int] = None

    def __enter__(self) -> _RefreshLogCapture:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")  # fresh log per run
            handler = logging.FileHandler(self.path, encoding="utf-8")
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
            pkg = logging.getLogger("energyanalyzer")
            self._prev_level = pkg.level
            pkg.setLevel(logging.INFO)
            pkg.addHandler(handler)
            self._handler = handler
        except Exception as exc:  # noqa: BLE001 -- logging must never break a refresh
            logger.debug("Could not attach refresh log handler: %r", exc)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._handler is not None:
            pkg = logging.getLogger("energyanalyzer")
            pkg.removeHandler(self._handler)
            if self._prev_level is not None:
                pkg.setLevel(self._prev_level)
            self._handler.close()


def read_log_tail(lines: int = 400, path: Path = LOG_PATH) -> str:
    """Last `lines` of the current/most recent run's log ("" if none)."""
    try:
        p = Path(path)
        if not p.exists() or p.stat().st_size > _MAX_LOG_BYTES:
            return p.read_text(errors="replace")[-200_000:] if p.exists() else ""
        return "\n".join(p.read_text(errors="replace").splitlines()[-lines:])
    except Exception:  # noqa: BLE001
        return ""


def load_state(path: Path = STATE_PATH) -> Optional[dict]:
    """The last recorded run, or None if there isn't one / it's unreadable."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001 -- absent or corrupt journal is not an error
        return None


def was_interrupted(state: Optional[dict]) -> bool:
    """True if the last run never reached a terminal state.

    A run marked "running" whose thread is gone was killed -- by a Streamlit
    rerun, a browser close, or a server restart. Since the journal is the only
    record that outlives the process, "still says running" is exactly the
    signal that something was lost.
    """
    if not state:
        return False
    if state.get("status") != "running":
        return False
    runner = _RUNNER
    return not (runner is not None and runner.is_running(state.get("run_id")))


class RefreshRunner:
    """Runs a refresh on a background thread so a Streamlit rerun can't kill it.

    The thread must not call `st.*` -- it has no ScriptRunContext. It reports
    only into the journal; the UI polls :func:`load_state`.
    """

    def __init__(
        self, journal: Optional[RefreshJournal] = None, log_path: Path = LOG_PATH
    ) -> None:
        self.journal = journal or RefreshJournal()
        # Injectable so tests never write into the repo's real data/ directory.
        self.log_path = Path(log_path)
        self._thread: Optional[threading.Thread] = None
        self._run_id: Optional[str] = None
        self._lock = threading.Lock()

    def is_running(self, run_id: Optional[str] = None) -> bool:
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            if run_id is None:
                return alive
            return alive and self._run_id == run_id

    def start(self, target: Callable[..., dict], **kwargs) -> Optional[str]:
        """Spawn `target(**kwargs, progress_callback=...)`. Returns the run id,
        or None if a refresh is already in flight (never run two at once -- they
        would both be writing plans/ and data/efl/)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return None
            run_id = self.journal.start(
                {k: str(v) for k, v in kwargs.items() if k != "progress_callback"}
            )
            self._run_id = run_id

            def _work() -> None:
                with _RefreshLogCapture(self.log_path):
                    try:
                        summary = target(progress_callback=self.journal.progress, **kwargs)
                        self.journal.finish(summary)
                    except Exception as exc:  # noqa: BLE001 -- record, never propagate
                        logger.exception("Background refresh failed")
                        self.journal.fail(repr(exc))

            # daemon=True: a stuck browser session must not keep the process up.
            self._thread = threading.Thread(target=_work, name=f"refresh-{run_id}", daemon=True)
            self._thread.start()
            return run_id


_RUNNER: Optional[RefreshRunner] = None


def get_runner() -> RefreshRunner:
    """Process-wide singleton. Streamlit reruns re-execute the page script but
    not this module, so the runner (and its thread) outlive a rerun."""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = RefreshRunner()
    return _RUNNER
