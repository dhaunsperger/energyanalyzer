"""Tests for energyanalyzer.fetchers.ptc -- built entirely from the local
tests/fixtures/ptc_sample.csv synthetic snapshot; no live network access
(powertochoose.org is blocked in the sandbox this module was developed in).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from energyanalyzer.fetchers import ptc

FIXTURE = Path(__file__).parent / "fixtures" / "ptc_sample.csv"


def test_load_ptc_from_file_normalizes_known_columns():
    df = ptc.load_ptc(FIXTURE)

    assert len(df) == 8
    for col in (
        "retailer",
        "plan_name",
        "tdu",
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
    ):
        assert col in df.columns, f"missing normalized column {col}"

    txu = df[df["plan_name"] == "TXU Solar BB 12"].iloc[0]
    assert txu["retailer"] == "TXU Energy"
    assert txu["tdu"] == "ONCOR"
    assert txu["term_months"] == 12
    assert txu["rate_type"] == "Fixed"
    assert txu["kwh1000"] == pytest.approx(12.4)
    assert txu["efl_url"] == "https://www.txu.com/efl/1001.pdf"
    assert bool(txu["prepaid"]) is False
    assert bool(txu["tou"]) is False

    prepaid_plan = df[df["plan_name"] == "Payless Prepaid 250"].iloc[0]
    assert bool(prepaid_plan["prepaid"]) is True
    assert bool(prepaid_plan["tou"]) is False

    tou_plan = df[df["plan_name"] == "Reliant Free Overnight"].iloc[0]
    assert bool(tou_plan["tou"]) is True


def test_load_ptc_from_directory_picks_most_recent_csv(tmp_path: Path):
    older = tmp_path / "ptc_20250101T000000Z.csv"
    newer = tmp_path / "ptc_20260701T000000Z.csv"
    older.write_text(FIXTURE.read_text())
    newer.write_text(FIXTURE.read_text())

    import os
    import time

    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    df = ptc.load_ptc(tmp_path)
    assert len(df) == 8


def test_load_ptc_missing_path_raises_filenotfound(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        ptc.load_ptc(tmp_path / "nope.csv")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        ptc.load_ptc(empty_dir)


def test_load_ptc_keeps_unknown_columns():
    df = pd.DataFrame(
        {
            "idKey": [1],
            "TduCompanyName": ["ONCOR"],
            "RepCompany": ["Acme Energy"],
            "Product": ["Acme Basic 12"],
            "Term": [12],
            "RateType": ["Fixed"],
            "kwh500": [13.0],
            "kwh1000": [12.0],
            "kwh2000": [11.0],
            "PrePaid": ["N"],
            "TimeOfUse": ["N"],
            "Renewable": [10],
            "CancellationFee": [150],
            "FactsURL": ["https://example.com/efl.pdf"],
            "EnrollURL": ["https://example.com/enroll"],
            "SomeBrandNewPtcField": ["mystery value"],
        }
    )
    tmp_csv = Path(pytest.importorskip("tempfile").mkdtemp()) / "custom.csv"
    df.to_csv(tmp_csv, index=False)

    out = ptc.load_ptc(tmp_csv)
    assert "SomeBrandNewPtcField" in out.columns
    assert out.loc[0, "SomeBrandNewPtcField"] == "mystery value"


def test_filter_plans_by_tdu_default_oncor():
    df = ptc.load_ptc(FIXTURE)
    filtered = ptc.filter_plans(df)  # default tdu="ONCOR"
    assert len(filtered) == 6
    assert set(filtered["tdu"]) == {"ONCOR"}
    assert "Direct Twelve Hour Power 24" not in filtered["plan_name"].values
    assert "Pulse Simple 12" not in filtered["plan_name"].values


def test_filter_plans_multiple_criteria():
    df = ptc.load_ptc(FIXTURE)
    filtered = ptc.filter_plans(df, tdu="ONCOR", prepaid=False, tou=True)
    assert len(filtered) == 2
    names = set(filtered["plan_name"])
    assert names == {"Reliant Free Overnight", "Pollution Free Nights"}


def test_filter_plans_renewable_and_term():
    df = ptc.load_ptc(FIXTURE)
    filtered = ptc.filter_plans(df, tdu=None, min_renewable_pct=50)
    assert len(filtered) == 1
    assert filtered.iloc[0]["plan_name"] == "Pollution Free Nights"


def test_fetch_ptc_csv_raises_clear_error_when_network_unavailable(tmp_path, monkeypatch):
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
        ptc.fetch_ptc_csv(dest_dir=tmp_path)
    msg = str(excinfo.value)
    assert "powertochoose.org" in msg.lower()
    assert str(tmp_path) in msg


def test_download_efls_skips_existing_and_downloads_new(tmp_path, monkeypatch):
    df = ptc.load_ptc(FIXTURE).head(3)

    class _FakeResponse:
        def __init__(self, content: bytes):
            self.content = content

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, *args, **kwargs):
            self.calls.append(url)
            return _FakeResponse(b"%PDF-1.4 fake efl content")

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    dest = tmp_path / "efl"
    # Pre-create one destination file to exercise the "skip existing" path.
    dest.mkdir(parents=True)
    first_row = df.iloc[0]
    existing_name = ptc._efl_filename(first_row)
    (dest / existing_name).write_bytes(b"already here")

    summary = ptc.download_efls(df, dest=dest)

    assert len(summary["downloaded"]) == 2
    assert len(summary["skipped"]) == 1
    assert summary["failed"] == []
    for path_str in summary["downloaded"]:
        assert Path(path_str).read_bytes().startswith(b"%PDF")
