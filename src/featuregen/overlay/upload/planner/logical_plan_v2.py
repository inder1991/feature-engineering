"""R9's logical plan layer: feature MEANING only, plus R14's logical temporal join semantics.

The three-layer identity split (plan `2026-08-24-cross-catalog-serving-first-card.md`,
execution-order item 3):

* THIS module — what the feature MEANS. Canonical definition, operation, typed operand bindings
  with their governed semantic revisions, the ORDERED output grain, the SELECTED parameter values,
  the logical relationship path with FULL ordered endpoint tuples, formula-policy identities, and
  — per R14 — the temporal JOIN semantics, because ``as_of_event_time`` vs ``as_of_cutoff`` and
  ``as_known_at_cutoff`` vs ``latest_available`` change what the feature IS.
* :mod:`.physical_plan_v1` — how the meaning is realized (R2: never in logical identity).
* :mod:`.render_profile` — how the realization is rendered.

**Provenance NEVER enters a digest** (R9's staleness law). Hypothesis text, planning-request hash,
chooser revision, menu content, display text ride the :class:`LogicalPlanProvenanceV1` side-car:
carried, recorded, and excluded from :func:`logical_digest`, so a link flipping proposed→confirmed
or a menu growing an unused allowed value can never rekey a feature.

**Never a second temporal language.** The knowledge/effective-time vocabulary is
:class:`featuregen.materialize.boundary_v2.KnowledgeTimeBasisV2`, imported, not redefined. The
interval-boundary spelling is the exact half-open ``[from,to)`` literal
``bridge_realization.AsOfIntervalRequirementV1`` already pins.

**R14: no defaults.** Every field of every contract here is explicit; a missing value is a
construction error. The type never fabricates a temporal meaning nobody declared —
``TEMPORAL_JOIN_POLICY_MISSING`` stays the consuming layer's refusal.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any

from featuregen.materialize.boundary_v2 import KnowledgeTimeBasisV2
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

__all__ = [
    "ContractDefect",
    "DrivingTimeRoleV1",
    "IntervalBoundaryPolicyV1",
    "KnowledgeTimeBasisV2",
    "LATEST_AVAILABLE_REFUSED_FOR_PURPOSE",
    "LogicalFeaturePlanV2",
    "LogicalOperandBindingV1",
    "LogicalPlanProvenanceV1",
    "LogicalRelationshipSegmentV1",
    "LogicalTemporalJoinSemanticsV1",
    "StaticLinkMeaningV1",
    "TemporalEvaluationPurposeV1",
    "UnmatchedRowMeaningV1",
    "logical_digest",
    "validate_temporal_semantics_for_purpose",
]

LOGICAL_PLAN_CONTRACT = "logical_feature_plan_v2"
_SEMANTICS_CONTRACT = "logical_temporal_join_semantics_v1"

#: Refusal code for R14's sixth mandatory case: ``latest_available`` knowledge for a purpose that
#: reconstructs the past (training/backtesting) reads facts the model could not have known.
LATEST_AVAILABLE_REFUSED_FOR_PURPOSE = "LATEST_AVAILABLE_REFUSED_FOR_PURPOSE"


class ContractDefect(ValueError):
    """A malformed identity contract — a CONSTRUCTION error, never a governed runtime refusal.

    ``code`` carries a typed refusal code when one exists (e.g. the fan-out law's
    ``ALLOCATION_POLICY_REQUIRED``); the code also appears in the message so callers matching
    on either see it."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code


class DrivingTimeRoleV1(StrEnum):
    """WHICH time drives the join: each driving row's own event time, or the one report cutoff."""

    DRIVING_EVENT_TIME = "driving_event_time"
    CUTOFF_PARAMETER = "cutoff_parameter"


class IntervalBoundaryPolicyV1(StrEnum):
    """Validity-interval inclusivity. ONE sanctioned member, spelled exactly as the existing
    ``AsOfIntervalRequirementV1`` identity payload spells it — explicit, never assumed, and never
    a fork of the half-open law the platform already executes."""

    CLOSED_OPEN = "[from,to)"


class UnmatchedRowMeaningV1(StrEnum):
    """What a driving row WITHOUT a joined match MEANS for the feature (logical, not mechanics)."""

    JOINED_ATTRIBUTES_NOT_APPLICABLE = "joined_attributes_not_applicable"
    EXCLUDE_DRIVING_ROW = "exclude_driving_row"
    REFUSE = "refuse"


class StaticLinkMeaningV1(StrEnum):
    """What a link with NO temporal validity means: declared timeless, or refused outright."""

    APPLIES_FOR_ALL_TIME = "applies_for_all_time"
    REFUSE = "refuse"


class TemporalEvaluationPurposeV1(StrEnum):
    """WHY values are being generated — the purpose flag R14's sixth mandatory case gates on."""

    TRAINING = "training"
    BACKTESTING = "backtesting"
    CURRENT_SCORING = "current_scoring"


#: Purposes that reconstruct the past, where ``latest_available`` knowledge is leakage.
_HISTORICAL_PURPOSES = frozenset({
    TemporalEvaluationPurposeV1.TRAINING,
    TemporalEvaluationPurposeV1.BACKTESTING,
})


def _enum(raw: object, vocab: type[Enum], *, what: str) -> Any:
    """Coerce an enum member or its string value; refuse ``None`` and unknown tokens.

    ``None`` is refused explicitly because R14's whole point is that the type never fabricates:
    an absent temporal decision must FAIL construction, not quietly become a default."""
    if isinstance(raw, vocab):
        return raw
    if isinstance(raw, str):
        try:
            return vocab(raw.strip().lower())
        except ValueError:
            pass
    raise ContractDefect(
        f"{what} {raw!r} is not one of {sorted(m.value for m in vocab)}; a missing or unknown "
        "value is a construction error — the contract never fabricates a policy")


def _non_empty(raw: object, *, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ContractDefect(f"{what} must be a non-empty string")
    return raw.strip()


def _column_ref(raw: object, *, what: str) -> str:
    """One normalized logical COLUMN ref (``catalog::schema.table.column``); never a table ref."""
    text = _non_empty(raw, what=what)
    try:
        source, schema, table, column = parse_ref(text.lower())
    except ValueError as exc:
        raise ContractDefect(f"{what} {raw!r} is not a normalized logical ref: {exc}") from exc
    if column is None:
        raise ContractDefect(f"{what} {raw!r} must address a COLUMN, not a table")
    return normalize_ref(source, schema, table, column)


_SCALAR_TYPES = (str, int, float, bool)


def _scalar(raw: object, *, what: str) -> object:
    if raw is not None and not isinstance(raw, _SCALAR_TYPES):
        raise ContractDefect(
            f"{what} must be a JSON scalar (str/int/float/bool) or an explicit None, got "
            f"{type(raw).__name__}")
    return raw


@dataclass(frozen=True, slots=True)
class LogicalTemporalJoinSemanticsV1:
    """R14: the temporal MEANING of one join segment — inside :func:`logical_digest`.

    Every field explicit, no defaults. ``effective_time_basis`` may never be
    ``latest_available``: "current state effective for a historical row" is exactly the
    latest-correction leakage the first journey forbids."""

    effective_time_basis: KnowledgeTimeBasisV2
    knowledge_time_basis: KnowledgeTimeBasisV2
    driving_time_role: DrivingTimeRoleV1
    interval_boundary_policy: IntervalBoundaryPolicyV1
    unmatched_row_meaning: UnmatchedRowMeaningV1
    static_link_meaning: StaticLinkMeaningV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_time_basis", _enum(
            self.effective_time_basis, KnowledgeTimeBasisV2, what="effective_time_basis"))
        if self.effective_time_basis is KnowledgeTimeBasisV2.LATEST_AVAILABLE:
            raise ContractDefect(
                "effective_time_basis may not be 'latest_available': reading current state as if "
                "it had been effective at a historical time is the latest-correction leakage R14 "
                "forbids; declare as_of_cutoff or event_time")
        object.__setattr__(self, "knowledge_time_basis", _enum(
            self.knowledge_time_basis, KnowledgeTimeBasisV2, what="knowledge_time_basis"))
        object.__setattr__(self, "driving_time_role", _enum(
            self.driving_time_role, DrivingTimeRoleV1, what="driving_time_role"))
        object.__setattr__(self, "interval_boundary_policy", _enum(
            self.interval_boundary_policy, IntervalBoundaryPolicyV1,
            what="interval_boundary_policy"))
        object.__setattr__(self, "unmatched_row_meaning", _enum(
            self.unmatched_row_meaning, UnmatchedRowMeaningV1, what="unmatched_row_meaning"))
        object.__setattr__(self, "static_link_meaning", _enum(
            self.static_link_meaning, StaticLinkMeaningV1, what="static_link_meaning"))

    def identity_payload(self) -> dict[str, str]:
        return {
            "contract": _SEMANTICS_CONTRACT,
            "effective_time_basis": self.effective_time_basis.value,
            "knowledge_time_basis": self.knowledge_time_basis.value,
            "driving_time_role": self.driving_time_role.value,
            "interval_boundary_policy": self.interval_boundary_policy.value,
            "unmatched_row_meaning": self.unmatched_row_meaning.value,
            "static_link_meaning": self.static_link_meaning.value,
        }


def validate_temporal_semantics_for_purpose(
    semantics: LogicalTemporalJoinSemanticsV1,
    purpose: TemporalEvaluationPurposeV1 | str,
) -> None:
    """R14's sixth mandatory case: ``latest_available`` knowledge is refused for purposes that
    reconstruct the past. Current scoring may read it — that IS its meaning, no leakage."""
    resolved = _enum(purpose, TemporalEvaluationPurposeV1, what="purpose")
    if (resolved in _HISTORICAL_PURPOSES
            and semantics.knowledge_time_basis is KnowledgeTimeBasisV2.LATEST_AVAILABLE):
        raise ContractDefect(
            f"knowledge_time_basis 'latest_available' is refused for purpose "
            f"{resolved.value!r}: it reads facts that became known after the cutoff, which the "
            f"model being evaluated could not have known",
            code=LATEST_AVAILABLE_REFUSED_FOR_PURPOSE)


@dataclass(frozen=True, slots=True)
class LogicalOperandBindingV1:
    """One typed operand: its role, the logical column it binds, and the governed semantic
    revision under which that column means what the operand needs it to mean."""

    role: str
    logical_column_ref: str
    governed_semantic_revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _non_empty(self.role, what="operand role"))
        object.__setattr__(self, "logical_column_ref", _column_ref(
            self.logical_column_ref, what=f"operand {self.role!r} logical_column_ref"))
        object.__setattr__(self, "governed_semantic_revision_id", _non_empty(
            self.governed_semantic_revision_id,
            what=f"operand {self.role!r} governed_semantic_revision_id"))

    def identity_payload(self) -> dict[str, str]:
        return {
            "role": self.role,
            "logical_column_ref": self.logical_column_ref,
            "governed_semantic_revision_id": self.governed_semantic_revision_id,
        }


@dataclass(frozen=True, slots=True)
class LogicalRelationshipSegmentV1:
    """One hop of the logical relationship path, with FULL ordered endpoint tuples.

    Endpoints pair positionally (composite keys), so BOTH tuples are ordered and must be the
    same length — reordering one endpoint is a different relationship, hence a different feature."""

    left_endpoint_refs: tuple[str, ...]
    right_endpoint_refs: tuple[str, ...]
    temporal_semantics: LogicalTemporalJoinSemanticsV1

    def __post_init__(self) -> None:
        for name in ("left_endpoint_refs", "right_endpoint_refs"):
            refs = tuple(_column_ref(r, what=f"{name} member") for r in getattr(self, name))
            if not refs:
                raise ContractDefect(f"{name} must name at least one column")
            if len(set(refs)) != len(refs):
                raise ContractDefect(f"{name} must be distinct")
            object.__setattr__(self, name, refs)
        if len(self.left_endpoint_refs) != len(self.right_endpoint_refs):
            raise ContractDefect(
                "endpoint tuples pair positionally and must have the same length; "
                f"got {len(self.left_endpoint_refs)} vs {len(self.right_endpoint_refs)}")
        if not isinstance(self.temporal_semantics, LogicalTemporalJoinSemanticsV1):
            raise ContractDefect(
                "temporal_semantics must be a LogicalTemporalJoinSemanticsV1 — a segment with no "
                "declared temporal meaning stays TEMPORAL_JOIN_POLICY_MISSING at the consuming "
                "layer; this type never fabricates one")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "left_endpoint_refs": list(self.left_endpoint_refs),
            "right_endpoint_refs": list(self.right_endpoint_refs),
            "temporal_semantics": self.temporal_semantics.identity_payload(),
        }


@dataclass(frozen=True, slots=True)
class LogicalPlanProvenanceV1:
    """The provenance SIDE-CAR: recorded, displayed — NEVER hashed.

    Everything R9 sends to provenance pins lives here. Fields default to empty because absence
    of provenance is not a decision; nothing here can change identity either way."""

    hypothesis_text: str = ""
    planning_request_hash: str = ""
    chooser_revision_id: str = ""
    menu_content_hash: str = ""
    display_text: str = ""


@dataclass(frozen=True, slots=True)
class LogicalFeaturePlanV2:
    """R9: the complete logical identity of one feature — meaning only.

    * ``operand_bindings`` are keyed by role (roles distinct; payload sorted by role).
    * ``output_grain_key_refs`` are ORDERED — grain order is identity.
    * ``selected_parameters`` are the SELECTED ``(name, value)`` pairs only — the menu they were
      chosen from is provenance. Named pairs: payload sorted by name, duplicate names refused.
    * ``relationship_path`` is the ORDERED path; each segment carries its R14 semantics.
    * ``provenance`` is the side-car and never reaches :func:`logical_digest`."""

    canonical_definition_content_hash: str
    canonical_definition_revision_id: str
    operation: str
    operand_bindings: tuple[LogicalOperandBindingV1, ...]
    output_grain_key_refs: tuple[str, ...]
    selected_parameters: tuple[tuple[str, Any], ...]
    relationship_path: tuple[LogicalRelationshipSegmentV1, ...]
    formula_policy_identities: tuple[tuple[str, str], ...]
    provenance: LogicalPlanProvenanceV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_definition_content_hash", _non_empty(
            self.canonical_definition_content_hash, what="canonical_definition_content_hash"))
        object.__setattr__(self, "canonical_definition_revision_id", _non_empty(
            self.canonical_definition_revision_id, what="canonical_definition_revision_id"))
        object.__setattr__(self, "operation", _non_empty(self.operation, what="operation"))

        bindings = tuple(self.operand_bindings)
        if not bindings:
            raise ContractDefect("a logical plan must bind at least one operand")
        for binding in bindings:
            if not isinstance(binding, LogicalOperandBindingV1):
                raise ContractDefect("operand_bindings must be LogicalOperandBindingV1 instances")
        roles = [b.role for b in bindings]
        if len(set(roles)) != len(roles):
            raise ContractDefect(f"operand roles must be distinct, got {roles}")
        object.__setattr__(self, "operand_bindings", bindings)

        grain = tuple(_column_ref(r, what="output_grain_key_ref") for r in
                      self.output_grain_key_refs)
        if not grain:
            raise ContractDefect("output_grain_key_refs must name at least one column")
        if len(set(grain)) != len(grain):
            raise ContractDefect("output_grain_key_refs must be distinct")
        object.__setattr__(self, "output_grain_key_refs", grain)

        parameters = tuple(
            (_non_empty(name, what="selected parameter name"),
             _scalar(value, what=f"selected parameter {name!r} value"))
            for name, value in self.selected_parameters)
        names = [name for name, _ in parameters]
        if len(set(names)) != len(names):
            raise ContractDefect(f"selected parameter names must be distinct, got {names}")
        object.__setattr__(self, "selected_parameters", parameters)

        path = tuple(self.relationship_path)
        for segment in path:
            if not isinstance(segment, LogicalRelationshipSegmentV1):
                raise ContractDefect(
                    "relationship_path must be LogicalRelationshipSegmentV1 instances")
        object.__setattr__(self, "relationship_path", path)

        policies = tuple(
            (_non_empty(role, what="formula policy role"),
             _non_empty(identity, what=f"formula policy {role!r} identity"))
            for role, identity in self.formula_policy_identities)
        policy_roles = [role for role, _ in policies]
        if len(set(policy_roles)) != len(policy_roles):
            raise ContractDefect(f"formula policy roles must be distinct, got {policy_roles}")
        object.__setattr__(self, "formula_policy_identities", policies)

        if not isinstance(self.provenance, LogicalPlanProvenanceV1):
            raise ContractDefect("provenance must be a LogicalPlanProvenanceV1 side-car")

    def content_payload(self) -> dict[str, Any]:
        """Canonical serialization — MEANING only; the provenance side-car never appears."""
        return {
            "contract": LOGICAL_PLAN_CONTRACT,
            "canonical_definition_content_hash": self.canonical_definition_content_hash,
            "canonical_definition_revision_id": self.canonical_definition_revision_id,
            "operation": self.operation,
            "operand_bindings": [
                b.identity_payload()
                for b in sorted(self.operand_bindings, key=lambda b: b.role)],
            "output_grain_key_refs": list(self.output_grain_key_refs),        # ORDER preserved
            "selected_parameters": [
                [name, value] for name, value in sorted(self.selected_parameters)],
            "relationship_path": [s.identity_payload() for s in self.relationship_path],
            "formula_policy_identities": [
                [role, identity]
                for role, identity in sorted(self.formula_policy_identities)],
        }


def logical_digest(plan: LogicalFeaturePlanV2) -> str:
    """Stage 1 of the identity chain: sha256 hex over the plan's canonical serialization."""
    if not isinstance(plan, LogicalFeaturePlanV2):
        raise ContractDefect("logical_digest takes a LogicalFeaturePlanV2")
    return materialize_hash(plan.content_payload())
