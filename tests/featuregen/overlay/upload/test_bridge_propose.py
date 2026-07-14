from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.overlay.upload.test_bridge_candidates import _two_catalog_customer

from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_T0 = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _propose(db) -> str:
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    cand = derive_bridge_candidates(db)[0]
    return propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)


def test_propose_opens_a_draft_bridge_fact(db):
    key = _propose(db)
    state = fold_overlay_state(load_fact(db, key))
    assert state.status == "DRAFT"


def test_propose_stamps_the_candidate_ledger(db):
    key = _propose(db)
    row = db.execute(
        "SELECT entity_id, fact_key, proposed_event_id, data_type_family "
        "FROM entity_bridge_candidate_evidence "
        "WHERE left_catalog_source = 'core' AND right_catalog_source = 'crm'").fetchone()
    assert row is not None
    assert row[0] == "customer" and row[1] == key and row[2] is not None and row[3] == "integer"


def test_propose_opens_one_governance_gate_task(db):
    _propose(db)
    # single-confirmer -> exactly one open human task (platform-admin governance), not two
    n = db.execute("SELECT count(*) FROM human_tasks WHERE status = 'open'").fetchone()[0]
    assert n == 1


def test_reproposing_same_candidate_is_idempotent(db):
    key1 = _propose(db)
    # a second propose of the SAME candidate must NOT raise, must return the same fact_key,
    # and must not open a second gate task or a second ledger row.
    cand = derive_bridge_candidates(db)[0]
    key2 = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)
    assert key2 == key1
    assert db.execute("SELECT count(*) FROM human_tasks WHERE status = 'open'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM entity_bridge_candidate_evidence").fetchone()[0] == 1
