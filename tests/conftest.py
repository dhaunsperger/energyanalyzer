"""Shared pytest configuration.

The refresh pipeline's directory parameters default to the real ``data/``
tree. A test that builds its own tmp directories but forgets one of them
would otherwise reach into the user's live data -- and the quarantine is the
worst place for that to happen, since ``reconcile_quarantine`` consumes what
it finds there: running the suite on a machine with an interrupted refresh
could restore or drop real plans and overwrite the run's authority record.

The autouse fixture below redirects the quarantine to a per-test tmp path, so
no test can touch the live one no matter which arguments it passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from energyanalyzer.app import common as app_common


@pytest.fixture(autouse=True)
def _isolate_quarantine_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the default quarantine at tmp for every test (see module docstring)."""
    monkeypatch.setattr(app_common, "QUARANTINE_DIR", tmp_path / "refresh_quarantine")
    yield
