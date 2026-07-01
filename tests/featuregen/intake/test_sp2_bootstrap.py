from __future__ import annotations

from featuregen.events.registry import event_registry
from featuregen.intake.bootstrap import seed_sp2_authz


def _authz_rows(conn):
    return {
        (r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT action, gate, permitted_role, actor_kind FROM authz_policy"
        ).fetchall()
    }


def test_seed_adds_the_additive_rejection_authority(conn):
    seed_sp2_authz(conn)
    rows = _authz_rows(conn)
    assert ("reject_intent", "", "intake-agent", "service") in rows
    # SP-2 issues its OWN reject_intent — it never seeds/reuses SP-0's validator-only `reject`
    # (authz/policy.py:42 stays untouched; SoD holds).
    assert not any(action == "reject" for (action, _gate, _role, _kind) in rows)
    # No onboarding-answer row is added (deferred, §14).
    assert not any(action == "answer_use_case_onboarding" for (action, _g, _r, _k) in rows)


def test_seed_adds_all_eight_command_capability_rows(conn):
    seed_sp2_authz(conn)
    rows = _authz_rows(conn)
    expected = {
        ("submit_intent", "", "data_scientist", "human"),
        ("submit_intent", "", "intake-agent", "service"),
        ("answer_clarification", "", "data_scientist", "human"),
        ("select_candidate_doc", "", "data_scientist", "human"),
        ("open_gate1_task", "", "intake-agent", "service"),
        ("confirm_contract", "", "data_scientist", "human"),
        ("request_edit", "", "data_scientist", "human"),
        ("reject_intent", "", "intake-agent", "service"),
    }
    assert expected <= rows


def test_seed_wires_primary_selected_and_checkpoints(conn):
    seed_sp2_authz(conn)
    # PRIMARY_SELECTED registered durably + in-memory (so candidate-promotion appends validate).
    durable = conn.execute(
        "SELECT 1 FROM event_type_registry WHERE type_name='PRIMARY_SELECTED'"
    ).fetchone()
    assert durable is not None
    assert event_registry().max_active_versions().get("PRIMARY_SELECTED") == 1
    # stage_primary + feature_contract checkpoints exist.
    names = {
        r[0]
        for r in conn.execute(
            "SELECT projection_name FROM projection_checkpoints "
            "WHERE projection_name IN ('stage_primary','feature_contract')"
        ).fetchall()
    }
    assert names == {"stage_primary", "feature_contract"}


def test_seed_is_idempotent(conn):
    seed_sp2_authz(conn)
    seed_sp2_authz(conn)  # must not raise (ON CONFLICT DO NOTHING everywhere)
    n = conn.execute(
        "SELECT count(*) FROM authz_policy WHERE action='reject_intent'"
    ).fetchone()[0]
    assert n == 1
