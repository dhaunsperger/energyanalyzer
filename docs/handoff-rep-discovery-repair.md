# Handoff: repair the Direct Energy and Reliant discovery harvesters

**Written:** 2026-09-26 by a cloud session (no network, no browser — everything
below is read from the code and from the 2026-09-26 refresh summary, not from a
live site).
**For:** a local Claude Code session on Doug's machine, where the sites render
and Playwright can drive a real browser.
**Priority:** after the renewal decision. Nothing here blocks Compare today.

---

## What happened

The 2026-09-26 "Refresh market data" run reported, among twelve healthy REPs:

```json
"direct_energy": {"status":"empty","plans_found":0,"buyback":0,"detail":"harvested live","live":true},
"reliant":       {"status":"empty","plans_found":0,"buyback":0,"detail":"harvested live","live":true}
```

Both scrapes ran to completion and matched **nothing**. Doug confirmed both
sites render plans fine in an ordinary browser, so this is selector/flow drift
(or bot detection), not an empty catalog.

## What it did and did not cause

**Did not cause any delisting.** Discovery only earns authority to mark a plan
gone when it returns plans — `app/common.py`:

```python
result["coverage"] = {
    rep["retailer"]: rep["plan_names"]
    for rep in result["reps"].values()
    if rep.get("status") == "ok" and rep.get("live") and rep.get("plan_names")
    and rep["retailer"] not in incomplete
}
```

`plan_names` was empty for both, so neither entered `coverage`. The 15 Direct
Energy and 3 Reliant delistings in that run came from **Power to Choose**, which
genuinely stopped listing them. Don't "fix" the delistings; fix the scrapers.

**Did cost coverage.** Direct Energy is now represented in the database by three
Autopay Texas plans and nothing else, with no live source checking it. Its two
solar-relevant plans — **Direct Solar Unlimited 12** and **Twelve Hour Power
24**, worth ~$1,500 and ~$1,244/yr in the reference analysis — survive only via
the meterplan.com index rows, and only because commit `1267095` stopped a
delisted plan from retiring the index row that still lists it.

## What is already ruled out

Don't spend time on these; they were checked.

- **Not headless.** `DIRECT_ENERGY` sets `force_headful=True`, and the caller
  honors it: `headless=headless and not config.force_headful`
  (`app/common.py`). Direct Energy ran in a real window and still got zero. Its
  config comment records the earlier headless-vs-headful finding (2026-07-26:
  headless 0 cards, headful 26) — that fix is still in place and is not the
  current failure.
- **Not the robots/politeness gate.** Both reached the site; `detail` is
  `"harvested live"`, and a refusal would have raised, not returned empty.
- **Not the empty-coverage guard.** It behaved correctly (above).

## Strongest lead

Both are **NRG** shops and both use interactive `harvester=` functions rather
than static `extractor=` parsing. They failing in the same run points at a
shared platform/frontend change rather than two coincidences. Check whether the
NRG shop frontend was rebuilt — if so, both selector sets below moved together.

## The code

`src/energyanalyzer/fetchers/rep_discovery.py`

| What | Where |
|---|---|
| `RepConfig` dataclass (fields: `render`, `harvester`, `broaden`, `force_headful`, `stealth`, `check_robots`) | ~line 134 |
| `_direct_energy_harvest` | ~line 2361 |
| `DIRECT_ENERGY` config | ~line 2474 |
| `_reliant_harvest` | ~line 2508 |
| `RELIANT` config | ~line 2590 |
| `REP_CONFIGS` registry (keys: `direct_energy`, `reliant`, …) | ~line 3008 |

### Direct Energy — the selectors to re-verify

Flow: `https://shop.directenergy.com/` → ZIP gate → plan cards, with a
"load more" button paging the rest in.

```python
page.wait_for_selector(".plan__wrapper", timeout=90000)   # ← the failure point
cards = page.locator(".plan__wrapper")
page.get_by_role("button", name=_DE_LOAD_MORE_RE)         # paging
card.locator(".rich-text-body h4").first.inner_text()     # plan name
# EFL URL comes from the api-oam docs endpoint
```

The log line `"Direct Energy: no plan cards rendered -- site or flow changed"`
fires when `.plan__wrapper` never appears. That is almost certainly what
happened. `broaden=True` is deliberate — take every plan, not just solar, because
Twelve Hour Power is a benchmark plan.

### Reliant — the selectors to re-verify

Flow: `https://shop.reliant.com/search-for-plans/` → type address → pick first
autocomplete result → "moving? no" → "renting? no" → "show plans" → optional
"Solar Plans" filter.

```python
page.get_by_test_id("search_address-textfield").fill(zip_code)
page.get_by_test_id("search-results__0-text").click(timeout=8000)
page.locator("#segmentation-moving-no").check(timeout=6000)
page.locator("#segmentation-renting-no").check(timeout=6000)
page.get_by_test_id("show-plans-button").click(timeout=8000)
page.get_by_test_id("Solar Plans-check-box").check(timeout=6000)
names = page.get_by_test_id("planName-text")
card = names.nth(i).locator("xpath=ancestor::div[contains(@class,'plan-container')][1]")
card.locator(".analyticsProductViewDetails:visible").first.click(timeout=6000)
card.locator('[data-testid="efl-text"]:visible').first.click(timeout=8000)
```

Note the known fragility already documented in the code: plan cards use
build-hashed CSS-module classes
(`OfferPlanContained-module--plan-container--<hash>`), matched by the
**substring** `plan-container`. A frontend rebuild can change that stem. The
`data-testid` attributes are the stabler anchors — check those first.

Also relevant: every step is wrapped in `_try(...)`, which swallows failures so
one changed step degrades to an empty result rather than an error. That is why
this surfaced as `status: "empty"` instead of a traceback. Consider whether a
step that fails *before* any plan is found should raise instead — see "Optional
hardening" below.

## How to work it

Discovery is opt-in and needs the extra:

```bash
pip install -e ".[discovery]" && playwright install chromium
```

Run one REP at a time rather than a whole refresh. `refresh_market_data` takes
`discovery_reps` (a list of keys) and `discovery_headless`; for a tight loop,
call the discovery layer directly against `REP_CONFIGS["direct_energy"]` so you
are not re-fetching PTC and re-parsing 300 EFLs on every iteration.

To re-derive a flow by hand there is already a tool:

```bash
.venv/bin/python scripts/stealth_codegen.py https://shop.directenergy.com/
```

It opens a headful, stealth-patched context with the Playwright Inspector
attached (plain `playwright codegen` can't install the stealth init scripts
before first navigation). Hit **Record**, drive the funnel manually, and copy the
generated calls into the harvester. Needs `DISPLAY` set — WSLg or an X server.

## Done looks like

1. `direct_energy` and `reliant` both return a non-zero `plans_found` against
   ZIP 78665, with `buyback` > 0 for Reliant (Solar Payback Match/Plus) and
   Direct Solar Unlimited present for Direct Energy.
2. Their retailers appear in `result["coverage"]`, so they regain delisting
   authority — which also means their stale PTC-sourced entries can be retired
   honestly on the next refresh.
3. A test in `tests/test_rep_discovery.py` covering the new selectors against a
   **saved HTML fixture** (that file's existing tests show the pattern), so the
   next drift fails in CI rather than in a refresh summary.
4. Full suite green: `python -m pytest -q` (594 passing as of `1267095`).

## Optional hardening, if the cause turns out to be bot detection

Several configs already carry `stealth=True` / `force_headful=True` for
Akamai-shaped edges (Tesla, and Direct Energy's own earlier fix). If Direct
Energy or Reliant now 403s or serves an empty shell to automation, the same
escape hatches are the first thing to try — `RepConfig.stealth`, and the
`_BrowserFetcher(stealth=...)` path.

Separately, and worth doing regardless: a REP whose harvester finds **zero**
plans is nearly always broken rather than genuinely empty. Right now that is a
quiet `status: "empty"` line among a dozen healthy ones. Doug reports the
refresh UI does surface a warning, so check what it actually says before adding
another — the goal is that a zero-plan harvest is impossible to scroll past, not
that there are two warnings.

## Ambit fallback, for reference

If a site defeats automation entirely, there is a precedent: Ambit blocks
automation, so its page is saved by hand as
`data/rep_discovery/ambit_<UTC-timestamp>.html` and discovery parses the newest
such capture. Octopus used the same mechanism in this run (`live render failed;
used capture octopus_20260726T232657Z.html`) — note that path also means stale
rates, so it is a stopgap, not a fix.
