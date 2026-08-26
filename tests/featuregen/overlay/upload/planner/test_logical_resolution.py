"""A5 — logical resolution WITHOUT physical evidence.

The plan's execution-order item 5 asks for one property above all: an unrealized AI-proposed link
must yield a REAL ``LogicalFeaturePlanV2`` for the card and formula rungs, while G3's physical
refusal is preserved untouched for the physical rungs. Both halves are measured here on ONE
fixture and ONE planner run, so the pair can never drift:

* the contract compile still refuses ``physical_cardinality_unavailable`` (G3, unchanged);
* the SAME result resolves a complete R9 logical plan, with a stable golden digest.

Plus R14's honesty law (a crossing whose temporal meaning nobody declared carries that ABSENCE,
never a fabricated strategy), the composite carry-forward at the logical layer, the plan-variant
address that keeps two undeclared paths apart, and the ``grain_refs`` derivation B3 will wire into
the draft worker.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import (
    seed_verified_bridge as _seed_verified_bridge_fact,
)
from tests.featuregen.overlay.upload.planner.test_composite_bridge_crossings import (
    _seed_composite_bridge,
)

from featuregen.materialize.boundary_v2 import KnowledgeTimeBasisV2
from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    RequiredOperandV1,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import (
    PLANNER_VERSION,
    CatalogScopeV1,
    PathResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget, build_compiler_context
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    DrivingTimeRoleV1,
    IntervalBoundaryPolicyV1,
    LogicalFeaturePlanV2,
    LogicalPlanProvenanceV1,
    LogicalTemporalJoinSemanticsV1,
    StaticLinkMeaningV1,
    UnmatchedRowMeaningV1,
)
from featuregen.overlay.upload.planner.logical_resolution import (
    BRIDGE_ENDPOINT_TUPLES_MISSING,
    CANONICAL_DEFINITION_REVISION_MISSING,
    GOVERNED_SEMANTIC_REVISION_MISSING,
    LOGICAL_PATH_NOT_RESOLVED,
    TEMPORAL_JOIN_POLICY_MISSING,
    LogicalResolutionRefused,
    grain_refs_from_logical_plan,
    resolve_logical_plan,
    select_logical_plan_candidate,
    semantic_revisions_for_plan,
)
from featuregen.overlay.upload.planner.requests import plan_planning_request

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_BRIDGE = "bfk_a5_unrealized"


# ── the fixture: an AI-PROPOSED, UNREALIZED cross-catalog link ────────────────────────────────


def _scope(*catalogs: str) -> CatalogScopeV1:
    return CatalogScopeV1(
        scope_id="s_a5_logical", authorized_catalog_sources=tuple(catalogs),
        catalog_state_stamps=(), omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-08-24T00:00:00Z",
        catalog_consideration_truncated=False)


def _seed(db, source, catalog):
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})


def _two_catalogs(db, *, composite: bool = False, columns=("account_id", "source_system")):
    """`ops` carries the transaction-grain operands; `rev` carries the account-grain landing
    table. The ONLY route to the account grain is the governed link — which is DRAFT (AI-proposed,
    unreviewed) and carries NO directional realization, so the physical rungs refuse."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("ops", "transactions", "source_system", "text"), "source_system"),
        (CanonicalRow("ops", "transactions", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("rev", "accounts", "source_system", "text"), "source_system"),
    ])
    if composite:
        _seed_composite_bridge(db, _BRIDGE, left_source="ops", left_table="transactions",
                               right_source="rev", right_table="accounts", columns=columns)
    else:
        _seed_verified_bridge_fact(
            db, _BRIDGE, entity="account", left_source="ops",
            left_ref="public.transactions.account_id", right_source="rev",
            right_ref="public.accounts.account_id")


#: A real, shipped V2 recipe — its output/temporal/objective/formula specs are borrowed verbatim
#: so the fixture request is a shape the platform genuinely produces, not one invented here.
_DONOR_RECIPE_ID = "posted_transaction_average_amount"


def _request(**overrides) -> FeaturePlanningRequestV1:
    """An LLM-intent request whose operands the planner can bind: the transaction anchor and one
    monetary measure staged across the crossing (which is what makes G3 fire)."""
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    donor = v2_recipe_by_id(_DONOR_RECIPE_ID)
    assert donor is not None
    values = {
        "origin": "llm_intent",
        "source_definition_id": "a5_unrealized_link_probe",
        "source_revision": "3",
        "source_content_hash": "a5contenthash",
        "primary_objective": donor.primary_objective,
        "output": donor.output,
        "operands": (
            RequiredOperandV1(role="txn", concept="transaction_id", operand_class="entity_key",
                              join_role=str(JoinRole.SOURCE_ENTITY_KEY)),
            RequiredOperandV1(role="amount", concept="monetary_flow", operand_class="measure",
                              join_role=str(JoinRole.MEASURE)),
        ),
        "source_grain": "transaction",
        "output_grain": "account",
        "temporal": donor.temporal,
        "computation_kind": "deterministic_formula",
        "formula": donor.formula,
        "parameter_values": (("window", 30),),
    }
    values.update(overrides)
    return FeaturePlanningRequestV1(**values)


def _plan_result(db, request, *, compile_it: bool = True):
    scope = _scope("ops", "rev")
    compile_ctx = build_compiler_context(db, scope, (), _NOW) if compile_it else None
    return plan_planning_request(
        db, request=request, target_entity="account", scope=scope, roles=(), now=_NOW,
        compile_ctx=compile_ctx,
        budget=CompileBudget(remaining=64, deadline_monotonic=1e9, clock=lambda: 0.0))


def _semantics() -> LogicalTemporalJoinSemanticsV1:
    """R14's first-journey selection, DECLARED: customer state effective AT CUTOFF, knowledge AS
    KNOWN AT CUTOFF, latest-correction forbidden."""
    return LogicalTemporalJoinSemanticsV1(
        effective_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        knowledge_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        driving_time_role=DrivingTimeRoleV1.CUTOFF_PARAMETER,
        interval_boundary_policy=IntervalBoundaryPolicyV1.CLOSED_OPEN,
        unmatched_row_meaning=UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE,
        static_link_meaning=StaticLinkMeaningV1.APPLIES_FOR_ALL_TIME)


def _resolve(db, *, composite: bool = False, temporal: bool = True, **kwargs):
    request = _request()
    result = _plan_result(db, request)
    plan = select_logical_plan_candidate(result)
    assert plan is not None, "the fixture must assemble a source→target path"
    return result, plan, resolve_logical_plan(
        request=request, plan=plan,
        semantic_revisions=_stub_revisions(plan),
        temporal_semantics={_BRIDGE: _semantics()} if temporal else {},
        **kwargs)


def _stub_revisions(plan):
    """The governed semantic revision of each bound column. The fixture's catalogs carry no
    ``field_evidence`` rows, so the pins are stated explicitly here — which is also the point:
    the resolver never invents one (see the refusal test)."""
    return {(b.bound_catalog_source, b.bound_object_ref): f"ev_{b.need_role}"
            for b in plan.ingredient_bindings}


# ── 1. G3 IS PRESERVED, and the logical lane resolves anyway ──────────────────────────────────


def test_the_physical_rung_still_refuses_an_unrealized_link(db):
    """The G3 regression pin: the governed bridge hop carries no directional realization, so the
    staged measure's cardinality is unavailable and the CONTRACT refuses — exactly as before A5.
    The logical lane is additive; it did not soften this."""
    _two_catalogs(db)
    result = _plan_result(db, _request())
    (cross,) = [p for p in result.candidate_plans
                if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved]
    assert cross.bridge_count == 1
    assert cross.contract_primary_reason_code is ReasonCode.physical_cardinality_unavailable
    assert all(s.bridge_realization_revision is None for s in cross.path_segments)


def test_an_unrealized_proposed_link_still_resolves_a_complete_logical_plan(db):
    """THE A5 property. The same run whose contract just refused yields a complete R9 plan: every
    identity-bearing field populated, from facts that are all logical."""
    _two_catalogs(db)
    result, plan, resolution = _resolve(db)
    # the plan this resolution is about is the one the CONTRACT refused — not a second run
    assert plan.contract_primary_reason_code is ReasonCode.physical_cardinality_unavailable

    logical = resolution.plan
    assert isinstance(logical, LogicalFeaturePlanV2)
    assert logical.canonical_definition_content_hash == "a5contenthash"
    assert logical.canonical_definition_revision_id == "3"
    assert logical.operation == "deterministic_formula"
    assert {b.role for b in logical.operand_bindings} == {"txn", "amount"}
    assert all(b.governed_semantic_revision_id for b in logical.operand_bindings)
    assert logical.output_grain_key_refs == ("rev::public.accounts.account_id",)
    assert logical.selected_parameters == (("window", 30),)
    assert len(logical.relationship_path) == 1
    assert resolution.logical_digest and len(resolution.logical_digest) == 64
    assert resolution.is_complete
    del result


def test_the_logical_resolution_never_consults_the_contract_axis(db):
    """Physical evidence is not read AT ALL: the identical logical identity comes out of a run
    that compiled a contract (and refused) and a run that never compiled one."""
    _two_catalogs(db)
    request = _request()
    digests = []
    for compile_it in (True, False):
        result = _plan_result(db, request, compile_it=compile_it)
        plan = select_logical_plan_candidate(result)
        assert plan is not None
        digests.append(resolve_logical_plan(
            request=request, plan=plan, semantic_revisions=_stub_revisions(plan),
            temporal_semantics={_BRIDGE: _semantics()}).logical_digest)
    assert digests[0] == digests[1]


def test_the_selection_is_the_path_verdict_never_the_contract_verdict(db):
    """``governed_lens`` selects on ``contract_result_status is resolved`` — a PHYSICAL verdict,
    which is why an unrealized link yields no option. A5 selects on the PATH."""
    _two_catalogs(db)
    from featuregen.overlay.upload.contract.governed_lens import _selected_resolved_plan

    result = _plan_result(db, _request())
    assert _selected_resolved_plan(result) is None          # the physical lane: nothing
    assert select_logical_plan_candidate(result) is not None  # the logical lane: a plan


# ── 2. the GOLDEN digest pin ──────────────────────────────────────────────────────────────────

# Captured once from the fixture below. It moves only when the MEANING moves — a new operand
# binding, a different grain, a changed parameter, a different crossing or a different declared
# temporal semantics. It is deliberately independent of PLANNER_VERSION and of every physical
# identity: `logical_digest` hashes R9 material only.
_PINNED_LOGICAL_DIGEST = "aa139314ae9b3e490e029fd207fcde41be7d8e30d4c1b1ae0e22968eb0e07fa0"
_PINNED_PLAN_VARIANT_ADDRESS = "194993ed1244dc374694a2cd96fa235948f37b9bd6058307057e8e8f58105554"


def test_the_logical_digest_and_variant_address_are_pinned(db):
    _two_catalogs(db)
    _result, _plan, resolution = _resolve(db)
    assert resolution.logical_digest == _PINNED_LOGICAL_DIGEST
    assert resolution.plan_variant_address == _PINNED_PLAN_VARIANT_ADDRESS


def test_provenance_never_moves_the_logical_digest(db):
    """R9's staleness law at the resolution layer: hypothesis wording, a chooser revision, menu
    content and display text are carried and never hashed."""
    _two_catalogs(db)
    _r, _p, plain = _resolve(db)
    _r2, _p2, dressed = _resolve(db, provenance=LogicalPlanProvenanceV1(
        hypothesis_text="newly onboarded customers whose outgoing payments spiked",
        planning_request_hash="prh", chooser_revision_id="cr", menu_content_hash="mc",
        display_text="Payment spike"))
    assert plain.logical_digest == dressed.logical_digest
    assert plain.plan_variant_address == dressed.plan_variant_address


# ── 3. R14 — the honest absence ───────────────────────────────────────────────────────────────


def test_a_crossing_with_no_declared_temporal_meaning_carries_the_absence(db):
    """The consuming layer mints ``TEMPORAL_JOIN_POLICY_MISSING`` from THIS, and the resolver
    fabricates nothing: the crossing keeps its endpoints on the complete path with
    ``temporal_semantics=None``, and never enters the digest-bearing relationship path."""
    _two_catalogs(db)
    _result, _plan, resolution = _resolve(db, temporal=False)
    assert not resolution.is_complete
    assert [a.code for a in resolution.absences] == [TEMPORAL_JOIN_POLICY_MISSING]
    assert resolution.absences[0].subject == _BRIDGE
    # the crossing is STILL on the complete ordered path, with its endpoints …
    assert len(resolution.path) == 1
    assert resolution.path[0].temporal_semantics is None
    assert resolution.path[0].left_endpoint_refs == ("ops::public.transactions.account_id",)
    # … and the plan is REAL (the owner's matrix keeps FORMULA available for a missing temporal
    # policy), it simply declares no relationship meaning it was not given.
    assert resolution.plan.relationship_path == ()
    assert resolution.logical_digest


def test_two_undeclared_paths_stay_apart_on_the_variant_address(db):
    """The sharp edge of dropping an undeclared crossing from the digest, closed structurally:
    two plans differing only in an UNDECLARED crossing share a ``logical_digest``, and the
    plan-variant address — the plan's §10/C3 ``served_plan_variant_id`` material — separates
    them."""
    _two_catalogs(db)
    request = _request()
    result = _plan_result(db, request, compile_it=False)
    plan = select_logical_plan_candidate(result)
    assert plan is not None
    import dataclasses

    from featuregen.overlay.upload.planner.contracts import SegmentKind

    other_segments = tuple(
        dataclasses.replace(s, bridge_fact_key="bfk_other_route",
                            bridge_to_member_refs=("public.accounts.alt_account_id",))
        if s.segment_kind is SegmentKind.governed_bridge else s
        for s in plan.path_segments)
    other = dataclasses.replace(plan, path_segments=other_segments)

    one = resolve_logical_plan(request=request, plan=plan,
                               semantic_revisions=_stub_revisions(plan), temporal_semantics={})
    two = resolve_logical_plan(request=request, plan=other,
                               semantic_revisions=_stub_revisions(other), temporal_semantics={})
    assert one.logical_digest == two.logical_digest          # the honest collision …
    assert one.plan_variant_address != two.plan_variant_address   # … and what separates them


# ── 4. the composite carry-forward, at the LOGICAL layer ──────────────────────────────────────


def test_a_composite_link_resolves_a_two_pair_logical_segment(db):
    """The probe-equivalent pin, carried all the way to R9's relationship path: a two-member link
    yields a crossing with BOTH ordered pairs — never a single-pair segment."""
    _two_catalogs(db, composite=True)
    _result, _plan, resolution = _resolve(db)
    (segment,) = resolution.path
    assert segment.left_endpoint_refs == ("ops::public.transactions.account_id",
                                          "ops::public.transactions.source_system")
    assert segment.right_endpoint_refs == ("rev::public.accounts.account_id",
                                           "rev::public.accounts.source_system")
    (logical_segment,) = resolution.plan.relationship_path
    assert logical_segment.left_endpoint_refs == segment.left_endpoint_refs
    assert logical_segment.right_endpoint_refs == segment.right_endpoint_refs
    assert len(logical_segment.left_endpoint_refs) == 2


def test_a_composite_link_never_shares_the_digest_of_its_first_pair_alone(db):
    """The identity half of the same law: the composite crossing's meaning is NOT the meaning of
    the single-column join the pre-A5 assembler would have planned."""
    _two_catalogs(db, composite=True)
    _r, _p, composite = _resolve(db)
    import dataclasses

    collapsed_path = tuple(
        dataclasses.replace(
            s, bridge_from_member_refs=s.bridge_from_member_refs[:1],
            bridge_to_member_refs=s.bridge_to_member_refs[:1])
        if s.bridge_from_member_refs else s
        for s in _p.path_segments)
    request = _request()
    collapsed = resolve_logical_plan(
        request=request, plan=dataclasses.replace(_p, path_segments=collapsed_path),
        semantic_revisions=_stub_revisions(_p),
        temporal_semantics={_BRIDGE: _semantics()})
    assert composite.logical_digest != collapsed.logical_digest


def test_a_bridge_segment_with_no_endpoint_tuples_is_refused_not_degraded(db):
    """Never inferred: a crossing that does not carry its ordered key is refused, because the
    only way to 'recover' it is the first-member collapse A5 exists to stop."""
    _two_catalogs(db)
    request = _request()
    result = _plan_result(db, request, compile_it=False)
    plan = select_logical_plan_candidate(result)
    assert plan is not None
    import dataclasses

    from featuregen.overlay.upload.planner.contracts import SegmentKind

    stripped = dataclasses.replace(plan, path_segments=tuple(
        dataclasses.replace(s, bridge_from_member_refs=(), bridge_to_member_refs=())
        if s.segment_kind is SegmentKind.governed_bridge else s
        for s in plan.path_segments))
    with pytest.raises(LogicalResolutionRefused) as excinfo:
        resolve_logical_plan(request=request, plan=stripped,
                             semantic_revisions=_stub_revisions(stripped),
                             temporal_semantics={_BRIDGE: _semantics()})
    assert excinfo.value.code == BRIDGE_ENDPOINT_TUPLES_MISSING


# ── 5. refusals: nothing is approximated ──────────────────────────────────────────────────────


def test_an_operand_with_no_governed_semantic_revision_is_refused(db):
    _two_catalogs(db)
    request = _request()
    result = _plan_result(db, request, compile_it=False)
    plan = select_logical_plan_candidate(result)
    assert plan is not None
    with pytest.raises(LogicalResolutionRefused) as excinfo:
        resolve_logical_plan(request=request, plan=plan, semantic_revisions={},
                             temporal_semantics={_BRIDGE: _semantics()})
    assert excinfo.value.code == GOVERNED_SEMANTIC_REVISION_MISSING


def test_a_blank_canonical_definition_revision_is_refused_not_substituted(db):
    """``source_revision`` is a REQUIRED field of the request and every production builder
    populates it, so a blank one is a caller defect. Refused under its own name — substituting the
    content hash would give "which revision of the definition" a second address."""
    _two_catalogs(db)
    request = _request()
    result = _plan_result(db, request, compile_it=False)
    plan = select_logical_plan_candidate(result)
    assert plan is not None
    import dataclasses

    blank = dataclasses.replace(request, source_revision="   ")
    with pytest.raises(LogicalResolutionRefused) as excinfo:
        resolve_logical_plan(request=blank, plan=plan,
                             semantic_revisions=_stub_revisions(plan),
                             temporal_semantics={_BRIDGE: _semantics()})
    assert excinfo.value.code == CANONICAL_DEFINITION_REVISION_MISSING


def test_the_temporal_refusal_code_is_the_registered_one_not_a_second_spelling(db):
    """A1 registered ``TEMPORAL_JOIN_POLICY_MISSING`` in the reason vocabulary three commits
    earlier. This module IMPORTS and re-exports it — the `ALLOCATION_POLICY_REQUIRED` precedent —
    so the two can never drift into two spellings of one refusal."""
    del db
    from featuregen.materialize.action_dispositions import ACTION_DISPOSITIONS
    from featuregen.overlay.upload import semantic_eligibility_reasons as vocabulary

    assert TEMPORAL_JOIN_POLICY_MISSING is vocabulary.TEMPORAL_JOIN_POLICY_MISSING
    assert any(reason == TEMPORAL_JOIN_POLICY_MISSING for reason, _action in ACTION_DISPOSITIONS)


def test_a_path_that_did_not_resolve_has_no_logical_meaning(db):
    _two_catalogs(db)
    request = _request()
    result = _plan_result(db, request, compile_it=False)
    tier1 = next(p for p in result.candidate_plans
                 if p.path_resolution_status is PathResolutionStatus.ingredient_binding_only)
    with pytest.raises(LogicalResolutionRefused) as excinfo:
        resolve_logical_plan(request=request, plan=tier1, semantic_revisions={},
                             temporal_semantics={})
    assert excinfo.value.code == LOGICAL_PATH_NOT_RESOLVED


def test_the_governed_semantic_revision_reader_is_the_platforms_own(db):
    """``semantic_revisions_for_plan`` reads the SAME governed authority the lens's role bindings
    read. With no active concept evidence the mapping is EMPTY — an absent key, never a blank
    revision that would bind an operand to a meaning nobody governs."""
    _two_catalogs(db)
    result = _plan_result(db, _request(), compile_it=False)
    plan = select_logical_plan_candidate(result)
    assert plan is not None
    assert semantic_revisions_for_plan(db, plan) == {}


# ── 6. grain_refs — the derivation B3 wires into the draft worker ──────────────────────────────


def test_grain_refs_from_logical_plan(db):
    """The draft worker's ``grain_refs`` shape — ``(catalog, schema.table.column)`` — derived from
    the plan's ORDERED output grain. A5 builds and tests the derivation; B3 wires the worker."""
    _two_catalogs(db)
    _r, _p, resolution = _resolve(db)
    assert grain_refs_from_logical_plan(resolution.plan) == (
        ("rev", "public.accounts.account_id"),)


def test_grain_refs_preserve_order_and_never_sort():
    """Grain ORDER is identity, so the derivation carries the plan's order verbatim."""
    from featuregen.overlay.upload.planner.logical_plan_v2 import LogicalOperandBindingV1

    plan = LogicalFeaturePlanV2(
        canonical_definition_content_hash="h", canonical_definition_revision_id="1",
        operation="conceptual_pattern",
        operand_bindings=(LogicalOperandBindingV1("m", "ops::public.t.c", "rev1"),),
        output_grain_key_refs=("rev::public.accounts.zone_id",
                               "rev::public.accounts.account_id"),
        selected_parameters=(), relationship_path=(), formula_policy_identities=(),
        provenance=LogicalPlanProvenanceV1())
    assert grain_refs_from_logical_plan(plan) == (
        ("rev", "public.accounts.zone_id"), ("rev", "public.accounts.account_id"))


# ── 7. the DECLARED identity change ───────────────────────────────────────────────────────────


def test_the_planner_version_bumped_with_the_composite_crossing_change():
    """A5 changes WHICH paths the frontier enumerates and WHAT a crossing says, so the planner
    version moves once, deliberately. The pre-A5 value is named so a silent revert is loud."""
    assert PLANNER_VERSION == "3b3a.2.0.0"
    assert PLANNER_VERSION != "3b3a.1.0.0"
