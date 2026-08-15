"""Phase-1B Task 2 — scope-record persistence (the recognition -> run -> scope lineage).

Three append-only writers/readers over the ``0974_intent_scope_records`` tables:

* :func:`record_recognition_attempt` — persists the recognizer's PROPOSAL for an intent, BEFORE any
  generation run exists. Idempotent on ``(intent_id, input_hash)``, where ``input_hash`` is the
  idempotency KEY: current callers key on the full REQUEST identity (input + prompt + schema +
  taxonomy + validator + model, see 1070), legacy rows key on the redacted input alone.
* :func:`find_recognition_attempt` / :func:`load_recognition_attempt` — the stored answer, by request
  identity or by id. Reading the row back is what keeps the served payload and the returned
  ``recognition_id`` describing the same fact.
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
from featuregen.overlay.upload.taxonomy.recognition import (
    CandidateDrop,
    RecognitionDisposition,
    RecognitionQuality,
    RecognitionResult,
    RecognitionStatus,
    UseCaseCandidate,
)


def _actor_dict(actor: Any) -> dict[str, Any]:
    """The actor identity shape for a ``created_by`` jsonb column. A string subject -> ``{"subject": …}``;
    an ``IdentityEnvelope`` -> :func:`identity_to_jsonb`; anything else -> a structured ``{"repr": …}``."""
    if isinstance(actor, str):
        return {"subject": actor}
    try:
        return identity_to_jsonb(actor)
    except Exception:
        return {"repr": str(actor)}


def _drop_json(drop: CandidateDrop) -> dict[str, Any]:
    """Serialize one partition drop for the ``dropped_candidates`` jsonb (1071). ``index`` stays
    ``null`` when the whole RESULT was refused — an aggregate defect belongs to the SET, and naming a
    candidate for it would blame one that may be perfectly well formed."""
    return {"index": drop.index, "reason_code": drop.reason_code}


def _drops_from_json(raw: Any) -> tuple[CandidateDrop, ...]:
    """Rebuild the drop records from the stored jsonb, skipping anything that is not a drop record.

    Tolerant on purpose: this feeds a served LABEL, and a row whose jsonb somebody hand-edited must
    degrade to a smaller honest answer rather than 500 the recognition read path — recognition is
    fail-open at every other layer and a reader is no place to break that."""
    if not isinstance(raw, (list, tuple)):
        return ()
    drops: list[CandidateDrop] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = entry.get("reason_code")
        index = entry.get("index")
        if isinstance(code, str) and (
                index is None or (isinstance(index, int) and not isinstance(index, bool))):
            drops.append(CandidateDrop(index=index, reason_code=code))
    return tuple(drops)


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
    redacted_feedback: str = ""
    supersedes_scope_id: str | None = None


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
    redacted_feedback: str = "",
    supersedes_scope_id: str | None = None,
) -> dict[str, Any]:
    """The complete redacted and policy-versioned input recognized by Gate #1."""
    material: dict[str, Any] = {
        "redacted_hypothesis": redacted_hypothesis,
        "redacted_prediction_goal": redacted_prediction_goal,
        "redaction_policy_version": redaction_policy_version,
    }
    if redacted_feedback:
        if not supersedes_scope_id:
            raise ValueError("feedback recognition requires a superseded scope")
        material.update({
            "redacted_feedback": redacted_feedback,
            "supersedes_scope_id": supersedes_scope_id,
        })
    elif supersedes_scope_id is not None:
        raise ValueError("superseded scope is only valid for feedback recognition")
    return material


@dataclass(frozen=True, slots=True)
class StoredRecognition:
    """One PERSISTED recognition attempt, rebuilt from its row.

    The point of reading a result back rather than trusting the in-memory one is the B2 invariant:
    the ``recognition_id`` a caller returns must name the row its payload was built from. Whenever
    those two can drift — a concurrent writer winning the insert, an earlier answer already stored —
    the ROW is the fact and the in-memory result is a guess.

    ``quality`` is ``None`` for a row written before migration 1071. That is not a gap to be filled:
    "the model answered first time" and "nobody recorded whether it did" are different facts, and
    only one of them is knowable about a legacy row. Callers serve the absence."""

    recognition_id: str
    result: RecognitionResult
    llm_call_ref: str | None
    recognition_request_hash: str | None
    quality: RecognitionQuality | None = None


_ATTEMPT_COLUMNS = (
    "recognition_id, status, candidates, ambiguity_note, taxonomy_version, "
    "applicability_mapping_version, recognizer_model_id, prompt_version, recipe_registry_version, "
    "modelling_contexts, target_entity, warnings, llm_call_ref, recognition_request_hash, "
    "recognition_disposition, repair_attempt_count, dropped_candidates")


def _stored_recognition(row: Any) -> StoredRecognition:
    """Rebuild a :class:`StoredRecognition` from one ``_ATTEMPT_COLUMNS`` row."""
    candidates = tuple(
        UseCaseCandidate(
            use_case_id=str(c["use_case_id"]),
            relationship=c["relationship"],
            confidence=c["confidence"],
            evidence_spans=tuple(c.get("evidence_spans") or ()),
            rationale=str(c.get("rationale", "")),
        )
        for c in (row[2] or ()) if isinstance(c, dict))
    # 1071 writes the three quality columns together or not at all (its
    # `intent_recognition_attempt_quality_is_coherent` CHECK), so ONE of them decides whether this
    # row has a quality — reading three would invent a half-record where the constraint forbids one.
    drops = _drops_from_json(row[16])
    quality = None if row[14] is None else RecognitionQuality(
        disposition=RecognitionDisposition(row[14]),
        repair_attempts=int(row[15] or 0),
        dropped_candidate_count=len(drops),
        drop_reason_codes=tuple(dict.fromkeys(d.reason_code for d in drops)),
    )
    return StoredRecognition(
        recognition_id=row[0],
        result=RecognitionResult(
            status=RecognitionStatus(row[1]),
            candidates=candidates,
            ambiguity_note=row[3],
            taxonomy_version=row[4],
            applicability_mapping_version=row[5],
            recognizer_model_id=row[6],
            prompt_version=row[7],
            recipe_registry_version=row[8],
            modelling_contexts=tuple(row[9] or ()),
            target_entity=row[10],
            warnings=tuple(row[11] or ()),
            dropped_candidates=drops,
        ),
        llm_call_ref=row[12],
        recognition_request_hash=row[13],
        quality=quality,
    )


def load_recognition_attempt(conn, *, recognition_id: str) -> StoredRecognition | None:
    """The stored attempt by id, or ``None`` when no such row exists."""
    row = conn.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM intent_recognition_attempt WHERE recognition_id = %s",
        (recognition_id,)).fetchone()
    return None if row is None else _stored_recognition(row)


def find_recognition_attempt(
    conn, *, intent_id: str, recognition_request_hash: str,
) -> StoredRecognition | None:
    """The stored answer to an IDENTICAL request, or ``None`` if this build has never asked it.

    Keyed on the request hash COLUMN, never on ``input_hash``: a legacy row carries an input-content
    hash in ``input_hash`` and a NULL request hash, and must never be mistaken for an answer to a
    request whose identity nobody recorded. A hit here is what lets the endpoint skip the provider."""
    row = conn.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM intent_recognition_attempt "
        "WHERE intent_id = %s AND recognition_request_hash = %s",
        (intent_id, recognition_request_hash)).fetchone()
    return None if row is None else _stored_recognition(row)


def record_recognition_attempt(
    conn,
    *,
    intent_id: str,
    input_hash: str,
    result: RecognitionResult,
    actor: Any,
    input_json: dict[str, Any] | None = None,
    redaction_policy_version: str | None = None,
    input_content_hash: str | None = None,
    recognition_request_hash: str | None = None,
    llm_call_ref: str | None = None,
    quality: RecognitionQuality | None = None,
) -> str:
    """Persist the recognizer's proposal for ``intent_id`` (append-only), stamping the version quintet,
    the candidate PROPOSALS, and the optional intent DIMENSIONS (``modelling_contexts`` / ``target_entity``)
    + per-dimension ``warnings`` from ``result``. Idempotent on ``(intent_id, input_hash)``: a repeat
    ``INSERT`` is a no-op and the EXISTING ``recognition_id`` is returned, so the same intent + the same
    KEY always resolves to the same attempt (never a second row).

    ``input_hash`` is the IDEMPOTENCY KEY, and Task 0 of the recognition repair seam widened what that
    key is allowed to be. Two forms coexist, distinguished by ``recognition_request_hash`` (1070):

    * **request-identity (current)** — the caller passes ``recognition_request_hash`` and, with it,
      ``input_hash`` EQUAL to it and ``input_content_hash`` carrying the redacted-input hash on its
      own. The key then covers prompt, schema, taxonomy, validator and model, so a re-run under a
      changed contract is a NEW attempt rather than a silent alias of the old one.
    * **input-only (legacy, and every caller that has not been migrated)** — no request hash; the key
      is the redacted-input hash exactly as before, and ``input_content_hash`` defaults from it.

    ``llm_call_ref`` ties the served result to the audit row for the call that produced it, so the
    answer and its evidence are one fact rather than two rows nobody joined.

    ``quality`` (1071) records WHICH OF FIVE THINGS happened — the disposition, the repair turns the
    model was asked for, and the candidates the partition discarded. It is written AT INSERT, never
    patched in afterwards: 1024's ``intent_recognition_attempt_no_mutation`` trigger refuses UPDATE
    and DELETE on this table, so there is no "record now, complete later" path and designing one
    would mean a mutable row. ``None`` writes three NULLs, which is what a caller that did not
    observe the quality should say — legacy rows read back as ``StoredRecognition.quality is None``
    and are served as an absence rather than a fabricated ``clean``."""
    recognition_id = mint_id("rcg")
    candidates = [_candidate_json(c) for c in result.candidates]
    content_hash = input_content_hash if input_content_hash is not None else input_hash
    if recognition_request_hash is not None:
        # The DB says the same thing (1070's `intent_recognition_attempt_request_is_the_key` CHECK);
        # saying it here too means a caller learns it from a name rather than from a constraint.
        if input_hash != recognition_request_hash:
            raise ValueError(
                "a request-identity recognition must key on its own recognition_request_hash")
        if input_content_hash is None:
            raise ValueError(
                "a request-identity recognition must seal input_content_hash separately")
    if quality is not None and quality.dropped_candidate_count != len(result.dropped_candidates):
        # The DROPS are stored from the result and the served count is a projection of them, so a
        # quality describing a DIFFERENT result would be persisted as a count that silently
        # disagrees with the record it summarises. Refuse rather than store a plausible lie.
        raise ValueError("recognition quality does not describe this result's dropped candidates")
    if input_json is not None:
        computed_hash = compute_input_hash(input_json)
        if computed_hash != content_hash:
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
        "input_json, input_content_hash, redaction_policy_version, recognition_request_hash, "
        "llm_call_ref, recognition_disposition, repair_attempt_count, dropped_candidates) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s) "
        "ON CONFLICT (intent_id, input_hash) DO NOTHING",
        (recognition_id, intent_id, input_hash, result.status.value, Jsonb(candidates),
         result.ambiguity_note, result.taxonomy_version, result.applicability_mapping_version,
         result.recognizer_model_id, result.prompt_version, result.recipe_registry_version,
         Jsonb(list(result.modelling_contexts)), result.target_entity, Jsonb(list(result.warnings)),
         Jsonb(_actor_dict(actor)), Jsonb(input_json) if input_json is not None else None,
         content_hash if input_json is not None else None, redaction_policy_version,
         recognition_request_hash, llm_call_ref,
         # All three together or all three NULL — 1071's coherence CHECK says the same thing in the
         # database, so a future caller that fills in one of them learns it from a constraint.
         None if quality is None else quality.disposition.value,
         None if quality is None else quality.repair_attempts,
         None if quality is None else Jsonb([_drop_json(d) for d in result.dropped_candidates])))
    # Read back the governing id for this (intent, key) — the one just inserted, or the pre-existing
    # one when the INSERT hit ON CONFLICT DO NOTHING. Either way a repeat returns the SAME id.
    row = conn.execute(
        "SELECT recognition_id, input_json, input_content_hash, redaction_policy_version "
        "FROM intent_recognition_attempt "
        "WHERE intent_id = %s AND input_hash = %s",
        (intent_id, input_hash)).fetchone()
    if input_json is not None and (
        row[1] != input_json
        or row[2] != content_hash
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
    base_keys = {
        "redacted_hypothesis",
        "redacted_prediction_goal",
        "redaction_policy_version",
    }
    feedback_keys = base_keys | {"redacted_feedback", "supersedes_scope_id"}
    if not isinstance(material, dict) or frozenset(material) not in {
        frozenset(base_keys), frozenset(feedback_keys),
    }:
        raise RecognitionInputUnavailable("recognition input has an unsupported shape")
    if not all(isinstance(material[key], str) for key in base_keys):
        raise RecognitionInputUnavailable("recognition input fields must be strings")
    redacted_feedback = material.get("redacted_feedback", "")
    supersedes_scope_id = material.get("supersedes_scope_id")
    if (
        not isinstance(redacted_feedback, str)
        or (supersedes_scope_id is not None and not isinstance(supersedes_scope_id, str))
        or bool(redacted_feedback) != bool(supersedes_scope_id)
    ):
        raise RecognitionInputUnavailable("recognition feedback lineage is malformed")
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
        redacted_feedback=redacted_feedback,
        supersedes_scope_id=supersedes_scope_id,
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
    supersedes_generation_run_id: str | None = None
    if supersedes_scope_id is not None:
        row = conn.execute(
            "SELECT generation_run_id FROM confirmed_generation_scope WHERE scope_id = %s",
            (supersedes_scope_id,),
        ).fetchone()
        supersedes_generation_run_id = row[0] if row is not None else None
    conn.execute(
        "INSERT INTO confirmed_generation_scope "
        "(scope_id, intent_id, generation_run_id, recognition_id, supersedes_scope_id, "
        "supersedes_generation_run_id, expansion, scope_mode, confirmation_source, confirmed_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (scope_id, intent_id, generation_run_id, recognition_id, supersedes_scope_id,
         supersedes_generation_run_id,
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
