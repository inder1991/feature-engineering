"""Shared fixtures/helpers for the upload overlay suites (Phase 2 / Pass B, Tasks 7-12).

Sits under ``tests/featuregen/overlay/conftest.py`` (which autoregisters the overlay commands +
event types per test and owns the ``catalog``/StubCatalog fixture). This conftest adds what the
Pass B tasks need: the upload-context catalog adapter (``overlay_conn``), the service proposer /
platform-admin confirmer pair the four-eyes flow needs, a seeded physical graph, and
confirm/reconfirm/reject helpers that dispatch the REAL gate commands against the open grain task
and DRAIN THE PROJECTION afterwards — ``resolve_fact`` reads the ``overlay_fact_state`` read model,
which only the projection populates (``confirm_fact`` does not), so a helper that skipped the drain
would leave later ``resolve_fact``-based assertions reading a stale model.
"""
from __future__ import annotations

import pytest

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.identity import fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref
from featuregen.projections.runner import run_projection


@pytest.fixture(autouse=True)
def _clear_upload_adapter():
    """Task-1 carry-forward: ``ingest_upload``/``ensure_upload_catalog_adapter`` registers a
    PROCESS-GLOBAL adapter and leaves it behind; clear it after every test in this package so it
    never leaks into a test that expects ``current_catalog_adapter()`` to fail closed. Double-clear
    is a no-op, so this coexists with the overlay ``catalog`` fixture's own teardown."""
    yield
    _clear_catalog_adapter()


@pytest.fixture
def overlay_conn(db):
    """The ephemeral-PG connection (``db``: writes roll back on teardown) with the upload-context
    adapter registered, so propose/confirm/expiry resolve an adapter instead of RuntimeError."""
    ensure_upload_catalog_adapter()
    return db


@pytest.fixture
def service_actor():
    """A NON-human service proposer (mirrors ``enrich_llm._ENRICH_ACTOR``) so four-eyes holds when
    a human later confirms what the service proposed (proposer != confirmer)."""
    return IdentityEnvelope(subject="featuregen-overlay-enrichment", actor_kind="service",
                            authenticated=True, auth_method="internal", role_claims=())


@pytest.fixture
def human_actor():
    """A platform-admin HUMAN confirmer. MUST hold ``platform-admin``: grain/availability route to
    the platform-admin GOVERNANCE queue (``UploadContextAdapter.owner_of`` -> None), so a
    data_owner-role confirmer would be DENIED by the authority check."""
    from tests.featuregen._helpers import mint_test_identity
    return mint_test_identity(subject="user:admin", role_claims=("platform-admin",))


@pytest.fixture
def seeded_graph(db):
    """``graph_node`` rows for source ``src``, table ``txn``, columns id/amt/txn_id
    (kind='column', is_grain=false) — the physical columns later tasks reason over."""
    for col in ("id", "amt", "txn_id"):
        db.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name,"
            " is_grain, is_as_of) VALUES ('src', %s, 'column', 'txn', %s, false, false)",
            (f"public.txn.{col}", col))
    return db


def _drain(conn) -> None:
    """Run the overlay projection until caught up (one pass caps at 500 events). Every
    confirm/reconfirm/reject helper MUST end here: ``resolve_fact`` reads ``overlay_fact_state``,
    which is populated ONLY by the projection."""
    while run_projection(conn, OverlayProjection()) >= 500:
        pass


def _open_grain_task(conn, source, table, *, actor, fact_type="grain"):
    """``(task_id, target_event_id, ref)`` for the open gate task on this table's ``fact_type``
    fact (opened by ``propose_fact`` or the expiry poller). The CAS target is read through
    ``get_task_proposal`` — the authorized, task-scoped read a real confirmer uses."""
    from featuregen.overlay.task_read import get_task_proposal

    ref = table_ref(source, table)
    key = fact_key(ref, fact_type)
    row = conn.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open'"
        " ORDER BY created_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    assert row is not None, f"no open {fact_type} gate task for {source}.{table}"
    task_id = row[0]
    proposal = get_task_proposal(conn, task_id, actor)
    return task_id, proposal["target_event_id"], ref


def _confirm_grain(conn, source, table, columns, *, actor):
    """Confirm the open grain task -> VERIFIED with ``{columns, is_unique: True}``; drain the
    projection so ``resolve_fact`` sees VERIFIED. ``actor`` must be the platform-admin human."""
    from featuregen.overlay.commands import confirm_fact

    _task_id, target_event_id, ref = _open_grain_task(conn, source, table, actor=actor)
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id,
         "value": {"columns": columns, "is_unique": True}},
        actor, f"confirm-{target_event_id}"))
    assert res.accepted, res.denied_reason
    _drain(conn)
    return res


def _reject_grain(conn, source, table, *, actor):
    """Reject the open grain task -> REJECTED (sticky fingerprint); drain the projection."""
    from featuregen.overlay.commands import reject_fact

    _task_id, target_event_id, ref = _open_grain_task(conn, source, table, actor=actor)
    res = reject_fact(conn, Command(
        "reject_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id,
         "reason": "not the grain"},
        actor, f"reject-{target_event_id}"))
    assert res.accepted, res.denied_reason
    _drain(conn)
    return res


def _reconfirm_grain(conn, source, table, columns, *, actor):
    """Drive a VERIFIED grain to a NEW VERIFIED value via the expiry/re-verify OVERRIDE path:
    fire the armed ``overlay_expiry`` timer far in the future (VERIFIED -> REVERIFY + a fresh gate
    task), then confirm with the override ``value`` — ``confirm_fact`` validates the FINAL value
    and would otherwise re-affirm ``state.prior_value`` (confirmation_commands.py). Drains the
    projection. NOTE: fires EVERY due overlay_expiry timer in the test database."""
    from datetime import UTC, datetime, timedelta

    from featuregen.overlay.commands import confirm_fact
    from featuregen.overlay.expiry import fire_due_overlay_expiries

    fired = fire_due_overlay_expiries(conn, now=datetime.now(UTC) + timedelta(days=4000))
    assert fired >= 1, f"no overlay_expiry timer fired for {source}.{table} — confirm it first"
    _drain(conn)  # surface EXPIRED to the read model before re-confirming
    _task_id, target_event_id, ref = _open_grain_task(conn, source, table, actor=actor)
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id,
         "value": {"columns": columns, "is_unique": True}},
        actor, f"reconfirm-{target_event_id}"))
    assert res.accepted, res.denied_reason
    _drain(conn)
    return res
