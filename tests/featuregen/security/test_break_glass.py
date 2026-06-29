import pytest

from featuregen.identity.build import build_human_identity
from featuregen.security.break_glass import (
    BreakGlassError,
    invoke_break_glass,
    sign_off_break_glass_review,
)


def _admin(subject):
    return build_human_identity(subject=subject, role_claims=["platform-admin"])


def test_invoke_requires_two_distinct_admins(db):
    a = _admin("user:adm1")
    with pytest.raises(BreakGlassError):
        invoke_break_glass(db, actor=a, co_signer=a, attempted_action="admin_correct")
    non_admin = build_human_identity(subject="user:b", role_claims=["data_scientist"])
    with pytest.raises(BreakGlassError):
        invoke_break_glass(db, actor=a, co_signer=non_admin, attempted_action="admin_correct")


def test_invoke_records_break_glass_and_opens_review(db):
    a, b = _admin("user:adm1"), _admin("user:adm2")
    review_id = invoke_break_glass(
        db,
        actor=a,
        co_signer=b,
        attempted_action="admin_correct",
        aggregate="run",
        aggregate_id="run_9",
    )
    assert review_id.startswith("bgr_")
    rows = db.execute("SELECT event_type, decision FROM security_audit ORDER BY seq ASC").fetchall()
    assert ("BREAK_GLASS", "allowed_break_glass") in rows
    assert ("BREAK_GLASS_REVIEW_REQUIRED", "flagged") in rows
    pending = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (review_id,)
    ).fetchone()[0]
    assert pending == 1


def test_review_must_be_independent(db):
    a, b = _admin("user:adm1"), _admin("user:adm2")
    review_id = invoke_break_glass(db, actor=a, co_signer=b, attempted_action="admin_correct")
    with pytest.raises(BreakGlassError):
        sign_off_break_glass_review(
            db,
            review_id,
            reviewer=a,
            invoker_subject="user:adm1",
            co_signer_subject="user:adm2",
        )


def test_review_sign_off_records_and_cancels_timer(db):
    a, b = _admin("user:adm1"), _admin("user:adm2")
    review_id = invoke_break_glass(db, actor=a, co_signer=b, attempted_action="admin_correct")
    reviewer = build_human_identity(subject="user:cmp", role_claims=["compliance"])
    sign_off_break_glass_review(
        db,
        review_id,
        reviewer=reviewer,
        invoker_subject="user:adm1",
        co_signer_subject="user:adm2",
    )
    signed = db.execute(
        "SELECT decision FROM security_audit WHERE event_type='BREAK_GLASS_REVIEW'"
    ).fetchall()
    assert signed == [("flagged",)]
    pending = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (review_id,)
    ).fetchone()[0]
    assert pending == 0
