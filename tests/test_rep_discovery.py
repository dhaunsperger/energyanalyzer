"""Tests for fetchers/rep_discovery.py -- the REP-site EFL discovery fetcher.

No live network: the Green Mountain static extractor runs against a committed
synthetic fixture, the LLM classifier is driven through its injectable
``chat_fn`` seam, and downloads go through a monkeypatched ``httpx.Client``
(same convention as tests/test_ptc.py). One optional integration test hits a
real local Ollama server and is skipped unless one is reachable with the model
pulled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from energyanalyzer.fetchers import rep_discovery as rd

FIXTURE = Path(__file__).parent / "fixtures" / "rep_green_mountain_sample.html"


# --------------------------------------------------------------------------- #
# Static extractor
# --------------------------------------------------------------------------- #
def _extract():
    html = FIXTURE.read_text(encoding="utf-8")
    return rd.discover(html, rd.GREEN_MOUNTAIN, use_llm_fallback=False)


def test_static_extractor_finds_all_plans():
    plans = _extract()
    assert len(plans) == 3
    assert all(p.extraction_method == "static" for p in plans)
    assert all(p.retailer == "Green Mountain Energy" for p in plans)


def test_static_extractor_cleans_trademark_glyphs():
    names = {p.plan_name for p in _extract()}
    # NFKD would have expanded (TM) to "TM"; names must come out clean.
    assert "Pollution Free e-Plus 12" in names
    assert "Renewable Rewards Solar Max 12" in names
    assert not any("™" in n or "®" in n or "TM" in n for n in names)


def test_static_extractor_self_labels_buyback():
    by_name = {p.plan_name: p for p in _extract()}
    assert by_name["Renewable Rewards Solar Max 12"].is_buyback is True
    assert by_name["Renewable Rewards Solar Max 12"].buyback_ckwh == pytest.approx(11.4)
    assert by_name["Renewable Rewards Solar Credit 12"].buyback_ckwh == pytest.approx(6.3)
    # A plan whose analytics div carries no ^BuyBack flag is a confident False,
    # not "undetermined".
    assert by_name["Pollution Free e-Plus 12"].is_buyback is False
    assert by_name["Pollution Free e-Plus 12"].buyback_ckwh is None


def test_static_extractor_resolves_relative_and_absolute_urls():
    by_name = {p.plan_name: p for p in _extract()}
    # Relative href joined onto the homepage.
    assert (
        by_name["Pollution Free e-Plus 12"].efl_url
        == "https://www.greenmountainenergy.com/files/pf_eplus_12.pdf"
    )
    # Absolute href preserved as-is.
    assert (
        by_name["Renewable Rewards Solar Max 12"].efl_url
        == "https://signup.greenmountainenergy.com/files/solar_max_12.pdf"
    )


def test_static_extractor_ignores_non_efl_documents():
    # Terms of Service / Your Rights anchors must not be picked up.
    urls = {p.efl_url for p in _extract()}
    assert not any("tos" in u or "yrac" in u for u in urls)


# --------------------------------------------------------------------------- #
# LLM fallback classifier
# --------------------------------------------------------------------------- #
def test_classify_link_llm_parses_json_via_chat_fn():
    def fake_chat(messages, model, ollama_url, timeout):
        # The real page context must reach the model (never a bare URL).
        assert any("Surrounding page context" in m["content"] for m in messages)
        return {
            "message": {
                "content": json.dumps(
                    {"is_efl": True, "is_buyback": True, "confidence": 0.9, "thinking": "ok"}
                )
            }
        }

    verdict = rd.classify_link_llm(
        "Download", "Solar Buyback 12 facts label", url="x.pdf", chat_fn=fake_chat
    )
    assert verdict == {
        "is_efl": True,
        "is_buyback": True,
        "confidence": 0.9,
        "thinking": "ok",
    }


def test_classify_link_llm_wraps_transport_errors():
    def boom(*args, **kwargs):
        raise ConnectionError("no ollama")

    with pytest.raises(RuntimeError, match="Ollama classification call failed"):
        rd.classify_link_llm("Download", "context", chat_fn=boom)


def test_classify_link_llm_raises_on_non_json():
    def fake_chat(messages, model, ollama_url, timeout):
        return {"message": {"content": "not json at all"}}

    with pytest.raises(RuntimeError, match="non-JSON"):
        rd.classify_link_llm("Download", "context", chat_fn=fake_chat)


# --------------------------------------------------------------------------- #
# Two-tier discovery orchestration (LLM fallback path)
# --------------------------------------------------------------------------- #
_NON_LABELED_HTML = """
<div>
  <h2>Solar Buyback 12</h2>
  <p>Great plan for solar customers with export credits.</p>
  <a href="/docs/sb12.pdf">Download</a>
</div>
"""


def _dummy_config(extractor):
    return rd.RepConfig(
        key="dummy",
        retailer="Dummy REP",
        homepage="https://dummy.example/",
        extractor=extractor,
    )


def test_discover_falls_back_to_llm_when_static_finds_nothing():
    def empty_extractor(html, config):
        return []

    def fake_chat(messages, model, ollama_url, timeout):
        return {
            "message": {
                "content": json.dumps(
                    {"is_efl": True, "is_buyback": True, "confidence": 0.8, "thinking": "efl"}
                )
            }
        }

    plans = rd.discover(
        _NON_LABELED_HTML,
        _dummy_config(empty_extractor),
        use_llm_fallback=True,
        chat_fn=fake_chat,
    )
    assert len(plans) == 1
    p = plans[0]
    assert p.extraction_method == "llm"
    assert p.is_buyback is True
    assert p.llm_confidence == pytest.approx(0.8)
    assert p.efl_url == "https://dummy.example/docs/sb12.pdf"


def test_discover_drops_low_confidence_llm_hits():
    def empty_extractor(html, config):
        return []

    def fake_chat(messages, model, ollama_url, timeout):
        return {
            "message": {
                "content": json.dumps(
                    {"is_efl": True, "is_buyback": False, "confidence": 0.3, "thinking": "meh"}
                )
            }
        }

    plans = rd.discover(
        _NON_LABELED_HTML,
        _dummy_config(empty_extractor),
        use_llm_fallback=True,
        llm_min_confidence=0.6,
        chat_fn=fake_chat,
    )
    assert plans == []


def test_discover_tolerates_ollama_unavailable():
    def empty_extractor(html, config):
        return []

    def boom(*args, **kwargs):
        raise ConnectionError("no ollama")

    # Static tier found nothing and the LLM tier is down -> empty, no raise.
    plans = rd.discover(
        _NON_LABELED_HTML,
        _dummy_config(empty_extractor),
        use_llm_fallback=True,
        chat_fn=boom,
    )
    assert plans == []


def test_discover_skips_llm_when_static_succeeds():
    calls = []

    def good_extractor(html, config):
        return [rd.DiscoveredPlan(retailer="R", plan_name="P", efl_url="u.pdf")]

    def tracking_chat(messages, model, ollama_url, timeout):
        calls.append(1)
        return {"message": {"content": "{}"}}

    plans = rd.discover(
        _NON_LABELED_HTML, _dummy_config(good_extractor), chat_fn=tracking_chat
    )
    assert len(plans) == 1
    assert calls == []  # LLM tier never invoked when static tier produced hits


# --------------------------------------------------------------------------- #
# Download + manifest
# --------------------------------------------------------------------------- #
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


def _sample_plans():
    return [
        rd.DiscoveredPlan(
            retailer="Green Mountain Energy",
            plan_name="Renewable Rewards Solar Max 12",
            efl_url="https://x/solar_max_12.pdf",
            is_buyback=True,
            buyback_ckwh=11.4,
        ),
        rd.DiscoveredPlan(
            retailer="Green Mountain Energy",
            plan_name="Pollution Free e-Plus 12",
            efl_url="https://x/pf_eplus_12.pdf",
            is_buyback=False,
        ),
    ]


def test_download_discovered_buyback_only_filters(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    dest = tmp_path / "efl"
    summary = rd.download_discovered(_sample_plans(), dest=dest, buyback_only=True)

    assert len(summary["downloaded"]) == 1
    assert summary["filtered_out"] == 1  # the non-buyback plan was excluded
    assert (dest / "Green_Mountain_Energy_Renewable_Rewards_Solar_Max_12.pdf").exists()
    assert not (dest / "Green_Mountain_Energy_Pollution_Free_e-Plus_12.pdf").exists()


def test_download_discovered_writes_manifest(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    dest = tmp_path / "efl"
    rd.download_discovered(_sample_plans(), dest=dest, buyback_only=True)

    manifest = dest / "rep_discovery_manifest.jsonl"
    assert manifest.exists()
    lines = [json.loads(ln) for ln in manifest.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["retailer"] == "Green Mountain Energy"
    assert entry["plan_name"] == "Renewable Rewards Solar Max 12"
    assert entry["source_url"] == "https://x/solar_max_12.pdf"
    assert entry["extraction_method"] == "static"
    assert entry["is_buyback"] is True
    assert entry["buyback_ckwh"] == pytest.approx(11.4)
    assert "discovered_at" in entry and entry["file"].endswith(".pdf")


def test_download_discovered_skips_existing(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    dest = tmp_path / "efl"
    dest.mkdir(parents=True)
    existing = dest / "Green_Mountain_Energy_Renewable_Rewards_Solar_Max_12.pdf"
    existing.write_bytes(b"already here")

    summary = rd.download_discovered(_sample_plans(), dest=dest, buyback_only=True)
    assert len(summary["skipped"]) == 1
    assert summary["downloaded"] == []
    assert existing.read_bytes() == b"already here"  # not overwritten
    # A skipped-but-present file still gets a manifest entry.
    manifest = dest / "rep_discovery_manifest.jsonl"
    assert len(manifest.read_text().splitlines()) == 1


def test_download_discovered_all_when_not_buyback_only(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    dest = tmp_path / "efl"
    summary = rd.download_discovered(_sample_plans(), dest=dest, buyback_only=False)
    assert len(summary["downloaded"]) == 2
    assert summary["filtered_out"] == 0


# --------------------------------------------------------------------------- #
# robots.txt / fetch guards (no browser)
# --------------------------------------------------------------------------- #
def test_fetch_rendered_html_without_playwright_raises_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="Playwright is required"):
        rd.fetch_rendered_html(rd.GREEN_MOUNTAIN, "78665")


# --------------------------------------------------------------------------- #
# Optional live Ollama integration (skipped unless a server is reachable)
# --------------------------------------------------------------------------- #
def _ollama_available() -> bool:
    try:
        import httpx

        base = rd.OLLAMA_URL.rsplit("/api/", 1)[0]
        resp = httpx.get(f"{base}/api/tags", timeout=2.0)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        return any(rd.OLLAMA_MODEL in n for n in names)
    except Exception:
        return False


@pytest.mark.skipif(
    os.environ.get("EA_RUN_OLLAMA_TESTS") != "1" or not _ollama_available(),
    reason="live Ollama server with the configured model not available "
    "(set EA_RUN_OLLAMA_TESTS=1 and run `ollama serve` to enable)",
)
def test_classify_link_llm_live_ollama():
    verdict = rd.classify_link_llm(
        "Electricity Facts Label",
        "Renewable Rewards Solar Buyback 12 -- Important Documents section",
    )
    assert verdict["is_efl"] is True
    assert 0.0 <= verdict["confidence"] <= 1.0
