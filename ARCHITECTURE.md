# EnergyAnalyzer — Architecture & Agent Coordination Doc

**Audience:** Claude subagents implementing modules, and the human owner (Doug).
**Maintainer:** Lead architect session. Subagents: read this whole file before coding.
Update the *Status Board* section when you finish a module; do not change design
decisions without noting an open question at the bottom.

## 1. What this app does

Replicates a solar electric plan analysis service:

1. Ingest 12 months of 15-minute interval usage (grid **import** and solar
   **export**) from SmartMeter Texas (SMT) exports.
2. Maintain a database of retail electric plans (parsed from EFL PDFs +
   Power to Choose CSV + manual entry), each described by a structured
   rate schema.
3. Simulate one year of monthly bills for every plan against the actual
   interval data — including free-hours plans, time-of-use plans, solar
   buyback variants, and real-time-wholesale (RTW) indexed plans priced from
   historical ERCOT settlement point prices.
4. Rank plans by **first-year net bill** and present results in a local
   Streamlit app with Excel export.

Reference artifacts (NOT in repo — privacy): the user's SMT CSV lives at
`data/IntervalData.csv` (gitignored), and the target report is a PDF from the
service. Key ground truths for validation:

- Interval CSV: 365 days × 96 intervals × 2 channels. Totals: **import
  11,278 kWh, export 9,803 kWh** (Jul 2025–Jun 2026).
- Current plan (Pulse Power, see `plans/pulse_current.yaml`) first-year net
  bill per the service: **≈ $1,031**.
- Other report benchmarks (July 2026, Oncor TDU $4.06/mo + 6.12¢/kWh):
  TXU Solar BB $1,211 · Direct Twelve Hour Power 24 $1,244 · Green Mtn
  Pollution Free Nights $1,265 · Reliant Free Overnight $1,330 · Gexa Solar
  Buyback 12 $1,613 · TXU Free Nights & Cool Summer $1,907.
  Target accuracy: ranking order and roughly ±5% on dollars.

## 2. Stack & conventions

- Python ≥ 3.11, `src/` layout, package `energyanalyzer`. Install dev mode:
  `pip install -e ".[dev]"`.
- pandas + pydantic v2 + PyYAML; Streamlit UI; xlsxwriter for Excel;
  pdfplumber/pdftotext for EFL parsing; httpx for fetchers; pytest.
- **Dependencies:** only the lead session edits `pyproject.toml`. If your
  module needs a new dep, check it's listed; if not, note it in your report.
- Money in **USD floats**, rates internally in **$/kWh** (convert ¢ → $ at
  the schema boundary; YAML files use `_ckwh` suffixes in cents because
  that's how EFLs quote them).
- Timestamps: canonical index is tz-aware **UTC**; local clock logic (rate
  windows) uses `America/Chicago`. Interval convention: timestamps are
  **interval START**, duration 15 min.
- Subagents: do NOT run `git commit`/`push`; the lead session commits. Do not
  create files outside your module's directories + `tests/`.
- Tests: put in `tests/test_<module>.py`. Synthetic fixtures only in the repo;
  tests that need the real CSV must skip gracefully if `data/IntervalData.csv`
  is absent (use `pytest.mark.skipif`).

## 3. Repo layout

```
ARCHITECTURE.md            ← this file
pyproject.toml
plans/*.yaml               ← plan database (human-editable, git-versioned)
tdu/oncor.yaml             ← versioned TDU delivery tariffs
data/                      ← gitignored: user CSVs, parquet cache, ERCOT prices
src/energyanalyzer/
  core/models.py           ← DONE (lead). Pydantic schema — THE contract.
  core/plans_io.py         ← DONE (lead). Load/save plan YAMLs, TDU tariffs.
  ingest/                  ← Task 2: SMT CSV + Green Button XML → canonical frame
  prices/                  ← Task 4: ERCOT RTM settlement price loading
  fetchers/                ← Task 4: Power to Choose CSV + EFL PDF downloads
  engine/                  ← Task 3: billing simulation
  eflparse/                ← Task 5: static EFL PDF → draft plan YAML
  report/                  ← Task 6: Excel export
  app/                     ← Task 6: Streamlit app (entry: app/Home.py)
tests/
```

## 4. Canonical interval data (contract for ingest & engine)

`ingest` produces, and `engine`/`app` consume, a pandas DataFrame:

- Index: `ts` — tz-aware UTC `DatetimeIndex`, 15-min interval starts,
  strictly increasing, no duplicates.
- Columns: `import_kwh: float`, `export_kwh: float` (both ≥ 0; blanks → 0.0).
- Helper `core.models.add_local_columns(df)` adds `local` (America/Chicago
  tz-aware), `month` (Period 'M' of local time), `hour`, `weekday` (0=Mon),
  `date` — used by rate-window matching. Billing months = **calendar months
  of local time**.

DST notes: SMT CSVs list local clock times per day; spring-forward days have
92 rows/channel, fall-back 100 (the 01:00 hour repeats — first pass is DST,
`fold=0`). Convert to UTC accordingly; never drop rows silently. Emit a
`QualityReport` (dataclass in ingest): row counts per channel, blank/estimated
counts, missing intervals, date range.

SMT CSV format (primary):
```
ESIID,USAGE_DATE,REVISION_DATE,USAGE_START_TIME,USAGE_END_TIME,USAGE_KWH,ESTIMATED_ACTUAL,CONSUMPTION_SURPLUSGENERATION
'10443...,07/01/2025,07/02/2025 07:37:17,00:00,00:15,0.580,A,Consumption
```
- ESIID has a leading apostrophe (Excel guard) — strip it.
- `CONSUMPTION_SURPLUSGENERATION` ∈ {`Consumption`, `Surplus Generation`}.
- `USAGE_END_TIME` of `00:00` means midnight of the next day.
- `ESTIMATED_ACTUAL`: 'A' actual, 'E' estimated (keep, count in QualityReport).

Green Button XML (secondary): NAESB ESPI Atom feed; `<IntervalReading>` has
`start` epoch seconds (UTC), `duration` 900, `value` in **Wh** (÷1000);
`flowDirection` 1 = delivered (import), 19 = received (export). A file may
contain only one channel; the loader must merge multiple files.

## 5. Plan schema (see `core/models.py` — authoritative)

Plans are YAML files in `plans/`, one per plan, loaded via
`core.plans_io.load_plans()`. The engine consumes `Plan` objects.

Key semantics implementers must honor:

- **`energy_rates`**: ordered list of `EnergyRate`; for each interval the
  FIRST rate whose `window` matches applies; the last entry must have
  `window: null` (default/catch-all). A rate is either `rate_ckwh` (fixed
  ¢/kWh) or `rtw` (indexed: `price × multiplier + adder_ckwh`, optional
  `cap_ckwh`, floor at `floor_ckwh` default 0). Free nights/weekends are just
  windows with `rate_ckwh: 0`. An `EnergyRate` may set `tdu_exempt: true`:
  import matched by that rate is excluded from TDU volumetric charges (some
  REPs' "free" hours waive delivery too — validated against the report:
  Green Mtn/Reliant/Direct free nights need it, TXU's do not).
- **`RateWindow`**: `months` (1-12), `weekdays` (0=Mon..6=Sun), `hours`
  (0-23, local interval-start hour). Empty list = wildcard. A window like
  hours [21,22,23,0,...,5] expresses "9pm–6am".
- **`ev_free_charging`** (optional `EvFreeCharging`): models plans (e.g. Tesla)
  that give *free EV charging* during certain hours — unlike a free-nights
  window (which zeroes ALL usage in it), this waives the energy charge on only
  up to `monthly_kwh_cap` import kWh inside `window` each billing month (the
  estimated car load, e.g. 271 = 3250 kWh/yr ÷ 12), spent chronologically at
  those kWh's own rate. Usage beyond the cap or outside the window is billed
  normally, and only the energy charge is waived — **TDU delivery still applies**
  (a REP can't waive TDU). The engine reports the freed kWh as a monthly
  `ev_free_kwh` column.
- **`buyback.kind`**: `none` | `fixed` (flat ¢/kWh) | `rtw` (indexed like
  above) | `windows` (time-of-use export via `rates: list[EnergyRate]`,
  same first-match semantics). "1:1" plans are `fixed` with rate equal to
  the energy charge. Each kind requires its own nested config or `Plan`
  validation fails (`assertion_error`, e.g. "rtw buyback needs rtw config"):
  - `kind: fixed` requires `rate_ckwh: <flat ¢/kWh>`.
  - `kind: rtw` requires a nested `rtw:` dict — same shape as an
    `EnergyRate.rtw` above: `{multiplier: 1.0, adder_ckwh: 0.0}` at minimum;
    add `cap_ckwh: <ceiling ¢/kWh>` if the EFL states one (e.g. "capped at
    25¢/kWh"), and `floor_ckwh` if it states a floor (rare; default 0.0).
    Example:
    ```yaml
    buyback:
      kind: rtw
      rtw: {multiplier: 1.0, adder_ckwh: 0.0, cap_ckwh: 25.0}
      offset_scope: all_charges
    ```
  - `kind: windows` requires `rates: [...]` (list of `EnergyRate`, last one
    `window: null`).
  - `kind: none` needs no extra config (the default).
- **`buyback.offset_scope`**: what monthly charges export credits may offset —
  `energy_only` (the `*`-marked "not offsettable" plans: credits reduce energy
  charges but never base or TDU) or `all_charges` (credits offset the whole
  REP+TDU bill). Excess beyond scope goes to rollover balance if
  `rollover: true` (default), else lost. `cash_out: true` = balance paid out,
  i.e. bill may go negative.
- **`buyback.monthly_credit_cap`**: `null` or `energy_charge` (credit earned
  in a month capped at that month's energy charges — e.g. Atlantex Glow).
- **`bill_credits`**: usage-tier credits, e.g. `{min_kwh: 1000, credit_usd: 100}`
  applied when the month's import kWh is in range.
- **`tdu_passthrough`**: true (normal; add TDU tariff charges) or false
  (rates bundle delivery).
- **`base_charge_usd`** monthly; ETF fields are informational (reported, not
  added to bills).
- `needs_review: true` marks auto-parsed/uncertain plans; UI must badge them.

TDU tariffs (`tdu/oncor.yaml`): list of `{effective: date, fixed_usd_month,
volumetric_ckwh}`; engine picks the record effective for each billing month.
For "first-year forward-looking" bills we use the LATEST tariff for all 12
months (matches the service's methodology); function
`core.plans_io.current_tdu()` provides it.

## 6. Billing engine (Task 3) — exact algorithm

For each plan, for each of the 12 local-calendar months in the interval data:

```
imp(t), exp(t)           # kWh per 15-min interval in month
rate(t)                  # $/kWh from first matching EnergyRate (RTW: join price series)
energy_cost   = Σ imp(t)·rate(t)
base          = base_charge_usd
tdu           = fixed_usd_month + volumetric·Σimp(t over non-tdu_exempt rates)
                                                       [if tdu_passthrough]
credit_earned = Σ exp(t)·buyback_rate(t)               [0 if kind=none]
credit_earned = min(credit_earned, energy_cost)        [if monthly_credit_cap=energy_charge]
bill_credit   = Σ credit_usd for tiers matching month import kWh   (subtract)
pool          = credit_earned + rollover_in
offsettable   = energy_cost                       [offset_scope=energy_only]
              | energy_cost + base + tdu - bill_credit  [all_charges]
used          = min(pool, max(offsettable, 0))
month_bill    = energy_cost + base + tdu - bill_credit - used
rollover_out  = (pool - used) if rollover else 0
```

- If `cash_out`: `used = pool` (bill may go negative), no rollover.
- Taxes/PUC assessment: excluded (service excludes them too).
- RTW rates: 15-min ERCOT RTM settlement point prices for the configured
  load zone (default `LZ_NORTH`; configurable), joined on UTC interval start;
  hourly-only price data may be forward-filled to 15-min. Missing price →
  raise, don't guess.
- Outputs per plan: `PlanResult` with `first_year_net`, `monthly` breakdown
  DataFrame (all components above), `final_rollover_balance`, effective
  avg ¢/kWh. Module: `engine/cost.py`, entry
  `simulate(plan, intervals, tdu, prices=None) -> PlanResult` and
  `rank(plans, intervals, tdu, prices) -> list[PlanResult]` sorted ascending.
- Watch the sign conventions: bills in dollars owed; credits reduce.
- Partial months at series edges: bill them as-is (they're only edge months
  when data isn't exactly 12 calendar months; with our data, Jul 1–Jun 30
  aligns perfectly).

## 7. ERCOT prices & fetchers (Task 4)

`prices/ercot.py`:
- `load_prices(zone: str, data_dir=Path("data/ercot")) -> pd.Series`
  ($/kWh, UTC 15-min index). ERCOT publishes RTM Settlement Point Prices
  ($/MWh — divide by 1000) per 15-min settlement interval, local interval
  *ending*, with DST flag column. Support the two common file shapes:
  (a) ERCOT "Historical RTM Load Zone and Hub Prices" annual/monthly XLSX
  (one sheet per month; columns Delivery Date, Delivery Hour, Delivery
  Interval, Repeated Hour Flag, Settlement Point Name/Price);
  (b) CSV concatenations of report 12301 (SPPHLZNP6905) files.
- Cache normalized series to `data/ercot/<zone>.parquet`.
- Optional best-effort downloader for the public MIS archive; network may be
  unavailable — everything must work from manually downloaded files, with a
  clear error telling the user what to download and where to put it.

`fetchers/ptc.py`: Power to Choose bulk CSV
(`https://www.powertochoose.org/en-us/Plan/ExportToCsv`), filter TDU=Oncor +
zip 78665, normalize columns (plan name, REP, term, kWh500/1000/2000 avg
prices, EFL URL, renewable %, prepaid/TOU flags), download EFL PDFs to
`data/efl/`. Output an index DataFrame + saved CSV snapshot in `data/ptc/`.
Network egress here is restricted; code defensively and make snapshots
loadable offline.

A refresh does not delete the plans and EFLs it is about to replace: step 1
moves them to `data/refresh_quarantine/`, and step 8
(`reconcile_quarantine`) decides each one's fate once the run is done. A plan
that was re-derived drops its copy; one a completed source still lists is
restored untouched; one a completed source no longer lists is restored *and*
flagged for review. When no source completed, nothing is delisted -- a run
that cannot reach its sources costs no data. Authority is recorded on disk
(`data/refresh_quarantine/authority.json`) because `finish_refresh` is
re-runnable on its own and has no summary in hand then.

`data/efl/manual/` holds EFLs supplied by hand for REPs no fetcher can reach
(Ambit, whose WAF refuses httpx and a real Chromium alike). A refresh wipes
`data/efl/` and re-downloads, which is only safe for files a fetcher can
restore, so the wipe's non-recursive `glob("*.pdf")` deliberately cannot see
this subdirectory; `manual_efl_paths()` adds it back at the parse stage.

`fetchers/meterplan.py`: meterplan.com Texas solar buyback plan index
(`https://meterplan.com/data/texas-solar-buyback-plans.md`), an
hourly-regenerated public markdown page published by Meter Energy Inc. (a
competing REP/broker, PUCT broker #BR250137) that covers solar buyback plans
Power to Choose's export doesn't carry (mostly non-Oncor TDUs, plus a handful
of Oncor ones). `fetch_meterplan` saves a timestamped `.md` snapshot to
`data/meterplan/`; `load_meterplan` parses only the "Meter Plan Availability"
and "Competitor Plan Availability" markdown tables (NOT "Top Plans By TDU For
The Default Profile", which re-lists a subset of the same rows) into a tidy
DataFrame (`tdu`, `retailer`, `plan_name`, `term_months`, `import_ckwh`,
`export_kind`/`export_ckwh`, `base_usd_month`, `etf_usd`,
`etf_per_month_remaining`, `battery_required`, `source_url`, `generated`);
`filter_meterplan` filters by TDU (meterplan's own labels: Oncor/Centerpoint/
AEP Central/AEP North/TNMP/Lubbock); `meterplan_to_drafts` turns rows into
draft plan YAMLs (same `_parse` confidence/evidence shape as `eflparse`),
skipping battery-required rows and rows already in the plan database. We
NEVER read their "Estimated annual cost" column — it's Meter Energy's own
cost-engine output against a fixed default usage profile; EnergyAnalyzer
always computes costs itself from the user's actual interval data. Network
egress here is restricted too; a committed reference snapshot
(`tests/fixtures/meterplan_sample.md`) is the format reference and test
fixture.

The markdown index omits document URLs, but Meter's **/plans?zipcode=<zip>**
HTML embeds Meter's OWN plans' *real* EFL PDFs in a JSON-LD `OfferCatalog`
(presigned S3, ~7-day). `fetch_meterplan_efls`/`parse_meterplan_efl_offers`
pull + download those (filtered to a TDU via `areaServed`), so Meter's own
plans (Earner/Saver/Standard ± Battery) come from real parsed EFLs; when that
succeeds, `meterplan_to_drafts(skip_retailers={"Meter Energy"})` drops the
synthetic markdown rows for them. No competitor EFLs are exposed here. Synthetic
markdown plans for *any* retailer are later removed by refresh step 8
(`app.common.supersede_meterplan_plans`) once a real/authoritative plan
(PTC/discovery/Meter EFL, or manual) covers the same plan — matched by a
conservative significant-token compare (`_plan_supersedes`: equal term,
overlapping retailer brand tokens, synthetic name-tokens ⊆ real name), which
distinguishes same-name variants and only ever deletes `source="meterplan"`
plans.

`fetchers/rep_discovery.py`: EFL discovery on individual REP marketing sites —
the plans PTC and meterplan.com both miss (see `rep_discovery_handoff.md`). The
extractors still flag solar **buyback** plans specifically, but the refresh
stage now downloads *all* discovered plans (`download_discovered(buyback_only=
False)`), deduped against the PTC listing first (`_discovered_plan_in_ptc`,
conservative — term parsed from the discovered name, kept when absent) so only
PTC-missed plans are added. REP sites are client-rendered SPAs, so
`fetch_rendered_html` drives a real browser via Playwright (optional dep:
`pip install 'energyanalyzer[discovery]' && playwright install chromium`;
lazily imported, raises a clear install/manual-fallback message when absent).
Extraction is **deterministic-first** (mirrors `eflparse`'s philosophy) with
two optional LLM tiers: `discover()` runs a per-REP static extractor first
(`extract_green_mountain` reads Green Mountain's self-labeling — an explicit
`<a>Electricity Facts Label</a>` per plan joined by normalized name to the
hidden analytics div whose `analyticscontractrates="...^BuyBack:<rate>"` flags
buyback plans; `extract_txu` reads TXU's `show-plan` cards, whose EFL links
self-label via a `PDFGenerator?formType=EnergyFactsLabel&comProdId=<id>` URL
with buyback announced in each card's visible text; `extract_chariot` reads
Chariot's `planbox` cards, whose EFL links self-label via a
`/Home/EFl?productId=<id>` URL and whose `plandescription` carries the buyback
wording + often the fixed ¢/kWh rate — all NO LLM, all strip the page's
embedded `<script>`/SVG/Next.js data blobs first so badge JSON can't
false-positive). Chariot's buyback plans (Shine/PowerBank/GreenVolt) are gated
behind a "My home has solar panels" path and its listing is paginated, so
`_chariot_render` selects the solar flow and concatenates every page's HTML
(the extractor dedups cards by product id; a `render()` may now return that
joined string instead of `None`). `extract_gexa` reads Gexa's
`plan-list-padding` rows, whose EFL links self-label via an
`eflviewer.aspx?...prodcode=<code>` URL and whose feature list carries
`Plan Type: Solar Buyback` for buyback plans (keyed per card, ignoring the
free-floating `Solar Buyback` ribbon the DOM positions on the *next* card — that
ribbon is also stripped from each plan's stored `context` so it can't mislead
the LLM review). `extract_ambit` reads Ambit's `show-plan` cards (same Vistra
platform as TXU): the collapsed list page hides the EFL link until a "See Plan
Details" expansion, so it **constructs** the `PDFGenerator?...&comProdId=<id>`
EFL URL from each card's `data-productid` rather than scraping it. Ambit sits
behind a WAF that blocks Playwright (so its `RepConfig` has no `render()` — HTML
is captured manually: save the plans page as `data/rep_discovery/ambit_<ts>.html`;
discovery globs `ambit_*.html` and parses the *newest*, so use a sortable UTC
stamp — `mv ambit_rendered.html "ambit_$(date -u +%Y%m%dT%H%M%SZ).html"`) *and*
403s a plain-httpx EFL download, so its 2 buyback
EFLs are opened/downloaded via a browser, not `download_discovered`. `extract_octopus` reads Octopus's `<h2 data-cy="product-title">` cards +
`octopusenergy.com/efl/` links (buyback is bundled in every plan *except*
OctopusFlex per the page's own statement, so `is_buyback = not Flex`; a
competitor comparison EFL, e.g. a txu.com link, is ignored). Octopus rejects a
bare ZIP that spans load zones and requires an **ESI ID** (PII) — so
`_octopus_render` reads it from a **gitignored secrets file**
(`data/rep_discovery_secrets.yaml`, via `_load_rep_secret`), never from code or
git; any REP needing PII to render follows that pattern. A few REPs
compute the EFL URL only on interaction (no URL in the DOM at all): Champion's
"Electricity Facts Label" is a JS `<button>` that opens the PDF in a popup. Such
REPs set an interactive `harvester` on their `RepConfig` instead of an
`extractor` — `harvest_live()` drives the browser (open each plan's "See More
Plan Details" modal, click the EFL button, read the popup's URL:
`docs.championenergyservices.com/ExternalDocs?planName=PN####`) and returns
`DiscoveredPlan`s directly (a config must set one of `extractor`/`harvester`;
`discover()` errors on a harvester-only config). It falls back to `classify_link_llm` (local Ollama, `lfm2.5`,
JSON-forced, fed real link text + page context, never a bare URL; best-effort,
tolerated if the server is down) only for sites that don't self-label. With
`llm_review=True`, every returned EFL is additionally LLM-reviewed
**upgrade-only** (may promote to buyback, never drops an EFL) so a site wording
change can't silently lose a buyback plan. `download_discovered` downloads matched EFLs (buyback-only by
default) into `data/efl/` and appends a per-download manifest
(`data/efl/rep_discovery_manifest.jsonl`: retailer, plan_name, source_url,
discovered_at, extraction_method, llm_confidence, is_buyback, buyback_ckwh) so
downstream dedup/refresh can distinguish REP-discovered plans. Offline-first
(static extractor + classifier run on rendered HTML on disk;
`tests/fixtures/rep_green_mountain_sample.html` is the format reference and
static-extractor fixture). Wired into `refresh_market_data` (§9) as an optional
stage: `app/common._run_rep_discovery` dispatches each configured REP by shape
(harvester → `harvest_live`; extractor+render → live `fetch_rendered_html` +
`discover`; extractor-only/WAF-blocked → `discover` on the newest manual
`data/rep_discovery/<key>_*.html` capture, else reported `manual-needed`),
downloads the buyback EFLs into `data/efl/`, and parses them into drafts the
auto-promote step handles — each REP tolerated independently so one site's
failure never aborts the run. Off by default (each REP is a live browser
session; the sweep is slow), gated behind a checkbox on the Plans page. Adding
more REPs (each needs its flow recorded with `playwright codegen` + a
rendered-HTML sample) is a follow-up.

## 8. EFL static parser (Task 5) — deterministic first, LLM only as an assist

`eflparse/parser.py`: `parse_efl(pdf_path) -> DraftPlan` where DraftPlan =
`{plan: Plan-shaped dict, confidence: {field: 0..1}, evidence: {field:
source text snippet}, unparsed_notes: [...]}`.

Approach: `pdftotext -layout` (poppler) or pdfplumber text; regex/heuristic
passes for: energy charge(s) ¢/kWh or $/kWh; base/monthly charge; TDU
delivery charges (per kWh + per month, and whether "included"/bundled); term
months; ETF ($X or $X/month remaining); buyback ("buyback rate", "solar",
"excess", 1:1 detection when buyback rate == energy charge); free windows
("free nights 9pm-6am/8pm-8am", "free weekends"); TOU tables; bill credits
("$X credit when usage ≥ Y kWh"); prepaid/variable flags. Every extracted
field carries confidence + evidence; anything below 0.8 → `needs_review:
true` on the draft. Test corpus: `tests/fixtures/efl_texts/*.txt` (synthetic
text in the style of real EFLs — create several REP styles; the real Pulse
EFL text is included there as `pulse.txt`).

Human override: drafts are saved to `plans/drafts/<id>.yaml`; the Streamlit
Plans page (Task 6) shows draft vs. parsed evidence side-by-side, lets the
user edit fields and promote to `plans/`.

### 8a. Accuracy harness (`scripts/eval_efl.py`) — the measurement baseline

`tests/fixtures/efl_texts/real/ground_truth.yaml` holds hand-verified
load-bearing values (base charge, energy rates + windows, buyback, term) for all
26 real-EFL fixtures, each with the quoted source line. `scripts/eval_efl.py`
scores the parser — and optionally the parser plus an LLM tier — against it:

```bash
python scripts/eval_efl.py                     # parser only
python scripts/eval_efl.py --model gemma3:4b   # + LLM suggestions
python scripts/eval_efl.py --compare gemma3:4b qwen3:4b   # rank models
```

The headline metric is **silent-wrong**: a field reported at ≥0.8 confidence
whose value is actually wrong. Those never reach a human, so they land straight
in the rankings. *Review-queue size is explicitly not the target* — a change that
halves the queue while adding one silent-wrong is a bad trade. Run this before
and after any parser or model change; do not change `llm.OLLAMA_MODEL` without
re-running `--compare`.

Ground truth is the contract, and it can itself be wrong: the harness caught a
mislabeled `term_months` on `nec_coop` (the EFL says "Contract Term 0
(month-to-month)") on its first run. Fix the YAML, not the parser, when they
disagree and the EFL backs the parser.

### 8c. One-off LLM audit (`scripts/audit_plans_llm.py`)

A diagnostic, not a pipeline stage: it has the model **independently re-read**
every EFL-sourced plan (deliberately *not* primed with the parser's answer,
unlike the repair tier — priming a small model biases it toward agreeing) and
flags disagreements on fields the parser was **confident** about. Writes nothing.

Two traps it exposed, both worth remembering:

* **Confidence lives in different places.** Drafts carry `_parse.confidence`;
  promoted plans do NOT (`plan_fields()` strips it — it isn't Plan schema). A
  first version read the missing block as 0.0 for every field, i.e. "never
  confident", and silently suppressed **every** finding — a clean bill of health
  that was structurally guaranteed rather than measured. Promotion is itself the
  confidence signal: an unflagged promoted plan cleared the gate.
* **Verify a null result before trusting it.** The bug was caught only by asking
  whether the audit had *made any comparisons at all* (a sampled coverage check),
  not by reading its output.

Results of the 2026-07-24 run (173 plans, gemma3:4b, ~4.5 min): 45 flagged, **44
false positives, 1 real bug**. The noise is systematic and predictable — the
model reads the *Average price per kWh* table (bundles TDU), confuses dollars and
cents, and reports bill/usage credits and ETFs as base charges or solar buyback.
The one true positive was worth it: Green Mountain "Renewable Rewards Solar
Credit 12" parsed as `buyback: none` at 0.95 confidence because the export credit
is branded "Renewable Rewards Credit" (~$618/yr of ignored credit). Fixed in
`_BUYBACK_LABEL`, with the EFL added to the corpus + ground truth and a dedicated
regression class. At ~2% precision this is a periodic sweep, not automation.

Memory note: the model holds ~4.7 GB RSS even when `ollama ps` reports
"100% GPU". On the 7.8 GB WSL dev box a full sweep pushes the system into swap,
so unload afterwards (`ollama stop <model>`).

### 8b. LLM suggestion tier (`eflparse/llm_repair.py`) — assist-only

A local Ollama model takes a second pass at drafts the static parser left with a
weak (<0.8) load-bearing field, and **pre-fills those fields for the reviewer**.

**It never promotes.** `needs_review` stays `True` on every draft it touches, and
the fields it supplied are recorded in the draft's `_llm_suggested`
(`{fields, model, reasoning}`) so the Plans review UI badges them and shows the
model's quoted justification next to the confidence table. This is a measured
choice, not caution for its own sake: verifying that a proposed number literally
appears in the EFL still accepts wrong values (the model lifts a real number off
the wrong line), and a silently-wrong rate costs far more than one more plan to
review. Two of the four benchmarked models were *worse than no LLM at all* by the
silent-wrong metric.

Its actual contribution over the deterministic parser is narrow and specific:
**PDFs with broken embedded fonts.** Atlantex's EFL drops `s`/`b`/`w`/`y`/`E`
glyphs, so its base charge reads `ae Charge $19.95 per ill` — legible to a
language model, not to a regex. Everything else the parser already does better.

Guards (all deterministic, all in `llm_repair`): never overrides a field the
parser scored ≥ `keep_threshold`; a flat LLM rate never replaces a structured
windowed/multi-rate schedule (structure is the parser's job); a proposed number
must be verifiable in the source text; the trailing catch-all rate must be
positive (a small model once proposed 0¢ around the clock); and the merged plan
must re-validate against `Plan` or the changes are discarded wholesale.

`llm.py` pins `temperature=0` and `num_ctx=8192`. Both matter: Ollama defaults to
`0.8` (making runs non-reproducible) and to a 4096-token window that **silently
drops the oldest tokens** — the schema system prompt — on a long EFL. Reasoning
models return their chain of thought in a separate `message.thinking` field,
which is logged at DEBUG and never parsed.

Model choice is benchmarked, not assumed — see the table in `llm.py`. Hard
constraint: the model must fit **entirely** in VRAM with its KV cache. When
Ollama can't fit one it silently offloads layers to CPU, which on WSL means
paging weights through ~7 GB of system RAM; that is what hung the dev box with
the 8.5B `lfm2.5` (5.2 GB). Stay at or below ~3 GB of weights unless measured.

Wired into `app.common.parse_downloaded_efls(llm_assist=True)`, exposed as a
default-off checkbox on the Plans page. Best-effort throughout: if Ollama is
down the drafts save exactly as the parser produced them.

## 9. Streamlit app + Excel (Task 6)

Pages (multipage app, `app/Home.py` + `app/pages/`):
1. **Usage** — upload SMT CSV/XML (writes to `data/`, triggers ingest),
   quality report; monthly import/export bar chart with net line; hour×month
   average-net-power heatmap (replicate report p.1, red=import blue=export);
   day/peak/night split table.
2. **Plans** — table of all plan YAMLs (badge `needs_review`); create/edit
   via form; import from EFL PDF (runs Task-5 parser, review UI); pull
   Power to Choose snapshot (statewide, ~1,700 rows -- filtered by TDU,
   picked from the snapshot's distinct values, default ONCOR, plus a
   default-on English-only filter to hide the export's Spanish-language
   duplicate rows; a caption always shows raw-snapshot-count →
   filtered-count so filtering never reads as truncation); EFL downloads
   and the batch "Parse all downloaded EFLs into drafts" step both show
   live per-item progress (`st.progress` + status line) instead of going
   silent until the end; a "Draft plans" section reviews/edits
   `plans/drafts/*.yaml` (confidence + evidence per field from parsing) and
   promotes them into `plans/` (or deletes them), with the main plan table
   refreshing immediately (cache invalidation + rerun) -- no manual reload;
   a "Meterplan solar plan index" subsection (fetch button with offline
   fallback to the newest `data/meterplan/` snapshot, snapshot picker, TDU
   filter, filtered table + raw→filtered count caption, "Import as drafts")
   turns meterplan.com's solar buyback plan index into the same
   `plans/drafts/` review/promote flow, deduped against plans already in the
   database; a "Refresh market data" one-button flow (confirmation-gated)
   orchestrates delete-old-imports → fetch (fallback to newest snapshot on
   disk on network failure) → download → parse → meterplan fetch/dedupe/
   draft (same fallback pattern, tolerated gracefully if unavailable) →
   auto-promote-if-confident in one pass (`app/common.refresh_market_data`),
   stamping promoted plans' `retrieved` date (manual/report-seed plans and
   the current plan are never touched by the delete step).
3. **Compare** — run engine over all plans; ranked table styled like report
   p.2 (Retailer, Plan, Term, Base $/mo, Import ¢/kWh +TDU, Export ¢/kWh,
   Other details, ETF, 1st-Year Net Bill, Stale?); expandable per-plan
   monthly breakdown chart/table; footnote current TDU rates; RTW plans
   marked ‡; staleness warnings (interval data >35 days old, ERCOT price
   coverage short of interval end, Oncor tariff >210 days old) plus a
   per-plan "Stale?" badge (`retrieved` >90 days old, or unstamped
   `report-*` seed) via `app/common` helpers.
4. **Export** — `report/excel.py` builds workbook: Summary (ranked table),
   Monthly Detail (per plan per month components), Usage (monthly + heatmap
   pivot), Plan Inputs (full schema dump). Download button.

### 9a. Refresh durability (`app/refresh_state.py`)

"Refresh market data" runs 10+ minutes with discovery enabled, and **Streamlit
kills the running script on any rerun** — including navigating to another page.
Originally the whole run lived inside that script run with the result landing in
`st.session_state` only at the very end, so an interruption lost the work in
flight *and* every trace that it had happened (observed 2026-07-24: a click-away
during REP discovery left 178 drafts, a 5-plan database, and no summary).

Three independent mechanisms now:

1. **`RefreshJournal`** — an append-as-you-go record at `data/refresh_state.json`
   (gitignored), written atomically (`tempfile` + `os.replace`, so a crash
   mid-write can't leave truncated JSON). Routine progress is throttled to ~1/s,
   but stage transitions and terminal states are never throttled — those are the
   events that matter after a crash. `was_interrupted()` reports a run still
   marked `running` with no live thread owning it.
2. **`RefreshRunner`** — runs the refresh on a **background thread**, which
   Streamlit does not kill on rerun. The thread never touches `st.*` (it has no
   ScriptRunContext); it reports into the journal and the page polls via an
   `st.fragment(run_every=2)`. Fetcher INFO logs go to `data/refresh_log.txt`
   (the discovery console, now tailed from disk — and it outlives the run, so an
   interrupted sweep can still be read back). A second concurrent run is refused:
   two would both be writing `plans/` and `data/efl/`.
3. **`common.finish_refresh()`** — steps 7+8 (auto-promote, then supersede) split
   out of `refresh_market_data` and callable standalone, because they are purely
   local. The slow stages write drafts to disk as they go, so an interrupted run
   is completed **without repeating the sweep**. Idempotent. Surfaced on the
   Plans page as "Finish incomplete refresh" whenever `was_interrupted()`.

Note the discovery caveat: promote/supersede recover the database from drafts
already on disk, but *discovery itself* is not resumable — REPs it never reached
still need a fresh run.

`common.promote_all_drafts()` is the separate "quick look" path: it promotes
**every** draft, confidence gate bypassed, preserving each one's `needs_review`
so unverified plans stay badged in Compare rather than being laundered into
trusted ones. Move semantics like single-draft promote (the draft file, and with
it the `_parse` evidence, is consumed; re-parsing the EFL regenerates it), and a
draft that fails `Plan` validation is left on disk and reported rather than
dropped. Confirmation-gated on the Plans page.

Launch: `streamlit run src/energyanalyzer/app/Home.py`.

## 10. Status board  (update when you finish; keep one line each)

| Module | Task | Status | Notes |
|---|---|---|---|
| core models + plans_io + seeds | #1 | DONE (lead) | schema is the contract |
| ingest | #2 | DONE | CSV position-based DST handling + GreenButton merge; parquet cache |
| engine | #3 | DONE | simulate()/rank() implemented per §6; validated against real CSV (see open Q below re: TDU during free windows). Report-benchmark regression (`test_integration_report_benchmarks`) now reads ALL its inputs -- the 7 `report-2026-07` plan YAMLs, the Oncor tariff, and the interval CSV -- from a frozen archive (`tests/fixtures/benchmark_2026_07/`, see its README; CSV gitignored/private, test skips when absent) so refreshing live usage data / tariffs / plans can't move the expected dollars. |
| prices + fetchers | #4 | DONE | ercot.py: xlsx (NP6-785-ER) + 12301 CSV shapes, parquet cache; ptc.py: fuzzy-column loader, filter_plans, download_efls. Downloaders (download_prices/fetch_ptc_csv) untested live (ercot.com/powertochoose.org blocked in sandbox); manual-download fallback documented in errors. |
| discovery: Chariot host fix + Ambit automation | #4 | DONE | Two REP-discovery fixes 2026-07-24. **Chariot** silently lost ALL 11 EFLs to 404s: its cards carry RELATIVE hrefs (`/Home/EFl?productId=...`) and the marketing site hands off to `signup.chariotenergy.com`, but the extractor resolved them against `homepage` (`chariotenergy.com`). Nothing caught it at discovery time because a bad base only surfaces later as a failed download. New `RepConfig.efl_base` / `link_base` property separates the *navigation* host from the *relative-link* host; Chariot sets `efl_base="https://signup.chariotenergy.com/"`. Verified live: 5/5 real PDFs (the Shine/PowerBank buyback plans). **Ambit** gained a real `render()`: the recorded note that its "WAF blocks Playwright" was WRONG -- Azure Front Door answers `Blocked by WAF` *probabilistically* (measured: plain httpx 3/6, Playwright 3/3), and what actually hid the plans was a qualification funnel (ZIP -> Get Started -> House -> Accept -> See Plans -> **radio** "No. I already live here." -> See Plans). Built from the user's `playwright codegen`; validated live at 14 plans / 2 buyback, matching the manual capture. It IS flaky (success and a card-wait timeout minutes apart), so `_run_rep_discovery` now falls back to the newest manual capture when a live render raises (`_newest_capture`) -- a bad night degrades to the old behaviour instead of dropping every buyback plan. Ambit's EFL download is **also fixed**: the `/PDFGenerator` link the "See Plan Details" panel exposes is only a *viewer page* -- it returns the Next.js 404 shell (text/html, 8 KB) to httpx, to `ctx.request` with 45 funnel cookies, AND to a real in-browser navigation. Watching the popup's own network traffic showed its JS calling a backend endpoint, `/api/getdocument`, and wrapping the result in a `blob:` (same shape as Direct Energy). That endpoint serves `application/pdf` to plain httpx with no session at all, so `download_discovered` needs no browser. Every query parameter is renamed between the two (`formType`/`comProdId`/`lang`/`custClass` -> `docType`/`productid`/`language`/`classification`) and `efldate` must be full ISO 8601 (`...T00:00:00`), not a bare date. Validated end-to-end: both buyback EFLs download, parse (base $9.95, 12.7c, buyback fixed 3.5c energy_only) and come out `needs_review=False`. Ambit is now fully automated -- no manual capture required. 403 tests green. |
| refresh durability | #6 | DONE | New `app/refresh_state.py` after a real incident (2026-07-24): a click-away during REP discovery killed the Streamlit script mid-refresh, leaving 178 drafts, a 5-plan database, and **no summary or record of how far it got**. Three fixes: (a) `RefreshJournal` — atomic append-as-you-go progress at `data/refresh_state.json`, throttled ~1/s but never for stage transitions or terminal states, so an interrupted run leaves an accurate trail; (b) `RefreshRunner` — the refresh now runs on a background thread Streamlit can't kill, reporting into the journal (never `st.*` — no ScriptRunContext) while the page polls it via `st.fragment(run_every=2)`; discovery console logs moved to `data/refresh_log.txt` and are tailed from disk, so they now outlive the run; concurrent runs refused. (c) `common.finish_refresh()` — steps 7+8 extracted from `refresh_market_data` and callable standalone since they're purely local, so an interrupted run is completed from drafts already on disk **without repeating the sweep**; idempotent; surfaced as "Finish incomplete refresh" on the Plans page when `was_interrupted()`. Used to recover the 2026-07-24 incident: 145 promoted, 0 failures, 5 -> 150 plans. Caveat: discovery itself is not resumable — REPs it never reached need a fresh run. 11 new tests, 391 green, ruff clean. |
| eflparse: accuracy harness + LLM assist tier | #5 | DONE | New `scripts/eval_efl.py` scores the parser against `tests/fixtures/efl_texts/real/ground_truth.yaml` (26 hand-verified EFLs, quoted evidence per field); headline metric is **silent-wrong** (confidence >=0.8 but value wrong), not review-queue size. Diagnosis first: of 58 drafts stuck in review, most were deterministic *vocabulary* gaps, not parsing difficulty — Octopus prints `Base Charge: 0.00 per month` verbatim and scored 0.0 confidence; Heritage's `Base Monthly Charge`; Constellation's `Minimum Usage Fee`. Fixed in `_extract_base_charge` (added `Base Monthly Charge` word order, made `$` optional, and a **zero-only** `Minimum Usage Fee/Charge` rule — a NON-zero minimum-usage fee is a conditional charge, never a base charge). Separately fixed a confidently-wrong class: `_extract_buyback` returned `{kind:none}` at 0.95 when no rate label matched, even on EFLs whose own PUCT disclosure answers "Yes, we purchase excess distributed renewable generation" — the LLM tier could never reach it since it only touches fields <0.8. Added `_buyback_disclosure_answer` to veto that confidence (value unchanged, only confidence drops) and `E?xport Credit Rate` to the label vocabulary (verified across all 200 downloaded EFLs: every "credit rate" occurrence is an export credit; the `E?` absorbs corrupted-font PDFs that drop the capital E). Result on the corpus: buyback 21/22 -> 22/22, silent-wrong 1 -> 0, review 9/26 -> 6/26. Then benchmarked 4 local models: `gemma3:4b` and `qwen3:4b` both reach 100/100 load-bearing fields, `granite4:micro` and `lfm2.5-thinking` are *worse than no LLM* (each adds a silent-wrong). Default is `gemma3:4b` — equal accuracy to qwen3 at 15x the speed (0.9s vs 13.9s/EFL), and US-origin per owner preference. Its whole contribution is one EFL: Atlantex's broken-font PDF (`ae Charge $19.95 per ill`). `llm.py` now pins `temperature=0` + `num_ctx=8192` (Ollama defaults 0.8 and 4096, the latter silently dropping the schema system prompt on long EFLs) and logs `message.thinking` at DEBUG. Tier is **assist-only**: pre-fills weak fields, records `_llm_suggested{fields,model,reasoning}`, and never clears `needs_review` (new `plan_fields()` strips both meta keys before promotion). 380 tests green, ruff clean. |
| eflparse | #5 | DONE | static regex/heuristic parser + 6 synthetic/pulse fixtures + 15-file real Texas EFL corpus regression suite (tests/fixtures/efl_texts/real/), 175 tests green; hardened against corrupted/PUA-encoded fonts, bullet/numbered-list/colon layouts, brand-prefixed TOU tables, per-day prepaid fees, and bundled-TDU phrasing; pdfplumber import-failure noise silenced |
| app + excel | #6 | DONE | Streamlit app (Home + 4 pages) + report/excel.py; 3 tests green in tests/test_excel.py; validated end-to-end against real data/IntervalData.csv + plans/*.yaml (pulse_current=$1031.37, txu_solar_bb=$1211.37, gmtn_pollution_free_nights=$1264.87 -- all within a few cents of report benchmarks) |
| app + fetchers followup | #6/#4 | DONE | fixed 3 user-reported Plans-page issues: `fetchers.ptc.filter_plans` gained a backward-compatible `language="English"` default filter + snapshot TDU picker in the UI (was reading as truncation, was actually TDU+Spanish-duplicate filtering); `download_efls` gained `progress_callback` wired to `st.progress`; new `app/common.parse_downloaded_efls` batch-parses `data/efl/*.pdf` into `plans/drafts/` (per-file try/except, skip-if-already-parsed) plus a "Draft plans" review/edit/promote UI -- promote/delete both invalidate the plans cache and `st.rerun()` so the main table updates immediately; 183 tests green (`pytest tests/`), plus manual `streamlit.testing.v1.AppTest` smoke passes on the Plans page across empty and populated states |
| app followup 2: refresh + staleness | #6/#4 | DONE | new `app/common.refresh_market_data` (delete old ptc/efl:-sourced plans+drafts+EFLs+stale snapshots -- never manual/report-*/current-plan -- then fetch-or-fallback → load+filter → download → parse → auto-promote drafts with needs_review=False and all load-bearing confidences >=0.8, stamping `retrieved`) wired to a confirmation-gated "Refresh market data" button + one progress bar with staged labels on the Plans page; new staleness helpers (`interval_staleness_warning`, `price_coverage_warning`, `tdu_staleness_warning`, `plan_is_stale`/`stale_plan_ids`) surfaced as warnings + a "Stale?" table column on Compare; single-draft promote also stamps `retrieved`; 190 tests green (`pytest tests/`, incl. new tests/test_refresh.py), ruff clean, manual AppTest smoke green on both Plans (checkbox-gated button, full refresh pipeline with faked transport, promote/delete) and Compare (staleness warnings + Stale? column render against the real data/IntervalData.csv + plans/*.yaml) |
| meterplan.com solar plan index | #4/#6 | DONE | new `fetchers/meterplan.py` (fetch_meterplan/load_meterplan/filter_meterplan/meterplan_to_drafts, offline-first, tests/fixtures/meterplan_sample.md as format reference); covers solar buyback plans (mostly non-Oncor TDUs) PTC's export lacks -- their "Estimated annual cost" column is never read, only rates; drafts get a `_parse` confidence/evidence block like eflparse, battery-required rows skipped, free-hours-named plans get an assumed 9pm-6am two-rate structure at low confidence + needs_review, deduped by (retailer, plan, term) against plans already in the database; wired into `app/common.refresh_market_data` as a new stage between EFL parsing and auto-promote (same fetch-or-fallback-to-newest-disk-snapshot pattern, tolerated gracefully if unavailable) and into a new "Meterplan solar plan index" subsection on the Plans page (fetch/snapshot-picker/TDU-filter/import-as-drafts, feeding the existing drafts review/promote UI unchanged); from the committed fixture, filtering to Oncor produces 30 imported / 0 skipped-battery / 0 skipped-existing / 10 flagged-for-review (all schema-valid via Plan.model_validate); 214 tests green (`pytest tests/`, incl. new tests/test_meterplan.py + extended tests/test_refresh.py), ruff clean, manual AppTest smoke green on the Plans page (empty + populated meterplan states, load/filter/import-as-drafts) with data/ and plans/drafts/ left with no git residue afterward |
| rep_discovery (REP-site EFL discovery) | #4 | DONE (Green Mountain + TXU + Chariot + Gexa + Frontier + Ambit + Octopus + Champion + Direct Energy + Reliant + Atlantex) | new `fetchers/rep_discovery.py`: deterministic-first (per-REP static self-label extractor) discovery of solar buyback EFLs on REP marketing sites PTC/meterplan miss, plus an interactive `harvester` seam for REPs whose EFL URL is JS-computed and not in the DOM (Champion — `harvest_live()` drives the browser, reads the EFL popup URL, returns `DiscoveredPlan`s directly), an Ollama `lfm2.5` JSON fallback (sites that don't self-label) and an upgrade-only `llm_review=True` pass over all returned EFLs (a site wording change can't silently lose a buyback plan). Octopus needs an ESI ID (PII) when a ZIP spans load zones — read from a gitignored secrets file (`data/rep_discovery_secrets.yaml`, `_load_rep_secret`), never in code/git. Playwright live fetch behind optional `[discovery]` extra (a `render()` may return concatenated multi-page HTML for paginated listings, or be `None` for WAF-blocked/manual-capture REPs); `download_discovered` → `data/efl/` + jsonl manifest. Green Mountain (analytics `^BuyBack` flag), TXU (`show-plan` cards + `PDFGenerator?formType=EnergyFactsLabel` URLs), Chariot (`planbox` cards + `/Home/EFl?productId=` URLs, solar-gated + paginated, ¢/kWh rate parsed from card text), Gexa (`plan-list-padding` rows + `eflviewer.aspx?prodcode=` URLs, `Plan Type: Solar Buyback` self-label keyed per card) and Ambit (`show-plan` cards, EFL URL *constructed* from `data-productid` since the collapsed list hides it; WAF-blocked so manual capture + no render()) static extractors each validated against real rendered-HTML captures (GM 11/2 buyback, TXU 10/1, Chariot 11/11, Gexa 15/2, Ambit 14/2), scripts/SVG stripped first so badge JSON can't false-positive; Octopus (`data-cy="product-title"` cards + `octopusenergy.com/efl/` links, buyback = all plans except OctopusFlex, ESI ID from gitignored secrets, competitor EFL ignored) validated against a real capture (3 plans, 2 buyback); Champion via the interactive `harvester` seam (EFL is a JS popup button; harvest_live drives modal→popup and reads the URL — all Champion plans bundle Indexed Solar Buyback; 3 plans are website-only, absent from PTC) tested through a fake-page seam and validated live (6/6 plans + EFL URLs); llm_review validated live (TXU 10 reviewed/1 flagged/0 false upgrades; Chariot 11/11 at 0.97–0.99 conf; Gexa 15/2 with 0 false upgrades after a context fix — an lfm2.5 probe initially false-upgraded "Energy Saver 12" because its `context` ended in the trailing "Solar Buyback" ribbon that the DOM positions on the *next* card; the ribbon is now stripped from card context, root-caused via a diagnostic prompt to the model itself); 61 tests + 1 skipif-gated live-Ollama test, ruff clean. Octopus's `render()` (ESI-ID flow) is not yet live-validated (the solar/EV/thermostat qualification checkboxes are intentionally omitted — obfuscated classes, and they don't gate which EFLs appear). Ambit's PDFGenerator endpoint also 403s plain-httpx downloads (WAF) — open its 2 EFLs via browser; a stealth `render()` (real-Chrome persistent profile) is a possible follow-up. Champion's harvester was validated live 2026-07-23 (harvested all 6 plans + EFL popup URLs cleanly; the per-plan modal closes with "Close", not the "Close this dialog" interstitial). Wired into `refresh_market_data` 2026-07-23 as an optional, checkbox-gated stage (`app/common._run_rep_discovery`: per-REP dispatch by config shape — harvester/render/manual-capture — with independent per-REP error tolerance, then `download_discovered` → `data/efl/` → `parse_downloaded_efls` → auto-promote; Plans page adds the checkbox, a discovery-ZIP input, a help tooltip covering the Playwright install / Ambit-style manual captures / Octopus ESI-ID secret, and a per-retailer status table). Frontier Utilities added 2026-07-23: runs the SAME Vistra/eflviewer enrollment platform as Gexa, so `extract_gexa`/`extract_frontier` were refactored to delegate to a shared `_extract_eflviewer_platform` (identical `plan-list-padding` cards, `<h3>` names, `eflviewer.aspx?prodcode` EFL links hosted at eflviewer.frontierutilities.com, `Plan Type: Solar Buyback` self-label, and the same next-card `.Product-tab` ribbon-bleed stripping); its plans page is reached directly via `/Home/Index?Zip=<zip>` (JS-injected cards, so `_frontier_render` navigates there and waits for the EFL links), validated live 12 plans/2 buyback (Sun Confidence 12 + Battery Awards 12). Frontier's `newenroll` subdomain robots.txt is a blanket `Disallow: /` ("Stop indexing of all content"); per the user's decision a single rate-limited render of their own shopping page is treated as outside that crawler-indexing intent, so its RepConfig sets the new `check_robots=False` field (threaded into `fetch_rendered_html`/`harvest_live` via the discovery dispatch) -- a deliberate per-REP override, never a blanket default. Direct Energy added 2026-07-23: `shop.directenergy.com` React SPA reached by the ZIP URL (`?zipCode=`, capital C) + a residential/"not moving" prelude; EFL is a client-generated `blob:` PDF, but the browser first fetches it from a stable backend endpoint `api-oam.directenergy.com/api/docs/files/<id>.pdf` (plain application/pdf, httpx-downloadable) which `_direct_energy_harvest` captures from the network response (scoped per plan card -- a global `.first` would give every plan the first plan's URL) and stores as the efl_url; targets Direct Energy's "Direct Solar Unlimited 12/24" solar buyback plans (validated live: 2 plans, distinct EFLs, buyback 5.3c/4.8c). Its buyback credit is labeled "Solar Grid Credit" -- added to the EFL parser's `_BUYBACK_LABEL` vocabulary. Reliant + Atlantex added 2026-07-23 (last two REPs; Base intentionally skipped -- battery-infrastructure requirement, multi-year payback). Reliant (`shop.reliant.com`): a *different* NRG SPA flow than Direct Energy (address autocomplete + moving/renting segmentation + a "Solar Plans" filter; hashed CSS-module classes matched by class *substring* via xpath); its "Solar Payback Match" EFL PDF comes from `myaccount.reliant.com/files/<id>.pdf`, captured from a CONTEXT-level response listener (the PDF loads in a popup, so a page-scoped expect_response misses it). Reliant's buyback is RTW (ERCOT 15-min RTSPP floored at 0), which required widening the parser's RTW-signal window (the RTSPP prose sits ~400 chars after the "Solar Grid Credit" label) using only *specific* market tokens (RTSPP/settlement-point/real-time-market -- never bare "real-time"/"ERCOT", which appear in unrelated EFL prose/boilerplate). Atlantex (`enroll.atlantexpower.com`, ASP.NET): its "Solar Buy Back Plan" is promoCode-gated (`?promoCode=tpgsolar`); EFL is a direct `efl.aspx` PDF captured the same way -- but Atlantex's PDF has a broken embedded font that drops letters, so the text parses poorly (lands needs_review for manual fix); discovery/harvest still delivers the PDF. |
| integration/validation | #7 | TODO | lead |

## 11. Open questions / decisions log

- Load zone: **RESOLVED 2026-07-25 — it is `LZ_SOUTH`, not the `LZ_NORTH`
  default.** Confirmed independently by an ESID lookup (electricityplans.com
  reports load zone "south" for the premise) and by Tesla's own plan page, which
  redirects a 78665 lookup to `view-plans?loadZone=SOUTH&tdsp=ONCOR`. County is
  NOT a reliable proxy: the ERCOT map splits Williamson County across SOUTH,
  NORTH, AEN and LCRA. Set in the gitignored `data/config.yaml` (`load_zone`),
  since it is premise-specific — anyone else running this must look up their
  own. Effect is confined to RTW-indexed plans, but hits all of them: every RTW
  plan in the database improved by $30.43/yr on the switch (identical because
  they all price exports at multiplier 1.0 / adder 0, so the delta is just
  `sum(export_kwh) x (south - north price)`). LZ_NORTH and LZ_SOUTH correlate
  only 0.79 with a mean absolute difference of 0.84c/kWh, so the two are not
  interchangeable even though their annual means are within 0.02c.
- Oncor tariff history seeded with only two points (see `tdu/oncor.yaml`);
  user updates on Oncor rate changes (Mar/Sep).
- Seed plans from the July 2026 report carry `source: report-2026-07` and are
  for engine validation; live shopping requires refreshed EFLs.
- Battery simulation: out of scope v1. Taxes: excluded by design.
- **TODO — make home features a setting, not an assumption (not urgent).**
  Today "this premise has rooftop solar" is baked in everywhere: the whole app
  is built around export, and `Plan.excludes_solar` hides plans whose REP won't
  sell to a solar home (TXU Free Nights & Cool Summer 12 is the only one in 243
  EFLs). Doug wants to share this with a friend whose setup is unknown, which
  breaks that assumption. Scope: checkboxes for **solar / EV / battery** (in
  `data/config.yaml` alongside `load_zone`, surfaced on a settings or Usage
  page), then use them for eligibility rather than hardcoding. Note the same
  TXU footnote excludes EV and battery owners too, so the parser should record
  *which* features disqualify a plan (`excludes_solar` generalises to an
  `excludes: [solar, ev, battery]` list) rather than collapsing them to one
  boolean. `Plan.ev_free_charging` is already an EV-only feature that is dead
  weight for a household without one.

## 12. Running the app

```
pip install -e ".[dev]"           # if not already done
streamlit run src/energyanalyzer/app/Home.py
```

Opens at `http://localhost:8501`. Pages (left sidebar): Home, Usage, Plans,
Compare, Export -- see §9 for what each does.

Data files (all gitignored, all local-only):

- `data/IntervalData*.csv` or `data/GreenButton*.xml` -- your SMT/Green Button
  interval export(s). Upload via the **Usage** page (writes into `data/` and
  reloads automatically), or drop the file(s) in by hand before launching.
  `data/intervals.parquet` is an auto-managed cache; delete it to force a
  re-parse.
- `data/ercot/` -- ERCOT RTM settlement price files (XLSX or 12301 CSV), only
  needed for RTW-indexed plans (see §7). `data/ercot/<zone>.parquet` is the
  cache. The Home page shows whether prices for the configured zone
  (`data/config.yaml`'s `load_zone`, default `LZ_NORTH`) are present.
- `data/ptc/` -- Power to Choose CSV snapshots (Plans page, "Load Power to
  Choose snapshot" section).
- `data/efl/` -- downloaded/uploaded EFL PDFs (Plans page, "Import from EFL
  PDF" and PTC download-EFLs button).
- `data/meterplan/` -- meterplan.com solar buyback plan index markdown
  snapshots (Plans page, "Meterplan solar plan index" section).
- `plans/*.yaml` -- the plan database (git-versioned); `plans/drafts/*.yaml`
  holds unpromoted EFL-parser/meterplan drafts.

The app never requires network access: every fetcher (ERCOT prices, Power to
Choose CSV, EFL downloads, meterplan.com) degrades to a clear manual-download
message (surfaced in the UI) if network calls fail or the relevant files aren't
present yet -- only RTW-indexed plans are skipped (with a warning) when
ERCOT prices are unavailable; everything else works from the interval CSV
and plan YAMLs alone.

Excel export (`report/excel.py`, `build_workbook`) is dependency-light
(xlsxwriter only, no Streamlit import) and unit-tested directly in
`tests/test_excel.py` -- it can also be called from a plain Python script
without running the app at all.
