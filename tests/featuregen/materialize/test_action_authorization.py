"""The development authorization policy — permissive by ruling, server-owned by design.

▲ The case worth reading first is `test_PRODUCTION_ACTIONS_CANNOT_BE_AUTHORIZED_AT_ALL`. Unavailable
is not the same as gated: a gated action still has a path a bypass could reach.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.materialize.action_authorization import (
    DEVELOPMENT_POLICY_VERSION,
    ActionAuthorizationV1,
    ActionUnavailable,
    ActionV1,
    action_available,
    authorize_action,
    load_action_authorization,
)

ENV = "hdfc-local"


def _authorize(db, action=ActionV1.GENERATE_PREVIEW, *, actor="user:sam", resource="res-1"):
    return authorize_action(
        db, action=action, resource_identity_hash=resource,
        actor_subject=actor, environment_id=ENV)


# ══ THE POLICY ═════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("action", [
    ActionV1.AUTHOR_FORMULA,
    ActionV1.GENERATE_PREVIEW,
    ActionV1.EXECUTE_SANDBOX,
    ActionV1.PUBLISH_SANDBOX,
])
def test_EVERY_NON_PRODUCTION_ACT_IS_AUTHORIZABLE(db, action):
    """Development policy: any authenticated user may trigger any implemented non-production stage.
    Permissive ON PURPOSE — and recorded, which is the part that is not temporary."""
    authorization = _authorize(db, action)

    assert authorization.permission_result == "allowed"
    assert authorization.policy_version == DEVELOPMENT_POLICY_VERSION


@pytest.mark.parametrize("action", [
    ActionV1.MATERIALIZE_PRODUCTION,
    ActionV1.PUBLISH_PRODUCTION,
])
def test_PRODUCTION_ACTIONS_CANNOT_BE_AUTHORIZED_AT_ALL(db, action):
    """▲ UNAVAILABLE, not gated, and the difference is the whole point: a gated action still has a
    code path a bypass could reach. Nothing is recorded either — an authorization table holds
    authorizations, and a refusal is a decision that belongs elsewhere."""
    assert action_available(action) is False

    with pytest.raises(ActionUnavailable, match="UNAVAILABLE until production governance"):
        _authorize(db, action)

    assert db.execute(
        "SELECT count(*) FROM action_authorization_revision WHERE action = %s",
        (str(action),)).fetchone()[0] == 0


def test_AN_ACT_WITH_NO_ACTOR_IS_NOT_AN_AUTHORIZED_ACT(db):
    """Permission is server-owned. The permissive policy relaxes WHO may act, never whether the
    platform knows who did."""
    with pytest.raises(ValueError, match="who triggered it"):
        _authorize(db, actor="   ")


# ══ IDENTITY ═══════════════════════════════════════════════════════════════════════════════════
def test_THE_SAME_ACT_TWICE_IS_ONE_AUTHORIZATION(db):
    """Content-addressed, so a double-click does not mint two authorizations for one act that could
    later disagree about what was approved."""
    first = _authorize(db)
    second = _authorize(db)

    assert first.authorization_id == second.authorization_id
    assert db.execute("SELECT count(*) FROM action_authorization_revision").fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [
    ("action", ActionV1.AUTHOR_FORMULA),
    ("resource_identity_hash", "res-2"),
    ("actor_subject", "user:other"),
    ("environment_id", "other-env"),
])
def test_EVERY_IDENTITY_FIELD_MOVES_THE_ID(field, value):
    """▲ Each of these changes WHAT WAS AUTHORIZED. A field that did not move the id would let one
    authorization stand for two different acts — which is how an approval for a preview comes to
    cover somebody else's build."""
    base = ActionAuthorizationV1(
        action=ActionV1.GENERATE_PREVIEW, resource_identity_hash="res-1",
        actor_subject="user:sam", environment_id=ENV)
    moved = replace(base, **{field: value})

    assert moved.authorization_id != base.authorization_id


def test_PROVENANCE_STAYS_OUT_OF_THE_IDENTITY(db):
    """`decided_at` is recorded and is NOT part of the id — 1099's rule. Two identical acts decided
    a minute apart are one authorization, not two."""
    first = _authorize(db)
    second = _authorize(db)

    # ▲ Asserted on the STORED row, not just the dataclass: `decided_at` has a default, so the only
    # way to see that it stayed out of the identity is that one act produced one row.
    assert first.authorization_id == second.authorization_id
    assert db.execute(
        "SELECT count(*) FROM action_authorization_revision "
        "WHERE authorization_id = %s", (first.authorization_id,)).fetchone()[0] == 1
    assert db.execute(
        "SELECT decided_at IS NOT NULL FROM action_authorization_revision "
        "WHERE authorization_id = %s", (first.authorization_id,)).fetchone()[0] is True


# ══ THE AUDIT KEY ══════════════════════════════════════════════════════════════════════════════
def test_THE_POLICY_VERSION_IS_STAMPED_ON_EVERY_ROW(db):
    """▲ This is what makes deferring real governance auditable rather than invisible. On the day
    production rules land, "which authorizations were issued under the permissive policy" must be
    ONE QUERY — otherwise a deferral and an omission look identical in hindsight."""
    _authorize(db, ActionV1.AUTHOR_FORMULA)
    _authorize(db, ActionV1.GENERATE_PREVIEW)

    versions = [r[0] for r in db.execute(
        "SELECT DISTINCT policy_version FROM action_authorization_revision").fetchall()]

    assert versions == [DEVELOPMENT_POLICY_VERSION]


# ══ ROUND TRIP ═════════════════════════════════════════════════════════════════════════════════
def test_AN_AUTHORIZATION_ROUND_TRIPS(db):
    authorization = _authorize(db, ActionV1.EXECUTE_SANDBOX)

    read_back = load_action_authorization(db, authorization.authorization_id)

    assert read_back == authorization


def test_A_ROW_THAT_CANNOT_REPRODUCE_ITS_ID_IS_CORRUPTION(db):
    """It would authorize an act on a resource it no longer names. Refusing loudly beats serving an
    authorization whose contents and id disagree."""
    authorization = _authorize(db)
    db.execute(
        "ALTER TABLE action_authorization_revision DISABLE TRIGGER "
        "action_authorization_revision_no_change")
    db.execute(
        "UPDATE action_authorization_revision SET resource_identity_hash = 'tampered' "
        "WHERE authorization_id = %s", (authorization.authorization_id,))

    with pytest.raises(ValueError, match="does not reproduce its own id"):
        load_action_authorization(db, authorization.authorization_id)


def test_AN_UNKNOWN_AUTHORIZATION_IS_NONE_not_an_error(db):
    assert load_action_authorization(db, "auth-nobody-issued") is None


# ══ APPEND-ONLY ════════════════════════════════════════════════════════════════════════════════
def test_AN_AUTHORIZATION_CANNOT_BE_EDITED(db):
    """An authorization that can be rewritten is a record of what somebody currently wishes had been
    approved — the actor above all, since rewriting it re-attributes the act."""
    import psycopg

    authorization = _authorize(db)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(
            "UPDATE action_authorization_revision SET actor_subject = 'user:someone-else' "
            "WHERE authorization_id = %s", (authorization.authorization_id,))


def test_AN_AUTHORIZATION_CANNOT_BE_DELETED(db):
    """▲ Its own test rather than a second arm of the one above: a failed statement aborts the
    transaction, so recovering inside one test means rolling back — which also discards the row
    being tested, and the DELETE then hits nothing and passes for the wrong reason."""
    import psycopg

    authorization = _authorize(db)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(
            "DELETE FROM action_authorization_revision WHERE authorization_id = %s",
            (authorization.authorization_id,))
