from datetime import datetime

from tests.featuregen.overlay._helpers import StubCatalog, catalog_columns

from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.profiler import (
    PROFILE_VERSION,
    ProfilerLimits,
    run_profiler_scan,
)


def _ref(table):
    return CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="public", table=table
    )


def test_scan_proposes_grain_for_unique_column(db):
    db.execute(
        "CREATE TABLE prof_accounts ("
        "account_id integer, region text, posted_at timestamptz)"
    )
    db.execute(
        "INSERT INTO prof_accounts "
        "SELECT g, 'eu', now() FROM generate_series(1, 40) AS g"
    )
    ref = _ref("prof_accounts")
    adapter = StubCatalog(
        objects=catalog_columns(
            ref,
            [("account_id", "integer"), ("region", "text"), ("posted_at", "timestamp with time zone")],
        )
    )
    limits = ProfilerLimits(allowed_schemas=frozenset({"public"}))

    proposals = run_profiler_scan(db, adapter, ref, limits=limits)

    grain = [p for p in proposals if p.fact_type == "grain"]
    assert len(grain) == 1
    p = grain[0]
    assert p.proposed_value == {"columns": ["account_id"], "is_unique": True}
    assert p.evidence_metrics["row_count"] == 40
    assert p.evidence_metrics["metric_values"]["distinct_count"] == 40
    assert p.evidence_metrics["metric_values"]["uniqueness_ratio"] == 1.0
    assert p.evidence_metrics["profile_version"] == PROFILE_VERSION
    assert isinstance(p.evidence_metrics["table_snapshot_at"], datetime)
    # region (distinct=1) is NOT proposed as grain.
    assert all("region" not in gp.proposed_value["columns"] for gp in grain)


def test_scan_detects_availability_time_candidate(db):
    db.execute(
        "CREATE TABLE prof_txns (txn_id integer, posted_at timestamptz, note text)"
    )
    db.execute(
        "INSERT INTO prof_txns "
        "SELECT g, now() - (g || ' hours')::interval, 'x' FROM generate_series(1, 12) AS g"
    )
    ref = _ref("prof_txns")
    adapter = StubCatalog(
        objects=catalog_columns(
            ref,
            [("txn_id", "integer"), ("posted_at", "timestamp with time zone"), ("note", "text")],
        )
    )
    limits = ProfilerLimits(allowed_schemas=frozenset({"public"}))

    proposals = run_profiler_scan(db, adapter, ref, limits=limits)

    avail = [p for p in proposals if p.fact_type == "availability_time"]
    assert len(avail) == 1
    assert avail[0].proposed_value == {"column": "posted_at", "basis": "posted_at"}
    assert avail[0].use_case is None
    # 'note' (text) is never an availability candidate.
    assert avail[0].proposed_value["column"] == "posted_at"
