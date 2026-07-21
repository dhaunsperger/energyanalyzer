# EnergyAnalyzer

Ranks every available Texas retail electric plan by what it would actually
cost **your house**, using your real 15-minute SmartMeter Texas interval data
(solar import *and* export). Replicates the "Texas Power Guide" style
analysis: free-nights/weekends plans, time-of-use, solar buyback variants
(1:1, partial, real-time wholesale), non-offsettable charges, credit caps,
Oncor delivery charges — simulated month by month, ranked by first-year net
bill, with an Excel export.

Validated against the commercial report it replicates: current plan computes
$1,031.37 vs. the service's $1,031; six other benchmark plans within a few
dollars. See `ARCHITECTURE.md` for design and internals.

## Quick start (fresh machine)

```bash
git clone -b claude/session-613kp5 https://github.com/dhaunsperger/energyanalyzer.git
cd energyanalyzer
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -q          # optional sanity check (~215 tests)
streamlit run src/energyanalyzer/app/Home.py
```

Requires Python ≥ 3.11. For best EFL parsing also install poppler
(`pdftotext`): `sudo apt install poppler-utils` / `brew install poppler`
(there is a pure-Python fallback, but poppler handles more PDFs).

## Data you supply (all private, all gitignored — see data/README.md)

| File | What | Where to get it |
|---|---|---|
| `data/IntervalData.csv` | 12 months of 15-min usage, BOTH channels (Consumption + Surplus Generation) | smartmetertexas.com → Reports → interval CSV. Or upload via the app's Usage page. |
| `data/ercot/*.xlsx` | Real-time wholesale price history (needed only for RTW plans) | ercot.com, product NP6-785-ER "Historical RTM Load Zone and Hub Prices" — the yearly XLSX file(s) covering your usage window |
| `data/config.yaml` | Optional: `load_zone: LZ_NORTH` (default; correct for Oncor/Round Rock) | — |

## Coming back after months? The refresh ritual

1. **Fresh usage**: download a new 12-month interval CSV from SMT → replace
   `data/IntervalData.csv`.
2. **Fresh prices**: re-download the current-year ERCOT XLSX into `data/ercot/`.
3. **Oncor tariff** (changes every March and September): if the newest entry in
   `tdu/oncor.yaml` is stale (the app warns), append a new
   `{effective, fixed_usd_month, volumetric_ckwh}` entry — don't edit old ones.
4. Launch the app → **Plans page → Refresh market data** (one button):
   re-fetches the Power to Choose snapshot and the meterplan.com solar index,
   downloads + parses EFLs, auto-promotes trustworthy plans, and queues
   uncertain ones as drafts.
5. Review the **draft queue** (plans with uncertain structure — free-hour
   windows, wholesale exports — show their parse evidence; confirm against the
   linked EFL and promote).
6. Read the **Compare page**. Heed the staleness warnings; plans marked ⚠️
   haven't been refreshed in 90+ days. Rankings marked ‡ use trailing
   wholesale prices (reference, not a guarantee).
7. **Excel export** page if you want the workbook.

## Where plan data comes from

- **Power to Choose** (PUCT's site): bulk CSV of the conventional market —
  automated. Solar buyback plans are largely NOT listed there.
- **meterplan.com solar index**: hourly-updated table of Texas solar buyback
  plans — automated. Published by a competing REP (Meter Energy), so only its
  rate columns are used; all costs are computed locally by our engine.
- **EFL PDFs / manual entry**: for anything else. Upload an EFL on the Plans
  page (parsed with per-field confidence + evidence for your review), or edit
  the YAML files in `plans/` directly — the schema is documented in
  `ARCHITECTURE.md` §5.

Plan YAMLs in `plans/` are the database — git-versioned, human-editable.
Hand-entered plans are never touched by refresh; auto-imported ones
(`source: ptc` / `efl:*` / `meterplan`) are replaced each refresh.

## Trusting a result before you switch

Always open the plan's actual EFL (linked in the plan YAML / drafts) and
confirm the load-bearing details the indexes can't see: exact free-hour
windows, whether credits can offset base/TDU charges ("not offsettable"),
buyback caps, and minimum-usage fees. The app flags what it isn't sure about
(`needs_review`), but the EFL is the contract.

## Troubleshooting

- **App shows no plans / no usage**: check the Home page status tiles; each
  missing input shows exact download instructions.
- **RTW plans missing from rankings**: ERCOT files in `data/ercot/` don't
  cover the usage window — the Compare page lists the skipped plans.
- **A plan's number looks wrong**: open its monthly breakdown on Compare, then
  its YAML in `plans/` — most discrepancies are plan-structure details
  (windows, offset scope), not engine math (the engine is benchmark-tested in
  `tests/test_engine.py`).
- **Numbers vs. reality drift**: taxes and the PUC assessment are excluded by
  design (the commercial report excludes them too); rankings are unaffected.
