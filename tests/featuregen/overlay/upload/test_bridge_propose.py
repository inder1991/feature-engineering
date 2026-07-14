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
