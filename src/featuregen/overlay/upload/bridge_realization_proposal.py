"""A4c: AI-proposed identifier links become PROVISIONAL sandbox realizations (R11).

This module closes the platform's oldest dead end: an AI-proposed cross-catalog link that could
never become a previewable join. Two service functions, no API route (that is B2b's surface):

* :func:`produce_provisional_realization` — the SERVER converts one available AI-proposed
  identifier link into an immutable, SANDBOX-scoped, directional realization revision. It writes
  through the realization store's APPEND half ONLY (:func:`bridge_store.append_realization_revision`)
  and NEVER the CAS-publish half: candidate generation mints immutable revisions; two users
  considering different mappings must not compete over the shared current pointer. Adoption pins
  the exact revision; promoting a governed current pointer is a separate, optional, later act.

* :func:`assess_realization_for_preview` — R11's distinct typed preview assessment. It reads the
  EXACT pinned revision (never latest), revalidates bridge lifecycle + physical bindings, accepts
  unknown cardinality ONLY with a complete pinned guard policy (``PROVISIONAL_WITH_GUARDS``), and
  still rejects missing mappings, revoked/withdrawn links and known fan-out cardinalities.
  ``revalidate_bridge_realization`` — the production reader, which requires production eligibility
  and refuses unknown cardinality — is deliberately NOT touched, weakened or reused.

**Honest absence, never fabrication.** Source bindings are READ from the platform's real binding
surface (:mod:`featuregen.data_agent.binding_store` over ``physical_dataset_binding`` +
``physical_dataset_binding_revision``); when no PERSISTED binding revision exists for an endpoint's
dataset the producer refuses with :data:`SOURCE_BINDING_REVISION_MISSING` — it never fabricates a
binding (journeys seed bindings in fixture setup). Cardinality is UNKNOWN unless deterministically
proven from a governed key; safety starts UNASSESSED; the ``JoinKeyNormalizationPolicy`` is
explicit — governed metadata where it exists, else :func:`refusing_normalization_policy` (which
declares NO cross-type comparison and blank keys never match).

The execution context here is the ``environment`` string; A3's execution-context revision store is
a later execution-order item and this producer deliberately does not invent a stand-in for it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from featuregen.data_agent.binding_store import binding_revision_exists, resolve_table
from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.overlay.upload.bridge_assessment import (
    AvailableIdentifierLinkV1,
    BridgeContractError,
    ConceptAuthority,
    IdentifierEndpointV1,
    LinkAvailability,
    LinkReviewStatus,
    TupleKeyRole,
    read_overlay_identifier_link_state,
)
from featuregen.overlay.upload.bridge_realization import (
    BridgeJoinRealizationRevisionV1,
    BridgeRealizationCurrentV1,
    CardinalityBasis,
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    BridgeStoreCorruption,
    _binding_revision_is_stored,
    append_realization_revision,
    bridge_candidate_currentness,
    bridge_dependency_snapshot_id,
    realization_from_json,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    ContractDefect,
    LogicalTemporalJoinSemanticsV1,
    UnmatchedRowMeaningV1,
)
from featuregen.overlay.upload.planner.physical_plan_v1 import (
    ALLOCATION_POLICY_REQUIRED,
    BlankKeyBehaviorV1,
    CaseNormalizationV1,
    CompositeKeyOrderingV1,
    JoinKeyNormalizationPolicy,
    JoinValidationPolicyRevisionV1,
    LeadingZeroPolicyV1,
    UnmatchedRowBehaviorV1,
    WhitespaceNormalizationV1,
)
from featuregen.overlay.upload.read_scope import allowed_classes, visibility_predicate
from featuregen.overlay.upload.semantic_eligibility_reasons import (
    DIRECTIONAL_CARDINALITY_UNPROVEN,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

__all__ = [
    "DEPENDENCY_SNAPSHOT_MISMATCH",
    "DIRECTIONAL_CARDINALITY_FANS_OUT",
    "DIRECTIONAL_MAPPING_INCOMPLETE",
    "ENDPOINT_COLUMN_NOT_READABLE",
    "FEATURE_GENERATION_PURPOSE",
    "IDENTIFIER_LINK_CANDIDATE_WITHDRAWN",
    "IDENTIFIER_LINK_NOT_AVAILABLE",
    "PreviewAssessmentVerdictV1",
    "ProvisionalBridgeRealizationV1",
    "ProvisionalRealizationRefused",
    "REALIZATION_DEMOTED",
    "REALIZATION_ENVIRONMENT_MISMATCH",
    "REALIZATION_PIN_SUPERSEDED",
    "REALIZATION_PURPOSE_MISMATCH",
    "REALIZATION_REVISION_NOT_FOUND",
    "RealizationPreviewAssessmentV1",
    "SOURCE_BINDING_REVISION_MISSING",
    "UNMATCHED_ROW_CONTRADICTION",
    "assess_realization_for_preview",
    "produce_provisional_realization",
    "refusing_normalization_policy",
    "validate_unmatched_row_coherence",
]

# ── named refusal codes ──────────────────────────────────────────────────────────────────────────
#: Owner's-matrix row "Missing directional mapping": the mapping does not resolve every endpoint
#: member exactly once (missing, unknown, or ambiguous/duplicated members all land here).
DIRECTIONAL_MAPPING_INCOMPLETE = "DIRECTIONAL_MAPPING_INCOMPLETE"
#: An endpoint column the governed catalog does not describe, or that the caller's read scope may
#: not see. Fail closed either way: a join over a column nobody may read is not previewable.
ENDPOINT_COLUMN_NOT_READABLE = "ENDPOINT_COLUMN_NOT_READABLE"
#: HONEST ABSENCE: no PERSISTED source-binding revision exists for an endpoint's dataset
#: (``physical_dataset_binding_revision`` is empty until an operator/journey seeds it). The
#: producer never fabricates a binding.
SOURCE_BINDING_REVISION_MISSING = "SOURCE_BINDING_REVISION_MISSING"
#: The backing identifier link is withdrawn/revoked/rejected/stale/unreadable — not available.
IDENTIFIER_LINK_NOT_AVAILABLE = "IDENTIFIER_LINK_NOT_AVAILABLE"
#: The governed candidate behind the link was withdrawn by a complete global derivation.
IDENTIFIER_LINK_CANDIDATE_WITHDRAWN = "IDENTIFIER_LINK_CANDIDATE_WITHDRAWN"
#: The pinned realization revision does not exist in the append-only store.
REALIZATION_REVISION_NOT_FOUND = "REALIZATION_REVISION_NOT_FOUND"
#: The pinned dependency snapshot disagrees with the stored revision/dependency rows.
DEPENDENCY_SNAPSHOT_MISMATCH = "DEPENDENCY_SNAPSHOT_MISMATCH"
#: A published current pointer names a NEWER revision — the staleness law: superseding realization
#: refuses the old pin; adoption of the new revision is the path, never silent substitution.
REALIZATION_PIN_SUPERSEDED = "REALIZATION_PIN_SUPERSEDED"
#: The pinned revision's own current pointer was demoted (stale/rejected/superseded lifecycle).
REALIZATION_DEMOTED = "REALIZATION_DEMOTED"
REALIZATION_ENVIRONMENT_MISMATCH = "REALIZATION_ENVIRONMENT_MISMATCH"
REALIZATION_PURPOSE_MISMATCH = "REALIZATION_PURPOSE_MISMATCH"
#: A KNOWN fan-out cardinality that is not a final-grain question still refuses preview; the
#: final-grain spelling is the fan-out law's ``ALLOCATION_POLICY_REQUIRED``.
DIRECTIONAL_CARDINALITY_FANS_OUT = "DIRECTIONAL_CARDINALITY_FANS_OUT"
#: The step-3 carry-forward: logical unmatched-row MEANING and physical guard-policy BEHAVIOR
#: contradict each other (exclude vs preserve, or not-applicable vs exclude).
UNMATCHED_ROW_CONTRADICTION = "UNMATCHED_ROW_CONTRADICTION"

FEATURE_GENERATION_PURPOSE = "feature_generation"

#: Provenance versions for provisionally produced revisions. ``admission_policy_version`` is
#: deliberately spelled to say what it IS: no admission policy ran — this revision was minted by
#: candidate generation and admission/safety assessment are later, separate acts.
PROVISIONAL_DERIVATION_VERSION = "provisional-directional-realization-v1"
PROVISIONAL_ADMISSION_POLICY_VERSION = "unadmitted-provisional-v1"

#: Concept authorities that constitute a GOVERNED declaration for cardinality proof. LLM is the
#: proposer, never the disposer: an LLM-asserted key proves nothing deterministically.
_GOVERNED_KEY_AUTHORITIES = frozenset({
    ConceptAuthority.SOURCE,
    ConceptAuthority.HUMAN,
    ConceptAuthority.DETERMINISTIC,
})


class ProvisionalRealizationRefused(RuntimeError):
    """A governed refusal from the producer, carrying its named ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def refusing_normalization_policy() -> JoinKeyNormalizationPolicy:
    """The fail-closed normalization policy used when NO governed metadata declares one.

    Everything preserved, NOTHING assumed: no whitespace trimming, no case folding, no leading-zero
    stripping, an EMPTY coercion list (no cross-type comparison is permitted — ``varchar(150)`` ↔
    ``string`` equality must be DECLARED by governance, never assumed), blank keys never match,
    null keys never match (the law), composite keys compare in declared pair order.
    """
    return JoinKeyNormalizationPolicy(
        whitespace=WhitespaceNormalizationV1.PRESERVE,
        case_handling=CaseNormalizationV1.PRESERVE,
        leading_zeros=LeadingZeroPolicyV1.PRESERVE,
        declared_type_coercions=(),
        blank_key_behavior=BlankKeyBehaviorV1.NEVER_MATCH,
        nulls_never_match=True,
        composite_key_ordering=CompositeKeyOrderingV1.DECLARED_PAIR_ORDER,
    )


@dataclass(frozen=True, slots=True)
class ProvisionalBridgeRealizationV1:
    """What the producer minted: the immutable revision (persisted through the APPEND half), the
    ``BridgeRealizationCurrentV1``-SHAPED row a later publish would use (NOT persisted — R11), the
    exact dependency rows, and the explicit key-normalization policy for the physical plan
    segment."""

    revision: BridgeJoinRealizationRevisionV1
    current: BridgeRealizationCurrentV1
    dependencies: tuple[BridgeDependencyRefV1, ...]
    key_normalization: JoinKeyNormalizationPolicy


# ── the producer ─────────────────────────────────────────────────────────────────────────────────
def _oriented_endpoints(
    link: AvailableIdentifierLinkV1, from_logical_table_ref: str
) -> tuple[IdentifierEndpointV1, IdentifierEndpointV1]:
    """Resolve the intended traversal direction over the link's UNORDERED endpoint pair."""
    source, schema, table, column = parse_ref(from_logical_table_ref.strip().lower())
    if column is not None:
        raise BridgeContractError(
            f"from_logical_table_ref must address a TABLE, got {from_logical_table_ref!r}")
    wanted = normalize_ref(source, schema, table)
    left = link.assessment.left_endpoint
    right = link.assessment.right_endpoint
    if left.logical_table_ref == right.logical_table_ref:
        raise BridgeContractError(
            "both link endpoints live on the same table; a table ref cannot orient the traversal")
    if wanted == left.logical_table_ref:
        return left, right
    if wanted == right.logical_table_ref:
        return right, left
    raise BridgeContractError(
        f"from_logical_table_ref {wanted!r} names neither link endpoint "
        f"({left.logical_table_ref!r}, {right.logical_table_ref!r})")


def _validated_pairs(
    from_endpoint: IdentifierEndpointV1,
    to_endpoint: IdentifierEndpointV1,
    ordered_member_pairs: tuple[tuple[str, str], ...],
) -> tuple[ColumnPairV1, ...]:
    """Obligations 1-3: resolve EVERY endpoint member, preserve declared composite order, refuse
    missing/unknown/ambiguous mappings by name."""
    if not ordered_member_pairs:
        raise ProvisionalRealizationRefused(
            DIRECTIONAL_MAPPING_INCOMPLETE, "the directional mapping is empty")
    pairs = tuple(
        ColumnPairV1(from_ref, to_ref) for from_ref, to_ref in ordered_member_pairs)
    from_refs = [pair.from_logical_column_ref for pair in pairs]
    to_refs = [pair.to_logical_column_ref for pair in pairs]
    for side, refs in (("from", from_refs), ("to", to_refs)):
        duplicated = sorted({ref for ref in refs if refs.count(ref) > 1})
        if duplicated:
            raise ProvisionalRealizationRefused(
                DIRECTIONAL_MAPPING_INCOMPLETE,
                f"ambiguous mapping: {side}-side member(s) mapped more than once: {duplicated}")
    from_members = {member.logical_column_ref for member in from_endpoint.members}
    to_members = {member.logical_column_ref for member in to_endpoint.members}
    unknown = sorted(set(from_refs) - from_members) + sorted(set(to_refs) - to_members)
    if unknown:
        raise ProvisionalRealizationRefused(
            DIRECTIONAL_MAPPING_INCOMPLETE,
            f"mapping names column(s) that are not endpoint members: {unknown}")
    unresolved = sorted(from_members - set(from_refs)) + sorted(to_members - set(to_refs))
    if unresolved:
        raise ProvisionalRealizationRefused(
            DIRECTIONAL_MAPPING_INCOMPLETE,
            f"endpoint member(s) left unresolved by the directional mapping: {unresolved}")
    return pairs


def _refuse_hidden_members(conn, endpoint: IdentifierEndpointV1, allowed: list[str]) -> None:
    """Obligation 4: an endpoint column the catalog does not describe, or that the caller's read
    scope hides, refuses — fail closed, one predicate (the shipped read-scope rule)."""
    for member in endpoint.members:
        source, _schema, table, column = parse_ref(member.logical_column_ref)
        rows = conn.execute(
            f"SELECT ({visibility_predicate()}) FROM graph_node "
            "WHERE catalog_source = %s AND lower(object_ref) = %s AND kind = 'column'",
            (allowed, source, f"public.{table}.{column}"),
        ).fetchall()
        if not rows:
            raise ProvisionalRealizationRefused(
                ENDPOINT_COLUMN_NOT_READABLE,
                f"the governed catalog does not describe {member.logical_column_ref}")
        if not all(bool(visible) for (visible,) in rows):
            raise ProvisionalRealizationRefused(
                ENDPOINT_COLUMN_NOT_READABLE,
                f"the caller's read scope hides {member.logical_column_ref}")


def _bound_endpoint(conn, endpoint: IdentifierEndpointV1) -> IdentifierEndpointV1:
    """Obligation 5: bind the EXACT persisted source revision, or refuse — never fabricate.

    Reads the platform's real source-binding surface (``binding_store.resolve_table`` over the
    per-table binding registry + the catalog-engine substrate) and then requires the binding's
    immutable revision to actually EXIST in ``physical_dataset_binding_revision``: a revision id is
    deterministic, so a derived binding whose revision was never recorded would name a row that
    does not exist — exactly the dead reference this refusal exists to prevent.
    """
    source, _schema, table, _column = parse_ref(endpoint.logical_table_ref)
    resolved = resolve_table(conn, catalog_source=source, table=table)
    if resolved is None:
        raise ProvisionalRealizationRefused(
            SOURCE_BINDING_REVISION_MISSING,
            f"no physical source binding is configured for {endpoint.logical_table_ref}; the "
            "producer never fabricates one — record the binding first")
    binding, _connection = resolved
    if not binding_revision_exists(conn, binding.binding_revision_id):
        raise ProvisionalRealizationRefused(
            SOURCE_BINDING_REVISION_MISSING,
            f"binding {binding.binding_id!r} for {endpoint.logical_table_ref} has no persisted "
            f"revision {binding.binding_revision_id!r}; a realization must pin a revision that "
            "exists, never one it would have to invent")
    return _rebind(endpoint, binding)


def _rebind(
    endpoint: IdentifierEndpointV1, binding: PhysicalDatasetBindingV1
) -> IdentifierEndpointV1:
    members = tuple(
        replace(
            member,
            physical_identity=binding.column(parse_ref(member.logical_column_ref)[3]),
        )
        for member in endpoint.members
    )
    return replace(
        endpoint,
        members=members,
        physical_binding=binding,
        binding_revision_id=binding.binding_revision_id,
    )


def _proven_cardinality(
    from_endpoint: IdentifierEndpointV1, to_endpoint: IdentifierEndpointV1
) -> tuple[DirectionalCardinalityVerdictV1, CardinalityBasis]:
    """Cardinality UNKNOWN unless DETERMINISTICALLY proven from a governed key declaration.

    The to-side tuple being a governed COMPLETE unique key proves each driving row matches at most
    one target row (N:1); both sides governed-unique proves 1:1. An LLM-asserted key proves
    nothing (the LLM proposes; governance disposes)."""

    def governed_unique(endpoint: IdentifierEndpointV1) -> bool:
        return (
            endpoint.tuple_key_role is TupleKeyRole.COMPLETE_UNIQUE_KEY
            and endpoint.concept_authority in _GOVERNED_KEY_AUTHORITIES
        )

    if governed_unique(to_endpoint):
        proven = (
            Cardinality.ONE_TO_ONE
            if governed_unique(from_endpoint)
            else Cardinality.MANY_TO_ONE
        )
        return DirectionalCardinalityVerdictV1(proven), CardinalityBasis.GOVERNED_KEY
    return DirectionalCardinalityVerdictV1.unknown(), CardinalityBasis.NONE


def produce_provisional_realization(
    conn,
    link: AvailableIdentifierLinkV1,
    *,
    from_logical_table_ref: str,
    ordered_member_pairs: tuple[tuple[str, str], ...],
    environment: str,
    roles: tuple[str, ...] = (),
    key_normalization: JoinKeyNormalizationPolicy | None = None,
) -> ProvisionalBridgeRealizationV1:
    """Convert one available AI-proposed identifier link into a provisional SANDBOX realization.

    Server-side, no human confirmation (provenance only). Applicability is pinned to
    ``purpose=feature_generation`` / ``environment`` / ``execution_tier=SANDBOX``; cardinality is
    UNKNOWN unless deterministically proven; safety starts UNASSESSED. Persists ONLY through the
    realization store's append half — no ``bridge_join_realization_current`` row is written or
    advanced. Idempotent on semantic content: the revision id is content-addressed, so the same
    inputs mint the same identity and re-appending is a no-op.
    """
    assessment = link.assessment
    if assessment.bridge_fact_key is None:
        raise BridgeContractError(
            "an AI-proposed link must carry its bridge_fact_key to be realized")
    if not environment.strip():
        raise BridgeContractError("environment must not be blank")

    # Live lifecycle recheck — availability was read once when the link was assembled, and a link
    # rejected/withdrawn since then must refuse now, not at preview.
    state = read_overlay_identifier_link_state(conn, assessment.bridge_fact_key)
    if state.availability is not LinkAvailability.AVAILABLE or state.overlay_head_event_id is None:
        raise ProvisionalRealizationRefused(
            IDENTIFIER_LINK_NOT_AVAILABLE,
            f"identifier link {assessment.bridge_fact_key!r} is not available "
            f"(status={state.folded_status}, reason={state.unavailable_reason})")
    if bridge_candidate_currentness(conn, assessment.bridge_fact_key) is False:
        raise ProvisionalRealizationRefused(
            IDENTIFIER_LINK_CANDIDATE_WITHDRAWN,
            f"the governed candidate behind {assessment.bridge_fact_key!r} was withdrawn")

    from_endpoint, to_endpoint = _oriented_endpoints(link, from_logical_table_ref)
    pairs = _validated_pairs(from_endpoint, to_endpoint, ordered_member_pairs)

    allowed = allowed_classes(roles)
    _refuse_hidden_members(conn, from_endpoint, allowed)
    _refuse_hidden_members(conn, to_endpoint, allowed)

    bound_from = _bound_endpoint(conn, from_endpoint)
    bound_to = _bound_endpoint(conn, to_endpoint)
    assert bound_from.physical_binding is not None  # _bound_endpoint refused otherwise
    assert bound_to.physical_binding is not None

    dependencies = (
        BridgeDependencyRefV1(
            "bridge_fact", assessment.bridge_fact_key, state.overlay_head_event_id),
        BridgeDependencyRefV1(
            "candidate_revision", assessment.candidate_id, assessment.candidate_revision_id),
        BridgeDependencyRefV1(
            "physical_binding",
            bound_from.physical_binding.binding_id,
            bound_from.physical_binding.binding_revision_id,
        ),
        BridgeDependencyRefV1(
            "physical_binding",
            bound_to.physical_binding.binding_id,
            bound_to.physical_binding.binding_revision_id,
        ),
    )

    cardinality, basis = _proven_cardinality(from_endpoint, to_endpoint)
    revision = BridgeJoinRealizationRevisionV1(
        bridge_fact_key=assessment.bridge_fact_key,
        from_endpoint=bound_from,
        to_endpoint=bound_to,
        column_pairs=pairs,
        predicates=(),
        applicability_scope=RealizationApplicabilityScopeV1(
            scope_id=f"provisional-{environment.strip()}-{FEATURE_GENERATION_PURPOSE}",
            execution_tier=ExecutionTier.SANDBOX,
            purposes=(FEATURE_GENERATION_PURPOSE,),
            environment=environment.strip(),
        ),
        cardinality=cardinality,
        cardinality_basis=basis,
        # Obligation 6: the AI proposal's evidence rides the revision verbatim — recorded, never
        # upgraded, never invented.
        evidence_refs=assessment.evidence_refs,
        dependency_snapshot_id=bridge_dependency_snapshot_id(dependencies),
        derivation_version=PROVISIONAL_DERIVATION_VERSION,
        admission_policy_version=PROVISIONAL_ADMISSION_POLICY_VERSION,
    )
    append_realization_revision(conn, revision, dependencies=dependencies)
    return ProvisionalBridgeRealizationV1(
        revision=revision,
        current=BridgeRealizationCurrentV1(
            realization_id=revision.realization_id,
            realization_revision_id=revision.realization_revision_id,
            safety_status=SafetyStatus.UNASSESSED,
            review_status=LinkReviewStatus.UNREVIEWED,
            lifecycle=RealizationLifecycle.ACTIVE,
            pointer_version=1,
        ),
        dependencies=dependencies,
        key_normalization=(
            key_normalization
            if key_normalization is not None
            else refusing_normalization_policy()
        ),
    )


# ── the preview assessment (R11) ─────────────────────────────────────────────────────────────────
class PreviewAssessmentVerdictV1(StrEnum):
    FULL_PREVIEW = "full_preview"
    PROVISIONAL_WITH_GUARDS = "provisional_with_guards"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class RealizationPreviewAssessmentV1:
    pinned_realization_revision_id: str
    pinned_dependency_snapshot_id: str
    environment_id: str
    join_validation_policy_revision_id: str | None
    verdict: PreviewAssessmentVerdictV1
    reason_codes: tuple[str, ...]
    realization: BridgeJoinRealizationRevisionV1 | None


#: The contradictory (logical meaning, physical behavior) pairs — each refused by name. The
#: logical layer says what an unmatched driving row MEANS for the feature; the guard policy says
#: what the executed join DOES with it; a pair where the mechanics contradict the meaning would
#: silently compute a different feature than the one declared.
_CONTRADICTORY_UNMATCHED_PAIRS = {
    (UnmatchedRowMeaningV1.EXCLUDE_DRIVING_ROW, UnmatchedRowBehaviorV1.PRESERVE_LEFT_NULL),
    (UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE, UnmatchedRowBehaviorV1.EXCLUDE),
}


def validate_unmatched_row_coherence(
    meaning: UnmatchedRowMeaningV1,
    behavior: UnmatchedRowBehaviorV1,
) -> None:
    """Step-3 carry-forward: the logical unmatched-row MEANING and the guard policy's physical
    BEHAVIOR must not contradict. ``exclude_driving_row`` executed as ``preserve_left_null``
    keeps rows the feature declared excluded; ``joined_attributes_not_applicable`` executed as
    ``exclude`` drops rows the feature declared kept. Both refuse by name."""
    if not isinstance(meaning, UnmatchedRowMeaningV1):
        raise ContractDefect("meaning must be an UnmatchedRowMeaningV1")
    if not isinstance(behavior, UnmatchedRowBehaviorV1):
        raise ContractDefect("behavior must be an UnmatchedRowBehaviorV1")
    if (meaning, behavior) in _CONTRADICTORY_UNMATCHED_PAIRS:
        raise ContractDefect(
            f"logical unmatched-row meaning {meaning.value!r} contradicts guard-policy behavior "
            f"{behavior.value!r}: the executed join would compute a different feature than the "
            "declared one",
            code=UNMATCHED_ROW_CONTRADICTION)


def assess_realization_for_preview(
    conn,
    *,
    pinned_realization_revision_id: str,
    pinned_dependency_snapshot_id: str,
    environment_id: str,
    join_validation_policy_revision_id: str | None = None,
    join_validation_policy: JoinValidationPolicyRevisionV1 | None = None,
    logical_temporal_semantics: LogicalTemporalJoinSemanticsV1 | None = None,
) -> RealizationPreviewAssessmentV1:
    """R11's distinct typed preview assessment — a SEPARATE function from (and never a weakening
    of) ``revalidate_bridge_realization``, the production reader.

    Reads the EXACT pinned revision — never a current pointer, never latest. Revalidates the
    bridge lifecycle (withdrawn/revoked/REJECTED/stale links refuse) and the physical binding
    revisions. Unknown cardinality is accepted ONLY with a complete pinned guard policy and yields
    ``PROVISIONAL_WITH_GUARDS``; known 1:1/N:1 yields the full-preview verdict; known fan-out
    refuses (``ALLOCATION_POLICY_REQUIRED`` for the final grain). Missing mappings refuse.

    ``join_validation_policy`` is the pinned guard-policy REVISION itself; until B2's policy store
    lands the caller supplies the constructed revision and this function enforces that it matches
    the pinned id exactly (a pin that cannot be verified is a construction defect, not a verdict).
    ``logical_temporal_semantics``, when the caller compiles against a logical segment, triggers
    the unmatched-row coherence validation against the guard policy.

    THE A4→B2b SEAM: ``pinned_dependency_snapshot_id`` here still means the per-revision
    dependency-set hash (``brds_``, ``bridge_dependency_snapshot_id``). A4's persisted
    ``BridgeRealizationSnapshotV1`` (``brsnap_``, migration 1131) is what R11 ultimately wants
    this pin to name — the frozen batched read of the WHOLE considered set — and routing this
    assessment through that snapshot, INCLUDING the mandatory ``snapshot.complete`` check
    (nothing may treat a truncated snapshot as the complete considered set), is B2b's wiring
    change: re-meaning the parameter re-contracts this function and its suite, so it is noted
    here rather than smuggled in.
    """
    if (join_validation_policy is None) != (join_validation_policy_revision_id is None):
        raise ContractDefect(
            "join_validation_policy and join_validation_policy_revision_id come together: the id "
            "pins the exact revision and the revision is what the pin is verified against")
    if join_validation_policy is not None:
        if join_validation_policy.revision_id != join_validation_policy_revision_id:
            raise ContractDefect(
                f"the pinned guard-policy id {join_validation_policy_revision_id!r} does not name "
                f"the supplied policy revision {join_validation_policy.revision_id!r}: an "
                "assessment verifies the exact revision it pins, never a look-alike")
        if logical_temporal_semantics is not None:
            validate_unmatched_row_coherence(
                logical_temporal_semantics.unmatched_row_meaning,
                join_validation_policy.unmatched_row_behavior,
            )

    row = conn.execute(
        "SELECT realization_json FROM bridge_join_realization_revision "
        "WHERE realization_revision_id = %s",
        (pinned_realization_revision_id,),
    ).fetchone()
    if row is None:
        return RealizationPreviewAssessmentV1(
            pinned_realization_revision_id=pinned_realization_revision_id,
            pinned_dependency_snapshot_id=pinned_dependency_snapshot_id,
            environment_id=environment_id,
            join_validation_policy_revision_id=join_validation_policy_revision_id,
            verdict=PreviewAssessmentVerdictV1.REFUSED,
            reason_codes=(REALIZATION_REVISION_NOT_FOUND,),
            realization=None,
        )
    revision = realization_from_json(row[0])
    if revision.realization_revision_id != pinned_realization_revision_id:
        raise BridgeStoreCorruption(
            f"realization identity mismatch for {pinned_realization_revision_id}")

    dependencies = tuple(
        BridgeDependencyRefV1(kind, key, dependency_revision)
        for kind, key, dependency_revision in conn.execute(
            "SELECT dependency_kind, dependency_key, dependency_revision "
            "FROM bridge_realization_dependency WHERE realization_revision_id = %s "
            "ORDER BY dependency_kind, dependency_key, dependency_revision",
            (pinned_realization_revision_id,),
        ).fetchall()
    )

    reasons: set[str] = set()
    if (
        pinned_dependency_snapshot_id != revision.dependency_snapshot_id
        or revision.dependency_snapshot_id != bridge_dependency_snapshot_id(dependencies)
    ):
        reasons.add(DEPENDENCY_SNAPSHOT_MISMATCH)

    # Bridge lifecycle, re-read live: a withdrawn/revoked/rejected/stale link refuses its
    # realization here — display provenance may change without rekeying, availability may not.
    state = read_overlay_identifier_link_state(conn, revision.bridge_fact_key)
    if state.availability is not LinkAvailability.AVAILABLE:
        reasons.add(IDENTIFIER_LINK_NOT_AVAILABLE)
    if bridge_candidate_currentness(conn, revision.bridge_fact_key) is False:
        reasons.add(IDENTIFIER_LINK_CANDIDATE_WITHDRAWN)

    # The pin stays honest against the shared pointer WITHOUT reading through it: a published
    # pointer naming a newer revision supersedes this pin; a demoted pointer for this exact
    # revision withdraws it. No pointer row (the provisional normal case) is fine.
    pointer = conn.execute(
        "SELECT realization_revision_id, lifecycle FROM bridge_join_realization_current "
        "WHERE realization_id = %s",
        (revision.realization_id,),
    ).fetchone()
    if pointer is not None:
        pointed_revision_id, lifecycle = pointer
        if pointed_revision_id != pinned_realization_revision_id:
            reasons.add(REALIZATION_PIN_SUPERSEDED)
        elif lifecycle != RealizationLifecycle.ACTIVE.value:
            reasons.add(REALIZATION_DEMOTED)

    if not _binding_revision_is_stored(conn, revision.from_endpoint):
        reasons.add(SOURCE_BINDING_REVISION_MISSING)
    if not _binding_revision_is_stored(conn, revision.to_endpoint):
        reasons.add(SOURCE_BINDING_REVISION_MISSING)

    scope = revision.applicability_scope
    if environment_id != scope.environment:
        reasons.add(REALIZATION_ENVIRONMENT_MISMATCH)
    if FEATURE_GENERATION_PURPOSE not in scope.purposes:
        reasons.add(REALIZATION_PURPOSE_MISMATCH)

    if revision.has_unresolved_requirements:
        reasons.add(DIRECTIONAL_MAPPING_INCOMPLETE)

    provisional = False
    if not revision.cardinality.known:
        if join_validation_policy is None:
            reasons.add(DIRECTIONAL_CARDINALITY_UNPROVEN)
        else:
            provisional = True
    elif revision.cardinality.value in (Cardinality.ONE_TO_ONE, Cardinality.MANY_TO_ONE):
        pass  # the full-preview verdict, absent other refusals
    else:
        # Known fan-out (1:N or M:N in the traversal direction). A final-grain aggregate over a
        # fan-out multiplies contributions — the fan-out law's code; a policy explicitly scoped
        # away from the final grain still refuses preview, by its own name. Fail closed when no
        # policy says which question is being asked.
        final_grain = (
            join_validation_policy is None
            or join_validation_policy.applies_to_final_grain_aggregate
        )
        reasons.add(
            ALLOCATION_POLICY_REQUIRED if final_grain else DIRECTIONAL_CARDINALITY_FANS_OUT)

    if reasons:
        verdict = PreviewAssessmentVerdictV1.REFUSED
    elif provisional:
        verdict = PreviewAssessmentVerdictV1.PROVISIONAL_WITH_GUARDS
    else:
        verdict = PreviewAssessmentVerdictV1.FULL_PREVIEW
    return RealizationPreviewAssessmentV1(
        pinned_realization_revision_id=pinned_realization_revision_id,
        pinned_dependency_snapshot_id=pinned_dependency_snapshot_id,
        environment_id=environment_id,
        join_validation_policy_revision_id=join_validation_policy_revision_id,
        verdict=verdict,
        reason_codes=tuple(sorted(reasons)),
        realization=revision,
    )
