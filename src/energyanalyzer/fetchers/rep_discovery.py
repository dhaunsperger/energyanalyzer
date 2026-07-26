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
    # Base for resolving RELATIVE EFL hrefs found in the rendered HTML. Defaults
    # to `homepage`, which is right whenever the plans page lives on the same
    # host you navigate to. Set it when a REP's shopping flow hands off to a
    # different host: Chariot's marketing site (chariotenergy.com) redirects into
    # signup.chariotenergy.com, whose cards carry relative hrefs like
    # "/Home/EFl?productId=...". Resolved against the marketing host those 404 --
    # and silently, because a bad base only surfaces later as a failed download.
    # Observed 2026-07-24: all 11 Chariot EFLs 404'd exactly this way.
    efl_base: Optional[str] = None
    # Force a HEADFUL browser for this REP. Tesla sits behind Akamai, which
    # answers headless Chromium with a 403 "Access Denied" page (verified
    # 2026-07-25: headless 403, headful 200 on the same URL and UA). Needs a
    # display -- fine under WSLg, which exports DISPLAY. Per-REP, never a
    # blanket default: headful pops a visible window and is slower.
    force_headful: bool = False
    # Apply playwright-stealth's evasions to this REP's browser context. Ambit
    # only, and measured rather than assumed (2026-07-26, site verifiably up):
    # plain Playwright gets a 165-byte "Blocked by WAF" at /Path2Plans, stealth
    # renders all 14 plans through the same funnel. Not a blanket default -- it
    # injects init scripts into every page, and the other REPs need none of it.
    stealth: bool = False

    @property
    def link_base(self) -> str:
        """Base URL for resolving relative links in this REP's rendered HTML."""
        return self.efl_base or self.homepage
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
    base = config.link_base

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
# --------------------------------------------------------------------------- #
# Vistra platform: the EFL *document* endpoint
# --------------------------------------------------------------------------- #
# TXU and Ambit both run Vistra's shopping platform, and both expose an EFL link
# of the form `<host>/PDFGenerator?formType=EnergyFactsLabel&comProdId=<id>...`.
# That URL is a **viewer page**, not the document: it returns the site's Next.js
# HTML shell (~8-9 KB, content-type text/html) to httpx, to a browser session
# carrying the full funnel's cookies, and even to a real in-browser navigation.
# Its JS then fetches the actual PDF from `/api/getdocument` and wraps it in a
# `blob:` for display.
#
# `/api/getdocument` serves `application/pdf` to plain httpx with no session at
# all, so rewriting to it keeps EFL downloads browser-free. Every query
# parameter is renamed, and `efldate` must be full ISO 8601:
#
#   viewer : formType  comProdId  efldate=YYYY-MM-DD  lang      custClass
#   backend: docType   productid  efldate=<ISO8601>   language  classification
#
# Found 2026-07-24 on Ambit by watching the popup's network traffic; TXU's
# identical deferral ("HTML response -- a browser-rendered EFL viewer/SPA")
# confirmed the same rewrite works there.
_VISTRA_PDFGEN_RE = re.compile(r"(?i)^(https?://[^/]+)/PDFGenerator\?(.*)$")


def _vistra_getdocument_url(
    host: str, product_id: str, efldate: str, tdsp: str = "ONCOR"
) -> str:
    """Vistra's real EFL-PDF endpoint for a product id. `efldate` is YYYY-MM-DD;
    the endpoint wants full ISO 8601, so midnight is appended."""
    return (
        f"{host}/api/getdocument?docType=EnergyFactsLabel&productid={product_id}"
        f"&efldate={efldate}T00:00:00&tdsp={tdsp}"
        f"&language=en&classification=Residential"
    )


def _rewrite_vistra_efl_url(url: str, tdsp: str = "ONCOR") -> str:
    """Rewrite a scraped `/PDFGenerator?...` viewer URL to the `/api/getdocument`
    document URL. Returns the input unchanged if it isn't a PDFGenerator URL or
    lacks a comProdId -- callers must never lose a URL to this."""
    m = _VISTRA_PDFGEN_RE.match(url or "")
    if not m:
        return url
    host, query = m.group(1), m.group(2)
    pid_m = re.search(r"comProdId=([A-Za-z0-9]+)", query, re.I)
    if not pid_m:
        return url
    date_m = re.search(r"efldate=(\d{4}-\d{2}-\d{2})", query, re.I)
    efldate = date_m.group(1) if date_m else dt.date.today().isoformat()
    tdsp_m = re.search(r"tdsp=([A-Za-z]+)", query, re.I)
    return _vistra_getdocument_url(host, pid_m.group(1), efldate, tdsp_m.group(1) if tdsp_m else tdsp)


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
    base = config.link_base

    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for card in cards:
        url_m = _TXU_EFL_URL_RE.search(card)
        if not url_m:
            continue
        efl_url = _rewrite_vistra_efl_url(urljoin(base, unescape(url_m.group(1))))
        pid_m = _TXU_COMPRODID_RE.search(efl_url) or re.search(r"productid=([A-Za-z0-9]+)", efl_url, re.I)
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
    base = config.link_base

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
    base = config.link_base

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
# `data-productid` attribute, from which we CONSTRUCT the EFL URL (pointing at
# the backend document endpoint, not the viewer -- see _AMBIT_EFL_BASE below).
_AMBIT_CARD_SPLIT_RE = re.compile(r'(?=<div\b[^>]*\bclass="[^"]*\bshow-plan\b)')
_AMBIT_PRODUCTID_RE = re.compile(r'data-productid="([^"]+)"', re.I)
_AMBIT_PLANNAME_RE = re.compile(r'data-planname="([^"]+)"', re.I)
# Buyback self-label: the plan name ("Texas Solar Buyback ...") or the card's
# visible export-credit description.
_AMBIT_BUYBACK_RE = re.compile(r"buyback|excess solar|get paid for your excess", re.I)
# The BACKEND document endpoint -- not the /PDFGenerator link the "See Plan
# Details" panel exposes. That one is only a *viewer page*: it returns
# text/html (the Next.js shell) to browser and httpx alike, and its JS then
# calls this endpoint and wraps the result in a blob: URL for display. Fetching
# /PDFGenerator directly yields the app's 404 shell no matter what -- with a
# session, with 45 cookies from a completed funnel, even on a real in-browser
# navigation. Confirmed 2026-07-24 by watching the popup's network traffic.
#
# Same shape as Direct Energy's blob-backed EFL (api-oam.directenergy.com).
# Note the parameter names are ALL different from the viewer's:
#   PDFGenerator: formType  comProdId  efldate=YYYY-MM-DD  lang  custClass
#   getdocument : docType   productid  efldate=<ISO 8601>  language classification
_AMBIT_EFL_BASE = "https://shopping.ambitenergy.com/api/getdocument"
# Doug's TDU. The capture is Oncor-specific (ZIP 78665); the endpoint needs a
# tdsp, absent from the collapsed list DOM, so we supply it. Change for another
# TDU territory.
_AMBIT_TDSP = "ONCOR"


def _ambit_efl_url(product_id: str, efldate: str) -> str:
    """Construct Ambit's EFL PDF URL for a product id.

    `efldate` is a plain ``YYYY-MM-DD`` date; the endpoint wants full ISO 8601,
    so midnight is appended. Returns a URL that serves ``application/pdf``
    directly to plain httpx -- no browser, no session, so `download_discovered`
    handles Ambit like any other REP.
    """
    return _vistra_getdocument_url("https://shopping.ambitenergy.com", product_id, efldate, _AMBIT_TDSP)


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
            href = urljoin(config.link_base, m.group(1).strip())
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


def _playwright_ctx(config: "RepConfig"):
    """Playwright context manager, stealth-patched when the REP needs it.

    Ambit is the only one so far, and the A/B on a healthy site is stark: plain
    Playwright gets a 165-byte "Blocked by WAF" at /Path2Plans, while a
    stealth-patched context walks the same funnel and renders all 14 plans. The
    site is emphatically up either way -- the EFL API serves PDFs to plain httpx
    with no session at all -- so this is fingerprinting of the automated browser
    specifically, not a policy against being read.

    Opt-in per REP rather than global: it is extra surface (init scripts on every
    page) and the other twelve retailers render fine without it.
    """
    from playwright.sync_api import sync_playwright

    if not getattr(config, "stealth", False):
        return sync_playwright()
    try:
        from playwright_stealth import Stealth
    except ImportError as exc:
        raise RuntimeError(
            f"{config.retailer} needs playwright-stealth to render (plain Playwright is "
            "served a 'Blocked by WAF' page). Install it with:  pip install playwright-stealth"
        ) from exc
    return Stealth().use_sync(sync_playwright())


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
        # Availability probe only -- _playwright_ctx does the real import (and
        # may wrap it in stealth). Kept here so a missing Playwright fails with
        # the install hint below rather than deep inside the render.
        from playwright.sync_api import sync_playwright  # noqa: F401
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

    with _playwright_ctx(config) as pw:
        logger.info(
            "%s: launching browser (headless=%s%s)",
            config.retailer,
            headless,
            ", stealth" if getattr(config, "stealth", False) else "",
        )
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
        # Availability probe only -- _playwright_ctx does the real import (and
        # may wrap it in stealth). Kept here so a missing Playwright fails with
        # the install hint below rather than deep inside the render.
        from playwright.sync_api import sync_playwright  # noqa: F401
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

    with _playwright_ctx(config) as pw:
        logger.info(
            "%s: launching browser (headless=%s%s)",
            config.retailer,
            headless,
            ", stealth" if getattr(config, "stealth", False) else "",
        )
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


# Statuses worth a second try. 403 is here for a specific, measured reason: Ambit
# (and TXU) sit behind an Azure Front Door WAF that can answer a plain-text
# "Blocked by WAF" 403. Do not read that as a verdict about automation: on
# 2026-07-26 it was the edge answering for a backend that was simply DOWN.
# Within the hour, and with no change on our side, the same EFL endpoint went
# from that 403 to a 500 for plain httpx, while a browser reached the app and
# was redirected to shopping.ambitenergy.com/maintenance ("temporarily down for
# maintenance"). TXU -- the other Vistra shopping site -- served a maintenance
# notice the same morning, so this is one platform outage, not a policy.
#
# The honest summary of three days' sampling: this endpoint fails in bursts and
# recovers on its own, so the right response to a bad day is to come back later,
# not to escalate. Retries are kept for transient statuses; an explicit block is
# not retried (see _BOT_BLOCK_RE) purely so we stop shouting at an edge that has
# already answered. Handful of requests for one household's own shopping.
_RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
_DOWNLOAD_ATTEMPTS = 3

# A 403 whose body carries one of these is a WAF/bot-detection verdict, not a
# transient hiccup. Retrying it is both useless and rude, so it is exempted from
# _RETRY_STATUSES and it trips the per-host breaker below. Bodies are tiny
# ("Blocked by WAF" is 14 bytes), so matching on content is cheap and specific --
# far better than treating every 403 as a block, which would abandon a whole
# retailer over one stale product ID.
_BOT_BLOCK_RE = re.compile(
    r"blocked by waf|access denied|attention required|request unsuccessful|"
    r"you (?:have been|are) blocked|bot detection",
    re.I,
)


def _is_bot_block(resp) -> bool:
    """True if `resp` is an explicit "we don't serve bots" verdict."""
    if getattr(resp, "status_code", None) != 403:
        return False
    try:
        # Only the first bytes: a block page is short, and a real PDF that
        # somehow 403s should not be decoded in full just to classify it.
        return bool(_BOT_BLOCK_RE.search(resp.content[:2048].decode("utf-8", "replace")))
    except Exception:  # noqa: BLE001 -- unreadable body -> not a recognised block
        return False


def _get_with_retry(client, url: str, host: str, attempts: int = _DOWNLOAD_ATTEMPTS):
    """GET `url`, retrying a transient status with linear backoff.

    Honours the per-host rate limit before every attempt, so a retry can never
    make us hit a site faster than the normal path does. An explicit bot block
    (:func:`_is_bot_block`) is returned immediately rather than retried -- the
    site has already answered, and asking twice more only makes us noisier.
    """
    last_exc: Optional[Exception] = None
    resp = None
    for attempt in range(1, attempts + 1):
        _respect_rate_limit(host)
        try:
            resp = client.get(url)
            # getattr: test seams supply minimal response doubles without a
            # status_code, and "no status" must mean "not retryable", never a
            # crash that turns a working download into a failure.
            if getattr(resp, "status_code", None) not in _RETRY_STATUSES:
                return resp
            if _is_bot_block(resp):
                logger.info("download: %s returned an explicit bot block -- not retrying", host)
                return resp
            last_exc = None
        except Exception as exc:  # noqa: BLE001 -- transport hiccups are retryable too
            last_exc = exc
            resp = None
        if attempt < attempts:
            logger.info(
                "download: %s returned %s (attempt %d/%d), retrying",
                host,
                getattr(resp, "status_code", None) if resp is not None else repr(last_exc)[:40],
                attempt,
                attempts,
            )
            time.sleep(1.5 * attempt)
    if resp is not None:
        return resp
    raise last_exc if last_exc else RuntimeError(f"download failed for {url}")


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
    # Hosts that have explicitly refused bots. Once a site says so we stop
    # asking: the remaining URLs are recorded as failures without a request,
    # which loses nothing (they would have failed anyway) and takes a refresh
    # from ~42 requests against a blocking host down to one.
    blocked_hosts: set[str] = set()
    with httpx.Client(
        timeout=timeout, headers=headers, follow_redirects=True, verify=_efl_ssl_context()
    ) as client:
        for i, plan in enumerate(targets, start=1):
            logger.info(
                "Download %d/%d: %s (%s)", i, total, plan.plan_name, plan.retailer
            )
            # Guard against a malformed/constructed EFL URL (missing scheme, etc.)
            # so it's a clear failure line, not an opaque httpx ValueError.
            if not re.match(r"^https?://", str(plan.efl_url or ""), re.I):
                summary["failed"].append(
                    {"url": plan.efl_url, "error": "malformed or non-http EFL URL -- skipped"}
                )
                if progress_callback:
                    progress_callback(i, total, plan.plan_name)
                continue
            host = urlparse(plan.efl_url).netloc
            if host in blocked_hosts:
                summary["failed"].append(
                    {
                        "url": plan.efl_url,
                        "error": f"not requested -- {host} blocked automated access "
                        "earlier in this run (bot/WAF block)",
                    }
                )
                if progress_callback:
                    progress_callback(i, total, plan.plan_name)
                continue
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
                    resp = _get_with_retry(client, plan.efl_url, host)
                    if _is_bot_block(resp):
                        blocked_hosts.add(host)
                        logger.warning(
                            "%s refused automated access (bot/WAF block); skipping its "
                            "remaining EFL downloads this run",
                            host,
                        )
                        summary["failed"].append(
                            {
                                "url": plan.efl_url,
                                "error": f"{host} blocked automated access (bot/WAF block)",
                            }
                        )
                        if progress_callback:
                            progress_callback(i, total, plan.plan_name)
                        continue
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
    # Was buyback-only on two premises that both expired. (1) "PDFGenerator
    # returns an HTML shell to httpx" -- fixed by _rewrite_vistra_efl_url, which
    # works on shopping.txu.com (verified 2026-07-25: a conventional plan's EFL
    # returns application/pdf). (2) "the rest are on PTC" -- measured false: the
    # 2026-07-25 PTC snapshot carries 2 TXU products against 10 on TXU's own
    # site, so buyback-only was silently dropping 8 plans nothing else supplies
    # (Free Nights & Cool Summer had to be hand-entered from the report for
    # exactly this reason). _discovered_plan_in_ptc dedups the overlap.
    broaden=True,
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
    # The ZIP gate on the marketing site hands off to signup.chariotenergy.com,
    # which is where the plan cards (and their relative EFL hrefs) live.
    efl_base="https://signup.chariotenergy.com/",
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

# Ambit's plans page is a Next.js app at shopping.ambitenergy.com, reached
# directly via /Path2Plans with the ZIP in the query string. Behind it sits an
# Azure Front Door WAF that answers a plain-text "Blocked by WAF" 403 --
# PROBABILISTICALLY, not deterministically: measured 2026-07-24, plain httpx got
# through 3 times in 6, while Playwright got HTTP 200 on 3 of 3. An earlier note
# in this module claimed the WAF "blocks Playwright"; that was an unlucky sample,
# not a rule, so the manual-capture fallback is no longer the only option.
#
# What actually gated the page was mundane: /Path2Plans opens on a short
# qualification prelude ("Are you moving to a new address?" -> "No. I already
# live here" -> "See Plans") and renders no plan cards until it's answered --
# the same shape as Direct Energy's residential/not-moving prelude.
def _ambit_render(page: object, zip_code: str) -> Optional[str]:
    """Ambit nav flow, from a `playwright codegen` recording of the real site.

    `fetch_rendered_html` has already opened `homepage` (the marketing site);
    this drives ZIP -> dwelling type -> the not-moving prelude -> plan list.

    Two details cost a round of guessing and are worth keeping written down:
    the "I already live here" control is a **radio**, not a button (clicking it
    by role=button silently no-ops), and **"See Plans" appears twice** -- once
    before the moving question and once after. Both steps are best-effort
    because the funnel varies (the cookie/terms "Accept" isn't always shown);
    what actually decides success is the plan-card wait at the end, which raises
    so discovery reports a failure rather than yielding zero plans silently.
    """
    def _try(desc: str, fn, timeout: int = 15_000) -> bool:
        try:
            fn(timeout)
            page.wait_for_timeout(1_200)
            return True
        except Exception:  # noqa: BLE001 -- optional funnel step
            logger.info("Ambit: step %r not present/clickable (continuing)", desc)
            return False

    zip_box = page.get_by_role("textbox", name="Enter ZIP Code")
    _try("ZIP entry", lambda t: zip_box.fill(zip_code, timeout=t))
    _try("Get Started", lambda t: page.get_by_role("button", name="Get Started").first.click(timeout=t))
    _try("dwelling type: House", lambda t: page.get_by_text("House", exact=True).first.click(timeout=t))
    _try("Accept", lambda t: page.get_by_role("button", name="Accept").first.click(timeout=t))
    _try("See Plans (1st)", lambda t: page.get_by_role("button", name="See Plans").first.click(timeout=t))
    # The moving question: a RADIO, not a button.
    _try(
        "radio: No. I already live here.",
        lambda t: page.get_by_role("radio", name="No. I already live here.").check(timeout=t),
    )
    _try("See Plans (2nd)", lambda t: page.get_by_role("button", name="See Plans").first.click(timeout=t))

    page.wait_for_selector("[data-productid], [id^='PlanCard_']", timeout=60_000)
    page.wait_for_timeout(2_500)
    return None


AMBIT = RepConfig(
    key="ambit",
    retailer="Ambit Energy",
    homepage="https://www.ambitenergy.com/",
    extractor=extract_ambit,
    render=_ambit_render,
    # EFLs come from shopping.ambitenergy.com/api/getdocument (plain PDF over
    # httpx). Was buyback-only on the same false premise as TXU: the 2026-07-25
    # PTC snapshot lists 1 Ambit product against 14 on Ambit's own site.
    broaden=True,
    # Required: plain Playwright is served "Blocked by WAF" at /Path2Plans.
    stealth=True,
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


def _popup_url_when_ready(popup: object, timeout_ms: int = 15_000) -> Optional[str]:
    """The popup's real URL, waiting for it to actually navigate.

    `popup.url` is available immediately after `expect_popup()` -- but before the
    navigation commits it is a placeholder (Playwright reports ":" here, not
    even "about:blank"). Waiting on `load` does not help: these popups render a
    **PDF**, whose load event may never fire, so the wait times out and the
    placeholder is read instead.

    Measured on Champion 2026-07-25: all 7 plans returned ":" , which then
    collapsed to ONE plan because the dedup key is derived from the URL -- the
    harvester reported "1 plan(s)" from 7 successfully-opened popups, and the
    surviving one carried an unusable URL ("malformed or non-http EFL URL").

    So: poll until the URL looks like a real http(s) address.
    """
    deadline = timeout_ms
    while deadline > 0:
        url = getattr(popup, "url", None)
        if url and url.lower().startswith(("http://", "https://")):
            return url
        try:
            popup.wait_for_timeout(250)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 -- popup closed under us
            break
        deadline -= 250
    return None


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

    def _enter_zip_and_wait() -> int:
        """Run the ZIP prelude and WAIT for plan cards. Returns the card count.

        A fixed sleep here used to be the whole synchronisation: on a slow load
        the sweep found 0 cards and the REP silently contributed nothing
        (observed 2026-07-25 -- 7 cards at 10:44, 0 at 12:03 with no other
        change). Waiting on the cards themselves makes it depend on the page
        being ready rather than on the site responding within 3 seconds.
        """
        # Champion renders the whole ZIP form TWICE (a desktop copy and a mobile
        # one). `.first` is frequently the off-screen copy, so the fill/click
        # silently time out and the flow never leaves the homepage -- the REP
        # then reports "0 plan card(s)" with no error anywhere. Try each copy.
        # The ZIP form is React-rendered; filling it the instant DOMContentLoaded
        # fires lands before the input is wired up.
        _try(lambda: page.wait_for_timeout(4000))  # type: ignore[attr-defined]
        # NB: do NOT click the "Enter Your Address or Zip Code" label first --
        # it matches twice and clicking the off-screen copy leaves the form in a
        # state where the subsequent fill lands nowhere. Filling the input
        # directly is both simpler and what actually works.
        if not _fill_any_matching(page, "input[name='zipcode']", zip_code):
            _fill_any_matching(page, "input[type='text']", zip_code)
        _click_any_matching(page, "View Rates and Plans", 8000)
        # Two interstitials, and they are SEQUENTIAL -- each appears only after
        # the previous is dismissed, and each takes several seconds:
        #   View Rates -> (~8s) "New Service" -> /ShopAndEnroll
        #                -> (~9s) a dialog that COVERS the plan cards
        # The old code fired both clicks back-to-back with 6s timeouts, so it
        # missed the first, never reached the second, and left the flow on the
        # homepage -- reported only as "0 plan card(s)", with no error anywhere.
        _click_any_matching(page, "New Service", 20_000)
        _try(lambda: page.wait_for_timeout(6000))  # type: ignore[attr-defined]
        for closer in ("Close this dialog", "Close", "\u00d7"):
            if _click_any_matching(page, closer, 15_000):
                break
        _try(lambda: page.wait_for_timeout(3000))  # type: ignore[attr-defined]
        _try(lambda: page.wait_for_selector(  # type: ignore[attr-defined]
            "button:has-text('See More Plan Details')", timeout=30_000
        ))
        try:
            return page.get_by_role("button", name="See More Plan Details").count()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return 0

    logger.info("Champion Energy: entering ZIP %s and loading plans", zip_code)
    if _enter_zip_and_wait() == 0:
        # One reload + retry: the prelude is interstitial-dependent and a single
        # bad run shouldn't cost the whole REP.
        logger.info("Champion Energy: no plan cards yet -- reloading and retrying the ZIP flow")
        _try(lambda: page.goto(config.homepage, wait_until="domcontentloaded", timeout=60_000))  # type: ignore[attr-defined]
        _try(lambda: page.wait_for_timeout(2000))  # type: ignore[attr-defined]
        _enter_zip_and_wait()

    # Champion serves the EFL as a DOWNLOAD (Content-Disposition: attachment),
    # not a navigation: the popup it opens stays blank forever and `popup.url`
    # reports the placeholder ":". Reading that gave every plan the same key, so
    # the dedup collapsed 7 plans into 1 -- with an unusable URL. The real URL is
    # on the response, so capture it there (same approach as Reliant, whose PDF
    # also arrives outside the page's own navigation).
    captured_pdfs: list[str] = []

    def _on_response(resp) -> None:
        try:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "application/pdf" in ctype:
                captured_pdfs.append(resp.url)
        except Exception:  # noqa: BLE001 -- listener must never raise
            pass

    try:
        page.context.on("response", _on_response)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- fake page seam in tests has no context
        pass

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
            before = len(captured_pdfs)
            with page.expect_popup() as pop:  # type: ignore[attr-defined]
                page.get_by_role("button", name="Electricity Facts Label").click()  # type: ignore[attr-defined]
            popup = pop.value
            # Give the download response a moment to arrive, then take the URL
            # the network saw. Fall back to the popup's own URL for any variant
            # that really does navigate.
            for _ in range(24):
                if len(captured_pdfs) > before:
                    break
                _try(lambda: page.wait_for_timeout(250))  # type: ignore[attr-defined]
            efl_url = captured_pdfs[-1] if len(captured_pdfs) > before else _popup_url_when_ready(popup, 2000)
            _try(lambda: popup.close())
            if efl_url:
                logger.info(
                    "Champion Energy: plan %d/%d -- captured EFL URL %s", i + 1, count, efl_url
                )
            else:
                logger.info(
                    "Champion Energy: plan %d/%d -- popup never resolved to an http URL",
                    i + 1, count,
                )
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
            logger.info(
                "Champion Energy: plan %d/%d (%s) -- duplicate EFL key %s, skipping",
                i + 1, count, plan_name or "?", key,
            )
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
        # standard PTC plans -- unless the config says broaden. Twelve Hour
        # Power is why: it is a free-window plan, not a solar one, so this
        # filter hid it, and the only other source (meterplan, which publishes
        # no EFL) guessed a generic 9pm-6am window for a plan whose name says
        # twelve hours, plus a day rate 2.3c off. It ranked ~$824 too high.
        if name in seen or (not getattr(config, "broaden", False) and "solar" not in _normalize_name(name)):
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
    # Take every plan, not just the solar ones: Twelve Hour Power is a
    # free-window plan whose real EFL beats meterplan's guess by ~$824/yr, and
    # it is one of the report's own benchmark plans.
    broaden=True,
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
# --------------------------------------------------------------------------- #
# Tesla Electric extractor
# --------------------------------------------------------------------------- #
# Tesla puts each plan behind a tab ("With Powerwall" / "With Vehicle" / "None")
# on /tesla-electric/view-plans, and each tab's "Electricity Facts Label" link is
# a DIRECT PDF on digitalassets-energy.tesla.com -- no viewer page, no popup URL
# to capture, so a plain extractor is enough once the tabs have been clicked.
# `_tesla_render` concatenates the per-tab HTML (same trick as Chariot's
# paginated listing) and this dedups by PDF URL.
_TESLA_EFL_URL_RE = re.compile(
    r'href="(https://digitalassets-energy\.tesla\.com/[^"]+\.pdf)"', re.I
)
# Plan identity is encoded in the filename, e.g.
# ".../Drive%2012M/TE_DRIVE_12M_PLAN_ONCOR_JUN_2026.pdf" -> "Drive 12M".
_TESLA_PLAN_FROM_FILE_RE = re.compile(r"/TE_(.+?)_PLAN", re.I)


def extract_tesla(html: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Static (no-LLM) extractor for Tesla Electric's rendered plans tabs.

    Every Tesla Electric plan buys back exported energy -- the EFLs state
    "Other Energy Exports: 3c / kWh" and "Vehicle Energy Exports: 90% of the
    Real-Time Market Price" -- so `is_buyback` is True for all of them. The rate
    itself is left to the EFL parser: for a solar house the relevant number is
    the "Other Energy Exports" one, not the vehicle rate.
    """
    html = _SCRIPT_RE.sub("", _strip_comments(html))
    plans: list[DiscoveredPlan] = []
    seen: set[str] = set()
    for m in _TESLA_EFL_URL_RE.finditer(html):
        url = unescape(m.group(1))
        if url in seen:
            continue
        seen.add(url)
        # The tabs also link Terms & Conditions PDFs from the same asset host.
        # Only the EFLs carry the `TE_<plan>_PLAN` filename, so that pattern --
        # not the host -- is what identifies an EFL.
        name_m = _TESLA_PLAN_FROM_FILE_RE.search(url)
        if not name_m:
            continue
        raw = name_m.group(1).replace("_", " ").title()
        plans.append(
            DiscoveredPlan(
                retailer=config.retailer,
                plan_name=raw,
                efl_url=url,
                is_buyback=True,
                extraction_method="static",
                context="Tesla Electric plan tab; EFL is a direct digitalassets PDF",
            )
        )
    return plans


def _tesla_render(page: object, zip_code: str) -> Optional[str]:
    """Tesla nav flow, from a `playwright codegen` recording: ZIP -> View Plans,
    then click each plan tab and collect its HTML.

    Returns the concatenated per-tab HTML (extract_tesla dedups by PDF URL), so
    one render captures every plan. Tabs are best-effort -- Tesla varies which
    are offered by address -- but at least one EFL link must appear or this
    raises, so discovery reports a failure rather than silently finding nothing.
    """
    page.get_by_role("textbox", name="Zip Code").fill(zip_code, timeout=25_000)
    page.get_by_role("button", name="View Plans").first.click(timeout=25_000)
    page.wait_for_timeout(6_000)

    parts: list[str] = []
    for tab in ("With Powerwall", "With Vehicle", "None"):
        try:
            page.get_by_role("tab", name=tab, exact=True).first.click(timeout=15_000)
            page.wait_for_timeout(3_000)
            parts.append(page.content())
        except Exception:  # noqa: BLE001 -- not every tab is offered everywhere
            logger.info("Tesla: tab %r not available (continuing)", tab)
    joined = "\n".join(parts) if parts else page.content()
    if not _TESLA_EFL_URL_RE.search(joined):
        raise RuntimeError("Tesla: no Electricity Facts Label PDF link found after the tab sweep")
    return joined


TESLA = RepConfig(
    key="tesla",
    retailer="Tesla Electric",
    homepage="https://www.tesla.com/tesla-electric/plans",
    extractor=extract_tesla,
    render=_tesla_render,
    # Akamai serves headless Chromium a 403 "Access Denied"; headful gets 200.
    force_headful=True,
)



# --------------------------------------------------------------------------- #
# Meter Energy interactive harvester
# --------------------------------------------------------------------------- #
# Meter (meterplan.com) publishes the markdown index `fetchers/meterplan.py`
# reads, but its OWN plans' real EFLs used to come from JSON-LD on /plans as
# presigned S3 links. Meter rebuilt that page as a client-rendered app and the
# links left the HTML entirely: `parse_meterplan_efl_offers` returned 0 offers on
# every refresh (verified 2026-07-25 -- 0 offers, 0 downloaded, 0 parsed), which
# is why Meter's six plans stayed synthetic markdown rows.
#
# The presigned URLs still exist, now behind a JS button ("View Electricity Facts
# Label (EFL)") with no href -- the same shape as Champion. Clicking it fetches
# `light-assets.s3.amazonaws.com/efls/EFL_<Plan>_<date>_<TDU>_<hash>.pdf` and
# hands it to the browser as a download, so the URL is captured from a
# context-level `application/pdf` response rather than from any anchor.
_METER_PLANS_URL = "https://meterplan.com/plans?zipcode={zip}"
_METER_EFL_NAME_RE = re.compile(r"/EFL_([A-Za-z0-9+]+)_", re.I)
# Term filters to sweep. Meter shows one term at a time; the EFL parser reads the
# actual term out of each PDF, so this is only about making every plan reachable.
_METER_TERMS = ("12 months", "24 months", "36 months")
# Usage profiles. "Solar + battery" is skipped deliberately: battery-required
# plans are excluded everywhere else in this app (they need hardware the owner
# doesn't have), matching `meterplan_to_drafts`' battery skip.
# Order matters: the page loads on a profile that already lists the solar plans,
# and clicking "No solar" first NARROWS it to Standard only. Sweep the default
# view first, then the alternatives.
# Sweep the default view (which lists the solar plans) plus the two non-battery
# profiles. "Solar + battery" is skipped on purpose: battery-required plans need
# hardware the owner doesn't have and are excluded everywhere else in this app
# (see meterplan_to_drafts' battery skip).
_METER_PROFILES = (None, "Solar", "No solar")


def _fill_any_matching(page: object, selector: str, value: str, timeout_ms: int = 6000) -> bool:
    """Fill the first ACTIONABLE input matching `selector`.

    Same hazard as `_click_any_matching`: sites commonly render a desktop and a
    mobile copy of the same form, so `.first` is often the off-screen one and the
    fill silently times out.
    """
    try:
        loc = page.locator(selector)  # type: ignore[attr-defined]
        n = loc.count()
    except Exception:  # noqa: BLE001
        return False
    for i in range(n):
        try:
            el = loc.nth(i)
            el.scroll_into_view_if_needed(timeout=2000)
            el.fill(value, timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _click_any_matching(page: object, name: str, timeout_ms: int = 6000) -> bool:
    """Click the first ACTIONABLE control with this accessible name.

    Meter renders its filter chips twice (a desktop row and a mobile one), so
    `.first` is often the off-screen copy: Playwright then waits for
    actionability and times out even though `is_visible()`/`is_enabled()` both
    report True. Trying each match -- scrolling it into view first -- is what
    makes the filter sweep work.
    """
    try:
        loc = page.get_by_role("button", name=name)  # type: ignore[attr-defined]
        # WAIT for the control to exist before counting. `count()` is evaluated
        # immediately, so on a control that hasn't rendered yet this returned 0
        # and the function gave up without ever waiting -- the per-click timeout
        # only ever applied once a match already existed. That silently skipped
        # Champion's "New Service" interstitial (which appears ~8s after the ZIP
        # submit), leaving the flow on the homepage and the REP reporting
        # "0 plan card(s)" with no error at all.
        try:
            loc.first.wait_for(state="attached", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 -- genuinely absent: nothing to click
            return False
        n = loc.count()
    except Exception:  # noqa: BLE001
        return False
    for i in range(n):
        try:
            el = loc.nth(i)
            el.scroll_into_view_if_needed(timeout=2000)
            el.click(timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001 -- try the next copy
            continue
    return False


def _close_extra_pages(page: object) -> None:
    """Close every page in the context except `page` itself."""
    ctx = page.context  # type: ignore[attr-defined]
    for other in list(getattr(ctx, "pages", [])):
        if other is page:
            continue
        try:
            other.close()
        except Exception:  # noqa: BLE001
            pass


def _meter_harvest(page: object, zip_code: str, config: RepConfig) -> list[DiscoveredPlan]:
    """Interactive harvester for Meter Energy's own plans.

    Meter shows one card per plan (Saver / Earner / Standard) and **each card
    carries its OWN 12/24/36-month tabs**. Clicking a page-level "24 months"
    therefore only re-terms the FIRST card -- every other plan silently keeps its
    default 12-month EFL, which is exactly the bug that made Earner return the
    same document for all three terms. So the term tab is scoped to the card that
    owns the EFL button being clicked.

    Two further behaviours, both measured:

    * A profile chip ("No solar" / "Solar") changes which plans are listed;
      Standard only appears under some of them, so profiles are swept too.
      "Solar + battery" is skipped -- battery-required plans need hardware the
      owner doesn't have and are excluded everywhere else in this app.
    * Clicking an EFL hands the browser a download and opens a blank popup, after
      which the page stops responding to clicks entirely. A fresh load before
      every capture is the only reliable reset found.
    """
    captured: list[str] = []

    def _on_response(resp) -> None:
        try:
            if "application/pdf" in (resp.headers.get("content-type") or "").lower():
                captured.append(resp.url)
        except Exception:  # noqa: BLE001 -- listener must never raise
            pass

    try:
        page.context.on("response", _on_response)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- fake page seam in tests
        pass

    url = _METER_PLANS_URL.format(zip=zip_code)

    def _efl_buttons():
        return page.get_by_role(  # type: ignore[attr-defined]
            "button", name="View Electricity Facts Label (EFL)"
        )

    def _load(profile: Optional[str]) -> int:
        """Fresh load + optional profile chip; returns the EFL-button count."""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)  # type: ignore[attr-defined]
            page.wait_for_timeout(6000)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return 0
        if profile and not _click_any_matching(page, profile, 8000):
            return 0
        try:
            page.wait_for_timeout(1500)  # type: ignore[attr-defined]
            return _efl_buttons().count()
        except Exception:  # noqa: BLE001
            return 0

    seen: set[str] = set()
    plans: list[DiscoveredPlan] = []
    for profile in _METER_PROFILES:
        n = _load(profile)
        logger.info("Meter Energy: %s -- %d plan card(s)", profile or "default view", n)
        for i in range(n):
            for term in _METER_TERMS:
                if _load(profile) <= i:
                    break
                try:
                    efl = _efl_buttons().nth(i)
                    # The card owning this button: nearest ancestor that also
                    # holds the per-plan term tabs.
                    card = efl.locator(
                        "xpath=ancestor::*[.//button[normalize-space()='12 months']][1]"
                    )
                    card.get_by_role("button", name=term).first.click(timeout=8000)
                    page.wait_for_timeout(2000)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 -- card may not offer this term
                    continue
                before = len(captured)
                try:
                    btn = _efl_buttons().nth(i)
                    btn.scroll_into_view_if_needed(timeout=2000)
                    btn.click(timeout=10_000)
                except Exception:  # noqa: BLE001
                    continue
                for _ in range(28):
                    if len(captured) > before:
                        break
                    try:
                        page.wait_for_timeout(250)  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        break
                _close_extra_pages(page)
                if len(captured) <= before:
                    continue
                got = captured[-1]
                # Presigned URLs carry a rotating signature; key on the path so
                # the same document isn't re-captured under another filter.
                key = got.split("?", 1)[0]
                if key in seen:
                    continue
                seen.add(key)
                name_m = _METER_EFL_NAME_RE.search(got)
                plan_name = name_m.group(1) if name_m else "Meter plan"
                logger.info("Meter Energy: captured %s (%s)", plan_name, term)
                plans.append(
                    DiscoveredPlan(
                        retailer=config.retailer,
                        plan_name=f"{plan_name} {term.split()[0]}",
                        efl_url=got,
                        # Saver/Earner are the solar buyback products; Standard isn't.
                        is_buyback=plan_name.lower() != "standard",
                        extraction_method="harvest",
                        context=f"Meter Energy /plans, {profile or 'default'} / {term}",
                    )
                )
    return plans


METER = RepConfig(
    key="meter",
    retailer="Meter Energy",
    homepage="https://meterplan.com/plans",
    harvester=_meter_harvest,
)


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
        TESLA,
        METER,
    )
}
