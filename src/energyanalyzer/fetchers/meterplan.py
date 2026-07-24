"""meterplan.com Texas solar buyback plan index fetcher (ARCHITECTURE.md §7/§9).

meterplan.com (operated by Meter Energy Inc., a competing REP/broker) publishes
an hourly-regenerated public markdown index of Texas solar buyback plans at
https://meterplan.com/data/texas-solar-buyback-plans.md. It covers several
solar buyback plans (mostly non-Oncor TDU territories, plus a few Oncor ones)
that Power to Choose's export doesn't carry. We use it strictly as a **rate
index**: the plan's advertised import/export rates, base fee, and ETF. Their
"Estimated annual cost" column is Meter Energy's own cost engine using a fixed
default usage profile (700/700 kWh, 40% night) -- we NEVER read that column;
EnergyAnalyzer's own billing engine (ARCHITECTURE.md §6) computes costs from
the user's actual interval data.

Like `fetchers/ptc.py`, this module is offline-first: :func:`load_meterplan`
and :func:`filter_meterplan` work entirely from a markdown file already on
disk (a snapshot saved by :func:`fetch_meterplan`, or the committed reference
fixture `tests/fixtures/meterplan_sample.md`). :func:`fetch_meterplan` is a
thin, defensively-wrapped httpx call that raises a clear RuntimeError with a
manual-download fallback if the network is unavailable -- meterplan.com is
blocked from this development sandbox, so that path is exercised in tests
only via monkeypatched transports, never live.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from energyanalyzer.eflparse.parser import slugify

METERPLAN_URL = "https://meterplan.com/data/texas-solar-buyback-plans.md"
# The human-facing plans page. Unlike the markdown index (which intentionally
# omits document URLs), the server-rendered HTML embeds a JSON-LD OfferCatalog
# whose offers carry the *real* EFL PDF as an `additionalProperty` -- presigned
# S3 links valid for ~7 days. These are Meter Energy's OWN plans only (Earner /
# Saver / Standard, plain + Battery); competitor EFLs are not exposed here.
METERPLAN_PLANS_URL = "https://meterplan.com/plans"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Markdown table sections to parse. "Top Plans By TDU For The Default Profile"
# is intentionally excluded: it's a curated top-N re-listing of rows that
# already appear in the two sections below, so including it would duplicate
# rows.
_TABLE_SECTIONS = ("Meter Plan Availability", "Competitor Plan Availability")

_TIDY_COLUMNS = [
    "tdu",
    "retailer",
    "plan_name",
    "term_months",
    "import_ckwh",
    "export_kind",
    "export_ckwh",
    "base_usd_month",
    "etf_usd",
    "etf_per_month_remaining",
    "battery_required",
    "source_url",
    "generated",
    "raw_row",
]

# Free-hours-indicating plan-name keywords -- these plans' single advertised
# "Import rate" almost certainly isn't a true flat rate (it's likely just the
# day/non-free rate, or a blended figure); meterplan.com's index doesn't
# publish the free window's exact hours, so we assume the common 9pm-6am
# convention and flag the draft for manual verification against the real EFL.
_FREE_HOURS_RE = re.compile(r"night|nighter|free|twelve hour|weekend", re.I)

_ASSUMED_NIGHT_HOURS = [21, 22, 23, 0, 1, 2, 3, 4, 5]  # 9pm-6am

_RATE_CKWH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:¢|cents?)\s*/?\s*kWh", re.I)
_DOLLAR_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")


def fetch_meterplan(dest_dir: Path = Path("data/meterplan"), timeout: float = 30.0) -> Path:
    """Download the meterplan.com Texas solar buyback plan index and save a
    timestamped snapshot.

    Raises RuntimeError with instructions to fetch it via a browser if the
    network request fails (meterplan.com is blocked from some sandboxes;
    this always works from the user's own machine).
    """
    import httpx

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/markdown,text/plain,*/*"}

    try:
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            resp = client.get(METERPLAN_URL)
            resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"Could not download the meterplan.com solar buyback plan index ({METERPLAN_URL}): "
            f"{exc!r}. Please open that URL in a browser, save the page as markdown/text, and "
            f"place it in {dest_dir}/ -- then call load_meterplan() on the saved file or on "
            f"{dest_dir}."
        ) from exc

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_path = dest_dir / f"meterplan_{ts}.md"
    dest_path.write_bytes(resp.content)
    return dest_path


# --------------------------------------------------------------------------- #
# Meter's own real EFLs (from the JSON-LD on the /plans HTML page)
# --------------------------------------------------------------------------- #
_LD_JSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)


def parse_meterplan_efl_offers(html: str, tdu: str = "Oncor") -> list[dict]:
    """Parse the /plans page's JSON-LD OfferCatalog into a list of Meter's own
    plan offers carrying a real EFL URL, filtered to `tdu`.

    Each returned dict: ``{"offer_id": str, "name": str, "tdu": str,
    "efl_url": str}``. Offers are matched to `tdu` via their ``areaServed``
    ``AdministrativeArea`` name (e.g. "Oncor"). The EFL URL comes from the
    offer's ``additionalProperty`` entry named "Electricity Facts Label";
    ``json.loads`` decodes the ``\\u0026``-escaped presigned query string for
    us. Offers with no EFL property, or not in `tdu`, are skipped. Deduped by
    ``offer_id`` (the page lists each offer more than once).
    """
    import json

    tdu_norm = tdu.strip().lower()
    out: dict[str, dict] = {}
    for block in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        for doc in data if isinstance(data, list) else [data]:
            if not isinstance(doc, dict):
                continue
            offers = doc.get("itemListElement") or []
            for off in offers:
                if not isinstance(off, dict):
                    continue
                area = off.get("areaServed") or []
                if isinstance(area, dict):
                    area = [area]
                admin = [
                    str(a.get("name") or "").strip().lower()
                    for a in area
                    if isinstance(a, dict) and a.get("@type") == "AdministrativeArea"
                ]
                if tdu_norm not in admin:
                    continue
                efl_url = None
                for prop in off.get("additionalProperty") or []:
                    if not isinstance(prop, dict):
                        continue
                    if "electricity facts label" in str(prop.get("name", "")).lower():
                        efl_url = str(prop.get("value") or "").strip()
                        break
                if not efl_url:
                    continue
                offer_id = str(off.get("@id") or off.get("name") or "").lstrip("#").strip()
                if not offer_id or offer_id in out:
                    continue
                out[offer_id] = {
                    "offer_id": offer_id,
                    "name": str(off.get("name") or "").strip(),
                    "tdu": tdu,
                    "efl_url": efl_url,
                }
    return list(out.values())


def _meter_efl_filename(offer: dict) -> str:
    """Stable local filename for a Meter EFL (independent of the presigned URL's
    daily-rotating date/hash, so refreshes don't churn on-disk names)."""
    return f"Meter_Energy_{slugify(offer['offer_id'])}.pdf"


def fetch_meterplan_efls(
    zip_code: str = "78665",
    dest: Path = Path("data/efl"),
    tdu: str = "Oncor",
    timeout: float = 30.0,
    progress_callback=None,
) -> dict:
    """Fetch Meter Energy's own real EFL PDFs from the /plans page and save them
    into `dest`, so the billing engine can parse them like any other EFL
    (superseding the synthetic markdown-index drafts for Meter's own plans).

    Fetches ``/plans?zipcode=<zip>``, parses its JSON-LD OfferCatalog
    (:func:`parse_meterplan_efl_offers`, filtered to `tdu`), and downloads each
    offer's presigned EFL PDF. Existing files are skipped; non-PDF responses and
    per-URL errors are tolerated and collected. Raises RuntimeError (with a
    browser fallback) only if the *page itself* can't be fetched.

    Returns ``{"offers": int, "downloaded": [path, ...], "skipped": [path, ...],
    "failed": [{"url", "error"}, ...]}``.
    """
    import httpx

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"}
    url = f"{METERPLAN_PLANS_URL}?zipcode={zip_code}"

    summary: dict = {"offers": 0, "downloaded": [], "skipped": [], "failed": []}
    try:
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            page = client.get(url)
            page.raise_for_status()
            offers = parse_meterplan_efl_offers(page.text, tdu=tdu)
            summary["offers"] = len(offers)
            total = len(offers)
            for done, offer in enumerate(offers, start=1):
                dest_path = dest / _meter_efl_filename(offer)
                if dest_path.exists():
                    summary["skipped"].append(str(dest_path))
                else:
                    try:
                        resp = client.get(offer["efl_url"])
                        resp.raise_for_status()
                        content = resp.content
                        if b"%PDF" not in content[:1024]:
                            ctype = resp.headers.get("content-type", "?")
                            summary["failed"].append(
                                {
                                    "url": offer["efl_url"],
                                    "error": f"response was not a PDF (content-type {ctype!r}, "
                                    f"{len(content)} bytes)",
                                }
                            )
                        else:
                            dest_path.write_bytes(content)
                            summary["downloaded"].append(str(dest_path))
                    except Exception as exc:  # noqa: BLE001 - tolerate per-EFL failures
                        summary["failed"].append({"url": offer["efl_url"], "error": repr(exc)})
                if progress_callback is not None:
                    progress_callback(done, total, offer["name"])
    except Exception as exc:
        raise RuntimeError(
            f"Could not fetch Meter Energy's plans page ({url}): {exc!r}. Open that URL in a "
            f"browser to confirm it loads; its EFL links are presigned and valid ~7 days."
        ) from exc

    return summary


# --------------------------------------------------------------------------- #
# Markdown parsing
# --------------------------------------------------------------------------- #
_GENERATED_RE = re.compile(r"^Generated:\s*(.+)$", re.M)


def _parse_generated(text: str) -> Optional[dt.datetime]:
    m = _GENERATED_RE.search(text)
    if not m:
        return None
    ts = m.group(1).strip()
    try:
        parsed = pd.Timestamp(ts)
    except (ValueError, TypeError):
        return None
    return parsed.to_pydatetime()


def _section_lines(text: str, heading: str) -> list[str]:
    """Lines belonging to the `## {heading}` section (up to, but not
    including, the next `## ` heading or end of document)."""
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.M)
    m = pattern.search(text)
    if not m:
        return []
    start = m.end()
    rest = text[start:]
    next_m = re.search(r"^##\s+", rest, re.M)
    end = len(rest) if next_m is None else next_m.start()
    return rest[:end].splitlines()


def _parse_md_table(lines: list[str]) -> list[dict]:
    """Parse a GitHub-flavored-markdown pipe table into a list of row dicts
    keyed by header cell text. Returns [] if fewer than a header + separator
    + one data row is present."""
    rows = [ln for ln in lines if ln.strip().startswith("|")]
    if len(rows) < 3:
        return []
    header = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    records = []
    for raw_line in rows[2:]:
        cells = [c.strip() for c in raw_line.strip().strip("|").split("|")]
        if len(cells) != len(header):
            continue
        record = dict(zip(header, cells))
        record["_raw_row"] = raw_line.strip()
        records.append(record)
    return records


def _parse_rate_ckwh(cell: str) -> Optional[float]:
    m = _RATE_CKWH_RE.search(cell)
    return float(m.group(1)) if m else None


def _parse_export(cell: str) -> tuple[str, Optional[float]]:
    """('fixed'|'rtw'|'none', rate_ckwh_or_None)."""
    cell = cell.strip()
    if re.search(r"real\s*time", cell, re.I):
        return "rtw", None
    rate = _parse_rate_ckwh(cell)
    if rate is None:
        return "none", None
    if rate == 0:
        return "none", 0.0
    return "fixed", rate


def _is_none_cell(cell: str) -> bool:
    return bool(re.fullmatch(r"none", cell.strip(), re.I))


def _parse_base_usd(cell: str) -> float:
    cell = cell.strip()
    if _is_none_cell(cell):
        return 0.0
    m = _DOLLAR_RE.search(cell)
    return float(m.group(1)) if m else 0.0


def _parse_etf(cell: str) -> tuple[float, bool]:
    """(etf_usd, etf_per_month_remaining)."""
    cell = cell.strip()
    if _is_none_cell(cell):
        return 0.0, False
    m = _DOLLAR_RE.search(cell)
    amount = float(m.group(1)) if m else 0.0
    per_month = bool(re.search(r"per\s*month", cell, re.I))
    return amount, per_month


def _parse_term_months(cell: str) -> Optional[int]:
    cell = cell.strip()
    try:
        return int(float(cell))
    except ValueError:
        return None


def load_meterplan(path: Path) -> pd.DataFrame:
    """Load a meterplan.com markdown snapshot into a tidy DataFrame.

    `path` may be a specific `.md` file, or a directory (e.g. `data/meterplan/`)
    in which case the most recently modified `*.md` in it is used.

    Parses only the "Meter Plan Availability" and "Competitor Plan
    Availability" tables (NOT "Top Plans By TDU For The Default Profile",
    which re-lists a curated subset of the same rows and would duplicate
    them). Returns one row per plan with tidy columns: `tdu`, `retailer`,
    `plan_name`, `term_months`, `import_ckwh`, `export_kind`
    (`fixed`/`rtw`/`none`), `export_ckwh`, `base_usd_month`, `etf_usd`,
    `etf_per_month_remaining`, `battery_required`, `source_url`, `generated`
    (the document's `Generated:` timestamp, same for every row), plus an
    internal `raw_row` column (the original markdown table row text, kept as
    parse evidence for `meterplan_to_drafts`).

    We deliberately do NOT surface Meter Energy's "Estimated annual cost"
    column -- it's their own cost-engine output against a fixed default
    usage profile, not ours to trust; EnergyAnalyzer computes costs itself
    from the user's actual interval data.
    """
    path = Path(path)
    if path.is_dir():
        candidates = sorted(path.glob("*.md"))
        if not candidates:
            raise FileNotFoundError(
                f"No meterplan.com snapshot files found in {path}. Run fetch_meterplan(), or "
                f"download the page manually from {METERPLAN_URL} and place it in {path}."
            )
        path = max(candidates, key=lambda p: p.stat().st_mtime)
    elif not path.exists():
        raise FileNotFoundError(f"meterplan.com snapshot not found: {path}")

    text = path.read_text(encoding="utf-8")
    generated = _parse_generated(text)

    records: list[dict] = []
    for heading in _TABLE_SECTIONS:
        lines = _section_lines(text, heading)
        records.extend(_parse_md_table(lines))

    if not records:
        raise ValueError(
            f"No 'Meter Plan Availability' / 'Competitor Plan Availability' tables found in {path}"
        )

    rows = []
    for r in records:
        plan_name = str(r.get("Plan", "")).strip()
        export_kind, export_ckwh = _parse_export(str(r.get("Export credit", "")))
        etf_usd, etf_per_month_remaining = _parse_etf(str(r.get("Early termination fee", "")))
        rows.append(
            {
                "tdu": str(r.get("TDU", "")).strip(),
                "retailer": str(r.get("Provider", "")).strip(),
                "plan_name": plan_name,
                "term_months": _parse_term_months(str(r.get("Term", ""))),
                "import_ckwh": _parse_rate_ckwh(str(r.get("Import rate", ""))),
                "export_kind": export_kind,
                "export_ckwh": export_ckwh,
                "base_usd_month": _parse_base_usd(str(r.get("Base fee", ""))),
                "etf_usd": etf_usd,
                "etf_per_month_remaining": etf_per_month_remaining,
                "battery_required": "+ Battery" in plan_name,
                "source_url": str(r.get("Source", "")).strip(),
                "generated": generated,
                "raw_row": r.get("_raw_row", ""),
            }
        )

    return pd.DataFrame(rows, columns=_TIDY_COLUMNS)


def filter_meterplan(df: pd.DataFrame, tdu: Optional[str] = "Oncor") -> pd.DataFrame:
    """Filter a tidy meterplan DataFrame by TDU territory.

    meterplan.com's own TDU labels are used (not Power to Choose's all-caps
    convention): `Oncor`, `Centerpoint`, `AEP Central`, `AEP North`, `TNMP`,
    `Lubbock`. Matching is a case-insensitive substring match, same
    convention as `fetchers.ptc.filter_plans`. `tdu=None` returns every row.
    """
    out = df
    if tdu is not None and "tdu" in out.columns:
        out = out[out["tdu"].astype(str).str.contains(re.escape(tdu), case=False, na=False)]
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Draft plan generation
# --------------------------------------------------------------------------- #
def _plan_id(retailer: str, plan_name: str, term_months: Optional[int]) -> str:
    term_part = f"{term_months}mo" if term_months is not None else "unk_mo"
    return "mp_" + slugify(f"{retailer}_{plan_name}_{term_part}")


def meterplan_to_drafts(
    df: pd.DataFrame,
    drafts_dir: Path,
    existing_plan_keys: set,
    retrieved: Optional[dt.date] = None,
) -> dict:
    """Turn each row of a tidy meterplan DataFrame (see `load_meterplan`) into
    a draft plan YAML in `drafts_dir` (same `_parse` confidence/evidence
    layout `eflparse.parser.save_draft` uses).

    Mapping:
    - `import_ckwh` -> a single fixed `EnergyRate`, UNLESS the plan name
      suggests free/discounted overnight hours (contains "night", "nighter",
      "free", "twelve hour", or "weekend", case-insensitively) -- meterplan's
      index doesn't publish the actual window, so those get a two-rate
      structure (assumed 9pm-6am free window + the published rate as the
      day/default rate), confidence 0.4, `needs_review=True`, with a note to
      verify the real window against the plan's EFL.
    - `export_kind="fixed"` -> `buyback.kind="fixed"`; `"rtw"` ->
      `buyback.kind="rtw"` (multiplier 1, adder 0); `"none"` -> `kind="none"`.
    - `buyback.offset_scope` always defaults to `all_charges` at confidence
      0.5 -- the index doesn't disclose which plans have "not offsettable"
      restrictions.
    - Rows with `battery_required=True` are SKIPPED entirely (the credit
      value/eligibility isn't disclosed in a form we can model; counted in
      the summary rather than silently dropped).
    - A plan is "simple" (fixed import, fixed/none export, non-free-night
      name) -> `needs_review=False` (eligible for the existing auto-promote
      gate); anything else -> `needs_review=True`.
    - `source="meterplan"`; `retrieved` is stamped from the row's `generated`
      timestamp date, or the `retrieved` argument if given.
    - Dedupe: a row is skipped (and counted `skipped_existing`) if its
      `(retailer.lower(), plan_name.lower(), term_months)` is already in
      `existing_plan_keys` (the caller's set of keys from currently-known
      plans -- see `app.common.refresh_market_data`).

    Returns `{"imported": [plan_id, ...], "skipped_battery": int,
    "skipped_existing": int, "flagged_for_review": int}`.
    """
    drafts_dir = Path(drafts_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "imported": [],
        "skipped_battery": 0,
        "skipped_existing": 0,
        "flagged_for_review": 0,
    }

    from energyanalyzer.eflparse.parser import DraftPlan, save_draft  # noqa: PLC0415

    for _, row in df.iterrows():
        plan_name = str(row["plan_name"])
        retailer = str(row["retailer"])
        term_months = row["term_months"]
        term_months = int(term_months) if pd.notna(term_months) else None

        if bool(row.get("battery_required")):
            summary["skipped_battery"] += 1
            continue

        key = (retailer.strip().lower(), plan_name.strip().lower(), term_months)
        if key in existing_plan_keys:
            summary["skipped_existing"] += 1
            continue

        generated = row.get("generated")
        row_retrieved = retrieved or (
            generated.date() if isinstance(generated, dt.datetime) else dt.date.today()
        )

        raw_row = str(row.get("raw_row") or "")
        is_free_hours = bool(_FREE_HOURS_RE.search(plan_name))
        import_ckwh = row["import_ckwh"]

        confidence: dict[str, float] = {}
        evidence: dict[str, str] = {}
        notes: list[str] = []

        if is_free_hours:
            energy_rates = [
                {
                    "label": "assumed free/discounted overnight",
                    "rate_ckwh": 0.0,
                    "window": {"hours": list(_ASSUMED_NIGHT_HOURS)},
                },
                {"label": "day", "rate_ckwh": import_ckwh, "window": None},
            ]
            confidence["energy_charge"] = 0.4
            confidence["free_window"] = 0.4
            evidence["energy_charge"] = raw_row
            evidence["free_window"] = raw_row
            notes.append(
                "Plan name suggests a free/discounted overnight window, but meterplan.com's "
                "index does not publish the exact hours -- assumed 9pm-6am (hours "
                f"{_ASSUMED_NIGHT_HOURS}). Verify against the real EFL before relying on this."
            )
        else:
            energy_rates = [{"rate_ckwh": import_ckwh, "window": None}]
            confidence["energy_charge"] = 0.85
            evidence["energy_charge"] = raw_row

        export_kind = row["export_kind"]
        if export_kind == "fixed":
            buyback = {
                "kind": "fixed",
                "rate_ckwh": row["export_ckwh"],
                "offset_scope": "all_charges",
            }
            confidence["buyback"] = 0.85
        elif export_kind == "rtw":
            buyback = {
                "kind": "rtw",
                "rtw": {"multiplier": 1.0, "adder_ckwh": 0.0},
                "offset_scope": "all_charges",
            }
            confidence["buyback"] = 0.5
            notes.append(
                "Export credit listed as real-time/wholesale-indexed; multiplier/adder assumed "
                "1x + 0 (meterplan.com's index doesn't disclose the exact indexing formula)."
            )
        else:
            buyback = {"kind": "none", "offset_scope": "all_charges"}
            confidence["buyback"] = 0.9
        evidence["buyback"] = raw_row

        confidence["offset_scope"] = 0.5
        evidence["offset_scope"] = raw_row

        confidence["base_charge"] = 0.9
        evidence["base_charge"] = raw_row

        simple = (not is_free_hours) and export_kind in ("fixed", "none")
        needs_review = not simple

        plan_id = _plan_id(retailer, plan_name, term_months)
        plan_dict = {
            "id": plan_id,
            "retailer": retailer,
            "name": plan_name,
            "term_months": term_months if term_months is not None else 12,
            "tdu": str(row["tdu"]).upper(),
            "base_charge_usd": row["base_usd_month"],
            "energy_rates": energy_rates,
            "buyback": buyback,
            "tdu_passthrough": True,
            "etf_usd": row["etf_usd"],
            "etf_per_month_remaining": bool(row["etf_per_month_remaining"]),
            "rate_type": "fixed",
            "source": "meterplan",
            "retrieved": row_retrieved,
            "efl_url": None,
            "notes": "; ".join(notes),
            "needs_review": needs_review,
        }

        draft = DraftPlan(
            plan_dict=plan_dict, confidence=confidence, evidence=evidence, unparsed_notes=notes
        )
        save_draft(draft, drafts_dir=drafts_dir)
        summary["imported"].append(plan_id)
        if needs_review:
            summary["flagged_for_review"] += 1

    return summary
