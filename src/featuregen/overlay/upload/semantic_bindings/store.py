"""D2 — the thin store wrapper over D1's ``store_projection``.

Maps the deterministic shortlist output (``SemanticBindingCandidate`` tuple) onto D1's immutable
``CandidateInput`` set, persists it via :func:`persist_candidate_set` (reusing D1's deterministic id
minting + ``completion_status``), and runs the CAS current projection. NO new persistence logic —
D1 owns the WORM store, the idempotent replay, and the tombstone/unverifiable lifecycle; this module
only translates the D2 contract into D1's and threads the fingerprint so a re-shortlist with
unchanged inputs is a no-op replay that lands on the SAME current set.

The proposal LINK (``semantic_binding_candidate_proposal``) is written by :mod:`propose` AFTER
``propose_fact`` succeeds; :func:`link_proposal` is the single insert helper it uses.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from featuregen.contracts import DbConn
from featuregen.overlay.upload.semantic_bindings.shortlist import PassBColumn, PassCIdentifier
from featuregen.overlay.upload.semantic_bindings.store_projection import (
    DETERMINISTIC_TASK_VERSION,
    CandidateInput,
    PersistResult,
    ProjectionOutcome,
    mint_candidate_id,
    persist_candidate_set,
    project_current_set,
    table_metadata_fingerprint,
    table_view_material,
)
from featuregen.overlay.upload.semantic_bindings.types import (
    CURRENCY_BINDING,
    SemanticBindingCandidate,
)

# D2 is deterministic + LLM-free: the provenance versions are FIXED constants (no model / prompt).
# The shortlist/task version is the SAME constant the current-set CAS uses to gate eligibility
# (I-A) — sourced from store_projection so the two can never drift apart.
DEFAULT_SHORTLIST_VERSION = DETERMINISTIC_TASK_VERSION
DEFAULT_SCHEMA_VERSION = "d2-schema-v1"
DEFAULT_CONFIG_VERSION = "d2-config-v1"
DETERMINISTIC_MODEL_VERSION = "deterministic"
NO_PROMPT_VERSION = "n/a"


@dataclass(frozen=True, slots=True)
class StoreResult:
    persist: PersistResult
    projection: ProjectionOutcome | None
    fingerprint: str


def table_graph_ref(table_view: object) -> str:
    """The table's graph ref (dotted ``schema.table`` — matches the D1 set-level ``table_graph_ref``
    and the column-level ``schema.table.column`` graph refs on the candidates)."""
    return f"{table_view.schema}.{table_view.table}"


def _passb_material(pass_b: Mapping[str, PassBColumn] | None) -> list[dict[str, object]]:
    return sorted(
        ({"logical_ref": k, "is_measure": v.is_measure, "is_currency": v.is_currency,
          "is_grain": v.is_grain, "is_as_of": v.is_as_of} for k, v in (pass_b or {}).items()),
        key=lambda d: str(d["logical_ref"]))


def _passc_material(pass_c: Mapping[str, PassCIdentifier] | None) -> list[dict[str, object]]:
    return sorted(
        ({"logical_ref": k, "join_key_eligible": v.join_key_eligible, "entity": v.entity}
         for k, v in (pass_c or {}).items()),
        key=lambda d: str(d["logical_ref"]))


def table_fingerprint(
    table_view: object,
    *,
    pass_b: Mapping[str, PassBColumn] | None = None,
    pass_c: Mapping[str, PassCIdentifier] | None = None,
    shortlist_version: str = DEFAULT_SHORTLIST_VERSION,
    config_version: str = DEFAULT_CONFIG_VERSION,
) -> str:
    """The versioned ingestion-stage metadata fingerprint (D1's ``sbf-v1``) for this table — a pure
    hash of the bounded table material + the Pass B/Pass C inputs the shortlist read. Deterministic:
    unchanged inputs → the same fingerprint, so the CAS keeps a matching set current on re-run."""
    return table_metadata_fingerprint(
        table_material=table_view_material(table_view),
        passb_dispositions=_passb_material(pass_b), passc_identifiers=_passc_material(pass_c),
        shortlist_version=shortlist_version, config_version=config_version)


def to_candidate_input(
    candidate: SemanticBindingCandidate,
    *,
    model_version: str = DETERMINISTIC_MODEL_VERSION,
    prompt_version: str = NO_PROMPT_VERSION,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
    config_version: str = DEFAULT_CONFIG_VERSION,
) -> CandidateInput:
    """Map ONE D2 candidate onto D1's ``CandidateInput`` (the kind shape is exactly D1's: currency
    carries a target column + no free value; entity carries a registry value + no target)."""
    if candidate.binding_kind == CURRENCY_BINDING:
        target = candidate.target
        target_graph = target.graph_ref if target is not None else None
        target_logical = target.logical_ref if target is not None else None
        proposed_value: object | None = None
    else:  # entity_assignment
        target_graph = None
        target_logical = None
        proposed_value = {"entity_id": candidate.entity_id}
    return CandidateInput(
        binding_kind=candidate.binding_kind,
        subject_graph_ref=candidate.subject.graph_ref,
        subject_logical_ref=candidate.subject.logical_ref,
        input_hash=candidate.input_hash, disposition=candidate.disposition,
        model_version=model_version, prompt_version=prompt_version,
        schema_version=schema_version, config_version=config_version,
        target_graph_ref=target_graph, target_logical_ref=target_logical,
        proposed_value=proposed_value, reason_codes=candidate.reason_codes,
        evidence_json=candidate.evidence.to_json())


def candidate_id_for(
    candidate: SemanticBindingCandidate, *, candidate_set_id: str,
) -> str:
    """The D1 deterministic ``candidate_id`` for a candidate in a persisted set — the same id
    :func:`persist_candidate_set` minted, so :mod:`propose` can link the proposal to the exact row."""
    return mint_candidate_id(
        candidate_set_id=candidate_set_id, binding_kind=candidate.binding_kind,
        subject_graph_ref=candidate.subject.graph_ref,
        target_graph_ref=candidate.target.graph_ref if candidate.target is not None else None,
        input_hash=candidate.input_hash)


def store_shortlist(
    conn: DbConn,
    *,
    table_view: object,
    candidates: Sequence[SemanticBindingCandidate],
    catalog_source: str,
    ingestion_run_id: str,
    attempt_no: int,
    metadata_input_fingerprint: str | None = None,
    pass_b: Mapping[str, PassBColumn] | None = None,
    pass_c: Mapping[str, PassCIdentifier] | None = None,
    completion_status: str = "complete",
    task_version: str = DEFAULT_SHORTLIST_VERSION,
    prompt_version: str = NO_PROMPT_VERSION,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
    config_version: str = DEFAULT_CONFIG_VERSION,
    model_version: str = DETERMINISTIC_MODEL_VERSION,
    project: bool = True,
    created_at: datetime | None = None,
    now: datetime | None = None,
) -> StoreResult:
    """Persist the shortlist as ONE immutable D1 candidate set + (optionally) run the CAS current
    projection. Idempotent by construction — the fingerprint + the candidate content are
    deterministic, so a re-store of unchanged inputs is a D1 replay (no new set) that re-projects the
    SAME current set. ALL candidates (including ``rejected``) are persisted — never silently dropped.

    ``metadata_input_fingerprint`` defaults to :func:`table_fingerprint` over the same inputs, so the
    projection's ``table_fingerprint_now`` matches the set's stored fingerprint (a set built against
    the live table stays ``current``)."""
    fingerprint = metadata_input_fingerprint or table_fingerprint(
        table_view, pass_b=pass_b, pass_c=pass_c, shortlist_version=task_version,
        config_version=config_version)
    tgr = table_graph_ref(table_view)
    inputs = [to_candidate_input(c, model_version=model_version, prompt_version=prompt_version,
                                 schema_version=schema_version, config_version=config_version)
              for c in candidates]
    persist = persist_candidate_set(
        conn, catalog_source=catalog_source, table_graph_ref=tgr,
        ingestion_run_id=ingestion_run_id, attempt_no=attempt_no,
        metadata_input_fingerprint=fingerprint, task_version=task_version,
        prompt_version=prompt_version, schema_version=schema_version, config_version=config_version,
        completion_status=completion_status, candidates=inputs, created_at=created_at)
    projection: ProjectionOutcome | None = None
    if project:
        projection = project_current_set(
            conn, catalog_source=catalog_source, table_graph_ref=tgr,
            candidate_set_id=persist.candidate_set_id, table_fingerprint_now=fingerprint, now=now)
    return StoreResult(persist=persist, projection=projection, fingerprint=fingerprint)


def link_proposal(
    conn: DbConn, *, candidate_id: str, fact_key: str, proposed_event_id: str,
) -> None:
    """Insert the candidate → governed-fact LINK (``semantic_binding_candidate_proposal``). Called
    by :mod:`propose` ONLY after ``propose_fact`` succeeds. Insert-only + idempotent
    (``ON CONFLICT DO NOTHING`` on the candidate_id PK)."""
    conn.execute(
        "INSERT INTO semantic_binding_candidate_proposal (candidate_id, fact_key, proposed_event_id) "
        "VALUES (%s, %s, %s) ON CONFLICT (candidate_id) DO NOTHING",
        (candidate_id, fact_key, proposed_event_id))
