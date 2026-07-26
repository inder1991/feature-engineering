"""Phase-1B Task 2 — scope-record persistence (the recognition -> run -> scope lineage).

Three append-only writers/readers over the ``0974_intent_scope_records`` tables:

* :func:`record_recognition_attempt` — persists the recognizer's PROPOSAL for an intent, BEFORE any
  generation run exists. Idempotent on ``(intent_id, input_hash)``: the same intent + redacted input
  resolves to the SAME ``recognition_id`` (never a second row), so re-recognising is free.
* :func:`use_case_provenance` — derives accepted/added/overridden provenance from the immutable
  recognition attempt and the confirmed scope. The client never supplies governance provenance.
* :func:`record_confirmed_scope` — writes the human-confirmed governing scope for exactly one
  generation run (parent) plus one normalized child per accepted use-case, each stamped with its
  server-derived provenance. The proposals and choices remain independently queryable.
* :func:`scope_for_run` — the CANONICAL lookup: the governing scope for a run, by run id only (the
  ``UNIQUE(generation_run_id)`` linkage). Never latest-by-time; ``supersedes_scope_id`` is lineage only.
* :func:`confirmation_delta` — the proposed-vs-confirmed DIMENSION delta for a run: the attempt's
  proposed ``modelling_contexts``/``target_entity`` reconciled against the confirmed dimension child
  rows (accepted / rejected / added / replaced), joined by ``recognition_id``.

Computation-free and behaviour-neutral: scope persistence lives here / in the API layer, never in
``build_considered_set``. See ``docs/superpowers/plans/2026-07-10-phase1b-scoped-grounding.md`` Task 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from featuregen.contracts.identity import identity_to_jsonb
from featuregen.idgen import mint_id
from featuregen.intake.llm import compute_input_hash
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


class RecognitionInputUnavailable(ValueError):
    """The recognition predates sealed inputs or its immutable content does not verify."""


class GenerationInputUnavailable(ValueError):
    """A generation run has no sealed input or its immutable content does not verify."""


@dataclass(frozen=True, slots=True)
class RecognitionInput:
    recognition_id: str
    intent_id: str
    redacted_hypothesis: str
    redacted_prediction_goal: str
    input_content_hash: str
    redaction_policy_version: str


@dataclass(frozen=True, slots=True)
class GenerationInput:
    generation_run_id: str
    intent_id: str
    recognition_id: str
    confirmed_scope_id: str
    redacted_hypothesis: str
    redacted_definition: str
    redacted_prediction_goal: str
    redacted_feedback: str
    target_ref: str | None
    recognition_input_content_hash: str
    generation_input_content_hash: str


def recognition_input_material(
    *,
    redacted_hypothesis: str,
    redacted_prediction_goal: str,
    redaction_policy_version: str,
) -> dict[str, str]:
    """The complete redacted and policy-versioned input recognized by Gate #1."""
    return {
        "redacted_hypothesis": redacted_hypothesis,
        "redacted_prediction_goal": redacted_prediction_goal,
        "redaction_policy_version": redaction_policy_version,
    }


def record_recognition_attempt(
    conn,
    *,
    intent_id: str,
    input_hash: str,
    result: RecognitionResult,
    actor: Any,
    input_json: dict[str, str] | None = None,
    redaction_policy_version: str | None = None,
) -> str:
    """Persist the recognizer's proposal for ``intent_id`` (append-only), stamping the version quintet,
    the candidate PROPOSALS, and the optional intent DIMENSIONS (``modelling_contexts`` / ``target_entity``)
    + per-dimension ``warnings`` from ``result``. Idempotent on ``(intent_id, input_hash)``: a repeat
    ``INSERT`` is a no-op and the EXISTING ``recognition_id`` is returned, so the same intent + redacted
    input always resolves to the same attempt (never a second row)."""
    recognition_id = mint_id("rcg")
    candidates = [_candidate_json(c) for c in result.candidates]
    if input_json is not None:
        computed_hash = compute_input_hash(input_json)
        if computed_hash != input_hash:
            raise ValueError("recognition input hash does not match input_json")
        if not redaction_policy_version:
            raise ValueError("redaction_policy_version is required with input_json")
        if input_json.get("redaction_policy_version") != redaction_policy_version:
            raise ValueError("recognition input redaction policy does not match its stamp")
    conn.execute(
        "INSERT INTO intent_recognition_attempt "
        "(recognition_id, intent_id, input_hash, status, candidates, ambiguity_note, "
        "taxonomy_version, applicability_mapping_version, recognizer_model_id, prompt_version, "
        "recipe_registry_version, modelling_contexts, target_entity, warnings, created_by, "
        "input_json, input_content_hash, redaction_policy_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (intent_id, input_hash) DO NOTHING",
        (recognition_id, intent_id, input_hash, result.status.value, Jsonb(candidates),
         result.ambiguity_note, result.taxonomy_version, result.applicability_mapping_version,
         result.recognizer_model_id, result.prompt_version, result.recipe_registry_version,
         Jsonb(list(result.modelling_contexts)), result.target_entity, Jsonb(list(result.warnings)),
         Jsonb(_actor_dict(actor)), Jsonb(input_json) if input_json is not None else None,
         input_hash if input_json is not None else None, redaction_policy_version))
    # Read back the governing id for this (intent, input) — the one just inserted, or the pre-existing
    # one when the INSERT hit ON CONFLICT DO NOTHING. Either way a repeat returns the SAME id.
    row = conn.execute(
        "SELECT recognition_id, input_json, input_content_hash, redaction_policy_version "
        "FROM intent_recognition_attempt "
        "WHERE intent_id = %s AND input_hash = %s",
        (intent_id, input_hash)).fetchone()
    if input_json is not None and (
        row[1] != input_json
        or row[2] != input_hash
        or row[3] != redaction_policy_version
    ):
        raise ValueError("existing recognition attempt has different sealed input")
    return row[0]


def load_recognition_input(
    conn,
    *,
    recognition_id: str,
    intent_id: str,
) -> RecognitionInput:
    """Load and verify the exact redacted input used for one recognition attempt."""
    row = conn.execute(
        "SELECT input_json, input_content_hash, redaction_policy_version "
        "FROM intent_recognition_attempt WHERE recognition_id = %s AND intent_id = %s",
        (recognition_id, intent_id),
    ).fetchone()
    if row is None:
        raise RecognitionInputUnavailable("unknown recognition")
    material, content_hash, policy_version = row
    if material is None or content_hash is None or policy_version is None:
        raise RecognitionInputUnavailable("recognition input is not sealed")
    expected_keys = {
        "redacted_hypothesis",
        "redacted_prediction_goal",
        "redaction_policy_version",
    }
    if not isinstance(material, dict) or set(material) != expected_keys:
        raise RecognitionInputUnavailable("recognition input has an unsupported shape")
    if not all(isinstance(material[key], str) for key in expected_keys):
        raise RecognitionInputUnavailable("recognition input fields must be strings")
    if compute_input_hash(material) != content_hash:
        raise RecognitionInputUnavailable("recognition input content hash mismatch")
    if material["redaction_policy_version"] != policy_version:
        raise RecognitionInputUnavailable("recognition input redaction policy mismatch")
    return RecognitionInput(
        recognition_id=recognition_id,
        intent_id=intent_id,
        redacted_hypothesis=material["redacted_hypothesis"],
        redacted_prediction_goal=material["redacted_prediction_goal"],
        input_content_hash=content_hash,
        redaction_policy_version=policy_version,
    )


def recognition_id_for_scope(
    conn,
    *,
    scope_id: str,
    intent_id: str,
) -> str | None:
    """Resolve broaden lineage to the original recognition without trusting the request."""
    row = conn.execute(
        "SELECT recognition_id FROM confirmed_generation_scope "
        "WHERE scope_id = %s AND intent_id = %s",
        (scope_id, intent_id),
    ).fetchone()
    return row[0] if row is not None else None


def _generation_input_material(
    *,
    generation_run_id: str,
    intent_id: str,
    recognition_id: str,
    confirmed_scope_id: str,
    redacted_hypothesis: str,
    redacted_definition: str,
    redacted_prediction_goal: str,
    redacted_feedback: str,
    target_ref: str | None,
    recognition_input_content_hash: str,
) -> dict[str, Any]:
    return {
        "version": "contract-generation-input@1",
        "generation_run_id": generation_run_id,
        "intent_id": intent_id,
        "recognition_id": recognition_id,
        "confirmed_scope_id": confirmed_scope_id,
        "redacted_hypothesis": redacted_hypothesis,
        "redacted_definition": redacted_definition,
        "redacted_prediction_goal": redacted_prediction_goal,
        "redacted_feedback": redacted_feedback,
        "target_ref": target_ref,
        "recognition_input_content_hash": recognition_input_content_hash,
    }


def record_generation_input(
    conn,
    *,
    generation_run_id: str,
    intent_id: str,
    recognition: RecognitionInput,
    confirmed_scope_id: str,
    redacted_definition: str,
    redacted_feedback: str,
    target_ref: str | None,
    actor: Any,
) -> GenerationInput:
    """Seal the exact text and leakage target consumed by one confirmed-scope generation run."""
    material = _generation_input_material(
        generation_run_id=generation_run_id,
        intent_id=intent_id,
        recognition_id=recognition.recognition_id,
        confirmed_scope_id=confirmed_scope_id,
        redacted_hypothesis=recognition.redacted_hypothesis,
        redacted_definition=redacted_definition,
        redacted_prediction_goal=recognition.redacted_prediction_goal,
        redacted_feedback=redacted_feedback,
        target_ref=target_ref,
        recognition_input_content_hash=recognition.input_content_hash,
    )
    content_hash = compute_input_hash(material)
    conn.execute(
        "INSERT INTO contract_generation_input "
        "(generation_run_id, intent_id, recognition_id, confirmed_scope_id, "
        "redacted_hypothesis, redacted_definition, redacted_prediction_goal, redacted_feedback, "
        "target_ref, recognition_input_content_hash, generation_input_content_hash, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            generation_run_id,
            intent_id,
            recognition.recognition_id,
            confirmed_scope_id,
            recognition.redacted_hypothesis,
            redacted_definition,
            recognition.redacted_prediction_goal,
            redacted_feedback,
            target_ref,
            recognition.input_content_hash,
            content_hash,
            Jsonb(_actor_dict(actor)),
        ),
    )
    return GenerationInput(
        generation_run_id=generation_run_id,
        intent_id=intent_id,
        recognition_id=recognition.recognition_id,
        confirmed_scope_id=confirmed_scope_id,
        redacted_hypothesis=recognition.redacted_hypothesis,
        redacted_definition=redacted_definition,
        redacted_prediction_goal=recognition.redacted_prediction_goal,
        redacted_feedback=redacted_feedback,
        target_ref=target_ref,
        recognition_input_content_hash=recognition.input_content_hash,
        generation_input_content_hash=content_hash,
    )


def generation_input_for_run(conn, generation_run_id: str) -> GenerationInput | None:
    """Load and hash-verify a run's sealed input; return ``None`` only for explicit legacy runs."""
    row = conn.execute(
        "SELECT intent_id, recognition_id, confirmed_scope_id, redacted_hypothesis, "
        "redacted_definition, redacted_prediction_goal, redacted_feedback, target_ref, "
        "recognition_input_content_hash, generation_input_content_hash "
        "FROM contract_generation_input WHERE generation_run_id = %s",
        (generation_run_id,),
    ).fetchone()
    if row is None:
        return None
    (
        intent_id,
        recognition_id,
        scope_id,
        hypothesis,
        definition,
        goal,
        feedback,
        target_ref,
        recognition_hash,
        content_hash,
    ) = row
    material = _generation_input_material(
        generation_run_id=generation_run_id,
        intent_id=intent_id,
        recognition_id=recognition_id,
        confirmed_scope_id=scope_id,
        redacted_hypothesis=hypothesis,
        redacted_definition=definition,
        redacted_prediction_goal=goal,
        redacted_feedback=feedback,
        target_ref=target_ref,
        recognition_input_content_hash=recognition_hash,
    )
    if compute_input_hash(material) != content_hash:
        raise GenerationInputUnavailable("generation input content hash mismatch")
    return GenerationInput(
        generation_run_id=generation_run_id,
        intent_id=intent_id,
        recognition_id=recognition_id,
        confirmed_scope_id=scope_id,
        redacted_hypothesis=hypothesis,
        redacted_definition=definition,
        redacted_prediction_goal=goal,
        redacted_feedback=feedback,
        target_ref=target_ref,
        recognition_input_content_hash=recognition_hash,
        generation_input_content_hash=content_hash,
    )


def record_confirmed_scope(
    conn,
    *,
    intent_id: str,
    generation_run_id: str,
    recognition_id: str | None,
    scope: ConfirmedScope,
    use_case_origins: dict[str, str],
    use_case_proposed_relationships: dict[str, str] | None = None,
    use_case_replacements: dict[str, str] | None = None,
    confirmation_source: str,
    confirmed_by: str,
    supersedes_scope_id: str | None = None,
    dimension_sources: dict[str, str] | None = None,
    replaces: dict[str, str] | None = None,
) -> str:
    """Write the human-confirmed governing scope for ``generation_run_id`` (parent) plus one normalized
    child per accepted use-case. The primary (``relationship='primary'``, ``display_order=0``) then each
    secondary (``relationship='secondary'``, ``display_order=1..N``); each child's provenance is
    server-derived by :func:`use_case_provenance`. An ``unscoped`` scope has no primary/secondary, so
    it writes zero child rows. Raises on a duplicate ``generation_run_id`` (the UNIQUE canonical linkage).
    Returns the minted ``scope_id``.

    Phase-2B also persists the human-confirmed intent DIMENSIONS as ``confirmed_scope_dimension`` child
    rows — one per confirmed ``modelling_context`` (from ``scope.modelling_contexts``, ordered) and, if
    set, the ``target_entity`` — each stamped with rich provenance: its ``source`` from
    ``dimension_sources`` (value -> one of ``accepted_llm_proposal`` / ``user_added`` /
    ``user_replacement`` / ``project_default`` / ``organization_default``; default
    ``'accepted_llm_proposal'``) and, for a ``user_replacement``, the value it superseded from
    ``replaces`` (value -> replaced value). UNLIKE the use-case children, the confirmed dimensions
    persist for BOTH scoped and unscoped scopes: they are confirmed DATA orthogonal to use-case
    scoping, so a broaden (an ``unscoped`` "show all buildable recipes") does NOT forget the confirmed
    context — ``scope_for_run`` rebuilds them either way."""
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

    derived_origins, derived_relationships, derived_replacements = use_case_provenance(
        conn, recognition_id, scope)
    origins = {**derived_origins, **use_case_origins}
    proposed_relationships = {
        **derived_relationships, **(use_case_proposed_relationships or {})}
    replacements = {**derived_replacements, **(use_case_replacements or {})}
    for use_case_id, relationship, display_order in children:
        conn.execute(
            "INSERT INTO confirmed_scope_use_case "
            "(scope_id, use_case_id, relationship, origin, proposed_relationship, "
            "replaces_use_case_id, display_order) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (scope_id, use_case_id, relationship,
             origins.get(use_case_id, "user_added"),
             proposed_relationships.get(use_case_id), replacements.get(use_case_id),
             display_order))

    # Confirmed intent DIMENSIONS (Phase-2B), each a normalized child with rich provenance. UNLIKE the
    # use-case children, these persist for BOTH scoped and unscoped scopes: the confirmed dimensions are
    # data orthogonal to use-case scoping, so a broaden ("show all buildable recipes") does NOT forget
    # the confirmed context — B3's ranker + signal_warnings are genuinely shaped by it, so the durable
    # record must be able to reproduce the presentation. scope_for_run rebuilds them unconditionally.
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


def use_case_provenance(
    conn, recognition_id: str | None, scope: ConfirmedScope,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Derive use-case provenance from the immutable recognition proposal.

    Returns ``(origins, proposed_relationships, replacements)`` keyed by confirmed use-case id.
    A candidate retained in its proposed role is accepted, a new secondary is user-added, and any
    role change is an override. A newly selected primary also records the recognizer's displaced
    primary when one existed. Unscoped confirmation has no use-case children and therefore no child
    provenance; its action provenance is carried by the parent ``confirmation_source``.
    """
    if scope.unscoped:
        return {}, {}, {}

    candidates: list[dict[str, Any]] = []
    if recognition_id is not None:
        row = conn.execute(
            "SELECT candidates FROM intent_recognition_attempt WHERE recognition_id = %s",
            (recognition_id,),
        ).fetchone()
        if row is not None and isinstance(row[0], list):
            candidates = [c for c in row[0] if isinstance(c, dict)]

    proposed: dict[str, str] = {
        str(candidate["use_case_id"]): str(candidate["relationship"])
        for candidate in candidates
        if isinstance(candidate.get("use_case_id"), str)
        and candidate.get("relationship") in {"primary", "secondary"}
    }
    proposed_primary = next(
        (uid for uid, relationship in proposed.items() if relationship == "primary"), None)

    confirmed: list[tuple[str, str]] = []
    if scope.primary is not None:
        confirmed.append((scope.primary, "primary"))
    confirmed.extend((uid, "secondary") for uid in scope.secondary)

    origins: dict[str, str] = {}
    proposed_relationships: dict[str, str] = {}
    replacements: dict[str, str] = {}
    for use_case_id, relationship in confirmed:
        prior_relationship = proposed.get(use_case_id)
        if prior_relationship == relationship:
            origins[use_case_id] = "accepted_llm_proposal"
        elif prior_relationship is not None:
            origins[use_case_id] = "user_overridden"
            proposed_relationships[use_case_id] = prior_relationship
            if (relationship == "primary" and proposed_primary is not None
                    and proposed_primary != use_case_id):
                replacements[use_case_id] = proposed_primary
        elif relationship == "primary" and proposed_primary is not None:
            origins[use_case_id] = "user_overridden"
            replacements[use_case_id] = proposed_primary
        else:
            origins[use_case_id] = "user_added"
    return origins, proposed_relationships, replacements


def dimension_provenance(
    conn, recognition_id: str | None, scope: ConfirmedScope,
) -> tuple[dict[str, str], dict[str, str]]:
    """Reconstruct the confirmed dimensions' PROVENANCE from the IMMUTABLE recognition attempt — never
    from the client (governance cannot trust a client's provenance claims). Returns ``(sources,
    replaces)`` keyed by dimension VALUE, ready to pass straight into :func:`record_confirmed_scope`.

    Loads the recognizer's PROPOSED ``modelling_contexts`` (jsonb) + ``target_entity`` (text) from
    ``intent_recognition_attempt`` by ``recognition_id`` (``((), None)`` when ``recognition_id`` is
    ``None`` or no row exists), then stamps each CONFIRMED value:

    * a confirmed ``modelling_context`` → ``accepted_llm_proposal`` if the recognizer proposed it, else
      ``user_added``;
    * the confirmed ``target_entity`` (if set) → ``accepted_llm_proposal`` if it equals the proposed
      entity; ``user_replacement`` (with ``replaces[value] = proposed_entity``) if a DIFFERENT entity
      was proposed; ``user_added`` if no entity was proposed.

    Contexts and entities are disjoint vocabularies, so the value-keyed dicts never collide. Only the
    first three of the ``source`` CHECK values are ever emitted (the two ``*_default`` sources are not
    a recognition-vs-human distinction)."""
    proposed_contexts: tuple[str, ...] = ()
    proposed_entity: str | None = None
    if recognition_id is not None:
        row = conn.execute(
            "SELECT modelling_contexts, target_entity FROM intent_recognition_attempt "
            "WHERE recognition_id = %s", (recognition_id,)).fetchone()
        if row is not None:
            proposed_contexts = tuple(row[0] or ())
            proposed_entity = row[1]
    proposed_context_set = set(proposed_contexts)

    sources: dict[str, str] = {}
    replaces: dict[str, str] = {}
    for context in scope.modelling_contexts:
        sources[context] = (
            "accepted_llm_proposal" if context in proposed_context_set else "user_added")
    entity = scope.target_entity
    if entity is not None:
        if entity == proposed_entity:
            sources[entity] = "accepted_llm_proposal"
        elif proposed_entity:
            sources[entity] = "user_replacement"
            replaces[entity] = proposed_entity
        else:
            sources[entity] = "user_added"
    return sources, replaces


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
