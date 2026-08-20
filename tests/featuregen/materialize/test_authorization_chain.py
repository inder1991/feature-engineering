"""authorization → generation request → sealed artifact, as REFERENCES rather than loose fields.

Every link existed as a value and none as a reference. `generation_request` recorded a build set and
an environment but never which approval permitted the work; `sealed_artifact_v2` recorded an
environment and a group but never what authorized producing it. So "which approval produced this
artifact" — the question an auditor asks about any published number — was answerable only by
matching fields and hoping the match meant something.

**The point of these tests is that a disagreement is UNWRITABLE, not that it is caught.** The
referenced columns travel inside the composite foreign key, so a request naming an authorization
issued for another environment is refused by the database. A checked-in-code version would leave the
row representable and rely on every future caller remembering to look.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.materialize.generation_authorization import (
    GenerationAuthorizationV1,
    record_generation_authorization,
)
from featuregen.overlay.upload.build_set_store import request_generation
from featuregen.overlay.upload.selection_revisions import TargetModeV1

ENV = "hdfc-local"
GROUP = "customer_txn_features"
BUILD_SET = "bs-chain-1"


def _authorization(db, *, environment=ENV, group=GROUP, build_set=BUILD_SET) -> str:
    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, redacted_hypothesis) "
        "VALUES ('int-1','h','hypothesis','h') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES ('trr-1','int-1','exploration','h') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
        "declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
        "VALUES (%s,'trr-1','dh','{}'::jsonb,%s,'user:ops','2026-08-20T00:00:00Z') "
        "ON CONFLICT DO NOTHING", (build_set, build_set))
    return record_generation_authorization(
        db,
        GenerationAuthorizationV1(
            environment_id=environment, logical_group_name=group,
            build_set_revision_id=build_set,
            target_mode=TargetModeV1.EXPLORATION, target_ref=None),
        authorized_by="user:ops", authorized_at="2026-08-20T00:00:00Z")


def _request(db, revision_id, *, request_id="gr-1", build_set=BUILD_SET, environment=ENV):
    return request_generation(
        db, request_id=request_id, build_set_revision_id=build_set,
        environment_id=environment, requested_by="user:ops",
        requested_at="2026-08-20T00:00:00Z",
        generation_authorization_revision_id=revision_id)


# ══ THE CHAIN HOLDS ════════════════════════════════════════════════════════════════════════════
def test_A_REQUEST_NAMES_THE_APPROVAL_THAT_PERMITTED_IT(db):
    revision_id = _authorization(db)
    _request(db, revision_id)

    stored = db.execute(
        "SELECT generation_authorization_revision_id FROM generation_request "
        "WHERE request_id='gr-1'").fetchone()[0]
    assert stored == revision_id


def test_AN_AUTHORIZATION_FOR_ANOTHER_ENVIRONMENT_IS_UNWRITABLE(db):
    """Not caught — UNWRITABLE. The environment travels inside the composite key, so a request
    citing an approval issued for a different cluster cannot exist as a row."""
    elsewhere = _authorization(db, environment="prod", build_set="bs-prod")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _request(db, elsewhere)


def test_AN_AUTHORIZATION_FOR_ANOTHER_BUILD_SET_IS_UNWRITABLE(db):
    """The same argument on the other referenced column: an approval covers a build SET, so citing
    one issued for a different set would claim permission nobody granted for this work."""
    other = _authorization(db, build_set="bs-other")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _request(db, other, build_set=BUILD_SET)


def test_AN_AUTHORIZATION_THAT_DOES_NOT_EXIST_IS_UNWRITABLE(db):
    _authorization(db)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _request(db, "gar-never-issued")


# ══ NULL IS "PREDATES THE CHAIN", AND IS NOT AN AUTHORIZATION ══════════════════════════════════
def test_NULL_IS_ACCEPTED_AND_IS_NOT_AN_APPROVAL(db):
    """The V1 chain writes no authorization, and backfilling one for rows that never had it would
    invent the very evidence this chain exists to make trustworthy.

    NULL is therefore legitimate and distinguishable from every authorization — it can never be
    confused for one, because no query matching a revision id will return it.
    """
    _authorization(db)
    _request(db, None, request_id="gr-legacy")

    stored = db.execute(
        "SELECT generation_authorization_revision_id FROM generation_request "
        "WHERE request_id='gr-legacy'").fetchone()[0]
    assert stored is None


def test_THE_AUDITORS_QUESTION_IS_ANSWERABLE(db):
    """Every request one approval produced — the query the chain exists to make possible, and which
    matching loose fields could only approximate."""
    revision_id = _authorization(db)
    _request(db, revision_id, request_id="gr-a")
    _request(db, None, request_id="gr-legacy")

    produced = [r[0] for r in db.execute(
        "SELECT request_id FROM generation_request "
        "WHERE generation_authorization_revision_id = %s ORDER BY request_id",
        (revision_id,)).fetchall()]
    assert produced == ["gr-a"]
