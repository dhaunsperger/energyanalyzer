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
import json
from dataclasses import dataclass
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import streamlit as st
import yaml

from energyanalyzer import llm as _llm
from energyanalyzer.core.models import Plan, TduTariff, add_local_columns
from energyanalyzer.core.plans_io import DRAFTS_DIR, PLANS_DIR, current_tdu, load_plans, save_plan
from energyanalyzer.ingest.smt import QualityReport, load_intervals

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
ERCOT_DIR = DATA_DIR / "ercot"
PTC_DIR = DATA_DIR / "ptc"
EFL_DIR = DATA_DIR / "efl"
# EFLs supplied by hand, for REPs no fetcher can reach (a WAF that refuses
# automation, a captcha, a document only linked from a logged-in funnel). A
# refresh wipes data/efl and re-downloads, which is safe only for files a
# fetcher can restore -- a hand-saved PDF is gone for good. Being a
# subdirectory keeps it out of the wipe's non-recursive glob("*.pdf"); the
# parse stage adds it back explicitly.
EFL_MANUAL_DIR = EFL_DIR / "manual"
METERPLAN_DIR = DATA_DIR / "meterplan"
# Where a refresh parks the plans and EFLs it is about to replace, so a
# fetcher that breaks mid-run cannot destroy what it failed to rebuild.
QUARANTINE_DIR = DATA_DIR / "refresh_quarantine"
REP_DISCOVERY_DIR = DATA_DIR / "rep_discovery"
CONFIG_PATH = DATA_DIR / "config.yaml"

# How many REP sites discovery queries at the same time. Each worker may drive
# its own Chromium, so the ceiling here is this box's memory (~7 GB under WSL),
# not politeness -- fetchers/hostpool.py already guarantees no single site is
# ever hit by two workers at once. Discovery was 7m56s of the 15m54s refresh on
# 2026-07-26, nearly all of it spent waiting on other people's JavaScript.
DISCOVERY_MAX_WORKERS = 3

# The premise's real zone, confirmed 2026-07-25 via the ESID lookup at
# electricityplans.com and Tesla's plan page (both report "south"); the ERCOT
# map splits Williamson County across SOUTH / NORTH / AEN / LCRA, so the county
# alone does not settle it. data/config.yaml overrides this and says the same,
# but data/ is gitignored -- so a fresh checkout falls back to this constant,
# and a wrong value here silently misprices every RTW-indexed plan rather than
# failing. That is exactly what happened while this read LZ_NORTH.
DEFAULT_LOAD_ZONE = "LZ_SOUTH"
CURRENT_PLAN_ID = "pulse_current"

DAY_HOURS = list(range(6, 18))  # 6a-6p
PEAK_HOURS = list(range(18, 21))  # 6p-9p
NIGHT_HOURS = list(range(21, 24)) + list(range(0, 6))  # 9p-6a


# --------------------------------------------------------------------------- #
# Plan identity matching (used to supersede synthetic meterplan.com drafts with
# a real EFL once we have one). Retailer/plan names diverge across sources --
# the markdown index says "Reliant Energy / Solar Payback Match", the real EFL
# parses as "Reliant Energy Retail Services LLC / Reliant Solar Payback Match
# 12" -- so exact matching misses. We compare *significant* tokens instead:
# strip generic corporate/industry noise + pure numbers, and treat a match as
# "same retailer brand + same term + the synthetic plan's distinctive name
# tokens all appear in the real plan's name". Deliberately conservative (only
# ever used to remove source="meterplan" plans, and every removal is logged).
# --------------------------------------------------------------------------- #
_RETAILER_NOISE_TOKENS = frozenset(
    {
        "llc", "lp", "inc", "co", "company", "corp", "corporation", "retail",
        "services", "service", "energy", "utilities", "utility", "power",
        "electric", "electricity", "texas", "tx", "rep", "cert", "certificate",
        "number", "no", "dba", "the", "of", "and",
    }
)


# Abbreviations one source uses where another spells the word out. meterplan.com
# writes "Solar BB System Flex" for what TXU calls "Solar Buyback System Flex",
# and {bb, flex, solar, system} is not a subset of {buyback, flexsm, solar,
# system}, so the synthetic row was never superseded by the real EFL -- leaving
# a stale meterplan rate ranked ABOVE the plan it stands in for. Keep this list
# tiny and unambiguous: a wrong synonym silently merges two different plans.
_TOKEN_SYNONYMS = {"bb": "buyback"}


def _significant_tokens(text: str, extra_drop: frozenset = frozenset()) -> set:
    """Lowercased alphanumeric tokens with corporate/industry noise, term
    numbers, and `<n>mo` term tokens removed (plus any `extra_drop`).

    Only *term-sized* numbers (1..60) are dropped -- callers compare the term
    separately, so the term number carries no identity, but a larger number
    usually names the product: "Smart 1000 Select 12" and "Smart 2000 Select 12"
    are different plans (the number is the usage tier the bill credit keys off).
    Dropping every pure number collapsed both to {smart, select} and made them
    compare equal, so PTC dedup discarded the 2000 as a duplicate of the 1000.
    """
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    drop = _RETAILER_NOISE_TOKENS | extra_drop
    out = set()
    for tok in cleaned.split():
        # Strip a service mark fused to the word by the (R)/(TM) glyph being
        # dropped above: "FlexSM" -> "flex", "Pollution FreeTM" -> "free",
        # "12SM" -> "12". `_NAME_FILLER_TOKENS` already drops a STANDALONE "sm",
        # which never helped here because the mark is not a separate token.
        #
        # The stem must be >=4 chars, or a digit run. Both guards are needed:
        # plenty of ordinary words end in -sm/-tm, and a looser rule turned
        # "Prism" into "pri". Audited over every plan name and retailer on disk,
        # this touches only 12sm, 24sm, flexsm, forwardsm and freetm.
        if re.fullmatch(r"\d+(?:sm|tm)", tok) or (
            len(tok) >= 6 and tok.endswith(("sm", "tm")) and tok[:-2].isalpha()
        ):
            tok = tok[:-2]
        tok = _TOKEN_SYNONYMS.get(tok, tok)
        if tok in drop or re.fullmatch(r"\d+mo", tok):
            continue
        if tok.isdigit() and int(tok) <= 60:  # a term, not a product number
            continue
        out.add(tok)
    return out


def _plan_supersedes(meter_plan: Plan, auth_plan: Plan) -> bool:
    """True if `auth_plan` (a real/authoritative plan) covers the same plan as
    the synthetic meterplan `meter_plan`: same term, overlapping retailer brand
    tokens, and every distinctive token of the synthetic plan's name present in
    the authoritative plan's name (retailer brand tokens removed from both)."""
    if meter_plan.term_months != auth_plan.term_months:
        return False
    r_meter = _significant_tokens(meter_plan.retailer)
    r_auth = _significant_tokens(auth_plan.retailer)
    if not r_meter or not r_auth:
        return False
    # One retailer's brand tokens must be a subset of the other's (handles the
    # short markdown name vs the verbose legal name).
    if not (r_meter <= r_auth or r_auth <= r_meter):
        return False
    n_meter = _significant_tokens(meter_plan.name, extra_drop=frozenset(r_meter | {"plan"}))
    n_auth = _significant_tokens(auth_plan.name, extra_drop=frozenset(r_auth | r_meter | {"plan"}))
    if not n_meter:
        return False
    return n_meter <= n_auth


# Filler/trademark tokens dropped from plan names before comparing identity.
_NAME_FILLER_TOKENS = frozenset(
    {"plan", "sm", "tm", "new", "customer", "special", "product", "residential", "meters", "meter"}
)


def _term_from_name(name: str) -> Optional[int]:
    """Best-effort contract term (months) parsed from a plan name, e.g.
    "Champ Saver 12" -> 12, "Sun Confidence 24 month" -> 24. Returns None when
    no plausible term (1..60) is present -- REP plan names often omit it."""
    for match in re.finditer(r"\b(\d{1,2})\b", name or ""):
        val = int(match.group(1))
        if 1 <= val <= 60:
            return val
    return None


_SAME_PLAN_SYSTEM = (
    "You decide whether two Texas retail-electricity plan listings describe the "
    "SAME underlying plan. The two listings come from different sources, so the "
    "retailer may appear as a short brand or a full legal name, and the plan name "
    "may add or drop a marketing suffix, a term number, or a trademark. That does "
    "NOT make them different plans.\n"
    "They are DIFFERENT plans if the names carry different distinguishing product "
    "features (e.g. 'Free Weekends' vs plain, 'Plus' vs 'Saver', EV vs non-EV, "
    "solar-buyback vs conventional) or different contract terms.\n"
    'Respond with exactly: {"same_plan": true|false, "confidence": 0..1, '
    '"reasoning": "one short sentence"}'
)


def _llm_same_plan(
    a: dict,
    b: dict,
    *,
    min_confidence: float = 0.7,
    chat_fn=None,
    model: str = _llm.OLLAMA_MODEL,
    ollama_url: str = _llm.OLLAMA_URL,
    timeout: float = 60.0,
) -> Optional[bool]:
    """Ask the local LLM whether two plan listings are the same plan.

    ``a``/``b`` are ``{"retailer", "plan_name", "term"}``. Returns True/False, or
    **None** when the LLM is unavailable, returns junk, or isn't confident enough
    -- callers must treat None as "no opinion" and fall back to deterministic
    behaviour (keep the plan).
    """
    user = (
        f"Listing A: retailer={a.get('retailer')!r}, plan={a.get('plan_name')!r}, "
        f"term_months={a.get('term')}\n"
        f"Listing B: retailer={b.get('retailer')!r}, plan={b.get('plan_name')!r}, "
        f"term_months={b.get('term')}"
    )
    parsed = _llm.chat_json(
        [{"role": "system", "content": _SAME_PLAN_SYSTEM}, {"role": "user", "content": user}],
        model=model,
        ollama_url=ollama_url,
        timeout=timeout,
        chat_fn=chat_fn,
    )
    if not parsed:
        return None
    try:
        confidence = float(parsed.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if confidence < min_confidence:
        return None
    return bool(parsed.get("same_plan", False))


def _discovered_plan_in_ptc(
    retailer: str,
    plan_name: str,
    ptc_index: list,
    *,
    llm_adjudicate: bool = False,
    max_llm_candidates: int = 3,
    chat_fn=None,
    model: str = _llm.OLLAMA_MODEL,
    ollama_url: str = _llm.OLLAMA_URL,
    timeout: float = 60.0,
) -> bool:
    """True if a discovered REP plan is already covered by the PTC listing.

    Deterministic-first: a discovered plan must share retailer brand tokens and an
    equal term with a PTC row to be a *candidate* at all (term is parsed from the
    discovered name -- discovery plans carry no term field, and with no term we
    keep the plan). Candidates whose distinctive name tokens match exactly are
    duplicates outright, with no LLM involved.

    When ``llm_adjudicate`` is set, the remaining *ambiguous* candidates (same
    retailer + term, similar-but-not-identical names -- e.g. "e-Plus 12" vs
    "e-Plus 12 Choice") are put to the LLM, which token matching can't settle
    without brittle suffix allow-lists. The LLM is only ever asked about
    already-narrowed candidates (at most ``max_llm_candidates``), and "no opinion"
    (LLM down / unsure) falls back to keeping the plan.

    ``ptc_index`` is built by :func:`_build_ptc_identity_index`.
    """
    d_ret = _significant_tokens(retailer)
    if not d_ret:
        return False
    d_term = _term_from_name(plan_name)
    if d_term is None:
        return False
    d_name = _significant_tokens(plan_name, extra_drop=frozenset(d_ret | _NAME_FILLER_TOKENS))
    if not d_name:
        return False

    candidates = [
        e
        for e in ptc_index
        if e["term"] == d_term and (d_ret <= e["r_tokens"] or e["r_tokens"] <= d_ret)
    ]
    if not candidates:
        return False
    # Exact distinctive-name match -> duplicate, deterministically.
    if any(e["n_tokens"] == d_name for e in candidates):
        return True
    if not llm_adjudicate:
        return False

    for entry in candidates[:max_llm_candidates]:
        verdict = _llm_same_plan(
            {"retailer": retailer, "plan_name": plan_name, "term": d_term},
            {"retailer": entry["retailer"], "plan_name": entry["plan_name"], "term": entry["term"]},
            chat_fn=chat_fn,
            model=model,
            ollama_url=ollama_url,
            timeout=timeout,
        )
        if verdict:
            logger.info(
                "LLM dedup: %r (%s) == PTC %r (%s)",
                plan_name, retailer, entry["plan_name"], entry["retailer"],
            )
            return True
    return False


def _build_ptc_identity_index(ptc_df) -> list:
    """Build the PTC plan-identity index for :func:`_discovered_plan_in_ptc`.

    Each entry is ``{"r_tokens", "n_tokens", "term", "retailer", "plan_name"}`` --
    tokens for the deterministic pass, and the raw strings so an ambiguous pair
    can be described to the LLM adjudicator.
    """
    index: list = []
    if ptc_df is None:
        return index
    for _, row in ptc_df.iterrows():
        retailer = str(row.get("retailer") or "")
        name = str(row.get("plan_name") or "")
        r_tokens = _significant_tokens(retailer)
        n_tokens = _significant_tokens(name, extra_drop=frozenset(r_tokens | _NAME_FILLER_TOKENS))
        term_raw = row.get("term_months")
        try:
            term = int(term_raw) if pd.notna(term_raw) else None
        except (TypeError, ValueError):
            term = None
        index.append(
            {
                "r_tokens": r_tokens,
                "n_tokens": n_tokens,
                "term": term,
                "retailer": retailer,
                "plan_name": name,
            }
        )
    return index


@dataclass
class _CoverageCandidate:
    """Minimal stand-in for a Plan, so a *draft* can act as authoritative
    coverage in :func:`_plan_supersedes` (which only reads retailer/name/term).
    Drafts can't be Plan-validated in general -- that's often exactly why
    they're still drafts -- so we don't try."""

    id: str
    retailer: str
    name: str
    term_months: Optional[int]


def _real_draft_candidates(drafts_dir: Path) -> list:
    """Real (non-meterplan) drafts, as coverage candidates.

    A parsed EFL sitting in review is still better evidence than a synthetic
    index row, so it should stop that synthetic from being promoted over it.
    """
    out = []
    for path in sorted(Path(drafts_dir).glob("*.yaml")) if Path(drafts_dir).exists() else []:
        try:
            raw = load_draft_raw(path)
        except Exception:  # noqa: BLE001 -- an unreadable draft simply isn't coverage
            continue
        if str(raw.get("source") or "") == "meterplan":
            continue
        out.append(
            _CoverageCandidate(
                id=str(raw.get("id") or path.stem),
                retailer=str(raw.get("retailer") or ""),
                name=str(raw.get("name") or ""),
                term_months=raw.get("term_months"),
            )
        )
    return out


def _covered_by_real(synthetic, candidates) -> Optional[object]:
    """The first authoritative plan/draft covering this synthetic, or None."""
    return next((c for c in candidates if _plan_supersedes(synthetic, c)), None)


def supersede_meterplan_plans(
    plans_dir: Path = PLANS_DIR, drafts_dir: Path = DRAFTS_DIR
) -> list[tuple]:
    """Delete synthetic meterplan.com plans (`source="meterplan"`, no EFL PDF)
    that a real/authoritative plan now covers, and return the removals as a list
    of ``(removed_plan_id, superseding_plan_id)`` tuples.

    "Authoritative" is any plan whose source isn't `"meterplan"` -- a parsed EFL
    (PTC / REP discovery / Meter's own /plans page), a manual entry, or a report
    seed. Matching uses :func:`_plan_supersedes` (conservative token-subset).
    The `CURRENT_PLAN_ID` plan is never removed.

    Synthetic DRAFTS are swept too. The meterplan importer dedups against
    promoted plans by an exact (retailer, name, term) tuple, which misses the
    name variants the index uses -- "Chariot Energy Shine 36" against a promoted
    "Shine 36", Green Mountain's "Solar Max" against "Renewable Rewards Solar
    Max 12". Those synthetics are pure noise: an unverifiable third-party rate
    row for a plan whose real EFL is already in the database, and because they
    are flagged (the index doesn't publish free-hour windows or RTW formulas)
    they never promote and never leave the queue on their own.
    """
    try:
        current_plans = load_plans(plans_dir)
    except Exception:  # noqa: BLE001 -- defensive; degrade to "supersede nothing"
        return []
    meter_plans = [p for p in current_plans if str(getattr(p, "source", "")) == "meterplan"]
    auth_plans = [p for p in current_plans if str(getattr(p, "source", "")) != "meterplan"]
    removed: list[tuple] = []
    for mp_plan in meter_plans:
        if mp_plan.id == CURRENT_PLAN_ID:
            continue
        match = next((ap for ap in auth_plans if _plan_supersedes(mp_plan, ap)), None)
        if match is not None:
            (Path(plans_dir) / f"{mp_plan.id}.yaml").unlink(missing_ok=True)
            removed.append((mp_plan.id, match.id))
            continue
        # Only a real DRAFT covers it: don't delete (that would leave a hole in
        # the rankings with nothing promoted in its place), but flag it so the
        # synthetic can't sit in Compare looking as verified as a parsed EFL.
        draft_match = _covered_by_real(mp_plan, _real_draft_candidates(drafts_dir))
        if draft_match is not None and not mp_plan.needs_review:
            _flag_plan_needs_review(
                Path(plans_dir) / f"{mp_plan.id}.yaml",
                f"Third-party index row; a real EFL for this plan ({draft_match.id}) is "
                "awaiting review in plans/drafts/. Promote that draft to replace this.",
            )

    # Synthetic DRAFTS a promoted real plan already covers. Only promoted
    # coverage counts: if the only cover were another draft, dropping the
    # synthetic would leave nothing in the rankings for that plan.
    for path in sorted(Path(drafts_dir).glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            if str(raw.get("source") or "") != "meterplan":
                continue
            draft_plan = Plan.model_validate(raw)
        except Exception:  # noqa: BLE001 -- an unreadable draft is left alone
            continue
        if draft_plan.id == CURRENT_PLAN_ID:
            continue
        match = next((ap for ap in auth_plans if _plan_supersedes(draft_plan, ap)), None)
        if match is not None:
            path.unlink(missing_ok=True)
            removed.append((draft_plan.id, match.id))
    return removed


DISCOVERY_COVERAGE_FILE = "discovery_coverage.json"


def _write_discovery_coverage(snapshot_dir: Path, coverage: dict) -> None:
    """Persist {retailer: [plan names]} for REPs whose site we scraped in full.

    Written so :func:`prune_stale_meterplan_drafts` still works when
    `finish_refresh` is re-run on its own (the "Finish incomplete refresh"
    path), which has no discovery result in hand. Best-effort.
    """
    try:
        Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
        (Path(snapshot_dir) / DISCOVERY_COVERAGE_FILE).write_text(
            json.dumps(
                {"written_at": dt.datetime.now(dt.timezone.utc).isoformat(), "coverage": coverage},
                indent=2,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- never break a refresh over telemetry
        logger.info("Could not write discovery coverage: %r", exc)


def _read_discovery_coverage(snapshot_dir: Path = REP_DISCOVERY_DIR) -> dict:
    try:
        raw = json.loads((Path(snapshot_dir) / DISCOVERY_COVERAGE_FILE).read_text())
        return dict(raw.get("coverage") or {})
    except Exception:  # noqa: BLE001 -- absent/corrupt -> prune nothing
        return {}


def prune_stale_meterplan_drafts(
    drafts_dir: Path = DRAFTS_DIR, coverage: Optional[dict] = None
) -> list[tuple]:
    """Drop synthetic meterplan drafts for plans a fully-scraped REP doesn't sell.

    meterplan.com is a third-party index published by a competing REP, and its
    rows go stale: it lists plans the retailer no longer offers. When we have
    driven that retailer's own site to completion -- a LIVE render (not a stale
    manual capture) in which every EFL it offered downloaded -- and the plan is
    not among what the site returned, the row is either out of date or not
    something Doug could actually enroll in. Either way it is not worth a review,
    and it can never be verified: the index publishes no EFL.

    Deliberately narrow, because a REP's site legitimately shows different
    subsets through different funnels (Champion's website-only plans versus its
    PTC listing are precedent). So this fires ONLY where the scrape was live and
    complete, and only against that same retailer's rows -- never as a general
    "not seen lately" sweep. Returns ``(removed_draft_id, retailer)`` tuples.
    """
    coverage = _read_discovery_coverage() if coverage is None else coverage
    if not coverage:
        return []
    removed: list[tuple] = []
    for path in sorted(Path(drafts_dir).glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            if str(raw.get("source") or "") != "meterplan":
                continue
            draft = Plan.model_validate(raw)
        except Exception:  # noqa: BLE001 -- an unreadable draft is left alone
            continue
        if draft.id == CURRENT_PLAN_ID:
            continue
        r_draft = _significant_tokens(draft.retailer)
        for retailer, names in coverage.items():
            r_cov = _significant_tokens(retailer)
            if not r_draft or not r_cov:
                continue
            if not (r_draft <= r_cov or r_cov <= r_draft):
                continue
            # Same retailer. Is this plan among what its site returned? Compare
            # with the same conservative token rule supersede uses, so a naming
            # variant ("Truly Free Nights" vs "Reliant Truly Free Nights 12")
            # counts as a match rather than a deletion.
            offered = any(
                _plan_supersedes(
                    draft, _CoverageCandidate("site", retailer, name, draft.term_months)
                )
                for name in names
            )
            if not offered:
                path.unlink(missing_ok=True)
                removed.append((draft.id, retailer))
            break
    return removed


def _flag_plan_needs_review(path: Path, note: str) -> None:
    """Set needs_review on a saved plan and append a note. Best-effort."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        raw["needs_review"] = True
        existing = str(raw.get("notes") or "").strip()
        raw["notes"] = f"{existing} {note}".strip() if existing else note
        path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    except Exception as exc:  # noqa: BLE001 -- flagging must never break a refresh
        logger.info("Could not flag %s for review: %r", path.name, exc)


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


def draft_summary_rows(paths: list) -> tuple[list[dict], list[Path]]:
    """Summary rows for `paths`, skipping drafts that vanished mid-render.

    A refresh runs in a BACKGROUND THREAD (see refresh_state.RefreshRunner) and
    its first act is to clear the draft queue, so the page can list a draft and
    then have it deleted before the row is read -- which crashed the Plans page
    on the very first render after a refresh was kicked off. Returns the rows
    and the surviving paths together, because the caller zips the two to build
    its selector and they must stay aligned.
    """
    rows, kept = [], []
    for path in paths:
        try:
            rows.append(draft_summary_row(path))
        except (FileNotFoundError, OSError):
            continue  # promoted or cleared by a refresh since we listed it
        kept.append(path)
    return rows, kept


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


def known_efl_identities(efl_dir: Path = EFL_DIR, ptc_dir: Path = PTC_DIR) -> dict:
    """Map ``<efl filename> -> (retailer, plan_name)`` from the sources that
    already know it.

    An EFL never arrives anonymously: it was either downloaded from a REP site
    by discovery (which records retailer/plan in
    `data/efl/rep_discovery_manifest.jsonl`) or listed in the Power to Choose
    snapshot (whose row supplies both, and from which the saved filename is
    built by `fetchers.ptc._efl_filename`). The parser re-derives identity from
    the PDF *text* and falls back to "Unknown Retailer"/"Unnamed Plan" when the
    document's font is damaged -- which also makes several unrelated plans
    collide on one draft filename. This recovers what the download step knew.

    Discovery wins over PTC on a filename collision: it is the more specific
    source (a REP's own site) and PTC rows are the generic fallback.
    """
    out: dict[str, tuple] = {}

    # Power to Choose: rebuild the exact filename each row would have produced.
    try:
        from energyanalyzer.fetchers.ptc import _efl_filename, load_ptc

        snaps = sorted(Path(ptc_dir).glob("*.csv")) if Path(ptc_dir).exists() else []
        if snaps:
            df = load_ptc(max(snaps, key=lambda p: p.stat().st_mtime))
            for _, row in df.iterrows():
                retailer = str(row.get("retailer") or "").strip()
                plan = str(row.get("plan_name") or "").strip()
                if retailer or plan:
                    out[_efl_filename(row)] = (retailer, plan)
    except Exception as exc:  # noqa: BLE001 -- identity recovery is best-effort
        logger.info("Could not read PTC identities: %r", exc)

    # REP discovery manifest (more specific -- applied second so it wins).
    manifest = Path(efl_dir) / "rep_discovery_manifest.jsonl"
    if manifest.exists():
        for line in manifest.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            fname = Path(str(rec.get("file") or "")).name
            retailer = str(rec.get("retailer") or "").strip()
            plan = str(rec.get("plan_name") or "").strip()
            if fname and (retailer or plan):
                out[fname] = (retailer, plan)
    return out


def parse_downloaded_efls(
    pdf_paths: list[Path],
    drafts_dir: Path = DRAFTS_DIR,
    plans_dir: Path = PLANS_DIR,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    llm_assist: bool = False,
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

    With `llm_assist=True`, any draft the static parser left with a weak
    load-bearing field gets a second pass from the local LLM tier
    (`eflparse.llm_repair`), which PRE-FILLS those fields for the reviewer. It
    never promotes: such drafts still come out `needs_review=True`, badged in the
    Plans review UI with the model's reasoning. Best-effort throughout -- if
    Ollama is down the drafts are saved exactly as the parser produced them, and
    a per-draft failure never aborts the batch. Costs roughly 1s per weak draft
    (see `llm.OLLAMA_MODEL` for the benchmark).

    Returns `{'parsed': [plan_id, ...], 'skipped': [filename, ...],
    'failed': [{'file': filename, 'error': str}, ...],
    'llm_assisted': [{'id': plan_id, 'fields': [...]}, ...]}`.
    """
    from energyanalyzer.eflparse.parser import extract_text, parse_efl, save_draft, slugify

    drafts_dir = Path(drafts_dir)
    plans_dir = Path(plans_dir)
    total = len(pdf_paths)
    summary: dict = {"parsed": [], "skipped": [], "failed": [], "llm_assisted": [], "identified": []}
    identities = known_efl_identities(efl_dir=Path(pdf_paths[0]).parent if pdf_paths else EFL_DIR)

    # One availability probe for the whole batch rather than a per-file timeout.
    use_llm = bool(llm_assist) and _llm.available()
    if llm_assist and not use_llm:
        summary["llm_note"] = "Ollama unreachable -- drafts saved without LLM suggestions"

    for i, pdf_path in enumerate(pdf_paths, start=1):
        pdf_path = Path(pdf_path)
        try:
            draft = parse_efl(pdf_path)
            # An EFL is never anonymous -- discovery or PTC knew its retailer and
            # plan at download time. When the PDF's font is too damaged for the
            # parser to read the header it falls back to "Unknown Retailer" /
            # "Unnamed Plan", which is both useless in the UI and a collision:
            # several such plans differ only by contract term and would overwrite
            # each other's draft file. Restore the known identity and rebuild the id.
            known = identities.get(pdf_path.name)
            if known:
                retailer, plan_name = known
                changed_identity = False
                if retailer and draft.plan_dict.get("retailer") == "Unknown Retailer":
                    draft.plan_dict["retailer"] = retailer
                    changed_identity = True
                if plan_name and draft.plan_dict.get("name") == "Unnamed Plan":
                    draft.plan_dict["name"] = plan_name
                    changed_identity = True
                if changed_identity:
                    draft.plan_dict["id"] = slugify(
                        f"{draft.plan_dict['retailer']}_{draft.plan_dict['name']}"
                        f"_{draft.plan_dict.get('term_months')}mo"
                    )
                    summary["identified"].append(
                        {"file": pdf_path.name, "id": draft.plan_dict["id"]}
                    )
            plan_id = draft.plan_dict.get("id")
            already = (drafts_dir / f"{plan_id}.yaml").exists() or (plans_dir / f"{plan_id}.yaml").exists()
            if already:
                summary["skipped"].append(pdf_path.name)
            else:
                if use_llm and draft.plan_dict.get("needs_review"):
                    try:
                        from energyanalyzer.eflparse.llm_repair import llm_repair_draft

                        draft, report = llm_repair_draft(draft, extract_text(pdf_path))
                        if report.get("changed"):
                            summary["llm_assisted"].append(
                                {"id": plan_id, "fields": report["changed"]}
                            )
                    except Exception as exc:  # noqa: BLE001 -- LLM tier is optional
                        logger.info("LLM assist failed for %s: %r", pdf_path.name, exc)
                save_draft(draft, drafts_dir=drafts_dir)
                summary["parsed"].append(plan_id)
        except Exception as exc:  # noqa: BLE001 -- a bad PDF must not abort the batch
            summary["failed"].append({"file": pdf_path.name, "error": repr(exc)})
        if progress_callback is not None:
            progress_callback(i, total, pdf_path.name)

    return summary


QUARANTINE_AUTHORITY_FILE = "authority.json"


def _reset_quarantine(quarantine_dir: Path = QUARANTINE_DIR) -> dict:
    """Empty the quarantine and return its {plans, efl} subdirectories.

    One run's quarantine at a time: holding several would make "was this plan
    re-derived?" ambiguous, and the answer only matters for the run that just
    moved the files.
    """
    quarantine_dir = Path(quarantine_dir)
    if quarantine_dir.exists():
        shutil.rmtree(quarantine_dir, ignore_errors=True)
    dirs = {"plans": quarantine_dir / "plans", "efl": quarantine_dir / "efl"}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _write_refresh_authority(
    quarantine_dir: Path,
    ptc_ok: bool,
    meterplan_ok: bool,
    listings: list[tuple],
    coverage: Optional[dict] = None,
) -> None:
    """Record which sources spoke for the market this run, and what they listed.

    `reconcile_quarantine` runs inside `finish_refresh`, which is re-runnable on
    its own (the "Finish incomplete refresh" path) and has no summary in hand
    then -- so this has to survive on disk, like the discovery coverage file.
    """
    try:
        Path(quarantine_dir).mkdir(parents=True, exist_ok=True)
        (Path(quarantine_dir) / QUARANTINE_AUTHORITY_FILE).write_text(
            json.dumps(
                {
                    "written_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "ptc_ok": bool(ptc_ok),
                    "meterplan_ok": bool(meterplan_ok),
                    "coverage_retailers": sorted((coverage or {}).keys()),
                    "listings": [list(x) for x in listings],
                },
                indent=2,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- never break a refresh over telemetry
        logger.info("Could not write refresh authority: %r", exc)


def _read_refresh_authority(quarantine_dir: Path = QUARANTINE_DIR) -> dict:
    try:
        return json.loads((Path(quarantine_dir) / QUARANTINE_AUTHORITY_FILE).read_text())
    except Exception:  # noqa: BLE001 -- absent/corrupt -> claim no authority
        return {}


def _listing_covers(plan: Plan, retailer: str, name: str, term: Optional[int]) -> bool:
    """True if the market listing (retailer, name, term) still offers `plan`.

    `_plan_supersedes` is the strict rule, but it answers False for two
    different reasons: "different plan", and "this name carries no distinctive
    tokens to compare". Plenty of real plans are just brand + term -- "Gexa 12",
    "Frontier 24" -- and for those it can never return True. That is safe where
    it is used to *supersede*, but here a False means a plan gets flagged as
    gone from the market, so an inconclusive comparison must fall back to
    retailer + term rather than count as evidence of absence.
    """
    if _plan_supersedes(plan, _CoverageCandidate("listing", retailer, name, term)):
        return True
    r_plan = _significant_tokens(plan.retailer)
    r_list = _significant_tokens(retailer)
    if not r_plan or not r_list or not (r_plan <= r_list or r_list <= r_plan):
        return False
    if plan.term_months != term:
        return False
    # Same retailer and term. Only treat that as a match when the names were
    # too generic to compare -- otherwise "Gexa 12" would cover "Gexa 24".
    distinctive = _significant_tokens(plan.name, extra_drop=frozenset(r_plan | {"plan"}))
    return not distinctive


def reconcile_quarantine(
    plans_dir: Path = PLANS_DIR,
    efl_dir: Path = EFL_DIR,
    quarantine_dir: Path = QUARANTINE_DIR,
    drafts_dir: Path = DRAFTS_DIR,
) -> dict:
    """Restore whatever the run failed to re-derive, and flag what's truly gone.

    Three outcomes per quarantined plan:

    Precedence for a given id, best first -- confidence, then freshness:

      1. this run's parse, promoted (confident AND new)
      2. the previous copy, if it was verified (needs_review False)
      3. this run's parse, sitting in review as a draft
      4. the previous copy, if it too was unreviewed -- dropped

    So a verified reading is never displaced by an unverified one: it stays
    rankable while its replacement waits for review (`kept_pending_review`).
    An old copy that was itself unreviewed loses to the fresher draft, since
    keeping it would only preserve an older guess.

    * **re-derived** -- the id exists again, so the quarantine copy is dropped.
      The normal path. A draft counts as rebuilt: the run DID parse the plan, it
      just did not clear the confidence gate. Missing that produced two failures
      at once on 2026-07-26 -- a stale promoted copy restored alongside its own
      fresh draft (20 ids in both places), and five Just Energy plans flagged as
      gone from a PTC snapshot that still listed all five.
    * **still listed** -- no plan file, but a source that ran this run still
      advertises it. Its EFL simply didn't make it (a WAF block, a download
      failure, a funnel that drifted). Restored untouched: the plan is real and
      our copy is the last good one.
    * **delisted** -- no plan file, and a source with authority over it ran to
      completion without listing it. Restored and flagged for review rather than
      deleted, because a plan leaving the market is a fact worth seeing --
      especially if it is the one you are currently on.

    Authority is deliberately narrow, mirroring `prune_stale_meterplan_drafts`:
    absence only means something when the source that would have listed it
    actually completed. If PTC never downloaded, nothing is delisted, and a
    broken run costs nothing.
    """
    plans_dir, efl_dir = Path(plans_dir), Path(efl_dir)
    q = Path(quarantine_dir)
    result = {"restored": [], "delisted": [], "dropped": 0, "efls_restored": 0,
              "kept_pending_review": []}
    _restored_plans: list = []
    if not q.exists():
        return result

    authority = _read_refresh_authority(q)
    ptc_ok = bool(authority.get("ptc_ok"))
    meterplan_ok = bool(authority.get("meterplan_ok"))
    coverage_retailers = {str(r) for r in (authority.get("coverage_retailers") or [])}
    # (retailer, name, term). A term of None means the source published no term
    # -- discovery coverage is plan names only -- so it is filled in from the
    # plan under test, making the term a non-discriminator rather than an
    # automatic mismatch. Same conservative rule prune_stale_meterplan_drafts
    # uses: when comparing, err towards "still listed".
    listings = [(str(r), str(n), t) for r, n, t in (authority.get("listings") or [])]

    drafts_dir = Path(drafts_dir)
    for path in sorted((q / "plans").glob("*.yaml")) if (q / "plans").exists() else []:
        # A confident promotion this run always wins: it is both newer and
        # verified, and it already overwrote plans/<id>.
        if (plans_dir / path.name).exists():
            result["dropped"] += 1
            continue
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            plan = Plan.model_validate(raw)
        except Exception:  # noqa: BLE001 -- unreadable: restore it and move on
            shutil.move(str(path), str(plans_dir / path.name))
            result["restored"].append(path.stem)
            continue
        if (drafts_dir / path.name).exists():
            # The run rebuilt this plan but the parse landed in review. Rank
            # order between the two copies is by CONFIDENCE, then freshness:
            # a previously verified reading outranks an unverified new one, so
            # it stays rankable while its replacement waits for review. An old
            # copy that was itself unreviewed loses to the fresher draft and is
            # dropped -- keeping it would only preserve an older guess.
            if not plan.needs_review:
                shutil.move(str(path), str(plans_dir / path.name))
                _restored_plans.append(plan)
                result["kept_pending_review"].append(plan.id)
            else:
                result["dropped"] += 1
            continue
        _restored_plans.append(plan)

        still_listed = any(
            _listing_covers(plan, r, n, plan.term_months if t is None else t)
            for r, n, t in listings
        )
        src = str(plan.source or "")
        # Who could have testified that this plan is gone?
        if src == "meterplan":
            spoke = meterplan_ok
        else:
            spoke = ptc_ok or any(
                _significant_tokens(plan.retailer) <= _significant_tokens(r)
                or _significant_tokens(r) <= _significant_tokens(plan.retailer)
                for r in coverage_retailers
            )

        shutil.move(str(path), str(plans_dir / path.name))
        if not still_listed and spoke:
            _flag_plan_needs_review(
                plans_dir / path.name,
                f"Not listed by its source on {dt.date.today().isoformat()} -- the source was "
                "reached and did not offer this plan. It may have left the market.",
            )
            result["delisted"].append(plan.id)
        else:
            result["restored"].append(plan.id)

    # EFLs: restore only the documents the surviving plans were parsed from
    # (Plan.source is "efl:<filename>"). Restoring every leftover instead would
    # resurrect orphans permanently -- each is re-parsed into a draft, then
    # quarantined and restored again on the next run, so a PDF nothing points at
    # would generate a review item forever.
    wanted = {
        str(src)[4:]
        for src in (getattr(p, "source", "") for p in _restored_plans)
        if str(src).startswith("efl:")
    }
    for path in sorted((q / "efl").glob("*.pdf")) if (q / "efl").exists() else []:
        if path.name not in wanted or (efl_dir / path.name).exists():
            continue
        shutil.move(str(path), str(efl_dir / path.name))
        result["efls_restored"] += 1

    if result["restored"] or result["delisted"] or result["kept_pending_review"]:
        invalidate_plans_cache()
    return result


def manual_efl_paths(efl_dir: Path = EFL_DIR) -> list[Path]:
    """Hand-supplied EFL PDFs, which survive the refresh wipe.

    Drop a PDF in ``data/efl/manual/`` for any REP automation cannot reach and
    it will be parsed on every refresh like a downloaded one, without ever
    being deleted. This exists because the wipe destroys what it cannot refetch:
    Ambit's EFLs, saved by hand after its WAF began refusing every client, were
    deleted by the next refresh and left only their Zone.Identifier stubs
    behind.
    """
    manual = Path(efl_dir) / "manual"
    return sorted(manual.glob("*.pdf")) if manual.exists() else []


def efl_pdf_health(efl_dir: Path = EFL_DIR) -> dict:
    """Quick health check of the downloaded-EFL cache: how many ``.pdf`` files
    are on disk, and which of them aren't actually PDFs.

    A download that returned an HTML "not found"/SPA shell or a bot-challenge
    (captcha) page with HTTP 200 can land under a ``.pdf`` name; the parser then
    silently fails on it (it's not a PDF) and no plan is produced. A real PDF
    starts with the ``%PDF`` signature, so any file lacking it in its first 1 KB
    is flagged. (The downloaders now reject non-PDF responses up front, so this
    mainly surfaces files saved before that guard, or ones placed by hand.)

    Returns ``{'total': int, 'invalid': [filename, ...]}``.
    """
    efl_dir = Path(efl_dir)
    pdfs = sorted(efl_dir.glob("*.pdf")) if efl_dir.exists() else []
    invalid: list[str] = []
    for p in pdfs:
        try:
            head = p.read_bytes()[:1024]
        except OSError:
            invalid.append(p.name)
            continue
        if b"%PDF" not in head:
            invalid.append(p.name)
    return {"total": len(pdfs), "invalid": invalid}


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


def _newest_capture(snapshot_dir: Path, key: str) -> Optional[Path]:
    """Newest manually-saved rendered-HTML capture for a REP, or None.

    Captures are named `<key>_<UTC timestamp>.html`; picked by mtime so a
    sortable stamp isn't strictly required.
    """
    snaps = sorted(snapshot_dir.glob(f"{key}_*.html")) if snapshot_dir.exists() else []
    return max(snaps, key=lambda p: p.stat().st_mtime) if snaps else None


def _run_rep_discovery(
    zip_code: str,
    llm_assist: bool = False,
    efl_dir: Path = EFL_DIR,
    drafts_dir: Path = DRAFTS_DIR,
    plans_dir: Path = PLANS_DIR,
    reps: Optional[list[str]] = None,
    headless: bool = True,
    snapshot_dir: Path = REP_DISCOVERY_DIR,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ptc_df=None,
    max_workers: int = DISCOVERY_MAX_WORKERS,
) -> dict:
    """Discover plan EFLs on individual REP marketing sites that Power to Choose
    and meterplan.com miss (ARCHITECTURE.md §7), download the EFLs into
    `efl_dir`, and parse the newly downloaded PDFs into drafts (so
    `refresh_market_data`'s auto-promote step handles them like any other draft).

    All plans found on each REP site are pulled (not just solar buyback), so
    REP-exclusive/website-only plans PTC lacks are captured too. When `ptc_df`
    (the filtered PTC listing) is given, discovered plans PTC already carries are
    deduped out first (`_discovered_plan_in_ptc`, conservative -- see there) so
    we don't re-download/duplicate what PTC already has; the count is reported as
    `ptc_deduped`.

    Each REP is dispatched by how its `RepConfig` is wired
    (`fetchers.rep_discovery.REP_CONFIGS`):

    * **harvester** (e.g. Champion, EFL URL JS-computed) -> `harvest_live`.
    * **extractor + render** (most REPs) -> `fetch_rendered_html` (live
      Playwright) then `discover`.
    * **extractor, no render** (e.g. Ambit, whose WAF blocks Playwright) ->
      `discover` on the newest manually-captured `<key>_*.html` in
      `snapshot_dir`; reported as `manual-needed` if no capture is on disk.

    Every REP runs inside its own try/except so one site's failure (Playwright
    missing, robots.txt block, a WAF, a missing ESI-ID secret, a nav-flow drift)
    is recorded and the rest still run -- nothing here aborts the refresh.

    Up to `max_workers` REPs are queried concurrently (one site per worker, never
    two workers on one site); results are still reported in `reps` order.

    `reps` limits the run to those REP keys (default: all configured). PII some
    sites need to render (e.g. Octopus's ESI ID) is read by the fetcher from the
    gitignored `data/rep_discovery_secrets.yaml`, never passed in here.

    Returns `{'reps': {key: {'retailer', 'status', 'plans_found', 'buyback',
    'detail'}, ...}, 'ptc_deduped': int, 'downloaded': <download_discovered()
    summary>, 'parsed': <parse_downloaded_efls() summary>}` where `status` is one
    of `'ok'`, `'error'`, or `'manual-needed'`.
    """
    from energyanalyzer.fetchers import hostpool
    from energyanalyzer.fetchers import rep_discovery as rd

    efl_dir = Path(efl_dir)
    drafts_dir = Path(drafts_dir)
    plans_dir = Path(plans_dir)
    snapshot_dir = Path(snapshot_dir)

    def _report(stage: str, done: int, total: int, item: str) -> None:
        if progress_callback is not None:
            progress_callback(done, total, f"{stage}: {item}")

    result: dict = {
        "reps": {},
        "ptc_deduped": 0,
        "downloaded": {"downloaded": [], "skipped": [], "failed": [], "filtered_out": 0},
        "parsed": {"parsed": [], "skipped": [], "failed": []},
    }

    keys = list(reps) if reps is not None else list(rd.REP_CONFIGS.keys())

    def _discover_one(key: str) -> tuple[dict, list]:
        """Query one REP site. Returns its result row and the plans it yielded.

        Everything is caught and reported here rather than raised: one site's
        WAF, expired secret, or drifted nav flow must never cost the other
        twelve. Runs on its own thread (see the pool below), so it touches no
        shared state -- the caller stitches the rows back together in `keys`
        order afterwards.
        """
        config = rd.REP_CONFIGS.get(key)
        if config is None:
            return {
                "retailer": key,
                "status": "error",
                "plans_found": 0,
                "buyback": 0,
                "detail": "unknown REP key (not in REP_CONFIGS)",
            }, []
        label = config.retailer
        logger.info("=== Discovery: %s ===", label)
        try:
            if config.harvester is not None:
                plans = rd.harvest_live(
                    config,
                    zip_code,
                    headless=headless and not config.force_headful,
                    check_robots=config.check_robots,
                )
                detail = "harvested live"
            elif config.render is not None:
                try:
                    html, _snap = rd.fetch_rendered_html(
                        config,
                        zip_code,
                        # Tesla's Akamai edge 403s headless Chromium; its config
                        # opts into a real window (see RepConfig.force_headful).
                        headless=headless and not config.force_headful,
                        snapshot_dir=snapshot_dir,
                        check_robots=config.check_robots,
                    )
                    plans = rd.discover(html, config)
                    detail = "rendered live"
                except Exception as render_exc:  # noqa: BLE001
                    # A live render can fail for reasons that say nothing about
                    # the parser: a nav-flow drift, or a probabilistic WAF (Ambit
                    # answers ~half of requests with a plain-text 403). Falling
                    # back to the newest manual capture keeps a REP working on a
                    # bad night instead of silently dropping all its plans --
                    # important for the buyback-only REPs, whose plans PTC and
                    # meterplan both miss entirely.
                    newest = _newest_capture(snapshot_dir, key)
                    if newest is None:
                        raise
                    logger.info(
                        "%s: live render failed (%r) -- falling back to %s",
                        label, render_exc, newest.name,
                    )
                    html = newest.read_text(encoding="utf-8")
                    plans = rd.discover(html, config)
                    detail = f"live render failed; used capture {newest.name}"
            else:
                # No render(): WAF/manual-capture REP. Use the newest saved
                # capture; if there's none, tell the user to run one by hand.
                newest = _newest_capture(snapshot_dir, key)
                if newest is None:
                    return {
                        "retailer": label,
                        "status": "manual-needed",
                        "plans_found": 0,
                        "buyback": 0,
                        "detail": (
                            f"no saved capture in {snapshot_dir}/ -- this site blocks "
                            f"automation; save its rendered plans page as {key}_<ts>.html"
                        ),
                    }, []
                html = newest.read_text(encoding="utf-8")
                plans = rd.discover(html, config)
                detail = f"from manual capture {newest.name}"
        except Exception as exc:  # noqa: BLE001 -- one REP's failure mustn't abort the rest
            logger.info("%s: FAILED -- %r", label, exc)
            return {
                "retailer": label,
                "status": "error",
                "plans_found": 0,
                "buyback": 0,
                "detail": repr(exc),
            }, []
        found = len(plans)
        buyback = sum(1 for p in plans if p.is_buyback)
        # REPs whose EFL URLs aren't httpx-downloadable (Vistra PDFGenerator:
        # TXU/Ambit) keep only their buyback plans -- pulling every conventional
        # plan would just add un-downloadable EFLs for plans already on PTC.
        if not getattr(config, "broaden", True):
            plans = [p for p in plans if p.is_buyback]
        logger.info(
            "%s: %s -- %d plan(s) found, %d buyback, %d kept",
            label, detail, found, buyback, len(plans),
        )
        return {
            "retailer": label,
            # A scrape that ran cleanly but came back empty is NOT "ok": the
            # site was up and we still learned nothing. Both known causes are
            # invisible otherwise -- TXU served a maintenance page, and Direct
            # Energy's funnel dropped out mid-harvest (2026-07-26), the latter
            # costing two real plans that a refresh had already deleted. Empty
            # is already excluded from coverage so it can't authorise pruning;
            # this just stops it reading as success in the run report.
            "status": "ok" if found else "empty",
            "plans_found": found,
            "buyback": buyback,
            "detail": detail,
            # Every plan name the REP's own site offered, BEFORE the PTC dedup
            # below drops the ones PTC already lists -- a deduped plan is still
            # one the REP sells, so it must count as "seen" for coverage.
            "plan_names": [p.plan_name for p in plans],
            "live": not str(detail).startswith("from manual capture")
            and "used capture" not in str(detail),
        }, plans

    # Query several REPs at once. `host_of` is the REP key, so each site gets its
    # own single-threaded queue of exactly one job: thirteen different hosts run
    # concurrently, but no host is ever touched twice at the same moment. This is
    # the run's longest phase -- 7m56s of the 15m54s on 2026-07-26 -- and almost
    # all of it is a browser waiting on someone else's JavaScript.
    outcomes = hostpool.run_per_host(
        keys,
        host_of=lambda key: key,
        work=_discover_one,
        max_workers=max_workers,
        # Reported as each site FINISHES -- with several in flight there is no
        # single "currently querying" REP to name, and completions still count
        # up cleanly for the progress bar.
        on_done=lambda done, tot, key: _report(
            "discovery",
            done,
            tot,
            f"{rd.REP_CONFIGS[key].retailer if key in rd.REP_CONFIGS else key} (done)",
        ),
    )
    all_plans: list = []
    for outcome in outcomes:  # in `keys` order, whichever site finished first
        if not outcome.ok:  # _discover_one catches its own; only a bug lands here
            result["reps"][outcome.item] = {
                "retailer": outcome.item,
                "status": "error",
                "plans_found": 0,
                "buyback": 0,
                "detail": repr(outcome.error),
            }
            continue
        rep_row, plans = outcome.value
        result["reps"][outcome.item] = rep_row
        all_plans.extend(plans)

    if not all_plans:
        return result

    # Drop discovered plans PTC already carries (conservative -- only confident
    # duplicates), so we don't re-download/duplicate what PTC has.
    ptc_index = _build_ptc_identity_index(ptc_df)
    if ptc_index:
        kept = [p for p in all_plans if not _discovered_plan_in_ptc(p.retailer, p.plan_name, ptc_index)]
        result["ptc_deduped"] = len(all_plans) - len(kept)
        all_plans = kept

    if not all_plans:
        return result

    # Download ALL discovered EFLs (not just buyback) into efl_dir (writes its
    # own rep_discovery manifest so downstream can tell these from PTC/meterplan).
    result["downloaded"] = rd.download_discovered(
        all_plans,
        dest=efl_dir,
        headless=headless,
        buyback_only=False,
        progress_callback=lambda d, t, n: _report("discovery-download", d, t, n),
    )

    # Per-REP coverage: a retailer counts as fully scraped only when its render
    # was LIVE (not a stale manual capture) and every EFL it offered is in hand.
    # Attribution is by URL, since download failures are reported globally.
    dl = result["downloaded"]
    failed_urls = {str(f.get("url") or "") for f in dl.get("failed", [])}
    incomplete = {p.retailer for p in all_plans if p.efl_url in failed_urls}
    result["coverage"] = {
        rep["retailer"]: rep["plan_names"]
        for rep in result["reps"].values()
        if rep.get("status") == "ok"
        and rep.get("live")
        and rep.get("plan_names")
        and rep["retailer"] not in incomplete
    }
    _write_discovery_coverage(snapshot_dir, result["coverage"])

    # Parse ONLY the newly discovered PDFs (downloaded now, or already on disk
    # from a prior run) into drafts -- the caller's stage-5 EFL parse already ran
    # before this, so this avoids re-parsing the whole efl_dir.
    discovered_pdfs = sorted({Path(p) for p in (dl["downloaded"] + dl["skipped"])})
    if discovered_pdfs:
        result["parsed"] = parse_downloaded_efls(
            discovered_pdfs,
            drafts_dir=drafts_dir,
            plans_dir=plans_dir,
            llm_assist=llm_assist,
            progress_callback=lambda d, t, n: _report("discovery-parse", d, t, n),
        )
    return result


def refresh_market_data(
    plans_dir: Path = PLANS_DIR,
    drafts_dir: Path = DRAFTS_DIR,
    efl_dir: Path = EFL_DIR,
    ptc_dir: Path = PTC_DIR,
    meterplan_dir: Path = METERPLAN_DIR,
    quarantine_dir: Path = QUARANTINE_DIR,
    tdu: str = "ONCOR",
    language: Optional[str] = "English",
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    fetch: bool = True,
    run_discovery: bool = False,
    discovery_zip: str = "78665",
    discovery_headless: bool = True,
    discovery_reps: Optional[list[str]] = None,
    llm_assist: bool = False,
) -> dict:
    """"Refresh market data" one-button pipeline (ARCHITECTURE.md §9).

    Orchestrates, in order:

    1. **Quarantine** stale auto-imported data into `quarantine_dir`: plans in
       `plans_dir` whose `source` starts with `"ptc"`, `"efl:"`, or is exactly
       `"meterplan"` (never `"manual"`, never a `"report-*"` seed, never
       `CURRENT_PLAN_ID`), and every PDF in `efl_dir` (except
       `efl_dir/manual/`, which is never touched). Clearing these is what
       forces fresh documents -- `download_efls` skips anything already on
       disk -- but they are MOVED, not deleted, so `reconcile_quarantine` in
       step 8 can restore whatever the run failed to rebuild. Drafts in
       `drafts_dir` are still deleted outright (they are regenerated every
       run), as is every PTC snapshot in `ptc_dir` except the newest (kept as
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
    8. **Supersede** synthetic meterplan plans (`supersede_meterplan_plans`):
       remove any `source="meterplan"` plan now covered by a real/authoritative
       plan (parsed EFL from PTC/discovery/Meter's own /plans page, or a manual
       entry) for the same underlying plan. Only meterplan-source plans are
       removed; each removal is recorded in `meterplan_superseded` + `notes`.

    Just before step 6, a **Meter-EFL** stage (`fetch_meterplan_efls`) pulls
    Meter Energy's own real EFLs from its /plans page and parses them, so Meter's
    own plans come from real EFLs (and its synthetic markdown rows are excluded
    in step 6). Between meterplan (6) and auto-promote (7), if ``run_discovery``
    is set, an
    optional **REP-site discovery** stage (`_run_rep_discovery`, ARCHITECTURE.md
    §7) queries individual retailer marketing sites for solar-buyback EFLs that
    Power to Choose and meterplan.com miss, downloads them into `efl_dir`, and
    parses them into drafts the auto-promote step then handles. It's off by
    default because each REP is a live browser session (the sweep is slow), uses
    `discovery_zip` for the ZIP gates (`discovery_reps` optionally limits which
    REPs run; `discovery_headless` toggles the browser), and is fully
    self-tolerant -- a per-REP failure (Playwright missing, a WAF, a missing
    ESI-ID secret) is recorded, not raised.

    `progress_callback`, if given, is called after every item in every stage
    as `progress_callback(stage_done, stage_total, "<stage>: <item>")` so a
    caller can drive one progress bar + status line across the whole run.

    Returns a summary dict: `{'deleted_plans': [id, ...], 'deleted_drafts':
    int, 'deleted_efls': int, 'deleted_snapshots': int, 'fetched': bool,
    'snapshot_path': str | None, 'downloaded': <download_efls() summary>,
    'parsed': <parse_downloaded_efls() summary>, 'meterplan': {'fetched':
    bool, 'snapshot_path': str | None, 'imported': [id, ...],
    'skipped_battery': int, 'skipped_existing': int, 'flagged_for_review':
    int}, 'discovery': {'enabled': bool, 'reps': {rep_key: {'retailer': str,
    'status': 'ok'|'error'|'manual-needed', 'plans_found': int, 'buyback': int,
    'detail': str}, ...}, 'downloaded': <download_discovered() summary>,
    'parsed': <parse_downloaded_efls() summary>}, 'promoted': [plan_id, ...],
    'needing_review': [draft_stem, ...], 'notes': [str, ...]}`.
    """
    from energyanalyzer.fetchers.meterplan import (
        fetch_meterplan,
        fetch_meterplan_efls,
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
    quarantine_dir = Path(quarantine_dir)

    notes: list[str] = []
    summary: dict = {
        "deleted_plans": [],
        "deleted_drafts": 0,
        "deleted_efls": 0,
        "deleted_snapshots": 0,
        "fetched": False,
        "snapshot_path": None,
        "downloaded": {"downloaded": [], "skipped": [], "failed": [], "deferred": []},
        "parsed": {"parsed": [], "skipped": [], "failed": []},
        "meterplan": {
            "fetched": False,
            "snapshot_path": None,
            "imported": [],
            "skipped_battery": 0,
            "skipped_existing": 0,
            "skipped_own": 0,
            "flagged_for_review": 0,
        },
        "meterplan_efl": {
            "fetched": False,
            "offers": 0,
            "downloaded": {"downloaded": [], "skipped": [], "failed": []},
            "parsed": {"parsed": [], "skipped": [], "failed": []},
        },
        "discovery": {
            "enabled": run_discovery,
            "reps": {},
            "downloaded": {"downloaded": [], "skipped": [], "failed": [], "filtered_out": 0},
            "parsed": {"parsed": [], "skipped": [], "failed": []},
        },
        "promoted": [],
        "needing_review": [],
        "meterplan_superseded": [],
        "notes": notes,
    }

    def _report(stage: str, done: int, total: int, item: str) -> None:
        if progress_callback is not None:
            progress_callback(done, total, f"{stage}: {item}")

    # --- 1. quarantine stale auto-imported data ---------------------------#
    # Plans and EFLs are MOVED aside, not deleted. Clearing them is what forces
    # fresh documents (download_efls skips anything already on disk), but doing
    # it destructively means any fetcher that breaks mid-run silently costs data
    # that nothing can rebuild -- on 2026-07-26 that was Direct Energy's two
    # solar plans and Ambit's hand-saved EFLs. Whatever the run fails to
    # re-derive is restored from here by reconcile_quarantine().
    quarantine = _reset_quarantine(quarantine_dir)
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
        shutil.move(str(p), str(quarantine["plans"] / p.name))
        summary["deleted_plans"].append(p.stem)
        delete_done += 1
        _report("delete", delete_done, delete_total, f"plan {p.name}")
    for p in draft_paths:
        # Drafts alone are still deleted outright: they are the review queue,
        # regenerated from EFLs every run, and a draft nothing re-derived is
        # noise rather than a loss.
        p.unlink(missing_ok=True)
        summary["deleted_drafts"] += 1
        delete_done += 1
        _report("delete", delete_done, delete_total, f"draft {p.name}")
    for p in efl_paths:
        shutil.move(str(p), str(quarantine["efl"] / p.name))
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

    ptc_df_for_dedup = None  # filtered PTC listing, reused by discovery dedup
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
        ptc_df_for_dedup = df

        # --- 4. download EFLs -------------------------------------------#
        summary["downloaded"] = download_efls(
            df, dest=efl_dir, progress_callback=lambda d, t, n: _report("download", d, t, n)
        )

        # --- 5. parse downloaded EFLs into drafts ------------------------#
        pdf_paths = sorted(efl_dir.glob("*.pdf")) if efl_dir.exists() else []
        pdf_paths += manual_efl_paths(efl_dir)
        summary["parsed"] = parse_downloaded_efls(
            pdf_paths,
            drafts_dir=drafts_dir,
            plans_dir=plans_dir,
            llm_assist=llm_assist,
            progress_callback=lambda d, t, n: _report("parse", d, t, n),
        )

    # --- 6a. Meter Energy's own real EFLs (from the /plans HTML page) -------#
    # The markdown index (step 6) omits document URLs, but Meter's /plans page
    # embeds the *real* EFL PDFs (presigned, ~7-day). Fetch + parse those so
    # Meter's own plans come from real EFLs, not the synthetic markdown rows.
    # When it succeeds we exclude Meter's markdown rows below (step 6) so the
    # two don't duplicate. Skipped when fetch=False (there's no on-disk fallback
    # for the HTML page); fully tolerant -- failure is noted, never raised.
    meter_efl_ok = False
    if fetch:
        _report("meter-efl", 0, 1, "fetching Meter Energy EFLs")
        try:
            me_dl = fetch_meterplan_efls(
                zip_code=discovery_zip,
                dest=efl_dir,
                tdu=tdu,
                progress_callback=lambda d, t, n: _report("meter-efl", d, t, n),
            )
            summary["meterplan_efl"]["fetched"] = True
            summary["meterplan_efl"]["offers"] = me_dl.get("offers", 0)
            summary["meterplan_efl"]["downloaded"] = me_dl
            meter_efl_ok = bool(me_dl.get("downloaded") or me_dl.get("skipped"))
            me_pdfs = [Path(p) for p in me_dl.get("downloaded", [])]
            if me_pdfs:
                summary["meterplan_efl"]["parsed"] = parse_downloaded_efls(
                    me_pdfs,
                    drafts_dir=drafts_dir,
                    plans_dir=plans_dir,
                    llm_assist=llm_assist,
                    progress_callback=lambda d, t, n: _report("meter-efl-parse", d, t, n),
                )
        except Exception as exc:  # noqa: BLE001 -- Meter EFL fetch must never abort the run
            notes.append(f"Meter Energy /plans EFL fetch failed: {exc!r}")
            _report("meter-efl", 1, 1, "meter EFL fetch failed")
    else:
        notes.append(
            "fetch=False -- skipped Meter Energy /plans EFL fetch (no on-disk fallback for it)."
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

            # When we fetched Meter's own real EFLs above, drop Meter Energy's
            # synthetic markdown rows so the two don't duplicate the same plans.
            skip_own = {"Meter Energy"} if meter_efl_ok else None
            mp_summary = meterplan_to_drafts(
                mp_df, drafts_dir, existing_plan_keys, skip_retailers=skip_own
            )
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

    # --- 6.5 REP-site solar buyback discovery (optional; slow) --------------#
    # Independent of the PTC/meterplan stages: queries individual REP marketing
    # sites (Playwright/manual capture) for solar-buyback EFLs the two aggregate
    # sources miss, downloads them into efl_dir, and parses them into drafts that
    # the auto-promote step below then handles. Off by default (each REP is a
    # live browser session, so the whole sweep is slow) and fully self-tolerant:
    # a per-REP failure is recorded in the summary, and the whole stage is
    # wrapped so it can never abort the refresh.
    if run_discovery:
        _report("discovery", 0, 1, "starting REP-site solar buyback discovery")
        try:
            summary["discovery"].update(
                _run_rep_discovery(
                    zip_code=discovery_zip,
                    llm_assist=llm_assist,
                    efl_dir=efl_dir,
                    drafts_dir=drafts_dir,
                    plans_dir=plans_dir,
                    reps=discovery_reps,
                    headless=discovery_headless,
                    progress_callback=progress_callback,
                    ptc_df=ptc_df_for_dedup,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- discovery must never abort the refresh
            notes.append(f"REP discovery stage failed: {exc!r}")
            _report("discovery", 1, 1, "discovery stage failed")

    # Record what the market actually offered this run, so reconcile_quarantine
    # can tell "we failed to fetch it" from "nobody sells it any more". Built
    # from the sources that completed: the PTC index, plus every plan name a
    # fully-scraped REP returned.
    _listings: list[tuple] = []
    if ptc_df_for_dedup is not None:
        for _, _row in ptc_df_for_dedup.iterrows():
            _term = _row.get("term_months")
            try:
                _term = int(_term) if pd.notna(_term) else None
            except (TypeError, ValueError):
                _term = None
            _listings.append((str(_row.get("retailer") or ""), str(_row.get("plan_name") or ""), _term))
    _coverage = (summary.get("discovery") or {}).get("coverage") or {}
    for _retailer, _names in _coverage.items():
        _listings.extend((str(_retailer), str(_n), None) for _n in _names)
    _write_refresh_authority(
        quarantine_dir,
        ptc_ok=bool(summary.get("fetched")),
        meterplan_ok=bool((summary.get("meterplan") or {}).get("fetched")),
        listings=_listings,
        coverage=_coverage,
    )

    # --- 7 + 8. auto-promote confident drafts, then supersede ---------------#
    finish_refresh(
        plans_dir=plans_dir,
        drafts_dir=drafts_dir,
        summary=summary,
        notes=notes,
        progress_callback=progress_callback,
    )

    return summary


def promote_all_drafts(
    plans_dir: Path = PLANS_DIR,
    drafts_dir: Path = DRAFTS_DIR,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Promote EVERY draft into the plan database, confidence gate bypassed,
    keeping each draft's `needs_review` flag as-is.

    This is the "quick look" path: the auto-promote gate deliberately holds back
    anything the parser wasn't sure about, but sometimes you want the whole
    market in Compare's rankings *now* and will sort out the details after. The
    plans land flagged, so `needs_review` badges, the Compare "Stale?"/review
    columns, and `stale_plan_ids` all still mark them as unverified -- nothing
    here silently launders an uncertain parse into a trusted one.

    Move semantics, matching single-draft promote: a promoted draft's file is
    deleted. That discards its `_parse` block (per-field confidence + evidence),
    which is review metadata and not part of the Plan schema -- re-parsing the
    source EFL regenerates it. Drafts that fail schema validation are LEFT in
    place and reported in `failed`, so a bad one is never silently dropped.

    Returns `{'promoted': [id, ...], 'failed': [{'draft': name, 'error': str}],
    'flagged': int}` where `flagged` counts promoted plans still needing review.
    """
    from energyanalyzer.eflparse.parser import plan_fields

    plans_dir, drafts_dir = Path(plans_dir), Path(drafts_dir)
    summary: dict = {"promoted": [], "failed": [], "flagged": 0}
    draft_paths = sorted(drafts_dir.glob("*.yaml")) if drafts_dir.exists() else []

    for i, draft_path in enumerate(draft_paths, start=1):
        try:
            raw = load_draft_raw(draft_path)
            plan_dict = plan_fields(raw)
            plan_dict.setdefault("source", "ptc")
            plan_dict["retrieved"] = dt.date.today()
            # Preserve the parser's verdict rather than forcing it: a draft the
            # parser was confident about stays unflagged.
            plan_dict["needs_review"] = bool(raw.get("needs_review", True))
            plan = Plan.model_validate(plan_dict)
            save_plan(plan, directory=plans_dir)
            draft_path.unlink(missing_ok=True)
            summary["promoted"].append(plan.id)
            if plan.needs_review:
                summary["flagged"] += 1
        except Exception as exc:  # noqa: BLE001 -- one bad draft mustn't abort the batch
            summary["failed"].append({"draft": draft_path.name, "error": repr(exc)[:200]})
        if progress_callback is not None:
            progress_callback(i, len(draft_paths), draft_path.name)

    if summary["promoted"]:
        invalidate_plans_cache()
    return summary


def finish_refresh(
    plans_dir: Path = PLANS_DIR,
    drafts_dir: Path = DRAFTS_DIR,
    summary: Optional[dict] = None,
    notes: Optional[list] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Steps 7 + 8 of the refresh: auto-promote confident drafts, then supersede
    synthetic meterplan plans a real EFL now covers.

    Split out of :func:`refresh_market_data` and safe to call on its own, because
    it is purely local -- it reads `drafts_dir`, writes `plans_dir`, and touches
    no network. That matters for recovery: the earlier stages (fetch, download,
    parse, discovery) are slow and can be interrupted, and when they are, the
    drafts they produced are already on disk while the database is still empty.
    Re-running this finishes the job without repeating the sweep. The Plans page
    exposes it as "Finish incomplete refresh".

    Idempotent: a draft that doesn't clear the confidence gate is left in place,
    and one that does is promoted and its draft deleted, so a second call is a
    no-op. Pass `summary`/`notes` to append into an in-flight refresh's result;
    omit them to get a fresh summary dict back.
    """
    from energyanalyzer.eflparse.parser import LOAD_BEARING_KEYS, plan_fields

    if summary is None:
        summary = {"promoted": [], "needing_review": [], "meterplan_superseded": []}
    for key in ("promoted", "needing_review", "meterplan_superseded"):
        summary.setdefault(key, [])
    if notes is None:
        notes = summary.setdefault("notes", [])

    def _report(stage: str, done: int, total: int, item: str) -> None:
        if progress_callback is not None:
            progress_callback(done, total, f"{stage}: {item}")

    # A synthetic meterplan row must never be promoted over a REAL plan for the
    # same plan -- not even one still sitting in review. Without this, an
    # unverified third-party rate can auto-promote into the rankings (simple
    # meterplan rows come out needs_review=False) while the authoritative EFL
    # waits in the draft queue. Supersede (below) only cleans that up once the
    # real plan is *promoted*, which may never happen.
    try:
        _real_coverage = [p for p in load_plans(plans_dir) if str(p.source) != "meterplan"]
    except Exception:  # noqa: BLE001 -- degrade to "no coverage known"
        _real_coverage = []
    _real_coverage += _real_draft_candidates(drafts_dir)

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
            if eligible and str(raw.get("source") or "") == "meterplan":
                synthetic = _CoverageCandidate(
                    id=str(raw.get("id") or draft_path.stem),
                    retailer=str(raw.get("retailer") or ""),
                    name=str(raw.get("name") or ""),
                    term_months=raw.get("term_months"),
                )
                covered = _covered_by_real(synthetic, _real_coverage)
                if covered is not None:
                    eligible = False
                    summary.setdefault("meterplan_not_promoted", []).append(
                        {"synthetic": synthetic.id, "covered_by": getattr(covered, "id", "?")}
                    )
                    notes.append(
                        f"Did not promote synthetic meterplan plan {synthetic.id}: real plan "
                        f"{getattr(covered, 'id', '?')} covers it (left as a draft)."
                    )
            if eligible:
                plan_dict = plan_fields(raw)
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

    # meterplan.com rows carry no EFL PDF (source="meterplan", efl_url=None). If
    # we now have a real/authoritative plan for the same underlying plan -- from
    # a parsed EFL (PTC, REP discovery, or Meter's own /plans page) or a manual
    # entry -- the synthetic row is redundant and is removed (logged).
    for mp_id, match_id in supersede_meterplan_plans(plans_dir, drafts_dir):
        summary["meterplan_superseded"].append(mp_id)
        notes.append(f"Superseded synthetic meterplan plan {mp_id} with real plan {match_id}.")
    # Rows for plans a fully-scraped REP doesn't actually sell (stale index).
    for draft_id, retailer in prune_stale_meterplan_drafts(drafts_dir):
        summary.setdefault("meterplan_pruned", []).append(draft_id)
        notes.append(
            f"Dropped synthetic meterplan draft {draft_id}: {retailer}'s own site was "
            "scraped in full and does not offer this plan."
        )

    # Last: restore anything this run quarantined but never rebuilt. Runs after
    # promotion so "was it re-derived?" is asked of the finished database, and
    # inside finish_refresh so the "Finish incomplete refresh" recovery path
    # un-quarantines too -- an interrupted run must not strand the old plans.
    reconciled = reconcile_quarantine(plans_dir=plans_dir, drafts_dir=drafts_dir)
    if reconciled["restored"] or reconciled["delisted"] or reconciled["efls_restored"]:
        summary["quarantine"] = reconciled
    for plan_id in reconciled.get("kept_pending_review", []):
        notes.append(
            f"Kept verified plan {plan_id} in the ranking: this refresh re-parsed it, but the "
            "new reading needs review. The previous verified copy stands until you accept it."
        )
    for plan_id in reconciled["restored"]:
        notes.append(
            f"Kept existing plan {plan_id}: this refresh did not rebuild it, but the "
            "source still lists it."
        )
    for plan_id in reconciled["delisted"]:
        notes.append(
            f"Flagged {plan_id} for review: its source was reached and no longer lists it."
        )
    if reconciled["efls_restored"]:
        notes.append(
            f"Restored {reconciled['efls_restored']} EFL PDF(s) this refresh failed to "
            "re-download."
        )

    if summary["meterplan_superseded"]:
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
