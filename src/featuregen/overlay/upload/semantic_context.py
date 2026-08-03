"""`SemanticContextBundleV1` — the one typed semantic context contract (semantic plan Task 1).

One immutable in-memory value contract over what the platform knows about a column, before and
after graph persistence — never a second truth store. Amended shapes come from the CONTROLLING
interface doc (`docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md`):

* **D1 (hashing).** `content_hash` is RFC 8785 JCS via :func:`materialize_hash` — no inline
  scheme. Excluded from the hash: wall-clock, job state, environment, physical bindings and
  projection timestamps (the bundle simply never carries them). Both builders serialize through
  the SAME canonicalizer (:func:`bundle_payload` / :func:`shared_identity_payload`).
* **D2 (authority).** Every evidence entry carries the REAL `(producer, strength, lifecycle)`
  triple. The flat label is a DERIVED display projection (:func:`display_authority_label`); it is
  never persisted, never hashed as the authority, and no consumer may branch on it.
* **D3 (relationships).** `RelationshipContextV1` is link + tuple-of-directional-realizations,
  mirroring `entity_map` and the two real readers (`available_identifier_links`,
  `load_current_bridge_realizations`). `availability` carries ONLY the two `LinkAvailability`
  words — availability never encodes safety. `production_eligible` is the PURE
  :func:`bridge_realization.eligible_for_production` predicate: it may label history, never a live
  capability — "executable NOW" requires the revalidating reader
  (`bridge_store.executable_bridge_realizations`).
* **D4 (observations).** `ObservationContextV1` mirrors `RelationshipObservationV2` field-for-
  field: both directional maxima, `method` x `row_coverage` as two axes, side-preserved binding
  revisions, no invented lifecycle/expiry/kind fields. The uniqueness asymmetry ("a sample may
  disprove uniqueness but never establish it") is READ from the store's own verdict, never
  re-derived here.
* **D5 (ownership).** This module owns `RelationshipKind` and the closed vocabularies
  `MISSING_CONTEXT_CODES` / `REASON_CODES` / `UNRESOLVED_REASONS` — defined before first emission
  because they are hash-load-bearing from day one.
* **D11 (read scope).** BOTH builders take `roles` and filter every neighbour/link/table read
  through the `visible_requires <@ allowed` scope. The bundle inherits nothing from the un-scoped
  scalar readers; a hidden column enters no roster, no relationship endpoint, no adapter payload.

**Batching (review C Task 1, the 157-scan defect class).** `bundle_from_store` never loops scalar
readers: one graph query family (anchor/cohort/table row), one `field_evidence` bulk read, one
`field_decision_event` bulk read, and ONE `check_projection_readiness` gate for the whole bundle.
Its query count is column-count- and field-count-independent (pinned by test).

**Purpose adapters.** `for_concept_enrichment` / `for_critic` / `for_summary` /
`for_feature_generation` / `for_analysis_planning` are bounded plain-dict projections. They are
NOT wired into `enrich_llm` dispatch here (that is Task 4); every key they emit must be classified
in the relevant egress allowlist before it may egress (D10) — key names reuse the existing
allowlist names wherever a matching key exists.

**DELIBERATE DEFERRALS (declared so nobody reads them as oversights).** Task 1 freezes the
contract; three of its surfaces are intentionally unconsumed at this commit:

* `structured_results` — the bundle carries no adjudication/critic result projection. Task 5 owns
  the structured-result subject/current pointer (migration 1046) and wires it in.
* Observation-store READS — this module never queries an observation store. `ObservationContextV1`
  is projected only from observations the CALLER supplies, together with the caller-supplied
  currentness pointer (see `bundle_from_store`); the store itself, and therefore
  `relationship_observation_current`, arrives with the Hive/ODS slice. Until then
  `observation_context` is empty and `observation_context_absent` is the honest code.
* `REASON_CODES` — frozen NOW because D5 makes it hash-load-bearing from day one, but it has no
  emitter in this module. Its consumers (adjudication + the critic surfacing) land in Task 5.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass, replace
from dataclasses import fields as dataclass_fields
from datetime import datetime
from enum import Enum, StrEnum

from featuregen.contracts import DbConn
from featuregen.data_agent.relationship_observation import RelationshipObservationV2
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.evidence import AssertionStrength, EvidenceLifecycle, EvidenceProducer
from featuregen.overlay.upload.bridge_assessment import (
    AvailableIdentifierLinkV1,
    IdentifierEndpointV1,
    LinkAvailability,
    LinkReviewStatus,
    available_identifier_links,
)
from featuregen.overlay.upload.bridge_realization import (
    BridgeJoinRealizationRevisionV1,
    BridgeRealizationCurrentV1,
    eligible_for_production,
    eligible_for_sandbox,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    load_current_bridge_realizations,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY, concept_path, display_entity
from featuregen.overlay.upload.field_resolution import _RETIRED_EVENTS
from featuregen.overlay.upload.glossary_reader import GlossaryRecord
from featuregen.overlay.upload.identifier_scope import resolve_identifier_issuer
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.party_vocab import normalize_party_role
from featuregen.overlay.upload.read_scope import allowed_classes
from featuregen.overlay.upload.source_profile import (
    FTR_GLOSSARY_PROFILE,
    TECHNICAL_CSV_PROFILE,
    strength_for,
)

CONTRACT_VERSION = 1

#: Bounded neighbour roster carried on the bundle itself (mirrors the Pass-B egress cap scale).
NEIGHBOUR_LIMIT = 64
#: Bounded list length inside every purpose-adapter payload.
ADAPTER_LIST_LIMIT = 40


class SemanticContextError(ValueError):
    """A semantic-context contract violation (a lying projection, a mismatched observation)."""


# ── closed vocabularies (D5 — hash-load-bearing, defined before first emission) ─────────────────

#: Why a piece of context is ABSENT from a bundle. Closed: arbitrary free-text reasons are
#: forbidden; both the semantic and the profile plan consume these codes.
#:
#: READ `*_absent` AS "NOT IN THIS BUNDLE", NEVER AS A DATA-QUALITY VERDICT. Both builders are
#: read-scoped (D11): a table, link or neighbour the CALLER may not see is filtered out before
#: `_missing_codes` runs, so the same column yields `relationship_context_absent` for a narrowly
#: scoped reader and no code at all for a broadly scoped one. The bundle is fail-closed by
#: design — it must not leak "something exists here that you cannot see" — so absence under
#: scope and absence in the platform are DELIBERATELY indistinguishable at this surface.
#: Consequently no consumer may present these codes as a gap in the data, raise them as a
#: quality finding, or use them to compute coverage/completeness across callers.
#: `*_unresolved` / `*_missing` are different: those describe the anchor column itself, which the
#: caller can see by construction (the builder raises `KeyError` otherwise).
MISSING_CONTEXT_CODES: frozenset[str] = frozenset({
    "concept_unclassified",          # no registry concept selected -> concept_path == ()
    "definition_missing",
    "domain_missing",
    "glossary_sidecar_absent",       # no curated business-glossary sidecar for this column
    "identifier_namespace_unresolved",
    "issuer_scope_unresolved",       # scheme known, issuing scope not configured (Task 2 wires it)
    "party_role_unresolved",
    "entity_unresolved",
    "currency_missing",              # monetary concept with no currency fact
    "unit_missing",
    "table_context_absent",
    "dataset_profile_absent",        # profile Release-A identity absent (flag off / not assembled)
    "catalog_profile_absent",
    "relationship_context_absent",
    "observation_context_absent",    # empty until the Hive/ODS slice supplies observations
    "realization_scope_missing",     # a directional realization without an applicability scope
    "neighbour_roster_truncated",
})

#: Closed adjudication/critic reason codes (consumed from Task 5 on; frozen NOW per D5).
REASON_CODES: frozenset[str] = frozenset({
    "deterministic_shape_conflict",
    "source_llm_conflict",
    "critic_refuted",
    "critic_uncertain",
    "ambiguous_alternatives",
    "insufficient_context",
    "name_only_signal",
    "registry_gap_suspected",
    "namespace_mismatch",
    "issuer_unresolved",
})

#: The three product families the UI renders — never a failure-shaped free string.
UNRESOLVED_REASON_FAMILIES: frozenset[str] = frozenset(
    {"undecided", "needs_data_check", "structurally_unsuitable"})

#: The closed unresolved-reason members — ONE spelling each, no aliases. Named here so consumers
#: reference a symbol rather than re-typing a string; the family-free suffix is WIRE-VISIBLE
#: through :func:`unresolved_label` (the Release-A profile surface publishes reason and family as
#: two columns), so a suffix rename is a wire change, not a refactor.
UNRESOLVED_NO_EVIDENCE = "undecided:no_evidence"
UNRESOLVED_PENDING_REVIEW = "undecided:pending_review"
UNRESOLVED_AUTHORITY_INSUFFICIENT = "undecided:authority_insufficient"
UNRESOLVED_CONFLICT = "needs_data_check:conflict"
UNRESOLVED_HASH_MISMATCH = "needs_data_check:hash_mismatch"
UNRESOLVED_PROJECTION_UNAVAILABLE = "needs_data_check:projection_unavailable"
UNRESOLVED_FORKED_DECISION_HEAD = "needs_data_check:forked_decision_head"
UNRESOLVED_PENDING_REVALIDATION = "needs_data_check:pending_revalidation"
UNRESOLVED_FIELD_NOT_APPLICABLE = "structurally_unsuitable:field_not_applicable"
UNRESOLVED_RETIRED = "structurally_unsuitable:retired"

#: Closed unresolved reasons. Every member is family-prefixed and maps to exactly one family
#: (validated at import). "No evidence at all" is `undecided:no_evidence`, DISTINCT from
#: `influence_not_operational` — which display fields report as their NORMAL state, never as
#: unresolved (D5).
#:
#: The family is written out rather than derived from the prefix ON PURPOSE: the import-time
#: validator then actually checks something (a member whose prefix and declared family disagree
#: trips it), instead of restating a split.
UNRESOLVED_REASONS: dict[str, str] = {
    UNRESOLVED_NO_EVIDENCE: "undecided",
    # An UNREVIEWED top-strength tie, or evidence below every display rule: nobody with authority
    # has decided anything yet. Never a failure-shaped conflict — that word is reserved for
    # contradictions between load-bearing-capable assertions.
    UNRESOLVED_PENDING_REVIEW: "undecided",
    # Said, but below the operational bar. Still `undecided`: an unmet authority bar is a decision
    # nobody has taken, not a defect in the data.
    UNRESOLVED_AUTHORITY_INSUFFICIENT: "undecided",
    UNRESOLVED_CONFLICT: "needs_data_check",
    UNRESOLVED_HASH_MISMATCH: "needs_data_check",
    UNRESOLVED_PROJECTION_UNAVAILABLE: "needs_data_check",
    UNRESOLVED_FORKED_DECISION_HEAD: "needs_data_check",
    # Material changed under a human confirmation; awaiting a re-check.
    UNRESOLVED_PENDING_REVALIDATION: "needs_data_check",
    UNRESOLVED_FIELD_NOT_APPLICABLE: "structurally_unsuitable",
    UNRESOLVED_RETIRED: "structurally_unsuitable",
}

#: The closed `SemanticValueV1.resolution_status` vocabulary: a source-declared value, a current
#: resolved value, or one of the closed unresolved reasons.
RESOLUTION_STATUSES: frozenset[str] = frozenset({"declared", "current"}) | frozenset(
    UNRESOLVED_REASONS)

#: The closed `IdentifierNamespaceV1.basis` vocabulary.
NAMESPACE_BASES: frozenset[str] = frozenset({"catalog_scope", "global_scheme", "unresolved"})


def _validate_vocabularies() -> None:
    for member, family in UNRESOLVED_REASONS.items():
        if family not in UNRESOLVED_REASON_FAMILIES:
            raise ValueError(f"unresolved reason {member!r} maps to unknown family {family!r}")
        if member.split(":", 1)[0] != family:
            raise ValueError(f"unresolved reason {member!r} is not prefixed by its family")
    if not MISSING_CONTEXT_CODES or not REASON_CODES:
        raise ValueError("the closed context vocabularies must not be empty")


_validate_vocabularies()


def unresolved_family(reason: str) -> str:
    """The product family of one closed unresolved reason.

    Raises ``KeyError`` on an unknown member: the vocabulary is CLOSED, so an unmapped reason is a
    programming error, not a display case the UI should be asked to render."""
    return UNRESOLVED_REASONS[reason]


def unresolved_label(reason: str) -> str:
    """The member's family-free label — ``undecided:no_evidence`` -> ``no_evidence``.

    For consumers that publish the family in its own column (the Release-A dataset-profile
    surface): the pair ``(unresolved_label, unresolved_family)`` IS the member, split in two. It is
    a projection of the one canonical spelling, never a second vocabulary."""
    unresolved_family(reason)   # closed-vocabulary check before we hand out a substring
    return reason.split(":", 1)[1]


# ── D2: the derived display label (display only — never persisted, never authority) ─────────────

def display_authority_label(
    producer: str, strength: str, *, operational_influence: str | None = None
) -> str:
    """The DERIVED flat display label for one evidence triple — the fixed D2 projection.

    Display only: it is never persisted, never hashed as the authority, and no consumer may branch
    on it. The `governed` row comes from the separate `OperationalColumnFacts.authority`
    (`operational_influence="governed"`), never from a producer identity. Combinations outside the
    D2 table collapse to the `system` family (the parser/taxonomy/legacy catch-all)."""
    if operational_influence == "governed":
        return "governed"
    if producer == EvidenceProducer.SOURCE.value:
        return ("source_attested"
                if strength in (AssertionStrength.ATTESTED.value,
                                AssertionStrength.CONFIRMED.value)
                else "source_proposed")
    if producer == EvidenceProducer.HUMAN.value and strength == AssertionStrength.CONFIRMED.value:
        return "human"
    if producer == EvidenceProducer.LLM.value:
        return "llm_proposed"
    if (producer in (EvidenceProducer.PROFILER.value, EvidenceProducer.STRUCTURAL_CONNECTOR.value)
            and strength == AssertionStrength.ATTESTED.value):
        return "deterministic"
    return "system"


# ── value contracts ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EvidenceAuthorityV1:
    """One evidence record's REAL authority triple (D2) plus its audit identity."""

    producer: str
    strength: str
    lifecycle: str
    producer_ref: str | None = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        EvidenceProducer(self.producer)
        AssertionStrength(self.strength)
        EvidenceLifecycle(self.lifecycle)


@dataclass(frozen=True, slots=True)
class SemanticValueV1:
    """One semantic field's value + full authority attribution.

    `operational_influence` is the separate `governed | hint` operational-facts axis — READ from
    the shipped authority readers, never inferred from a producer; `None` for fields outside the
    operational-facts surface."""

    field_name: str
    value: object | None
    evidence: tuple[EvidenceAuthorityV1, ...]
    resolution_status: str
    operational_influence: str | None = None

    def __post_init__(self) -> None:
        if self.resolution_status not in RESOLUTION_STATUSES:
            raise SemanticContextError(
                f"resolution_status {self.resolution_status!r} is not in the closed vocabulary")
        if self.operational_influence not in (None, "governed", "hint"):
            raise SemanticContextError(
                f"operational_influence {self.operational_influence!r} is not governed|hint|None")

    def display_label(self) -> str:
        """The derived D2 display label of the LEADING evidence entry (display only)."""
        if not self.evidence:
            return "system"
        lead = self.evidence[0]
        return display_authority_label(
            lead.producer, lead.strength, operational_influence=self.operational_influence)


@dataclass(frozen=True, slots=True)
class IdentifierNamespaceV1:
    """The identifier value space: scheme (from `Concept.namespace`) + issuing scope.

    A missing issuer NEVER appears as verified: `issuer_scope=None` forces `basis="unresolved"`
    (validated), and scheme equality alone is never equality proof (functional rule 5)."""

    scheme: str
    issuer_scope: str | None
    basis: str

    def __post_init__(self) -> None:
        if self.basis not in NAMESPACE_BASES:
            raise SemanticContextError(f"namespace basis {self.basis!r} is not in {sorted(NAMESPACE_BASES)}")
        if self.issuer_scope is None and self.basis != "unresolved":
            raise SemanticContextError("a namespace without an issuer_scope must be 'unresolved'")
        if self.issuer_scope is not None and self.basis == "unresolved":
            raise SemanticContextError("a resolved issuer_scope cannot carry basis 'unresolved'")


@dataclass(frozen=True, slots=True)
class NeighbourColumnV1:
    """One same-table neighbour, identity-only. `object_ref` is the SCHEMA-PRESERVING logical
    ref (never the public-flattened graph ref — the builder byte-identity caveat)."""

    object_ref: str
    column_name: str
    concept: str | None
    party_role: str | None


class RelationshipKind(StrEnum):
    """Owned HERE per D5; the profile plan and Release C import it."""

    DIRECT_EQUALITY = "direct_equality"
    CROSSWALK = "crosswalk"
    TRANSFORMED = "transformed"
    SEMANTIC_ONLY = "semantic_only"


@dataclass(frozen=True, slots=True)
class DirectionalRealizationContextV1:
    """One CURRENT directional realization (D3 verbatim).

    `production_eligible`/`sandbox_eligible` are the PURE `bridge_realization` predicates — they
    label history, never a live capability ("executable NOW" requires the revalidating
    `bridge_store.executable_bridge_realizations` reader). Construction refuses a LYING
    projection: eligibility claimed for a non-active lifecycle or an unvalidated/unsafe
    safety_status cannot exist as a value."""

    realization_revision_id: str
    from_ref: str
    to_ref: str
    lifecycle: str
    safety_status: str
    cardinality: str | None
    scope_id: str | None            # RealizationApplicabilityScopeV1.scope_id — no invented hash
    sandbox_eligible: bool
    production_eligible: bool

    def __post_init__(self) -> None:
        if self.production_eligible and not self.sandbox_eligible:
            raise SemanticContextError("production eligibility implies sandbox eligibility")
        if self.production_eligible and self.safety_status != "deterministically_validated":
            raise SemanticContextError(
                "production_eligible requires deterministic validation — review or a stale "
                "projection can never substitute (D3)")
        if self.sandbox_eligible and self.lifecycle != "active":
            raise SemanticContextError(
                f"a {self.lifecycle!r} realization cannot be projected as eligible")
        if self.sandbox_eligible and self.safety_status == "unsafe":
            raise SemanticContextError("an unsafe realization cannot be projected as eligible")


@dataclass(frozen=True, slots=True)
class RelationshipContextV1:
    """One link + its 0..N current directional realizations (D3 verbatim).

    `availability` carries ONLY the `LinkAvailability` values — availability never encodes
    safety. `relationship_ref` is the bridge fact_key today; Release C extends this contract
    additively with a `crosswalk` field for crosswalk definitions."""

    relationship_ref: str
    kind: str
    left_ref: str
    right_ref: str
    availability: str
    review_status: str | None
    assessment_revision_id: str | None   # = candidate_revision_id
    realizations: tuple[DirectionalRealizationContextV1, ...]
    producer: str
    strength: str
    lifecycle: str
    current: bool
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.availability not in {a.value for a in LinkAvailability}:
            raise SemanticContextError(
                f"availability {self.availability!r} is not a LinkAvailability value — the "
                "four-way availability/safety merge is deleted (D3)")
        if self.kind not in {k.value for k in RelationshipKind}:
            raise SemanticContextError(f"unknown relationship kind {self.kind!r}")
        EvidenceProducer(self.producer)
        AssertionStrength(self.strength)
        EvidenceLifecycle(self.lifecycle)


@dataclass(frozen=True, slots=True)
class ObservationEndpointContextV1:
    """Faithful projection of `EndpointTupleObservationV2` — sides preserved, store names."""

    physical_id: str
    binding_revision_id: str
    binding_content_hash: str
    columns: tuple[str, ...]
    row_count: int
    non_null_row_count: int
    distinct_tuple_count: int
    duplicate_tuple_count: int
    duplicate_row_count: int
    max_rows_per_tuple: int
    partitions_read: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservationContextV1:
    """Faithful projection of `RelationshipObservationV2` (D4) plus current-pointer identity.

    BOTH directional maxima are kept (no single `direction` field); `method` x `row_coverage`
    stay two axes; the side-specific binding revisions ride on `left`/`right` exactly as stored.
    `left_uniqueness`/`right_uniqueness` are READ from the store's own asymmetric verdict
    (`uniqueness_verdict`) at projection time — never re-derived downstream: a sampled/approximate
    observation can DISPROVE uniqueness but never establish it. `current` comes from the
    `relationship_observation_current` pointer."""

    observation_revision_id: str
    realization_revision_id: str
    plan_hash: str
    scope_id: str
    left: ObservationEndpointContextV1
    right: ObservationEndpointContextV1
    matched_left_distinct: int
    unmatched_left_distinct: int
    matched_right_distinct: int
    unmatched_right_distinct: int
    left_orphan_rows: int
    right_orphan_rows: int
    joined_row_count: int
    max_right_matches_per_left_row: int
    max_left_matches_per_right_row: int
    normalization_ids: tuple[str, ...]
    predicate_ids: tuple[str, ...]
    left_source_snapshot_id: str
    right_source_snapshot_id: str
    snapshot_or_as_of: str | None
    execution_principal: str
    method: str
    row_coverage: str
    complete: bool
    observed_at: str
    failures: tuple[str, ...]
    producer: str
    strength: str
    current: bool
    left_uniqueness: str
    right_uniqueness: str


def _endpoint_observation_context(endpoint) -> ObservationEndpointContextV1:
    return ObservationEndpointContextV1(
        physical_id=endpoint.physical_id,
        binding_revision_id=endpoint.binding_revision_id,
        binding_content_hash=endpoint.binding_content_hash,
        columns=tuple(endpoint.columns),
        row_count=endpoint.row_count,
        non_null_row_count=endpoint.non_null_row_count,
        distinct_tuple_count=endpoint.distinct_tuple_count,
        duplicate_tuple_count=endpoint.duplicate_tuple_count,
        duplicate_row_count=endpoint.duplicate_row_count,
        max_rows_per_tuple=endpoint.max_rows_per_tuple,
        partitions_read=tuple(endpoint.partitions_read),
    )


def observation_context_from(
    observation: RelationshipObservationV2,
    *,
    realization: DirectionalRealizationContextV1,
    current: bool,
) -> ObservationContextV1:
    """Project one stored observation onto the realization it is being attached to.

    An observation is applicable ONLY to its exact realization revision (whose content identity
    pins both endpoint binding revisions, column pairs and predicates) and applicability scope —
    a mismatch REFUSES rather than projecting stale/wrong-direction evidence as current context."""
    if observation.realization_revision_id != realization.realization_revision_id:
        raise SemanticContextError(
            "observation belongs to realization revision "
            f"{observation.realization_revision_id!r}, not "
            f"{realization.realization_revision_id!r} — an observation is never reusable across "
            "realizations/bindings")
    if realization.scope_id is None:
        raise SemanticContextError(
            "a realization without an applicability scope cannot carry observations")
    if observation.scope_id != realization.scope_id:
        raise SemanticContextError(
            f"observation scope {observation.scope_id!r} does not match realization scope "
            f"{realization.scope_id!r}")
    return ObservationContextV1(
        observation_revision_id=observation.observation_revision_id,
        realization_revision_id=observation.realization_revision_id,
        plan_hash=observation.plan_hash,
        scope_id=observation.scope_id,
        left=_endpoint_observation_context(observation.left),
        right=_endpoint_observation_context(observation.right),
        matched_left_distinct=observation.matched_left_distinct,
        unmatched_left_distinct=observation.unmatched_left_distinct,
        matched_right_distinct=observation.matched_right_distinct,
        unmatched_right_distinct=observation.unmatched_right_distinct,
        left_orphan_rows=observation.left_orphan_rows,
        right_orphan_rows=observation.right_orphan_rows,
        joined_row_count=observation.joined_row_count,
        max_right_matches_per_left_row=observation.max_right_matches_per_left_row,
        max_left_matches_per_right_row=observation.max_left_matches_per_right_row,
        normalization_ids=tuple(observation.normalization_ids),
        predicate_ids=tuple(observation.predicate_ids),
        left_source_snapshot_id=observation.left_source_snapshot_id,
        right_source_snapshot_id=observation.right_source_snapshot_id,
        snapshot_or_as_of=observation.snapshot_or_as_of,
        execution_principal=observation.execution_principal,
        method=str(observation.method),
        row_coverage=(observation.row_coverage.value
                      if isinstance(observation.row_coverage, Enum)
                      else str(observation.row_coverage)),
        complete=observation.complete,
        observed_at=observation.observed_at.isoformat(),
        failures=tuple(observation.failures),
        producer=observation.producer,
        strength=observation.strength,
        current=current,
        left_uniqueness=observation.uniqueness_verdict("left"),
        right_uniqueness=observation.uniqueness_verdict("right"),
    )


# ── the bundle ──────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SemanticContextBundleV1:
    contract_version: int
    catalog_source: str
    object_ref: str                 # schema-preserving logical ref of the column
    table_ref: str                  # schema-preserving logical ref of its table
    source_semantics: tuple[SemanticValueV1, ...]
    resolved_semantics: tuple[SemanticValueV1, ...]
    concept_path: tuple[str, ...]   # selected concept followed by its is_a ancestors
    identifier_namespace: IdentifierNamespaceV1 | None
    table_context: tuple[SemanticValueV1, ...]
    catalog_profile_revision_id: str | None
    dataset_profile_hash: str | None
    neighbouring_columns: tuple[NeighbourColumnV1, ...]
    relationship_context: tuple[RelationshipContextV1, ...]
    observation_context: tuple[ObservationContextV1, ...]  # empty until the Hive/ODS slice
    missing_context: tuple[str, ...]
    content_hash: str = ""


def _plain(value):
    """Canonical JSON-able projection: dataclasses -> dicts, tuples -> lists, enums -> values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclass_fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def bundle_payload(bundle: SemanticContextBundleV1) -> dict:
    """The canonical hash payload (D1): every field except `content_hash` itself. The bundle
    carries no wall-clock, job state, environment or physical-binding fields, so the exclusions
    hold by construction."""
    payload = _plain(bundle)
    payload.pop("content_hash", None)
    return payload


def _finish(**fields_) -> SemanticContextBundleV1:
    unhashed = SemanticContextBundleV1(**fields_)
    return replace(unhashed, content_hash=materialize_hash(bundle_payload(unhashed)))


def shared_identity_payload(bundle: SemanticContextBundleV1) -> dict:
    """The builder byte-identity projection (D1): the facts BOTH builders can know from the same
    upload material, keyed by schema-preserving logical refs. Evidence is reduced to its
    (producer, strength, lifecycle) triple — audit ids exist only after persistence. Derived
    fields whose inputs legitimately differ between the builders (resolution, links, missing
    codes, profile identity) are deliberately outside this projection."""
    return {
        "contract_version": bundle.contract_version,
        "catalog_source": bundle.catalog_source,
        "object_ref": bundle.object_ref,
        "table_ref": bundle.table_ref,
        "source_semantics": [
            {
                "field_name": v.field_name,
                "value": _plain(v.value),
                "resolution_status": v.resolution_status,
                "evidence": [
                    {"producer": e.producer, "strength": e.strength, "lifecycle": e.lifecycle}
                    for e in v.evidence
                ],
            }
            for v in bundle.source_semantics
        ],
        "neighbouring_columns": [
            {"object_ref": n.object_ref, "column_name": n.column_name,
             "party_role": n.party_role}
            for n in bundle.neighbouring_columns
        ],
    }


# ── shared link + realization composition (D3 — extracted from entity_map._link_view) ───────────

@dataclass(frozen=True, slots=True)
class ComposedLinkRealizationV1:
    """One current directional realization joined to its eligibility, read via the shipped
    readers. The ONE composition both the Entity Map and the bundle render — a second fold of
    lifecycle/eligibility is the banned defect class."""

    revision: BridgeJoinRealizationRevisionV1
    current: BridgeRealizationCurrentV1
    dependencies: tuple[BridgeDependencyRefV1, ...]
    sandbox_eligible: bool
    production_eligible: bool


def composed_link_realizations(
    conn: DbConn, bridge_fact_key: str
) -> tuple[ComposedLinkRealizationV1, ...]:
    """Load the CURRENT directional realizations of one link with their pure eligibility verdicts
    (`eligible_for_sandbox` / `eligible_for_production` — the same answers every other consumer
    gets). Pure predicates: they label the stored state, never a live capability."""
    return tuple(
        ComposedLinkRealizationV1(
            revision=stored.revision,
            current=stored.current,
            dependencies=stored.dependencies,
            sandbox_eligible=eligible_for_sandbox(stored.revision, stored.current),
            production_eligible=eligible_for_production(stored.revision, stored.current),
        )
        for stored in load_current_bridge_realizations(conn, bridge_fact_key=bridge_fact_key)
    )


def _endpoint_ref(endpoint: IdentifierEndpointV1) -> str:
    """One deterministic display ref per endpoint: the single member's column ref, else (for a
    composite tuple key) the endpoint's table ref."""
    if len(endpoint.members) == 1:
        return endpoint.members[0].logical_column_ref
    return endpoint.logical_table_ref


def _directional_context(composed: ComposedLinkRealizationV1) -> DirectionalRealizationContextV1:
    revision, current = composed.revision, composed.current
    return DirectionalRealizationContextV1(
        realization_revision_id=revision.realization_revision_id,
        from_ref=revision.from_endpoint.logical_table_ref,
        to_ref=revision.to_endpoint.logical_table_ref,
        lifecycle=current.lifecycle.value,
        safety_status=current.safety_status.value,
        cardinality=(revision.cardinality.value.value if revision.cardinality.known else None),
        scope_id=revision.applicability_scope.scope_id,
        sandbox_eligible=composed.sandbox_eligible,
        production_eligible=composed.production_eligible,
    )


def relationship_context_from_link(
    conn: DbConn, link: AvailableIdentifierLinkV1
) -> RelationshipContextV1:
    """Project one AVAILABLE link (the shipped reader's truth, verbatim) into the D3 shape.

    The authority triple is the derivation's honest axis: bridge candidates are derived from the
    concept registry's identifier namespaces, so `producer=taxonomy`; a human-verified review
    raises strength to CONFIRMED, otherwise the link stays a usable PROPOSED output (never a
    failure state). `current=True` by construction — only current assessments are available."""
    assessment, availability = link.assessment, link.availability
    confirmed = availability.review_status is LinkReviewStatus.HUMAN_VERIFIED
    return RelationshipContextV1(
        relationship_ref=availability.bridge_fact_key,
        kind=RelationshipKind.DIRECT_EQUALITY.value,
        left_ref=_endpoint_ref(assessment.left_endpoint),
        right_ref=_endpoint_ref(assessment.right_endpoint),
        availability=availability.availability.value,
        review_status=availability.review_status.value,
        assessment_revision_id=assessment.candidate_revision_id,
        realizations=tuple(
            _directional_context(composed)
            for composed in composed_link_realizations(conn, availability.bridge_fact_key)
        ),
        producer=EvidenceProducer.TAXONOMY.value,
        strength=(AssertionStrength.CONFIRMED.value if confirmed
                  else AssertionStrength.PROPOSED.value),
        lifecycle=EvidenceLifecycle.ACTIVE.value,
        current=True,
        evidence_ids=tuple(ref.evidence_id for ref in assessment.evidence_refs),
    )


# ── missing-context derivation (shared by both builders) ────────────────────────────────────────

def _missing_codes(
    *,
    concept_name: str | None,
    concept_path_t: tuple[str, ...],
    resolved: Mapping[str, SemanticValueV1],
    source: Mapping[str, SemanticValueV1],
    identifier_namespace: IdentifierNamespaceV1 | None,
    glossary_present: bool,
    table_context: tuple[SemanticValueV1, ...],
    relationship_context: tuple[RelationshipContextV1, ...],
    observation_context: tuple[ObservationContextV1, ...],
    catalog_profile_revision_id: str | None,
    dataset_profile_hash: str | None,
    party_role: str | None,
    neighbours_truncated: bool,
) -> tuple[str, ...]:
    codes: set[str] = set()
    registered = CONCEPT_REGISTRY.get(concept_name) if concept_name else None
    if not concept_path_t:
        codes.add("concept_unclassified")
    if not _has_value(resolved, source, "definition"):
        codes.add("definition_missing")
    if not _has_value(resolved, source, "domain"):
        codes.add("domain_missing")
    if not glossary_present:
        codes.add("glossary_sidecar_absent")
    if registered is not None and registered.group == "identifier":
        if identifier_namespace is None:
            codes.add("identifier_namespace_unresolved")
        elif identifier_namespace.issuer_scope is None:
            codes.add("issuer_scope_unresolved")
        if party_role is None:
            codes.add("party_role_unresolved")
        if not _has_value(resolved, source, "entity"):
            codes.add("entity_unresolved")
    if registered is not None and registered.group == "monetary":
        if not _has_value(resolved, source, "currency"):
            codes.add("currency_missing")
        if not _has_value(resolved, source, "unit"):
            codes.add("unit_missing")
    if not table_context:
        codes.add("table_context_absent")
    if not relationship_context:
        codes.add("relationship_context_absent")
    if not observation_context:
        codes.add("observation_context_absent")
    if catalog_profile_revision_id is None:
        codes.add("catalog_profile_absent")
    if dataset_profile_hash is None:
        codes.add("dataset_profile_absent")
    for link in relationship_context:
        for realized in link.realizations:
            if realized.scope_id is None:
                codes.add("realization_scope_missing")
    if neighbours_truncated:
        codes.add("neighbour_roster_truncated")
    illegal = codes - MISSING_CONTEXT_CODES
    if illegal:  # structurally impossible; guards vocabulary drift
        raise SemanticContextError(f"missing-context codes outside the closed set: {sorted(illegal)}")
    return tuple(sorted(codes))


def _has_value(resolved: Mapping[str, SemanticValueV1], source: Mapping[str, SemanticValueV1],
               field_name: str) -> bool:
    for values in (resolved, source):
        got = values.get(field_name)
        if got is not None and got.value not in (None, ""):
            return True
    return False


def _identifier_namespace(
    concept_name: str | None, *, issuer_scope: str | None = None, basis: str | None = None
) -> IdentifierNamespaceV1 | None:
    """`IdentifierNamespaceV1` from the concept registry's scheme. The ISSUER axis is wired by
    Task 2 (catalog semantic scope via `identifier_scope.py`); until an issuer is supplied the
    namespace is honest `unresolved` — never a claimed equality basis."""
    registered = CONCEPT_REGISTRY.get(concept_name) if concept_name else None
    if registered is None or registered.group != "identifier" or not registered.namespace:
        return None
    if issuer_scope is None:
        return IdentifierNamespaceV1(scheme=registered.namespace, issuer_scope=None,
                                     basis="unresolved")
    return IdentifierNamespaceV1(scheme=registered.namespace, issuer_scope=issuer_scope,
                                 basis=basis or "catalog_scope")


# ── bundle_from_upload ──────────────────────────────────────────────────────────────────────────

#: The glossary source-semantics fields, mirroring `ingest._write_glossary_source_evidence` —
#: same names, same joined `related_terms` rendering, same per-field profile strengths.
_GLOSSARY_SOURCE_FIELDS = ("definition", "domain", "business_term", "bian_path", "fibo_path",
                           "term_type", "process_path", "related_terms", "physical_fqn")
#: The technical source-semantics fields, mirroring `ingest._write_technical_source_evidence`.
_TECHNICAL_SOURCE_FIELDS = ("definition", "sensitivity", "additivity", "unit", "currency",
                            "entity")


def _tag_visible(sensitivity: str, allowed: set[str]) -> bool:
    """Upload-time read scope: pre-graph the only visibility axis is the DECLARED sensitivity tag
    (`visible_requires` is derived from it by migration 1032 once graphed). An unrecognized tag
    fails closed — hidden, exactly like the enforcement column's precedence."""
    tag = (sensitivity or "").strip().lower()
    if not tag:
        return True
    return tag in allowed


def _source_value(field_name: str, value: str, profile) -> SemanticValueV1:
    return SemanticValueV1(
        field_name=field_name,
        value=value,
        evidence=(EvidenceAuthorityV1(
            producer=EvidenceProducer.SOURCE.value,
            strength=strength_for(profile, field_name).value,
            lifecycle=EvidenceLifecycle.ACTIVE.value,
        ),),
        resolution_status="declared",
    )


def bundle_from_upload(
    row: CanonicalRow,
    *,
    glossary_record: GlossaryRecord | None = None,
    cohort: Sequence[CanonicalRow] = (),
    roles: Iterable[str] = (),
    relationship_context: Sequence[RelationshipContextV1] = (),
    observation_context: Sequence[ObservationContextV1] = (),
    catalog_profile_revision_id: str | None = None,
    dataset_profile_hash: str | None = None,
    table_context: Sequence[SemanticValueV1] = (),
) -> SemanticContextBundleV1:
    """Build the bundle from upload material only — `CanonicalRow` + glossary sidecar + the same
    table's cohort. NO graph queries: this runs before `build_graph` exists. Relationship and
    observation context are absent unless a pre-existing READ-SCOPED store value is explicitly
    supplied by the caller.

    Read scope (D11): the anchor must itself be visible to `roles`; cohort neighbours are
    filtered by their declared tags. The glossary record's schema keys the whole table's
    schema-preserving refs (one physical table has one schema — `ingest._schema_by_table`).

    `table_context` (joint Task 4) is the CALLER-supplied table-grain semantics — the previous
    run's `table_role`/`primary_entity` resolution at the TABLE logical ref. It is caller-supplied
    for the same reason `relationship_context` is: this builder runs before `build_graph` exists
    and issues NO queries. It is deliberately OUTSIDE `shared_identity_payload` (like every other
    derived field whose inputs legitimately differ between the two builders), so supplying it
    cannot break builder byte-identity; supplying nothing keeps `table_context_absent` honest."""
    allowed = set(allowed_classes(roles))
    if not _tag_visible(row.sensitivity, allowed):
        raise KeyError(f"column {row.column!r} is not visible to the caller")
    profile = FTR_GLOSSARY_PROFILE if glossary_record is not None else TECHNICAL_CSV_PROFILE
    schema = (glossary_record.schema or None) if glossary_record is not None else None
    object_ref = normalize_ref(row.source, schema, row.table, row.column)
    table_ref = normalize_ref(row.source, schema, row.table)

    source_values: list[SemanticValueV1] = []
    if glossary_record is not None:
        rec = glossary_record
        material = {
            "definition": "" if rec.definition_suppressed else rec.definition,
            "domain": rec.domain,
            "business_term": rec.term_name,
            "bian_path": rec.bian_path,
            "fibo_path": rec.fibo_path,
            "term_type": rec.term_type,
            "process_path": rec.process_path,
            "related_terms": ", ".join(rec.related_terms),
            "physical_fqn": rec.physical_fqn,
        }
        for field_name in _GLOSSARY_SOURCE_FIELDS:
            if material[field_name]:
                source_values.append(_source_value(field_name, material[field_name], profile))
    else:
        material = {
            "definition": row.definition,
            "sensitivity": row.sensitivity,
            "additivity": row.additivity,
            "unit": row.unit,
            "currency": row.currency,
            "entity": row.entity,
        }
        for field_name in _TECHNICAL_SOURCE_FIELDS:
            if material[field_name]:
                source_values.append(_source_value(field_name, material[field_name], profile))
    source_values.sort(key=lambda v: v.field_name)

    seen: dict[str, NeighbourColumnV1] = {}
    for other in cohort:
        if other.table != row.table or other.source != row.source:
            continue
        if other.column == row.column:
            continue
        if not _tag_visible(other.sensitivity, allowed):
            continue
        ref = normalize_ref(row.source, schema, other.table, other.column)
        role = normalize_party_role(other.column)
        seen[ref] = NeighbourColumnV1(
            object_ref=ref, column_name=other.column.strip().lower(), concept=None,
            party_role=role.value if role is not None else None)
    ordered = sorted(seen)
    truncated = len(ordered) > NEIGHBOUR_LIMIT
    neighbours = tuple(seen[ref] for ref in ordered[:NEIGHBOUR_LIMIT])

    source_map = {v.field_name: v for v in source_values}
    relationship = tuple(relationship_context)
    observations = tuple(observation_context)
    table_values = tuple(sorted(table_context, key=lambda v: v.field_name))
    missing = _missing_codes(
        concept_name=None,
        concept_path_t=(),
        resolved={},
        source=source_map,
        identifier_namespace=None,
        glossary_present=glossary_record is not None,
        table_context=table_values,
        relationship_context=relationship,
        observation_context=observations,
        catalog_profile_revision_id=catalog_profile_revision_id,
        dataset_profile_hash=dataset_profile_hash,
        party_role=None,
        neighbours_truncated=truncated,
    )
    return _finish(
        contract_version=CONTRACT_VERSION,
        catalog_source=row.source.strip().lower(),
        object_ref=object_ref,
        table_ref=table_ref,
        source_semantics=tuple(source_values),
        resolved_semantics=(),
        concept_path=(),
        identifier_namespace=None,
        table_context=table_values,
        catalog_profile_revision_id=catalog_profile_revision_id,
        dataset_profile_hash=dataset_profile_hash,
        neighbouring_columns=neighbours,
        relationship_context=relationship,
        observation_context=observations,
        missing_context=missing,
    )


# ── bundle_from_store ───────────────────────────────────────────────────────────────────────────

#: bundle field -> the decision-log field its governance rides on (mirrors
#: `column_authority._DECISION_ID_COLUMN` / `_VALUE_COLUMN`, batched).
_DECISION_GOVERNED = {"additivity": "additivity", "data_type": "logical_representation"}
#: bundle field -> field_evidence field name (identity except the physical-type axis).
_EVIDENCE_FIELD = {"data_type": "logical_representation"}
#: The operational-facts fields the bundle mirrors (authority `governed|hint`, like
#: `read_column_facts`); every other semantic field carries `operational_influence=None`.
_OPERATIONAL_FIELDS = ("additivity", "currency", "data_type", "declared_type", "entity",
                       "is_grain", "is_as_of", "unit")
_DISPLAY_FIELDS = ("ai_summary", "concept", "definition", "domain", "party_role",
                   "semantic_terms")


def _render(raw: object) -> str | None:
    """Mirror `column_authority._render`: booleans as "true"/"false", everything else str."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw)


def _bulk_eligibility(conn: DbConn, logical_ref: str) -> set[str]:
    """The decision-log fields of `logical_ref` that are feature-eligible, in ONE query — the
    batched mirror of `field_resolution.is_feature_eligible` (latest decision by
    (created_at, decision_event_id); not retired; carries a load-bearing value hash)."""
    rows = conn.execute(
        "SELECT DISTINCT ON (field_name) field_name, event_type, load_bearing_value_hash "
        "FROM field_decision_event WHERE logical_ref = %s "
        "ORDER BY field_name, created_at DESC, decision_event_id DESC",
        (logical_ref,)).fetchall()
    return {
        field_name
        for field_name, event_type, value_hash in rows
        if event_type not in _RETIRED_EVENTS and value_hash is not None
    }


def _bulk_active_evidence(
    conn: DbConn, logical_refs: Sequence[str]
) -> tuple[dict[tuple[str, str], list[EvidenceAuthorityV1]], dict[tuple[str, str], object]]:
    """Every ACTIVE evidence row for `logical_refs`, in ONE query: `(authorities, values)`, both
    keyed (logical_ref, field_name). Authorities are strongest-first then evidence_id for a
    deterministic tuple; `values` carries each field's latest declared `proposed_value`."""
    strength_rank = {s.value: i for i, s in enumerate(AssertionStrength)}
    rows = conn.execute(
        "SELECT logical_ref, field_name, producer, strength, lifecycle, producer_ref, "
        "evidence_id, proposed_value "
        "FROM field_evidence WHERE logical_ref = ANY(%s) AND lifecycle = 'active' "
        "ORDER BY logical_ref, field_name, created_at, evidence_id",
        (list(logical_refs),)).fetchall()
    out: dict[tuple[str, str], list] = {}
    values: dict[tuple[str, str, str], object] = {}
    for logical_ref, field_name, producer, strength, lifecycle, producer_ref, ev_id, value in rows:
        out.setdefault((logical_ref, field_name), []).append(
            (strength_rank.get(strength, 0), ev_id,
             EvidenceAuthorityV1(producer=producer, strength=strength, lifecycle=lifecycle,
                                 producer_ref=producer_ref, evidence_id=ev_id)))
        # Latest value per (ref, field, producer) — created_at order, so a later row wins.
        values[(logical_ref, field_name, producer)] = value
    return {
        key: [entry[2] for entry in sorted(entries, key=lambda e: (-e[0], e[1]))]
        for key, entries in out.items()
    }, values


def bundle_from_store(
    conn: DbConn,
    catalog_source: str,
    object_ref: str,
    *,
    roles: Iterable[str] = (),
    catalog_profile_revision_id: str | None = None,
    dataset_profile_hash: str | None = None,
    observations: Sequence[RelationshipObservationV2] = (),
    current_observation_revision_ids: Iterable[str] | None = None,
) -> SemanticContextBundleV1:
    """Build the bundle from the persisted stores through read-scoped, BATCHED reads.

    `object_ref` is the graph's (public-flattened) ref; the bundle re-keys everything by the
    SCHEMA-PRESERVING logical ref rebuilt from the authoritative `graph_node.schema_name` (the
    `logical_ref_of` rule, without its per-call query). Fails closed: an anchor that does not
    exist OR is not visible to `roles` raises `KeyError` (no existence oracle), and a degraded /
    lagged load-bearing projection raises `CatalogProjectionUnavailable` before any read is
    trusted (checked ONCE for the whole bundle, never per field).

    CURRENTNESS IS SUPPLIED, NEVER ASSUMED. `ObservationContextV1.current` mirrors the
    `relationship_observation_current` pointer (D4), and this module cannot read that pointer yet
    (the observation store arrives with the Hive/ODS slice). So a caller passing `observations`
    MUST also pass `current_observation_revision_ids` — the pointer rows' revision ids — and each
    observation's `current` is membership in that set. Supplying observations WITHOUT the pointer
    set raises: defaulting to `current=True` would let a superseded observation (measured against
    an older binding revision) present itself to feature generation as the live measurement, which
    is exactly the stale-evidence class the realization/scope guards exist to refuse. An EMPTY set
    is a legitimate answer (every supplied observation is superseded)."""
    from featuregen.overlay.upload.feature_metadata_snapshot import check_projection_readiness

    check_projection_readiness(conn)
    allowed = allowed_classes(roles)
    source = catalog_source.strip().lower()
    flat_ref = object_ref.strip().lower()

    anchor = conn.execute(
        "SELECT object_ref, schema_name, table_name, column_name, data_type, declared_type, "
        "definition, domain, concept, semantic_terms, ai_summary, additivity, unit, currency, "
        "entity, is_grain, is_as_of, party_role, grain_fact_event_id, availability_fact_event_id "
        "FROM graph_node WHERE catalog_source = %s AND lower(object_ref) = %s "
        "AND kind = 'column' AND COALESCE(visible_requires, '{}') <@ %s",
        (source, flat_ref, allowed)).fetchone()
    if anchor is None:
        raise KeyError(f"no visible column {object_ref!r} in catalog {catalog_source!r}")
    (_ref, schema_name, table_name, column_name, data_type, declared_type, definition, domain,
     concept_name, semantic_terms, ai_summary, additivity, unit, currency, entity, is_grain,
     is_as_of, party_role, grain_event, availability_event) = anchor
    logical_ref = normalize_ref(source, schema_name or None, table_name, column_name)
    table_logical_ref = normalize_ref(source, schema_name or None, table_name)

    neighbours_rows = conn.execute(
        "SELECT schema_name, column_name, concept, party_role FROM graph_node "
        "WHERE catalog_source = %s AND kind = 'column' AND table_name = %s "
        "AND lower(object_ref) <> %s AND COALESCE(visible_requires, '{}') <@ %s "
        "ORDER BY object_ref",
        (source, table_name, flat_ref, allowed)).fetchall()
    seen: dict[str, NeighbourColumnV1] = {}
    for n_schema, n_column, n_concept, n_party_role in neighbours_rows:
        ref = normalize_ref(source, n_schema or None, table_name, n_column)
        # `party_role` is a fill-only-NULL projection of the SAME deterministic normalizer;
        # falling back to it for an un-projected node reads the value the projection WOULD fill.
        role = n_party_role or getattr(normalize_party_role(n_column), "value", None)
        seen[ref] = NeighbourColumnV1(
            object_ref=ref, column_name=n_column.strip().lower(), concept=n_concept,
            party_role=role)
    ordered = sorted(seen)
    truncated = len(ordered) > NEIGHBOUR_LIMIT
    neighbours = tuple(seen[ref] for ref in ordered[:NEIGHBOUR_LIMIT])

    # The table anchor's visibility is DERIVED (D11): the caller provably sees >= 1 of its
    # columns (the anchor above), so the table row itself needs no second predicate.
    table_row = conn.execute(
        "SELECT definition, domain, semantic_terms, ai_summary FROM graph_node "
        "WHERE catalog_source = %s AND kind = 'table' AND table_name = %s",
        (source, table_name)).fetchone()

    evidence_by_field, evidence_values = _bulk_active_evidence(
        conn, [logical_ref, table_logical_ref])
    eligible = _bulk_eligibility(conn, logical_ref)

    source_values: list[SemanticValueV1] = []
    resolved_values: list[SemanticValueV1] = []
    display = {
        "ai_summary": ai_summary, "concept": concept_name, "definition": definition,
        "domain": domain, "semantic_terms": semantic_terms,
        "party_role": party_role or getattr(normalize_party_role(column_name), "value", None),
    }
    operational = {
        "additivity": additivity, "currency": currency, "data_type": data_type,
        "declared_type": declared_type,
        # Display entity resolves through the alias seam (Task 2 / D12.1): a counterparty_id
        # column displays `customer`; stored facts and fact keys are untouched.
        "entity": display_entity(concept_name, entity),
        "is_grain": bool(is_grain),
        "is_as_of": bool(is_as_of), "unit": unit,
    }
    anchor_fields = sorted({
        f for (ref, f) in evidence_by_field if ref == logical_ref})
    for field_name in anchor_fields:
        entries = evidence_by_field[(logical_ref, field_name)]
        from_source = [e for e in entries if e.producer == EvidenceProducer.SOURCE.value]
        if from_source:
            # Source evidence rows carry the declared value verbatim (jsonb round-trip).
            source_values.append(SemanticValueV1(
                field_name=field_name,
                value=evidence_values[
                    (logical_ref, field_name, EvidenceProducer.SOURCE.value)],
                evidence=tuple(from_source),
                resolution_status="declared",
            ))
    source_values.sort(key=lambda v: v.field_name)

    for field_name in _DISPLAY_FIELDS:
        value = _render(display.get(field_name))
        entries = tuple(evidence_by_field.get((logical_ref, _EVIDENCE_FIELD.get(
            field_name, field_name)), ()))
        if value is None and not entries:
            continue
        resolved_values.append(SemanticValueV1(
            field_name=field_name,
            value=value,
            evidence=entries,
            resolution_status="current" if value is not None else UNRESOLVED_PENDING_REVIEW,
        ))
    for field_name in _OPERATIONAL_FIELDS:
        value = _render(operational.get(field_name))
        entries = tuple(evidence_by_field.get((logical_ref, _EVIDENCE_FIELD.get(
            field_name, field_name)), ()))
        if value is None and not entries:
            continue
        if field_name in _DECISION_GOVERNED:
            influence = "governed" if _DECISION_GOVERNED[field_name] in eligible else "hint"
        elif field_name == "is_grain":
            influence = "governed" if (bool(is_grain) and grain_event is not None) else "hint"
        elif field_name == "is_as_of":
            influence = "governed" if (bool(is_as_of) and availability_event is not None) else "hint"
        else:
            influence = "hint"
        resolved_values.append(SemanticValueV1(
            field_name=field_name,
            value=value,
            evidence=entries,
            resolution_status="current" if value is not None else UNRESOLVED_PENDING_REVIEW,
            operational_influence=influence,
        ))
    resolved_values.sort(key=lambda v: v.field_name)

    table_values: list[SemanticValueV1] = []
    if table_row is not None:
        t_definition, t_domain, t_semantic_terms, t_ai_summary = table_row
        for field_name, raw in (("ai_summary", t_ai_summary), ("definition", t_definition),
                                ("domain", t_domain), ("semantic_terms", t_semantic_terms)):
            value = _render(raw)
            entries = tuple(evidence_by_field.get((table_logical_ref, field_name), ()))
            if value is None and not entries:
                continue
            table_values.append(SemanticValueV1(
                field_name=field_name, value=value, evidence=entries,
                resolution_status="current" if value is not None else
                UNRESOLVED_PENDING_REVIEW))

    relationship = _scoped_relationship_context(conn, flat_ref, allowed)
    path = concept_path(concept_name)
    # The ISSUER axis (Task 2): the same `identifier_scope` production the grounded bridge path
    # consumes — one seam, no third namespace surface. Unresolved stays honest (basis
    # "unresolved" + the closed missing-context code).
    issuer_scope, issuer_basis = resolve_identifier_issuer(conn, source, concept_name)
    namespace = _identifier_namespace(
        concept_name, issuer_scope=issuer_scope,
        basis=issuer_basis if issuer_scope is not None else None)
    resolved_map = {v.field_name: v for v in resolved_values}
    source_map = {v.field_name: v for v in source_values}

    observation_context: list[ObservationContextV1] = []
    if observations and current_observation_revision_ids is None:
        raise SemanticContextError(
            "observations supplied without `current_observation_revision_ids`: currentness "
            "mirrors the `relationship_observation_current` pointer and is never assumed — pass "
            "the pointer rows' revision ids (an empty set means every observation is superseded)")
    current_ids = frozenset(current_observation_revision_ids or ())
    realized_by_revision = {
        realized.realization_revision_id: realized
        for link in relationship for realized in link.realizations
    }
    for observation in observations:
        realized = realized_by_revision.get(observation.realization_revision_id)
        if realized is None:
            raise SemanticContextError(
                "observation supplied for a realization outside this bundle's relationship "
                f"context: {observation.realization_revision_id!r}")
        observation_context.append(observation_context_from(
            observation, realization=realized,
            current=observation.observation_revision_id in current_ids))

    missing = _missing_codes(
        concept_name=concept_name,
        concept_path_t=path,
        resolved=resolved_map,
        source=source_map,
        identifier_namespace=namespace,
        glossary_present="business_term" in source_map,
        table_context=tuple(table_values),
        relationship_context=relationship,
        observation_context=tuple(observation_context),
        catalog_profile_revision_id=catalog_profile_revision_id,
        dataset_profile_hash=dataset_profile_hash,
        party_role=resolved_map.get("party_role").value if "party_role" in resolved_map else None,
        neighbours_truncated=truncated,
    )
    return _finish(
        contract_version=CONTRACT_VERSION,
        catalog_source=source,
        object_ref=logical_ref,
        table_ref=table_logical_ref,
        source_semantics=tuple(source_values),
        resolved_semantics=tuple(resolved_values),
        concept_path=path,
        identifier_namespace=namespace,
        table_context=tuple(table_values),
        catalog_profile_revision_id=catalog_profile_revision_id,
        dataset_profile_hash=dataset_profile_hash,
        neighbouring_columns=neighbours,
        relationship_context=relationship,
        observation_context=tuple(observation_context),
        missing_context=missing,
    )


def _scoped_relationship_context(
    conn: DbConn, flat_ref: str, allowed: list[str]
) -> tuple[RelationshipContextV1, ...]:
    """The anchor's available links through the ONE shipped reader, then read-scoped (D11): a
    link is shown only when EVERY member column of both endpoints is itself visible — a
    restricted column's name never enters another column's context. Fail-closed: an endpoint
    column absent from the graph is not provably visible, so its link is omitted."""
    links = available_identifier_links(conn, object_ref=flat_ref)
    if not links:
        return ()
    member_keys: set[tuple[str, str]] = set()
    for link in links:
        for endpoint in (link.assessment.left_endpoint, link.assessment.right_endpoint):
            for member in endpoint.members:
                m_source, m_schema, m_table, m_column = parse_ref(member.logical_column_ref)
                member_keys.add((m_source, f"{m_schema}.{m_table}.{m_column}"))
    refs = sorted({ref for (_s, ref) in member_keys})
    visible_rows = conn.execute(
        "SELECT catalog_source, lower(object_ref) FROM graph_node "
        "WHERE kind = 'column' AND lower(object_ref) = ANY(%s) "
        "AND COALESCE(visible_requires, '{}') <@ %s",
        (refs, allowed)).fetchall()
    visible = {(row[0], row[1]) for row in visible_rows}
    out: list[RelationshipContextV1] = []
    for link in links:
        members = {
            (parse_ref(member.logical_column_ref)[0],
             ".".join(parse_ref(member.logical_column_ref)[1:4]))
            for endpoint in (link.assessment.left_endpoint, link.assessment.right_endpoint)
            for member in endpoint.members
        }
        if members <= visible:
            out.append(relationship_context_from_link(conn, link))
    out.sort(key=lambda link: link.relationship_ref)
    return tuple(out)


# ── purpose adapters (bounded plain-dict projections; egress classification is Task 4 / D10) ────

def _value_of(bundle: SemanticContextBundleV1, field_name: str):
    for values in (bundle.resolved_semantics, bundle.source_semantics):
        for v in values:
            if v.field_name == field_name and v.value not in (None, ""):
                return v.value
    return None


def _identity_parts(bundle: SemanticContextBundleV1) -> tuple[str, str]:
    _source, _schema, table, column = parse_ref(bundle.object_ref)
    return table, column or ""


def _roster(bundle: SemanticContextBundleV1) -> list[dict]:
    """The bounded sibling roster. Entry keys are exactly the widened `_ROSTER_ENTRY_KEYS`
    (`column` + the two closed-vocabulary tokens); an absent concept/role is OMITTED rather than
    sent as null — the egress roster gate admits short strings only, and "we do not know" is
    honestly the absence of the key, not a null masquerading as a value."""
    out: list[dict] = []
    for n in bundle.neighbouring_columns[:ADAPTER_LIST_LIMIT]:
        entry: dict = {"column": n.column_name}
        if n.concept:
            entry["concept"] = n.concept
        if n.party_role:
            entry["party_role"] = n.party_role
        out.append(entry)
    return out


#: The separator `ingest._write_glossary_source_evidence` joins `GlossaryRecord.related_terms` with
#: before persisting it as ONE evidence value. The adapters split on it to restore the LIST form,
#: which is the shape the egress layer classifies `related_terms` under (`_LIST_PROSE_META_KEYS`):
#: each term is then PII-scanned at its own indexed path (`related_terms[0]`) and length-bounded
#: per TERM rather than per joined blob — a 40-term glossary column whose joined string exceeds the
#: 200-char per-value cap would otherwise have its WHOLE item egress-excluded.
_RELATED_TERMS_JOIN = ", "


def _related_terms(bundle: SemanticContextBundleV1) -> list[str] | None:
    raw = _value_of(bundle, "related_terms")
    if raw is None:
        return None
    terms = [t.strip() for t in str(raw).split(_RELATED_TERMS_JOIN) if t.strip()]
    return terms or None


def _table_value(bundle: SemanticContextBundleV1, field_name: str):
    for v in bundle.table_context:
        if v.field_name == field_name and v.value not in (None, ""):
            return v.value
    return None


def _with_extra(payload: dict, extra: Mapping | None) -> dict:
    """Merge caller-supplied UPLOAD-only material under the adapter's own keys.

    The bundle contract deliberately does not carry three things the ingest-time classifier has
    always had: the file's DECLARED SQL type token, the sidecar `synonyms`, and the uploader's
    residual `source_attributes` columns. None of them exists as persisted field evidence, so
    putting them in `source_semantics` would fork the two builders' byte-identity (D1) for material
    only one of them can ever see. They ride HERE instead — lowest precedence, so a bundle-derived
    key always wins and `extra` can only ADD. Empty/None values are dropped: an adapter payload
    never carries a fabricated blank."""
    if not extra:
        return payload
    for key in sorted(extra):
        value = extra[key]
        if key in payload or value in (None, "", [], ()):
            continue
        payload[key] = list(value) if isinstance(value, (list, tuple)) else value
    return payload


def _namespace_dict(bundle: SemanticContextBundleV1) -> dict | None:
    ns = bundle.identifier_namespace
    if ns is None:
        return None
    return {"scheme": ns.scheme, "issuer_scope": ns.issuer_scope, "basis": ns.basis}


#: The EXACT key list `for_concept_enrichment` renders from bundle-derived material, in emission
#: order. Frozen as data (not implied by the code) because the replay fingerprint (semantic Task 3)
#: hashes precisely what is RENDERED to the classifier: a key added here is a payload change and
#: MUST re-key the cache; a bundle field that is not here was never shown and must not.
CONCEPT_ENRICHMENT_RENDERED_KEYS: tuple[str, ...] = (
    "table", "column",
    "term_name", "business_definition", "data_domain", "bian_path", "fibo_path", "term_type",
    "process_path", "related_terms",
    "concept", "declared_type", "operational_type", "party_role",
    "table_role", "primary_entity",
    "column_roster",
)

#: The critic's ADDITIONAL rendered keys on top of the classifier payload (same contract).
CRITIC_RENDERED_KEYS: tuple[str, ...] = ("entity", "concept_path", "identifier_namespace")

#: The summary/definition/synonym/unit drafting keys (same contract).
SUMMARY_RENDERED_KEYS: tuple[str, ...] = (
    "table", "column", "term_name", "business_definition", "data_domain", "term_type",
    "process_path", "related_terms", "semantic_terms", "concept", "party_role",
    "table_role", "primary_entity",
)


def for_concept_enrichment(bundle: SemanticContextBundleV1, *,
                           extra: Mapping | None = None) -> dict:
    """Pass-A concept classification context — the ONE assembly point for the classifier payload.

    Key names reuse `_ITEM_META_ALLOWED` / `_COLUMN_PROFILE_KEYS` members where one exists; every
    key emitted here is classified in the enrichment egress allowlists (D10), including the widened
    `_ROSTER_ENTRY_KEYS` the roster entries use. `extra` carries the upload-only material the
    bundle contract cannot hold (see :func:`_with_extra`)."""
    table, column = _identity_parts(bundle)
    out: dict = {"table": table, "column": column}
    for key, field_name in (("term_name", "business_term"),
                            ("business_definition", "definition"),
                            ("data_domain", "domain"), ("bian_path", "bian_path"),
                            ("fibo_path", "fibo_path"), ("term_type", "term_type"),
                            ("process_path", "process_path")):
        value = _value_of(bundle, field_name)
        if value is not None:
            out[key] = value
    related = _related_terms(bundle)
    if related is not None:
        out["related_terms"] = related
    for key, field_name in (("concept", "concept"), ("declared_type", "declared_type"),
                            ("operational_type", "data_type"), ("party_role", "party_role")):
        value = _value_of(bundle, field_name)
        if value is not None:
            out[key] = value
    # Table-grain semantics (joint Task 4): the role this column's TABLE plays and the entity it is
    # about. Both are the platform's own resolved closed-vocabulary tokens, never uploader text.
    for key in ("table_role", "primary_entity"):
        value = _table_value(bundle, key)
        if value is not None:
            out[key] = value
    out["column_roster"] = _roster(bundle)
    return _with_extra(out, extra)


def for_critic(bundle: SemanticContextBundleV1, *, extra: Mapping | None = None) -> dict:
    """Concept-critic context: the classification inputs plus the deterministic identity axes."""
    out = for_concept_enrichment(bundle, extra=extra)
    entity = _value_of(bundle, "entity")
    if entity is not None:
        out["entity"] = entity
    out["concept_path"] = list(bundle.concept_path)
    out["identifier_namespace"] = _namespace_dict(bundle)
    return out


def for_summary(bundle: SemanticContextBundleV1, *, extra: Mapping | None = None) -> dict:
    """Drafting context for the prose/annotation Pass-A tasks (summary, definition, synonyms,
    unit): curated meaning + role + table grain, no relationship payloads and no sibling roster —
    those tasks answer about ONE column and a roster is contamination surface, not signal."""
    table, column = _identity_parts(bundle)
    out: dict = {"table": table, "column": column}
    for key, field_name in (("term_name", "business_term"),
                            ("business_definition", "definition"),
                            ("data_domain", "domain"), ("term_type", "term_type"),
                            ("process_path", "process_path"),
                            ("semantic_terms", "semantic_terms"), ("concept", "concept"),
                            ("party_role", "party_role")):
        value = _value_of(bundle, field_name)
        if value is not None:
            out[key] = value
    related = _related_terms(bundle)
    if related is not None:
        out["related_terms"] = related
    for key in ("table_role", "primary_entity"):
        value = _table_value(bundle, key)
        if value is not None:
            out[key] = value
    return _with_extra(out, extra)


def for_feature_generation(bundle: SemanticContextBundleV1) -> dict:
    """Feature-generation context (the v4 shape's raw material). Fact keys reuse the
    `_FEATURE_COLUMN_FACT_KEYS` wrapper convention: `{value, authority}` with authority the
    OPERATIONAL `governed|hint` axis — an LLM-proposed semantic value NEVER rides this wrapper
    (its authority is the D2 triple on the bundle, visible separately)."""
    table, column = _identity_parts(bundle)
    resolved = {v.field_name: v for v in bundle.resolved_semantics}
    out: dict = {
        "object_ref": bundle.object_ref,
        "table": table,
        "column": column,
        "concept": _value_of(bundle, "concept"),
        "domain": _value_of(bundle, "domain"),
        "definition": _value_of(bundle, "definition"),
        "ai_summary": _value_of(bundle, "ai_summary"),
        "semantic_terms": _value_of(bundle, "semantic_terms"),
    }
    for fact_key in ("data_type", "declared_type", "entity", "additivity", "unit", "currency",
                     "is_grain", "is_as_of"):
        got = resolved.get(fact_key)
        value = _render(got.value) if got is not None else None
        authority = got.operational_influence if got is not None else "hint"
        out[fact_key] = {"value": value, "authority": authority or "hint"}
    out["concept_path"] = list(bundle.concept_path)
    out["identifier_namespace"] = _namespace_dict(bundle)
    out["party_role"] = _value_of(bundle, "party_role")
    out["relationships"] = [
        {"relationship_ref": link.relationship_ref, "kind": link.kind,
         "availability": link.availability, "review_status": link.review_status}
        for link in bundle.relationship_context[:ADAPTER_LIST_LIMIT]
    ]
    out["missing_context"] = list(bundle.missing_context)
    return out


def for_analysis_planning(bundle: SemanticContextBundleV1) -> dict:
    """Data-agent planning context: grain/time/identity axes plus honest missing-context codes."""
    table, column = _identity_parts(bundle)
    return {
        "object_ref": bundle.object_ref,
        "table": table,
        "column": column,
        "concept": _value_of(bundle, "concept"),
        "entity": _value_of(bundle, "entity"),
        "data_type": _value_of(bundle, "data_type"),
        "declared_type": _value_of(bundle, "declared_type"),
        "is_grain": _value_of(bundle, "is_grain"),
        "is_as_of": _value_of(bundle, "is_as_of"),
        "party_role": _value_of(bundle, "party_role"),
        "concept_path": list(bundle.concept_path),
        "identifier_namespace": _namespace_dict(bundle),
        "missing_context": list(bundle.missing_context),
    }
