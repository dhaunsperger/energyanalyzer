#!/usr/bin/env python
"""One-off audit: have a local LLM independently re-read every EFL-sourced plan
in the database and flag where it DISAGREES with a value the parser was
confident about.

This hunts the silent-wrong class -- fields the parser scored >= 0.8 and
therefore never showed a human, so a wrong one goes straight into the rankings.
It is a diagnostic, not a pipeline stage: it writes nothing and changes nothing.

Design note -- INDEPENDENCE. Unlike ``eflparse.llm_repair`` (which shows the
model the parser's current answer so it can fill gaps), this deliberately does
NOT reveal the parser's values. Priming a small model with an answer biases it
heavily toward agreeing, which would make the audit worthless. The model reads
the EFL cold; the comparison happens afterwards in Python.

Expect false positives -- a 4B model misreads tables, and the parser is right far
more often than not. Output is ranked by estimated annual dollar impact so the
findings worth opening the PDF for float to the top. Every hit needs a human to
check it against the actual EFL before anything is changed.

Usage::

    python scripts/audit_plans_llm.py                    # audit plans/
    python scripts/audit_plans_llm.py --include-drafts   # + plans/drafts/
    python scripts/audit_plans_llm.py --limit 20 --model gemma3:4b
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

from energyanalyzer import llm  # noqa: E402
from energyanalyzer.eflparse.parser import extract_text  # noqa: E402

EFL_DIR = REPO / "data" / "efl"
CONFIDENT = 0.8

# Rough annual volumes from the owner's real interval data (ARCHITECTURE.md §1),
# used ONLY to rank findings by materiality -- never to compute a bill.
ANNUAL_IMPORT_KWH = 11_278.0
ANNUAL_EXPORT_KWH = 9_803.0

_SYSTEM = (
    "You read a Texas residential Electricity Facts Label (EFL) and report its rate "
    "structure as strict JSON. Report ONLY what the EFL clearly states. Use null when "
    "it does not state a value. Do not guess, and do not infer from typical plans.\n\n"
    "Some of these PDFs have damaged fonts that drop letters (s, b, w, y, E), so "
    "'ae Charge' means 'Base Charge' and 'nerg Charge' means 'Energy Charge'.\n\n"
    "Report:\n"
    "- base_charge_usd: the REP's own fixed monthly charge in dollars (0 if the EFL "
    "says there is none). This is NOT the TDU/delivery monthly charge -- ignore any "
    "line labeled TDU, TDSP, Oncor, CenterPoint, AEP or 'delivery'.\n"
    "- energy_ckwh: the REP's energy charge in CENTS per kWh. Careful with units: "
    "'$0.0559 per kWh' is 5.59 cents, '5.59c per kWh' is 5.59 cents. This is NOT the "
    "'Average price per kWh' table, which bundles delivery charges -- ignore that table.\n"
    "- buyback_kind: 'none', 'fixed' (a set cents/kWh credit for exported solar), or "
    "'rtw' (a credit indexed to real-time/wholesale/market/settlement-point prices).\n"
    "- buyback_ckwh: the fixed export credit in cents per kWh, or null.\n"
    "- quote: the exact line(s) from the EFL you read these off.\n\n"
    'Respond with exactly: {"base_charge_usd": number|null, "energy_ckwh": number|null, '
    '"buyback_kind": string, "buyback_ckwh": number|null, "quote": string}'
)


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parser_energy_ckwh(plan: dict) -> Optional[float]:
    """The plan's flat/default energy rate, or None if it has a windowed
    schedule (which this audit deliberately does not second-guess -- structure
    is the parser's strength and the model's weakness)."""
    rates = plan.get("energy_rates") or []
    if len(rates) != 1 or (rates[0] or {}).get("window") is not None:
        return None
    return _num(rates[0].get("rate_ckwh"))


def audit_one(path: Path, model: str, timeout: float) -> Optional[dict]:
    raw = yaml.safe_load(path.read_text()) or {}
    source = str(raw.get("source") or "")
    if not source.startswith("efl:"):
        return None  # PTC/meterplan-sourced: no document to re-read
    pdf = EFL_DIR / source[4:]
    if not pdf.exists():
        return None
    conf = (raw.get("_parse") or {}).get("confidence") or {}

    try:
        text = extract_text(pdf)
    except Exception as exc:  # noqa: BLE001
        return {"id": raw.get("id"), "error": f"unreadable: {exc!r}", "findings": []}

    got = llm.chat_json(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"EFL text:\n{text.strip()[:6000]}"},
        ],
        model=model,
        timeout=timeout,
    )
    if not got:
        return {"id": raw.get("id"), "error": "no LLM response", "findings": []}

    findings = []

    # How "was the parser confident about this?" is answered depends on where the
    # plan lives, and getting this wrong silently disables the whole audit:
    #
    #  * A DRAFT still carries `_parse.confidence`, so use it directly.
    #  * A PROMOTED plan does NOT -- `plan_fields()` strips `_parse` on promotion
    #    because it isn't part of the Plan schema. Reading a missing block gives
    #    0.0 for every field, which reads as "never confident" and suppresses
    #    every finding. But promotion is itself the confidence signal: a plan only
    #    gets into plans/ by clearing the auto-promote gate (needs_review False
    #    AND all load-bearing fields >= 0.8), or by being entered by hand. So an
    #    unflagged promoted plan IS the confident population -- exactly what this
    #    audit exists to check.
    has_parse_meta = bool(conf)
    promoted_unflagged = not raw.get("needs_review", False)

    def _was_confident(field: str) -> bool:
        if has_parse_meta:
            return float(conf.get(field, 0.0) or 0.0) >= CONFIDENT
        return promoted_unflagged

    def flag(field: str, parser_val, llm_val, impact: float, note: str = "") -> None:
        # Only a field the parser was CONFIDENT about counts -- a low-confidence
        # field is already queued for review, so disagreement there is expected
        # and uninteresting.
        if not _was_confident(field):
            return
        findings.append(
            {
                "field": field,
                "parser": parser_val,
                "llm": llm_val,
                "annual_usd": round(impact, 2),
                "note": note,
            }
        )

    # base charge
    p_base, l_base = _num(raw.get("base_charge_usd")), _num(got.get("base_charge_usd"))
    if p_base is not None and l_base is not None and abs(p_base - l_base) > 0.01:
        flag("base_charge", p_base, l_base, abs(p_base - l_base) * 12)

    # energy rate (flat-rate plans only)
    p_energy, l_energy = _parser_energy_ckwh(raw), _num(got.get("energy_ckwh"))
    if p_energy is not None and l_energy is not None and abs(p_energy - l_energy) > 0.02:
        flag("energy_charge", p_energy, l_energy, abs(p_energy - l_energy) / 100 * ANNUAL_IMPORT_KWH)

    # buyback kind + rate
    p_bb = raw.get("buyback") or {}
    p_kind, l_kind = str(p_bb.get("kind") or "none"), str(got.get("buyback_kind") or "none").lower()
    if l_kind in ("none", "fixed", "rtw") and p_kind != l_kind:
        # A missed buyback is the costliest error class: every exported kWh
        # earns nothing. Value it at a mid-range credit rate purely for ranking.
        flag("buyback", p_kind, l_kind, 0.05 * ANNUAL_EXPORT_KWH, "buyback KIND differs")
    elif p_kind == l_kind == "fixed":
        pr, lr = _num(p_bb.get("rate_ckwh")), _num(got.get("buyback_ckwh"))
        if pr is not None and lr is not None and abs(pr - lr) > 0.02:
            flag("buyback", pr, lr, abs(pr - lr) / 100 * ANNUAL_EXPORT_KWH)

    return {
        "id": raw.get("id"),
        "file": path.name,
        "pdf": pdf.name,
        "findings": findings,
        "quote": str(got.get("quote") or "")[:220],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=llm.OLLAMA_MODEL)
    ap.add_argument("--include-drafts", action="store_true")
    ap.add_argument("--limit", type=int, help="audit only the first N plans (smoke test)")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--json-out", type=Path, help="write full results here")
    args = ap.parse_args()

    paths = sorted((REPO / "plans").glob("*.yaml"))
    if args.include_drafts:
        paths += sorted((REPO / "plans" / "drafts").glob("*.yaml"))
    if args.limit:
        paths = paths[: args.limit]

    if not llm.available():
        sys.exit("Ollama is not reachable -- start it and retry.")

    results, skipped, t0 = [], 0, time.time()
    for i, p in enumerate(paths, 1):
        res = audit_one(p, args.model, args.timeout)
        if res is None:
            skipped += 1
        else:
            results.append(res)
        print(f"\r  {i}/{len(paths)}  audited={len(results)} skipped={skipped}", end="", flush=True)
    print(f"\n\nElapsed {time.time() - t0:.0f}s using {args.model}\n")

    flagged = [r for r in results if r.get("findings")]
    errored = [r for r in results if r.get("error")]
    flagged.sort(key=lambda r: -max(f["annual_usd"] for f in r["findings"]))

    print("=" * 78)
    print(f"{len(results)} EFL-sourced plans audited  |  {skipped} skipped (no source EFL PDF)")
    print(f"{len(flagged)} with a disagreement on a CONFIDENT field  |  {len(errored)} errors")
    print("=" * 78)
    print("\nRanked by estimated annual $ impact. These are CANDIDATES: a 4B model")
    print("misreads tables, and the parser is usually right. Open the EFL before")
    print("changing anything.\n")

    for r in flagged:
        top = max(f["annual_usd"] for f in r["findings"])
        print(f"~${top:>7.0f}/yr  {r['id']}")
        for f in r["findings"]:
            print(f"             {f['field']}: parser={f['parser']!r}  llm={f['llm']!r}  {f['note']}")
        if r.get("quote"):
            print(f"             model quoted: {r['quote'][:150]}")
        print(f"             pdf: {r['pdf']}\n")

    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=2, default=str))
        print(f"Full results -> {args.json_out}")


if __name__ == "__main__":
    main()
