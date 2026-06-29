from __future__ import annotations

import pytest

from featuregen.contracts import ProjectionApplyError, run_projection
from featuregen.contracts.documents import NewDocument
from featuregen.documents.primary import (
    StagePrimaryProjection,
    current_primary,
    new_primary_selected,
    register_primary_selected,
)
from featuregen.documents.store import append_document
from featuregen.events import append_event
from featuregen.ids import new_id


def _candidate(provenance):
    return NewDocument(
        doc_id=new_id("doc"),
        stage="CANDIDATE_SQL",
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:x",
        body_classification="governance-retained",
        provenance=provenance,
        body_ref="blob_1",
    )


def _emit_primary(db, *, run_id, doc_id, expected_version, actor, provenance):
    ev = new_primary_selected(
        run_id=run_id, stage="CANDIDATE_SQL", doc_id=doc_id,
        actor=actor, provenance=provenance,
    )
    return append_event(db, ev, expected_version=expected_version, table_version=1)


def test_current_primary_is_the_latest_by_global_seq(db, actor, provenance):
    register_primary_selected(db)
    a = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    b = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    e1 = _emit_primary(db, run_id="run_1", doc_id=a, expected_version=0,
                       actor=actor, provenance=provenance)
    e2 = _emit_primary(db, run_id="run_1", doc_id=b, expected_version=1,
                       actor=actor, provenance=provenance)

    proj = StagePrimaryProjection()
    proj.apply(db, e1)
    proj.apply(db, e2)

    assert current_primary(db, "run_1", "CANDIDATE_SQL") == b
    count = db.execute(
        "SELECT count(*) FROM stage_primary WHERE run_id='run_1' AND stage='CANDIDATE_SQL'"
    ).fetchone()[0]
    assert count == 1  # one live primary per (run_id, stage)


def test_out_of_order_lower_seq_does_not_override(db, actor, provenance):
    register_primary_selected(db)
    a = append_document(db, _candidate(provenance), run_id="run_2", actor=actor)
    b = append_document(db, _candidate(provenance), run_id="run_2", actor=actor)
    e1 = _emit_primary(db, run_id="run_2", doc_id=a, expected_version=0,
                       actor=actor, provenance=provenance)
    e2 = _emit_primary(db, run_id="run_2", doc_id=b, expected_version=1,
                       actor=actor, provenance=provenance)

    proj = StagePrimaryProjection()
    proj.apply(db, e2)   # higher seq first
    proj.apply(db, e1)   # lower seq must not win
    assert current_primary(db, "run_2", "CANDIDATE_SQL") == b


def test_projection_is_fail_closed_on_unknown_doc(db, actor, provenance):
    register_primary_selected(db)
    ev = _emit_primary(db, run_id="run_3", doc_id="doc_ghost", expected_version=0,
                       actor=actor, provenance=provenance)
    proj = StagePrimaryProjection()
    assert proj.is_analytics is False
    with pytest.raises(ProjectionApplyError):
        proj.apply(db, ev)


def test_current_primary_none_when_unselected(db):
    assert current_primary(db, "run_none", "CANDIDATE_SQL") is None


def test_run_projection_applies_in_global_seq_order(db, actor, provenance):
    # End-to-end: StagePrimaryProjection is consumable by the Phase 01 runner,
    # which feeds events in global_seq order off the projection's checkpoint row
    # (created by register_primary_selected — see step 3).
    register_primary_selected(db)
    a = append_document(db, _candidate(provenance), run_id="run_4", actor=actor)
    b = append_document(db, _candidate(provenance), run_id="run_4", actor=actor)
    _emit_primary(db, run_id="run_4", doc_id=a, expected_version=0,
                  actor=actor, provenance=provenance)
    _emit_primary(db, run_id="run_4", doc_id=b, expected_version=1,
                  actor=actor, provenance=provenance)

    applied = run_projection(db, StagePrimaryProjection())
    assert applied >= 2
    assert current_primary(db, "run_4", "CANDIDATE_SQL") == b
