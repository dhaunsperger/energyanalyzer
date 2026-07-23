# data/ — your private inputs (never committed)

Everything in this folder except this README is gitignored on purpose:
interval files contain your ESIID and usage patterns. After cloning, create
the files below yourself (or let the app's Usage page uploader do it).

```
data/
  IntervalData.csv     ← SmartMeter Texas 15-min interval export (CSV report
                          with both Consumption and Surplus Generation rows).
                          Any file matching IntervalData*.csv is picked up.
  GreenButton*.xml     ← optional alternative: SMT Green Button XML export(s)
  intervals.parquet    ← cache, created automatically
  ercot/               ← ERCOT price history for real-time-wholesale plans:
                          "Historical RTM Load Zone and Hub Prices" XLSX
                          (product NP6-785-ER, one file per year) and/or
                          report-12301 SPPHLZNP6905 CSVs. Parquet cache is
                          created alongside.
  efl/                 ← EFL PDFs downloaded by the Power to Choose fetcher
  ptc/                 ← Power to Choose CSV snapshots
  config.yaml          ← optional settings, e.g.  load_zone: LZ_NORTH
```

Getting the inputs:
- SmartMeter Texas: smartmetertexas.com → Reports → interval data (CSV),
  12 months, both consumption and surplus generation.
- ERCOT prices: https://www.ercot.com/mp/data-products/data-product-details?id=NP6-785-ER
  — download the year(s) covering your interval data into `data/ercot/`.
