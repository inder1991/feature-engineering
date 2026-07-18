"""MF-5 — the upload result reports truthful, additive counts (Task 8).

The synthetic single-table FTR fixture and its exact 126/127 numbers land in Task 11's acceptance
test; here we prove the RELATIONSHIPS the truthful counts must satisfy, on a small upload we build
ourselves so this suite has no cross-task fixture dependency:

  objects_stored   == tables + columns   (one node per table + one per column)
  containment_edges == columns           (one `contains` edge per column)
  facts_asserted    == result.asserted    (the Pass A count, not re-derived)
  passb_proposed + passb_abstained accounted (both 0 with Pass B off)
  join_candidates   == 0                   (Pass C default-OFF)

The counts are also cross-checked against the REAL persisted graph so the fields track reality,
not just each other.
"""
from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_success_result_counts_agree(db):
    _seal_config()
    now = datetime(2026, 7, 10, tzinfo=UTC)
    source = "counts_src"
    # 2 tables, 3 columns total; grain on id + cust_id -> 2 Pass A grain facts asserted.
    rows = [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "customers", "cust_id", "integer", is_grain=True),
    ]
    result = ingest_upload(db, source, rows, actor=_actor(), now=now)
    assert result.status == "ingested"

    # The shape of THIS upload, stated truthfully.
    assert result.tables == 2
    assert result.columns == 3
    assert result.objects_stored == result.tables + result.columns   # 5, not "3 stored"
    assert result.containment_edges == result.columns                # 3 contains edges
    assert result.facts_asserted == result.asserted                  # not double-counted

    # Pass B / Pass C are default-OFF: no syntheses, no candidates.
    assert result.passb_proposed == 0
    assert result.passb_abstained == 0
    assert result.join_candidates == 0

    # The counts track the REAL persisted graph, not just one another.
    node_count = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = %s", (source,)).fetchone()[0]
    contains_count = db.execute(
        "SELECT count(*) FROM graph_edge WHERE catalog_source = %s AND kind = 'contains'",
        (source,)).fetchone()[0]
    assert node_count == result.objects_stored
    assert contains_count == result.containment_edges


def test_passb_proposed_and_abstained_accounted(db):
    """With a synthesizing client on, passb_proposed + passb_abstained == the syntheses total, and
    an abstention (no grain AND no as-of) counts as abstained, not proposed."""
    import os

    from featuregen.intake.llm import FakeLLM, FakeResponse

    _seal_config()
    now = datetime(2026, 7, 10, tzinfo=UTC)
    source = "counts_passb"
    rows = [
        CanonicalRow(source, "txn", "txn_id", "varchar"),
        CanonicalRow(source, "txn", "amt", "numeric"),
    ]
    # A synthesis that ABSTAINS: no grain column, no as-of -> passb_abstained, not proposed.
    synthesis = {"grain_columns": [], "as_of_column": None, "as_of_basis": None,
                 "table_role": "fact", "primary_entity": "transaction",
                 "event_or_snapshot": "event"}
    from featuregen.overlay.upload.enrich import content_hash
    hashes = [content_hash(r) for r in rows]
    client = FakeLLM(script={
        "table_synth": FakeResponse(output={"results": [{"ref": "txn", "synthesis": synthesis}]}),
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": "monetary_stock"} for h in hashes]}),
        "overlay.enrich.definition": FakeResponse(output={"results": [
            {"ref": h, "definition": "A one-line business definition."} for h in hashes]}),
        "overlay.enrich.domain": FakeResponse(output={"results": [{"ref": "txn", "domain": "pay"}]}),
    })

    prior = os.environ.get("OVERLAY_TABLE_SYNTH")
    os.environ["OVERLAY_TABLE_SYNTH"] = "1"
    try:
        result = ingest_upload(db, source, rows, actor=_actor(), now=now, client=client)
    finally:
        if prior is None:
            os.environ.pop("OVERLAY_TABLE_SYNTH", None)
        else:
            os.environ["OVERLAY_TABLE_SYNTH"] = prior

    assert result.status == "ingested"
    # One table synthesized, and it abstained (no grain, no as-of).
    assert result.passb_proposed + result.passb_abstained == 1
    assert result.passb_abstained == 1
    assert result.passb_proposed == 0
