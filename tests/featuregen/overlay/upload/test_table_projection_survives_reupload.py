"""Task 0.6 Seam 5a — table DISPLAY projections must survive a re-upload with Pass B OFF.

``build_graph`` DELETEs every graph_node row for the source and recreates them with NULL display
columns. The only unconditional table-ref reprojection lived inside the Pass-B-gated block
(``table_synth_enabled()``, default off), and the glossary path re-projects only THIS upload's
table-term refs — so a plain re-upload wiped a confirmed ``table_role``/``primary_entity``/
``domain`` from the table node even though its evidence and decisions survive untouched. The
repair re-projects every schema-agreeing table ref that still carries active field evidence,
without double-projecting refs a stage above already handled.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref

_SRC = "deposits"
_ROWS = [CanonicalRow(_SRC, "transactions", "amount", "numeric")]
_TABLE_REF = normalize_ref(_SRC, None, "transactions")
_TABLE_OBJ = "public.transactions"


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _confirm_table_role(db, value: str) -> None:
    record_field_evidence(
        db, logical_ref=_TABLE_REF, field_name="table_role", proposed_value=value,
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="user:reviewer", source_snapshot_id="human-confirm-1",
        input_hash=field_input_hash(logical_ref=_TABLE_REF, field_name="table_role",
                                    material=value))
    resolve_and_project(db, source=_SRC, logical_refs=[_TABLE_REF])


def _projected_table_role(db):
    row = db.execute(
        "SELECT table_role FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (_SRC, _TABLE_OBJ)).fetchone()
    return row[0] if row else None


def test_confirmed_table_projection_survives_reupload_with_pass_b_off(db, monkeypatch):
    monkeypatch.delenv("OVERLAY_TABLE_SYNTH", raising=False)   # Pass B off (the default)
    _seal()
    t0 = datetime(2026, 7, 5, tzinfo=UTC)
    assert ingest_upload(db, _SRC, _ROWS, actor=_actor(), now=t0).status == "ingested"

    _confirm_table_role(db, "fact")
    assert _projected_table_role(db) == "fact"

    # The same file again: build_graph wipes + recreates the table node — the confirmed display
    # value must be re-projected from the surviving evidence, not silently blanked.
    assert ingest_upload(db, _SRC, _ROWS, actor=_actor(),
                         now=t0 + timedelta(minutes=5)).status == "ingested"
    assert _projected_table_role(db) == "fact"


def test_reprojection_records_one_decision_per_reupload_not_two(db, monkeypatch):
    # The repair must not fight the existing paths: with Pass B off and no glossary table term,
    # exactly ONE new RESOLVED decision lands per re-upload (the unconditional reprojection), not a
    # double from a second projecting stage.
    monkeypatch.delenv("OVERLAY_TABLE_SYNTH", raising=False)
    _seal()
    t0 = datetime(2026, 7, 5, tzinfo=UTC)
    assert ingest_upload(db, _SRC, _ROWS, actor=_actor(), now=t0).status == "ingested"
    _confirm_table_role(db, "fact")

    def _n_decisions():
        return db.execute(
            "SELECT count(*) FROM field_decision_event WHERE logical_ref = %s "
            "AND field_name = 'table_role'", (_TABLE_REF,)).fetchone()[0]

    before = _n_decisions()
    assert ingest_upload(db, _SRC, _ROWS, actor=_actor(),
                         now=t0 + timedelta(minutes=5)).status == "ingested"
    assert _n_decisions() == before + 1
