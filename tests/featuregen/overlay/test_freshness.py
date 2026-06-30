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
    OVERLAY_FACT_STALED,
)
from featuregen.overlay.freshness import (
    detect_catalog_changes,
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


def _obj(ref, *, data_type=None, oid):
    return CatalogObject(
        object_ref=display_object_ref(ref),
        object_kind=ref.object_kind,
        schema=ref.schema,
        table=ref.table,
        column=ref.column,
        data_type=data_type,
        native_oid=oid,
    )


def _adapter(objs, owners=None):
    # FixtureCatalog API (pin 15): catalog_source ctor + add_object(...) / set_owner(ref, ...).
    # `owners` maps a CatalogObjectRef -> owner subject (set_owner takes the typed ref).
    adapter = FixtureCatalog(catalog_source="pg:core")
    for obj in objs:
        adapter.add_object(obj)
    for ref, owner in (owners or {}).items():
        adapter.set_owner(ref, owner)
    return adapter


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


def test_detect_catalog_changes_classifies_add_drop_typechange_and_rename(db):
    # finding 16 / pin 16: a column's native_oid is the stable "<table_oid>:<attnum>" composite
    # (a table's native_oid is its bare oid). pg_attribute.attnum is fixed at creation and not
    # reused on rename, so a renamed column keeps the SAME native_oid — that is how the rename is
    # detected (same id, different name) rather than degraded to drop+add.
    tbl = _table_ref()
    before = [
        _obj(tbl, oid="oid-cust"),
        _obj(_col_ref("region"), data_type="text", oid="oid-cust:2"),
        _obj(_col_ref("tier"), data_type="text", oid="oid-cust:3"),
    ]
    # baseline snapshot — first run reports only adds, stales nothing
    first = detect_catalog_changes(db, _adapter(before), actor=SERVICE_ACTOR)
    assert {c.kind for c in first} == {"add"}

    after = [
        _obj(tbl, oid="oid-cust"),
        _obj(_col_ref("region"), data_type="varchar", oid="oid-cust:2"),  # type change (same attnum)
        _obj(_col_ref("segment"), data_type="text", oid="oid-cust:3"),  # rename of tier (same attnum)
        _obj(_col_ref("score"), data_type="int", oid="oid-cust:5"),  # genuine add (new attnum)
    ]
    changes = {
        (c.kind, c.object_ref): c
        for c in detect_catalog_changes(db, _adapter(after), actor=SERVICE_ACTOR)
    }

    assert ("type_change", display_object_ref(_col_ref("region"))) in changes
    assert ("add", display_object_ref(_col_ref("score"))) in changes
    rename = changes[("rename", display_object_ref(_col_ref("tier")))]
    assert rename.renamed_to == display_object_ref(_col_ref("segment"))
    # the renamed-to object is onboarded afresh, not reported as a bare "add"
    assert ("add", display_object_ref(_col_ref("segment"))) not in changes


def test_drop_referenced_column_stales_grain_availability_and_join_source_side(db):
    cust = _table_ref("customers")
    acct = _table_ref("accounts")
    # the soon-to-be-dropped column lives on accounts — the join's SOURCE/from side
    src_col = _col_ref("cust_id", "accounts")
    # grain + availability_time on accounts both reference core.accounts.cust_id
    grain_key, _ = _seed_verified(
        db, ref=acct, fact_type="grain", value={"columns": ["cust_id"]}, owner="user:owner-a",
    )
    avail_key, _ = _seed_verified(
        db, ref=acct, fact_type="availability_time",
        value={"column": "cust_id"}, owner="user:owner-a",
    )
    # an approved_join whose from_col is core.accounts.cust_id — indexed via column_pairs
    # (not from_columns/to_columns, not the display relation string)
    join_ref = ApprovedJoinRef(
        from_ref=acct, to_ref=cust,
        column_pairs=(ColumnPair(from_col="cust_id", to_col="customer_id"),),
        cardinality="N:1",
    )
    join_key, _ = _seed_verified(
        db, ref=join_ref, fact_type="approved_join",
        value={"from_ref": asdict(acct), "to_ref": asdict(cust),
               "column_pairs": [{"from_col": "cust_id", "to_col": "customer_id"}],
               "cardinality": "N:1"}, owner="user:owner-a",
    )

    owners = {cust: "user:owner-a", acct: "user:owner-a"}
    before = [_obj(cust, oid="oid-cust"), _obj(acct, oid="oid-acct"),
              _obj(src_col, data_type="text", oid="oid-acct:4")]
    detect_catalog_changes(db, _adapter(before, owners), actor=SERVICE_ACTOR)  # baseline

    after = [_obj(cust, oid="oid-cust"), _obj(acct, oid="oid-acct")]  # accounts.cust_id dropped
    changes = detect_catalog_changes(db, _adapter(after, owners), actor=SERVICE_ACTOR)

    assert ("drop", display_object_ref(src_col)) in {(c.kind, c.object_ref) for c in changes}
    for key in (grain_key, avail_key, join_key):
        assert load_fact(db, key)[-1].type == OVERLAY_FACT_STALED
    run_projection(db, OverlayProjection())
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM overlay_fact_state "
            "WHERE fact_key = ANY(%s) AND status = 'STALE'",
            ([grain_key, avail_key, join_key],),
        )
        assert cur.fetchone()["n"] == 3
        cur.execute(
            "SELECT count(*) AS n FROM human_tasks "
            "WHERE fact_key = ANY(%s) AND status = 'open'",
            ([grain_key, avail_key, join_key],),
        )
        assert cur.fetchone()["n"] == 3


def test_rename_yields_new_key_and_stales_old_fact(db):
    cust = _table_ref("customers")
    old_col = _col_ref("region", "customers")
    new_col = _col_ref("region_code", "customers")
    grain_key, _ = _seed_verified(
        db, ref=cust, fact_type="grain", value={"columns": ["region"]}, owner="user:owner-a",
    )
    owners = {cust: "user:owner-a"}
    # finding 16 / pin 16: region keeps the SAME stable "<table_oid>:<attnum>" id across the
    # rename (attnum is not reused), so the diff sees one id whose name changed -> a rename.
    before = [_obj(cust, oid="oid-cust"), _obj(old_col, data_type="text", oid="oid-cust:2")]
    detect_catalog_changes(db, _adapter(before, owners), actor=SERVICE_ACTOR)  # baseline

    after = [_obj(cust, oid="oid-cust"), _obj(new_col, data_type="text", oid="oid-cust:2")]
    changes = {
        (c.kind, c.object_ref): c
        for c in detect_catalog_changes(db, _adapter(after, owners), actor=SERVICE_ACTOR)
    }

    rename = changes[("rename", display_object_ref(old_col))]
    assert rename.renamed_to == display_object_ref(new_col)  # new fact_key territory
    # the old fact (keyed on the old name) is STALEd; no identity carries across the rename
    assert load_fact(db, grain_key)[-1].type == OVERLAY_FACT_STALED
    run_projection(db, OverlayProjection())
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM overlay_fact_state WHERE fact_key = %s", (grain_key,))
        assert cur.fetchone()["status"] == "STALE"


def test_stale_signal_is_noop_when_fact_already_advanced(db):
    cust = _table_ref("customers")
    region = _col_ref("region", "customers")
    grain_key, confirmed_id = _seed_verified(
        db, ref=cust, fact_type="grain", value={"columns": ["region"]}, owner="user:owner-a",
    )
    owners = {cust: "user:owner-a"}
    before = [_obj(cust, oid="oid-cust"), _obj(region, data_type="text", oid="oid-cust:2")]
    after = [_obj(cust, oid="oid-cust")]
    adapter_before, adapter_after = _adapter(before, owners), _adapter(after, owners)
    detect_catalog_changes(db, adapter_before, actor=SERVICE_ACTOR)  # baseline

    # the fact is already STALE (a prior change-signal advanced it past VERIFIED)
    from featuregen.overlay.store import append_overlay_event
    append_overlay_event(
        db, fact_key=grain_key, type=OVERLAY_FACT_STALED, actor=SERVICE_ACTOR,
        payload={"catalog_change_ref": "drop:earlier", "stales_confirmed_event_id": confirmed_id},
    )
    types_before = [e.type for e in load_fact(db, grain_key)]

    detect_catalog_changes(db, adapter_after, actor=SERVICE_ACTOR)

    # no second OVERLAY_FACT_STALED appended (CAS no-op: not VERIFIED)
    assert [e.type for e in load_fact(db, grain_key)] == types_before


def test_dependency_index_tracks_confirmed_override_not_proposed_column(db):
    """confirm_fact lets a human override the proposed value (pin 17); for grain/availability/scd
    that override can change the REFERENCED COLUMNS. The general dependency index must follow the
    CONFIRMED value, not the original proposal — else a drop/type-change of the actually-confirmed
    column never STALEs the fact (false negative) and a change to the discarded proposed column
    wrongly STALEs it (false positive)."""
    from dataclasses import asdict

    from featuregen.overlay.projection import dependents_of
    from featuregen.overlay.store import append_overlay_event

    tbl = _table_ref()            # core.customers
    proposed_col = "region"       # proposed grain column (later discarded by the override)
    confirmed_col = "tier"        # the column the human actually confirmed

    key = fact_key(tbl, "grain", None)
    proposer = build_human_identity(subject="user:proposer", role_claims=("data_owner",))
    proposed = append_overlay_event(
        db, fact_key=key, type=OVERLAY_FACT_PROPOSED, actor=proposer, expected_version=0,
        payload={
            "catalog_object_ref": asdict(tbl),
            "object_ref": display_object_ref(tbl),
            "fact_type": "grain",
            "use_case": None,
            "proposed_value": {"columns": [proposed_col], "is_unique": True},
            "proposal_fingerprint": proposal_fingerprint({"columns": [proposed_col]}),
            "proposed_by": proposer.subject,
        },
    )
    # CONFIRMED with an OVERRIDE that changes the column set (region -> tier)
    append_overlay_event(
        db, fact_key=key, type=OVERLAY_FACT_CONFIRMED,
        actor=build_human_identity(subject="user:owner-a", role_claims=("data_owner",)),
        payload={
            "value": {"columns": [confirmed_col], "is_unique": True},
            "confirmers": [{"subject": "user:owner-a", "role": "data_owner"}],
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "confirms_event_id": proposed.event_id,
        },
    )
    run_projection(db, OverlayProjection())

    # the dependency index must point at the CONFIRMED column, not the proposed one
    assert key in dependents_of(db, display_object_ref(_col_ref(confirmed_col)))
    assert key not in dependents_of(db, display_object_ref(_col_ref(proposed_col)))

    # and a drop of the confirmed column must STALE the fact (pre-fix it does NOT)
    owners = {tbl: "user:owner-a"}
    before = [_obj(tbl, oid="oid-cust"),
              _obj(_col_ref(confirmed_col), data_type="text", oid="oid-cust:3")]
    detect_catalog_changes(db, _adapter(before, owners), actor=SERVICE_ACTOR)  # baseline
    after = [_obj(tbl, oid="oid-cust")]  # tier dropped
    detect_catalog_changes(db, _adapter(after, owners), actor=SERVICE_ACTOR)
    assert load_fact(db, key)[-1].type == OVERLAY_FACT_STALED
