"""Ingest SmartMeter Texas (SMT) interval CSV and NAESB Green Button XML
exports into the canonical interval frame described in ARCHITECTURE.md §4.

Public API:
    load_smt_csv(path) -> (df, QualityReport)
    load_greenbutton_xml(paths) -> (df, QualityReport)
    load_intervals(data_dir=Path("data")) -> (df, QualityReport)
    QualityReport
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from energyanalyzer.core.models import validate_intervals

LOCAL_TZ = "America/Chicago"

CHANNEL_MAP = {
    "Consumption": "import_kwh",
    "Surplus Generation": "export_kwh",
}
CANONICAL_COLUMNS = ("import_kwh", "export_kwh")


# --------------------------------------------------------------------------- #
# Quality report
# --------------------------------------------------------------------------- #
@dataclass
class QualityReport:
    """Summary of what was found/assumed while loading interval data."""

    source: str = ""
    start: Optional[pd.Timestamp] = None
    end: Optional[pd.Timestamp] = None
    rows_per_channel: dict = field(default_factory=dict)
    blank_count: int = 0
    estimated_count: int = 0
    missing_interval_count: int = 0
    duplicate_count: int = 0
    warnings: list = field(default_factory=list)

    def __str__(self) -> str:
        lines = [f"QualityReport(source={self.source!r})"]
        if self.start is not None and self.end is not None:
            lines.append(f"  range: {self.start} .. {self.end}")
        else:
            lines.append("  range: <empty>")
        rows = ", ".join(f"{k}={v}" for k, v in sorted(self.rows_per_channel.items()))
        lines.append(f"  rows per channel: {rows or '<none>'}")
        lines.append(
            f"  blanks={self.blank_count}  estimated(E)={self.estimated_count}  "
            f"missing_intervals={self.missing_interval_count}  "
            f"duplicates={self.duplicate_count}"
        )
        if self.warnings:
            lines.append("  warnings:")
            for w in self.warnings:
                lines.append(f"    - {w}")
        return "\n".join(lines)


def _finalize_report(df: pd.DataFrame, report: QualityReport) -> None:
    """Fill in date range + missing-interval count from a finished frame.

    True UTC instants are uniformly 15-min spaced year-round regardless of
    local DST (DST only relabels local clock time, not the flow of UTC), so
    the expected grid is just a plain UTC range from the frame's first to
    last timestamp -- no local-calendar-day reasoning needed here."""
    if df.empty:
        return
    report.start = df.index.min()
    report.end = df.index.max()

    expected = pd.date_range(start=report.start, end=report.end, freq="15min", tz="UTC")
    missing = expected.difference(df.index)
    report.missing_interval_count = int(len(missing))
    if report.missing_interval_count:
        report.warnings.append(
            f"{report.missing_interval_count} interval(s) missing vs the expected 15-min grid"
        )


# --------------------------------------------------------------------------- #
# SMT CSV
# --------------------------------------------------------------------------- #
def _label_parse_day(day_sub: pd.DataFrame, col: str, warnings: list) -> pd.Series:
    """Fallback for a calendar day whose row count doesn't match the expected
    DST-aware grid length: localize each row from its own USAGE_START_TIME
    label (fold resolved by occurrence order), dropping nonexistent-time rows
    (e.g. blank placeholder rows some SMT exports keep for the 02:00-02:45
    spring-forward gap)."""
    kwh = pd.to_numeric(day_sub["USAGE_KWH"], errors="coerce").fillna(0.0)
    local_naive = pd.to_datetime(
        day_sub["USAGE_DATE"] + " " + day_sub["USAGE_START_TIME"], format="%m/%d/%Y %H:%M"
    )
    occurrence = day_sub.groupby([day_sub["USAGE_DATE"], day_sub["USAGE_START_TIME"]]).cumcount()
    ambiguous = (occurrence == 0).to_numpy()

    local_aware = local_naive.dt.tz_localize(LOCAL_TZ, ambiguous=ambiguous, nonexistent="NaT")
    nonexistent_mask = local_aware.isna().to_numpy()
    n_nonexistent = int(nonexistent_mask.sum())
    if n_nonexistent:
        date_str = day_sub["USAGE_DATE"].iloc[0]
        warnings.append(
            f"{col}: {date_str} dropped {n_nonexistent} row(s) at a nonexistent local time "
            "(spring-forward DST gap)"
        )

    keep = ~nonexistent_mask
    idx = pd.DatetimeIndex(local_aware[keep]).tz_convert("UTC")
    vals = kwh.to_numpy()[keep]
    return pd.Series(vals, index=idx, name=col)


def _channel_series(sub: pd.DataFrame, col: str, warnings: list) -> tuple:
    """Convert one channel's raw SMT rows into a deduped, UTC-indexed Series
    of kWh values. Returns (series, blank_count, estimated_count, dup_count).

    Rows are grouped by local calendar day (USAGE_DATE) and, when a day's row
    count matches the DST-aware expected grid length for that date (96 for a
    normal day, 100 fall-back, 92 spring-forward), timestamps are assigned by
    file-order position rather than by parsing USAGE_START_TIME text. This
    sidesteps a known SMT export quirk where, on the fall-back day, one row's
    USAGE_START_TIME can be mislabeled (e.g. printed as "02:00" while
    USAGE_END_TIME still correctly reads "01:15"). File order within a day is
    always chronological, so position-based assignment is reliable whenever
    the count matches. Only when the count is off (real missing/extra data,
    or blank placeholder rows on a spring-forward day) do we fall back to
    explicit per-row label parsing.
    """
    kwh_raw = sub["USAGE_KWH"]
    blank_mask = kwh_raw.isna() | (kwh_raw.astype(str).str.strip() == "")
    blanks_total = int(blank_mask.sum())
    est_total = int((sub["ESTIMATED_ACTUAL"].astype(str) == "E").sum())

    day_series = []
    for date_str in pd.unique(sub["USAGE_DATE"].to_numpy()):
        day_sub = sub[sub["USAGE_DATE"] == date_str]
        n_rows = len(day_sub)
        day_start = pd.to_datetime(date_str, format="%m/%d/%Y")
        day_end = day_start + pd.Timedelta(days=1)
        expected = pd.date_range(
            start=day_start, end=day_end, freq="15min", tz=LOCAL_TZ, inclusive="left"
        )

        if len(expected) == n_rows:
            vals = pd.to_numeric(day_sub["USAGE_KWH"], errors="coerce").fillna(0.0).to_numpy()
            s = pd.Series(vals, index=expected.tz_convert("UTC"), name=col)
        else:
            warnings.append(
                f"{col}: {date_str} has {n_rows} row(s) but expected {len(expected)} for "
                "this local calendar day; falling back to explicit time-label parsing"
            )
            s = _label_parse_day(day_sub, col, warnings)
        day_series.append(s)

    if day_series:
        combined = pd.concat(day_series).sort_index()
    else:
        combined = pd.Series(dtype=float, name=col)

    dup_mask = combined.index.duplicated(keep="first")
    n_dup = int(dup_mask.sum())
    if n_dup:
        warnings.append(f"{col}: collapsed {n_dup} duplicate interval timestamp(s) (kept first)")
    combined = combined[~dup_mask]

    return combined, blanks_total, est_total, n_dup


def load_smt_csv(path: Union[str, Path]) -> tuple:
    """Parse a SmartMeter Texas interval-data CSV export into the canonical
    (UTC-indexed, import_kwh/export_kwh) frame. See ARCHITECTURE.md §4."""
    path = Path(path)
    raw = pd.read_csv(path, dtype=str)
    raw.columns = [c.strip() for c in raw.columns]

    if "ESIID" in raw.columns:
        raw["ESIID"] = raw["ESIID"].str.lstrip("'")

    report = QualityReport(source=str(path))

    channel_col = raw["CONSUMPTION_SURPLUSGENERATION"]
    unknown = sorted(set(channel_col.dropna().unique()) - set(CHANNEL_MAP))
    if unknown:
        report.warnings.append(f"unknown CONSUMPTION_SURPLUSGENERATION labels ignored: {unknown}")

    series_by_col = {}
    for label, col in CHANNEL_MAP.items():
        sub = raw[channel_col == label]
        if sub.empty:
            continue
        s, blanks, est, n_dup = _channel_series(sub, col, report.warnings)
        series_by_col[col] = s
        report.rows_per_channel[col] = int(len(s))
        report.blank_count += blanks
        report.estimated_count += est
        report.duplicate_count += n_dup

    if not series_by_col:
        raise ValueError(f"no recognizable Consumption/Surplus Generation rows found in {path}")

    df = pd.DataFrame(series_by_col)
    for col in CANONICAL_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0
            report.warnings.append(
                f"channel {col} entirely absent from {path.name}; filled with 0.0"
            )
    df = df[list(CANONICAL_COLUMNS)].fillna(0.0).sort_index()
    df = df[~df.index.duplicated(keep="first")]

    _finalize_report(df, report)
    validate_intervals(df)
    return df, report


# --------------------------------------------------------------------------- #
# Green Button (NAESB ESPI) XML
# --------------------------------------------------------------------------- #
def _local_tag(elem: ET.Element) -> str:
    tag = elem.tag
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_all(root: ET.Element, tagname: str) -> list:
    return [e for e in root.iter() if _local_tag(e) == tagname]


def _file_channel(root: ET.Element, path: Path, warnings: list) -> str:
    """Inspect the file's ReadingType entries to figure out whether this file
    carries the import (flowDirection=1) or export (flowDirection=19) channel."""
    flow_dirs = set()
    for rt in _find_all(root, "ReadingType"):
        for fd in _find_all(rt, "flowDirection"):
            if fd.text is not None:
                flow_dirs.add(fd.text.strip())

    if not flow_dirs:
        warnings.append(f"{path.name}: no flowDirection found; assuming import (flowDirection=1)")
        return "import_kwh"
    if len(flow_dirs) > 1:
        warnings.append(
            f"{path.name}: multiple flowDirection values {sorted(flow_dirs)} found; using first"
        )
    flow = sorted(flow_dirs)[0]
    if flow == "19":
        return "export_kwh"
    if flow == "1":
        return "import_kwh"
    warnings.append(f"{path.name}: unrecognized flowDirection={flow!r}; assuming import")
    return "import_kwh"


def _read_greenbutton_file(path: Path, warnings: list) -> tuple:
    """Returns (channel_col, Series[value_wh-derived kwh] indexed by UTC start)."""
    tree = ET.parse(path)
    root = tree.getroot()
    col = _file_channel(root, path, warnings)

    starts = []
    values_wh = []
    for ir in _find_all(root, "IntervalReading"):
        tp_list = _find_all(ir, "timePeriod")
        start_el = _find_all(tp_list[0], "start") if tp_list else _find_all(ir, "start")
        val_el = _find_all(ir, "value")
        if not start_el or not val_el or start_el[0].text is None or val_el[0].text is None:
            continue
        starts.append(int(start_el[0].text))
        values_wh.append(float(val_el[0].text))

    if not starts:
        warnings.append(f"{path.name}: no IntervalReading elements found")
        return col, pd.Series(dtype=float)

    idx = pd.to_datetime(pd.Series(starts), unit="s", utc=True)
    s = pd.Series([v / 1000.0 for v in values_wh], index=pd.DatetimeIndex(idx), name=col)
    return col, s


def load_greenbutton_xml(paths) -> tuple:
    """Load one or more NAESB ESPI Green Button Atom XML files and merge them
    into the canonical frame. A file may carry only one channel (import or
    export); the missing channel is filled with 0.0 and noted."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError("load_greenbutton_xml requires at least one path")

    report = QualityReport(source=", ".join(str(p) for p in paths))
    series_lists: dict = {}

    for path in paths:
        col, s = _read_greenbutton_file(path, report.warnings)
        if s.empty:
            continue
        series_lists.setdefault(col, []).append(s)

    if not series_lists:
        raise ValueError(f"no interval readings found in any of {paths}")

    result_cols = {}
    for col, series_list in series_lists.items():
        combined = pd.concat(series_list).sort_index()
        dup_mask = combined.index.duplicated(keep="first")
        n_dup = int(dup_mask.sum())
        if n_dup:
            report.warnings.append(f"{col}: collapsed {n_dup} duplicate interval timestamp(s)")
        report.duplicate_count += n_dup
        combined = combined[~dup_mask]
        result_cols[col] = combined
        report.rows_per_channel[col] = int(len(combined))

    df = pd.DataFrame(result_cols)
    for col in CANONICAL_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0
            report.warnings.append(
                f"channel {col} missing from all Green Button file(s); filled with 0.0"
            )
    df = df[list(CANONICAL_COLUMNS)].sort_index().fillna(0.0)
    df = df[~df.index.duplicated(keep="first")]

    _finalize_report(df, report)
    validate_intervals(df)
    return df, report


# --------------------------------------------------------------------------- #
# Convenience loader with parquet caching
# --------------------------------------------------------------------------- #
def _source_manifest(source_paths: list) -> list:
    """Identity of the source set a cache was built from: name, size, mtime.

    Compared instead of "is the cache newer than the newest source?", which
    misses the two changes that matter most here. REMOVING a file leaves every
    survivor older than the cache, so deleting a superseded export kept serving
    the merged frame that still contained it; and a file landed by `cp -p`, an
    unzip, or a restored backup carries an old mtime, so a newly added export
    was ignored outright. Adding, removing, or replacing any file changes this.
    """
    return sorted([p.name, p.stat().st_size, int(p.stat().st_mtime)] for p in source_paths)


def _manifest_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(".manifest.json")


def _read_manifest(cache_path: Path):
    try:
        return json.loads(_manifest_path(cache_path).read_text())
    except Exception:  # noqa: BLE001 -- absent/corrupt manifest just means "rebuild"
        return None


def load_intervals(data_dir: Union[str, Path] = Path("data")) -> tuple:
    """Load the canonical interval frame from `data_dir`, preferring
    IntervalData*.csv, falling back to GreenButton*.xml. Caches the result to
    `<data_dir>/intervals.parquet`, reusing it only while the set of source
    files is unchanged (see `_source_manifest`).

    Multiple exports are merged: overlapping intervals are deduplicated with
    the alphabetically-first file winning, which is how a re-download of the
    same period (carrying revised meter readings) supersedes the older copy.
    The merged span can therefore exceed 12 months -- see
    `core.models.describe_billing_window`, which is what decides the window a
    first-year cost is actually computed over."""
    data_dir = Path(data_dir)
    cache_path = data_dir / "intervals.parquet"

    csv_paths = sorted(data_dir.glob("IntervalData*.csv"))
    xml_paths = sorted(data_dir.glob("GreenButton*.xml"))
    source_paths = csv_paths if csv_paths else xml_paths

    if not source_paths:
        if cache_path.exists():
            df = pd.read_parquet(cache_path)
            validate_intervals(df)
            report = QualityReport(source=f"{cache_path} (cache; no source files present)")
            _finalize_report(df, report)
            report.warnings.append("loaded from cache; no source files found in data_dir")
            return df, report
        raise FileNotFoundError(
            f"No IntervalData*.csv or GreenButton*.xml found in {data_dir}, "
            f"and no cache at {cache_path}"
        )

    manifest = _source_manifest(source_paths)
    if cache_path.exists() and _read_manifest(cache_path) == manifest:
        df = pd.read_parquet(cache_path)
        validate_intervals(df)
        report = QualityReport(source=f"{cache_path} (cache)")
        _finalize_report(df, report)
        return df, report

    if csv_paths:
        frames = []
        report = QualityReport(source=", ".join(str(p) for p in csv_paths))
        for p in csv_paths:
            d, r = load_smt_csv(p)
            frames.append(d)
            report.warnings.extend(r.warnings)
            report.blank_count += r.blank_count
            report.estimated_count += r.estimated_count
            report.duplicate_count += r.duplicate_count
            for k, v in r.rows_per_channel.items():
                report.rows_per_channel[k] = report.rows_per_channel.get(k, 0) + v
        df = pd.concat(frames).sort_index() if len(frames) > 1 else frames[0]
        df = df[~df.index.duplicated(keep="first")]
    else:
        df, report = load_greenbutton_xml(xml_paths)

    validate_intervals(df)
    _finalize_report(df, report)

    data_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    _manifest_path(cache_path).write_text(json.dumps(manifest))
    return df, report
