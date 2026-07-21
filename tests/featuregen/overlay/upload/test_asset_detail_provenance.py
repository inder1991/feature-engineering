"""P1a — effective_metadata surfaces a value's evidence provenance when there is no governed decision,
so a known-author value never reads as 'unattested'."""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform_admin",))


def _concept_field(conn, source):
    body = build_asset_detail(conn, source=source, object_ref="public.trades.notional",
                              roles=list(ADMIN.role_claims), identity=ADMIN,
                              include=["effective_metadata"])
    return body["effective_metadata"]["fields"]["concept"]


def test_unconfirmed_value_carries_its_evidence_provenance(overlay_conn):
    source = "prov_ai"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "notional", "numeric")])
    record_field_evidence(
        overlay_conn, logical_ref=f"{source}::public.trades.notional", field_name="concept",
        proposed_value="monetary_flow", producer="llm", strength="proposed",
        producer_ref="test", source_snapshot_id="snap", input_hash="h1",
    )
    field = _concept_field(overlay_conn, source)
    assert field["provenance"] is None                     # no governed decision
    assert field["evidence_provenance"] == "AI proposed"    # but the author is known


def test_source_attested_value_reads_source_attested(overlay_conn):
    source = "prov_src"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "notional", "numeric")])
    record_field_evidence(
        overlay_conn, logical_ref=f"{source}::public.trades.notional", field_name="concept",
        proposed_value="monetary_flow", producer="source", strength="attested",
        producer_ref="test", source_snapshot_id="snap", input_hash="h2",
    )
    assert _concept_field(overlay_conn, source)["evidence_provenance"] == "source attested"


def test_no_evidence_leaves_provenance_none(overlay_conn):
    source = "prov_none"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "notional", "numeric")])
    field = _concept_field(overlay_conn, source)
    assert field["provenance"] is None
    assert field["evidence_provenance"] is None
