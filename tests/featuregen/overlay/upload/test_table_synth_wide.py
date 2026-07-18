"""#1 — two-phase wide-table synthesis.

A table WIDER than ``_MAX_COLUMN_PROFILES`` (the OLD single-giant-item path egress-REJECTED it, so a
wide table produced no grain/availability/table-role/primary-entity proposal at all) is now handled in
two phases:

* Phase 1 (NO fact output): its column profiles are split into consecutive chunks of
  ``<=_MAX_COLUMN_PROFILES`` and each chunk is SUMMARIZED (candidate grain/id + temporal/as-of +
  entity signals + event/snapshot hint). Each chunk item is egress-safe (<=64 profiles).
* Phase 2 (single synthesis): ONE call per table over ALL its chunk summaries PLUS a compact COMPLETE
  column roster (names/types only). Emits the SAME table-fact result shape ``_propose_table_facts``
  already consumes, so downstream proposal/projection is unchanged.

A NARROW table keeps the today's single-call fast path (no phase-1 summary). A wide table that fails to
summarize every chunk, or whose synthesis is invalid, resolves to NOTHING — never a phantom "resolved".
"""
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.enrich_batch import BatchItem
from featuregen.overlay.upload.enrich_llm import _MAX_COLUMN_PROFILES, _item_egress_ok
from featuregen.overlay.upload.ingest import _enrichment_outcome
from featuregen.overlay.upload.table_synth import synthesize_tables


class _RecordingLLM(FakeLLM):
    """FakeLLM that captures every dispatched request so a test can inspect which task ran and the
    exact per-item metadata that reached the egress-guarded payload."""

    def __init__(self, script):
        super().__init__(script=script)
        self.requests = []

    def call(self, request):
        self.requests.append(request)
        return super().call(request)


def _profiles(n):
    return [{"column": f"c{i}", "operational_type": "integer", "declared_type": ""}
            for i in range(n)]


def _items_of(request):
    return request.inputs["catalog_metadata"]["items"]


def _summary(**kw):
    base = {"grain_candidates": [], "temporal_candidates": [], "entity_signals": [],
            "event_or_snapshot": None}
    base.update(kw)
    return base


# --- the bug + the fix -------------------------------------------------------------------------

def test_old_giant_item_would_egress_reject_but_a_chunk_is_admissible():
    """The bug: a 126-profile single item is egress-REJECTED (>64); a <=64 chunk is admissible."""
    profiles = _profiles(126)
    assert _item_egress_ok({"table": "ftr", "column_profiles": profiles}) is False
    assert _item_egress_ok(
        {"table": "ftr", "column_profiles": profiles[:_MAX_COLUMN_PROFILES]}) is True


def test_phase2_wide_item_is_egress_admissible():
    """The phase-2 item carries chunk summaries + a complete STRUCTURED roster — admissible even
    though the roster is longer than 64 (it is bounded identity entries, not full profiles)."""
    meta = {"table": "ftr",
            "chunk_summaries": [_summary(grain_candidates=["c0"], temporal_candidates=["c1"],
                                         entity_signals=["transaction"], event_or_snapshot="event")],
            "column_roster": [{"column": f"c{i}", "operational_type": "integer",
                               "declared_type": ""} for i in range(126)]}
    assert _item_egress_ok(meta) is True
    # a summary carrying a data-value / unknown key is rejected (egress-safe: bounded fields only)
    bad = {"table": "ftr", "chunk_summaries": [{"grain_candidates": ["c0"], "rows": ["secret"]}]}
    assert _item_egress_ok(bad) is False


def test_wide_table_two_phase_produces_proposal(db):
    """126-col table -> 2 chunk summaries (phase 1) + 1 synthesis (phase 2) -> a proposal (was: none)."""
    n = 126
    profiles = _profiles(n)
    cols = {f"c{i}" for i in range(n)}
    summary = _summary(grain_candidates=["c0"], temporal_candidates=["c1"],
                       entity_signals=["transaction"], event_or_snapshot="event")
    synthesis = {"grain_columns": ["c0"], "as_of_column": "c1", "as_of_basis": "posted_at",
                 "table_role": "fact", "primary_entity": "transaction", "event_or_snapshot": "event"}
    client = _RecordingLLM({
        "table_synth_summary": FakeResponse(output={"results": [
            {"ref": "ftr#chunk0", "summary": summary},
            {"ref": "ftr#chunk1", "summary": summary}]}),
        "table_synth": FakeResponse(output={"results": [{"ref": "ftr", "synthesis": synthesis}]}),
    })
    items = [BatchItem("ftr", {"table": "ftr", "column_profiles": profiles})]
    out = synthesize_tables(db, client, items, columns_by_table={"ftr": cols}, actor=None)

    # phase 2 produced the SINGLE table-level synthesis in the exact shape _propose_table_facts wants
    assert set(out) == {"ftr"}
    assert out["ftr"]["grain"] == {"columns": ["c0"], "is_unique": True}
    assert out["ftr"]["availability_time"] == {"column": "c1", "basis": "posted_at"}
    assert out["ftr"]["table_role"] == "fact"
    assert out["ftr"]["primary_entity"] == "transaction"
    assert out["ftr"]["event_or_snapshot"] == "event"

    # phase 1 summarized ceil(126/64)=2 consecutive chunks
    summary_reqs = [r for r in client.requests if r.task == "table_synth_summary"]
    chunk_refs = {it["ref"] for r in summary_reqs for it in _items_of(r)}
    assert chunk_refs == {"ftr#chunk0", "ftr#chunk1"}
    for r in summary_reqs:                                   # every chunk item is egress-shaped
        for it in _items_of(r):
            assert len(it["column_profiles"]) <= _MAX_COLUMN_PROFILES

    # phase 2 = exactly ONE synthesis call carrying summaries + roster, NOT the 126 full profiles
    synth_reqs = [r for r in client.requests if r.task == "table_synth"]
    assert len(synth_reqs) == 1
    synth_items = _items_of(synth_reqs[0])
    assert len(synth_items) == 1
    meta = synth_items[0]
    assert "column_profiles" not in meta                    # NOT the giant profile list
    assert len(meta["chunk_summaries"]) == 2                # both chunk summaries
    assert len(meta["column_roster"]) == n                  # the complete structured roster
    assert meta["column_roster"][0] == {"column": "c0", "operational_type": "integer",
                                        "declared_type": ""}


def test_narrow_table_keeps_the_fast_path(db):
    """A <=64-col table takes today's single synthesis call directly — no phase-1 summary call."""
    profiles = _profiles(3)
    synthesis = {"grain_columns": ["c0"], "as_of_column": None, "as_of_basis": None,
                 "table_role": "dim", "primary_entity": "thing", "event_or_snapshot": "snapshot"}
    client = _RecordingLLM({"table_synth": FakeResponse(output={"results": [
        {"ref": "narrow", "synthesis": synthesis}]})})
    items = [BatchItem("narrow", {"table": "narrow", "column_profiles": profiles})]
    out = synthesize_tables(db, client, items,
                            columns_by_table={"narrow": {"c0", "c1", "c2"}}, actor=None)
    assert out["narrow"]["grain"] == {"columns": ["c0"], "is_unique": True}
    assert not [r for r in client.requests if r.task == "table_synth_summary"]   # fast path


def test_wide_missing_chunk_summary_is_honest_no_phantom(db):
    """Phase-1 loses a chunk (chunk1 unreturned) -> the table never fully summarizes -> NO phantom
    resolved, and phase-2 synthesis is not even attempted for it. The stage reports failed."""
    n = 126
    profiles = _profiles(n)
    cols = {f"c{i}" for i in range(n)}
    client = _RecordingLLM({
        "table_synth_summary": FakeResponse(output={"results": [
            {"ref": "ftr#chunk0", "summary": _summary(grain_candidates=["c0"])}]}),  # chunk1 MISSING
        "table_synth": FakeResponse(output={"results": [
            {"ref": "ftr", "synthesis": {"grain_columns": ["c0"]}}]}),
    })
    items = [BatchItem("ftr", {"table": "ftr", "column_profiles": profiles})]
    out = synthesize_tables(db, client, items, columns_by_table={"ftr": cols}, actor=None)
    assert out == {}                                        # never a phantom "resolved"
    assert not [r for r in client.requests if r.task == "table_synth"]   # no synthesis attempted
    state, reason, _ = _enrichment_outcome(out, expected=1)
    assert state == "failed" and reason == "no_items_resolved"    # honest stage report


def test_wide_invalid_synthesis_is_honest_no_phantom(db):
    """All chunks summarize but phase-2 names a GHOST grain column -> make_ref_accept rejects ->
    the table resolves to NOTHING (no phantom), so the stage reports honestly."""
    n = 70                                                  # wide (>64) -> 2 chunks (64 + 6)
    profiles = _profiles(n)
    cols = {f"c{i}" for i in range(n)}
    summ = _summary(grain_candidates=["c0"])
    client = _RecordingLLM({
        "table_synth_summary": FakeResponse(output={"results": [
            {"ref": "ftr#chunk0", "summary": summ}, {"ref": "ftr#chunk1", "summary": summ}]}),
        "table_synth": FakeResponse(output={"results": [
            {"ref": "ftr", "synthesis": {"grain_columns": ["ghost"]}}]}),   # not a column of ftr
    })
    items = [BatchItem("ftr", {"table": "ftr", "column_profiles": profiles})]
    out = synthesize_tables(db, client, items, columns_by_table={"ftr": cols}, actor=None)
    assert out == {}
    state, reason, _ = _enrichment_outcome(out, expected=1)
    assert state == "failed" and reason == "no_items_resolved"


def test_mixed_narrow_and_wide_both_resolve(db):
    """A batch of one narrow + one wide table: narrow via the fast path, wide via two-phase — both
    reach _propose_table_facts' result shape."""
    wide = _profiles(80)                                    # >64 -> 2 chunks
    narrow = _profiles(2)
    summ = _summary(grain_candidates=["c0"])
    client = _RecordingLLM({
        "table_synth_summary": FakeResponse(output={"results": [
            {"ref": "wide#chunk0", "summary": summ}, {"ref": "wide#chunk1", "summary": summ}]}),
        "table_synth": FakeResponse(output={"results": [
            {"ref": "wide", "synthesis": {"grain_columns": ["c0"], "table_role": "fact"}},
            {"ref": "narrow", "synthesis": {"grain_columns": ["c0"], "table_role": "dim"}}]}),
    })
    items = [BatchItem("wide", {"table": "wide", "column_profiles": wide}),
             BatchItem("narrow", {"table": "narrow", "column_profiles": narrow})]
    out = synthesize_tables(db, client, items,
                            columns_by_table={"wide": {f"c{i}" for i in range(80)},
                                              "narrow": {"c0", "c1"}}, actor=None)
    assert set(out) == {"wide", "narrow"}
    assert out["wide"]["grain"] == {"columns": ["c0"], "is_unique": True}
    assert out["narrow"]["grain"] == {"columns": ["c0"], "is_unique": True}
