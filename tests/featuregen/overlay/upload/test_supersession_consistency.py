"""Supersession + projection consistency (semantic plan Task 6).

The plan's five claims, each pinned by a test that FAILS against the pre-Task-6 writer:

1. Re-deriving a DIFFERENT LLM value retires the prior result through the existing producer-scoped
   staleness; re-deriving the SAME value preserves the current evidence ID for every LLM field.
   ``_write_llm_field_evidence`` was documented "SUPERSEDE-AND-REWRITE, unconditionally … no
   unchanged-detection", so five of the six LLM fields minted a fresh evidence ID on every ingest —
   "same current value AND evidence ID" was unachievable without changing that writer.
2. Source-attested and human values survive an LLM rerun BYTE-FOR-BYTE.
3. A system correction never creates a human-rejection event.
4. After resolution, evidence / graph_node / search / asset detail / the semantic-context bundle all
   return the same current value and evidence ID for a changed field. Entity Map's un-scoped links
   are the DOCUMENTED exception (`entity_map.py:20-27`, review B-8) — asserted as documented, not
   "fixed": scoping them there would break the one-truth byte-identity that read model exists for.
5. Projection lag is DETECTED and REPORTED (a typed marker), never silently absorbed and never
   turned into a new refusal.

Plus the mutation gate: disable stale-value retirement and the consistency suite must DIE.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload import enrich as enrich_mod
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
    projection_lag_marker,
)
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.search import search
from featuregen.overlay.upload.semantic_context import bundle_from_store

_SOURCE = "deposits"
_ROW = CanonicalRow(_SOURCE, "accounts", "balance", "numeric")
_REF = normalize_ref(_SOURCE, None, "accounts", "balance")
_OBJECT_REF = "public.accounts.balance"
_ROLES = ("platform-admin",)
_NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _write(db, field_name: str, value: str, *, material: object = None) -> int:
    """One pass of the REAL LLM field-evidence writer for one column."""
    return enrich_mod._write_llm_field_evidence(
        db, field_name=field_name, items={_REF: value},
        ref_of=lambda key, _m=(material if material is not None else value): (key, key, _m),
        source_snapshot_id="snap-1")


def _active(db, field_name: str = "definition"):
    return read_active_field_evidence(db, _REF, field_name)


def _lifecycles(db, field_name: str = "definition") -> set[tuple[str, str]]:
    """``{(value, lifecycle)}`` for the ref's whole history. A SET, not a list: rows written in one
    transaction share `created_at`, so any row order here would be the id minter's, not history's."""
    return {tuple(r) for r in db.execute(
        "SELECT proposed_value, lifecycle FROM field_evidence WHERE logical_ref=%s "
        "AND field_name=%s", (_REF, field_name)).fetchall()}


@pytest.fixture
def graphed(db):
    build_graph(db, _SOURCE, [_ROW])
    # Search joins the drift watermark and filters on freshness; `build_graph` alone never writes
    # one (only a real ingest does), so seed it — otherwise search is trivially empty and the
    # cross-surface assertions below would pass vacuously.
    db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, "
               "last_run_id) VALUES (%s, %s, 'test-run') ON CONFLICT (catalog_source) "
               "DO UPDATE SET last_completed_at = EXCLUDED.last_completed_at", (_SOURCE, _NOW))
    return db


# ── 1. value-diff supersession ──────────────────────────────────────────────────────────────────


def test_the_same_value_rederived_keeps_the_same_evidence_id(graphed):
    """The Task-6 headline. Three identical reruns, one evidence row, one id — the writer no longer
    manufactures a supersession that represents no change."""
    assert _write(graphed, "definition", "The ledger balance.") == 0
    first = _active(graphed)
    assert len(first) == 1
    for _ in range(2):
        assert _write(graphed, "definition", "The ledger balance.") == 0
        current = _active(graphed)
        assert len(current) == 1
        assert current[0].evidence_id == first[0].evidence_id
        assert current[0].proposed_value == "The ledger balance."
    assert _lifecycles(graphed) == {("The ledger balance.", "active")}


def test_a_same_value_rederivation_from_different_input_material_still_reuses(graphed):
    """VALUE-diff, not input-diff: a redraft that reached the same answer from a different prompt
    is the same assertion, so it keeps the same id (and its original input hash — identity belongs
    to the assertion, not the prompt that reproduced it)."""
    _write(graphed, "definition", "The ledger balance.", material={"prompt": "v1"})
    first = _active(graphed)[0]
    _write(graphed, "definition", "The ledger balance.", material={"prompt": "v2-reworded"})
    again = _active(graphed)
    assert len(again) == 1
    assert again[0].evidence_id == first.evidence_id
    assert again[0].input_hash == first.input_hash


def test_a_different_value_stales_the_prior_row_and_writes_a_fresh_one(graphed):
    _write(graphed, "definition", "The ledger balance.")
    first = _active(graphed)[0]
    _write(graphed, "definition", "End-of-day ledger balance.")
    current = _active(graphed)
    assert len(current) == 1                       # never two live LLM proposals for one field
    assert current[0].evidence_id != first.evidence_id
    assert current[0].proposed_value == "End-of-day ledger balance."
    assert _lifecycles(graphed) == {("The ledger balance.", "stale"),
                                    ("End-of-day ledger balance.", "active")}


def test_a_value_that_returns_is_written_fresh_not_resurrected(graphed):
    """A → B → A. Reuse is over the ACTIVE set only, so the return does NOT recover the original
    row: A was staled by B, and the third pass writes a NEW row with a new id. Named for what it
    proves — the earlier name ("reuses the row it never deleted") claimed the opposite of the
    assertions underneath it. What must hold is the invariant: exactly one live LLM proposal, and
    it says A."""
    _write(graphed, "definition", "A")
    original = _active(graphed)[0]
    _write(graphed, "definition", "B")
    _write(graphed, "definition", "A")
    current = _active(graphed)
    assert len(current) == 1 and current[0].proposed_value == "A"
    assert current[0].evidence_id != original.evidence_id      # a fresh row, not a resurrection
    assert [lifecycle for _v, lifecycle in _lifecycles(graphed)].count("active") == 1
    assert ("A", "active") in _lifecycles(graphed)


def test_a_same_value_reuse_still_retires_the_llms_other_live_proposal(graphed):
    """MUTATION GATE (M3): the `stale_source_evidence` call inside the value-diff REUSE branch.

    Deleting it survived the whole suite, because every other reuse test starts from a store
    holding exactly ONE live LLM row — where "retire the others" is a no-op and its absence is
    invisible. The state it defends is the one the resolver NULLs a field on: two live LLM
    proposals for one field. Reached here by seeding a second active LLM row directly (a prior
    run's write that a crash left live, or any path that recorded beside the writer), then
    re-deriving the FIRST value so the reuse branch — not the supersede branch — is the only thing
    that can clean it up."""
    _write(graphed, "definition", "The ledger balance.")
    kept = _active(graphed)[0]
    record_field_evidence(
        graphed, logical_ref=_REF, field_name="definition",
        proposed_value="A second live opinion.", producer=EvidenceProducer.LLM,
        strength=AssertionStrength.PROPOSED, producer_ref="test", source_snapshot_id="snap-0",
        input_hash=field_input_hash(logical_ref=_REF, field_name="definition",
                                    material="a different prompt"))
    assert len([e for e in _active(graphed) if e.producer == EvidenceProducer.LLM.value]) == 2

    assert _write(graphed, "definition", "The ledger balance.") == 0   # the REUSE branch

    live = [e for e in _active(graphed) if e.producer == EvidenceProducer.LLM.value]
    assert [e.evidence_id for e in live] == [kept.evidence_id]     # reused, id preserved
    assert ("A second live opinion.", "stale") in _lifecycles(graphed)


@pytest.mark.parametrize("field_name", ["definition", "ai_summary", "domain", "semantic_terms",
                                        "unit", "currency"])
def test_value_diff_holds_for_every_llm_field(graphed, field_name):
    """All six fields the writer serves — the plan's "five of six" gap closed for all of them."""
    _write(graphed, field_name, "value-1")
    first = _active(graphed, field_name)[0]
    _write(graphed, field_name, "value-1")
    assert _active(graphed, field_name)[0].evidence_id == first.evidence_id
    _write(graphed, field_name, "value-2")
    assert _active(graphed, field_name)[0].evidence_id != first.evidence_id


# ── 2/3. what an LLM rerun may never touch ──────────────────────────────────────────────────────


def test_source_attested_and_human_values_survive_an_llm_rerun_byte_for_byte(graphed):
    for producer, strength, value in (
            (EvidenceProducer.SOURCE, AssertionStrength.ATTESTED, "The file said this."),
            (EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED, "A steward said this.")):
        record_field_evidence(
            graphed, logical_ref=_REF, field_name="definition", proposed_value=value,
            producer=producer, strength=strength, producer_ref="test",
            source_snapshot_id="snap-0",
            input_hash=field_input_hash(logical_ref=_REF, field_name="definition",
                                        material=value))
    before = [(e.evidence_id, e.producer, e.proposed_value, e.lifecycle)
              for e in _active(graphed)]
    _write(graphed, "definition", "The AI drafted this.")
    _write(graphed, "definition", "The AI redrafted this.")
    after = {(e.evidence_id, e.producer, e.proposed_value, e.lifecycle) for e in _active(graphed)}
    assert set(before) <= after                    # byte-for-byte, both reruns
    assert all(e.lifecycle == "active" for e in _active(graphed)
               if e.producer in (EvidenceProducer.SOURCE.value, EvidenceProducer.HUMAN.value))


def test_a_system_correction_never_mints_a_human_rejection_event(graphed):
    """A correction is a re-derivation of the machine's own proposal. Recording it as a human
    REJECTION would fabricate a decision nobody took — and would retire the field for feature
    eligibility on the strength of a robot's second thought."""
    _write(graphed, "definition", "first")
    _write(graphed, "definition", "second")
    decisions = read_field_decisions(graphed, _REF, "definition")
    assert [d for d in decisions if d.event_type == "rejected"] == []
    assert [d for d in decisions if d.actor_ref] == []


# ── 4. one current value + one evidence id, everywhere ──────────────────────────────────────────


def _current_definition_evidence(db):
    active = [e for e in _active(db) if e.producer == EvidenceProducer.LLM.value]
    assert len(active) == 1
    return active[0]


def _surfaces(db):
    """Every consumer's answer for the anchor's definition: (value, evidence_id)."""
    evidence = _current_definition_evidence(db)
    node = db.execute(
        "SELECT definition, search_doc::text FROM graph_node WHERE catalog_source=%s "
        "AND object_ref=%s", (_SOURCE, _OBJECT_REF)).fetchone()
    detail = build_asset_detail(db, source=_SOURCE, object_ref=_OBJECT_REF, roles=_ROLES)
    detail_active = detail["evidence"]["proposals_by_field"]["definition"]["active"]
    hits = search(db, "", now=_NOW, roles=_ROLES).hits
    hit = next(h for h in hits if h.object_ref == _OBJECT_REF)
    bundle = bundle_from_store(db, _SOURCE, _OBJECT_REF, roles=_ROLES)
    bundle_value = next(v for v in bundle.resolved_semantics if v.field_name == "definition")
    return {
        "evidence": (evidence.proposed_value, evidence.evidence_id),
        "graph_node": (node[0], None),
        "search_hit": (hit.definition, None),
        "search_doc_contains": (node[1], None),
        "asset_detail": (detail["effective_metadata"]["fields"]["definition"]["value"],
                         detail_active[0]["evidence_id"]),
        "bundle": (bundle_value.value, bundle_value.evidence[0].evidence_id),
    }


def test_every_surface_returns_the_same_current_value_and_evidence_id_after_a_change(graphed):
    _write(graphed, "definition", "The ledger balance.")
    resolve_and_project(graphed, source=_SOURCE, logical_refs=[_REF])
    _write(graphed, "definition", "End-of-day ledger balance.")
    resolve_and_project(graphed, source=_SOURCE, logical_refs=[_REF])

    surfaces = _surfaces(graphed)
    expected_value, expected_id = surfaces["evidence"]
    assert expected_value == "End-of-day ledger balance."
    for name in ("graph_node", "search_hit", "asset_detail", "bundle"):
        assert surfaces[name][0] == expected_value, name
    for name in ("asset_detail", "bundle"):
        assert surfaces[name][1] == expected_id, name
    # The search DOCUMENT was re-derived too: the decoy text is gone, not merely outranked.
    assert "end-of-day" in surfaces["search_doc_contains"][0].lower()
    assert "the ledger balance" not in surfaces["search_doc_contains"][0].lower()


def test_an_unchanged_rerun_leaves_every_surface_identical(graphed):
    _write(graphed, "definition", "The ledger balance.")
    resolve_and_project(graphed, source=_SOURCE, logical_refs=[_REF])
    before = _surfaces(graphed)
    _write(graphed, "definition", "The ledger balance.")
    resolve_and_project(graphed, source=_SOURCE, logical_refs=[_REF])
    assert _surfaces(graphed) == before


def test_entity_map_links_stay_deliberately_un_scoped_the_documented_exception(graphed):
    """Review B-8 / D11: the Entity Map's LINKS are the un-scoped availability truth every other
    consumer of `available_identifier_links` already serves; its COUNTS and sample refs are
    read-scoped. Task 6's "same value everywhere" names this as an exception rather than changing
    it — scoping the links here would fork the one-truth byte-identity the map exists to keep.
    Asserted as DOCUMENTED so a future "fix" trips a test instead of shipping."""
    import inspect

    from featuregen.overlay.upload import entity_map

    doc = inspect.getdoc(entity_map) or ""
    assert "un-scoped" in doc or "unscoped" in doc
    assert "read-scoped" in doc          # counts/sample refs ARE scoped — the split is the design
    # The map takes roles for the SCOPED half; the link half does not consult them.
    assert "roles" in inspect.signature(entity_map.build_entity_map).parameters
    assert "roles" not in inspect.signature(entity_map._link_view).parameters


# ── 5. projection lag: detected, reported, never a new refusal ──────────────────────────────────


def test_a_ready_projection_reports_ready_out_loud(graphed):
    marker = projection_lag_marker(graphed)
    assert marker.status == "ready"
    detail = build_asset_detail(graphed, source=_SOURCE, object_ref=_OBJECT_REF, roles=_ROLES)
    assert detail["projection"]["status"] == "ready"
    assert search(graphed, "", now=_NOW, roles=_ROLES).projection.status == "ready"


def test_the_readiness_gate_is_one_statement(graphed, monkeypatch):
    """The gate ran four queries (head, checkpoint-exists, degraded, checkpoint-seq). That is fine
    once per feature-generation snapshot and wrong on a read surface that calls the detection-only
    sibling on every request — so the three predicates are read in ONE statement. Counted at the
    driver, because "one round trip" is a claim about statements, not about how the code reads."""
    import psycopg

    statements: list[object] = []
    execute = psycopg.Cursor.execute

    def _counting(self, query, *args, **kwargs):
        statements.append(query)
        return execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", _counting)
    assert projection_lag_marker(graphed).status == "ready"
    assert len(statements) == 1


def test_an_empty_search_page_does_not_pay_for_a_lag_marker(graphed, monkeypatch):
    """The marker qualifies the rows SERVED ("these may not yet reflect the newest resolved
    semantics"). A page that serves none has nothing to qualify, so the gate is not run at all —
    and a page that DOES serve rows always carries a freshly computed marker."""
    from featuregen.overlay.upload import search as search_mod

    checks: list[object] = []
    real = search_mod.projection_lag_marker
    monkeypatch.setattr(
        search_mod, "projection_lag_marker",
        lambda conn, **kw: (checks.append(conn), real(conn, **kw))[1])

    served = search(graphed, "", now=_NOW, roles=_ROLES)
    assert served.hits and len(checks) == 1          # exactly one readiness check per call
    empty = search(graphed, "no_such_term_anywhere", now=_NOW, roles=_ROLES)
    assert empty.hits == [] and empty.total == 0
    assert len(checks) == 1                          # no rows served -> no check at all
    assert empty.projection.status == "ready"


def _lag(monkeypatch):
    """Force the shared readiness gate to report lag — the same seam the feature path aborts on."""
    def _raise(_conn, **_kw):
        raise CatalogProjectionUnavailable(
            CATALOG_PROJECTION_UNAVAILABLE,
            "load-bearing projection 'overlay' is LAGGED: checkpoint 3 < event head 9")

    import featuregen.overlay.upload.feature_metadata_snapshot as fms

    monkeypatch.setattr(fms, "check_projection_readiness", _raise)


def test_asset_detail_reports_lag_and_still_serves(graphed, monkeypatch):
    _write(graphed, "definition", "The ledger balance.")
    resolve_and_project(graphed, source=_SOURCE, logical_refs=[_REF])
    _lag(monkeypatch)
    detail = build_asset_detail(graphed, source=_SOURCE, object_ref=_OBJECT_REF, roles=_ROLES)
    assert detail is not None                                  # detection, NOT a refusal
    assert detail["projection"]["status"] == "lagged"
    assert detail["projection"]["code"] == CATALOG_PROJECTION_UNAVAILABLE
    assert "LAGGED" in detail["projection"]["detail"]
    assert detail["identity"]["object_ref"] == _OBJECT_REF


def test_a_lagged_read_is_not_token_identical_to_a_ready_one(graphed, monkeypatch):
    """The silent version is the defect: serving a newer value under an older context hash as
    though the two agreed. The marker rides INSIDE the fingerprinted body, so a lagged snapshot
    cannot masquerade as a ready one to any client caching on the token."""
    ready = build_asset_detail(graphed, source=_SOURCE, object_ref=_OBJECT_REF, roles=_ROLES)
    _lag(monkeypatch)
    lagged = build_asset_detail(graphed, source=_SOURCE, object_ref=_OBJECT_REF, roles=_ROLES)
    assert lagged["consistency_token"] != ready["consistency_token"]


def test_search_reports_lag_and_still_returns_hits(graphed, monkeypatch):
    _lag(monkeypatch)
    result = search(graphed, "", now=_NOW, roles=_ROLES)
    assert result.projection.status == "lagged"
    assert result.projection.code == CATALOG_PROJECTION_UNAVAILABLE
    assert any(h.object_ref == _OBJECT_REF for h in result.hits)   # served, not refused


# ── the mutation gate ───────────────────────────────────────────────────────────────────────────


def test_mutation_disabling_stale_value_retirement_kills_the_consistency_invariant(
        graphed, monkeypatch):
    """MUTATION: neuter producer-scoped retirement, then re-run the consistency check.

    With retirement disabled the superseded LLM proposal stays ACTIVE beside the new one, so the
    "exactly one current value + evidence id" invariant fails. This test PASSES only because the
    invariant DIES — if it survived a disabled retirement, the consistency suite above would be
    proving nothing."""
    _write(graphed, "definition", "The ledger balance.")
    resolve_and_project(graphed, source=_SOURCE, logical_refs=[_REF])
    monkeypatch.setattr(enrich_mod, "stale_all_llm_field_evidence",
                        lambda *_a, **_kw: 0)
    monkeypatch.setattr(enrich_mod, "stale_source_evidence", lambda *_a, **_kw: 0)
    _write(graphed, "definition", "End-of-day ledger balance.")
    resolve_and_project(graphed, source=_SOURCE, logical_refs=[_REF])

    live = [e for e in _active(graphed) if e.producer == EvidenceProducer.LLM.value]
    assert len(live) == 2, "the mutation did not take — retirement is still running"
    with pytest.raises(AssertionError):
        _surfaces(graphed)          # the one-current-value invariant is gone
