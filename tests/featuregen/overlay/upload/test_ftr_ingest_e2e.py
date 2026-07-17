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
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload

_SOURCE = "ftr"
_COL_REF = "public.comp_fin_tran.cust_name"
_TABLE_REF = "public.comp_fin_tran"

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
