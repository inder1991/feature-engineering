"""Task 2A — the GOVERNED CROSS-CATALOG candidate carries its directional realization.

The same-catalog gauntlet retains the ordered join path it selected; the cross-catalog planner must
retain the equivalent — the ordered path segments of the compiled plan, in the direction they cross,
with the exact realization each leg used. A V2 explainer may never re-run the planner to say why a
governed option exists (freeze 0F-7, plan rule 15).
"""
from __future__ import annotations

from tests.featuregen.overlay.upload.planner.test_plan import _NOW, _txn_template
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

from featuregen.overlay.upload.contract.gate1 import _governed_cross_catalog_options
from featuregen.overlay.upload.grounding_trace import (
    CROSS_CATALOG_CONTRACT,
    CROSS_CATALOG_PATH_SEGMENT,
    READ_SCOPE,
    recompute_trace_content_hash,
    trace_completeness_gaps,
)
from featuregen.overlay.upload.planner import contracts as c
from featuregen.overlay.upload.planner.plan_envelope import (
    plan_dependency_pins,
    plan_relationship_dependencies,
)


def _direct(catalog="core"):
    return c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, catalog)


def _bridge(**kw):
    base = {
        "segment_kind": c.SegmentKind.governed_bridge, "catalog_source": "rev",
        "from_entity": "account", "to_entity": "account", "bridge_fact_key": "bf-1",
        "cardinality": "N:1", "direction": "forward",
        "bridge_from_catalog_source": "ops", "bridge_from_object_ref": "public.acct.id",
        "bridge_to_catalog_source": "rev", "bridge_to_object_ref": "public.rev_acct.acct_no",
        "relationship_id": "rel-1", "relationship_version": "v3",
    }
    base.update(kw)
    return c.BindingPathSegmentV1(**base)


def _plan(*segments) -> c.BindingPlanV1:
    return c.make_binding_plan(
        recipe_id="t_roll", target_entity="account", catalog_source="core",
        ingredient_bindings=(), path_segments=segments,
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)


# ── the projection (pure) ───────────────────────────────────────────────────────────────────────
def test_a_direct_catalog_segment_is_not_a_traversal():
    """A plan leg that says "this ingredient lives here" crosses nothing; claiming a relationship
    for it would put a link in the trace that the planner never traversed."""
    assert plan_relationship_dependencies(_plan(_direct())) == ()


def test_a_governed_bridge_becomes_one_directional_leg():
    legs = plan_relationship_dependencies(_plan(_direct(), _bridge()))
    assert len(legs) == 1
    leg = legs[0]
    assert leg.relationship_ref == "rel-1"
    assert leg.relationship_kind == "crosswalk"
    assert leg.from_ref == ("ops", "public.acct.id")
    assert leg.to_ref == ("rev", "public.rev_acct.acct_no")
    assert leg.cardinality == "N:1"
    assert leg.review_status == "governed_bridge"
    # no immutable realization revision attached — a discovery/sandbox crossing, stated as such
    assert leg.safety_status == "unverified"
    assert leg.realization_content_hash


def test_the_direction_of_the_crossing_is_part_of_the_realization():
    forward = plan_relationship_dependencies(_plan(_bridge()))[0]
    backward = plan_relationship_dependencies(_plan(_bridge(
        bridge_from_catalog_source="rev", bridge_from_object_ref="public.rev_acct.acct_no",
        bridge_to_catalog_source="ops", bridge_to_object_ref="public.acct.id",
        cardinality="1:N", direction="reverse")))[0]
    assert forward.relationship_ref == backward.relationship_ref     # ONE relationship
    assert forward.realization_content_hash != backward.realization_content_hash


def test_the_exact_realization_revision_is_a_provenance_pin_not_a_content_field():
    """The plan's ``realization_revision_id`` identifies WHICH revision was consumed — a currentness
    pointer. It rides on the dependency pin and never inside a content identity, so replaying the
    identical crossing under a new revision does not fork the candidate's identity."""
    plain = _plan(_bridge())
    pins = plan_dependency_pins(plain)
    segment_pins = [p for p in pins if p.dependency_kind == CROSS_CATALOG_PATH_SEGMENT]
    assert len(segment_pins) == 1 and segment_pins[0].current_revision_id is None
    assert any(p.dependency_kind == CROSS_CATALOG_CONTRACT for p in pins)


def test_a_multi_bridge_path_keeps_its_order():
    legs = plan_relationship_dependencies(_plan(
        _direct(), _bridge(catalog_source="rev", relationship_id="rel-1"),
        _bridge(catalog_source="third", relationship_id="rel-2", bridge_fact_key="bf-2")))
    assert [leg.relationship_ref for leg in legs] == ["rel-1", "rel-2"]


# ── the live governed lens carries it ───────────────────────────────────────────────────────────
def test_a_governed_cross_catalog_option_carries_its_trace(db):
    _cross_seed(db)   # ops + rev + a VERIFIED bridge -> a resolved cross-catalog plan
    ideas, rejections, _evidence = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert len(ideas) == 1 and not rejections
    trace = ideas[0].grounding_trace
    assert trace is not None
    assert trace.candidate_key == ideas[0].plan_envelope.physical_plan_id
    assert trace.validation_status == ideas[0].validation_status
    assert recompute_trace_content_hash(trace) == trace.trace_content_hash
    assert any(p.dependency_kind == READ_SCOPE for p in trace.dependency_pins)
    assert any(p.dependency_kind == CROSS_CATALOG_PATH_SEGMENT for p in trace.dependency_pins)
    assert trace.ordered_operand_roles, "the plan's ingredient bindings are the operand roles"
    assert trace_completeness_gaps(trace, validation_status=ideas[0].validation_status,
                                   requirements=ideas[0].requirements) == ()


def test_the_cross_catalog_trace_is_reproducible_for_the_same_catalog_state(db):
    """IDENTITY, not a build observation — the cross-catalog counterpart of the same-catalog
    reproducibility pin.

    This candidate's identity is ``plan.physical_plan_id`` (it has no recipe candidate key), and
    the segment pin keys embed it, so the whole trace hangs off that id being a property of the
    COMPILED PLAN and not of the run that compiled it. If a run-scoped input ever leaks into it,
    every cross-catalog suggestion identity churns on every rebuild — silently. This fails loudly
    instead.
    """
    _cross_seed(db)
    first, _, _ = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    second, _, _ = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert len(first) == len(second) == 1
    assert first[0].plan_envelope.physical_plan_id == second[0].plan_envelope.physical_plan_id
    assert first[0].grounding_trace.candidate_key == second[0].grounding_trace.candidate_key
    assert (first[0].grounding_trace.trace_content_hash
            == second[0].grounding_trace.trace_content_hash)
    assert (first[0].grounding_trace.ordered_relationship_path
            == second[0].grounding_trace.ordered_relationship_path)
    # NON-VACUOUS, and the reason this test is the tripwire for a churning plan id: that id IS the
    # candidate key and is embedded in every segment pin key, and `candidate_key` is covered by
    # `trace_content_hash` (pinned in test_grounding_trace.py) — so an id that absorbed a
    # run-scoped input could not leave the assertions above green.
    assert first[0].grounding_trace.candidate_key == first[0].plan_envelope.physical_plan_id
    assert any(p.dependency_key.startswith(first[0].plan_envelope.physical_plan_id)
               for p in first[0].grounding_trace.dependency_pins)
