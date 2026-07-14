"""Audit fix I-2 — the governed-join seam must be fail-soft against DB-class faults.

`_propose_governed_joins` was the ONLY advisory pass `ingest_upload` called with NO savepoint:
a DB-class fault inside `propose_fact` was swallowed as a Python exception by the seam's own
per-row `except`, but left the REQUEST transaction ABORTED — the next unguarded statement
(`projection_lag` / `persist_quarantine`) then raised `InFailedSqlTransaction`, 500'd the upload
and rolled back the already-asserted Pass A facts. The seam now mirrors the Pass B / Pass C
shape: an outer savepoint + except at the call site, and a per-proposal savepoint inside the
loop so a swallowed DB fault is ROLLED BACK TO before the loop continues.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.upload_catalog import table_ref

_NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _join_rows() -> list[CanonicalRow]:
    return [
        CanonicalRow("deposits", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("deposits", "accounts", "account_id", "integer", is_grain=True),
    ]


def _assert_pass_a_intact(db) -> None:
    """The upload's own facts + graph must hold: the source-attested grain fact is VERIFIED and
    the graph nodes for this source exist (nothing was rolled back by the advisory failure)."""
    grain = load_fact(db, fact_key(table_ref("deposits", "accounts"), "grain"))
    assert fold_overlay_state(grain).status == "VERIFIED"
    nodes = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'deposits'").fetchone()[0]
    assert nodes > 0
    assert db.execute("SELECT 1").fetchone()[0] == 1   # the request tx is healthy, not aborted


def test_db_fault_inside_propose_fact_degrades_to_warning(db, monkeypatch):
    """A GENUINE DB-class fault inside `propose_fact` (statement fails -> tx aborted at the PG
    level, Python exception swallowed by the seam's per-row except): the upload must still
    complete and the Pass A facts + graph must survive. Pre-fix, the aborted tx made the next
    statement raise InFailedSqlTransaction and the whole upload errored."""
    monkeypatch.setenv("OVERLAY_GOVERNED_JOINS", "1")
    _seal_config()

    def _db_fault(conn, cmd):
        conn.execute("SELECT 1/0")   # DB-class fault: aborts the transaction it runs in

    # _propose_governed_joins imports propose_fact lazily from overlay.commands at call time.
    monkeypatch.setattr("featuregen.overlay.commands.propose_fact", _db_fault)

    res = ingest_upload(db, "deposits", _join_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"                    # degraded to a warning, never a 500
    _assert_pass_a_intact(db)


def test_error_in_governed_join_seam_never_fails_upload(db, monkeypatch):
    """The call site itself is savepointed + guarded (mirroring the Pass C block): even a fault
    raised straight out of `_propose_governed_joins` degrades to a warning."""
    import featuregen.overlay.upload.ingest as ingest_mod

    monkeypatch.setenv("OVERLAY_GOVERNED_JOINS", "1")
    _seal_config()

    def _boom(conn, rows, *, actor):
        raise RuntimeError("governed-join seam exploded")

    monkeypatch.setattr(ingest_mod, "_propose_governed_joins", _boom)

    res = ingest_upload(db, "deposits", _join_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    _assert_pass_a_intact(db)
