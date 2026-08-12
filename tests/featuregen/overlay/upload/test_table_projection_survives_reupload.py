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
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
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


def test_one_bad_ref_does_not_kill_the_remaining_reprojections(db, monkeypatch):
    """FIX 8 (per-ref savepoint) + FIX 6 (stage record): with TWO evidence-bearing table refs, a
    forced failure on the first ref's projection must leave the second ref projected — and the run's
    stage account says so honestly (`partial`, one reprojected, one failed) instead of the whole
    tail block dying on the first fault."""
    from featuregen.overlay.upload import ingest as ingest_module
    from featuregen.overlay.upload.stage_report import StageRecorder

    monkeypatch.delenv("OVERLAY_TABLE_SYNTH", raising=False)
    _seal()
    t0 = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [CanonicalRow(_SRC, "alpha", "amount", "numeric"),
            CanonicalRow(_SRC, "beta", "amount", "numeric")]
    assert ingest_upload(db, _SRC, rows, actor=_actor(), now=t0).status == "ingested"

    ref_alpha = normalize_ref(_SRC, None, "alpha")   # sorts FIRST in the pending set
    ref_beta = normalize_ref(_SRC, None, "beta")
    for ref in (ref_alpha, ref_beta):
        record_field_evidence(
            db, logical_ref=ref, field_name="table_role", proposed_value="fact",
            producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
            producer_ref="user:reviewer", source_snapshot_id="human-confirm-1",
            input_hash=field_input_hash(logical_ref=ref, field_name="table_role",
                                        material="fact"))
        resolve_and_project(db, source=_SRC, logical_refs=[ref])

    real = ingest_module.resolve_and_project

    def _boom(conn, *, source, logical_refs, now=None, **kw):
        if list(logical_refs) == [ref_alpha]:
            raise RuntimeError("forced projection failure (test)")
        return real(conn, source=source, logical_refs=logical_refs, now=now, **kw)

    monkeypatch.setattr(ingest_module, "resolve_and_project", _boom)
    rec = StageRecorder()
    assert ingest_upload(db, _SRC, rows, actor=_actor(), now=t0 + timedelta(minutes=5),
                         stage_recorder=rec).status == "ingested"

    def _table_role(table_obj):
        row = db.execute(
            "SELECT table_role FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
            (_SRC, table_obj)).fetchone()
        return row[0] if row else None

    assert _table_role("public.alpha") is None       # its projection failed (contained)
    assert _table_role("public.beta") == "fact"      # the second ref still projected

    report = next(r for r in rec.reports if r.stage == "table_display_reprojection")
    assert report.state == "partial" and report.reason_code == "items_failed"
    assert report.detail == {"reprojected": 1, "failed": 1}
    assert report.started_at is not None


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
