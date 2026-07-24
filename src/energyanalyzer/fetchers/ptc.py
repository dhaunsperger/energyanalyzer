"""Power to Choose (PTC) bulk plan CSV + EFL PDF fetchers.

See ARCHITECTURE.md §7. Like `prices/ercot.py`, this module is offline-first:
:func:`load_ptc` and :func:`filter_plans` work entirely from a CSV file
already on disk (a snapshot the user downloaded, or one saved by
:func:`fetch_ptc_csv`). Network calls (`fetch_ptc_csv`, `download_efls`) are
thin, defensively-wrapped httpx calls that raise a clear RuntimeError with a
manual fallback if the network is unavailable -- both powertochoose.org and
EFL hosts are blocked from this development sandbox, so those code paths are
exercised in tests only via monkeypatched transports, never live.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

PTC_EXPORT_URL = "https://www.powertochoose.org/en-us/Plan/ExportToCsv"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# EFL "URLs" that are actually an HTML viewer page, not a PDF -- a plain httpx
# GET returns HTML and the %PDF guard (correctly) rejects it. These are handled
# by REP discovery's headless-render path (`rep_discovery._render_efl_pdf`), so
# the PTC download path defers them instead of logging a bogus "failed" download.
_HTML_VIEWER_URL_RE = re.compile(r"octopusenergy\.com/efl/", re.I)


def _is_html_viewer_url(url: str) -> bool:
    """True if `url` serves an HTML EFL viewer (needs rendering, not httpx)."""
    return bool(_HTML_VIEWER_URL_RE.search(url or ""))


def _efl_ssl_context():
    """TLS context that tolerates a few EFL hosts' legacy servers.

    Some REP EFL hosts (e.g. Tara Energy / Amigo Energy on the shared Just
    Energy `webs.*.com/Generate_Docs` platform) run TLS stacks that need
    legacy renegotiation, which OpenSSL 3.x refuses by default -- httpx then
    fails the download with a bare ``ConnectError`` (SSL routines: unsafe
    legacy renegotiation disabled). Re-enabling ``OP_LEGACY_SERVER_CONNECT``
    lets those handshakes complete; verification is otherwise unchanged. Only
    affects hosts that actually request legacy renegotiation.
    """
    import ssl

    ctx = ssl.create_default_context()
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx

# Canonical field -> accepted raw header spellings. PTC has tweaked its
# export headers over time; matching is done after stripping everything but
# letters/digits and lowercasing, so "Fees/Credits", "FeesCredits", and
# "fees_credits" all match the same alias.
_COLUMN_ALIASES: dict[str, list[str]] = {
    "plan_id": ["idKey", "PlanId", "PlanID", "Id"],
    "tdu": ["TduCompanyName", "TDU", "TDSP", "UtilityName", "Utility"],
    "language": ["Language", "PlanLanguage", "Lang"],
    "retailer": ["RepCompany", "CompanyName", "Company", "ProviderName", "REP"],
    "plan_name": ["Product", "PlanName", "Plan"],
    "term_months": ["Term", "TermMonths", "ContractTerm", "Terms"],
    "rate_type": ["RateType", "RateClass"],
    "fixed_flag": ["Fixed", "IsFixed"],
    "kwh500": ["kwh500", "Kwh500", "KWH500", "Price500", "Avg500"],
    "kwh1000": ["kwh1000", "Kwh1000", "KWH1000", "Price1000", "Avg1000"],
    "kwh2000": ["kwh2000", "Kwh2000", "KWH2000", "Price2000", "Avg2000"],
    "fees_credits": ["FeesCredits", "Fees/Credits", "FeesAndCredits"],
    "prepaid": ["PrePaid", "Prepaid", "IsPrepaid"],
    "tou": ["TimeOfUse", "TOU", "IsTimeOfUse"],
    "renewable_pct": ["Renewable", "RenewablePercentage", "RenewableContent", "PctRenewable"],
    "cancel_fee": ["CancellationFee", "ETF", "EarlyTerminationFee"],
    "website": ["Website", "CompanyWebsite"],
    "enroll_url": ["EnrollURL", "EnrollUrl", "EnrollmentURL"],
    "efl_url": ["FactsURL", "FactsUrl", "EFLURL", "EFL", "FactSheetURL"],
}

# Order the "known" columns should appear in on the returned frame; anything
# else in the source CSV is kept, appended after these.
_TIDY_ORDER = [
    "retailer",
    "plan_name",
    "tdu",
    "language",
    "term_months",
    "rate_type",
    "kwh500",
    "kwh1000",
    "kwh2000",
    "efl_url",
    "enroll_url",
    "renewable_pct",
    "prepaid",
    "tou",
    "cancel_fee",
    "plan_id",
    "fees_credits",
    "website",
]

_NUMERIC_COLUMNS = ("kwh500", "kwh1000", "kwh2000", "term_months", "renewable_pct", "cancel_fee")
_BOOL_COLUMNS = ("prepaid", "tou")


def _normalize_name(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            lookup[_normalize_name(alias)] = canonical
    return lookup


_ALIAS_LOOKUP = _alias_lookup()


def _rename_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename: dict[object, str] = {}
    seen: set[str] = set()
    for col in df.columns:
        canonical = _ALIAS_LOOKUP.get(_normalize_name(col))
        if canonical and canonical not in seen:
            rename[col] = canonical
            seen.add(canonical)
    return df.rename(columns=rename)


def _to_bool(series: pd.Series) -> pd.Series:
    def conv(v: object) -> Optional[bool]:
        if pd.isna(v):
            return None
        s = str(v).strip().upper()
        if s in ("Y", "YES", "TRUE", "1"):
            return True
        if s in ("N", "NO", "FALSE", "0"):
            return False
        return None

    return series.map(conv)


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in _NUMERIC_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "term_months" in out.columns:
        out["term_months"] = out["term_months"].astype("Int64")
    for col in _BOOL_COLUMNS:
        if col in out.columns:
            out[col] = _to_bool(out[col])

    if "rate_type" not in out.columns and "fixed_flag" in out.columns:
        fixed_bool = _to_bool(out["fixed_flag"])
        out["rate_type"] = fixed_bool.map({True: "fixed", False: "variable"})

    for col in ("retailer", "plan_name", "tdu", "language", "efl_url", "enroll_url", "website"):
        if col in out.columns:
            out[col] = out[col].astype(str).str.strip().replace({"nan": None, "": None})

    return out


def _reorder_columns(df: pd.DataFrame) -> pd.DataFrame:
    present = [c for c in _TIDY_ORDER if c in df.columns]
    rest = [c for c in df.columns if c not in present]
    return df[present + rest]


def _read_csv_defensive(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    # Last resort: let pandas raise its own error with the real traceback.
    return pd.read_csv(path)


def fetch_ptc_csv(dest_dir: Path = Path("data/ptc"), timeout: float = 30.0) -> Path:
    """Download the Power to Choose bulk plan CSV and save a timestamped snapshot.

    Raises RuntimeError with instructions to fetch it via a browser if the
    network request fails (powertochoose.org is blocked from some sandboxes;
    this always works from the user's own machine).
    """
    import httpx

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/csv,*/*"}

    try:
        with httpx.Client(
        timeout=timeout, headers=headers, follow_redirects=True, verify=_efl_ssl_context()
    ) as client:
            resp = client.get(PTC_EXPORT_URL)
            resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"Could not download the Power to Choose plan export ({PTC_EXPORT_URL}): {exc!r}. "
            "Please open that URL in a browser (or go to powertochoose.org, set your zip code "
            "and any filters, and click 'Download Results'), save the CSV, and place it in "
            f"{dest_dir}/ -- then call load_ptc() on the saved file or on {dest_dir}."
        ) from exc

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_path = dest_dir / f"ptc_{ts}.csv"
    dest_path.write_bytes(resp.content)
    return dest_path


def load_ptc(path_or_dir: Path) -> pd.DataFrame:
    """Load a Power to Choose plan export CSV into a tidy DataFrame.

    `path_or_dir` may be a specific CSV file, or a directory (e.g.
    `data/ptc/`) in which case the most recently modified `*.csv` in it is
    used. Column matching is case/spacing-insensitive and tolerant of PTC's
    header tweaks (see `_COLUMN_ALIASES`); recognized columns are renamed to
    canonical names and reordered first (retailer, plan_name, tdu,
    term_months, rate_type, kwh500/1000/2000, efl_url, enroll_url,
    renewable_pct, prepaid, tou, cancel_fee, ...) -- any columns PTC adds
    that we don't recognize are kept as-is, appended at the end, rather than
    silently dropped.
    """
    path = Path(path_or_dir)
    if path.is_dir():
        candidates = sorted(path.glob("*.csv"))
        if not candidates:
            raise FileNotFoundError(
                f"No PTC CSV files found in {path}. Run fetch_ptc_csv(), or download the "
                f"CSV manually from {PTC_EXPORT_URL} and place it in {path}."
            )
        path = max(candidates, key=lambda p: p.stat().st_mtime)
    elif not path.exists():
        raise FileNotFoundError(f"PTC CSV not found: {path}")

    df = _read_csv_defensive(path)
    df = _rename_columns(df)
    df = _coerce_types(df)
    df = _reorder_columns(df)
    return df


def filter_plans(
    df: pd.DataFrame,
    tdu: Optional[str] = "ONCOR",
    prepaid: Optional[bool] = None,
    tou: Optional[bool] = None,
    rate_type: Optional[str] = None,
    min_term_months: Optional[int] = None,
    max_term_months: Optional[int] = None,
    min_renewable_pct: Optional[float] = None,
    retailer: Optional[str] = None,
    language: Optional[str] = "English",
) -> pd.DataFrame:
    """Filter a tidy PTC DataFrame (as returned by `load_ptc`) by common criteria.

    All filters are optional and combined with AND; string filters
    (`tdu`, `rate_type`, `retailer`, `language`) are case-insensitive substring
    matches. Filters referencing columns absent from `df` are silently
    skipped so this stays usable even against partially-recognized exports.

    `language` defaults to `"English"`: the statewide PTC export includes a
    Spanish-language duplicate row for most plans (same plan, translated
    fields), which otherwise roughly doubles the apparent row count. Pass
    `language=None` to disable this filter and see every row.
    """
    out = df

    def _contains(col: str, value: str) -> pd.Series:
        return out[col].astype(str).str.contains(re.escape(value), case=False, na=False)

    if tdu is not None and "tdu" in out.columns:
        out = out[_contains("tdu", tdu)]
    if language is not None and "language" in out.columns:
        out = out[_contains("language", language)]
    if retailer is not None and "retailer" in out.columns:
        out = out[_contains("retailer", retailer)]
    if rate_type is not None and "rate_type" in out.columns:
        out = out[_contains("rate_type", rate_type)]
    if prepaid is not None and "prepaid" in out.columns:
        out = out[out["prepaid"] == prepaid]
    if tou is not None and "tou" in out.columns:
        out = out[out["tou"] == tou]
    if min_term_months is not None and "term_months" in out.columns:
        out = out[out["term_months"] >= min_term_months]
    if max_term_months is not None and "term_months" in out.columns:
        out = out[out["term_months"] <= max_term_months]
    if min_renewable_pct is not None and "renewable_pct" in out.columns:
        out = out[out["renewable_pct"] >= min_renewable_pct]

    return out.reset_index(drop=True)


def _sanitize_filename_part(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned


def _efl_filename(row: "pd.Series[object]") -> str:
    retailer = _sanitize_filename_part(str(row.get("retailer") or ""))
    plan = _sanitize_filename_part(str(row.get("plan_name") or ""))
    base = "_".join(p for p in (retailer, plan) if p) or "efl"
    return f"{base}.pdf"[:150]


def download_efls(
    df: pd.DataFrame,
    dest: Path = Path("data/efl"),
    limit: Optional[int] = None,
    timeout: float = 30.0,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Download each plan's EFL PDF (`efl_url` column) into `dest`.

    Existing files are skipped (not re-downloaded). Individual failures are
    tolerated and collected rather than aborting the whole batch -- EFL
    hosts vary in reliability and this is meant to run unattended against a
    few hundred plans. Returns a summary dict with 'downloaded', 'skipped',
    and 'failed' lists.

    If `progress_callback` is given, it is called after *every* row is
    processed (downloaded, skipped, failed, or missing a URL) as
    `progress_callback(done_count, total, current_name)`, so a caller (e.g.
    the Streamlit Plans page) can drive a progress bar/status line instead
    of leaving the user with no feedback until the whole batch finishes.
    """
    import httpx

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if "efl_url" not in df.columns:
        raise ValueError(
            "DataFrame has no 'efl_url' column -- pass the output of load_ptc()/filter_plans()"
        )

    rows = df.dropna(subset=["efl_url"])
    if limit is not None:
        rows = rows.head(limit)

    summary: dict = {"downloaded": [], "skipped": [], "failed": [], "deferred": []}
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*"}
    total = len(rows)
    done = 0

    def _report(name: str) -> None:
        nonlocal done
        done += 1
        if progress_callback is not None:
            progress_callback(done, total, name)

    with httpx.Client(
        timeout=timeout, headers=headers, follow_redirects=True, verify=_efl_ssl_context()
    ) as client:
        for _, row in rows.iterrows():
            name = str(row.get("plan_name") or row.get("retailer") or _efl_filename(row))
            url = str(row["efl_url"]).strip()
            if not url or url.lower() in ("nan", "none"):
                _report(name)
                continue
            dest_path = dest / _efl_filename(row)
            if dest_path.exists():
                summary["skipped"].append(str(dest_path))
                _report(name)
                continue
            if _is_html_viewer_url(url):
                # Not a PDF over httpx -- REP discovery renders these to PDF.
                # Defer rather than record a spurious download "failure".
                summary["deferred"].append(
                    {
                        "url": url,
                        "reason": "HTML EFL viewer -- captured via REP discovery's "
                        "renderer, not an httpx download",
                    }
                )
                _report(name)
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
                content = resp.content
                # Guard against saving a non-PDF (an HTML "not found"/SPA shell or
                # a bot-challenge/captcha page returned with HTTP 200) as a .pdf --
                # those can't be parsed and otherwise land silently on disk. A PDF
                # begins with the "%PDF" signature (allow a little leading junk).
                if b"%PDF" not in content[:1024]:
                    ctype = resp.headers.get("content-type", "?")
                    summary["failed"].append(
                        {
                            "url": url,
                            "error": f"response was not a PDF (content-type {ctype!r}, "
                            f"{len(content)} bytes) -- likely an HTML error/redirect page",
                        }
                    )
                    _report(name)
                    continue
                dest_path.write_bytes(content)
                summary["downloaded"].append(str(dest_path))
            except Exception as exc:
                summary["failed"].append({"url": url, "error": repr(exc)})
            _report(name)

    return summary
