"""Wiring the cross-catalog entity bridge into ingest.

Everything needed to link a customer in one catalog to the same customer in another already existed
— `derive_bridge_candidates` finds the pairs, `propose_bridge` puts them on the governed fact spine,
`entity_bridge_candidate_evidence` is the durable ledger, and `entity_bridge_edge` is the VERIFIED
projection the planner reads. Nothing called any of it. Against two real catalogs the derivation
returns 9 candidates, and all 9 evaporated when the process exited.

This stage closes that gap: after evidence resolution has populated `graph_node.concept` (the
derivation's only input), derive and propose. It is a PROPOSAL stage — it never confirms, so the
invariant that only a VERIFIED fact is operational is untouched.

The stage is ADVISORY: a failure here must never fail an upload that has already stored its facts
and graph.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import entity_bridges_enabled, ingest_upload

_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="owner", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _rows(source: str, table: str, id_col: str) -> list[CanonicalRow]:
    return [CanonicalRow(source=source, table=table, column=id_col, type="text"),
            CanonicalRow(source=source, table=table, column="amount", type="numeric")]


def _seed_two_catalogs(db) -> None:
    """Two catalogs whose identifier columns carry the SAME entity concept — the shape that must
    bridge. Concepts are set directly: this test is about the WIRING, not about enrichment."""
    for source, table, id_col in (("cat_a", "customer", "cust_num"),
                                  ("cat_b", "txn", "cif_id")):
        ingest_upload(db, source, _rows(source, table, id_col), actor=_actor(), now=_NOW)
        db.execute(
            "UPDATE graph_node SET concept = 'customer_id', declared_type = 'varchar' "
            "WHERE catalog_source = %s AND kind = 'column' AND column_name = %s",
            (source, id_col))


def _trigger(db):
    """Upload an UNRELATED third catalog to drive the stage.

    `ingest_upload` wipes and rebuilds the uploading catalog's graph nodes, so re-uploading `cat_b`
    here would erase the concept this test just set and there would be no pair left to find. In
    production the concept survives that rebuild because `resolve_and_project` re-projects it from
    `field_evidence` after `build_graph` — machinery this test deliberately does not stand up.

    Triggering from a third catalog keeps the two seeded endpoints intact and exercises exactly what
    the wiring claims: the stage runs on EVERY upload and derives GLOBALLY, not scoped to the catalog
    being uploaded.
    """
    return ingest_upload(db, "cat_c", _rows("cat_c", "other", "some_id"), actor=_actor(), now=_NOW)


def _ledger(db) -> list[tuple]:
    return db.execute(
        "SELECT entity_id, left_catalog_source, left_object_ref, right_catalog_source, "
        "       right_object_ref FROM entity_bridge_candidate_evidence ORDER BY 1,2,3").fetchall()


# ── the flag ─────────────────────────────────────────────────────────────────────────────────────

def test_the_stage_is_off_by_default(monkeypatch):
    monkeypatch.delenv("OVERLAY_ENTITY_BRIDGES", raising=False)
    assert entity_bridges_enabled() is False


def test_flag_off_writes_no_candidate(db, monkeypatch):
    """Flag-off must be byte-identical to today: no ledger row, no fact."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "0")
    _seed_two_catalogs(db)
    assert _ledger(db) == []


# ── the wiring ───────────────────────────────────────────────────────────────────────────────────

def test_the_second_catalog_s_upload_persists_the_bridge(db, monkeypatch):
    """THE point of the change. Before this, the derivation produced 9 candidates that were never
    written anywhere; now uploading the second catalog leaves a durable ledger row."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    _trigger(db)
    ledger = _ledger(db)
    assert len(ledger) == 1, ledger
    entity, left_src, left_ref, right_src, right_ref = ledger[0]
    assert entity == "customer"
    assert {left_src, right_src} == {"cat_a", "cat_b"}
    assert "cust_num" in left_ref + right_ref and "cif_id" in left_ref + right_ref


def test_a_bridge_is_PROPOSED_never_verified(db, monkeypatch):
    """The governing invariant: this stage proposes. Only a human confirm makes a bridge
    operational, so nothing may land in the VERIFIED projection."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    _trigger(db)
    assert _ledger(db), "precondition: a candidate was proposed"
    assert db.execute("SELECT count(*) FROM entity_bridge_edge").fetchone()[0] == 0


def test_a_single_catalog_proposes_nothing(db, monkeypatch):
    """A bridge is cross-catalog by definition; one catalog alone must produce none."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    ingest_upload(db, "cat_a", _rows("cat_a", "customer", "cust_num"), actor=_actor(), now=_NOW)
    db.execute("UPDATE graph_node SET concept = 'customer_id' WHERE kind='column' "
               "AND column_name = 'cust_num'")
    ingest_upload(db, "cat_a", _rows("cat_a", "customer", "cust_num"), actor=_actor(), now=_NOW)
    assert _ledger(db) == []


def test_re_uploading_does_not_duplicate_the_candidate(db, monkeypatch):
    """The ledger is keyed on the pair, and the fact spine dedups on the deterministic fact_key, so
    a re-upload is a no-op rather than a second proposal."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    for _ in range(3):
        _trigger(db)
    assert len(_ledger(db)) == 1


# ── advisory: never fail the upload ──────────────────────────────────────────────────────────────

def test_a_failure_in_the_stage_does_not_fail_the_upload(db, monkeypatch):
    """The facts and graph are already stored by this point. A bridge fault must degrade to a
    warning, exactly as the sibling Pass C / governed-join seams do."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)

    def boom(*a, **k):
        raise RuntimeError("derivation exploded")

    monkeypatch.setattr("featuregen.overlay.upload.ingest.derive_bridge_candidates", boom)
    result = _trigger(db)
    assert result.status == "ingested"
    assert _ledger(db) == []


def test_the_count_is_reported_on_the_result(db, monkeypatch):
    """Observability: the count rides the IngestResult like every sibling stage's, so the run
    report can show it instead of it being discoverable only by querying the table."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    result = _trigger(db)
    assert result.entity_bridges_proposed == 1


@pytest.mark.parametrize("flag", ["0", None])
def test_flag_off_reports_zero(db, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("OVERLAY_ENTITY_BRIDGES", raising=False)
    else:
        monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", flag)
    _seed_two_catalogs(db)
    result = _trigger(db)
    assert result.entity_bridges_proposed == 0
