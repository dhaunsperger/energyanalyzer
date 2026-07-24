"""EnergyAnalyzer -- Home page (ARCHITECTURE.md §9).

Run with: streamlit run src/energyanalyzer/app/Home.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Allow `streamlit run .../app/Home.py` to find the `energyanalyzer` package
# without requiring `pip install -e .` first (dev convenience).
_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from energyanalyzer.app.common import (  # noqa: E402
    DATA_DIR,
    ERCOT_DIR,
    get_intervals,
    get_load_zone,
    get_plans,
    render_missing_data_help,
    try_get_prices,
)

st.set_page_config(page_title="EnergyAnalyzer", page_icon="⚡", layout="wide")

st.title("⚡ EnergyAnalyzer")
st.markdown(
    """
Replicates the Texas Power Guide solar electric plan analysis:

1. **Usage** -- ingest 12 months of 15-minute SmartMeter Texas (SMT) interval
   data (grid import + solar export), review data quality, and see monthly /
   hour-of-day usage patterns.
2. **Plans** -- maintain a database of retail electric plans (manual entry,
   EFL PDF import, or a Power to Choose snapshot).
3. **Compare** -- simulate a full year of bills for every plan against your
   actual usage and rank them by first-year net cost.
4. **Export** -- download an Excel workbook with the full ranking and
   supporting detail.

Use the sidebar to navigate between pages. New here? See the **Help** page for a
guided tour, the data sources behind the plans, and troubleshooting.
"""
)

st.divider()
st.subheader("Data status")

col1, col2, col3 = st.columns(3)

# --------------------------------------------------------------------------- #
# Interval data
# --------------------------------------------------------------------------- #
with col1:
    st.markdown("**Interval usage data**")
    try:
        df, report = get_intervals(DATA_DIR)
        n_days = (report.end - report.start).days + 1 if report.start is not None else 0
        st.success(f"Loaded {len(df):,} intervals")
        st.metric("Date range", f"{n_days} days")
        st.caption(f"{report.start} → {report.end}")
        st.metric("Total import", f"{df['import_kwh'].sum():,.0f} kWh")
        st.metric("Total export", f"{df['export_kwh'].sum():,.0f} kWh")
        if report.warnings:
            with st.expander(f"{len(report.warnings)} quality warning(s)"):
                for w in report.warnings:
                    st.caption(f"- {w}")
    except FileNotFoundError as exc:
        st.info("No interval data loaded yet.")
        render_missing_data_help(exc, title="How to load your usage data")
        st.caption("Go to the **Usage** page to upload a SmartMeter Texas CSV or Green Button XML.")

# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
with col2:
    st.markdown("**Plan database**")
    try:
        plans = get_plans()
        n_review = sum(1 for p in plans if p.needs_review)
        st.success(f"{len(plans)} plan(s) loaded")
        if n_review:
            st.warning(f"{n_review} plan(s) flagged `needs_review`")
        st.caption("See the **Plans** page to add, edit, or import plans.")
    except Exception as exc:  # noqa: BLE001 -- surface any plan-load problem to the user
        st.error("Could not load plans/*.yaml")
        st.code(str(exc), language=None)

# --------------------------------------------------------------------------- #
# ERCOT prices
# --------------------------------------------------------------------------- #
with col3:
    zone = get_load_zone()
    st.markdown(f"**ERCOT prices ({zone})**")
    prices, err = try_get_prices(zone)
    if prices is not None and not prices.empty:
        st.success(f"{len(prices):,} price points cached")
        st.caption(f"{prices.index.min()} → {prices.index.max()}")
    else:
        st.info("Not loaded (only needed for RTW-indexed plans).")
        if err:
            render_missing_data_help(Exception(err), title="How to load ERCOT prices")
        st.caption(f"Place downloaded files in `{ERCOT_DIR}`.")

st.divider()
st.caption(
    "EnergyAnalyzer keeps all data local: interval CSVs, plan YAMLs, and price caches "
    "never leave this machine except when you explicitly fetch a Power to Choose "
    "snapshot or an EFL PDF."
)
