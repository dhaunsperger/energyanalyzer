"""Shared Streamlit helpers for the EnergyAnalyzer app (ARCHITECTURE.md §9).

Only this module touches `st.cache_data` for the "core" loaders (intervals,
plans, TDU, prices) so pages stay thin. Pages should import from here rather
than calling ingest/plans_io/prices directly, EXCEPT for one-shot actions
(saving a plan, writing an uploaded file) which naturally live on the page
that triggers them -- but they should call the `invalidate_*` helpers here
afterwards so the cache picks up the change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import streamlit as st
import yaml

from energyanalyzer.core.models import Plan, TduTariff, add_local_columns
from energyanalyzer.core.plans_io import DRAFTS_DIR, PLANS_DIR, current_tdu, load_plans
from energyanalyzer.ingest.smt import QualityReport, load_intervals

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
ERCOT_DIR = DATA_DIR / "ercot"
PTC_DIR = DATA_DIR / "ptc"
EFL_DIR = DATA_DIR / "efl"
CONFIG_PATH = DATA_DIR / "config.yaml"

DEFAULT_LOAD_ZONE = "LZ_NORTH"
CURRENT_PLAN_ID = "pulse_current"

DAY_HOURS = list(range(6, 18))  # 6a-6p
PEAK_HOURS = list(range(18, 21))  # 6p-9p
NIGHT_HOURS = list(range(21, 24)) + list(range(0, 6))  # 9p-6a


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def get_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}
    return {}


def get_load_zone() -> str:
    return str(get_config().get("load_zone", DEFAULT_LOAD_ZONE))


# --------------------------------------------------------------------------- #
# Interval data
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner="Loading interval data...")
def _load_intervals_cached(data_dir: str) -> tuple[pd.DataFrame, QualityReport]:
    return load_intervals(Path(data_dir))


def get_intervals(data_dir: Path = DATA_DIR) -> tuple[pd.DataFrame, QualityReport]:
    """Load the canonical interval frame + QualityReport. Raises
    FileNotFoundError (propagated from ingest.smt.load_intervals) if no
    source files or cache are present -- callers should catch this and show
    `render_missing_data_help`."""
    return _load_intervals_cached(str(data_dir))


def invalidate_intervals_cache() -> None:
    _load_intervals_cached.clear()


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def _load_plans_cached(directory: str) -> list[Plan]:
    return load_plans(Path(directory))


def get_plans(directory: Path = PLANS_DIR) -> list[Plan]:
    return _load_plans_cached(str(directory))


def get_plans_dict(directory: Path = PLANS_DIR) -> dict[str, Plan]:
    return {p.id: p for p in get_plans(directory)}


def invalidate_plans_cache() -> None:
    _load_plans_cached.clear()


def get_draft_plans() -> list[Path]:
    """Draft YAMLs saved in plans/drafts/ (not yet promoted)."""
    if not DRAFTS_DIR.exists():
        return []
    return sorted(DRAFTS_DIR.glob("*.yaml"))


def load_draft_raw(path: Path) -> dict:
    """Load a draft YAML as a plain dict (NOT `Plan.model_validate`-ed --
    drafts are allowed to be incomplete/invalid until promoted). May carry a
    `_parse` sub-key (confidence/evidence/unparsed_notes) if it came from
    `eflparse.parser.save_draft`; plain plan-shaped drafts saved via the
    Add/Edit form won't have one.
    """
    with open(path) as f:
        return yaml.safe_load(f) or {}


def draft_energy_rate_summary(plan_dict: dict) -> str:
    """One-line human summary of a draft/plan dict's `energy_rates` list, for
    the drafts overview table."""
    rates = plan_dict.get("energy_rates") or []
    if not rates:
        return "-"
    parts = []
    for r in rates:
        if not isinstance(r, dict):
            continue
        if r.get("rate_ckwh") is not None:
            parts.append(f"{r['rate_ckwh']:g}c/kWh")
        elif r.get("rtw"):
            parts.append("RTW-indexed")
        else:
            parts.append("?")
    label = " / ".join(parts) if parts else "?"
    return f"{label} ({len(rates)} rate{'s' if len(rates) != 1 else ''})"


def draft_summary_row(path: Path) -> dict:
    """One flattened row for the Draft plans overview table (ARCHITECTURE.md §9)."""
    raw = load_draft_raw(path)
    parse_meta = raw.get("_parse") or {}
    confidence = parse_meta.get("confidence") or {}
    min_confidence = min(confidence.values()) if confidence else None
    return {
        "file": path.name,
        "id": raw.get("id", path.stem),
        "Retailer": raw.get("retailer", ""),
        "Plan": raw.get("name", ""),
        "Term (mo)": raw.get("term_months", ""),
        "Energy rate": draft_energy_rate_summary(raw),
        "Min confidence": f"{min_confidence:.2f}" if min_confidence is not None else "-",
        "Needs Review": "⚠️" if raw.get("needs_review") else "",
    }


def parse_downloaded_efls(
    pdf_paths: list[Path],
    drafts_dir: Path = DRAFTS_DIR,
    plans_dir: Path = PLANS_DIR,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Batch-parse EFL PDFs into draft plan YAMLs (ARCHITECTURE.md §8/§9).

    For each PDF, runs `eflparse.parser.parse_efl` (static, no network/LLM)
    to determine its plan id, then `eflparse.parser.save_draft` -- unless a
    draft or promoted plan with that id already exists, in which case it's
    counted as `skipped` rather than re-saved (this is the "already parsed"
    tracking: identity is the derived draft filename, `<id>.yaml`). Each
    PDF is parsed inside its own try/except so a single corrupt/unreadable
    file cannot abort the batch -- such files are collected in `failed`.

    If `progress_callback` is given, it's called after every file as
    `progress_callback(done_count, total, current_filename)`.

    Returns `{'parsed': [plan_id, ...], 'skipped': [filename, ...],
    'failed': [{'file': filename, 'error': str}, ...]}`.
    """
    from energyanalyzer.eflparse.parser import parse_efl, save_draft

    drafts_dir = Path(drafts_dir)
    plans_dir = Path(plans_dir)
    total = len(pdf_paths)
    summary: dict = {"parsed": [], "skipped": [], "failed": []}

    for i, pdf_path in enumerate(pdf_paths, start=1):
        pdf_path = Path(pdf_path)
        try:
            draft = parse_efl(pdf_path)
            plan_id = draft.plan_dict.get("id")
            already = (drafts_dir / f"{plan_id}.yaml").exists() or (plans_dir / f"{plan_id}.yaml").exists()
            if already:
                summary["skipped"].append(pdf_path.name)
            else:
                save_draft(draft, drafts_dir=drafts_dir)
                summary["parsed"].append(plan_id)
        except Exception as exc:  # noqa: BLE001 -- a bad PDF must not abort the batch
            summary["failed"].append({"file": pdf_path.name, "error": repr(exc)})
        if progress_callback is not None:
            progress_callback(i, total, pdf_path.name)

    return summary


# --------------------------------------------------------------------------- #
# TDU
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def get_tdu() -> TduTariff:
    return current_tdu()


def invalidate_tdu_cache() -> None:
    get_tdu.clear()


# --------------------------------------------------------------------------- #
# ERCOT prices (optional -- may not be present)
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner="Loading ERCOT prices...")
def _load_prices_cached(zone: str, data_dir: str) -> pd.Series:
    from energyanalyzer.prices.ercot import load_prices

    return load_prices(zone, Path(data_dir))


def try_get_prices(zone: Optional[str] = None) -> tuple[Optional[pd.Series], Optional[str]]:
    """Best-effort ERCOT price load. Returns (series, None) on success, or
    (None, human_readable_error) if unavailable (missing files, network,
    etc.) -- callers should treat `prices=None` gracefully, since only
    RTW-indexed plans require it."""
    zone = zone or get_load_zone()
    try:
        return _load_prices_cached(zone, str(ERCOT_DIR)), None
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return None, str(exc)


def invalidate_prices_cache() -> None:
    _load_prices_cached.clear()


# --------------------------------------------------------------------------- #
# Usage summaries (pure functions -- no caching needed, cheap over ~35k rows)
# --------------------------------------------------------------------------- #
def monthly_summary(intervals: pd.DataFrame) -> pd.DataFrame:
    """One row per calendar month: import_kwh, export_kwh, net_kwh."""
    df = add_local_columns(intervals)
    out = df.groupby("month", sort=True)[["import_kwh", "export_kwh"]].sum().reset_index()
    out["net_kwh"] = out["import_kwh"] - out["export_kwh"]
    out["month"] = out["month"].astype(str)
    return out


def hour_month_net_kw_pivot(intervals: pd.DataFrame) -> pd.DataFrame:
    """Hour-of-day (rows) x month (columns) pivot of average net kW
    (import-export)/0.25, for the heatmap (report p.1)."""
    df = add_local_columns(intervals).copy()
    df["net_kw"] = (df["import_kwh"] - df["export_kwh"]) / 0.25
    pivot = df.pivot_table(
        index="hour", columns="month", values="net_kw", aggfunc="mean", observed=True
    )
    pivot.columns = [str(c) for c in pivot.columns]
    return pivot


def day_peak_night_split(intervals: pd.DataFrame) -> pd.DataFrame:
    """Annual net kWh split into Day (6a-6p) / Peak (6p-9p) / Night (9p-6a)."""
    df = add_local_columns(intervals)
    net = df["import_kwh"] - df["export_kwh"]
    rows = [
        ("Day (6a-6p)", float(net[df["hour"].isin(DAY_HOURS)].sum())),
        ("Peak (6p-9p)", float(net[df["hour"].isin(PEAK_HOURS)].sum())),
        ("Night (9p-6a)", float(net[df["hour"].isin(NIGHT_HOURS)].sum())),
    ]
    return pd.DataFrame(rows, columns=["Period", "Net kWh"])


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def render_missing_data_help(exc: Exception, title: str = "Data not available") -> None:
    """Surface a FileNotFoundError/RuntimeError's manual-download instructions
    in a readable way (these modules deliberately write user-facing guidance
    into the exception message -- see ingest/smt.py, prices/ercot.py,
    fetchers/ptc.py)."""
    st.warning(f"**{title}**")
    st.code(str(exc), language=None)


def needs_review_badge(plan: Plan) -> str:
    return "⚠️ NEEDS REVIEW" if plan.needs_review else ""


def plan_summary_row(plan: Plan) -> dict:
    """One flattened row for the Plans page overview table."""
    from energyanalyzer.report.excel import (
        plan_etf_label,
        plan_export_label,
        plan_other_details,
    )

    return {
        "id": plan.id,
        "Retailer": plan.retailer,
        "Plan": plan.name,
        "Term (mo)": plan.term_months,
        "Base $/mo": plan.base_charge_usd,
        "Export": plan_export_label(plan),
        "ETF": plan_etf_label(plan),
        "Details": plan_other_details(plan),
        "Source": plan.source,
        "Needs Review": "⚠️" if plan.needs_review else "",
    }
