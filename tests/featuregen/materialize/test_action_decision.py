"""One question, two moments — and the refusal that makes the second one worth asking.

▲ The case worth reading first is `test_DRIFT_REFUSES_RATHER_THAN_RE_DECIDING`. A worker that
re-evaluates instead would usually get "allowed" again and proceed under an answer no human saw.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.action_authorization import (
    ActionUnavailable,
    ActionV1,
    authorize_action,
)
from featuregen.materialize.action_decision import (
    ActionRequestV1,
    DecisionDrift,
    DecisionMissing,
    ask,
    decide,
    recheck,
)

ENV = "hdfc-local"
RESOURCE = "res-1"
PINS = {"build_set": "bs-1", "formula": "sha256:f1"}


def _authorized(db, action=ActionV1.GENERATE_PREVIEW, resource=RESOURCE):
    return authorize_action(
        db, action=action, resource_identity_hash=resource,
        actor_subject="user:sam", environment_id=ENV).authorization_id


def _request(action=ActionV1.GENERATE_PREVIEW, *, resource=RESOURCE, blockers=None,
             warnings=None, pins=None) -> ActionRequestV1:
    return ActionRequestV1(
        action=action, resource_identity_hash=resource,
        member_blockers=blockers or {}, member_warnings=warnings or {},
        evidence_pins=PINS if pins is None else pins)


# ══ ASK vs DECIDE ══════════════════════════════════════════════════════════════════════════════
def test_ASKING_RECORDS_NOTHING(db):
    """▲ A workspace enabling a button asks. `verify_eligibility` already says why: recording an
    attempt every time a screen rendered would fill the history with things nobody did — which makes
    the decisions somebody DID act on harder to find, not easier."""
    authorization = _authorized(db)

    decision = ask(db, _request(), authorization_id=authorization)

    assert decision.allowed is True
    assert db.execute("SELECT count(*) FROM action_decision_revision").fetchone()[0] == 0


def test_DECIDING_RECORDS_ONE_ROW(db):
    authorization = _authorized(db)

    decision_id, decision = decide(db, _request(), authorization_id=authorization)

    assert decision.allowed is True
    stored = db.execute(
        "SELECT allowed, policy_version FROM action_decision_revision WHERE decision_id = %s",
        (decision_id,)).fetchone()
    assert stored == (True, decision.policy_version)


def test_DECIDING_THE_SAME_THING_TWICE_IS_ONE_DECISION(db):
    """Content-addressed, so a double-click does not mint two answers to one question."""
    authorization = _authorized(db)

    first, _ = decide(db, _request(), authorization_id=authorization)
    second, _ = decide(db, _request(), authorization_id=authorization)

    assert first == second
    assert db.execute("SELECT count(*) FROM action_decision_revision").fetchone()[0] == 1


# ══ THE REFUSAL THAT MATTERS ═══════════════════════════════════════════════════════════════════
def test_DRIFT_REFUSES_RATHER_THAN_RE_DECIDING(db):
    """▲ The whole reason the decision persists. Re-evaluating on moved evidence usually returns
    "allowed" again, and the act proceeds under a verdict nobody was shown."""
    authorization = _authorized(db)
    decision_id, _ = decide(db, _request(), authorization_id=authorization)

    with pytest.raises(DecisionDrift, match="has since moved"):
        recheck(db, decision_id, current_pins={**PINS, "formula": "sha256:SOMETHING-ELSE"})


def test_THE_DRIFT_REFUSAL_NAMES_WHICH_PIN_MOVED(db):
    """"Something changed" sends somebody to read everything. Naming it is the actionable half."""
    authorization = _authorized(db)
    decision_id, _ = decide(db, _request(), authorization_id=authorization)

    with pytest.raises(DecisionDrift, match="formula"):
        recheck(db, decision_id, current_pins={**PINS, "formula": "moved"})


def test_UNMOVED_EVIDENCE_RETURNS_THE_RECORDED_ANSWER(db):
    """The same answer, not a fresh one — that is what "checked twice" has to mean."""
    authorization = _authorized(db)
    decision_id, original = decide(db, _request(), authorization_id=authorization)

    rechecked = recheck(db, decision_id, current_pins=PINS)

    assert rechecked.allowed == original.allowed
    assert rechecked.evidence_hash == original.evidence_hash


def test_AN_ACT_WITH_NO_DECISION_IS_A_BYPASS(db):
    with pytest.raises(DecisionMissing, match="queue bypass"):
        recheck(db, "dec-nobody-made", current_pins=PINS)


# ══ ALL MUST PASS ══════════════════════════════════════════════════════════════════════════════
def test_ONE_REFUSED_MEMBER_REFUSES_THE_ACT(db):
    """A caller handed the survivors of a refused group would build a group whose membership nobody
    decided."""
    authorization = _authorized(db)

    decision = ask(db, _request(blockers={"feature_b": ["TARGET_LEAKAGE_BLOCKED"]},
                                warnings={"feature_a": []}),
                   authorization_id=authorization)

    assert decision.allowed is False
    assert [v.member_name for v in decision.per_member if not v.allowed] == ["feature_b"]


def test_WARNINGS_SURVIVE_AN_ALLOWED_DECISION(db):
    """▲ A warning computed and dropped is worse than no warning: it teaches the platform to believe
    it warned."""
    authorization = _authorized(db)

    decision = ask(db, _request(warnings={"feature_a": ["METHOD_CERTIFICATE_MISSING"]}),
                   authorization_id=authorization)

    assert decision.allowed is True
    assert "METHOD_CERTIFICATE_MISSING" in decision.warnings


# ══ THE AUTHORIZATION MUST BE FOR THIS ACT ═════════════════════════════════════════════════════
def test_AN_AUTHORIZATION_FOR_ANOTHER_ACTION_DOES_NOT_AUTHORIZE_THIS_ONE(db):
    """▲ A plain id reference would let a preview decision cite an authorization issued for
    something else and every key would still pass. 1095 solved this shape by keying on the
    relationship; the replacement is at least as strong."""
    authorization = _authorized(db, action=ActionV1.AUTHOR_FORMULA)

    decision = ask(db, _request(ActionV1.GENERATE_PREVIEW), authorization_id=authorization)

    assert decision.allowed is False
    assert "ACTION_AUTHORIZATION_NOT_FOR_THIS_ACT" in decision.blockers


def test_AN_AUTHORIZATION_FOR_ANOTHER_RESOURCE_DOES_NOT_AUTHORIZE_THIS_ONE(db):
    authorization = _authorized(db, resource="some-other-resource")

    decision = ask(db, _request(), authorization_id=authorization)

    assert "ACTION_AUTHORIZATION_NOT_FOR_THIS_ACT" in decision.blockers


def test_NO_AUTHORIZATION_IS_A_REFUSAL_not_a_crash(db):
    decision = ask(db, _request(), authorization_id="auth-nobody-issued")

    assert decision.allowed is False
    assert "ACTION_AUTHORIZATION_MISSING" in decision.blockers


# ══ PRODUCTION IS UNAVAILABLE ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("action", [
    ActionV1.MATERIALIZE_PRODUCTION, ActionV1.PUBLISH_PRODUCTION])
def test_A_PRODUCTION_ACT_CANNOT_BE_DECIDED_AND_RECORDS_NOTHING(db, action):
    """▲ Unavailable, not gated. And nothing is recorded, because a decision names an authorization
    and none can be issued for these acts — the column is NOT NULL deliberately."""
    with pytest.raises(ActionUnavailable, match="unavailable until production governance"):
        decide(db, _request(action), authorization_id="anything")

    assert db.execute("SELECT count(*) FROM action_decision_revision").fetchone()[0] == 0


@pytest.mark.parametrize("action", [
    ActionV1.MATERIALIZE_PRODUCTION, ActionV1.PUBLISH_PRODUCTION])
def test_ASKING_ABOUT_A_PRODUCTION_ACT_ANSWERS_HONESTLY(db, action):
    """Asking is still allowed — a workspace has to be able to say WHY the button is off."""
    decision = ask(db, _request(action), authorization_id="anything")

    assert decision.allowed is False
    assert "ACTION_UNAVAILABLE" in decision.blockers


# ══ IMMUTABILITY ═══════════════════════════════════════════════════════════════════════════════
def test_A_DECISION_CANNOT_BE_EDITED(db):
    import psycopg

    authorization = _authorized(db)
    decision_id, _ = decide(db, _request(), authorization_id=authorization)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE action_decision_revision SET allowed = false WHERE decision_id = %s",
                   (decision_id,))


def test_DECIDING_AGAINST_AN_UNUSABLE_AUTHORIZATION_REFUSES_TYPED_and_records_nothing(db):
    """▲ Workflow finding W6: this used to surface as a bare ForeignKeyViolation out of the INSERT —
    an ungoverned crash that also aborted the caller's transaction (which, on the route, carries the
    queue enqueue) — while ask() with identical inputs returned clean typed blockers. The refusal is
    UNRECORDABLE by the schema's own composite FK, so the type says that instead of the driver."""
    from featuregen.materialize.action_decision import AuthorizationUnusable

    with pytest.raises(AuthorizationUnusable, match="cannot carry a decision"):
        decide(db, _request(), authorization_id="auth-nobody-issued")
    assert db.execute("SELECT count(*) FROM action_decision_revision").fetchone()[0] == 0

    # And a REAL authorization for a DIFFERENT act is equally unusable — the mismatch case.
    other = _authorized(db, action=ActionV1.AUTHOR_FORMULA)
    with pytest.raises(AuthorizationUnusable):
        decide(db, _request(ActionV1.GENERATE_PREVIEW), authorization_id=other)


# ══ OWNER RULING 2026-08-23, ITEM 4 ════════════════════════════════════════════════════════════
def test_DIFFERENT_MEMBER_VERDICTS_ARE_DIFFERENT_DECISIONS_even_over_the_same_pins(db):
    """▲ Member facts are NOT evidence pins, and the id used to omit them — so two decisions over
    the same pins with different member verdicts collided on one id, ON CONFLICT kept whichever was
    first, and the second caller proceeded believing its answer was recorded. The id now covers the
    entire canonical payload, so a same-id conflict IS the identical decision."""
    authorization = _authorized(db)

    clean_id, _ = decide(db, _request(warnings={"feature_a": []}),
                         authorization_id=authorization)
    blocked_id, _ = decide(db, _request(blockers={"feature_a": ["TARGET_LEAKAGE_BLOCKED"]}),
                           authorization_id=authorization)

    assert clean_id != blocked_id
    assert db.execute("SELECT count(*) FROM action_decision_revision").fetchone()[0] == 2


def test_A_CLEAN_MEMBER_IS_RECORDED_BY_NAME_not_vanished(db):
    """▲ The member set used to be derived from the blocker/warning map keys, so a member with
    neither vanished from the record — and "which members did this decision cover" was unanswerable
    for exactly the members that passed."""
    authorization = _authorized(db)

    decision = ask(db, ActionRequestV1(
        action=ActionV1.GENERATE_PREVIEW, resource_identity_hash=RESOURCE,
        member_names=("clean_feature", "warned_feature"),
        member_warnings={"warned_feature": ["METHOD_CERTIFICATE_MISSING"]},
        evidence_pins=PINS), authorization_id=authorization)

    assert [v.member_name for v in decision.per_member] == ["clean_feature", "warned_feature"]
    clean = decision.per_member[0]
    assert clean.allowed is True and clean.blockers == () and clean.warnings == ()


def test_ASK_IS_GENUINELY_READ_ONLY_needing_no_authorization(db):
    """▲ ask() used to REQUIRE an authorization id, so a read-only /plan preflight had to WRITE one
    first — a preflight that writes is not a preflight. Under the development policy decide() mints
    its own server-owned authorization, so its absence at ask time is not a fact about the act."""
    decision = ask(db, _request())

    assert decision.allowed is True
    assert db.execute("SELECT count(*) FROM action_authorization_revision").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM action_decision_revision").fetchone()[0] == 0
