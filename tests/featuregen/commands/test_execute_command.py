import pytest
from tests.featuregen._helpers import make_cmd

from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import (
    AuthzDecision,
    current_authorizer,
    register_command_authorizer,
)
from featuregen.commands.registry import clear_registry, register_command
from featuregen.contracts import CommandResult


class _AllowAll:
    def authorize(self, conn, cmd):
        return AuthzDecision(allowed=True)


@pytest.fixture(autouse=True)
def _clean_registry():
    clean_authorizer = current_authorizer()
    clear_registry()
    # These seam/dispatch tests are not exercising authz; register an explicit allow-all so they
    # are unaffected by the fail-safe deny-all default. Authz-specific tests override below.
    register_command_authorizer(_AllowAll())
    yield
    clear_registry()
    register_command_authorizer(clean_authorizer)


def test_dispatch_routes_to_registered_handler(db):
    def handler(conn, cmd):
        return CommandResult(accepted=True, aggregate_id="agg1", produced_event_ids=("e1",))

    register_command("act", handler)
    res = execute_command(db, make_cmd("act", "run", "agg1", {}))
    assert res.accepted and res.produced_event_ids == ("e1",)


def test_duplicate_idempotency_key_replays_original(db):
    calls = []

    def handler(conn, cmd):
        calls.append(1)
        return CommandResult(accepted=True, aggregate_id="agg1", produced_event_ids=("e1",))

    register_command("act", handler)
    cmd = make_cmd("act", "run", "agg1", {}, idem="k1")
    first = execute_command(db, cmd)
    second = execute_command(db, cmd)
    assert first == second
    assert calls == [1]
    rows = db.execute(
        "SELECT count(*) FROM command_idempotency WHERE idempotency_key = %s", ("k1",)
    ).fetchone()[0]
    assert rows == 1


def test_authz_denial_returns_not_accepted_and_does_not_dispatch(db):
    called = []
    register_command("act", lambda c, m: called.append(1))

    class Deny:
        def authorize(self, conn, cmd):
            return AuthzDecision(allowed=False, reason="not permitted")

    register_command_authorizer(Deny())
    res = execute_command(db, make_cmd("act", "run", "agg1", {}))
    assert res.accepted is False
    assert res.denied_reason == "not permitted"
    assert called == []


def test_unconfigured_default_authorizer_denies_state_mutating_command(db):
    # Fail-safe: with NO real authorizer registered (bootstrap_phase07 not run), the factory
    # default MUST deny every state-mutating command and never dispatch the handler.
    from featuregen.commands import authz_seam

    assert isinstance(authz_seam._DEFAULT_AUTHORIZER, authz_seam._DenyAllAuthorizer)
    called = []
    register_command("act", lambda c, m: called.append(1))
    register_command_authorizer(authz_seam._DenyAllAuthorizer())
    res = execute_command(db, make_cmd("act", "run", "agg1", {}))
    assert res.accepted is False
    assert called == []
    assert "no command authorizer configured" in (res.denied_reason or "")


def test_degraded_run_is_blocked(db):
    db.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, degraded) "
        "VALUES ('run_deg', 'req_x', 'DRAFT', 1, true)"
    )
    register_command("act", lambda c, m: CommandResult(accepted=True, aggregate_id="run_deg"))
    res = execute_command(db, make_cmd("act", "run", "run_deg", {}))
    assert res.accepted is False
    assert "degraded" in res.denied_reason


def test_denied_command_is_not_cached(db):
    register_command("act", lambda c, m: CommandResult(accepted=True, aggregate_id="agg1"))

    class Deny:
        def authorize(self, conn, cmd):
            return AuthzDecision(allowed=False, reason="nope")

    register_command_authorizer(Deny())
    execute_command(db, make_cmd("act", "run", "agg1", {}, idem="dk"))
    rows = db.execute(
        "SELECT count(*) FROM command_idempotency WHERE idempotency_key = %s", ("dk",)
    ).fetchone()[0]
    assert rows == 0  # denials release the claim; a later legitimate retry can run


def test_accepted_command_stores_final_non_pending_result(db):
    register_command(
        "act",
        lambda c, m: CommandResult(accepted=True, aggregate_id="agg1", produced_event_ids=("e1",)),
    )
    execute_command(db, make_cmd("act", "run", "agg1", {}, idem="fk"))
    stored = db.execute(
        "SELECT result FROM command_idempotency WHERE idempotency_key = %s", ("fk",)
    ).fetchone()[0]
    assert stored.get("_pending") is None  # claim was finalized, not left pending
    assert stored["accepted"] is True and stored["produced_event_ids"] == ["e1"]


def test_replay_does_not_rerun_handler_when_prior_committed(db):
    # Simulate a prior committed winner by pre-inserting a finalized idempotency row.
    db.execute(
        "INSERT INTO command_idempotency (idempotency_key, action, result) VALUES "
        "(%s, %s, %s::jsonb)",
        (
            "pre",
            "act",
            '{"accepted": true, "aggregate_id": "agg9", "produced_event_ids": ["x1"], '
            '"denied_reason": null}',
        ),
    )
    calls = []
    register_command("act", lambda c, m: calls.append(1))
    res = execute_command(db, make_cmd("act", "run", "agg9", {}, idem="pre"))
    assert res.accepted and res.aggregate_id == "agg9" and res.produced_event_ids == ("x1",)
    assert calls == []  # handler never invoked; result replayed from the committed claim
