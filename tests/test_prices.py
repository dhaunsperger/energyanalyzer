"""Tests for energyanalyzer.prices.ercot -- built entirely from synthetic
local fixtures (XLSX built with openpyxl, CSV built with pandas); no live
network access, per ARCHITECTURE.md §7 and the restricted-egress sandbox
this module was developed in.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd
import pytest

from energyanalyzer.prices import ercot

XLSX_HEADERS = [
    "Delivery Date",
    "Delivery Hour",
    "Delivery Interval",
    "Repeated Hour Flag",
    "Settlement Point Name",
    "Settlement Point Price",
]

CSV_HEADERS = [
    "DeliveryDate",
    "DeliveryHour",
    "DeliveryInterval",
    "SettlementPointName",
    "SettlementPointPrice",
    "DSTFlag",
]


def _write_xlsx(path: Path, sheets: dict[str, list[tuple]]) -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        ws.append(XLSX_HEADERS)
        for row in rows:
            ws.append(list(row))
    wb.save(path)


def _write_csv(path: Path, rows: list[tuple]) -> None:
    df = pd.DataFrame(rows, columns=CSV_HEADERS)
    df.to_csv(path, index=False)


# --------------------------------------------------------------------------- #
# Normal (non-ambiguous) day: verify UTC conversion, $/kWh scaling, zone filter
# --------------------------------------------------------------------------- #
def test_xlsx_normal_day_conversion_and_zone_filter(tmp_path: Path):
    data_dir = tmp_path / "ercot"
    data_dir.mkdir()
    rows = [
        # date, hour, interval, repeated_flag, zone, price($/MWh)
        ("07/15/2025", 1, 1, "N", "LZ_NORTH", 25.0),
        ("07/15/2025", 1, 1, "N", "LZ_HOUSTON", 999.0),  # different zone, must be excluded
        ("07/15/2025", 24, 4, "N", "LZ_NORTH", 40.0),
    ]
    _write_xlsx(data_dir / "rtm_2025.xlsx", {"Jul-25": rows})

    prices = ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)

    assert prices.name == "price_usd_kwh"
    assert isinstance(prices.index, pd.DatetimeIndex)
    assert str(prices.index.tz) == "UTC"
    assert prices.index.is_monotonic_increasing
    assert not prices.index.has_duplicates
    assert len(prices) == 2  # LZ_HOUSTON row excluded

    # hour=1,interval=1 -> local 00:00 CDT (UTC-5) on 2025-07-15 -> 05:00 UTC
    ts0 = pd.Timestamp("2025-07-15 05:00:00", tz="UTC")
    assert ts0 in prices.index
    assert prices.loc[ts0] == pytest.approx(25.0 / 1000.0)

    # hour=24,interval=4 -> local 23:45 CDT -> 2025-07-16 04:45 UTC
    ts1 = pd.Timestamp("2025-07-16 04:45:00", tz="UTC")
    assert ts1 in prices.index
    assert prices.loc[ts1] == pytest.approx(40.0 / 1000.0)


# --------------------------------------------------------------------------- #
# Fall-back repeated hour (2025-11-02): Repeated Hour Flag disambiguation
# --------------------------------------------------------------------------- #
def test_xlsx_fallback_repeated_hour(tmp_path: Path):
    data_dir = tmp_path / "ercot"
    data_dir.mkdir()
    rows = [
        ("11/02/2025", 1, 1, "N", "LZ_NORTH", 10.0),  # 00:00 CDT, only occurrence
        ("11/02/2025", 2, 1, "N", "LZ_NORTH", 20.0),  # 01:00 CDT, 1st pass (fold=0)
        ("11/02/2025", 2, 1, "Y", "LZ_NORTH", 30.0),  # 01:00 CST, 2nd pass (fold=1)
        ("11/02/2025", 3, 1, "N", "LZ_NORTH", 40.0),  # 02:00 CST, only occurrence
    ]
    _write_xlsx(data_dir / "rtm_nov2025.xlsx", {"Nov-25": rows})

    prices = ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)

    assert len(prices) == 4
    assert not prices.index.has_duplicates

    expected = {
        "2025-11-02 05:00:00": 10.0,  # hour=1 -> 00:00 CDT -> 05:00 UTC
        "2025-11-02 06:00:00": 20.0,  # hour=2 1st pass -> 01:00 CDT -> 06:00 UTC
        "2025-11-02 07:00:00": 30.0,  # hour=2 2nd pass -> 01:00 CST -> 07:00 UTC
        "2025-11-02 08:00:00": 40.0,  # hour=3 -> 02:00 CST -> 08:00 UTC
    }
    for ts_str, mwh_price in expected.items():
        ts = pd.Timestamp(ts_str, tz="UTC")
        assert ts in prices.index, f"missing expected timestamp {ts}"
        assert prices.loc[ts] == pytest.approx(mwh_price / 1000.0)

    # Chronological order must be strictly increasing UTC (no overlap/gap).
    assert list(prices.sort_index().index) == list(prices.index)


# --------------------------------------------------------------------------- #
# 12301 / SPPHLZNP6905-style CSV shape
# --------------------------------------------------------------------------- #
def test_csv_12301_shape(tmp_path: Path):
    data_dir = tmp_path / "ercot"
    data_dir.mkdir()
    rows = [
        ("07/15/2025", 1, 1, "LZ_NORTH", 25.0, "Y"),
        ("07/15/2025", 1, 1, "LZ_HOUSTON", 999.0, "Y"),
        ("07/15/2025", 2, 1, "LZ_NORTH", 26.5, "Y"),
    ]
    _write_csv(data_dir / "cdr.00012301.0000000000.SPPHLZNP6905_20250715_000000.csv", rows)

    prices = ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)

    assert len(prices) == 2
    ts0 = pd.Timestamp("2025-07-15 05:00:00", tz="UTC")
    ts1 = pd.Timestamp("2025-07-15 06:00:00", tz="UTC")
    assert prices.loc[ts0] == pytest.approx(25.0 / 1000.0)
    assert prices.loc[ts1] == pytest.approx(26.5 / 1000.0)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_load_prices_no_files_raises_helpful_error(tmp_path: Path):
    data_dir = tmp_path / "ercot_empty"
    with pytest.raises(FileNotFoundError) as excinfo:
        ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)
    msg = str(excinfo.value)
    assert "NP6-785-ER" in msg
    assert "Historical RTM Load Zone and Hub Prices" in msg
    assert str(data_dir) in msg


def test_load_prices_zone_missing_from_files_raises(tmp_path: Path):
    data_dir = tmp_path / "ercot"
    data_dir.mkdir()
    rows = [("07/15/2025", 1, 1, "N", "LZ_HOUSTON", 25.0)]
    _write_xlsx(data_dir / "rtm.xlsx", {"Jul-25": rows})
    with pytest.raises(FileNotFoundError) as excinfo:
        ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)
    assert "LZ_NORTH" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
def test_cache_written_and_reused(tmp_path: Path):
    data_dir = tmp_path / "ercot"
    data_dir.mkdir()
    rows = [("07/15/2025", 1, 1, "N", "LZ_NORTH", 25.0)]
    _write_xlsx(data_dir / "rtm.xlsx", {"Jul-25": rows})

    prices1 = ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)
    cache_path = data_dir / "LZ_NORTH.parquet"
    assert cache_path.exists()

    # Reading again should return identical data (from cache).
    prices2 = ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)
    pd.testing.assert_series_equal(prices1, prices2)


def test_cache_rebuilds_when_source_file_is_newer(tmp_path: Path):
    data_dir = tmp_path / "ercot"
    data_dir.mkdir()
    rows = [("07/15/2025", 1, 1, "N", "LZ_NORTH", 25.0)]
    xlsx_path = data_dir / "rtm.xlsx"
    _write_xlsx(xlsx_path, {"Jul-25": rows})

    prices1 = ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)
    assert len(prices1) == 1

    # Rewrite the source file with an additional row, and make sure its
    # mtime is unambiguously newer than the cache's.
    cache_path = data_dir / "LZ_NORTH.parquet"
    cache_mtime = cache_path.stat().st_mtime
    rows2 = rows + [("07/15/2025", 2, 1, "N", "LZ_NORTH", 30.0)]
    _write_xlsx(xlsx_path, {"Jul-25": rows2})
    newer = cache_mtime + 5
    os.utime(xlsx_path, (newer, newer))

    prices2 = ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)
    assert len(prices2) == 2


def test_dedupe_across_overlapping_files(tmp_path: Path):
    data_dir = tmp_path / "ercot"
    data_dir.mkdir()
    rows = [("07/15/2025", 1, 1, "N", "LZ_NORTH", 25.0)]
    _write_xlsx(data_dir / "rtm_a.xlsx", {"Jul-25": rows})
    _write_xlsx(data_dir / "rtm_b.xlsx", {"Jul-25": rows})  # identical overlapping row

    prices = ercot.load_prices(zone="LZ_NORTH", data_dir=data_dir)
    assert len(prices) == 1
    ts0 = pd.Timestamp("2025-07-15 05:00:00", tz="UTC")
    assert prices.loc[ts0] == pytest.approx(25.0 / 1000.0)


# --------------------------------------------------------------------------- #
# download_prices: URL construction / error path only, no live network calls
# --------------------------------------------------------------------------- #
def test_download_prices_raises_clear_error_when_network_unavailable(tmp_path, monkeypatch):
    import httpx

    class _BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            raise httpx.ConnectError("network blocked in this environment")

    monkeypatch.setattr(httpx, "Client", _BoomClient)

    with pytest.raises(RuntimeError) as excinfo:
        ercot.download_prices(zone="LZ_NORTH", dest_dir=tmp_path)
    msg = str(excinfo.value)
    assert "NP6-785-ER" in msg
    assert str(tmp_path) in msg


# --------------------------------------------------------------------------- #
# Short price gaps are estimated; long ones are refused
# --------------------------------------------------------------------------- #
def _price_series(start: str, days: int, tz: str = "UTC") -> pd.Series:
    """Diurnal synthetic prices: cheap overnight, expensive late afternoon."""
    idx = pd.date_range(start, periods=days * 96, freq="15min", tz=tz)
    hour = idx.tz_convert("America/Chicago").hour
    vals = np.where((hour >= 15) & (hour < 20), 0.20, 0.02)
    return pd.Series(vals, index=idx, name="price_usd_kwh")


def test_fill_price_gaps_estimates_a_short_tail_by_time_of_day():
    from energyanalyzer.prices.ercot import fill_price_gaps

    # A full billing year with ERCOT two days behind -- 0.55% of intervals,
    # which is the real-world shape of this gap.
    full = _price_series("2025-09-01", 365)
    published = full.iloc[: -2 * 96]
    filled, gap = fill_price_gaps(published, full.index)

    assert gap.missing == 2 * 96
    assert gap.within_tolerance and gap.filled == 2 * 96
    assert not filled.isna().any()

    # Filled values follow the diurnal shape rather than a flat average: the
    # 4pm slot must come back expensive and the 3am slot cheap.
    local = filled.index.tz_convert("America/Chicago")
    tail = filled[filled.index >= published.index.max()]
    tail_local = tail.index.tz_convert("America/Chicago")
    assert tail[tail_local.hour == 16].mean() == pytest.approx(0.20, abs=1e-6)
    assert tail[tail_local.hour == 3].mean() == pytest.approx(0.02, abs=1e-6)
    assert local is not None  # index kept tz-aware


def test_fill_price_gaps_refuses_a_large_gap():
    from energyanalyzer.prices.ercot import fill_price_gaps

    full = _price_series("2025-09-01", 365)
    published = full.iloc[: -40 * 96]  # 11% of the window missing
    filled, gap = fill_price_gaps(published, full.index)

    assert not gap.within_tolerance
    assert gap.filled == 0
    assert filled.isna().any(), "NaNs must remain so the caller still refuses to price"
    assert "excluded from the ranking" in gap.note


def test_fill_price_gaps_is_a_noop_with_complete_coverage():
    from energyanalyzer.prices.ercot import fill_price_gaps

    full = _price_series("2026-06-01", 10)
    filled, gap = fill_price_gaps(full, full.index)
    assert gap.missing == 0 and gap.note == ""
    pd.testing.assert_series_equal(filled, full)
