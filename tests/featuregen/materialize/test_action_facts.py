"""R1 — the typed fact-loader framework, and the AUTHOR_FORMULA loader over the 1103 subject.

The framework tests prove the two structural laws: an unknown subject TYPE refuses (typed
dispatch never coerces), and adding a loader is REGISTRATION, not modification (shown with a
test double for an action whose real loader lands at step 8/B3). The loader tests run against a
real seeded considered revision — the shipped resolver, not a hand-written blob.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace

import pytest

from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_decision import ask
from featuregen.materialize.action_facts import (
    ActionFactsV1,
    AuthoringSubjectDiverged,
    AuthoringSubjectKeyV1,
    FactLoaderCollision,
    FactLoaderMissing,
    FactSubjectMismatch,
    facts_for_author_formula,
    load_action_facts,
    register_fact_loader,
    registered_subject_type,
    unregister_fact_loader,
)


# ══ the framework: typed-subject dispatch ════════════════════════════════════════════════════════
def test_an_action_with_no_loader_refuses_BY_NAME():
    """GENERATE_PREVIEW's facts do not exist until B1/B2 — asking for them is refused with the
    remedy named, never answered with an empty ActionFactsV1 (an invented clean bill)."""
    with pytest.raises(FactLoaderMissing, match="GENERATE_PREVIEW"):
        load_action_facts(None, ActionV1.GENERATE_PREVIEW, object())


def test_a_subject_of_the_wrong_TYPE_refuses_never_coerces():
    """A bare string that happens to look like a scope key is exactly how the wrong resource
    gets decided — typed dispatch refuses and names the expected type."""
    with pytest.raises(FactSubjectMismatch, match="AuthoringSubjectKeyV1"):
        load_action_facts(None, ActionV1.AUTHOR_FORMULA, "sha256-that-looks-plausible")


def test_a_SUBCLASS_is_not_the_registered_type():
    """`type(...) is` deliberately — a subclass smuggling state past a loader written for the
    base shape is the forgery-by-shape problem the decision module's rules exist for."""

    class Widened(AuthoringSubjectKeyV1):
        pass

    subject = Widened("crev", "opt", "prh", "cat", "def")
    with pytest.raises(FactSubjectMismatch):
        load_action_facts(None, ActionV1.AUTHOR_FORMULA, subject)


def test_ADDING_a_loader_is_registration_not_modification():
    """The step-8 completion path, proven with a double: a new action's loader arrives by
    registering its subject type and callable — the dispatch, refusals and facts shape are
    already here, and nothing in the module needed editing."""

    @dataclass(frozen=True, slots=True)
    class BuildSetRevisionSubject:
        build_set_revision_id: str

    def fake_loader(conn, subject: BuildSetRevisionSubject) -> ActionFactsV1:
        return ActionFactsV1(
            member_names=("m1",),
            evidence_pins={"build_set_revision": subject.build_set_revision_id})

    register_fact_loader(ActionV1.GENERATE_PREVIEW, BuildSetRevisionSubject, fake_loader)
    try:
        assert registered_subject_type(ActionV1.GENERATE_PREVIEW) is BuildSetRevisionSubject
        facts = load_action_facts(
            None, ActionV1.GENERATE_PREVIEW, BuildSetRevisionSubject("bsr-1"))
        assert facts.evidence_pins == {"build_set_revision": "bsr-1"}
        # The wrong-type refusal applies to the double exactly as to the real loader.
        with pytest.raises(FactSubjectMismatch):
            load_action_facts(None, ActionV1.GENERATE_PREVIEW, "bsr-1")
    finally:
        unregister_fact_loader(ActionV1.GENERATE_PREVIEW)
    assert registered_subject_type(ActionV1.GENERATE_PREVIEW) is None


def test_a_SECOND_loader_for_one_action_is_a_collision_not_a_silent_swap():
    """One authority per action: replacing a loader is an explicit act."""
    with pytest.raises(FactLoaderCollision, match="AUTHOR_FORMULA"):
        register_fact_loader(
            ActionV1.AUTHOR_FORMULA, AuthoringSubjectKeyV1, facts_for_author_formula)
    # replace=True is the deliberate substitution path (used here to restore the real loader).
    register_fact_loader(
        ActionV1.AUTHOR_FORMULA, AuthoringSubjectKeyV1, facts_for_author_formula,
        replace=True)


def test_facts_become_the_canonical_request_verbatim():
    """`ActionFactsV1.request` carries every half into ActionRequestV1 — including the CLEAN
    members (owner ruling 2026-08-23 item 4), and never invents a verdict of its own."""
    facts = ActionFactsV1(
        member_names=("clean", "flagged"),
        member_blockers={"flagged": ("BINDING_NOT_BOUND",)},
        member_warnings={"flagged": ("RECIPE_REVIEW_NOT_CURRENT",)},
        evidence_pins={"retirement_scope_key": "key-1"})
    request = facts.request(
        action=ActionV1.AUTHOR_FORMULA, resource_identity_hash="key-1")
    assert request.member_names == ("clean", "flagged")
    assert request.member_blockers == {"flagged": ("BINDING_NOT_BOUND",)}
    assert request.member_warnings == {"flagged": ("RECIPE_REVIEW_NOT_CURRENT",)}
    assert request.evidence_pins == {"retirement_scope_key": "key-1"}
    assert request.resource_identity_hash == "key-1"


# ══ the AUTHOR_FORMULA loader over a REAL seeded candidate ═══════════════════════════════════════
def _seed_revision(conn, *, revision_id="crev-af1", snapshot_id="snap-af1"):
    """A considered revision with ONE option, assembled by the SHIPPED identity builders (the
    `test_formula_drafts` discipline: a hand-written blob only proves two hand-written things
    agree)."""
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.contract.gate1 import _candidate_identity, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    idea = FeatureIdea(
        name="avg_balance_90d", description="mean balance",
        derives_from=["deposits.balance"], aggregation="avg", grain_table="deposits")
    public_feature = _idea_json(idea)
    identity = _candidate_identity(path="anchor", source="anchor", lens="anchor", feature=idea)
    considered = {
        "version": "contract-considered-v3",
        "public": {"anchor": {**public_feature, "option_id": "opt-af"}, "rejections": []},
        "options_by_id": {
            "opt-af": {
                "source": "anchor", "lens": "anchor",
                "canonical_candidate_identity": identity,
                "canonical_candidate_identity_hash": canonical_hash(identity),
                "recipe_candidate_key": None,
            },
        },
        "recipe_grounding_context_by_candidate_key": {},
        "recipe_candidate_keys_by_recipe_id": {},
    }
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-af1','dormancy predicts churn','hypothesis') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, metadata_snapshot_content_hash, "
        "considered_json, considered_content_hash, canonicalization_version) "
        "VALUES (%s,'int-af1','run-af1',%s,'sha256:snap-af',%s::jsonb,'sha256:considered-af',"
        "'contract-considered-v3')",
        (revision_id, snapshot_id, json.dumps(considered)))
    return revision_id


def _true_subject(conn, revision_id: str, option_id: str) -> AuthoringSubjectKeyV1:
    from featuregen.overlay.upload.formula_draft_service import frozen_candidate

    candidate = frozen_candidate(conn, revision_id, option_id)
    return AuthoringSubjectKeyV1(
        considered_revision_id=revision_id, option_id=option_id,
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        definition_revision=candidate.definition_revision)


def test_the_loader_assembles_the_authoring_facts_over_the_1103_subject(db):
    """The three pins are the shared `authoring_evidence_pins` composition — the scope key (the
    subject's own hash), the frozen catalog, and the evidence that chose the method — so the
    service's decide and the worker's recheck read what this loader loaded."""
    revision = _seed_revision(db)
    subject = _true_subject(db, revision, "opt-af")

    facts = load_action_facts(db, ActionV1.AUTHOR_FORMULA, subject)

    assert facts.member_names == (subject.option_id,)
    assert set(facts.evidence_pins) == {
        "retirement_scope_key", "catalog_snapshot_hash", "strategy_identity_hash"}
    assert facts.evidence_pins["retirement_scope_key"] == subject.subject_key
    assert facts.evidence_pins["catalog_snapshot_hash"] == subject.catalog_snapshot_hash
    # Facts, never verdicts: the loader's output folds through the ONE service.
    decision = ask(db, facts.request(
        action=ActionV1.AUTHOR_FORMULA, resource_identity_hash=subject.subject_key))
    assert decision.allowed


def test_a_subject_the_store_contradicts_REFUSES_naming_the_moved_fields(db):
    revision = _seed_revision(db, revision_id="crev-af2", snapshot_id="snap-af2")
    subject = _true_subject(db, revision, "opt-af")
    forged = replace(subject, planning_request_hash="sha256:not-what-was-frozen")

    with pytest.raises(AuthoringSubjectDiverged, match="planning_request_hash"):
        facts_for_author_formula(db, forged)


def test_an_unresolvable_candidate_is_the_stores_TYPED_refusal(db):
    """No candidate, no facts — the loader propagates `CandidateUnavailable` (the same typed
    refusal every caller of the shipped resolver already maps), never an empty facts record."""
    from featuregen.overlay.upload.formula_draft_service import CandidateUnavailable

    subject = AuthoringSubjectKeyV1(
        considered_revision_id="crev-never-existed", option_id="opt-x",
        planning_request_hash="p", catalog_snapshot_hash="c", definition_revision="d")
    with pytest.raises(CandidateUnavailable):
        facts_for_author_formula(db, subject)


def test_the_subject_key_IS_the_1103_retirement_scope_key():
    """One tuple, one hash, three uses — the subject key must be byte-identical to the shipped
    retirement scope key, or authorization, retirement and the money guard drift apart."""
    from featuregen.overlay.upload.retirement_scope import retirement_scope_key

    subject = AuthoringSubjectKeyV1("crev", "opt", "prh", "cat", "defrev")
    assert subject.subject_key == retirement_scope_key(
        considered_revision_id="crev", option_id="opt", planning_request_hash="prh",
        catalog_snapshot_hash="cat", definition_revision="defrev")
