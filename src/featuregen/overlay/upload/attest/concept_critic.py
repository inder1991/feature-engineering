"""Audited, refute-oriented LLM critic for enrichment concept assignments.

Mirrors :mod:`attest.bridge_critic`'s pattern — a typed frozen result with closed reason codes, the
same audited dispatch seam, replay through the shared content-addressed structured-result store —
for a different question: *given only this metadata evidence, is the ALREADY-PROPOSED concept
assignment supported?* The critic exists because the classifier's failure mode is confident
conflation (a branch DESCRIPTION classified ``branch_id``, a BIC classified ``counterparty_id``),
and a second pass that is asked to refute is structurally harder to please than one asked to label.

Order of authority, deliberately fixed:

1. :func:`attest.representation.shape_conflicts` is computed FIRST. A deterministic contradiction
   refutes on its own — no model may overturn it, so the critique question is never even dispatched
   for a conflicted item (mirroring the bridge critic's hard-conflict short-circuit) and the
   refutation survives having no LLM client at all.
2. The model's critique is a closed verdict (``supported | refuted | uncertain``) — never a
   replacement concept, so an off-vocabulary answer has no channel here.
3. One revise pass for flagged items (the bounded ``feature_assist`` fix-pass shape): the model
   may name a better concept, but the answer only stands if it is IN the registry, differs from
   the refuted proposal, and itself survives ``shape_conflicts``. Anything else falls to refuted.

The critic critiques LLM proposals only. It never sees — and its consumers never touch —
human-confirmed or source-attested concept authority (correction is re-derivation of LLM values,
never mutation of human decisions).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.attest.representation import shape_conflicts
from featuregen.overlay.upload.concepts import (
    CONCEPT_REGISTRY,
    UNCLASSIFIED,
    canonical_concept_name,
    classification_vocabulary,
)
from featuregen.overlay.upload.concepts import concept as lookup_concept
from featuregen.overlay.upload.enrich_llm import (
    MAX_DEFINITION_LEN,
    drive_audited_structured_call,
)
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)

if TYPE_CHECKING:
    from featuregen.contracts.envelopes import IdentityEnvelope
    from featuregen.intake.llm import LLMClient

logger = logging.getLogger(__name__)

CONCEPT_CRITIC_VERSION = 1
CONCEPT_CRITIC_RESULT_TYPE = "concept_critique"
CONCEPT_CRITIC_RUN_ID = "concept-critic"
CONCEPT_CRITIC_TASK = "overlay.enrich.concept_critique"
CONCEPT_CRITIC_PROMPT_ID = "concept_critique_v1"
CONCEPT_CRITIC_SCHEMA_ID = "concept_critique"
CONCEPT_REVISION_TASK = "overlay.enrich.concept_revision"
CONCEPT_REVISION_PROMPT_ID = "concept_revision_v1"
CONCEPT_REVISION_SCHEMA_ID = "concept_revision"

_CRITIQUE_INSTRUCTION = (
    "You are auditing an ALREADY-PROPOSED concept assignment for a data-catalog column. Given "
    "ONLY the evidence provided (column name, declared type, business definition, the proposed "
    "concept's registry meaning, and any deterministic shape conflicts), decide whether the "
    "assignment is supported by that evidence. Answer 'refuted' when the evidence contradicts the "
    "assignment, 'uncertain' when it cannot decide, 'supported' only when the evidence positively "
    "backs it. Do not propose an alternative concept in this step."
)
_REVISION_INSTRUCTION = (
    "The proposed concept assignment for this column was refuted (see the conflict evidence). "
    "From the provided controlled vocabulary choose the SINGLE concept name the evidence does "
    "support for this column, or 'unclassified' if none fits. Return the concept name exactly."
)

#: The closed reason vocabulary a ``ConceptCriticResultV1`` may carry. Deterministic conflict
#: CODES ride separately in ``conflict_codes`` (they are :data:`representation.SHAPE_CONFLICT_CODES`
#: members); these codes explain the critic's own path to the disposition.
CONCEPT_CRITIC_REASON_CODES = frozenset({
    "deterministic_shape_conflict",   # shape_conflicts refuted before any model was consulted
    "llm_supported",                  # the model positively upheld the assignment
    "llm_refuted",                    # the model refuted a shape-clean assignment
    "llm_uncertain",                  # the model could not decide -> abstain, proposal stands
    "revision_accepted",              # the revise pass produced a valid registry replacement
    "revision_rejected",              # the revise answer was off-registry/conflicted/withdrawn
    "llm_critic_unavailable",         # no client, provider failure, or dispatch blocked
    "llm_critic_invalid_result",      # a validated-looking output failed the code-side gate
})


class CritiqueVerdict(StrEnum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNCERTAIN = "uncertain"


class ConceptDisposition(StrEnum):
    """Per-item outcome. ``ACCEPTED``/``ABSTAINED`` keep the proposal (the critic is
    refute-oriented — absence of support is not an eviction); ``REVISED`` replaces it with the
    validated revision; ``REFUTED`` evicts it with no replacement (the caller records the
    non-identifier abstain, e.g. ``unclassified``)."""

    ACCEPTED = "accepted"
    REVISED = "revised"
    REFUTED = "refuted"
    ABSTAINED = "abstained"


@dataclass(frozen=True, slots=True)
class ConceptCriticItemV1:
    """One assignment under critique. ``definition`` must already be the SANITIZED, bounded
    business definition (the caller owns the M4 rule that a technical upload's free text never
    egresses — pass ``None`` for those); it is defensively re-bounded before any dispatch."""

    logical_ref: str
    column_name: str
    declared_type: str | None
    definition: str | None
    proposed_concept: str


@dataclass(frozen=True, slots=True)
class ConceptCriticResultV1:
    """The critic's honest, closed-vocabulary account of one assignment.

    ``resolved_concept`` is the concept that should STAND after criticism: the proposal for
    ``accepted``/``abstained``, the validated revision for ``revised``, ``None`` for ``refuted``
    (never silently the refuted value). Every name in it is registry-validated — an off-registry
    critic answer is unemittable by construction."""

    logical_ref: str
    proposed_concept: str
    disposition: ConceptDisposition
    resolved_concept: str | None
    conflict_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    structured_result_id: str | None
    llm_call_ref: str | None
    skipped_reason: str | None = None


def _registry_fingerprint() -> str:
    """Identity of the registry facts the critic reasons over (name/group/namespace). Folded into
    every replay hash so a registry repair (e.g. a namespace fix) re-critiques instead of replaying
    a verdict reached against the old vocabulary. Names-only fingerprints are not enough here —
    the namespace IS the load-bearing axis."""
    return canonical_hash(sorted(
        f"{c.name}:{c.group}:{c.namespace or ''}" for c in CONCEPT_REGISTRY.values()
    ))


def _bounded(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return text[:limit]


def _item_payload(item: ConceptCriticItemV1, conflicts: tuple[str, ...]) -> dict[str, object]:
    """The bounded metadata-only egress payload for one item. The proposed concept's registry
    meaning rides along (its description is the routing signal the registry authors wrote for
    exactly this purpose); no sample values, no free SQL, nothing row-level."""
    registered = lookup_concept(item.proposed_concept)
    payload: dict[str, object] = {
        "logical_ref": item.logical_ref,
        "column": item.column_name,
        "type": _bounded(item.declared_type, 64) or "unknown",
        "proposed_concept": item.proposed_concept,
        "shape_conflicts": list(conflicts),
    }
    definition = _bounded(item.definition, MAX_DEFINITION_LEN)
    if definition:
        payload["business_definition"] = definition
    if registered is not None:
        payload["proposed_concept_meaning"] = {
            "group": registered.group,
            "namespace": registered.namespace or "",
            "entity_link": registered.entity_link or "",
            "hint": registered.description.split(".")[0].strip()[:120],
        }
    return payload


def _input_hash(payload: dict[str, object], catalog_revision: str) -> str:
    return canonical_hash({
        "critic_version": CONCEPT_CRITIC_VERSION,
        "registry_fingerprint": _registry_fingerprint(),
        "catalog_revision": catalog_revision,
        "input": payload,
    })


def _closed_reasons(codes: Sequence[str]) -> tuple[str, ...]:
    """Keep ONLY closed vocabulary members, preserving first-seen order. The model's free-text
    reason strings are audit color inside the stored output/llm_call, never part of the typed
    result — closed means closed."""
    seen: list[str] = []
    for code in codes:
        if code in CONCEPT_CRITIC_REASON_CODES and code not in seen:
            seen.append(code)
    return tuple(seen)


def _drive(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
           payload: dict[str, object], instruction: str,
           actor: IdentityEnvelope | None,
           cacheable: tuple[str, ...] = ()) -> tuple[dict | None, str | None]:
    """One audited structured call through the exact seam the bridge critic uses. A client throw
    (a fake with no script, a provider outage) is contained to ``(None, None)`` — the critic must
    degrade to abstention/deterministic-only, never abort the enrichment run that hosts it."""
    try:
        call = drive_audited_structured_call(
            conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
            catalog_metadata=payload, instruction=instruction, actor=actor,
            run_id=CONCEPT_CRITIC_RUN_ID, record_egress_block=True,
            cacheable_metadata_keys=cacheable)
    except Exception:  # noqa: BLE001 — advisory critic: a provider fault never aborts enrichment
        logger.warning("concept critic call %s failed; treating as unavailable", task,
                       exc_info=True)
        return None, None
    return call.output, call.llm_call_ref


def _revise(conn, client: LLMClient, item: ConceptCriticItemV1,
            payload: dict[str, object], *, actor: IdentityEnvelope | None,
            ) -> tuple[str | None, str, str | None]:
    """The ONE revise pass for a flagged item: ``(accepted_concept | None, reason_code,
    llm_call_ref)``. An acceptable revision is IN the registry, is not the literal
    ``unclassified`` withdrawal, differs from the refuted proposal, and itself survives
    :func:`shape_conflicts` — the ruleset that refuted the original applies undiminished to the
    replacement, so the critic can never trade one conflation for another.

    The answer canonicalizes through the ONE alias seam BEFORE any gate, exactly like Pass A's
    ``_accept_concept``: a revise answer of ``counterparty_id`` (a legacy alias, still a registry
    member) lands as ``customer_id`` and is never stored raw. The difference gate compares
    CANONICAL forms, so an alias can never resurrect its own refuted successor (or vice versa) as
    a "different" concept."""
    output, llm_call_ref = _drive(
        conn, client, task=CONCEPT_REVISION_TASK, prompt_id=CONCEPT_REVISION_PROMPT_ID,
        schema_id=CONCEPT_REVISION_SCHEMA_ID,
        payload={**payload, "vocabulary": list(classification_vocabulary())},
        instruction=_REVISION_INSTRUCTION, actor=actor, cacheable=("vocabulary",))
    if output is None:
        return None, "llm_critic_unavailable", llm_call_ref
    revised = canonical_concept_name(str(output.get("concept") or "").strip().lower())
    if (
        not revised
        or revised == UNCLASSIFIED
        or revised == canonical_concept_name(item.proposed_concept)
        or revised not in CONCEPT_REGISTRY
        or shape_conflicts(item.column_name, item.declared_type, item.definition, revised)
    ):
        return None, "revision_rejected", llm_call_ref
    return revised, "revision_accepted", llm_call_ref


def _from_stored(item: ConceptCriticItemV1, conflicts: tuple[str, ...],
                 stored_output: dict, structured_result_id: str,
                 llm_call_ref: str | None) -> ConceptCriticResultV1:
    """Rebuild a result from the replay store, RE-VALIDATING the code-side gates: the store is
    immutable but the registry is not, and an off-registry (or now-conflicted) resolved concept
    must be as unemittable on replay as on first derivation — fail closed to abstention."""
    try:
        disposition = ConceptDisposition(stored_output["disposition"])
    except (KeyError, TypeError, ValueError):
        disposition = None
    resolved = stored_output.get("resolved_concept")
    resolved = str(resolved).strip().lower() if isinstance(resolved, str) and resolved else None
    valid = disposition is not None and (
        (disposition is ConceptDisposition.REFUTED and resolved is None)
        or (disposition is not ConceptDisposition.REFUTED
            and resolved is not None
            and resolved in CONCEPT_REGISTRY
            and not shape_conflicts(item.column_name, item.declared_type,
                                    item.definition, resolved))
    )
    if not valid:
        return ConceptCriticResultV1(
            item.logical_ref, item.proposed_concept, ConceptDisposition.ABSTAINED,
            item.proposed_concept if item.proposed_concept in CONCEPT_REGISTRY else None,
            conflicts, ("llm_critic_invalid_result",), structured_result_id, llm_call_ref,
            "llm_critic_invalid_result")
    reason_codes = _closed_reasons([
        str(code) for code in stored_output.get("reason_codes", ())
        if isinstance(code, str)
    ])
    return ConceptCriticResultV1(
        item.logical_ref, item.proposed_concept, disposition, resolved,
        conflicts, reason_codes, structured_result_id, llm_call_ref)


def _store(conn, *, input_hash: str, disposition: ConceptDisposition,
           resolved: str | None, reason_codes: tuple[str, ...],
           conflicts: tuple[str, ...], llm_call_ref: str,
           item: ConceptCriticItemV1) -> str:
    stored = record_structured_result(
        conn,
        result_type=CONCEPT_CRITIC_RESULT_TYPE,
        result_version=CONCEPT_CRITIC_VERSION,
        input_content_hash=input_hash,
        output={
            "disposition": disposition.value,
            "resolved_concept": resolved,
            "reason_codes": list(reason_codes),
            "conflict_codes": list(conflicts),
        },
        producer_kind="llm_call",
        producer_ref=llm_call_ref,
        authority={
            "authority": "llm_advisory",
            "logical_ref": item.logical_ref,
            "proposed_concept": item.proposed_concept,
        },
    )
    return stored.structured_result_id


def _stored_llm_call_ref(conn, structured_result_id: str) -> str | None:
    provenance = conn.execute(
        "SELECT producer_ref FROM structured_result_provenance "
        "WHERE structured_result_id=%s AND producer_kind='llm_call' "
        "ORDER BY recorded_at,producer_ref LIMIT 1",
        (structured_result_id,),
    ).fetchone()
    return None if provenance is None else provenance[0]


def _critique_one(conn, client: LLMClient | None, item: ConceptCriticItemV1,
                  *, catalog_revision: str,
                  actor: IdentityEnvelope | None) -> ConceptCriticResultV1:
    conflicts = shape_conflicts(
        item.column_name, item.declared_type, item.definition, item.proposed_concept)
    payload = _item_payload(item, conflicts)
    input_hash = _input_hash(payload, catalog_revision)

    stored = find_structured_result(
        conn, result_type=CONCEPT_CRITIC_RESULT_TYPE,
        result_version=CONCEPT_CRITIC_VERSION, input_content_hash=input_hash)
    if stored is not None:
        return _from_stored(item, conflicts, dict(stored.output),
                            stored.structured_result_id,
                            _stored_llm_call_ref(conn, stored.structured_result_id))

    proposal_stands = (
        item.proposed_concept if item.proposed_concept in CONCEPT_REGISTRY else None)

    # NO CLIENT: deterministic conflicts alone can refute (the suite app and any degraded
    # deployment keep the shape guarantee); everything shape-clean abstains honestly.
    if client is None:
        if conflicts:
            return ConceptCriticResultV1(
                item.logical_ref, item.proposed_concept, ConceptDisposition.REFUTED, None,
                conflicts, ("deterministic_shape_conflict",), None, None,
                "llm_critic_unavailable")
        return ConceptCriticResultV1(
            item.logical_ref, item.proposed_concept, ConceptDisposition.ABSTAINED,
            proposal_stands, conflicts, ("llm_critic_unavailable",), None, None,
            "llm_critic_unavailable")

    reason_codes: list[str] = []
    llm_call_ref: str | None = None
    if conflicts:
        # Deterministic refutation stands regardless of any model opinion, so the critique
        # question is never dispatched (the bridge critic's short-circuit, applied here).
        reason_codes.append("deterministic_shape_conflict")
    else:
        output, llm_call_ref = _drive(
            conn, client, task=CONCEPT_CRITIC_TASK, prompt_id=CONCEPT_CRITIC_PROMPT_ID,
            schema_id=CONCEPT_CRITIC_SCHEMA_ID, payload=payload,
            instruction=_CRITIQUE_INSTRUCTION, actor=actor)
        if output is None:
            return ConceptCriticResultV1(
                item.logical_ref, item.proposed_concept, ConceptDisposition.ABSTAINED,
                proposal_stands, conflicts, ("llm_critic_unavailable",), None, llm_call_ref,
                "llm_critic_unavailable")
        try:
            verdict = CritiqueVerdict(output["verdict"])
        except (KeyError, TypeError, ValueError):
            # The registered schema should make this unreachable; retain an explicit fail-closed
            # seam for fake/custom clients (the bridge critic's invalid-result seam).
            return ConceptCriticResultV1(
                item.logical_ref, item.proposed_concept, ConceptDisposition.ABSTAINED,
                proposal_stands, conflicts, ("llm_critic_invalid_result",), None, llm_call_ref,
                "llm_critic_invalid_result")
        if verdict is CritiqueVerdict.SUPPORTED:
            reason_codes.append("llm_supported")
            result_id = _store(
                conn, input_hash=input_hash, disposition=ConceptDisposition.ACCEPTED,
                resolved=item.proposed_concept, reason_codes=tuple(reason_codes),
                conflicts=conflicts, llm_call_ref=llm_call_ref or "", item=item)
            return ConceptCriticResultV1(
                item.logical_ref, item.proposed_concept, ConceptDisposition.ACCEPTED,
                proposal_stands, conflicts, tuple(reason_codes), result_id, llm_call_ref)
        if verdict is CritiqueVerdict.UNCERTAIN:
            reason_codes.append("llm_uncertain")
            result_id = _store(
                conn, input_hash=input_hash, disposition=ConceptDisposition.ABSTAINED,
                resolved=item.proposed_concept, reason_codes=tuple(reason_codes),
                conflicts=conflicts, llm_call_ref=llm_call_ref or "", item=item)
            return ConceptCriticResultV1(
                item.logical_ref, item.proposed_concept, ConceptDisposition.ABSTAINED,
                proposal_stands, conflicts, tuple(reason_codes), result_id, llm_call_ref)
        reason_codes.append("llm_refuted")

    # ONE revise pass for the flagged item (deterministically conflicted, or model-refuted).
    revised, revision_reason, revise_call_ref = _revise(
        conn, client, item, payload, actor=actor)
    llm_call_ref = revise_call_ref or llm_call_ref
    if revision_reason != "llm_critic_unavailable":
        reason_codes.append(revision_reason)
    disposition = ConceptDisposition.REVISED if revised else ConceptDisposition.REFUTED
    result_id: str | None = None
    if llm_call_ref:
        # Store only what an LLM contributed to; a no-call deterministic refutation is
        # recomputable for free and needs no replay row.
        result_id = _store(
            conn, input_hash=input_hash, disposition=disposition, resolved=revised,
            reason_codes=tuple(reason_codes), conflicts=conflicts,
            llm_call_ref=llm_call_ref, item=item)
    return ConceptCriticResultV1(
        item.logical_ref, item.proposed_concept, disposition, revised,
        conflicts, tuple(reason_codes), result_id, llm_call_ref,
        "llm_critic_unavailable" if revision_reason == "llm_critic_unavailable" else None)


def critique_concept_batch(
    conn,
    client: LLMClient | None,
    items: Sequence[ConceptCriticItemV1],
    *,
    catalog_revision: str,
    actor: IdentityEnvelope | None = None,
) -> dict[str, ConceptCriticResultV1]:
    """Critique a batch of proposed concept assignments; returns ``{logical_ref: result}``.

    Per-item and fail-soft by construction: each item resolves to a disposition even when the
    provider is scripted for nothing (abstain), the client is absent (deterministic-only), or the
    replay store already holds the validated outcome (no dispatch at all). ``catalog_revision`` is
    part of every item's replay identity — the same column under a changed catalog is a new
    question, while a byte-identical re-upload replays for free."""
    results: dict[str, ConceptCriticResultV1] = {}
    for item in items:
        results[item.logical_ref] = _critique_one(
            conn, client, item, catalog_revision=catalog_revision, actor=actor)
    return results
