"""Tests for the synthetic-meterplan supersede logic in app.common: when a real
EFL (from PTC/discovery/Meter) yields a plan for the same underlying plan as a
meterplan.com markdown-derived synthetic one, the synthetic is removed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from energyanalyzer.app import common as app_common
from energyanalyzer.core.models import Plan


def _mk(retailer: str, name: str, term: int, source: str, plan_id: str) -> Plan:
    return Plan.model_validate(
        {
            "id": plan_id,
            "retailer": retailer,
            "name": name,
            "term_months": term,
            "energy_rates": [{"rate_ckwh": 11.0}],
            "source": source,
        }
    )


# --------------------------------------------------------------------------- #
# _plan_supersedes: match across the divergent naming of the two sources
# --------------------------------------------------------------------------- #
def test_plan_supersedes_matches_short_vs_verbose_names():
    meter = _mk("Reliant Energy", "Solar Payback Match", 12, "meterplan", "mp_x")
    real = _mk(
        "Reliant Energy Retail Services LLC",
        "Reliant Solar Payback Match 12",
        12,
        "efl:reliant.pdf",
        "real_x",
    )
    assert app_common._plan_supersedes(meter, real)


def test_plan_supersedes_matches_verbose_cert_suffix_retailer():
    meter = _mk("Green Mountain", "Solar Credit", 12, "meterplan", "mp_gm")
    real = _mk(
        "Green Mountain Energy Company (REP Cert No 10009)",
        "Renewable Rewards Solar Credit 12",
        12,
        "efl:gm.pdf",
        "real_gm",
    )
    assert app_common._plan_supersedes(meter, real)


def test_plan_supersedes_rejects_different_variant_same_retailer():
    # "Solar Buyback Saver" must NOT be superseded by "Solar Buyback Plus".
    meter = _mk("TXU Energy", "Solar Buyback Saver", 12, "meterplan", "mp_txu")
    real = _mk(
        "TXU Energy Retail Company LLC", "TXU Energy Solar Buyback Plus", 12, "efl:txu.pdf", "real"
    )
    assert not app_common._plan_supersedes(meter, real)


def test_plan_supersedes_rejects_term_mismatch():
    meter = _mk("Gexa Energy", "Solar Buyback", 12, "meterplan", "mp_g")
    real = _mk("Gexa Energy LP", "Gexa Solar Buyback", 24, "efl:g.pdf", "real_g")
    assert not app_common._plan_supersedes(meter, real)


def test_plan_supersedes_rejects_different_retailer():
    meter = _mk("Ambit Energy", "Texas Solar Buyback", 12, "meterplan", "mp_a")
    real = _mk("Reliant Energy Retail Services LLC", "Reliant Solar Buyback", 12, "efl:r.pdf", "r")
    assert not app_common._plan_supersedes(meter, real)


# --------------------------------------------------------------------------- #
# supersede_meterplan_plans: only removes matched meterplan-source plans
# --------------------------------------------------------------------------- #
def _write(plans_dir: Path, plan: Plan) -> None:
    (plans_dir / f"{plan.id}.yaml").write_text(yaml.safe_dump(plan.model_dump(mode="json")))


def test_supersede_removes_only_matched_meterplan_plans(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()

    matched = _mk("Reliant Energy", "Solar Payback Match", 12, "meterplan", "mp_reliant_spm_12mo")
    unmatched = _mk("Almika Solar", "60 Energy Plus Buyback", 60, "meterplan", "mp_almika_60mo")
    real = _mk(
        "Reliant Energy Retail Services LLC",
        "Reliant Solar Payback Match 12",
        12,
        "efl:reliant.pdf",
        "reliant_real_12mo",
    )
    for p in (matched, unmatched, real):
        _write(plans_dir, p)

    removed = app_common.supersede_meterplan_plans(plans_dir)

    assert removed == [("mp_reliant_spm_12mo", "reliant_real_12mo")]
    remaining = {p.stem for p in plans_dir.glob("*.yaml")}
    assert "mp_reliant_spm_12mo" not in remaining  # superseded
    assert "mp_almika_60mo" in remaining  # no real match -> kept
    assert "reliant_real_12mo" in remaining  # real plan never removed


def test_supersede_never_removes_the_current_plan(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    # Even if a meterplan-source plan shares CURRENT_PLAN_ID and matches, keep it.
    cur = _mk("Reliant Energy", "Solar Payback Match", 12, "meterplan", app_common.CURRENT_PLAN_ID)
    real = _mk(
        "Reliant Energy Retail Services LLC",
        "Reliant Solar Payback Match 12",
        12,
        "efl:reliant.pdf",
        "reliant_real_12mo",
    )
    _write(plans_dir, cur)
    _write(plans_dir, real)

    removed = app_common.supersede_meterplan_plans(plans_dir)
    assert removed == []
    assert (plans_dir / f"{app_common.CURRENT_PLAN_ID}.yaml").exists()
