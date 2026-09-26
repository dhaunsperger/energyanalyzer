"""User settings read from ``data/config.yaml``, with environment overrides.

Deliberately dependency-free (yaml + stdlib): the low-level modules that need a
setting -- :mod:`energyanalyzer.llm`, :mod:`energyanalyzer.fetchers.rep_discovery`
-- must not import Streamlit to find out which model to talk to.

Resolution order for every setting is **environment, then config file, then the
code default**. The env layer exists for the scripts and for one-off comparisons
(``EA_LLM_MODEL=gemma4:... python scripts/eval_efl.py``) without editing a
gitignored file; the file layer is where a lasting choice belongs.

Every accessor reads at CALL time rather than import time. A module constant
captured into a function's default argument freezes at import and silently
ignores the config -- the same late-binding trap that made the refresh
quarantine reconcile the wrong directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "data" / "config.yaml"

# Ollama defaults. The model is MEASURED, not assumed -- see the benchmark table
# in energyanalyzer.llm and scripts/eval_efl.py before changing it.
DEFAULT_LLM_MODEL = "gemma3:4b"
DEFAULT_LLM_URL = "http://localhost:11434/api/chat"


def load_config(path: Optional[Path] = None) -> dict:
    """Parse data/config.yaml. A missing or unreadable file is simply empty --
    every setting has a working default, so a typo must not break the app."""
    path = Path(path) if path is not None else CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with open(path) as handle:
            return yaml.safe_load(handle) or {}
    except Exception:  # noqa: BLE001 -- malformed config degrades to defaults
        return {}


def setting(key: str, default, env_var: Optional[str] = None, path: Optional[Path] = None):
    """Resolve one setting: environment, then config file, then `default`."""
    if env_var:
        from_env = os.environ.get(env_var)
        if from_env:
            return from_env
    value = load_config(path).get(key)
    return default if value is None else value


def llm_model(path: Optional[Path] = None) -> str:
    """Ollama model for EFL field repair and plan-identity adjudication."""
    return str(setting("llm_model", DEFAULT_LLM_MODEL, "EA_LLM_MODEL", path))


def llm_url(path: Optional[Path] = None) -> str:
    """Ollama chat endpoint."""
    return str(setting("llm_url", DEFAULT_LLM_URL, "EA_LLM_URL", path))


def llm_tags_url(path: Optional[Path] = None) -> str:
    """Ollama's model-list endpoint, derived from `llm_url` -- a cheap GET used
    to probe availability without running inference."""
    return llm_url(path).replace("/api/chat", "/api/tags")


def discovery_llm_model(path: Optional[Path] = None) -> str:
    """Model for the REP-discovery site classifier.

    Follows `llm_model` unless `discovery_llm_model` is set explicitly. They used
    to be separate hardcoded constants, so switching the EFL model left discovery
    on whatever it had been pinned to -- one setting by default, two only when
    you mean it.
    """
    return str(setting("discovery_llm_model", llm_model(path), "EA_DISCOVERY_LLM_MODEL", path))
