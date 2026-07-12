"""Whole-branch fix #2 — a file-DECLARED grain must survive a re-upload that drift-stales its fact.

Phase 2 added an UNCONDITIONAL end-of-ingest ``project_table_facts`` that clear-then-sets
is_grain/is_as_of on every column of a table. A technical CSV that DECLARES grain on a column,
re-uploaded with that column's SAFETY metadata changed (a drift-relevant sensitivity change, folded
into the safety fingerprint), drift-STALEs the grain fact -> ``resolve_fact`` serves None -> the
clear left is_grain FALSE, silently WIPING the grain ``build_graph`` had just set from the declared
row. Pre-Phase-2 ``build_graph``'s is_grain=true was final and survived. ``graph_node.is_grain`` is
load-bearing (templates.py grain-column selection, search ranking), so this violates flag-off
byte-for-byte.

The projection MUST NOT clear the flags the CURRENT upload declares.

NOTE ON SENSITIVITY: the only recognized sensitivity tags are "" (none), "pii" and "restricted"
(read_scope.SENSITIVITY_ROLES) — any other value QUARANTINES the row (canonical.validate_rows), which
would drop the grain column entirely and make this test vacuous. The drift is triggered by a valid
change ("" -> "pii"), which changes the column's safety fingerprint and drift-STALEs the grain fact.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload

_NOW = datetime(2026, 7, 12, tzinfo=UTC)


def _rows(sensitivity: str) -> list[CanonicalRow]:
    """A technical upload declaring is_grain on ``id`` (auto-confirmed VERIFIED by _assert_fact) plus
    an ``amt`` column. ``id``'s sensitivity is drift-relevant (folded into the safety fingerprint)."""
    return [CanonicalRow("src", "txn", "id", "integer", is_grain=True, sensitivity=sensitivity),
            CanonicalRow("src", "txn", "amt", "numeric")]


def _is_grain(conn, column: str) -> bool:
    row = conn.execute(
        "SELECT is_grain FROM graph_node WHERE catalog_source='src' AND table_name='txn' "
        "AND column_name=%s AND kind='column'", (column,)).fetchone()
    return bool(row and row[0])


def test_declared_grain_survives_drift_stale_reupload(overlay_conn, human_actor):
    # COMMON-PATH wipe (no sealed config): the drift-STALEd grain fact serves None regardless, so
    # the clear-then-set projection wipes the file-declared grain unless declared columns are spared.
    # 1. First upload declares grain on `id`; _assert_fact auto-confirms it VERIFIED and build_graph
    #    sets is_grain=true.
    r1 = ingest_upload(overlay_conn, "src", _rows(""), actor=human_actor, now=_NOW)
    assert r1.status == "ingested"
    assert _is_grain(overlay_conn, "id") is True   # first upload: declared grain lands

    # 2. Re-upload the SAME grain declaration but with `id`'s sensitivity changed ("" -> "pii") ->
    #    drift-STALEs the grain fact (the safety fingerprint includes sensitivity). The upload STILL
    #    declares grain on id.
    r2 = ingest_upload(overlay_conn, "src", _rows("pii"), actor=human_actor,
                       now=_NOW + timedelta(minutes=1))
    assert r2.status == "ingested"
    assert r2.staled == 1   # the grain fact drift-STALEd — resolve_fact now serves None

    # The file-declared grain must NOT be wiped by the clear-then-set projection.
    assert _is_grain(overlay_conn, "id") is True


def test_projection_lag_skips_end_of_ingest_projection(overlay_conn, human_actor, monkeypatch):
    # PROJECTION-LAG variant: under overlay projection lag the end-of-ingest project_table_facts must
    # be SKIPPED entirely (mirroring the drift path beside it) so build_graph's declared flags stand
    # rather than being cleared against a stale read model. Simulate lag and assert the projection is
    # not invoked AND the declared grain survives.
    r1 = ingest_upload(overlay_conn, "src", _rows(""), actor=human_actor, now=_NOW)
    assert r1.status == "ingested"

    import featuregen.overlay.upload.ingest as ingest_mod

    calls: list[str] = []
    real_project = ingest_mod.project_table_facts

    def _spy(*args, **kwargs):
        calls.append("called")
        return real_project(*args, **kwargs)

    monkeypatch.setattr(ingest_mod, "projection_lag",
                        lambda conn, name: 1 if name == "overlay" else 0)
    monkeypatch.setattr(ingest_mod, "project_table_facts", _spy)
    r2 = ingest_upload(overlay_conn, "src", _rows(""), actor=human_actor,
                       now=_NOW + timedelta(minutes=1))
    assert r2.status == "ingested"
    assert calls == [], "end-of-ingest projection must be skipped under projection lag"
    # build_graph's declared grain still stands (projection never cleared it).
    assert _is_grain(overlay_conn, "id") is True
    # Drain so we do not leave the projection lagging for other assertions.
    from featuregen.projections.runner import run_projection
    while run_projection(overlay_conn, OverlayProjection()) >= 500:
        pass
