"""Load/save plan YAMLs and TDU tariffs (ARCHITECTURE.md §5)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from .models import Plan, TduTariff

REPO_ROOT = Path(__file__).resolve().parents[3]
PLANS_DIR = REPO_ROOT / "plans"
DRAFTS_DIR = PLANS_DIR / "drafts"
TDU_DIR = REPO_ROOT / "tdu"


def load_plan(path: Path) -> Plan:
    with open(path) as f:
        return Plan.model_validate(yaml.safe_load(f))


def load_plans(directory: Path = PLANS_DIR) -> list[Plan]:
    plans = [load_plan(p) for p in sorted(directory.glob("*.yaml"))]
    ids = [p.id for p in plans]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate plan ids: {dupes}"
    return plans


def save_plan(plan: Plan, directory: Path = PLANS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plan.id}.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(
            plan.model_dump(mode="json", exclude_none=True),
            f,
            sort_keys=False,
            allow_unicode=True,
        )
    return path


def load_tdu_tariffs(name: str = "oncor") -> list[TduTariff]:
    with open(TDU_DIR / f"{name}.yaml") as f:
        raw = yaml.safe_load(f)
    tariffs = [TduTariff.model_validate(r) for r in raw["tariffs"]]
    return sorted(tariffs, key=lambda t: t.effective)


def tdu_for_date(tariffs: list[TduTariff], on: dt.date) -> TduTariff:
    applicable = [t for t in tariffs if t.effective <= on]
    assert applicable, f"no TDU tariff effective on {on}"
    return applicable[-1]


def current_tdu(name: str = "oncor") -> TduTariff:
    """Latest tariff — used for all 12 forward-looking billing months."""
    return load_tdu_tariffs(name)[-1]
