#!/usr/bin/env python
"""Accuracy harness for the EFL parser and its optional local-LLM repair tier.

Scores the deterministic parser -- and, with ``--model``, the parser plus an LLM
repair pass -- against the hand-verified corpus in
``tests/fixtures/efl_texts/real/ground_truth.yaml``.

The headline metric is NOT the size of the review queue. It is **silent-wrong**:
a load-bearing field the parser reported at >= 0.8 confidence whose value is
actually wrong. Those never reach a human, so they land straight in the
rankings. A change that halves the review queue while adding one silent-wrong is
a bad trade, and this harness is built to make that visible.

Usage::

    python scripts/eval_efl.py                       # deterministic parser only
    python scripts/eval_efl.py --model qwen3:4b      # + LLM repair tier
    python scripts/eval_efl.py --model qwen3:4b --only ATLANTEX --verbose
    python scripts/eval_efl.py --compare qwen3:4b granite4:micro gemma3:4b
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from energyanalyzer.eflparse.parser import parse_efl_text  # noqa: E402

CORPUS = REPO / "tests" / "fixtures" / "efl_texts" / "real"
GROUND_TRUTH = CORPUS / "ground_truth.yaml"
CONFIDENT = 0.8

# Fields scored. `free_window` is folded into energy_rates (a window IS the
# free-hours structure), so it is not scored separately.
FIELDS = ("base_charge", "energy_charge", "buyback", "term_months")


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def _close(a: Optional[float], b: Optional[float], tol: float = 0.005) -> bool:
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= tol


def _norm_window(w: Optional[dict]) -> Optional[tuple]:
    """Windows compare on content, not key order or empty-vs-missing lists."""
    if not w:
        return None
    out = []
    for key in ("hours", "weekdays", "months"):
        vals = w.get(key) or []
        out.append((key, tuple(sorted(int(v) for v in vals))))
    return tuple(o for o in out if o[1])or None


def _rates_match(got: list, want: list) -> bool:
    if got is None or len(got) != len(want):
        return False
    for g, w in zip(got, want):
        if not _close(g.get("rate_ckwh"), w.get("rate_ckwh")):
            return False
        if _norm_window(g.get("window")) != _norm_window(w.get("window")):
            return False
    return True


def _buyback_match(got: dict, want: dict) -> bool:
    got = got or {}
    if str(got.get("kind") or "none") != str(want.get("kind") or "none"):
        return False
    if want.get("kind") == "fixed":
        return _close(got.get("rate_ckwh"), want.get("rate_ckwh"))
    return True


def score_draft(plan: dict, conf: dict, truth: dict) -> dict:
    """Compare one parsed plan against ground truth.

    Returns ``{field: {"ok": bool|None, "confident": bool, "got": ..., "want": ...}}``
    where ``ok is None`` means the field is unscoreable for this EFL (the
    document genuinely does not disclose it) and is excluded from all totals.
    """
    out: dict[str, dict] = {}

    def rec(field: str, ok: Optional[bool], got, want, conf_key: str):
        out[field] = {
            "ok": ok,
            "confident": float(conf.get(conf_key, 0.0) or 0.0) >= CONFIDENT,
            "got": got,
            "want": want,
        }

    rec(
        "base_charge",
        _close(plan.get("base_charge_usd"), truth.get("base_charge_usd"), tol=0.011),
        plan.get("base_charge_usd"),
        truth.get("base_charge_usd"),
        "base_charge",
    )
    rec(
        "energy_charge",
        _rates_match(plan.get("energy_rates"), truth.get("energy_rates") or []),
        plan.get("energy_rates"),
        truth.get("energy_rates"),
        "energy_charge",
    )
    want_bb = truth.get("buyback") or {}
    rec(
        "buyback",
        None if want_bb.get("unknown") else _buyback_match(plan.get("buyback"), want_bb),
        plan.get("buyback"),
        want_bb,
        "buyback",
    )
    rec(
        "term_months",
        plan.get("term_months") == truth.get("term_months"),
        plan.get("term_months"),
        truth.get("term_months"),
        "term_months",
    )
    return out


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(model: Optional[str], only: Optional[str], verbose: bool, timeout: float) -> dict:
    truths = yaml.safe_load(GROUND_TRUTH.read_text())
    rows, elapsed_llm = [], 0.0

    for name, truth in sorted(truths.items()):
        if only and only.lower() not in name.lower():
            continue
        text = (CORPUS / name).read_text(errors="replace")
        draft = parse_efl_text(text, source_name=name)
        note = ""
        if model:
            from energyanalyzer.eflparse.llm_repair import llm_repair_draft

            t0 = time.time()
            draft, report = llm_repair_draft(draft, text, model=model, timeout=timeout)
            elapsed_llm += time.time() - t0
            note = report.get("note", "")
        rows.append(
            {
                "name": name,
                "score": score_draft(draft.plan_dict, draft.confidence, truth),
                "review": bool(draft.plan_dict.get("needs_review")),
                "note": note,
            }
        )

    totals = {f: {"ok": 0, "bad": 0, "silent_wrong": 0, "n": 0} for f in FIELDS}
    review = silent_any = 0
    for r in rows:
        if r["review"]:
            review += 1
        for f, s in r["score"].items():
            if s["ok"] is None:
                continue
            t = totals[f]
            t["n"] += 1
            if s["ok"]:
                t["ok"] += 1
            else:
                t["bad"] += 1
                if s["confident"]:
                    t["silent_wrong"] += 1
        if any(s["ok"] is False and s["confident"] for s in r["score"].values()):
            silent_any += 1

    label = f"parser + LLM({model})" if model else "deterministic parser only"
    print(f"\n{'=' * 78}\n{label}   [{len(rows)} EFLs]\n{'=' * 78}")
    print(f"{'field':<16}{'correct':>12}{'wrong':>8}{'SILENT-WRONG':>15}")
    for f in FIELDS:
        t = totals[f]
        if not t["n"]:
            continue
        pct = 100.0 * t["ok"] / t["n"]
        print(f"{f:<16}{t['ok']:>4}/{t['n']:<3}{pct:>5.0f}%{t['bad']:>8}{t['silent_wrong']:>15}")
    n = len(rows)
    print(f"\n  EFLs fully correct : {sum(1 for r in rows if all(s['ok'] is not False for s in r['score'].values()))}/{n}")
    print(f"  EFLs in review     : {review}/{n}")
    print(f"  EFLs SILENT-WRONG  : {silent_any}/{n}   <-- confidently wrong, never seen by a human")
    if model:
        print(f"  LLM time           : {elapsed_llm:.0f}s total ({elapsed_llm / max(n, 1):.1f}s/EFL)")

    if verbose:
        print(f"\n{'-' * 78}\nper-EFL detail (only fields that are wrong)\n{'-' * 78}")
        for r in rows:
            bad = {f: s for f, s in r["score"].items() if s["ok"] is False}
            if not bad and not verbose:
                continue
            flag = "REVIEW" if r["review"] else "auto  "
            print(f"\n[{flag}] {r['name']}")
            for f, s in bad.items():
                mark = "!! SILENT" if s["confident"] else "   (flagged)"
                print(f"   {mark} {f}: got {json.dumps(s['got'], default=str)[:90]}")
                print(f"{'':>13}  want {json.dumps(s['want'], default=str)[:90]}")
            if r["note"]:
                print(f"   note: {r['note'][:110]}")

    return {"rows": rows, "totals": totals, "review": review, "silent": silent_any, "n": n}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="Ollama model for the LLM repair tier (omit for parser only)")
    ap.add_argument("--compare", nargs="+", metavar="MODEL", help="run the parser baseline then each model")
    ap.add_argument("--only", help="substring filter on fixture name")
    ap.add_argument("--verbose", action="store_true", help="per-EFL breakdown of wrong fields")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    if args.compare:
        results = [("baseline", run(None, args.only, args.verbose, args.timeout))]
        for m in args.compare:
            results.append((m, run(m, args.only, args.verbose, args.timeout)))
        print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
        print(f"{'variant':<26}{'correct':>10}{'review':>10}{'silent-wrong':>15}")
        for label, res in results:
            ok = sum(t["ok"] for t in res["totals"].values())
            tot = sum(t["n"] for t in res["totals"].values())
            print(f"{label:<26}{ok:>4}/{tot:<5}{res['review']:>7}/{res['n']:<3}{res['silent']:>12}/{res['n']}")
    else:
        run(args.model, args.only, args.verbose, args.timeout)


if __name__ == "__main__":
    main()
