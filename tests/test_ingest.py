"""Tests for energyanalyzer.ingest (ARCHITECTURE.md §4)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from energyanalyzer.ingest.smt import (
    QualityReport,
    load_greenbutton_xml,
    load_intervals,
    load_smt_csv,
)

CSV_HEADER = (
    "ESIID,USAGE_DATE,REVISION_DATE,USAGE_START_TIME,USAGE_END_TIME,"
    "USAGE_KWH,ESTIMATED_ACTUAL,CONSUMPTION_SURPLUSGENERATION"
)
REAL_CSV = Path("data/IntervalData.csv")


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def _hhmm(total_minutes: int) -> str:
    h, m = divmod(total_minutes % (24 * 60), 60)
    return f"{h:02d}:{m:02d}"


def _slots_for_day(kind: str) -> list[tuple[str, str]]:
    """Return list of (start, end) HH:MM strings for one local calendar day.

    kind: "normal" (96), "fallback" (100, hour 01:00-01:45 repeated),
    "springforward" (92, hour 02:00-02:45 entirely absent).
    """
    starts_min = list(range(0, 24 * 60, 15))
    if kind == "fallback":
        # duplicate the 01:00-01:45 block right after itself
        extra = list(range(60, 120, 15))
        idx = starts_min.index(60)
        starts_min = starts_min[:idx] + extra + starts_min[idx:]
    elif kind == "springforward":
        gap = set(range(120, 180, 15))
        starts_min = [m for m in starts_min if m not in gap]
    elif kind != "normal":
        raise ValueError(kind)

    out = []
    for m in starts_min:
        start = _hhmm(m)
        end = _hhmm(m + 15)
        out.append((start, end))
    return out


def make_smt_csv(
    tmp_path: Path,
    date: str,
    kind: str = "normal",
    blank_indices: tuple[int, ...] = (),
    filename: str = "IntervalData.csv",
) -> Path:
    """Write a synthetic single-day SMT CSV covering both channels."""
    slots = _slots_for_day(kind)
    lines = [CSV_HEADER]
    for label, base_val in (("Consumption", 0.5), ("Surplus Generation", 0.1)):
        for i, (start, end) in enumerate(slots):
            kwh = "" if (label == "Consumption" and i in blank_indices) else f"{base_val:.3f}"
            lines.append(
                f"'10443720006274163,{date},{date} 07:00:00,{start},{end},"
                f"{kwh},A,{label}"
            )
    path = tmp_path / filename
    path.write_text("\n".join(lines) + "\n")
    return path


def make_greenbutton_xml(
    tmp_path: Path,
    start_epoch: int,
    n_intervals: int,
    value_wh: float,
    flow_direction: int,
    filename: str,
    prefixed_ns: bool = False,
) -> Path:
    """Write a minimal single-channel Green Button Atom XML file."""
    readings = []
    for i in range(n_intervals):
        s = start_epoch + i * 900
        if prefixed_ns:
            readings.append(
                f"<espi:IntervalReading><espi:timePeriod><espi:duration>900</espi:duration>"
                f"<espi:start>{s}</espi:start></espi:timePeriod>"
                f"<espi:value>{value_wh}</espi:value></espi:IntervalReading>"
            )
        else:
            readings.append(
                f"<IntervalReading><timePeriod><duration>900</duration>"
                f"<start>{s}</start></timePeriod>"
                f"<value>{value_wh}</value></IntervalReading>"
            )
    readings_xml = "".join(readings)

    if prefixed_ns:
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:espi="http://naesb.org/espi">
<entry><title>ReadingType</title>
<content><espi:ReadingType><espi:flowDirection>{flow_direction}</espi:flowDirection></espi:ReadingType></content>
</entry>
<entry><title>IntervalBlock</title>
<content><espi:IntervalBlock>{readings_xml}</espi:IntervalBlock></content>
</entry>
</feed>"""
    else:
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:espi="http://naesb.org/espi">
<entry><title>ReadingType</title>
<content><ReadingType xmlns="http://naesb.org/espi"><flowDirection>{flow_direction}</flowDirection></ReadingType></content>
</entry>
<entry><title>IntervalBlock</title>
<content><IntervalBlock xmlns="http://naesb.org/espi">{readings_xml}</IntervalBlock></content>
</entry>
</feed>"""
    path = tmp_path / filename
    path.write_text(content)
    return path


# --------------------------------------------------------------------------- #
# CSV: normal day
# --------------------------------------------------------------------------- #
def test_normal_day(tmp_path):
    path = make_smt_csv(tmp_path, "07/01/2025", kind="normal")
    df, report = load_smt_csv(path)

    assert list(df.columns) == ["import_kwh", "export_kwh"]
    assert len(df) == 96
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing
    assert not df.index.has_duplicates
    assert df["import_kwh"].sum() == pytest.approx(96 * 0.5)
    assert df["export_kwh"].sum() == pytest.approx(96 * 0.1)

    assert isinstance(report, QualityReport)
    assert report.rows_per_channel == {"import_kwh": 96, "export_kwh": 96}
    assert report.blank_count == 0
    assert report.estimated_count == 0
    assert report.missing_interval_count == 0
    assert report.duplicate_count == 0

    local_start = df.index.min().tz_convert("America/Chicago")
    assert str(local_start) == "2025-07-01 00:00:00-05:00"


# --------------------------------------------------------------------------- #
# CSV: blank values
# --------------------------------------------------------------------------- #
def test_blank_values_become_zero_and_are_counted(tmp_path):
    blanks = (0, 5, 10)
    path = make_smt_csv(tmp_path, "07/02/2025", kind="normal", blank_indices=blanks)
    df, report = load_smt_csv(path)

    assert len(df) == 96
    assert report.blank_count == len(blanks)
    # blanked rows are import channel (Consumption) -> zero-filled, never dropped
    local_idx = df.index.tz_convert("America/Chicago")
    for i in blanks:
        expected_local_hour_min = _slots_for_day("normal")[i][0]
        row = df[local_idx.strftime("%H:%M") == expected_local_hour_min]
        assert (row["import_kwh"] == 0.0).all()


# --------------------------------------------------------------------------- #
# CSV: DST fall-back day (2025-11-02) -- 100 rows/channel
# --------------------------------------------------------------------------- #
def test_dst_fallback_day(tmp_path):
    path = make_smt_csv(tmp_path, "11/02/2025", kind="fallback")
    df, report = load_smt_csv(path)

    assert len(df) == 100
    assert not df.index.has_duplicates
    assert df.index.is_monotonic_increasing
    assert report.rows_per_channel == {"import_kwh": 100, "export_kwh": 100}
    assert report.duplicate_count == 0
    assert report.missing_interval_count == 0

    # first pass through 01:00 is CDT (UTC-5 -> 06:00Z), second is CST (UTC-6 -> 07:00Z)
    first_0100 = df.index[4]
    second_0100 = df.index[8]
    assert first_0100 == pd.Timestamp("2025-11-02 06:00:00", tz="UTC")
    assert second_0100 == pd.Timestamp("2025-11-02 07:00:00", tz="UTC")
    assert (second_0100 - first_0100) == pd.Timedelta(hours=1)


# --------------------------------------------------------------------------- #
# CSV: DST spring-forward day (2026-03-08) -- 92 rows/channel
# --------------------------------------------------------------------------- #
def test_dst_springforward_day(tmp_path):
    path = make_smt_csv(tmp_path, "03/08/2026", kind="springforward")
    df, report = load_smt_csv(path)

    assert len(df) == 92
    assert not df.index.has_duplicates
    assert report.rows_per_channel == {"import_kwh": 92, "export_kwh": 92}
    assert report.missing_interval_count == 0

    local_idx = df.index.tz_convert("America/Chicago")
    # no 02:00-02:45 local times should be present
    assert not any((h, m) in {(2, 0), (2, 15), (2, 30), (2, 45)} for h, m in zip(local_idx.hour, local_idx.minute))
    # 01:45 is CST (before spring forward), 03:00 is CDT (after)
    assert local_idx[7].strftime("%H:%M") == "01:45"
    assert local_idx.tz.key if hasattr(local_idx.tz, "key") else True


# --------------------------------------------------------------------------- #
# Green Button XML: single-channel merge
# --------------------------------------------------------------------------- #
def test_greenbutton_single_channel_merge(tmp_path):
    start_epoch = 1751328000  # 2025-07-01T00:00:00Z per spec's literal epoch=UTC handling
    n = 8
    import_path = make_greenbutton_xml(
        tmp_path, start_epoch, n, value_wh=500.0, flow_direction=1, filename="import.xml"
    )
    export_path = make_greenbutton_xml(
        tmp_path,
        start_epoch,
        n,
        value_wh=125.0,
        flow_direction=19,
        filename="export.xml",
        prefixed_ns=True,
    )

    df, report = load_greenbutton_xml([import_path, export_path])

    assert len(df) == n
    assert df["import_kwh"].sum() == pytest.approx(n * 0.5)
    assert df["export_kwh"].sum() == pytest.approx(n * 0.125)
    assert report.rows_per_channel == {"import_kwh": n, "export_kwh": n}
    assert not df.index.has_duplicates
    assert str(df.index.tz) == "UTC"
    assert df.index.min() == pd.Timestamp(start_epoch, unit="s", tz="UTC")


def test_greenbutton_missing_channel_filled_and_noted(tmp_path):
    start_epoch = 1751328000
    path = make_greenbutton_xml(
        tmp_path, start_epoch, 4, value_wh=400.0, flow_direction=1, filename="import_only.xml"
    )
    df, report = load_greenbutton_xml([path])

    assert (df["export_kwh"] == 0.0).all()
    assert any("export_kwh" in w and "missing" in w for w in report.warnings)


# --------------------------------------------------------------------------- #
# load_intervals: prefers CSV, caches to parquet, reuses cache
# --------------------------------------------------------------------------- #
def test_load_intervals_prefers_csv_and_caches(tmp_path):
    make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalData.csv")
    make_greenbutton_xml(
        tmp_path, 1751328000, 96, value_wh=999.0, flow_direction=1, filename="GreenButton.xml"
    )

    df, report = load_intervals(data_dir=tmp_path)
    assert len(df) == 96
    # CSV import value (0.5/interval) should win over the XML's 999 Wh stub
    assert df["import_kwh"].iloc[0] == pytest.approx(0.5)

    cache_path = tmp_path / "intervals.parquet"
    assert cache_path.exists()

    # Unchanged source set -> the cache is reused.
    df_again, report_again = load_intervals(data_dir=tmp_path)
    assert "cache" in report_again.source
    assert len(df_again) == 96
    assert df_again["import_kwh"].iloc[0] == pytest.approx(0.5)

    # Removing a source CHANGES the source set, so the cache is rebuilt from
    # what remains (here the XML's 999 Wh) rather than replaying a frame built
    # from a file the user deleted. Deleting a superseded export is exactly how
    # a stale window used to survive: every remaining file is older than the
    # cache, so a newest-mtime check saw nothing to do.
    (tmp_path / "IntervalData.csv").unlink()
    df2, _ = load_intervals(data_dir=tmp_path)
    assert df2["import_kwh"].iloc[0] == pytest.approx(0.999)

    # With no sources left at all, the cache is the only thing to serve.
    (tmp_path / "GreenButton.xml").unlink()
    df3, report3 = load_intervals(data_dir=tmp_path)
    assert "cache" in report3.source
    assert len(df3) == 96


def test_load_intervals_raises_without_source_or_cache(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_intervals(data_dir=tmp_path)


# --------------------------------------------------------------------------- #
# QualityReport readability
# --------------------------------------------------------------------------- #
def test_quality_report_str_is_readable(tmp_path):
    path = make_smt_csv(tmp_path, "07/01/2025", kind="normal")
    _, report = load_smt_csv(path)
    text = str(report)
    assert "QualityReport" in text
    assert "import_kwh" in text
    assert "export_kwh" in text


# --------------------------------------------------------------------------- #
# Integration test against the real user CSV (skips gracefully if absent)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not REAL_CSV.exists(), reason="data/IntervalData.csv not present")
def test_real_interval_csv_integration():
    df, report = load_smt_csv(REAL_CSV)

    assert len(df) == 35040
    assert df["import_kwh"].sum() == pytest.approx(11278, abs=1)
    assert df["export_kwh"].sum() == pytest.approx(9803, abs=1)
    assert not df.index.has_duplicates
    assert df.index.is_monotonic_increasing

    local_start = df.index.min().tz_convert("America/Chicago")
    local_end = df.index.max().tz_convert("America/Chicago")
    assert local_start == pd.Timestamp("2025-07-01 00:00:00-05:00", tz="America/Chicago")
    assert local_end == pd.Timestamp("2026-06-30 23:45:00-05:00", tz="America/Chicago")


def test_deleting_a_superseded_export_rebuilds_the_cache(tmp_path):
    """Two overlapping exports merge; deleting the older one must take effect.

    Regression for the exact sequence a user hits when SmartMeter Texas hands
    out rolling 12-month windows: download a second export, notice the app is
    now billing 13-14 months, delete the older file -- and keep getting the
    merged frame, because the cache was only rebuilt when a *source* was newer
    than it, and deleting a file makes nothing newer.
    """
    make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalData.csv")
    make_smt_csv(tmp_path, "07/02/2025", kind="normal", filename="IntervalData (3).csv")

    merged, _ = load_intervals(data_dir=tmp_path)
    assert len(merged) == 192  # both days

    (tmp_path / "IntervalData.csv").unlink()
    after, _ = load_intervals(data_dir=tmp_path)
    assert len(after) == 96, "cache must be rebuilt from the remaining export"
    assert after.index.min() > merged.index.min()


def test_cache_rebuilds_for_a_back_dated_new_export(tmp_path):
    """A file restored from a backup or unzipped carries an old mtime; the
    cache must still notice it (same defect as the ERCOT price cache)."""
    import os
    import time

    make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalData.csv")
    first, _ = load_intervals(data_dir=tmp_path)
    assert len(first) == 96

    make_smt_csv(tmp_path, "07/02/2025", kind="normal", filename="IntervalData (3).csv")
    old = time.time() - 86400 * 30
    os.utime(tmp_path / "IntervalData (3).csv", (old, old))

    after, _ = load_intervals(data_dir=tmp_path)
    assert len(after) == 192, "a back-dated export must not be ignored"
