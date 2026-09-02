"""Compare page (ARCHITECTURE.md §9): rank all plans by simulated
first-year net bill, styled like the reference analysis report p.2 table."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from energyanalyzer.core.models import select_billing_window  # noqa: E402
from energyanalyzer.app.common import (  # noqa: E402
    CURRENT_PLAN_ID,
    DATA_DIR,
    get_intervals,
    get_plans,
    get_tdu,
    group_plan_siblings,
    interval_staleness_warning,
    plan_is_stale,
    price_coverage_warning,
    render_missing_data_help,
    tdu_staleness_warning,
    try_get_prices,
)
from energyanalyzer.engine.cost import rank  # noqa: E402
from energyanalyzer.report.excel import (  # noqa: E402
    plan_etf_label,
    plan_export_label,
    plan_import_ckwh_with_tdu,
    plan_other_details,
)

st.set_page_config(page_title="EnergyAnalyzer - Compare", page_icon="⚡", layout="wide")
st.title("Compare plans")

try:
    intervals, quality = get_intervals(DATA_DIR)
except FileNotFoundError as exc:
    render_missing_data_help(exc, title="No usage data yet")
    st.stop()

all_plans = get_plans()
if not all_plans:
    st.warning("No plans found in plans/*.yaml. Add some on the Plans page.")
    st.stop()

tdu = get_tdu()
prices, price_err = try_get_prices()
if price_err:
    st.caption(f"ERCOT prices not loaded (RTW-indexed plans will be skipped): {price_err}")

# --- Billing window ------------------------------------------------------- #
# A first-year cost is only comparable over one year. Two overlapping SMT
# exports merge into 13-14 calendar months, and billing all of them double-
# counts a season -- which reorders the ranking rather than just inflating it.
intervals, billing_window = select_billing_window(intervals)
if billing_window.trimmed:
    st.info(billing_window.note, icon="📅")
elif not billing_window.is_reliable:
    st.warning(billing_window.note, icon="⚠️")
else:
    st.caption(billing_window.note)

# --- Staleness warnings (ARCHITECTURE.md §9) ------------------------------- #
interval_warning = interval_staleness_warning(quality)
if interval_warning:
    st.warning(interval_warning)
if prices is not None and not intervals.empty:
    price_warning = price_coverage_warning(prices, intervals.index.max())
    if price_warning:
        st.warning(price_warning)
tdu_warning = tdu_staleness_warning(tdu)
if tdu_warning:
    st.warning(tdu_warning)
n_stale_plans = sum(1 for p in all_plans if plan_is_stale(p))
if n_stale_plans:
    st.caption(
        f"{n_stale_plans} plan(s) below have rate data more than 90 days old (or an "
        "un-refreshed report seed) -- marked 'Stale?' in the table; consider running "
        "'Refresh market data' on the Plans page."
    )

include_review = st.toggle("Include plans flagged `needs_review`", value=False, key="compare_include_review")
usable_plans = all_plans if include_review else [p for p in all_plans if not p.needs_review]
n_hidden = len(all_plans) - len(usable_plans)
if n_hidden:
    st.caption(f"{n_hidden} `needs_review` plan(s) hidden -- toggle above to include them.")

# Eligibility is not a scoring question: a REP that won't sell to a solar home
# makes its plan unbuyable here, however well it prices. These are hidden by
# default and behind their own toggle, because free-nights plans score well
# against a solar export profile and would otherwise rank near the top.
excluded_plans = [p for p in usable_plans if getattr(p, "excludes_solar", False)]
if excluded_plans:
    include_ineligible = st.toggle(
        "Include plans this home can't enroll in (rooftop solar excluded by the REP)",
        value=False,
        key="compare_include_ineligible",
    )
    if not include_ineligible:
        usable_plans = [p for p in usable_plans if not getattr(p, "excludes_solar", False)]
        st.caption(
            f"{len(excluded_plans)} plan(s) hidden as ineligible: "
            + ", ".join(f"{p.retailer} {p.name}" for p in excluded_plans[:4])
            + (" …" if len(excluded_plans) > 4 else "")
        )

results = rank(usable_plans, intervals, tdu, prices)
for w in getattr(results, "warnings", []):
    st.warning(w)

if not results:
    st.error("No plans could be simulated (see warnings above).")
    st.stop()

plans_by_id = {p.id: p for p in usable_plans}
cheapest_id = min(results, key=lambda r: r.first_year_net).plan_id

# One product, several brands. Texas retail is full of white labels -- Frontier
# Battery Awards 12, Frontier Sun Confidence 12, Gexa Battery Benefits 12 and
# Gexa Solar Buyback 12 are one NRG product with four names -- and three copies
# of the same deal crowding the top ten hides the actual alternatives. Grouped
# in the VIEW only: every plan stays in the database, because these are separate
# contracts with separate enrollment links, and one brand's price may drift from
# its siblings' later.
group_siblings = st.toggle(
    "Group identical plans sold under sibling brands",
    value=True,
    key="compare_group_siblings",
)
siblings: dict = {}
if group_siblings:
    kept_ids, siblings = group_plan_siblings([r.plan_id for r in results], plans_by_id)
    keep = set(kept_ids)
    n_collapsed = len(results) - len(keep)
    results = [r for r in results if r.plan_id in keep]
    if n_collapsed:
        st.caption(
            f"{n_collapsed} row(s) folded into the identical plan above them -- see "
            "'Also sold as'. Untoggle to list every brand separately."
        )

st.caption(
    f"Oncor TDU (effective {tdu.effective}): ${tdu.fixed_usd_month:.2f}/mo + "
    f"{tdu.volumetric_ckwh:.4f}¢/kWh. '*' = import rate not offsettable by export credits "
    "(offset_scope=energy_only). '‡' = RTW-indexed rate (ERCOT settlement prices)."
)
if any(r.prices_estimated_fraction for r in results):
    worst = max(r.prices_estimated_fraction for r in results)
    st.caption(
        f"'~' = some ERCOT prices for this window were unpublished (up to {worst:.1%} "
        "of intervals) and were estimated from recent prices at the same time of day."
    )

rows = []
for r in results:
    plan = plans_by_id[r.plan_id]
    import_ckwh, star = plan_import_ckwh_with_tdu(r.monthly, plan)
    rows.append(
        {
            "_plan_id": r.plan_id,
            "Retailer": plan.retailer,
            # '~' marks a row whose wholesale prices were partly estimated
            # (ERCOT's archive trails real time); explained in the footnote.
            "Plan": plan.name
            + (" ‡" if r.uses_rtw else "")
            + ("~" if r.prices_estimated_fraction else ""),
            "Term (mo)": plan.term_months,
            "Base $/mo": plan.base_charge_usd,
            "Import ¢/kWh (+TDU)": f"{import_ckwh:.2f}{'*' if star else ''}",
            "Export ¢/kWh": plan_export_label(plan),
            "Other Details": plan_other_details(plan),
            "ETF": plan_etf_label(plan),
            "Also sold as": ", ".join(
                f"{s.retailer} {s.name}" for s in siblings.get(r.plan_id, [])
            ),
            "1st-Year Net Bill": r.first_year_net,
            "Stale?": "⚠️" if plan_is_stale(plan) else "",
        }
    )
table = pd.DataFrame(rows).set_index("_plan_id")


def _highlight(row: pd.Series) -> list[str]:
    # Explicit dark text color pinned alongside the light background --
    # without it, dark-mode's default white text sits on these light
    # pastels and is unreadable.
    if row.name == CURRENT_PLAN_ID:
        return ["background-color: #FFF2CC; color: #1a1a1a"] * len(row)
    if row.name == cheapest_id:
        return ["background-color: #C6EFCE; color: #1a1a1a"] * len(row)
    return [""] * len(row)


styled = table.style.apply(_highlight, axis=1).format(
    {"Base $/mo": "${:.2f}", "1st-Year Net Bill": "${:,.2f}"}
)
try:
    styled = styled.hide(axis="index")
except Exception:  # noqa: BLE001 -- older pandas Styler API
    pass
st.dataframe(styled, width="stretch")
st.caption(
    "Highlighted rows: yellow = your current plan (pulse_current), green = cheapest."
)

st.divider()
st.subheader("Monthly detail")
for r in results:
    plan = plans_by_id[r.plan_id]
    with st.expander(f"{plan.retailer} — {plan.name}  (${r.first_year_net:,.2f}/yr)"):
        monthly = r.monthly
        st.dataframe(monthly, width="stretch", hide_index=True)
        fig = go.Figure()
        fig.add_bar(x=monthly["month"], y=monthly["energy_cost"], name="Energy")
        fig.add_bar(x=monthly["month"], y=monthly["base"], name="Base")
        fig.add_bar(x=monthly["month"], y=monthly["tdu"], name="TDU")
        fig.add_bar(x=monthly["month"], y=-monthly["bill_credit"], name="Bill credit")
        fig.add_bar(x=monthly["month"], y=-monthly["credit_used"], name="Export credit used")
        fig.add_scatter(
            x=monthly["month"],
            y=monthly["bill"],
            name="Total bill",
            mode="lines+markers",
            line=dict(color="black", width=2),
        )
        fig.update_layout(barmode="relative", yaxis_title="$", xaxis_title="Month", legend_title="")
        st.plotly_chart(fig, width="stretch", key=f"compare_chart_{r.plan_id}")
