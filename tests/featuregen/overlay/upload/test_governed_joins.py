"""Task 7 — the governed `joins_to` seam.

Two layers under test:
  * PURE builders (3a): `parse_join_ref` (table.column | schema.table.column, diagnostics not silent
    Nones) and `governed_join_proposal` (a well-formed parse -> an ApprovedJoinRef).
  * INGEST wiring (3b/3c), behind OVERLAY_GOVERNED_JOINS=1:
      - flag OFF (default) -> today's behaviour: the raw 'joins' edge is authority='operational',
        no approved_join proposal is written.
      - flag ON + a registered catalog adapter -> the raw edge is authority='display_only' AND an
        approved_join proposal exists AND the upload still succeeds.
      - flag ON, caller cleared the adapter -> Phase-2 Task 1 wired `ensure_upload_catalog_adapter()`
        as the first line of `ingest_upload`, so the upload-context adapter is ALWAYS re-ensured at
        the ingest chokepoint (the Phase-1 dependency is now satisfied) and the proposal IS written.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import fact_key
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import governed_join_proposal, parse_join_ref
from featuregen.overlay.upload.ingest import ingest_upload

_NOW = datetime(2026, 7, 11, tzinfo=UTC)


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


def _edge_authority(db, source: str, from_ref: str) -> str:
    row = db.execute(
        "SELECT authority FROM graph_edge WHERE catalog_source = %s AND kind = 'joins' "
        "AND from_ref = %s", (source, from_ref)).fetchone()
    assert row is not None, "expected a 'joins' edge to be written"
    return row[0]


# --- 3a: pure parser + builder -------------------------------------------------------------------

def test_parse_table_column_and_schema_qualified():
    assert parse_join_ref("accounts.id").ok and parse_join_ref("accounts.id").to_table == "accounts"
    q = parse_join_ref("public.accounts.id")
    assert q.ok and q.to_table == "accounts" and q.to_col == "id"


def test_parse_two_part_extracts_table_and_column():
    p = parse_join_ref("accounts.id")
    assert p.ok and p.to_table == "accounts" and p.to_col == "id" and p.diagnostic is None


def test_malformed_join_yields_diagnostic_not_silent_none():
    bad = parse_join_ref("accounts")            # no column
    assert not bad.ok and bad.diagnostic        # a reason, not a silent drop
    assert bad.to_table is None and bad.to_col is None


def test_empty_string_yields_diagnostic():
    bad = parse_join_ref("")
    assert not bad.ok and bad.diagnostic


def test_empty_column_component_yields_diagnostic():
    bad = parse_join_ref("accounts.")           # trailing dot -> empty column
    assert not bad.ok and bad.diagnostic
    empty_table = parse_join_ref(".id")         # leading dot -> empty table
    assert not empty_table.ok and empty_table.diagnostic


def test_too_many_parts_yields_diagnostic():
    bad = parse_join_ref("db.public.accounts.id")   # 4 parts, unsupported
    assert not bad.ok and bad.diagnostic


def test_declared_join_builds_approved_join_ref():
    ref = governed_join_proposal(CanonicalRow("deposits", "transactions", "account_id", "integer",
                                              joins_to="accounts.id", cardinality="N:1"))
    assert ref is not None
    assert ref.from_ref.table == "transactions" and ref.to_ref.table == "accounts"
    assert ref.cardinality == "N:1" and ref.column_pairs[0].from_col == "account_id"
    assert ref.column_pairs[0].to_col == "id"
    assert ref.from_ref.catalog_source == "deposits" and ref.to_ref.catalog_source == "deposits"
    assert ref.from_ref.object_kind == "column" and ref.from_ref.column == "account_id"


def test_governed_proposal_defaults_cardinality_to_n1():
    ref = governed_join_proposal(CanonicalRow("deposits", "transactions", "account_id", "integer",
                                              joins_to="accounts.id"))
    assert ref is not None and ref.cardinality == "N:1"


def test_governed_proposal_none_for_absent_or_malformed_join():
    assert governed_join_proposal(
        CanonicalRow("deposits", "transactions", "account_id", "integer")) is None
    assert governed_join_proposal(
        CanonicalRow("deposits", "transactions", "account_id", "integer",
                     joins_to="accounts")) is None   # malformed -> None, not a crash


# --- 3b/3c: ingest-level off / on --------------------------------------------------------------

def test_flag_off_writes_operational_edge_and_no_proposal(db, monkeypatch):
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)
    _clear_catalog_adapter()
    _seal_config()
    res = ingest_upload(db, "deposits", _join_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    assert _edge_authority(db, "deposits", "public.transactions.acct_id") == "operational"
    ref = governed_join_proposal(_join_rows()[0])
    assert load_fact(db, fact_key(ref, "approved_join")) == []   # no governed proposal


def test_flag_on_with_adapter_marks_display_only_and_proposes(db, monkeypatch, catalog):
    monkeypatch.setenv("OVERLAY_GOVERNED_JOINS", "1")
    _seal_config()
    res = ingest_upload(db, "deposits", _join_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"                              # upload still succeeds
    assert _edge_authority(db, "deposits", "public.transactions.acct_id") == "display_only"
    ref = governed_join_proposal(_join_rows()[0])
    events = load_fact(db, fact_key(ref, "approved_join"))
    assert any(e.type == "OVERLAY_FACT_PROPOSED" for e in events)   # governed proposal exists


def test_flag_on_ingest_reensures_adapter_and_proposes(db, monkeypatch):
    # Phase-2 Task 1: even when the caller cleared the process adapter, ingest_upload re-ensures the
    # UploadContextAdapter on its first line, so the governed-join proposal is now written (this is
    # the un-gating of the Phase-1-deferred _propose_governed_joins).
    monkeypatch.setenv("OVERLAY_GOVERNED_JOINS", "1")
    _clear_catalog_adapter()                                    # ingest_upload re-ensures it (Task 1)
    _seal_config()
    try:
        res = ingest_upload(db, "deposits", _join_rows(), actor=_actor(), now=_NOW)
        assert res.status == "ingested"                         # never aborts the upload
        assert _edge_authority(db, "deposits", "public.transactions.acct_id") == "display_only"
        ref = governed_join_proposal(_join_rows()[0])
        events = load_fact(db, fact_key(ref, "approved_join"))
        assert any(e.type == "OVERLAY_FACT_PROPOSED" for e in events)  # proposal written, not skipped
    finally:
        _clear_catalog_adapter()   # don't leak the ensured process adapter into later tests
