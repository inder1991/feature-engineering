"""Task 9 (MF-6) — protect the dedicated-source limitation.

An FTR/glossary upload carries a schema (the schema segment of its ``schema.table.column`` FQN), so
it can only enrich a NEW source or an existing FTR-only (schema-carrying) source. Pointed at an
EXISTING schema-less TECHNICAL source (column nodes whose ``schema_name`` is NULL — what a plain
technical CSV upload leaves behind), it previously half-landed behind the column-level cross-schema
fence with an opaque "schema conflict" message (the legacy-NULL policy). ``_source_is_schema_less``
detects that case up front and ``ingest_upload`` returns a ``held`` result with an actionable message
BEFORE any side effect.

The three cases (task resolution #3):
  (a) FTR upload onto a seeded schema-less technical source -> ``held`` + message names an
      "FTR-only source" (and it is THIS guard, not the fence — columns are disjoint so the fence
      would not fire).
  (b) FTR upload onto a brand-new source -> proceeds (not held for this reason).
  (c) FTR upload onto an existing FTR source (schema_name set) -> proceeds (not schema-less).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen.overlay.upload.test_ftr_adapter import _FTR_CSV

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow, RowError
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import _source_is_schema_less, ingest_upload
from featuregen.overlay.upload.review_queue import list_quarantine, persist_quarantine
from featuregen.overlay.upload.stage_report import StageRecorder

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


def _ingest_ftr(db, source: str, stage_recorder: StageRecorder | None = None):
    """Ingest the canonical FTR fixture via the REAL FTR path (adapter -> glossary upload). The FTR
    adapter derives ``schema_name`` from the FQN's schema part, so this SETS schema_name on the nodes
    it builds — the reason a re-upload of an FTR source is not falsely held (case c)."""
    upload = to_glossary_upload(read_ftr_glossary(_FTR_CSV, source=source))
    return ingest_upload(db, source, upload.rows, actor=_actor(), now=NOW, client=None,
                         glossary=upload, stage_recorder=stage_recorder)


def test_ftr_upload_held_on_schema_less_technical_source(db):
    _seal()
    source = "guard_schemaless"
    # Seed an EXISTING schema-less technical source: column nodes whose schema_name is NULL (exactly
    # what a plain technical CSV upload leaves behind — build_graph writes schema_name only for a
    # schema-carrying glossary). Columns are DISJOINT from the FTR fixture's, so the column-level
    # cross-schema fence would NOT fire — proving it is THIS guard, not the fence, that holds it.
    build_graph(db, source, [
        CanonicalRow(source, "legacy_ledger", "legacy_col", "integer"),
        CanonicalRow(source, "legacy_ledger", "amount", "numeric"),
    ])
    assert _source_is_schema_less(db, source) is True

    res = _ingest_ftr(db, source)
    assert res.status == "held"
    assert "FTR-only source" in (res.reason or "")        # the actionable, up-front message
    assert "schema conflict" not in (res.reason or "")    # the guard, not the column-level fence
    assert source in (res.reason or "")                   # names the offending source


def test_ftr_upload_proceeds_on_brand_new_source(db):
    _seal()
    source = "guard_brandnew"
    assert _source_is_schema_less(db, source) is False    # zero existing nodes -> not schema-less
    res = _ingest_ftr(db, source)
    assert res.status == "ingested"                       # not held for the source-kind reason


def test_ftr_upload_proceeds_on_existing_ftr_source(db):
    _seal()
    source = "guard_ftr"
    assert _ingest_ftr(db, source).status == "ingested"   # first FTR upload SETS schema_name
    assert _source_is_schema_less(db, source) is False    # schema_name present -> not schema-less
    res = _ingest_ftr(db, source)                         # re-upload of the SAME FTR file
    assert res.status == "ingested"                       # proceeds; the guard does not fire


def test_mf6_hold_records_run_manifest_and_does_not_wipe_queue(db):
    """MF-6 fix: the source-kind hold must record THIS run's manifest (a ``skipped_upload_held``
    skipped-downstream account, like every other terminal held path) — an incomplete manifest was the
    finding. And it must NOT run ``persist_quarantine``: the whole-source review-queue refresh would
    wipe the existing schema-less source's queue that the guard is refusing to modify."""
    _seal()
    source = "guard_manifest"
    build_graph(db, source, [
        CanonicalRow(source, "legacy_ledger", "legacy_col", "integer"),
        CanonicalRow(source, "legacy_ledger", "amount", "numeric"),
    ])
    # Seed a pre-existing quarantine row on the target source (a reviewer's pending item). If the
    # MF-6 path called persist_quarantine, its delete-then-insert refresh would wipe this.
    persist_quarantine(db, source, [
        RowError(row_index=0, message="pre-existing pending review item",
                 row=CanonicalRow(source, "legacy_ledger", "legacy_col", "integer"))])
    assert len(list_quarantine(db, source)) == 1

    rec = StageRecorder()
    res = _ingest_ftr(db, source, stage_recorder=rec)
    assert res.status == "held"

    # (1) the run manifest is complete: the brake stage is a `held` deferral and every downstream
    # stage is a `skipped_upload_held` not_run record — not a truncated/missing account.
    brake = next(r for r in rec.reports if r.stage == "brake")
    assert brake.state == "deferred" and brake.reason_code == "held"
    held_skips = [r for r in rec.reports if r.reason_code == "skipped_upload_held"]
    assert held_skips, "MF-6 hold recorded no skipped-downstream stages"
    assert all(r.state == "not_run" for r in held_skips)

    # (2) the target source's review queue is UNTOUCHED — no persist_quarantine side effect.
    remaining = list_quarantine(db, source)
    assert len(remaining) == 1
    assert remaining[0].reason == "pre-existing pending review item"
