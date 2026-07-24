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

import logging
from typing import Callable, Optional

from energyanalyzer import llm
from energyanalyzer.eflparse.parser import LOAD_BEARING_KEYS, DraftPlan

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You extract the load-bearing fields of a Texas residential Electricity Facts "
    "Label (EFL) as strict JSON. Read the EFL text and report ONLY what it clearly "
    "states; use null when the EFL does not clearly state a value. Do not guess.\n\n"
    "Definitions:\n"
    "- base_charge_usd: the fixed monthly base/minimum-usage charge in dollars (0 if none).\n"
    "- energy: the retailer's energy charge in cents per kWh. If the plan has free or "
    "discounted hours (e.g. free nights/weekends), report free_hours (integers 0-23, the "
    "local clock hours that are free/discounted), free_rate_ckwh (the cents/kWh during "
    "those hours; 0 if truly free), and other_rate_ckwh (cents/kWh the rest of the time). "
    "Otherwise report only flat_rate_ckwh.\n"
    "- buyback: the solar export credit. kind is 'none', 'fixed' (a set cents/kWh -> "
    "rate_ckwh), or 'rtw' (a real-time/wholesale/market-indexed credit). offset_scope is "
    "'all_charges' if the credit can offset any charge, or 'energy_only' if the EFL says "
    "the credit applies to energy charges only / is not offsettable against base/TDU.\n"
    "- confidence: your 0..1 confidence for each of base_charge, energy_charge, "
    "free_window, buyback.\n\n"
    'Respond with exactly this JSON shape: {"base_charge_usd": number|null, "energy": '
    '{"flat_rate_ckwh": number|null, "free_hours": [int]|null, "free_rate_ckwh": '
    'number|null, "other_rate_ckwh": number|null}, "buyback": {"kind": '
    '"none"|"fixed"|"rtw", "rate_ckwh": number|null, "offset_scope": '
    '"all_charges"|"energy_only"}, "confidence": {"base_charge": number, '
    '"energy_charge": number, "free_window": number, "buyback": number}, "reasoning": '
    '"one short sentence"}'
)


def _weak(draft: DraftPlan, key: str, keep_threshold: float) -> bool:
    """A load-bearing field is 'weak' (eligible for LLM fill) when the parser
    scored it but below keep_threshold. A field the parser never scored (e.g.
    free_window on a flat-rate plan) is NOT weak -- it's simply not applicable,
    and the auto-promote gate ignores it too, so we don't invent one."""
    return key in draft.confidence and draft.confidence[key] < keep_threshold


def _build_energy_rates(energy: dict) -> Optional[list]:
    """Turn the LLM 'energy' object into an energy_rates list, or None if it
    didn't give a usable rate. Returns None (never raises) on a malformed value
    so a junk LLM number degrades to 'no change', not a crash."""
    try:
        free_hours = energy.get("free_hours")
        other = energy.get("other_rate_ckwh")
        free_rate = energy.get("free_rate_ckwh")
        if free_hours and other is not None and free_rate is not None:
            return [
                {
                    "label": "free/discounted (LLM)",
                    "rate_ckwh": float(free_rate),
                    "window": {"hours": [int(h) for h in free_hours]},
                },
                {"rate_ckwh": float(other), "window": None},
            ]
        flat = energy.get("flat_rate_ckwh")
        if flat is not None:
            return [{"rate_ckwh": float(flat), "window": None}]
    except (TypeError, ValueError):
        return None
    return None


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
    chat_fn: Optional[Callable[[list[dict], str, str, float], dict]] = None,
    model: str = llm.OLLAMA_MODEL,
    ollama_url: str = llm.OLLAMA_URL,
    timeout: float = 90.0,
) -> tuple[DraftPlan, dict]:
    """Fill a draft's weak load-bearing fields from an LLM read of the EFL text.

    Returns ``(draft, report)``. ``report`` is ``{"used_llm": bool, "changed":
    [field, ...], "note": str}``. The draft is only modified when the merge
    re-validates against the ``Plan`` schema and the LLM cleared at least one
    weak field at >= ``min_confidence``; otherwise the original draft is returned.
    Fields the parser was already confident about (>= ``keep_threshold``) are
    never touched.
    """
    from energyanalyzer.core.models import Plan  # local import: avoid cycle at import time

    report = {"used_llm": False, "changed": [], "note": ""}
    weak_keys = [k for k in LOAD_BEARING_KEYS if _weak(draft, k, keep_threshold)]
    if not weak_keys:
        report["note"] = "no weak load-bearing fields"
        return draft, report

    user = (
        "Weak fields the deterministic parser needs help with: "
        f"{', '.join(weak_keys)}.\n\nEFL text:\n{(text or '').strip()[:6000]}"
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

    # base_charge
    base = _num(parsed.get("base_charge_usd"))
    if "base_charge" in weak_keys and base is not None:
        if float(conf.get("base_charge", 0.0)) >= min_confidence:
            new_dict["base_charge_usd"] = base
            new_conf["base_charge"] = _cap(conf.get("base_charge"))
            new_ev["base_charge"] = "llm"
            changed.append("base_charge")

    # energy_charge (+ free_window)
    if ("energy_charge" in weak_keys or "free_window" in weak_keys) and parsed.get("energy"):
        rates = _build_energy_rates(parsed["energy"])
        if rates is not None and float(conf.get("energy_charge", 0.0)) >= min_confidence:
            new_dict["energy_rates"] = rates
            new_conf["energy_charge"] = _cap(conf.get("energy_charge"))
            new_ev["energy_charge"] = "llm"
            changed.append("energy_charge")
            if len(rates) > 1:  # a free/discounted window was set
                new_conf["free_window"] = _cap(conf.get("free_window"))
                new_ev["free_window"] = "llm"
                changed.append("free_window")

    # buyback
    if "buyback" in weak_keys and parsed.get("buyback"):
        bb = _apply_buyback(parsed["buyback"])
        if bb is not None and float(conf.get("buyback", 0.0)) >= min_confidence:
            new_dict["buyback"] = bb
            new_conf["buyback"] = _cap(conf.get("buyback"))
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
