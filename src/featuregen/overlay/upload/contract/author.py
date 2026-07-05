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
from featuregen.overlay.upload.feature_assist import FeatureIdea
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


def _as_of_column(conn, grain_table: str | None) -> str | None:
    if not grain_table:
        return None
    row = conn.execute(
        "SELECT column_name FROM graph_node WHERE table_name = %s AND is_as_of = true LIMIT 1",
        (grain_table,)).fetchone()
    return row[0] if row else None


def _column_defs(conn, object_refs: list[str], roles: Iterable[str]) -> list[dict]:
    # Read-scope (M1): never feed a sensitivity-tagged column the caller can't see to the LLM — same
    # guard the discovery loop applies in _candidate_columns.
    if not object_refs:
        return []
    rows = conn.execute(
        "SELECT object_ref, column_name, concept, definition FROM graph_node "
        "WHERE object_ref = ANY(%s) AND (sensitivity IS NULL OR sensitivity = ANY(%s))",
        (object_refs, allowed_sensitivities(roles))).fetchall()
    return [{"object_ref": r[0], "column": r[1], "concept": r[2], "definition": r[3]} for r in rows]


def draft_contract(conn, feature: FeatureIdea, client: LLMClient, *, actor=None,
                   roles: Iterable[str] = (), target_ref: str | None = None) -> ContractDraft:
    """Author a contract draft for the chosen feature. Structured facts deterministic; the definition
    narrative LLM-authored via the audited seam (metadata only, read-scoped by roles). `target_ref`
    (the prediction target) is carried on the draft so the downstream leakage check cannot no-op."""
    definition = audited_enrich_call(
        conn, client, task="overlay.contract.draft", prompt_id="overlay_contract_v1",
        schema_id="overlay_contract",
        catalog_metadata={"feature": feature.name, "aggregation": feature.aggregation or "",
                          "columns": _column_defs(conn, feature.derives_from, roles)},
        out_key="definition",
        instruction="Write a concise business definition of this feature from its columns and "
                    "aggregation — what it measures and at what grain. Metadata only; no data values.",
        actor=actor) or ""
    return ContractDraft(
        feature_name=feature.name, definition=definition, grain_table=feature.grain_table,
        aggregation=feature.aggregation, as_of_column=_as_of_column(conn, feature.grain_table),
        derives_from=list(feature.derives_from), target_ref=target_ref)
