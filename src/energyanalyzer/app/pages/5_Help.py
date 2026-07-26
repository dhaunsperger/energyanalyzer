"""Help page (ARCHITECTURE.md §9): in-app usage documentation.

Explains the four-page workflow, the plan-data sources and the "Refresh market
data" pipeline, the key concepts behind the numbers, and troubleshooting -- with
a small live "your setup" panel so the docs reflect the current state. Written
to absorb the operational knowledge that otherwise lives only in tooltips.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from energyanalyzer.app.common import (  # noqa: E402
    DATA_DIR,
    DRAFTS_DIR,
    EFL_DIR,
    METERPLAN_DIR,
    PTC_DIR,
    REP_DISCOVERY_DIR,
    efl_pdf_health,
    get_intervals,
    get_load_zone,
    get_plans,
    try_get_prices,
)

st.set_page_config(page_title="EnergyAnalyzer - Help", page_icon="⚡", layout="wide")
st.title("Help")
st.caption(
    "How EnergyAnalyzer works, where its plan data comes from, and how to fix the common "
    "snags. Everything stays on this machine unless you explicitly fetch a snapshot or an EFL."
)

overview_tab, pages_tab, data_tab, concepts_tab, trouble_tab = st.tabs(
    ["Overview", "Page guide", "Data & refresh", "Concepts", "Troubleshooting"]
)

# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
with overview_tab:
    st.markdown(
        """
EnergyAnalyzer performs a professional-grade solar-electric plan analysis: it simulates
a full year of electricity bills for every plan in your database against **your own**
15-minute interval usage, then ranks them by first-year net cost.

The workflow runs across four pages (sidebar):

1. **Usage** — load 12 months of SmartMeter Texas interval data (grid import + solar export)
   and review its quality and patterns.
2. **Plans** — maintain the plan database: enter plans by hand, import EFL PDFs, or pull a
   Power to Choose snapshot / meterplan index / retailer-site discovery in one **Refresh**.
3. **Compare** — simulate a year of bills for every plan and rank by net cost.
4. **Export** — download an Excel workbook with the full ranking and supporting detail.

The billing engine always computes costs itself from your actual intervals — it never uses
any retailer's advertised "estimated annual cost."
"""
    )

    st.divider()
    st.subheader("Your current setup")
    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown("**Interval usage data**")
        try:
            df, report = get_intervals(DATA_DIR)
            st.metric("Intervals loaded", f"{len(df):,}")
            if report.start is not None:
                st.caption(f"{report.start} → {report.end}")
            if report.warnings:
                st.caption(f"{len(report.warnings)} quality warning(s) — see the Usage page.")
        except Exception:  # noqa: BLE001 -- Help page must render even with no data
            st.info("Not loaded — start on the **Usage** page.")

    with c2:
        st.markdown("**Plan database**")
        try:
            plans = get_plans()
            n_review = sum(1 for p in plans if p.needs_review)
            st.metric("Plans", f"{len(plans)}")
            health = efl_pdf_health(EFL_DIR)
            n_drafts = len(list(DRAFTS_DIR.glob("*.yaml"))) if DRAFTS_DIR.exists() else 0
            st.caption(
                f"{health['total']} EFL PDF(s) on disk"
                + (f" ({len(health['invalid'])} unreadable)" if health["invalid"] else "")
                + f" · {n_drafts} draft(s) pending"
                + (f" · {n_review} flagged needs_review" if n_review else "")
            )
        except Exception:  # noqa: BLE001
            st.info("No plans yet — add some on the **Plans** page.")

    with c3:
        zone = get_load_zone()
        st.markdown(f"**ERCOT prices ({zone})**")
        prices, err = try_get_prices(zone)
        if prices is not None and not prices.empty:
            st.metric("Price points", f"{len(prices):,}")
            st.caption("Needed only for real-time-wholesale (RTW) plans.")
        else:
            st.info("Not loaded — only RTW-indexed plans need these.")

    st.caption("These numbers are live; the guidance below is the same regardless of state.")

# --------------------------------------------------------------------------- #
# Page guide
# --------------------------------------------------------------------------- #
with pages_tab:
    st.markdown("What each page does and how to use it.")

    with st.expander("Usage — load and review your interval data"):
        st.markdown(
            """
- **Upload** a SmartMeter Texas (SMT) interval **CSV** export, or a NAESB **Green Button XML**.
  Multiple files merge automatically; click **Save & reload** to persist them into `data/`.
- **Data quality** flags gaps, duplicates, and coverage so you know whether a full year is present.
- Charts: **monthly import/export**, an **hour-of-day × month** net-power heatmap, and the
  **day (6a–6p) / peak (6p–9p) / night (9p–6a)** annual split that drives time-of-use plans.
- Get your CSV from the SmartMeter Texas portal (smartmetertexas.com): request the 15-minute
  interval export for the last 12 months.
"""
        )

    with st.expander("Plans — maintain the plan database"):
        st.markdown(
            """
Three ways to add plans:

- **Manual entry / edit** — the form at the bottom; use it to fix a draft the parser flagged,
  or to model a plan by hand (including special structures like a Tesla-style EV free-charging
  window).
- **EFL PDF import** — upload an Electricity Facts Label; the parser reads rates, base charge,
  buyback terms, and TDU pass-through, scoring each field's confidence.
- **Refresh market data** — the one-button pipeline (see the **Data & refresh** tab) that
  pulls Power to Choose, the meterplan index + Meter's real EFLs, and (optionally) per-retailer
  site discovery, then auto-promotes the confident results.

Below the database you'll see the **plan count, EFL-PDF count (and any unreadable ones)**, and
**draft plans** awaiting review. The plan database is a git-versioned folder of YAML files
(`plans/*.yaml`); the **commit & push** control saves your curated database to the repo.
"""
        )

    with st.expander("Compare — rank every plan against your usage"):
        st.markdown(
            """
Simulates a year of bills for each plan on your actual intervals and ranks them by first-year
net cost. Watch for:

- **Staleness warnings** — old interval data, thin ERCOT price coverage, an aging TDU tariff, or
  plans with rate data >90 days old (marked **Stale?**). Re-run **Refresh** to freshen plans.
- **RTW-indexed plans are skipped if ERCOT prices aren't loaded** (they need real-time prices to
  value exports) — the page tells you when that happens.
- **Monthly detail** breaks a selected plan down month by month.
"""
        )

    with st.expander("Export — download the full workbook"):
        st.markdown(
            "Produces an Excel workbook with the complete ranking plus supporting detail, so you "
            "can share or archive the comparison outside the app."
        )

# --------------------------------------------------------------------------- #
# Data & refresh
# --------------------------------------------------------------------------- #
with data_tab:
    st.markdown(
        """
Plan data comes from three sources, combined by the **Refresh market data** button on the Plans
page. Refresh is confirmation-gated and shows one progress bar with staged labels.
"""
    )

    with st.expander("The three plan sources"):
        st.markdown(
            """
- **Power to Choose (PTC)** — the state's bulk plan export. Broadest coverage; the baseline.
- **meterplan.com** — an hourly-regenerated index of Texas **solar buyback** plans that PTC's
  export under-covers. Two parts: (a) a markdown **rate index** for competitor plans (used as
  rates only — never its "estimated cost" column), and (b) Meter Energy's **own real EFLs**,
  pulled from its `/plans` page (presigned links, valid ~7 days) and parsed like any EFL.
- **Retailer-site discovery** (optional, slow) — drives a real browser across 11 retailer sites
  (Green Mountain, TXU, Chariot, Gexa, Frontier, Octopus, Champion, Direct Energy, Reliant,
  Atlantex, Ambit) to capture EFLs the aggregators miss. It now pulls **all** plans it finds,
  skipping ones PTC already carries.
"""
        )

    with st.expander("What a refresh does, step by step"):
        st.markdown(
            """
1. **Delete** stale auto-imported plans/drafts/EFLs (never your manual or report-seed plans, or
   the current plan).
2. **Fetch** a fresh PTC snapshot (falls back to the newest on disk if the network is down).
3. **Load + filter** it to your TDU.
4. **Download** each plan's EFL PDF.
5. **Parse** every EFL into a draft (with per-field confidence scores).
6. **Meter EFLs** then the **meterplan index**: fetch Meter's own real EFLs; import the markdown
   competitor rows as drafts (Meter's own rows are dropped when its real EFLs were fetched).
7. *(optional)* **Retailer-site discovery** if the checkbox is on.
8. **Auto-promote** every draft the parser was confident about (`needs_review=False` **and** all
   load-bearing fields scored ≥ 0.8). Anything less is left as a draft for you to review.
9. **Supersede**: remove a synthetic meterplan plan once a real EFL (from PTC, discovery, or
   Meter) — or a manual entry — covers the same plan. A synthetic is never promoted over a real
   plan, including one still sitting in the draft queue.

Tick **"Pre-fill unreadable fields with the local LLM"** before starting if you have Ollama
running. A refresh re-parses every EFL from scratch, so any hand-fix you made to a
broken-font PDF is lost each time — the LLM tier is what makes those stick without retyping.

**It runs in the background.** You can navigate away, or close the tab, and it keeps going —
progress is written to disk as it goes, so the Plans page always shows where it got to, even
after restarting the app.
"""
        )

    with st.expander("A refresh was interrupted / there's no summary"):
        st.markdown(
            """
If a refresh is cut short (app restarted, machine slept, an older version killed by navigating
away), the Plans page shows a banner naming the stage it stopped in, plus a **Finish incomplete
refresh** button.

Downloading and parsing write their drafts to disk as they go, so the database can be completed
locally — the button promotes every confident draft and supersedes what's covered. **It does not
re-download anything**, so it takes seconds rather than repeating the whole fetch.

The one thing it can't rebuild is **retailer-site discovery**: retailers the sweep never reached
have no drafts on disk, so those plans need a fresh run with the discovery checkbox on. If solar
buyback plans look missing afterwards, that's why.
"""
        )

    with st.expander("Enabling retailer-site discovery (Playwright)"):
        st.markdown(
            """
Discovery drives a headless browser, so it needs the optional extra:

```
pip install 'energyanalyzer[discovery]' && playwright install chromium
```

It's **off by default** and slow — each site is a live browser session and every EFL it finds is
downloaded. While it runs, open the **Discovery console (live)** panel to watch each step in real
time (ZIP entry, per-plan EFL capture, downloads), so you can tell a slow site from a stuck one;
the full log is kept after the run too. Per-retailer results (and any that need a manual capture)
show up under the button when it finishes.
"""
        )

    with st.expander("Manual captures & the ESI-ID secret"):
        st.markdown(
            """
- **Ambit** blocks automation, so its plans page must be saved by hand as
  `data/rep_discovery/ambit_<timestamp>.html`. Discovery parses the **newest** `ambit_*.html`,
  so use a sortable UTC timestamp when you rename the saved file:

```
mv ambit_rendered.html "ambit_$(date -u +%Y%m%dT%H%M%SZ).html"
```

  Any retailer shown as **manual-needed** is captured the same way.
- **Octopus** needs your ESI ID (its ZIP can span load zones). Put it in the **gitignored**
  `data/rep_discovery_secrets.yaml` under `octopus:` — it is never committed or logged, and
  your address/ESI ID must never go into a tracked file.
"""
        )

# --------------------------------------------------------------------------- #
# Concepts
# --------------------------------------------------------------------------- #
with concepts_tab:
    st.markdown("The terms behind the numbers.")

    st.markdown(
        """
- **EFL (Electricity Facts Label)** — the standardized PDF each Texas plan must publish: rates,
  base charge, term, buyback terms, TDU pass-through. The parser turns these into plan YAML.
- **Buyback (solar export credit)** — how a plan pays for energy you export:
  - **none** — no export credit.
  - **fixed** — a flat ¢/kWh credit.
  - **RTW (real-time wholesale)** — pays the ERCOT real-time settlement price (RTSPP), which
    varies every 15 minutes and is floored at 0. **These plans need ERCOT prices loaded**, or
    Compare skips them.
- **offset_scope** — whether export credits can offset **all charges** or **energy charges only**
  (some plans don't let credits cancel the base/TDU fees).
- **TDU pass-through** — the delivery utility's (e.g. Oncor's) regulated charges, passed through
  on top of the energy rate.
- **needs_review / auto-promote** — a parsed draft is auto-promoted into the database only when
  it isn't flagged `needs_review` **and** every load-bearing field scored ≥ 0.8 confidence.
  Everything else waits as a draft for you to check on the Plans page.
- **Promote all drafts (quick look)** — bulk-moves every draft into the database *without* the
  confidence gate, so the whole market shows up in Compare at once. Each plan keeps its
  `needs_review` flag, so unverified ones stay badged — but their numbers are the parser's
  unreviewed guess, so treat rankings that include them as provisional and check the EFL before
  acting on one. Promoting consumes the draft and its per-field confidence/evidence; re-parsing
  the source EFL brings that back, and "Demote to drafts" on the Plan detail view reverses it.
- **LLM-suggested fields** — with "Pre-fill unreadable fields with the local LLM" ticked, a local
  Ollama model proposes values for fields the parser couldn't read (mainly PDFs with broken
  embedded fonts). Such fields are badged **LLM** in the draft's confidence table with the model's
  own reasoning shown beneath. These drafts **always** stay in review: the model pre-fills the
  form to save you typing, it never decides. Optional and off by default.
- **EV free charging** — a Tesla-style capped free-charging window: the engine waives the energy
  charge on the first N kWh/month used within the plan's eligible overnight hours.
"""
    )

# --------------------------------------------------------------------------- #
# Troubleshooting
# --------------------------------------------------------------------------- #
with trouble_tab:
    with st.expander("A refresh reports EFL downloads that failed"):
        st.markdown(
            """
Almost always a host quirk, **not** a problem with your data — and none need OCR:

- **Legacy-TLS hosts** (Tara/Amigo, on the Just Energy platform) — handled automatically now
  (the downloader tolerates their old TLS).
- **SPA storefronts** (TriEagle, Express, Veteran) return an app shell instead of a PDF; the PDF
  is generated in-browser. These need a browser harvester (not yet built for those retailers).
- **Bot-challenge / captcha** (e.g. NEC Co-op) — can't be fetched headless.
- **HTML-viewer EFLs** (Octopus) aren't direct PDFs; discovery renders them, so they're reported
  as *deferred*, not failed.

Group the failing URLs by host first — the pattern usually points straight at the cause.
"""
        )

    with st.expander("A downloaded PDF won't parse / shows as unreadable"):
        st.markdown(
            """
The Plans page shows a count of unreadable EFL PDFs beneath the database. Usual causes are an
HTML error page saved as `.pdf` (see above) or a genuinely **image-only** EFL. The parser reads
text, not images — a scanned/image EFL would need OCR (e.g. `ocrmypdf`/Tesseract) before parsing.
One known data-quality case: **Atlantex**'s EFL has a broken embedded font that drops letters
(`ae Charge $19.95 per ill`), so the regex parser can't read its base charge even though the PDF
downloads fine. This is exactly what the **"Pre-fill unreadable fields with the local LLM"**
checkbox is for — a language model reads the damaged text easily. It still lands in review for
you to confirm; the suggestion just saves you retyping it.
"""
        )

    with st.expander("Compare shows staleness warnings or skips plans"):
        st.markdown(
            """
- **Stale plans / interval data / TDU tariff** — re-run **Refresh market data**, or re-load a
  fresh interval export on the Usage page.
- **RTW plans skipped** — load ERCOT real-time prices (Home page shows where) so those plans can
  be valued.
"""
        )

    with st.expander("Where things live on disk"):
        st.markdown(
            f"""
- Interval data & caches: `{DATA_DIR}`
- Plan database (git-versioned): `plans/*.yaml` · drafts (scratch): `{DRAFTS_DIR}`
- Downloaded EFL PDFs: `{EFL_DIR}`
- PTC snapshots: `{PTC_DIR}` · meterplan snapshots: `{METERPLAN_DIR}`
- Discovery captures & manifest: `{REP_DISCOVERY_DIR}`
"""
        )
