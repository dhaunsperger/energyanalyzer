"""LLM-assisted repair of low-confidence EFL drafts.

The deterministic parser (``eflparse.parser``) reads most Texas EFLs cleanly, but
some -- unusual table layouts, free-hours plans whose window isn't spelled out,
garbled fonts -- come out ``needs_review`` with low-confidence load-bearing
fields. Rather than leave every one of those for manual entry, we take a second
pass with a local LLM (``energyanalyzer.llm``): feed it the EFL's extracted text
and ask it to report the load-bearing fields as JSON, then fill ONLY the fields
the deterministic parser was unsure about.

Safety is deterministic-first and conservative:

* The LLM never *overrides* a field the static parser was already confident about
  (>= ``keep_threshold``); it only fills the weak/missing ones.
* Whatever it proposes is written into the draft and the whole plan is
  re-validated against the ``Plan`` schema -- if the merge doesn't validate, the
  LLM changes are discarded and the draft is returned untouched.
* LLM-sourced confidences are capped (``cap_confidence``) so a plan only clears
  review on the LLM's say-so when it's genuinely confident, and every touched
  field is recorded in the draft's notes + the returned report.
* If Ollama is unreachable or returns junk, the draft is returned unchanged.

This module is import-safe without Ollama (all LLM I/O goes through
``llm.chat_json``, which returns ``None`` on any failure) and fully mockable via
``chat_fn``.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from energyanalyzer import llm
from energyanalyzer.eflparse.parser import LOAD_BEARING_KEYS, DraftPlan

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You read a Texas residential Electricity Facts Label (EFL) and report its rate "
    "structure as strict JSON matching the schema below. Report ONLY what the EFL "
    "clearly states; use null when it does not state a value. Do not guess.\n\n"
    "SCHEMA (this mirrors the plan file the app stores):\n"
    "- base_charge_usd: number -- the fixed monthly base/minimum-usage charge in "
    "dollars (0 if the EFL says there is none). Do NOT include TDU delivery charges.\n"
    "- energy_rates: an ORDERED list of rate rules, first match wins:\n"
    '    {"rate_ckwh": number, "label": string, "window": {"hours": [int], '
    '"weekdays": [int], "months": [int]} | null}\n'
    "    * rate_ckwh is the RETAILER energy charge in cents per kWh (not the "
    "'average price per kWh', which bundles TDU charges -- ignore that table).\n"
    "    * window.hours are local clock hours 0-23. window.weekdays are "
    "0=Monday,1=Tue,2=Wed,3=Thu,4=Friday,5=Saturday,6=Sunday. window.months are 1-12. "
    "An omitted or empty list means 'any' for that dimension.\n"
    "    * Use a window for time-limited rates (free nights, free weekends, "
    "time-of-use). Example: 'free Friday through Sunday' is "
    '{"weekdays": [4, 5, 6]}; \'free 9pm-6am\' is {"hours": [21,22,23,0,1,2,3,4,5]}.\n'
    "    * The LAST entry MUST be the catch-all default with window null, and its "
    "rate_ckwh must be the normal (non-free) rate -- never 0 unless the EFL truly "
    "charges nothing at all times.\n"
    "    * A simple fixed-rate plan is a single entry with window null.\n"
    '- buyback: the solar export/buyback credit: {"kind": "none"|"fixed"|"rtw", '
    '"rate_ckwh": number|null, "offset_scope": "all_charges"|"energy_only"}. '
    "'fixed' = a set cents/kWh credit (put it in rate_ckwh); 'rtw' = a real-time / "
    "wholesale / market-indexed credit; 'none' = no export credit. offset_scope is "
    "'energy_only' when the EFL says the credit offsets energy charges only or is "
    "not offsettable against base/TDU charges, else 'all_charges'.\n"
    '- reasoning: one short sentence quoting the EFL line(s) you used.\n\n'
    'Respond with exactly: {"base_charge_usd": number|null, "energy_rates": [...]|null, '
    '"buyback": {...}|null, "reasoning": string}'
)


def _verify_number_in_text(value: float, text: str) -> bool:
    """True if `value` literally appears in the EFL text.

    A small local model cannot self-assess calibrated confidence (lfm2.5-thinking
    returns 0 for every field), so we do NOT trust its ``confidence`` block.
    Instead the LLM *proposes* and this deterministically *verifies*: a proposed
    rate/charge is only accepted if the number is actually present in the source
    document, in one of the formats EFLs use (22.9, 22.90, 22.9000).
    """
    if value is None:
        return False
    haystack = (text or "").replace(",", "")
    for fmt in ("%g", "%.1f", "%.2f", "%.3f", "%.4f"):
        if (fmt % value) in haystack:
            return True
    return False


def _has_windowed_rates(plan_dict: dict) -> bool:
    """True if the static parser already produced a structured (multi-rate or
    windowed) energy schedule -- richer than a single flat rate, so an LLM's flat
    number must never replace it."""
    rates = plan_dict.get("energy_rates") or []
    return len(rates) > 1 or any((r or {}).get("window") for r in rates)


def _weak(draft: DraftPlan, key: str, keep_threshold: float) -> bool:
    """A load-bearing field is 'weak' (eligible for LLM fill) when the parser
    scored it but below keep_threshold. A field the parser never scored (e.g.
    free_window on a flat-rate plan) is NOT weak -- it's simply not applicable,
    and the auto-promote gate ignores it too, so we don't invent one."""
    return key in draft.confidence and draft.confidence[key] < keep_threshold


def _build_energy_rates(proposed: object) -> Optional[list]:
    """Normalise the LLM's ``energy_rates`` list into the Plan shape, or None if
    unusable. Never raises -- a malformed value degrades to 'no change'.

    Accepts the schema the prompt asks for: an ordered list of
    ``{rate_ckwh, label, window:{hours,weekdays,months}|null}`` where the last
    entry is the catch-all (window null).
    """
    if not isinstance(proposed, list) or not proposed:
        return None
    out: list[dict] = []
    try:
        for entry in proposed:
            if not isinstance(entry, dict):
                return None
            rate = entry.get("rate_ckwh")
            if rate is None:
                return None
            rule: dict = {"rate_ckwh": float(rate)}
            label = entry.get("label")
            if label:
                rule["label"] = str(label)
            win = entry.get("window")
            if isinstance(win, dict):
                w: dict = {}
                for key, lo, hi in (("hours", 0, 23), ("weekdays", 0, 6), ("months", 1, 12)):
                    vals = win.get(key)
                    if vals:
                        ints = [int(v) for v in vals]
                        if any(v < lo or v > hi for v in ints):
                            return None
                        w[key] = ints
                rule["window"] = w or None
            else:
                rule["window"] = None
            out.append(rule)
    except (TypeError, ValueError):
        return None
    return out or None


def _catch_all_rate_sane(rates: list) -> bool:
    """The trailing catch-all rate (window=None) must be a real, positive rate.

    Observed failure: lfm2.5-thinking read a Free-Weekends EFL and proposed a
    0.0 c/kWh catch-all -- i.e. free electricity around the clock. The number
    genuinely appears in the EFL (it's the *weekend* rate), so text-verification
    alone accepts it; this rejects the nonsensical shape instead.
    """
    if not rates:
        return False
    last = rates[-1] or {}
    if last.get("window") is not None:
        return False  # no catch-all default at all
    rate = last.get("rate_ckwh")
    return isinstance(rate, (int, float)) and rate > 0


def _apply_buyback(bb: dict) -> Optional[dict]:
    """Map the LLM 'buyback' object to a Plan buyback dict, or None if unusable
    (never raises)."""
    try:
        kind = str(bb.get("kind") or "").lower()
        scope = (
            bb.get("offset_scope")
            if bb.get("offset_scope") in ("all_charges", "energy_only")
            else "all_charges"
        )
        if kind == "none":
            return {"kind": "none", "offset_scope": scope}
        if kind == "fixed" and bb.get("rate_ckwh") is not None:
            return {"kind": "fixed", "rate_ckwh": float(bb["rate_ckwh"]), "offset_scope": scope}
        if kind == "rtw":
            return {"kind": "rtw", "rtw": {"multiplier": 1.0, "adder_ckwh": 0.0}, "offset_scope": scope}
    except (TypeError, ValueError):
        return None
    return None


def llm_repair_draft(
    draft: DraftPlan,
    text: str,
    *,
    keep_threshold: float = 0.8,
    min_confidence: float = 0.7,
    cap_confidence: float = 0.85,
    verified_confidence: float = 0.8,
    chat_fn: Optional[Callable[[list[dict], str, str, float], dict]] = None,
    model: str = llm.OLLAMA_MODEL,
    ollama_url: str = llm.OLLAMA_URL,
    timeout: float = 90.0,
) -> tuple[DraftPlan, dict]:
    """Fill a draft's weak load-bearing fields from an LLM read of the EFL text.

    The LLM only ever *proposes*; acceptance is deterministic. A proposed number
    is taken when it is literally present in the EFL text
    (:func:`_verify_number_in_text`) -- small local models report 0 confidence for
    every field, so their self-assessment can't be the gate -- and such a value is
    recorded at ``verified_confidence``. A model that does report real confidence
    (>= ``min_confidence``) is honoured too, capped at ``cap_confidence``.

    Returns ``(draft, report)``. ``report`` is ``{"used_llm": bool, "changed":
    [field, ...], "note": str}``. The draft is only modified when at least one
    weak field was accepted AND the merge re-validates against the ``Plan``
    schema; otherwise the original draft is returned. Fields the parser was
    already confident about (>= ``keep_threshold``) are never touched, and a flat
    LLM rate never replaces a structured (windowed/multi-rate) schedule.
    """
    from energyanalyzer.core.models import Plan  # local import: avoid cycle at import time

    report = {"used_llm": False, "changed": [], "note": ""}
    weak_keys = [k for k in LOAD_BEARING_KEYS if _weak(draft, k, keep_threshold)]
    if not weak_keys:
        report["note"] = "no weak load-bearing fields"
        return draft, report

    # Show the model what the deterministic parser already produced, so it
    # confirms/corrects a real starting point instead of re-deriving blind.
    current = {
        "base_charge_usd": draft.plan_dict.get("base_charge_usd"),
        "energy_rates": draft.plan_dict.get("energy_rates"),
        "buyback": draft.plan_dict.get("buyback"),
    }
    user = (
        "The deterministic parser produced this, but is UNSURE about: "
        f"{', '.join(weak_keys)}.\n"
        f"Current parse: {json.dumps(current, default=str)}\n\n"
        "Read the EFL below and return the correct values in the schema. If the "
        "current parse is already right, return the same values.\n\n"
        f"EFL text:\n{(text or '').strip()[:6000]}"
    )
    parsed = llm.chat_json(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        model=model,
        ollama_url=ollama_url,
        timeout=timeout,
        chat_fn=chat_fn,
    )
    report["used_llm"] = parsed is not None
    if not parsed:
        report["note"] = "LLM unavailable or non-JSON response"
        return draft, report

    conf = parsed.get("confidence") or {}
    new_dict = dict(draft.plan_dict)
    new_conf = dict(draft.confidence)
    new_ev = dict(draft.evidence)
    changed: list[str] = []

    def _cap(c: float) -> float:
        return min(float(c or 0.0), cap_confidence)

    def _num(x) -> Optional[float]:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    # A proposal is accepted when the number is verifiable in the EFL text, OR
    # (for a mocked/large model that does self-assess) when it reports real
    # confidence. Small local models return 0 for every field, so verification
    # -- not self-reported confidence -- is the primary gate.
    def _accept(field: str, value: Optional[float]) -> bool:
        if _verify_number_in_text(value, text):
            return True
        return float(conf.get(field, 0.0) or 0.0) >= min_confidence

    def _score(field: str) -> float:
        """Confidence to record: the model's own if it gave a usable one, else a
        moderate score earned by verifying the value against the source text."""
        reported = float(conf.get(field, 0.0) or 0.0)
        return _cap(reported) if reported >= min_confidence else _cap(verified_confidence)

    # base_charge
    base = _num(parsed.get("base_charge_usd"))
    if "base_charge" in weak_keys and base is not None:
        if _accept("base_charge", base):
            new_dict["base_charge_usd"] = base
            new_conf["base_charge"] = _score("base_charge")
            new_ev["base_charge"] = "llm(verified in EFL text)"
            changed.append("base_charge")

    # energy_charge (+ free_window)
    if ("energy_charge" in weak_keys or "free_window" in weak_keys) and parsed.get("energy_rates"):
        rates = _build_energy_rates(parsed["energy_rates"])
        # The LLM must never RESTRUCTURE a schedule the static parser already
        # worked out. Measured: on a Free-Weekends EFL the model replaced a
        # correct {0c weekends, 22.9c weekdays} split with {0c hour-23, 0c
        # catch-all}. Structure is the parser's job; the LLM only fills in a
        # plain flat rate the parser couldn't read.
        if _has_windowed_rates(draft.plan_dict):
            report["note"] = "static parser already has a structured schedule -- LLM energy ignored"
        elif rates is not None and not _catch_all_rate_sane(rates):
            report["note"] = "LLM energy schedule rejected (no sane positive catch-all rate)"
        elif rates is not None and _accept("energy_charge", rates[0].get("rate_ckwh")):
            new_dict["energy_rates"] = rates
            new_conf["energy_charge"] = _score("energy_charge")
            new_ev["energy_charge"] = "llm(verified in EFL text)"
            changed.append("energy_charge")
            if len(rates) > 1:  # a free/discounted window was set
                new_conf["free_window"] = _score("free_window")
                new_ev["free_window"] = "llm"
                changed.append("free_window")

    # buyback -- a fixed credit must have its rate verifiable in the text; a
    # 'none'/'rtw' verdict has no number to check, so it needs real confidence.
    if "buyback" in weak_keys and parsed.get("buyback"):
        bb = _apply_buyback(parsed["buyback"])
        if bb is not None:
            ok = (
                _accept("buyback", bb["rate_ckwh"])
                if bb.get("kind") == "fixed"
                else float(conf.get("buyback", 0.0) or 0.0) >= min_confidence
            )
            if ok:
                new_dict["buyback"] = bb
                new_conf["buyback"] = _score("buyback")
                new_ev["buyback"] = "llm"
                changed.append("buyback")

    if not changed:
        report["note"] = "LLM had no confident value for the weak fields"
        return draft, report

    # Re-validate the merged plan before accepting the LLM's changes.
    try:
        Plan.model_validate({k: v for k, v in new_dict.items() if k != "_parse"})
    except Exception as exc:  # noqa: BLE001 -- reject an LLM merge that doesn't validate
        report["note"] = f"LLM merge rejected (schema invalid): {exc!r}"
        return draft, report

    # If every load-bearing field is now confident, let the draft auto-promote.
    all_conf = all(new_conf.get(k, 0.0) >= keep_threshold for k in LOAD_BEARING_KEYS if k in new_conf)
    note = f"LLM parse set: {', '.join(changed)}."
    existing_notes = str(new_dict.get("notes") or "").strip()
    new_dict["notes"] = f"{existing_notes} {note}".strip() if existing_notes else note
    if all_conf and new_dict.get("needs_review"):
        new_dict["needs_review"] = False
        report["note"] = "cleared needs_review"
    else:
        report["note"] = "filled fields; still needs_review"

    logger.info(
        "LLM repaired draft %s: %s (%s)",
        draft.plan_dict.get("id", "?"), ", ".join(changed), report["note"],
    )
    report["changed"] = changed
    return DraftPlan(plan_dict=new_dict, confidence=new_conf, evidence=new_ev, unparsed_notes=draft.unparsed_notes), report
