"""Phase-1A Task 2 — the LLM-only, fail-open use-case recognizer.

``recognize`` builds ONE ``LLMRequest`` over the *redacted* hypothesis/goal (NEVER catalog columns),
drives it through the AUDITED seam (``drive_audited_structured_call`` → ``drive_structured_call``),
then maps the outcome to a ``RecognitionResult``.

Since Task 3 of ``docs/superpowers/plans/2026-08-15-recognition-repair-seam.md`` the seam is handed
``validate_semantics=validate_recognition_output``, so the closed-taxonomy rules are enforced INSIDE
that bounded repair/retry/fail-closed runtime contract, after the registered JSON Schema. **This
docstring previously claimed that was already true; it was not** — the audited wrapper hardcoded its
validator to schema-only, and the taxonomy rules ran only in the post-call pass below. A body no JSON
Schema can fault (two primaries, a duplicated id) was therefore discarded whole with the model never
asked to fix it, which is the 2026-08-15 live incident.

It is FAIL-OPEN by construction: it NEVER raises to its caller and NEVER blocks generation. Every
technical failure — a provider failure/refusal, a validation budget exhausted (``STATUS_FAILED``), a
dispatch exception, or an unexpected mapping — folds to a candidate-free ``TECHNICAL_FAILURE`` result
so grounding continues unfiltered. A well-formed ``unscoped`` body folds to ``UNSCOPED``. The output
is already validated by ``drive_structured_call``, so the mapping is total; the guards are belt-and-
braces to honour the never-raise contract.

Behaviour-neutral: recognition only produces a result object — nothing here filters grounding or
touches ``templates.py``. See ``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 2.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from featuregen.canonical import contract_hash_v1, jcs_sha256
from featuregen.contracts import SchemaValidationError
from featuregen.contracts.contract_versions import register_contract_version
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import DEFAULT_LLM_MODEL, LLMClient
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.overlay.upload.enrich_llm import (
    ENRICHMENT_RUN_ID,
    FAILURE_KIND_SEMANTIC_INVALID,
    AuditedStructuredResult,
    canonical_output_schema,
    current_enrichment_generation_settings,
    drive_audited_structured_call,
)
from featuregen.overlay.upload.taxonomy.recognition import (
    RECOGNITION_VALIDATOR_VERSION,
    TAXONOMY_VERSION,
    RecognitionQuality,
    RecognitionResult,
    RecognitionStatus,
    UseCaseCandidate,
    normalize_dimensions,
    partition_candidates,
    recognition_quality,
    status_after_partition,
    unscoped_result,
    validate_recognition_output,
)
from featuregen.overlay.upload.taxonomy.recognizer_prompt import (
    PROMPT_ID,
    PROMPT_VERSION,
    build_recognition_prompt,
)
from featuregen.overlay.upload.taxonomy.use_cases import USE_CASE_REGISTRY, selectable_leaves
from featuregen.overlay.upload.taxonomy.versions import (
    APPLICABILITY_MAPPING_VERSION,
    RECIPE_REGISTRY_VERSION,
)

logger = logging.getLogger(__name__)

# The audited task name + the structured-output schema identity for this call. Kept equal to the
# prompt id so the recognizer reads as one coherent (task, prompt, schema) unit in the llm_call store.
RECOGNIZER_TASK = "use_case_recognition"
_OUTPUT_SCHEMA_ID = "use_case_recognition"
# v2 (2026-08-15 repair seam, Task 1): the frozen contract whose `use_case_id` carries the closed
# 88-leaf enum and whose `status` no longer offers the platform-internal `technical_failure`. This
# constant is BOTH the version dispatched (threaded to the audited seam below — its default is 1, so
# the constant alone activates nothing) and a leg of the request identity: a recognition answered
# under v1 is not an answer to the v2 question, and is deliberately not reused across the change.
_OUTPUT_SCHEMA_VERSION = 2

# The serialized identity of ONE recognition request (Task 0 of the recognition repair seam). Owned
# here because this module is where the (task, prompt, schema, validator, model) unit is assembled.
RECOGNITION_REQUEST_CONTRACT = "recognition-request"
RECOGNITION_REQUEST_VERSION = "1"
register_contract_version(RECOGNITION_REQUEST_CONTRACT, RECOGNITION_REQUEST_VERSION,
                          owner="featuregen.overlay.upload.taxonomy.recognizer")


@dataclass(frozen=True, slots=True)
class AuditedRecognition:
    """One recognition and everything the platform knows about how it got there.

    ``quality`` (repair seam, Task 5) is derived HERE rather than at the endpoint because the repair
    count only exists on the seam result: by the time a caller holds a ``RecognitionResult`` the
    number of turns the model was asked to fix its answer is gone. It is computed on EVERY arm,
    including the fail-open ones — "which of the five things happened" has an answer even when the
    answer to "what did you recognise?" is nothing."""

    result: RecognitionResult
    llm_call_ref: str | None
    provider_calls: int
    usage: dict[str, Any]
    quality: RecognitionQuality


def resolve_recognizer_model(model_id: str | None = None) -> str:
    """The model id this build would actually recognise with. ONE resolution, used both by the call
    and by the request identity that decides whether the call happens at all — an identity computed
    from a different rule than the dispatch would key a stored answer to a model that never saw it."""
    return model_id or os.environ.get("FEATUREGEN_LLM_MODEL", DEFAULT_LLM_MODEL)


def _taxonomy_content_hash() -> str:
    """The closed vocabulary the validator enforces and the prompt offers, by CONTENT.

    ``TAXONOMY_VERSION`` alone is not enough: a leaf added or renamed without a version bump changes
    which answers are reachable, and the whole point of request identity is that a stored answer is
    only reusable for a question that has not changed underneath it."""
    return jcs_sha256({
        "selectable_leaves": sorted(selectable_leaves()),
        "registry_ids": sorted(USE_CASE_REGISTRY),
    })


def recognition_request_material(*, input_content_hash: str,
                                 model_id: str | None = None) -> dict[str, Any]:
    """The complete identity of one recognition REQUEST: the redacted user input PLUS everything
    about this build that decides what the answer means.

    ``input_content_hash`` keeps its own, narrower meaning — the redacted user input and the
    redaction policy that produced it, and nothing else. The two hashes answer different questions
    ("did the user ask the same thing?" vs "would this build ask it the same way?") and NEITHER may
    absorb the other: sealed-input verification (``load_recognition_input``) must stay able to
    re-derive the input hash from the input alone, and idempotency must stop reusing an answer the
    current build would no longer give.

    Identity fields only, per ``featuregen.canonical``: no timestamps, no actor, no run ids. The
    prompt and schema enter as CONTENT hashes rather than by version, because a version is a promise
    and content is a fact — and this build's registry is mutable (``register_schema`` upserts), so a
    version number alone can mean two different bodies on two deployments."""
    return {
        "input_content_hash": input_content_hash,
        "task": RECOGNIZER_TASK,
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_content_hash": jcs_sha256({"prompt": build_recognition_prompt()}),
        "schema_id": _OUTPUT_SCHEMA_ID,
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "schema_content_hash": jcs_sha256(
            canonical_output_schema(_OUTPUT_SCHEMA_ID, _OUTPUT_SCHEMA_VERSION)),
        "taxonomy_version": TAXONOMY_VERSION,
        "taxonomy_content_hash": _taxonomy_content_hash(),
        "applicability_mapping_version": APPLICABILITY_MAPPING_VERSION,
        "recipe_registry_version": RECIPE_REGISTRY_VERSION,
        "semantic_validator_version": RECOGNITION_VALIDATOR_VERSION,
        "recognizer_model_id": resolve_recognizer_model(model_id),
        "generation_settings": current_enrichment_generation_settings(),
    }


def recognition_request_hash(*, input_content_hash: str, model_id: str | None = None) -> str:
    """The JCS contract hash of :func:`recognition_request_material` — the idempotency key for one
    recognition attempt. Deterministic and provider-free: it is computed BEFORE any dispatch, which
    is what lets an already-answered request be served from storage without calling the provider."""
    return contract_hash_v1(
        RECOGNITION_REQUEST_CONTRACT, RECOGNITION_REQUEST_VERSION,
        recognition_request_material(input_content_hash=input_content_hash, model_id=model_id))


def _result_from_output(output: Mapping[str, Any], *, model_id: str) -> RecognitionResult:
    """Map a validated recognition body to a ``RecognitionResult``. ``output`` has already passed
    ``validate_recognition_output`` (a valid enum ``status``; closed-taxonomy ids; banded
    ``relationship``/``confidence``; the shape caps), so the CORE mapping is total. The optional
    DIMENSIONS are cleaned per-dimension (non-fatally) via ``normalize_dimensions`` — an invalid
    ``modelling_context``/``target_entity`` is dropped/cleared and recorded in ``warnings``, and NEVER
    invalidates the (already valid) use-case recognition."""
    status = RecognitionStatus(str(output["status"]))
    candidates = tuple(
        UseCaseCandidate(
            use_case_id=str(candidate["use_case_id"]),
            relationship=candidate["relationship"],
            confidence=candidate["confidence"],
            evidence_spans=tuple(candidate.get("evidence_spans") or ()),
            rationale=str(candidate.get("rationale", "")),
        )
        for candidate in (output.get("candidates") or ())
    )
    modelling_contexts, target_entity, warnings = normalize_dimensions(output)
    return RecognitionResult(
        status=status,
        candidates=candidates,
        ambiguity_note=output.get("ambiguity_note"),
        taxonomy_version=TAXONOMY_VERSION,
        recognizer_model_id=model_id,
        prompt_version=PROMPT_VERSION,
        modelling_contexts=modelling_contexts,
        target_entity=target_entity,
        warnings=warnings,
    )


def _partial_recovery(audited: AuditedStructuredResult, *, model: str) -> RecognitionResult | None:
    """The strict partial partition, folded (repair seam, Task 4). Returns the recovered result, or
    ``None`` to mean "nothing was recovered — take the caller's unchanged technical-failure path".

    It fires on exactly ONE seam disposition: ``semantic_invalid`` — the body passed the registered
    JSON Schema, failed this module's own rules, and could not be repaired within the §9.2 budget.
    Every other failing arm (egress-blocked, provider-failed, schema-invalid, validator-fault,
    audit-degraded) carries no body at all, by the seam's own design, and is untouched here.

    The ``failure_kind`` check below is therefore DEFENCE IN DEPTH and not, today, load-bearing:
    deleting it changes no test, because ``last_schema_valid_semantic_invalid_output`` is already
    ``None`` everywhere else. It stays because a precondition this function needs should be stated
    here rather than borrowed from another module's invariant — the seam's exposure rule is one
    edit away from being wider than it is now.

    Order of events, all of which have already happened: the model was ASKED to fix its answer
    (Task 3) and did not. Only then is the last answer partitioned — never instead of the repair.

    NEVER raises: recognition is fail-open, and a bug in the partition must degrade to today's
    behaviour rather than to a 500 on the intake path."""
    if audited.failure_kind != FAILURE_KIND_SEMANTIC_INVALID:
        return None
    body = audited.last_schema_valid_semantic_invalid_output
    if not body:
        return None
    try:
        kept, dropped = partition_candidates(body)
        if not kept:
            # An aggregate refusal, or every candidate individually invalid. Either way nothing
            # survived, so the disposition is unchanged: a candidate-free TECHNICAL_FAILURE that
            # fails OPEN to full grounding. Nothing is invented, and no scope narrows on a body the
            # model never got right.
            logger.info("recognition partition kept nothing (%d dropped); technical failure stands",
                        len(dropped))
            return None
        partitioned = {**body, "candidates": list(kept),
                       "status": status_after_partition(str(body["status"]), kept)}
        # The floor again, now over the body the partition BUILT. If a partitioned result cannot pass
        # the very validator that rejected its parent, the partition is wrong and we keep nothing —
        # that is a platform bug, and the fail-open path is the honest place to land.
        validate_recognition_output(partitioned)
        result = _result_from_output(partitioned, model_id=model)
    except Exception:
        logger.exception("recognition partition raised; failing open to technical_failure")
        return None
    return replace(result, dropped_candidates=dropped)


def _audited(result: RecognitionResult,
             audited: AuditedStructuredResult | None = None) -> AuditedRecognition:
    """Wrap a result in its audit facts and its QUALITY — the one constructor every arm of
    ``recognize_with_audit`` returns through.

    It is a single function on purpose. There are five terminal arms and each one must answer "which
    of the five things happened"; five separate constructions is five chances for one arm to answer
    it differently, or not at all, and the arm most likely to be forgotten is a failure arm — which
    is exactly the one the user is currently mis-told about. ``audited=None`` is the pre-seam
    dispatch failure: no provider call, no repair, no audit row."""
    return AuditedRecognition(
        result,
        audited.llm_call_ref if audited is not None else None,
        audited.provider_calls if audited is not None else 0,
        audited.usage if audited is not None else {},
        recognition_quality(
            result, repair_attempts=audited.repair_attempts if audited is not None else 0),
    )


def _recognition_instruction(
    redacted_hypothesis: str,
    redacted_goal: str | None,
    redacted_feedback: str | None = None,
) -> str:
    """The model-facing text: the closed-taxonomy prompt + the redacted request. This is passed as the
    audited seam's ``instruction`` (the reserved ``redacted_intent`` the adapter renders to the model);
    it carries ONLY the already-redacted hypothesis/goal and optional human revision feedback —
    never catalog columns."""
    lines = [build_recognition_prompt(), "", "=== REQUEST TO CLASSIFY ===",
             f"HYPOTHESIS: {redacted_hypothesis}"]
    if redacted_goal:
        lines.append(f"PREDICTION GOAL: {redacted_goal}")
    if redacted_feedback:
        lines.append(f"HUMAN FEEDBACK FOR THIS REVISION: {redacted_feedback}")
    return "\n".join(lines)


def recognize_with_audit(
    conn,
    client: LLMClient,
    *,
    redacted_hypothesis: str,
    redacted_goal: str | None = None,
    redacted_feedback: str | None = None,
    model_id: str | None = None,
    actor: IdentityEnvelope | None = None,
    ingestion_run_id: str | None = None,
    audit_run_id: str | None = None,
) -> AuditedRecognition:
    """Recognise the governed use-case scope of a *redacted* request. LLM-only and FAIL-OPEN: never
    raises to its caller. Routes through the platform's AUDITED seam (``audited_structured_call``) so a
    real provider gets the registered output-schema (never fails closed for lack of one), the egress
    guard scans the text, and the call is recorded in ``llm_call``. Any failure (egress block, provider
    failure/refusal, invalid body, dispatch/mapping error) folds to a candidate-free
    ``TECHNICAL_FAILURE``; a well-formed ``unscoped`` body folds to ``UNSCOPED``. The input carries only
    the redacted hypothesis + prediction goal and optional redacted human revision feedback
    (``catalog_metadata`` is empty — recognition never sees columns). ``model_id`` defaults to the
    env-configured model (matching the wired client).

    ``ingestion_run_id`` (C5-T5): when a caller runs recognition in service of an ingestion run, the
    dispatch is pre-audited + attributed to that run (stage ``recognizer``; subjects empty — the
    call is about the redacted request, never a catalog object). Today's callers pass nothing —
    ``None`` dispatches unattributed, byte-for-byte as before."""
    model = resolve_recognizer_model(model_id)
    instruction = _recognition_instruction(
        redacted_hypothesis, redacted_goal, redacted_feedback)
    dispatch_audit = None
    if ingestion_run_id is not None:
        dispatch_audit = DispatchAuditContext(ingestion_run_id=ingestion_run_id,
                                              stage="recognizer", subjects=())

    try:
        audited = drive_audited_structured_call(
            conn, client, task=RECOGNIZER_TASK, prompt_id=PROMPT_ID,
            schema_id=_OUTPUT_SCHEMA_ID, schema_version=_OUTPUT_SCHEMA_VERSION,
            catalog_metadata={}, instruction=instruction, actor=actor,
            dispatch_audit=dispatch_audit,
            run_id=audit_run_id or ENRICHMENT_RUN_ID,
            record_egress_block=audit_run_id is not None,
            # Repair seam, Task 3: the closed-taxonomy rules run INSIDE the §9.2 repair loop, after
            # the registered schema. Before this, they ran only in the post-call pass below — so a
            # body the JSON Schema could not fault (two primaries; a duplicated id) was discarded
            # whole without the model ever being asked to fix it. This is the ONE reviewed entry in
            # the seam's `_SEMANTIC_SEAM_CONSUMERS` allow-list.
            validate_semantics=validate_recognition_output,
        )
    except Exception:
        logger.exception("recognition dispatch raised; failing open to technical_failure")
        return _audited(unscoped_result(
            "recognition dispatch error",
            model_id=model,
            prompt_version=PROMPT_VERSION,
            technical=True,
        ))

    output = audited.output
    if not output:                              # egress block / provider failure / empty body
        # Task 4: ONE arm of that "not output" is different in kind — the model answered, the answer
        # was structurally well formed, and the repair budget was spent trying to fix a rule it kept
        # breaking. The seam exposes that final body (and ONLY on that arm). Partition it: a valid
        # candidate must not be lost to a sloppy sibling just because the model could not repair.
        recovered = _partial_recovery(audited, model=model)
        if recovered is not None:
            return _audited(recovered, audited)
        return _audited(unscoped_result(
            "recognition failed or egress-blocked",
            model_id=model,
            prompt_version=PROMPT_VERSION,
            technical=True,
        ), audited)

    try:
        # The FLOOR, not the check: the same rules already ran inside the repair loop (above), so
        # reaching here means a body the seam believed valid is not — a wiring regression, not a
        # model failure. It stays because a repair that never succeeded must still never yield a
        # scope, and because `output` is about to be mapped into a governed result object.
        validate_recognition_output(output)     # closed-taxonomy semantics (id in registry, primary leaf)
    except SchemaValidationError as exc:
        return _audited(unscoped_result(
            # `exc`'s message is value-free by construction (`recognition._reject`): it names
            # the closed code and the candidate POSITION, never the model's text. This note is
            # persisted and shown to a human.
            f"recognition output invalid: {exc}",
            model_id=model,
            prompt_version=PROMPT_VERSION,
            technical=True,
        ), audited)

    try:
        result = _result_from_output(output, model_id=model)
    except Exception:
        logger.exception("recognition output mapping raised; failing open to technical_failure")
        result = unscoped_result(
            "recognition mapping error",
            model_id=model,
            prompt_version=PROMPT_VERSION,
            technical=True,
        )
    return _audited(result, audited)


def recognize(
    conn,
    client: LLMClient,
    *,
    redacted_hypothesis: str,
    redacted_goal: str | None = None,
    redacted_feedback: str | None = None,
    model_id: str | None = None,
    actor: IdentityEnvelope | None = None,
    ingestion_run_id: str | None = None,
) -> RecognitionResult:
    """Compatibility projection over :func:`recognize_with_audit`."""
    return recognize_with_audit(
        conn,
        client,
        redacted_hypothesis=redacted_hypothesis,
        redacted_goal=redacted_goal,
        redacted_feedback=redacted_feedback,
        model_id=model_id,
        actor=actor,
        ingestion_run_id=ingestion_run_id,
    ).result
