"""Phase 3C.2b-i-B · Task 7 — DB-backed tests for the governed source-side structural binding.

Every authority is established through the REAL governed commands — NEVER a hand-built object we
then assert on, and NEVER a manufactured grain/entity:
  * the VERIFIED grain fact via the real four-eyes flow (``propose_fact`` -> ``confirm_fact`` ->
    drain), driven through the spike's ``confirm_grain_fact`` helper;
  * each grain-key column's concept via the REAL evidence writer ``record_field_evidence`` at
    ``(HUMAN, CONFIRMED)`` — exactly the accepted cohort T5's resolver reads (T7 reads concept
    authority through that resolver over ``field_evidence``, not ``graph_node.concept``, so the
    evidence writer alone is the faithful, sufficient setup);
  * the physical graph + ``schema_name`` via the REAL ``ingest_upload`` path (a technical upload for
    the ``public`` cases; a business-glossary upload declaring ``DPL_EIB_COMPLIANCE`` for the
    schema-recovery case).

A test that passed by faking a VERIFIED grain, or by asserting an entity T7 did not derive from the
keys' governed concepts, would be a failure of THIS file, not a pass.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner import b_slice_spike as spike
from featuregen.overlay.upload.planner.b_concept_authority import (
    ConceptRejection,
    resolve_planner_concept_binding,
)
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_source_grain import (
    GovernedSourceBindingV1,
    SourceBindingReason,
    SourceBindingRejection,
    reason_to_b_disposition,
    resolve_source_binding,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedSourceBindingV1 as ContractsBinding,
)
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref

_NOW = datetime(2026, 7, 22, tzinfo=UTC)

# T7 returns the contracts' GovernedSourceBindingV1 (re-exported by the module). Guard the identity
# so a future divergence is caught here rather than silently.
assert GovernedSourceBindingV1 is ContractsBinding


def _data_owner() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _confirm_concept(db, logical_ref: str, concept: str) -> None:
    """Write ONE real ``(HUMAN, CONFIRMED)`` concept field-evidence row via the production writer —
    the accepted cohort T5's ``resolve_planner_concept_binding`` binds on."""
    record_field_evidence(
        db, logical_ref=logical_ref, field_name="concept", proposed_value=concept,
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="human:admin", source_snapshot_id="t7-snap",
        input_hash=field_input_hash(logical_ref=logical_ref, field_name="concept", material=concept))


def _ingest_technical(db, source: str, table: str, columns: list[str]) -> None:
    """Ingest a technical upload (no glossary) — every column node lands with ``schema_name`` NULL,
    so T7's schema recovery falls back to ``public``."""
    rows = [CanonicalRow(source, table, c, "numeric" if c == "amt" else "varchar")
            for c in columns]
    res = ingest_upload(db, source, rows, actor=_data_owner(), now=_NOW, client=None)
    assert res.status == "ingested", f"technical ingest failed: {res.status} / {res.reason}"


def _stand_up_public(db, source, table, *, columns, grain, concepts, service_actor, human_actor):
    """Real ingest + real (HUMAN,CONFIRMED) concepts (public logical_ref) + a REAL VERIFIED grain."""
    _ingest_technical(db, source, table, columns)
    for col, con in concepts.items():
        _confirm_concept(db, normalize_ref(source, None, table, col), con)
    spike.confirm_grain_fact(db, source=source, table=table, columns=grain,
                             service_actor=service_actor, human_actor=human_actor)


def _operand_ref(table: str) -> str:
    """The operand column's flattened graph ref (``amt`` is the measure operand in every table)."""
    return f"public.{table}.amt"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 1 — single-key governed grain -> one entity, deterministic fact_key (matches A).
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_single_key_governed_grain(db, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    src, tbl = "t7_single", "txn"
    _stand_up_public(db, src, tbl, columns=["tran_id", "amt"], grain=["tran_id"],
                     concepts={"tran_id": "transaction_id"},
                     service_actor=service_actor, human_actor=human_actor)
    adapter = current_catalog_adapter()

    res = resolve_source_binding(db, adapter, catalog_source=src,
                                 object_ref=_operand_ref(tbl), now=_NOW)

    assert isinstance(res, GovernedSourceBindingV1)
    assert res.source_grain_entity == "transaction"
    assert res.source_grain_key_refs == (f"public.{tbl}.tran_id",)
    # deterministic grain fact_key — the SAME key A's source-endpoint revalidation recomputes.
    assert res.grain_fact_key == fact_key(table_ref(src, tbl), "grain")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 2 — composite grain: one entity-linked key + a temporal partition key with no entity_link.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_composite_grain_one_entity_plus_partition(db, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    src, tbl = "t7_composite", "bal"
    _stand_up_public(db, src, tbl, columns=["account_id", "as_of_date", "amt"],
                     grain=["account_id", "as_of_date"],
                     concepts={"account_id": "account_id", "as_of_date": "as_of_date"},
                     service_actor=service_actor, human_actor=human_actor)
    adapter = current_catalog_adapter()

    res = resolve_source_binding(db, adapter, catalog_source=src,
                                 object_ref=_operand_ref(tbl), now=_NOW)

    assert isinstance(res, GovernedSourceBindingV1)
    assert res.source_grain_entity == "account"
    # BOTH keys stay in the composite key refs; the partition key is not an entity.
    assert res.source_grain_key_refs == (f"public.{tbl}.account_id", f"public.{tbl}.as_of_date")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 3 — multiple keys AGREEING on one entity is NOT a conflict.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_multiple_agreeing_entity_links(db, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    src, tbl = "t7_agree", "cust"
    _stand_up_public(db, src, tbl, columns=["cif_id", "customer_ref", "amt"],
                     grain=["cif_id", "customer_ref"],
                     concepts={"cif_id": "customer_id", "customer_ref": "customer_id"},
                     service_actor=service_actor, human_actor=human_actor)
    adapter = current_catalog_adapter()

    res = resolve_source_binding(db, adapter, catalog_source=src,
                                 object_ref=_operand_ref(tbl), now=_NOW)

    assert isinstance(res, GovernedSourceBindingV1)
    assert res.source_grain_entity == "customer"
    assert res.source_grain_key_refs == (f"public.{tbl}.cif_id", f"public.{tbl}.customer_ref")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 4 — DISTINCT entity-links across keys -> source_entity_conflict -> source_entity_ungoverned.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_conflicting_entity_links(db, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    src, tbl = "t7_conflict", "mix"
    _stand_up_public(db, src, tbl, columns=["account_id", "customer_id", "amt"],
                     grain=["account_id", "customer_id"],
                     concepts={"account_id": "account_id", "customer_id": "customer_id"},
                     service_actor=service_actor, human_actor=human_actor)
    adapter = current_catalog_adapter()

    res = resolve_source_binding(db, adapter, catalog_source=src,
                                 object_ref=_operand_ref(tbl), now=_NOW)

    assert isinstance(res, SourceBindingRejection)
    assert res.reason is SourceBindingReason.source_entity_conflict
    assert reason_to_b_disposition(res.reason) is BDisposition.source_entity_ungoverned


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 5 — no entity-linked key (a governed concept whose entity_link is None) -> source_entity_missing.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_no_entity_linked_key(db, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    src, tbl = "t7_noentity", "snap"
    _stand_up_public(db, src, tbl, columns=["as_of_date", "amt"], grain=["as_of_date"],
                     concepts={"as_of_date": "as_of_date"},
                     service_actor=service_actor, human_actor=human_actor)
    adapter = current_catalog_adapter()

    res = resolve_source_binding(db, adapter, catalog_source=src,
                                 object_ref=_operand_ref(tbl), now=_NOW)

    assert isinstance(res, SourceBindingRejection)
    assert res.reason is SourceBindingReason.source_entity_missing
    assert reason_to_b_disposition(res.reason) is BDisposition.source_entity_ungoverned


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 6 — no VERIFIED grain fact (proposed-only / none) -> no_verified_grain_fact -> structural_need.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_no_verified_grain_fact(db, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    src, tbl = "t7_nograin", "raw"
    # Ingest the table + concept, but NEVER confirm a grain — resolve_fact serves VERIFIED-only.
    _ingest_technical(db, src, tbl, ["tran_id", "amt"])
    _confirm_concept(db, normalize_ref(src, None, tbl, "tran_id"), "transaction_id")
    adapter = current_catalog_adapter()

    res = resolve_source_binding(db, adapter, catalog_source=src,
                                 object_ref=_operand_ref(tbl), now=_NOW)

    assert isinstance(res, SourceBindingRejection)
    assert res.reason is SourceBindingReason.no_verified_grain_fact
    assert reason_to_b_disposition(res.reason) is BDisposition.structural_need_ungoverned


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 7 — SCHEMA-PRESERVING concept resolution (the new seam): graph_node.schema_name is a NON-public
# declared schema, and the concept evidence lives under that real schema's logical_ref. T7 must
# recover the schema and still bind the entity — proving it does NOT assume `public`.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_schema_preserving_concept_resolution(db, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    src, tbl, schema = "t7_schema", "grn", "DPL_EIB_COMPLIANCE"
    # Real technical ingest builds the flattened graph nodes; then reproduce the FTR declared-schema
    # condition by stamping graph_node.schema_name = the real (non-public) schema. schema_name is
    # DECLARED METADATA (what the source's glossary declared) — NOT grain/entity/key authority, all
    # of which stay fully governed below (the VERIFIED grain fact + real concept evidence). This is
    # exactly the graph state the FTR adapter (schema_by_ref) lands for DPL_EIB_COMPLIANCE.
    _ingest_technical(db, src, tbl, ["acct", "amt"])
    db.execute(
        "UPDATE graph_node SET schema_name=%s WHERE catalog_source=%s AND object_ref=%s "
        "AND kind='column'", (schema, src, f"public.{tbl}.acct"))

    # graph_node.schema_name carries the REAL declared schema on the flattened node.
    schema_row = db.execute(
        "SELECT schema_name FROM graph_node WHERE catalog_source=%s AND object_ref=%s "
        "AND kind='column'", (src, f"public.{tbl}.acct")).fetchone()
    assert schema_row is not None and schema_row[0] == schema

    # Concept evidence keyed on the SCHEMA-PRESERVING logical_ref (the real schema), NOT public.
    _confirm_concept(db, normalize_ref(src, schema, tbl, "acct"), "account_id")
    # CONTROL: nothing is confirmed under the public-flattened ref, so a T7 that assumed `public`
    # would find NO governed concept and fail source_entity_missing. It must recover the schema.
    assert isinstance(resolve_planner_concept_binding(db, normalize_ref(src, None, tbl, "acct")),
                      ConceptRejection)

    spike.confirm_grain_fact(db, source=src, table=tbl, columns=["acct"],
                             service_actor=service_actor, human_actor=human_actor)
    adapter = current_catalog_adapter()

    res = resolve_source_binding(db, adapter, catalog_source=src,
                                 object_ref=_operand_ref(tbl), now=_NOW)

    assert isinstance(res, GovernedSourceBindingV1)
    assert res.source_grain_entity == "account"
    assert res.source_grain_key_refs == (f"public.{tbl}.acct",)
    assert res.grain_fact_key == fact_key(table_ref(src, tbl), "grain")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 8 — defense-in-depth: a REAL VERIFIED grain naming a column the graph does not carry ->
# grain_columns_absent -> structural_need_ungoverned (mirrors A's governed_endpoint membership).
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_grain_names_column_absent_from_graph(db, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    src, tbl = "t7_absent", "part"
    _ingest_technical(db, src, tbl, ["tran_id", "amt"])   # graph carries tran_id, amt — NOT "ghost"
    _confirm_concept(db, normalize_ref(src, None, tbl, "tran_id"), "transaction_id")
    # A genuine four-eyes VERIFIED grain that names a column absent from the graph.
    spike.confirm_grain_fact(db, source=src, table=tbl, columns=["tran_id", "ghost"],
                             service_actor=service_actor, human_actor=human_actor)
    adapter = current_catalog_adapter()

    res = resolve_source_binding(db, adapter, catalog_source=src,
                                 object_ref=_operand_ref(tbl), now=_NOW)

    assert isinstance(res, SourceBindingRejection)
    assert res.reason is SourceBindingReason.grain_columns_absent
    assert reason_to_b_disposition(res.reason) is BDisposition.structural_need_ungoverned


# ════════════════════════════════════════════════════════════════════════════════════════════════
# guard — a malformed operand object_ref is a caller contract violation, surfaced not masked.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_malformed_object_ref_raises(db):
    ensure_upload_catalog_adapter()
    adapter = current_catalog_adapter()
    with pytest.raises(ValueError):
        resolve_source_binding(db, adapter, catalog_source="s",
                               object_ref="not_a_flattened_ref", now=_NOW)
