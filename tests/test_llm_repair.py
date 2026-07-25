"""Tests for eflparse.llm_repair -- the LLM second-pass over low-confidence
drafts. All LLM I/O is mocked via chat_fn; no Ollama, no network. Covers the
merge/validation/safety logic (the prompt quality itself is tuned live).
"""

from __future__ import annotations

import json

from energyanalyzer.eflparse.llm_repair import llm_repair_draft
from energyanalyzer.eflparse.parser import DraftPlan


def _draft(confidence, *, needs_review=True, energy_rates=None, buyback=None):
    return DraftPlan(
        plan_dict={
            "id": "test_plan_12mo",
            "retailer": "Test Retailer",
            "name": "Test Plan 12",
            "term_months": 12,
            "base_charge_usd": 9.95,
            "energy_rates": energy_rates or [{"rate_ckwh": 11.0}],
            "buyback": buyback or {"kind": "none"},
            "tdu_passthrough": True,
            "source": "efl:test.pdf",
            "needs_review": needs_review,
        },
        confidence=dict(confidence),
        evidence={},
        unparsed_notes=[],
    )


def _chat(payload):
    """A chat_fn seam returning `payload` as the model's JSON content."""
    return lambda messages, model, url, timeout: {"message": {"content": json.dumps(payload)}}


def test_fills_weak_buyback_when_llm_confident():
    draft = _draft({"energy_charge": 0.9, "base_charge": 0.9, "buyback": 0.4})
    payload = {
        "base_charge_usd": None,
        "energy": None,
        "buyback": {"kind": "fixed", "rate_ckwh": 5.3, "offset_scope": "energy_only"},
        "confidence": {"buyback": 0.9},
        "reasoning": "EFL states a fixed 5.3 cent solar credit.",
    }
    repaired, report = llm_repair_draft(draft, "efl text", chat_fn=_chat(payload))
    assert report["changed"] == ["buyback"]
    assert repaired.plan_dict["buyback"] == {
        "kind": "fixed", "rate_ckwh": 5.3, "offset_scope": "energy_only"
    }
    assert repaired.confidence["buyback"] <= 0.85  # capped
    assert repaired.evidence["buyback"] == "llm"
    # ASSIST-ONLY: even with every load-bearing field now scoring confidently,
    # an LLM-touched draft still goes to a human. The tier pre-fills the review
    # form; it never promotes.
    assert repaired.plan_dict["needs_review"] is True
    assert "LLM suggested: buyback" in repaired.plan_dict["notes"]
    # ...and the UI can tell which fields the model supplied, and why.
    assert repaired.plan_dict["_llm_suggested"]["fields"] == ["buyback"]
    assert "5.3" in repaired.plan_dict["_llm_suggested"]["reasoning"]


def test_llm_suggestions_never_clear_review_even_when_all_fields_confident():
    """Regression guard for the assist-only contract: no combination of high
    parser confidence + high LLM confidence may auto-promote a draft."""
    draft = _draft({"energy_charge": 0.95, "base_charge": 0.95, "buyback": 0.79})
    payload = {
        "buyback": {"kind": "fixed", "rate_ckwh": 5.3},
        "confidence": {"buyback": 1.0},
        "reasoning": "stated plainly",
    }
    repaired, _ = llm_repair_draft(draft, "efl text", chat_fn=_chat(payload))
    assert repaired.plan_dict["needs_review"] is True


def test_llm_metadata_is_stripped_before_promotion():
    """`_llm_suggested` is review metadata, not a Plan field -- plan_fields()
    must remove it (alongside `_parse`) so promotion validates."""
    from energyanalyzer.eflparse.parser import plan_fields

    raw = {"id": "x", "_parse": {"confidence": {}}, "_llm_suggested": {"fields": ["buyback"]}}
    assert plan_fields(raw) == {"id": "x"}


def test_low_llm_confidence_leaves_draft_unchanged():
    draft = _draft({"energy_charge": 0.9, "base_charge": 0.9, "buyback": 0.4})
    payload = {"buyback": {"kind": "fixed", "rate_ckwh": 5.3}, "confidence": {"buyback": 0.3}}
    repaired, report = llm_repair_draft(draft, "text", chat_fn=_chat(payload), min_confidence=0.7)
    assert report["changed"] == []
    assert repaired is draft


def test_confident_static_field_not_overridden():
    # energy is already confident (0.9); the LLM must not touch it even if it
    # returns a different rate.
    draft = _draft({"energy_charge": 0.9, "base_charge": 0.9, "buyback": 0.4})
    payload = {
        "energy": {"flat_rate_ckwh": 99.0},
        "buyback": {"kind": "none"},
        "confidence": {"energy_charge": 0.95, "buyback": 0.9},
    }
    repaired, report = llm_repair_draft(draft, "text", chat_fn=_chat(payload))
    assert repaired.plan_dict["energy_rates"] == [{"rate_ckwh": 11.0}]  # unchanged
    assert "energy_charge" not in report["changed"]


def test_free_window_rebuilds_energy_rates():
    draft = _draft({"energy_charge": 0.4, "free_window": 0.4, "base_charge": 0.9})
    payload = {
        "energy_rates": [
            {
                "rate_ckwh": 0.0,
                "label": "free nights",
                "window": {"hours": [21, 22, 23, 0, 1, 2, 3, 4, 5]},
            },
            {"rate_ckwh": 14.5, "window": None},
        ],
        "confidence": {"energy_charge": 0.85, "free_window": 0.85},
    }
    repaired, report = llm_repair_draft(draft, "text", chat_fn=_chat(payload))
    assert "energy_charge" in report["changed"] and "free_window" in report["changed"]
    rates = repaired.plan_dict["energy_rates"]
    assert len(rates) == 2
    assert rates[0]["window"]["hours"] == [21, 22, 23, 0, 1, 2, 3, 4, 5]
    assert rates[0]["rate_ckwh"] == 0.0
    assert rates[1]["rate_ckwh"] == 14.5


def test_invalid_merge_is_rejected():
    # LLM returns a buyback that won't validate (fixed with no rate handled as
    # unusable -> no change); force an invalid energy structure instead.
    draft = _draft({"energy_charge": 0.4, "base_charge": 0.9, "buyback": 0.9})
    # flat_rate as a non-number string slips past _build_energy_rates? No -- guard
    # by making the merged plan invalid: negative not caught, so use a bad type.
    payload = {
        "energy": {"flat_rate_ckwh": "not-a-number"},
        "confidence": {"energy_charge": 0.9},
    }
    repaired, report = llm_repair_draft(draft, "text", chat_fn=_chat(payload))
    # _build_energy_rates raises on float("not-a-number") -> treated as no usable
    # rate -> no change.
    assert report["changed"] == []
    assert repaired is draft


def test_ollama_unavailable_returns_original():
    draft = _draft({"buyback": 0.4})

    def _boom(messages, model, url, timeout):
        raise OSError("connection refused")

    repaired, report = llm_repair_draft(draft, "text", chat_fn=_boom)
    assert report["used_llm"] is False
    assert repaired is draft


def test_no_weak_fields_skips_llm():
    called = {"n": 0}

    def _count(messages, model, url, timeout):
        called["n"] += 1
        return {"message": {"content": "{}"}}

    draft = _draft({"energy_charge": 0.9, "base_charge": 0.9, "buyback": 0.9}, needs_review=False)
    repaired, report = llm_repair_draft(draft, "text", chat_fn=_count)
    assert called["n"] == 0
    assert report["used_llm"] is False
    assert repaired is draft
