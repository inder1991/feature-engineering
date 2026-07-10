"""Phase-1B Task 2 — scope-record persistence (the recognition -> run -> scope lineage).

Three append-only writers/readers over the ``0974_intent_scope_records`` tables:

* :func:`record_recognition_attempt` — persists the recognizer's PROPOSAL for an intent, BEFORE any
  generation run exists. Idempotent on ``(intent_id, input_hash)``: the same intent + redacted input
  resolves to the SAME ``recognition_id`` (never a second row), so re-recognising is free.
* :func:`record_confirmed_scope` — writes the human-confirmed governing scope for exactly one
  generation run (parent) plus one normalized child per accepted use-case, each stamped with its
  ``origin`` (``llm_proposed``/``user_added``/``user_overridden``). The proposals (on the attempt) and
  the choices (child rows) are both retained, joined by ``recognition_id`` — so the proposed-vs-accepted
  delta stays queryable.
* :func:`scope_for_run` — the CANONICAL lookup: the governing scope for a run, by run id only (the
  ``UNIQUE(generation_run_id)`` linkage). Never latest-by-time; ``supersedes_scope_id`` is lineage only.

Computation-free and behaviour-neutral: scope persistence lives here / in the API layer, never in
``build_considered_set``. See ``docs/superpowers/plans/2026-07-10-phase1b-scoped-grounding.md`` Task 2.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from featuregen.contracts.identity import identity_to_jsonb
from featuregen.idgen import mint_id
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope, ScopeExpansion
from featuregen.overlay.upload.taxonomy.recognition import RecognitionResult, UseCaseCandidate


def _actor_dict(actor: Any) -> dict[str, Any]:
    """The actor identity shape for a ``created_by`` jsonb column. A string subject -> ``{"subject": …}``;
    an ``IdentityEnvelope`` -> :func:`identity_to_jsonb`; anything else -> a structured ``{"repr": …}``."""
    if isinstance(actor, str):
        return {"subject": actor}
    try:
        return identity_to_jsonb(actor)
    except Exception:
        return {"repr": str(actor)}


def _candidate_json(candidate: UseCaseCandidate) -> dict[str, Any]:
    """Serialize one recognizer proposal for the ``candidates`` jsonb — the retained PROPOSAL half of
    the proposed-vs-accepted delta (``evidence_spans`` tuple -> a JSON list)."""
    return {
        "use_case_id": candidate.use_case_id,
        "relationship": candidate.relationship,
        "confidence": candidate.confidence,
        "evidence_spans": list(candidate.evidence_spans),
        "rationale": candidate.rationale,
    }


def record_recognition_attempt(
    conn,
    *,
    intent_id: str,
    input_hash: str,
    result: RecognitionResult,
    actor: Any,
) -> str:
    """Persist the recognizer's proposal for ``intent_id`` (append-only), stamping the version quintet,
    the candidate PROPOSALS, and the optional intent DIMENSIONS (``modelling_contexts`` / ``target_entity``)
    + per-dimension ``warnings`` from ``result``. Idempotent on ``(intent_id, input_hash)``: a repeat
    ``INSERT`` is a no-op and the EXISTING ``recognition_id`` is returned, so the same intent + redacted
    input always resolves to the same attempt (never a second row)."""
    recognition_id = mint_id("rcg")
    candidates = [_candidate_json(c) for c in result.candidates]
    conn.execute(
        "INSERT INTO intent_recognition_attempt "
        "(recognition_id, intent_id, input_hash, status, candidates, ambiguity_note, "
        "taxonomy_version, applicability_mapping_version, recognizer_model_id, prompt_version, "
        "recipe_registry_version, modelling_contexts, target_entity, warnings, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (intent_id, input_hash) DO NOTHING",
        (recognition_id, intent_id, input_hash, result.status.value, Jsonb(candidates),
         result.ambiguity_note, result.taxonomy_version, result.applicability_mapping_version,
         result.recognizer_model_id, result.prompt_version, result.recipe_registry_version,
         Jsonb(list(result.modelling_contexts)), result.target_entity, Jsonb(list(result.warnings)),
         Jsonb(_actor_dict(actor))))
    # Read back the governing id for this (intent, input) — the one just inserted, or the pre-existing
    # one when the INSERT hit ON CONFLICT DO NOTHING. Either way a repeat returns the SAME id.
    row = conn.execute(
        "SELECT recognition_id FROM intent_recognition_attempt "
        "WHERE intent_id = %s AND input_hash = %s",
        (intent_id, input_hash)).fetchone()
    return row[0]


def record_confirmed_scope(
    conn,
    *,
    intent_id: str,
    generation_run_id: str,
    recognition_id: str | None,
    scope: ConfirmedScope,
    use_case_origins: dict[str, str],
    confirmation_source: str,
    confirmed_by: str,
    supersedes_scope_id: str | None = None,
) -> str:
    """Write the human-confirmed governing scope for ``generation_run_id`` (parent) plus one normalized
    child per accepted use-case. The primary (``relationship='primary'``, ``display_order=0``) then each
    secondary (``relationship='secondary'``, ``display_order=1..N``); each child's ``origin`` comes from
    ``use_case_origins`` (default ``'llm_proposed'``). An ``unscoped`` scope has no primary/secondary, so
    it writes zero child rows. Raises on a duplicate ``generation_run_id`` (the UNIQUE canonical linkage).
    Returns the minted ``scope_id``."""
    scope_id = mint_id("scp")
    conn.execute(
        "INSERT INTO confirmed_generation_scope "
        "(scope_id, intent_id, generation_run_id, recognition_id, supersedes_scope_id, expansion, "
        "scope_mode, confirmation_source, confirmed_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (scope_id, intent_id, generation_run_id, recognition_id, supersedes_scope_id,
         scope.expansion.value, "unscoped" if scope.unscoped else "scoped",
         confirmation_source, confirmed_by))

    children: list[tuple[str, str, int]] = []
    if not scope.unscoped:
        # An unscoped scope grounds every recipe (fail-open) and confirms no use-cases → ZERO child rows,
        # even if a stray primary/secondary rode in on the value object (see docstring). Guarding here
        # keeps the persisted rows consistent with ``scope_mode='unscoped'`` and with ``scope_for_run``,
        # which rebuilds an unscoped scope as ``ConfirmedScope(primary=None, secondary=(), unscoped=True)``.
        if scope.primary is not None:
            children.append((scope.primary, "primary", 0))
        for order, use_case_id in enumerate(scope.secondary, start=1):
            children.append((use_case_id, "secondary", order))

    for use_case_id, relationship, display_order in children:
        conn.execute(
            "INSERT INTO confirmed_scope_use_case "
            "(scope_id, use_case_id, relationship, origin, display_order) "
            "VALUES (%s, %s, %s, %s, %s)",
            (scope_id, use_case_id, relationship,
             use_case_origins.get(use_case_id, "llm_proposed"), display_order))
    return scope_id


def scope_for_run(conn, generation_run_id: str) -> ConfirmedScope | None:
    """The governing :class:`ConfirmedScope` for a run — looked up by ``generation_run_id`` ONLY (the
    ``UNIQUE`` canonical linkage), never latest-by-time. Returns ``None`` if the run has no scope. The
    child rows rebuild the primary (the single ``'primary'`` child, or ``None``) and the ordered
    secondary tuple; ``scope_mode='unscoped'`` -> ``unscoped=True`` (and no children)."""
    parent = conn.execute(
        "SELECT scope_id, expansion, scope_mode FROM confirmed_generation_scope "
        "WHERE generation_run_id = %s",
        (generation_run_id,)).fetchone()
    if parent is None:
        return None
    scope_id, expansion, scope_mode = parent
    children = conn.execute(
        "SELECT use_case_id, relationship FROM confirmed_scope_use_case "
        "WHERE scope_id = %s ORDER BY display_order",
        (scope_id,)).fetchall()
    primary = next((uc for uc, rel in children if rel == "primary"), None)
    secondary = tuple(uc for uc, rel in children if rel == "secondary")
    return ConfirmedScope(
        primary=primary,
        secondary=secondary,
        expansion=ScopeExpansion(expansion),
        unscoped=(scope_mode == "unscoped"))
