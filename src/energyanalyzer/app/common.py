"""Shared Streamlit helpers for the EnergyAnalyzer app (ARCHITECTURE.md §9).

Only this module touches `st.cache_data` for the "core" loaders (intervals,
plans, TDU, prices) so pages stay thin. Pages should import from here rather
than calling ingest/plans_io/prices directly, EXCEPT for one-shot actions
(saving a plan, writing an uploaded file) which naturally live on the page
that triggers them -- but they should call the `invalidate_*` helpers here
afterwards so the cache picks up the change.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import streamlit as st
import yaml

from energyanalyzer.core.models import Plan, TduTariff, add_local_columns
from energyanalyzer.core.plans_io import DRAFTS_DIR, PLANS_DIR, current_tdu, load_plans, save_plan
from energyanalyzer.ingest.smt import QualityReport, load_intervals

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
ERCOT_DIR = DATA_DIR / "ercot"
PTC_DIR = DATA_DIR / "ptc"
EFL_DIR = DATA_DIR / "efl"
METERPLAN_DIR = DATA_DIR / "meterplan"
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


def ptc_efl_resolution_report(ptc_df: pd.DataFrame, efl_dir: Path = EFL_DIR) -> pd.DataFrame:
    """For each row in a PTC DataFrame, check whether its EFL resolved to a
    downloaded, parseable PDF -- so a human can go check those retailer
    sites by hand for the ones that didn't.

    Only problem rows are returned (empty DataFrame if everything resolved
    cleanly). A row is a problem if: the PTC listing has no `efl_url` at
    all; the expected PDF (same filename convention as
    `fetchers.ptc.download_efls`/`_efl_filename`) hasn't been downloaded
    into `efl_dir` yet; or the PDF is there but `eflparse.parser.parse_efl`
    raised an exception on it (corrupt/unreadable/non-PDF download).
    Columns: retailer, plan_name, tdu, status, detail, efl_url, enroll_url,
    website.
    """
    from energyanalyzer.eflparse.parser import parse_efl
    from energyanalyzer.fetchers.ptc import _efl_filename

    efl_dir = Path(efl_dir)
    rows: list[dict] = []
    for _, row in ptc_df.iterrows():
        efl_url = row.get("efl_url")
        has_url = bool(str(efl_url).strip()) and str(efl_url).strip().lower() not in ("nan", "none")
        status: Optional[str] = None
        detail = ""
        if not has_url:
            status = "no EFL URL"
            detail = "PTC listing has no Facts Label link for this plan"
        else:
            pdf_path = efl_dir / _efl_filename(row)
            if not pdf_path.exists():
                status = "not downloaded"
                detail = f"expected {pdf_path.name}, not found in {efl_dir}"
            else:
                try:
                    parse_efl(pdf_path)
                except Exception as exc:  # noqa: BLE001 -- reporting only, not raising
                    status = "parse failed"
                    detail = repr(exc)
        if status is not None:
            rows.append(
                {
                    "retailer": row.get("retailer"),
                    "plan_name": row.get("plan_name"),
                    "tdu": row.get("tdu"),
                    "status": status,
                    "detail": detail,
                    "efl_url": efl_url if has_url else None,
                    "enroll_url": row.get("enroll_url"),
                    "website": row.get("website"),
                }
            )
    return pd.DataFrame(
        rows, columns=["retailer", "plan_name", "tdu", "status", "detail", "efl_url", "enroll_url", "website"]
    )


def refresh_market_data(
    plans_dir: Path = PLANS_DIR,
    drafts_dir: Path = DRAFTS_DIR,
    efl_dir: Path = EFL_DIR,
    ptc_dir: Path = PTC_DIR,
    meterplan_dir: Path = METERPLAN_DIR,
    tdu: str = "ONCOR",
    language: Optional[str] = "English",
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    fetch: bool = True,
) -> dict:
    """"Refresh market data" one-button pipeline (ARCHITECTURE.md §9).

    Orchestrates, in order:

    1. **Delete** stale auto-imported data: plans in `plans_dir` whose
       `source` starts with `"ptc"`, `"efl:"`, or is exactly `"meterplan"`
       (never `"manual"`, never a `"report-*"` seed, never
       `CURRENT_PLAN_ID`); every draft in `drafts_dir`; every PDF in
       `efl_dir`; every PTC snapshot in `ptc_dir` except the newest (kept as
       a fallback).
    2. **Fetch** a fresh PTC snapshot (`fetchers.ptc.fetch_ptc_csv`) unless
       `fetch=False`. If the live fetch raises `RuntimeError` (network
       blocked), falls back to the newest snapshot kept in step 1 and notes
       it. If neither a fetch nor an existing snapshot is available, stops
       here (download/parse/promote are skipped) and says so in `notes`.
    3. **Load + filter** the snapshot (`load_ptc` + `filter_plans(tdu,
       language)`).
    4. **Download** EFLs for the filtered plans (`download_efls`).
    5. **Parse** every downloaded EFL into a draft (`parse_downloaded_efls`).
    6. **Meterplan**: fetch a fresh meterplan.com solar buyback plan index
       snapshot (`fetchers.meterplan.fetch_meterplan`) unless `fetch=False`;
       on failure (or `fetch=False`), falls back to the newest snapshot in
       `meterplan_dir` and notes it -- if neither is available, this stage
       is skipped (noted) rather than aborting the run. The snapshot is
       loaded + filtered to `tdu` (`load_meterplan` + `filter_meterplan`)
       and turned into drafts (`fetchers.meterplan.meterplan_to_drafts`),
       deduped against every plan still present in `plans_dir` after step 1
       (so a plan already in the database by retailer/name/term isn't
       re-drafted). Simple plans (fixed import, fixed/none export, no
       free-hours name) come out with `needs_review=False` and
       high-confidence load-bearing fields -- eligible for the same
       auto-promote gate as EFL drafts below; anything else is left flagged.
    7. **Auto-promote** drafts (EFL- and meterplan-sourced alike) the parser
       was confident about: `needs_review` is `False` on the draft AND every
       load-bearing field (`eflparse.parser.LOAD_BEARING_KEYS`) it saw a
       confidence score for scored >= 0.8 (a draft with no confidence data
       at all is treated conservatively -- left for manual review, not
       auto-promoted). Promoted plans are validated via `Plan.model_validate`,
       stamped with `retrieved = today` (and `source` defaulted to `"ptc"`
       if somehow unset), saved via `plans_io.save_plan`, and their draft
       file deleted. A single bad draft cannot abort the batch.

    `progress_callback`, if given, is called after every item in every stage
    as `progress_callback(stage_done, stage_total, "<stage>: <item>")` so a
    caller can drive one progress bar + status line across the whole run.

    Returns a summary dict: `{'deleted_plans': [id, ...], 'deleted_drafts':
    int, 'deleted_efls': int, 'deleted_snapshots': int, 'fetched': bool,
    'snapshot_path': str | None, 'downloaded': <download_efls() summary>,
    'parsed': <parse_downloaded_efls() summary>, 'meterplan': {'fetched':
    bool, 'snapshot_path': str | None, 'imported': [id, ...],
    'skipped_battery': int, 'skipped_existing': int, 'flagged_for_review':
    int}, 'promoted': [plan_id, ...], 'needing_review': [draft_stem, ...],
    'notes': [str, ...]}`.
    """
    from energyanalyzer.eflparse.parser import LOAD_BEARING_KEYS
    from energyanalyzer.fetchers.meterplan import (
        fetch_meterplan,
        filter_meterplan,
        load_meterplan,
        meterplan_to_drafts,
    )
    from energyanalyzer.fetchers.ptc import download_efls, fetch_ptc_csv, filter_plans, load_ptc

    plans_dir = Path(plans_dir)
    drafts_dir = Path(drafts_dir)
    efl_dir = Path(efl_dir)
    ptc_dir = Path(ptc_dir)
    meterplan_dir = Path(meterplan_dir)

    notes: list[str] = []
    summary: dict = {
        "deleted_plans": [],
        "deleted_drafts": 0,
        "deleted_efls": 0,
        "deleted_snapshots": 0,
        "fetched": False,
        "snapshot_path": None,
        "downloaded": {"downloaded": [], "skipped": [], "failed": []},
        "parsed": {"parsed": [], "skipped": [], "failed": []},
        "meterplan": {
            "fetched": False,
            "snapshot_path": None,
            "imported": [],
            "skipped_battery": 0,
            "skipped_existing": 0,
            "flagged_for_review": 0,
        },
        "promoted": [],
        "needing_review": [],
        "notes": notes,
    }

    def _report(stage: str, done: int, total: int, item: str) -> None:
        if progress_callback is not None:
            progress_callback(done, total, f"{stage}: {item}")

    # --- 1. delete stale auto-imported data -------------------------------#
    plan_paths_to_delete = []
    if plans_dir.exists():
        for p in sorted(plans_dir.glob("*.yaml")):
            if p.stem == CURRENT_PLAN_ID:
                continue
            try:
                raw = yaml.safe_load(p.read_text()) or {}
            except Exception:  # noqa: BLE001 -- unreadable plan file, leave it alone
                continue
            src = str(raw.get("source") or "")
            if src.startswith("ptc") or src.startswith("efl:") or src == "meterplan":
                plan_paths_to_delete.append(p)

    draft_paths = sorted(drafts_dir.glob("*.yaml")) if drafts_dir.exists() else []
    efl_paths = sorted(efl_dir.glob("*.pdf")) if efl_dir.exists() else []
    snapshot_paths = sorted(ptc_dir.glob("*.csv")) if ptc_dir.exists() else []
    newest_snapshot = max(snapshot_paths, key=lambda p: p.stat().st_mtime) if snapshot_paths else None
    old_snapshots = [p for p in snapshot_paths if p != newest_snapshot]

    delete_total = len(plan_paths_to_delete) + len(draft_paths) + len(efl_paths) + len(old_snapshots)
    delete_done = 0
    for p in plan_paths_to_delete:
        p.unlink(missing_ok=True)
        summary["deleted_plans"].append(p.stem)
        delete_done += 1
        _report("delete", delete_done, delete_total, f"plan {p.name}")
    for p in draft_paths:
        p.unlink(missing_ok=True)
        summary["deleted_drafts"] += 1
        delete_done += 1
        _report("delete", delete_done, delete_total, f"draft {p.name}")
    for p in efl_paths:
        p.unlink(missing_ok=True)
        summary["deleted_efls"] += 1
        delete_done += 1
        _report("delete", delete_done, delete_total, f"efl {p.name}")
    for p in old_snapshots:
        p.unlink(missing_ok=True)
        summary["deleted_snapshots"] += 1
        delete_done += 1
        _report("delete", delete_done, delete_total, f"snapshot {p.name}")

    if plan_paths_to_delete:
        invalidate_plans_cache()

    # --- 2. fetch (or fall back to the newest remaining snapshot) --------#
    snapshot_path = None
    if fetch:
        _report("fetch", 0, 1, "contacting powertochoose.org")
        try:
            snapshot_path = fetch_ptc_csv(ptc_dir)
            summary["fetched"] = True
            _report("fetch", 1, 1, f"saved {snapshot_path.name}")
        except RuntimeError as exc:
            notes.append(f"Live PTC fetch failed, falling back to existing snapshot: {exc}")
            snapshot_path = newest_snapshot
            _report("fetch", 1, 1, "fetch failed -- using existing snapshot")
    else:
        snapshot_path = newest_snapshot
        notes.append("fetch=False -- using existing snapshot without contacting powertochoose.org")

    if snapshot_path is None:
        notes.append(
            "No Power to Choose snapshot available (live fetch failed/skipped and none on "
            "disk) -- PTC download/parse stages skipped."
        )
    else:
        summary["snapshot_path"] = str(snapshot_path)

        # --- 3. load + filter ------------------------------------------#
        df_raw = load_ptc(snapshot_path)
        df = filter_plans(df_raw, tdu=tdu, language=language)

        # --- 4. download EFLs -------------------------------------------#
        summary["downloaded"] = download_efls(
            df, dest=efl_dir, progress_callback=lambda d, t, n: _report("download", d, t, n)
        )

        # --- 5. parse downloaded EFLs into drafts ------------------------#
        pdf_paths = sorted(efl_dir.glob("*.pdf")) if efl_dir.exists() else []
        summary["parsed"] = parse_downloaded_efls(
            pdf_paths,
            drafts_dir=drafts_dir,
            plans_dir=plans_dir,
            progress_callback=lambda d, t, n: _report("parse", d, t, n),
        )

    # --- 6. meterplan.com solar buyback plan index --------------------------#
    # Independent of the PTC stages above (runs even if PTC's live fetch/disk
    # snapshot was unavailable) -- it's a different site, covering solar
    # buyback plans PTC's export lacks. Same fetch-or-fallback-to-newest-disk-
    # snapshot pattern as PTC; tolerated gracefully (noted, not raised) if
    # neither a live fetch nor an existing snapshot is available.
    mp_snapshot_paths = sorted(meterplan_dir.glob("*.md")) if meterplan_dir.exists() else []
    mp_newest_snapshot = (
        max(mp_snapshot_paths, key=lambda p: p.stat().st_mtime) if mp_snapshot_paths else None
    )
    mp_snapshot_path = None
    if fetch:
        _report("meterplan-fetch", 0, 1, "contacting meterplan.com")
        try:
            mp_snapshot_path = fetch_meterplan(meterplan_dir)
            summary["meterplan"]["fetched"] = True
            _report("meterplan-fetch", 1, 1, f"saved {mp_snapshot_path.name}")
        except RuntimeError as exc:
            notes.append(f"Live meterplan.com fetch failed, falling back to existing snapshot: {exc}")
            mp_snapshot_path = mp_newest_snapshot
            _report("meterplan-fetch", 1, 1, "fetch failed -- using existing snapshot")
    else:
        mp_snapshot_path = mp_newest_snapshot
        notes.append(
            "fetch=False -- using existing meterplan.com snapshot without contacting meterplan.com"
        )

    if mp_snapshot_path is None:
        notes.append(
            "No meterplan.com snapshot available (live fetch failed/skipped and none on disk) "
            "-- solar buyback plan index import skipped."
        )
    else:
        summary["meterplan"]["snapshot_path"] = str(mp_snapshot_path)
        try:
            mp_df_raw = load_meterplan(mp_snapshot_path)
            mp_df = filter_meterplan(mp_df_raw, tdu=tdu)

            try:
                surviving_plans = load_plans(plans_dir)
            except Exception:  # noqa: BLE001 -- defensive; dedupe just degrades to "no matches"
                surviving_plans = []
            existing_plan_keys = {
                (p.retailer.strip().lower(), p.name.strip().lower(), p.term_months)
                for p in surviving_plans
            }

            mp_summary = meterplan_to_drafts(mp_df, drafts_dir, existing_plan_keys)
            summary["meterplan"].update(mp_summary)
            _report(
                "meterplan",
                1,
                1,
                f"imported {len(mp_summary['imported'])}, flagged {mp_summary['flagged_for_review']}",
            )
        except Exception as exc:  # noqa: BLE001 -- a bad/corrupt snapshot mustn't abort the run
            notes.append(f"Could not parse meterplan.com snapshot {mp_snapshot_path.name}: {exc!r}")
            _report("meterplan", 1, 1, "snapshot parse failed")

    # --- 7. auto-promote confident drafts (EFL- and meterplan-sourced) -----#
    current_draft_paths = sorted(drafts_dir.glob("*.yaml")) if drafts_dir.exists() else []
    promote_total = len(current_draft_paths)
    for i, draft_path in enumerate(current_draft_paths, start=1):
        try:
            raw = load_draft_raw(draft_path)
            parse_meta = raw.get("_parse") or {}
            confidence = parse_meta.get("confidence") or {}
            seen = [confidence[k] for k in LOAD_BEARING_KEYS if k in confidence]
            min_conf = min(seen) if seen else None
            needs_review_flag = raw.get("needs_review", True)
            eligible = (not needs_review_flag) and min_conf is not None and min_conf >= 0.8
            if eligible:
                plan_dict = {k: v for k, v in raw.items() if k != "_parse"}
                plan_dict.setdefault("source", "ptc")
                plan_dict["retrieved"] = dt.date.today()
                plan = Plan.model_validate(plan_dict)
                save_plan(plan, directory=plans_dir)
                draft_path.unlink(missing_ok=True)
                summary["promoted"].append(plan.id)
            else:
                summary["needing_review"].append(draft_path.stem)
        except Exception as exc:  # noqa: BLE001 -- one bad draft mustn't abort auto-promote
            summary["needing_review"].append(draft_path.stem)
            notes.append(f"Could not auto-promote {draft_path.name}: {exc!r}")
        _report("promote", i, promote_total, draft_path.name)

    if summary["promoted"]:
        invalidate_plans_cache()

    return summary


# --------------------------------------------------------------------------- #
# Plan database git sync: plans/*.yaml is the git-versioned database
# (README.md); plans/drafts/ is gitignored scratch space and never touched
# here. Lets the "commit and push" step happen from the app instead of a
# terminal. Every git call is scoped to the `plans` pathspec only -- never
# `-A` / whole-repo -- so this can't accidentally sweep in unrelated changes.
# --------------------------------------------------------------------------- #
def _run_git(*args: str, repo_root: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def git_plan_db_status(repo_root: Path = REPO_ROOT) -> dict:
    """Pending git changes under plans/ (plans/drafts/ is gitignored and so
    never appears here). Returns `{'branch': str, 'changed': [{'status':
    'M'|'A'|'D'|'??', 'path': str}, ...]}`."""
    status = _run_git("status", "--porcelain", "--", "plans", repo_root=repo_root)
    changed = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        changed.append({"status": line[:2].strip(), "path": line[3:]})
    branch = _run_git("rev-parse", "--abbrev-ref", "HEAD", repo_root=repo_root)
    return {"branch": branch.stdout.strip() or "HEAD", "changed": changed}


def default_plan_db_commit_message(changed: list[dict]) -> str:
    added = sum(1 for c in changed if c["status"] in ("A", "??"))
    modified = sum(1 for c in changed if c["status"] == "M")
    deleted = sum(1 for c in changed if c["status"] == "D")
    parts = []
    if added:
        parts.append(f"{added} added")
    if modified:
        parts.append(f"{modified} modified")
    if deleted:
        parts.append(f"{deleted} deleted")
    return f"Plan database update: {', '.join(parts) if parts else 'no changes'}"


def commit_and_push_plan_db(message: str, repo_root: Path = REPO_ROOT) -> dict:
    """Stage, commit, and push changes under plans/ only. Never raises --
    every failure mode (nothing to commit, git identity not configured,
    push rejected because the remote has commits this checkout doesn't)
    is reported in the returned dict instead, so the UI can show a clear
    message rather than a stack trace. Returns `{'committed': bool,
    'pushed': bool, 'note': str | None, 'error': str | None}`."""
    add = _run_git("add", "--", "plans", repo_root=repo_root)
    if add.returncode != 0:
        return {"committed": False, "pushed": False, "note": None, "error": f"git add failed: {add.stderr.strip()}"}

    staged = _run_git("diff", "--cached", "--quiet", "--", "plans", repo_root=repo_root)
    if staged.returncode == 0:
        return {"committed": False, "pushed": False, "note": "Nothing to commit.", "error": None}

    commit = _run_git("commit", "-m", message, "--", "plans", repo_root=repo_root)
    if commit.returncode != 0:
        err = commit.stderr.strip() or commit.stdout.strip()
        return {"committed": False, "pushed": False, "note": None, "error": f"git commit failed: {err}"}

    push = _run_git("push", repo_root=repo_root)
    if push.returncode != 0:
        return {
            "committed": True,
            "pushed": False,
            "note": None,
            "error": f"Committed locally but push failed: {push.stderr.strip()}",
        }

    return {"committed": True, "pushed": True, "note": None, "error": None}


# --------------------------------------------------------------------------- #
# Staleness (ARCHITECTURE.md §9): warn when the inputs behind the numbers are
# old, rather than silently showing stale results.
# --------------------------------------------------------------------------- #
INTERVAL_STALENESS_DAYS = 35
TDU_STALENESS_DAYS = 210
PLAN_STALENESS_DAYS = 90


def interval_staleness_warning(
    quality: QualityReport, as_of: Optional[dt.date] = None, threshold_days: int = INTERVAL_STALENESS_DAYS
) -> Optional[str]:
    """Warn if the interval data's last day is more than `threshold_days` old."""
    if quality.end is None:
        return None
    as_of = as_of or dt.date.today()
    end_date = quality.end.date() if hasattr(quality.end, "date") else quality.end
    age_days = (as_of - end_date).days
    if age_days > threshold_days:
        return (
            f"Interval data ends {end_date} ({age_days} days ago) -- pull a fresh "
            "SmartMeter Texas export on the Usage page for accurate results."
        )
    return None


def price_coverage_warning(prices: Optional[pd.Series], interval_end) -> Optional[str]:
    """Warn if the loaded ERCOT price series doesn't reach as far as the
    interval data (RTW-indexed plans would be priced on missing data for the
    uncovered tail)."""
    if prices is None or len(prices) == 0 or interval_end is None:
        return None
    price_end = prices.index.max()
    iend = pd.Timestamp(interval_end)
    if getattr(price_end, "tzinfo", None) is not None and iend.tzinfo is None:
        iend = iend.tz_localize(price_end.tzinfo)
    elif getattr(price_end, "tzinfo", None) is None and iend.tzinfo is not None:
        iend = iend.tz_localize(None)
    if pd.Timestamp(price_end) < iend:
        return (
            f"ERCOT price coverage ends {price_end} but interval data ends {iend} -- "
            "RTW-indexed plans will be priced on incomplete/missing data for the gap."
        )
    return None


def tdu_staleness_warning(
    tariff: TduTariff, as_of: Optional[dt.date] = None, threshold_days: int = TDU_STALENESS_DAYS
) -> Optional[str]:
    """Warn if the latest known Oncor tariff record is old enough that a rate
    change (typically Mar/Sep) may have been missed."""
    as_of = as_of or dt.date.today()
    age_days = (as_of - tariff.effective).days
    if age_days > threshold_days:
        return (
            f"Latest Oncor TDU tariff is effective {tariff.effective} ({age_days} days ago) -- "
            "check for a rate change and update tdu/oncor.yaml if needed."
        )
    return None


def plan_is_stale(
    plan: Plan, as_of: Optional[dt.date] = None, threshold_days: int = PLAN_STALENESS_DAYS
) -> bool:
    """True if `plan`'s rate data looks stale: `retrieved` older than
    `threshold_days`, or no `retrieved` at all on a `report-*` seed plan."""
    as_of = as_of or dt.date.today()
    if plan.retrieved is not None:
        return (as_of - plan.retrieved).days > threshold_days
    return plan.source.startswith("report-")


def stale_plan_ids(
    plans: list[Plan], as_of: Optional[dt.date] = None, threshold_days: int = PLAN_STALENESS_DAYS
) -> list[str]:
    return [p.id for p in plans if plan_is_stale(p, as_of=as_of, threshold_days=threshold_days)]


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
