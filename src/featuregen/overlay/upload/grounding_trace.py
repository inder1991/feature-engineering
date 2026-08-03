"""The grounding DECISION TRACE — why a candidate is what it is (Task 2A; freeze 0F-7).

The gauntlet (``feature_assist._validate_idea``) already reads type, grain, as-of, unit,
currency, additivity, relationship state and read scope; it then threw every one of those reads
away and returned a bare ``(FeatureIdea, Rejection)``. A later surface could therefore neither
EXPLAIN a ``DESIGN_CHECKED`` candidate nor INVALIDATE it when one of those inputs moved — except
by re-running the gauntlet and the join planner, which would be a second, freely-drifting copy of
the decision.

This module is the value-object half of the fix. It defines what a decision trace IS and how it
hashes; the trace is PRODUCED at the decision points themselves (``feature_assist`` for the
same-catalog gauntlet, ``join_path`` for the ordered path it selected, ``planner/plan_envelope``
for the cross-catalog directional realization) and merely CARRIED by ``contract/gate1``. No
adapter — emphatically not ``suggestions.py`` — may reconstruct one, and none may re-run path
selection to explain a path that was already selected.

**Two identities, deliberately separated.**

``trace_content_hash`` covers the DECISION and the LOGICAL content it rested on: the candidate
key, the ordered operand roles, the ordered relationship path (kind, direction, endpoints and the
selected realization's content hash per leg), the validation status, the canonicalized
requirements, the ``(class, kind, key, content_hash)`` dependency pins, and the exact validation /
read-scope rules that were evaluated. It EXCLUDES ``current_revision_id``, evidence occurrence ids
(``producer_ref`` / ``evidence_id``), timestamps and any other build observation — those persist
as scope/build provenance so a reader can compare CURRENTNESS without the identity moving every
time the same content is replayed under a new event id (freeze 0F-4 rule 3, 0F-7).

Evidence AXES (producer / strength / lifecycle) are content and DO enter the hash, through
:func:`~featuregen.contracts.evidence_axes.canonical_evidence_axes`, which sorts and deduplicates
them — so re-recording an identical occurrence moves nothing, while a value that weakened from
``confirmed`` to ``proposed`` moves everything.

**Leaf module by construction.** Nothing here imports ``featuregen.overlay.upload``: the producers
(``feature_assist`` / ``join_path`` / ``plan_envelope``) import THIS, and each of them sits inside
that package. The one place that needs the requirement registry
(:func:`trace_completeness_gaps`) imports it inside the function, mirroring the established
``feature_assist`` -> ``validation_requirements`` pattern.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.canonical import contract_hash_v1
from featuregen.contracts.contract_versions import register_contract_version
from featuregen.contracts.evidence_axes import (
    AssertionStrength,
    EvidenceAuthorityV1,
    EvidenceLifecycle,
    EvidenceProducer,
    canonical_evidence_axes,
)
from featuregen.contracts.relationship_kinds import validate_relationship_kind

__all__ = [
    "CONTRACT_VERSION",
    "DEPENDENCY_CONTRACT",
    "DEPENDENCY_KINDS",
    "REALIZATION_CONTRACT",
    "TRACE_CONTRACT",
    "GroundingDecisionTraceV1",
    "GroundingDependencyPinV1",
    "GroundingTraceRecorder",
    "SuggestionDependencyClass",
    "SuggestionRelationshipDependencyV1",
    "build_trace",
    "column_dependency_key",
    "dependency_pin",
    "recompute_trace_content_hash",
    "relationship_leg",
    "table_dependency_key",
    "trace_completeness_gaps",
]

_OWNER = "featuregen.overlay.upload.grounding_trace"
CONTRACT_VERSION = "1"
#: The trace identity itself.
TRACE_CONTRACT = "grounding-decision-trace"
#: One dependency pin's content (what a single check actually read).
DEPENDENCY_CONTRACT = "grounding-dependency-content"
#: One SELECTED directional realization of one traversed relationship leg.
REALIZATION_CONTRACT = "join-leg-realization"


class SuggestionDependencyClass(StrEnum):
    """How a dependency's drift must be handled downstream (plan "Visibility scope and dependency
    classes"): a HARD_AVAILABILITY loss withholds the hit entirely, a VALIDATION drift suppresses
    the readiness claim until rebuilt, a SEMANTIC drift only stales the wording."""

    HARD_AVAILABILITY = "hard_availability"
    VALIDATION = "validation"
    SEMANTIC = "semantic"


# ── dependency kinds: one per READ the gauntlet already performs ────────────────────────────────
# The name says WHICH read produced the pin, so a consumer can tell "the governed additivity said
# no" from "the additivity hint was empty" without re-reading anything.
GROUNDING_CANDIDATE_SET = "grounding_candidate_set"     # _ground_refs' resolved refs
READ_SCOPE = "read_scope"                               # the visibility classes this run applied
COLUMN_EXISTENCE = "column_existence"                   # _column_meta's per-pair existence check
COLUMN_UNIT_HINT = "column_unit_hint"                   # _column_meta unit hint
COLUMN_CURRENCY_HINT = "column_currency_hint"           # _column_meta currency hint
TEMPLATE_OPERAND_ROLES = "template_operand_roles"       # the template's own operand declaration
GOVERNED_LOGICAL_REPRESENTATION = "governed_logical_representation"   # C1 type read
DECLARED_TYPE_HINT = "declared_type_hint"               # the declared-type hint read beside it
GOVERNED_ADDITIVITY = "governed_additivity"             # C1 additivity read
AS_OF_COLUMN_LOOKUP = "as_of_column_lookup"             # _as_of_column_ref structural lookup
GOVERNED_IS_AS_OF = "governed_is_as_of"                 # C1 as-of read
GRAIN_COLUMN_LOOKUP = "grain_column_lookup"             # _grain_column_ref structural lookup
GOVERNED_IS_GRAIN = "governed_is_grain"                 # C1 grain read
AI_UNIT_SUGGESTION = "ai_unit_suggestion"               # llm/proposed unit surfaced on a requirement
AI_CURRENCY_SUGGESTION = "ai_currency_suggestion"       # llm/proposed currency, likewise
JOIN_PATH = "join_path"                                 # classify_join_path outcome per operand
CROSS_CATALOG_PATH_SEGMENT = "cross_catalog_path_segment"   # one governed plan path segment
CROSS_CATALOG_CONTRACT = "cross_catalog_contract"       # the governed plan's contract resolution

DEPENDENCY_KINDS: frozenset[str] = frozenset({
    GROUNDING_CANDIDATE_SET, READ_SCOPE, COLUMN_EXISTENCE, COLUMN_UNIT_HINT, COLUMN_CURRENCY_HINT,
    TEMPLATE_OPERAND_ROLES, GOVERNED_LOGICAL_REPRESENTATION, DECLARED_TYPE_HINT,
    GOVERNED_ADDITIVITY, AS_OF_COLUMN_LOOKUP, GOVERNED_IS_AS_OF, GRAIN_COLUMN_LOOKUP,
    GOVERNED_IS_GRAIN, AI_UNIT_SUGGESTION, AI_CURRENCY_SUGGESTION, JOIN_PATH,
    CROSS_CATALOG_PATH_SEGMENT, CROSS_CATALOG_CONTRACT,
})

#: Which pin kind MUST exist for a requirement of this code to be explainable, keyed at the
#: requirement's own operand. This is the "every requirement names the exact dependency/operand
#: that caused it" rule, expressed once.
_PIN_KIND_BY_REQUIREMENT: dict[str, str] = {
    "TYPE_IS_NUMERIC": GOVERNED_LOGICAL_REPRESENTATION,
    "ADDITIVITY_SUPPORTS_OPERATION": GOVERNED_ADDITIVITY,
    "TEMPORAL_IS_POPULATED": GOVERNED_IS_AS_OF,
    "GRAIN_IS_UNIQUE": GOVERNED_IS_GRAIN,
    "UNIT_CONSISTENT": COLUMN_UNIT_HINT,
    "CURRENCY_CONSISTENT": COLUMN_CURRENCY_HINT,
    "JOIN_CONNECTIVITY": JOIN_PATH,
}

#: Which pin kind must exist SOMEWHERE once a rule was EVALUATED — even where it cleared and minted
#: no requirement. Without this a trace could "explain" a cleared type check with no type pin.
_PIN_KIND_BY_EVALUATED_RULE: dict[str, str] = {
    "TYPE_IS_NUMERIC": GOVERNED_LOGICAL_REPRESENTATION,
    "ADDITIVITY_SUPPORTS_OPERATION": GOVERNED_ADDITIVITY,
    "TEMPORAL_IS_POPULATED": AS_OF_COLUMN_LOOKUP,
    "GRAIN_IS_UNIQUE": GRAIN_COLUMN_LOOKUP,
    "UNIT_CONSISTENT": COLUMN_UNIT_HINT,
    "CURRENCY_CONSISTENT": COLUMN_CURRENCY_HINT,
    "JOIN_CONNECTIVITY": JOIN_PATH,
}

#: The pins every trace carries whatever the candidate looked like: what the caller could SEE and
#: what the grounding step resolved. Both are HARD_AVAILABILITY — lose either and the candidate is
#: not merely stale, it is unshowable.
_ALWAYS_REQUIRED_PIN_KINDS = (READ_SCOPE, GROUNDING_CANDIDATE_SET)

#: A relationship leg's safety, decided by the SAME rule ``join_path._classified_edges`` used to
#: decide traversal: declared (no fact) or governed-VERIFIED clears; a fact-linked edge that is not
#: VERIFIED is authorized but not cleared.
SAFETY_CLEARING = "clearing"
SAFETY_UNVERIFIED = "unverified"
#: ``review_status`` for an edge no ``approved_join`` fact governs — declared by an upload and
#: confirmed by nobody. A meaningful answer, not a missing one.
REVIEW_FILE_DECLARED = "file_declared"
#: ``cardinality`` for an edge that declared none. Never guessed at 1:1.
CARDINALITY_UNKNOWN = "unknown"


def column_dependency_key(catalog_source: str, object_ref: str) -> str:
    """The pin key for a COLUMN dependency — the same ``(catalog_source, object_ref)`` pair a
    :class:`~featuregen.overlay.upload.feature_assist.Requirement` names as its operand, so a
    requirement and the read that caused it join on this string and on nothing looser."""
    return f"{catalog_source}::{object_ref}"


def table_dependency_key(catalog_source: str, table: str) -> str:
    """The pin key for a TABLE-scoped structural lookup (the as-of / grain column searches, which
    ask a question OF a table and may honestly answer "there is none")."""
    return f"{catalog_source}::table::{table}"


@dataclass(frozen=True, slots=True)
class GroundingDependencyPinV1:
    """One thing a check READ, pinned where it read it.

    ``content_hash`` is the identity of the value that was read (mint it with
    :func:`dependency_pin`); ``current_revision_id`` is the exact decision / fact / realization
    revision that produced it and is deliberately NOT part of any content identity — it is what a
    later reader compares against the current pointer to decide whether this trace is still true.
    """

    dependency_class: SuggestionDependencyClass
    dependency_kind: str
    dependency_key: str
    content_hash: str
    current_revision_id: str | None
    evidence: tuple[EvidenceAuthorityV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependency_class",
                           SuggestionDependencyClass(self.dependency_class))
        if self.dependency_kind not in DEPENDENCY_KINDS:
            raise ValueError(
                f"unknown dependency kind {self.dependency_kind!r}; a pin names one of the reads "
                f"the gauntlet actually performs: {sorted(DEPENDENCY_KINDS)}")


@dataclass(frozen=True, slots=True)
class SuggestionRelationshipDependencyV1:
    """One TRAVERSED relationship leg, in the direction it was traversed.

    ``relationship_ref`` is direction-free: one semantic relationship may expose many directional
    realizations, and flattening them into one arbitrary direction is exactly the error this shape
    prevents. ``realization_content_hash`` identifies the ONE realization that was selected;
    ``cardinality`` is stated for the direction of travel (a reverse ``N:1`` hop really is ``1:N``).
    """

    relationship_ref: str
    relationship_kind: str          # frozen vocabulary (freeze D3); the StrEnum lands with semantics
    from_ref: tuple[str, str]       # (catalog_source, object_ref) — the direction of travel
    to_ref: tuple[str, str]
    realization_content_hash: str | None
    cardinality: str
    safety_status: str
    review_status: str
    evidence: tuple[EvidenceAuthorityV1, ...]

    def __post_init__(self) -> None:
        validate_relationship_kind(self.relationship_kind)


@dataclass(frozen=True, slots=True)
class GroundingDecisionTraceV1:
    """Everything needed to EXPLAIN and INVALIDATE one candidate's decision without re-deciding it.

    Build it with :func:`build_trace` (which mints ``trace_content_hash``); never construct the
    hash by hand.
    """

    candidate_key: str
    ordered_operand_roles: tuple[tuple[str, str, str], ...]   # (catalog_source, object_ref, role)
    ordered_relationship_path: tuple[SuggestionRelationshipDependencyV1, ...]
    validation_status: str
    requirements: tuple[Any, ...]           # feature_assist.Requirement (D1); typed there, not here
    dependency_pins: tuple[GroundingDependencyPinV1, ...]
    validation_rule_content_hashes: tuple[str, ...]
    read_scope_rule_content_hashes: tuple[str, ...]
    trace_content_hash: str


# ── hashing (one place, the 0S canonical hasher) ────────────────────────────────────────────────
def _dependency_content_hash(dependency_kind: str, content: Mapping[str, Any],
                             evidence: Sequence[EvidenceAuthorityV1]) -> str:
    return contract_hash_v1(DEPENDENCY_CONTRACT, CONTRACT_VERSION, {
        "dependency_kind": dependency_kind,
        "content": dict(content),
        # AXES only — occurrence ids are provenance (0F-4 rule 3), and the canonical form is a
        # sorted, deduplicated set, so a replayed occurrence never moves a content identity.
        "evidence_axes": canonical_evidence_axes(evidence),
    })


def dependency_pin(*, dependency_class: SuggestionDependencyClass | str, dependency_kind: str,
                   dependency_key: str, content: Mapping[str, Any],
                   current_revision_id: str | None = None,
                   evidence: Sequence[EvidenceAuthorityV1] = ()) -> GroundingDependencyPinV1:
    """Mint a pin, hashing ``content`` (+ the evidence axes) into its identity."""
    if dependency_kind not in DEPENDENCY_KINDS:
        raise ValueError(
            f"unknown dependency kind {dependency_kind!r}; a pin names one of the reads the "
            f"gauntlet actually performs: {sorted(DEPENDENCY_KINDS)}")
    evidence = tuple(evidence)
    return GroundingDependencyPinV1(
        dependency_class=SuggestionDependencyClass(dependency_class),
        dependency_kind=dependency_kind,
        dependency_key=dependency_key,
        content_hash=_dependency_content_hash(dependency_kind, content, evidence),
        current_revision_id=current_revision_id,
        evidence=evidence)


def relationship_leg(*, relationship_ref: str, relationship_kind: str,
                     from_ref: tuple[str, str], to_ref: tuple[str, str],
                     realization_content: Mapping[str, Any] | None,
                     cardinality: str, safety_status: str, review_status: str,
                     evidence: Sequence[EvidenceAuthorityV1] = ()
                     ) -> SuggestionRelationshipDependencyV1:
    """Mint one traversed leg. ``realization_content`` is the SELECTED directional realization's
    content — endpoints, direction, cardinality and the governing fact — and never its revision id
    (that is a provenance pin on the dependency, not a field of the semantic dependency)."""
    validate_relationship_kind(relationship_kind)
    realization_hash = (
        None if realization_content is None
        else contract_hash_v1(REALIZATION_CONTRACT, CONTRACT_VERSION, dict(realization_content)))
    return SuggestionRelationshipDependencyV1(
        relationship_ref=relationship_ref, relationship_kind=relationship_kind,
        from_ref=from_ref, to_ref=to_ref, realization_content_hash=realization_hash,
        cardinality=cardinality, safety_status=safety_status, review_status=review_status,
        evidence=tuple(evidence))


def _param_value(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _canonical_requirement(requirement: Any) -> dict[str, Any]:
    """A requirement's FULL content — every field, unconditionally. Deliberately not
    ``requirements_to_json``: that wire form omits defaulted fields to keep persisted bytes
    byte-identical, which is exactly the wrong property for an identity payload."""
    return {
        "code": requirement.code,
        "operand": [requirement.operand[0], requirement.operand[1]],
        "detail": requirement.detail,
        "schema_version": requirement.schema_version,
        "params": [[name, _param_value(value)] for name, value in requirement.params],
    }


def _trace_payload(*, candidate_key: str,
                   ordered_operand_roles: Sequence[tuple[str, str, str]],
                   ordered_relationship_path: Sequence[SuggestionRelationshipDependencyV1],
                   validation_status: str, requirements: Sequence[Any],
                   dependency_pins: Sequence[GroundingDependencyPinV1],
                   validation_rule_content_hashes: Sequence[str],
                   read_scope_rule_content_hashes: Sequence[str]) -> dict[str, Any]:
    canonical_requirements = sorted(
        (_canonical_requirement(r) for r in requirements),
        key=lambda d: (d["code"], d["operand"], d["schema_version"], d["detail"],
                       repr(d["params"])))
    return {
        "candidate_key": candidate_key,
        # ORDERED: the operand order is the binding order the gauntlet reasoned in.
        "ordered_operand_roles": [list(role) for role in ordered_operand_roles],
        # ORDERED: a path is a sequence; reversing it is a different traversal.
        "ordered_relationship_path": [
            {"relationship_ref": leg.relationship_ref,
             "relationship_kind": leg.relationship_kind,
             "from_ref": [leg.from_ref[0], leg.from_ref[1]],
             "to_ref": [leg.to_ref[0], leg.to_ref[1]],
             "realization_content_hash": leg.realization_content_hash,
             "cardinality": leg.cardinality,
             "safety_status": leg.safety_status,
             "review_status": leg.review_status,
             "evidence_axes": canonical_evidence_axes(leg.evidence)}
            for leg in ordered_relationship_path],
        "validation_status": validation_status,
        # UNORDERED: the requirement set is a set; its mint order is an implementation detail of the
        # gauntlet's check order and must not fork identity.
        "requirements": canonical_requirements,
        # UNORDERED: pins are a set. `current_revision_id` and evidence occurrence ids are excluded
        # by construction — they are not in this tuple at all.
        "dependency_pins": sorted(
            [pin.dependency_class.value, pin.dependency_kind, pin.dependency_key, pin.content_hash]
            for pin in dependency_pins),
        "validation_rule_content_hashes": sorted(set(validation_rule_content_hashes)),
        "read_scope_rule_content_hashes": sorted(set(read_scope_rule_content_hashes)),
    }


def build_trace(*, candidate_key: str,
                ordered_operand_roles: Sequence[tuple[str, str, str]],
                ordered_relationship_path: Sequence[SuggestionRelationshipDependencyV1],
                validation_status: str, requirements: Sequence[Any],
                dependency_pins: Sequence[GroundingDependencyPinV1],
                validation_rule_content_hashes: Sequence[str],
                read_scope_rule_content_hashes: Sequence[str]) -> GroundingDecisionTraceV1:
    """Assemble the trace and mint its content hash. The carried tuples keep the producer's own
    order (the V1 payload reads ``requirements`` verbatim); only the HASH canonicalizes."""
    payload = _trace_payload(
        candidate_key=candidate_key, ordered_operand_roles=ordered_operand_roles,
        ordered_relationship_path=ordered_relationship_path, validation_status=validation_status,
        requirements=requirements, dependency_pins=dependency_pins,
        validation_rule_content_hashes=validation_rule_content_hashes,
        read_scope_rule_content_hashes=read_scope_rule_content_hashes)
    return GroundingDecisionTraceV1(
        candidate_key=candidate_key,
        ordered_operand_roles=tuple(ordered_operand_roles),
        ordered_relationship_path=tuple(ordered_relationship_path),
        validation_status=validation_status,
        requirements=tuple(requirements),
        dependency_pins=tuple(dependency_pins),
        validation_rule_content_hashes=tuple(validation_rule_content_hashes),
        read_scope_rule_content_hashes=tuple(read_scope_rule_content_hashes),
        trace_content_hash=contract_hash_v1(TRACE_CONTRACT, CONTRACT_VERSION, payload))


def recompute_trace_content_hash(trace: GroundingDecisionTraceV1) -> str:
    """Re-derive the hash from the trace's own carried content — the tamper/derivation check."""
    return contract_hash_v1(TRACE_CONTRACT, CONTRACT_VERSION, _trace_payload(
        candidate_key=trace.candidate_key,
        ordered_operand_roles=trace.ordered_operand_roles,
        ordered_relationship_path=trace.ordered_relationship_path,
        validation_status=trace.validation_status,
        requirements=trace.requirements,
        dependency_pins=trace.dependency_pins,
        validation_rule_content_hashes=trace.validation_rule_content_hashes,
        read_scope_rule_content_hashes=trace.read_scope_rule_content_hashes))


# ── completeness: the V2 admission rule ─────────────────────────────────────────────────────────
def trace_completeness_gaps(trace: GroundingDecisionTraceV1 | None, *, validation_status: str,
                            requirements: Sequence[Any] = ()) -> tuple[str, ...]:
    """The reasons this trace cannot explain that decision — empty means it can.

    A ``DESIGN_CHECKED`` candidate whose trace has ANY gap is invalid for V2 (freeze 0F-7): it
    would be a readiness claim nothing could justify or invalidate. The checks are deliberately
    structural — every requirement's own operand must be pinned by the read that caused it, every
    rule that was EVALUATED must have left its pin even where it cleared, an evaluated join rule
    must have left the ordered path it selected, and the stored hash must re-derive.
    """
    # Imported here, not at module scope: `validation_requirements` imports `feature_assist`, which
    # imports THIS module — the same cycle-avoiding local import `_validate_idea` already uses for
    # `build_requirement`.
    from featuregen.overlay.upload.validation_requirements import rule_codes_for_content_hashes

    if trace is None:
        return ("missing_trace",)
    gaps: list[str] = []
    if not trace.candidate_key:
        gaps.append("missing_candidate_key")
    if trace.validation_status != validation_status:
        gaps.append("validation_status_mismatch")
    if tuple(trace.requirements) != tuple(requirements):
        gaps.append("requirements_mismatch")
    if not trace.dependency_pins:
        gaps.append("no_dependency_pins")
    if not trace.read_scope_rule_content_hashes:
        gaps.append("missing_read_scope_rules")

    kinds = {pin.dependency_kind for pin in trace.dependency_pins}
    keys_by_kind: dict[str, set[str]] = {}
    for pin in trace.dependency_pins:
        keys_by_kind.setdefault(pin.dependency_kind, set()).add(pin.dependency_key)
    for kind in _ALWAYS_REQUIRED_PIN_KINDS:
        if kind not in kinds:
            gaps.append(f"unpinned_{kind}")

    for requirement in requirements:
        kind = _PIN_KIND_BY_REQUIREMENT.get(requirement.code)
        if kind is None:
            continue
        key = column_dependency_key(requirement.operand[0], requirement.operand[1])
        if key not in keys_by_kind.get(kind, ()):
            gaps.append(f"unpinned_requirement_operand:{requirement.code}:{key}")

    evaluated = rule_codes_for_content_hashes(trace.validation_rule_content_hashes)
    for code in sorted(evaluated):
        kind = _PIN_KIND_BY_EVALUATED_RULE.get(code)
        if kind is not None and kind not in kinds:
            gaps.append(f"unpinned_evaluated_rule:{code}")
    # A join rule was evaluated and the candidate was not rejected on it, so a path WAS selected —
    # it must be retained here or the only way to explain the candidate is to search again.
    if ("JOIN_CONNECTIVITY" in evaluated and validation_status != "REJECTED"
            and not trace.ordered_relationship_path):
        gaps.append("unpinned_relationship_path")

    if recompute_trace_content_hash(trace) != trace.trace_content_hash:
        gaps.append("trace_content_hash_mismatch")
    return tuple(gaps)


# ── the producer-side collector ─────────────────────────────────────────────────────────────────
class GroundingTraceRecorder:
    """Collects pins AT the decision points and mints the trace once, at the end.

    Deliberately mutable and deliberately dumb: it decides nothing, it only remembers what a check
    read. ``enabled`` is False when the caller threaded no candidate identity (the LLM / planner
    paths, which have no recipe candidate key and whose candidates V2 does not consume) — every
    method is then a cheap no-op and no hashing is done at all.
    """

    __slots__ = ("enabled", "_pins", "_legs", "_leg_seen", "_rules", "_operand_roles",
                 "_candidate_key", "_template_id", "_read_scope_rules")

    def __init__(self, *, candidate_key: str | None, template_id: str | None = None,
                 read_scope_rule_content_hashes: Sequence[str] = ()) -> None:
        self.enabled = candidate_key is not None
        self._candidate_key = candidate_key or ""
        self._template_id = template_id
        self._pins: list[GroundingDependencyPinV1] = []
        self._legs: list[SuggestionRelationshipDependencyV1] = []
        self._leg_seen: set[tuple[str, str, str, str | None]] = set()
        self._rules: set[str] = set()
        self._operand_roles: tuple[tuple[str, str, str], ...] = ()
        self._read_scope_rules = tuple(read_scope_rule_content_hashes)

    @property
    def template_id(self) -> str | None:
        return self._template_id

    def pin(self, dependency_class: SuggestionDependencyClass, dependency_kind: str,
            dependency_key: str, content: Mapping[str, Any], *,
            current_revision_id: str | None = None,
            evidence: Sequence[EvidenceAuthorityV1] = ()) -> None:
        if not self.enabled:
            return
        self._pins.append(dependency_pin(
            dependency_class=dependency_class, dependency_kind=dependency_kind,
            dependency_key=dependency_key, content=content,
            current_revision_id=current_revision_id, evidence=evidence))

    def record_operand_roles(self, roles: Sequence[tuple[str, str, str]]) -> None:
        if self.enabled:
            self._operand_roles = tuple(roles)

    def record_rule(self, code: str) -> None:
        """Remember that this registry rule was EVALUATED (not that it failed)."""
        if self.enabled:
            self._rules.add(code)

    def record_path(self, legs: Iterable[SuggestionRelationshipDependencyV1]) -> None:
        """Append the legs of one selected path, first-seen order preserved.

        Two operands in the same neighbour table are classified separately and select the same
        legs; recording the duplicate would claim a traversal that happened once happened twice.
        Which operand needed which legs stays recoverable from that operand's own JOIN_PATH pin.
        """
        if not self.enabled:
            return
        for leg in legs:
            identity = (leg.relationship_ref, leg.from_ref[1], leg.to_ref[1],
                        leg.realization_content_hash)
            if identity in self._leg_seen:
                continue
            self._leg_seen.add(identity)
            self._legs.append(leg)

    @property
    def evaluated_rule_codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    @property
    def selected_path(self) -> tuple[SuggestionRelationshipDependencyV1, ...]:
        return tuple(self._legs)

    def build(self, *, validation_status: str, requirements: Sequence[Any],
              validation_rule_content_hashes: Sequence[str]) -> GroundingDecisionTraceV1 | None:
        """The trace for this decision, or None when no candidate identity was threaded."""
        if not self.enabled:
            return None
        return build_trace(
            candidate_key=self._candidate_key,
            ordered_operand_roles=self._operand_roles,
            ordered_relationship_path=tuple(self._legs),
            validation_status=validation_status,
            requirements=tuple(requirements),
            dependency_pins=tuple(self._pins),
            validation_rule_content_hashes=tuple(validation_rule_content_hashes),
            read_scope_rule_content_hashes=self._read_scope_rules)


# ── evidence helpers shared by the producers ────────────────────────────────────────────────────
def governed_read_evidence(operational_value: Any) -> tuple[EvidenceAuthorityV1, ...]:
    """The evidence axes behind ONE C1 governed read, from the value the read already returned.

    ``read_operational_value`` resolves ``selected_evidence_ids`` against the currently ACTIVE
    field evidence and reports the STRONGEST selected (producer, strength) — so the axes are the
    read's own, and every selected occurrence id rides along as provenance. No second read.
    """
    producer = getattr(operational_value, "producer", None)
    strength = getattr(operational_value, "strength", None)
    if producer is None or strength is None:
        return ()
    fact_key = getattr(operational_value, "fact_key", None)
    ids = tuple(getattr(operational_value, "selected_evidence_ids", ()) or ())
    if not ids:
        return (EvidenceAuthorityV1(producer, strength, EvidenceLifecycle.ACTIVE, fact_key, None),)
    return tuple(EvidenceAuthorityV1(producer, strength, EvidenceLifecycle.ACTIVE, fact_key, eid)
                 for eid in ids)


def join_edge_evidence(*, approved_join_fact_key: str | None,
                       approved_join_status: str | None) -> tuple[EvidenceAuthorityV1, ...]:
    """The evidence axes of one join edge, from what the traversal already knew about it.

    Every ``graph_edge`` originates in an upload's declared relationships, so the PRODUCER is the
    source; what the governed ``approved_join`` fact adds is STRENGTH. Nothing here claims a human
    actor the step did not carry.
    """
    strength = (AssertionStrength.CONFIRMED if approved_join_status == "VERIFIED"
                else AssertionStrength.PROPOSED)
    return (EvidenceAuthorityV1(EvidenceProducer.SOURCE, strength, EvidenceLifecycle.ACTIVE,
                                approved_join_fact_key, None),)


register_contract_version(TRACE_CONTRACT, CONTRACT_VERSION, owner=_OWNER)
register_contract_version(DEPENDENCY_CONTRACT, CONTRACT_VERSION, owner=_OWNER)
register_contract_version(REALIZATION_CONTRACT, CONTRACT_VERSION, owner=_OWNER)
