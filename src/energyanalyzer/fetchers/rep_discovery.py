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
import re
import time
import unicodedata
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

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
    extraction_method: str = "static"  # "static" | "llm"
    llm_confidence: Optional[float] = None
    context: str = ""  # link text / surrounding snippet (audit trail)


@dataclass
class RepConfig:
    """Per-REP discovery configuration. Each site's navigation flow differs, so
    ``render`` is a per-REP function (record it once with
    ``playwright codegen <homepage>`` and adapt), and ``extractor`` is a per-REP
    static parser over the rendered HTML. Start with Green Mountain and add REPs
    without touching the rest of the module."""

    key: str
    retailer: str
    homepage: str
    extractor: Callable[[str, "RepConfig"], list[DiscoveredPlan]]
    # render(page, zip_code): drive the ZIP gate / "View Plans" flow. Returns
    # None (caller captures page.content() once) OR, for a paginated listing, a
    # string of concatenated per-page HTML the render collected itself (the
    # extractor splits on plan cards and dedups, so page boundaries don't matter).
    render: Optional[Callable[[object, str], Optional[str]]] = None


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
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=_USER_AGENT)
        page = context.new_page()
        try:
            page.goto(config.homepage, wait_until="domcontentloaded", timeout=timeout_ms)
            rendered = config.render(page, zip_code)
            if isinstance(rendered, str):
                # Paginated render collected + concatenated the pages itself
                # (and did its own per-page settling); use it verbatim.
                html = rendered
            else:
                if settle_ms:
                    page.wait_for_timeout(settle_ms)
                html = page.content()
        finally:
            context.close()
            browser.close()

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = snapshot_dir / f"{config.key}_{ts}.html"
    snapshot_path.write_text(html, encoding="utf-8")
    return html, snapshot_path


# --------------------------------------------------------------------------- #
# Download + manifest
# --------------------------------------------------------------------------- #
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

    entries: list[dict] = []
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for i, plan in enumerate(targets, start=1):
            host = urlparse(plan.efl_url).netloc
            file_path = dest / _efl_filename(plan)
            if file_path.exists():
                summary["skipped"].append(str(file_path))
                entries.append(_manifest_entry(plan, file_path))
                if progress_callback:
                    progress_callback(i, total, plan.plan_name)
                continue
            try:
                _respect_rate_limit(host)
                resp = client.get(plan.efl_url)
                resp.raise_for_status()
                file_path.write_bytes(resp.content)
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
    # so match it by id prefix rather than the exact hash.
    zipbox = page.locator('[id^="zip-form-widget-"]')  # type: ignore[attr-defined]
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

# Registry of configured REPs. Add more here as their flows are recorded.
REP_CONFIGS: dict[str, RepConfig] = {c.key: c for c in (GREEN_MOUNTAIN, TXU, CHARIOT)}
