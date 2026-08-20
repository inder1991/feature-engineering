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


def _artifact(db, revision_id, *, artifact_id="art-1", environment=ENV, group=GROUP):
    db.execute(
        "INSERT INTO sealed_artifact_v2 (artifact_id, generation_authorization_revision_id, "
        "environment_id, logical_group_name, compilation_identity_hash, group_plan_hash, "
        "project_digest, subgraph_satisfied, triggered_requirements, subgraph_findings, sealed_at) "
        "VALUES (%s,%s,%s,%s,'sha256:c','sha256:p','sha256:d',true,'[]'::jsonb,'[]'::jsonb,'t')",
        (artifact_id, revision_id, environment, group))
    return artifact_id


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


# ══ THE EDGE THAT MAKES IT A CHAIN: request -> artifact ════════════════════════════════════════
def test_A_REQUEST_MAY_POINT_AT_AN_ARTIFACT_ITS_OWN_APPROVAL_PRODUCED(db):
    revision_id = _authorization(db)
    _artifact(db, revision_id)
    _request(db, revision_id)
    db.execute("UPDATE generation_request SET sealed_artifact_id='art-1' "
               "WHERE request_id='gr-1'")

    assert db.execute("SELECT sealed_artifact_id FROM generation_request "
                      "WHERE request_id='gr-1'").fetchone()[0] == "art-1"


def test_AN_ARTIFACT_FROM_ANOTHER_APPROVAL_IS_UNWRITABLE(db):
    """`sealed_artifact_id` was plain text, so a SUCCEEDED request could claim an artifact somebody
    else's approval produced and nothing in the schema would object. The approval travels inside the
    key, so that row cannot exist."""
    mine = _authorization(db)
    theirs = _authorization(db, build_set="bs-theirs")
    _artifact(db, theirs, artifact_id="art-theirs")
    _request(db, mine)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("UPDATE generation_request SET sealed_artifact_id='art-theirs' "
                   "WHERE request_id='gr-1'")


def test_AN_ARTIFACT_THAT_DOES_NOT_EXIST_IS_UNWRITABLE(db):
    revision_id = _authorization(db)
    _request(db, revision_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("UPDATE generation_request SET sealed_artifact_id='art-imaginary' "
                   "WHERE request_id='gr-1'")


# ══ AUTHORIZATION IS MANDATORY, NOT NULLABLE ═══════════════════════════════════════════════════
def test_A_REQUEST_WITHOUT_AN_APPROVAL_IS_UNWRITABLE(db):
    """An earlier version allowed NULL, reasoning it meant "predates the chain". Both writers
    defaulted the argument to None, so NULL equally meant "a caller forgot" — and a column whose
    absence has two meanings cannot distinguish them. The product is pre-live and both tables were
    empty, so the honest form is NOT NULL."""
    _authorization(db)
    with pytest.raises(psycopg.errors.NotNullViolation):
        db.execute(
            "INSERT INTO generation_request (request_id, build_set_revision_id, environment_id, "
            "status, requested_by, requested_at) VALUES "
            "('gr-null',%s,%s,'REQUESTED','user:ops','2026-08-20T00:00:00Z')",
            (BUILD_SET, ENV))


def test_AN_APPROVAL_FOR_A_NONEXISTENT_BUILD_SET_IS_UNWRITABLE(db):
    """An approval for a build set nobody declared is permission to generate something that does not
    exist. A downstream request rejecting it later is a worse place to learn that than the moment it
    is granted."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        record_generation_authorization(
            db,
            GenerationAuthorizationV1(
                environment_id=ENV, logical_group_name=GROUP,
                build_set_revision_id="bs-never-declared",
                target_mode=TargetModeV1.EXPLORATION, target_ref=None),
            authorized_by="user:ops", authorized_at="2026-08-20T00:00:00Z")


def test_THE_AUDITORS_QUESTION_IS_ANSWERABLE(db):
    """Every request one approval produced — the query the chain exists to make possible, and which
    matching loose fields could only approximate."""
    revision_id = _authorization(db)
    other = _authorization(db, build_set="bs-other")
    _request(db, revision_id, request_id="gr-a")
    _request(db, other, request_id="gr-b", build_set="bs-other")

    produced = [r[0] for r in db.execute(
        "SELECT request_id FROM generation_request "
        "WHERE generation_authorization_revision_id = %s ORDER BY request_id",
        (revision_id,)).fetchall()]
    assert produced == ["gr-a"]
