"""Smoke test for the in-app Help page: it must render without raising even
with no data present, and expose its tabbed structure. Guards against an import
drift (it pulls several constants/helpers from app.common) or a Streamlit API
misuse silently breaking the page.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

HELP_PAGE = Path(__file__).resolve().parents[1] / "src" / "energyanalyzer" / "app" / "pages" / "5_Help.py"


def test_help_page_renders_without_exception():
    at = AppTest.from_file(str(HELP_PAGE), default_timeout=30)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert at.title and at.title[0].value == "Help"
    # Overview / Page guide / Data & refresh / Concepts / Troubleshooting.
    assert len(at.tabs) == 5
