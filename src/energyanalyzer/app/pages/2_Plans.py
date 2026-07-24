"""Plans page (ARCHITECTURE.md §9): plan table, detail view, add/edit form,
EFL PDF import, and Power to Choose snapshot loading."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from energyanalyzer.app.common import (  # noqa: E402
    CURRENT_PLAN_ID,
    EFL_DIR,
    METERPLAN_DIR,
    PTC_DIR,
    commit_and_push_plan_db,
    default_plan_db_commit_message,
    draft_summary_row,
    efl_pdf_health,
    get_draft_plans,
    get_plans,
    git_plan_db_status,
    invalidate_plans_cache,
    load_draft_raw,
    parse_downloaded_efls,
    plan_is_stale,
    plan_summary_row,
    ptc_efl_resolution_report,
    refresh_market_data,
    render_missing_data_help,
)
from energyanalyzer.core.models import Plan  # noqa: E402
from energyanalyzer.core.plans_io import DRAFTS_DIR, PLANS_DIR, save_plan  # noqa: E402

st.set_page_config(page_title="EnergyAnalyzer - Plans", page_icon="⚡", layout="wide")
st.title("Plans")

plans = get_plans()

# --------------------------------------------------------------------------- #
# Overview table
# --------------------------------------------------------------------------- #
st.subheader("Plan database")
if plans:
    table = pd.DataFrame([plan_summary_row(p) for p in plans])
    st.dataframe(table.drop(columns=["id"]), width="stretch", hide_index=True)
else:
    st.info(f"No plans found in {PLANS_DIR}.")

draft_paths = get_draft_plans()
if draft_paths:
    st.caption(f"{len(draft_paths)} unpromoted draft(s) in {DRAFTS_DIR}: " + ", ".join(p.stem for p in draft_paths))

_efl_health = efl_pdf_health(EFL_DIR)
st.caption(
    f"{len(plans)} plan(s) in {PLANS_DIR} · {_efl_health['total']} EFL PDF(s) in {EFL_DIR}"
    + (f" ({len(_efl_health['invalid'])} unreadable)" if _efl_health["invalid"] else "")
    + "."
)
if _efl_health["invalid"]:
    _bad = _efl_health["invalid"]
    st.warning(
        f"{len(_bad)} file(s) in {EFL_DIR} are not valid PDFs, so the parser can't read them "
        "and they produce no plan. This happens when a download returns an HTML error/redirect "
        "or bot-challenge (captcha) page instead of the EFL. The downloader now rejects non-PDF "
        "responses, so re-running **Refresh market data** clears these; ones that still fail need "
        "a manual capture. Affected: " + ", ".join(_bad[:15]) + (" …" if len(_bad) > 15 else "")
    )

st.divider()

# --------------------------------------------------------------------------- #
# Select-a-plan detail view (editable: save in place, demote to drafts, delete)
# --------------------------------------------------------------------------- #
st.subheader("Plan detail")
if plans:
    detail_plans = sorted(plans, key=lambda p: not plan_is_stale(p))
    plan_labels = {
        f"{'(STALE) ' if plan_is_stale(p) else ''}{p.retailer} — {p.name} ({p.id})": p.id
        for p in detail_plans
    }
    label = st.selectbox("Select a plan", list(plan_labels.keys()), key="detail_select")
    selected_plan = next(p for p in plans if p.id == plan_labels[label])
    if selected_plan.needs_review:
        st.warning("⚠️ NEEDS REVIEW")

    is_current_plan = selected_plan.id == CURRENT_PLAN_ID
    if is_current_plan:
        st.caption(
            "This is your current plan (used for comparisons/exports elsewhere in the app) -- "
            "demote and delete are disabled for it. Editing and saving is still available."
        )

    detail_yaml_default = yaml.safe_dump(
        selected_plan.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    edited_detail_yaml = st.text_area(
        "Plan YAML (editable)",
        value=detail_yaml_default,
        height=360,
        key=f"detail_yaml_{selected_plan.id}",
    )

    dcol1, dcol2, dcol3 = st.columns(3)
    with dcol1:
        if st.button("Save changes", key="detail_save_btn"):
            try:
                edited_dict = yaml.safe_load(edited_detail_yaml)
                edited_plan = Plan.model_validate(edited_dict)
                new_path = save_plan(edited_plan, directory=PLANS_DIR)
                if edited_plan.id != selected_plan.id:
                    (PLANS_DIR / f"{selected_plan.id}.yaml").unlink(missing_ok=True)
                invalidate_plans_cache()
                st.success(f"Saved {new_path}")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not save: {exc}")
    with dcol2:
        if st.button("Demote to drafts", key="detail_demote_btn", disabled=is_current_plan):
            try:
                edited_dict = yaml.safe_load(edited_detail_yaml)
                edited_plan = Plan.model_validate(edited_dict)
                draft_path = save_plan(edited_plan, directory=DRAFTS_DIR)
                (PLANS_DIR / f"{selected_plan.id}.yaml").unlink(missing_ok=True)
                invalidate_plans_cache()
                st.success(f"Moved to {draft_path}")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not demote: {exc}")
    with dcol3:
        detail_delete_confirm = st.checkbox(
            "Confirm delete", key="detail_delete_confirm", disabled=is_current_plan
        )
        if st.button(
            "Delete plan",
            key="detail_delete_btn",
            disabled=is_current_plan or not detail_delete_confirm,
        ):
            (PLANS_DIR / f"{selected_plan.id}.yaml").unlink(missing_ok=True)
            invalidate_plans_cache()
            st.success(f"Deleted {selected_plan.id}.yaml")
            st.rerun()
else:
    st.info(f"No plans found in {PLANS_DIR}.")

st.divider()

# --------------------------------------------------------------------------- #
# Add / Edit plan form
# --------------------------------------------------------------------------- #
st.subheader("Add / edit plan")

edit_choices = ["-- New plan --"] + [f"{p.retailer} — {p.name} ({p.id})" for p in plans]
edit_label = st.selectbox("Create new, or edit existing:", edit_choices, key="edit_select")
editing_plan: Plan | None = None
if edit_label != "-- New plan --":
    edit_id = edit_label.rsplit("(", 1)[-1].rstrip(")")
    editing_plan = next((p for p in plans if p.id == edit_id), None)

form_key_suffix = editing_plan.id if editing_plan else "__new__"


def _default_yaml(obj) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)


if editing_plan is not None:
    dump = editing_plan.model_dump(mode="json", exclude_none=True)
    defaults = dict(
        id=editing_plan.id,
        retailer=editing_plan.retailer,
        name=editing_plan.name,
        term_months=editing_plan.term_months,
        tdu=editing_plan.tdu,
        base_charge_usd=editing_plan.base_charge_usd,
        tdu_passthrough=editing_plan.tdu_passthrough,
        etf_usd=editing_plan.etf_usd,
        etf_per_month_remaining=editing_plan.etf_per_month_remaining,
        rate_type=editing_plan.rate_type,
        renewable_pct=editing_plan.renewable_pct or 0.0,
        source=editing_plan.source,
        efl_url=editing_plan.efl_url or "",
        notes=editing_plan.notes,
        needs_review=editing_plan.needs_review,
    )
    energy_rates_default = _default_yaml(dump.get("energy_rates", []))
    buyback_default = _default_yaml(dump.get("buyback", {"kind": "none"}))
    bill_credits_default = _default_yaml(dump.get("bill_credits", []))
    ev_free_default = _default_yaml(dump["ev_free_charging"]) if dump.get("ev_free_charging") else ""
else:
    defaults = dict(
        id="",
        retailer="",
        name="",
        term_months=12,
        tdu="ONCOR",
        base_charge_usd=0.0,
        tdu_passthrough=True,
        etf_usd=0.0,
        etf_per_month_remaining=False,
        rate_type="fixed",
        renewable_pct=0.0,
        source="manual",
        efl_url="",
        notes="",
        needs_review=False,
    )
    energy_rates_default = _default_yaml([{"rate_ckwh": 12.0}])
    buyback_default = _default_yaml({"kind": "none"})
    bill_credits_default = _default_yaml([])
    ev_free_default = ""

with st.form(f"plan_form_{form_key_suffix}"):
    c1, c2 = st.columns(2)
    with c1:
        f_id = st.text_input("Plan ID (filename-safe, unique)", value=defaults["id"])
        f_retailer = st.text_input("Retailer", value=defaults["retailer"])
        f_name = st.text_input("Plan name", value=defaults["name"])
        f_term = st.number_input("Term (months)", min_value=1, max_value=60, value=int(defaults["term_months"]))
        f_tdu = st.text_input("TDU", value=defaults["tdu"])
        f_base = st.number_input(
            "Base charge $/mo", min_value=0.0, value=float(defaults["base_charge_usd"]), step=0.01, format="%.2f"
        )
        f_passthrough = st.checkbox("TDU passthrough (delivery billed separately)", value=defaults["tdu_passthrough"])
    with c2:
        f_etf = st.number_input("ETF $", min_value=0.0, value=float(defaults["etf_usd"]), step=1.0)
        f_etf_per_month = st.checkbox("ETF is per-month-remaining", value=defaults["etf_per_month_remaining"])
        f_rate_type = st.selectbox(
            "Rate type",
            ["fixed", "variable", "indexed"],
            index=["fixed", "variable", "indexed"].index(defaults["rate_type"]),
        )
        f_renewable = st.number_input(
            "Renewable %", min_value=0.0, max_value=100.0, value=float(defaults["renewable_pct"])
        )
        f_source = st.text_input("Source", value=defaults["source"])
        f_efl_url = st.text_input("EFL URL", value=defaults["efl_url"])
        f_needs_review = st.checkbox("Needs review", value=defaults["needs_review"])
    f_notes = st.text_area("Notes", value=defaults["notes"], height=68)

    st.markdown(
        "**Energy rates** -- YAML list of `{rate_ckwh | rtw, window, label, tdu_exempt}`. "
        "First matching window wins; the last entry must have no `window` (catch-all)."
    )
    f_energy_rates = st.text_area(
        "energy_rates", value=energy_rates_default, height=160, label_visibility="collapsed"
    )

    st.markdown("**Buyback** -- YAML dict: `kind: none|fixed|rtw|windows`, plus its fields.")
    f_buyback = st.text_area("buyback", value=buyback_default, height=140, label_visibility="collapsed")

    st.markdown("**Bill credits** -- YAML list of `{min_kwh, max_kwh, credit_usd}` (optional).")
    f_bill_credits = st.text_area(
        "bill_credits", value=bill_credits_default, height=80, label_visibility="collapsed"
    )

    st.markdown(
        "**Free EV charging** -- optional YAML `{window: {hours: [...]}, monthly_kwh_cap: N}` "
        "(e.g. Tesla: waives the energy charge on the first N kWh/mo inside the window). "
        "Leave blank for none."
    )
    f_ev_free = st.text_area(
        "ev_free_charging", value=ev_free_default, height=100, label_visibility="collapsed"
    )

    f_save_target = st.radio(
        "Save to", ["Active plans (plans/)", "Drafts (plans/drafts/)"], horizontal=True
    )
    submitted = st.form_submit_button("Validate & save")

if submitted:
    try:
        plan_dict = dict(
            id=f_id.strip(),
            retailer=f_retailer.strip(),
            name=f_name.strip(),
            term_months=int(f_term),
            tdu=f_tdu.strip(),
            base_charge_usd=float(f_base),
            tdu_passthrough=f_passthrough,
            etf_usd=float(f_etf),
            etf_per_month_remaining=f_etf_per_month,
            rate_type=f_rate_type,
            renewable_pct=(f_renewable or None),
            source=f_source.strip(),
            efl_url=(f_efl_url.strip() or None),
            notes=f_notes,
            needs_review=f_needs_review,
            energy_rates=yaml.safe_load(f_energy_rates) or [],
            buyback=yaml.safe_load(f_buyback) or {"kind": "none"},
            bill_credits=yaml.safe_load(f_bill_credits) or [],
            ev_free_charging=(yaml.safe_load(f_ev_free) if f_ev_free.strip() else None),
        )
        plan = Plan.model_validate(plan_dict)
        target_dir = PLANS_DIR if f_save_target.startswith("Active") else DRAFTS_DIR
        path = save_plan(plan, directory=target_dir)
        if editing_plan is not None:
            # editing_plan always comes from the active plans/ list (see
            # edit_choices above); if the id changed or it was saved to
            # Drafts instead, the original active file is now stale --
            # remove it so this acts as a rename/demote, not a duplicate.
            original_path = PLANS_DIR / f"{editing_plan.id}.yaml"
            if original_path != path and original_path.exists():
                original_path.unlink()
        invalidate_plans_cache()
        st.success(f"Saved {path}")
        st.rerun()
    except Exception as exc:  # noqa: BLE001 -- surface any validation/YAML error to the user
        st.error(f"Could not save plan: {exc}")

st.divider()

# --------------------------------------------------------------------------- #
# Import from EFL PDF
# --------------------------------------------------------------------------- #
st.subheader("Import from EFL PDF")
st.caption(
    "Runs the static EFL parser (no network / no LLM calls). Extracted fields are shown "
    "with confidence scores and source evidence for review before saving."
)
uploaded_pdf = st.file_uploader("Upload EFL PDF", type=["pdf"], key="efl_pdf_uploader")
if uploaded_pdf is not None:
    EFL_DIR.mkdir(parents=True, exist_ok=True)
    efl_path = EFL_DIR / uploaded_pdf.name
    efl_path.write_bytes(uploaded_pdf.getvalue())
    st.caption(f"Saved to {efl_path}")
    if st.button("Parse EFL", key="parse_efl_btn"):
        try:
            # Lazy import: eflparse is being finished by another agent; degrade
            # gracefully if the module/signature isn't ready yet.
            from energyanalyzer.eflparse.parser import parse_efl  # noqa: PLC0415

            draft = parse_efl(efl_path)
            st.session_state["efl_draft"] = draft
        except Exception as exc:  # noqa: BLE001
            st.error(f"EFL parsing failed (parser may still be in progress): {exc}")

draft = st.session_state.get("efl_draft")
if draft is not None:
    st.markdown("**Extracted fields**")
    confidence = getattr(draft, "confidence", {}) or {}
    evidence = getattr(draft, "evidence", {}) or {}
    rows = [
        {"field": field, "confidence": conf, "evidence": evidence.get(field, "")}
        for field, conf in sorted(confidence.items())
    ]
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    unparsed = getattr(draft, "unparsed_notes", []) or []
    if unparsed:
        with st.expander(f"{len(unparsed)} unparsed note(s)"):
            for n in unparsed:
                st.caption(f"- {n}")

    st.markdown("**Edit before saving**")
    plan_dict_default = getattr(draft, "plan_dict", {}) or {}
    edited_yaml = st.text_area(
        "Draft plan YAML",
        value=yaml.safe_dump(plan_dict_default, sort_keys=False, allow_unicode=True),
        height=300,
        key="efl_draft_yaml",
    )
    efl_save_target = st.radio(
        "Save to", ["Active plans (plans/)", "Drafts (plans/drafts/)"], horizontal=True, key="efl_save_target"
    )
    if st.button("Validate & save", key="efl_save_btn"):
        try:
            edited_dict = yaml.safe_load(edited_yaml)
            plan = Plan.model_validate(edited_dict)
            target_dir = PLANS_DIR if efl_save_target.startswith("Active") else DRAFTS_DIR
            path = save_plan(plan, directory=target_dir)
            invalidate_plans_cache()
            st.success(f"Saved {path}")
            del st.session_state["efl_draft"]
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not save: {exc}")

st.divider()

# --------------------------------------------------------------------------- #
# Refresh market data (one button: delete old imports, fetch, download,
# parse, auto-promote confident drafts)
# --------------------------------------------------------------------------- #
st.subheader("Refresh market data")
st.caption(
    "One button: deletes previously auto-imported plans/drafts/EFLs and old PTC "
    "snapshots, re-fetches the Power to Choose snapshot (falling back to the newest "
    "one on disk if the live fetch fails), re-downloads EFLs, re-parses them into "
    "drafts, re-fetches the meterplan.com solar buyback plan index the same way and "
    "turns it into drafts (deduped against plans already in the database), and "
    "auto-promotes anything the parser was confident about (all load-bearing fields "
    ">= 0.8 confidence, not flagged needs_review). Less-certain drafts are left in "
    "'Draft plans' below for manual review. Manually-entered and report-benchmark "
    "plans -- including your current plan -- are never touched."
)
refresh_confirm = st.checkbox(
    "I understand auto-imported plans will be replaced", key="refresh_confirm"
)
run_discovery = st.checkbox(
    "Also discover solar buyback plans from retailer sites (slow)",
    key="refresh_run_discovery",
    help=(
        "Beyond Power to Choose and meterplan.com, query individual retailer marketing "
        "sites (Green Mountain, TXU, Chariot, Gexa, Frontier, Octopus, Champion, Direct Energy, "
        "Reliant, Atlantex, Ambit) for solar "
        "**buyback** EFLs the aggregators miss. Each site is a live browser session, so a "
        "full sweep takes several minutes; per-retailer results appear below when it "
        "finishes.\n\n"
        "**Requires the Playwright browser:** `pip install 'energyanalyzer[discovery]' && "
        "playwright install chromium`. A retailer whose browser step fails is reported as "
        "an error and the rest still run.\n\n"
        "**Manual captures:** some sites block automation. Ambit's plans page must be saved "
        "by hand as `data/rep_discovery/ambit_<timestamp>.html` (open it in a browser, "
        "complete the ZIP gate, then Save Page As -> Web Page, HTML Only). Any retailer "
        "shown as 'manual-needed' below is captured the same way.\n\n"
        "**Private info stays local:** Octopus needs your ESI ID (its ZIP spans load "
        "zones). Put it in the gitignored `data/rep_discovery_secrets.yaml` under "
        "`octopus:` -- it is never committed or logged."
    ),
)
discovery_zip = st.text_input(
    "Discovery ZIP code",
    value="78665",
    key="refresh_discovery_zip",
    help="ZIP entered into each retailer's plan-shopping gate during discovery.",
    disabled=not run_discovery,
)
if st.button("Refresh market data", key="refresh_market_btn", disabled=not refresh_confirm):
    refresh_progress = st.progress(0.0)
    refresh_status = st.empty()

    def _refresh_progress(done: int, total: int, label: str) -> None:
        refresh_progress.progress(done / total if total else 1.0)
        refresh_status.caption(label)

    refresh_summary = refresh_market_data(
        plans_dir=PLANS_DIR,
        drafts_dir=DRAFTS_DIR,
        efl_dir=EFL_DIR,
        ptc_dir=PTC_DIR,
        meterplan_dir=METERPLAN_DIR,
        progress_callback=_refresh_progress,
        run_discovery=run_discovery,
        discovery_zip=discovery_zip.strip() or "78665",
    )
    refresh_progress.progress(1.0)
    invalidate_plans_cache()
    st.session_state["refresh_summary"] = refresh_summary
    st.rerun()

refresh_summary = st.session_state.get("refresh_summary")
if refresh_summary is not None:
    st.success(
        f"Deleted {len(refresh_summary['deleted_plans'])} old imported plan(s), "
        f"{refresh_summary['deleted_drafts']} draft(s), {refresh_summary['deleted_efls']} EFL(s). "
        f"Downloaded {len(refresh_summary['downloaded']['downloaded'])}, "
        f"parsed {len(refresh_summary['parsed']['parsed'])}, "
        f"auto-promoted {len(refresh_summary['promoted'])}, "
        f"{len(refresh_summary['needing_review'])} draft(s) left for review."
    )
    # Surface EFLs that couldn't be downloaded or turned into a draft YAML, so a
    # failed download (HTML/captcha saved as .pdf) or an unparseable PDF isn't
    # silent. Aggregates the PTC and REP-discovery download/parse failure lists.
    _disc = refresh_summary.get("discovery") or {}
    _dl_failed = list(refresh_summary["downloaded"].get("failed", [])) + list(
        (_disc.get("downloaded") or {}).get("failed", [])
    )
    _parse_failed = list(refresh_summary["parsed"].get("failed", [])) + list(
        (_disc.get("parsed") or {}).get("failed", [])
    )
    if _dl_failed or _parse_failed:
        _msg = []
        if _dl_failed:
            _msg.append(f"{len(_dl_failed)} EFL download(s) failed or weren't valid PDFs")
        if _parse_failed:
            _msg.append(f"{len(_parse_failed)} downloaded PDF(s) couldn't be parsed into a plan")
        st.warning(
            " · ".join(_msg)
            + ". These produced no plan; see the details below. Non-PDF responses (an HTML "
            "error/redirect or captcha page) are the usual cause; a genuinely image-only EFL "
            "would need OCR."
        )
        with st.expander("EFL download/parse failures"):
            if _dl_failed:
                st.caption("Download failures:")
                st.json(_dl_failed[:25])
            if _parse_failed:
                st.caption("Parse failures:")
                st.json(_parse_failed[:25])
    mp_refresh = refresh_summary.get("meterplan") or {}
    st.caption(
        f"Meterplan solar plan index: imported {len(mp_refresh.get('imported', []))} draft(s) "
        f"({mp_refresh.get('skipped_battery', 0)} battery-required skipped, "
        f"{mp_refresh.get('skipped_existing', 0)} already in the plan database, "
        f"{mp_refresh.get('flagged_for_review', 0)} flagged for review)."
    )
    disc_refresh = refresh_summary.get("discovery") or {}
    if disc_refresh.get("enabled"):
        disc_reps = disc_refresh.get("reps") or {}
        disc_dl = disc_refresh.get("downloaded") or {}
        disc_parsed = disc_refresh.get("parsed") or {}
        n_ok = sum(1 for r in disc_reps.values() if r.get("status") == "ok")
        st.caption(
            f"REP-site discovery: queried {len(disc_reps)} retailer(s), {n_ok} ok; "
            f"downloaded {len(disc_dl.get('downloaded', []))} buyback EFL(s), "
            f"parsed {len(disc_parsed.get('parsed', []))} into draft(s)."
        )
        if disc_reps:
            st.dataframe(
                [
                    {
                        "Retailer": r.get("retailer", key),
                        "Status": r.get("status", ""),
                        "Plans": r.get("plans_found", 0),
                        "Buyback": r.get("buyback", 0),
                        "Detail": r.get("detail", ""),
                    }
                    for key, r in disc_reps.items()
                ],
                hide_index=True,
            )
    for note in refresh_summary["notes"]:
        st.caption(f"- {note}")
    with st.expander("Full refresh summary"):
        st.json(refresh_summary)

st.divider()

# --------------------------------------------------------------------------- #
# Plan database sync (plans/*.yaml is the git-versioned database; commit and
# push from here instead of a terminal)
# --------------------------------------------------------------------------- #
st.subheader("Plan database sync")
st.caption(
    "plans/*.yaml is the git-versioned plan database (plans/drafts/ is local-only "
    "scratch space, never committed). Review the changes below, then commit and push."
)
db_status = git_plan_db_status()
if not db_status["changed"]:
    st.caption("Nothing to commit -- plan database matches the last commit.")
else:
    st.caption(f"{len(db_status['changed'])} file(s) changed on branch `{db_status['branch']}`:")
    with st.expander("Changed files", expanded=True):
        for c in db_status["changed"]:
            st.text(f"{c['status']:>2}  {c['path']}")
    commit_message = st.text_input(
        "Commit message",
        value=default_plan_db_commit_message(db_status["changed"]),
        key="plan_db_commit_msg",
    )
    push_confirm = st.checkbox("I've reviewed the changed files above", key="plan_db_push_confirm")
    if st.button("Commit & push plan database", key="plan_db_commit_btn", disabled=not push_confirm):
        result = commit_and_push_plan_db(commit_message)
        if result["error"]:
            st.error(result["error"])
        elif result["pushed"]:
            st.success(f"Committed and pushed to `{db_status['branch']}`.")
            st.session_state.pop("plan_db_push_confirm", None)
            st.rerun()
        else:
            st.info(result["note"] or "Nothing to commit.")

st.divider()

# --------------------------------------------------------------------------- #
# Power to Choose snapshot
# --------------------------------------------------------------------------- #
st.subheader("Power to Choose snapshot")
st.caption(
    "Load a previously-downloaded Power to Choose CSV snapshot (data/ptc/), or fetch a new one."
)

if st.button("Fetch new snapshot from powertochoose.org", key="fetch_ptc_btn"):
    try:
        from energyanalyzer.fetchers.ptc import fetch_ptc_csv  # noqa: PLC0415

        path = fetch_ptc_csv(PTC_DIR)
        st.success(f"Saved {path}")
        st.rerun()
    except RuntimeError as exc:
        render_missing_data_help(exc, title="Power to Choose download failed")

snapshots = sorted(PTC_DIR.glob("*.csv")) if PTC_DIR.exists() else []
if snapshots:
    chosen_name = st.selectbox(
        "Snapshot", [p.name for p in snapshots], index=len(snapshots) - 1, key="ptc_snapshot_select"
    )
    if st.button("Load snapshot", key="load_ptc_btn"):
        try:
            from energyanalyzer.fetchers.ptc import load_ptc  # noqa: PLC0415

            # Load the raw, unfiltered snapshot -- it's statewide (~1,700 rows,
            # every TDU, English + Spanish duplicate rows for most plans).
            # Filtering (TDU + language) happens below, driven by widgets, so
            # it's clear this is filtering-by-design, not truncation.
            st.session_state["ptc_df_raw"] = load_ptc(PTC_DIR / chosen_name)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not load snapshot: {exc}")
else:
    st.info(f"No snapshots found in {PTC_DIR} yet.")

ptc_df_raw = st.session_state.get("ptc_df_raw")
if ptc_df_raw is not None:
    from energyanalyzer.fetchers.ptc import filter_plans  # noqa: PLC0415

    tdu_choices = sorted(ptc_df_raw["tdu"].dropna().unique()) if "tdu" in ptc_df_raw.columns else []
    if tdu_choices:
        default_idx = tdu_choices.index("ONCOR") if "ONCOR" in tdu_choices else 0
        chosen_tdu = st.selectbox("TDU", tdu_choices, index=default_idx, key="ptc_tdu_select")
    else:
        chosen_tdu = None
    english_only = st.checkbox(
        "English only (hide Spanish-language duplicate rows)", value=True, key="ptc_english_only"
    )
    lang = "English" if english_only else None

    ptc_df = filter_plans(ptc_df_raw, tdu=chosen_tdu, language=lang)
    st.session_state["ptc_df"] = ptc_df

    filter_desc = f"filtering to {chosen_tdu}" if chosen_tdu else "no TDU filter"
    if lang:
        filter_desc += f", {lang}"
    st.caption(f"{len(ptc_df_raw)} rows in snapshot → {len(ptc_df)} after {filter_desc}.")

ptc_df = st.session_state.get("ptc_df")
if ptc_df is not None:
    st.dataframe(ptc_df, width="stretch", height=300)

    if st.button("Check EFL resolution for listed plans", key="check_efl_resolution_btn"):
        st.session_state["efl_resolution_report"] = ptc_efl_resolution_report(ptc_df, efl_dir=EFL_DIR)

    efl_resolution_report = st.session_state.get("efl_resolution_report")
    if efl_resolution_report is not None:
        if efl_resolution_report.empty:
            st.success("Every listed plan resolved to a parseable EFL.")
        else:
            st.warning(
                f"{len(efl_resolution_report)} of {len(ptc_df)} listed plan(s) did not resolve to a "
                "parseable EFL -- check these retailer sites manually:"
            )
            st.dataframe(efl_resolution_report, width="stretch", height=300)

    dl_limit = st.number_input("Max EFLs to download", min_value=1, max_value=500, value=20, key="efl_dl_limit")
    if st.button("Download EFLs for listed plans", key="download_efls_btn"):
        from energyanalyzer.fetchers.ptc import download_efls  # noqa: PLC0415

        progress_bar = st.progress(0.0)
        status_line = st.empty()

        def _dl_progress(done: int, total: int, name: str) -> None:
            progress_bar.progress(done / total if total else 1.0)
            status_line.caption(f"{done}/{total}: {name}")

        try:
            summary = download_efls(
                ptc_df, dest=EFL_DIR, limit=int(dl_limit), progress_callback=_dl_progress
            )
            progress_bar.progress(1.0)
            st.success(
                f"Downloaded {len(summary['downloaded'])}, skipped {len(summary['skipped'])}, "
                f"failed {len(summary['failed'])}"
            )
            if summary["failed"]:
                st.json(summary["failed"][:10])
        except Exception as exc:  # noqa: BLE001
            render_missing_data_help(exc, title="EFL download failed")

st.divider()

# --------------------------------------------------------------------------- #
# Meterplan solar plan index
# --------------------------------------------------------------------------- #
st.subheader("Meterplan solar plan index")
st.caption(
    "Solar buyback plan rates from meterplan.com (published by Meter Energy, a competing "
    "REP/broker) -- covers solar buyback plans Power to Choose's export doesn't carry. Used "
    "strictly as a rate index: their 'Estimated annual cost' column is never used -- "
    "EnergyAnalyzer computes costs itself from your actual interval data."
)

if st.button("Fetch new snapshot from meterplan.com", key="fetch_meterplan_btn"):
    try:
        from energyanalyzer.fetchers.meterplan import fetch_meterplan  # noqa: PLC0415

        path = fetch_meterplan(METERPLAN_DIR)
        st.success(f"Saved {path}")
        st.rerun()
    except RuntimeError as exc:
        render_missing_data_help(exc, title="meterplan.com fetch failed")

mp_snapshots = sorted(METERPLAN_DIR.glob("*.md")) if METERPLAN_DIR.exists() else []
if mp_snapshots:
    mp_chosen_name = st.selectbox(
        "Snapshot",
        [p.name for p in mp_snapshots],
        index=len(mp_snapshots) - 1,
        key="mp_snapshot_select",
    )
    if st.button("Load snapshot", key="load_mp_btn"):
        try:
            from energyanalyzer.fetchers.meterplan import load_meterplan  # noqa: PLC0415

            st.session_state["mp_df_raw"] = load_meterplan(METERPLAN_DIR / mp_chosen_name)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not load snapshot: {exc}")
else:
    st.info(
        f"No snapshots found in {METERPLAN_DIR} yet -- fetch one above, or use 'Refresh market "
        "data' below (falls back to the newest one on disk if the live fetch fails)."
    )

mp_df_raw = st.session_state.get("mp_df_raw")
if mp_df_raw is not None:
    from energyanalyzer.fetchers.meterplan import filter_meterplan  # noqa: PLC0415

    mp_tdu_choices = sorted(mp_df_raw["tdu"].dropna().unique()) if "tdu" in mp_df_raw.columns else []
    if mp_tdu_choices:
        mp_default_idx = mp_tdu_choices.index("Oncor") if "Oncor" in mp_tdu_choices else 0
        mp_chosen_tdu = st.selectbox("TDU", mp_tdu_choices, index=mp_default_idx, key="mp_tdu_select")
    else:
        mp_chosen_tdu = None

    mp_df = filter_meterplan(mp_df_raw, tdu=mp_chosen_tdu)
    st.session_state["mp_df"] = mp_df

    mp_filter_desc = f"filtering to {mp_chosen_tdu}" if mp_chosen_tdu else "no TDU filter"
    st.caption(f"{len(mp_df_raw)} rows in snapshot → {len(mp_df)} after {mp_filter_desc}.")

mp_df = st.session_state.get("mp_df")
if mp_df is not None:
    st.dataframe(mp_df.drop(columns=["raw_row"], errors="ignore"), width="stretch", height=300)
    if st.button("Import as drafts", key="import_meterplan_btn"):
        from energyanalyzer.fetchers.meterplan import meterplan_to_drafts  # noqa: PLC0415

        progress_bar = st.progress(0.0)
        status_line = st.empty()
        status_line.caption(f"0/{len(mp_df)}: building drafts...")

        existing_plan_keys = {
            (p.retailer.strip().lower(), p.name.strip().lower(), p.term_months) for p in plans
        }
        mp_import_summary = meterplan_to_drafts(mp_df, DRAFTS_DIR, existing_plan_keys)
        progress_bar.progress(1.0)
        status_line.caption(
            f"{len(mp_df)}/{len(mp_df)}: "
            f"imported {len(mp_import_summary['imported'])}, "
            f"skipped {mp_import_summary['skipped_battery']} battery-required, "
            f"skipped {mp_import_summary['skipped_existing']} already in the plan database"
        )
        st.success(
            f"Imported {len(mp_import_summary['imported'])} draft(s) "
            f"({mp_import_summary['flagged_for_review']} flagged for review) into {DRAFTS_DIR}."
        )
        st.rerun()

st.divider()

# --------------------------------------------------------------------------- #
# Parse downloaded EFLs into drafts
# --------------------------------------------------------------------------- #
st.subheader("Parse downloaded EFLs")
st.caption(
    "Runs the static EFL parser (ARCHITECTURE.md §8) over every PDF in data/efl/ that "
    "hasn't already produced a draft or promoted plan, and saves the results to "
    "plans/drafts/ for review below. Downloading EFLs alone does not add them to the "
    "plan database -- this step does."
)
efl_pdfs = sorted(EFL_DIR.glob("*.pdf")) if EFL_DIR.exists() else []
st.caption(f"{len(efl_pdfs)} PDF(s) in {EFL_DIR}.")
if efl_pdfs:
    if st.button("Parse all downloaded EFLs into drafts", key="parse_all_efls_btn"):
        progress_bar = st.progress(0.0)
        status_line = st.empty()

        def _parse_progress(done: int, total: int, name: str) -> None:
            progress_bar.progress(done / total if total else 1.0)
            status_line.caption(f"{done}/{total}: {name}")

        summary = parse_downloaded_efls(
            efl_pdfs, drafts_dir=DRAFTS_DIR, plans_dir=PLANS_DIR, progress_callback=_parse_progress
        )
        progress_bar.progress(1.0)
        st.success(
            f"Parsed {len(summary['parsed'])}, skipped {len(summary['skipped'])} (already parsed), "
            f"failed {len(summary['failed'])}"
        )
        if summary["failed"]:
            st.json(summary["failed"][:10])
        st.rerun()
else:
    st.info(
        f"No downloaded EFL PDFs yet in {EFL_DIR} -- use the Power to Choose section above, "
        "or upload one in 'Import from EFL PDF'."
    )

st.divider()

# --------------------------------------------------------------------------- #
# Draft plans: review, edit, promote
# --------------------------------------------------------------------------- #
def _resolve_source_efl_pdf(source: str) -> Path | None:
    """A draft's `source` field is `efl:<filename>.pdf` (eflparse/parser.py);
    resolve that back to the downloaded PDF in EFL_DIR, if it's still there."""
    prefix = "efl:"
    if not source.startswith(prefix):
        return None
    pdf_path = EFL_DIR / source[len(prefix) :]
    return pdf_path if pdf_path.is_file() else None


st.subheader("Draft plans")
st.caption(
    "Auto-parsed (or hand-saved) drafts in plans/drafts/, not yet part of the active plan "
    "database used by Compare/Export. Review the extracted fields, edit the YAML if needed, "
    "then promote."
)

current_draft_paths = get_draft_plans()
if current_draft_paths:
    draft_rows = [draft_summary_row(p) for p in current_draft_paths]
    draft_table = pd.DataFrame(draft_rows)
    st.dataframe(draft_table.drop(columns=["file"]), width="stretch", hide_index=True)

    draft_labels = {
        f"{row['Retailer']} — {row['Plan']} ({row['id']})": path
        for row, path in zip(draft_rows, current_draft_paths)
    }
    draft_label = st.selectbox("Select a draft", list(draft_labels.keys()), key="draft_select")
    selected_draft_path = draft_labels[draft_label]
    raw_draft = load_draft_raw(selected_draft_path)
    parse_meta = raw_draft.get("_parse") or {}
    draft_confidence = parse_meta.get("confidence") or {}
    draft_evidence = parse_meta.get("evidence") or {}
    draft_unparsed = parse_meta.get("unparsed_notes") or []

    source_pdf = _resolve_source_efl_pdf(raw_draft.get("source", ""))
    review_col, pdf_col = st.columns([1, 1])

    with review_col:
        if raw_draft.get("needs_review"):
            st.warning("⚠️ NEEDS REVIEW")

        if draft_confidence:
            st.markdown("**Field confidence / evidence**")
            conf_rows = [
                {"field": field, "confidence": conf, "evidence": draft_evidence.get(field, "")}
                for field, conf in sorted(draft_confidence.items())
            ]
            st.dataframe(pd.DataFrame(conf_rows), width="stretch", hide_index=True)
        if draft_unparsed:
            with st.expander(f"{len(draft_unparsed)} unparsed note(s)"):
                for note in draft_unparsed:
                    st.caption(f"- {note}")

        plan_only_dict = {k: v for k, v in raw_draft.items() if k != "_parse"}
        edited_draft_yaml = st.text_area(
            "Draft plan YAML (editable)",
            value=yaml.safe_dump(plan_only_dict, sort_keys=False, allow_unicode=True),
            height=500,
            key=f"draft_yaml_{selected_draft_path.stem}",
        )

        dcol1, dcol2 = st.columns(2)
        with dcol1:
            if st.button("Promote to plan database", key="promote_draft_btn"):
                try:
                    edited_dict = yaml.safe_load(edited_draft_yaml)
                    edited_dict["retrieved"] = dt.date.today()  # promotion (re)stamps freshness
                    plan = Plan.model_validate(edited_dict)
                    promoted_path = save_plan(plan, directory=PLANS_DIR)
                    selected_draft_path.unlink(missing_ok=True)
                    invalidate_plans_cache()
                    st.success(f"Promoted to {promoted_path}")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not promote: {exc}")
        with dcol2:
            delete_efl_too = st.checkbox(
                "Also delete source EFL PDF",
                key="delete_draft_also_efl",
                disabled=source_pdf is None,
            )
            if st.button("Delete draft", key="delete_draft_btn"):
                selected_draft_path.unlink(missing_ok=True)
                if delete_efl_too and source_pdf is not None:
                    source_pdf.unlink(missing_ok=True)
                    st.success(f"Deleted {selected_draft_path} and {source_pdf}")
                else:
                    st.success(f"Deleted {selected_draft_path}")
                st.rerun()

    with pdf_col:
        if source_pdf is None:
            st.info("Source EFL PDF not found on disk for this draft.")
        else:
            st.caption(f"Source: {source_pdf.name}")
            st.pdf(source_pdf, height=850)
else:
    st.info(f"No drafts found in {DRAFTS_DIR}.")
