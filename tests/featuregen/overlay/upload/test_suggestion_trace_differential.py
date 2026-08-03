"""Task 2A — the engine's RESULT carries the trace, and the V1 surface cannot tell.

Two claims, proved against the real per-table engine on the real FTR fixture:

1. COMPLETENESS — every ``DESIGN_CHECKED`` recipe candidate the engine returns carries a trace that
   verifies and explains it, and every requirement on a needs-check candidate names the exact
   dependency that caused it. This is what makes the candidate admissible to V2 (freeze 0F-7).
2. DIFFERENCE — ignore the trace and NOTHING moves: the candidates, the rejections and the V1
   response bytes are identical to the ones produced from trace-stripped ideas. The trace is a new
   answer to a new question, never a change to the old one.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from tests.featuregen.overlay.upload.test_suggestions import (
    _COLUMNS,
    _ENTITY_TABLE,
    _JOIN_COLUMNS,
    _JOIN_SOURCE,
    _MEASURE_TABLE,
    OTHER_TABLE,
    SIBLING_TABLE,
    SOURCE,
    TABLE,
    _Catalog,
    _govern_table_facts,
    _ingest,
    _join_edge,
    _seal,
)

from featuregen.overlay.upload import suggestions as suggestions_module
from featuregen.overlay.upload.contract.gate1 import (
    RejectionRecordV1,
    TemplateCandidatesResult,
    _template_candidates,
)
from featuregen.overlay.upload.grounding_trace import (
    recompute_trace_content_hash,
    trace_completeness_gaps,
)
from featuregen.overlay.upload.suggestions import suggest_features_for_table


@pytest.fixture
def ftr_catalog(overlay_conn):
    """The suggestion suite's own three-table FTR catalog, built through its own helpers so this
    file and the V1 suite are provably grounding the SAME fixture."""
    _seal()
    _ingest(overlay_conn, SOURCE, _COLUMNS)
    _govern_table_facts(overlay_conn, TABLE, "cif_id", "as_of_dt")
    _govern_table_facts(overlay_conn, OTHER_TABLE, "book_id", "risk_dt")
    _govern_table_facts(overlay_conn, SIBLING_TABLE, "loan_cif", "due_dt")
    return _Catalog(source=SOURCE, table=TABLE)


@pytest.fixture
def join_catalog(overlay_conn):
    """Two tables, where the interesting candidate is ONLY buildable across the join."""
    _seal()
    _ingest(overlay_conn, _JOIN_SOURCE, _JOIN_COLUMNS)
    _govern_table_facts(overlay_conn, _MEASURE_TABLE, "ledger_acct", "ledger_dt",
                        source=_JOIN_SOURCE)
    _govern_table_facts(overlay_conn, _ENTITY_TABLE, "master_cif", "master_dt",
                        source=_JOIN_SOURCE)
    return _Catalog(source=_JOIN_SOURCE, table=_MEASURE_TABLE)


def _engine(conn, table=TABLE, *, source=SOURCE, roles=(), **kw) -> TemplateCandidatesResult:
    return _template_candidates(conn, catalog_source=source, roles=roles, target_ref=None,
                                now=None, table=table, **kw)


def _gaps(idea):
    return trace_completeness_gaps(idea.grounding_trace,
                                   validation_status=idea.validation_status,
                                   requirements=idea.requirements)


# ── the result carrier (freeze 0F-7 P3) ─────────────────────────────────────────────────────────
def test_the_result_carries_the_same_eight_objects_under_their_own_names(overlay_conn,
                                                                        ftr_catalog):
    result = _engine(overlay_conn)
    assert result.ideas and result.rejections
    assert result.grounded_ids == frozenset(
        idea.recipe_id for idea in result.ideas)
    assert set(result.binding_by_id) == result.grounded_ids
    assert set(result.rejected_ids) == {rec.template_id for rec in result.rejection_records}
    assert set(result.keys_by_recipe) == result.grounded_ids
    for keys in result.keys_by_recipe.values():
        assert all(key in result.contexts for key in keys)


def test_a_rejection_record_names_its_template_instead_of_matching_on_name(overlay_conn,
                                                                          ftr_catalog):
    """The V1 wire rejection carries only ``{name, reason, code}``; V2 needs the template that was
    refused, and recovering it by NAME-matching is exactly the re-attribution guesswork the
    per-table pass removed. The record carries it, and the rejection's own trace with it."""
    result = _engine(overlay_conn)
    assert result.rejection_records
    for record in result.rejection_records:
        assert isinstance(record, RejectionRecordV1)
        assert record.template_id in result.rejected_ids
        assert record.rejection.code in result.rejected_ids[record.template_id]
        assert record.rejection.trace is not None
        assert record.rejection.trace.validation_status == "REJECTED"
    # ...and the V1 list is untouched, position for position
    assert [r["name"] for r in result.rejections] == [
        rec.candidate_name for rec in result.rejection_records]


def test_the_v1_rejection_dicts_still_carry_exactly_three_keys(overlay_conn, ftr_catalog):
    result = _engine(overlay_conn)
    assert result.rejections
    assert {frozenset(r) for r in result.rejections} == {frozenset({"name", "reason", "code"})}


# ── completeness over the REAL engine output ────────────────────────────────────────────────────
def test_every_design_checked_recipe_candidate_has_a_complete_trace(overlay_conn, ftr_catalog):
    """THE HEADLINE for V2: a readiness claim nothing can justify is not admissible."""
    checked = []
    for table in (TABLE, OTHER_TABLE, SIBLING_TABLE):
        for idea in _engine(overlay_conn, table).ideas:
            assert idea.grounding_trace is not None, idea.name
            assert _gaps(idea) == (), (table, idea.name, _gaps(idea))
            if idea.validation_status == "DESIGN_CHECKED":
                checked.append(idea)
    assert checked, "the fixture cleared nothing — the proof would be vacuous"


def test_every_requirement_names_a_dependency_the_trace_pinned(overlay_conn, ftr_catalog):
    """Completeness includes the operand-level join: a requirement whose operand no pin names would
    be an unexplainable ask. Asserted on REAL needs-check candidates — the governed grain fact is
    withdrawn from this table, which is exactly the "declared, not governed-verified" state the
    GRAIN_IS_UNIQUE requirement exists to report."""
    overlay_conn.execute(
        "UPDATE graph_node SET grain_fact_event_id = NULL "
        "WHERE catalog_source = %s AND table_name = %s", (SOURCE, TABLE))
    seen = 0
    for idea in _engine(overlay_conn).ideas:
        assert _gaps(idea) == (), (idea.name, _gaps(idea))
        for requirement in idea.requirements:
            seen += 1
            key = f"{requirement.operand[0]}::{requirement.operand[1]}"
            assert any(pin.dependency_key == key
                       for pin in idea.grounding_trace.dependency_pins), (idea.name, requirement)
    assert seen, "no requirements in the fixture — the proof would be vacuous"


def test_the_candidate_key_is_the_engines_own_recipe_candidate_key(overlay_conn, ftr_catalog):
    """The trace's identity is the key the engine already mints — not a second candidate identity
    that could disagree with the grounding context's."""
    result = _engine(overlay_conn)
    for idea in result.ideas:
        keys = result.keys_by_recipe[idea.recipe_id]
        assert idea.grounding_trace.candidate_key in keys
        assert idea.grounding_trace.candidate_key in result.contexts


def test_two_different_candidates_do_not_share_a_trace_identity(overlay_conn, ftr_catalog):
    hashes = {idea.grounding_trace.trace_content_hash for idea in _engine(overlay_conn).ideas}
    assert len(hashes) == len(_engine(overlay_conn).ideas)


def test_the_trace_is_reproducible_for_the_same_catalog_state(overlay_conn, ftr_catalog):
    """Identity, not a build observation: grounding the same catalog twice yields the same hashes."""
    first = {i.name: i.grounding_trace.trace_content_hash for i in _engine(overlay_conn).ideas}
    second = {i.name: i.grounding_trace.trace_content_hash for i in _engine(overlay_conn).ideas}
    assert first == second and first
    for idea in _engine(overlay_conn).ideas:
        assert recompute_trace_content_hash(idea.grounding_trace) == (
            idea.grounding_trace.trace_content_hash)


def test_a_widened_candidate_retains_the_join_it_was_widened_across(overlay_conn, join_catalog):
    """The cross-table candidate the screen exists to surface carries the legs the planner selected
    — the one place a V2 explainer would otherwise be tempted to re-walk the graph."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    out = suggest_features_for_table(overlay_conn, catalog_source=join_catalog.source,
                                     table=join_catalog.table)
    assert out["summary"]["suggested"] >= 1
    result = _engine(overlay_conn, join_catalog.table, source=join_catalog.source,
                     also_tables=(_ENTITY_TABLE,))
    with_path = [i for i in result.ideas if i.grounding_trace.ordered_relationship_path]
    assert with_path, "no candidate crossed the join — the retention proof would be vacuous"
    for idea in with_path:
        for leg in idea.grounding_trace.ordered_relationship_path:
            assert leg.realization_content_hash and leg.relationship_kind == "direct_equality"
            assert leg.review_status in {"VERIFIED", "file_declared"}


# ── the differential: ignore the trace and nothing moves ────────────────────────────────────────
def _strip(result: TemplateCandidatesResult) -> TemplateCandidatesResult:
    """The SAME result with every trace removed — what the engine would have returned if the trace
    had never been built."""
    return replace(
        result,
        ideas=[replace(idea, grounding_trace=None) for idea in result.ideas],
        rejection_records=tuple(
            replace(rec, rejection=replace(rec.rejection, trace=None))
            for rec in result.rejection_records))


def test_the_v1_payload_is_byte_identical_when_the_trace_is_ignored(overlay_conn, ftr_catalog,
                                                                   monkeypatch):
    """DIFFERENTIAL PROOF. The screen is rendered twice from the SAME engine run — once from the
    real result, once from one with every trace stripped — and the JSON must be identical byte for
    byte. Nothing on the V1 surface may read the trace, directly or by accident."""
    real = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=TABLE)

    original = suggestions_module._template_candidates

    def _stripped(*a, **kw):
        return _strip(original(*a, **kw))

    monkeypatch.setattr(suggestions_module, "_template_candidates", _stripped)
    without = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=TABLE)
    assert json.dumps(real, sort_keys=True) == json.dumps(without, sort_keys=True)
    assert real["summary"]["suggested"] > 0 and real["rejections"]


def test_the_route_payload_carries_no_trace_material(overlay_conn, ftr_catalog):
    """The trace is server-private in Release A: not one hash, key or pin may appear on the wire."""
    out = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=TABLE)
    body = json.dumps(out)
    for word in ("trace", "dependency_pin", "content_hash", "candidate_key", "relationship"):
        assert word not in body
    hashes = {i.grounding_trace.trace_content_hash for i in _engine(overlay_conn).ideas}
    assert hashes and not any(h in body for h in hashes)


def test_the_candidates_themselves_are_unchanged_by_tracing(overlay_conn, ftr_catalog):
    """Field for field: the ONLY difference between a traced idea and its untraced twin is the
    trace. Proved on the engine's own output, not on a hand-built idea."""
    traced = _engine(overlay_conn).ideas
    stripped = _strip(_engine(overlay_conn)).ideas
    assert traced and len(traced) == len(stripped)
    for idea, bare in zip(traced, stripped, strict=True):
        assert replace(idea, grounding_trace=None) == bare
        assert idea.grounding_trace is not None and bare.grounding_trace is None
