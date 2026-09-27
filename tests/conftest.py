"""Shared pytest configuration.

The refresh pipeline's directory parameters default to the real ``data/``
tree. A test that builds its own tmp directories but forgets one of them
would otherwise reach into the user's live data -- and the quarantine is the
worst place for that to happen, since ``reconcile_quarantine`` consumes what
it finds there: running the suite on a machine with an interrupted refresh
could restore or drop real plans and overwrite the run's authority record.

The autouse fixture below redirects the quarantine to a per-test tmp path, so
no test can touch the live one no matter which arguments it passes.

The user's ``data/config.yaml`` gets the same treatment: some settings in it
change what the EFL parser emits (``ev_home_charging_kwh_month`` attaches an EV
add-on to a plan), so a test must see the code defaults, not whatever this
machine's owner configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from energyanalyzer.app import common as app_common
from energyanalyzer.core import config as core_config


@pytest.fixture(autouse=True)
def _isolate_quarantine_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the default quarantine at tmp for every test (see module docstring)."""
    monkeypatch.setattr(app_common, "QUARANTINE_DIR", tmp_path / "refresh_quarantine")
    yield


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every test starts from code defaults (see module docstring)."""
    monkeypatch.setattr(core_config, "CONFIG_PATH", tmp_path / "config.yaml")
    for var in (
        "EA_LLM_MODEL",
        "EA_LLM_URL",
        "EA_DISCOVERY_LLM_MODEL",
        "EA_EV_HOME_CHARGING_KWH_MONTH",
        "EA_EV_CHARGER_KW",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
