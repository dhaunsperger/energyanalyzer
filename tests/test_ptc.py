"""Tests for energyanalyzer.fetchers.ptc -- built entirely from the local
tests/fixtures/ptc_sample.csv synthetic snapshot; no live network access
(powertochoose.org is blocked in the sandbox this module was developed in).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from energyanalyzer.app import common as app_common
from energyanalyzer.eflparse import parser as eflparser
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


def test_download_efls_defers_html_response(tmp_path, monkeypatch):
    # A 200 response whose body is HTML (a browser-rendered EFL viewer / SPA
    # shell, e.g. the Vistra shopping.* PDFGenerator endpoint) must NOT be saved
    # as a .pdf. It's not a real failure -- it's deferred (needs a browser).
    df = ptc.load_ptc(FIXTURE).head(2)

    class _HtmlResponse:
        content = b"<!DOCTYPE html><html><head></head><body>Not found</body></html>"
        headers = {"content-type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, *args, **kwargs):
            return _HtmlResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    dest = tmp_path / "efl"

    summary = ptc.download_efls(df, dest=dest)

    assert summary["downloaded"] == []
    assert summary["failed"] == []
    assert len(summary["deferred"]) == 2
    assert "HTML response" in summary["deferred"][0]["reason"]
    # Nothing written to disk.
    assert not list(dest.glob("*.pdf"))


def test_download_efls_non_html_non_pdf_stays_failed(tmp_path, monkeypatch):
    # A non-PDF, non-HTML body (a stale link / truncated response) is a genuine
    # failure, NOT a deferrable browser-rendered viewer.
    df = ptc.load_ptc(FIXTURE).head(1)

    class _JunkResponse:
        content = b"garbage-not-a-pdf"
        headers = {"content-type": "application/octet-stream"}

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, *args, **kwargs):
            return _JunkResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    summary = ptc.download_efls(df, dest=tmp_path / "efl")
    assert summary["deferred"] == []
    assert len(summary["failed"]) == 1


def test_download_efls_defers_html_viewer_urls(tmp_path, monkeypatch):
    # An HTML-viewer "EFL" URL (e.g. octopusenergy.com/efl/...) can't be
    # fetched as a PDF over httpx -- REP discovery renders it instead. It must
    # be deferred (reported separately), NOT counted as a download failure, and
    # must not even hit the network.
    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "retailer": "Octopus Energy",
                "plan_name": "Octopus Simple 12",
                "efl_url": "https://octopusenergy.com/efl/OCTO-SIMPLE-12-ONCOR.html",
            }
        ]
    )

    class _NoNetClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("HTML-viewer URL should be deferred, not fetched")

    import httpx

    monkeypatch.setattr(httpx, "Client", _NoNetClient)
    dest = tmp_path / "efl"

    summary = ptc.download_efls(df, dest=dest)

    assert summary["failed"] == []
    assert summary["downloaded"] == []
    assert len(summary["deferred"]) == 1
    assert "octopusenergy.com/efl/" in summary["deferred"][0]["url"]
    assert not list(dest.glob("*.pdf"))


def test_efl_ssl_context_enables_legacy_server_connect():
    # Some EFL hosts (Tara/Amigo on the shared Just Energy platform) run TLS
    # stacks that require legacy renegotiation, which OpenSSL 3.x refuses by
    # default -- the download would fail with a bare ConnectError. The EFL
    # client must opt back into OP_LEGACY_SERVER_CONNECT so those handshakes
    # complete (verification otherwise unchanged).
    import ssl

    ctx = ptc._efl_ssl_context()
    assert ctx.options & ssl.OP_LEGACY_SERVER_CONNECT
    # Still a verifying context -- we relaxed renegotiation, not trust.
    assert ctx.verify_mode == ssl.CERT_REQUIRED


# --------------------------------------------------------------------------- #
# Issue: statewide PTC snapshot includes Spanish-language duplicate rows,
# which made a correct tdu-filtered result look "truncated". `filter_plans`
# now defaults to language="English" (backward compatible: existing callers
# that never had a `language` column keep their exact prior behavior).
# --------------------------------------------------------------------------- #
def _bilingual_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "tdu": ["ONCOR", "ONCOR", "ONCOR", "ONCOR"],
            "language": ["English", "Spanish", "English", "Spanish"],
            "plan_name": ["Acme A", "Acme A (ES)", "Acme B", "Acme B (ES)"],
        }
    )


def test_filter_plans_language_default_excludes_spanish_duplicates():
    df = _bilingual_df()
    filtered = ptc.filter_plans(df, tdu=None)  # language defaults to "English"
    assert len(filtered) == 2
    assert set(filtered["plan_name"]) == {"Acme A", "Acme B"}


def test_filter_plans_language_none_disables_filter():
    df = _bilingual_df()
    filtered = ptc.filter_plans(df, tdu=None, language=None)
    assert len(filtered) == 4


def test_filter_plans_language_column_absent_is_backward_compatible():
    # FIXTURE has no "language" column at all -- the new default filter must
    # be silently skipped, exactly like every other filter referencing an
    # absent column, so this matches the pre-existing behavior/row count.
    df = ptc.load_ptc(FIXTURE)
    filtered = ptc.filter_plans(df)
    assert len(filtered) == 6


def test_load_ptc_normalizes_language_column_alias(tmp_path: Path):
    df = pd.DataFrame(
        {
            "idKey": [1, 2],
            "TduCompanyName": ["ONCOR", "ONCOR"],
            "RepCompany": ["Acme", "Acme"],
            "Product": ["Acme A", "Acme A (ES)"],
            "Language": ["English", "Spanish"],
        }
    )
    tmp_csv = tmp_path / "bilingual.csv"
    df.to_csv(tmp_csv, index=False)

    out = ptc.load_ptc(tmp_csv)
    assert "language" in out.columns
    assert set(out["language"]) == {"English", "Spanish"}


# --------------------------------------------------------------------------- #
# Issue: "Download EFLs" gave no feedback until the whole batch finished.
# --------------------------------------------------------------------------- #
def test_download_efls_progress_callback_called_per_item(tmp_path, monkeypatch):
    df = ptc.load_ptc(FIXTURE).head(3)

    class _FakeResponse:
        def __init__(self, content: bytes):
            self.content = content

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, *args, **kwargs):
            return _FakeResponse(b"%PDF-1.4 fake efl content")

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    calls: list[tuple[int, int, str]] = []
    summary = ptc.download_efls(
        df,
        dest=tmp_path / "efl",
        progress_callback=lambda done, total, name: calls.append((done, total, name)),
    )

    assert len(summary["downloaded"]) == 3
    assert len(calls) == 3
    assert [c[0] for c in calls] == [1, 2, 3]
    assert all(c[1] == 3 for c in calls)
    assert all(isinstance(c[2], str) and c[2] for c in calls)


def test_download_efls_progress_callback_counts_skipped_and_failed(tmp_path, monkeypatch):
    df = ptc.load_ptc(FIXTURE).head(2)

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, *args, **kwargs):
            raise RuntimeError("boom")

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    calls = []
    summary = ptc.download_efls(
        df,
        dest=tmp_path / "efl",
        progress_callback=lambda done, total, name: calls.append((done, total, name)),
    )
    assert len(summary["failed"]) == 2
    assert len(calls) == 2
    assert calls[-1][0] == 2


# --------------------------------------------------------------------------- #
# Issue: downloaded EFLs never entered the plan database. `parse_downloaded_efls`
# (app/common.py) is the plain, testable batch helper the Plans page wires to
# a progress bar; it wraps eflparse.parser per-file so one bad PDF can't abort
# the batch, and skips PDFs whose derived plan id already has a draft/plan.
# --------------------------------------------------------------------------- #
def test_parse_downloaded_efls_batch_parses_skips_and_tolerates_failures(tmp_path, monkeypatch):
    drafts_dir = tmp_path / "drafts"
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()

    pdf_a = tmp_path / "a.pdf"
    pdf_b = tmp_path / "b.pdf"
    pdf_bad = tmp_path / "bad.pdf"
    for p in (pdf_a, pdf_b, pdf_bad):
        p.write_bytes(b"dummy pdf bytes")

    def fake_parse_efl(path: Path) -> eflparser.DraftPlan:
        path = Path(path)
        if path.name == "bad.pdf":
            raise ValueError("corrupt/unreadable PDF")
        plan_id = path.stem
        return eflparser.DraftPlan(
            plan_dict={
                "id": plan_id,
                "retailer": "Acme",
                "name": plan_id,
                "term_months": 12,
                "base_charge_usd": 4.95,
                "energy_rates": [{"rate_ckwh": 12.0}],
                "buyback": {"kind": "none"},
                "needs_review": False,
            },
            confidence={"energy_charge": 0.9, "base_charge": 0.95},
            evidence={"energy_charge": "12.0 cents per kWh"},
            unparsed_notes=[],
        )

    monkeypatch.setattr(eflparser, "parse_efl", fake_parse_efl)

    calls: list[tuple[int, int, str]] = []
    summary = app_common.parse_downloaded_efls(
        [pdf_a, pdf_b, pdf_bad],
        drafts_dir=drafts_dir,
        plans_dir=plans_dir,
        progress_callback=lambda done, total, name: calls.append((done, total, name)),
    )

    assert sorted(summary["parsed"]) == ["a", "b"]
    assert summary["skipped"] == []
    assert len(summary["failed"]) == 1
    assert summary["failed"][0]["file"] == "bad.pdf"
    assert len(calls) == 3
    assert (drafts_dir / "a.yaml").exists()
    assert (drafts_dir / "b.yaml").exists()

    # Re-running over the same (now-parsed) PDFs must skip, not re-save.
    summary2 = app_common.parse_downloaded_efls(
        [pdf_a, pdf_b], drafts_dir=drafts_dir, plans_dir=plans_dir
    )
    assert summary2["parsed"] == []
    assert set(summary2["skipped"]) == {"a.pdf", "b.pdf"}


def test_parse_downloaded_efls_skips_pdfs_already_promoted(tmp_path, monkeypatch):
    drafts_dir = tmp_path / "drafts"
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "already_promoted.yaml").write_text("id: already_promoted\n")

    pdf = tmp_path / "already_promoted.pdf"
    pdf.write_bytes(b"dummy")

    def fake_parse_efl(path: Path) -> eflparser.DraftPlan:
        return eflparser.DraftPlan(plan_dict={"id": "already_promoted"})

    monkeypatch.setattr(eflparser, "parse_efl", fake_parse_efl)

    summary = app_common.parse_downloaded_efls([pdf], drafts_dir=drafts_dir, plans_dir=plans_dir)
    assert summary["parsed"] == []
    assert summary["skipped"] == ["already_promoted.pdf"]
    assert not drafts_dir.exists() or not any(drafts_dir.glob("*.yaml"))
