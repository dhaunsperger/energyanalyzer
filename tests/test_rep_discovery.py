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
TXU_FIXTURE = Path(__file__).parent / "fixtures" / "rep_txu_sample.html"
CHARIOT_FIXTURE = Path(__file__).parent / "fixtures" / "rep_chariot_sample.html"
GEXA_FIXTURE = Path(__file__).parent / "fixtures" / "rep_gexa_sample.html"
FRONTIER_FIXTURE = Path(__file__).parent / "fixtures" / "rep_frontier_sample.html"
AMBIT_FIXTURE = Path(__file__).parent / "fixtures" / "rep_ambit_sample.html"
OCTOPUS_FIXTURE = Path(__file__).parent / "fixtures" / "rep_octopus_sample.html"


# --------------------------------------------------------------------------- #
# Green Mountain static extractor
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
# TXU static extractor
# --------------------------------------------------------------------------- #
def _extract_txu():
    html = TXU_FIXTURE.read_text(encoding="utf-8")
    return rd.discover(html, rd.TXU, use_llm_fallback=False)


def test_txu_extractor_finds_all_cards():
    plans = _extract_txu()
    assert len(plans) == 3
    assert all(p.retailer == "TXU Energy" for p in plans)
    assert all(p.extraction_method == "static" for p in plans)
    # EFL links self-label via the PDFGenerator query string.
    assert all("formType=EnergyFactsLabel" in p.efl_url for p in plans)


def test_txu_extractor_unescapes_plan_names():
    names = {p.plan_name for p in _extract_txu()}
    assert "Free Nights & Cool Summer 12" in names  # &amp; decoded
    assert "Solar Buyback System Flex" in names


def test_txu_extractor_flags_only_the_buyback_plan():
    by_name = {p.plan_name: p for p in _extract_txu()}
    assert by_name["Solar Buyback System Flex"].is_buyback is True
    assert by_name["Free Nights & Cool Summer 12"].is_buyback is False


def test_txu_extractor_ignores_script_buyback_badges():
    # The embedded Next.js data blob labels a "Solar Panel Buyback" badge, but
    # Flex Forward's *visible* card is not a buyback plan -- scripts must be
    # stripped so that badge JSON can't leak in and false-positive it.
    by_name = {p.plan_name: p for p in _extract_txu()}
    assert by_name["Flex Forward"].is_buyback is False


def test_txu_buyback_efl_url_carries_product_id():
    buyback = [p for p in _extract_txu() if p.is_buyback]
    assert len(buyback) == 1
    assert "comProdId=ONXSBBSYSV00AB" in buyback[0].efl_url
    assert buyback[0].buyback_ckwh is None  # rate isn't published on the page


def test_txu_registered_in_rep_configs():
    assert rd.REP_CONFIGS["txu"].retailer == "TXU Energy"
    assert rd.REP_CONFIGS["txu"].render is not None


# --------------------------------------------------------------------------- #
# Chariot static extractor
# --------------------------------------------------------------------------- #
def _extract_chariot():
    html = CHARIOT_FIXTURE.read_text(encoding="utf-8")
    return rd.discover(html, rd.CHARIOT, use_llm_fallback=False)


def test_chariot_extractor_finds_all_cards():
    plans = _extract_chariot()
    # 4 unique plans: the #:ProductId# template row is skipped and the
    # PowerBank card duplicated across the two concatenated pages is deduped.
    assert len(plans) == 4
    assert all(p.retailer == "Chariot Energy" for p in plans)
    assert all(p.extraction_method == "static" for p in plans)
    assert all("/Home/EFl?productId=" in p.efl_url for p in plans)


def test_chariot_extractor_dedups_across_pages_and_skips_template():
    names = [p.plan_name for p in _extract_chariot()]
    assert names.count("PowerBank 12") == 1  # deduped across pages
    assert not any("#:" in n or "Title" == n for n in names)  # template skipped


def test_chariot_extractor_flags_solar_buyback_plans():
    by_name = {p.plan_name: p for p in _extract_chariot()}
    assert by_name["Shine 12"].is_buyback is True
    assert by_name["PowerBank 12"].is_buyback is True
    assert by_name["GreenVolt 24"].is_buyback is True


def test_chariot_extractor_does_not_flag_rooftop_solar_disclaimer():
    # "Free Days 12" carries a "Restrictions apply for customers with rooftop
    # solar and/or batteries" disclaimer -- that must NOT read as buyback.
    by_name = {p.plan_name: p for p in _extract_chariot()}
    assert by_name["Free Days 12"].is_buyback is False
    assert by_name["Free Days 12"].buyback_ckwh is None


def test_chariot_extractor_parses_fixed_buyback_rate():
    by_name = {p.plan_name: p for p in _extract_chariot()}
    # "...buyback rate of 3 Cents per kWh..."
    assert by_name["PowerBank 12"].buyback_ckwh == pytest.approx(3.0)
    # "Fixed 7¢ Buyback" tagline (the alt-rate pattern).
    assert by_name["GreenVolt 24"].buyback_ckwh == pytest.approx(7.0)
    # Market-rate plan advertises no number -> None despite being buyback.
    assert by_name["Shine 12"].is_buyback is True
    assert by_name["Shine 12"].buyback_ckwh is None


def test_chariot_extractor_unescapes_efl_url_and_ignores_tos_yrac():
    urls = {p.efl_url for p in _extract_chariot()}
    # &amp; in the href decoded; sibling TOS/YRAC links not picked up.
    assert "https://signup.chariotenergy.com/Home/EFl?productId=40536&Promo=15225" in urls
    assert not any("/Home/TOS?" in u or "/Home/YRAC?" in u for u in urls)


def test_chariot_registered_in_rep_configs():
    assert rd.REP_CONFIGS["chariot"].retailer == "Chariot Energy"
    assert rd.REP_CONFIGS["chariot"].render is not None


# --------------------------------------------------------------------------- #
# Gexa static extractor
# --------------------------------------------------------------------------- #
def _extract_gexa():
    html = GEXA_FIXTURE.read_text(encoding="utf-8")
    return rd.discover(html, rd.GEXA, use_llm_fallback=False)


def test_gexa_extractor_finds_all_cards():
    plans = _extract_gexa()
    # 5 plans: the "Gexa Stable Plans" section-header row (no EFL) is skipped.
    assert len(plans) == 5
    assert all(p.retailer == "Gexa Energy" for p in plans)
    assert all(p.extraction_method == "static" for p in plans)
    assert all("eflviewer.aspx" in p.efl_url for p in plans)


def test_gexa_extractor_flags_only_solar_buyback_plans():
    by_name = {p.plan_name: p for p in _extract_gexa()}
    assert by_name["Gexa Solar Buyback 12"].is_buyback is True
    assert by_name["Gexa Battery Benefits 12"].is_buyback is True
    # SavEV is an EV plan, not buyback -- and the preceding buyback card's
    # "export to the grid" wording must not bleed across the card boundary.
    assert by_name["Gexa SavEV 12"].is_buyback is False
    assert by_name["Gexa 55+"].is_buyback is False


def test_gexa_extractor_ignores_solar_buyback_ribbon():
    # A .Product-tab "Solar Buyback" ribbon sits (in the DOM) at the end of the
    # Gexa 12 card, but Gexa 12 is a Fixed plan -- keying on the ribbon rather
    # than "Plan Type: Solar Buyback" would wrongly flag it.
    by_name = {p.plan_name: p for p in _extract_gexa()}
    assert by_name["Gexa 12"].is_buyback is False


def test_gexa_extractor_url_handling_and_no_phantom_from_script():
    by_name = {p.plan_name: p for p in _extract_gexa()}
    # &amp; decoded; %2b-encoded prodcode preserved.
    assert (
        by_name["Gexa 55+"].efl_url
        == "https://eflviewer.gexaenergy.com/eflviewer.aspx?lang=EN&prodcode=GXA55%2b&tdspcode=ONCOR_ELEC"
    )
    # The script blob's "GHOST" eflviewer URL must be stripped, not discovered.
    assert not any("GHOST" in p.efl_url for p in _extract_gexa())


def test_gexa_ribbon_does_not_leak_into_context():
    # The trailing "Solar Buyback" ribbon must be stripped from a plan's context
    # too, not just ignored by the buyback check -- otherwise it misleads the
    # optional LLM review (an lfm2.5 probe false-upgraded a plan whose context
    # ended in that ribbon). Gexa 12 carries the ribbon in the DOM.
    by_name = {p.plan_name: p for p in _extract_gexa()}
    assert "Solar Buyback" not in by_name["Gexa 12"].context


def test_gexa_registered_in_rep_configs():
    assert rd.REP_CONFIGS["gexa"].retailer == "Gexa Energy"
    assert rd.REP_CONFIGS["gexa"].render is not None


# --------------------------------------------------------------------------- #
# Frontier static extractor (shared eflviewer platform with Gexa)
# --------------------------------------------------------------------------- #
def _extract_frontier():
    html = FRONTIER_FIXTURE.read_text(encoding="utf-8")
    return rd.discover(html, rd.FRONTIER, use_llm_fallback=False)


def test_frontier_extractor_finds_all_cards():
    plans = _extract_frontier()
    # 5 plans: the "Frontier Stable Plans" section-header row (no EFL) is skipped.
    assert len(plans) == 5
    assert all(p.retailer == "Frontier Utilities" for p in plans)
    assert all(p.extraction_method == "static" for p in plans)
    assert all("eflviewer.frontierutilities.com/eflviewer.aspx" in p.efl_url for p in plans)


def test_frontier_extractor_flags_only_solar_buyback_plans():
    by_name = {p.plan_name: p for p in _extract_frontier()}
    assert by_name["Frontier Sun Confidence 12"].is_buyback is True
    assert by_name["Frontier Battery Awards 12"].is_buyback is True
    # The EV plan right after a buyback card must not inherit its "export to the
    # grid" wording across the card boundary.
    assert by_name["Frontier Nighttime EV Charging 24"].is_buyback is False
    assert by_name["Frontier Super Saver 12"].is_buyback is False
    assert by_name["Frontier 12"].is_buyback is False


def test_frontier_extractor_ignores_solar_buyback_ribbon():
    # A .Product-tab "Solar buyback" ribbon sits (in the DOM) at the end of the
    # Frontier Super Saver 12 card, but that plan is a Usage Credit plan -- keying
    # on the ribbon rather than "Plan Type: Solar Buyback" would wrongly flag it.
    by_name = {p.plan_name: p for p in _extract_frontier()}
    assert by_name["Frontier Super Saver 12"].is_buyback is False
    assert "Solar buyback" not in by_name["Frontier Super Saver 12"].context


def test_frontier_extractor_url_handling_and_no_phantom_from_script():
    by_name = {p.plan_name: p for p in _extract_frontier()}
    # &amp; decoded; %2b-encoded prodcode preserved.
    assert (
        by_name["Frontier 12"].efl_url
        == "https://eflviewer.frontierutilities.com/eflviewer.aspx?lang=EN&prodcode=F12%2b&tdspcode=ONCOR_ELEC"
    )
    # The script blob's "GHOST" eflviewer URL must be stripped, not discovered.
    assert not any("GHOST" in p.efl_url for p in _extract_frontier())


def test_frontier_registered_in_rep_configs():
    assert rd.REP_CONFIGS["frontier"].retailer == "Frontier Utilities"
    assert rd.REP_CONFIGS["frontier"].render is not None
    assert rd.REP_CONFIGS["frontier"].extractor is not None


# --------------------------------------------------------------------------- #
# Ambit static extractor (EFL URL constructed from product id)
# --------------------------------------------------------------------------- #
def _extract_ambit():
    html = AMBIT_FIXTURE.read_text(encoding="utf-8")
    return rd.discover(html, rd.AMBIT, use_llm_fallback=False)


def test_ambit_extractor_finds_all_cards_deduped():
    plans = _extract_ambit()
    # 4 show-plan cards; the two same-productid buttons per card dedup to one.
    assert len(plans) == 4
    assert all(p.retailer == "Ambit Energy" for p in plans)
    assert all(p.extraction_method == "static" for p in plans)


def test_ambit_constructs_backend_document_url_not_the_viewer_page():
    """Ambit's EFL must point at /api/getdocument, NOT the /PDFGenerator link the
    "See Plan Details" panel exposes.

    /PDFGenerator is only a viewer page: it returns the Next.js 404 shell
    (text/html, 8 KB) to httpx AND to a real in-browser navigation, even with a
    full funnel session -- so pointing at it silently yielded zero usable EFLs.
    Watching the popup's network traffic (2026-07-24) showed its JS calling
    /api/getdocument and wrapping the result in a blob:. That endpoint serves
    application/pdf to plain httpx, so download_discovered needs no browser.
    Every query parameter is renamed between the two, which is why this asserts
    them individually.
    """
    by_name = {p.plan_name: p for p in _extract_ambit()}
    url = by_name["Texas Solar Buyback 12"].efl_url
    assert url.startswith("https://shopping.ambitenergy.com/api/getdocument?")
    assert "PDFGenerator" not in url
    assert "docType=EnergyFactsLabel" in url
    assert "productid=ONAMTXSBBC12AA" in url
    assert "classification=Residential" in url and "language=en" in url
    assert "tdsp=ONCOR" in url
    # efldate must be full ISO 8601 -- a bare YYYY-MM-DD is not accepted.
    assert "T00:00:00" in url


def test_ambit_flags_buyback_by_name_and_by_description():
    by_name = {p.plan_name: p for p in _extract_ambit()}
    # "12" flagged via the export-credit description; "24" via the name alone
    # (its card has no buyback wording).
    assert by_name["Texas Solar Buyback 12"].is_buyback is True
    assert by_name["Texas Solar Buyback 24"].is_buyback is True
    assert by_name["Free & Clear Nights 12"].is_buyback is False
    assert by_name["Lone Star Flex"].is_buyback is False


def test_ambit_unescapes_plan_name_and_no_phantom_from_script():
    names = {p.plan_name for p in _extract_ambit()}
    assert "Free & Clear Nights 12" in names  # &amp; decoded
    # The script blob's GHOST PDFGenerator URL must be stripped, not discovered.
    assert not any("GHOST" in p.efl_url for p in _extract_ambit())


def test_ambit_has_a_render_flow_with_manual_capture_as_backstop():
    """Ambit gained a render() on 2026-07-24. The old note that its WAF "blocks
    Playwright" was an unlucky sample, not a rule: Azure Front Door answers a
    plain-text 403 probabilistically (plain httpx measured 3/6, Playwright 3/3),
    and what actually hid the plans was a not-moving qualification prelude.

    The render must not become a single point of failure -- `_run_rep_discovery`
    falls back to the newest manual capture when a live render raises, so a
    blocked night degrades to the old behaviour instead of dropping every
    buyback plan (which PTC and meterplan both miss).
    """
    assert rd.REP_CONFIGS["ambit"].retailer == "Ambit Energy"
    assert rd.REP_CONFIGS["ambit"].render is not None


# --------------------------------------------------------------------------- #
# Octopus static extractor (buyback = all plans except OctopusFlex)
# --------------------------------------------------------------------------- #
def _extract_octopus():
    html = OCTOPUS_FIXTURE.read_text(encoding="utf-8")
    return rd.discover(html, rd.OCTOPUS, use_llm_fallback=False)


def test_octopus_extractor_finds_all_cards():
    plans = _extract_octopus()
    assert len(plans) == 3
    assert all(p.retailer == "Octopus Energy" for p in plans)
    assert all("octopusenergy.com/efl/" in p.efl_url for p in plans)


def test_octopus_efl_flagged_as_html_viewer():
    # octopusenergy.com/efl/<code> is a client-rendered viewer, not a PDF, so
    # the downloader must render + print-to-PDF instead of a plain GET.
    assert all(p.efl_is_html_viewer for p in _extract_octopus())


def test_octopus_buyback_is_all_plans_except_flex():
    by_name = {p.plan_name: p for p in _extract_octopus()}
    assert by_name["Octopus Flex"].is_buyback is False
    assert by_name["Octo Green 12"].is_buyback is True
    assert by_name["Octopus Lite 12"].is_buyback is True


def test_octopus_ignores_competitor_and_script_efls():
    urls = {p.efl_url for p in _extract_octopus()}
    # The txu.com PDFGenerator competitor link isn't an Octopus plan.
    assert not any("txu.com" in u for u in urls)
    # The script blob's GHOST /efl/ URL must be stripped, not discovered.
    assert not any("GHOST" in u for u in urls)


def test_octopus_plan_name_from_product_title():
    names = {p.plan_name for p in _extract_octopus()}
    assert {"Octopus Flex", "Octo Green 12", "Octopus Lite 12"} <= names


def test_octopus_registered_in_rep_configs():
    assert rd.REP_CONFIGS["octopus"].retailer == "Octopus Energy"
    assert rd.REP_CONFIGS["octopus"].render is not None


def test_direct_energy_registered_as_harvester():
    cfg = rd.REP_CONFIGS["direct_energy"]
    assert cfg.retailer == "Direct Energy"
    # EFL URLs are JS/blob-driven -> harvester, no static extractor/render.
    assert cfg.harvester is not None
    assert cfg.extractor is None and cfg.render is None


def test_reliant_registered_as_harvester():
    cfg = rd.REP_CONFIGS["reliant"]
    assert cfg.retailer == "Reliant Energy"
    assert cfg.harvester is not None
    assert cfg.extractor is None and cfg.render is None


def test_atlantex_registered_as_harvester():
    cfg = rd.REP_CONFIGS["atlantex"]
    assert cfg.retailer == "Atlantex Power"
    assert cfg.harvester is not None
    assert cfg.extractor is None and cfg.render is None


# --------------------------------------------------------------------------- #
# Octopus render reads ESI ID (PII) from the gitignored secrets file
# --------------------------------------------------------------------------- #
def test_load_rep_secret_reads_yaml(tmp_path):
    p = tmp_path / "secrets.yaml"
    p.write_text("octopus:\n  esiid: 'TEST-ESIID'\n  address_button: '1 MAIN ST'\n")
    got = rd._load_rep_secret("octopus", path=p)
    assert got["esiid"] == "TEST-ESIID"
    assert rd._load_rep_secret("nope", path=p) == {}


def test_load_rep_secret_missing_file_returns_empty(tmp_path):
    assert rd._load_rep_secret("octopus", path=tmp_path / "absent.yaml") == {}


class _RecordingPage:
    """Records role/name interactions so the render flow can be asserted
    without a browser. fill/click are no-ops that just log."""

    def __init__(self):
        self.filled = {}
        self.clicked = []

    def get_by_role(self, role, name=None, **k):
        return _RecordingLoc(self, name)

    def wait_for_timeout(self, ms):
        pass


class _RecordingLoc:
    def __init__(self, page, name):
        self.page = page
        self.name = name

    @property
    def first(self):
        return self

    def fill(self, value, **k):
        self.page.filled[self.name] = value

    def click(self, **k):
        self.page.clicked.append(self.name)


def test_octopus_render_reads_esiid_from_secrets(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("octopus:\n  esiid: 'ESI-123'\n  address_button: '1 MAIN ST'\n")
    monkeypatch.setattr(rd, "_SECRETS_PATH", secrets)
    page = _RecordingPage()
    rd._octopus_render(page, "78665")
    # ZIP + the ESI ID (from secrets, not hardcoded) reach the form.
    assert page.filled.get("Zip code") == "78665"
    assert page.filled.get("Enter your ESI ID Number") == "ESI-123"
    assert "Get a quote" in page.clicked
    assert "1 MAIN ST" in page.clicked  # matched-address confirmation


def test_octopus_render_errors_without_esiid(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("octopus:\n  address_button: '1 MAIN ST'\n")  # no esiid
    monkeypatch.setattr(rd, "_SECRETS_PATH", secrets)
    with pytest.raises(RuntimeError, match="requires an ESI ID"):
        rd._octopus_render(_RecordingPage(), "78665")


# --------------------------------------------------------------------------- #
# Champion interactive harvester (EFL URL read from a popup, not the DOM)
# --------------------------------------------------------------------------- #
class _FakeLoc:
    """A minimal Playwright-locator stand-in for the harvester's needs."""

    def __init__(self, page, name=None):
        self.page = page
        self.name = name
        self._nth = 0

    @property
    def first(self):
        return self

    def nth(self, i):
        self._nth = i
        return self

    def count(self):
        if self.name == "See More Plan Details":
            return len(self.page.plans)
        return 1

    def fill(self, value, **k):
        pass

    def inner_text(self, **k):
        # The "Details of <plan>" modal heading for the currently open plan.
        if self.page.current is not None:
            return f"Details of {self.page.plans[self.page.current]['name']}"
        return ""

    def click(self, **k):
        if self.name == "See More Plan Details":
            self.page.current = self._nth
        elif self.name == "Electricity Facts Label":
            # The click that (in a real browser) spawns the EFL popup.
            self.page._pending_popup = self.page.plans[self.page.current]["efl"]
        elif self.name in ("Close", "Close this dialog"):
            # "Close" closes the plan-details modal; "Close this dialog" is the
            # nav-prelude interstitial. Either way, no modal is open afterward.
            self.page.current = None
        # nav buttons (View Rates, New Service, zip text) are no-ops


class _FakePopupCtx:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        self.page._pending_popup = None
        return self

    def __exit__(self, *exc):
        return False

    @property
    def value(self):
        return _FakePopup(self.page._pending_popup)


class _FakePopup:
    def __init__(self, url):
        self.url = url

    def wait_for_load_state(self, *a, **k):
        pass

    def close(self):
        pass


class _FakeChampionPage:
    def __init__(self, plans):
        self.plans = plans  # [{"name":..., "efl":...}]
        self.current = None
        self._pending_popup = None

    def get_by_role(self, role, name=None, **k):
        return _FakeLoc(self, name=name)

    def get_by_text(self, text, **k):
        return _FakeLoc(self, name="__text__")

    def expect_popup(self):
        return _FakePopupCtx(self)

    def wait_for_timeout(self, ms):
        pass


_CHAMPION_PLANS = [
    {"name": "Champ Saver-24",
     "efl": "https://docs.championenergyservices.com/ExternalDocs?planName=PN2388&state=TX&language=EN"},
    {"name": "Green Energy-24",
     "efl": "https://docs.championenergyservices.com/ExternalDocs?planName=PN5129&state=TX&language=EN"},
    {"name": "EV Saver-12",
     "efl": "https://docs.championenergyservices.com/ExternalDocs?planName=PN5130&state=TX&language=EN"},
]


def test_champion_harvester_reads_efl_url_from_popup():
    page = _FakeChampionPage(_CHAMPION_PLANS)
    plans = rd._champion_harvest(page, "78665", rd.CHAMPION)
    assert len(plans) == 3
    assert all(p.extraction_method == "harvest" for p in plans)
    assert all(p.is_buyback is True for p in plans)  # all bundle indexed buyback
    by_name = {p.plan_name: p for p in plans}
    assert "planName=PN5129" in by_name["Green Energy-24"].efl_url
    assert "planName=PN5130" in by_name["EV Saver-12"].efl_url


def test_champion_harvester_dedups_by_plan_code():
    dupes = _CHAMPION_PLANS + [_CHAMPION_PLANS[1]]  # Green Energy twice
    plans = rd._champion_harvest(_FakeChampionPage(dupes), "78665", rd.CHAMPION)
    codes = [p.efl_url for p in plans]
    assert len(codes) == len(set(codes)) == 3  # the repeat is deduped


def test_champion_harvester_skips_plan_when_popup_has_no_url():
    plans_in = [
        _CHAMPION_PLANS[0],
        {"name": "Broken Plan", "efl": None},  # popup yields no URL
        _CHAMPION_PLANS[2],
    ]
    plans = rd._champion_harvest(_FakeChampionPage(plans_in), "78665", rd.CHAMPION)
    names = {p.plan_name for p in plans}
    assert "Broken Plan" not in names and len(plans) == 2


def test_champion_registered_as_harvester_only():
    cfg = rd.REP_CONFIGS["champion"]
    assert cfg.harvester is not None
    assert cfg.extractor is None and cfg.render is None


def test_discover_rejects_harvester_only_config():
    # discover() is the static path; a harvester-only REP must route to
    # harvest_live() instead, with a clear error rather than a None crash.
    with pytest.raises(ValueError, match="no static extractor"):
        rd.discover("<html></html>", rd.CHAMPION)


def test_repconfig_requires_extractor_or_harvester():
    with pytest.raises(ValueError, match="must define either an extractor"):
        rd.RepConfig(key="bad", retailer="X", homepage="https://x/")


def test_harvest_live_without_playwright_raises_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="Playwright is required"):
        rd.harvest_live(rd.CHAMPION, "78665")


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
# LLM review pass (upgrade-only, over all returned EFLs)
# --------------------------------------------------------------------------- #
def _static_two(html, config):
    return [
        rd.DiscoveredPlan(retailer="R", plan_name="Solar Saver 12", efl_url="a.pdf",
                          is_buyback=False, context="a plan with export credits"),
        rd.DiscoveredPlan(retailer="R", plan_name="Simple Fixed 12", efl_url="b.pdf",
                          is_buyback=False, context="a flat-rate plan"),
    ]


def test_llm_review_upgrades_missed_buyback():
    # Static missed the buyback flag on "Solar Saver 12"; review recovers it so
    # a buyback-only download won't drop it.
    def review_chat(messages, model, ollama_url, timeout):
        user = messages[-1]["content"]
        is_bb = "Solar Saver" in user
        return {"message": {"content": json.dumps(
            {"is_efl": True, "is_buyback": is_bb, "confidence": 0.9, "thinking": "reviewed"}
        )}}

    plans = rd.discover(
        "<html></html>", _dummy_config(_static_two),
        use_llm_fallback=False, llm_review=True, chat_fn=review_chat,
    )
    by_name = {p.plan_name: p for p in plans}
    assert by_name["Solar Saver 12"].is_buyback is True   # upgraded
    assert by_name["Simple Fixed 12"].is_buyback is False  # unchanged
    assert all(p.llm_confidence == 0.9 for p in plans)     # every EFL reviewed


def test_llm_review_is_upgrade_only_never_drops():
    # Even if the LLM says "not buyback", the plan is kept (never removed) and a
    # statically-flagged buyback is never downgraded.
    def deny_chat(messages, model, ollama_url, timeout):
        return {"message": {"content": json.dumps(
            {"is_efl": False, "is_buyback": False, "confidence": 0.99, "thinking": "no"}
        )}}

    def static_bb(html, config):
        return [rd.DiscoveredPlan(retailer="R", plan_name="Solar BB", efl_url="a.pdf",
                                  is_buyback=True, context="buyback")]

    plans = rd.discover(
        "<html></html>", _dummy_config(static_bb),
        use_llm_fallback=False, llm_review=True, chat_fn=deny_chat,
    )
    assert len(plans) == 1                 # not dropped
    assert plans[0].is_buyback is True     # not downgraded


def test_llm_review_below_threshold_does_not_upgrade():
    def weak_chat(messages, model, ollama_url, timeout):
        return {"message": {"content": json.dumps(
            {"is_efl": True, "is_buyback": True, "confidence": 0.5, "thinking": "maybe"}
        )}}

    plans = rd.discover(
        "<html></html>", _dummy_config(_static_two),
        use_llm_fallback=False, llm_review=True, review_min_confidence=0.7, chat_fn=weak_chat,
    )
    assert all(p.is_buyback is False for p in plans)  # 0.5 < 0.7 -> no upgrade


def test_llm_review_tolerates_ollama_down():
    def boom(*args, **kwargs):
        raise ConnectionError("no ollama")

    plans = rd.discover(
        "<html></html>", _dummy_config(_static_two),
        use_llm_fallback=False, llm_review=True, chat_fn=boom,
    )
    assert len(plans) == 2  # deterministic results stand, no raise


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


def test_download_discovered_renders_html_viewer_to_pdf(tmp_path, monkeypatch):
    # A plan whose EFL is an HTML viewer must be rendered + print-to-PDF via
    # _render_efl_pdf, NOT fetched over httpx (which would get only the shell).
    render_calls = []

    def _fake_render(url, headless=True, timeout_ms=60000, settle_ms=6000):
        render_calls.append(url)
        return b"%PDF-1.7 rendered octopus efl"

    class _NoGetClient(_FakeClient):
        def get(self, url, *args, **kwargs):  # must not be used for viewer plans
            raise AssertionError("httpx GET used for an HTML-viewer EFL")

    import httpx

    monkeypatch.setattr(rd, "_render_efl_pdf", _fake_render)
    monkeypatch.setattr(httpx, "Client", _NoGetClient)

    plan = rd.DiscoveredPlan(
        retailer="Octopus Energy",
        plan_name="Octo Green 12",
        efl_url="https://octopusenergy.com/efl/OCTO-GREEN-12-ONCOR-LZ_SOUTH-2026",
        is_buyback=True,
        efl_is_html_viewer=True,
    )
    dest = tmp_path / "efl"
    summary = rd.download_discovered([plan], dest=dest, buyback_only=True)

    assert render_calls == [plan.efl_url]
    assert len(summary["downloaded"]) == 1
    saved = dest / "Octopus_Energy_Octo_Green_12.pdf"
    assert saved.exists() and saved.read_bytes().startswith(b"%PDF")


def test_download_discovered_defers_html_response(tmp_path, monkeypatch):
    # A discovered EFL URL that resolves to an HTML viewer/SPA shell (not a PDF)
    # -- e.g. the Vistra shopping.* PDFGenerator endpoint -- is deferred (needs a
    # browser), not counted as a download failure and not saved as a .pdf.
    class _HtmlResponse:
        content = b"<!DOCTYPE html><html><body>viewer</body></html>"
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            return None

    class _HtmlClient(_FakeClient):
        def get(self, url, *args, **kwargs):
            self.calls.append(url)
            return _HtmlResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", _HtmlClient)
    dest = tmp_path / "efl"
    summary = rd.download_discovered(_sample_plans(), dest=dest, buyback_only=True)

    assert summary["downloaded"] == []
    assert summary["failed"] == []
    assert len(summary["deferred"]) == 1
    assert "HTML response" in summary["deferred"][0]["reason"]
    assert not list(dest.glob("*.pdf"))
    # A deferred download writes no manifest entry either.
    assert not (dest / "rep_discovery_manifest.jsonl").exists()


def test_download_discovered_guards_malformed_url(tmp_path, monkeypatch):
    # A malformed/constructed EFL URL (no scheme) is a clear failure line, not an
    # opaque httpx ValueError, and never hits the network.
    class _NoNetClient(_FakeClient):
        def get(self, url, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("malformed URL should not be fetched")

    import httpx

    monkeypatch.setattr(httpx, "Client", _NoNetClient)
    bad = rd.DiscoveredPlan(retailer="Ambit Energy", plan_name="Weird", efl_url="/", is_buyback=True)
    summary = rd.download_discovered([bad], dest=tmp_path / "efl", buyback_only=True)
    assert summary["downloaded"] == []
    assert len(summary["failed"]) == 1
    assert "malformed" in summary["failed"][0]["error"]


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


def test_download_discovered_logs_progress(tmp_path, monkeypatch, caplog):
    # Discovery emits INFO logs the Plans page streams into a live "console"
    # (attached to the "energyanalyzer" package logger). Confirm the download
    # step narrates itself so a slow run isn't a frozen status line.
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    dest = tmp_path / "efl"
    with caplog.at_level("INFO", logger="energyanalyzer.fetchers.rep_discovery"):
        rd.download_discovered(_sample_plans(), dest=dest, buyback_only=True)
    messages = [r.getMessage() for r in caplog.records]
    assert any("Downloading" in m and "EFL" in m for m in messages)
    assert any("Download 1/" in m for m in messages)


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


# --------------------------------------------------------------------------- #
# RepConfig.link_base: relative EFL hrefs must resolve to the host that serves
# them, which is not always the host you navigate to.
# --------------------------------------------------------------------------- #
def test_chariot_efl_urls_resolve_to_the_signup_host():
    """Regression for a silent, whole-REP outage (2026-07-24): all 11 Chariot
    EFLs 404'd.

    Chariot's cards carry RELATIVE hrefs ("/Home/EFl?productId=..."). The
    marketing site chariotenergy.com hands off to signup.chariotenergy.com, but
    the extractor resolved against `homepage`, producing
    chariotenergy.com/Home/EFl?... -- a 404. Nothing caught it at discovery time
    because a bad base only surfaces much later, as a failed download.
    """
    plans = _extract_chariot()
    assert plans, "fixture should yield plans"
    for p in plans:
        assert p.efl_url.startswith("https://signup.chariotenergy.com/Home/EFl?"), p.efl_url


def test_link_base_defaults_to_homepage_when_unset():
    """Only REPs that hand off to another host need `efl_base`; everyone else
    must keep resolving against `homepage`."""
    from energyanalyzer.fetchers.rep_discovery import GREEN_MOUNTAIN, CHARIOT

    assert GREEN_MOUNTAIN.efl_base is None
    assert GREEN_MOUNTAIN.link_base == GREEN_MOUNTAIN.homepage
    assert CHARIOT.link_base == "https://signup.chariotenergy.com/"
    assert CHARIOT.link_base != CHARIOT.homepage
