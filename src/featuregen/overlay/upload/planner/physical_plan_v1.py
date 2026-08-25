"""The physical plan layer: HOW one logical plan is realized (R2: never in logical identity).

Everything execution-shaped lives here — the execution context, frozen source bindings, per-segment
realization identity, ordered column pairs, predicates, directional cardinality, R14's PHYSICAL
temporal binding (which columns and parameters realize the declared semantics), R11's
``JoinKeyNormalizationPolicy``, and the ``JoinValidationPolicyRevisionV1`` guard policy with the
fan-out law.

**Reused vocabularies, never redefined** (the plan's "never a second temporal language" law):

* ``ColumnPairV1``, ``AsOfIntervalRequirementV1``, ``FixedValueReferencePredicateV1`` and
  ``DirectionalCardinalityVerdictV1`` from :mod:`featuregen.overlay.upload.bridge_realization`;
* ``TEMPORAL_POLICY_ID_PREFIX`` / ``DatasetTemporalPolicyRevisionV1`` /
  ``TemporalSelectionKind`` from :mod:`featuregen.overlay.upload.temporal_policy`;
* the ``TEMPORAL_HISTORICAL_CURRENT_ONLY`` refusal code from ``source_selection``;
* the literal-cutoff rejection from ``temporal_policy`` (a report date is a parameter, never an
  identity-entering value).

**R14: no defaults.** ``None`` is accepted only as an EXPLICIT declaration of a column that does
not exist on the dataset (a snapshot dimension has no ``effective_from``); omission is a
construction error, and a binding anchoring NO temporal column at all refuses — the type never
fabricates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.bridge_realization import (
    AsOfIntervalRequirementV1,
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    FixedValueReferencePredicateV1,
)
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    ContractDefect,
    TemporalEvaluationPurposeV1,
    _column_ref,
    _enum,
    _non_empty,
)
from featuregen.overlay.upload.semantic_eligibility_reasons import ALLOCATION_POLICY_REQUIRED
from featuregen.overlay.upload.source_selection import TEMPORAL_HISTORICAL_CURRENT_ONLY
from featuregen.overlay.upload.temporal_policy import (
    TEMPORAL_POLICY_ID_PREFIX,
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
    _reject_literal_cutoff,
)

__all__ = [
    "ALLOCATION_POLICY_REQUIRED",
    "AsOfIntervalRequirementV1",
    "BlankKeyBehaviorV1",
    "CaseNormalizationV1",
    "ColumnPairV1",
    "CompositeKeyOrderingV1",
    "CoverageDenominatorV1",
    "CoverageNumeratorV1",
    "DirectionalCardinalityVerdictV1",
    "FanOutControlOperatorV1",
    "FixedValueReferencePredicateV1",
    "JOIN_VALIDATION_POLICY_ID_PREFIX",
    "JoinKeyNormalizationPolicy",
    "JoinOrientationV1",
    "JoinValidationPolicyRevisionV1",
    "LeadingZeroPolicyV1",
    "NullKeyBehaviorV1",
    "PhysicalExecutionPlanV1",
    "PhysicalJoinSegmentV1",
    "PhysicalTemporalJoinBindingV1",
    "SnapshotSelectionRuleV1",
    "TEMPORAL_POLICY_ID_PREFIX",
    "UnmatchedRowBehaviorV1",
    "WhitespaceNormalizationV1",
    "physical_digest",
    "validate_temporal_binding_for_purpose",
]

PHYSICAL_PLAN_CONTRACT = "physical_execution_plan_v1"
_SEGMENT_CONTRACT = "physical_join_segment_v1"
_BINDING_CONTRACT = "physical_temporal_join_binding_v1"
_NORMALIZATION_CONTRACT = "join_key_normalization_policy_v1"
_VALIDATION_POLICY_CONTRACT = "join_validation_policy_revision_v1"

#: Deterministic id prefix for a join-validation policy revision (the ``dtp_``/``cpr_`` family).
JOIN_VALIDATION_POLICY_ID_PREFIX = "jvp_"

#: The fan-out law's refusal code (plan §Identity chain; owner's matrix row "Known M:N, final
#: grain"). ONE spelling: since A1's three-part registration the constant is OWNED by the closed
#: vocabulary (`semantic_eligibility_reasons`, with a family row and an all-six-actions
#: disposition row) and re-exported from the top import block here, where the fan-out law that
#: emits it lives.

_HEX_DIGITS = frozenset("0123456789abcdef")


def _hex_digest(raw: object, *, what: str) -> str:
    text = _non_empty(raw, what=what)
    if len(text) != 64 or not set(text) <= _HEX_DIGITS:
        raise ContractDefect(f"{what} must be a 64-char lowercase sha256 hex digest, got {raw!r}")
    return text


# ─────────────────────────────────────────────────────────────────────────────────────────────
# R11: JoinKeyNormalizationPolicy — every comparison rule DECLARED, none assumed
# ─────────────────────────────────────────────────────────────────────────────────────────────
class WhitespaceNormalizationV1(StrEnum):
    PRESERVE = "preserve"
    TRIM = "trim"


class CaseNormalizationV1(StrEnum):
    PRESERVE = "preserve"
    FOLD_LOWER = "fold_lower"
    FOLD_UPPER = "fold_upper"


class LeadingZeroPolicyV1(StrEnum):
    PRESERVE = "preserve"
    STRIP = "strip"


class BlankKeyBehaviorV1(StrEnum):
    TREAT_AS_VALUE = "treat_as_value"
    NEVER_MATCH = "never_match"


class CompositeKeyOrderingV1(StrEnum):
    """One sanctioned member: composite keys compare in the DECLARED ordered-pair order. The enum
    exists so the declaration is explicit on every policy, never an unstated assumption."""

    DECLARED_PAIR_ORDER = "declared_pair_order"


@dataclass(frozen=True, slots=True)
class JoinKeyNormalizationPolicy:
    """R11 (round-13 P0-7): how join-key VALUES compare — all explicit, no hidden defaults.

    ``declared_type_coercions`` is the closed list of physical-type pairs DECLARED comparable
    (``varchar(150)`` ↔ ``string`` is declared, never assumed); an empty tuple declares that no
    cross-type comparison is permitted. ``nulls_never_match`` is a LAW, not a choice — the field
    exists so every policy states it, and ``False`` is refused outright."""

    whitespace: WhitespaceNormalizationV1
    case_handling: CaseNormalizationV1
    leading_zeros: LeadingZeroPolicyV1
    declared_type_coercions: tuple[tuple[str, str], ...]
    blank_key_behavior: BlankKeyBehaviorV1
    nulls_never_match: bool
    composite_key_ordering: CompositeKeyOrderingV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "whitespace", _enum(
            self.whitespace, WhitespaceNormalizationV1, what="whitespace"))
        object.__setattr__(self, "case_handling", _enum(
            self.case_handling, CaseNormalizationV1, what="case_handling"))
        object.__setattr__(self, "leading_zeros", _enum(
            self.leading_zeros, LeadingZeroPolicyV1, what="leading_zeros"))
        coercions = tuple(
            (_non_empty(a, what="declared_type_coercions from-type"),
             _non_empty(b, what="declared_type_coercions to-type"))
            for a, b in self.declared_type_coercions)
        if len(set(coercions)) != len(coercions):
            raise ContractDefect("declared_type_coercions must be distinct pairs")
        object.__setattr__(self, "declared_type_coercions", coercions)
        object.__setattr__(self, "blank_key_behavior", _enum(
            self.blank_key_behavior, BlankKeyBehaviorV1, what="blank_key_behavior"))
        if self.nulls_never_match is not True:
            raise ContractDefect(
                "nulls_never_match must be True: null join keys matching each other is not a "
                "policy anyone may declare (R11); the field exists so the law is stated, not "
                "so it can be waived")
        object.__setattr__(self, "composite_key_ordering", _enum(
            self.composite_key_ordering, CompositeKeyOrderingV1, what="composite_key_ordering"))

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract": _NORMALIZATION_CONTRACT,
            "whitespace": self.whitespace.value,
            "case_handling": self.case_handling.value,
            "leading_zeros": self.leading_zeros.value,
            "declared_type_coercions": [list(pair) for pair in self.declared_type_coercions],
            "blank_key_behavior": self.blank_key_behavior.value,
            "nulls_never_match": True,
            "composite_key_ordering": self.composite_key_ordering.value,
        }


# ─────────────────────────────────────────────────────────────────────────────────────────────
# R14's physical half: which columns/parameters realize the declared temporal semantics
# ─────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PhysicalTemporalJoinBindingV1:
    """The PHYSICAL temporal binding of one join segment — inside :func:`physical_digest`.

    ``None`` on a column ref is an EXPLICIT declaration that the dataset has no such column (a
    snapshot dimension has no ``effective_from``); it is never a default. A binding that anchors
    NO temporal column at all refuses — it would bind the semantics to nothing. Interval bounds
    come in pairs. ``tie_break_column_refs`` must be non-empty: deterministic selection over
    overlapping or tied rows requires a DECLARED total order, never "first row read wins"."""

    dataset_temporal_policy_revision_id: str
    effective_from_column_ref: str | None
    effective_to_column_ref: str | None
    availability_or_knowledge_time_column_ref: str | None
    cutoff_parameter_ref: str
    source_binding_revision_id: str
    tie_break_column_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        policy_id = _non_empty(self.dataset_temporal_policy_revision_id,
                               what="dataset_temporal_policy_revision_id")
        if not policy_id.startswith(TEMPORAL_POLICY_ID_PREFIX):
            raise ContractDefect(
                f"dataset_temporal_policy_revision_id {policy_id!r} is not a "
                f"{TEMPORAL_POLICY_ID_PREFIX}<hash> revision id — the binding must name the "
                "governed temporal policy it realizes, never a free label")
        object.__setattr__(self, "dataset_temporal_policy_revision_id", policy_id)
        for name in ("effective_from_column_ref", "effective_to_column_ref",
                     "availability_or_knowledge_time_column_ref"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _column_ref(value, what=name))
        if (self.effective_from_column_ref is None) != (self.effective_to_column_ref is None):
            raise ContractDefect(
                "interval bounds come in pairs: effective_from_column_ref and "
                "effective_to_column_ref must both be named or both be explicitly absent")
        if (self.effective_from_column_ref is None
                and self.availability_or_knowledge_time_column_ref is None):
            raise ContractDefect(
                "a temporal binding that names NO temporal column binds the declared semantics "
                "to nothing; declare the interval pair or the availability/knowledge-time "
                "column — the type never fabricates one")
        object.__setattr__(self, "cutoff_parameter_ref", _reject_literal_cutoff(
            _non_empty(self.cutoff_parameter_ref, what="cutoff_parameter_ref"),
            what="cutoff_parameter_ref"))
        object.__setattr__(self, "source_binding_revision_id", _non_empty(
            self.source_binding_revision_id, what="source_binding_revision_id"))
        ties = tuple(_column_ref(r, what="tie_break_column_ref")
                     for r in self.tie_break_column_refs)
        if not ties:
            raise ContractDefect(
                "tie_break_column_refs must be non-empty: two rows valid at one cutoff (or "
                "overlapping intervals) need a DECLARED deterministic order, never whichever "
                "row is read first (the TEMPORAL_SNAPSHOT_TIE condition, settled at declaration)")
        if len(set(ties)) != len(ties):
            raise ContractDefect("tie_break_column_refs must be distinct")
        object.__setattr__(self, "tie_break_column_refs", ties)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract": _BINDING_CONTRACT,
            "dataset_temporal_policy_revision_id": self.dataset_temporal_policy_revision_id,
            "effective_from_column_ref": self.effective_from_column_ref,
            "effective_to_column_ref": self.effective_to_column_ref,
            "availability_or_knowledge_time_column_ref":
                self.availability_or_knowledge_time_column_ref,
            "cutoff_parameter_ref": self.cutoff_parameter_ref,
            "source_binding_revision_id": self.source_binding_revision_id,
            "tie_break_column_refs": list(self.tie_break_column_refs),
        }


def validate_temporal_binding_for_purpose(
    binding: PhysicalTemporalJoinBindingV1,
    policy: DatasetTemporalPolicyRevisionV1,
    purpose: TemporalEvaluationPurposeV1 | str,
) -> None:
    """R14's fifth mandatory case at contract level.

    The binding must actually name the policy it claims to realize, and a policy whose historical
    selection is ``explicit_only`` (the honest declaration of a current-only/history-less dataset)
    cannot serve a purpose that reconstructs the past."""
    resolved = _enum(purpose, TemporalEvaluationPurposeV1, what="purpose")
    if binding.dataset_temporal_policy_revision_id != policy.revision_id:
        raise ContractDefect(
            f"the binding names policy {binding.dataset_temporal_policy_revision_id!r} but was "
            f"validated against {policy.revision_id!r}: a binding is validated against the exact "
            "revision it pins, never a look-alike")
    if (resolved in (TemporalEvaluationPurposeV1.TRAINING,
                     TemporalEvaluationPurposeV1.BACKTESTING)
            and policy.historical_selection is TemporalSelectionKind.EXPLICIT_ONLY):
        raise ContractDefect(
            f"dataset {policy.dataset_logical_ref!r} declares historical selection "
            f"'explicit_only' ({policy.temporal_storage_model.value} storage): it cannot answer "
            f"a {resolved.value} question, which reconstructs the past — ask another source or "
            "declare an explicit limitation",
            code=TEMPORAL_HISTORICAL_CURRENT_ONLY)


# ─────────────────────────────────────────────────────────────────────────────────────────────
# JoinValidationPolicyRevisionV1 — closed enums, semantic-only hash, the fan-out law
# ─────────────────────────────────────────────────────────────────────────────────────────────
class NullKeyBehaviorV1(StrEnum):
    EXCLUDE_ROW = "exclude_row"
    REFUSE = "refuse"


class UnmatchedRowBehaviorV1(StrEnum):
    PRESERVE_LEFT_NULL = "preserve_left_null"
    EXCLUDE = "exclude"
    REFUSE = "refuse"


class CoverageNumeratorV1(StrEnum):
    MATCHED_LEFT_ROWS = "matched_left_rows"


class CoverageDenominatorV1(StrEnum):
    ALL_LEFT_ROWS = "all_left_rows"
    NON_NULL_KEY_LEFT_ROWS = "non_null_key_left_rows"


class JoinOrientationV1(StrEnum):
    LEFT_DRIVING = "left_driving"
    RIGHT_DRIVING = "right_driving"


class SnapshotSelectionRuleV1(StrEnum):
    """HOW a snapshot is selected — the RULE lives in the policy; the concrete snapshot ids land
    in (future) runtime observations, never here."""

    LATEST_AT_OR_BEFORE_CUTOFF = "latest_snapshot_at_or_before_cutoff"
    EXACT_AT_CUTOFF = "exact_snapshot_at_cutoff"
    NOT_APPLICABLE = "not_applicable"


class FanOutControlOperatorV1(StrEnum):
    """The CLOSED set of typed operators that prove a fan-out is controlled."""

    PRE_AGGREGATION = "pre_aggregation"
    DETERMINISTIC_DEDUP = "deterministic_dedup"
    GOVERNED_ALLOCATION = "governed_allocation"


@dataclass(frozen=True, slots=True)
class JoinValidationPolicyRevisionV1:
    """One immutable join-validation policy revision — the guard policy a preview compiles in.

    ``content_hash`` covers SEMANTIC fields only; ``declared_by``/``declared_at`` are provenance
    and re-declaring the same policy under another name is the SAME revision.

    THE FAN-OUT LAW (validated at construction): a policy applying to a final-grain aggregate
    that admits more than one match per left row multiplies contributions — refused with
    ``ALLOCATION_POLICY_REQUIRED`` unless a typed operator from the closed
    :class:`FanOutControlOperatorV1` set proves the fan-out is controlled."""

    null_key_behavior: NullKeyBehaviorV1
    unmatched_row_behavior: UnmatchedRowBehaviorV1
    coverage_numerator: CoverageNumeratorV1
    coverage_denominator: CoverageDenominatorV1
    minimum_coverage_ratio: float
    orientation: JoinOrientationV1
    max_matches_per_left_row: int
    snapshot_selection_rule: SnapshotSelectionRuleV1
    applies_to_final_grain_aggregate: bool
    fan_out_control_operator: FanOutControlOperatorV1 | None
    declared_by: str
    declared_at: str
    content_hash: str = field(init=False, default="")
    revision_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "null_key_behavior", _enum(
            self.null_key_behavior, NullKeyBehaviorV1, what="null_key_behavior"))
        object.__setattr__(self, "unmatched_row_behavior", _enum(
            self.unmatched_row_behavior, UnmatchedRowBehaviorV1, what="unmatched_row_behavior"))
        object.__setattr__(self, "coverage_numerator", _enum(
            self.coverage_numerator, CoverageNumeratorV1, what="coverage_numerator"))
        object.__setattr__(self, "coverage_denominator", _enum(
            self.coverage_denominator, CoverageDenominatorV1, what="coverage_denominator"))
        ratio = self.minimum_coverage_ratio
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or \
                ratio != ratio or not 0.0 <= float(ratio) <= 1.0:
            raise ContractDefect(
                f"minimum_coverage_ratio must be a number in [0, 1], got {ratio!r}")
        object.__setattr__(self, "minimum_coverage_ratio", float(ratio))
        object.__setattr__(self, "orientation", _enum(
            self.orientation, JoinOrientationV1, what="orientation"))
        if not isinstance(self.max_matches_per_left_row, int) \
                or isinstance(self.max_matches_per_left_row, bool) \
                or self.max_matches_per_left_row < 1:
            raise ContractDefect(
                f"max_matches_per_left_row must be an integer >= 1, got "
                f"{self.max_matches_per_left_row!r}")
        object.__setattr__(self, "snapshot_selection_rule", _enum(
            self.snapshot_selection_rule, SnapshotSelectionRuleV1,
            what="snapshot_selection_rule"))
        if not isinstance(self.applies_to_final_grain_aggregate, bool):
            raise ContractDefect("applies_to_final_grain_aggregate must be an explicit bool")
        if self.fan_out_control_operator is not None:
            object.__setattr__(self, "fan_out_control_operator", _enum(
                self.fan_out_control_operator, FanOutControlOperatorV1,
                what="fan_out_control_operator"))
        object.__setattr__(self, "declared_by", _non_empty(self.declared_by, what="declared_by"))
        object.__setattr__(self, "declared_at", _non_empty(self.declared_at, what="declared_at"))

        # THE FAN-OUT LAW.
        if (self.applies_to_final_grain_aggregate
                and self.max_matches_per_left_row > 1
                and self.fan_out_control_operator is None):
            raise ContractDefect(
                f"a final-grain aggregate admitting up to {self.max_matches_per_left_row} "
                "matches per left row multiplies contributions; declare a typed operator from "
                f"{sorted(m.value for m in FanOutControlOperatorV1)} or an allocation policy",
                code=ALLOCATION_POLICY_REQUIRED)

        content_hash = materialize_hash(self.content_payload())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "revision_id",
                           f"{JOIN_VALIDATION_POLICY_ID_PREFIX}{content_hash}")

    def content_payload(self) -> dict[str, Any]:
        """SEMANTIC fields only — ``declared_by``/``declared_at`` are provenance, excluded."""
        return {
            "contract": _VALIDATION_POLICY_CONTRACT,
            "null_key_behavior": self.null_key_behavior.value,
            "unmatched_row_behavior": self.unmatched_row_behavior.value,
            "coverage_numerator": self.coverage_numerator.value,
            "coverage_denominator": self.coverage_denominator.value,
            "minimum_coverage_ratio": self.minimum_coverage_ratio,
            "orientation": self.orientation.value,
            "max_matches_per_left_row": self.max_matches_per_left_row,
            "snapshot_selection_rule": self.snapshot_selection_rule.value,
            "applies_to_final_grain_aggregate": self.applies_to_final_grain_aggregate,
            "fan_out_control_operator": (None if self.fan_out_control_operator is None
                                         else self.fan_out_control_operator.value),
        }


# ─────────────────────────────────────────────────────────────────────────────────────────────
# The plan itself
# ─────────────────────────────────────────────────────────────────────────────────────────────
_PREDICATE_TYPES = (FixedValueReferencePredicateV1, AsOfIntervalRequirementV1)


@dataclass(frozen=True, slots=True)
class PhysicalJoinSegmentV1:
    """One realized join hop: the exact provisional/adopted realization revision, the ordered
    column pairs it executes, its predicates, proven directional cardinality (or UNKNOWN), the
    realization's content + dependency hashes, R11's key-normalization policy, and R14's
    physical temporal binding."""

    realization_revision_id: str
    column_pairs: tuple[ColumnPairV1, ...]
    predicates: tuple[FixedValueReferencePredicateV1 | AsOfIntervalRequirementV1, ...]
    directional_cardinality: DirectionalCardinalityVerdictV1
    realization_content_hash: str
    realization_dependency_hash: str
    key_normalization: JoinKeyNormalizationPolicy
    temporal_binding: PhysicalTemporalJoinBindingV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "realization_revision_id", _non_empty(
            self.realization_revision_id, what="realization_revision_id"))
        pairs = tuple(self.column_pairs)
        if not pairs:
            raise ContractDefect("a join segment must carry at least one ordered column pair")
        for pair in pairs:
            if not isinstance(pair, ColumnPairV1):
                raise ContractDefect("column_pairs must be bridge_realization.ColumnPairV1")
        object.__setattr__(self, "column_pairs", pairs)
        predicates = tuple(self.predicates)
        for predicate in predicates:
            if not isinstance(predicate, _PREDICATE_TYPES):
                raise ContractDefect(
                    "predicates must be the closed bridge_realization predicate types "
                    f"{tuple(t.__name__ for t in _PREDICATE_TYPES)}, never free SQL")
        object.__setattr__(self, "predicates", predicates)
        if not isinstance(self.directional_cardinality, DirectionalCardinalityVerdictV1):
            raise ContractDefect(
                "directional_cardinality must be a DirectionalCardinalityVerdictV1 "
                "(UNKNOWN is spelled DirectionalCardinalityVerdictV1.unknown(), never None)")
        object.__setattr__(self, "realization_content_hash", _hex_digest(
            self.realization_content_hash, what="realization_content_hash"))
        object.__setattr__(self, "realization_dependency_hash", _hex_digest(
            self.realization_dependency_hash, what="realization_dependency_hash"))
        if not isinstance(self.key_normalization, JoinKeyNormalizationPolicy):
            raise ContractDefect("key_normalization must be a JoinKeyNormalizationPolicy (R11)")
        if not isinstance(self.temporal_binding, PhysicalTemporalJoinBindingV1):
            raise ContractDefect(
                "temporal_binding must be a PhysicalTemporalJoinBindingV1 — a segment with no "
                "physical temporal binding stays TEMPORAL_JOIN_POLICY_MISSING at the consuming "
                "layer; this type never fabricates one")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract": _SEGMENT_CONTRACT,
            "realization_revision_id": self.realization_revision_id,
            "column_pairs": [p.identity_payload() for p in self.column_pairs],   # ORDERED
            "predicates": [p.identity_payload() for p in self.predicates],       # ORDERED
            "directional_cardinality": self.directional_cardinality.identity_payload(),
            "realization_content_hash": self.realization_content_hash,
            "realization_dependency_hash": self.realization_dependency_hash,
            "key_normalization": self.key_normalization.identity_payload(),
            "temporal_binding": self.temporal_binding.identity_payload(),
        }


@dataclass(frozen=True, slots=True)
class PhysicalExecutionPlanV1:
    """The complete physical identity of one member's execution (R2: everything here may change
    without touching the logical feature).

    ``source_binding_revisions`` are ``(dataset_logical_ref, revision_id)`` pairs — a set keyed
    by dataset (payload sorted, duplicates refused). ``segments`` are ORDERED (the traversal).
    ``join_validation_policy_revision_id`` pins the exact guard-policy revision."""

    logical_digest_ref: str
    execution_context_revision_id: str
    source_binding_revisions: tuple[tuple[str, str], ...]
    segments: tuple[PhysicalJoinSegmentV1, ...]
    join_validation_policy_revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_digest_ref", _hex_digest(
            self.logical_digest_ref, what="logical_digest_ref"))
        object.__setattr__(self, "execution_context_revision_id", _non_empty(
            self.execution_context_revision_id, what="execution_context_revision_id"))
        bindings = tuple(
            (_non_empty(ref, what="source binding dataset ref"),
             _non_empty(revision, what=f"source binding revision for {ref!r}"))
            for ref, revision in self.source_binding_revisions)
        if not bindings:
            raise ContractDefect("a physical plan must freeze at least one source binding")
        refs = [ref for ref, _ in bindings]
        if len(set(refs)) != len(refs):
            raise ContractDefect(f"source binding dataset refs must be distinct, got {refs}")
        object.__setattr__(self, "source_binding_revisions", bindings)
        segments = tuple(self.segments)
        for segment in segments:
            if not isinstance(segment, PhysicalJoinSegmentV1):
                raise ContractDefect("segments must be PhysicalJoinSegmentV1 instances")
        object.__setattr__(self, "segments", segments)
        policy_id = _non_empty(self.join_validation_policy_revision_id,
                               what="join_validation_policy_revision_id")
        if not policy_id.startswith(JOIN_VALIDATION_POLICY_ID_PREFIX):
            raise ContractDefect(
                f"join_validation_policy_revision_id {policy_id!r} is not a "
                f"{JOIN_VALIDATION_POLICY_ID_PREFIX}<hash> revision id — the plan pins the exact "
                "guard-policy revision, never a free label")
        object.__setattr__(self, "join_validation_policy_revision_id", policy_id)

    def content_payload(self) -> dict[str, Any]:
        return {
            "contract": PHYSICAL_PLAN_CONTRACT,
            "logical_digest": self.logical_digest_ref,
            "execution_context_revision_id": self.execution_context_revision_id,
            "source_binding_revisions": [
                [ref, revision]
                for ref, revision in sorted(self.source_binding_revisions)],
            "segments": [s.identity_payload() for s in self.segments],           # ORDERED
            "join_validation_policy_revision_id": self.join_validation_policy_revision_id,
        }


def physical_digest(plan: PhysicalExecutionPlanV1) -> str:
    """The physical stage's digest: sha256 hex over the plan's canonical serialization."""
    if not isinstance(plan, PhysicalExecutionPlanV1):
        raise ContractDefect("physical_digest takes a PhysicalExecutionPlanV1")
    return materialize_hash(plan.content_payload())
