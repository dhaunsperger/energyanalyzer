"""Tests for energyanalyzer.ingest (ARCHITECTURE.md §4)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from energyanalyzer.ingest.smt import (
    QualityReport,
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
# load_intervals: merges exports, caches to parquet, reuses cache
# --------------------------------------------------------------------------- #
def test_load_intervals_merges_exports_and_caches(tmp_path):
    import os
    import time

    older = make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalData.csv")
    # A second export covering the same day with a different reading, named so
    # that filename order cannot be what decides the winner.
    newer = make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalDataNEW.csv")
    newer.write_text(newer.read_text().replace("0.500", "0.900"))
    now = time.time()
    os.utime(older, (now - 600, now - 600))
    os.utime(newer, (now, now))

    df, report = load_intervals(data_dir=tmp_path)
    assert len(df) == 96, "the overlap is deduplicated, never summed"
    assert df["import_kwh"].iloc[0] == pytest.approx(0.9), "newest download wins"

    cache_path = tmp_path / "intervals.parquet"
    assert cache_path.exists()

    # Unchanged source set -> the cache is reused.
    df_again, report_again = load_intervals(data_dir=tmp_path)
    assert "cache" in report_again.source
    assert df_again["import_kwh"].iloc[0] == pytest.approx(0.9)

    # Removing a source CHANGES the source set, so the cache is rebuilt from
    # what remains. Deleting a superseded export is exactly how a stale window
    # used to survive: every remaining file is older than the cache, so a
    # newest-mtime check saw nothing to do.
    newer.unlink()
    df2, _ = load_intervals(data_dir=tmp_path)
    assert df2["import_kwh"].iloc[0] == pytest.approx(0.5)

    # With no sources left at all, the cache is the only thing to serve.
    older.unlink()
    df3, report3 = load_intervals(data_dir=tmp_path)
    assert "cache" in report3.source
    assert len(df3) == 96


def test_green_button_xml_is_not_read_and_says_so(tmp_path):
    """Dropping an XML in should fail loudly, not look like an empty folder."""
    (tmp_path / "GreenButton.xml").write_text("<feed/>")
    with pytest.raises(FileNotFoundError) as excinfo:
        load_intervals(data_dir=tmp_path)
    message = str(excinfo.value)
    assert "Green Button is no longer read" in message
    assert "smartmetertexas.com" in message


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


def test_cache_manifest_survives_sub_second_replacement(tmp_path):
    """Two files written in the same second must still invalidate the cache.

    The manifest truncated mtime to whole seconds, so a scripted replacement of
    an export with a different-content file of the same size within one second
    looked unchanged.
    """
    from energyanalyzer.ingest.smt import _source_manifest

    a = tmp_path / "IntervalData.csv"
    make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalData.csv")
    first = _source_manifest([a])
    # Same size, same second, different content.
    a.write_text(a.read_text().replace("0.500", "0.600"))
    assert _source_manifest([a]) != first, "sub-second replacement must be detected"


def test_newer_export_wins_an_overlapping_interval(tmp_path):
    """Where two exports disagree about the same interval, keep the newer read.

    Usually they agree, so this rarely bites -- but a blank in an earlier export
    becomes 0.0 kWh, and an estimated read later settles to an actual. Those are
    exactly the intervals where the values differ, and the fresher pull is right.

    Regression: dedup ran after sort_index, which pandas does not sort stably
    across a mix of unique and duplicated keys, so the survivor of an overlap
    was decided by the sort's internals rather than by any rule.
    """
    import os
    import time

    old = make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalData.csv")
    # Same day, same ESIID, different reading -- and a name that sorts LATER,
    # so filename order cannot be what decides this.
    new = make_smt_csv(
        tmp_path, "07/01/2025", kind="normal", filename="IntervalDataNEW.csv"
    )
    new.write_text(new.read_text().replace("0.500", "0.900"))
    now = time.time()
    os.utime(old, (now - 600, now - 600))
    os.utime(new, (now, now))

    df, report = load_intervals(data_dir=tmp_path)

    assert len(df) == 96, "the overlap must be deduplicated, never double-counted"
    assert df["import_kwh"].iloc[0] == pytest.approx(0.9), "newest export wins"
    assert any("most recently downloaded" in w for w in report.warnings)


def test_overlap_is_deduplicated_regardless_of_which_export_is_newer(tmp_path):
    """The load-bearing guarantee: one row per interval, whichever file is newer."""
    import os
    import time

    a = make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalData.csv")
    b = make_smt_csv(tmp_path, "07/01/2025", kind="normal", filename="IntervalData (3).csv")
    now = time.time()
    for older, newer in ((a, b), (b, a)):
        os.utime(older, (now - 600, now - 600))
        os.utime(newer, (now, now))
        (tmp_path / "intervals.parquet").unlink(missing_ok=True)
        df, _ = load_intervals(data_dir=tmp_path)
        assert len(df) == 96
        assert df["import_kwh"].sum() == pytest.approx(96 * 0.5)
