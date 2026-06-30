from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from psycopg.rows import dict_row

from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.catalog import CatalogObject, FixtureCatalog, register_catalog_adapter
from featuregen.overlay.facts import (
    OVERLAY_FACT_CONFIRMED,
    OVERLAY_FACT_EXPIRED,
    OVERLAY_FACT_PROPOSED,
)
from featuregen.overlay.freshness import (
    fire_due_overlay_expiries,
    open_reverify_task,
    schedule_expiry,
)
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    display_object_ref,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.store import load_fact
from featuregen.projections.runner import run_projection

SERVICE_ACTOR = build_service_identity(
    subject="service:overlay-freshness", role_claims=("overlay",), attestation="att-test"
)


def _table_ref(table="customers"):
    return CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="core", table=table, column=None
    )


def _col_ref(column, table="customers"):
    return CatalogObjectRef(
        catalog_source="pg:core", object_kind="column", schema="core", table=table, column=column
    )


def _seed_verified(conn, *, ref, fact_type, value, owner, use_case=None):
    """Append PROPOSED + CONFIRMED for one fact and run the projection so
    overlay_fact_state + overlay_fact_dependency are populated. Returns
    (fact_key, confirmed_event_id) where confirmed_event_id is the CONFIRMED event id."""
    from featuregen.overlay.store import append_overlay_event

    key = fact_key(ref, fact_type, use_case)
    proposer = build_human_identity(subject="user:proposer", role_claims=("data_owner",))
    proposed = append_overlay_event(
        conn,
        fact_key=key,
        type=OVERLAY_FACT_PROPOSED,
        actor=proposer,
        expected_version=0,
        payload={
            "catalog_object_ref": asdict(ref),
            "object_ref": display_object_ref(ref),
            "fact_type": fact_type,
            "use_case": use_case,
            "proposed_value": value,
            "proposal_fingerprint": proposal_fingerprint(value),
            "proposed_by": proposer.subject,
        },
    )
    confirmer = build_human_identity(subject=owner, role_claims=("data_owner",))
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type=OVERLAY_FACT_CONFIRMED,
        actor=confirmer,
        payload={
            "value": value,
            "confirmers": [{"subject": owner, "role": "data_owner"}],
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "confirms_event_id": proposed.event_id,
        },
    )
    run_projection(conn, OverlayProjection())
    return key, confirmed.event_id


def test_open_reverify_task_opens_data_owner_gate_targeting_confirmed_event(db):
    ref = _table_ref()
    key, confirmed_id = _seed_verified(
        db, ref=ref, fact_type="grain", value={"columns": ["customer_id"]}, owner="user:owner-a"
    )
    # FixtureCatalog API (pin 15): catalog_source ctor + add_object(...) / set_owner(ref, ...)
    adapter = FixtureCatalog(catalog_source="pg:core")
    adapter.add_object(
        CatalogObject(
            object_ref=display_object_ref(ref),
            object_kind="table",
            schema="core",
            table="customers",
            column=None,
            data_type=None,
            native_oid="oid-cust",
        )
    )
    adapter.set_owner(ref, "user:owner-a")
    authority = resolve_authority(db, adapter, ref, "grain")

    # single-authority fact -> exactly ONE re-verify task (task_assignees is a 1-tuple; pin 19)
    task_ids = open_reverify_task(
        db,
        fact_key=key,
        fact_type="grain",
        target_confirmed_event_id=confirmed_id,
        authority=authority,
        actor=SERVICE_ACTOR,
    )

    assert len(task_ids) == 1
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT gate, fact_key, target_event_id, status FROM human_tasks WHERE task_id = %s",
            (task_ids[0],),
        )
        row = cur.fetchone()
    assert row["gate"] == "OVERLAY_DATA_OWNER"
    assert row["fact_key"] == key
    assert row["target_event_id"] == confirmed_id
    assert row["status"] == "open"


def test_open_reverify_task_for_approved_join_opens_one_task_per_side(db):
    # pin 19: an approved_join with two DISTINCT owners reopens one re-verify task PER side,
    # using the SAME per-side authority.task_assignees the initial proposal (Task 4.2) used.
    acct = _table_ref("accounts")
    cust = _table_ref("customers")
    join_ref = ApprovedJoinRef(
        from_ref=acct,
        to_ref=cust,
        column_pairs=(ColumnPair(from_col="cust_id", to_col="customer_id"),),
        cardinality="N:1",
    )
    key, confirmed_id = _seed_verified(
        db,
        ref=join_ref,
        fact_type="approved_join",
        value={
            "from_ref": asdict(acct),
            "to_ref": asdict(cust),
            "column_pairs": [{"from_col": "cust_id", "to_col": "customer_id"}],
            "cardinality": "N:1",
        },
        owner="user:owner-a",
    )
    adapter = FixtureCatalog(catalog_source="pg:core")
    adapter.add_object(
        CatalogObject(
            object_ref=display_object_ref(acct),
            object_kind="table",
            schema="core",
            table="accounts",
            column=None,
            data_type=None,
            native_oid="oid-acct",
        )
    )
    adapter.add_object(
        CatalogObject(
            object_ref=display_object_ref(cust),
            object_kind="table",
            schema="core",
            table="customers",
            column=None,
            data_type=None,
            native_oid="oid-cust",
        )
    )
    adapter.set_owner(acct, "user:owner-a")
    adapter.set_owner(cust, "user:owner-b")  # two distinct owners -> two sides
    authority = resolve_authority(db, adapter, join_ref, "approved_join")

    task_ids = open_reverify_task(
        db,
        fact_key=key,
        fact_type="approved_join",
        target_confirmed_event_id=confirmed_id,
        authority=authority,
        actor=SERVICE_ACTOR,
    )

    assert len(task_ids) == 2
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT eligible_assignees, gate, target_event_id, status "
            "FROM human_tasks WHERE fact_key = %s AND status = 'open'",
            (key,),
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert all(r["gate"] == "OVERLAY_DATA_OWNER" for r in rows)
    assert all(r["target_event_id"] == confirmed_id for r in rows)
    # one task per side, matching the initial proposal's per-side assignees
    assert {r["eligible_assignees"].get("subject") for r in rows} == {
        "user:owner-a",
        "user:owner-b",
    }


def _grain_adapter(ref):
    # FixtureCatalog API (pin 15): catalog_source ctor + add_object(...) / set_owner(ref, ...)
    adapter = FixtureCatalog(catalog_source="pg:core")
    adapter.add_object(
        CatalogObject(
            object_ref=display_object_ref(ref),
            object_kind="table",
            schema="core",
            table="customers",
            column=None,
            data_type=None,
            native_oid="oid-cust",
        )
    )
    adapter.set_owner(ref, "user:owner-a")
    return adapter


def test_fire_due_overlay_expiries_emits_expired_and_opens_reverify_task(db):
    ref = _table_ref()
    key, confirmed_id = _seed_verified(
        db, ref=ref, fact_type="grain", value={"columns": ["customer_id"]}, owner="user:owner-a"
    )
    # the poller resolves the adapter via the single-source accessor (decision 5)
    register_catalog_adapter(_grain_adapter(ref))
    # confirm_fact (Phase 4.3) would have armed this timer; arm it directly, due in the past
    schedule_expiry(db, key, confirmed_id, datetime.now(UTC) - timedelta(seconds=1))

    fired = fire_due_overlay_expiries(db, now=datetime.now(UTC))

    assert fired == 1
    # OVERLAY_FACT_EXPIRED appended to the fact stream, targeting the confirmed event
    stream = load_fact(db, key)
    assert stream[-1].type == OVERLAY_FACT_EXPIRED
    assert stream[-1].payload["expires_confirmed_event_id"] == confirmed_id
    # the timer is consumed (marked fired), no longer scheduled
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status FROM timers WHERE aggregate_id = %s AND kind = 'overlay_expiry'",
            (key,),
        )
        assert cur.fetchone()["status"] == "fired"
    # the projection folds the fact to REVERIFY with prior_value retained, value cleared
    run_projection(db, OverlayProjection())
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status, value, prior_value FROM overlay_fact_state WHERE fact_key = %s",
            (key,),
        )
        state = cur.fetchone()
    assert state["status"] == "REVERIFY"
    assert state["value"] is None
    assert state["prior_value"] == {"columns": ["customer_id"]}
    # a re-verify task is open for this fact
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM human_tasks WHERE fact_key = %s AND status = 'open'",
            (key,),
        )
        assert cur.fetchone()["n"] == 1
    # a second poll is idempotent: the timer is already fired, nothing new emitted
    assert fire_due_overlay_expiries(db, now=datetime.now(UTC)) == 0


def test_stale_expiry_timer_is_noop_when_newer_confirm_supersedes_target(db):
    from featuregen.overlay.store import append_overlay_event

    ref = _table_ref()
    key, first_confirmed_id = _seed_verified(
        db, ref=ref, fact_type="grain", value={"columns": ["customer_id"]}, owner="user:owner-a"
    )
    register_catalog_adapter(_grain_adapter(ref))
    # the OLD timer was armed against the original confirmation, due in the past
    schedule_expiry(db, key, first_confirmed_id, datetime.now(UTC) - timedelta(seconds=1))
    # a newer FACT_CONFIRMED supersedes the original confirmation (e.g. a re-confirm)
    confirmer = build_human_identity(subject="user:owner-a", role_claims=("data_owner",))
    newer = append_overlay_event(
        db,
        fact_key=key,
        type=OVERLAY_FACT_CONFIRMED,
        actor=confirmer,
        payload={
            "value": {"columns": ["customer_id"]},
            "confirmers": [{"subject": "user:owner-a", "role": "data_owner"}],
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "confirms_event_id": first_confirmed_id,
        },
    )
    assert newer.event_id != first_confirmed_id
    types_before = [e.type for e in load_fact(db, key)]

    # the OLD timer fires via the poller, targeting the now-superseded confirmation
    fired = fire_due_overlay_expiries(db, now=datetime.now(UTC))

    assert fired == 0
    # NO OVERLAY_FACT_EXPIRED appended, NO task opened
    assert [e.type for e in load_fact(db, key)] == types_before
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM human_tasks WHERE fact_key = %s", (key,))
        assert cur.fetchone()["n"] == 0
    # the superseded timer is still consumed (marked fired), not left scheduled
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status FROM timers WHERE aggregate_id = %s AND kind = 'overlay_expiry'",
            (key,),
        )
        assert cur.fetchone()["status"] == "fired"
