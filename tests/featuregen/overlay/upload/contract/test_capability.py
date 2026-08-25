"""CapabilityAssessmentV1 — projection equals service, proven over the WHOLE vocabulary.

The property test is the module's contract: for every reason code × every action, the projected
card state must agree with ``ask()``'s verdict — the projection may add copy and a render mode,
never a verdict. The copy pins keep the two plan-required lines ("potentially available after
selection"; the production governance-not-released copy) product decisions rather than drift.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_decision import ActionRequestV1, ask
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.contract.capability import (
    GUARD_RENDERED_WARNINGS,
    LADDER_RUNG_VIEW,
    PRE_RESOURCE_COPY,
    PRODUCTION_NOT_RELEASED_COPY,
    CapabilityProjectionDefect,
    CapabilityRenderMode,
    CapabilityResourceFactsV1,
    project_capabilities,
)

ALL_EXIST = CapabilityResourceFactsV1(
    authoring_subject_exists=True, build_set_revision_exists=True,
    sealed_artifact_exists=True, verified_output_exists=True,
    production_output_exists=True)


def _decision(action, *, blockers=(), warnings=()):
    return ask(None, ActionRequestV1(
        action=action, resource_identity_hash="res-1", member_names=("m1",),
        member_blockers={"m1": tuple(blockers)} if blockers else {},
        member_warnings={"m1": tuple(warnings)} if warnings else {}))


_SUBJECT_FIELD = {
    ActionV1.AUTHOR_FORMULA: "authoring_subject_exists",
    ActionV1.GENERATE_PREVIEW: "build_set_revision_exists",
    ActionV1.EXECUTE_SANDBOX: "sealed_artifact_exists",
    ActionV1.PUBLISH_SANDBOX: "verified_output_exists",
    ActionV1.MATERIALIZE_PRODUCTION: "sealed_artifact_exists",
    ActionV1.PUBLISH_PRODUCTION: "production_output_exists",
}


def _assess_one(action, decision):
    """Project with ONLY this action's subject existing; the rest render pre-resource. The
    sealed artifact is shared by two actions (the R1 table), so when it exists the sibling gets
    its own clean decision — the projection refuses a decision-less existing resource."""
    only = CapabilityResourceFactsV1(**{_SUBJECT_FIELD[action]: True})
    decisions = {action: decision}
    if only.sealed_artifact_exists:
        for shared in (ActionV1.EXECUTE_SANDBOX, ActionV1.MATERIALIZE_PRODUCTION):
            decisions.setdefault(shared, _decision(shared))
    return project_capabilities(decisions, resources=only).for_action(action)


# ══ THE PROPERTY — no projected capability contradicts ask/decide, whole vocabulary ══════════════
def test_PROJECTION_EQUALS_SERVICE_over_every_code_and_every_action():
    """For every (code, action) in the closed vocabulary: fold through the ONE service, project,
    and the card state must agree — UNAVAILABLE exactly when the service refused, the service's
    codes verbatim, PROVISIONAL exactly for allowed-with-guard-warnings, EXACT otherwise."""
    for code in sorted(R.REASON_FAMILIES):
        for action in ActionV1:
            decision = _decision(action, blockers=(code,))
            entry = _assess_one(action, decision)

            assert (entry.render_mode is CapabilityRenderMode.UNAVAILABLE) == (
                not decision.allowed), (code, action)
            if not decision.allowed:
                member_blockers = {c for v in decision.per_member for c in v.blockers}
                assert set(entry.blockers) == set(decision.blockers) | member_blockers, (
                    code, action)
            else:
                assert entry.blockers == ()
                assert set(entry.warnings) == set(decision.warnings), (code, action)
                expected = (CapabilityRenderMode.PROVISIONAL
                            if set(decision.warnings) & GUARD_RENDERED_WARNINGS
                            else CapabilityRenderMode.EXACT)
                assert entry.render_mode is expected, (code, action)


def test_a_MIXED_matrix_row_projects_each_channel_correctly():
    """The owner's matrix's provisional row end-to-end: unknown cardinality (a guard-rendered
    warning) plus a non-guard maturity warning — allowed, PROVISIONAL, both warnings served."""
    decision = _decision(
        ActionV1.GENERATE_PREVIEW,
        blockers=(R.DIRECTIONAL_CARDINALITY_UNPROVEN,),   # cell says WARN at preview
        warnings=(R.FORMULA_NOT_REVIEWED,))
    entry = _assess_one(ActionV1.GENERATE_PREVIEW, decision)
    assert decision.allowed
    assert entry.render_mode is CapabilityRenderMode.PROVISIONAL
    assert set(entry.warnings) == {R.DIRECTIONAL_CARDINALITY_UNPROVEN, R.FORMULA_NOT_REVIEWED}

    exact = _assess_one(ActionV1.GENERATE_PREVIEW,
                        _decision(ActionV1.GENERATE_PREVIEW,
                                  warnings=(R.FORMULA_NOT_REVIEWED,)))
    assert exact.render_mode is CapabilityRenderMode.EXACT   # non-guard warnings stay exact


# ══ the pinned copy ══════════════════════════════════════════════════════════════════════════════
def test_the_PRE_RESOURCE_copy_is_pinned_verbatim():
    """R1: a card for an action whose subject does not exist yet says exactly this — with NO
    blocker codes, because an absent resource is not a refusal."""
    assert PRE_RESOURCE_COPY == "potentially available after selection"
    assessment = project_capabilities({}, resources=CapabilityResourceFactsV1())
    preview = assessment.for_action(ActionV1.GENERATE_PREVIEW)
    assert preview.pre_resource
    assert preview.render_mode is CapabilityRenderMode.UNAVAILABLE
    assert preview.copy == PRE_RESOURCE_COPY
    assert preview.blockers == () and preview.warnings == ()


def test_PRODUCTION_actions_render_the_governance_not_released_copy():
    """Both production acts, decided or pre-resource: the copy names a release gate, never a
    missing certificate and never "potentially available" — selection will not change it."""
    assert "release gate" in PRODUCTION_NOT_RELEASED_COPY
    assert "production governance" in PRODUCTION_NOT_RELEASED_COPY

    # Pre-resource: still the governance copy, not the selection copy.
    bare = project_capabilities({}, resources=CapabilityResourceFactsV1())
    for action in (ActionV1.MATERIALIZE_PRODUCTION, ActionV1.PUBLISH_PRODUCTION):
        entry = bare.for_action(action)
        assert entry.copy == PRODUCTION_NOT_RELEASED_COPY, action
        assert entry.render_mode is CapabilityRenderMode.UNAVAILABLE

    # Decided: ask() refuses production acts as unavailable; the projection serves that verdict
    # with the same copy.
    decision = _decision(ActionV1.MATERIALIZE_PRODUCTION)
    assert not decision.allowed and "ACTION_UNAVAILABLE" in decision.blockers
    entry = _assess_one(ActionV1.MATERIALIZE_PRODUCTION, decision)
    assert entry.render_mode is CapabilityRenderMode.UNAVAILABLE
    assert entry.copy == PRODUCTION_NOT_RELEASED_COPY
    assert "ACTION_UNAVAILABLE" in entry.blockers


# ══ never a verdict of its own ═══════════════════════════════════════════════════════════════════
def test_an_existing_resource_with_no_decision_REFUSES():
    """The projection may never fill a gap with an invented verdict — an existing subject means
    somebody must ask the service first."""
    with pytest.raises(CapabilityProjectionDefect, match="never computes a verdict"):
        project_capabilities(
            {}, resources=CapabilityResourceFactsV1(build_set_revision_exists=True))


def test_a_decision_over_an_absent_resource_REFUSES():
    """Contradictory inputs describe two different worlds; the projection picks neither."""
    decision = _decision(ActionV1.GENERATE_PREVIEW)
    with pytest.raises(CapabilityProjectionDefect, match="different worlds"):
        project_capabilities(
            {ActionV1.GENERATE_PREVIEW: decision},
            resources=CapabilityResourceFactsV1(build_set_revision_exists=False))


def test_the_module_never_imports_the_disposition_table():
    """Structural half of "never computes a verdict": the projection has no access to the fold
    it would need to second-guess the service."""
    import inspect

    from featuregen.overlay.upload.contract import capability

    source = inspect.getsource(capability)
    assert "action_dispositions" not in source
    assert "fold_member_codes" not in source
    assert "ACTION_DISPOSITIONS" not in source


def test_the_ladder_view_names_the_five_rungs_and_their_canonical_columns():
    """The legacy wire's five rung names, projected onto the one authority: author_formula is
    canonical (the R1 adapter); save_idea/create_contract stay the ladder's; the two
    materialization rungs are retired in place until their step-8 successor."""
    assert set(LADDER_RUNG_VIEW) == {
        "save_idea", "create_contract", "author_formula",
        "request_materialization", "execute_materialization"}
    assert LADDER_RUNG_VIEW["author_formula"] is ActionV1.AUTHOR_FORMULA
    assert LADDER_RUNG_VIEW["save_idea"] is None
    assert LADDER_RUNG_VIEW["create_contract"] is None
    assert LADDER_RUNG_VIEW["request_materialization"] is None
    assert LADDER_RUNG_VIEW["execute_materialization"] is None
