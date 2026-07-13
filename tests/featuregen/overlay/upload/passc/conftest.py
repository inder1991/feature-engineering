"""Shared fixtures/helpers for the Pass C DB suites (Phase 3A, Tasks 6-11).

Sits under ``tests/featuregen/overlay/upload/conftest.py`` (which autoregisters the overlay
commands/event types and clears the process-global upload adapter after every test). This conftest
adds what the Pass C tasks need: the upload-context connection (``passc_conn``), the service
proposer + TWO DISTINCT platform-admin confirmers, and propose/confirm/reject/expire helpers that
dispatch the REAL overlay gate commands.

DUAL confirmation (the critical shape — authority.py:118-124, join_confirmation.py): in the upload
flow ``UploadContextAdapter.owner_of -> None`` for BOTH join endpoints, so ``resolve_authority``
returns ``Authority(dual=True, governance_queue=True)`` — both-unknown is STILL dual — and
``propose_fact`` opens TWO side-labelled platform-admin gate tasks. The first platform-admin
confirm reaches only ``PARTIALLY_CONFIRMED``; a SECOND, DISTINCT platform-admin must confirm the
other side to reach ``VERIFIED`` (``_confirm_approved_join`` denies a repeat subject). A single
confirmer NEVER verifies a dual join, so ``_confirm_join`` drives ``admin1`` then ``admin2``.

Every state-changing helper ends by draining the projection: ``resolve_fact`` reads the
``overlay_fact_state`` read model, which ONLY ``run_projection(conn, OverlayProjection())``
populates (``confirm_fact`` does not).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from featuregen.contracts.envelopes import Command
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.commands import confirm_fact, propose_fact, reject_fact
from featuregen.overlay.expiry import fire_due_overlay_expiries
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter
from featuregen.projections.runner import run_projection

# ── Identities ────────────────────────────────────────────────────────────────────────────────────

# The service proposer (mirrors the real enrichment actor): four-eyes holds when a human later
# confirms what the service proposed (proposer != confirmer).
SERVICE_ACTOR = _ENRICH_ACTOR


@pytest.fixture
def passc_conn(db):
    """The ephemeral-PG connection (``db``: writes roll back on teardown) with the upload-context
    adapter registered, so propose/confirm/expiry resolve an adapter instead of RuntimeError. The
    parent conftest's autouse ``_clear_upload_adapter`` clears the process global afterwards."""
    ensure_upload_catalog_adapter()
    yield db


@pytest.fixture
def service_actor():
    """The NON-human service proposer — the real ``enrich_llm._ENRICH_ACTOR``."""
    return SERVICE_ACTOR


@pytest.fixture
def human_admin_1():
    """First platform-admin confirmer (governance queue: ``owner_of -> None`` on both sides)."""
    from tests.featuregen._helpers import mint_test_identity
    return mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))


@pytest.fixture
def human_admin_2():
    """Second, DISTINCT platform-admin — a dual join NEVER verifies with one confirmer."""
    from tests.featuregen._helpers import mint_test_identity
    return mint_test_identity(subject="user:admin2", role_claims=("platform-admin",))


# ── Helpers (module-level, mirroring the Phase-2 upload conftest style) ───────────────────────────

def _drain(conn) -> None:
    """Run the overlay projection until caught up (one pass caps at 500 events)."""
    while run_projection(conn, OverlayProjection()) >= 500:
        pass


def _join_value(ref) -> dict:
    """The approved_join proposed_value that matches ``ref`` (join_write_error demands they agree)."""
    from dataclasses import asdict
    return {
        "from_ref": asdict(ref.from_ref),
        "to_ref": asdict(ref.to_ref),
        "column_pairs": [{"from_col": p.from_col, "to_col": p.to_col} for p in ref.column_pairs],
        "cardinality": ref.cardinality,
    }


def _propose_join(conn, ref, evidence=None, *, actor=SERVICE_ACTOR):
    """Dispatch ``propose_fact`` for an approved_join (the ``_propose_governed_joins`` Command
    shape). ``evidence`` (JoinCandidateEvidenceV1) is accepted for the Task-10 call shape but is
    NOT threaded into the overlay command — its durable home is the ``pass_c_candidate_evidence``
    ledger (migration 0988), which Task 10 writes; the overlay `evidence` arg expects the profiler
    metric payload, not Pass-C candidate evidence."""
    del evidence
    value = _join_value(ref)
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "proposed_value": value},
        actor, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return res


def _open_join_tasks(conn, key) -> dict[str, str]:
    """The OPEN side-labelled governance gate tasks for the join, as ``{side: task_id}``. A dual
    both-unknown join must have exactly TWO, one per side, both routed to platform-admin."""
    rows = conn.execute(
        "SELECT task_id, eligible_assignees FROM human_tasks WHERE fact_key=%s AND status='open'",
        (key,)).fetchall()
    tasks: dict[str, str] = {}
    for task_id, eligible in rows:
        assert eligible.get("role") == "platform-admin", eligible
        tasks[eligible["side"]] = task_id
    return tasks


def _confirm_join(conn, ref, *, admin1, admin2):
    """Drive the dual (two-distinct-platform-admin) confirmation to VERIFIED, then drain.

    ``owner_of -> None`` for both endpoints makes the join dual + governance-queue: TWO
    side-labelled platform-admin tasks are open. ``admin1`` confirms one side (CAS target = the
    fact's current head) -> PARTIALLY_CONFIRMED; the state is RE-READ; ``admin2`` (a DISTINCT
    subject — join_confirmation denies a repeat) confirms the other side -> VERIFIED."""
    key = fact_key(ref, "approved_join")
    tasks = _open_join_tasks(conn, key)
    assert set(tasks) == {"from", "to"}, f"expected two side-labelled gate tasks, got {tasks}"

    state = fold_overlay_state(load_fact(conn, key))
    target = _cas_target(state)
    first = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "target_event_id": target},
        admin1, f"confirm1-{target}"))
    assert first.accepted, first.denied_reason
    state = fold_overlay_state(load_fact(conn, key))          # re-read between the two confirms
    assert state.status == "PARTIALLY_CONFIRMED", state.status

    target = _cas_target(state)   # PARTIALLY_CONFIRMED -> confirmed_event_id or draft_event_id
    second = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "target_event_id": target},
        admin2, f"confirm2-{target}"))
    assert second.accepted, second.denied_reason
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    _drain(conn)
    return second


def _reject_join(conn, ref, *, admin):
    """Reject the pending join -> REJECTED (sticky fingerprint); only valid PRE-VERIFIED
    (``reject_fact`` requires an awaiting-confirmation status). Drains the projection."""
    key = fact_key(ref, "approved_join")
    target = _cas_target(fold_overlay_state(load_fact(conn, key)))
    res = reject_fact(conn, Command(
        "reject_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "target_event_id": target,
         "reason": "not a real join"},
        admin, f"reject-{target}"))
    assert res.accepted, res.denied_reason
    _drain(conn)
    return res


def _expire_join(conn, ref):
    """Fire the armed ``overlay_expiry`` timer far in the future (VERIFIED -> REVERIFY — the
    VERIFIED-demotion path; note EXPIRED folds to REVERIFY, there is no 'EXPIRED' folded status).
    Drains the projection. NOTE: fires EVERY due overlay_expiry timer in the test database."""
    fired = fire_due_overlay_expiries(conn, now=datetime.now(UTC) + timedelta(days=4000))
    assert fired >= 1, "no overlay_expiry timer fired — confirm the join first"
    key = fact_key(ref, "approved_join")
    assert fold_overlay_state(load_fact(conn, key)).status == "REVERIFY"
    _drain(conn)
    return fired
