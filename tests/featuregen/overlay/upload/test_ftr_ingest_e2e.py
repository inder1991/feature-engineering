"""Task 5 — FTR ingest end-to-end: additive schema preservation + the cross-schema fence.

Contracts under test (round-4 resolutions #1/#4/#5):
- ``build_graph`` retains the REAL (pre-flatten) schema additively: the column node carries
  ``schema_name`` (raw case, as declared) and the bounded, NON-operational ``declared_type``,
  while the operational ``data_type`` stays ``UNKNOWN_TYPE`` (#1). The table node carries
  ``schema_name`` too (#5 — written by ``build_graph``'s INSERTs, never ``resolve_and_project``).
- A second upload that would silently re-attribute an existing ``public.table.column`` to a
  DIFFERENT schema is HELD fail-closed BEFORE any side effect (#4): honest ``IngestResult``
  (``held``, quarantined == the REAL conflict count, never ``len(rows)``) and the existing
  node's ``schema_name`` untouched (no delete/rebuild happened).

Fixture is the Task-3a inline ``_FTR_CSV`` (never read from ~/Downloads); refs are LOWERCASE and
every query is ``catalog_source``-scoped.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen.overlay.upload.test_ftr_adapter import _FTR_CSV

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.search import search

_SOURCE = "ftr"
_COL_REF = "public.comp_fin_tran.cust_name"
_TABLE_REF = "public.comp_fin_tran"
_TABLE_LREF = "ftr::dpl_eib_compliance.comp_fin_tran"   # schema-preserving evidence key
_AMT_LREF = "ftr::dpl_eib_compliance.comp_fin_tran.txn_amt"   # schema-preserving column key

NOW = datetime(2026, 7, 17, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _ingest(db, csv_text: str):
    upload = to_glossary_upload(read_ftr_glossary(csv_text, source=_SOURCE))
    return ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=NOW, client=None,
                         glossary=upload)


def _node(db, object_ref: str, *cols):
    return db.execute(
        f"SELECT {', '.join(cols)} FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s", (_SOURCE, object_ref)).fetchone()


def test_schema_name_and_declared_type_retained(db):
    _seal()
    res = _ingest(db, _FTR_CSV)
    assert res.status == "ingested"

    # Column node: real schema preserved additively (raw case), FTR-declared type retained as
    # NON-operational metadata, operational type stays UNKNOWN_TYPE (resolution #1).
    row = _node(db, _COL_REF, "schema_name", "declared_type", "data_type")
    assert row == ("DPL_EIB_COMPLIANCE", "varchar", UNKNOWN_TYPE)

    # Table node too (resolution #5 — build_graph writes schema_name in BOTH INSERTs).
    assert _node(db, _TABLE_REF, "schema_name") == ("DPL_EIB_COMPLIANCE",)


def test_cross_schema_upload_is_held(db):
    _seal()
    assert _ingest(db, _FTR_CSV).status == "ingested"

    # Second upload to the SAME source re-attributes CUST_NAME to a different schema. Only that
    # one column conflicts (TXN_AMT still agrees), so the REAL conflict count is exactly 1.
    second = _FTR_CSV.replace("DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.CUST_NAME",
                              "OTHER_SCHEMA.COMP_FIN_TRAN.CUST_NAME")
    res = _ingest(db, second)
    assert res.status == "held"
    assert res.quarantined == 1                      # the real conflict count, never len(rows)
    assert "schema conflict" in (res.reason or "")

    # Fail-closed BEFORE any side effect: the original attribution survives untouched — the node
    # still exists with its original schema_name, proving no delete/rebuild ran.
    assert _node(db, _COL_REF, "schema_name") == ("DPL_EIB_COMPLIANCE",)


def test_table_term_reaches_table_node_via_evidence(db):
    """Task 6 (round-4 #5): the ONE table-level term reaches the TABLE node through the SAME
    governed source-evidence + resolve_and_project path columns use — never a direct UPDATE.
    ``build_graph`` inserts the table node with ``definition = NULL``; only the projection of the
    resolved evidence may fill it."""
    _seal()
    assert _ingest(db, _FTR_CSV).status == "ingested"

    row = db.execute(
        "SELECT definition, domain, definition_decision_id FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
        (_SOURCE, _TABLE_REF)).fetchone()
    assert row is not None
    definition, domain, definition_decision_id = row
    assert definition is not None
    assert "compliance transaction repository" in definition.lower()
    assert (domain or "").lower() == "compliance"
    # The projection recorded a field DECISION and linked it (display != authority) — proof the
    # value came from resolve_and_project, not a write straight onto the node.
    assert definition_decision_id is not None

    # And the SOURCE evidence rows live at the schema-preserving TABLE logical_ref — proof the
    # term flowed through the evidence store.
    fields = {r[0] for r in db.execute(
        "SELECT field_name FROM field_evidence WHERE logical_ref = %s AND lifecycle = 'active'",
        (_TABLE_LREF,)).fetchall()}
    assert {"business_term", "definition", "domain"} <= fields


def test_parser_evidence_survives_sanitization(db):
    """Task 7 (round-4 #4): the sample clause is STRIPPED from the definition at parse time, so
    re-parsing ``rec.definition`` at evidence time finds nothing. The SAFE facets the sanitizer
    captured BEFORE stripping (``GlossaryRecord.logical_representation`` / ``.semantic_type``) must
    still reach ``field_evidence`` as ACTIVE parser:supported rows at the schema-preserving ref."""
    _seal()
    # TXN_AMT's definition now embeds a canonical FTR sample clause (pre-flight verified:
    # parse_sample_profile -> decimal/amount; sanitize -> state='stripped', prose kept).
    with_sample = _FTR_CSV.replace(
        '"The monetary amount of the transaction."',
        '"The monetary amount of the transaction. The sample profile is NUMERIC, with '
        'representative values such as 100.00; 200.00, which supports interpretation as '
        'an amount."')
    assert with_sample != _FTR_CSV                     # the TXN_AMT row really was rewritten
    assert _ingest(db, with_sample).status == "ingested"

    for field_name, expected in (("logical_representation", "decimal"),
                                 ("semantic_type", "amount")):
        rows = [e for e in read_active_field_evidence(db, _AMT_LREF, field_name)
                if e.producer == "parser"]
        assert rows, f"no ACTIVE parser evidence for {field_name}"
        assert {e.strength for e in rows} == {"supported"}
        assert {e.proposed_value for e in rows} == {expected}


def test_table_term_schema_disagreement_skips_table_evidence(db):
    """Round-4 #5 tail: a table term whose declared schema disagrees with its columns' is SKIPPED
    (the columns are authoritative for the schema) — no evidence attached, node left NULL."""
    _seal()
    disagreeing = _FTR_CSV.replace("DPL_EIB_COMPLIANCE.COMP_FIN_TRAN,Financial",
                                   "OTHER_SCHEMA.COMP_FIN_TRAN,Financial")
    assert disagreeing != _FTR_CSV                     # the table row really was rewritten
    assert _ingest(db, disagreeing).status == "ingested"

    assert _node(db, _TABLE_REF, "definition") == (None,)
    assert db.execute(
        "SELECT count(*) FROM field_evidence WHERE logical_ref = %s",
        ("ftr::other_schema.comp_fin_tran",)).fetchone() == (0,)


# ── Task 8: glossary semantics reach full-text search via semantic_terms ─────────────────────────
# CUST_NAME's synonym "Account Holder" and its BIAN level "Identification" appear ONLY in the
# glossary sidecar — never in the column name or definition — so a hit proves the semantic_terms
# slot is populated AND indexed (search_doc rebuilt after the projection).

def test_search_finds_column_by_synonym(db):
    _seal()
    assert _ingest(db, _FTR_CSV).status == "ingested"

    hits = search(db, "Account Holder", now=NOW, roles=["catalog_viewer"]).hits
    assert any(h.object_ref == _COL_REF and h.catalog_source == _SOURCE for h in hits)


def test_search_finds_by_bian_path(db):
    _seal()
    assert _ingest(db, _FTR_CSV).status == "ingested"

    # "Identification" is BIAN level 3 for CUST_NAME (bian_path), nowhere else in the fixture.
    hits = search(db, "Identification", now=NOW, roles=["catalog_viewer"]).hits
    assert any(h.object_ref == _COL_REF and h.catalog_source == _SOURCE for h in hits)


def test_search_finds_table_by_term(db):
    _seal()
    assert _ingest(db, _FTR_CSV).status == "ingested"

    # "Financial" appears only in the TABLE term "Financial Transaction Repository" — not in the
    # table name (comp_fin_tran) or its projected definition — so the TABLE node's semantic_terms
    # must be populated too (round-4 #8).
    hits = search(db, "Financial", now=NOW, roles=["catalog_viewer"]).hits
    assert any(h.object_ref == _TABLE_REF and h.kind == "table" and h.catalog_source == _SOURCE
               for h in hits)


def test_semantic_terms_column_populated(db):
    _seal()
    assert _ingest(db, _FTR_CSV).status == "ingested"

    # The raw projection column (tsvector-agnostic): non-empty and carrying the synonym verbatim.
    row = _node(db, _COL_REF, "semantic_terms")
    assert row is not None
    (semantic_terms,) = row
    assert semantic_terms
    assert "account holder" in semantic_terms.lower()
