"""D1 — the persistence contract for the semantic-binding candidate store (migration 1014).

This is the write/projection layer over the four 1014 tables. It has FOUR responsibilities and
enforces the D1 invariants in code (the DB enforces them in schema — WORM triggers, kind/registry
CHECKs, deterministic-id UNIQUEs):

1. **Deterministic idempotent id minting.** ``candidate_set_id`` is a stable hash of the set's
   identity tuple (ingestion_run_id, attempt_no, catalog_source/table, metadata_input_fingerprint,
   task versions); ``candidate_id`` is a stable hash of (candidate_set_id, binding_kind, subject,
   target, input_hash). Both match the 1014 UNIQUE constraints, so REPLAYING the same attempt is a
   no-op (``ON CONFLICT DO NOTHING``). An explicit RETRY is a NEW ``attempt_no`` — a NEW id, a NEW
   immutable row that may SUPERSEDE a partial/failed attempt WITHOUT mutating it.

2. **Current-set CAS projection.** Only a ``complete`` set whose ``metadata_input_fingerprint`` still
   matches the table's live fingerprint may become ``current`` (a compare-and-swap update of
   ``current_semantic_binding_candidate_set``). A ``partial``/``failed`` set — or a set whose
   fingerprint no longer matches — makes currentness ``unverifiable`` (never silently keeping a stale
   set current for CHANGED metadata). A ``complete`` EMPTY set is an explicit TOMBSTONE: projecting it
   retires the previous set (it becomes current with zero candidates — a deliberate "no bindings"
   outcome, not a gap).

3. **Reset/rebuild — NO LLM.** :func:`rebuild_current_sets` reconstructs
   ``current_semantic_binding_candidate_set`` from the immutable candidate store alone: per table, the
   latest ``complete`` set by ``(created_at, candidate_set_id)``. Each winner's stored ``content_hash``
   is RE-VERIFIED against its candidates and a mismatch FAILS CLOSED (an impossible content-hash
   conflict — tamper/corruption — never projected). Projection loss recovers with a metadata read, not
   an LLM call.

4. **Stale linked DRAFT.** When a candidate leaves the current set,
   :func:`stale_orphaned_proposals` retires (DELETEs) any linked proposal whose governed fact is NOT
   yet VERIFIED. A VERIFIED fact is NEVER revoked here (only its own governed deps invalidate it); its
   link SURVIVES its candidate leaving — and that survival IS the durable divergence/re-review signal.

NO fact is created here — candidates only. The link to a governed fact
(``semantic_binding_candidate_proposal``) is written by D2/D4 AFTER ``propose_fact`` succeeds.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.overlay.field_evidence import canonical_hash

if TYPE_CHECKING:  # pragma: no cover - typing only (avoid a runtime import of the heavy view module)
    from featuregen.overlay.upload.column_view import TableMetadataView

# Closed registries (mirror the 1014 CHECKs — fail closed in code before the DB).
BINDING_KINDS = frozenset({"currency_binding", "entity_assignment"})
DISPOSITIONS = frozenset({"strong", "weak", "rejected"})
COMPLETION_STATUSES = frozenset({"complete", "partial", "failed"})

# Versioned fingerprint algo — an INGESTION-STAGE input hash, NOT the C0 snapshot, NOT gn-v1.
FINGERPRINT_ALGO_VERSION = "sbf-v1"
# Versioned set content-hash algo — recomputed + verified on rebuild (fail-closed on drift).
CONTENT_HASH_ALGO_VERSION = "sbc-content-v1"

_MAX_DEFINITION_LEN = 600


class SemanticBindingContentConflict(Exception):
    """Fail-closed: a deterministic ``candidate_set_id`` already exists with a DIFFERENT
    ``content_hash`` (a replay of the same attempt produced different content), or a rebuild found a
    set whose stored ``content_hash`` no longer matches its candidates (tamper/corruption). Never
    resolved by silently overwriting — the caller degrades."""


# ==================================================================================================
# Inputs
# ==================================================================================================
@dataclass(frozen=True, slots=True)
class CandidateInput:
    """One proposed semantic-binding candidate (NOT a fact). ``proposed_value`` carries the closed
    registry value for ``entity_assignment`` (e.g. ``{"entity_id": "customer"}``) and MUST be ``None``
    for ``currency_binding`` (the currency IS the ``target`` ref — no free value). ``target_*`` is the
    currency column for ``currency_binding`` and MUST be ``None`` for ``entity_assignment``."""

    binding_kind: str
    subject_graph_ref: str
    subject_logical_ref: str
    input_hash: str
    disposition: str
    model_version: str
    prompt_version: str
    schema_version: str
    config_version: str
    target_graph_ref: str | None = None
    target_logical_ref: str | None = None
    proposed_value: object | None = None
    reason_codes: Sequence[object] = ()
    evidence_json: Mapping[str, object] = field(default_factory=dict)
    llm_call_ref: str | None = None


# ==================================================================================================
# Results
# ==================================================================================================
@dataclass(frozen=True, slots=True)
class PersistResult:
    candidate_set_id: str
    inserted: bool                    # False = an idempotent replay (the set already existed)
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    status: str                       # 'current' | 'unverifiable'
    candidate_set_id: str | None


@dataclass(frozen=True, slots=True)
class RebuildResult:
    tables: int
    projected: int                    # tables set to 'current'
    unverifiable: int                 # tables set to 'unverifiable'


@dataclass(frozen=True, slots=True)
class StaleResult:
    staled: int                       # DRAFT/absent-fact links DELETEd
    diverged: int                     # VERIFIED-fact links LEFT (durable divergence signal)


# ==================================================================================================
# Deterministic ids + hashes
# ==================================================================================================
def _digest(prefix: str, material: object) -> str:
    return prefix + "_" + hashlib.sha256(
        canonical_hash(material).encode("utf-8")).hexdigest()[:32]


def mint_candidate_set_id(
    *, ingestion_run_id: str, attempt_no: int, catalog_source: str, table_graph_ref: str,
    metadata_input_fingerprint: str, task_version: str, prompt_version: str, schema_version: str,
    config_version: str,
) -> str:
    """Stable hash of exactly the 1014 set UNIQUE tuple — replaying the same attempt re-mints the SAME
    id (idempotent); a retry (new ``attempt_no``) mints a new id."""
    return _digest("sbcs", {
        "ingestion_run_id": ingestion_run_id, "attempt_no": attempt_no,
        "catalog_source": catalog_source, "table_graph_ref": table_graph_ref,
        "metadata_input_fingerprint": metadata_input_fingerprint, "task_version": task_version,
        "prompt_version": prompt_version, "schema_version": schema_version,
        "config_version": config_version,
    })


def mint_candidate_id(
    *, candidate_set_id: str, binding_kind: str, subject_graph_ref: str,
    target_graph_ref: str | None, input_hash: str,
) -> str:
    """Stable hash of exactly the 1014 candidate UNIQUE tuple — the deterministic id that backs the
    ``ON CONFLICT DO NOTHING`` idempotent replay."""
    return _digest("sbc", {
        "candidate_set_id": candidate_set_id, "binding_kind": binding_kind,
        "subject_graph_ref": subject_graph_ref, "target_graph_ref": target_graph_ref,
        "input_hash": input_hash,
    })


def _bound(text: str | None) -> str | None:
    return text[:_MAX_DEFINITION_LEN] if text else text


def table_view_material(view: TableMetadataView) -> dict:
    """The BOUNDED canonical projection of a ``TableMetadataView`` for the fingerprint — the
    identity-bearing fields ONLY (never the full row payload / sample values), columns sorted so the
    material is order-independent. Callers that assemble their own material may skip this helper."""
    columns = sorted(
        (
            {
                "column": col.column, "logical_ref": col.logical_ref,
                "operational_type": col.operational_type, "declared_type": col.declared_type,
                "term_name": col.term_name, "concept": col.concept,
                "semantic_type": col.semantic_type,
                "logical_representation": col.logical_representation,
            }
            for col in view.columns
        ),
        key=lambda c: str(c["column"]),
    )
    return {
        "source": view.source, "schema": view.schema, "table": view.table,
        "logical_ref": view.logical_ref, "term_name": view.term_name,
        "table_definition": _bound(view.table_definition), "columns": columns,
    }


def table_metadata_fingerprint(
    *, table_material: object, passb_dispositions: object, passc_identifiers: object,
    shortlist_version: str, config_version: str, algo_version: str = FINGERPRINT_ALGO_VERSION,
) -> str:
    """The versioned, canonical INGESTION-STAGE metadata fingerprint (``sbf-v1``) that keys a
    candidate set to the table state it was authored against. A hash over the bounded table material
    (:func:`table_view_material`), the validated Pass B dispositions, the Pass C identifier metadata,
    and the shortlist/config versions. NOT the C0 snapshot, NOT ``gn-v1``: this is an input hash the
    current-set CAS compares to decide whether a set is still verifiable against the live table."""
    return _digest("sbf", {
        "algo": algo_version, "table": table_material, "passb": passb_dispositions,
        "passc": passc_identifiers, "shortlist_version": shortlist_version,
        "config_version": config_version,
    })


def _set_content_hash(
    *, catalog_source: str, table_graph_ref: str, ingestion_run_id: str, attempt_no: int,
    metadata_input_fingerprint: str, task_version: str, prompt_version: str, schema_version: str,
    config_version: str, completion_status: str,
    candidates: Sequence[Mapping[str, object]],
) -> str:
    """The set's deterministic content hash — a pure function of its identity dims + its candidates'
    stored content (id, disposition, proposed_value), sorted by candidate_id. Computed at persist and
    RE-VERIFIED on rebuild: a mismatch is an impossible content-hash conflict (a replay of the same
    identity produced different content, or a stored row was tampered) and fails closed."""
    items = sorted(
        (
            {
                "candidate_id": c["candidate_id"], "disposition": c["disposition"],
                "proposed_value": c["proposed_value"],
            }
            for c in candidates
        ),
        key=lambda c: str(c["candidate_id"]),
    )
    return canonical_hash({
        "algo": CONTENT_HASH_ALGO_VERSION, "catalog_source": catalog_source,
        "table_graph_ref": table_graph_ref, "ingestion_run_id": ingestion_run_id,
        "attempt_no": attempt_no, "metadata_input_fingerprint": metadata_input_fingerprint,
        "task_version": task_version, "prompt_version": prompt_version,
        "schema_version": schema_version, "config_version": config_version,
        "completion_status": completion_status, "candidates": items,
    })


# ==================================================================================================
# Persist (immutable set + candidates)
# ==================================================================================================
def _validate_candidate_shape(c: CandidateInput) -> None:
    """Mirror the 1014 kind/registry CHECKs in code so the store fails closed with a clear message
    (the DB CHECK is the real guard). ``currency_binding`` needs a target + no free value;
    ``entity_assignment`` needs a value + no target ref."""
    if c.binding_kind not in BINDING_KINDS:
        raise ValueError(f"unknown binding_kind: {c.binding_kind!r}")
    if c.disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition: {c.disposition!r}")
    if c.binding_kind == "currency_binding":
        if c.target_graph_ref is None or c.proposed_value is not None:
            raise ValueError("currency_binding requires a target column and no free value")
    else:  # entity_assignment
        if c.target_graph_ref is not None or c.proposed_value is None:
            raise ValueError("entity_assignment requires a registry value and no target ref")


def next_attempt_no(conn: DbConn, *, ingestion_run_id: str, catalog_source: str,
                    table_graph_ref: str) -> int:
    """The next ``attempt_no`` for a (run, table) — one past the current max, or 1 for the first. A
    RETRY uses this to mint a NEW immutable set that supersedes a prior partial/failed one WITHOUT
    mutating it."""
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) FROM semantic_binding_candidate_set "
        "WHERE ingestion_run_id = %s AND catalog_source = %s AND table_graph_ref = %s",
        (ingestion_run_id, catalog_source, table_graph_ref)).fetchone()
    assert row is not None  # COALESCE(MAX(...), 0) always returns exactly one row
    return int(row[0]) + 1


def persist_candidate_set(
    conn: DbConn, *, catalog_source: str, table_graph_ref: str, ingestion_run_id: str,
    attempt_no: int, metadata_input_fingerprint: str, task_version: str, prompt_version: str,
    schema_version: str, config_version: str, completion_status: str,
    candidates: Sequence[CandidateInput], created_at: datetime | None = None,
) -> PersistResult:
    """Persist ONE immutable candidate set + its candidates (NO fact creation). Idempotent by
    construction: the deterministic ids + ``ON CONFLICT DO NOTHING`` make a replay of the SAME attempt
    a no-op. If the set id already exists with a DIFFERENT ``content_hash`` (same attempt, different
    content) it FAILS CLOSED with :class:`SemanticBindingContentConflict`."""
    if completion_status not in COMPLETION_STATUSES:
        raise ValueError(f"unknown completion_status: {completion_status!r}")
    set_id = mint_candidate_set_id(
        ingestion_run_id=ingestion_run_id, attempt_no=attempt_no, catalog_source=catalog_source,
        table_graph_ref=table_graph_ref, metadata_input_fingerprint=metadata_input_fingerprint,
        task_version=task_version, prompt_version=prompt_version, schema_version=schema_version,
        config_version=config_version)

    prepared: list[tuple[str, CandidateInput]] = []
    for c in candidates:
        _validate_candidate_shape(c)
        cid = mint_candidate_id(
            candidate_set_id=set_id, binding_kind=c.binding_kind,
            subject_graph_ref=c.subject_graph_ref, target_graph_ref=c.target_graph_ref,
            input_hash=c.input_hash)
        prepared.append((cid, c))

    content_hash = _set_content_hash(
        catalog_source=catalog_source, table_graph_ref=table_graph_ref,
        ingestion_run_id=ingestion_run_id, attempt_no=attempt_no,
        metadata_input_fingerprint=metadata_input_fingerprint, task_version=task_version,
        prompt_version=prompt_version, schema_version=schema_version, config_version=config_version,
        completion_status=completion_status,
        candidates=[{"candidate_id": cid, "disposition": c.disposition,
                     "proposed_value": c.proposed_value} for cid, c in prepared])

    params: list[object] = [
        set_id, catalog_source, table_graph_ref, ingestion_run_id, attempt_no,
        metadata_input_fingerprint, task_version, prompt_version, schema_version, config_version,
        completion_status, content_hash,
    ]
    columns = (
        "candidate_set_id, catalog_source, table_graph_ref, ingestion_run_id, attempt_no, "
        "metadata_input_fingerprint, task_version, prompt_version, schema_version, config_version, "
        "completion_status, content_hash")
    values = "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
    if created_at is not None:
        columns += ", created_at"
        values += ", %s"
        params.append(created_at)
    row = conn.execute(
        f"INSERT INTO semantic_binding_candidate_set ({columns}) VALUES ({values}) "
        "ON CONFLICT (candidate_set_id) DO NOTHING RETURNING candidate_set_id",
        tuple(params)).fetchone()
    inserted = row is not None
    if not inserted:
        existing_row = conn.execute(
            "SELECT content_hash FROM semantic_binding_candidate_set WHERE candidate_set_id = %s",
            (set_id,)).fetchone()
        assert existing_row is not None  # the ON CONFLICT proved the row already exists
        if existing_row[0] != content_hash:
            raise SemanticBindingContentConflict(
                f"candidate_set_id {set_id} already exists with a different content_hash — a replay "
                "of the same attempt produced different content (fail-closed)")

    for cid, c in prepared:
        conn.execute(
            "INSERT INTO semantic_binding_candidate "
            "(candidate_id, candidate_set_id, catalog_source, subject_graph_ref, "
            " subject_logical_ref, binding_kind, target_graph_ref, target_logical_ref, "
            " proposed_value, disposition, reason_codes, evidence_json, input_hash, model_version, "
            " prompt_version, schema_version, config_version, llm_call_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (candidate_id) DO NOTHING",
            (cid, set_id, catalog_source, c.subject_graph_ref, c.subject_logical_ref,
             c.binding_kind, c.target_graph_ref, c.target_logical_ref,
             None if c.proposed_value is None else Jsonb(c.proposed_value), c.disposition,
             Jsonb(list(c.reason_codes)), Jsonb(dict(c.evidence_json)), c.input_hash,
             c.model_version, c.prompt_version, c.schema_version, c.config_version, c.llm_call_ref))

    return PersistResult(candidate_set_id=set_id, inserted=inserted,
                         candidate_ids=tuple(cid for cid, _ in prepared))


# ==================================================================================================
# Current-set CAS projection
# ==================================================================================================
def _upsert_current(conn: DbConn, *, catalog_source: str, table_graph_ref: str,
                    candidate_set_id: str | None, fingerprint: str, status: str,
                    now: datetime) -> None:
    conn.execute(
        "INSERT INTO current_semantic_binding_candidate_set "
        "(catalog_source, table_graph_ref, candidate_set_id, metadata_input_fingerprint, status, "
        " projected_at) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (catalog_source, table_graph_ref) DO UPDATE SET "
        "candidate_set_id = EXCLUDED.candidate_set_id, "
        "metadata_input_fingerprint = EXCLUDED.metadata_input_fingerprint, "
        "status = EXCLUDED.status, projected_at = EXCLUDED.projected_at",
        (catalog_source, table_graph_ref, candidate_set_id, fingerprint, status, now))


def project_current_set(
    conn: DbConn, *, catalog_source: str, table_graph_ref: str, candidate_set_id: str,
    table_fingerprint_now: str, now: datetime | None = None,
) -> ProjectionOutcome:
    """Compare-and-swap ``current_semantic_binding_candidate_set`` for one table. The COMPARE: a set
    may become ``current`` ONLY if it is ``complete`` AND its ``metadata_input_fingerprint`` still
    equals ``table_fingerprint_now``. Otherwise currentness is ``unverifiable`` (candidate_set_id
    NULL) — a ``partial``/``failed`` set, or a set built against now-CHANGED metadata, never silently
    stays current. A ``complete`` EMPTY set is eligible: it becomes current with zero candidates,
    RETIRING the previous set (the tombstone)."""
    now = now or datetime.now(UTC)
    row = conn.execute(
        "SELECT catalog_source, table_graph_ref, completion_status, metadata_input_fingerprint "
        "FROM semantic_binding_candidate_set WHERE candidate_set_id = %s",
        (candidate_set_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown candidate_set_id: {candidate_set_id}")
    set_source, set_table, completion_status, set_fp = row
    if set_source != catalog_source or set_table != table_graph_ref:
        raise ValueError(
            f"candidate_set_id {candidate_set_id} belongs to "
            f"({set_source}, {set_table}), not ({catalog_source}, {table_graph_ref})")

    if completion_status == "complete" and set_fp == table_fingerprint_now:
        _upsert_current(conn, catalog_source=catalog_source, table_graph_ref=table_graph_ref,
                        candidate_set_id=candidate_set_id, fingerprint=table_fingerprint_now,
                        status="current", now=now)
        return ProjectionOutcome(status="current", candidate_set_id=candidate_set_id)

    _upsert_current(conn, catalog_source=catalog_source, table_graph_ref=table_graph_ref,
                    candidate_set_id=None, fingerprint=table_fingerprint_now,
                    status="unverifiable", now=now)
    return ProjectionOutcome(status="unverifiable", candidate_set_id=None)


# ==================================================================================================
# Reset / rebuild (NO LLM)
# ==================================================================================================
def rebuild_current_sets(
    conn: DbConn, *, live_fingerprints: Mapping[tuple[str, str], str] | None = None,
    now: datetime | None = None,
) -> RebuildResult:
    """Deterministically rebuild ``current_semantic_binding_candidate_set`` from the immutable
    candidate store ALONE — NO LLM call. Per (catalog_source, table_graph_ref): the LATEST ``complete``
    set by ``(created_at, candidate_set_id)``. Each winner's stored ``content_hash`` is RE-VERIFIED
    against its candidates; a mismatch FAILS CLOSED (:class:`SemanticBindingContentConflict`).

    ``live_fingerprints`` (optional) maps a table to its current metadata fingerprint: a winner whose
    fingerprint no longer matches is projected ``unverifiable`` (not silently current). Omitted → every
    winner is trusted as current (pure projection-loss recovery)."""
    now = now or datetime.now(UTC)
    winners = conn.execute(
        "SELECT DISTINCT ON (catalog_source, table_graph_ref) "
        "  candidate_set_id, catalog_source, table_graph_ref, ingestion_run_id, attempt_no, "
        "  metadata_input_fingerprint, task_version, prompt_version, schema_version, config_version, "
        "  completion_status, content_hash "
        "FROM semantic_binding_candidate_set WHERE completion_status = 'complete' "
        "ORDER BY catalog_source, table_graph_ref, created_at DESC, candidate_set_id DESC"
    ).fetchall()

    projected = unverifiable = 0
    for (set_id, cat, tbl, run_id, attempt, fp, taskv, promptv, schemav, configv,
         completion, stored_hash) in winners:
        cands = conn.execute(
            "SELECT candidate_id, disposition, proposed_value FROM semantic_binding_candidate "
            "WHERE candidate_set_id = %s", (set_id,)).fetchall()
        recomputed = _set_content_hash(
            catalog_source=cat, table_graph_ref=tbl, ingestion_run_id=run_id, attempt_no=attempt,
            metadata_input_fingerprint=fp, task_version=taskv, prompt_version=promptv,
            schema_version=schemav, config_version=configv, completion_status=completion,
            candidates=[{"candidate_id": cid, "disposition": disp, "proposed_value": val}
                        for cid, disp, val in cands])
        if recomputed != stored_hash:
            raise SemanticBindingContentConflict(
                f"rebuild: candidate_set_id {set_id} content_hash does not match its candidates — "
                "impossible content-hash conflict (tamper/corruption); fail-closed")

        live_fp = None if live_fingerprints is None else live_fingerprints.get((cat, tbl))
        if live_fingerprints is not None and live_fp != fp:
            _upsert_current(conn, catalog_source=cat, table_graph_ref=tbl, candidate_set_id=None,
                            fingerprint=live_fp or fp, status="unverifiable", now=now)
            unverifiable += 1
        else:
            _upsert_current(conn, catalog_source=cat, table_graph_ref=tbl, candidate_set_id=set_id,
                            fingerprint=fp, status="current", now=now)
            projected += 1
    return RebuildResult(tables=len(winners), projected=projected, unverifiable=unverifiable)


# ==================================================================================================
# Stale linked DRAFT
# ==================================================================================================
def stale_orphaned_proposals(
    conn: DbConn, *, catalog_source: str, table_graph_ref: str, now: datetime | None = None,
) -> StaleResult:
    """When a candidate leaves the current set, retire (DELETE) its ``semantic_binding_candidate_
    proposal`` IFF the linked governed fact is NOT VERIFIED. A VERIFIED fact is NEVER revoked here —
    its link SURVIVES (the durable divergence/re-review signal that the shortlist now disagrees with a
    human-confirmed truth). "Left the current set" = the candidate's set is not the table's current
    set (including when currentness is ``unverifiable`` — candidate_set_id NULL). Idempotent."""
    del now  # currentness is compared structurally; no clock needed (kept for a uniform signature)
    orphans = conn.execute(
        "SELECT p.candidate_id, p.fact_key "
        "FROM semantic_binding_candidate_proposal p "
        "JOIN semantic_binding_candidate c ON c.candidate_id = p.candidate_id "
        "JOIN semantic_binding_candidate_set s ON s.candidate_set_id = c.candidate_set_id "
        "LEFT JOIN current_semantic_binding_candidate_set cur "
        "  ON cur.catalog_source = s.catalog_source AND cur.table_graph_ref = s.table_graph_ref "
        "WHERE s.catalog_source = %s AND s.table_graph_ref = %s "
        "  AND (cur.candidate_set_id IS NULL OR cur.candidate_set_id <> c.candidate_set_id)",
        (catalog_source, table_graph_ref)).fetchall()

    staled = diverged = 0
    for candidate_id, fact_key in orphans:
        state = conn.execute(
            "SELECT status FROM overlay_fact_state WHERE fact_key = %s", (fact_key,)).fetchone()
        if state is not None and state[0] == "VERIFIED":
            diverged += 1
            continue
        conn.execute(
            "DELETE FROM semantic_binding_candidate_proposal WHERE candidate_id = %s",
            (candidate_id,))
        staled += 1
    return StaleResult(staled=staled, diverged=diverged)
