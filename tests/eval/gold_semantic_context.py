"""Loader for ``semantic_context_gold_v1.json`` — the Release-A semantic gold set.

Turns the JSON fixture into the exact objects the REAL Pass-A path consumes (``CanonicalRow`` +
``GlossaryRecord``), builds the two payloads the evaluation contrasts, and implements the replay
oracle.

WHAT THE ORACLE MEASURES, said plainly. It is not a model. It reads the payload the pipeline
ACTUALLY assembled for a column and answers the reviewer's concept only when the disambiguating
evidence is present in those bytes. So a "hit" means *the platform delivered the evidence*, and a
"miss" means *it did not*. Provider skill is deliberately out of scope — that is the live
same-model comparison, which D9 places in Gate B.

The thin/rich seam is `enrich._classifier_payload(row, rec, bundle)`: passing ``bundle=None``
reproduces the historical metadata-only payload byte-for-byte (its own docstring says so), and
passing a real bundle produces the v4 purpose-adapted payload. No flag, no DB, no client.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import semantic_context as sc
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord

GOLD_PATH = Path(__file__).with_name("semantic_context_gold_v1.json")

UNCLASSIFIED = "unclassified"

MODE_REQUIRES_ANY = "requires_any"
MODE_REQUIRES_ABSENT = "requires_absent"


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(GOLD_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class SemanticGoldCase:
    """One graded column. ``confusable_concept`` is what the SAME question collapses to when the
    discriminator is absent — it is the defect, named, not a random wrong answer."""

    case_id: str
    family: str
    role: str                      # case | counterexample
    catalog_source: str
    table: str
    column: str
    column_type: str
    glossary: dict | None
    expected_concept: str
    acceptable_concepts: frozenset[str]
    confusable_concept: str
    discriminator_mode: str
    discriminator_terms: tuple[str, ...]
    discriminator_keys: tuple[str, ...]
    forbidden_concepts: frozenset[str]
    witness: bool
    sensitivity: str

    @property
    def logical_ref(self) -> str:
        return f"{self.catalog_source}::{load()['schema']}.{self.table}.{self.column}"


def _case(raw: dict, *, family: str, source: str, table: str) -> SemanticGoldCase:
    disc = raw["discriminator"]
    return SemanticGoldCase(
        case_id=raw["id"],
        family=family,
        role=raw["role"],
        catalog_source=source,
        table=table,
        column=raw["column"],
        column_type=raw["type"],
        glossary=raw.get("glossary"),
        expected_concept=raw["expected_concept"],
        acceptable_concepts=frozenset(raw["acceptable_concepts"]),
        confusable_concept=raw["confusable_concept"],
        discriminator_mode=disc["mode"],
        discriminator_terms=tuple(disc.get("terms", ())),
        discriminator_keys=tuple(disc.get("keys", ())),
        forbidden_concepts=frozenset(raw.get("forbidden_concepts", ())),
        witness=bool(raw.get("witness", False)),
        sensitivity=raw.get("sensitivity", ""),
    )


@lru_cache(maxsize=1)
def cases() -> tuple[SemanticGoldCase, ...]:
    gold = load()
    source, table = gold["catalog_source"], gold["table"]
    return tuple(
        _case(raw, family=fam["family"], source=source, table=table)
        for fam in gold["families"]
        for raw in fam["cases"]
    )


@lru_cache(maxsize=1)
def families() -> tuple[str, ...]:
    return tuple(fam["family"] for fam in load()["families"])


def cases_in(family: str) -> tuple[SemanticGoldCase, ...]:
    return tuple(c for c in cases() if c.family == family)


def case_by_column(column: str) -> SemanticGoldCase:
    for c in cases():
        if c.column == column:
            return c
    raise KeyError(f"no gold case for column {column!r}")


# ── fixture objects ──────────────────────────────────────────────────────────────────────────────


def row_of(case: SemanticGoldCase) -> CanonicalRow:
    return CanonicalRow(case.catalog_source, case.table, case.column, case.column_type)


def physical_type(spec: dict | None) -> str:
    """The bare type family of a declared SQL type — `varchar(11)` -> `varchar`.

    The classifier fixtures keep FTR's real shape (`CanonicalRow.type == "unknown"`; a business
    glossary is not the physical-type authority). The CATALOG seed used for identifier pairing and
    retrieval needs an attested type, because `_resolve_family` classifies an unattested,
    undeclared column as family `other` and candidate derivation excludes it — measured on the first
    run of this harness, that produced zero candidates and made the cross-namespace bar vacuous.
    Attesting the type here keeps the question under test the NAMESPACE gate rather than type
    attestation, which has its own tests elsewhere.
    """
    declared = (spec or {}).get("declared_type", "")
    return declared.split("(", 1)[0].strip() or "text"


def catalog_row_of(case: SemanticGoldCase) -> CanonicalRow:
    """The gold column as the CATALOG holds it: attested physical type plus the curated definition,
    so the search index and the pairing path have the material they really would have."""
    return CanonicalRow(
        case.catalog_source, case.table, case.column, physical_type(case.glossary),
        definition=(case.glossary or {}).get("definition", ""),
        sensitivity=case.sensitivity,
    )


@lru_cache(maxsize=1)
def cohort() -> tuple[CanonicalRow, ...]:
    """Every gold column of the anchor table, in declaration order — this IS the sibling roster the
    rich payload renders, so the roster-based discriminators are answering about real siblings."""
    return tuple(row_of(c) for c in cases())


def _record(source: str, schema: str, table: str, column: str, spec: dict) -> GlossaryRecord:
    return GlossaryRecord(
        logical_ref=f"{source}::{schema}.{table}.{column}",
        term_name=spec["term_name"],
        definition=spec["definition"],
        domain=spec.get("domain", ""),
        synonyms=tuple(spec.get("synonyms", ())),
        bian_path=spec.get("bian_path", ""),
        fibo_path=spec.get("fibo_path", ""),
        term_type=spec.get("term_type", ""),
        process_path=spec.get("process_path", ""),
        related_terms=tuple(spec.get("related_terms", ())),
        schema=schema,
        physical_fqn=f"{schema}.{table}.{column}",
        declared_type=spec.get("declared_type", ""),
    )


def record_of(case: SemanticGoldCase) -> GlossaryRecord | None:
    """``None`` for the honestly-unclassified columns: they have no curated sidecar at all, which is
    the whole point of that family."""
    if case.glossary is None:
        return None
    return _record(case.catalog_source, load()["schema"], case.table, case.column, case.glossary)


@lru_cache(maxsize=1)
def table_context() -> tuple[sc.SemanticValueV1, ...]:
    ctx = load()["table_context"]
    return tuple(
        sc.SemanticValueV1(field_name=name, value=value, evidence=(), resolution_status="current")
        for name, value in ctx.items()
    )


def bundle_of(case: SemanticGoldCase) -> sc.SemanticContextBundleV1:
    return sc.bundle_from_upload(
        row_of(case),
        glossary_record=record_of(case),
        cohort=list(cohort()),
        roles=enrich._ENRICHMENT_ROLES,
        table_context=table_context(),
    )


def thin_payload(case: SemanticGoldCase) -> dict:
    """The v1/flag-off classifier payload: metadata only, no bundle. `_classifier_payload` returns
    `_concept_metadata(row, rec)` unchanged when the bundle is None."""
    return enrich._classifier_payload(row_of(case), record_of(case), None)


def rich_payload(case: SemanticGoldCase) -> dict:
    """The v4/flag-on classifier payload: the `for_concept_enrichment` purpose adapter."""
    return enrich._classifier_payload(row_of(case), record_of(case), bundle_of(case))


# ── the oracle ───────────────────────────────────────────────────────────────────────────────────


def serialize(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def oracle_answer(case: SemanticGoldCase, payload: dict) -> str:
    """The concept a careful reader reaches from THESE bytes, and nothing else.

    `requires_any`  — the discriminating evidence is present ⇒ the reviewer's concept; absent ⇒ the
                      named confusable. This is the lift axis.
    `requires_absent` — no meaning-bearing key is present ⇒ decline (`unclassified`); one IS present
                      ⇒ the named confusable. This is the precision axis: it goes wrong exactly when
                      a payload builder FABRICATES meaning the source never declared.
    """
    if case.discriminator_mode == MODE_REQUIRES_ANY:
        blob = serialize(payload)
        return case.expected_concept if any(t in blob for t in case.discriminator_terms) \
            else case.confusable_concept
    if case.discriminator_mode == MODE_REQUIRES_ABSENT:
        present = [k for k in case.discriminator_keys if payload.get(k)]
        return case.confusable_concept if present else case.expected_concept
    raise ValueError(f"unknown discriminator mode {case.discriminator_mode!r}")


def is_hit(case: SemanticGoldCase, answer: str) -> bool:
    return answer in case.acceptable_concepts


# ── the auxiliary gold sections ──────────────────────────────────────────────────────────────────


def peer_columns() -> tuple[tuple[CanonicalRow, str, GlossaryRecord], ...]:
    """The second catalog source, so cross-catalog pairing has a counterpart to pair with."""
    peer = load()["peer_catalog"]
    schema = load()["schema"]
    out = []
    for spec in peer["columns"]:
        row = CanonicalRow(peer["catalog_source"], peer["table"], spec["column"],
                           physical_type(spec["glossary"]),
                           definition=spec["glossary"]["definition"])
        rec = _record(peer["catalog_source"], schema, peer["table"], spec["column"],
                      spec["glossary"])
        out.append((row, spec["expected_concept"], rec))
    return tuple(out)


def must_not_pair() -> tuple[tuple[str, str, str], ...]:
    """The graded controls. Every one is CROSS-source, because that is the only shape
    `derive_bridge_candidates` can ever produce — see `unreachable_by_topology` below."""
    return tuple((p["left"], p["right"], p["why"])
                 for p in load()["cross_namespace_pairs"]["must_not_pair"])


def unreachable_by_topology() -> tuple[tuple[str, str, str], ...]:
    """Dangerous pairs that the DERIVATION CANNOT OFFER whatever the namespace gate does.

    They were graded as must-not-pair controls until the review of 2026-08-03 pointed out that all
    six are intra-source, and `_derive_from_identifier_columns` enumerates only
    `combinations(sources, 2)` — so "zero violations" was guaranteed by topology and said nothing
    about the gate. They are kept, honestly relabelled, and asserted to be exactly what this name
    claims: still forbidden, and refused one layer earlier than the bar is about.
    """
    return tuple((p["left"], p["right"], p["why"])
                 for p in load()["cross_namespace_pairs"]["unreachable_by_topology"])


def may_pair() -> tuple[tuple[str, str, str], ...]:
    return tuple((p["left"], p["right"], p["why"])
                 for p in load()["cross_namespace_pairs"]["may_pair"])


def column_source(column: str) -> str:
    """The catalog source a gold column belongs to — the anchor catalog or the peer."""
    for c in cases():
        if c.column == column:
            return c.catalog_source
    for row, _concept, _rec in peer_columns():
        if row.column == column:
            return row.source
    raise KeyError(f"no gold column {column!r} in either catalog source")


def retrieval_questions() -> tuple[dict, ...]:
    return tuple(load()["retrieval_questions"]["questions"])


def unsafe_feature_cases() -> tuple[dict, ...]:
    return tuple(load()["unsafe_features"]["cases"])


def witnesses() -> tuple[str, ...]:
    return tuple(load()["witnesses"])
