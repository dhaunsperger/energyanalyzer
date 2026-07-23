# Benchmark archive — Texas Power Guide report, July 2026

This is a **frozen regression baseline** for
`tests/test_engine.py::test_integration_report_benchmarks`, which validates the
billing engine (`engine/cost.py`, ARCHITECTURE.md §6) against the dollar figures
in the July 2026 Texas Power Guide report (ARCHITECTURE.md §1).

Those figures are only reproducible against the exact inputs that produced them,
so **every input the benchmark depends on is pinned here** and the test reads
them from this folder — never from the live, mutable project state. Refreshing
your usage data, adding a new Oncor tariff, or pruning the live `plans/` database
will **not** move these numbers, so a failure means the engine actually changed.

## Contents

| File | What it pins | Tracked in git? |
|---|---|---|
| `plans/*.yaml` | The 7 `source: report-2026-07` benchmark plans | yes |
| `oncor.yaml` | Oncor delivery tariff; the test uses the latest record ($4.06/mo + 6.12¢/kWh, effective 2026-03-01), matching the report | yes |
| `IntervalData.csv` | The exact 12 months of SMT interval data (import 11,278 kWh / export 9,803 kWh, Jul 2025–Jun 2026) behind the report | **no** — private usage data, gitignored like `data/` |

Because `IntervalData.csv` is gitignored (privacy), the benchmark test is
`skipif`-guarded on its presence: it runs on the owner's machine where the file
exists, and skips cleanly on fresh clones / CI.

## Do not edit

Treat this folder as read-only. If a benchmark input legitimately changes (e.g.
the report is re-run against new data), update the expected dollar figures in the
test **and** refresh the pinned inputs here in the same commit, and note it in
ARCHITECTURE.md §10/§11.
