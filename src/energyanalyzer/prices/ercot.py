"""ERCOT Real-Time Market (RTM) settlement point price loading.

See ARCHITECTURE.md §7. This module is deliberately *offline-first*: the
primary entry point, :func:`load_prices`, works entirely from files the user
has manually downloaded into ``data_dir`` (no network access required). A
best-effort :func:`download_prices` helper is provided for convenience but is
not required for the module to function, and it fails loudly with manual
instructions if ERCOT's site is unreachable or its download flow changes.

Supported source file shapes (auto-detected by column names, not by
filename or extension):

(a) ERCOT "Historical RTM Load Zone and Hub Prices" workbook (report
    NP6-785-ER), an .xlsx with one sheet per month and columns "Delivery
    Date" (mm/dd/yyyy), "Delivery Hour" (1-24), "Delivery Interval" (1-4),
    "Repeated Hour Flag" (Y/N), "Settlement Point Name",
    "Settlement Point Price" ($/MWh).

(b) CSV exports of ERCOT report 12301 (SPPHLZNP6905), e.g.
    ``cdr.00012301.*.SPPHLZNP6905*.csv``, with columns DeliveryDate,
    DeliveryHour, DeliveryInterval, SettlementPointName,
    SettlementPointPrice, DSTFlag.

Both shapes report price per 15-minute settlement interval; this module
converts "Delivery Hour" + "Delivery Interval" (both 1-based) into the local
wall-clock **interval start**:

    local_start = (Delivery Date) + (Delivery Hour - 1) hours
                                   + (Delivery Interval - 1) * 15 minutes

On the one day per year clocks fall back (early November), the 1:00am-1:59am
wall-clock hour occurs twice. Shape (a) disambiguates this with "Repeated
Hour Flag": 'Y' marks the *second* pass (standard time / CST, `fold=1`),
'N' (or blank) the first (daylight time / CDT, `fold=0`). Shape (b)'s
DSTFlag is assumed to follow ERCOT's usual convention of 'Y' meaning the row
is still within daylight time (CDT, `fold=0`) and 'N' meaning standard time
(CST, `fold=1`) -- i.e. opposite polarity from "Repeated Hour Flag". This
assumption could not be verified against a live ERCOT download in this
environment (network egress to ercot.com is blocked here); if a real 12301
CSV disagrees, adjust `_DST_FLAG_MEANS_DST` accordingly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

LOCAL_TZ = "America/Chicago"

ERCOT_REPORT_ID = "NP6-785-ER"
ERCOT_REPORT_NAME = "Historical RTM Load Zone and Hub Prices"
ERCOT_REPORT_LANDING_URL = (
    "https://www.ercot.com/mp/data-products/data-product-details?id=NP6-785-ER"
)
# ERCOT's public MIS document-list JSON API. reportTypeId below is our best
# recollection of the id used for NP6-785-ER; ERCOT does occasionally
# renumber these, and this could not be confirmed live (network blocked in
# this environment). Verify at ERCOT_REPORT_LANDING_URL if download_prices()
# stops finding documents.
ERCOT_DOC_LIST_URL = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS"
ERCOT_DOWNLOAD_URL = "https://www.ercot.com/misdownload/servlets/mirDownload"
ERCOT_REPORT_TYPE_ID = 13061

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Canonical column name -> accepted raw header spellings (matched after
# stripping everything but letters/digits and lowercasing, so "Delivery
# Date", "DeliveryDate", "delivery_date" etc. all match the same alias).
_COLUMN_ALIASES: dict[str, list[str]] = {
    "delivery_date": ["Delivery Date", "DeliveryDate"],
    "delivery_hour": ["Delivery Hour", "DeliveryHour"],
    "delivery_interval": ["Delivery Interval", "DeliveryInterval"],
    "settlement_point_name": ["Settlement Point Name", "SettlementPointName"],
    "settlement_point_price": ["Settlement Point Price", "SettlementPointPrice"],
    "repeated_hour_flag": ["Repeated Hour Flag", "RepeatedHourFlag"],
    "dst_flag": ["DSTFlag", "DST Flag"],
}

_REQUIRED_COLUMNS = {
    "delivery_date",
    "delivery_hour",
    "delivery_interval",
    "settlement_point_name",
    "settlement_point_price",
}


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


def _parse_dates(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series)
    try:
        return pd.to_datetime(series, format="%m/%d/%Y")
    except (ValueError, TypeError):
        return pd.to_datetime(series, errors="coerce")


def _interval_start_utc(
    dates: pd.Series,
    hours: pd.Series,
    intervals: pd.Series,
    flag: pd.Series | None,
    flag_means_dst: bool,
) -> pd.DatetimeIndex:
    """Compute UTC interval-start timestamps for ERCOT Delivery Hour/Interval rows.

    flag_means_dst=True: flag=='Y' rows are daylight time (fold=0, ambiguous=True).
    flag_means_dst=False: flag=='Y' rows are standard time (fold=1, ambiguous=False)
        -- this is the "Repeated Hour Flag" convention (Y = second/CST pass).
    """
    parsed_dates = _parse_dates(dates).reset_index(drop=True)
    hour_num = pd.to_numeric(hours, errors="coerce").reset_index(drop=True)
    interval_num = pd.to_numeric(intervals, errors="coerce").reset_index(drop=True)

    naive = (
        parsed_dates
        + pd.to_timedelta(hour_num - 1, unit="h")
        + pd.to_timedelta((interval_num - 1) * 15, unit="m")
    )

    if flag is not None:
        flag_norm = flag.reset_index(drop=True).astype(str).str.strip().str.upper()
        is_y = flag_norm == "Y"
    else:
        is_y = pd.Series(False, index=naive.index)

    ambiguous = is_y.to_numpy() if flag_means_dst else (~is_y).to_numpy()

    idx = pd.DatetimeIndex(naive)
    local = idx.tz_localize(LOCAL_TZ, ambiguous=ambiguous, nonexistent="shift_forward")
    return local.tz_convert("UTC")


def _parse_sheet(df: pd.DataFrame, zone: str, source: str) -> pd.Series:
    df = _rename_columns(df)
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"{source}: missing expected column(s) {sorted(missing)}; "
            f"found columns {list(df.columns)}"
        )

    df = df.dropna(
        subset=[
            "delivery_date",
            "delivery_hour",
            "delivery_interval",
            "settlement_point_price",
        ]
    )
    zone_mask = df["settlement_point_name"].astype(str).str.strip().str.upper() == zone.strip().upper()
    df = df[zone_mask]
    if df.empty:
        return pd.Series(dtype="float64", name="price_usd_kwh")

    if "repeated_hour_flag" in df.columns:
        flag = df["repeated_hour_flag"]
        flag_means_dst = False  # Y = second (CST/fold=1) occurrence
    elif "dst_flag" in df.columns:
        flag = df["dst_flag"]
        flag_means_dst = True  # Y = still daylight time (CDT/fold=0)
    else:
        flag = None
        flag_means_dst = False

    ts = _interval_start_utc(
        df["delivery_date"], df["delivery_hour"], df["delivery_interval"], flag, flag_means_dst
    )
    price_mwh = pd.to_numeric(df["settlement_point_price"], errors="coerce")
    price_kwh = (price_mwh / 1000.0).to_numpy()
    return pd.Series(price_kwh, index=ts, name="price_usd_kwh")


def _parse_file(path: Path, zone: str) -> pd.Series:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
        pieces = [
            _parse_sheet(sheet_df, zone, f"{path.name}:{sheet_name}")
            for sheet_name, sheet_df in sheets.items()
            if not sheet_df.empty
        ]
        if not pieces:
            return pd.Series(dtype="float64", name="price_usd_kwh")
        return pd.concat(pieces)
    elif suffix == ".csv":
        df = pd.read_csv(path)
        return _parse_sheet(df, zone, path.name)
    else:
        raise ValueError(
            f"Unsupported ERCOT price file type '{suffix}' for {path}; expected .xlsx or .csv"
        )


def _discover_source_files(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    files: list[Path] = []
    for pattern in ("*.xlsx", "*.csv"):
        files.extend(p for p in data_dir.rglob(pattern) if not p.name.startswith("~$"))
    return sorted(set(files))


def _no_files_error(data_dir: Path) -> FileNotFoundError:
    return FileNotFoundError(
        f"No ERCOT price files found in {data_dir}.\n\n"
        f"Please download '{ERCOT_REPORT_NAME}' (ERCOT report {ERCOT_REPORT_ID}) from "
        "ercot.com under Market Prices "
        f"({ERCOT_REPORT_LANDING_URL}), and place the downloaded XLSX (or a 12301 / "
        f"SPPHLZNP6905 CSV export) file(s) in {data_dir}. Then call load_prices() again."
    )


def _read_cache(cache_path: Path) -> pd.Series:
    try:
        cached_df = pd.read_parquet(cache_path)
    except Exception:
        return pd.Series(dtype="float64", name="price_usd_kwh")
    if "price_usd_kwh" not in cached_df.columns:
        return pd.Series(dtype="float64", name="price_usd_kwh")
    series = cached_df["price_usd_kwh"]
    series.name = "price_usd_kwh"
    series.index.name = "ts"
    return series


def _write_cache(series: pd.Series, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    frame = series.to_frame(name="price_usd_kwh")
    frame.index.name = "ts"
    frame.to_parquet(cache_path)


def load_prices(zone: str = "LZ_NORTH", data_dir: Path = Path("data/ercot")) -> pd.Series:
    """Load ERCOT RTM settlement point prices for `zone` as a $/kWh Series.

    Scans `data_dir` for user-downloaded price files (see module docstring
    for supported shapes), parses + concatenates + dedupes them, and returns
    a tz-aware UTC, 15-minute, interval-start-indexed, sorted Series named
    "price_usd_kwh". Results are cached to `data_dir/<zone>.parquet` and the
    cache is rebuilt automatically whenever any source file is newer than it.

    Raises FileNotFoundError with instructions if no source files are found.
    """
    data_dir = Path(data_dir)
    source_files = _discover_source_files(data_dir)
    if not source_files:
        raise _no_files_error(data_dir)

    cache_path = data_dir / f"{zone}.parquet"
    newest_source_mtime = max(f.stat().st_mtime for f in source_files)
    if cache_path.exists() and cache_path.stat().st_mtime >= newest_source_mtime:
        cached = _read_cache(cache_path)
        if not cached.empty:
            return cached

    pieces: list[pd.Series] = []
    for f in source_files:
        try:
            s = _parse_file(f, zone)
        except Exception as exc:
            raise ValueError(f"Failed to parse ERCOT price file {f}: {exc}") from exc
        if not s.empty:
            pieces.append(s)

    if not pieces:
        raise FileNotFoundError(
            f"Found {len(source_files)} file(s) in {data_dir} but none contained rows for "
            f"settlement point '{zone}'. Check the zone/hub name (e.g. LZ_NORTH, LZ_HOUSTON, "
            "LZ_SOUTH, LZ_WEST, HB_HUBAVG) or verify the downloaded file covers the right point."
        )

    combined = pd.concat(pieces)
    combined = combined[~combined.index.isna()]
    combined = combined.sort_index()
    combined = combined.groupby(level=0).mean()
    combined.name = "price_usd_kwh"
    combined.index.name = "ts"

    _write_cache(combined, cache_path)
    return combined


def download_prices(
    zone: str = "LZ_NORTH",
    dest_dir: Path = Path("data/ercot"),
    report_type_id: int = ERCOT_REPORT_TYPE_ID,
    timeout: float = 30.0,
) -> Path:
    """Best-effort automatic download of the latest ERCOT NP6-785-ER workbook.

    This hits ERCOT's public MIS document-list API to find the most recent
    posting of "Historical RTM Load Zone and Hub Prices" and downloads it
    into `dest_dir`. ERCOT's report picker and doc-list API can change
    without notice, and this could not be exercised against a live endpoint
    in this environment (network egress to ercot.com is blocked here). On
    any failure this raises RuntimeError with the manual-download fallback
    instructions -- load_prices() only needs files to exist in dest_dir, it
    never requires this function to have run.
    """
    import httpx

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json,*/*"}
    manual_hint = (
        f"Please download '{ERCOT_REPORT_NAME}' (report {ERCOT_REPORT_ID}) manually from "
        f"{ERCOT_REPORT_LANDING_URL}, save the XLSX under {dest_dir}, and re-run load_prices()."
    )

    try:
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            list_resp = client.get(ERCOT_DOC_LIST_URL, params={"reportTypeId": report_type_id})
            list_resp.raise_for_status()
            docs = list_resp.json()

            doc_entries = docs.get("ListDocsByRptTypeRes", {}).get("DocumentList") or []
            if not doc_entries:
                raise RuntimeError(f"ERCOT returned no documents for reportTypeId={report_type_id}")

            first = doc_entries[0]
            doc_id = (first.get("Document") or {}).get("DocID") or first.get("DocID")
            if not doc_id:
                raise RuntimeError("Could not find a DocID in ERCOT's document list response")

            dl_resp = client.get(ERCOT_DOWNLOAD_URL, params={"doclookupId": doc_id})
            dl_resp.raise_for_status()

            dest_path = dest_dir / f"ercot_{ERCOT_REPORT_ID}_{doc_id}.xlsx"
            dest_path.write_bytes(dl_resp.content)
            return dest_path
    except Exception as exc:
        raise RuntimeError(
            f"Automatic ERCOT download failed ({exc!r}). {manual_hint}"
        ) from exc
