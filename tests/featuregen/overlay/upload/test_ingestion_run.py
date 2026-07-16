"""The ingestion_run lifecycle module (first-release hardening #3 CORE).

Durability model under test: ``open_run`` / ``terminalize_run_durable`` write on a FRESH
independent connection resolved from ``get_settings().dsn`` (the ``_record_llm_call_durable``
pattern) so the manifest survives the request transaction rolling back; with no DSN configured
(this suite's default — the rolled-back test harness) both fall back to the caller's connection.
``terminalize_run`` runs on the GIVEN connection so an 'ingested' terminal state commits
atomically with the ingest transaction it describes.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.upload.ingestion_run import (
    _effective_config_snapshot,
    get_run,
    open_run,
    reconcile_ingestion_runs,
    source_fingerprint,
    terminalize_run,
    terminalize_run_durable,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(seconds=30)

_ACTOR = mint_test_identity(subject="user:tester", role_claims=("platform-admin", "data_owner"))


@pytest.fixture
def no_dsn(monkeypatch):
    """Force the request-connection fallback path (the suite normally has no FEATUREGEN_DSN, but a
    developer shell might)."""
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)


@pytest.fixture
def durable_dsn(monkeypatch, _dsn):
    """Point FEATUREGEN_DSN at the test cluster so the fresh-connection path really commits — and
    clean the committed rows up afterwards (they outlive the test conn's rollback by design)."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    created: list[str] = []
    yield created
    with psycopg.connect(_dsn) as c:
        c.execute("DELETE FROM ingestion_run_status_event WHERE ingestion_run_id = ANY(%s)",
                  (created,))
        c.execute("DELETE FROM ingestion_run WHERE id = ANY(%s)", (created,))


def _open(conn, **overrides) -> str:
    kwargs = dict(origin_type="upload", catalog_source="deposits", filename="deposits.csv",
                  actor=_ACTOR, effective_config={"config_schema_version": 1, "pass_c": False},
                  now=_NOW)
    kwargs.update(overrides)
    return open_run(conn, **kwargs)


# ── open_run + get_run (fallback path) ────────────────────────────────────────────────────────────


def test_open_run_records_in_progress_run(db, no_dsn) -> None:
    run_id = _open(db)
    assert run_id.startswith("ingrun_")
    run = get_run(db, run_id)
    assert run["status"] == "in_progress"
    assert run["origin_type"] == "upload"
    assert run["catalog_source"] == "deposits"
    assert run["filename"] == "deposits.csv"
    assert run["actor_subject"] == "user:tester"
    assert run["actor_role_claims"] == ["platform-admin", "data_owner"]
    assert run["effective_config"] == {"config_schema_version": 1, "pass_c": False}
    assert run["started_at"] == _NOW
    assert run["heartbeat_at"] == _NOW
    assert run["completed_at"] is None
    assert run["status_history"] == [{"status": "in_progress", "at": _NOW, "reason_code": None}]


def test_open_run_sanitizes_filename(db, no_dsn) -> None:
    run_id = _open(db, filename="../../etc/" + "a" * 500 + ".csv")
    name = get_run(db, run_id)["filename"]
    assert "/" not in name and len(name) <= 200
    run_id = _open(db, filename=None)
    assert get_run(db, run_id)["filename"] is None


def test_get_run_missing_is_none(db, no_dsn) -> None:
    assert get_run(db, "ingrun_NOPE") is None


# ── terminalize_run (request-connection, atomic with the ingest tx) ───────────────────────────────


def test_terminalize_run_records_terminal_state(db, no_dsn) -> None:
    run_id = _open(db)
    assert terminalize_run(
        db, run_id, status="ingested", now=_LATER, row_count=9, quarantined_count=0,
        file_sha256="ab" * 32, pre_fingerprint="f" * 64, post_fingerprint="e" * 64,
        fingerprint_algo_version="gn-v1")
    run = get_run(db, run_id)
    assert run["status"] == "ingested"
    assert run["completed_at"] == _LATER
    assert run["row_count"] == 9
    assert run["quarantined_count"] == 0
    assert run["file_sha256"] == "ab" * 32
    assert run["pre_source_fingerprint"] == "f" * 64
    assert run["post_source_fingerprint"] == "e" * 64
    assert run["fingerprint_algo_version"] == "gn-v1"
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "ingested"]


def test_terminalize_run_only_transitions_from_in_progress(db, no_dsn) -> None:
    """Idempotent-safe: a second terminalize is a no-op — it neither clobbers the terminal state
    nor appends a duplicate history event."""
    run_id = _open(db)
    assert terminalize_run(db, run_id, status="held", now=_LATER, reason_code="brake")
    assert not terminalize_run(db, run_id, status="failed", now=_LATER + timedelta(seconds=5))
    run = get_run(db, run_id)
    assert run["status"] == "held"
    assert run["completed_at"] == _LATER
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "held"]
    assert run["status_history"][-1]["reason_code"] == "brake"


def test_terminalize_run_rejects_non_terminal_status(db, no_dsn) -> None:
    run_id = _open(db)
    with pytest.raises(ValueError, match="terminal"):
        terminalize_run(db, run_id, status="in_progress", now=_LATER)


# ── the independent-connection (durable) paths ────────────────────────────────────────────────────


def test_open_run_survives_request_rollback(db, durable_dsn) -> None:
    run_id = _open(db)
    durable_dsn.append(run_id)
    db.rollback()   # the request transaction dies — the manifest row must not
    with psycopg.connect(os.environ["FEATUREGEN_DSN"]) as fresh:
        run = get_run(fresh, run_id)
    assert run is not None and run["status"] == "in_progress"


def test_terminalize_run_durable_survives_request_rollback(db, durable_dsn) -> None:
    run_id = _open(db)
    durable_dsn.append(run_id)
    db.rollback()   # e.g. a parse failure: the request tx rolls back before the route's except
    terminalize_run_durable(run_id, status="rejected", now=_LATER,
                            redacted_failure_code="UnicodeDecodeError", reason_code="http_400")
    with psycopg.connect(os.environ["FEATUREGEN_DSN"]) as fresh:
        run = get_run(fresh, run_id)
    assert run["status"] == "rejected"
    assert run["completed_at"] == _LATER
    assert run["redacted_failure_code"] == "UnicodeDecodeError"
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "rejected"]
    assert run["status_history"][-1]["reason_code"] == "http_400"


def test_open_run_falls_back_to_request_conn_when_connect_fails(db, monkeypatch) -> None:
    """Best-effort like _record_llm_call_durable: an unreachable DSN degrades to the request
    connection (transactional manifest beats none) instead of failing the upload."""
    monkeypatch.setenv("FEATUREGEN_DSN", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    run_id = _open(db)
    assert get_run(db, run_id)["status"] == "in_progress"


def test_terminalize_run_durable_falls_back_to_given_conn(db, no_dsn) -> None:
    run_id = _open(db)
    terminalize_run_durable(run_id, status="failed", now=_LATER,
                            redacted_failure_code="RuntimeError", fallback_conn=db)
    assert get_run(db, run_id)["status"] == "failed"


def test_terminalize_run_durable_never_raises(no_dsn) -> None:
    """The route calls this from its EXCEPT path: a failing manifest write (e.g. the fallback conn
    sits in an aborted transaction after a real DB fault) must never mask the actual failure."""
    class Broken:
        def execute(self, *args, **kwargs):
            raise RuntimeError("current transaction is aborted")

    terminalize_run_durable("ingrun_X", status="failed", now=_LATER, fallback_conn=Broken())


# ── reconciliation sweep (crash recovery) ─────────────────────────────────────────────────────────

_LEASE = timedelta(minutes=30)


def test_reconcile_abandons_expired_in_progress_runs_only(db, no_dsn) -> None:
    """A run whose process died stays in_progress forever; the sweep terminalizes exactly the
    lease-expired ones to 'abandoned' and leaves fresh + already-terminal runs alone."""
    stale = _open(db, now=_NOW - timedelta(hours=2))
    fresh = _open(db)
    finished = _open(db, now=_NOW - timedelta(hours=2))
    terminalize_run(db, finished, status="ingested", now=_NOW - timedelta(hours=1))

    assert reconcile_ingestion_runs(db, now=_NOW, lease_timeout=_LEASE) == 1

    run = get_run(db, stale)
    assert run["status"] == "abandoned"
    assert run["completed_at"] == _NOW
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "abandoned"]
    assert run["status_history"][-1]["reason_code"] == "lease_expired"
    assert get_run(db, fresh)["status"] == "in_progress"        # heartbeat within the lease
    assert get_run(db, finished)["status"] == "ingested"        # terminal state never clobbered


def test_reconcile_heartbeat_exactly_at_cutoff_is_not_swept(db, no_dsn) -> None:
    """The lease is `heartbeat_at < now - lease_timeout`, strictly — a run heartbeating exactly on
    the boundary still holds its lease."""
    run_id = _open(db, now=_NOW - _LEASE)
    assert reconcile_ingestion_runs(db, now=_NOW, lease_timeout=_LEASE) == 0
    assert get_run(db, run_id)["status"] == "in_progress"


def test_reconcile_is_zero_on_nothing_expired(db, no_dsn) -> None:
    _open(db)
    assert reconcile_ingestion_runs(db, now=_NOW, lease_timeout=_LEASE) == 0


def test_reconcile_is_idempotent(db, no_dsn) -> None:
    """A second sweep finds nothing: the abandoned run is terminal, so it neither re-transitions
    nor appends duplicate history."""
    stale = _open(db, now=_NOW - timedelta(hours=2))
    assert reconcile_ingestion_runs(db, now=_NOW, lease_timeout=_LEASE) == 1
    assert reconcile_ingestion_runs(db, now=_NOW, lease_timeout=_LEASE) == 0
    assert [e["status"] for e in get_run(db, stale)["status_history"]] == \
        ["in_progress", "abandoned"]


# ── effective config snapshot ─────────────────────────────────────────────────────────────────────


def test_effective_config_snapshot_is_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)
    monkeypatch.delenv("OVERLAY_TABLE_SYNTH", raising=False)
    monkeypatch.setenv("FEATUREGEN_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FEATUREGEN_LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("FEATUREGEN_DSN", "postgres://user:SECRET@host/db")   # must never appear
    snap = _effective_config_snapshot()
    assert snap == {
        "config_schema_version": 1,
        "governed_joins": True,   # OVERLAY_PASS_C implies the governed joins_to seam
        "pass_c": True,
        "table_synth": False,
        "llm_provider": "anthropic",
        "llm_model": "claude-sonnet-5",
    }
    assert "SECRET" not in str(snap)


def test_effective_config_snapshot_all_off(monkeypatch) -> None:
    for var in ("OVERLAY_PASS_C", "OVERLAY_GOVERNED_JOINS", "OVERLAY_TABLE_SYNTH",
                "FEATUREGEN_LLM_PROVIDER", "FEATUREGEN_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    snap = _effective_config_snapshot()
    assert snap["governed_joins"] is False and snap["pass_c"] is False
    assert snap["table_synth"] is False
    assert snap["llm_provider"] is None and snap["llm_model"] is None


# ── source fingerprint ────────────────────────────────────────────────────────────────────────────


def _node(db, ref: str, *, kind: str = "column", data_type: str | None = "integer",
          concept: str | None = None, source: str = "deposits") -> None:
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, concept) VALUES (%s, %s, %s, 'accounts', 'id', %s, %s)",
        (source, ref, kind, data_type, concept))


def test_source_fingerprint_is_deterministic_and_versioned(db) -> None:
    _node(db, "public.accounts.id")
    _node(db, "public.accounts.balance", data_type="numeric")
    fp1, algo = source_fingerprint(db, "deposits")
    fp2, _ = source_fingerprint(db, "deposits")
    assert algo == "gn-v1"
    assert fp1 == fp2
    assert len(fp1) == 64   # hex sha256


def test_source_fingerprint_tracks_node_changes_per_source(db) -> None:
    _node(db, "public.accounts.id")
    before, _ = source_fingerprint(db, "deposits")
    _node(db, "public.other.col", source="loans")     # another catalog: no effect
    assert source_fingerprint(db, "deposits")[0] == before
    db.execute("UPDATE graph_node SET concept = 'account identity' "
               "WHERE catalog_source = 'deposits' AND object_ref = 'public.accounts.id'")
    changed, _ = source_fingerprint(db, "deposits")
    assert changed != before
    empty, _ = source_fingerprint(db, "never-uploaded")
    assert empty not in (before, changed)
