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
* :func:`confirmation_delta` — the proposed-vs-confirmed DIMENSION delta for a run: the attempt's
  proposed ``modelling_contexts``/``target_entity`` reconciled against the confirmed dimension child
  rows (accepted / rejected / added / replaced), joined by ``recognition_id``.

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
    dimension_sources: dict[str, str] | None = None,
    replaces: dict[str, str] | None = None,
) -> str:
    """Write the human-confirmed governing scope for ``generation_run_id`` (parent) plus one normalized
    child per accepted use-case. The primary (``relationship='primary'``, ``display_order=0``) then each
    secondary (``relationship='secondary'``, ``display_order=1..N``); each child's ``origin`` comes from
    ``use_case_origins`` (default ``'llm_proposed'``). An ``unscoped`` scope has no primary/secondary, so
    it writes zero child rows. Raises on a duplicate ``generation_run_id`` (the UNIQUE canonical linkage).
    Returns the minted ``scope_id``.

    Phase-2B also persists the human-confirmed intent DIMENSIONS as ``confirmed_scope_dimension`` child
    rows — one per confirmed ``modelling_context`` (from ``scope.modelling_contexts``, ordered) and, if
    set, the ``target_entity`` — each stamped with rich provenance: its ``source`` from
    ``dimension_sources`` (value -> one of ``accepted_llm_proposal`` / ``user_added`` /
    ``user_replacement`` / ``project_default`` / ``organization_default``; default
    ``'accepted_llm_proposal'``) and, for a ``user_replacement``, the value it superseded from
    ``replaces`` (value -> replaced value). Like the use-case children, an ``unscoped`` scope writes
    zero dimension rows."""
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

    # Confirmed intent dimensions (Phase-2B), each a normalized child with rich provenance. Skipped for
    # an unscoped scope (no confirmed dimensions), consistent with the use-case children and with
    # scope_for_run rebuilding an unscoped scope as ``()``/``None`` dimensions.
    if not scope.unscoped:
        sources = dimension_sources or {}
        replaced = replaces or {}
        dimension_rows: list[tuple[str, str, str, str | None, int]] = [
            ("modelling_context", context, sources.get(context, "accepted_llm_proposal"),
             replaced.get(context), order)
            for order, context in enumerate(scope.modelling_contexts)
        ]
        if scope.target_entity is not None:
            dimension_rows.append((
                "target_entity", scope.target_entity,
                sources.get(scope.target_entity, "accepted_llm_proposal"),
                replaced.get(scope.target_entity), 0))
        for dimension, value, source, replaces_value, display_order in dimension_rows:
            conn.execute(
                "INSERT INTO confirmed_scope_dimension "
                "(scope_id, dimension, value, source, replaces_value, display_order) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (scope_id, dimension, value, source, replaces_value, display_order))
    return scope_id


def scope_for_run(conn, generation_run_id: str) -> ConfirmedScope | None:
    """The governing :class:`ConfirmedScope` for a run — looked up by ``generation_run_id`` ONLY (the
    ``UNIQUE`` canonical linkage), never latest-by-time. Returns ``None`` if the run has no scope. The
    child rows rebuild the primary (the single ``'primary'`` child, or ``None``) and the ordered
    secondary tuple; ``scope_mode='unscoped'`` -> ``unscoped=True`` (and no children). The
    ``confirmed_scope_dimension`` rows rebuild the ordered ``modelling_contexts`` and the single
    ``target_entity`` (both empty when the scope confirmed no dimensions)."""
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

    # Rebuild the confirmed dimensions from the child rows: the ordered modelling_context values and the
    # single (optional) target_entity. A scope with no dimension rows rebuilds as ``()``/``None``.
    dimensions = conn.execute(
        "SELECT dimension, value FROM confirmed_scope_dimension "
        "WHERE scope_id = %s ORDER BY dimension, display_order",
        (scope_id,)).fetchall()
    modelling_contexts = tuple(v for d, v in dimensions if d == "modelling_context")
    target_entity = next((v for d, v in dimensions if d == "target_entity"), None)

    return ConfirmedScope(
        primary=primary,
        secondary=secondary,
        expansion=ScopeExpansion(expansion),
        unscoped=(scope_mode == "unscoped"),
        modelling_contexts=modelling_contexts,
        target_entity=target_entity)


def confirmation_delta(conn, generation_run_id: str) -> dict[str, Any]:
    """The proposed-vs-confirmed DIMENSION delta for a run: reconcile the confirmed
    ``confirmed_scope_dimension`` values against the linked recognition attempt's PROPOSED
    ``modelling_contexts``/``target_entity`` (joined via ``confirmed_generation_scope.recognition_id``).

    Returns ``{"accepted": [...], "rejected": [...], "added": [...], "replaced": [{"from":.., "to":..}]}``
    over the flat set of dimension *values*:

    * ``accepted`` — proposed ∩ confirmed (the human kept the LLM's proposal);
    * ``rejected`` — proposed − confirmed, EXCLUDING values that were superseded by a replacement (those
      surface in ``replaced``, not as a bare rejection);
    * ``added`` — confirmed − proposed (a value the human introduced, incl. a replacement's new value);
    * ``replaced`` — one ``{"from": replaces_value, "to": value}`` per confirmed row carrying a
      ``replaces_value`` (a ``user_replacement``).

    Returns all-empty lists for an unknown run. A scope with no linked recognition (``recognition_id``
    NULL) has no proposals, so every confirmed value reads as ``added``."""
    row = conn.execute(
        "SELECT s.scope_id, a.modelling_contexts, a.target_entity "
        "FROM confirmed_generation_scope s "
        "LEFT JOIN intent_recognition_attempt a ON a.recognition_id = s.recognition_id "
        "WHERE s.generation_run_id = %s",
        (generation_run_id,)).fetchone()
    if row is None:
        return {"accepted": [], "rejected": [], "added": [], "replaced": []}
    scope_id, proposed_contexts, proposed_entity = row

    proposed: set[str] = set(proposed_contexts or [])
    if proposed_entity:
        proposed.add(proposed_entity)

    dimensions = conn.execute(
        "SELECT value, replaces_value FROM confirmed_scope_dimension WHERE scope_id = %s",
        (scope_id,)).fetchall()
    confirmed: set[str] = {value for value, _replaces in dimensions}
    replaced = [{"from": rep, "to": value} for value, rep in dimensions if rep is not None]
    replaced_from = {rep for _value, rep in dimensions if rep is not None}

    return {
        "accepted": sorted(proposed & confirmed),
        "rejected": sorted((proposed - confirmed) - replaced_from),
        "added": sorted(confirmed - proposed),
        "replaced": replaced,
    }
