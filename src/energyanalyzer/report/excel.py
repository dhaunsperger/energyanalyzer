"""Excel workbook export (ARCHITECTURE.md §9 / Task 6).

Public API:
    build_workbook(results, plans, intervals, tdu, output) -> None

Writes a 4-sheet xlsxwriter workbook mirroring the Texas Power Guide report:
    - Summary        : ranked plan table (styled like report p.2)
    - Monthly Detail : per plan x month billing components
    - Usage          : monthly import/export/net totals + hour x month
                        average-net-kW pivot (report p.1 heatmap data)
    - Plan Inputs    : flattened plan parameters (full schema dump)

Deliberately dependency-light (xlsxwriter only) and safe to call headless
(no Streamlit / display dependency) so it is easy to unit test.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Union

import pandas as pd
import xlsxwriter

from ..core.models import Plan, PlanResult, TduTariff, add_local_columns

CURRENT_PLAN_ID = "pulse_current"


# --------------------------------------------------------------------------- #
# Small plan-description helpers (shared with the Compare page's rendering
# logic conceptually, kept local here to avoid a Streamlit import from
# report/).
# --------------------------------------------------------------------------- #
def plan_import_ckwh_with_tdu(monthly: pd.DataFrame, plan: Plan) -> tuple[float, bool]:
    """Effective average import ¢/kWh for the year, energy + TDU combined.
    Returns (value, star) where star marks offset_scope == energy_only (the
    report's '*' = "not offsettable by credits")."""
    total_import = float(monthly["import_kwh"].sum())
    if total_import <= 0:
        return 0.0, plan.buyback.offset_scope == "energy_only"
    total = float(monthly["energy_cost"].sum()) + float(monthly["tdu"].sum())
    return (total / total_import) * 100.0, plan.buyback.offset_scope == "energy_only"


def plan_export_label(plan: Plan) -> str:
    bb = plan.buyback
    if bb.kind == "none":
        return "-"
    if bb.kind == "fixed":
        return f"{bb.rate_ckwh:.1f}"
    if bb.kind == "rtw":
        return "RTW (indexed)‡"
    if bb.kind == "windows":
        return "TOU windows‡" if any(r.rtw for r in bb.rates) else "TOU windows"
    return str(bb.kind)


def plan_other_details(plan: Plan) -> str:
    bits: list[str] = []
    free_labels = [
        er.label or "free window"
        for er in plan.energy_rates
        if er.rate_ckwh == 0 and er.window is not None
    ]
    if free_labels:
        bits.append(", ".join(free_labels))
    if plan.bill_credits:
        for bc in plan.bill_credits:
            hi = f"-{bc.max_kwh:g}" if bc.max_kwh is not None else "+"
            bits.append(f"${bc.credit_usd:g} credit @ {bc.min_kwh:g}{hi} kWh")
    if plan.buyback.cash_out:
        bits.append("cash-out credits")
    if not plan.tdu_passthrough:
        bits.append("TDU bundled")
    if plan.needs_review:
        bits.append("NEEDS REVIEW")
    if plan.notes:
        bits.append(plan.notes)
    return "; ".join(bits) if bits else "-"


def plan_etf_label(plan: Plan) -> str:
    if not plan.etf_usd:
        return "-"
    suffix = "/mo remaining" if plan.etf_per_month_remaining else ""
    return f"${plan.etf_usd:g}{suffix}"


# --------------------------------------------------------------------------- #
# Workbook construction
# --------------------------------------------------------------------------- #
def _make_formats(wb: "xlsxwriter.Workbook") -> dict[str, Any]:
    return {
        "title": wb.add_format({"bold": True, "font_size": 14}),
        "subtitle": wb.add_format({"italic": True, "font_size": 9, "font_color": "#555555"}),
        "header": wb.add_format(
            {
                "bold": True,
                "bg_color": "#2F5496",
                "font_color": "white",
                "border": 1,
                "text_wrap": True,
                "valign": "vcenter",
            }
        ),
        "usd": wb.add_format({"num_format": "$#,##0.00", "border": 1}),
        "usd_bold": wb.add_format({"num_format": "$#,##0.00", "border": 1, "bold": True}),
        "usd0": wb.add_format({"num_format": "$#,##0", "border": 1}),
        "num": wb.add_format({"num_format": "#,##0.00", "border": 1}),
        "num1": wb.add_format({"num_format": "#,##0.0", "border": 1}),
        "int": wb.add_format({"num_format": "#,##0", "border": 1}),
        "text": wb.add_format({"border": 1, "text_wrap": True, "valign": "top"}),
        "text_nb": wb.add_format({"text_wrap": True, "valign": "top"}),
        "current_usd": wb.add_format(
            {"num_format": "$#,##0.00", "border": 1, "bg_color": "#FFF2CC", "bold": True}
        ),
        "current_text": wb.add_format({"border": 1, "bg_color": "#FFF2CC", "text_wrap": True}),
        "cheapest_usd": wb.add_format(
            {"num_format": "$#,##0.00", "border": 1, "bg_color": "#C6EFCE", "bold": True}
        ),
        "cheapest_text": wb.add_format({"border": 1, "bg_color": "#C6EFCE", "text_wrap": True}),
        "heat_pos": wb.add_format({"bg_color": "#F8696B", "num_format": "0.00", "border": 1}),
        "heat_neg": wb.add_format({"bg_color": "#5A8AC6", "num_format": "0.00", "border": 1}),
        "heat_zero": wb.add_format({"num_format": "0.00", "border": 1}),
    }


def _write_summary(
    wb: "xlsxwriter.Workbook",
    fmts: dict[str, Any],
    results: list[PlanResult],
    plans: dict[str, Plan],
    tdu: TduTariff,
    current_plan_id: str,
) -> None:
    ws = wb.add_worksheet("Summary")
    ws.write(0, 0, "EnergyAnalyzer — Plan Ranking Summary", fmts["title"])
    ws.write(
        1,
        0,
        f"Oncor TDU (current, effective {tdu.effective}): "
        f"${tdu.fixed_usd_month:.2f}/mo + {tdu.volumetric_ckwh:.4f}¢/kWh. "
        "'*' = import ¢/kWh not offsettable by export credits (offset_scope=energy_only). "
        "'‡' = RTW-indexed rate (priced from ERCOT settlement prices).",
        fmts["subtitle"],
    )

    headers = [
        "Retailer",
        "Plan",
        "Term (mo)",
        "Base $/mo",
        "Import ¢/kWh (+TDU)",
        "Export ¢/kWh",
        "Other Details",
        "ETF",
        "1st-Year Net Bill",
    ]
    header_row = 3
    for c, h in enumerate(headers):
        ws.write(header_row, c, h, fmts["header"])

    cheapest_id = None
    if results:
        cheapest_id = min(results, key=lambda r: r.first_year_net).plan_id

    row = header_row + 1
    for r in results:
        plan = plans.get(r.plan_id)
        is_current = r.plan_id == current_plan_id
        is_cheapest = r.plan_id == cheapest_id
        usd_fmt = (
            fmts["current_usd"] if is_current else fmts["cheapest_usd"] if is_cheapest else fmts["usd"]
        )
        text_fmt = (
            fmts["current_text"]
            if is_current
            else fmts["cheapest_text"]
            if is_cheapest
            else fmts["text"]
        )

        if plan is None:
            ws.write(row, 0, r.plan_id, text_fmt)
            for c in range(1, 8):
                ws.write(row, c, "?", text_fmt)
            ws.write(row, 8, r.first_year_net, usd_fmt)
            row += 1
            continue

        import_ckwh, star = plan_import_ckwh_with_tdu(r.monthly, plan)
        import_label = f"{import_ckwh:.2f}{'*' if star else ''}"

        ws.write(row, 0, plan.retailer, text_fmt)
        name = plan.name + (" ‡" if r.uses_rtw else "")
        ws.write(row, 1, name, text_fmt)
        ws.write(row, 2, plan.term_months, text_fmt)
        ws.write(row, 3, plan.base_charge_usd, usd_fmt)
        ws.write(row, 4, import_label, text_fmt)
        ws.write(row, 5, plan_export_label(plan), text_fmt)
        ws.write(row, 6, plan_other_details(plan), text_fmt)
        ws.write(row, 7, plan_etf_label(plan), text_fmt)
        ws.write(row, 8, r.first_year_net, usd_fmt)
        row += 1

    warnings = getattr(results, "warnings", [])
    if warnings:
        row += 1
        ws.write(row, 0, "Skipped plans (missing data):", fmts["subtitle"])
        for w in warnings:
            row += 1
            ws.write(row, 0, w, fmts["text_nb"])

    widths = [18, 28, 9, 10, 16, 14, 40, 16, 14]
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    ws.freeze_panes(header_row + 1, 1)


def _write_monthly_detail(
    wb: "xlsxwriter.Workbook",
    fmts: dict[str, Any],
    results: list[PlanResult],
    plans: dict[str, Plan],
) -> None:
    ws = wb.add_worksheet("Monthly Detail")
    ws.write(0, 0, "Monthly billing components by plan", fmts["title"])

    headers = [
        "Plan ID",
        "Retailer",
        "Plan Name",
        "Month",
        "Import kWh",
        "Export kWh",
        "Energy Cost",
        "Base",
        "TDU",
        "Bill Credit",
        "Credit Earned",
        "Credit Used",
        "Rollover Out",
        "Bill",
    ]
    header_row = 2
    for c, h in enumerate(headers):
        ws.write(header_row, c, h, fmts["header"])

    row = header_row + 1
    for r in results:
        plan = plans.get(r.plan_id)
        retailer = plan.retailer if plan else ""
        name = plan.name if plan else r.plan_id
        monthly = r.monthly
        for _, m in monthly.iterrows():
            ws.write(row, 0, r.plan_id, fmts["text"])
            ws.write(row, 1, retailer, fmts["text"])
            ws.write(row, 2, name, fmts["text"])
            ws.write(row, 3, str(m["month"]), fmts["text"])
            ws.write(row, 4, float(m["import_kwh"]), fmts["num"])
            ws.write(row, 5, float(m["export_kwh"]), fmts["num"])
            ws.write(row, 6, float(m["energy_cost"]), fmts["usd"])
            ws.write(row, 7, float(m["base"]), fmts["usd"])
            ws.write(row, 8, float(m["tdu"]), fmts["usd"])
            ws.write(row, 9, float(m["bill_credit"]), fmts["usd"])
            ws.write(row, 10, float(m["credit_earned"]), fmts["usd"])
            ws.write(row, 11, float(m["credit_used"]), fmts["usd"])
            ws.write(row, 12, float(m["rollover_out"]), fmts["usd"])
            ws.write(row, 13, float(m["bill"]), fmts["usd_bold"])
            row += 1

    widths = [22, 18, 26, 10, 11, 11, 12, 9, 9, 11, 13, 12, 12, 12]
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    ws.freeze_panes(header_row + 1, 3)


def _write_usage(wb: "xlsxwriter.Workbook", fmts: dict[str, Any], intervals: pd.DataFrame) -> None:
    ws = wb.add_worksheet("Usage")
    ws.write(0, 0, "Usage summary", fmts["title"])
    df = add_local_columns(intervals)

    # --- monthly import/export/net -------------------------------------- #
    monthly = (
        df.groupby("month", sort=True)[["import_kwh", "export_kwh"]]
        .sum()
        .reset_index()
    )
    monthly["net_kwh"] = monthly["import_kwh"] - monthly["export_kwh"]

    headers = ["Month", "Import kWh", "Export kWh", "Net kWh (import-export)"]
    header_row = 2
    for c, h in enumerate(headers):
        ws.write(header_row, c, h, fmts["header"])
    row = header_row + 1
    for _, m in monthly.iterrows():
        ws.write(row, 0, str(m["month"]), fmts["text"])
        ws.write(row, 1, float(m["import_kwh"]), fmts["num"])
        ws.write(row, 2, float(m["export_kwh"]), fmts["num"])
        ws.write(row, 3, float(m["net_kwh"]), fmts["num"])
        row += 1

    day_night_start = row + 2
    _write_day_peak_night(ws, fmts, df, day_night_start)

    # --- hour x month average net kW pivot ------------------------------ #
    df = df.copy()
    df["net_kw"] = (df["import_kwh"] - df["export_kwh"]) / 0.25
    pivot = df.pivot_table(
        index="hour", columns="month", values="net_kw", aggfunc="mean", observed=True
    )
    months = list(pivot.columns)

    heat_row0 = day_night_start + 6
    ws.write(heat_row0 - 1, 0, "Hour-of-day × month average net kW (red=import, blue=export)", fmts["title"])
    ws.write(heat_row0, 0, "Hour", fmts["header"])
    for c, month in enumerate(months, start=1):
        ws.write(heat_row0, c, str(month), fmts["header"])

    vmax = float(pivot.to_numpy(na_value=0.0).max()) if pivot.size else 0.0
    vmin = float(pivot.to_numpy(na_value=0.0).min()) if pivot.size else 0.0
    scale = max(abs(vmax), abs(vmin), 1e-9)

    for r_i, hour in enumerate(pivot.index):
        rr = heat_row0 + 1 + r_i
        ws.write(rr, 0, int(hour), fmts["text"])
        for c_i, month in enumerate(months, start=1):
            val = pivot.loc[hour, month]
            if pd.isna(val):
                ws.write(rr, c_i, "", fmts["text"])
                continue
            val = float(val)
            fmt = fmts["heat_pos"] if val > 0 else fmts["heat_neg"] if val < 0 else fmts["heat_zero"]
            ws.write_number(rr, c_i, val, fmt)
    del scale  # reserved for future gradient shading; flat two-tone for now

    ws.set_column(0, 0, 14)
    ws.set_column(1, max(1, len(months)), 10)


_DAY_HOURS = list(range(6, 18))  # 6a-6p
_PEAK_HOURS = list(range(18, 21))  # 6p-9p
_NIGHT_HOURS = list(range(21, 24)) + list(range(0, 6))  # 9p-6a


def _write_day_peak_night(ws, fmts: dict[str, Any], df: pd.DataFrame, start_row: int) -> None:
    net = (df["import_kwh"] - df["export_kwh"])
    day = float(net[df["hour"].isin(_DAY_HOURS)].sum())
    peak = float(net[df["hour"].isin(_PEAK_HOURS)].sum())
    night = float(net[df["hour"].isin(_NIGHT_HOURS)].sum())

    ws.write(start_row, 0, "Annual net kWh by period", fmts["title"])
    ws.write(start_row + 1, 0, "Period", fmts["header"])
    ws.write(start_row + 1, 1, "Net kWh", fmts["header"])
    for i, (label, val) in enumerate(
        [("Day (6a-6p)", day), ("Peak (6p-9p)", peak), ("Night (9p-6a)", night)]
    ):
        ws.write(start_row + 2 + i, 0, label, fmts["text"])
        ws.write(start_row + 2 + i, 1, val, fmts["num"])


def _flatten_plan(plan: Plan) -> dict[str, Any]:
    data = plan.model_dump(mode="json", exclude_none=True)
    energy_rates = data.pop("energy_rates", [])
    buyback = data.pop("buyback", {})
    bill_credits = data.pop("bill_credits", [])
    buyback_rates = buyback.pop("rates", []) if isinstance(buyback, dict) else []

    flat: dict[str, Any] = dict(data)
    for k, v in (buyback or {}).items():
        flat[f"buyback_{k}"] = v
    flat["energy_rates_json"] = json.dumps(energy_rates)
    flat["buyback_rates_json"] = json.dumps(buyback_rates)
    flat["bill_credits_json"] = json.dumps(bill_credits)
    return flat


def _write_plan_inputs(wb: "xlsxwriter.Workbook", fmts: dict[str, Any], plans: dict[str, Plan]) -> None:
    ws = wb.add_worksheet("Plan Inputs")
    ws.write(0, 0, "Plan Inputs (full schema dump)", fmts["title"])

    rows = [_flatten_plan(p) for p in plans.values()]
    all_cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in all_cols:
                all_cols.append(k)
    # keep a stable, readable lead order
    lead = ["id", "retailer", "name", "term_months", "tdu", "base_charge_usd", "rate_type",
            "tdu_passthrough", "etf_usd", "etf_per_month_remaining", "renewable_pct",
            "source", "efl_url", "needs_review", "notes"]
    ordered = [c for c in lead if c in all_cols] + [c for c in all_cols if c not in lead]

    header_row = 2
    for c, h in enumerate(ordered):
        ws.write(header_row, c, h, fmts["header"])

    for r_i, r in enumerate(rows):
        rr = header_row + 1 + r_i
        for c_i, col in enumerate(ordered):
            val = r.get(col, "")
            if isinstance(val, bool):
                ws.write(rr, c_i, str(val), fmts["text"])
            elif isinstance(val, (int, float)):
                ws.write(rr, c_i, val, fmts["num"])
            else:
                ws.write(rr, c_i, "" if val is None else str(val), fmts["text"])

    for c in range(len(ordered)):
        ws.set_column(c, c, 20)


def build_workbook(
    results: list[PlanResult],
    plans: dict[str, Plan],
    intervals: pd.DataFrame,
    tdu: TduTariff,
    output: Union[io.BytesIO, str, Path],
    current_plan_id: str = CURRENT_PLAN_ID,
) -> None:
    """Build the full Excel export workbook.

    `results` is the (ideally rank()-sorted) list of PlanResult to show;
    `plans` maps plan_id -> Plan for the same universe (extra plans not in
    `results` are still included in the Plan Inputs sheet); `intervals` is
    the canonical interval frame (for the Usage sheet); `tdu` is the current
    TduTariff (for the footnote). `output` is a path or an open BytesIO.
    """
    target = str(output) if isinstance(output, Path) else output
    wb = xlsxwriter.Workbook(target)
    try:
        fmts = _make_formats(wb)
        _write_summary(wb, fmts, results, plans, tdu, current_plan_id)
        _write_monthly_detail(wb, fmts, results, plans)
        _write_usage(wb, fmts, intervals)
        _write_plan_inputs(wb, fmts, plans)
    finally:
        wb.close()
