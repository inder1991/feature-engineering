from __future__ import annotations

import psycopg
import pytest
from tests.featuregen._helpers import mint_test_service_identity
from ulid import ULID

from featuregen.aggregates._append import append
from featuregen.events.registry import event_registry

_EVENT_TYPE = "WRITE_ONCE_TEST"


def _actor():
    return mint_test_service_identity(
        subject="service:write-once-test",
        role_claims=["overlay"],
        attestation="signed-deploy-id:write-once@1.0.0",
    )


@pytest.fixture
def seed_event(db):
    """Append one event via the production `append` path and return its event_id.

    The row is seeded INSIDE the test's own (uncommitted) transaction — never `db.commit()`ed —
    so it stays private to this test and the `conn` fixture's teardown rollback discards it
    (no isolation pollution of the shared session DB). To survive the failing mutations below,
    each mutation is wrapped in its own SAVEPOINT (`with db.transaction():`) so the trigger's
    RaiseException aborts only that savepoint, leaving the outer transaction — and the seeded
    row — intact for the next assertion."""

    def _seed() -> str:
        event_registry().register_schema(_EVENT_TYPE, 1, {"type": "object"}, "overlay")
        agg_id = f"req_{ULID()}"
        env = append(
            db,
            aggregate="request",
            aggregate_id=agg_id,
            request_id=agg_id,
            type=_EVENT_TYPE,
            payload={"seed": True},
            actor=_actor(),
        )
        return env.event_id

    return _seed


def test_events_are_write_once(db, seed_event) -> None:
    """An appended event row must be immutable at the DB level (review BLOCKER #4).

    Each mutation runs inside a nested `with db.transaction():` savepoint so the trigger's
    RaiseException rolls back only that savepoint; the outer test transaction (holding the
    seeded row) survives, so the DELETE below still targets the live row and fires the trigger."""
    ev = seed_event()  # seeded in the test txn, NOT committed
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute("UPDATE events SET type='TAMPERED' WHERE event_id=%s", (ev,))
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute("DELETE FROM events WHERE event_id=%s", (ev,))
