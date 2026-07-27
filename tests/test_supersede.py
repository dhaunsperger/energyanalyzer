"""Tests for the synthetic-meterplan supersede logic in app.common: when a real
EFL (from PTC/discovery/Meter) yields a plan for the same underlying plan as a
meterplan.com markdown-derived synthetic one, the synthetic is removed.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
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


# --------------------------------------------------------------------------- #
# Discovery-vs-PTC dedup: conservative, term-aware, variant-safe
# --------------------------------------------------------------------------- #
def _ptc_index():
    df = pd.DataFrame(
        [
            {"retailer": "Champion Energy Services LLC", "plan_name": "Champ Saver-12", "term_months": 12},
            {"retailer": "TXU Energy Retail Company LLC", "plan_name": "Solar Buyback Plus", "term_months": 12},
        ]
    )
    return app_common._build_ptc_identity_index(df)


def test_discovered_plan_in_ptc_skips_confident_duplicate():
    idx = _ptc_index()
    assert app_common._discovered_plan_in_ptc("Champion Energy", "Champ Saver 12", idx)
    # verbose legal name on the discovered side matches the short PTC brand too
    assert app_common._discovered_plan_in_ptc("TXU Energy", "Solar Buyback Plus 12", idx)


def test_discovered_plan_in_ptc_keeps_variants_and_other_terms():
    idx = _ptc_index()
    # different term -> not a dup
    assert not app_common._discovered_plan_in_ptc("Champion Energy", "Champ Saver 24", idx)
    # different plan (extra distinctive token) -> not a dup
    assert not app_common._discovered_plan_in_ptc("Champion Energy", "Free Weekends 24", idx)
    # a REP-exclusive plan not in PTC -> kept
    assert not app_common._discovered_plan_in_ptc("Reliant Energy", "Truly Free Nights 12", idx)


def test_discovered_plan_in_ptc_distinguishes_product_numbers():
    """A number that names the product must not be treated as a term.

    "Smart 1000 Select 12" and "Smart 2000 Select 12" are different TXU plans --
    the number is the usage tier the bill credit keys off. Token extraction used
    to drop every pure number, collapsing both to {smart, select}, so discovery's
    Smart 2000 was discarded as a duplicate of PTC's Smart 1000. Only term-sized
    numbers (1..60) are dropped now.
    """
    df = pd.DataFrame(
        [{"retailer": "TXU ENERGY", "plan_name": "Smart 1000 Select 12", "term_months": 12}]
    )
    idx = app_common._build_ptc_identity_index(df)
    assert app_common._discovered_plan_in_ptc("TXU Energy", "Smart 1000 Select 12", idx)
    assert not app_common._discovered_plan_in_ptc("TXU Energy", "Smart 2000 Select 12", idx)


def test_discovered_plan_in_ptc_keeps_when_term_unknown():
    # No term in the discovered name -> we can't be sure, so we keep it (never
    # drop a possibly-distinct plan on a weak signal).
    idx = _ptc_index()
    assert not app_common._discovered_plan_in_ptc("TXU Energy", "Solar Buyback Plus", idx)


def test_build_ptc_identity_index_empty_for_none():
    assert app_common._build_ptc_identity_index(None) == []


# --------------------------------------------------------------------------- #
# A synthetic must never outrank a real EFL -- not even one still in review
# --------------------------------------------------------------------------- #
def _mkplan(path, **kw):
    import yaml as _y
    d = {"id": path.stem, "retailer": "X", "name": "Y", "term_months": 12,
         "base_charge_usd": 0.0, "energy_rates": [{"rate_ckwh": 10.0}], "source": "ptc"}
    d.update(kw)
    path.write_text(_y.safe_dump(d, sort_keys=False))


def test_synthetic_is_not_promoted_when_a_real_draft_covers_it(tmp_path):
    """The inversion this guards against: simple meterplan rows come out
    needs_review=False, so an unverified third-party rate could auto-promote
    into the rankings while the authoritative EFL for the SAME plan sat in the
    draft queue. Supersede alone doesn't help -- it only fires once the real
    plan is promoted, which may never happen."""
    plans, drafts = tmp_path / "plans", tmp_path / "plans" / "drafts"
    drafts.mkdir(parents=True)
    _mkplan(drafts / "mp_txu_energy_solar_buyback_12mo.yaml", retailer="TXU Energy",
           name="Solar Buyback", term_months=12, source="meterplan", needs_review=False,
           _parse={"confidence": {"energy_charge": 0.95, "base_charge": 0.95}})
    _mkplan(drafts / "txu_energy_solar_buyback_12_12mo.yaml", retailer="TXU Energy Retail Company",
           name="TXU Energy Solar Buyback 12", term_months=12, source="efl:txu.pdf",
           needs_review=True, _parse={"confidence": {"energy_charge": 0.4}})

    out = app_common.finish_refresh(plans_dir=plans, drafts_dir=drafts)

    assert out["promoted"] == [], "the synthetic must not promote over a real draft"
    assert out["meterplan_not_promoted"][0]["synthetic"] == "mp_txu_energy_solar_buyback_12mo"
    assert (drafts / "mp_txu_energy_solar_buyback_12mo.yaml").exists()  # left as a draft


def test_synthetic_still_promotes_when_no_real_plan_covers_it(tmp_path):
    """The gate must not block synthetics that are the only source for a plan --
    those are genuinely useful leads."""
    plans, drafts = tmp_path / "plans", tmp_path / "plans" / "drafts"
    drafts.mkdir(parents=True)
    _mkplan(drafts / "mp_tesla_electric_drive_plan_12mo.yaml", retailer="Tesla Electric",
           name="Drive Plan", term_months=12, source="meterplan", needs_review=False,
           _parse={"confidence": {"energy_charge": 0.95, "base_charge": 0.95}})

    out = app_common.finish_refresh(plans_dir=plans, drafts_dir=drafts)
    assert out["promoted"] == ["mp_tesla_electric_drive_plan_12mo"]
    assert out.get("meterplan_not_promoted", []) == []


def test_promoted_synthetic_is_flagged_when_only_a_real_draft_covers_it(tmp_path):
    """Deleting would leave a hole in the rankings with nothing in its place, so
    a draft-only match flags rather than removes."""
    import yaml as _y
    plans, drafts = tmp_path / "plans", tmp_path / "plans" / "drafts"
    drafts.mkdir(parents=True)
    _mkplan(plans / "mp_txu_energy_solar_buyback_12mo.yaml", retailer="TXU Energy",
           name="Solar Buyback", term_months=12, source="meterplan", needs_review=False)
    _mkplan(drafts / "txu_energy_solar_buyback_12_12mo.yaml", retailer="TXU Energy Retail Company",
           name="TXU Energy Solar Buyback 12", term_months=12, source="efl:txu.pdf")

    removed = app_common.supersede_meterplan_plans(plans_dir=plans, drafts_dir=drafts)

    assert removed == []                                        # not deleted
    saved = _y.safe_load((plans / "mp_txu_energy_solar_buyback_12mo.yaml").read_text())
    assert saved["needs_review"] is True
    assert "awaiting review" in saved["notes"]


# --------------------------------------------------------------------------- #
# Synthetic DRAFTS a promoted real plan already covers
# --------------------------------------------------------------------------- #
def _save(plan: Plan, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plan.id}.yaml"
    raw = plan.model_dump(mode="json", exclude_none=True)
    raw["needs_review"] = True  # index rows are flagged: no window/formula published
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def test_supersede_removes_synthetic_drafts_covered_by_a_promoted_plan(tmp_path):
    """A synthetic draft duplicating an already-promoted real plan is noise.

    The meterplan importer dedups against promoted plans by an exact
    (retailer, name, term) tuple, which misses the index's name variants --
    "Chariot Energy Shine 36" against a promoted "Shine 36". Those synthetics
    are flagged (the index publishes no free-hour window or RTW formula), so
    they never promote and never leave the queue on their own.
    """
    plans, drafts = tmp_path / "plans", tmp_path / "drafts"
    _save(_mk("Chariot Energy", "Shine 36", 36, "efl:CHARIOT_Shine_36.pdf", "chariot_shine_36"), plans)
    synthetic = _save(
        _mk("Chariot Energy", "Chariot Energy Shine", 36, "meterplan", "mp_chariot_energy_shine_36mo"),
        drafts,
    )
    # An UNCOVERED synthetic must survive -- removing it would leave nothing in
    # the rankings for that plan.
    uncovered = _save(
        _mk("Reliant Energy", "Truly Free Nights", 12, "meterplan", "mp_reliant_truly_free_nights_12mo"),
        drafts,
    )
    # A real draft is not promoted coverage, so a synthetic it covers stays too.
    _save(_mk("Gexa Energy", "Gexa Solar Buyback", 12, "efl:GEXA.pdf", "gexa_solar_buyback_12"), drafts)
    only_draft_cover = _save(
        _mk("Gexa Energy", "Gexa Solar Buyback", 12, "meterplan", "mp_gexa_solar_buyback_12mo"), drafts
    )

    removed = app_common.supersede_meterplan_plans(plans, drafts)

    assert ("mp_chariot_energy_shine_36mo", "chariot_shine_36") in removed
    assert not synthetic.exists()
    assert uncovered.exists()
    assert only_draft_cover.exists()


# --------------------------------------------------------------------------- #
# prune_stale_meterplan_drafts: the index lists plans the REP no longer sells
# --------------------------------------------------------------------------- #
def test_prune_drops_rows_a_fully_scraped_rep_does_not_offer(tmp_path):
    """meterplan.com is a competitor's index and its rows go stale.

    When we have driven the retailer's own site to completion and the plan is
    not among what it returned, the row is out of date or not something we could
    enroll in -- and it can never be verified, because the index publishes no EFL.
    """
    drafts = tmp_path / "drafts"
    stale = _save(
        _mk("TXU Energy", "Free Nights & Solar Days", 12, "meterplan", "mp_txu_free_nights_solar_days_12mo"),
        drafts,
    )
    offered = _save(
        _mk("TXU Energy", "Free Nights & Cool Summer", 12, "meterplan", "mp_txu_free_nights_cool_summer_12mo"),
        drafts,
    )
    coverage = {"TXU Energy": ["Free Nights & Cool Summer 12", "Simple Rate 12", "e-Saver 12"]}

    removed = prune = app_common.prune_stale_meterplan_rows(drafts, coverage)

    assert [d for d, _ in removed] == ["mp_txu_free_nights_solar_days_12mo"]
    assert not stale.exists()
    assert offered.exists(), "a naming variant of an offered plan must not be pruned"
    assert prune is removed


def test_prune_never_touches_a_rep_we_did_not_fully_scrape(tmp_path):
    """The safety property that makes this rule usable.

    Ambit genuinely sells Free & Clear Nights 12 -- discovery found it -- but its
    12 conventional EFLs 403'd, so Ambit is absent from coverage entirely and its
    rows must survive. Without this, a rate-limited REP would look like a REP
    that had discontinued its whole lineup.
    """
    drafts = tmp_path / "drafts"
    ambit = _save(
        _mk("Ambit Energy", "Free & Clear Nights", 12, "meterplan", "mp_ambit_free_clear_nights_12mo"),
        drafts,
    )
    # Coverage names a DIFFERENT retailer; Ambit isn't in it at all.
    assert app_common.prune_stale_meterplan_rows(drafts, {"TXU Energy": ["Simple Rate 12"]}) == []
    assert ambit.exists()
    # Empty coverage (no discovery run, or none completed) prunes nothing.
    assert app_common.prune_stale_meterplan_rows(drafts, {}) == []
    assert ambit.exists()


def test_prune_leaves_real_drafts_alone(tmp_path):
    drafts = tmp_path / "drafts"
    real = _save(_mk("TXU Energy", "Something Discontinued", 12, "efl:txu.pdf", "txu_real_12"), drafts)
    assert app_common.prune_stale_meterplan_rows(drafts, {"TXU Energy": ["Simple Rate 12"]}) == []
    assert real.exists(), "only meterplan-sourced drafts are ever pruned"


def test_service_mark_fused_to_a_word_is_stripped():
    """"FlexSM" must tokenize to "flex".

    The (R)/(TM) glyph is removed before tokenizing, which fuses the mark onto
    the preceding word. `_NAME_FILLER_TOKENS` already drops a STANDALONE "sm",
    which never helped: the mark is not a separate token. Audited over every
    plan name and retailer on disk -- the only tokens this touches are 12sm,
    24sm, flexsm, forwardsm and freetm, all real service marks.
    """
    assert "flex" in app_common._significant_tokens("TXU Energy Solar Buyback System FlexSM")
    assert "flexsm" not in app_common._significant_tokens("Solar Buyback System FlexSM")
    assert "forward" in app_common._significant_tokens("TXU Energy Flex ForwardSM")
    # Green Mountain's "Pollution FreeTM e-Plus" must match the unmarked spelling.
    assert app_common._significant_tokens("Pollution FreeTM e-Plus 12") == app_common._significant_tokens(
        "Pollution Free e-Plus 12"
    )
    # A short token is never truncated -- "sm" alone is filler, not a suffix.
    assert app_common._significant_tokens("Prism") == {"prism"}


def test_meterplan_bb_abbreviation_supersedes_the_spelled_out_plan():
    """meterplan's "Solar BB System Flex" IS TXU's "Solar Buyback System FlexSM".

    Real miss found 2026-07-26: the synthetic ranked #2 overall, ABOVE the real
    plan it stands in for, carrying a stale 15.6c rate against the EFL's 15.8c.
    Two independent mismatches had to fall for it -- "bb" vs "buyback" and
    "flex" vs "flexsm".
    """
    synthetic = _mk("TXU Energy", "Solar BB System Flex", 1, "meterplan", "mp_txu_solar_bb_flex")
    real = _mk(
        "TXU Energy Retail Company LLC",
        "TXU Energy Solar Buyback System FlexSM",
        1,
        "efl:TXU_Solar_Buyback_System_Flex.pdf",
        "txu_solar_buyback_system_flexsm_1mo",
    )
    assert app_common._plan_supersedes(synthetic, real)
    # Still term-sensitive, and still not a license to merge different products.
    other = _mk("TXU Energy", "TXU Energy Solar Buyback Saver 12", 12, "efl:x.pdf", "txu_saver")
    assert not app_common._plan_supersedes(synthetic, other)


# --------------------------------------------------------------------------- #
# Synthetic meterplan rows that outlived their usefulness
# --------------------------------------------------------------------------- #
def test_prune_reaches_promoted_plans_not_just_drafts(tmp_path):
    """The blind spot with the long half-life.

    A synthetic promoted BEFORE we started surveying its REP directly was never
    re-examined, so it sat in the ranking forever. Four such rows survived the
    2026-07-26 run -- Chariot Fusion, Reliant Solar Payback Plus, TXU Solar
    Buyback Plus and Saver -- none of them still sold by their retailer.
    """
    plans = tmp_path / "plans"
    plans.mkdir()
    _save(_mk("TXU Energy", "Solar Buyback Plus", 12, "meterplan", "mp_txu_solar_buyback_plus_12mo"), plans)
    _save(_mk("TXU Energy", "Simple Rate 12", 12, "meterplan", "mp_txu_simple_rate_12mo"), plans)

    removed = app_common.prune_stale_meterplan_rows(plans, {"TXU Energy": ["Simple Rate 12"]})

    assert [r for r, _ in removed] == ["mp_txu_solar_buyback_plus_12mo"]
    assert not (plans / "mp_txu_solar_buyback_plus_12mo.yaml").exists()
    assert (plans / "mp_txu_simple_rate_12mo.yaml").exists(), "still offered -- keep it"


def test_prune_spares_a_rep_we_do_not_survey(tmp_path):
    """Almika is not in REP_CONFIGS, so nothing ever testifies that it stopped
    selling a plan. Absence of evidence must not retire its row."""
    plans = tmp_path / "plans"
    plans.mkdir()
    _save(_mk("Almika Solar", "60 Energy Plus Buyback", 60, "meterplan", "mp_almika_60mo"), plans)

    assert app_common.prune_stale_meterplan_rows(plans, {"TXU Energy": ["Simple Rate 12"]}) == []
    assert (plans / "mp_almika_60mo.yaml").exists()


def test_prune_reads_the_term_out_of_the_listed_plan_name(tmp_path):
    """Discovery coverage is plan names only -- no term column -- and the old
    code handed the candidate the DRAFT's own term, making the term a
    non-discriminator by construction.

    So a synthetic "All Nighter" 24mo counted as offered because Green Mountain
    sells "Solar All Nighter 12": the token comparison drops the trailing
    number, and the term was the only thing left to tell them apart.
    """
    plans = tmp_path / "plans"
    plans.mkdir()
    _save(_mk("Green Mountain", "All Nighter", 24, "meterplan", "mp_gm_all_nighter_24mo"), plans)
    _save(_mk("Green Mountain", "All Nighter", 12, "meterplan", "mp_gm_all_nighter_12mo"), plans)

    removed = app_common.prune_stale_meterplan_rows(
        plans, {"Green Mountain Energy": ["Solar All Nighter 12"]}
    )

    assert [r for r, _ in removed] == ["mp_gm_all_nighter_24mo"]
    assert (plans / "mp_gm_all_nighter_12mo.yaml").exists(), "the 12mo IS sold"


def test_a_listing_without_a_term_stays_permissive(tmp_path):
    """"Pollution Free e-Plus", "Gexa Flex Plan", "Octopus Flex" carry no term.
    Guessing one there would prune real plans, so the draft's term is used and
    the match falls back to names alone."""
    plans = tmp_path / "plans"
    plans.mkdir()
    _save(_mk("Octopus Energy", "Octopus Flex", 24, "meterplan", "mp_octopus_flex_24mo"), plans)

    assert app_common.prune_stale_meterplan_rows(plans, {"Octopus Energy": ["Octopus Flex"]}) == []


def test_a_trailing_number_that_is_not_a_term_is_ignored():
    """"Gexa 55+" is a seniors plan and "Smart 2000 Select" counts kWh -- only
    plausible contract lengths are read back as a term."""
    assert app_common._term_from_plan_name("Solar All Nighter 12") == 12
    assert app_common._term_from_plan_name("Champ Saver-24") == 24
    assert app_common._term_from_plan_name("Gexa 55+") is None
    assert app_common._term_from_plan_name("Smart 2000 Select") is None
    assert app_common._term_from_plan_name("Pollution Free e-Plus") is None
    assert app_common._term_from_plan_name("Free 3 Day Weekends 12") == 12


# --------------------------------------------------------------------------- #
# Sibling grouping (one product, several brands)
# --------------------------------------------------------------------------- #
def _p(pid, retailer, name, **kw):
    from energyanalyzer.core.models import EnergyRate, Plan
    base = dict(id=pid, retailer=retailer, name=name, term_months=12,
                energy_rates=[EnergyRate(rate_ckwh=9.3)])
    base.update(kw)
    return Plan(**base)


def test_identical_deals_under_different_brands_collapse_to_one_row():
    """Frontier Battery Awards 12, Frontier Sun Confidence 12, Gexa Battery
    Benefits 12 and Gexa Solar Buyback 12 are one NRG product with four names --
    same effective date, same 475 kWh outflow assumption in all four EFLs. Three
    copies of the same deal crowding the top ten hide the real alternatives."""
    plans = [
        _p("a", "Frontier Utilities", "Frontier Battery Awards 12"),
        _p("b", "Frontier Utilities", "Frontier Sun Confidence 12"),
        _p("c", "Gexa Energy", "Gexa Battery Benefits 12"),
        _p("d", "Gexa Energy", "Gexa Solar Buyback 12"),
    ]
    by_id = {p.id: p for p in plans}

    kept, siblings = app_common.group_plan_siblings(["a", "b", "c", "d"], by_id)

    assert kept == ["a"], "the best-ranked one represents the group"
    assert [p.id for p in siblings["a"]] == ["b", "c", "d"]


def test_a_cheaper_sibling_is_not_hidden_behind_a_dearer_one():
    """Anything that can move a bill keeps plans apart -- only a genuinely
    indistinguishable deal is folded away."""
    by_id = {p.id: p for p in (
        _p("cheap", "Gexa Energy", "Gexa 12"),
        _p("dear", "Frontier Utilities", "Frontier 12", base_charge_usd=9.95),
    )}
    kept, siblings = app_common.group_plan_siblings(["cheap", "dear"], by_id)
    assert kept == ["cheap", "dear"] and siblings == {}


def test_eligibility_keeps_plans_apart():
    """A plan this home cannot buy is not interchangeable with one it can,
    however identical the arithmetic."""
    by_id = {p.id: p for p in (
        _p("ok", "Gexa Energy", "Gexa 12"),
        _p("no", "TXU Energy", "Free Nights 12", excludes_solar=True),
    )}
    kept, _ = app_common.group_plan_siblings(["ok", "no"], by_id)
    assert kept == ["ok", "no"]


def test_the_current_plan_always_keeps_its_own_row():
    """It is the baseline every other row is read against."""
    by_id = {p.id: p for p in (
        _p("rival", "Gexa Energy", "Gexa 12"),
        _p(app_common.CURRENT_PLAN_ID, "Pulse Power", "Your Current Plan"),
    )}
    kept, _ = app_common.group_plan_siblings(["rival", app_common.CURRENT_PLAN_ID], by_id)
    assert app_common.CURRENT_PLAN_ID in kept
