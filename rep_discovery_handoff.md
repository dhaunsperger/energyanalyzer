# EnergyAnalyzer — REP EFL Discovery Agent: Handoff to Claude Code

## Context

`energyanalyzer` already has two working plan-discovery pipelines (`fetchers/ptc.py`,
`fetchers/meterplan.py`) feeding `data/efl/` → `app/common.parse_downloaded_efls` →
`plans/drafts/` review/promote UI. That whole downstream chain is done and untouched.

The gap: **solar buyback plans that aren't on Power to Choose or meterplan.com** —
they only exist on individual REP marketing sites. This handoff covers a new
`fetchers/rep_discovery.py` module to find and download those EFLs, feeding the
same `data/efl/` landing zone.

## What we learned during manual investigation (do this work once, not per-REP from scratch)

1. **REP sites require a real browser, not `requests`/`httpx`.** They're
   client-rendered SPAs (Gatsby/React/etc.) — confirmed on Green Mountain's
   site, where a raw HTML fetch returns an essentially empty `<div id="___gatsby">`
   shell with zero plan data. **Use Playwright.**

2. **`wait_until="load"` and `wait_for_load_state("networkidle")` are unreliable
   on marketing sites.** Third-party trackers (Dynatrace, Optimizely, geoip-js,
   chat widgets, etc.) keep network activity alive indefinitely, so these
   waits can hang or time out. Use `wait_until="domcontentloaded"` on `goto`,
   then explicitly wait for a specific selector/text you know should appear
   rather than waiting for "everything to settle."

3. **Hero banners/carousels cause click-interception errors** (Playwright
   refuses to click an element mid-animation — "subtree intercepts pointer
   events"). Workaround: a short `wait_for_timeout()` after page load, and
   `force=True` on the click *only* after visually confirming (headed mode)
   the target element is real and just flagged as unstable — not as a
   default response to every click timeout.

4. **Use `playwright codegen <url>` to build each REP's navigation flow**
   (ZIP-code gate, "View Plans" click, etc.) rather than hand-writing
   selectors — record the flow once by clicking through it like a customer,
   then adapt the generated script. Each REP's flow needs to be
   recorded/verified separately; there's no universal path.

5. **Key finding — some REPs self-label everything, no LLM needed.**
   On Green Mountain's rendered plans page:
   - Each plan's "Learn more" modal (already present in the DOM, just
     hidden — no need to click through 11 modals) contains an
     "Important Documents" section with an explicit
     `<a>Electricity Facts Label</a>` link — labeled by the site itself.
   - Each plan's hidden analytics metadata div carries
     `analyticsproductname="..."` and `analyticscontractrates="...^BuyBack:11.4"`
     — the site's own tracking markup flags buyback plans directly.
   - **On sites like this, a static parser (regex/DOM query) is enough —
     no LLM classification required.** This mirrors the existing
     `eflparse` module's philosophy (deterministic where possible).

6. **The LLM (local Ollama model) is the fallback for sites that DON'T
   self-label this cleanly** — no "Electricity Facts Label" link text, no
   buyback flag in an attribute, just an ambiguous "Download" button or
   filename. Confirmed working test: Ollama model `lfm2.5`, forced JSON
   output via `format: "json"` on `/api/chat`, given link text + surrounding
   page context, correctly classified an EFL/buyback link. It does **not**
   reliably know domain facts unprompted (hallucinated a wrong definition of
   "EFL" in an ungrounded chat test) — so always feed it real page context,
   never rely on its unprompted knowledge, and give the classifier prompt an
   explicit definition + a few worked positive/negative examples rather than
   trusting it to infer "solar buyback" from priors.

7. **Not yet verified**: whether Green Mountain's clean self-labeling pattern
   is typical or a lucky first case. Before generalizing, check at least
   1–2 more REPs (e.g. Reliant, TXU) to see how often the static-parse path
   works vs. how often the LLM fallback is actually needed.

## Reference implementation (working, Green Mountain, headed mode)

```python
import re
from playwright.sync_api import Playwright, sync_playwright


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.greenmountainenergy.com/", wait_until="domcontentloaded", timeout=60000)

    page.get_by_title("Sustainable electricity for a").get_by_placeholder("Enter ZIP code").click()
    page.get_by_title("Sustainable electricity for a").get_by_placeholder("Enter ZIP code").fill("78665")
    page.get_by_title("Sustainable electricity for a").get_by_role("button").click()

    try:
        page.get_by_role("button", name=re.compile("accept", re.I)).click(timeout=3000)
    except Exception:
        pass

    page.wait_for_timeout(8000)  # let hero animation/carousel finish moving

    html = page.content()
    open("/tmp/gm_rendered.html", "w").write(html)

    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
```

Confirmed output includes per-plan modal blocks with pattern:

```html
<h3>Solar All Nighter 12</h3>
...
<h6>Important Documents</h6>
<a href="https://signup.greenmountainenergy.com/files/....pdf">Electricity Facts Label</a>
<a href="https://signup.greenmountainenergy.com/files/....pdf">Terms of Service</a>
<a href="https://signup.greenmountainenergy.com/files/....pdf">Your Rights as a Customer</a>
```

And separately, hidden per-plan analytics divs:

```html
<div hidden analyticsid="Product_57699341" ... analyticscontractrates="500:21.3|1000:19.4|2000:18.4^BuyBack:11.4"
     analyticsproductname="Renewable Rewards Solar Max 12" ...></div>
```

## What Claude Code should build

Read `ARCHITECTURE.md` fully first, then build `fetchers/rep_discovery.py`
following the conventions of `fetchers/ptc.py` and `fetchers/meterplan.py`:

- **Per-REP config**: a small structured list (domain, ZIP-entry flow
  description or a per-REP Playwright script/function, since each site's
  navigation differs) — start with just Green Mountain using the reference
  script above, structured so more REPs can be added without rewriting the
  module.
- **Two-tier extraction per REP's rendered HTML**:
  1. Try a static/deterministic parse first — look for explicit "Electricity
     Facts Label" link text, `analyticsproductname`/buyback-flag attributes,
     or similar self-labeling patterns (site-specific, so this will need a
     small per-REP extraction function, not one universal regex).
  2. Fall back to LLM classification (Ollama, `lfm2.5`, JSON-forced output)
     only when the static parse doesn't find a confident match — pass real
     link text + surrounding page context, never bare URLs. Log the model's
     `thinking` field alongside each decision for audit purposes.
- **Output**: download matched EFL PDFs to `data/efl/`, write a manifest
  entry per download (retailer, plan_name if known, source_url,
  discovered_at, extraction_method: "static" | "llm", llm_confidence if
  applicable) so downstream dedup/refresh logic can distinguish
  REP-discovered plans from PTC/meterplan ones.
- **Tests**: `tests/test_rep_discovery.py`, using `/tmp/gm_rendered.html`
  (or a trimmed copy committed as a fixture) as a synthetic-style fixture
  for the Green Mountain static extractor — no live network calls in tests.
- **Politeness**: respect `robots.txt`, rate-limit per domain, realistic
  User-Agent.
- **Do NOT** wire into `app/common.refresh_market_data` yet — get Green
  Mountain working standalone first, confirm the two-tier approach on 1-2
  more REPs, then wire in as a follow-up.

Ask before guessing at another REP's site structure — record the flow with
`playwright codegen <url>` first (human does this step) and hand you the
resulting script + a saved rendered-HTML sample, same as the Green Mountain
workflow above.
