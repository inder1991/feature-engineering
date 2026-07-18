"""Phase 3 — contract authoring (catalog-grounded, audited).

From the human's chosen feature, produce a `ContractDraft`. The **structured facts** — grain, as-of
column, aggregation, derives-from — come DETERMINISTICALLY from the feature + catalog (grounded, never
invented). The LLM authors only the **definition narrative**, through the audited enrichment seam, from
column METADATA (names/types/definitions) — no data values. The draft is transient here; Phase 4 refines
it and Phase 5 persists the confirmed contract.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.enrich_llm import audited_enrich_call
from featuregen.overlay.upload.entity import find_cross_catalog_path
from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement
from featuregen.overlay.upload.join_path import find_join_path
from featuregen.overlay.upload.read_scope import allowed_sensitivities


@dataclass(frozen=True, slots=True)
class ContractDraft:
    feature_name: str
    definition: str               # LLM-authored narrative (grounded + audited)
    grain_table: str | None       # deterministic
    aggregation: str | None       # deterministic
    as_of_column: str | None      # deterministic — the grain table's as-of column
    derives_from: list[str]       # deterministic — the columns the feature reads
    target_ref: str | None = None  # M3: the prediction target travels WITH the draft, so the MCV
    #                                leakage check at author/confirm can never silently no-op
    # B3: (catalog_source, object_ref) carried from the FeatureIdea — no ambiguous re-derivation.
    derives_pairs: tuple[tuple[str, str], ...] = ()
    # The deterministic join path grain -> derived tables (the no-DB-honesty piece); [] if single-table
    # or cross-catalog (cross-catalog join-path authoring rides find_cross_catalog_path, follow-up).
    join_path: tuple[dict, ...] = ()
    # 3A-ii: the honest tri-state carried from the chosen FeatureIdea, so a NEEDS_EXTERNAL_VALIDATION
    # feature reaches confirm/persistence honestly (never silently DESIGN_CHECKED). This is a SEPARATE
    # axis from the hyphenated `verification` stamp; underscore VALIDATION_STATES vocabulary.
    validation_status: str = "DESIGN_CHECKED"
    requirements: tuple[Requirement, ...] = ()


def _as_of_column(conn, grain_table: str | None, catalog_source: str | None) -> str | None:
    # Catalog-scoped (B3): the as-of column of the grain table IN its catalog, not any catalog's.
    if not grain_table or not catalog_source:
        return None
    row = conn.execute(
        "SELECT column_name FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND is_as_of = true LIMIT 1", (catalog_source, grain_table)).fetchone()
    return row[0] if row else None


def _column_defs(conn, pairs: tuple[tuple[str, str], ...], roles: Iterable[str]) -> list[dict]:
    # Read-scope (M1) + catalog-scope (B3): only the EXACT (catalog_source, object_ref) columns the
    # feature reads, never a same-named column from another catalog.
    if not pairs:
        return []
    refs = [ref for _, ref in pairs]
    rows = conn.execute(
        "SELECT catalog_source, object_ref, column_name, concept, definition FROM graph_node "
        "WHERE object_ref = ANY(%s) AND (sensitivity IS NULL OR sensitivity = ANY(%s))",
        (refs, allowed_sensitivities(roles))).fetchall()
    wanted = set(pairs)
    return [{"object_ref": r[1], "column": r[2], "concept": r[3], "definition": r[4]}
            for r in rows if (r[0], r[1]) in wanted]


def _join_path(conn, grain_table: str | None, pairs: tuple[tuple[str, str], ...],
               roles: Iterable[str] = ()) -> tuple[dict, ...]:
    """The deterministic join path from the grain table to each other table the feature reads. Single-
    catalog uses the column-level `find_join_path`; CROSS-catalog uses `entity.find_cross_catalog_path`,
    so a feature spanning catalogs records how its tables bridge via the shared entity (Customer)."""
    if not grain_table or not pairs:
        return ()
    tables = sorted({(cs, ref.split(".")[-2]) for cs, ref in pairs if ref.count(".") >= 2})
    if not tables:
        return ()
    catalogs = {cs for cs, _ in tables}
    steps: list[dict] = []
    if len(catalogs) == 1:                              # single-catalog: column-level path
        catalog = next(iter(catalogs))
        for _, t in tables:
            if t != grain_table:
                for s in (find_join_path(conn, catalog, grain_table, t, roles=roles) or []):
                    steps.append({"kind": "join", "from": s.from_ref, "to": s.to_ref,
                                  "cardinality": s.cardinality})
        return tuple(steps)
    # cross-catalog: bridge each other-catalog table to the grain via the entity graph (wires entity.py)
    grain_catalog = next((cs for cs, t in tables if t == grain_table), tables[0][0])
    for cs, t in tables:
        if (cs, t) != (grain_catalog, grain_table):
            for xs in (find_cross_catalog_path(conn, grain_catalog, grain_table, cs, t,
                                               roles=roles) or []):   # CrossStep, not JoinStep
                steps.append({"kind": xs.kind, "from": f"{xs.from_source}.{xs.from_table}",
                              "to": f"{xs.to_source}.{xs.to_table}", "via": xs.detail})
    return tuple(steps)


def draft_contract(conn, feature: FeatureIdea, client: LLMClient, *, actor=None,
                   roles: Iterable[str] = (), target_ref: str | None = None) -> ContractDraft:
    """Author a contract draft for the chosen feature. Structured facts deterministic (catalog-scoped via
    the feature's resolved pairs, B3); the definition narrative LLM-authored via the audited seam (metadata
    only, read-scoped). `target_ref` is carried on the draft so the leakage check cannot no-op."""
    catalogs = {cs for cs, _ in feature.derives_pairs}
    grain_catalog = next(iter(catalogs)) if len(catalogs) == 1 else None   # single-catalog grain
    definition = audited_enrich_call(
        conn, client, task="overlay.contract.draft", prompt_id="overlay_contract_v1",
        schema_id="overlay_contract",
        catalog_metadata={"feature": feature.name, "aggregation": feature.aggregation or "",
                          "columns": _column_defs(conn, feature.derives_pairs, roles)},
        out_key="definition",
        instruction="Write a concise business definition of this feature from its columns and "
                    "aggregation — what it measures and at what grain. Metadata only; no data values.",
        actor=actor) or ""
    return ContractDraft(
        feature_name=feature.name, definition=definition, grain_table=feature.grain_table,
        aggregation=feature.aggregation,
        as_of_column=_as_of_column(conn, feature.grain_table, grain_catalog),
        derives_from=list(feature.derives_from), target_ref=target_ref,
        derives_pairs=feature.derives_pairs,
        join_path=_join_path(conn, feature.grain_table, feature.derives_pairs, roles),
        validation_status=feature.validation_status, requirements=feature.requirements)
