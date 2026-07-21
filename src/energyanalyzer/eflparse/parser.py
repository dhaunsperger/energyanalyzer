"""Static (NO LLM) EFL PDF -> DraftPlan parser. ARCHITECTURE.md Sec 8.

Pipeline: extract_text(pdf) -> parse_efl_text(text) -> DraftPlan -> save_draft().

Every extracted field is layered: several regex/heuristic patterns are tried
from most-specific/reliable to least, the first hit wins, and its match text
is recorded as `evidence` with a 0..1 `confidence`. Fields that are
load-bearing for billing (an energy rate, the base charge, buyback, a free
window if one was detected) force `needs_review=True` on the draft plan
whenever their confidence is < 0.8, or when we saw conflicting values for the
same fact.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DRAFTS_DIR = REPO_ROOT / "plans" / "drafts"

LOAD_BEARING_KEYS = ("energy_charge", "base_charge", "buyback", "free_window")


# --------------------------------------------------------------------------- #
# DraftPlan
# --------------------------------------------------------------------------- #
@dataclass
class DraftPlan:
    plan_dict: dict
    confidence: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    unparsed_notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
def extract_text(pdf_path: str | Path) -> str:
    """Extract raw text from an EFL PDF: pdfplumber first, pdftotext -layout
    fallback (poppler-utils, no python deps)."""
    pdf_path = Path(pdf_path)
    try:
        import pdfplumber  # noqa: PLC0415

        with pdfplumber.open(pdf_path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages)
        if text.strip():
            return text
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        # pdfplumber pulls in pdfminer -> cryptography -> a Rust extension;
        # in some environments (e.g. missing _cffi_backend) the import itself
        # raises a pyo3_runtime.PanicException, which subclasses BaseException
        # rather than Exception, so a plain `except Exception` would miss it.
        # Either way: fall through to the pdftotext subprocess fallback.
        pass

    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# --------------------------------------------------------------------------- #
# Small generic helpers
# --------------------------------------------------------------------------- #
def _snippet(m: re.Match) -> str:
    s = re.sub(r"\s+", " ", m.group(0)).strip()
    return s[:180]


def _snippet_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s[:180]


def _clean_name(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" -–—,.")
    return s


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    return s or "plan"


_DOLLAR_KWH = re.compile(r"\$\s*(\d*\.\d+)\s*(?:per\s*kwh|/\s*kwh)?", re.I)
_CENT_KWH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:¢|cents?)", re.I)


def _rate_ckwh_from_snippet(s: str) -> Optional[float]:
    """Parse a rate expressed either as '$0.158 per kWh' or '15.8cents/15.8c'
    out of a short text snippet. Returns cents/kWh."""
    m = _DOLLAR_KWH.search(s)
    if m:
        return round(float(m.group(1)) * 100, 4)
    m = _CENT_KWH.search(s)
    if m:
        return round(float(m.group(1)), 4)
    return None


def _rate_usd_from_snippet(s: str) -> Optional[float]:
    """Parse a flat dollar amount (e.g. base charge, ETF) '$4.95'."""
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1))
    return None


# --------------------------------------------------------------------------- #
# Time-range parsing ("9 p.m. to 6 a.m." / "9 p.m. and 5:59 a.m." / ...)
# --------------------------------------------------------------------------- #
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", re.I)


def _to_hour(h: str, m: Optional[str], ap: str) -> int:
    hour = int(h) % 12
    if ap.lower() == "p":
        hour += 12
    return hour


def parse_time_range(s: str) -> list[int]:
    """Parse a phrase containing two clock times into a local-hour list
    (0-23, interval-start hour), e.g. '9 p.m. to 6 a.m.' ->
    [21,22,23,0,1,2,3,4,5]. '9 p.m. and 5:59 a.m.' (inclusive end minute)
    -> [21,...,5]. Returns [] if fewer than two times are found."""
    matches = list(_TIME_RE.finditer(s))
    if len(matches) < 2:
        return []
    sh, sm, sap = matches[0].groups()
    eh, em, eap = matches[1].groups()
    start_hour = _to_hour(sh, sm, sap)
    end_hour = _to_hour(eh, em, eap)
    end_inclusive = em is not None and int(em) > 0

    hours = [start_hour]
    h = start_hour
    for _ in range(24):
        if h == end_hour:
            break
        h = (h + 1) % 24
        if h == end_hour and not end_inclusive:
            break
        hours.append(h)
    return hours


_WEEKDAY_WORDS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _weekdays_from_snippet(s: str) -> list[int]:
    low = s.lower()
    if "weekend" in low or ("saturday" in low and "sunday" in low and "monday" not in low):
        return [5, 6]
    if "weekday" in low or re.search(r"monday\s*(through|-|to)\s*friday", low):
        return [0, 1, 2, 3, 4]
    days = sorted({v for k, v in _WEEKDAY_WORDS.items() if k in low})
    return days if days and len(days) < 7 else []


# --------------------------------------------------------------------------- #
# Layered-pattern matcher
# --------------------------------------------------------------------------- #
Extraction = tuple[object, float, str]  # value, confidence, evidence snippet


def _first_match(
    text: str, patterns: list[tuple[re.Pattern, float, Callable[[re.Match], object]]]
) -> Optional[Extraction]:
    for pat, conf, fn in patterns:
        m = pat.search(text)
        if m:
            try:
                val = fn(m)
            except Exception:
                continue
            if val is None:
                continue
            return val, conf, _snippet(m)
    return None


# --------------------------------------------------------------------------- #
# Field extractors
# --------------------------------------------------------------------------- #
def _extract_retailer(text: str) -> Optional[Extraction]:
    patterns = [
        (
            re.compile(
                r"([A-Z][A-Za-z0-9&.' \-]{2,60}?)(?:,?\s*(?:LLC|L\.L\.C\.|Inc\.?|LP|L\.P\.))?"
                r"\s*\(PUC (?:Certification\s*)?(?:No\.?|Number)",
                re.I,
            ),
            0.9,
            lambda m: _clean_name(m.group(1)),
        ),
        (
            re.compile(r"Retailer(?:\s*Name)?\s*[:\-]\s*(.+)", re.I),
            0.8,
            lambda m: _clean_name(m.group(1)),
        ),
        (
            re.compile(
                r"Electricity Facts Label[^\n]*\n+\s*([A-Z][A-Za-z0-9&.' \-]{2,55})",
                re.M,
            ),
            0.6,
            lambda m: _clean_name(m.group(1)),
        ),
    ]
    return _first_match(text, patterns)


def _extract_plan_name(text: str) -> Optional[Extraction]:
    patterns = [
        (
            re.compile(r"Plan\s*Name\s*[:\-]\s*(.+)", re.I),
            0.9,
            lambda m: _clean_name(m.group(1)),
        ),
        (
            re.compile(r"Product\s*Name\s*[:\-]\s*(.+)", re.I),
            0.85,
            lambda m: _clean_name(m.group(1)),
        ),
    ]
    return _first_match(text, patterns)


def _extract_term_months(text: str) -> Optional[Extraction]:
    patterns = [
        (
            re.compile(r"Contract\s*Term\s*[:\-]?\s*(\d+)\s*Months?", re.I),
            0.95,
            lambda m: int(m.group(1)),
        ),
        (
            re.compile(r"Month[- ]to[- ]Month", re.I),
            0.9,
            lambda m: 1,
        ),
        (
            re.compile(r"Term\s*(?:Length)?\s*[:\-]\s*(\d+)\s*[Mm]o(?:nths?)?\b"),
            0.7,
            lambda m: int(m.group(1)),
        ),
        (
            re.compile(r"(\d+)\s*[- ]?month\s*(?:term|contract|agreement)", re.I),
            0.6,
            lambda m: int(m.group(1)),
        ),
    ]
    return _first_match(text, patterns)


def _extract_rate_type(text: str) -> Optional[Extraction]:
    patterns = [
        (
            re.compile(r"Type\s*of\s*Product\s*[:\-]?\s*(Fixed|Variable|Indexed)", re.I),
            0.9,
            lambda m: m.group(1).lower(),
        ),
    ]
    return _first_match(text, patterns)


def _extract_base_charge(text: str) -> Optional[Extraction]:
    patterns = [
        (
            re.compile(
                r"Base\s*Charge\**\s*[:\-]?\s*\$\s*(\d+(?:\.\d+)?)\s*per\s*(?:billing\s*cycle|month)",
                re.I,
            ),
            0.95,
            lambda m: float(m.group(1)),
        ),
        (
            re.compile(
                r"Monthly\s*(?:Service|Customer|Base)\s*Charge\s*[:\-]?\s*\$\s*(\d+(?:\.\d+)?)",
                re.I,
            ),
            0.85,
            lambda m: float(m.group(1)),
        ),
        (
            re.compile(r"Base\s*Charge\**\s*\$\s*(\d+(?:\.\d+)?)", re.I),
            0.75,
            lambda m: float(m.group(1)),
        ),
        (
            re.compile(r"no\s*(?:monthly\s*)?base\s*charge", re.I),
            0.7,
            lambda m: 0.0,
        ),
    ]
    return _first_match(text, patterns)


def _extract_tdu(text: str) -> tuple[Optional[Extraction], Optional[Extraction], bool]:
    """Returns (per_kwh_extraction, monthly_extraction, bundled)."""
    bundled = bool(
        re.search(
            r"TDU[^.\n]{0,100}\b(?:included|bundled)\b[^.\n]{0,80}\bEnergy\s*Charge\b",
            text,
            re.I,
        )
        or re.search(
            r"Energy\s*Charge[^.\n]{0,100}\b(?:includes?|bundled)\b[^.\n]{0,80}\bTDU\b",
            text,
            re.I,
        )
    )
    ckwh = _first_match(
        text,
        [
            (
                re.compile(r"TDU\s*Delivery\s*Charge\s*\$\s*(\d+(?:\.\d+)?)\s*per\s*kWh", re.I),
                0.9,
                lambda m: float(m.group(1)) * 100,
            ),
        ],
    )
    monthly = _first_match(
        text,
        [
            (
                re.compile(
                    r"TDU\s*Delivery\s*Charge\s*\$\s*(\d+(?:\.\d+)?)\s*per\s*(?:billing\s*cycle|month)",
                    re.I,
                ),
                0.9,
                lambda m: float(m.group(1)),
            ),
        ],
    )
    return ckwh, monthly, bundled


def _extract_etf(text: str) -> Extraction:
    # "termination fee ... $X" then check the surrounding text (which, in a
    # two-column EFL flattened by pdftotext -layout, may have the *next*
    # column's label text interleaved before "month remaining") for the
    # per-month-remaining qualifier.
    m = re.search(
        r"(?:termination\s*fee|early\s*termination\s*fee|ETF)[^\n$]{0,80}?\$\s*(\d+(?:\.\d+)?)",
        text,
        re.I,
    )
    if m:
        tail = text[m.end() : m.end() + 250]
        evidence = _snippet_text(text[m.start() : m.end() + 40])
        if re.search(r"month\s*remaining", tail, re.I):
            return (float(m.group(1)), True), 0.9, evidence
        return (float(m.group(1)), False), 0.8, evidence
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)\s*per\s*month\s*remaining", text, re.I)
    if m:
        return (float(m.group(1)), True), 0.85, _snippet(m)
    return (0.0, False), 0.0, ""


def _extract_renewable_pct(text: str) -> Optional[Extraction]:
    patterns = [
        (
            re.compile(r"(?:is|product\s*is)\s*(\d+(?:\.\d+)?)\s*%\s*renewable", re.I),
            0.9,
            lambda m: float(m.group(1)),
        ),
        (
            re.compile(r"Renewable\s*Content\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*%", re.I),
            0.9,
            lambda m: float(m.group(1)),
        ),
        (
            re.compile(r"(\d+(?:\.\d+)?)\s*%\s*renewable", re.I),
            0.7,
            lambda m: float(m.group(1)),
        ),
    ]
    return _first_match(text, patterns)


def _extract_avg_prices(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    m = re.search(
        r"500\s*kWh[^\n]*?1,?000\s*kWh[^\n]*?2,?000\s*kWh\s*\n+\s*"
        r"(?:Average\s*Price\s*per\s*kWh)?\s*(\d+(?:\.\d+)?)\s*¢\s*"
        r"(\d+(?:\.\d+)?)\s*¢\s*(\d+(?:\.\d+)?)\s*¢",
        text,
        re.I,
    )
    if m:
        out["avg_price_500_ckwh"] = float(m.group(1))
        out["avg_price_1000_ckwh"] = float(m.group(2))
        out["avg_price_2000_ckwh"] = float(m.group(3))
    return out


# --------------------------------------------------------------------------- #
# Energy charge / free-windows / TOU
# --------------------------------------------------------------------------- #
_ENERGY_LINE = re.compile(
    r"(?:^|\n)\s*([A-Za-z][A-Za-z\-]{0,20}\s+)?Energy\s*Charge\**\s*[:\-]?\s*([^\n]{0,80})",
    re.I,
)

_TOU_LABELS = ("on-peak", "on peak", "off-peak", "off peak", "mid-peak", "mid peak", "shoulder", "peak")


def _classify_label(prefix: Optional[str]) -> Optional[str]:
    if not prefix:
        return None
    p = prefix.strip().lower()
    for lab in _TOU_LABELS:
        if lab in p:
            return lab.replace(" ", "-")
    return None


def _find_energy_lines(text: str) -> list[tuple[Optional[str], str, str]]:
    """Return list of (tou_label_or_None, rate_snippet, full_match_text) for
    every '<prefix> Energy Charge ... <rate>' occurrence."""
    out = []
    for m in _ENERGY_LINE.finditer(text):
        prefix, tail = m.group(1), m.group(2)
        label = _classify_label(prefix)
        out.append((label, tail, _snippet(m)))
    return out


def _extract_free_window(text: str) -> Optional[dict]:
    """Look for a 'free' claim near a two-time phrase or 'weekend(s)'.
    Returns {"hours": [...], "weekdays": [...], "evidence": str} or None.
    Excludes "toll-free"/"fee-free" style false positives and requires the
    snippet to actually be about a time-of-use/night/weekend freebie."""
    # Note: stop at a paragraph break (blank line), not at every '.' -- clock
    # times like "9 p.m." and "5:59 a.m." contain periods themselves.
    for m in re.finditer(r"(?<!toll[- ])(?<!fee[- ])\bfree\b(?:(?!\n\s*\n)[\s\S]){0,220}", text, re.I):
        snippet = m.group(0)
        low = snippet.lower()
        if not re.search(r"night|weekend|hour|electricity|energy|power|day", low):
            continue
        hours = parse_time_range(snippet)
        weekdays = _weekdays_from_snippet(snippet) if not hours else []
        if hours or weekdays:
            return {"hours": hours, "weekdays": weekdays, "evidence": _snippet(m)}
    return None


def _extract_tou_table(text: str) -> Optional[list[dict]]:
    """Detect a labeled Peak/Mid-Peak/Off-Peak energy-charge table where each
    line also carries its time window in parentheses, e.g.:
    'On-Peak Energy Charge: 35.7c per kWh (weekdays 5 p.m. to 9 p.m.)'
    Returns a list of {"label", "rate_ckwh", "hours", "weekdays", "is_default"}
    or None if fewer than 2 distinct TOU labels are found.
    """
    pat = re.compile(
        r"(On-?Peak|Mid-?Peak|Off-?Peak|Shoulder)\s+(?:Period\s+)?Energy\s*Charge\s*[:\-]?"
        r"\s*([^\n(]{0,60})(?:\(([^)]*)\))?",
        re.I,
    )
    rows = []
    for m in pat.finditer(text):
        label = m.group(1).lower().replace(" ", "-")
        rate_snip = m.group(2) or ""
        win_snip = m.group(3) or ""
        rate = _rate_ckwh_from_snippet(rate_snip)
        if rate is None:
            continue
        is_default = "off-peak" in label or "all other" in win_snip.lower() or not win_snip.strip()
        hours = [] if is_default else parse_time_range(win_snip)
        weekdays = [] if is_default else _weekdays_from_snippet(win_snip)
        rows.append(
            {
                "label": label,
                "rate_ckwh": rate,
                "hours": hours,
                "weekdays": weekdays,
                "is_default": is_default,
                "evidence": _snippet(m),
            }
        )
    labels = {r["label"] for r in rows}
    if len(labels) < 2:
        return None
    return rows


def _extract_bill_credits(text: str) -> list[dict]:
    """'bill credit of $X when usage is at least Y kWh [but less than Z kWh]'."""
    out = []
    pat = re.compile(
        r"(?:bill\s*)?credit\s*of\s*\$(\d+(?:\.\d+)?)\s*(?:will\s*be\s*applied\s*)?"
        r"(?:when|if)\s*(?:your|the customer'?s|monthly)?\s*usage\s*is\s*"
        r"(?:at\s*least|>=|greater\s*than\s*or\s*equal\s*to)\s*(\d+(?:,\d{3})*)\s*kWh"
        r"(?:\s*(?:but|and)\s*(?:less\s*than|<)\s*(\d+(?:,\d{3})*)\s*kWh)?",
        re.I,
    )
    for m in pat.finditer(text):
        credit = float(m.group(1))
        min_kwh = float(m.group(2).replace(",", ""))
        max_kwh = float(m.group(3).replace(",", "")) if m.group(3) else None
        out.append({"min_kwh": min_kwh, "max_kwh": max_kwh, "credit_usd": credit})
    return out


_BUYBACK_LABEL = re.compile(
    r"(Solar\s*(?:Repurchase|Buyback)|Buyback\s*Rate|Excess\s*Energy\s*(?:Credit|Rate|"
    r"Purchase)|Renewable\s*Buyback)[^\n]{0,100}",
    re.I,
)


def _extract_buyback(text: str, energy_ckwh: Optional[float]) -> tuple[dict, float, str]:
    """Returns (buyback_dict, confidence, evidence). Scans every buyback-ish
    label occurrence (skipping the ones that are just part of a "Plan Name:"
    / "Product Name:" heading) and takes the first that yields a usable
    rate or an rtw/1:1 signal."""
    candidates = []
    for m in _BUYBACK_LABEL.finditer(text):
        preceding = text[max(0, m.start() - 25) : m.start()].lower()
        if "plan name" in preceding or "product name" in preceding:
            continue
        candidates.append(m)

    if not candidates:
        return {"kind": "none"}, 0.95, ""

    fallback_evidence = _snippet(candidates[0])
    for m in candidates:
        context = text[max(0, m.start() - 30) : m.end() + 240]
        evidence = _snippet(m)

        if re.search(
            r"real-?time|wholesale|market\s*price|ERCOT\s*(?:price|settlement)|hourly",
            context,
            re.I,
        ):
            return {"kind": "rtw", "rtw": {"multiplier": 1.0, "adder_ckwh": 0.0}}, 0.85, evidence

        rate = _rate_ckwh_from_snippet(m.group(0))
        if rate is None:
            rate = _rate_ckwh_from_snippet(context)
        if rate is None and re.search(
            r"(?:equal\s*to|same\s*as|at)\s*(?:the\s*)?Energy\s*Charge", context, re.I
        ):
            if energy_ckwh is not None:
                return (
                    {"kind": "fixed", "rate_ckwh": energy_ckwh},
                    0.8,
                    evidence + " [inferred 1:1 == Energy Charge]",
                )
            continue
        if rate is None:
            continue

        offset_scope = "all_charges"
        if re.search(
            r"not\s*offsettable|energy\s*charges?\s*only|excludes?\s*(?:base|tdu)", context, re.I
        ):
            offset_scope = "energy_only"
        return {"kind": "fixed", "rate_ckwh": rate, "offset_scope": offset_scope}, 0.85, evidence

    return {"kind": "none"}, 0.3, fallback_evidence


# --------------------------------------------------------------------------- #
# Main entry points
# --------------------------------------------------------------------------- #
def parse_efl_text(text: str, source_name: str = "") -> DraftPlan:
    confidence: dict[str, float] = {}
    evidence: dict[str, str] = {}
    notes: list[str] = []

    def record(key: str, extraction: Optional[Extraction]) -> Optional[object]:
        if extraction is None:
            confidence[key] = 0.0
            return None
        val, conf, ev = extraction
        confidence[key] = conf
        evidence[key] = ev
        return val

    retailer = record("retailer", _extract_retailer(text)) or "Unknown Retailer"
    plan_name = record("plan_name", _extract_plan_name(text)) or "Unnamed Plan"
    term_months = record("term_months", _extract_term_months(text))
    if term_months is None:
        notes.append("term_months not found; defaulting to 12")
        term_months = 12
    rate_type = record("rate_type", _extract_rate_type(text)) or "fixed"

    # --- energy charge / free windows / TOU -------------------------------
    tou_rows = _extract_tou_table(text)
    free_win = _extract_free_window(text)
    energy_rates: list[dict] = []
    flat_ckwh: Optional[float] = None

    if tou_rows:
        defaults = [r for r in tou_rows if r["is_default"]]
        non_defaults = [r for r in tou_rows if not r["is_default"]]
        for r in non_defaults:
            window = {}
            if r["hours"]:
                window["hours"] = r["hours"]
            if r["weekdays"]:
                window["weekdays"] = r["weekdays"]
            energy_rates.append(
                {"label": r["label"], "rate_ckwh": r["rate_ckwh"], "window": window or None}
            )
        if defaults:
            energy_rates.append(
                {"label": defaults[0]["label"], "rate_ckwh": defaults[0]["rate_ckwh"], "window": None}
            )
        else:
            # no explicit catch-all row; use the cheapest as default fallback
            fallback = min(non_defaults, key=lambda r: r["rate_ckwh"])
            energy_rates.append({"label": "default", "rate_ckwh": fallback["rate_ckwh"], "window": None})
            notes.append("TOU table had no explicit off-peak/default row; used cheapest rate as catch-all")
        flat_ckwh = energy_rates[-1]["rate_ckwh"]
        confidence["energy_charge"] = 0.85
        evidence["energy_charge"] = " | ".join(r["evidence"] for r in tou_rows[:4])
    else:
        lines = _find_energy_lines(text)
        flat_candidates = [(lab, tail, ev) for lab, tail, ev in lines if lab is None]
        rates_found = []
        for _lab, tail, _ev in flat_candidates:
            r = _rate_ckwh_from_snippet(tail)
            if r is not None:
                rates_found.append(r)
        if rates_found:
            flat_ckwh = rates_found[0]
            conf = 0.9 if len(set(rates_found)) == 1 else 0.6
            if len(set(rates_found)) > 1:
                notes.append(
                    f"multiple differing flat Energy Charge values found {sorted(set(rates_found))}; "
                    f"used first ({flat_ckwh})"
                )
            confidence["energy_charge"] = conf
            evidence["energy_charge"] = flat_candidates[0][2]
        else:
            confidence["energy_charge"] = 0.0
            notes.append("could not find an Energy Charge rate")

        if free_win and flat_ckwh is not None:
            window = {}
            if free_win["hours"]:
                window["hours"] = free_win["hours"]
            if free_win["weekdays"]:
                window["weekdays"] = free_win["weekdays"]
            energy_rates.append({"label": "free", "rate_ckwh": 0.0, "window": window or None})
            energy_rates.append({"label": "", "rate_ckwh": flat_ckwh, "window": None})
            confidence["free_window"] = 0.85
            evidence["free_window"] = free_win["evidence"]
        elif flat_ckwh is not None:
            energy_rates.append({"label": "", "rate_ckwh": flat_ckwh, "window": None})
        else:
            energy_rates.append({"label": "", "rate_ckwh": 0.0, "window": None})

    # --- base charge --------------------------------------------------- #
    base_charge = record("base_charge", _extract_base_charge(text))
    if base_charge is None:
        notes.append("base charge not found; defaulting to 0.0")
        base_charge = 0.0

    # --- TDU ------------------------------------------------------------ #
    tdu_ckwh_ext, tdu_monthly_ext, bundled = _extract_tdu(text)
    if tdu_ckwh_ext:
        confidence["tdu_ckwh"] = tdu_ckwh_ext[1]
        evidence["tdu_ckwh"] = tdu_ckwh_ext[2]
    if tdu_monthly_ext:
        confidence["tdu_monthly"] = tdu_monthly_ext[1]
        evidence["tdu_monthly"] = tdu_monthly_ext[2]
    tdu_passthrough = not bundled
    if bundled:
        notes.append("TDU delivery charges appear bundled into the Energy Charge (tdu_passthrough=False)")

    # --- ETF -------------------------------------------------------------#
    (etf_usd, etf_per_month), etf_conf, etf_ev = _extract_etf(text)
    confidence["etf"] = etf_conf
    if etf_ev:
        evidence["etf"] = etf_ev

    # --- renewable pct ---------------------------------------------------#
    renewable_pct = record("renewable_pct", _extract_renewable_pct(text))

    # --- average price sanity table --------------------------------------#
    avg_prices = _extract_avg_prices(text)
    for k, v in avg_prices.items():
        confidence[k] = 0.7
        evidence[k] = f"{v}c/kWh (from average-price table)"

    # --- bill credits ------------------------------------------------------#
    bill_credits = _extract_bill_credits(text)
    if bill_credits:
        confidence["bill_credits"] = 0.85
        evidence["bill_credits"] = f"{len(bill_credits)} tier(s) found"

    # --- buyback ----------------------------------------------------------#
    buyback, buyback_conf, buyback_ev = _extract_buyback(text, flat_ckwh)
    confidence["buyback"] = buyback_conf
    if buyback_ev:
        evidence["buyback"] = buyback_ev

    # --- assemble id -------------------------------------------------------#
    plan_id = slugify(f"{retailer}_{plan_name}_{term_months}mo")

    plan_dict: dict = {
        "id": plan_id,
        "retailer": retailer,
        "name": plan_name,
        "term_months": term_months,
        "base_charge_usd": base_charge,
        "energy_rates": energy_rates,
        "buyback": buyback,
        "bill_credits": bill_credits,
        "tdu_passthrough": tdu_passthrough,
        "etf_usd": etf_usd,
        "etf_per_month_remaining": etf_per_month,
        "rate_type": rate_type if rate_type in ("fixed", "variable", "indexed") else "fixed",
        "source": f"efl:{source_name}" if source_name else "efl:unknown",
        "notes": "; ".join(notes),
        "needs_review": False,
    }
    if renewable_pct is not None:
        plan_dict["renewable_pct"] = renewable_pct

    needs_review = any(confidence.get(k, 0.0) < 0.8 for k in LOAD_BEARING_KEYS if k in confidence)
    if any("multiple differing" in n for n in notes):
        needs_review = True
    plan_dict["needs_review"] = needs_review

    return DraftPlan(
        plan_dict=plan_dict, confidence=confidence, evidence=evidence, unparsed_notes=notes
    )


def parse_efl(pdf_path: str | Path) -> DraftPlan:
    pdf_path = Path(pdf_path)
    text = extract_text(pdf_path)
    return parse_efl_text(text, source_name=pdf_path.name)


def save_draft(draft: DraftPlan, drafts_dir: str | Path = DEFAULT_DRAFTS_DIR) -> Path:
    drafts_dir = Path(drafts_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    out = dict(draft.plan_dict)
    out["_parse"] = {
        "confidence": draft.confidence,
        "evidence": draft.evidence,
        "unparsed_notes": draft.unparsed_notes,
    }
    path = drafts_dir / f"{draft.plan_dict['id']}.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True)
    return path
