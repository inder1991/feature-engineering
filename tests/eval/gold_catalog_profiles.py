"""Loader for ``catalog_profile_gold_v1.json`` — the Release-A catalog-profile gold set.

Table-grain. Each dataset declares the classification a reviewer accepts on all three closed
vocabularies, the evidence that licenses it, and — separately — whether that classification may be
LOAD-BEARING. The separation is the point: an llm_proposed classification can be entirely correct
and still must never become load-bearing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from featuregen.overlay.upload.canonical import CanonicalRow

GOLD_PATH = Path(__file__).with_name("catalog_profile_gold_v1.json")


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(GOLD_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class ProfileGoldDataset:
    dataset_id: str
    family: str
    role: str                       # case | counterexample
    catalog_source: str
    table: str
    columns: tuple[str, ...]
    narrative: str | None
    business_context: str | None
    expected: dict[str, str | None]
    expected_synthesis: dict | None
    evidence: dict[str, dict]
    load_bearing: dict[str, bool]
    why: str

    @property
    def table_ref(self) -> str:
        return f"{self.catalog_source}::{self.table}"

    def rows(self) -> tuple[CanonicalRow, ...]:
        return tuple(CanonicalRow(self.catalog_source, self.table, c, "unknown")
                     for c in self.columns)


def _dataset(raw: dict, *, family: str, source: str) -> ProfileGoldDataset:
    return ProfileGoldDataset(
        dataset_id=raw["id"],
        family=family,
        role=raw["role"],
        catalog_source=source,
        table=raw["table"],
        columns=tuple(raw["columns"]),
        narrative=raw.get("narrative"),
        business_context=raw.get("business_context"),
        expected=dict(raw["expected"]),
        expected_synthesis=raw.get("expected_synthesis"),
        evidence={k: dict(v) for k, v in (raw.get("evidence") or {}).items()},
        load_bearing=dict(raw["load_bearing"]),
        why=raw["why"],
    )


@lru_cache(maxsize=1)
def datasets() -> tuple[ProfileGoldDataset, ...]:
    gold = load()
    source = gold["catalog_source"]
    return tuple(
        _dataset(raw, family=fam["family"], source=source)
        for fam in gold["families"]
        for raw in fam["datasets"]
    )


@lru_cache(maxsize=1)
def families() -> tuple[str, ...]:
    return tuple(fam["family"] for fam in load()["families"])


def datasets_in(family: str) -> tuple[ProfileGoldDataset, ...]:
    return tuple(d for d in datasets() if d.family == family)


def dataset_by_table(table: str) -> ProfileGoldDataset:
    for d in datasets():
        if d.table == table:
            return d
    raise KeyError(f"no gold dataset for table {table!r}")


def selection_questions() -> tuple[dict, ...]:
    return tuple(load()["table_selection_questions"]["questions"])


def unsafe_physical_assertions() -> tuple[str, ...]:
    return tuple(c["assertion"] for c in load()["unsafe_physical_assertions"]["cases"])
