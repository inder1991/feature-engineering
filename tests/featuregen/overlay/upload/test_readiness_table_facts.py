"""Task 10: readiness reads the table's grain/availability overlay fact state.

``_table_fact_status`` maps the table's fact stream (the SAME ``fact_key`` Pass B proposes under)
to the readiness ``(status, cause)`` pair so the diagnostic flips missing -> proposed -> confirmed
as a grain proposal moves through the governed lifecycle. Only VERIFIED is feature-ready; every
other state is non-ready but the CAUSE distinguishes why (a REJECTED grain must never read
indistinguishable from never-proposed).

Flow per the Task 7 helpers: ``_propose_table_facts`` (service actor) opens the gate task,
``_confirm_grain`` / ``_reject_grain`` (platform-admin human) resolve it and drain the projection.
"""
from featuregen.overlay.upload.readiness import (
    CAUSE_FACT_EXPIRED,
    CAUSE_FACT_REJECTED,
    CAUSE_FACT_STALE,
    CAUSE_NOT_PROMOTED,
    _table_fact_status,
)
from featuregen.overlay.upload.table_synth import _propose_table_facts
from tests.featuregen.overlay.upload.conftest import _confirm_grain, _reject_grain


def _propose_grain(conn, columns, *, actor):
    _propose_table_facts(conn, "src",
                         {"txn": {"grain": {"columns": columns, "is_unique": True},
                                  "availability_time": None,
                                  "table_role": None, "primary_entity": None}},
                         actor=actor, source_snapshot_id="snap-test")


def test_absent_grain_is_missing(overlay_conn):
    status, cause = _table_fact_status(overlay_conn, "src", "txn", "grain")
    assert status == "missing" and cause == CAUSE_NOT_PROMOTED


def test_proposed_grain_is_proposed(overlay_conn, service_actor):
    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    status, cause = _table_fact_status(overlay_conn, "src", "txn", "grain")
    assert status == "proposed" and cause == "proposed_unconfirmed"


def test_confirmed_grain_is_confirmed(overlay_conn, service_actor, human_actor):
    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)  # helper -> VERIFIED
    assert _table_fact_status(overlay_conn, "src", "txn", "grain")[0] == "confirmed"


def test_rejected_grain_is_missing_but_distinct_cause(overlay_conn, service_actor, human_actor):
    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    _reject_grain(overlay_conn, "src", "txn", actor=human_actor)   # helper -> REJECTED
    status, cause = _table_fact_status(overlay_conn, "src", "txn", "grain")
    assert status == "missing" and cause == CAUSE_FACT_REJECTED   # not "never proposed"


def test_expired_confirmation_is_proposed_with_expired_cause(
        overlay_conn, service_actor, human_actor):
    """A LAPSED confirmation — the armed overlay_expiry timer fires, folding VERIFIED -> REVERIFY
    (state.py folds OVERLAY_FACT_EXPIRED to the literal "REVERIFY", never "EXPIRED") — must carry
    the EXPIRED cause, distinct from a drift-STALE. NOTE: fires EVERY due overlay_expiry timer in
    the test database (same caveat as conftest._reconfirm_grain)."""
    from datetime import UTC, datetime, timedelta

    from featuregen.overlay.expiry import fire_due_overlay_expiries

    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)  # VERIFIED + armed timer
    fired = fire_due_overlay_expiries(overlay_conn, now=datetime.now(UTC) + timedelta(days=4000))
    assert fired >= 1, "confirm_fact should have armed an overlay_expiry timer"
    status, cause = _table_fact_status(overlay_conn, "src", "txn", "grain")
    assert status == "proposed" and cause == CAUSE_FACT_EXPIRED


def test_drift_staled_grain_is_proposed_with_stale_cause(overlay_conn, service_actor, human_actor):
    """A drift-STALED confirmation (the catalog_changes scan appends OVERLAY_FACT_STALED, folding
    VERIFIED -> STALE) must carry the STALE cause, distinct from a lapsed-expiry REVERIFY."""
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import append_overlay_event, load_fact
    from featuregen.overlay.upload.upload_catalog import table_ref

    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)  # -> VERIFIED
    key = fact_key(table_ref("src", "txn"), "grain")
    stream = load_fact(overlay_conn, key)
    state = fold_overlay_state(stream)
    append_overlay_event(  # the exact event catalog_changes._stale_one appends on drift
        overlay_conn, fact_key=key, type="OVERLAY_FACT_STALED",
        payload={"catalog_change_ref": "chg-test",
                 "stales_confirmed_event_id": state.confirmed_event_id},
        actor=human_actor, expected_version=stream[-1].stream_version)
    status, cause = _table_fact_status(overlay_conn, "src", "txn", "grain")
    assert status == "proposed" and cause == CAUSE_FACT_STALE
