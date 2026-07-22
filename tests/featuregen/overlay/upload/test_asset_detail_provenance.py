"""P1a — effective_metadata surfaces a value's evidence provenance when there is no governed decision,
so a known-author value never reads as 'unattested'."""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

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


def test_evidence_recorded_under_a_real_schema_is_readable(overlay_conn):
    """logical_ref_of used to hardcode schema="public" when rebuilding the key it reads
    field_evidence/field_decision under, so evidence recorded under a REAL (non-public) schema's
    schema-preserving logical_ref (the FTR norm) was invisible to build_asset_detail even though it
    was recorded and active. graph_node.schema_name now drives that lookup, so it must surface."""
    source = "prov_realschema"
    object_ref = "public.comp_financial_tran_repos_dly.cif_id"
    build_graph(
        overlay_conn, source,
        [CanonicalRow(source, "comp_financial_tran_repos_dly", "cif_id", "text")],
        schemas={object_ref: "DPL_EIB_COMPLIANCE"},
    )
    real_schema_ref = normalize_ref(
        source, "DPL_EIB_COMPLIANCE", "comp_financial_tran_repos_dly", "cif_id")
    assert real_schema_ref == f"{source}::dpl_eib_compliance.comp_financial_tran_repos_dly.cif_id"
    record_field_evidence(
        overlay_conn, logical_ref=real_schema_ref, field_name="concept",
        proposed_value="customer_identifier", producer="llm", strength="proposed",
        producer_ref="test", source_snapshot_id="snap", input_hash="h3",
    )
    body = build_asset_detail(
        overlay_conn, source=source, object_ref=object_ref,
        roles=list(ADMIN.role_claims), identity=ADMIN, include=["effective_metadata"])
    field = body["effective_metadata"]["fields"]["concept"]
    assert field["provenance"] is None                     # no governed decision
    assert field["evidence_provenance"] == "AI proposed"    # but the real-schema evidence is found
