"""Export page (ARCHITECTURE.md §9): download the Excel workbook built by
report/excel.py -- Summary, Monthly Detail, Usage, Plan Inputs sheets."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import streamlit as st

_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from energyanalyzer.app.common import (  # noqa: E402
    CURRENT_PLAN_ID,
    DATA_DIR,
    get_intervals,
    get_plans_dict,
    get_tdu,
    render_missing_data_help,
    try_get_prices,
)
from energyanalyzer.engine.cost import rank  # noqa: E402
from energyanalyzer.report.excel import build_workbook  # noqa: E402

st.set_page_config(page_title="EnergyAnalyzer - Export", page_icon="⚡", layout="wide")
st.title("Export")

try:
    intervals, _ = get_intervals(DATA_DIR)
except FileNotFoundError as exc:
    render_missing_data_help(exc, title="No usage data yet")
    st.stop()

plans_dict = get_plans_dict()
if not plans_dict:
    st.warning("No plans found in plans/*.yaml. Add some on the Plans page.")
    st.stop()

tdu = get_tdu()
prices, price_err = try_get_prices()
if price_err:
    st.caption(f"ERCOT prices not loaded (RTW-indexed plans will be skipped): {price_err}")

include_review = st.toggle("Include plans flagged `needs_review`", value=False, key="export_include_review")
usable_plans = [p for p in plans_dict.values() if include_review or not p.needs_review]

results = rank(usable_plans, intervals, tdu, prices)
for w in getattr(results, "warnings", []):
    st.warning(w)

if not results:
    st.error("No plans could be simulated -- nothing to export.")
    st.stop()

st.write(
    f"The workbook will include **{len(results)}** plan(s), ranked by first-year net bill, "
    "with monthly detail, usage summaries, and full plan parameter dumps."
)

buf = io.BytesIO()
try:
    build_workbook(results, plans_dict, intervals, tdu, buf, current_plan_id=CURRENT_PLAN_ID)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not build workbook: {exc}")
    st.stop()
buf.seek(0)

st.download_button(
    "Download energyanalyzer_export.xlsx",
    data=buf.getvalue(),
    file_name="energyanalyzer_export.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
