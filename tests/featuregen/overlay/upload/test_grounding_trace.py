"""Task 2A — the grounding decision trace CONTRACT (pure; no DB).

The emission tests (the real gauntlet producing these objects) live in
``test_grounding_trace_emission.py``. This file pins the value objects themselves: what the
trace hash covers, what it deliberately EXCLUDES, and what "a complete trace" means — the two
properties Release-A V2 rests on (freeze 0F-7).
"""
from __future__ import annotations

from dataclasses import fields, replace

import pytest

from featuregen.contracts.contract_versions import contract_owner
from featuregen.contracts.evidence_axes import (
    AssertionStrength,
    EvidenceAuthorityV1,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.upload import grounding_trace as gt
from featuregen.overlay.upload.grounding_trace import (
    GroundingDecisionTraceV1,
    GroundingDependencyPinV1,
    SuggestionDependencyClass,
    SuggestionRelationshipDependencyV1,
    build_trace,
    dependency_pin,
    recompute_trace_content_hash,
    relationship_leg,
    trace_completeness_gaps,
)
from featuregen.overlay.upload.join_path import (
    JoinOutcome,
    JoinStep,
    join_outcome_relationship_path,
)
from featuregen.overlay.upload.validation_requirements import (
    build_requirement,
    evaluated_rule_content_hashes,
)

_CAT = "bank"
_EVIDENCE = (
    EvidenceAuthorityV1(EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED,
                        EvidenceLifecycle.ACTIVE, "fact-1", "ev-1"),
    EvidenceAuthorityV1(EvidenceProducer.SOURCE, AssertionStrength.PROPOSED,
                        EvidenceLifecycle.ACTIVE, None, "ev-2"),
)


def _pin(kind: str, key: str, content: dict, **kw) -> GroundingDependencyPinV1:
    return dependency_pin(
        dependency_class=kw.pop("dependency_class", SuggestionDependencyClass.VALIDATION),
        dependency_kind=kind, dependency_key=key, content=content, **kw)


def _legs() -> tuple[SuggestionRelationshipDependencyV1, ...]:
    return join_outcome_relationship_path(
        JoinOutcome(kind=JoinOutcome.OPERATIONAL, steps=(
            JoinStep("public.txn.acct_id", "public.acct.id", "N:1",
                     approved_join_fact_key="ajf-1", approved_join_status="VERIFIED"),
            JoinStep("public.acct.cif_id", "public.cust.cif_id", "N:1"),
        )), catalog_source=_CAT)


def _trace(**overrides) -> GroundingDecisionTraceV1:
    base: dict = {
        "candidate_key": "cand-1",
        "ordered_operand_roles": ((_CAT, "public.txn.amt", "flow_col"),
                                  (_CAT, "public.txn.acct_id", "entity")),
        "ordered_relationship_path": _legs(),
        "validation_status": "NEEDS_EXTERNAL_VALIDATION",
        "requirements": (build_requirement(code="TYPE_IS_NUMERIC",
                                           operand=(_CAT, "public.txn.amt"), detail="d"),),
        "dependency_pins": (
            _pin(gt.READ_SCOPE, "read-scope", {"allowed_classes": []},
                 dependency_class=SuggestionDependencyClass.HARD_AVAILABILITY),
            _pin(gt.GROUNDING_CANDIDATE_SET, "derives_from",
                 {"resolved_object_refs": ["public.txn.amt"]},
                 dependency_class=SuggestionDependencyClass.HARD_AVAILABILITY),
            _pin(gt.GOVERNED_LOGICAL_REPRESENTATION,
                 gt.column_dependency_key(_CAT, "public.txn.amt"),
                 {"field_name": "logical_representation", "value": None, "status": "no_decision"},
                 current_revision_id="dec-1", evidence=_EVIDENCE),
            _pin(gt.JOIN_PATH, gt.column_dependency_key(_CAT, "public.txn.amt"),
                 {"from_table": "acct", "to_table": "txn", "outcome": "OPERATIONAL"}),
        ),
        "validation_rule_content_hashes": evaluated_rule_content_hashes(
            ("TYPE_IS_NUMERIC", "JOIN_CONNECTIVITY")),
        "read_scope_rule_content_hashes": ("rs-hash",),
    }
    base.update(overrides)
    return build_trace(**base)


# ── the frozen shape (freeze 0F-7 / plan "Grounding and validation trace") ───────────────────────
def test_the_three_value_objects_carry_exactly_the_frozen_fields():
    assert [f.name for f in fields(GroundingDependencyPinV1)] == [
        "dependency_class", "dependency_kind", "dependency_key", "content_hash",
        "current_revision_id", "evidence"]
    assert [f.name for f in fields(SuggestionRelationshipDependencyV1)] == [
        "relationship_ref", "relationship_kind", "from_ref", "to_ref",
        "realization_content_hash", "cardinality", "safety_status", "review_status", "evidence"]
    assert [f.name for f in fields(GroundingDecisionTraceV1)] == [
        "candidate_key", "ordered_operand_roles", "ordered_relationship_path", "validation_status",
        "requirements", "dependency_pins", "validation_rule_content_hashes",
        "read_scope_rule_content_hashes", "trace_content_hash"]
    assert [c.value for c in SuggestionDependencyClass] == [
        "hard_availability", "validation", "semantic"]


def test_every_hashed_contract_is_registered_to_this_owner():
    """0S rule: ``contract_hash_v1`` refuses an unregistered (name, version), so the trace's own
    contracts are registered by their owner module — never minted ungoverned."""
    for name in (gt.TRACE_CONTRACT, gt.DEPENDENCY_CONTRACT, gt.REALIZATION_CONTRACT):
        assert contract_owner(name, gt.CONTRACT_VERSION) == "featuregen.overlay.upload.grounding_trace"


def test_an_unknown_dependency_kind_is_refused():
    with pytest.raises(ValueError, match="dependency kind"):
        _pin("invented_kind", "k", {})


# ── what the hash COVERS ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mutation", [
    {"candidate_key": "cand-2"},
    {"validation_status": "DESIGN_CHECKED"},
    {"ordered_operand_roles": ((_CAT, "public.txn.amt", "stock_col"),)},
    {"requirements": ()},
    {"read_scope_rule_content_hashes": ("other",)},
])
def test_the_decision_and_its_inputs_are_covered_by_the_hash(mutation):
    assert _trace(**mutation).trace_content_hash != _trace().trace_content_hash


def test_a_changed_dependency_content_hash_moves_the_trace_hash():
    """The pins are the trace's grip on the catalog: if what a check READ changed, the trace that
    explained the decision must no longer verify."""
    pins = list(_trace().dependency_pins)
    pins[2] = _pin(gt.GOVERNED_LOGICAL_REPRESENTATION,
                   gt.column_dependency_key(_CAT, "public.txn.amt"),
                   {"field_name": "logical_representation", "value": "varchar",
                    "status": "resolved"})
    assert _trace(dependency_pins=tuple(pins)).trace_content_hash != _trace().trace_content_hash


def test_a_changed_evidence_AXIS_moves_the_hash_even_though_occurrence_ids_do_not():
    weaker = (replace(_EVIDENCE[0], strength=AssertionStrength.PROPOSED), _EVIDENCE[1])
    pins = list(_trace().dependency_pins)
    pins[2] = _pin(gt.GOVERNED_LOGICAL_REPRESENTATION,
                   gt.column_dependency_key(_CAT, "public.txn.amt"),
                   {"field_name": "logical_representation", "value": None, "status": "no_decision"},
                   current_revision_id="dec-1", evidence=weaker)
    assert _trace(dependency_pins=tuple(pins)).trace_content_hash != _trace().trace_content_hash


def test_the_ordered_relationship_path_is_ordered():
    """A path is a SEQUENCE. Reversing it is a different traversal and must not hash the same."""
    assert _trace(ordered_relationship_path=tuple(reversed(_legs()))).trace_content_hash != (
        _trace().trace_content_hash)


# ── what the hash EXCLUDES (freeze 0F-7) ────────────────────────────────────────────────────────
def test_revision_pins_and_evidence_occurrence_ids_are_excluded():
    """MUST-SURVIVE. ``current_revision_id`` / ``producer_ref`` / ``evidence_id`` are provenance for
    CURRENTNESS comparison — replaying identical content under new ids must not mint a new identity."""
    pins = list(_trace().dependency_pins)
    reidentified = tuple(replace(e, producer_ref="fact-9", evidence_id=f"ev-{i}9")
                         for i, e in enumerate(_EVIDENCE))
    pins[2] = _pin(gt.GOVERNED_LOGICAL_REPRESENTATION,
                   gt.column_dependency_key(_CAT, "public.txn.amt"),
                   {"field_name": "logical_representation", "value": None, "status": "no_decision"},
                   current_revision_id="dec-2-NEW", evidence=reidentified)
    assert _trace(dependency_pins=tuple(pins)).trace_content_hash == _trace().trace_content_hash


def test_reordering_the_unordered_sets_survives_the_hash():
    """MUST-SURVIVE (the brief's second mutation). Pins are a SET and an evidence tuple is a SET of
    axes: reordering — or replaying a duplicate occurrence — is not a content change."""
    pins = _trace().dependency_pins
    shuffled = (pins[3], pins[0], pins[2], pins[1])
    assert _trace(dependency_pins=shuffled).trace_content_hash == _trace().trace_content_hash

    reordered_evidence = _pin(
        gt.GOVERNED_LOGICAL_REPRESENTATION, gt.column_dependency_key(_CAT, "public.txn.amt"),
        {"field_name": "logical_representation", "value": None, "status": "no_decision"},
        current_revision_id="dec-1", evidence=(_EVIDENCE[1], _EVIDENCE[0], _EVIDENCE[0]))
    with_reordered = (pins[0], pins[1], reordered_evidence, pins[3])
    assert _trace(dependency_pins=with_reordered).trace_content_hash == _trace().trace_content_hash


def test_requirement_mint_order_is_not_identity_but_the_carried_tuple_is_verbatim():
    """The requirement SET is unordered for identity, yet the trace carries the gauntlet's own tuple
    verbatim — the V1 payload reads that tuple, so it may never be re-sorted underneath it."""
    reqs = (build_requirement(code="TYPE_IS_NUMERIC", operand=(_CAT, "public.txn.amt")),
            build_requirement(code="GRAIN_IS_UNIQUE", operand=(_CAT, "public.txn.acct_id")))
    forward = _trace(requirements=reqs)
    backward = _trace(requirements=tuple(reversed(reqs)))
    assert forward.trace_content_hash == backward.trace_content_hash
    assert forward.requirements == reqs and backward.requirements == tuple(reversed(reqs))


def test_recompute_reproduces_the_stored_hash():
    trace = _trace()
    assert recompute_trace_content_hash(trace) == trace.trace_content_hash


# ── completeness: what makes a DESIGN_CHECKED trace VALID for V2 ────────────────────────────────
def _gaps(trace, status="NEEDS_EXTERNAL_VALIDATION", requirements=None):
    return trace_completeness_gaps(
        trace, validation_status=status,
        requirements=_trace().requirements if requirements is None else requirements)


def test_a_full_trace_has_no_gaps():
    assert _gaps(_trace()) == ()


def test_a_missing_trace_is_the_first_gap():
    assert trace_completeness_gaps(None, validation_status="DESIGN_CHECKED") == ("missing_trace",)


@pytest.mark.parametrize("dropped_kind", [
    gt.GOVERNED_LOGICAL_REPRESENTATION,   # type
    gt.READ_SCOPE,                        # visibility
    gt.GROUNDING_CANDIDATE_SET,
    gt.JOIN_PATH,                         # path
])
def test_removing_a_dependency_pin_kills_completeness(dropped_kind):
    """MUTATION (the brief's first): drop the type / visibility / grounding-set / path dependency and
    the completeness proof must DIE, not shrug."""
    kept = tuple(p for p in _trace().dependency_pins if p.dependency_kind != dropped_kind)
    mutated = replace(_trace(), dependency_pins=kept)
    assert _gaps(mutated) != ()


def test_removing_the_grain_or_as_of_dependency_kills_completeness():
    """The grain / as-of reads are pinned at the point the check reads them, so a trace that
    evaluated those rules and kept no pin for them is incomplete."""
    trace = _trace(
        requirements=(
            build_requirement(code="GRAIN_IS_UNIQUE", operand=(_CAT, "public.acct.id")),
            build_requirement(code="TEMPORAL_IS_POPULATED", operand=(_CAT, "public.txn.as_of")),
        ),
        dependency_pins=(*_trace().dependency_pins,
                         _pin(gt.GRAIN_COLUMN_LOOKUP, gt.table_dependency_key(_CAT, "acct"),
                              {"table": "acct", "grain_object_ref": "public.acct.id"},
                              dependency_class=SuggestionDependencyClass.HARD_AVAILABILITY),
                         _pin(gt.GOVERNED_IS_GRAIN, gt.column_dependency_key(_CAT, "public.acct.id"),
                              {"field_name": "is_grain", "value": "true", "status": "resolved"}),
                         _pin(gt.AS_OF_COLUMN_LOOKUP, gt.table_dependency_key(_CAT, "txn"),
                              {"table": "txn", "as_of_object_ref": "public.txn.as_of"},
                              dependency_class=SuggestionDependencyClass.HARD_AVAILABILITY),
                         _pin(gt.GOVERNED_IS_AS_OF,
                              gt.column_dependency_key(_CAT, "public.txn.as_of"),
                              {"field_name": "is_as_of", "value": "true", "status": "no_value"})),
        validation_rule_content_hashes=evaluated_rule_content_hashes(
            ("TYPE_IS_NUMERIC", "JOIN_CONNECTIVITY", "GRAIN_IS_UNIQUE", "TEMPORAL_IS_POPULATED")))
    assert _gaps(trace, requirements=trace.requirements) == ()
    for kind in (gt.GOVERNED_IS_GRAIN, gt.GOVERNED_IS_AS_OF,
                 gt.GRAIN_COLUMN_LOOKUP, gt.AS_OF_COLUMN_LOOKUP):
        stripped = replace(trace, dependency_pins=tuple(
            p for p in trace.dependency_pins if p.dependency_kind != kind))
        assert _gaps(stripped, requirements=trace.requirements) != (), kind


def test_removing_the_relationship_path_kills_completeness():
    """The path is RETAINED, never re-searched: a candidate whose join rule was evaluated and whose
    ordered path is gone cannot be explained without rerunning the planner."""
    mutated = replace(_trace(), ordered_relationship_path=())
    assert "unpinned_relationship_path" in _gaps(mutated)


def test_a_requirement_whose_operand_is_not_pinned_is_a_gap():
    """Every requirement must name the exact dependency that caused it."""
    orphan = build_requirement(code="TYPE_IS_NUMERIC", operand=(_CAT, "public.other.col"))
    gaps = _gaps(_trace(), requirements=(orphan,))
    assert any(g.startswith("unpinned_requirement_operand") for g in gaps)


def test_a_trace_whose_requirements_disagree_with_the_candidate_is_a_gap():
    assert "requirements_mismatch" in _gaps(_trace(), requirements=())


def test_a_tampered_trace_hash_is_a_gap():
    tampered = replace(_trace(), trace_content_hash="0" * 64)
    assert "trace_content_hash_mismatch" in _gaps(tampered)


def test_a_status_disagreement_is_a_gap():
    assert "validation_status_mismatch" in _gaps(_trace(), status="DESIGN_CHECKED")


def test_a_trace_with_no_candidate_key_is_a_gap():
    assert "missing_candidate_key" in _gaps(_trace(candidate_key=""))


# ── the same-catalog join path (P2): converted AT the join seam, never re-walked ─────────────────
def test_the_join_outcome_becomes_the_ordered_directional_realization():
    legs = _legs()
    assert len(legs) == 2
    first, second = legs
    assert first.from_ref == (_CAT, "public.txn.acct_id")
    assert first.to_ref == (_CAT, "public.acct.id")
    assert first.cardinality == "N:1"
    assert first.relationship_kind == "direct_equality"
    assert first.safety_status == "clearing" and first.review_status == "VERIFIED"
    assert second.review_status == "file_declared"      # no approved_join fact — a meaningful answer
    assert second.safety_status == "clearing"


def test_a_relationship_ref_is_direction_free_but_the_realization_is_not():
    """One semantic relationship, many directional realizations: the ref identifies the relationship,
    the realization hash identifies the leg that was actually traversed."""
    forward = join_outcome_relationship_path(
        JoinOutcome(kind=JoinOutcome.OPERATIONAL,
                    steps=(JoinStep("public.txn.acct_id", "public.acct.id", "N:1"),)),
        catalog_source=_CAT)[0]
    reverse = join_outcome_relationship_path(
        JoinOutcome(kind=JoinOutcome.OPERATIONAL,
                    steps=(JoinStep("public.acct.id", "public.txn.acct_id", "1:N"),)),
        catalog_source=_CAT)[0]
    assert forward.relationship_ref == reverse.relationship_ref
    assert forward.realization_content_hash != reverse.realization_content_hash


def test_an_unverified_leg_is_recorded_as_unverified_not_dropped():
    legs = join_outcome_relationship_path(
        JoinOutcome(kind=JoinOutcome.UNVERIFIED,
                    steps=(JoinStep("public.txn.acct_id", "public.acct.id", "N:1",
                                    approved_join_fact_key="ajf-2",
                                    approved_join_status="DRAFT"),),
                    endpoints=(("public.txn.acct_id", "public.acct.id"),),
                    fact_keys=("ajf-2",)),
        catalog_source=_CAT)
    assert legs[0].safety_status == "unverified" and legs[0].review_status == "DRAFT"
    assert legs[0].evidence and legs[0].evidence[0].producer_ref == "ajf-2"


def test_a_pathless_outcome_yields_no_legs():
    for outcome in (JoinOutcome(kind=JoinOutcome.NO_PATH),
                    JoinOutcome(kind=JoinOutcome.DENIED,
                                endpoints=(("public.a.x", "public.b.y"),))):
        assert join_outcome_relationship_path(outcome, catalog_source=_CAT) == ()


def test_an_undeclared_cardinality_says_so_rather_than_inventing_one():
    leg = join_outcome_relationship_path(
        JoinOutcome(kind=JoinOutcome.OPERATIONAL,
                    steps=(JoinStep("public.a.x", "public.b.y", None),)),
        catalog_source=_CAT)[0]
    assert leg.cardinality == "unknown"


def test_relationship_kind_comes_from_the_frozen_vocabulary():
    with pytest.raises(ValueError, match="unknown relationship kind"):
        relationship_leg(relationship_ref="r", relationship_kind="invented",
                         from_ref=(_CAT, "a"), to_ref=(_CAT, "b"), realization_content={},
                         cardinality="1:1", safety_status="clearing", review_status="x")
