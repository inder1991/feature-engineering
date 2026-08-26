"""A5 — the probe-backed carry-forward: a composite link NEVER emits a single-pair crossing.

A prior reviewer's empirical probe proved the pre-A5 assembler crossed a composite governed link
on its FIRST member pair alone: ``rollup_bridges`` / ``reposition_bridges`` walked the current
table COLUMN by column and matched ``ActiveBridgeV1``'s thin ``left_object_ref`` /
``right_object_ref``, which A2 documents as the ``members[0]`` compat flattening. A
``source_system + customer_number`` key therefore planned as a ONE-column join — a different,
weaker join than the one the link declares — and which member won depended on the declared order.

These tests pin the fix from both ends:

* the SEGMENT a composite link emits carries BOTH ordered pairs, and the physical realization
  matcher compares them as ordered tuples rather than by membership;
* a single-member link is byte-for-byte what it always was (the regression pin), so the composite
  law costs the overwhelmingly common shape nothing.
"""
from __future__ import annotations

from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload._bridge_fixtures import (
    seed_verified_bridge as _seed_verified_bridge_fact,
)

from featuregen.overlay.upload.bridge_assessment import (
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
    NamespaceVerdict,
    PopulationRelation,
    TypeBasis,
)
from featuregen.overlay.upload.bridge_store import record_candidate_assessment
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.assembly import (
    _Position,
    reposition_bridges,
    rollup_bridges,
    semantic_rollup_paths,
)
from featuregen.overlay.upload.planner.contracts import CatalogScopeV1, SegmentKind


def _scope(*catalogs: str) -> CatalogScopeV1:
    return CatalogScopeV1(
        scope_id="s_a5", authorized_catalog_sources=tuple(catalogs), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-08-24T00:00:00Z",
        catalog_consideration_truncated=False)


def _seed(db, source, catalog):
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})


def _txn_to_account_hop():
    paths, _ = semantic_rollup_paths("transaction", "account")
    return paths[0].hops[0]


def _endpoint(source: str, table: str, columns: tuple[str, ...]) -> IdentifierEndpointV1:
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=tuple(
            IdentifierColumnMemberV1(
                normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED)
            for column in columns),
        entity_id="account")


def _seed_composite_bridge(db, fact_key: str, *, left_source: str, left_table: str,
                           right_source: str, right_table: str,
                           columns: tuple[str, ...]) -> None:
    """An AVAILABLE (DRAFT — AI-proposed, unreviewed) link whose endpoints are COMPOSITE.

    The overlay fact value carries one column per side (the governed fact's schema), so the
    composite shape lives where the platform really keeps it: the candidate ASSESSMENT, which is
    what ``available_identifier_links`` — and therefore ``active_bridges`` — reads."""
    govern_bridge_fact(
        db, fact_key, entity="account",
        left_source=left_source, left_ref=f"public.{left_table}.{columns[0]}",
        right_source=right_source, right_ref=f"public.{right_table}.{columns[0]}",
        status="DRAFT")
    record_candidate_assessment(db, IdentifierLinkAssessmentV1(
        left_endpoint=_endpoint(left_source, left_table, columns),
        right_endpoint=_endpoint(right_source, right_table, columns),
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1",
        bridge_fact_key=fact_key), expected_pointer_version=0)


def _composite_split(db, *, extra_ops=(), extra_rev=()):
    """`ops` holds the transaction-grain table carrying BOTH members of the composite key; `rev`
    holds the account-grain landing table carrying both far members."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("ops", "transactions", "source_system", "text"), "source_system"),
        *extra_ops,
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("rev", "accounts", "source_system", "text"), "source_system"),
        *extra_rev,
    ])


# ── 1. THE PROBE-EQUIVALENT PIN ───────────────────────────────────────────────────────────────


def test_a_composite_link_crosses_with_both_ordered_pairs_never_the_first_alone(db):
    """The exact pre-A5 defect: ``account_id`` is declared FIRST, so the thin ``members[0]``
    flattening made the old per-column walk match it and emit a ONE-pair crossing, silently
    dropping ``source_system``. The crossing now carries the WHOLE declared key."""
    _composite_split(db)
    _seed_composite_bridge(db, "bfk_composite", left_source="ops", left_table="transactions",
                           right_source="rev", right_table="accounts",
                           columns=("account_id", "source_system"))
    moves = rollup_bridges(db, _Position("transaction", "ops", "public.transactions"),
                           _txn_to_account_hop(), _scope("ops", "rev"))
    assert len(moves) == 1
    (_rollup, bridge) = moves[0].segments
    assert bridge.segment_kind is SegmentKind.governed_bridge
    assert bridge.bridge_from_member_refs == ("public.transactions.account_id",
                                              "public.transactions.source_system")
    assert bridge.bridge_to_member_refs == ("public.accounts.account_id",
                                            "public.accounts.source_system")
    # the pin, stated as the probe stated it: the crossing is never a single pair for a two-member
    # link — and the pairs correspond positionally, in DECLARED order.
    assert len(bridge.bridge_from_member_refs) == len(bridge.bridge_to_member_refs) == 2
    # the thin fields stay the documented first-member compat surface
    assert bridge.bridge_from_object_ref == "public.transactions.account_id"
    assert bridge.bridge_to_object_ref == "public.accounts.account_id"


def test_a_composite_link_whose_first_member_is_not_the_key_still_crosses_whole(db):
    """The other half of the order-dependence the probe exposed: with ``source_system`` declared
    FIRST the pre-A5 walk matched no entity-keyed anchor at all and the link was invisible. The
    crossing is now enumerated — as ONE composite join, in declared (non-lexical) order."""
    _composite_split(db)
    _seed_composite_bridge(db, "bfk_composite_rev", left_source="ops", left_table="transactions",
                           right_source="rev", right_table="accounts",
                           columns=("source_system", "account_id"))
    moves = rollup_bridges(db, _Position("transaction", "ops", "public.transactions"),
                           _txn_to_account_hop(), _scope("ops", "rev"))
    assert len(moves) == 1
    bridge = moves[0].segments[1]
    assert bridge.bridge_from_member_refs == ("public.transactions.source_system",
                                              "public.transactions.account_id")
    assert bridge.bridge_to_member_refs == ("public.accounts.source_system",
                                            "public.accounts.account_id")


def test_a_composite_link_missing_a_member_on_the_current_table_never_crosses(db):
    """Fail-closed: the WHOLE key must live here. A near endpoint naming a column this table does
    not expose is not crossable from this position — never crossable on the members it does have,
    which is the single-pair join by another name."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
    ])   # NO source_system column on the near table
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("rev", "accounts", "source_system", "text"), "source_system"),
    ])
    _seed_composite_bridge(db, "bfk_partial", left_source="ops", left_table="transactions",
                           right_source="rev", right_table="accounts",
                           columns=("account_id", "source_system"))
    assert rollup_bridges(db, _Position("transaction", "ops", "public.transactions"),
                          _txn_to_account_hop(), _scope("ops", "rev")) == ()


def test_a_composite_reposition_carries_both_pairs(db):
    """The same law on the same-entity reposition crossing."""
    _seed(db, "core", [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "source_system", "text"), "source_system"),
    ])
    _seed(db, "far", [
        (CanonicalRow("far", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("far", "accounts", "source_system", "text"), "source_system"),
    ])
    _seed_composite_bridge(db, "bfk_repo", left_source="core", left_table="accounts",
                           right_source="far", right_table="accounts",
                           columns=("account_id", "source_system"))
    moves = reposition_bridges(db, _Position("account", "core", "public.accounts"),
                               _scope("core", "far"))
    assert len(moves) == 1
    (bridge,) = moves[0].segments
    assert bridge.bridge_from_member_refs == ("public.accounts.account_id",
                                             "public.accounts.source_system")
    assert bridge.bridge_to_member_refs == ("public.accounts.account_id",
                                            "public.accounts.source_system")


# ── 2. THE SINGLE-MEMBER REGRESSION PIN ───────────────────────────────────────────────────────


def test_a_single_member_link_is_exactly_what_it_always_was(db):
    """The overwhelmingly common shape pays nothing for the composite law: the same one crossing,
    the same thin endpoint addresses, with a one-member tuple beside them."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    _seed_verified_bridge_fact(db, "bfk_single", entity="account", left_source="ops",
                               left_ref="public.transactions.account_id", right_source="rev",
                               right_ref="public.accounts.account_id")
    moves = rollup_bridges(db, _Position("transaction", "ops", "public.transactions"),
                           _txn_to_account_hop(), _scope("ops", "rev"))
    assert len(moves) == 1
    move = moves[0]
    assert move.bridge_fact_key == "bfk_single"
    assert move.next_position == _Position("account", "rev", "public.accounts")
    (rollup, bridge) = move.segments
    assert rollup.segment_kind is SegmentKind.semantic_rollup
    assert bridge.bridge_from_catalog_source == "ops"
    assert bridge.bridge_from_object_ref == "public.transactions.account_id"
    assert bridge.bridge_to_catalog_source == "rev"
    assert bridge.bridge_to_object_ref == "public.accounts.account_id"
    assert bridge.bridge_from_member_refs == ("public.transactions.account_id",)
    assert bridge.bridge_to_member_refs == ("public.accounts.account_id",)


# ── 3. THE READ SET SEES EVERY MEMBER — or safety is fail-OPEN ────────────────────────────────
#
# `build_physical_read_set` states the law itself: "each bridge segment's BOTH endpoints in FULL".
# Once path assembly emits a two-column join, inventorying only the thin `bridge_*_object_ref`
# fields breaks it — and `stage_safety` can only inspect columns that are IN the set, so a leakage
# anchor or protected attribute sitting in a composite key's SECOND member would be joined on and
# reported SAFE. These two tests are the pin: the whole key is inventoried, and a leakage anchor
# hiding in a second member is SEEN.


def _txn_template():
    from featuregen.overlay.upload.binding_roles import JoinRole
    from featuregen.overlay.upload.templates import Need, Template

    return Template(id="t_a5_readset", family="f", intent="i",
                    needs=(Need(role="txn", concept="transaction_id",
                                join_role=JoinRole.SOURCE_ENTITY_KEY),),
                    params={}, aggregation="sum", additivity="additive", explain="M",
                    use_cases=(), pit="trailing", source_entity_need_role="txn")


def _cross_plan(db):
    from datetime import UTC, datetime

    from featuregen.overlay.upload.planner.contracts import PathResolutionStatus
    from featuregen.overlay.upload.planner.declarations import build_compiler_context
    from featuregen.overlay.upload.planner.plan import plan_bindings

    now = datetime(2026, 8, 26, tzinfo=UTC)
    scope = _scope("ops", "rev")
    result = plan_bindings(db, template=_txn_template(), target_entity="account", scope=scope,
                           roles=(), now=now, compile_ctx=build_compiler_context(db, scope, (), now))
    (cross,) = [p for p in result.candidate_plans
                if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved]
    return cross


def test_a_composite_crossings_read_set_carries_every_member_on_both_sides(db):
    _composite_split(db)
    _seed_composite_bridge(db, "bfk_readset", left_source="ops", left_table="transactions",
                           right_source="rev", right_table="accounts",
                           columns=("account_id", "source_system"))
    cross = _cross_plan(db)
    assert cross.physical_read_set is not None
    from featuregen.overlay.upload.planner.contracts import ColumnRole

    bridge_keys = {(c.catalog_source, c.object_ref) for c in cross.physical_read_set.columns
                   if ColumnRole.bridge_key in c.roles}
    # BOTH members, on BOTH sides — the second member is exactly what the thin fields dropped
    assert bridge_keys == {
        ("ops", "public.transactions.account_id"),
        ("ops", "public.transactions.source_system"),
        ("rev", "public.accounts.account_id"),
        ("rev", "public.accounts.source_system"),
    }


def test_a_leakage_anchor_in_a_second_key_member_is_seen_not_reported_safe(db):
    """The fail-open this closes: a column the plan genuinely JOINS ON, invisible to safety merely
    because it was the second member of a composite key. ``fraud_flag`` is a governed leakage
    anchor; joining on it leaks exactly as much as reading it as an ingredient."""
    _composite_split(
        db,
        extra_ops=[(CanonicalRow("ops", "transactions", "fraud_flag", "boolean"), "fraud_flag")],
        extra_rev=[(CanonicalRow("rev", "accounts", "fraud_flag", "boolean"), "fraud_flag")])
    _seed_composite_bridge(db, "bfk_leak", left_source="ops", left_table="transactions",
                           right_source="rev", right_table="accounts",
                           columns=("account_id", "fraud_flag"))
    cross = _cross_plan(db)
    from featuregen.overlay.upload.planner.contracts import BindingSafety, ReasonCode
    from featuregen.overlay.upload.planner.declarations import stage_safety

    assert cross.physical_read_set is not None
    anchor = next(c for c in cross.physical_read_set.columns
                  if (c.catalog_source, c.object_ref) == ("ops", "public.transactions.fraud_flag"))
    assert anchor.safety is BindingSafety.unsafe
    assert ReasonCode.leakage_anchor_read in anchor.reason_codes
    verdict, codes = stage_safety(cross.physical_read_set)
    assert verdict is BindingSafety.unsafe
    assert ReasonCode.leakage_anchor_read in codes
    # …and it reaches the compiled contract rather than dying in the read set
    assert ReasonCode.leakage_anchor_read in cross.contract_reason_codes


# ── 4. THE PHYSICAL-SIDE TWIN: exact ordered realization matching ─────────────────────────────


def _member(source: str, table: str, column: str) -> IdentifierColumnMemberV1:
    return IdentifierColumnMemberV1(
        normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED)


def _bound_endpoint(source: str, table: str, columns: tuple[str, ...]) -> IdentifierEndpointV1:
    """An EXECUTABLE endpoint — a directional realization refuses one with no resolved physical
    binding, so the matcher test builds the same shape the realization store really holds."""
    from featuregen.overlay.upload.bridge_assessment import (
        PhysicalDatasetBindingV1,
        PhysicalObjectIdentityV1,
    )

    binding = PhysicalDatasetBindingV1(
        binding_id=f"binding-{source}-{table}",
        catalog_logical_ref=normalize_ref(source, "public", table),
        connection_id="conn-hive",
        identity=PhysicalObjectIdentityV1(
            catalog_source=source, database="hive-primary", schema="banking", table=table,
            object_kind="table"))
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=tuple(_member(source, table, column) for column in columns),
        entity_id="account",
        physical_binding=binding,
        binding_revision_id=binding.binding_revision_id)


def _revision(*, from_columns: tuple[str, ...], to_columns: tuple[str, ...],
              pair_with: tuple[str, ...] | None = None):
    """``pair_with`` re-pairs the SAME members without reordering either endpoint's member tuple —
    the cross-paired realization (``a↔y, b↔x`` where the segment declares ``a↔x, b↔y``). It is the
    case a members-only comparison cannot see: the revision validates pairs against members as
    SETS, so both realizations have identical member tuples and describe different joins."""
    from featuregen.overlay.upload.bridge_realization import (
        BridgeJoinRealizationRevisionV1,
        CardinalityBasis,
        ColumnPairV1,
        DirectionalCardinalityVerdictV1,
        ExecutionTier,
        RealizationApplicabilityScopeV1,
    )

    return BridgeJoinRealizationRevisionV1(
        bridge_fact_key="bfk_composite",
        from_endpoint=_bound_endpoint("ops", "transactions", from_columns),
        to_endpoint=_bound_endpoint("rev", "accounts", to_columns),
        column_pairs=tuple(
            ColumnPairV1(normalize_ref("ops", "public", "transactions", f),
                         normalize_ref("rev", "public", "accounts", t))
            for f, t in zip(from_columns, pair_with or to_columns, strict=True)),
        predicates=(),
        applicability_scope=RealizationApplicabilityScopeV1(
            scope_id="a5-scope", execution_tier=ExecutionTier.SANDBOX,
            purposes=("feature_generation",), environment="pilot"),
        cardinality=DirectionalCardinalityVerdictV1.unknown(),
        cardinality_basis=CardinalityBasis.NONE,
        evidence_refs=(),
        dependency_snapshot_id="dep-a5",
        derivation_version="derive-v1",
        admission_policy_version="admit-v1")


def _current(revision):
    from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
    from featuregen.overlay.upload.bridge_realization import (
        BridgeRealizationCurrentV1,
        RealizationLifecycle,
        SafetyStatus,
    )

    return BridgeRealizationCurrentV1(
        realization_id=revision.realization_id,
        realization_revision_id=revision.realization_revision_id,
        safety_status=SafetyStatus.UNASSESSED,
        review_status=LinkReviewStatus.UNREVIEWED,
        lifecycle=RealizationLifecycle.ACTIVE,
        pointer_version=1)


def _realization(revision):
    from featuregen.overlay.upload.bridge_store import CurrentBridgeRealizationV1

    return CurrentBridgeRealizationV1(
        revision=revision, current=_current(revision), dependencies=())


def test_the_realization_matcher_compares_ordered_tuples_not_membership(db):
    """The physical-side twin of the discovery defect: the pre-A5 matcher asked whether the
    segment's single from-ref was AMONG the revision's members, so a WIDER composite realization
    bound to a one-column crossing. Ordered equality, or no match."""
    del db
    from dataclasses import replace

    from featuregen.overlay.upload.planner.assembly import _realization_matches_segment
    from featuregen.overlay.upload.planner.contracts import BindingPathSegmentV1

    composite_segment = BindingPathSegmentV1(
        segment_kind=SegmentKind.governed_bridge, catalog_source="rev",
        bridge_fact_key="bfk_composite",
        bridge_from_catalog_source="ops", bridge_from_object_ref="public.transactions.account_id",
        bridge_to_catalog_source="rev", bridge_to_object_ref="public.accounts.account_id",
        bridge_from_member_refs=("public.transactions.account_id",
                                 "public.transactions.source_system"),
        bridge_to_member_refs=("public.accounts.account_id", "public.accounts.source_system"))
    composite = _realization(_revision(from_columns=("account_id", "source_system"),
                                       to_columns=("account_id", "source_system")))

    assert _realization_matches_segment(composite, composite_segment)
    # a narrower realization does not realize the composite crossing …
    narrow = _realization(_revision(from_columns=("account_id",), to_columns=("account_id",)))
    assert not _realization_matches_segment(narrow, composite_segment)
    # … and the WIDE realization no longer binds itself to a one-column crossing (the pre-A5
    # membership test accepted exactly this).
    single_segment = replace(composite_segment,
                             bridge_from_member_refs=("public.transactions.account_id",),
                             bridge_to_member_refs=("public.accounts.account_id",))
    assert not _realization_matches_segment(composite, single_segment)
    assert _realization_matches_segment(narrow, single_segment)
    # order is identity: the same two members in the other declared order is a different key
    reordered = _realization(_revision(from_columns=("source_system", "account_id"),
                                       to_columns=("source_system", "account_id")))
    assert not _realization_matches_segment(reordered, composite_segment)
    # THE CASE A MEMBERS-ONLY COMPARISON CANNOT SEE: the same members on both sides, in the same
    # tuple order, CROSS-PAIRED (account_id↔source_system, source_system↔account_id). The
    # revision's own validation checks pairs against members as SETS, so its member tuples are
    # byte-identical to `composite`'s — only `column_pairs` distinguishes the two joins.
    cross_paired = _realization(_revision(
        from_columns=("account_id", "source_system"), to_columns=("account_id", "source_system"),
        pair_with=("source_system", "account_id")))
    assert (tuple(m.logical_column_ref for m in cross_paired.revision.to_endpoint.members)
            == tuple(m.logical_column_ref for m in composite.revision.to_endpoint.members)), (
        "the control: this realization differs from `composite` ONLY in its column_pairs")
    assert not _realization_matches_segment(cross_paired, composite_segment)
