"""REP-site EFL discovery fetcher (ARCHITECTURE.md §7; see rep_discovery_handoff.md).

Power to Choose and meterplan.com between them miss some solar **buyback** plans
that only ever appear on an individual retailer's marketing site. This module
finds and downloads those EFLs, feeding the same ``data/efl/`` landing zone the
rest of the pipeline consumes (``app.common.parse_downloaded_efls`` ->
``plans/drafts/`` review/promote UI).

Design (deterministic-first, mirroring ``eflparse``'s philosophy), with two
optional LLM tiers layered on top:

1. **Static extraction.** Many REP sites self-label everything -- Green
   Mountain's rendered plans page carries an explicit
   ``<a>Electricity Facts Label</a>`` link per plan and a hidden analytics div
   flagging buyback plans directly (``analyticscontractrates="...^BuyBack:11.4"``);
   TXU's EFL links self-label via a ``PDFGenerator?formType=EnergyFactsLabel``
   URL, with buyback announced in each ``show-plan`` card's visible text. A
   per-REP static extractor (regex/DOM query, NO LLM) handles those.
2. **LLM fallback.** For sites that *don't* self-label this cleanly (an
   ambiguous "Download" button, a bare filename), :func:`classify_link_llm`
   asks a local Ollama model (``lfm2.5``, JSON-forced output) to classify a
   link given its real surrounding page context. The model does not reliably
   know domain facts unprompted, so the prompt carries an explicit EFL/buyback
   definition plus worked examples and always feeds real page text -- never a
   bare URL.
3. **LLM review** (``discover(..., llm_review=True)``). Every EFL the static
   extractor returned is re-checked by the LLM, so a wording change on the site
   can't silently lose a solar-buyback plan to the static buyback heuristic.
   Review is **upgrade-only** (promotes to buyback at/above a confidence
   threshold, never removes a discovered EFL) -- erring toward keeping buyback
   EFLs. Validated live against TXU: all 10 plans reviewed, only the real
   buyback plan flagged, zero false upgrades.

Like the other fetchers in this package the module is **offline-first**: the
static extractors and the LLM classifier operate on rendered HTML already on
disk. The only network-touching pieces are:

* :func:`fetch_rendered_html` -- drives a real browser via Playwright (REP
  sites are client-rendered SPAs that a raw ``httpx`` fetch returns empty).
  Playwright is an optional dependency (``pip install 'energyanalyzer[discovery]'``
  then ``playwright install chromium``); it is imported lazily and the function
  raises a clear RuntimeError with install/manual-fallback instructions if it
  is missing or the navigation fails.
* :func:`classify_link_llm` -- a thin ``httpx`` call to a local Ollama server.
* :func:`download_discovered` -- downloads matched EFL PDFs (plain ``httpx``).

Per ARCHITECTURE.md §9 this is **not** wired into
``app.common.refresh_market_data`` yet -- Green Mountain works standalone first;
generalizing to more REPs is a follow-up.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

# Discovery is slow (live browser per REP). These INFO logs narrate each step so
# a caller can stream them into a live "console" (the Plans page attaches a
# handler to the "energyanalyzer" logger during a refresh) -- so a long-running
# harvester shows what it's doing, not just a frozen "querying site" line.
logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Ollama defaults for the LLM fallback classifier.
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "lfm2.5"

# Politeness: minimum seconds between successive live requests to the same
# host (both browser navigations and EFL downloads share this throttle).
_MIN_REQUEST_INTERVAL_S = 2.0
_last_request_at: dict[str, float] = {}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class DiscoveredPlan:
    """One plan discovered on a REP site, with its EFL link and what we could
    determine about it deterministically (or via the LLM fallback)."""

    retailer: str
    plan_name: str
    efl_url: str
    is_buyback: Optional[bool] = None  # None = undetermined
    buyback_ckwh: Optional[float] = None
    extraction_method: str = "static"  # "static" | "llm" | "harvest"
    llm_confidence: Optional[float] = None
    context: str = ""  # link text / surrounding snippet (audit trail)
    # True when ``efl_url`` is an HTML EFL *viewer* (client-rendered), not a
    # direct PDF -- e.g. Octopus's octopusenergy.com/efl/<code> page. The
    # downloader renders these in a headless browser and print-to-PDFs them
    # instead of a plain httpx GET (which would only save the SPA shell).
    efl_is_html_viewer: bool = False


@dataclass
class RepConfig:
    """Per-REP discovery configuration. Each site's navigation flow differs, so
    ``render`` is a per-REP function (record it once with
    ``playwright codegen <homepage>`` and adapt), and ``extractor`` is a per-REP
    static parser over the rendered HTML. Start with Green Mountain and add REPs
    without touching the rest of the module.

    Most REPs self-label an EFL URL in their rendered HTML, so they set
    ``extractor`` (parsed by :func:`discover`). A few compute the EFL URL only on
    interaction -- e.g. Champion's EFL is a JS ``<button>`` that opens the PDF in
    a popup, with no URL anywhere in the DOM. Those set ``harvester`` instead: an
    interactive function that drives the browser and returns ``DiscoveredPlan``s
    directly (run by :func:`harvest_live`). A config must set at least one of the
    two."""

    key: str
    retailer: str
    homepage: str
    extractor: Optional[Callable[[str, "RepConfig"], list[DiscoveredPlan]]] = None
    # render(page, zip_code): drive the ZIP gate / "View Plans" flow. Returns
    # None (caller captures page.content() once) OR, for a paginated listing, a
    # string of concatenated per-page HTML the render collected itself (the
    # extractor splits on plan cards and dedups, so page boundaries don't matter).
    render: Optional[Callable[[object, str], Optional[str]]] = None
    # harvester(page, zip_code, config) -> [DiscoveredPlan]: for REPs whose EFL
    # URLs aren't in the DOM. Drives the browser (open each plan's details, click
    # the EFL trigger, read the popup URL) and returns plans directly -- no HTML
    # extract step. Mutually complementary with `extractor`; a config needs one.
    harvester: Optional[
        Callable[[object, str, "RepConfig"], list[DiscoveredPlan]]
    ] = None
    # Whether the live-fetch helpers honor this REP's robots.txt. Default True
    # (we abort out of politeness on an explicit Disallow). Set False ONLY as a
    # deliberate, per-REP decision -- e.g. Frontier's enrollment subdomain
    # carries a blanket `Disallow: /` aimed at search-crawler indexing, but the
    # user opted to treat a single rate-limited fetch of their own shopping page
    # as outside that intent. Never a blanket default.
    check_robots: bool = True
    # Whether to keep ALL of this REP's discovered plans (True) or only its solar
    # buyback ones (False). Default True -- pulling every plan captures the
    # website-only plans PTC misses. Set False for REPs whose EFL URLs aren't
    # httpx-downloadable (the Vistra shopping.* PDFGenerator endpoint TXU/Ambit
    # use returns an HTML SPA shell), so their many conventional plans -- already
    # on Power to Choose -- don't flood the run with un-downloadable EFLs.
    broaden: bool = True

    def __post_init__(self) -> None:
        if self.extractor is None and self.harvester is None:
            raise ValueError(
                f"RepConfig {self.key!r} must define either an extractor "
                "(static HTML parse) or a harvester (interactive browser flow)."
            )


# --------------------------------------------------------------------------- #
# Shared HTML helpers
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _strip_comments(html: str) -> str:
    """Drop HTML comments before parsing -- rendered pages carry commented-out
    markup (and our own fixtures document structure in comments) that would
    otherwise pollute tag/anchor matching."""
    return _HTML_COMMENT_RE.sub("", html)

# Trademark/registered/copyright glyphs, stripped from plan names before any
# NFKD folding -- NFKD expands U+2122 ((TM)) to the *letters* "TM", which would
# otherwise inject stray characters and break name matching (e.g. the
# "Pollution Free (TM) e-Plus 18" heading vs. the analytics div's clean
# "Pollution Free e-Plus 18").
_TRADEMARK_RE = re.compile(r"[™®©]")


def _strip_tags(s: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", s)).strip()


def _clean_plan_name(name: str) -> str:
    """Human-facing plan name: decode HTML entities, drop trademark glyphs, and
    collapse whitespace."""
    return _WS_RE.sub(" ", _TRADEMARK_RE.sub("", unescape(name))).strip()


def _normalize_name(name: str) -> str:
    """Fold a plan name to a comparison key: drop trademark glyphs, accents and
    everything but alphanumerics, lowercase. So ``Renewable Rewards(R) Solar
    Max 12`` and the analytics ``Renewable Rewards Solar Max 12`` collapse to
    the same key."""
    name = _TRADEMARK_RE.sub("", name)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Some REP sites require PII to render plans (e.g. Octopus needs an ESI ID when a
# ZIP spans load zones). That never belongs in code or git -- it's read at render
# time from a gitignored secrets file (data/* is ignored; only data/README.md is
# tracked). Keyed by REP: {rep_key: {esiid, address_button, ...}}.
_SECRETS_PATH = Path("data/rep_discovery_secrets.yaml")


def _load_rep_secret(rep_key: str, path: Optional[Path] = None) -> dict:
    """Read a REP's private render inputs from the gitignored secrets file.
    Returns {} if the file or the REP's entry is absent. ``path`` resolves to the
    module ``_SECRETS_PATH`` at call time (not import time) so it stays patchable."""
    if path is None:
        path = _SECRETS_PATH
    try:
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return data.get(rep_key, {}) or {}


# --------------------------------------------------------------------------- #
# Green Mountain static extractor
# --------------------------------------------------------------------------- #
_EFL_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref="([^"]+)"[^>]*>\s*Electricity Facts Label\s*</a>', re.I
)
_H3_RE = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.I | re.S)
_ANALYTICS_DIV_RE = re.compile(r"<div\b[^>]*\banalyticsproductname=\"[^\"]*\"[^>]*>", re.I)
_ANALYTICS_NAME_RE = re.compile(r'analyticsproductname="([^"]*)"', re.I)
_ANALYTICS_RATES_RE = re.compile(r'analyticscontractrates="([^"]*)"', re.I)
_BUYBACK_FLAG_RE = re.compile(r"\bBuyBack:(\d+(?:\.\d+)?)", re.I)


def _analytics_buyback_index(html: str) -> dict[str, Optional[float]]:
    """Map normalized plan name -> buyback ¢/kWh (or None if the plan's hidden
    analytics div carries no ``^BuyBack:`` flag). Green Mountain's own tracking
    markup is the authoritative self-label for which plans are buyback."""
    index: dict[str, Optional[float]] = {}
    for m in _ANALYTICS_DIV_RE.finditer(html):
        div = m.group(0)
        name_m = _ANALYTICS_NAME_RE.search(div)
        if not name_m:
            continue
        key = _normalize_name(name_m.group(1))
        rates_m = _ANALYTICS_RATES_RE.search(div)
        bb = _BUYBACK_FLAG_RE.search(rates_m.group(1)) if rates_m else None
        index[key] = float(bb.group(1)) if bb else None
    return index


def extract_green_mountain(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static (no-LLM) extractor for Green Mountain's rendered plans page.

    Each plan's (DOM-present but hidden) "Learn more" modal contains an
    "Important Documents" section with an explicit ``Electricity Facts Label``
    anchor; the nearest preceding ``<h3>`` is the plan name. Separately, each
    plan's hidden analytics div flags buyback plans via
    ``analyticscontractrates="...^BuyBack:<rate>"``. We join the two by
    normalized plan name so every discovered plan carries its self-labeled
    buyback status -- no LLM required.
    """
    html = _strip_comments(html)
    # (offset, plan_name) for every <h3>, in document order.
    headings = [(m.start(), _strip_tags(m.group(1))) for m in _H3_RE.finditer(html)]
    buyback_index = _analytics_buyback_index(html)
    base = config.homepage

    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for m in _EFL_ANCHOR_RE.finditer(html):
        href = urljoin(base, m.group(1).strip())
        # Nearest <h3> before this anchor is the plan the modal belongs to.
        prior = [name for off, name in headings if off < m.start()]
        plan_name = _clean_plan_name(prior[-1]) if prior else ""
        key = _normalize_name(plan_name)
        if key in seen:  # a plan can list the EFL link more than once
            continue
        seen.add(key)
        is_buyback: Optional[bool]
        buyback_ckwh: Optional[float] = None
        if key in buyback_index:
            buyback_ckwh = buyback_index[key]
            is_buyback = buyback_ckwh is not None
        else:
            is_buyback = None  # no analytics match -> undetermined
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=plan_name,
                efl_url=href,
                is_buyback=is_buyback,
                buyback_ckwh=buyback_ckwh,
                extraction_method="static",
                context="Important Documents > Electricity Facts Label",
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# TXU static extractor
# --------------------------------------------------------------------------- #
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)
# TXU renders each plan in a card <div class="... show-plan ...">.
_TXU_CARD_SPLIT_RE = re.compile(r'(?=<div\b[^>]*\bclass="[^"]*\bshow-plan\b)')
# The card title is a <p> whose class carries both these tokens.
_TXU_TITLE_RE = re.compile(r'<p\b[^>]*\bclass="([^"]*)"[^>]*>(.*?)</p>', re.I | re.S)
# The EFL link IS its own self-label: TXU points at a PDF generator with
# formType=EnergyFactsLabel and the plan's product id in comProdId.
_TXU_EFL_URL_RE = re.compile(
    r'href="([^"]*PDFGenerator\?formType=EnergyFactsLabel[^"]*)"', re.I
)
_TXU_COMPRODID_RE = re.compile(r"comProdId=([A-Za-z0-9]+)", re.I)
# Buyback self-labeling in a card's *visible* text (scripts are stripped first,
# so TXU's embedded Next.js "Solar Panel Buyback" badge JSON can't leak in).
_TXU_BUYBACK_RE = re.compile(r"buyback|excess energy|excess solar|panels required", re.I)


def _txu_card_title(card: str) -> str:
    """First <p> in the card whose class marks it as the plan title
    (`font-mProBlack` + `text-txublue`)."""
    for m in _TXU_TITLE_RE.finditer(card):
        cls = m.group(1)
        if "font-mProBlack" in cls and "text-txublue" in cls:
            return _clean_plan_name(_strip_tags(m.group(2)))
    return ""


def extract_txu(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static (no-LLM) extractor for TXU's rendered plans page.

    TXU self-labels cleanly, but differently from Green Mountain: EFL links are
    a ``PDFGenerator?formType=EnergyFactsLabel&comProdId=<id>`` URL (the query
    string *is* the label — no reliance on the link text, which trails an icon
    ``<span>``), and each plan is a ``show-plan`` card carrying a styled title
    ``<p>`` and a visible description. Buyback plans announce themselves in that
    description ("...bill credits for your excess energy... Panels required.").

    Scripts are stripped first: TXU embeds a Next.js data blob (~half the page)
    whose badge metadata includes "Solar Panel Buyback"/"Our Best Buyback Rate"
    strings that would otherwise false-positive non-buyback cards (e.g. Flex
    Forward). Buyback rate isn't published on this page (it's in the EFL PDF),
    so `buyback_ckwh` stays None.
    """
    # Comments before scripts: a commented-out <script> tag must not let the
    # script regex run past it into real markup.
    html = _SCRIPT_RE.sub("", _strip_comments(html))
    cards = [c for c in _TXU_CARD_SPLIT_RE.split(html) if "show-plan" in c[:120]]
    base = config.homepage

    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for card in cards:
        url_m = _TXU_EFL_URL_RE.search(card)
        if not url_m:
            continue
        efl_url = urljoin(base, unescape(url_m.group(1)))
        pid_m = _TXU_COMPRODID_RE.search(efl_url)
        product_id = pid_m.group(1) if pid_m else efl_url
        if product_id in seen:
            continue
        seen.add(product_id)
        plan_name = _txu_card_title(card) or product_id
        is_buyback = bool(
            _TXU_BUYBACK_RE.search(card) or re.search(r"solar|buyback", plan_name, re.I)
        )
        # Store the card's visible text as context so an optional LLM review
        # pass (discover(..., llm_review=True)) has real wording to judge.
        card_text = _strip_tags(card)[:600]
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=plan_name,
                efl_url=efl_url,
                is_buyback=is_buyback,
                buyback_ckwh=None,
                extraction_method="static",
                context=f"[comProdId={product_id}] {card_text}",
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# Chariot Energy static extractor
# --------------------------------------------------------------------------- #
# Chariot renders each plan as a <div class="planbox"> card. Its solar-buyback
# products (Shine / PowerBank / GreenVolt) live behind a "My home has solar
# panels" gate -- the general shop-rates listing has none -- so this extractor
# is always driven with the solar render flow (see _chariot_render).
_CHARIOT_CARD_SPLIT_RE = re.compile(r'(?=<div\b[^>]*\bclass="planbox")')
_CHARIOT_NAME_RE = re.compile(r'<p\b[^>]*\bclass="planname"[^>]*>(.*?)</p>', re.I | re.S)
# The EFL link IS the self-label: /Home/EFl?productId=<id> (the sibling TOS/YRAC
# links use /Home/TOS? and /Home/YRAC?, so this pattern won't catch them). The
# \d+ requirement also skips the un-rendered #:ProductId# template row.
_CHARIOT_EFL_URL_RE = re.compile(r'href="(/Home/EFl\?productId=\d+[^"]*)"', re.I)
_CHARIOT_PRODID_RE = re.compile(r"productId=(\d+)", re.I)
# Buyback self-label: "buyback" / "excess energy" appear in every solar card's
# visible description and in none of the non-solar plans. ("rooftop solar" is
# deliberately NOT a signal -- the non-solar plans carry a "Restrictions apply
# for customers with rooftop solar and/or batteries" disclaimer.)
_CHARIOT_BUYBACK_RE = re.compile(r"buyback|excess energy", re.I)
# Published buyback rate, when fixed: "...buyback rate of 3 Cents per kWh..." or
# a "Fixed 7¢ Buyback" tagline. Shine advertises a "real-time market buyback
# rate" with no number, so buyback_ckwh stays None for it.
_CHARIOT_RATE_RE = re.compile(r"buyback rate\s+of\s+(\d+(?:\.\d+)?)\s*(?:¢|cents?)\b", re.I)
_CHARIOT_RATE_ALT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*¢\s*buyback", re.I)


def _chariot_buyback_ckwh(text: str) -> Optional[float]:
    """Parse a fixed buyback ¢/kWh rate from a card's visible text, or None for
    a market-rate plan (no published number)."""
    m = _CHARIOT_RATE_RE.search(text) or _CHARIOT_RATE_ALT_RE.search(text)
    return float(m.group(1)) if m else None


def extract_chariot(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static (no-LLM) extractor for Chariot's rendered plans page(s).

    Chariot self-labels like TXU: each plan is a ``planbox`` card with a
    ``planname`` title and an EFL link that is its own label
    (``/Home/EFl?productId=<id>``). The card's ``plandescription`` announces
    buyback ("Earn a fixed buyback rate of 3 Cents per kWh for excess energy
    from your panels") and often the fixed rate, which we parse into
    ``buyback_ckwh`` (None for Shine's market-rate plans).

    The listing is paginated; ``_chariot_render`` concatenates the pages, so we
    dedup cards by product id. Comments/scripts/SVGs are stripped first (the
    page is dense with inline icon ``<svg>`` path data that would pollute the
    card text).
    """
    html = _strip_comments(html)
    html = _SCRIPT_RE.sub("", html)
    html = re.sub(r"<svg\b[^>]*>.*?</svg>", " ", html, flags=re.I | re.S)
    cards = [c for c in _CHARIOT_CARD_SPLIT_RE.split(html) if 'class="planbox"' in c[:60]]
    base = config.homepage

    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for card in cards:
        url_m = _CHARIOT_EFL_URL_RE.search(card)
        if not url_m:
            continue
        efl_url = urljoin(base, unescape(url_m.group(1)))
        pid_m = _CHARIOT_PRODID_RE.search(efl_url)
        product_id = pid_m.group(1) if pid_m else efl_url
        if product_id in seen:  # same plan repeated across concatenated pages
            continue
        seen.add(product_id)
        name_m = _CHARIOT_NAME_RE.search(card)
        plan_name = _clean_plan_name(_strip_tags(name_m.group(1))) if name_m else product_id
        card_text = _strip_tags(unescape(card))
        is_buyback = bool(_CHARIOT_BUYBACK_RE.search(card_text))
        buyback_ckwh = _chariot_buyback_ckwh(card_text) if is_buyback else None
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=plan_name,
                efl_url=efl_url,
                is_buyback=is_buyback,
                buyback_ckwh=buyback_ckwh,
                extraction_method="static",
                context=f"[productId={product_id}] {card_text[:600]}",
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# Gexa Energy static extractor
# --------------------------------------------------------------------------- #
# Shared "eflviewer" enrollment platform (Gexa + Frontier both run the same
# Vistra/eflviewer shopping stack).
# --------------------------------------------------------------------------- #
# Each plan renders as a <div class="row ... plan-list-padding"> card: an <h3>
# plan name, a <ul class="plan-list"> with "Plan Type: <b>Solar Buyback</b>" for
# buyback plans, and an EFL link (an eflviewer.aspx URL carrying the prodcode)
# inside a nested .EFL_PlanCard. The "Solar Buyback & EV" category tab is a
# client-side filter -- all plans stay in the DOM -- so the extractor sees every
# plan and flags buyback per card.
_EFLVIEWER_CARD_SPLIT_RE = re.compile(r'(?=<div\b[^>]*\bclass="row[^"]*\bplan-list-padding\b)')
_EFLVIEWER_NAME_RE = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.I | re.S)
# The EFL link IS the self-label: an eflviewer.aspx URL carrying the prodcode
# (the host differs per REP -- eflviewer.gexaenergy.com vs
# eflviewer.frontierutilities.com -- so the pattern keys on the path, not host).
_EFLVIEWER_EFL_URL_RE = re.compile(r'href="([^"]*eflviewer\.aspx\?[^"]*)"', re.I)
_EFLVIEWER_PRODCODE_RE = re.compile(r"prodcode=([^&\"]+)", re.I)
# Buyback self-label (matched on tag-stripped card text): the plan-list line
# "Plan Type: Solar Buyback", or the export-credit wording. A bare "Solar
# Buyback" ribbon (the .Product-tab badge, which the DOM positions at the end of
# the *previous* card) is deliberately NOT a signal.
_EFLVIEWER_BUYBACK_RE = re.compile(r"Plan Type:\s*Solar Buyback", re.I)
_EFLVIEWER_EXPORT_RE = re.compile(r"excess energy[^.]{0,40}(?:export|grid)", re.I)
# The .Product-tab category ribbon is DOM-positioned at the END of a card, but
# it labels the NEXT one -- so a "Solar Buyback" ribbon trails the *previous*
# (often non-buyback) card. The deterministic buyback check already ignores it
# (it keys on the plan-list "Plan Type"), but it must also be stripped so it
# can't leak into a plan's `context` and mislead the optional LLM review: an
# lfm2.5 probe of the Gexa capture false-upgraded "Energy Saver 12" solely
# because its context ended in the trailing "Solar Buyback" ribbon, and the
# model (correctly, given the flattened text) read it as part of the plan.
_EFLVIEWER_RIBBON_RE = re.compile(r"<div\b[^>]*\bProduct-tab\b.*?</div>\s*</div>", re.I | re.S)


def _extract_eflviewer_platform(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static (no-LLM) extractor for the shared eflviewer enrollment platform
    (Gexa, Frontier).

    Each plan is a ``plan-list-padding`` row: the ``<h3>`` is the name and the
    EFL link self-labels via an ``eflviewer.aspx?...&prodcode=<code>`` URL.
    Buyback plans carry ``Plan Type: Solar Buyback`` in their feature list (plus
    export-credit wording); we key on that per card rather than the free-floating
    ``Solar Buyback`` ribbon, whose DOM position belongs to the *next* card.
    Scripts/SVGs are stripped first. Rate isn't published on this page (it's in
    the EFL PDF), so ``buyback_ckwh`` stays None.
    """
    html = _strip_comments(html)
    html = _SCRIPT_RE.sub("", html)
    html = re.sub(r"<svg\b[^>]*>.*?</svg>", " ", html, flags=re.I | re.S)
    # Drop the category ribbons before splitting: they carry no plan we need and
    # would otherwise trail into a card's context and mislead the LLM review.
    html = _EFLVIEWER_RIBBON_RE.sub(" ", html)
    cards = [c for c in _EFLVIEWER_CARD_SPLIT_RE.split(html) if "plan-list-padding" in c[:80]]
    base = config.homepage

    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for card in cards:
        url_m = _EFLVIEWER_EFL_URL_RE.search(card)
        if not url_m:  # a section-header row ("Gexa Stable Plans") with no EFL
            continue
        efl_url = urljoin(base, unescape(url_m.group(1)))
        code_m = _EFLVIEWER_PRODCODE_RE.search(efl_url)
        product_code = code_m.group(1) if code_m else efl_url
        if product_code in seen:
            continue
        seen.add(product_code)
        name_m = _EFLVIEWER_NAME_RE.search(card)
        plan_name = _clean_plan_name(_strip_tags(name_m.group(1))) if name_m else product_code
        card_text = _strip_tags(unescape(card))
        is_buyback = bool(_EFLVIEWER_BUYBACK_RE.search(card_text) or _EFLVIEWER_EXPORT_RE.search(card_text))
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=plan_name,
                efl_url=efl_url,
                is_buyback=is_buyback,
                buyback_ckwh=None,
                extraction_method="static",
                context=f"[prodcode={product_code}] {card_text[:600]}",
            )
        )
    return plans


def extract_gexa(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static extractor for Gexa's rendered plans page -- the shared eflviewer
    platform (see :func:`_extract_eflviewer_platform`)."""
    return _extract_eflviewer_platform(html, config)


def extract_frontier(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static extractor for Frontier Utilities' rendered plans page.

    Frontier runs the same eflviewer enrollment platform as Gexa -- identical
    ``plan-list-padding`` cards, ``<h3>`` names, ``eflviewer.aspx?...prodcode``
    EFL links (hosted at eflviewer.frontierutilities.com), and the same
    ``Plan Type: Solar Buyback`` self-label -- so it delegates to
    :func:`_extract_eflviewer_platform`. Its plans page is reached directly via
    ``/Home/Index?Zip=<zip>`` (no ZIP-gate click flow); see
    :func:`_frontier_render`."""
    return _extract_eflviewer_platform(html, config)


# --------------------------------------------------------------------------- #
# Ambit Energy static extractor
# --------------------------------------------------------------------------- #
# Ambit runs the same Vistra shopping platform as TXU: plans are `show-plan`
# cards and EFLs are served by an identical `PDFGenerator?formType=
# EnergyFactsLabel&comProdId=<id>` endpoint. But Ambit's list page only reveals
# the EFL link after a per-card "See Plan Details" expansion, so a static
# capture has NO EFL URL in it -- instead each card carries the product id in a
# `data-productid` attribute, from which we CONSTRUCT the EFL URL. (Ambit's site
# also sits behind a WAF that blocks Playwright, so its capture is manual and
# its RepConfig has no render(); see project_ambit_discovery memory.)
_AMBIT_CARD_SPLIT_RE = re.compile(r'(?=<div\b[^>]*\bclass="[^"]*\bshow-plan\b)')
_AMBIT_PRODUCTID_RE = re.compile(r'data-productid="([^"]+)"', re.I)
_AMBIT_PLANNAME_RE = re.compile(r'data-planname="([^"]+)"', re.I)
# Buyback self-label: the plan name ("Texas Solar Buyback ...") or the card's
# visible export-credit description.
_AMBIT_BUYBACK_RE = re.compile(r"buyback|excess solar|get paid for your excess", re.I)
_AMBIT_EFL_BASE = "https://shopping.ambitenergy.com/PDFGenerator"
# Doug's TDU. The capture is Oncor-specific (ZIP 78665); PDFGenerator needs a
# tdsp, absent from the collapsed list DOM, so we supply it. Change for another
# TDU territory.
_AMBIT_TDSP = "ONCOR"


def _ambit_efl_url(product_id: str, efldate: str) -> str:
    """Construct Ambit's EFL URL for a product id (the collapsed list page omits
    it; expanding "See Plan Details" reveals this exact PDFGenerator link)."""
    return (
        f"{_AMBIT_EFL_BASE}?formType=EnergyFactsLabel&comProdId={product_id}"
        f"&efldate={efldate}&tdsp={_AMBIT_TDSP}&lang=en&custClass=Residential"
    )


def extract_ambit(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static (no-LLM) extractor for Ambit's rendered plans page.

    Each plan is a ``show-plan`` card carrying ``data-planname`` +
    ``data-productid``. Ambit reuses TXU's Vistra ``PDFGenerator`` EFL endpoint,
    but the list page only exposes the link after a "See Plan Details"
    expansion, so we CONSTRUCT the EFL URL from the product id
    (:func:`_ambit_efl_url`) rather than scraping it. Buyback plans self-label in
    the name ("Texas Solar Buyback") and the card's export-credit description.
    Rate isn't on the page (it's in the EFL), so ``buyback_ckwh`` stays None.
    """
    html = _SCRIPT_RE.sub("", _strip_comments(html))
    html = re.sub(r"<svg\b[^>]*>.*?</svg>", " ", html, flags=re.I | re.S)
    cards = [c for c in _AMBIT_CARD_SPLIT_RE.split(html) if "show-plan" in c[:120]]
    efldate = dt.date.today().isoformat()

    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for card in cards:
        pid_m = _AMBIT_PRODUCTID_RE.search(card)
        if not pid_m:
            continue
        product_id = pid_m.group(1)
        if product_id in seen:
            continue
        seen.add(product_id)
        name_m = _AMBIT_PLANNAME_RE.search(card)
        plan_name = _clean_plan_name(name_m.group(1)) if name_m else product_id
        card_text = _strip_tags(unescape(card))
        is_buyback = bool(
            _AMBIT_BUYBACK_RE.search(card_text) or re.search(r"buyback", plan_name, re.I)
        )
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=plan_name,
                efl_url=_ambit_efl_url(product_id, efldate),
                is_buyback=is_buyback,
                buyback_ckwh=None,
                extraction_method="static",
                context=f"[comProdId={product_id}] {card_text[:600]}",
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# Octopus Energy static extractor
# --------------------------------------------------------------------------- #
# Octopus self-labels: each plan card has an <h2 data-cy="product-title"> and an
# "Electricity Facts Label" link to octopusenergy.com/efl/<CODE>-<TDU>-<LZ>-<ts>/.
# Buyback isn't a separate plan -- the page states "Solar buyback is automatically
# included in all of our plans except OctopusFlex" -- so every plan but Flex is
# buyback. (Octopus requires an ESI ID to render the list when a ZIP spans load
# zones; that PII is read from a gitignored secrets file by _octopus_render, not
# stored here. A stray competitor EFL, e.g. a txu.com PDFGenerator link, is
# ignored: only octopusenergy.com/efl/ URLs are taken.)
_OCTOPUS_TITLE_RE = re.compile(r'data-cy="product-title"[^>]*>(.*?)</h2>', re.I | re.S)
_OCTOPUS_EFL_RE = re.compile(r'href="(https://octopusenergy\.com/efl/[^"]+)"', re.I)


def extract_octopus(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static (no-LLM) extractor for Octopus's rendered plans page.

    Each plan's EFL self-labels via an ``octopusenergy.com/efl/...`` link; the
    plan name is the nearest preceding ``<h2 data-cy="product-title">``. Buyback
    is bundled into every plan except OctopusFlex (per the page's own statement),
    so ``is_buyback`` is just "not Flex". Rate isn't on the page (it's in the
    EFL), so ``buyback_ckwh`` stays None.
    """
    html = _SCRIPT_RE.sub("", _strip_comments(html))
    titles = [
        (m.start(), _clean_plan_name(_strip_tags(m.group(1))))
        for m in _OCTOPUS_TITLE_RE.finditer(html)
    ]

    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for m in _OCTOPUS_EFL_RE.finditer(html):
        url = m.group(1)
        if url in seen:
            continue
        seen.add(url)
        prior = [name for off, name in titles if off < m.start()]
        plan_name = prior[-1] if prior else url
        # "...included in all of our plans except OctopusFlex."
        is_buyback = "flex" not in _normalize_name(plan_name)
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=plan_name,
                efl_url=url,
                is_buyback=is_buyback,
                buyback_ckwh=None,
                extraction_method="static",
                context="Electricity Facts Label",
                # octopusenergy.com/efl/<code> is a client-rendered HTML viewer,
                # not a PDF; the downloader renders + print-to-PDFs it.
                efl_is_html_viewer=True,
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# LLM fallback classifier (Ollama, lfm2.5, JSON-forced)
# --------------------------------------------------------------------------- #
_CLASSIFIER_SYSTEM = (
    "You classify hyperlinks found on a Texas retail-electricity provider's "
    "website. Definitions:\n"
    "- An 'Electricity Facts Label' (EFL) is the standardized one-page PDF "
    "disclosure (energy rate in cents/kWh, base charge, TDU delivery charges, "
    "contract term, early-termination fee) a Texas retailer must publish for "
    "each plan. Links to it are usually a PDF and labeled 'Electricity Facts "
    "Label', 'EFL', or 'Facts Label'; sometimes just 'Download' or a bare "
    "'<plan>-efl.pdf' filename.\n"
    "- A 'solar buyback' plan credits the customer for surplus solar energy "
    "exported to the grid. Signals: 'buyback', 'solar', 'net metering', "
    "'export credit', 'renewable rewards', 'surplus'.\n"
    "Use ONLY the provided link text and surrounding page context -- do not "
    "guess from the URL alone. Reply with a JSON object with keys: "
    '"is_efl" (bool), "is_buyback" (bool), "confidence" (0..1 float), '
    '"thinking" (short string explaining the decision).\n'
    "Examples:\n"
    'link text "Electricity Facts Label", context "Solar Buyback 12 ... '
    'Important Documents" -> {"is_efl": true, "is_buyback": true, '
    '"confidence": 0.97, "thinking": "explicit EFL link inside a solar '
    'buyback plan block"}\n'
    'link text "Terms of Service", context "Important Documents" -> '
    '{"is_efl": false, "is_buyback": false, "confidence": 0.95, "thinking": '
    '"ToS, not the EFL"}\n'
    'link text "Download", context "Fixed 12 plan details ... 14.2 cents/kWh '
    'facts label" -> {"is_efl": true, "is_buyback": false, "confidence": 0.6, '
    '"thinking": "ambiguous label but context is the facts-label download; no '
    'buyback signal"}'
)


def classify_link_llm(
    link_text: str,
    context: str,
    url: str = "",
    model: str = OLLAMA_MODEL,
    ollama_url: str = OLLAMA_URL,
    timeout: float = 60.0,
    chat_fn: Optional[Callable[[list[dict], str, str, float], dict]] = None,
) -> dict:
    """Classify a link as an EFL / solar-buyback EFL using a local Ollama model.

    Returns a dict ``{is_efl, is_buyback, confidence, thinking}``. Feeds the
    model real ``link_text`` + surrounding ``context`` (never a bare URL) and
    forces JSON output. ``chat_fn`` is an injection seam for tests
    (``(messages, model, ollama_url, timeout) -> response_dict``); by default a
    thin ``httpx`` POST to Ollama's ``/api/chat`` with ``format="json"``.

    Raises RuntimeError with a clear message if the Ollama call fails (server
    not running / model not pulled) -- callers should treat the LLM tier as
    best-effort and fall back to skipping the ambiguous link.
    """
    user = (
        f"Link text: {link_text!r}\n"
        f"URL: {url!r}\n"
        f"Surrounding page context:\n{context.strip()[:2000]}"
    )
    messages = [
        {"role": "system", "content": _CLASSIFIER_SYSTEM},
        {"role": "user", "content": user},
    ]

    if chat_fn is None:
        chat_fn = _ollama_chat
    try:
        resp = chat_fn(messages, model, ollama_url, timeout)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Ollama classification call failed ({ollama_url}, model={model!r}): {exc!r}. "
            "Ensure `ollama serve` is running and the model is pulled "
            f"(`ollama pull {model}`). The LLM fallback is best-effort; static "
            "extraction still works without it."
        ) from exc

    content = resp.get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            f"Ollama returned non-JSON content despite format=json: {content!r}"
        ) from exc

    return {
        "is_efl": bool(parsed.get("is_efl", False)),
        "is_buyback": bool(parsed.get("is_buyback", False)),
        "confidence": float(parsed.get("confidence", 0.0) or 0.0),
        "thinking": str(parsed.get("thinking", "")),
    }


def _ollama_chat(messages: list[dict], model: str, ollama_url: str, timeout: float) -> dict:
    import httpx

    payload = {"model": model, "messages": messages, "stream": False, "format": "json"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(ollama_url, json=payload)
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------- #
# Two-tier discovery orchestration
# --------------------------------------------------------------------------- #
_GENERIC_EFL_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref="([^"]+\.pdf[^"]*)"[^>]*>(.*?)</a>', re.I | re.S
)


def discover(
    html: str,
    config: RepConfig,
    use_llm_fallback: bool = True,
    llm_review: bool = False,
    llm_min_confidence: float = 0.6,
    review_min_confidence: float = 0.7,
    **llm_kwargs: object,
) -> list[DiscoveredPlan]:
    """Run the deterministic extraction for one REP's rendered HTML, with two
    optional LLM tiers.

    Tier 1 is ``config.extractor`` (deterministic self-label parse).

    If it finds nothing and ``use_llm_fallback`` is set, a fallback walks every
    ``.pdf`` anchor and asks the LLM classifier whether it is an EFL (keeping
    those at or above ``llm_min_confidence``).

    If ``llm_review`` is set, EVERY plan the static extractor returned is then
    re-checked by the LLM -- so a wording change on the site can't silently
    lose a solar-buyback plan to the static buyback heuristic. Review is
    **upgrade-only**: the LLM may promote a plan to ``is_buyback=True`` (at or
    above ``review_min_confidence``) but never removes a discovered EFL, so we
    err toward keeping buyback EFLs rather than dropping them. Each reviewed
    plan keeps the model's confidence + reasoning for audit.

    All LLM tiers are best-effort: a failure (Ollama down) is swallowed and the
    deterministic result stands.
    """
    if config.extractor is None:
        raise ValueError(
            f"RepConfig {config.key!r} has no static extractor (its EFL URLs "
            "aren't in the DOM); use harvest_live() to drive its interactive flow."
        )
    plans = list(config.extractor(html, config))

    if not plans and use_llm_fallback:
        stripped = _strip_comments(html)
        for m in _GENERIC_EFL_ANCHOR_RE.finditer(stripped):
            href = urljoin(config.homepage, m.group(1).strip())
            link_text = _strip_tags(m.group(2))
            context = _strip_tags(stripped[max(0, m.start() - 600) : m.end() + 200])
            try:
                verdict = classify_link_llm(link_text, context, url=href, **llm_kwargs)  # type: ignore[arg-type]
            except RuntimeError:
                break  # Ollama unavailable -> abandon the LLM tier entirely
            if verdict["is_efl"] and verdict["confidence"] >= llm_min_confidence:
                plans.append(
                    DiscoveredPlan(
                        retailer=config.retailer,
                        plan_name=link_text or "(unknown)",
                        efl_url=href,
                        is_buyback=verdict["is_buyback"] or None,
                        extraction_method="llm",
                        llm_confidence=verdict["confidence"],
                        context=f"{link_text} :: {verdict['thinking']}",
                    )
                )

    if llm_review and plans:
        _llm_review_plans(plans, review_min_confidence, **llm_kwargs)

    return plans


def _llm_review_plans(
    plans: list[DiscoveredPlan], review_min_confidence: float, **llm_kwargs: object
) -> None:
    """Upgrade-only LLM review of already-discovered plans (see `discover`).
    Mutates each plan in place; stops silently if Ollama is unavailable."""
    for p in plans:
        try:
            verdict = classify_link_llm(p.plan_name, p.context, url=p.efl_url, **llm_kwargs)  # type: ignore[arg-type]
        except RuntimeError:
            break  # Ollama down -> keep the deterministic verdicts we have
        p.llm_confidence = verdict["confidence"]
        if (
            verdict["is_buyback"]
            and verdict["confidence"] >= review_min_confidence
            and not p.is_buyback
        ):
            p.is_buyback = True
            p.context += f" :: LLM review upgraded to buyback ({verdict['thinking']})"


# --------------------------------------------------------------------------- #
# Live browser fetch (Playwright) -- optional, lazily imported
# --------------------------------------------------------------------------- #
def _respect_rate_limit(host: str) -> None:
    now = time.monotonic()
    last = _last_request_at.get(host)
    if last is not None:
        wait = _MIN_REQUEST_INTERVAL_S - (now - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[host] = time.monotonic()


def robots_allows(url: str, user_agent: str = _USER_AGENT, timeout: float = 10.0) -> bool:
    """Best-effort robots.txt check for ``url``. Returns True (allow) if the
    robots file can't be fetched/parsed -- we don't block discovery on a
    missing or malformed robots.txt, but we do honor an explicit Disallow."""
    import urllib.robotparser

    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        import httpx

        with httpx.Client(timeout=timeout, headers={"User-Agent": user_agent}) as client:
            resp = client.get(robots_url, follow_redirects=True)
        if resp.status_code >= 400:
            return True
        rp.parse(resp.text.splitlines())
    except Exception:  # noqa: BLE001
        return True
    return rp.can_fetch(user_agent, url)


def fetch_rendered_html(
    config: RepConfig,
    zip_code: str,
    headless: bool = True,
    timeout_ms: int = 60000,
    settle_ms: int = 8000,
    snapshot_dir: Path = Path("data/rep_discovery"),
    check_robots: bool = True,
) -> tuple[str, Path]:
    """Drive a real browser through ``config.render`` and return the rendered
    HTML plus the path of a saved timestamped snapshot.

    REP sites are client-rendered SPAs -- a raw HTTP fetch returns an empty
    shell -- so this uses Playwright (optional dep; see module docstring). Per
    the lessons in rep_discovery_handoff.md the render flow should use
    ``wait_until="domcontentloaded"`` and wait for a *specific* selector rather
    than network-idle, and let hero animations settle before scraping.

    Raises RuntimeError (never a bare ImportError) if Playwright isn't
    installed, pointing at the install command and the manual fallback of
    saving the page's HTML by hand into ``snapshot_dir``.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for live REP-site discovery but is not installed. "
            "Install it with:  pip install 'energyanalyzer[discovery]' && playwright install chromium\n"
            "Alternatively, open the REP's plans page in a browser, save the fully-rendered "
            f"HTML, and drop it in {snapshot_dir}/ -- then call discover() on the saved file."
        ) from exc

    if config.render is None:
        raise ValueError(f"RepConfig {config.key!r} has no render() flow defined")

    host = urlparse(config.homepage).netloc
    if check_robots and not robots_allows(config.homepage):
        raise RuntimeError(
            f"robots.txt at {host} disallows automated fetching of {config.homepage}. "
            "Aborting out of politeness; fetch and save the HTML manually instead."
        )
    _respect_rate_limit(host)

    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        logger.info("%s: launching browser (headless=%s)", config.retailer, headless)
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=_USER_AGENT)
        page = context.new_page()
        try:
            logger.info("%s: opening %s", config.retailer, config.homepage)
            page.goto(config.homepage, wait_until="domcontentloaded", timeout=timeout_ms)
            logger.info("%s: running render flow (entering ZIP, waiting for plans)", config.retailer)
            rendered = config.render(page, zip_code)
            if isinstance(rendered, str):
                # Paginated render collected + concatenated the pages itself
                # (and did its own per-page settling); use it verbatim.
                html = rendered
            else:
                if settle_ms:
                    page.wait_for_timeout(settle_ms)
                html = page.content()
            logger.info("%s: captured %d chars of rendered HTML", config.retailer, len(html))
        finally:
            context.close()
            browser.close()

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = snapshot_dir / f"{config.key}_{ts}.html"
    snapshot_path.write_text(html, encoding="utf-8")
    return html, snapshot_path


def harvest_live(
    config: RepConfig,
    zip_code: str,
    headless: bool = True,
    timeout_ms: int = 60000,
    check_robots: bool = True,
    llm_review: bool = False,
    review_min_confidence: float = 0.7,
    **llm_kwargs: object,
) -> list[DiscoveredPlan]:
    """Drive a REP's interactive ``harvester`` flow and return its plans.

    The counterpart to :func:`fetch_rendered_html` + :func:`discover` for REPs
    whose EFL URLs aren't in the DOM (see ``RepConfig.harvester``). Champion's
    EFL, for instance, is a JS ``<button>`` that opens the PDF in a popup; the
    harvester opens each plan's details, clicks the button, and reads the popup
    URL. Same politeness (robots.txt, rate limit) and optional upgrade-only
    ``llm_review`` as the static path. Raises RuntimeError if Playwright is
    missing, ValueError if the config has no harvester.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for live REP-site discovery but is not installed. "
            "Install it with:  pip install 'energyanalyzer[discovery]' && playwright install chromium"
        ) from exc

    if config.harvester is None:
        raise ValueError(f"RepConfig {config.key!r} has no harvester() flow defined")

    host = urlparse(config.homepage).netloc
    if check_robots and not robots_allows(config.homepage):
        raise RuntimeError(
            f"robots.txt at {host} disallows automated fetching of {config.homepage}. "
            "Aborting out of politeness."
        )
    _respect_rate_limit(host)

    with sync_playwright() as pw:
        logger.info("%s: launching browser (headless=%s)", config.retailer, headless)
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=_USER_AGENT)
        page = context.new_page()
        try:
            logger.info("%s: opening %s", config.retailer, config.homepage)
            page.goto(config.homepage, wait_until="domcontentloaded", timeout=timeout_ms)
            logger.info("%s: running interactive harvester (this can take a minute)", config.retailer)
            plans = list(config.harvester(page, zip_code, config))
            logger.info("%s: harvester returned %d plan(s)", config.retailer, len(plans))
        finally:
            context.close()
            browser.close()

    if llm_review and plans:
        _llm_review_plans(plans, review_min_confidence, **llm_kwargs)
    return plans


# --------------------------------------------------------------------------- #
# Download + manifest
# --------------------------------------------------------------------------- #
def _render_efl_pdf(
    url: str,
    headless: bool = True,
    timeout_ms: int = 60000,
    settle_ms: int = 6000,
) -> bytes:
    """Render an HTML EFL *viewer* URL in a headless browser and return a
    print-to-PDF of the page.

    For REPs (e.g. Octopus) whose EFL link is a client-rendered HTML page rather
    than a direct PDF -- a plain GET would only fetch the SPA shell. Chromium's
    print-to-PDF preserves the full EFL text (rate tables, disclosures) so the
    normal parser can read it. Raises RuntimeError if Playwright isn't installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required to render HTML EFL viewers (e.g. Octopus) but is "
            "not installed. Install it with:  pip install 'energyanalyzer[discovery]' "
            "&& playwright install chromium"
        ) from exc

    _respect_rate_limit(urlparse(url).netloc)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=_USER_AGENT)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if settle_ms:
                page.wait_for_timeout(settle_ms)
            return page.pdf(format="Letter", print_background=True)
        finally:
            context.close()
            browser.close()


def _sanitize_filename_part(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip())
    return re.sub(r"_+", "_", cleaned).strip("_")


def _efl_filename(plan: DiscoveredPlan) -> str:
    retailer = _sanitize_filename_part(plan.retailer)
    name = _sanitize_filename_part(plan.plan_name)
    base = "_".join(p for p in (retailer, name) if p) or "efl"
    return f"{base}.pdf"[:150]


def download_discovered(
    plans: list[DiscoveredPlan],
    dest: Path = Path("data/efl"),
    manifest_path: Optional[Path] = None,
    buyback_only: bool = True,
    timeout: float = 30.0,
    headless: bool = True,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Download discovered plans' EFL PDFs into ``dest`` and append a manifest
    entry per download so downstream dedup/refresh can tell REP-discovered
    plans from PTC/meterplan ones.

    ``buyback_only`` (default True) restricts downloads to plans whose
    ``is_buyback`` is True -- the whole point of this fetcher is the buyback
    plans PTC/meterplan miss. Existing files are skipped. Individual failures
    are tolerated and collected. The manifest (default
    ``dest/rep_discovery_manifest.jsonl``) gets one JSON line per download:
    ``{retailer, plan_name, source_url, discovered_at, extraction_method,
    llm_confidence, is_buyback, buyback_ckwh, file}``.

    Returns ``{"downloaded": [...], "skipped": [...], "failed": [...],
    "filtered_out": int}``.
    """
    import httpx

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if manifest_path is None:
        manifest_path = dest / "rep_discovery_manifest.jsonl"
    manifest_path = Path(manifest_path)

    targets = [p for p in plans if (p.is_buyback if buyback_only else True)]
    summary: dict = {
        "downloaded": [],
        "skipped": [],
        "failed": [],
        "deferred": [],
        "filtered_out": len(plans) - len(targets),
    }
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*"}
    total = len(targets)
    discovered_at = dt.datetime.now(dt.timezone.utc).isoformat()

    def _manifest_entry(plan: DiscoveredPlan, file_path: Path) -> dict:
        return {
            "retailer": plan.retailer,
            "plan_name": plan.plan_name,
            "source_url": plan.efl_url,
            "discovered_at": discovered_at,
            "extraction_method": plan.extraction_method,
            "llm_confidence": plan.llm_confidence,
            "is_buyback": plan.is_buyback,
            "buyback_ckwh": plan.buyback_ckwh,
            "file": str(file_path),
        }

    from energyanalyzer.fetchers.ptc import _efl_ssl_context

    logger.info("Downloading %d discovered EFL(s)", total)
    entries: list[dict] = []
    with httpx.Client(
        timeout=timeout, headers=headers, follow_redirects=True, verify=_efl_ssl_context()
    ) as client:
        for i, plan in enumerate(targets, start=1):
            logger.info(
                "Download %d/%d: %s (%s)", i, total, plan.plan_name, plan.retailer
            )
            host = urlparse(plan.efl_url).netloc
            file_path = dest / _efl_filename(plan)
            if file_path.exists():
                summary["skipped"].append(str(file_path))
                entries.append(_manifest_entry(plan, file_path))
                if progress_callback:
                    progress_callback(i, total, plan.plan_name)
                continue
            try:
                if plan.efl_is_html_viewer:
                    # An HTML EFL viewer (e.g. Octopus): render + print-to-PDF in
                    # a browser rather than a plain GET (which gets only the SPA
                    # shell). _render_efl_pdf handles its own rate limiting.
                    content = _render_efl_pdf(plan.efl_url, headless=headless)
                else:
                    _respect_rate_limit(host)
                    resp = client.get(plan.efl_url)
                    resp.raise_for_status()
                    content = resp.content
                # A direct-GET EFL URL can still resolve to an HTML viewer/SPA
                # shell or error page; saving that as a .pdf would only fail the
                # parser later, so reject anything without the "%PDF" signature.
                if b"%PDF" not in content[:1024]:
                    ctype = resp.headers.get("content-type", "?")
                    # An HTML body is a browser-rendered EFL viewer/SPA shell (the
                    # Vistra shopping.* PDFGenerator endpoint TXU/Ambit use returns
                    # the app shell to httpx) -- defer, don't count as a failure.
                    if "html" in ctype.lower():
                        summary["deferred"].append(
                            {
                                "url": plan.efl_url,
                                "reason": f"HTML response (content-type {ctype!r}) -- a "
                                "browser-rendered EFL viewer/SPA, not an httpx-downloadable PDF",
                            }
                        )
                    else:
                        summary["failed"].append(
                            {
                                "url": plan.efl_url,
                                "error": f"response was not a PDF (content-type {ctype!r}, "
                                f"{len(content)} bytes) -- likely a stale link or error page",
                            }
                        )
                    if progress_callback:
                        progress_callback(i, total, plan.plan_name)
                    continue
                file_path.write_bytes(content)
                summary["downloaded"].append(str(file_path))
                entries.append(_manifest_entry(plan, file_path))
            except Exception as exc:  # noqa: BLE001
                summary["failed"].append({"url": plan.efl_url, "error": repr(exc)})
            if progress_callback:
                progress_callback(i, total, plan.plan_name)

    if entries:
        with open(manifest_path, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    return summary


# --------------------------------------------------------------------------- #
# Per-REP registry
# --------------------------------------------------------------------------- #
def _green_mountain_render(page: object, zip_code: str) -> None:
    """Green Mountain nav flow, adapted from a `playwright codegen` recording
    (see rep_discovery_handoff.md). Enters the ZIP gate, dismisses a cookie
    banner if present, and lets the hero carousel settle. The caller
    (`fetch_rendered_html`) handles goto() and the post-render settle wait."""
    gate = page.get_by_title("Sustainable electricity for a")  # type: ignore[attr-defined]
    gate.get_by_placeholder("Enter ZIP code").click()
    gate.get_by_placeholder("Enter ZIP code").fill(zip_code)
    gate.get_by_role("button").click()
    try:
        page.get_by_role("button", name=re.compile("accept", re.I)).click(timeout=3000)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


GREEN_MOUNTAIN = RepConfig(
    key="green_mountain",
    retailer="Green Mountain Energy",
    homepage="https://www.greenmountainenergy.com/",
    extractor=extract_green_mountain,
    render=_green_mountain_render,
)


def _txu_render(page: object, zip_code: str) -> None:
    """TXU nav flow, adapted from a `playwright codegen` recording. Dismisses
    the privacy banner and the two "No"-style interstitials (all best-effort --
    they don't always appear), selects the residential/"already live here"
    path, then enters the ZIP gate. `fetch_rendered_html` handles goto() and
    the post-render settle wait."""

    def _try(action) -> None:
        try:
            action()
        except Exception:  # noqa: BLE001
            pass

    _try(lambda: page.locator(".privacy-warning > .close").click(timeout=4000))  # type: ignore[attr-defined]
    page.get_by_role("button", name="Shop Plans").click()  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("button", name="No", exact=True).click(timeout=4000))  # type: ignore[attr-defined]
    page.get_by_role("img", name="House icon").click()  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("button", name="No, I already live here").click(timeout=4000))  # type: ignore[attr-defined]
    page.locator("#p2p-house-zipcode").click()  # type: ignore[attr-defined]
    page.locator("#p2p-house-zipcode").fill(zip_code)  # type: ignore[attr-defined]
    page.get_by_role("button", name="See Plans").click()  # type: ignore[attr-defined]


TXU = RepConfig(
    key="txu",
    retailer="TXU Energy",
    homepage="https://www.txu.com/",
    extractor=extract_txu,
    render=_txu_render,
    # EFLs are the Vistra shopping.txu.com/PDFGenerator endpoint, which returns
    # an HTML SPA shell to httpx -- keep only buyback plans (the rest are on PTC).
    broaden=False,
)


def _chariot_render(page: object, zip_code: str) -> str:
    """Chariot nav flow, adapted from a `playwright codegen` recording. Chariot's
    solar-buyback plans (Shine/PowerBank/GreenVolt) are gated behind a "My home
    has solar panels" path -- the general shop-rates listing has none -- so this
    flow selects Residential, the solar-owner option, then the ZIP gate.

    The listing is paginated, so this render captures every page's HTML itself
    and returns the concatenation (extract_chariot dedups by product id); the
    "Go to the next page" link is followed until it disappears. Steps that don't
    always appear (cookie modal, the solar interstitial + its "I Understand"
    confirm when solar is already selected) are best-effort."""

    def _try(action) -> None:
        try:
            action()
        except Exception:  # noqa: BLE001
            pass

    _try(lambda: page.get_by_role("button", name="Close").click(timeout=5000))  # type: ignore[attr-defined]
    page.get_by_role("link", name="Residential").click()  # type: ignore[attr-defined]
    _try(lambda: page.get_by_text("My home has solar panels.").click(timeout=5000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("button", name="I Understand").click(timeout=5000))  # type: ignore[attr-defined]
    # The ZIP widget id (#zip-form-widget-<hash>) is auto-generated per render,
    # so match it by id prefix rather than the exact hash. The page now renders
    # more than one (desktop + mobile copies), so scope to the first visible one
    # -- a bare prefix match hits both and trips Playwright's strict mode.
    zipbox = page.locator('[id^="zip-form-widget-"]:visible').first  # type: ignore[attr-defined]
    zipbox.get_by_role("textbox", name="Enter ZIP Code").fill(zip_code)
    zipbox.get_by_role("button", name="Shop Rates").click()
    page.get_by_role("link", name="ALL Products").click()  # type: ignore[attr-defined]

    parts: list[str] = []
    for _ in range(12):  # safety cap; real listing is ~2 pages
        page.wait_for_load_state("networkidle")  # type: ignore[attr-defined]
        parts.append(page.content())  # type: ignore[attr-defined]
        nxt = page.get_by_role("link", name="Go to the next page")  # type: ignore[attr-defined]
        if nxt.count() == 0:
            break
        try:
            nxt.first.click(timeout=4000)
            page.wait_for_timeout(1500)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            break  # link present but not clickable (last page) -> done
    return "\n".join(parts)


CHARIOT = RepConfig(
    key="chariot",
    retailer="Chariot Energy",
    homepage="https://chariotenergy.com/",
    extractor=extract_chariot,
    render=_chariot_render,
)


def _gexa_render(page: object, zip_code: str) -> None:
    """Gexa nav flow, adapted from a `playwright codegen` recording. Enters the
    ZIP gate, opens the plans page, then selects the "Solar Buyback & EV"
    category (best-effort -- it's a client-side filter that leaves every plan in
    the DOM, so extract_gexa still sees them all if the tab label shifts).
    `fetch_rendered_html` handles goto() and the post-render settle wait -- Gexa
    holds a connection open, so rely on that fixed settle, not network-idle."""
    page.get_by_role("textbox", name="Enter Your Zip Code").fill(zip_code)  # type: ignore[attr-defined]
    page.get_by_role("link", name="Shop Plans").click()  # type: ignore[attr-defined]
    try:
        page.get_by_role("link", name="Solar Buyback & EV").click(timeout=6000)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


GEXA = RepConfig(
    key="gexa",
    retailer="Gexa Energy",
    homepage="https://www.gexaenergy.com/",
    extractor=extract_gexa,
    render=_gexa_render,
)


# Frontier's enrollment site takes the ZIP directly in the URL
# (/Home/Index?Zip=<zip>) -- no click-through ZIP gate -- but the plan cards are
# still injected by JS after load, so a plain HTTP GET returns only the shell;
# render() navigates to the ZIP URL and waits for the eflviewer EFL links to
# appear before the caller captures page.content().
_FRONTIER_PLANS_URL = "https://newenroll.frontierutilities.com/Home/Index?Zip={zip}"


def _frontier_render(page: object, zip_code: str) -> None:
    page.goto(  # type: ignore[attr-defined]
        _FRONTIER_PLANS_URL.format(zip=zip_code),
        wait_until="domcontentloaded",
        timeout=60000,
    )
    try:
        page.wait_for_selector(  # type: ignore[attr-defined]
            "a.efl[href*='eflviewer.aspx']", timeout=30000
        )
    except Exception:  # noqa: BLE001 -- fall back to the caller's fixed settle wait
        pass


FRONTIER = RepConfig(
    key="frontier",
    retailer="Frontier Utilities",
    homepage="https://newenroll.frontierutilities.com/",
    extractor=extract_frontier,
    render=_frontier_render,
    # newenroll.frontierutilities.com robots.txt is a blanket `Disallow: /`
    # ("Stop indexing of all content") aimed at search crawlers; per the user's
    # decision, a single rate-limited render of their own shopping page is
    # treated as outside that intent, so live discovery skips the robots check
    # for this REP only.
    check_robots=False,
)

# Ambit has no render(): its WAF blocks Playwright after ZIP entry, so its HTML
# is captured manually (Doug's own browser) and discover() runs on the saved
# file. A stealth render() (real-Chrome persistent profile) is a possible
# follow-up. See project_ambit_discovery memory.
AMBIT = RepConfig(
    key="ambit",
    retailer="Ambit Energy",
    homepage="https://www.ambitenergy.com/",
    extractor=extract_ambit,
    render=None,
    # Same Vistra shopping.ambitenergy.com/PDFGenerator endpoint as TXU (HTML to
    # httpx); keep only buyback plans -- the rest are on PTC.
    broaden=False,
)


def _octopus_render(page: object, zip_code: str) -> None:
    """Octopus nav flow. Octopus rejects a bare ZIP that spans load zones and
    requires an ESI ID (PII) -- read from the gitignored secrets file, never
    hardcoded. Enters ZIP -> Explore plans -> the address/ESI-ID panel -> ESI ID
    -> confirm the matched address. `fetch_rendered_html` handles goto + settle.

    NOTE: the solar/EV/thermostat qualification checkboxes from the codegen are
    intentionally omitted -- they use obfuscated styled-component classes that
    drift across deploys, and they don't gate which plans (or EFLs) appear
    (buyback is bundled in all plans but Flex regardless). Not yet validated
    against the live site; the "please enter your address" / "Alternatively"
    link+button names may need tuning."""
    secret = _load_rep_secret("octopus")
    esiid = secret.get("esiid")
    if not esiid:
        raise RuntimeError(
            "Octopus requires an ESI ID to render plans (ZIP spans load zones). "
            "Add octopus.esiid to data/rep_discovery_secrets.yaml (gitignored)."
        )
    address_button = secret.get("address_button")

    def _try(action) -> bool:
        try:
            action()
            return True
        except Exception:  # noqa: BLE001
            return False

    page.get_by_role("textbox", name="Zip code").fill(zip_code)  # type: ignore[attr-defined]
    page.get_by_role("button", name="Explore our plans").click()  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("link", name="please enter your address or").click(timeout=8000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("button", name="Alternatively, if you know").click(timeout=8000))  # type: ignore[attr-defined]
    page.get_by_role("textbox", name="Enter your ESI ID Number").fill(esiid)  # type: ignore[attr-defined]
    page.get_by_role("button", name="Get a quote").click()  # type: ignore[attr-defined]
    if address_button:
        _try(lambda: page.get_by_role("button", name=address_button).click(timeout=10000))  # type: ignore[attr-defined]


OCTOPUS = RepConfig(
    key="octopus",
    retailer="Octopus Energy",
    homepage="https://octopusenergy.com/",
    extractor=extract_octopus,
    render=_octopus_render,
)


# --------------------------------------------------------------------------- #
# Champion Energy interactive harvester (EFL URL is JS-computed, not in the DOM)
# --------------------------------------------------------------------------- #
# Champion's "Electricity Facts Label" is a <button> whose click handler opens
# the plan-specific EFL PDF in a popup (docs.championenergyservices.com/
# ExternalDocs?planName=PN####). The URL is nowhere in the DOM -- not even after
# expanding a plan's "See More Plan Details" modal -- so there's no static
# extractor; instead the harvester drives the browser and reads the popup URL.
# Every Champion plan bundles "Indexed Solar Buyback", so all are flagged buyback.
_CHAMPION_PLANNAME_PARAM_RE = re.compile(r"planName=([^&]+)", re.I)
_CHAMPION_MODAL_TITLE_RE = re.compile(r"Details of\s+(.+)", re.I)


def _champion_plan_name(page: object) -> str:
    """Read the open details modal's "Details of <plan>" heading."""
    try:
        txt = page.get_by_text(_CHAMPION_MODAL_TITLE_RE).first.inner_text()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return ""
    m = _CHAMPION_MODAL_TITLE_RE.search(txt or "")
    return _clean_plan_name(m.group(1)) if m else ""


def _champion_harvest(page: object, zip_code: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Interactive harvester for Champion (see the section comment). For each
    plan card: open its "See More Plan Details" modal, click "Electricity Facts
    Label", capture the popup's URL (the EFL), then close the popup and modal.
    `harvest_live` handles goto() and browser lifecycle."""

    def _try(action) -> bool:
        try:
            action()
            return True
        except Exception:  # noqa: BLE001
            return False

    logger.info("Champion Energy: entering ZIP %s and loading plans", zip_code)
    _try(lambda: page.get_by_text("Enter Your Address or Zip Code").first.click())  # type: ignore[attr-defined]
    # The ZIP input id (_r_g_) is a React-generated id that changes per render;
    # target the focused textbox instead.
    _try(lambda: page.get_by_role("textbox").first.fill(zip_code))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("button", name="View Rates and Plans").first.click())  # type: ignore[attr-defined]
    # Session-dependent interstitials -- best-effort.
    _try(lambda: page.get_by_role("button", name="New Service").click(timeout=6000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("button", name="Close this dialog").click(timeout=6000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(3000))  # type: ignore[attr-defined]

    details = page.get_by_role("button", name="See More Plan Details")  # type: ignore[attr-defined]
    count = details.count()
    logger.info("Champion Energy: found %d plan card(s); harvesting EFLs one by one", count)
    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for i in range(count):
        logger.info("Champion Energy: plan %d/%d -- opening details", i + 1, count)
        if not _try(lambda i=i: details.nth(i).click(timeout=6000)):
            logger.info("Champion Energy: plan %d/%d -- could not open details, skipping", i + 1, count)
            continue
        _try(lambda: page.wait_for_timeout(500))  # type: ignore[attr-defined]
        plan_name = _champion_plan_name(page)
        efl_url: Optional[str] = None
        try:
            logger.info(
                "Champion Energy: plan %d/%d (%s) -- clicking EFL, waiting for popup",
                i + 1,
                count,
                plan_name or "?",
            )
            with page.expect_popup() as pop:  # type: ignore[attr-defined]
                page.get_by_role("button", name="Electricity Facts Label").click()  # type: ignore[attr-defined]
            popup = pop.value
            _try(lambda: popup.wait_for_load_state())
            efl_url = popup.url
            _try(lambda: popup.close())
            logger.info("Champion Energy: plan %d/%d -- captured EFL URL", i + 1, count)
        except Exception:  # noqa: BLE001
            logger.info("Champion Energy: plan %d/%d -- no EFL popup captured", i + 1, count)
            efl_url = None
        # Close the one-at-a-time plan-details modal before the next plan. Its
        # close button is named "Close" (per the codegen recording) -- distinct
        # from the "Close this dialog" interstitial dismissed in the nav prelude,
        # which is already gone by now, so a plain (substring) name match is safe.
        _try(lambda: page.get_by_role("button", name="Close").click(timeout=6000))  # type: ignore[attr-defined]
        if not efl_url:
            continue
        code_m = _CHAMPION_PLANNAME_PARAM_RE.search(efl_url)
        key = code_m.group(1) if code_m else efl_url
        if key in seen:
            continue
        seen.add(key)
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=plan_name or key,
                efl_url=efl_url,
                is_buyback=True,  # all Champion plans bundle Indexed Solar Buyback
                buyback_ckwh=None,  # indexed (variable), no fixed rate on the EFL page
                extraction_method="harvest",
                context=f"[{key}] Champion plan; all plans include Indexed Solar Buyback",
            )
        )
    return plans


CHAMPION = RepConfig(
    key="champion",
    retailer="Champion Energy",
    homepage="https://championenergyservices.com/",
    harvester=_champion_harvest,
)


# --------------------------------------------------------------------------- #
# Direct Energy interactive harvester (EFL blob is fetched from a backend URL)
# --------------------------------------------------------------------------- #
# Direct Energy's shop (shop.directenergy.com) is a React SPA. The plan list is
# reached by navigating to the ZIP URL (note: ?zipCode=, capital C) then clicking
# a residential/"not moving" prelude. Each plan's "Electricity Facts Label" link
# opens a client-generated blob: PDF (popup.url is a useless per-session blob),
# but the browser first fetches the PDF from a STABLE backend endpoint --
# api-oam.directenergy.com/api/docs/files/<id>.pdf (plain application/pdf,
# httpx-downloadable) -- which we capture from the network response and store as
# the efl_url. Direct Energy's *solar* plans ("Direct Solar Unlimited ...") are
# the buyback plans PTC/meterplan miss; the rest are standard PTC plans, so this
# harvester targets the solar ones (widen the name filter to take them all).
_DE_PLANS_URL = "https://shop.directenergy.com/tx/plan-selection?zipCode={zip}"
_DE_EFL_API_RE = re.compile(r"api-oam\.directenergy\.com/api/docs/files/", re.I)


def _direct_energy_harvest(
    page: object, zip_code: str, config: RepConfig
) -> list[DiscoveredPlan]:
    """Interactive harvester for Direct Energy (see the section comment).
    ``harvest_live`` handles the initial goto()/browser lifecycle; this
    re-navigates to the ZIP-specific plans URL, clears the prelude, loads every
    plan, and for each solar plan captures its EFL's backend PDF URL."""

    def _try(action) -> bool:
        try:
            action()
            return True
        except Exception:  # noqa: BLE001
            return False

    _try(lambda: page.goto(_DE_PLANS_URL.format(zip=zip_code), wait_until="domcontentloaded", timeout=60000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(5000))  # type: ignore[attr-defined]
    # Residential / "not moving" prelude -- all best-effort (session-dependent).
    _try(lambda: page.get_by_role("button", name=" Home").click(timeout=8000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("radio", name="No").check(timeout=6000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("button", name="No").click(timeout=6000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_test_id("view-plans").click(timeout=10000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(4000))  # type: ignore[attr-defined]
    # The listing paginates behind a "Load 8 More" button; click until it's gone.
    for _ in range(10):
        btn = page.get_by_role("button", name="Load 8 More")  # type: ignore[attr-defined]
        if btn.count() == 0:
            break
        if not _try(lambda: btn.first.click(timeout=4000)):
            break
        _try(lambda: page.wait_for_timeout(1500))  # type: ignore[attr-defined]

    cards = page.locator(".plan__wrapper")  # type: ignore[attr-defined]
    count = cards.count()
    logger.info("Direct Energy: found %d plan card(s); harvesting solar EFLs", count)
    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for i in range(count):
        card = cards.nth(i)
        try:
            name = _clean_plan_name(card.locator(".rich-text-body h4").first.inner_text(timeout=3000))
        except Exception:  # noqa: BLE001
            continue
        # Discovery targets Direct Energy's solar (buyback) plans; the rest are
        # standard PTC plans. Widen this to take every plan if ever needed.
        if "solar" not in _normalize_name(name) or name in seen:
            continue
        seen.add(name)
        logger.info("Direct Energy: capturing EFL for %r", name)
        if not _try(lambda card=card: card.locator(".plan-doc").first.click(timeout=6000)):
            continue
        _try(lambda: page.wait_for_timeout(800))  # type: ignore[attr-defined]
        efl_url: Optional[str] = None
        try:
            # Scope the EFL link to THIS card -- a global .first would re-click
            # the first plan's link every iteration (they'd all share one URL).
            with page.expect_response(  # type: ignore[attr-defined]
                lambda r: bool(_DE_EFL_API_RE.search(r.url)) and r.url.lower().endswith(".pdf"),
                timeout=15000,
            ) as resp_info:
                card.get_by_role("link", name="promo Electricity Facts Label").first.click(timeout=8000)
            efl_url = resp_info.value.url
        except Exception:  # noqa: BLE001
            efl_url = None
        # Close any EFL popup tab + the plan-doc panel before the next plan.
        for extra in list(getattr(page, "context", None).pages if getattr(page, "context", None) else [])[1:]:
            _try(lambda extra=extra: extra.close())
        _try(lambda: page.keyboard.press("Escape"))  # type: ignore[attr-defined]
        if not efl_url:
            continue
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=name,
                efl_url=efl_url,
                is_buyback=True,  # DE solar plans; EFL parse confirms terms
                extraction_method="harvest",
                context="Direct Energy solar plan; EFL PDF via api-oam docs endpoint",
            )
        )
    return plans


DIRECT_ENERGY = RepConfig(
    key="direct_energy",
    retailer="Direct Energy",
    homepage="https://shop.directenergy.com/",
    harvester=_direct_energy_harvest,
)


# --------------------------------------------------------------------------- #
# Reliant Energy interactive harvester (sibling NRG shop; EFL from a backend URL)
# --------------------------------------------------------------------------- #
# Reliant (shop.reliant.com) is another NRG shop but a DIFFERENT SPA flow from
# Direct Energy: enter an address -> pick the first autocomplete result -> answer
# "moving? no" / "renting? no" -> "show plans" -> a "Solar Plans" filter narrows
# to the solar plans. Plan cards use build-hashed CSS-module classes
# (OfferPlanContained-module--plan-container--<hash>) -- fragile -- but the hash
# is only a suffix, so an xpath class *substring* match ("plan-container") is
# stable, and the nav/EFL controls have stable data-testids. Each plan's EFL
# opens from a backend PDF (myaccount.reliant.com/files/<id>.pdf, plain
# application/pdf) captured from the network response. Targets Reliant's solar
# plans ("Reliant Solar Payback Match ...").
_RELIANT_SEARCH_URL = "https://shop.reliant.com/search-for-plans/"
_RELIANT_EFL_RESP_RE = re.compile(r"reliant\.com/[^\"']*?files/[^\"'?]+\.pdf", re.I)


def _reliant_harvest(page: object, zip_code: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Interactive harvester for Reliant (see the section comment). ``harvest_live``
    handles the initial goto()/lifecycle; this runs the address/segmentation
    prelude, applies the Solar Plans filter, and captures each solar plan's
    backend EFL PDF URL from the network."""

    def _try(action) -> bool:
        try:
            action()
            return True
        except Exception:  # noqa: BLE001
            return False

    _try(lambda: page.goto(_RELIANT_SEARCH_URL, wait_until="domcontentloaded", timeout=60000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(4000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_test_id("search_address-textfield").fill(zip_code))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(2500))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_test_id("search-results__0-text").click(timeout=8000))  # type: ignore[attr-defined]
    _try(lambda: page.locator("#segmentation-moving-no").check(timeout=6000))  # type: ignore[attr-defined]
    _try(lambda: page.locator("#segmentation-renting-no").check(timeout=6000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_test_id("show-plans-button").click(timeout=8000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(6000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_test_id("Solar Plans-check-box").check(timeout=6000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(3000))  # type: ignore[attr-defined]

    # The EFL PDF is fetched inside the popup the "efl-text" click opens, so a
    # page-scoped expect_response never sees it -- listen at the CONTEXT level.
    efl_seen: list[str] = []
    ctx = getattr(page, "context", None)
    if ctx is not None:
        ctx.on("response", lambda r: efl_seen.append(r.url) if _RELIANT_EFL_RESP_RE.search(r.url) else None)

    names = page.get_by_test_id("planName-text")  # type: ignore[attr-defined]
    count = names.count()
    logger.info("Reliant Energy: found %d plan(s) after Solar filter; capturing EFLs", count)
    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for i in range(count):
        try:
            raw = names.nth(i).inner_text(timeout=3000)
        except Exception:  # noqa: BLE001
            continue
        name = _clean_plan_name(re.sub(r"\bplan\s*$", "", raw, flags=re.I).strip())
        if "solar" not in _normalize_name(name) or name in seen:
            continue
        seen.add(name)
        logger.info("Reliant Energy: capturing EFL for %r", name)
        # Nearest ancestor plan card (class *contains* the hashed "plan-container").
        card = names.nth(i).locator(
            "xpath=ancestor::div[contains(@class,'plan-container')][1]"
        )
        # A card carries desktop + mobile view-details; click a visible one.
        if not _try(lambda card=card: card.locator(".analyticsProductViewDetails:visible").first.click(timeout=6000)):
            continue
        _try(lambda: page.wait_for_timeout(800))  # type: ignore[attr-defined]
        before = len(efl_seen)
        _try(lambda card=card: card.locator('[data-testid="efl-text"]:visible').first.click(timeout=8000))
        efl_url: Optional[str] = None
        for _ in range(24):  # wait up to ~12s for the popup's PDF fetch
            if len(efl_seen) > before:
                efl_url = efl_seen[-1].split("?")[0]  # drop the ?_gl= analytics query
                break
            _try(lambda: page.wait_for_timeout(500))  # type: ignore[attr-defined]
        for extra in list(ctx.pages if ctx else [])[1:]:
            _try(lambda extra=extra: extra.close())
        _try(lambda: page.keyboard.press("Escape"))  # type: ignore[attr-defined]
        if not efl_url:
            continue
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=name,
                efl_url=efl_url,
                is_buyback=True,
                extraction_method="harvest",
                context="Reliant solar plan; EFL PDF via myaccount.reliant.com/files",
            )
        )
    return plans


RELIANT = RepConfig(
    key="reliant",
    retailer="Reliant Energy",
    homepage="https://shop.reliant.com/",
    harvester=_reliant_harvest,
)


# --------------------------------------------------------------------------- #
# Atlantex Power interactive harvester (ASP.NET; EFL is a direct efl.aspx PDF)
# --------------------------------------------------------------------------- #
# Atlantex's enrollment site is classic ASP.NET WebForms. Its solar plan ("Solar
# Buy Back Plan") only appears with the ?promoCode=tpgsolar query, so the flow
# navigates there, enters the ZIP, clicks Continue, expands "More info", and
# clicks "Electricity Facts Label" -- which opens the EFL. The popup is flaky to
# read, but the click fetches a direct PDF from an efl.aspx endpoint
# (enroll.atlantexpower.com/EmailHTML/efl.aspx?RateID=..&BrandID=..&PromoCodeID=..,
# plain application/pdf, httpx-downloadable) captured from the network response.
_ATLANTEX_URL = "https://enroll.atlantexpower.com/Enrollment/Default.aspx?promoCode=tpgsolar"
_ATLANTEX_EFL_RESP_RE = re.compile(r"atlantexpower\.com/[^\"']*efl\.aspx[^\"']*", re.I)


def _atlantex_harvest(page: object, zip_code: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Interactive harvester for Atlantex (see the section comment). The solar
    plan is promoCode-gated; ``harvest_live`` handles goto()/lifecycle but this
    re-navigates to the promoCode URL to reveal it."""

    def _try(action) -> bool:
        try:
            action()
            return True
        except Exception:  # noqa: BLE001
            return False

    efl_seen: list[str] = []
    ctx = getattr(page, "context", None)
    if ctx is not None:
        ctx.on("response", lambda r: efl_seen.append(r.url) if _ATLANTEX_EFL_RESP_RE.search(r.url) else None)

    logger.info("Atlantex: opening promoCode-gated enrollment and entering ZIP %s", zip_code)
    _try(lambda: page.goto(_ATLANTEX_URL, wait_until="domcontentloaded", timeout=60000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(3000))  # type: ignore[attr-defined]
    _try(lambda: page.locator("#ctl00_EnrollmentPlaceHolder_txtZipCode").fill(zip_code))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("button", name="Continue").click(timeout=8000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(4000))  # type: ignore[attr-defined]
    _try(lambda: page.get_by_role("link", name="More info").first.click(timeout=8000))  # type: ignore[attr-defined]
    _try(lambda: page.wait_for_timeout(1000))  # type: ignore[attr-defined]

    logger.info("Atlantex: clicking EFL link, waiting for the PDF response")
    before = len(efl_seen)
    _try(lambda: page.get_by_role("link", name="Electricity Facts Label").first.click(timeout=8000))  # type: ignore[attr-defined]
    efl_url: Optional[str] = None
    for _ in range(24):
        if len(efl_seen) > before:
            efl_url = efl_seen[-1]
            break
        _try(lambda: page.wait_for_timeout(500))  # type: ignore[attr-defined]
    for extra in list(ctx.pages if ctx else [])[1:]:
        _try(lambda extra=extra: extra.close())
    if not efl_url:
        return []
    return [
        DiscoveredPlan(
            retailer=config.retailer,
            plan_name="Solar Buy Back Plan",
            efl_url=efl_url,
            is_buyback=True,
            extraction_method="harvest",
            context="Atlantex solar buyback plan (promoCode tpgsolar); EFL via efl.aspx",
        )
    ]


ATLANTEX = RepConfig(
    key="atlantex",
    retailer="Atlantex Power",
    homepage="https://enroll.atlantexpower.com/",
    harvester=_atlantex_harvest,
)

# Registry of configured REPs. Add more here as their flows are recorded.
REP_CONFIGS: dict[str, RepConfig] = {
    c.key: c
    for c in (
        GREEN_MOUNTAIN,
        TXU,
        CHARIOT,
        GEXA,
        FRONTIER,
        AMBIT,
        OCTOPUS,
        CHAMPION,
        DIRECT_ENERGY,
        RELIANT,
        ATLANTEX,
    )
}
