"""Targeted semantic adjudication (semantic plan Task 5) — a second opinion, on purpose, for few.

Pass A classifies every column and the refute-oriented critic audits the high-impact ones. Both
answer a CLOSED question ("which concept?" / "is this one supported?"). Neither can say *"none of
your words fit — here is the one you are missing"*, and neither is asked to explain WHY a column is
hard. This module adds exactly that, for a DETERMINISTICALLY SELECTED few:

* **Selection is a pure function, not a confidence threshold.** :func:`selection_reasons` reads
  computed facts — no concept at all, a deterministic shape conflict, the critic's refutation or
  uncertainty, a source-declared entity contradicting the concept's entity link — and returns the
  closed :data:`semantic_context.REASON_CODES` that justify asking. An empty tuple means the column
  is CLEAR and is never adjudicated. There is no model-confidence gate anywhere: a model's own
  certainty is the thing under review, so using it to decide what to review is circular.
* **The answer is closed and registry-validated.** One concept (a registry member or the literal
  ``unclassified``), at most three registry alternatives, closed reason and missing-context codes,
  and an optional ontology-gap suggestion. Free text is audit colour inside the stored output and
  the ``llm_call``; it never becomes a concept, a join key or an operand.
* **Every accepted name passes `canonical_concept_name` (D12.1)** BEFORE any gate, exactly like
  Pass A's `_accept_concept` and the critic's revise pass — an answer of ``counterparty_id`` lands
  as ``customer_id``, and the "did it change?" comparison is between canonical forms so an alias can
  never look like a correction of itself.
* **`confidence_band` is explanation, never authority.** It rides the stored result and the read
  surface for a human to weigh; nothing in this module or downstream branches on it, and it cannot
  turn an LLM proposal into a stronger assertion. Evidence authority stays `llm/proposed`.
* **A gap suggestion writes NO concept evidence and never touches the registry** — see
  :mod:`semantic_gap`. It becomes discoverable through the migration-1046 subject/current pointer.

**Why a bounded single path rather than `run_batched`.** `run_batched` returns
``dict[ref, str]`` — one accepted STRING per item — and its degradation ladder (salvage, split,
single fallback) is built around that shape; a structured multi-field answer only fits through it by
smuggling the rest out in mutable out-params, which is how a "batch" result stops being auditable.
Adjudication is also, by construction, a SMALL and highly variable target set (the unclear columns
of a run), so the batching win is small while the identity/replay cost of the smuggling is
permanent. This module therefore uses the same per-item audited seam the concept critic uses
(`drive_audited_structured_call` + the 1039 replay store) plus an EXPLICIT provider-call cap
(:func:`adjudication_bounds`): every item beyond the cap is reported ``not_attempted`` — an honest
disposition, never a silent drop. Replays never consume the cap (no call is issued).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.concepts import (
    CONCEPT_REGISTRY,
    UNCLASSIFIED,
    canonical_concept_name,
    canonical_entity,
    classification_vocabulary,
)
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.semantic_context import MISSING_CONTEXT_CODES, REASON_CODES
from featuregen.overlay.upload.semantic_gap import (
    OntologyGapSuggestionV1,
    gap_from_output,
)
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
    set_current_structured_result,
    withdraw_current_structured_result,
)

if TYPE_CHECKING:
    from featuregen.contracts.envelopes import IdentityEnvelope
    from featuregen.intake.llm import LLMClient

logger = logging.getLogger(__name__)

#: v2 per the plan: the structured-result VERSION axis is independent of the output-schema version
#: (the concept critic's precedent — result v2 over schema `concept_critique` v1). v2 rather than v1
#: because `semantic_adjudication` names the same question the pre-plan design sketched with a
#: different shape; starting at 2 keeps a stored v1 row (were one ever to exist) from replaying
#: against a contract it was not reached under.
SEMANTIC_ADJUDICATION_VERSION = 2
SEMANTIC_ADJUDICATION_RESULT_TYPE = "semantic_adjudication"
SEMANTIC_ADJUDICATION_RUN_ID = "semantic-adjudication"
SEMANTIC_ADJUDICATION_TASK = "overlay.enrich.semantic_adjudication"
SEMANTIC_ADJUDICATION_PROMPT_ID = "semantic_adjudication_v1"
SEMANTIC_ADJUDICATION_PROMPT_VERSION = 1
SEMANTIC_ADJUDICATION_SCHEMA_ID = "semantic_adjudication"
SEMANTIC_ADJUDICATION_SCHEMA_VERSION = 1
#: The ingest stage name dispatches are attributed to — the SAME string `stage_report` registers,
#: so the run's stage account and its dispatch audit name one thing.
SEMANTIC_ADJUDICATION_STAGE = "semantic_adjudication"

#: Pointer subject kinds over the 1046 table. `column` answers "what is the current adjudication of
#: this column?" (the asset-detail read); `ontology_gap` lists "every column whose adjudication
#: suggests a missing concept" (the review surface). Both may name ONE immutable revision — two
#: questions, one answer, separate supersession lifetimes.
SUBJECT_COLUMN = "column"
SUBJECT_ONTOLOGY_GAP = "ontology_gap"

#: Explanatory only (never authority, never branched on). Closed so the stored result cannot carry
#: an invented grade that a reader would then try to interpret.
CONFIDENCE_BANDS: frozenset[str] = frozenset({"high", "medium", "low"})

#: At most three, per the contract — an "alternative" list long enough to contain the answer by
#: accident is not a shortlist.
MAX_ALTERNATIVES = 3

_INSTRUCTION = (
    "You are adjudicating the SEMANTIC CLASSIFICATION of one data-catalog column that the "
    "platform's own classifier could not settle. You are given the column's evidence (name, "
    "declared type, business definition and glossary context, its sibling columns, the concept "
    "currently assigned if any, and the closed reasons this column was referred to you), plus the "
    "controlled concept vocabulary. Choose the SINGLE concept from that vocabulary the evidence "
    "supports, or 'unclassified' when none of them fits — 'unclassified' is a real, correct answer "
    "and is always better than a guess. List at most three vocabulary alternatives you seriously "
    "considered. Explain yourself ONLY with the supplied reason codes and missing-context codes. "
    "If the evidence describes a real, distinct business concept the vocabulary genuinely lacks, "
    "describe it in ontology_gap — a suggestion for human review, never a classification."
)


class AdjudicationOutcome(StrEnum):
    """One item's headline disposition. These are STAGE-DETAIL OUTCOMES, not stage states: the
    0996 CHECK constraint owns the closed `state` vocabulary (`succeeded`/`partial`/…) and these
    live in the stage `detail` payload beside their counts (D12.3)."""

    SELECTED = "selected"          # a different, valid registry concept — evidence is written
    UNCHANGED = "unchanged"        # the adjudicator upheld the current concept — no write
    UNCLASSIFIED = "unclassified"  # honest abstention — no concept evidence (never a proposal)
    GAP_SUGGESTED = "gap_suggested"  # a validated vocabulary gap was stored + pointed at
    INVALID = "invalid"            # a validated-looking output failed the code-side gate
    NOT_ATTEMPTED = "not_attempted"  # no client, call cap reached, or a contained dispatch fault


#: Outcome PRECEDENCE for the headline label, strongest first. A gap outranks the concept verdict
#: because it is the rarer, reviewable event; the independent `evidence_written` / `gaps` counters
#: in the stage detail mean nothing is hidden by the ranking.
_OUTCOME_ORDER = (AdjudicationOutcome.INVALID, AdjudicationOutcome.NOT_ATTEMPTED,
                  AdjudicationOutcome.GAP_SUGGESTED, AdjudicationOutcome.SELECTED,
                  AdjudicationOutcome.UNCHANGED, AdjudicationOutcome.UNCLASSIFIED)


@dataclass(frozen=True, slots=True)
class AdjudicationBounds:
    """The explicit ceiling on this stage. Fail-soft ISOLATION boundaries (the D3 semantic-binding
    precedent): crossing one yields `not_attempted` items with truthful counts, never an exception
    into ingestion and never a silently shortened target list."""

    max_provider_calls: int   # hard ceiling on adjudication provider calls per run
    max_targets: int          # hard ceiling on items considered at all (selection is unbounded)


def adjudication_bounds() -> AdjudicationBounds:
    """Env-overridable bounds. Deliberately small: adjudication is the exception path, and a run
    where hundreds of columns are unclear is a classification problem to fix, not a budget to
    raise."""
    return AdjudicationBounds(
        max_provider_calls=int(os.environ.get("OVERLAY_ADJUDICATION_MAX_PROVIDER_CALLS", "12")),
        max_targets=int(os.environ.get("OVERLAY_ADJUDICATION_MAX_TARGETS", "24")),
    )


# ── contracts ───────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SemanticAdjudicationV2:
    """The closed, validated answer. Every field is already gated: `selected_concept` is a registry
    member or ``unclassified``, `alternatives` are registry members (deduped, selection excluded,
    capped), the code tuples are subsets of their closed vocabularies, and `confidence_band` is a
    :data:`CONFIDENCE_BANDS` member that NOTHING branches on."""

    selected_concept: str
    alternatives: tuple[str, ...]
    confidence_band: str
    reason_codes: tuple[str, ...]
    missing_context: tuple[str, ...]
    ontology_gap: OntologyGapSuggestionV1 | None

    def as_dict(self) -> dict:
        return {
            "selected_concept": self.selected_concept,
            "alternatives": list(self.alternatives),
            "confidence_band": self.confidence_band,
            "reason_codes": list(self.reason_codes),
            "missing_context": list(self.missing_context),
            "ontology_gap": None if self.ontology_gap is None else self.ontology_gap.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class AdjudicationCandidateV1:
    """One column offered to :func:`selection_reasons`. Every field is a COMPUTED fact from this
    run — nothing here is a model opinion about itself.

    `critic_disposition` / `critic_reason_codes` are the concept critic's own output for this ref
    (absent when it did not run: only high-impact groups are criticised). `source_entity` is the
    SOURCE-attested entity the upload declared for the column, if any — the deterministic signal
    behind `source_llm_conflict` (see :func:`selection_reasons`)."""

    logical_ref: str
    column_name: str
    declared_type: str | None = None
    definition: str | None = None
    concept: str | None = None
    shape_conflicts: tuple[str, ...] = ()
    critic_disposition: str | None = None
    critic_reason_codes: tuple[str, ...] = ()
    source_entity: str | None = None


@dataclass(frozen=True, slots=True)
class AdjudicationTargetV1:
    """A candidate that SELECTION accepted, carrying the closed reasons it was referred and the
    purpose-adapted context payload the model will see."""

    candidate: AdjudicationCandidateV1
    selection_reasons: tuple[str, ...]
    context: Mapping[str, object] | None = None

    @property
    def logical_ref(self) -> str:
        return self.candidate.logical_ref


@dataclass(frozen=True, slots=True)
class AdjudicationResultV1:
    """One item's full disposition — the typed record the stage detail counts and the read surface
    renders. `adjudication` is ``None`` for `not_attempted` / `invalid`: there is no validated
    answer, and manufacturing an empty one would read as an abstention the model never made."""

    logical_ref: str
    outcome: AdjudicationOutcome
    adjudication: SemanticAdjudicationV2 | None
    selection_reasons: tuple[str, ...]
    structured_result_id: str | None = None
    llm_call_ref: str | None = None
    skipped_reason: str | None = None

    @property
    def corrected_concept(self) -> str | None:
        """The registry concept this result asserts as a CORRECTION, or ``None``.

        Only a `selected` outcome corrects. `unchanged` asserts nothing new, `unclassified` is the
        deliberate non-proposal (C3: `unclassified` is never field evidence), and the failure
        outcomes assert nothing at all."""
        if self.outcome is not AdjudicationOutcome.SELECTED or self.adjudication is None:
            return None
        return self.adjudication.selected_concept


# ── selection: a pure function over computed facts ──────────────────────────────────────────────

def _canonical_entity(entity: str | None) -> str | None:
    """One operand, reduced to its canonical entity name through the alias seam (D12.1-revised) so
    the legacy `counterparty` / canonical `customer` pair is not read as a contradiction.

    BOTH sides of the comparison go through this, which is the whole point. The earlier version
    routed through `display_entity(concept, entity)`, which normalizes only when the COLUMN'S OWN
    CONCEPT is the alias — so the shape production actually emits (a stored/declared entity of
    `counterparty` against a freshly canonicalized `customer_id`) came out as a conflict, and every
    such column spent one of the twelve capped calls re-deriving a non-disagreement. Nothing here
    is stored: this is comparison-only normalization, and the persisted `entity_link` stays the
    byte-stable fact-key input."""
    return canonical_entity(entity)


def selection_reasons(candidate: AdjudicationCandidateV1) -> tuple[str, ...]:
    """The closed reasons this column must be adjudicated — ``()`` when it is CLEAR.

    PURE: same inputs, same answer, no clock, no DB, no model. Every rule names a signal that
    provably exists on this platform's data:

    * ``unclassified_column`` — no concept at all (``None``/blank/the ``unclassified`` sentinel), or
      a name that is not a registry member. The classifier abstained or produced something
      unusable, so nothing downstream can key on it.
    * ``deterministic_shape_conflict`` — `attest.representation.shape_conflicts` found a
      contradiction between the column's shape and its concept. Deterministic evidence outranks any
      model, so this is a REFERRAL, never a verdict the adjudicator may overturn: it is passed to
      the model as evidence and the conflict codes stay in the audit.
    * ``critic_refuted`` — the concept critic evicted the assignment (disposition ``refuted``).
      Its revise pass already declined to name a replacement, so the column now has no answer.
    * ``critic_uncertain`` — the critic abstained BECAUSE the model could not decide
      (``llm_uncertain`` in its reason codes). An abstention for any other reason — no client, a
      provider fault — is NOT uncertainty about the data and does not qualify.
    * ``source_llm_conflict`` — the upload SOURCE-attested an `entity` for this column and the
      assigned concept links a DIFFERENT entity. Source-attested facts outrank LLM proposals
      (functional rule 1), so a disagreement is a genuine referral. BOTH operands are reduced
      through the entity alias seam (:func:`_canonical_entity`), so `counterparty`/`customer` is
      not a false conflict in EITHER direction, and only concepts that actually declare an
      `entity_link` are compared at all (a monetary or temporal concept links no entity, so silence
      there is not disagreement).

    A CLEAR column — a registry concept, no conflict, no critic objection, no source disagreement —
    is never referred. That is the whole point of "targeted": the adjudicator exists for the
    exceptions, and spending a call on a settled column buys nothing but drift."""
    reasons: list[str] = []
    concept = (candidate.concept or "").strip().lower()
    if not concept or concept == UNCLASSIFIED or concept not in CONCEPT_REGISTRY:
        reasons.append("unclassified_column")
    if candidate.shape_conflicts:
        reasons.append("deterministic_shape_conflict")
    if (candidate.critic_disposition or "").strip().lower() == "refuted":
        reasons.append("critic_refuted")
    if "llm_uncertain" in candidate.critic_reason_codes:
        reasons.append("critic_uncertain")
    registered = CONCEPT_REGISTRY.get(concept)
    if registered is not None and registered.entity_link and candidate.source_entity:
        declared = _canonical_entity(candidate.source_entity)
        linked = _canonical_entity(registered.entity_link)
        if declared and linked and declared != linked:
            reasons.append("source_llm_conflict")
    illegal = [r for r in reasons if r not in REASON_CODES]
    if illegal:   # structurally impossible; guards vocabulary drift between the two modules
        raise ValueError(f"selection reasons outside the closed set: {sorted(illegal)}")
    return tuple(reasons)


def select_adjudication_targets(
    candidates: Sequence[AdjudicationCandidateV1],
    *,
    context_of: Mapping[str, Mapping[str, object]] | None = None,
    max_targets: int | None = None,
) -> tuple[AdjudicationTargetV1, ...]:
    """Every candidate with a non-empty :func:`selection_reasons`, deterministically ordered.

    ORDER IS PART OF THE CONTRACT because the call cap truncates: targets sort by (descending
    reason count, logical_ref), so the columns with the MOST independent signals of trouble are
    adjudicated first and a capped run is truncated at the least-referred end rather than
    arbitrarily. `max_targets` is applied here so the cap is visible in the selection, not hidden
    inside the dispatch loop."""
    targets = [
        AdjudicationTargetV1(candidate=c, selection_reasons=reasons,
                             context=(context_of or {}).get(c.logical_ref))
        for c in candidates
        for reasons in (selection_reasons(c),)
        if reasons
    ]
    targets.sort(key=lambda t: (-len(t.selection_reasons), t.logical_ref))
    if max_targets is not None:
        targets = targets[:max_targets]
    return tuple(targets)


# ── the model-facing payload + replay identity ──────────────────────────────────────────────────

#: EVERY key this module adds ON TOP of the caller's `for_critic` context, in emission order. Frozen
#: as data so the egress classification test can assert the whole surface (D10: an unclassified key
#: fails the payload closed, loudly).
ADJUDICATION_PAYLOAD_KEYS: tuple[str, ...] = (
    "logical_ref", "column", "type", "proposed_concept", "shape_conflicts",
    "business_definition", "selection_reasons", "missing_context", "vocabulary",
)


def _bounded(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return str(text)[:limit]


def adjudication_payload(target: AdjudicationTargetV1) -> dict[str, object]:
    """The bounded metadata-only egress payload for one target.

    The caller's `semantic_context.for_critic` projection is merged UNDER this module's own keys —
    identical to the critic's rule, so widening the shared context can never rewrite the question.
    `proposed_concept` carries the CURRENT concept (``unclassified`` when there is none): the model
    is told what the platform currently believes, because "is this right?" and "what is it?" are
    different questions and only the first is answerable without it."""
    from featuregen.overlay.upload.enrich_llm import MAX_DEFINITION_LEN

    candidate = target.candidate
    payload: dict[str, object] = dict(target.context or {})
    payload.update({
        "logical_ref": candidate.logical_ref,
        "column": candidate.column_name,
        "type": _bounded(candidate.declared_type, 64) or "unknown",
        "proposed_concept": (candidate.concept or UNCLASSIFIED),
        "shape_conflicts": list(candidate.shape_conflicts),
        "selection_reasons": list(target.selection_reasons),
    })
    definition = _bounded(candidate.definition, MAX_DEFINITION_LEN)
    if definition:
        payload["business_definition"] = definition
    payload["missing_context"] = sorted(MISSING_CONTEXT_CODES)
    payload["vocabulary"] = list(classification_vocabulary())
    return payload


def _vocabulary_fingerprint() -> str:
    """Identity of the vocabulary the model was SHOWN. Folded into the replay hash so a registry
    change re-asks the question instead of replaying an answer reached against different words.
    Sorted + canonical, so declaration order is not identity (the `_vocab_fingerprint` defect)."""
    return canonical_hash(sorted(classification_vocabulary(), key=lambda c: c["name"]))


def adjudication_input_hash(payload: Mapping[str, object], catalog_revision: str) -> str:
    return canonical_hash({
        "adjudication_version": SEMANTIC_ADJUDICATION_VERSION,
        "prompt_version": SEMANTIC_ADJUDICATION_PROMPT_VERSION,
        "schema_version": SEMANTIC_ADJUDICATION_SCHEMA_VERSION,
        "vocabulary_fingerprint": _vocabulary_fingerprint(),
        "catalog_revision": catalog_revision,
        "input": dict(payload),
    })


# ── validation: closed vocabularies, registry membership, canonical names ───────────────────────

def _closed(codes: object, vocabulary: frozenset[str]) -> tuple[str, ...]:
    """Keep ONLY closed-vocabulary members, first-seen order preserved. A free-text explanation the
    model volunteered survives in the stored output and the llm_call audit; it never enters a typed
    field."""
    if not isinstance(codes, Sequence) or isinstance(codes, str):
        return ()
    seen: list[str] = []
    for code in codes:
        if isinstance(code, str) and code in vocabulary and code not in seen:
            seen.append(code)
    return tuple(seen)


def adjudication_from_output(raw: Mapping | None) -> SemanticAdjudicationV2 | None:
    """Validate one model output into :class:`SemanticAdjudicationV2`, or ``None`` when it fails.

    ``None`` (an `invalid` outcome) for exactly two structural reasons: the selection is missing/
    unusable after canonicalization, or `confidence_band` is off-vocabulary. Everything else is
    FILTERED rather than fatal — off-registry alternatives are dropped, unknown codes are dropped,
    a malformed gap is dropped (:func:`semantic_gap.gap_from_output`) — because losing a valid
    concept selection over a bad alias list would be a worse answer than the one we have."""
    if not isinstance(raw, Mapping):
        return None
    selected = canonical_concept_name(str(raw.get("selected_concept") or "").strip().lower())
    if not selected or (selected != UNCLASSIFIED and selected not in CONCEPT_REGISTRY):
        return None
    band = str(raw.get("confidence_band") or "").strip().lower()
    if band not in CONFIDENCE_BANDS:
        return None
    alternatives: list[str] = []
    raw_alternatives = raw.get("alternatives")
    if isinstance(raw_alternatives, Sequence) and not isinstance(raw_alternatives, str):
        for name in raw_alternatives:
            if not isinstance(name, str):
                continue
            alt = canonical_concept_name(name.strip().lower())
            if alt and alt != selected and alt in CONCEPT_REGISTRY and alt not in alternatives:
                alternatives.append(alt)
    return SemanticAdjudicationV2(
        selected_concept=selected,
        alternatives=tuple(alternatives[:MAX_ALTERNATIVES]),
        confidence_band=band,
        reason_codes=_closed(raw.get("reason_codes"), REASON_CODES),
        missing_context=_closed(raw.get("missing_context"), MISSING_CONTEXT_CODES),
        ontology_gap=gap_from_output(raw.get("ontology_gap")),
    )


def _outcome_for(target: AdjudicationTargetV1,
                 adjudication: SemanticAdjudicationV2) -> AdjudicationOutcome:
    if adjudication.ontology_gap is not None:
        return AdjudicationOutcome.GAP_SUGGESTED
    if adjudication.selected_concept == UNCLASSIFIED:
        return AdjudicationOutcome.UNCLASSIFIED
    current = canonical_concept_name((target.candidate.concept or "").strip().lower())
    if current == adjudication.selected_concept:
        return AdjudicationOutcome.UNCHANGED
    return AdjudicationOutcome.SELECTED


# ── persistence ─────────────────────────────────────────────────────────────────────────────────

def _store(conn, *, input_hash: str, target: AdjudicationTargetV1,
           adjudication: SemanticAdjudicationV2, llm_call_ref: str) -> str:
    """Persist the validated answer as an immutable structured result with its `llm_call`
    provenance (the EXISTING `structured_result_provenance` mechanism — no second linkage)."""
    stored = record_structured_result(
        conn,
        result_type=SEMANTIC_ADJUDICATION_RESULT_TYPE,
        result_version=SEMANTIC_ADJUDICATION_VERSION,
        input_content_hash=input_hash,
        output={
            **adjudication.as_dict(),
            "selection_reasons": list(target.selection_reasons),
        },
        producer_kind="llm_call",
        producer_ref=llm_call_ref,
        authority={
            "authority": "llm_advisory",
            "logical_ref": target.logical_ref,
            "current_concept": target.candidate.concept or UNCLASSIFIED,
        },
    )
    return stored.structured_result_id


def _point(conn, *, target: AdjudicationTargetV1, structured_result_id: str,
           adjudication: SemanticAdjudicationV2) -> None:
    """Advance the subject/current pointers (migration 1046).

    The COLUMN pointer always advances — "the current adjudication of this column" is exactly this
    revision. The ONTOLOGY-GAP pointer advances only when this answer suggests a gap and is
    WITHDRAWN when it does not: a re-derivation that no longer sees a missing concept must retire
    the old suggestion, or the review queue accumulates claims nobody is making any more."""
    set_current_structured_result(
        conn, subject_kind=SUBJECT_COLUMN, subject_ref=target.logical_ref,
        result_type=SEMANTIC_ADJUDICATION_RESULT_TYPE,
        structured_result_id=structured_result_id)
    if adjudication.ontology_gap is not None:
        set_current_structured_result(
            conn, subject_kind=SUBJECT_ONTOLOGY_GAP, subject_ref=target.logical_ref,
            result_type=SEMANTIC_ADJUDICATION_RESULT_TYPE,
            structured_result_id=structured_result_id)
    else:
        withdraw_current_structured_result(
            conn, subject_kind=SUBJECT_ONTOLOGY_GAP, subject_ref=target.logical_ref,
            result_type=SEMANTIC_ADJUDICATION_RESULT_TYPE)


def _stored_llm_call_ref(conn, structured_result_id: str) -> str | None:
    row = conn.execute(
        "SELECT producer_ref FROM structured_result_provenance "
        "WHERE structured_result_id=%s AND producer_kind='llm_call' "
        "ORDER BY recorded_at,producer_ref LIMIT 1",
        (structured_result_id,),
    ).fetchone()
    return None if row is None else row[0]


def load_current_adjudication(conn, logical_ref: str) -> tuple[SemanticAdjudicationV2 | None, str | None]:
    """The column's CURRENT adjudication, through the 1046 pointer — ``(adjudication, result_id)``.

    RE-VALIDATES on read, exactly like the critic's `_from_stored`: the store is immutable but the
    registry is not, so a concept that has since left the vocabulary must be as unemittable on
    replay as it was on first derivation. A withdrawn pointer, a missing revision or an output that
    no longer validates all yield ``(None, None)`` — never a stale claim rendered as current."""
    from featuregen.overlay.upload.structured_results import (
        load_current_structured_result,
        load_structured_result,
    )

    pointer = load_current_structured_result(
        conn, subject_kind=SUBJECT_COLUMN, subject_ref=logical_ref,
        result_type=SEMANTIC_ADJUDICATION_RESULT_TYPE)
    if pointer is None or pointer.lifecycle != "current":
        return None, None
    stored = load_structured_result(conn, pointer.structured_result_id)
    if stored is None:
        return None, None
    return adjudication_from_output(stored.output), pointer.structured_result_id


# ── the bounded dispatch loop ───────────────────────────────────────────────────────────────────

def _dispatch_context(target: AdjudicationTargetV1,
                      ingestion_run_id: str | None) -> DispatchAuditContext | None:
    """The C5-T5 attribution for ONE adjudication dispatch: the run it serves, the stage, and the
    single column subject it is about.

    Threaded for the same reason every Pass-A stage threads it, and one more: the pre-dispatch
    audit gate is what makes egress FAIL-CLOSED when the audit store is unavailable. A stage that
    dispatched without it would keep talking to the provider during exactly the outage in which
    nothing can be recorded — an unauditable call is not a degraded call, it is an unlogged one.
    ``None`` (a direct caller with no run) dispatches unattributed, as every other seam does."""
    if ingestion_run_id is None:
        return None
    source, _schema, _table, _column = parse_ref(target.logical_ref)
    return DispatchAuditContext(
        ingestion_run_id=ingestion_run_id,
        stage=SEMANTIC_ADJUDICATION_STAGE,
        subjects=({"catalog_source": source, "object_ref": None,
                   "logical_ref": target.logical_ref, "field_names": ["concept"]},))


def _drive(conn, client: LLMClient, payload: dict[str, object],
           actor: IdentityEnvelope | None,
           dispatch_audit: DispatchAuditContext | None = None) -> tuple[dict | None, str | None]:
    """One audited structured call through the seam the concept critic uses. A client throw (an
    unscripted fake, a provider outage, a fail-closed pre-dispatch audit) is contained to
    ``(None, None)`` — adjudication is advisory and must degrade to `not_attempted`, never abort
    the ingest that hosts it."""
    try:
        call = drive_audited_structured_call(
            conn, client, task=SEMANTIC_ADJUDICATION_TASK,
            prompt_id=SEMANTIC_ADJUDICATION_PROMPT_ID,
            prompt_version=SEMANTIC_ADJUDICATION_PROMPT_VERSION,
            schema_id=SEMANTIC_ADJUDICATION_SCHEMA_ID,
            schema_version=SEMANTIC_ADJUDICATION_SCHEMA_VERSION,
            catalog_metadata=payload, instruction=_INSTRUCTION, actor=actor,
            run_id=SEMANTIC_ADJUDICATION_RUN_ID, record_egress_block=True,
            dispatch_audit=dispatch_audit,
            cacheable_metadata_keys=("vocabulary",))
    except Exception:  # noqa: BLE001 — advisory: a provider fault never aborts the ingest
        logger.warning("semantic adjudication call failed; treating as not attempted",
                       exc_info=True)
        return None, None
    return call.output, call.llm_call_ref


def adjudicate_targets(
    conn,
    client: LLMClient | None,
    targets: Sequence[AdjudicationTargetV1],
    *,
    catalog_revision: str,
    actor: IdentityEnvelope | None = None,
    bounds: AdjudicationBounds | None = None,
    ingestion_run_id: str | None = None,
) -> tuple[AdjudicationResultV1, ...]:
    """Adjudicate the selected targets under an explicit provider-call cap.

    Per item and fail-soft: a replay HIT is served from the 1039 store WITHOUT a call (and without
    spending the cap), a cap-exhausted or client-less item is `not_attempted`, a contained dispatch
    fault is `not_attempted`, and an output that fails the code-side gate is `invalid`. Every item
    in ``targets`` gets exactly one result — the caller's counts always sum to the target count."""
    limits = bounds or adjudication_bounds()
    results: list[AdjudicationResultV1] = []
    calls = 0
    for target in targets:
        payload = adjudication_payload(target)
        input_hash = adjudication_input_hash(payload, catalog_revision)
        stored = find_structured_result(
            conn, result_type=SEMANTIC_ADJUDICATION_RESULT_TYPE,
            result_version=SEMANTIC_ADJUDICATION_VERSION, input_content_hash=input_hash)
        if stored is not None:
            adjudication = adjudication_from_output(stored.output)
            if adjudication is None:
                results.append(AdjudicationResultV1(
                    target.logical_ref, AdjudicationOutcome.INVALID, None,
                    target.selection_reasons, stored.structured_result_id,
                    _stored_llm_call_ref(conn, stored.structured_result_id),
                    "adjudication_invalid_result"))
                continue
            _point(conn, target=target, structured_result_id=stored.structured_result_id,
                   adjudication=adjudication)
            results.append(AdjudicationResultV1(
                target.logical_ref, _outcome_for(target, adjudication), adjudication,
                target.selection_reasons, stored.structured_result_id,
                _stored_llm_call_ref(conn, stored.structured_result_id)))
            continue
        if client is None or calls >= limits.max_provider_calls:
            results.append(AdjudicationResultV1(
                target.logical_ref, AdjudicationOutcome.NOT_ATTEMPTED, None,
                target.selection_reasons, None, None,
                "adjudicator_unavailable" if client is None else "call_cap_reached"))
            continue
        calls += 1
        output, llm_call_ref = _drive(
            conn, client, payload, actor, _dispatch_context(target, ingestion_run_id))
        if output is None:
            results.append(AdjudicationResultV1(
                target.logical_ref, AdjudicationOutcome.NOT_ATTEMPTED, None,
                target.selection_reasons, None, llm_call_ref, "adjudicator_unavailable"))
            continue
        adjudication = adjudication_from_output(output)
        if adjudication is None:
            results.append(AdjudicationResultV1(
                target.logical_ref, AdjudicationOutcome.INVALID, None, target.selection_reasons,
                None, llm_call_ref, "adjudication_invalid_result"))
            continue
        result_id = _store(conn, input_hash=input_hash, target=target,
                           adjudication=adjudication, llm_call_ref=llm_call_ref or "")
        _point(conn, target=target, structured_result_id=result_id, adjudication=adjudication)
        results.append(AdjudicationResultV1(
            target.logical_ref, _outcome_for(target, adjudication), adjudication,
            target.selection_reasons, result_id, llm_call_ref))
    return tuple(results)


def outcome_counts(results: Sequence[AdjudicationResultV1]) -> dict[str, int]:
    """The six outcome counts, ALWAYS all six present (a zero is information — "nothing was
    invalid" is an answer, a missing key is a question). Ordered by :data:`_OUTCOME_ORDER`."""
    counts = {outcome.value: 0 for outcome in _OUTCOME_ORDER}
    for result in results:
        counts[result.outcome.value] += 1
    return counts
