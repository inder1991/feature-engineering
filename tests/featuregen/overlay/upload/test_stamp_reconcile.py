"""Task 5: table-fact stamps survive re-ingest; drift is reconciled and visible.

THE ROOT CAUSE the failing tests pin (diagnosed, not assumed): `project_table_facts_for_ref`
stamped ONLY `kind='column'` rows — the TABLE node's `grain_fact_event_id` /
`availability_fact_event_id` (added by migration 0986 for exactly this purpose, and what the
audit script's `stamp_drift` metric reads) were NEVER written by any code path, so every
VERIFIED grain/availability fact reported stamp drift forever, healthy ingest or not. The
second, real dropped-stamp path: an ingest whose `table_fact_projection` stage records
``lagged`` skips the projection AFTER `build_graph` already wiped every node — and nothing
ever backfills, so the stamps stay NULL until a NEXT caught-up ingest of the same source
(which may never come). `stamp_reconcile` makes that drift visible on every run;
`repair_table_fact_stamps` closes it by re-running the EXISTING projection (event ids are
re-read from `resolve_fact` — never fabricated).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.stage_report import StageRecorder
from featuregen.overlay.upload.stamp_reconcile import (
    StampDrift,
    reconcile_table_fact_stamps,
    repair_table_fact_stamps,
)
from featuregen.overlay.upload.upload_catalog import table_ref

_NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _rows(source: str) -> list[CanonicalRow]:
    """A source-attested grain + as-of: `_assert_fact` auto-confirms both VERIFIED at ingest —
    the same shape as the live cib/ftr uploads (uploader-declared facts)."""
    return [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
    ]


def _table_stamps(db, source: str, table: str) -> tuple[str | None, str | None]:
    return db.execute(
        "SELECT grain_fact_event_id, availability_fact_event_id FROM graph_node "
        "WHERE catalog_source=%s AND table_name=%s AND kind='table'",
        (source, table)).fetchone()


def _column_stamp(db, source: str, column: str, col: str) -> str | None:
    return db.execute(
        f"SELECT {col} FROM graph_node WHERE catalog_source=%s AND column_name=%s "
        "AND kind='column'", (source, column)).fetchone()[0]


def _confirmed_event_ids(db, source: str, table: str) -> dict[str, str]:
    """{fact_type: confirmed_event_id} for the table's VERIFIED overlay facts."""
    out = {}
    for fact_type in ("grain", "availability_time"):
        row = db.execute(
            "SELECT confirmed_event_id FROM overlay_fact_state WHERE fact_key=%s "
            "AND status='VERIFIED'", (fact_key(table_ref(source, table), fact_type),)).fetchone()
        if row is not None:
            out[fact_type] = row[0]
    return out


# ── Step 2: the failing test at the located seam ─────────────────────────────────────────────────


def test_first_ingest_stamps_the_table_node(db):
    """The fact is TABLE-scoped: its confirmed-event provenance must land on the table node (what
    the audit script and the table-level dossier read), not only on the member columns."""
    _seal_config()
    assert ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(),
                         now=_NOW).status == "ingested"
    ids = _confirmed_event_ids(db, "deposits", "accounts")
    assert set(ids) == {"grain", "availability_time"}   # source-attested, auto-VERIFIED
    grain_stamp, avail_stamp = _table_stamps(db, "deposits", "accounts")
    assert grain_stamp == ids["grain"]
    assert avail_stamp == ids["availability_time"]


def test_reingest_restamps_verified_facts_on_the_table_node(db):
    """After re-ingest + drain, VERIFIED grain/availability facts are re-stamped on the table
    node — build_graph wipes every node, so the tail projection must restore BOTH the column
    stamps (already covered elsewhere) and the table-node stamps (never written before this fix)."""
    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(),
                        now=_NOW + timedelta(hours=1))
    assert res.status == "ingested"
    ids = _confirmed_event_ids(db, "deposits", "accounts")
    grain_stamp, avail_stamp = _table_stamps(db, "deposits", "accounts")
    assert grain_stamp == ids["grain"]
    assert avail_stamp == ids["availability_time"]
    # the column stamps still land too — the table-node stamp is additive, not a relocation
    assert _column_stamp(db, "deposits", "id", "grain_fact_event_id") == ids["grain"]
    assert _column_stamp(db, "deposits", "posted_at",
                         "availability_fact_event_id") == ids["availability_time"]


def test_projection_clears_a_stale_table_stamp(db):
    """Clear-then-set is rebuild-safe for the table node too: a stamp whose fact no longer
    resolves must not survive a re-projection as a stale provenance link."""
    from featuregen.overlay.upload.table_fact_projection import project_table_facts

    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    db.execute("UPDATE graph_node SET grain_fact_event_id='evt_stale' "
               "WHERE catalog_source='deposits' AND kind='table'")
    db.execute("DELETE FROM overlay_fact_state")   # no VERIFIED fact serves any more
    project_table_facts(db, source="deposits", tables=["accounts"], now=_NOW)
    grain_stamp, avail_stamp = _table_stamps(db, "deposits", "accounts")
    assert grain_stamp is None
    assert avail_stamp is None


# ── the live dropped-stamp path: lagged skip, then no backfill ───────────────────────────────────


def test_lagged_reingest_drops_stamps_and_repair_restores_them(db, monkeypatch):
    """The kind-cluster mechanism: a re-ingest under projection lag skips the whole
    table_fact_projection stage AFTER build_graph wiped the nodes — VERIFIED facts, NULL stamps,
    and no later backfill. `repair_table_fact_stamps` re-runs the existing projection once the
    read model is caught up; the restored ids are re-read from resolve_fact, never fabricated."""
    from featuregen.overlay.upload import ingest as ingest_mod

    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    lag = {"value": 1}
    monkeypatch.setattr(ingest_mod, "projection_lag", lambda conn, name: lag["value"])
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(),
                        now=_NOW + timedelta(hours=1), stage_recorder=rec)
    assert res.status == "ingested"
    assert next(r for r in rec.reports if r.stage == "table_fact_projection").state == "lagged"
    ids = _confirmed_event_ids(db, "deposits", "accounts")
    assert set(ids) == {"grain", "availability_time"}   # the facts themselves are intact
    assert _table_stamps(db, "deposits", "accounts") == (None, None)   # ...but unstamped
    assert _column_stamp(db, "deposits", "id", "grain_fact_event_id") is None
    # the reconcile stage on that very run already made the drift visible
    stage = next(r for r in rec.reports if r.stage == "stamp_reconcile")
    assert stage.detail == {"drift": 2}
    assert stage.reason_code == "stamp_drift"
    # the projection is genuinely caught up (the fake lag only skipped the stage) — repair
    drift = reconcile_table_fact_stamps(db, source="deposits")
    assert {(d.fact_type, d.stamped_event_id) for d in drift} == {
        ("grain", None), ("availability_time", None)}
    remaining = repair_table_fact_stamps(db, current_catalog_adapter(),
                                         now=_NOW + timedelta(hours=1))
    assert remaining == ()
    grain_stamp, avail_stamp = _table_stamps(db, "deposits", "accounts")
    assert grain_stamp == ids["grain"]                   # re-read, not minted
    assert avail_stamp == ids["availability_time"]
    assert _column_stamp(db, "deposits", "id", "grain_fact_event_id") == ids["grain"]
    assert reconcile_table_fact_stamps(db) == ()


# ── Step 4: the reconcile check ──────────────────────────────────────────────────────────────────


def test_reconcile_is_empty_when_stamps_agree(db):
    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    assert reconcile_table_fact_stamps(db) == ()
    assert reconcile_table_fact_stamps(db, source="deposits") == ()


def test_reconcile_reports_a_manually_nulled_stamp(db):
    """The Task 8 adversarial case: a stamp wiped out-of-band is reported as drift, carrying the
    overlay's confirmed event id against the (NULL) stamped id."""
    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    ids = _confirmed_event_ids(db, "deposits", "accounts")
    db.execute("UPDATE graph_node SET grain_fact_event_id=NULL "
               "WHERE catalog_source='deposits' AND kind='table'")
    drift = reconcile_table_fact_stamps(db)
    assert drift == (StampDrift(object_ref="public.accounts", fact_type="grain",
                                overlay_event_id=ids["grain"], stamped_event_id=None),)


def test_reconcile_reports_a_wrong_stamp_not_only_a_null_one(db):
    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    ids = _confirmed_event_ids(db, "deposits", "accounts")
    db.execute("UPDATE graph_node SET availability_fact_event_id='evt_bogus' "
               "WHERE catalog_source='deposits' AND kind='table'")
    drift = reconcile_table_fact_stamps(db)
    assert drift == (StampDrift(object_ref="public.accounts", fact_type="availability_time",
                                overlay_event_id=ids["availability_time"],
                                stamped_event_id="evt_bogus"),)


def test_reconcile_source_filter_scopes_the_check(db):
    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    db.execute("UPDATE graph_node SET grain_fact_event_id=NULL "
               "WHERE catalog_source='deposits' AND kind='table'")
    assert reconcile_table_fact_stamps(db, source="other") == ()
    assert len(reconcile_table_fact_stamps(db, source="deposits")) == 1


def test_repair_without_drift_writes_nothing(db):
    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    before = db.execute(
        "SELECT column_name, is_grain, is_as_of, grain_fact_event_id, availability_fact_event_id "
        "FROM graph_node WHERE catalog_source='deposits' ORDER BY object_ref").fetchall()
    assert repair_table_fact_stamps(db, current_catalog_adapter(), now=_NOW) == ()
    after = db.execute(
        "SELECT column_name, is_grain, is_as_of, grain_fact_event_id, availability_fact_event_id "
        "FROM graph_node WHERE catalog_source='deposits' ORDER BY object_ref").fetchall()
    assert after == before


def test_repair_never_wipes_declared_flags_when_the_fact_is_unservable(db):
    """The c715a16d hazard, re-proven for the repair path: a VERIFIED fact resolve_fact refuses
    to serve (drift-stale watermark) must NOT let the repair's clear-then-set wipe a
    file-declared is_grain — repair skips the fact and leaves the drift visible instead."""
    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    db.execute("UPDATE graph_node SET grain_fact_event_id=NULL, availability_fact_event_id=NULL "
               "WHERE catalog_source='deposits'")
    # resolve far past the drift-freshness SLA (24h): the watermark is stale, resolve fails closed
    stale_now = _NOW + timedelta(days=30)
    remaining = repair_table_fact_stamps(db, current_catalog_adapter(), now=stale_now)
    assert len(remaining) == 2                       # both facts still drifted — honestly reported
    flags = dict(db.execute(
        "SELECT column_name, is_grain FROM graph_node WHERE catalog_source='deposits' "
        "AND kind='column'").fetchall())
    assert flags["id"] is True                       # the file-declared flag survived


# ── Step 4 wiring: the ingest tail stage ─────────────────────────────────────────────────────────


def test_ingest_records_a_clean_stamp_reconcile_stage(db):
    _seal_config()
    rec = StageRecorder()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW,
                  stage_recorder=rec)
    stage = next(r for r in rec.reports if r.stage == "stamp_reconcile")
    assert stage.state == "succeeded"
    assert stage.reason_code is None
    assert stage.detail == {"drift": 0}
    assert stage.started_at is not None


def test_a_reconcile_fault_never_fails_the_upload(db, monkeypatch):
    from featuregen.overlay.upload import ingest as ingest_mod

    _seal_config()

    def boom(conn, *, source=None):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(ingest_mod, "reconcile_table_fact_stamps", boom)
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW,
                        stage_recorder=rec)
    assert res.status == "ingested"
    stage = next(r for r in rec.reports if r.stage == "stamp_reconcile")
    assert stage.state == "failed"
    assert stage.reason_code == "exception"


# ── Step 5: the basis-review task — review WITHOUT staling ───────────────────────────────────────


def test_basis_review_task_opens_without_staling_the_fact(db):
    """The CIB availability case: `{basis: ingested_at, column: business_dt}` is questionable but
    the fact must stay VERIFIED and servable while a human reviews it. The command opens ONE
    human_tasks row bound to the fact's CURRENT confirmed event, reason `basis_review` — and
    appends no overlay event, so the status cannot move."""
    from featuregen.overlay.resolve import resolve_fact
    from featuregen.overlay.reverify_tasks import open_fact_review_task

    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    key = fact_key(table_ref("deposits", "accounts"), "availability_time")
    ids = _confirmed_event_ids(db, "deposits", "accounts")
    task_id = open_fact_review_task(db, fact_key=key, reason="basis_review", actor=_actor())
    row = db.execute(
        "SELECT status, target_event_id, required_inputs FROM human_tasks WHERE task_id=%s",
        (task_id,)).fetchone()
    assert row is not None
    status, target_event_id, required_inputs = row
    assert status == "open"
    assert target_event_id == ids["availability_time"]   # bound to the CURRENT confirmed event
    assert "basis_review" in required_inputs
    # the fact did not move: still VERIFIED in the read model AND still servable
    assert db.execute("SELECT status FROM overlay_fact_state WHERE fact_key=%s",
                      (key,)).fetchone()[0] == "VERIFIED"
    resolved = resolve_fact(db, current_catalog_adapter(),
                            table_ref("deposits", "accounts"), "availability_time", now=_NOW)
    assert resolved is not None and resolved.value is not None


def test_basis_review_task_is_idempotent_one_open_row(db):
    from featuregen.overlay.reverify_tasks import open_fact_review_task

    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    key = fact_key(table_ref("deposits", "accounts"), "availability_time")
    first = open_fact_review_task(db, fact_key=key, reason="basis_review", actor=_actor())
    second = open_fact_review_task(db, fact_key=key, reason="basis_review", actor=_actor())
    assert first == second
    assert db.execute("SELECT count(*) FROM human_tasks WHERE fact_key=%s AND status='open'",
                      (key,)).fetchone()[0] == 1


def test_basis_review_requires_a_verified_fact(db):
    import pytest

    from featuregen.overlay._lifecycle import OverlayCommandError
    from featuregen.overlay.reverify_tasks import open_fact_review_task

    _seal_config()
    ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW)
    with pytest.raises(OverlayCommandError):
        open_fact_review_task(db, fact_key="no-such-fact", reason="basis_review",
                              actor=_actor())
