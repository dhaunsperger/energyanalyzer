# EnergyAnalyzer — Architecture & Agent Coordination Doc

**Audience:** Claude subagents implementing modules, and the human owner (Doug).
**Maintainer:** Lead architect session. Subagents: read this whole file before coding.
Update the *Status Board* section when you finish a module; do not change design
decisions without noting an open question at the bottom.

## 1. What this app does

Replicates the "Texas Power Guide" solar electric plan analysis service:

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

`fetchers/rep_discovery.py`: solar **buyback** EFL discovery on individual REP
marketing sites — the plans PTC and meterplan.com both miss (see
`rep_discovery_handoff.md`). REP sites are client-rendered SPAs, so
`fetch_rendered_html` drives a real browser via Playwright (optional dep:
`pip install 'energyanalyzer[discovery]' && playwright install chromium`;
lazily imported, raises a clear install/manual-fallback message when absent).
Extraction is **two-tier, deterministic-first** (mirrors `eflparse`'s
philosophy): `discover()` runs a per-REP static extractor first
(`extract_green_mountain` reads the site's own self-labeling — an explicit
`<a>Electricity Facts Label</a>` per plan joined by normalized name to the
hidden analytics div whose `analyticscontractrates="...^BuyBack:<rate>"` flags
buyback plans — NO LLM), and only falls back to `classify_link_llm` (local
Ollama, `lfm2.5`, JSON-forced, fed real link text + page context, never a bare
URL; best-effort, tolerated if the server is down) for sites that don't
self-label. `download_discovered` downloads matched EFLs (buyback-only by
default) into `data/efl/` and appends a per-download manifest
(`data/efl/rep_discovery_manifest.jsonl`: retailer, plan_name, source_url,
discovered_at, extraction_method, llm_confidence, is_buyback, buyback_ckwh) so
downstream dedup/refresh can distinguish REP-discovered plans. Offline-first
(static extractor + classifier run on rendered HTML on disk;
`tests/fixtures/rep_green_mountain_sample.html` is the format reference and
static-extractor fixture). Per §9 this is **not** wired into
`refresh_market_data` yet — Green Mountain works standalone first; adding more
REPs (each needs its flow recorded with `playwright codegen` + a rendered-HTML
sample) is a follow-up.

## 8. EFL static parser (Task 5) — NO LLM calls

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

Launch: `streamlit run src/energyanalyzer/app/Home.py`.

## 10. Status board  (update when you finish; keep one line each)

| Module | Task | Status | Notes |
|---|---|---|---|
| core models + plans_io + seeds | #1 | DONE (lead) | schema is the contract |
| ingest | #2 | DONE | CSV position-based DST handling + GreenButton merge; parquet cache |
| engine | #3 | DONE | simulate()/rank() implemented per §6; validated against real CSV (see open Q below re: TDU during free windows). Report-benchmark regression (`test_integration_report_benchmarks`) now reads ALL its inputs -- the 7 `report-2026-07` plan YAMLs, the Oncor tariff, and the interval CSV -- from a frozen archive (`tests/fixtures/benchmark_2026_07/`, see its README; CSV gitignored/private, test skips when absent) so refreshing live usage data / tariffs / plans can't move the expected dollars. |
| prices + fetchers | #4 | DONE | ercot.py: xlsx (NP6-785-ER) + 12301 CSV shapes, parquet cache; ptc.py: fuzzy-column loader, filter_plans, download_efls. Downloaders (download_prices/fetch_ptc_csv) untested live (ercot.com/powertochoose.org blocked in sandbox); manual-download fallback documented in errors. |
| eflparse | #5 | DONE | static regex/heuristic parser + 6 synthetic/pulse fixtures + 15-file real Texas EFL corpus regression suite (tests/fixtures/efl_texts/real/), 175 tests green; hardened against corrupted/PUA-encoded fonts, bullet/numbered-list/colon layouts, brand-prefixed TOU tables, per-day prepaid fees, and bundled-TDU phrasing; pdfplumber import-failure noise silenced |
| app + excel | #6 | DONE | Streamlit app (Home + 4 pages) + report/excel.py; 3 tests green in tests/test_excel.py; validated end-to-end against real data/IntervalData.csv + plans/*.yaml (pulse_current=$1031.37, txu_solar_bb=$1211.37, gmtn_pollution_free_nights=$1264.87 -- all within a few cents of report benchmarks) |
| app + fetchers followup | #6/#4 | DONE | fixed 3 user-reported Plans-page issues: `fetchers.ptc.filter_plans` gained a backward-compatible `language="English"` default filter + snapshot TDU picker in the UI (was reading as truncation, was actually TDU+Spanish-duplicate filtering); `download_efls` gained `progress_callback` wired to `st.progress`; new `app/common.parse_downloaded_efls` batch-parses `data/efl/*.pdf` into `plans/drafts/` (per-file try/except, skip-if-already-parsed) plus a "Draft plans" review/edit/promote UI -- promote/delete both invalidate the plans cache and `st.rerun()` so the main table updates immediately; 183 tests green (`pytest tests/`), plus manual `streamlit.testing.v1.AppTest` smoke passes on the Plans page across empty and populated states |
| app followup 2: refresh + staleness | #6/#4 | DONE | new `app/common.refresh_market_data` (delete old ptc/efl:-sourced plans+drafts+EFLs+stale snapshots -- never manual/report-*/current-plan -- then fetch-or-fallback → load+filter → download → parse → auto-promote drafts with needs_review=False and all load-bearing confidences >=0.8, stamping `retrieved`) wired to a confirmation-gated "Refresh market data" button + one progress bar with staged labels on the Plans page; new staleness helpers (`interval_staleness_warning`, `price_coverage_warning`, `tdu_staleness_warning`, `plan_is_stale`/`stale_plan_ids`) surfaced as warnings + a "Stale?" table column on Compare; single-draft promote also stamps `retrieved`; 190 tests green (`pytest tests/`, incl. new tests/test_refresh.py), ruff clean, manual AppTest smoke green on both Plans (checkbox-gated button, full refresh pipeline with faked transport, promote/delete) and Compare (staleness warnings + Stale? column render against the real data/IntervalData.csv + plans/*.yaml) |
| meterplan.com solar plan index | #4/#6 | DONE | new `fetchers/meterplan.py` (fetch_meterplan/load_meterplan/filter_meterplan/meterplan_to_drafts, offline-first, tests/fixtures/meterplan_sample.md as format reference); covers solar buyback plans (mostly non-Oncor TDUs) PTC's export lacks -- their "Estimated annual cost" column is never read, only rates; drafts get a `_parse` confidence/evidence block like eflparse, battery-required rows skipped, free-hours-named plans get an assumed 9pm-6am two-rate structure at low confidence + needs_review, deduped by (retailer, plan, term) against plans already in the database; wired into `app/common.refresh_market_data` as a new stage between EFL parsing and auto-promote (same fetch-or-fallback-to-newest-disk-snapshot pattern, tolerated gracefully if unavailable) and into a new "Meterplan solar plan index" subsection on the Plans page (fetch/snapshot-picker/TDU-filter/import-as-drafts, feeding the existing drafts review/promote UI unchanged); from the committed fixture, filtering to Oncor produces 30 imported / 0 skipped-battery / 0 skipped-existing / 10 flagged-for-review (all schema-valid via Plan.model_validate); 214 tests green (`pytest tests/`, incl. new tests/test_meterplan.py + extended tests/test_refresh.py), ruff clean, manual AppTest smoke green on the Plans page (empty + populated meterplan states, load/filter/import-as-drafts) with data/ and plans/drafts/ left with no git residue afterward |
| rep_discovery (REP-site EFL discovery) | #4 | DONE (Green Mountain) | new `fetchers/rep_discovery.py`: two-tier (static self-label extractor + Ollama `lfm2.5` JSON fallback) discovery of solar buyback EFLs on REP marketing sites PTC/meterplan miss; Playwright live fetch behind optional `[discovery]` extra; `download_discovered` → `data/efl/` + jsonl manifest. Green Mountain static extractor validated against a real rendered-HTML capture (11 plans, 2 buyback self-labeled correctly); 17 tests + 1 skipif-gated live-Ollama test in tests/test_rep_discovery.py, ruff clean. NOT yet wired into `refresh_market_data` (per §7/§9); more REPs need their flow recorded first. |
| integration/validation | #7 | TODO | lead |

## 11. Open questions / decisions log

- Load zone for Round Rock/Oncor assumed `LZ_NORTH` — confirm against ESIID
  premise; configurable in `data/config.yaml` (`load_zone`).
- Oncor tariff history seeded with only two points (see `tdu/oncor.yaml`);
  user updates on Oncor rate changes (Mar/Sep).
- Seed plans from the July 2026 report carry `source: report-2026-07` and are
  for engine validation; live shopping requires refreshed EFLs.
- Battery simulation: out of scope v1. Taxes: excluded by design.

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
