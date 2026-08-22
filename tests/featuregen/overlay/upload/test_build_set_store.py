"""The build set and the attempts made on it — immutability, idempotency, and all-or-nothing.

Step 3 of the V2-only plan. The rules under test, in the order they cost something:

1. **A build set is immutable**, because every artifact downstream is derived from its membership.
2. **All or nothing**: no partial build, because a group's identity IS its membership.
3. **One live attempt per set and environment** — a double-click must not start a second compile.
4. **A retry after failure is allowed**, which is a different thing from a double-click.
5. **The lifecycle is explicit**, so "running" is distinguishable from "the worker died".
"""
from __future__ import annotations

import psycopg
import pytest
from tests.featuregen.materialize.crosswalk_fixtures import (
    bind_ready_formula,
    bind_ready_formulas,
    build_set_declaration,
)

from featuregen.overlay.upload.build_set_store import (
    GenerationStatusV1,
    InvalidStatusMove,
    advance_request,
    build_set_identity,
    read_build_set,
    read_request,
    record_build_set,
    request_generation,
)


def _sealed(db, approval: str, *, artifact_id: str = "art-1", environment: str) -> str:
    """A real sealed artifact under this approval — the request's FK target.

    `sealed_artifact_id` was plain text, so a SUCCEEDED request could name an artifact that does not
    exist and nothing objected. It now travels inside a composite key with the approval and the
    environment, which is why this fixture has to exist at all.
    """
    db.execute(
        "INSERT INTO sealed_artifact_v2 (artifact_id, generation_authorization_revision_id, "
        "environment_id, logical_group_name, compilation_identity_hash, group_plan_hash, "
        "project_digest, subgraph_satisfied, triggered_requirements, subgraph_findings, sealed_at) "
        "VALUES (%s,%s,%s,'customer_txn_features','sha256:c','sha256:p','sha256:d',"
        "true,'[]'::jsonb,'[]'::jsonb,'t') ON CONFLICT DO NOTHING",
        (artifact_id, approval, environment))
    return artifact_id


def _approval(db, build_set: str, environment: str) -> str:
    """A real approval covering this build set here — the request's FK target.

    Requests carry a MANDATORY authorization now. An earlier version allowed NULL and called it
    "predates the chain", but both writers defaulted the argument to None, so NULL equally meant
    "a caller forgot" — a column whose absence has two meanings cannot tell them apart.
    """
    from featuregen.materialize.generation_authorization import (
        GenerationAuthorizationV1,
        record_generation_authorization,
    )
    from featuregen.overlay.upload.selection_revisions import TargetModeV1

    return record_generation_authorization(
        db, GenerationAuthorizationV1(
            environment_id=environment, logical_group_name="customer_txn_features",
            build_set_revision_id=build_set,
            target_mode=TargetModeV1.EXPLORATION, target_ref=None),
        authorized_by="user:ops", authorized_at="t")


ENV = "hdfc-local"


def _target(conn, revision_id: str = "trr-1") -> str:
    """A target reading, which a build set must point at: a set with no target predicts nothing."""
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s,'int-1','exploration','sha256:target') ON CONFLICT DO NOTHING", (revision_id,))
    return revision_id


def _selection(conn, revision_id: str, target: str = "trr-1") -> str:
    conn.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, binding_plan_hash, "
        "content_hash) VALUES (%s,%s,'crev-1',%s,%s,'sha256:asked','sha256:binding',%s) "
        "ON CONFLICT DO NOTHING",
        (revision_id, target, f"opt-{revision_id}", f"dec-{revision_id}",
         f"sha256:{revision_id}"))
    return revision_id


def _set(conn, revision_id="bs-1", members=("sel-a", "sel-b"), target="trr-1"):
    """▲ Every member now arrives with a PINNED FORMULA. A build set whose members named only
    selections resolved the formula at build time as "the newest draft", so a re-author between the
    decision and the build changed what the build meant under an unchanged identity (1101)."""
    _target(conn, target)
    for m in members:
        _selection(conn, m, target)
    return record_build_set(
        conn, revision_id=revision_id, target_reading_revision_id=target,
        selection_formula_binding_ids=bind_ready_formulas(conn, members),
        declaration=build_set_declaration(),
        declared_by="user:ops", declared_at="2026-08-18T00:00:00Z")


# ══ THE SET IS IMMUTABLE ════════════════════════════════════════════════════════════════════════
def test_A_BUILD_SET_CANNOT_BE_EDITED(db):
    """Every artifact downstream — group plan, contract, sealed project, published columns — is
    derived from this membership. If it could be edited, a sealed artifact could stop matching the
    request it came from while both still looked current."""
    _set(db)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        db.execute("UPDATE build_set_revision SET target_reading_revision_id = 'trr-other' "
                   "WHERE revision_id = 'bs-1'")


def test_a_member_cannot_be_added_or_removed_after_the_fact(db):
    """Changing your mind mints a NEW set. Editing membership in place would silently change what a
    person asked for, under an identity that says it did not change."""
    _set(db)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        db.execute("DELETE FROM build_set_member WHERE revision_id = 'bs-1' AND position = 0")


def test_THE_ORDER_SURVIVES_A_ROUND_TRIP(db):
    """The order a person chose features in is a fact about the build, and a set would discard it."""
    _set(db, members=("sel-c", "sel-a", "sel-b"))
    assert read_build_set(db, "bs-1").selection_revision_ids == ("sel-c", "sel-a", "sel-b")


def test_the_same_declaration_is_the_SAME_SET(db):
    """Two people asking for the same build get the same set — which makes a re-request cheap
    rather than a duplicate, and keeps one root for its attempts instead of two."""
    first_id, first_created = _set(db, "bs-1")
    second_id, second_created = _set(db, "bs-2")     # same members, target and declaration

    assert first_created is True
    assert second_created is False
    assert second_id == first_id == "bs-1"
    assert db.execute("SELECT count(*) FROM build_set_revision").fetchone()[0] == 1


def test_ORDER_IS_PART_OF_THE_IDENTITY(db):
    """A hash over a SET would quietly disagree with the dataclass, which says order is meaningful."""
    a = build_set_identity(target_reading_revision_id="t",
                       selection_formula_binding_ids=["x", "y"],
                           declaration_hash="d")
    b = build_set_identity(target_reading_revision_id="t",
                       selection_formula_binding_ids=["y", "x"],
                           declaration_hash="d")
    assert a != b


def test_a_duplicate_selection_is_refused(db):
    """The same selection twice makes 'which position is this feature in' unanswerable."""
    _target(db)
    _selection(db, "sel-a")
    with pytest.raises(ValueError, match="appears twice"):
        record_build_set(
            db, revision_id="bs-dup", target_reading_revision_id="trr-1",
            selection_formula_binding_ids=("b-1", "b-1"),
            declaration=build_set_declaration(),
            declared_by="user:ops", declared_at="2026-08-18T00:00:00Z")


def test_an_empty_build_set_is_refused(db):
    """A build set with no selections builds nothing, and would sit in the table looking like work."""
    _target(db)
    with pytest.raises(ValueError, match="builds nothing"):
        record_build_set(
            db, revision_id="bs-empty", target_reading_revision_id="trr-1",
            selection_formula_binding_ids=(), declaration=build_set_declaration(),
            declared_by="user:ops", declared_at="2026-08-18T00:00:00Z")


# ══ ONE LIVE ATTEMPT, BUT RETRY IS ALLOWED ══════════════════════════════════════════════════════
def test_A_DOUBLE_CLICK_DOES_NOT_START_A_SECOND_COMPILE(db):
    """Idempotent on the WORK — the build set and environment — not on a caller-supplied key, which
    a client minting a fresh one per click would defeat. Generation costs real compute."""
    _set(db)
    first, created_first = request_generation(
        db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
        requested_by="user:ops", requested_at="t",
        generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    second, created_second = request_generation(
        db, request_id="gr-2", build_set_revision_id="bs-1", environment_id=ENV,
        requested_by="user:ops", requested_at="t",
        generation_authorization_revision_id=_approval(db, "bs-1", ENV))

    assert created_first is True
    assert created_second is False
    assert second == first == "gr-1"
    assert db.execute("SELECT count(*) FROM generation_request").fetchone()[0] == 1


def test_A_RETRY_AFTER_A_FAILURE_IS_ALLOWED(db):
    """The guard protects against double-clicks, not against recovery.

    A failed attempt must not hold a build set hostage — that is the defect the publication lane
    has, where an unresolved attempt blocks retries permanently.
    """
    _set(db)
    request_generation(db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    advance_request(db, "gr-1", GenerationStatusV1.FAILED, failure_reason="provider unreachable")

    retry, created = request_generation(
        db, request_id="gr-2", build_set_revision_id="bs-1", environment_id=ENV,
        requested_by="user:ops", requested_at="t",
        generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    assert created is True and retry == "gr-2"


def test_the_same_set_in_a_DIFFERENT_environment_is_a_different_attempt(db):
    """Building for local and for prod are two builds, not one — the artifact differs."""
    _set(db)
    request_generation(db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    other, created = request_generation(
        db, request_id="gr-2", build_set_revision_id="bs-1", environment_id="hdfc-prod",
        requested_by="user:ops", requested_at="t",
        generation_authorization_revision_id=_approval(db, "bs-1", "hdfc-prod"))
    assert created is True and other == "gr-2"


# ══ THE LIFECYCLE IS EXPLICIT ═══════════════════════════════════════════════════════════════════
def test_THE_HAPPY_PATH_VISITS_EVERY_STAGE(db):
    """So "still running" is distinguishable from "the worker died" and from "nothing consumes this
    table" — the gap S11's verification attempts have and this deliberately does not."""
    _set(db)
    approval = _approval(db, "bs-1", ENV)
    # The artifact must EXIST and be one this approval produced — `sealed_artifact_id` is no longer
    # free text, so succeeding into a name nobody sealed is now unwritable rather than unnoticed.
    _sealed(db, approval, environment=ENV)
    request_generation(db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=approval)
    for stage in (GenerationStatusV1.CLAIMED, GenerationStatusV1.RUNNING):
        advance_request(db, "gr-1", stage)
    advance_request(db, "gr-1", GenerationStatusV1.SUCCEEDED, sealed_artifact_id="art-1")

    request = read_request(db, "gr-1")
    assert request.status is GenerationStatusV1.SUCCEEDED
    assert request.sealed_artifact_id == "art-1"
    assert request.stage_label == "Code ready"


def test_a_SKIPPED_STAGE_IS_REFUSED(db):
    """A SUCCEEDED request whose history says it was never worked on would be a status with nothing
    behind it."""
    _set(db)
    request_generation(db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    with pytest.raises(InvalidStatusMove, match="cannot move"):
        advance_request(db, "gr-1", GenerationStatusV1.SUCCEEDED, sealed_artifact_id="art-1")


def test_SUCCEEDING_WITHOUT_AN_ARTIFACT_IS_REFUSED_BY_THE_SCHEMA(db):
    """The whole point of the request is the artifact; succeeding without one is a status with
    nothing behind it."""
    _set(db)
    request_generation(db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    advance_request(db, "gr-1", GenerationStatusV1.CLAIMED)
    advance_request(db, "gr-1", GenerationStatusV1.RUNNING)
    with pytest.raises(psycopg.errors.CheckViolation):
        advance_request(db, "gr-1", GenerationStatusV1.SUCCEEDED)


def test_A_REFUSAL_MUST_NAME_WHAT_REFUSED_IT(db):
    """All-or-nothing is only defensible if the refusal is actionable.

    A group's identity IS its membership, so building four of five silently delivers something
    nobody asked for. That is acceptable ONLY because the refusal names the member that could not be
    built — a quiet four-fifths would not be.
    """
    _set(db)
    request_generation(db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    with pytest.raises(psycopg.errors.CheckViolation):
        advance_request(db, "gr-1", GenerationStatusV1.REFUSED)      # no refusals named

    db.rollback()
    _set(db)
    request_generation(db, request_id="gr-2", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    advance_request(db, "gr-2", GenerationStatusV1.REFUSED, refusals=[
        {"selection_revision_id": "sel-b", "code": "FORMULA_SCHEMA_UNSUPPORTED",
         "reason": "the renderer cannot emit aggregate:median in this build"}])
    assert read_request(db, "gr-2").refusals[0]["selection_revision_id"] == "sel-b"


def test_a_refusal_is_reachable_from_ANY_live_stage(db):
    """A member with no formula is knowable at the start; an unsupported operator only once the
    formula is read. Both are refusals, and forcing the early one through stages that never happened
    would write a history that is not true."""
    _set(db)
    request_generation(db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    advance_request(db, "gr-1", GenerationStatusV1.REFUSED,
                    refusals=[{"selection_revision_id": "sel-a", "code": "NO_READY_FORMULA",
                               "reason": "this feature has no drafted formula yet"}])
    assert read_request(db, "gr-1").status is GenerationStatusV1.REFUSED


def test_the_request_identity_is_FROZEN(db):
    """Status and results move; which set, which environment and who asked do not."""
    _set(db)
    request_generation(db, request_id="gr-1", build_set_revision_id="bs-1", environment_id=ENV,
                       requested_by="user:ops", requested_at="t",
                       generation_authorization_revision_id=_approval(db, "bs-1", ENV))
    with pytest.raises(psycopg.errors.RaiseException, match="frozen"):
        db.execute("UPDATE generation_request SET environment_id = 'somewhere-else' "
                   "WHERE request_id = 'gr-1'")


def test_the_SAME_DECLARATION_MADE_TWICE_IS_ONE_BUILD_SET(db) -> None:
    """▲ THE PROPERTY THE IDENTITY PROJECTION EXISTS FOR, and it was unasserted until now.

    `SpineSourceDeclarationV1` carries provenance — `declared_by`, `recorded_at`,
    `declaration_record_id` — and `record_build_set` hashes the declaration into the set's identity.
    If that hash covered provenance, the SAME population declared twice would be two build sets, one
    per clock read, and "two people asking for the same build get the same set" would quietly stop
    being true. The spine type solves this for its own contract hash with `identity_payload`; this
    asserts the build set uses it.
    """
    import dataclasses

    from tests.featuregen.materialize.crosswalk_fixtures import (
        SPINE_DECLARATION,
        build_set_declaration,
    )

    conn = db
    _target(conn, "trr-prov")
    _selection(conn, "sel-a", "trr-prov")
    later = build_set_declaration(spine_declaration=dataclasses.replace(
        SPINE_DECLARATION,
        recorded_at="2027-01-01T00:00:00+00:00",
        declaration_record_id="a-different-record",
        declaration_reason="typed up again by somebody else"))

    first, created_first = record_build_set(
        conn, revision_id="bsr-prov-1", target_reading_revision_id="trr-prov",
        selection_formula_binding_ids=(bind_ready_formula(conn, "sel-a"),),
        declaration=build_set_declaration(),
        declared_by="engineer@bank", declared_at="2026-08-22T09:00:00+00:00")
    second, created_second = record_build_set(
        conn, revision_id="bsr-prov-2", target_reading_revision_id="trr-prov",
        selection_formula_binding_ids=(bind_ready_formula(conn, "sel-a"),),
        declaration=later,
        declared_by="colleague@bank", declared_at="2027-01-01T00:00:00+00:00")

    assert created_first is True
    assert created_second is False, "provenance moved the build set identity"
    assert second == first
