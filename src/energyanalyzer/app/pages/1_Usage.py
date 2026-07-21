"""Usage page (ARCHITECTURE.md §9): upload interval data, quality report,
monthly import/export chart, hour x month heatmap, day/peak/night split."""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from energyanalyzer.app.common import (  # noqa: E402
    DATA_DIR,
    day_peak_night_split,
    get_intervals,
    hour_month_net_kw_pivot,
    invalidate_intervals_cache,
    monthly_summary,
    render_missing_data_help,
)

st.set_page_config(page_title="EnergyAnalyzer - Usage", page_icon="⚡", layout="wide")
st.title("Usage")

# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
st.subheader("Upload interval data")
st.caption(
    "SmartMeter Texas (SMT) interval CSV export, or NAESB Green Button XML. "
    "Multiple files are merged automatically."
)
uploaded_files = st.file_uploader(
    "Choose file(s)", type=["csv", "xml"], accept_multiple_files=True, key="interval_uploader"
)
if uploaded_files:
    if st.button("Save & reload", key="save_reload_btn"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in uploaded_files:
            suffix = Path(f.name).suffix.lower()
            if suffix == ".csv":
                target_name = f.name if f.name.startswith("IntervalData") else f"IntervalData_{f.name}"
            elif suffix == ".xml":
                target_name = f.name if f.name.startswith("GreenButton") else f"GreenButton_{f.name}"
            else:
                st.warning(f"Skipping unrecognized file type: {f.name}")
                continue
            target = DATA_DIR / target_name
            target.write_bytes(f.getvalue())
            saved.append(target_name)
        invalidate_intervals_cache()
        st.success(f"Saved: {', '.join(saved)}. Reloading...")
        st.rerun()

st.divider()

# --------------------------------------------------------------------------- #
# Load + quality report
# --------------------------------------------------------------------------- #
try:
    intervals, report = get_intervals(DATA_DIR)
except FileNotFoundError as exc:
    render_missing_data_help(exc, title="No usage data yet")
    st.caption(
        "SmartMeter Texas: log in at smartmetertexas.com, go to My Usage, and export "
        "15-minute interval data as CSV for your ESIID. Upload it above."
    )
    st.stop()

st.subheader("Data quality")
with st.expander("QualityReport", expanded=False):
    st.text(str(report))
cols = st.columns(4)
n_days = (report.end - report.start).days + 1 if report.start is not None else 0
cols[0].metric("Intervals", f"{len(intervals):,}")
cols[1].metric("Days", n_days)
cols[2].metric("Import total", f"{intervals['import_kwh'].sum():,.0f} kWh")
cols[3].metric("Export total", f"{intervals['export_kwh'].sum():,.0f} kWh")
if report.warnings:
    st.warning(f"{len(report.warnings)} quality warning(s) -- see QualityReport above.")

st.divider()

# --------------------------------------------------------------------------- #
# Monthly import/export bar chart with net line
# --------------------------------------------------------------------------- #
st.subheader("Monthly import / export")
ms = monthly_summary(intervals)
fig = go.Figure()
fig.add_bar(x=ms["month"], y=ms["import_kwh"], name="Import (grid)", marker_color="#c53030")
fig.add_bar(x=ms["month"], y=-ms["export_kwh"], name="Export (solar)", marker_color="#2b6cb0")
fig.add_scatter(
    x=ms["month"],
    y=ms["net_kwh"],
    name="Net (import - export)",
    mode="lines+markers",
    line=dict(color="black", width=2),
)
fig.update_layout(barmode="relative", yaxis_title="kWh", xaxis_title="Month", legend_title="")
st.plotly_chart(fig, width="stretch")

# --------------------------------------------------------------------------- #
# Hour-of-day x month heatmap of average net kW
# --------------------------------------------------------------------------- #
st.subheader("Hour-of-day × month average net power (kW)")
pivot = hour_month_net_kw_pivot(intervals)
fig2 = go.Figure(
    data=go.Heatmap(
        z=pivot.values,
        x=list(pivot.columns),
        y=list(pivot.index),
        colorscale=[[0, "#2b6cb0"], [0.5, "#f7f7f7"], [1, "#c53030"]],
        zmid=0,
        colorbar=dict(title="kW"),
    )
)
fig2.update_layout(xaxis_title="Month", yaxis_title="Hour of day (local)")
st.plotly_chart(fig2, width="stretch")
st.caption("Red = net import (grid draw). Blue = net export (solar surplus).")

st.divider()

# --------------------------------------------------------------------------- #
# Day / Peak / Night split
# --------------------------------------------------------------------------- #
st.subheader("Day (6a-6p) / Peak (6p-9p) / Night (9p-6a) annual net kWh")
st.dataframe(day_peak_night_split(intervals), width="stretch", hide_index=True)
