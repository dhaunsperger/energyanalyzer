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

import hashlib
import logging
import os
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

# Keys a draft YAML carries for the review UI that are NOT part of the Plan
# schema: `_parse` (per-field confidence/evidence/notes) and `_llm_suggested`
# (which fields a local LLM pre-filled, and its stated reasoning). Both must be
# stripped before `Plan.model_validate` or promotion into `plans/`.
DRAFT_META_KEYS = ("_parse", "_llm_suggested")


def plan_fields(raw: dict) -> dict:
    """A draft dict reduced to Plan-schema fields (review metadata removed)."""
    return {k: v for k, v in raw.items() if k not in DRAFT_META_KEYS}


# --------------------------------------------------------------------------- #
# DraftPlan
# --------------------------------------------------------------------------- #
@dataclass
class DraftPlan:
    plan_dict: dict
    confidence: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    unparsed_notes: list[str] = field(default_factory=list)
    # SHA-256 of the PDF this reading came from, when parsed from a file.
    # Carried onto the Plan at promote time so a later refresh can tell "the
    # document changed" from "the same document, read the same way again" --
    # the difference between a review worth doing and one already done.
    source_sha256: Optional[str] = None


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
# Whether `import pdfplumber` succeeds in this environment. Cached at module
# level (computed once, lazily) so we only ever pay the (slow, and in some
# environments noisy) import attempt a single time per process.
_PDFPLUMBER_AVAILABLE: Optional[bool] = None


def _pdfplumber_available() -> bool:
    """Attempt `import pdfplumber` exactly once, with the OS-level stderr fd
    (not just sys.stderr) redirected to devnull while doing so.

    pdfplumber pulls in pdfminer -> cryptography -> a Rust extension; in some
    environments (e.g. missing the `_cffi_backend` C extension) the import
    itself raises a pyo3_runtime.PanicException, which subclasses
    BaseException rather than Exception (so a plain `except Exception` would
    miss it) -- and, worse, the Rust panic hook prints a "thread '<unnamed>'
    panicked at ..." traceback directly to the process's stderr file
    descriptor via Rust's eprintln!, *before* the exception is even raised
    into Python. That means `contextlib.redirect_stderr` (which only retargets
    Python's `sys.stderr` object) cannot silence it -- only redirecting the
    real OS fd 2 for the duration of the import does.
    """
    global _PDFPLUMBER_AVAILABLE
    if _PDFPLUMBER_AVAILABLE is not None:
        return _PDFPLUMBER_AVAILABLE

    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_fd = os.dup(2)
    try:
        os.dup2(devnull_fd, 2)
        try:
            import pdfplumber  # noqa: F401,PLC0415

            _PDFPLUMBER_AVAILABLE = True
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            _PDFPLUMBER_AVAILABLE = False
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        os.close(devnull_fd)
    return _PDFPLUMBER_AVAILABLE


def extract_text(pdf_path: str | Path) -> str:
    """Extract raw text from an EFL PDF: pdfplumber first, pdftotext -layout
    fallback (poppler-utils, no python deps)."""
    pdf_path = Path(pdf_path)
    if _pdfplumber_available():
        try:
            import pdfplumber  # noqa: PLC0415

            # Some EFLs (e.g. the True Power "True Value" set) carry a slightly
            # corrupted FlateDecode stream; pdfminer recovers the full text but
            # logs a noisy "Data-loss while decompressing corrupted data" warning
            # per file. Extraction still succeeds, so silence that one logger for
            # the duration -- genuine unreadable-PDF failures raise, not warn, and
            # still fall through to the pdftotext fallback / ValueError below.
            _pdfminer_log = logging.getLogger("pdfminer")
            _prev_level = _pdfminer_log.level
            _pdfminer_log.setLevel(logging.ERROR)
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    pages = [p.extract_text() or "" for p in pdf.pages]
            finally:
                _pdfminer_log.setLevel(_prev_level)
            text = "\n".join(pages)
            if text.strip():
                return text
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # Fall through to the pdftotext subprocess fallback below.
            pass

    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ValueError(
            f"{pdf_path.name}: not a readable PDF (pdfplumber failed) and the "
            "pdftotext fallback (poppler-utils) is not installed"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{pdf_path.name}: not a readable PDF ({exc})") from exc
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


_DOLLAR_KWH = re.compile(r"\$\s*(\d*\.\d+)\s*(per\s*kwh|/\s*kwh)?", re.I)
_CENT_KWH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:¢|cents?)", re.I)

# A per-kWh price in dollars is always well under $1 -- Texas energy charges and
# buyback rates live around $0.03-$0.20/kWh. The "per kWh" suffix in _DOLLAR_KWH
# is optional (rates often sit in a table whose unit is only in the header), so
# without this bound ANY dollar amount in the search window parses as a rate:
# Champion's Free Weekends-24 read its "$250.00" early termination fee as a
# 25000c/kWh solar buyback and reported it at 0.85 confidence. Cap only applies
# when the unit is absent -- an explicit "per kWh" is trusted as written.
_MAX_UNITLESS_USD_PER_KWH = 1.0


def _rate_ckwh_from_snippet(s: str) -> Optional[float]:
    """Parse a rate expressed either as '$0.158 per kWh' or '15.8cents/15.8c'
    out of a short text snippet. Returns cents/kWh."""
    m = _DOLLAR_KWH.search(s)
    if m:
        usd = float(m.group(1))
        if m.group(2) or usd < _MAX_UNITLESS_USD_PER_KWH:
            return round(usd * 100, 4)
        return None
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


def _find_night_hours(text: str) -> list[int]:
    """Scan the whole document for a clock-time range whose immediate context
    mentions "night" (e.g. "Bright Nights hours are 11:00 PM to 06:00 AM.",
    a separate sentence from the rate table itself for brand-prefixed
    multi-tier plans like Chariot), or which states a free/no-charge period
    outright. Returns [] if none is found.

    The second phrasing matters because the footnote defining the window need
    not use the word "night" at all: Green Mountain's Pollution Free Nights
    marks its 0.00 tier with an asterisk and explains it as "*There is no charge
    applied to usage from 9:00 PM to 6:00 AM."
    """
    patterns = (
        r"[^\n.]{0,40}\bnight[^\n.]{0,80}",
        r"[^\n.]{0,40}\b(?:no|free)\s+charge[^\n.]{0,30}(?:applied\s+)?to\s+usage[^\n.]{0,60}",
        # A named free period, where the name carries the hours and the word
        # "night" never appears: Direct Energy's Twelve Hour Power prints
        # "0c per kWh - Designated Free Period (9:00 PM until 9:00 AM)".
        # The parenthesised range may wrap to the next line, so newlines are
        # allowed inside this one -- unlike the patterns above, which are
        # anchored on prose that stays on a single line.
        r"free\s+period[^)\n]{0,40}\([^)]{0,200}\)",
    )
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            hours = parse_time_range(_clean_window_fragment(m.group(0)))
            if hours:
                return hours
    return []


def _clean_window_fragment(fragment: str) -> str:
    """Drop rate cells and collapse whitespace before reading a clock range.

    A two-column layout can drop an unrelated cell INSIDE the phrase defining
    the window: Direct Energy's Twelve Hour Power renders as
    "Designated Free Period (9:00 <newline> 0c <newline> PM until 9:00 AM)",
    so the "9:00" and its "PM" are separated by a rate from the other column.
    Removing the amount and collapsing the whitespace restores
    "(9:00 PM until 9:00 AM)" and the range parses.
    """
    return " ".join(re.sub(r"\d+(?:\.\d+)?\s*¢", " ", fragment).split())


_WEEKDAY_WORDS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


# An EFL may DEFINE what "weekend"/"weekday" means for this plan rather than
# leaving it to the Sat/Sun default -- e.g. Gexa "Free 3 Day Weekends":
#   "Weekends is defined as 12:00 AM Friday to 12:00 AM Monday, ..."
#   "Weekdays is defined as 12:01 AM Monday to 11:59 PM Thursday, ..."
# so Friday is a weekend day here. Parse that definition instead of assuming.
# The gap before "defined as" must not swallow the OTHER keyword. These EFLs
# print the rate rows immediately above the definitions, so the text runs
# "...0.0000 ¢ per kWh - Weekends Weekdays is defined as 12:01 AM Monday to
# 11:59 PM Friday": a permissive gap let a match start at the rate row's
# "Weekends", skip " Weekdays is " as filler, and return the WEEKDAY range as
# the weekend definition. Frontier's weekend came back as Mon-Fri and Gexa's
# ("Free 3 Day Weekends", genuinely Fri-Sun) as Mon-Thu -- which would have
# applied the free rate to weekdays and the full rate to the weekend.
# An EXTRA percentage credit stacked on top of the ordinary pricing -- TXU's
# "Cool Summer Bonus: an additional 25% credit on all Energy Charges" during
# Jul-Aug-Sep. Deliberately requires "additional": a bare "100% credit/discount
# on all Energy Charges" is the standard free-nights wording, which the parser
# already models as a 0.0 rate over the stated window. Audited over 243 EFLs:
# matches exactly one plan, and the plain free-nights clause is not caught.
_BONUS_CREDIT_RE = re.compile(
    r"additional\s+\d{1,3}\s*%\s*(?:credit|discount)", re.I
)

# The REP refusing to sell this plan to a home with rooftop solar. TXU's Free
# Nights & Cool Summer 12 puts it in a footnote: "Customers with electric
# vehicles, batteries, and/or solar panels are ineligible". Requires BOTH an
# ineligibility word and a solar/EV/battery word within one sentence, because
# "solar" alone is everywhere in these documents (renewable content, buyback
# terms, brand names) and would flag half the corpus.
_SOLAR_EXCLUSION_RE = re.compile(
    r"[^.\n]{0,200}?(?:ineligible|not eligible|excluded|exclusions? include|"
    r"not available to|do(?:es)? not qualify)[^.\n]{0,200}",
    re.I,
)
_SOLAR_SUBJECT_RE = re.compile(r"rooftop solar|solar panel|distributed generation|net meter", re.I)

# --------------------------------------------------------------------------- #
# TDU relief during a free window
# --------------------------------------------------------------------------- #
# The period label a free window is sold under. "free" is included for plans
# that label the row "Free Nights" rather than naming the period.
_FREE_PERIOD = r"(?:night|nighttime|night-time|weekend|free)"

# An explicit per-period delivery-charge row priced at zero. Two real layouts:
#   Frontier: "TDU Delivery Charges   0.0000 c per kWh - Weekends"
#   Green Mtn: "Oncor Electric Delivery Nighttime Delivery Charges   $0.00"
# The period may sit before or after the amount, so both orders are matched.
_ZERO_AMOUNT = r"(?:\$\s*0(?:\.0+)?|\b0(?:\.0+)?\s*[¢c])"
_TDU_ZERO_AFTER = re.compile(
    # Never cross a sentence boundary, and require a real zero AMOUNT (with a
    # currency unit) followed by a dash-qualified period. Without both guards
    # this matched a clock time inside Champion EV Saver's average-price
    # formula -- "...Delivery Charge per kWh)] / Monthly Usage. EV charging
    # hours are from 10:00 PM to 4:00 AM every night".
    r"delivery charges?[^.\n]{0,40}?" + _ZERO_AMOUNT + r"[^.\n]{0,25}?[-–—]\s*" + _FREE_PERIOD,
    re.I,
)
_TDU_ZERO_BEFORE = re.compile(
    _FREE_PERIOD + r"[a-z \-]{0,20}delivery charges?\s*[:]?\s*" + _ZERO_AMOUNT,
    re.I,
)
# Prose form (Ambit): "TDU Per kWh Delivery Charges will be credited for usage
# during the nighttime hours".
_TDU_CREDITED = re.compile(
    r"delivery charges?[^.\n]{0,80}?(?:will be |are )?credited[^.\n]{0,80}?" + _FREE_PERIOD,
    re.I,
)
# The same promise stated as a negative, and about a NAMED period rather than
# "night"/"weekend" (Direct Energy Twelve Hour Power: "the customer will not be
# billed for any TDU delivery charges during the Designated Free Period").
# Newlines allowed: this sentence is centred across two lines in that layout.
_TDU_NOT_BILLED = re.compile(
    r"(?:not be billed|no charge|will not (?:be )?(?:apply|charge))[^.]{0,80}?"
    r"delivery charges[^.]{0,60}?during[^.]{0,40}?(?:" + _FREE_PERIOD + r"|free period)",
    re.I,
)
# Explicit denial, which must beat every positive above (SoFed Free Energy
# Lunch: "delivery charges apply to all electricity usage, including
# electricity used during the Free Lunch Hour").
_TDU_APPLIES_ANYWAY = re.compile(
    r"delivery charges? apply to all[^.\n]{0,120}", re.I
)


# A zero price stated beside a NAMED free period, e.g. Direct Energy's
# "0c per kWh - Designated Free Period (9:00 PM until 9:00 AM)". The zero may
# sit either side of the label because the two are in different columns and the
# flattened text interleaves them.
_FREE_PERIOD_ZERO = re.compile(
    r"(?:\b0(?:\.0+)?\s*[¢c][^.\n]{0,60}?free period"
    r"|free period[^.\n]{0,60}?\b0(?:\.0+)?\s*[¢c])",
    re.I,
)


def _free_window_waives_tdu(text: str) -> tuple[bool, str]:
    """Does this EFL waive the TDU per-kWh charge inside its free window?

    Returns ``(waived, evidence)``. An explicit "charges apply to all usage"
    wins over any positive signal: a REP that spells out that delivery charges
    still apply is answering exactly this question.
    """
    flat = " ".join((text or "").split())
    denial = _TDU_APPLIES_ANYWAY.search(flat)
    if denial:
        return False, ""
    for pattern in (_TDU_ZERO_BEFORE, _TDU_ZERO_AFTER, _TDU_CREDITED, _TDU_NOT_BILLED):
        m = pattern.search(flat)
        if m:
            return True, m.group(0).strip()[:150]
    return False, ""


_DAY_DEFN_RE = re.compile(
    r"\b(weekend|weekday)s?\b(?:(?!week)[^.\n]){0,20}?defined\s+as\s+"
    r"(\d{1,2}:\d{2}\s*[ap]\.?\s*m\.?)\s+"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+"
    r"(?:to|through|until|-)\s+"
    r"(\d{1,2}:\d{2}\s*[ap]\.?\s*m\.?)\s+"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
    re.I,
)


def _is_midnight(t: str) -> bool:
    """True for a '12:00 AM' style time (the end of a day-range that ends at
    midnight is EXCLUSIVE of that day)."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap])", t.strip(), re.I)
    if not m:
        return False
    hour, minute, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    return ap == "a" and hour in (0, 12) and minute == 0


def _day_span(start: int, end: int, end_exclusive: bool) -> list[int]:
    """Weekdays (0=Mon..6=Sun) from `start` to `end` cyclically, dropping `end`
    itself when the range ends at midnight of that day."""
    days: list[int] = []
    d = start
    for _ in range(8):  # safety cap
        days.append(d)
        if d == end:
            break
        d = (d + 1) % 7
    if end_exclusive and len(days) > 1:
        days = days[:-1]
    return sorted(set(days))


def _defined_weekdays(full_text: str, kind: str) -> Optional[list[int]]:
    """Weekday list from an EFL's explicit '<Weekends|Weekdays> is defined as
    <time> <day> to <time> <day>' clause, or None when there's no such clause."""
    if not full_text:
        return None
    for m in _DAY_DEFN_RE.finditer(full_text):
        if m.group(1).lower() != kind:
            continue
        start_day = _WEEKDAY_WORDS.get(m.group(3).lower())
        end_day = _WEEKDAY_WORDS.get(m.group(5).lower())
        if start_day is None or end_day is None:
            continue
        return _day_span(start_day, end_day, _is_midnight(m.group(4)))
    return None


def _weekdays_from_snippet(s: str, full_text: str = "") -> list[int]:
    low = s.lower()
    if "weekend" in low or ("saturday" in low and "sunday" in low and "monday" not in low):
        # Prefer the EFL's own "Weekends is defined as ..." clause; fall back to
        # the Sat/Sun default only when the label isn't explicitly defined.
        return _defined_weekdays(full_text, "weekend") or [5, 6]
    if "weekday" in low or re.search(r"monday\s*(through|-|to)\s*friday", low):
        return _defined_weekdays(full_text, "weekday") or [0, 1, 2, 3, 4]
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
# Header-block scan: retailer name + plan name.
#
# Real-corpus finding: almost none of these EFLs use an explicit "Retailer
# Name:"/"Plan Name:" label -- instead the PUCT-mandated template puts the
# retailer on the line right after the "Electricity Facts Label" heading
# (or inline with it, separated by "|"/"•") and the plan name on the
# line after that, e.g.:
#   "Electricity Facts Label | BKV Energy\nDaisy 12 - Fixed Rate ... Plan\n"
#   "Electricity Facts Label\nBudget Power\nNo Gimmicks 12 - Oncor\n"
# `pdftotext -layout` on multi-column EFLs (contact-info sidebar next to the
# header block) interleaves noise lines ("P: 555-555-5555", "E: x@y.com",
# taglines, dates, bare TDU names) between the two real lines we want, so we
# scan several lines and skip anything that looks like that noise.
# --------------------------------------------------------------------------- #
_EFL_HEADING = re.compile(r"Electricity\s*Facts\s*Label\b(?:[\s\-|•]*\(?EFL\)?)?", re.I)

_HEADER_LEADING_JUNK = re.compile(
    r"^(?:Residential\s*Service\s*(?:⇒|=>|->|-)\s*|By\s*Phone:?\s*[\d.\-() ]{5,20}\s*)",
    re.I,
)
_HEADER_TRAILING_JUNK = re.compile(
    r"\s*[•,]?\s*(?:PUCT?|REP)\s*(?:Cert(?:ification)?\.?)?\s*#?\s*[\dA-Za-z]+\s*$"
    r"|\s*DATE\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$",
    re.I,
)
_HEADER_CONTACT_PREFIX = re.compile(r"^[A-Za-z]{1,2}:\s")
_HEADER_TAGLINE = re.compile(r"^(?:We[’']re Here To Help!?|Adding Header)\s*$", re.I)
_HEADER_DATE_LINE = re.compile(
    r"^(?:Date:?\s*)?\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$"
    r"|^[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}$"
    r"|^\d{1,2}-[A-Za-z]+-\d{4}$",
    re.I,
)
_HEADER_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_HEADER_URL = re.compile(r"https?://|www\.|\.com\b", re.I)
_HEADER_TDU_ONLY_LINE = re.compile(
    r"^(?:Oncor|CenterPoint(?:\s*Energy)?|AEP\s*Texas(?:\s*(?:Central|North))?|TNMP)"
    r"(?:\s+Electric(?:\s+Delivery)?)?(?:\s+service\s*area)?\.?$",
    re.I,
)


def _header_line_or_none(raw: str) -> Optional[str]:
    """Strip known leading junk from a candidate header line, then return it
    unless what's left is itself just noise (contact info, a tagline, a bare
    date, an email/URL, or a bare TDU name)."""
    s = _HEADER_LEADING_JUNK.sub("", raw).strip()
    if not s:
        return None
    if (
        _HEADER_CONTACT_PREFIX.match(s)
        or _HEADER_TAGLINE.match(s)
        or _HEADER_DATE_LINE.match(s)
        or _HEADER_EMAIL.search(s)
        or _HEADER_URL.search(s)
        or _HEADER_TDU_ONLY_LINE.match(s)
        or s in ("®", "™")
    ):
        return None
    return s


def _clean_header_field(s: str) -> str:
    s = re.sub(r"[®™]", "", s)
    s = _HEADER_TRAILING_JUNK.sub("", s)
    return _clean_name(s)


def _scan_efl_header(text: str) -> tuple[Optional[str], Optional[str], str, str]:
    """Find the retailer name and plan name from the two lines following the
    'Electricity Facts Label' heading, tolerating an inline retailer
    ("... Label | Retailer") and skipping interleaved contact-info noise.
    Returns (retailer_or_None, plan_name_or_None, retailer_evidence,
    plan_evidence)."""
    m = _EFL_HEADING.search(text)
    if not m:
        return None, None, "", ""

    first_line_tail, _, after = text[m.end() :].partition("\n")
    found: list[str] = []
    inline_m = re.match(r"\s*[|•]\s*(.+)", first_line_tail)
    if inline_m:
        line = _header_line_or_none(inline_m.group(1))
        if line:
            found.append(line)
        lines = after.split("\n")
    else:
        lines = (first_line_tail + "\n" + after).split("\n")

    for raw in lines[:12]:
        if len(found) >= 2:
            break
        line = _header_line_or_none(raw)
        if line:
            found.append(line)

    retailer = _clean_header_field(found[0]) if len(found) >= 1 else None
    plan_name = _clean_header_field(found[1]) if len(found) >= 2 else None
    ev_r = _snippet_text(found[0]) if len(found) >= 1 else ""
    ev_p = _snippet_text(found[1]) if len(found) >= 2 else ""
    return retailer or None, plan_name or None, ev_r, ev_p


# --------------------------------------------------------------------------- #
# Field extractors
# --------------------------------------------------------------------------- #
# Legal entity -> brand, applied to whatever `_extract_retailer` reads off the
# EFL. Texas REPs often issue EFLs under a license-holding entity whose name
# appears nowhere else the user would recognize, which makes a plan hard to place
# in the UI and -- worse -- stops `app.common._plan_supersedes` from matching the
# brand-named synthetic index row for the same plan (it compares retailer brand
# tokens, and "Light Energy" shares none with "Meter Energy").
#
# Keep this table SMALL and evidence-based: only add a pair when the EFL itself
# shows the connection. Light Energy, LLC issues EFLs whose plan names are
# literally "Meter Saver Plan" / "Meter Earner Plan" / "Meter Standard Plan",
# and meterplan.com is published by Meter Energy.
_RETAILER_ALIASES = {
    "light energy": "Meter Energy",
}


def _apply_retailer_alias(retailer: str) -> str:
    """Map a license-holding legal entity to the brand customers shop under."""
    key = re.sub(r"[^a-z ]+", "", (retailer or "").lower()).strip()
    for legal, brand in _RETAILER_ALIASES.items():
        if key.startswith(legal):
            return brand
    return retailer


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
    ]
    result = _first_match(text, patterns)
    if result is not None:
        return result
    retailer, _plan_name, ev_r, _ev_p = _scan_efl_header(text)
    if retailer:
        return retailer, 0.75, ev_r
    return None


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
    result = _first_match(text, patterns)
    if result is not None:
        return result
    _retailer, plan_name, _ev_r, ev_p = _scan_efl_header(text)
    if plan_name:
        return plan_name, 0.7, ev_p
    return None


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
            # Allows a stray repeated unit symbol between value and "per",
            # e.g. two-column table artifacts like "Base Charge $0.00 $ per
            # bill month". "Base Monthly Charge" is the same field under a
            # different word order (Heritage, Chariot's brand-prefixed tables),
            # and the "$" is optional because some REPs omit it and let the
            # column header carry the unit (Octopus: "Base Charge: 0.00 per
            # month"). The trailing "per <period>" anchor keeps this from
            # matching a per-kWh rate.
            re.compile(
                r"Base\s*(?:Monthly\s*)?(?:Charge|Fee)\**\s*[:\-]?\s*\$?\s*(\d+(?:\.\d+)?)\s*\$?\s*per\s*"
                r"(?:billing\s*cycle|bill\s*month|month)",
                re.I,
            ),
            0.95,
            lambda m: float(m.group(1)),
        ),
        (
            # Reversed unit/value order, e.g. "Base Charge per month: $0.00" or
            # "Base Charge: Per Month ($) $9.95"
            re.compile(
                r"Base\s*Charge\**\s*:?\s*Per\s*Month\s*\(?\$?\)?\s*:?\s*\$?\s*(\d+(?:\.\d+)?)",
                re.I,
            ),
            0.85,
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
        (
            # Brand-prefixed table style explicitly stating no base charge, e.g.
            # "Chariot Energy Base Monthly Charge N/A per billing cycle"
            re.compile(r"Base\s*(?:Monthly\s*)?Charge\s*N/?A\b", re.I),
            0.85,
            lambda m: 0.0,
        ),
        (
            # Numbered-list style with reversed unit/value order, e.g.
            # "2) Base Charge ($) per month: $0.00"
            re.compile(r"Base\s*Charge\s*\(\$\)\s*per\s*month\s*:?\s*\$?\s*(\d+(?:\.\d+)?)", re.I),
            0.85,
            lambda m: float(m.group(1)),
        ),
        (
            # Average-price-table row style, e.g. "Base Charge($ per month) $ 0.00"
            re.compile(r"Base\s*Charge\s*\([^)]*\)\s*\$?\s*(\d+(?:\.\d+)?)\b", re.I),
            0.85,
            lambda m: float(m.group(1)),
        ),
        (
            # A ZERO minimum-usage fee is an affirmative statement that this plan
            # has no unconditional monthly charge (Constellation: "Minimum Usage
            # Fee 0.00000 $ per bill month"; Heritage: "Minimum Usage Charge: $0
            # per billing cycle < 0 kWh"). Matched only at zero, and last, on
            # purpose: a NON-zero minimum-usage fee is a conditional charge that
            # applies below a usage threshold, NOT a base charge, and must never
            # be read as one. Anything nonzero falls through to the "not found"
            # path so a human decides.
            re.compile(
                r"Minimum\s*Usage\s*(?:Fee|Charge)\s*[:\-]?\s*\$?\s*0(?:\.0+)?\s*\$?\s*(?:per|<)",
                re.I,
            ),
            0.85,
            lambda m: 0.0,
        ),
    ]
    return _first_match(text, patterns)


_TDU_MARK = r"(?:TDU|TDSP|Oncor|CenterPoint(?:\s*Energy)?|AEP(?:\s*Texas)?|TNMP)"

# Combined single-line TDU charge, e.g. "3) Energy Delivery Charges: 6.1196c
# per kWh and $4.06 per month" (AP Gas & Electric numbered-list style).
_TDU_COMBINED_LINE = re.compile(
    r"(?:TDU|TDSP|Energy)\s*Delivery\s*Charges?\s*:?\s*(\d+(?:\.\d+)?)\s*¢?\s*per\s*kWh\s*"
    r"and\s*\$\s*(\d+(?:\.\d+)?)\s*per\s*month",
    re.I,
)


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
        or re.search(
            r"included\s*in\s*(?:the\s*)?(?:variable\s*rate|energy\s*charge)",
            text,
            re.I,
        )
    )

    combined = _TDU_COMBINED_LINE.search(text)
    if combined:
        ev = _snippet(combined)
        ckwh: Optional[Extraction] = (float(combined.group(1)), 0.9, ev)
        monthly: Optional[Extraction] = (float(combined.group(2)), 0.9, ev)
        return ckwh, monthly, bundled

    ckwh = _first_match(
        text,
        [
            (
                re.compile(rf"{_TDU_MARK}\s*Deliver\w*\s*Charges?\s*\$\s*(\d+(?:\.\d+)?)\s*per\s*kWh", re.I),
                0.9,
                lambda m: float(m.group(1)) * 100,
            ),
            (
                # compact cent-style, arbitrary charge-type word, e.g.
                # "Pass-Through TDSP Distribution Charge: 6.1196c/kWh"
                re.compile(
                    rf"{_TDU_MARK}[^\n$]{{0,40}}?Charges?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*¢\s*/?\s*(?:per\s*)?kWh",
                    re.I,
                ),
                0.85,
                lambda m: float(m.group(1)),
            ),
        ],
    )
    monthly = _first_match(
        text,
        [
            (
                re.compile(
                    rf"{_TDU_MARK}\s*Deliver\w*\s*Charges?\s*\$\s*(\d+(?:\.\d+)?)\s*per\s*(?:billing\s*cycle|month)",
                    re.I,
                ),
                0.9,
                lambda m: float(m.group(1)),
            ),
            (
                # arbitrary charge-type word, e.g. "Pass-Through TDSP Customer
                # Charge: $4.06 per month" / "Oncor Delivery Charges $4.06 per
                # billing cycle"
                re.compile(
                    rf"{_TDU_MARK}[^\n$]{{0,40}}?Charges?\s*[:\-]?\s*\$\s*(\d+(?:\.\d+)?)\s*per\s*"
                    r"(?:billing\s*cycle|month)",
                    re.I,
                ),
                0.85,
                lambda m: float(m.group(1)),
            ),
        ],
    )

    if ckwh is None or monthly is None:
        generic = _generic_charge_rows(text)
        if ckwh is None:
            tdu_kwh_rows = [r for r in generic if r["kind"] == "kwh" and r["is_tdu"]]
            if tdu_kwh_rows:
                r = tdu_kwh_rows[0]
                ckwh = (r["value"], 0.6, r["evidence"])
        if monthly is None:
            tdu_month_rows = [r for r in generic if r["kind"] == "month" and r["is_tdu"]]
            if tdu_month_rows:
                r = tdu_month_rows[0]
                monthly = (r["value"], 0.6, r["evidence"])

    return ckwh, monthly, bundled


_ETF_PERMO_RE = re.compile(r"months?\s*remaining", re.I)
_ETF_LABEL_RE = re.compile(
    r"(?:do\s+i\s+have|is\s+there)\s+an?\s+(?:early\s+)?termination\s+fee|"
    r"termination\s+fee\s+or\s+any\s+fees?|"
    r"are\s+there\s+fees?\s+if\s+i\s+(?:choose\s+to\s+)?leave",
    re.I,
)


# A one-off charge that is a CONDITION of taking the plan, not a usage charge.
# Just Energy's family (Amigo, Tara, Just Energy) sells six 5-month "Sustainable
# / Bundle" plans whose EFL reads "One-time GoodBundle set up and carbon offset
# purchase: $49.99 ... required to enroll on this product". Those plans price
# their energy at 4.9c/kWh and rank near the top on that alone, so leaving a
# mandatory $49.99 out of the comparison flatters them against plans with no
# such fee.
#
# Deliberately narrow. It must say one-time AND name a setup/enrollment/purchase
# AND carry an amount, so it cannot swallow a conditional fee (a disconnection
# charge, a late fee) or the EFL's own note that 1/12 of the cost is baked into
# the average-price table.
_SIGNUP_FEE_RE = re.compile(
    r"one[-\s]?time[^.\n]{0,80}?"
    r"(?:set[-\s]?up|setup|enroll(?:ment)?|activation|sign[-\s]?up|purchase)"
    r"[^.\n]{0,80}?\$\s?(\d[\d,]*(?:\.\d{1,2})?)",
    re.I,
)
_SIGNUP_REQUIRED_RE = re.compile(r"required to enroll|must be purchased|is required", re.I)


# A usage-TIERED energy charge: the rate depends on how many kWh you used that
# month. Seen three ways in the corpus, all meaning the same thing:
#   "Energy Charge (0 to 1000 kWh): 8.8798c per kWh" / "(> 1000 kWh): 10.8798c"
#   "Energy Charge: (0 to 1000 kWh) 10.7798c per kWh" / "(> 1000 kWh) 5.7798c"
#   "0 - 1200 kWh 12.7000c" / "1201 - 2000 kWh 6.4000c" / "> 2000 kWh 13.3000c"
#
# The schema models ONE rate per window, so a tiered plan cannot be priced --
# a decision taken on measurement, not convenience (ARCHITECTURE.md section 11:
# every tiered plan in the corpus lands $674-$1,004 off the top ten, because
# what a plan pays for EXPORTS dominates any discount on imports here).
#
# What matters is that they fail LOUDLY. Picking one tier and carrying on is the
# dangerous outcome: Direct Apartment 12 grabbed its first tier, 8.8798c, which
# looks cheap and ranks high, and the note said only "multiple differing flat
# Energy Charge values found; used first" -- which reads like parser trouble
# rather than a plan we cannot price at all.
_TIER_BOUND_RE = re.compile(
    r"(?:^|[(\s])"
    r"(?:(0)\s*(?:to|-|–)\s*([\d,]+)"          # 0 to 1000 / 0 - 1200
    r"|(>|over|above)\s*([\d,]+)"                # > 1000
    r"|([\d,]+)\s*(?:to|-|–)\s*([\d,]+))"      # 1201 - 2000
    r"\s*kWh\s*\)?\s*:?\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*(?:¢|c\b|cents)",
    re.I,
)


def detect_usage_tiers(text: str) -> list:
    """Usage-tier brackets found in an EFL, as ``(label, rate_ckwh)``.

    Two or more distinct brackets means the plan is usage-tiered. One is just a
    rate that happens to mention a kWh bound, so it is not treated as tiered.
    """
    tiers: list = []
    seen: set = set()
    for m in _TIER_BOUND_RE.finditer(text or ""):
        if m.group(1) is not None:
            label = f"0-{m.group(2)} kWh"
        elif m.group(3) is not None:
            label = f">{m.group(4)} kWh"
        else:
            label = f"{m.group(5)}-{m.group(6)} kWh"
        try:
            rate = float(m.group(7))
        except (TypeError, ValueError):
            continue
        if label in seen:
            continue
        seen.add(label)
        tiers.append((label, rate))
    return tiers if len(tiers) >= 2 else []


def _extract_signup_fee(text: str) -> Extraction:
    """A mandatory one-off enrollment cost, e.g. a required carbon-offset purchase."""
    match = _SIGNUP_FEE_RE.search(text or "")
    if not match:
        return (None, 0.0, "")
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return (None, 0.0, "")
    if amount <= 0:
        return (None, 0.0, "")
    evidence = " ".join(match.group(0).split())[:160]
    # "required to enroll" nearby makes it unambiguous; without it the charge is
    # real but might be optional, so flag it for a human rather than assume.
    window = (text or "")[max(0, match.start() - 300) : match.end() + 300]
    confident = bool(_SIGNUP_REQUIRED_RE.search(window))
    return (amount, 0.9 if confident else 0.6, evidence)


def _extract_etf(text: str) -> Extraction:
    # "termination fee ... $X" on the same line - the common case.
    m = re.search(
        r"(?:termination\s*fee|early\s*termination\s*fee|ETF)[^\n$]{0,80}?\$\s*(\d+(?:\.\d+)?)",
        text,
        re.I,
    )
    if m:
        tail = text[m.end() : m.end() + 250]
        evidence = _snippet_text(text[m.start() : m.end() + 40])
        if _ETF_PERMO_RE.search(tail):
            return (float(m.group(1)), True), 0.9, evidence
        return (float(m.group(1)), False), 0.8, evidence
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)\s*per\s*month\s*remaining", text, re.I)
    if m:
        return (float(m.group(1)), True), 0.85, _snippet(m)

    # In a two-column EFL flattened by pdftotext -layout, the disclosure
    # chart's "Do I have a termination fee..." question label and its
    # "Yes, $X..." / "No" answer frequently land on different lines - and
    # the answer can be flattened either before or after the label, and
    # split across a newline the same-line regex above can't cross. Anchor
    # on the question label itself and search a bounded window around it.
    lm = _ETF_LABEL_RE.search(text)
    if lm:
        window_start = max(0, lm.start() - 250)
        window_end = min(len(text), lm.end() + 250)
        window = text[window_start:window_end]
        label_pos = lm.start() - window_start
        dollar_matches = list(re.finditer(r"\$\s*(\d+(?:\.\d+)?)", window))
        if dollar_matches:
            # Prefer a dollar amount that reads as a direct Yes/No answer
            # (e.g. "Yes. $99", "No. $0") over an incidental "$" elsewhere
            # in the window (e.g. a plan name like "a daily $0 energy
            # hour") that just happens to sit closer to the label.
            answer_like = [
                dm for dm in dollar_matches
                if re.search(r"(?:yes|no)\W{0,4}$", window[max(0, dm.start() - 15) : dm.start()], re.I)
            ]
            candidates = answer_like or dollar_matches
            best = min(
                candidates,
                key=lambda dm: min(abs(dm.start() - label_pos), abs(dm.end() - label_pos)),
            )
            val = float(best.group(1))
            context = window[max(0, best.start() - 60) : best.end() + 150]
            permo = bool(_ETF_PERMO_RE.search(context))
            evidence = _snippet_text(context)
            return (val, permo), 0.75, evidence
        if re.search(r"\bNo\b", window[max(0, label_pos - 30) : label_pos + 150]):
            return (0.0, False), 0.7, _snippet_text(window)

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
    r"(?:^|\n|:\s)\s*(?:\d+[.\)]\s*)?(?:[•*\-]\s*)?((?:[A-Za-z][A-Za-z\-]{0,20}[ \t]+){0,3})?"
    r"Energy\s*(?:Charge|Rate)\**\s*[:\-]?\s*([^\n]{0,80})",
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
        weekdays = _weekdays_from_snippet(snippet, text) if not hours else []
        if hours or weekdays:
            return {"hours": hours, "weekdays": weekdays, "evidence": _snippet(m)}
    return None


_CREDITED_WINDOW_RE = re.compile(
    r"credit\s+for\s+Energy\s+Charges?\s+resulting\s+from\s+energy\s+consumed\s+"
    r"during\s+(?:the\s+)?([A-Za-z][A-Za-z ]{1,20}?\bHours)",
    re.I,
)


def _extract_credited_window(text: str) -> Optional[dict]:
    """A window whose energy charge is credited back rather than called "free".

    The Amigo/Just Energy/Tara "Days Bundle" plans say "Your bill will contain a
    credit for Energy Charges resulting from energy consumed during Day Hours",
    and define the window separately as "Day Hours = 9:00 AM - 4:00 PM". Nothing
    on the document says "free", so :func:`_extract_free_window` -- which keys
    off that word -- never saw it, and the plans were modeled as billing the
    full rate for seven hours a day that cost nothing.

    Returns the same shape as `_extract_free_window`, or None.
    """
    # Match against whitespace-normalised text: both the credit sentence and the
    # window definition wrap mid-phrase in these PDFs ("Day Hours = 9:00 AM -"
    # with "4:00 PM" on the next line), so anything newline-sensitive sees only
    # half of each and silently finds nothing.
    flat = " ".join((text or "").split())
    m = _CREDITED_WINDOW_RE.search(flat)
    if not m:
        return None
    label = m.group(1).strip()
    defn = re.search(rf"{re.escape(label)}\s*(?:=|:|are|is)\s*([^.]{{4,50}})", flat, re.I)
    if not defn:
        return None
    hours = parse_time_range(defn.group(0))
    if not hours:
        return None
    return {"hours": hours, "weekdays": [], "evidence": defn.group(0).strip()[:120]}


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
        weekdays = [] if is_default else _weekdays_from_snippet(win_snip, text)
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


_SUFFIXED_TIER_RE = re.compile(
    r"Energy\s*Charge\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:¢|cents?)\s*per\s*kWh\s*"
    r"[–—-]\s*([A-Za-z][A-Za-z ]{2,20}?)(?=[\s.,]|$)",
    re.I,
)


def _extract_suffixed_energy_tiers(text: str) -> Optional[list[dict]]:
    """Rate rows whose qualifier TRAILS the rate rather than leading it:

        Energy Charge 17.6000 ¢ per kWh – Weekdays
        Energy Charge 0.0000 ¢ per kWh – Weekends

    (Frontier Free Weekends, Gexa Free 3 Day Weekends.) The brand-tier reader
    only recognizes a leading label, so these fell through to the flat-rate
    reader, which took the first row and modeled the WEEKDAY rate every day of
    the week -- the free weekend silently dropped.

    Returns rows as ``{"qualifier", "rate_ckwh", "weekdays", "evidence"}`` only
    when exactly two rows resolve to non-empty, non-overlapping day sets --
    anything less clear-cut is left to the callers' other readers.
    """
    rows = []
    for m in _SUFFIXED_TIER_RE.finditer(text):
        qualifier = m.group(2).strip()
        days = _weekdays_from_snippet(qualifier, text)
        rows.append(
            {
                "qualifier": qualifier,
                "rate_ckwh": float(m.group(1)),
                "weekdays": days,
                "evidence": _snippet(m),
            }
        )
    if len(rows) != 2:
        return None
    a, b = rows
    if not a["weekdays"] or not b["weekdays"]:
        return None
    if set(a["weekdays"]) & set(b["weekdays"]):
        return None  # overlapping definitions -- don't guess which wins
    if a["rate_ckwh"] == b["rate_ckwh"]:
        return None
    return rows


def _extract_brand_energy_tiers(text: str) -> list[dict]:
    """Detect multi-tier '<arbitrary label, possibly brand-prefixed> Energy
    Charge <rate>[c] per kWh' lines that don't use the standard
    On-Peak/Off-Peak/Mid-Peak/Shoulder vocabulary, e.g. Chariot Energy's:
    'Chariot Energy Daytime Energy Charge       6.78c      per kWh'
    'Chariot Energy Bright Nights Energy Charge       0c       per kWh'
    Returns a list of {"prefix", "rate_ckwh", "evidence"} (possibly empty).
    """
    # The rate may be given in cents ("0¢ per kWh") or dollars ("$0.00 per
    # kWh") -- Green Mountain's Pollution Free Nights prints its daytime tier in
    # cents and its night tier in dollars on the very same list. Only matching
    # cents found one tier, which is not a schedule, so the whole plan fell
    # through to the flat-rate reader and modeled the DAY rate around the clock.
    pat = re.compile(
        r"(?:^|\n)[ \t]*([A-Za-z][A-Za-z0-9&.'\- ]{0,60}?)\s+Energy\s*Charge\s*[:\-]?\s*"
        r"(?:\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:¢|cents?)?)\s*per\s*kWh",
        re.I,
    )
    rows = []
    for m in pat.finditer(text):
        usd, cents = m.group(2), m.group(3)
        rate = float(usd) * 100 if usd is not None else float(cents)
        rows.append({"prefix": m.group(1).strip(), "rate_ckwh": rate, "evidence": _snippet(m)})
    return rows


# --------------------------------------------------------------------------- #
# Generic "<label> Charge ... $X / X<c> per <unit>" table-line fallback --
# used when the specific, higher-confidence patterns above find nothing.
# Handles two real-world failure modes seen in the Texas EFL corpus:
#   1. pdftotext -layout mangles some fonts and drops specific letters
#      throughout the whole document (e.g. "Energy Charge" -> "nerg Charge",
#      "Base Charge" -> "ae Charge", "billing cycle" -> "illing ccle") --
#      but the literal word "Charge" itself never contains any of the
#      dropped letters, so it remains a reliable anchor.
#   2. Non-standard/brand-prefixed labels the specific regexes don't
#      recognize (e.g. "Chariot Energy Base Monthly Charge $9.95 per
#      billing cycle" -- "Base" isn't immediately followed by "Charge").
# --------------------------------------------------------------------------- #
_GENERIC_CHARGE_LINE = re.compile(
    r"(?:^|\n)[ \t]*([^\n$¢]{0,50}?)Charges?\**\s*[:\-]?\s*"
    r"(?:\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*¢)"
    r"\s*(?:/\s*)?per\s+([^\n$¢]{1,25})",
    re.I,
)

_TDU_MARK_WORDS = re.compile(r"\btdu\b|\btdsp\b|deliver|oncor|centerpoint|\baep\b|tnmp", re.I)


def _strip_pua(s: str) -> str:
    """Drop Unicode Private-Use-Area codepoints (U+E000-U+F8FF). Some EFL
    PDFs use subset fonts whose ToUnicode CMap maps a handful of glyphs
    (usually just a few specific letters) into the PUA instead of the real
    character, so pdftotext emits an invisible/unprintable codepoint exactly
    where that letter belongs -- e.g. "Delivery" comes out as "Deliver"
    with the 'y' silently replaced. Stripping them out lets substring/keyword
    classification (billing -> "illing", cycle -> "ccle", etc.) still work."""
    return "".join(ch for ch in s if not (0xE000 <= ord(ch) <= 0xF8FF))


# Unit markers per charge basis. "ccle"/"illing" are the broken-subset-font
# spellings of cycle/billing (see _strip_pua).
_UNIT_MARKERS = (
    ("kwh", ("kwh",)),
    ("day", ("day",)),
    # "ill"/"illing"/"ccle" are broken-subset-font spellings of bill/billing/
    # cycle. Atlantex prints "ae Charge $19.95 per ill" -- "Base Charge $19.95
    # per bill" -- which matched no unit at all, so its $19.95 base charge went
    # unread entirely and defaulted to $0.00.
    ("month", ("month", "cycle", "ccle", "illing", "ill")),
)


def _classify_unit_kind(unit_word: str) -> str:
    """Charge basis of a '... per <unit>' phrase, decided by which unit is named
    FIRST rather than by a fixed precedence.

    The captured phrase can name more than one unit, because a trailing
    qualifier gets swallowed: "Minimum Usage Charge: $0 per billing cycle < 0
    kWh" is a per-billing-cycle charge, but testing for "kwh" first classified
    it per-kWh -- a $0.00 ENERGY RATE, i.e. free electricity around the clock.
    It was latent (those EFLs resolve their rate by an earlier path) but became
    reachable once the generic scan's reads started scoring high enough to
    auto-promote. The unit immediately after "per" is the real basis.
    """
    u = _strip_pua(unit_word).lower()
    best, best_pos = "other", len(u) + 1
    for kind, markers in _UNIT_MARKERS:
        for marker in markers:
            i = u.find(marker)
            if i != -1 and i < best_pos:
                best, best_pos = kind, i
    return best


def _generic_charge_rows(text: str) -> list[dict]:
    """Return every '<label>Charge ... <amount> per <unit>' line in the
    document as {"prefix", "kind" ("kwh"/"day"/"month"/"other"), "value"
    (cents/kWh for kind=kwh, else USD), "is_tdu", "evidence"}."""
    rows = []
    for m in _GENERIC_CHARGE_LINE.finditer(text):
        prefix = m.group(1).strip()
        dollar, cents, unit = m.group(2), m.group(3), m.group(4)
        kind = _classify_unit_kind(unit)
        if kind == "kwh":
            value = float(cents) if cents is not None else float(dollar) * 100
        else:
            value = float(dollar) if dollar is not None else float(cents) / 100
        rows.append(
            {
                "prefix": prefix,
                "kind": kind,
                "value": value,
                "is_tdu": bool(_TDU_MARK_WORDS.search(prefix)),
                "evidence": _snippet(m),
            }
        )
    return rows


def _extract_daily_fee_as_base(text: str) -> Optional[Extraction]:
    """Prepaid plans sometimes state a per-day customer/base fee instead of a
    monthly base charge, e.g. 'Daily Customer Fee (DCF) $0.39 cents per day'
    or plainly 'Daily Charge $0.00 per day'. Convert to an approximate
    monthly figure: fee * 365 / 12, rounded to cents. Confidence is capped
    at 0.7 for a nonzero fee -- this is a derived, not stated, monthly
    figure -- but a stated $0 daily fee converts to an exact $0 monthly
    figure with no rounding error, so it gets a higher confidence."""
    m = re.search(
        r"Daily\s*(?:Customer\s*)?(?:Charge|Fee)(?:\s*\([^)]*\))?[^\n$]{0,45}"
        r"\$\s*(\d+(?:\.\d+)?)\s*(?:cents?)?\s*per\s*day",
        text,
        re.I,
    )
    if not m:
        return None
    daily = float(m.group(1))
    monthly = round(daily * 365 / 12, 2)
    conf = 0.9 if daily == 0 else 0.65
    # Some EFLs state the period equivalent alongside the daily rate -- Pronto
    # Power: "$0.39 cents per day ($11.70 per 30 days)". That doubles as a
    # check on our reading of an ambiguously-written figure ("$0.39 cents"):
    # if daily x N days matches their own total, the daily rate is confirmed and
    # only the day-count convention is left, which is a modelling choice rather
    # than a doubt about the document. We keep fee x 365/12 (a calendar-average
    # month) instead of their 30-day figure, so the difference is pennies.
    if daily:
        stated = re.search(
            r"\(\s*\$\s*(\d+(?:\.\d+)?)\s*(?:per|/)\s*(\d{2,3})\s*days?\s*\)",
            text[m.start() : m.end() + 80],
            re.I,
        )
        if stated and abs(daily * float(stated.group(2)) - float(stated.group(1))) < 0.02:
            conf = 0.85
    return monthly, conf, _snippet(m)


_PRICE_COMPONENTS_ANCHOR = re.compile(
    r"(?:based\s*on\s*the\s*following|following\s*components?\s*of\s*the\s*price"
    r"|includes\s*the\s*energy\s*charge\s*and\s*(?:tdu|tdsp)?\s*deliver\w*\s*charges?"
    # "The price you pay each month will consist of the Energy Charge, and ONCOR
    # (TDU) Delivery Charges." (Meter Energy) -- an exhaustive component list
    # just like the others, phrased as "consist of" rather than "includes".
    r"|(?:will\s*)?consists?\s*of\s*the\s*energy\s*charge[^.]{0,80}?deliver\w*\s*charges?"
    r"|utilizing\s*the\s*follo\w*\s*price\s*component)\s*:?",
    re.I,
)

# Labels a REP's fixed monthly charge goes by. Matched as a SUBSEQUENCE against
# the PUA-stripped prefix, because the EFLs that reach this point are usually the
# broken-font ones: a subset font with no ToUnicode mapping drops letters, so
# "Base Charge" arrives as "ae Charge" ("B" and "s" are Private-Use codepoints)
# and "Energy" as "nerg". Exact label matching can't see those, which left the
# value -- correctly read, right there in the row -- scored too low to promote.
_BASE_LABEL_WORDS = ("base", "basemonthly", "monthlybase", "customer", "monthlyservice", "minimum")
_ENERGY_LABEL_WORDS = ("energy", "electricity", "energycharge", "supply", "usage")


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _label_matches(prefix: str, words: tuple[str, ...]) -> bool:
    """True if a charge row's label plausibly names one of `words`.

    Two directions, because labels go wrong in two opposite ways:

    * SHORTER than the word -- a broken subset font drops letters, so "Base"
      arrives as "ae" and "Energy" as "nerg". Matched as a subsequence.
    * LONGER than the word -- the label is brand-prefixed or compound:
      "SmartEnergy Fixed Charge", "Base Usage Charge", "Chariot Energy Daytime".
      Matched as a substring.

    Requires >=2 surviving letters so a single stray character can't match
    everything. Which bucket a row lands in is decided by its UNIT before this
    is consulted (per-kWh rows are energy candidates, per-month rows base
    candidates), so this only has to identify the row, not classify it.
    """
    p = re.sub(r"[^a-z]", "", _strip_pua(prefix or "").lower())
    if len(p) < 2:
        return False
    return any(_is_subsequence(p, w) or w in p for w in words)


def _looks_like_base_label(prefix: str) -> bool:
    return _label_matches(prefix, _BASE_LABEL_WORDS)


def _looks_like_energy_label(prefix: str) -> bool:
    return _label_matches(prefix, _ENERGY_LABEL_WORDS)
_CHARGE_LINE = re.compile(r"^.{0,80}\b(?:Charge|Fee)\b.{0,80}$", re.I | re.M)


def _extract_base_charge_absent_from_itemized_list(text: str) -> Optional[Extraction]:
    """Some EFLs (e.g. Companion Energy, Frontier Utilities, Gexa Energy,
    Just Energy) itemize every price component in a short list right after
    the average-price table -- 'This price disclosure is based on the
    following: Energy Charge X per kWh / TDU Delivery Charges ...'. Texas
    EFLs must disclose all price components in this list, so if it has an
    Energy Charge line and only TDU/TDSP line(s) besides -- no separate REP
    base or customer charge line -- the REP genuinely charges no base fee,
    rather than the parser having simply failed to find one."""
    anchor = _PRICE_COMPONENTS_ANCHOR.search(text)
    if not anchor:
        return None
    window = text[anchor.end() : anchor.end() + 500]
    lines = _CHARGE_LINE.findall(window)
    if not lines:
        return None
    rep_lines = [ln for ln in lines if not re.search(_TDU_MARK, ln, re.I)]
    if not rep_lines:
        return None
    has_energy_charge = any(re.search(r"Energy\s*Charge", ln, re.I) for ln in rep_lines)
    has_base_or_customer = any(
        re.search(r"\b(?:Base|Customer|Monthly\s*Service)\b", ln, re.I) for ln in rep_lines
    )
    if has_energy_charge and not has_base_or_customer:
        return 0.0, 0.8, anchor.group(0) + " " + " / ".join(rep_lines[:3])
    return None


_COMPONENT_SENTENCE = re.compile(
    r"(?:price\s*you\s*pay|total\s*price|price\s*for\s*electric\w*\s*service)"
    r"[^.]{0,60}?(?:will\s*)?(?:consists?\s*of|includes?|is\s*made\s*up\s*of)\s*([^.]{0,220})\.",
    re.I,
)
_BASE_COMPONENT_WORDS = re.compile(
    r"\bbase\b|\bcustomer\s*charge|\bminimum\b|monthly\s*(?:service|fee)|\bmonthly\s*charge", re.I
)


def _extract_base_charge_from_component_sentence(text: str) -> Optional[Extraction]:
    """A Texas EFL that states its price components in prose is making an
    exhaustive disclosure, so a list that names only an energy charge and the
    TDU's delivery charges means the REP levies no fixed monthly charge.

    Meter Energy: "The price you pay each month will consist of the Energy
    Charge, and ONCOR (TDU) Delivery Charges." Distinct from
    :func:`_extract_base_charge_absent_from_itemized_list`, which scans the
    itemized block FOLLOWING its anchor -- Meter prints the items above the
    sentence and labels them "Energy Rate", so neither the direction nor the
    label vocabulary lines up. The sentence alone is the sounder signal: a plan
    that did have a base charge would have to name it here.
    """
    for m in _COMPONENT_SENTENCE.finditer(text):
        clause = m.group(1)
        if _BASE_COMPONENT_WORDS.search(clause):
            return None  # a base/customer charge IS one of the components
        has_energy = re.search(r"energy\s*(?:charge|rate)", clause, re.I)
        has_tdu = re.search(r"deliver|\btdu\b|\btdsp\b", clause, re.I)
        if has_energy and has_tdu:
            return 0.0, 0.85, _snippet(m)
    return None


_BASE_CHARGE_TRAILING_AMOUNT = re.compile(
    r"\b(?:monthly\s+)?Base\s+(?:\w+\s+){0,2}Charge\b[^.$]{0,40}?\bof\s*\$\s*(\d+(?:\.\d+)?)",
    re.I,
)


def _extract_base_charge_trailing_amount(text: str) -> Optional[Extraction]:
    """A base charge whose amount follows the unit rather than preceding it.

    Constellation writes the components as a numbered prose clause: "(iii) a
    monthly Base Electricity Charge per ESI-ID of $0.00". Every labeled reader
    expects "<label> ... $X per <unit>", so the amount was never found and the
    charge defaulted to $0.00 -- right by luck here, but unread.
    """
    m = _BASE_CHARGE_TRAILING_AMOUNT.search(text)
    return (float(m.group(1)), 0.85, _snippet(m)) if m else None


# "Base Charge: $9.95" with no period unit at all. Direct Energy prints the
# unit on some EFLs ("Base Charge: $9.95   per billing cycle" -- Free Days 12)
# and omits it on others (Twelve Hour Power 24), and every labeled reader
# requires the unit, so the charge silently defaulted to $0.00 -- understating
# the plan by ~$119/yr. Anchored tight: the amount must follow the label on the
# SAME line with only a colon and spaces between, which the "Price per kWh =
# (Base Charge + Energy Charge ..." formula line cannot satisfy.
_BASE_CHARGE_BARE_AMOUNT = re.compile(
    r"\bbase(?:\s+\w+){0,2}\s+charge\s*:?\s*\$\s*(\d+(?:\.\d+)?)(?!\s*(?:per|/)\s*k?wh)",
    re.I,
)


def _extract_base_charge_bare_amount(text: str) -> Optional[Extraction]:
    """A labeled base charge stated as a bare dollar amount, with no unit.

    Skips the DELIVERY utility's own base charge, which is written the same way
    and is not the REP's: Tesla's Drive 12M prints "Oncor Base Charge: $4.06
    /month" right above its energy rates, and reading that as the plan's base
    charge understates every other plan it is compared against.
    """
    for m in _BASE_CHARGE_BARE_AMOUNT.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if _TDU_MARK_WORDS.search(text[line_start : m.start()]):
            continue
        return (float(m.group(1)), 0.85, _snippet(m))
    return None


_BULLET_ITEM = re.compile(r"[•▪●]\s*([^•▪●\n]{3,160})")


def _extract_base_charge_absent_from_bullet_list(text: str) -> Optional[Extraction]:
    """A BULLETED price-component list with no REP monthly charge in it.

    Amigo/Tara/Just Energy bundle plans itemize as bullets:
        • Energy Charge: 7.3¢/kWh.
        • One-time GoodBundle set up and carbon offset purchase: $49.99.
        • Pass-Through TDSP Distribution Charge: 6.1196¢/kWh.
        • Pass-Through TDSP Customer Charge: $4.06 per month.
    Every recurring component is listed, and the only per-month charge is the
    TDSP's -- so the REP levies no base charge. Same reasoning as
    :func:`_extract_base_charge_absent_from_itemized_list`, but these EFLs open
    with "This price disclosure is based on the average usage levels above",
    which is not a components anchor; the bullet list itself is the structure.

    Note the TDSP line says "Customer Charge": it is excluded because it is
    TDU-marked, not because of its label.
    """
    items = [m.group(1).strip() for m in _BULLET_ITEM.finditer(text)]
    if len(items) < 2:
        return None
    if not any(re.search(r"energy\s*(?:charge|rate)", i, re.I) for i in items):
        return None
    if not any(_TDU_MARK_WORDS.search(i) for i in items):
        return None
    rep_items = [i for i in items if not _TDU_MARK_WORDS.search(i)]
    # A one-time/setup fee is not a recurring monthly charge.
    recurring = [i for i in rep_items if not re.search(r"one[\s-]*time|set\s*up|enrollment", i, re.I)]
    if any(_BASE_COMPONENT_WORDS.search(i) for i in recurring):
        return None  # the REP DOES levy a base charge; let a labeled reader find it
    return 0.0, 0.85, "; ".join(items[:4])[:200]


def _extract_all_kwh_rate(text: str) -> Optional[Extraction]:
    """Some EFLs (e.g. TXU) split the 'Energy Charge' header from its value
    across lines in a flattened table -- the header line has no inline
    number, and the rate instead appears on a following 'All kWh <rate>c'
    row.

    Confidence turns on whether an 'Energy Charge' header actually precedes the
    row. Anchored, this is unambiguous -- the whole TXU/Vistra template prints
    "Energy Charge: Per kWh (¢) Electricity All kWh 9.7000¢" -- and the old flat
    0.75 (below the review bar) sent every such plan to the LLM, and from there
    to the review queue under the assist-only policy, for a rate the parser had
    read correctly all along. Measured across the EFL corpus: all 8 documents
    using this row are header-anchored, and their rates check out against the
    documents. Unanchored, keep the cautious score -- a bare "All kWh" elsewhere
    could be anything.
    """
    for m in re.finditer(r"\bAll\s*kWh\s*(\d+(?:\.\d+)?)\s*¢", text, re.I):
        anchored = re.search(
            r"Energy\s*Charge[^\n]{0,120}$",
            " ".join(text[max(0, m.start() - 160) : m.start()].split()),
            re.I,
        )
        return float(m.group(1)), (0.85 if anchored else 0.75), _snippet(m)
    return None


_SPLIT_CHARGE_ROW = re.compile(
    r"(\d+(?:\.\d+)?)\s*¢\s*/\s*kWh\s+\$(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s*¢\s*/\s*kWh\s+\$(\d+(?:\.\d+)?)"
)

# The same table with N rate columns instead of one. Champion prints every plan
# this way: its own rate(s), then its base charge, then the TDU's two charges --
# and the TDU pair is ALWAYS last, which is what makes the row readable without
# parsing the interleaved headers:
#
#   single rate : "6.7¢/kWh $0.00 6.1196¢/kWh $4.06"
#   two rates   : "7.4¢/kWh 6.0¢/kWh $0.00 6.1196¢/kWh $4.06"   (EV Saver)
#                 "10.9¢/kWh 0.0¢/kWh $0.00 6.1196¢/kWh $4.06"  (Free Weekends)
#
# _SPLIT_CHARGE_ROW only ever matched one leading rate column, so on a two-rate
# plan it either matched at the SECOND rate (reading the discounted rate as the
# flat rate) or, because its header guard wants a literal "Energy Charge" that
# these variants don't print, fell through entirely -- EV Saver-12 and Free
# Weekends-24 both ended up at a flat 0.0¢/kWh catch-all, i.e. free electricity
# around the clock. Both had to be hand-entered, which a refresh would silently
# undo. Hence reading the columns properly.
_MULTI_RATE_CHARGE_ROW = re.compile(
    r"((?:\d+(?:\.\d+)?\s*¢\s*/\s*kWh\s+){2,})\$(\d+(?:\.\d+)?)\s+"
    r"(\d+(?:\.\d+)?)\s*¢\s*/\s*kWh\s+\$(\d+(?:\.\d+)?)"
)

# Column headers, split into the one that carries a time restriction and the one
# that's the everyday/default rate. Order of appearance in the header region
# tells us which data column is which -- Champion prints the general column
# first ("Daytime Hours | EV Charging Hours", "Weekdays | Weekends").
_RESTRICTED_COL = re.compile(r"EV\s*Charging|Weekend|Night|Free", re.I)
_GENERAL_COL = re.compile(r"Daytime|Weekday|Standard|Anytime|Energy\s*Charge", re.I)


def _extract_split_energy_base_row(text: str) -> Optional[tuple[float, float, str]]:
    """Some EFLs (e.g. Champion Energy) render Energy/Base/TDU charges as a
    4-column table whose headers sit 2-3 lines above a single data row, e.g.:
    'Energy Charge ... Base Charge ... per kWh per month\\n6.4c/kWh $0.00
    6.1196c/kWh $4.06' -- energy rate, base charge, TDU rate, TDU monthly in
    that column order. Returns (energy_ckwh, base_usd, evidence) only when
    both 'Energy Charge' and 'Base Charge' labels appear just above the row,
    to avoid matching an unrelated 4-number line."""
    for m in _SPLIT_CHARGE_ROW.finditer(text):
        context = text[max(0, m.start() - 200) : m.start()]
        if re.search(r"Energy\s*Charge", context, re.I) and re.search(r"Base\s*Charge", context, re.I):
            return float(m.group(1)), float(m.group(2)), _snippet(m)
    return None


def _extract_multi_rate_charge_row(text: str) -> Optional[dict]:
    """Read a REP/TDU split charge row carrying MORE than one rate column.

    Returns ``{"rates": [ckwh, ...], "base_usd", "tdu_ckwh", "tdu_usd",
    "restricted_index", "evidence"}``, or None. ``restricted_index`` is the
    position of the time-restricted column (the free/EV/weekend rate) when the
    headers identify it, else None -- callers must not guess a window without it.

    The row is only accepted when a 'Base Charge' label sits above it, so an
    unrelated run of numbers can't match.
    """
    for m in _MULTI_RATE_CHARGE_ROW.finditer(text):
        header = text[max(0, m.start() - 320) : m.start()]
        # The row's LAST two columns are the utility's, so the header must name
        # the delivery utility -- that, plus a charge/per-month label, is what
        # distinguishes this table from an unrelated run of numbers. Requiring a
        # contiguous "Base Charge" does not work: Champion's headers interleave
        # across lines ("Base per kWh per month ... Usage Charge Usage Charge
        # Charge"), so the words are present but never adjacent.
        if not re.search(r"deliver", header, re.I):
            continue
        if not re.search(r"charge", header, re.I) or not re.search(r"per\s*month", header, re.I):
            continue
        rates = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*¢", m.group(1))]
        if len(rates) < 2:
            continue

        # Which data column is the restricted one? Take the LAST occurrence of
        # each header keyword: the header region also contains prose above the
        # table ("...30% of usage occurs during Weekends...") whose word order
        # doesn't reflect the columns.
        restricted = list(_RESTRICTED_COL.finditer(header))
        general = list(_GENERAL_COL.finditer(header))
        restricted_index = None
        restricted_label = ""
        if restricted and general and len(rates) == 2:
            restricted_index = 1 if restricted[-1].start() > general[-1].start() else 0
            restricted_label = restricted[-1].group(0).strip()

        return {
            "rates": rates,
            "base_usd": float(m.group(2)),
            "tdu_ckwh": float(m.group(3)),
            "tdu_usd": float(m.group(4)),
            "restricted_index": restricted_index,
            "restricted_label": restricted_label,
            "evidence": _snippet(m),
        }
    return None


def _restricted_window(text: str, label_hint: str) -> Optional[dict]:
    """The window for a restricted rate column, read from the EFL's own prose
    ("EV charging hours are from 10:00 PM to 4:00 AM every night."; "Weekend
    hours are all day Saturday and Sunday"). None when it isn't stated."""
    low = (label_hint or "").lower()
    if "weekend" in low or "weekday" in low:
        days = _weekdays_from_snippet(label_hint, text)
        return {"weekdays": days} if days else None
    for m in re.finditer(r"[^\n.]{0,60}(?:EV\s*charging|free|night)[^\n.]{0,90}", text, re.I):
        hours = parse_time_range(m.group(0))
        if hours:
            return {"hours": hours}
    return None


_BASE_THEN_KWH_HEADER = re.compile(r"Base\s*Charge\s+Per\s*kWh\s*Charge", re.I)
_BASE_THEN_KWH_ROW = re.compile(r"^(.{0,40}?)\$(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s*¢", re.M)


def _extract_base_then_kwh_row(text: str) -> Optional[tuple[float, float, str]]:
    """Some EFLs (e.g. NEC Co-op Energy) render pricing as a 'Charge details
    | Base Charge | Per kWh Charge' table with one labeled row per provider,
    e.g. 'NEC Co-op Energy $7.50 9.94c' / 'Delivery Costs - Oncor $4.06
    6.1196c' -- base charge ($) then per-kWh rate (c), in that column order.
    Returns (energy_ckwh, base_usd, evidence) for the first row whose label
    isn't the TDU/delivery row."""
    header = _BASE_THEN_KWH_HEADER.search(text)
    if not header:
        return None
    for m in _BASE_THEN_KWH_ROW.finditer(text, header.end()):
        label = m.group(1).strip(" \t-")
        if not label or re.search(_TDU_MARK + r"|Delivery", label, re.I):
            continue
        return float(m.group(3)), float(m.group(2)), _snippet(m)
    return None


def _extract_variable_rate(text: str) -> Optional[Extraction]:
    """Variable/prepaid plans without a labeled 'Energy Charge' line often
    state the rate in prose, e.g. 'included in variable rate of 17.9 cents'."""
    patterns = [
        (
            re.compile(r"variable\s*rate\s*of\s*(\d+(?:\.\d+)?)\s*cents?", re.I),
            0.7,
            lambda m: float(m.group(1)),
        ),
    ]
    return _first_match(text, patterns)


def _extract_flat_avg_price_row(text: str) -> Optional[Extraction]:
    """Last-resort: a TDU-labeled row of 3-4 repeated identical average
    prices (500/1000/2000 kWh columns all equal) implies a flat, usage-tier
    -independent variable rate, e.g. 'ONCOR 17.9c 17.9c 17.9c 17.9c'."""
    m = re.search(
        r"\b(?:Oncor|CenterPoint|AEP|TNMP)\b\s+(\d+(?:\.\d+)?)\s*¢\s+(\d+(?:\.\d+)?)\s*¢\s+"
        r"(\d+(?:\.\d+)?)\s*¢(?:\s+(\d+(?:\.\d+)?)\s*¢)?",
        text,
        re.I,
    )
    if not m:
        return None
    vals = [float(g) for g in m.groups() if g]
    if len(set(vals)) == 1:
        return vals[0], 0.55, _snippet(m)
    return None


def _extract_bill_credits(text: str) -> list[dict]:
    """Usage-tiered bill credits: 'a $X credit when usage >= Y kWh [but < Z
    kWh]', in whatever word order the retailer uses. Real EFLs phrase this
    disclosure several distinct ways for the same tier -- each pattern below
    covers one order seen in the corpus. Non-positive "credits" (a $0
    placeholder row seen in some average-price disclosure tables) are
    dropped as noise, not real tiers.
    """
    out: list[dict] = []
    seen: set[tuple[float, Optional[float], float]] = set()

    def _add(credit: float, min_kwh: float, max_kwh: Optional[float]) -> None:
        if credit <= 0:
            return
        key = (min_kwh, max_kwh, credit)
        if key in seen:
            return
        seen.add(key)
        out.append({"min_kwh": min_kwh, "max_kwh": max_kwh, "credit_usd": credit})

    # "credit of $125 ... when/if usage is at least 1,000 kWh [but less than 2,000 kWh]"
    for m in re.finditer(
        r"(?:bill\s*)?credit\s*of\s*\$(\d+(?:\.\d+)?)\s*(?:will\s*be\s*applied\s*)?"
        r"(?:when|if)\s*(?:your|the customer'?s|monthly)?\s*usage\s*is\s*"
        r"(?:at\s*least|>=|greater\s*than\s*or\s*equal\s*to)\s*(\d+(?:,\d{3})*)\s*kWh"
        r"(?:\s*(?:but|and)\s*(?:less\s*than|<)\s*(\d+(?:,\d{3})*)\s*kWh)?",
        text,
        re.I,
    ):
        _add(
            float(m.group(1)),
            float(m.group(2).replace(",", "")),
            float(m.group(3).replace(",", "")) if m.group(3) else None,
        )

    # "$125 credit when/if usage is >= 1,000 kWh [in a billing cycle]"
    for m in re.finditer(
        r"\$(\d+(?:\.\d+)?)\s*credit\s*(?:will\s*be\s*applied\s*)?"
        r"(?:when|if)\s*(?:your|the customer'?s|monthly)?\s*usage\s*is\s*"
        r"(?:at\s*least|>=|greater\s*than\s*or\s*equal\s*to)\s*(\d+(?:,\d{3})*)\s*kWh",
        text,
        re.I,
    ):
        _add(float(m.group(1)), float(m.group(2).replace(",", "")), None)

    # "Usage Credit $125 per billing cycle when usage >=1000 kWh"
    for m in re.finditer(
        r"Usage\s*Credit[:\s]*\$(\d+(?:\.\d+)?)\s*(?:per\s*(?:billing\s*cycle|month)\s*)?"
        r"when\s*usage\s*(?:is\s*)?(?:>=|at\s*least|greater\s*than\s*or\s*equal\s*to)\s*"
        r"(\d+(?:,\d{3})*)\s*kWh",
        text,
        re.I,
    ):
        _add(float(m.group(1)), float(m.group(2).replace(",", "")), None)

    # "Usage Credit $125.00 per billing cycle for usage (>=1000) kWh" -- the
    # Gexa / Frontier / Discount Power price-table row. It states the threshold
    # as "for usage (>=N)" with the comparator in parentheses, rather than the
    # "when usage >= N" the patterns above expect. Missing it silently dropped
    # credits worth $50-$125 PER MONTH from 9 plans, 5 of which had already
    # auto-promoted -- the rest of those EFLs parse confidently, so nothing
    # flagged them. Found 2026-07-24 while reviewing the draft queue.
    for m in re.finditer(
        r"Usage\s*Credit[:\s]*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*\$?\s*"
        r"(?:per\s*(?:billing\s*cycle|bill\s*month|month)\s*)?"
        r"for\s*usage\s*\(?\s*(?:>=|=>|≥)\s*(\d+(?:,\d{3})*)\s*\)?\s*kWh",
        text,
        re.I,
    ):
        _add(float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")), None)

    # Prose variant: "A Usage Credit of $50.00 will be included for each billing
    # cycle when your usage on this plan is above or equal to 500 kWh."
    # ("above or equal to", plus filler between "usage" and "is".)
    for m in re.finditer(
        r"(?:usage|bill)\s*credit\s*of\s*\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)"
        r"[^.]{0,120}?usage\b[^.]{0,40}?\bis\s*"
        r"(?:above|greater\s*than)\s*or\s*equal\s*to\s*(\d+(?:,\d{3})*)\s*kWh",
        text,
        re.I,
    ):
        _add(float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")), None)

    # "Usage Credit for 1,000 kWh or more: $125"
    for m in re.finditer(
        r"Usage\s*Credit\s*for\s*(\d+(?:,\d{3})*)\s*kWh\s*or\s*more[:\s]*\$(\d+(?:\.\d+)?)",
        text,
        re.I,
    ):
        _add(float(m.group(2)), float(m.group(1).replace(",", "")), None)

    # "[Additional] Residential Usage Credit 35.00 $ per bill month if usage
    # >= 1000kWh" -- reversed value/$ order, "if" instead of "when". A plan
    # with a second, higher-threshold line of this form (e.g. "Additional
    # ... Credit ... if usage >= 2000kWh") is a genuinely cumulative/stacking
    # credit, not a replacement tier: min_kwh open-ended (max_kwh=None) on
    # both rows is correct, since cost.py sums every bill_credits row whose
    # min_kwh the month's usage clears.
    for m in re.finditer(
        r"Usage\s*Credit\s*(\d+(?:\.\d+)?)\s*\$\s*per\s*(?:bill\s*month|billing\s*cycle|month)\s*"
        r"(?:when|if)\s*usage\s*(?:is\s*)?(?:>=|at\s*least|greater\s*than\s*or\s*equal\s*to)\s*"
        r"(\d+(?:,\d{3})*)\s*kWh",
        text,
        re.I,
    ):
        _add(float(m.group(1)), float(m.group(2).replace(",", "")), None)

    return out


# "E?xport Credit Rate" is deliberate: several REPs (Atlantex, Chariot) label the
# export credit that way, and the corrupted-font PDFs in this corpus render the
# capital E as an invisible Private-Use-Area codepoint that extract_text strips,
# leaving a bare "xport Credit Rate". Verified safe across all 200 downloaded
# EFLs: every occurrence of "credit rate" is an export/excess-energy credit.
# "Renewable Rewards Credit" is Green Mountain's brand name for its solar export
# credit ("You will receive a Renewable Rewards Credit on your bill for the excess
# energy delivered by your eligible renewable energy system to the grid").
# Missing it made the parser report buyback=none at high confidence on a plan
# literally named "Renewable Rewards Solar Credit 12" -- found by the LLM audit
# (scripts/audit_plans_llm.py), which is exactly the silent-wrong class that
# audit exists to catch. The (R) is optional because the glyph survives some
# text extractions and not others.
_BUYBACK_LABEL = re.compile(
    r"(Solar\s*(?:Repurchase|Buyback)|Buy\s*Back\s*Rate|Excess\s*Energy\s*(?:Credit|Rate|"
    r"Purchase)|Renewable\s*Buyback|Solar\s*Grid\s*Credit|E?xport\s*Credit\s*Rate"
    r"|Renewable\s*Rewards\s*(?:®|\(R\))?\s*Credit)[^\n]{0,100}",
    re.I,
)


# The PUCT-mandated disclosure line every EFL carries. When a REP answers YES
# here but no buyback rate label resolves, the parser must NOT confidently
# report "no buyback" -- the document itself says otherwise. Answer text is
# often wrapped onto following lines, so a generous window is scanned and only
# an unambiguous leading yes/no counts.
_BUYBACK_DISCLOSURE_RE = re.compile(
    r"purchase\s+excess\s+distributed\s+renewable(?:\s+generation)?\s*\??(.{0,120})",
    re.I | re.S,
)


def _buyback_disclosure_answer(text: str) -> Optional[bool]:
    """True/False if the EFL's excess-generation disclosure clearly answers
    yes/no, else None (wrapped/absent/unparseable answer -- most EFLs)."""
    m = _BUYBACK_DISCLOSURE_RE.search(text)
    if not m:
        return None
    tail = re.sub(r"\s+", " ", m.group(1)).strip()
    if re.match(r"(?i)\W*yes\b", tail):
        return True
    if re.match(r"(?i)\W*no\b", tail):
        return False
    return None


_BUYBACK_HEDGE = re.compile(
    r"please\s*(?:inquire|contact|call|visit)|may\s*be\s*available|eligib(?:le|ility)|enrolled",
    re.I,
)


# A hedged buyback mention means one of two very different things, and treating
# them alike is wrong in opposite directions:
#
#   PLAN-GATED   "Yes, for solar buy-back plans only" (Abundance); "for homeowners
#                enrolled on an eligible TXU Energy solar buyback plan" (TXU).
#                Buyback requires switching to a DIFFERENT product, so for the
#                plan this EFL describes, kind=none is correct and confident.
#
#   ATTACHABLE   "Solar Buyback may be available WITH THIS PLAN" (Champion);
#                "Champion may then add Solar Buyback to your services".
#                Buyback attaches to this very plan -- it is simply priced in a
#                separate addendum instead of on the EFL. Reporting a confident
#                kind=none here understates the plan for a solar owner (Champion
#                confirms buyback can be added to every residential plan except
#                Free Nights, without changing the rate), so this must land in
#                review as an unresolved field rather than assert "no buyback".
_BUYBACK_ATTACHABLE = re.compile(
    r"(?:may\s*be\s*available|available)\s*(?:with|on)\s*this\s*plan"
    r"|(?:may\s*then\s*)?add\s*solar\s*buy\s*-?\s*back\s*to\s*your",
    re.I,
)


_RTW_CAP_LABEL_RE = re.compile(r"\bcap(?:ped)?\b(?:\s+at)?", re.I)


def _extract_rtw_cap(text: str) -> Optional[float]:
    """Look for a 'capped at 25c per kWh' / 'capped at $0.25 per kWh' style
    ceiling on an RTW buyback rate. Searched across the full EFL text
    rather than a window around the buyback label, since EFL prose often
    states the cap in a separate paragraph well after the rate-table label
    (e.g. Chariot Shine 36: the "Buy Back Rate" table entry and the
    "...capped at 25c per kWh..." sentence are >1000 chars apart)."""
    for m in _RTW_CAP_LABEL_RE.finditer(text):
        rate = _rate_ckwh_from_snippet(text[m.end() : m.end() + 40])
        if rate is not None:
            return rate
    return None


# Buyback credits that offset only the energy charge -- never the base charge,
# TDU delivery, or taxes/fees. Searched across the FULL EFL text (not just the
# window around the rate label) because the scope prose is often in a separate
# paragraph well after the rate table -- e.g. Ambit states "Buyback Rate: 3.5c"
# in the price table but "...can offset up to 100% of your Energy Charges each
# month (excluding base charge, TDU charges, and all other taxes and fees)"
# ~600 chars later. "exclud\w*" catches "excludes"/"excluding".
_OFFSET_ENERGY_ONLY_RE = re.compile(
    r"not\s*offsettable|energy\s*charges?\s*only|exclud\w*\s+(?:the\s+)?(?:base|tdu)",
    re.I,
)


def _buyback_offset_scope(text: str) -> str:
    """'energy_only' if the EFL restricts buyback credits to the energy charge
    (excluding base/TDU/taxes), else 'all_charges'."""
    return "energy_only" if _OFFSET_ENERGY_ONLY_RE.search(text) else "all_charges"


# REP-level buyback terms that are REAL but live outside the EFL, in a separate
# addendum. Applied ONLY where the EFL itself says buyback attaches to this plan
# (`_BUYBACK_ATTACHABLE`) and no rate resolved -- so it never overrides a
# published rate, and never fires on a REP that gates buyback behind switching
# products. Keep this table small and evidence-backed: each entry needs a
# document, not a marketing page.
#
# Champion (addendum read 2026-07-25, championenergyservices.com .../Solar-Addendum):
# a straight ERCOT real-time settlement, no modifiers --
#   "Champion will provide you a billing credit... determined by multiplying your
#    Excess for that Interval by the corresponding real-time settlement point
#    price... for the Interconnected Meter's load-zone"
#   "does not contain any other costs, charges, fees, or taxes"
# -> multiplier 1.0, adder 0.0, no cap. Settled per 15-minute interval, which is
# exactly the engine's RTW model. Credits carry into a renewal but are void on
# cancellation and never cashed out -> rollover, no cash_out. The credit is a
# billing credit on the invoice, not an energy-charge-only offset -> all_charges.
#
# floor_ckwh stays 0.0 (the engine default every other RTW plan uses) even though
# the addendum states no floor: measured against Doug's 2025-26 exports and
# LZ_SOUTH prices, 9.4% of export kWh land on negative prices but the difference
# is $4.15/yr on a $215 credit -- immaterial, and consistency across RTW plans
# matters more for ranking.
#
# Only FREE NIGHTS plans are excluded -- the exclusion is literal, not a general
# rule about free-hours plans. Champion's site states buyback IS available on
# Free Weekends (confirmed by Doug 2026-07-25), and Champion's Oncor lineup
# carries no Free Nights plan at all, so today this excludes nothing here. Do
# not widen this to "free weekends": that silently drops ~$215/yr of buyback
# credit from a plan that qualifies.
_ATTACHABLE_BUYBACK_POLICY: dict[str, dict] = {
    "champion": {
        "buyback": {
            "kind": "rtw",
            "rtw": {"multiplier": 1.0, "adder_ckwh": 0.0, "floor_ckwh": 0.0},
            "offset_scope": "all_charges",
            "rollover": True,
            "cash_out": False,
        },
        "exclude_plan": re.compile(r"free\s*nights?", re.I),
        "confidence": 0.85,
        "evidence": (
            "REP addendum (not the EFL): ERCOT real-time settlement point price "
            "for the meter's load zone, no fees or modifiers"
        ),
    },
}


def _attachable_buyback_policy(
    retailer: str, plan_name: str, text: str, resolved: dict
) -> Optional[tuple[dict, float, str]]:
    """Apply a known REP addendum to a plan whose EFL says buyback attaches to
    it but prices it elsewhere. Returns None to leave the parse untouched."""
    if (resolved or {}).get("kind") != "none":
        return None  # a published rate always wins
    if not _BUYBACK_ATTACHABLE.search(text or ""):
        return None
    brand = (retailer or "").lower()
    for key, policy in _ATTACHABLE_BUYBACK_POLICY.items():
        if key not in brand:
            continue
        if policy["exclude_plan"].search(plan_name or ""):
            return None
        return dict(policy["buyback"]), policy["confidence"], policy["evidence"]
    return None


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

    # Prefer candidates that disclose a rate on the label line itself (e.g.
    # Ambit's "Buyback Rate: Per kWh (c) 3.5c", pulse's "Buyback Rate $0.158 per
    # kWh") over generic title/prose mentions ("...Texas Solar Buyback 12"), whose
    # wide context window can otherwise sweep an unrelated "Average Price per kWh"
    # estimate and read it as the buyback rate. Stable sort keeps document order
    # within each group, so a prose-only EFL (TXU: "Solar Buyback: ...at a rate of
    # 3.0 cents per kWh" on the next line) still resolves via its lone candidate.
    candidates.sort(key=lambda m: 0 if _rate_ckwh_from_snippet(m.group(0)) is not None else 1)

    # The EFL's own disclosure answer vetoes a confident "no buyback": if the
    # REP says it DOES purchase excess generation, an unresolved rate is an
    # unread field, not an absent one. Only the confidence moves (the value
    # stays "none"), so the draft lands in review / becomes LLM-repairable
    # instead of silently entering the rankings as a non-buyback plan.
    says_yes = _buyback_disclosure_answer(text) is True

    if not candidates:
        if says_yes:
            return {"kind": "none"}, 0.3, "EFL discloses it purchases excess generation, but no rate found"
        return {"kind": "none"}, 0.95, ""

    fallback_evidence = _snippet(candidates[0])
    for m in candidates:
        context = text[max(0, m.start() - 30) : m.end() + 240]
        evidence = _snippet(m)

        # RTW/market-indexed signal -- searched in a WIDER window than the rate
        # context, because the "...ERCOT 15-minute Real-Time Settlement Point
        # Price (RTSPP)..." prose can sit several sentences after the buyback
        # label (e.g. Reliant's Solar Payback Match: label and RTSPP wording are
        # ~400 chars apart). Kept separate from the rate context (still +240) so
        # a distant number can't be misread as a fixed rate.
        rtw_context = text[max(0, m.start() - 30) : m.end() + 800]
        # Specific market-index signals only -- deliberately NOT bare "ERCOT",
        # which appears in the boilerplate "...changes to the Electric Reliability
        # Council of Texas administrative fees..." that most EFLs carry and would
        # false-flag a fixed buyback as RTW in this wider window.
        if re.search(
            r"RTSPP|settlement\s*point|wholesale|market\s*pric|hourly"
            r"|real\s*-?\s*time\s*(?:market|settlement|energy|price|pric)",
            rtw_context,
            re.I,
        ):
            rtw: dict = {"multiplier": 1.0, "adder_ckwh": 0.0}
            cap = _extract_rtw_cap(text)
            if cap is not None:
                rtw["cap_ckwh"] = cap
                evidence = evidence + f" [cap: {cap}c/kWh]"
            return {"kind": "rtw", "rtw": rtw}, 0.85, evidence

        rate = _rate_ckwh_from_snippet(m.group(0))
        if rate is None:
            # A hedged mention ("Solar Buyback may be available -- please
            # contact Customer Care") publishes no rate at all, so any number
            # near it belongs to something else and must not be swept out of
            # the wide context window. The label line itself is still read
            # above: an EFL that hedges AND prints a rate is taken at its word.
            if _BUYBACK_HEDGE.search(text[max(0, m.start() - 150) : m.end() + 240]):
                continue
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

        offset_scope = _buyback_offset_scope(text)
        return {"kind": "fixed", "rate_ckwh": rate, "offset_scope": offset_scope}, 0.85, evidence

    # None of the candidates yielded a rate. If they read like a marketing
    # disclaimer ("Solar Buyback may be available -- please contact Customer
    # Care", "Yes, for solar buy-back plans only, please inquire for more
    # details") rather than a genuine unresolved rate, that's a confident
    # signal this EFL itself doesn't disclose a buyback rate -- not a low-
    # confidence guess.
    hedged = any(
        _BUYBACK_HEDGE.search(text[max(0, m.start() - 150) : m.end() + 240]) for m in candidates
    )
    # "...available with THIS plan" is not a disclaimer that there's no buyback
    # (see _BUYBACK_ATTACHABLE) -- the value stays "none" because no rate is
    # published, but the confidence must send it to a human.
    if _BUYBACK_ATTACHABLE.search(text):
        return (
            {"kind": "none"},
            0.3,
            fallback_evidence + " [buyback attaches to THIS plan; rate not on the EFL]",
        )
    conf = 0.85 if hedged and not says_yes else 0.3
    return {"kind": "none"}, conf, fallback_evidence


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
    aliased = _apply_retailer_alias(retailer)
    if aliased != retailer:
        evidence["retailer"] = f"{evidence.get('retailer', retailer)} [brand alias -> {aliased}]"
        retailer = aliased
    plan_name = record("plan_name", _extract_plan_name(text)) or "Unnamed Plan"
    term_months = record("term_months", _extract_term_months(text))
    if term_months is None:
        notes.append("term_months not found; defaulting to 12")
        term_months = 12
    rate_type = record("rate_type", _extract_rate_type(text)) or "fixed"

    # --- energy charge / free windows / TOU -------------------------------
    tou_rows = _extract_tou_table(text)
    free_win = _extract_free_window(text) or _extract_credited_window(text)
    energy_rates: list[dict] = []
    flat_ckwh: Optional[float] = None
    split_base_charge: Optional[float] = None
    # Confidence for a base charge read out of a split charge row. Both readers
    # pin $base on BOTH sides -- rate column(s) before it, the TDU's (¢/kWh,
    # $/month) pair after it -- and the 4-column reader additionally requires
    # "Energy Charge" and "Base Charge" labels above the row, so neither is a
    # guess. The old 0.75 kept every Champion plan permanently in review over a
    # $0.00 base charge the document states plainly.
    split_base_conf: float = 0.85

    brand_rows: Optional[list[dict]] = None
    if not tou_rows:
        candidate_brand_rows = _extract_brand_energy_tiers(text)
        if len(candidate_brand_rows) >= 2 and len({r["rate_ckwh"] for r in candidate_brand_rows}) >= 2:
            night_rows = [r for r in candidate_brand_rows if re.search(r"night", r["prefix"], re.I)]
            other_rows = [r for r in candidate_brand_rows if r not in night_rows]
            if night_rows and other_rows:
                brand_rows = candidate_brand_rows

    # A REP/TDU split row with two rate columns is a real TOU schedule, and a
    # more reliable read than the generic scanners -- the columns are positional
    # and the TDU's pair is always last. Checked before them so the discounted
    # column can't be mistaken for the flat rate.
    suffixed_rows = None if tou_rows else _extract_suffixed_energy_tiers(text)
    multi_row = None if (tou_rows or suffixed_rows) else _extract_multi_rate_charge_row(text)
    if multi_row is not None and multi_row["restricted_index"] is None:
        multi_row = None  # can't tell which column is restricted; don't guess

    if suffixed_rows is not None:
        # The cheaper row carries the window; the dearer one is the catch-all,
        # so any interval the window misses still bills at the full rate.
        cheap, dear = sorted(suffixed_rows, key=lambda r: r["rate_ckwh"])
        energy_rates.append(
            {
                "label": cheap["qualifier"],
                "rate_ckwh": cheap["rate_ckwh"],
                "window": {"weekdays": cheap["weekdays"]},
            }
        )
        energy_rates.append({"label": dear["qualifier"], "rate_ckwh": dear["rate_ckwh"], "window": None})
        flat_ckwh = dear["rate_ckwh"]
        confidence["energy_charge"] = 0.85
        confidence["free_window"] = 0.85
        evidence["energy_charge"] = " | ".join(r["evidence"] for r in suffixed_rows)
        evidence["free_window"] = cheap["evidence"]
    elif multi_row is not None:
        ri = multi_row["restricted_index"]
        restricted_rate = multi_row["rates"][ri]
        general_rate = multi_row["rates"][1 - ri]
        label = multi_row["restricted_label"] or "restricted"
        window = _restricted_window(text, label)
        if window is None:
            # Rates are trustworthy, the window isn't stated -- record the rates
            # and let review supply the window rather than inventing one.
            flat_ckwh = general_rate
            energy_rates.append({"label": "", "rate_ckwh": general_rate, "window": None})
            confidence["energy_charge"] = 0.6
            notes.append(
                f"split charge row gave a restricted rate of {restricted_rate}c/kWh but the EFL "
                "does not state its hours; only the general rate is modeled"
            )
        else:
            energy_rates.append({"label": label, "rate_ckwh": restricted_rate, "window": window})
            energy_rates.append({"label": "", "rate_ckwh": general_rate, "window": None})
            flat_ckwh = general_rate
            confidence["energy_charge"] = 0.85
            confidence["free_window"] = 0.85
            evidence["free_window"] = multi_row["evidence"]
        evidence["energy_charge"] = multi_row["evidence"]
        split_base_charge = multi_row["base_usd"]
        split_base_conf = 0.85
    elif tou_rows:
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
    elif brand_rows:
        # Multi-tier brand-prefixed charge table (e.g. Chariot Energy's
        # "Chariot Energy Daytime Energy Charge 6.78c per kWh" /
        # "Chariot Energy Bright Nights Energy Charge 0c per kWh") where a
        # 0(-ish) tier's label suggests free/discounted nighttime hours.
        night_rows = [r for r in brand_rows if re.search(r"night", r["prefix"], re.I)]
        other_rows = [r for r in brand_rows if r not in night_rows]
        night = night_rows[0]
        day_rate = other_rows[0]["rate_ckwh"]
        hours = _find_night_hours(text)
        if hours:
            # Both the two rates and the window come from the document -- there
            # is nothing left to guess, so this is a full read, not a partial
            # one. At 0.75 every free-nights plan sat one notch below the bar
            # and went to review with nothing for a human to actually resolve.
            conf = 0.85
        else:
            hours = [21, 22, 23, 0, 1, 2, 3, 4, 5]  # assumed 9 p.m.-6 a.m.
            conf = 0.5
            notes.append(
                "brand-prefixed multi-tier table found a free/discounted night rate but no "
                "explicit hour range in the text; assumed a 9 p.m.-6 a.m. window (low confidence)"
            )
        energy_rates.append({"label": "night", "rate_ckwh": night["rate_ckwh"], "window": {"hours": hours}})
        energy_rates.append({"label": "day", "rate_ckwh": day_rate, "window": None})
        flat_ckwh = day_rate
        confidence["energy_charge"] = conf
        confidence["free_window"] = conf
        evidence["energy_charge"] = night["evidence"] + " | " + other_rows[0]["evidence"]
        evidence["free_window"] = night["evidence"]
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
            # Layered fallbacks, most-specific first: (a) prose "variable
            # rate of X cents" statement, (b) a split header/value table row
            # ("Energy Charge: Per kWh (c)" ... "All kWh 14.0000c" on a
            # later line), (c) a split 4-column charge row (energy/base/TDU
            # rate/TDU monthly all on one data row below their headers),
            # (d) generic "<label>Charge ... X per kWh" table-line scan
            # (handles both corrupted-font documents where only the literal
            # word "Charge" survives intact, and brand-prefixed labels like
            # "Chariot Energy Base Monthly Charge"), (e) a flat repeated
            # avg-price table row.
            var_ext = _extract_variable_rate(text)
            all_kwh_ext = _extract_all_kwh_rate(text)
            split_row = _extract_split_energy_base_row(text)
            base_kwh_row = _extract_base_then_kwh_row(text)
            generic_kwh_rows = [
                r for r in _generic_charge_rows(text) if r["kind"] == "kwh" and not r["is_tdu"]
            ]
            avg_ext = _extract_flat_avg_price_row(text)
            if var_ext is not None:
                flat_ckwh, conf, ev = var_ext
                confidence["energy_charge"] = conf
                evidence["energy_charge"] = ev
                notes.append("energy rate derived from a 'variable rate of X cents' statement")
            elif all_kwh_ext is not None:
                flat_ckwh, conf, ev = all_kwh_ext
                confidence["energy_charge"] = conf
                evidence["energy_charge"] = ev
                notes.append("energy rate derived from a split header/value table ('All kWh <rate>' row)")
            elif split_row is not None:
                flat_ckwh, split_base_charge, ev = split_row
                # Same reasoning as split_base_conf: the row is positionally
                # unambiguous (rate, base, then the TDU's pair) and only matches
                # when "Energy Charge" and "Base Charge" head the columns.
                confidence["energy_charge"] = 0.85
                evidence["energy_charge"] = ev
                notes.append("energy rate derived from a split 4-column Energy/Base/TDU charge row")
            elif base_kwh_row is not None:
                flat_ckwh, split_base_charge, ev = base_kwh_row
                confidence["energy_charge"] = 0.8
                evidence["energy_charge"] = ev
                notes.append(
                    "energy rate derived from a 'Base Charge / Per kWh Charge' labeled provider row"
                )
            elif generic_kwh_rows:
                # As with the base charge: a row whose label reads as an energy
                # charge -- even through a broken subset font, where "Energy"
                # survives as "nerg" -- is a real read, not a guess. Only fall
                # back to the cautious score when nothing identifies the row.
                labeled = [r for r in generic_kwh_rows if _looks_like_energy_label(r["prefix"])]
                r = (labeled or generic_kwh_rows)[0]
                flat_ckwh = r["value"]
                # Several DIFFERING energy rows are a schedule, not a flat rate
                # ("Energy Charge 17.6000¢ per kWh - Weekdays" / "... 0.0000¢
                # per kWh - Weekends"). Taking the first as a flat rate drops
                # the free window entirely, so this must never look confident --
                # the same rule the labeled-line reader applies above.
                distinct = {row["value"] for row in (labeled or generic_kwh_rows)}
                if len(distinct) > 1:
                    confidence["energy_charge"] = 0.5
                    notes.append(
                        f"multiple differing Energy Charge rows found {sorted(distinct)}; used "
                        f"{flat_ckwh} as a flat rate -- any time-of-use window is NOT modeled"
                    )
                else:
                    confidence["energy_charge"] = 0.85 if labeled else 0.6
                evidence["energy_charge"] = r["evidence"]
                notes.append("energy rate derived from a generic '<label> Charge ... per kWh' table-line scan")
            elif avg_ext is not None:
                flat_ckwh, conf, ev = avg_ext
                confidence["energy_charge"] = conf
                evidence["energy_charge"] = ev
                notes.append("energy rate derived from a flat repeated average-price table row")
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
        trailing_ext = _extract_base_charge_trailing_amount(text)
        if trailing_ext is not None:
            base_charge = record("base_charge", trailing_ext)
    if base_charge is None:
        bare_ext = _extract_base_charge_bare_amount(text)
        if bare_ext is not None:
            base_charge = record("base_charge", bare_ext)
    if base_charge is None and split_base_charge is not None:
        base_charge = record(
            "base_charge", (split_base_charge, split_base_conf, evidence.get("energy_charge", ""))
        )
        notes.append("base charge derived from the same split 4-column Energy/Base/TDU charge row")
    if base_charge is None:
        daily_ext = _extract_daily_fee_as_base(text)
        if daily_ext is not None:
            base_charge = record("base_charge", daily_ext)
            notes.append(
                f"base_charge_usd (${base_charge:.2f}/mo) derived from a per-day customer fee "
                f"(fee * 365 / 12, rounded to cents)"
            )
        else:
            absent_ext = _extract_base_charge_absent_from_itemized_list(text)
            generic_month_rows = [
                r for r in _generic_charge_rows(text) if r["kind"] == "month" and not r["is_tdu"]
            ]
            if generic_month_rows:
                # Prefer a row whose label actually reads as a base charge; that
                # is what separates "the REP's fixed monthly charge" from some
                # other per-month fee, and it earns a promotable score.
                labeled = [r for r in generic_month_rows if _looks_like_base_label(r["prefix"])]
                r = (labeled or generic_month_rows)[0]
                base_charge = record(
                    "base_charge", (r["value"], 0.85 if labeled else 0.6, r["evidence"])
                )
                notes.append("base charge derived from a generic '<label> Charge ... per month' table-line scan")
            elif absent_ext is not None:
                base_charge = record("base_charge", absent_ext)
                notes.append(
                    "base charge inferred as $0.00: itemized price-components list has no "
                    "REP base/customer charge line"
                )
            elif (sentence_ext := _extract_base_charge_from_component_sentence(text)) is not None:
                base_charge = record("base_charge", sentence_ext)
                notes.append(
                    "base charge inferred as $0.00: the EFL states its price components in prose "
                    "and names only an energy charge and TDU delivery charges"
                )
            elif (bullet_ext := _extract_base_charge_absent_from_bullet_list(text)) is not None:
                base_charge = record("base_charge", bullet_ext)
                notes.append(
                    "base charge inferred as $0.00: the EFL's bulleted price-component list has "
                    "no recurring REP monthly charge (only the TDSP's)"
                )
            else:
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
    signup_fee, signup_conf, signup_ev = _extract_signup_fee(text)
    usage_tiers = detect_usage_tiers(text)
    confidence["etf"] = etf_conf
    if signup_fee:
        # Recorded only when one was FOUND: scoring every fee-less EFL 0.0
        # would fill the review table with a field most plans do not have.
        confidence["signup_fee"] = signup_conf
        evidence["signup_fee"] = signup_ev
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
    policy = _attachable_buyback_policy(retailer, plan_name, text, buyback)
    if policy is not None:
        buyback, buyback_conf, buyback_ev = policy
    confidence["buyback"] = buyback_conf
    if buyback_ev:
        evidence["buyback"] = buyback_ev

    # --- free window stated outside any recognized rate table --------------#
    # Direct Energy's Twelve Hour Power prices its free period in a separate
    # column from its label, so no row extractor sees it: the flat scan finds
    # only the daytime 21.7727c and the plan looks like an ordinary fixed rate
    # priced at its EXPENSIVE tier. The window and its zero price are both
    # stated plainly in prose, so pair them here when no windowed rate was
    # built. Requires an explicit zero beside the named period -- a window
    # alone is not enough, or a plan that merely *mentions* nighttime hours
    # would be given free electricity.
    if flat_ckwh and not any(r.get("window") for r in energy_rates):
        free_hours = _find_night_hours(text)
        if free_hours and _FREE_PERIOD_ZERO.search(" ".join((text or "").split())):
            energy_rates.insert(0, {"label": "free", "rate_ckwh": 0.0, "window": {"hours": free_hours}})
            confidence["free_window"] = 0.85
            evidence["free_window"] = f"free period priced at 0, {len(free_hours)} hours"
            notes.append(
                f"free window ({len(free_hours)}h) read from prose; it is priced in a "
                "different column from its label, so no rate table carries it"
            )

    # --- TDU relief inside a free window -----------------------------------#
    # A free-nights/weekends plan may ALSO waive the TDU per-kWh delivery
    # charge during the window. Whether it does is the single biggest lever on
    # what these plans cost -- at Oncor's 6.12c/kWh, missing it overstated
    # Green Mountain Pollution Free Nights by ~$383/yr against the report
    # benchmark -- and it is genuinely plan-specific: TXU's Cool Summer formula
    # subtracts only the Energy Charge, and SoFed says outright that delivery
    # charges apply during its free hour. So it is read, never assumed, and the
    # default stays False -- which overstates a plan's cost rather than
    # understating it, and so under-ranks rather than wrongly recommends.
    if any(r.get("window") and not r.get("rate_ckwh") for r in energy_rates):
        exempt, exempt_ev = _free_window_waives_tdu(text)
        if exempt:
            for r in energy_rates:
                if r.get("window") and not r.get("rate_ckwh"):
                    r["tdu_exempt"] = True
            notes.append(f"TDU delivery charge waived during the free window ({exempt_ev})")
            confidence["tdu_free_window"] = 0.9
            evidence["tdu_free_window"] = exempt_ev

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
        "signup_fee_usd": signup_fee or 0.0,
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
    # A bonus credit the schema cannot express makes the plan look WORSE than it
    # is, which is quiet in a way a wrong rate is not: nothing is misparsed, so
    # nothing scores low, and the plan simply under-ranks. Flag it rather than
    # promote a model we know is incomplete. Only "additional N%" is caught --
    # the plain "100% credit/discount" of an ordinary free-nights plan IS
    # modeled (a 0.0 rate over the stated window) and must not be flagged.
    # Eligibility comes before economics: a plan the REP won't sell to a solar
    # home is not a cheap plan, it is not a plan at all. Recorded on the Plan so
    # ranking can hide it, and noted so the reason survives promotion (which
    # strips the _parse block).
    for _sentence in _SOLAR_EXCLUSION_RE.finditer(text or ""):
        if _SOLAR_SUBJECT_RE.search(_sentence.group(0)):
            plan_dict["excludes_solar"] = True
            notes.append(
                "REP excludes homes with rooftop solar from this plan: "
                f"{' '.join(_sentence.group(0).split())[:160]}"
            )
            plan_dict["notes"] = "; ".join(notes)
            break

    bonus = _BONUS_CREDIT_RE.search(text or "")
    if bonus:
        note = (
            f"unmodelled bonus credit ({bonus.group(0).strip()}) -- the schema has no way to "
            "express it, so this plan's cost is OVERstated"
        )
        notes.append(note)
        plan_dict["notes"] = "; ".join(notes)
        needs_review = True
    if usage_tiers:
        # Loud, and specific about WHY: the old note ("multiple differing flat
        # Energy Charge values found; used first") read like parser trouble
        # rather than a plan whose shape the schema cannot hold.
        shape = ", ".join(f"{label} @ {rate:g}c" for label, rate in usage_tiers)
        plan_dict["unpriceable_reason"] = (
            f"usage-tiered energy charge ({shape}) -- the schema models one rate "
            "per window, so any single rate here would misprice the plan"
        )
        notes.append(plan_dict["unpriceable_reason"])
        plan_dict["notes"] = "; ".join(notes)
        needs_review = True
    plan_dict["needs_review"] = needs_review

    return DraftPlan(
        plan_dict=plan_dict, confidence=confidence, evidence=evidence, unparsed_notes=notes
    )


def efl_sha256(pdf_path: str | Path) -> Optional[str]:
    """SHA-256 of an EFL PDF, or None if it can't be read.

    Identity for "is this the same document I already reviewed?". Content, not
    mtime: every refresh re-downloads the whole EFL directory, so timestamps
    change on every run while the bytes usually don't.
    """
    try:
        return hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    except OSError:
        return None


def parse_efl(pdf_path: str | Path) -> DraftPlan:
    pdf_path = Path(pdf_path)
    text = extract_text(pdf_path)
    draft = parse_efl_text(text, source_name=pdf_path.name)
    draft.source_sha256 = efl_sha256(pdf_path)
    return draft


def save_draft(draft: DraftPlan, drafts_dir: str | Path = DEFAULT_DRAFTS_DIR) -> Path:
    drafts_dir = Path(drafts_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    out = dict(draft.plan_dict)
    out["_parse"] = {
        "confidence": draft.confidence,
        "evidence": draft.evidence,
        "unparsed_notes": draft.unparsed_notes,
        "source_sha256": draft.source_sha256,
    }
    path = drafts_dir / f"{draft.plan_dict['id']}.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True)
    return path
