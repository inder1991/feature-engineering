"""Task 2A — the gauntlet EMITS its decision trace, at the decision points (freeze 0F-7 P1).

The contract's own laws live in ``test_grounding_trace.py``. Here the real
``feature_assist._validate_idea`` runs against a real graph and must leave, at every read it
already performs, a pin that names exactly what it read — for survivors AND for rejections — while
changing no disposition whatsoever.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload import grounding_trace as gt
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import RejectCode, _validate_idea
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.grounding_trace import (
    SuggestionDependencyClass,
    recompute_trace_content_hash,
    trace_completeness_gaps,
)
from featuregen.overlay.upload.join_path import (
    classify_join_path,
    join_outcome_relationship_path,
)
from featuregen.overlay.upload.read_scope import read_scope_rule_content_hash

NOW = datetime(2026, 7, 18, tzinfo=UTC)
FRESH = timedelta(hours=24)
_KEY = "cand-key-1"
_TEMPLATE = "balance_trend"


def _fresh(db, source="bank"):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = %s", (source, NOW, NOW))


def _kv(refs, catalog="bank"):
    return set(refs), {r: {catalog} for r in refs}


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric"),
        CanonicalRow("bank", "accounts", "fees", "numeric"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
    ])
    _fresh(db)


def _validate(db, raw, refs, *, key=_KEY, roles=(), operand_roles=(), catalog="bank"):
    known, src_of = _kv(refs, catalog)
    return _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=roles,
                          operand_roles=operand_roles, candidate_key=key, template_id=_TEMPLATE)


def _kinds(trace) -> set[str]:
    return {pin.dependency_kind for pin in trace.dependency_pins}


def _pin(trace, kind: str, key: str | None = None):
    hits = [p for p in trace.dependency_pins
            if p.dependency_kind == kind and (key is None or p.dependency_key == key)]
    assert hits, f"no {kind} pin for {key!r}; kinds present: {sorted(_kinds(trace))}"
    return hits[0]


def _gaps(idea):
    return trace_completeness_gaps(idea.grounding_trace,
                                   validation_status=idea.validation_status,
                                   requirements=idea.requirements)


# ── the carrier (freeze 0F-7 P1: arity unchanged, the trace rides on the returned objects) ───────
def test_a_caller_that_threads_no_candidate_identity_gets_no_trace(db):
    """The LLM / planner paths pass nothing and are untouched — they have no recipe candidate key
    and V2 does not consume their candidates, so they pay neither the hashing nor a half-trace."""
    _bank(db)
    known, src_of = _kv(["public.accounts.balance"])
    idea, rej = _validate_idea(
        db, {"name": "avg_balance", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg"}, known, src_of, None, NOW, FRESH)
    assert rej is None and idea.grounding_trace is None


def test_threading_a_candidate_key_neither_moves_the_decision_nor_the_idea(db):
    """The trace is ADDITIVE: every other field of the returned idea is identical with and without
    it, so nothing downstream can read a different candidate because a trace was asked for."""
    _bank(db)
    raw = {"name": "avg_balance", "derives_from": ["public.accounts.balance"],
           "aggregation": "avg"}
    bare, _ = _validate_idea(db, raw, *_kv(["public.accounts.balance"]), None, NOW, FRESH)
    traced, _ = _validate(db, raw, ["public.accounts.balance"])
    assert traced.grounding_trace is not None
    assert replace(traced, grounding_trace=None) == bare


def test_the_trace_verifies_and_is_complete_for_a_design_checked_candidate(db):
    _bank(db)
    idea, rej = _validate(db, {"name": "avg_balance",
                               "derives_from": ["public.accounts.balance"],
                               "aggregation": "avg"}, ["public.accounts.balance"])
    assert rej is None and idea.validation_status == "DESIGN_CHECKED"
    trace = idea.grounding_trace
    assert trace.candidate_key == _KEY
    assert trace.validation_status == "DESIGN_CHECKED"
    assert recompute_trace_content_hash(trace) == trace.trace_content_hash
    assert _gaps(idea) == ()


# ── one pin per read, AT the read ───────────────────────────────────────────────────────────────
def test_the_read_scope_and_the_grounding_set_are_pinned_on_every_candidate(db):
    _bank(db)
    idea, _ = _validate(db, {"name": "avg_balance",
                             "derives_from": ["public.accounts.balance"],
                             "aggregation": "avg"}, ["public.accounts.balance"])
    trace = idea.grounding_trace
    scope = _pin(trace, gt.READ_SCOPE)
    assert scope.dependency_class is SuggestionDependencyClass.HARD_AVAILABILITY
    assert trace.read_scope_rule_content_hashes == (read_scope_rule_content_hash(),)
    grounding = _pin(trace, gt.GROUNDING_CANDIDATE_SET)
    assert grounding.dependency_class is SuggestionDependencyClass.HARD_AVAILABILITY
    assert _pin(trace, gt.COLUMN_EXISTENCE,
                gt.column_dependency_key("bank", "public.accounts.balance"))


def test_a_different_read_scope_is_a_different_trace(db):
    """Visibility is a DEPENDENCY, not a rendering detail: the same candidate seen under a wider
    scope was decided against a different catalog and must not share an identity with it."""
    _bank(db)
    raw = {"name": "avg_balance", "derives_from": ["public.accounts.balance"],
           "aggregation": "avg"}
    narrow, _ = _validate(db, raw, ["public.accounts.balance"], roles=())
    wide, _ = _validate(db, raw, ["public.accounts.balance"], roles=("pii_reader",))
    assert (narrow.grounding_trace.trace_content_hash
            != wide.grounding_trace.trace_content_hash)


def test_the_type_check_pins_the_governed_read_and_the_declared_hint_it_consulted(db):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, declared_type) VALUES ('bank', 'public.loans.balance', 'column', 'loans', "
        "'balance', 'unknown', 'numeric')")
    _fresh(db)
    idea, rej = _validate(db, {"name": "avg_bal", "derives_from": ["public.loans.balance"],
                               "aggregation": "avg"}, ["public.loans.balance"])
    assert rej is None
    key = gt.column_dependency_key("bank", "public.loans.balance")
    governed = _pin(idea.grounding_trace, gt.GOVERNED_LOGICAL_REPRESENTATION, key)
    assert governed.dependency_class is SuggestionDependencyClass.VALIDATION
    assert _pin(idea.grounding_trace, gt.DECLARED_TYPE_HINT, key)
    # the requirement it minted names the very operand that pin names
    assert [r.code for r in idea.requirements] == ["TYPE_IS_NUMERIC"]
    assert _gaps(idea) == ()


def test_the_additivity_check_pins_the_governed_read(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive")])
    _fresh(db)
    idea, rej = _validate(db, {"name": "sum_bal", "derives_from": ["public.accounts.balance"],
                               "aggregation": "sum"}, ["public.accounts.balance"])
    assert rej is None and any(r.code == "ADDITIVITY_SUPPORTS_OPERATION" for r in idea.requirements)
    assert _pin(idea.grounding_trace, gt.GOVERNED_ADDITIVITY,
                gt.column_dependency_key("bank", "public.accounts.balance"))
    assert _gaps(idea) == ()


def test_the_temporal_check_pins_the_as_of_lookup_and_the_governed_read(db):
    _bank(db)
    idea, rej = _validate(db, {"name": "bal_trend_90d",
                               "derives_from": ["public.accounts.balance"],
                               "aggregation": "trend_90d"}, ["public.accounts.balance"])
    assert rej is None
    lookup = _pin(idea.grounding_trace, gt.AS_OF_COLUMN_LOOKUP,
                  gt.table_dependency_key("bank", "accounts"))
    assert lookup.dependency_class is SuggestionDependencyClass.HARD_AVAILABILITY
    assert _pin(idea.grounding_trace, gt.GOVERNED_IS_AS_OF,
                gt.column_dependency_key("bank", "public.accounts.posted_at"))
    assert _gaps(idea) == ()


def test_the_grain_check_pins_the_grain_lookup_and_the_governed_read(db):
    _bank(db)
    idea, rej = _validate(db, {"name": "avg_bal_per_acct",
                               "derives_from": ["public.accounts.balance"],
                               "aggregation": "avg", "grain_table": "accounts"},
                          ["public.accounts.balance"])
    assert rej is None
    assert _pin(idea.grounding_trace, gt.GRAIN_COLUMN_LOOKUP,
                gt.table_dependency_key("bank", "accounts"))
    assert _pin(idea.grounding_trace, gt.GOVERNED_IS_GRAIN,
                gt.column_dependency_key("bank", "public.accounts.id"))
    assert _gaps(idea) == ()


def test_the_unit_and_currency_hints_are_pinned_where_column_meta_read_them(db):
    """Both hard rejects (MIXED_UNITS / MIXED_CURRENCY) and both needs-checks read the same
    ``_column_meta`` hints, so the pin sits at that ONE read."""
    _bank(db)
    idea, rej = _validate(db, {"name": "fee_ratio",
                               "derives_from": ["public.accounts.balance", "public.accounts.fees"],
                               "aggregation": "ratio"},
                          ["public.accounts.balance", "public.accounts.fees"])
    assert rej is None
    for ref in ("public.accounts.balance", "public.accounts.fees"):
        key = gt.column_dependency_key("bank", ref)
        assert _pin(idea.grounding_trace, gt.COLUMN_UNIT_HINT, key)
        assert _pin(idea.grounding_trace, gt.COLUMN_CURRENCY_HINT, key)
    assert {r.code for r in idea.requirements} >= {"UNIT_CONSISTENT", "CURRENCY_CONSISTENT"}
    assert _gaps(idea) == ()


def test_the_ai_unit_suggestion_is_pinned_as_a_SEMANTIC_dependency(db):
    """The llm/proposed unit changes no disposition — it decorates a requirement — so its drift
    class is SEMANTIC, not VALIDATION. The class is what stops a later reader suppressing a
    readiness claim because an advisory hint moved."""
    _bank(db)
    record_field_evidence(
        db, logical_ref="bank::public.accounts.balance", field_name="unit",
        proposed_value="AED", producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        producer_ref="drafter", source_snapshot_id="s1", input_hash="unit-hash")
    idea, rej = _validate(db, {"name": "fee_ratio",
                               "derives_from": ["public.accounts.balance", "public.accounts.fees"],
                               "aggregation": "ratio"},
                          ["public.accounts.balance", "public.accounts.fees"])
    assert rej is None
    pin = _pin(idea.grounding_trace, gt.AI_UNIT_SUGGESTION,
               gt.column_dependency_key("bank", "public.accounts.balance"))
    assert pin.dependency_class is SuggestionDependencyClass.SEMANTIC
    assert pin.evidence and pin.evidence[0].producer is EvidenceProducer.LLM
    assert pin.evidence[0].lifecycle is EvidenceLifecycle.ACTIVE
    unit = [r for r in idea.requirements
            if r.code == "UNIT_CONSISTENT" and r.operand[1] == "public.accounts.balance"]
    assert unit and "AED" in unit[0].detail
    assert _gaps(idea) == ()


def test_the_template_operand_role_declaration_is_pinned_where_it_narrows_the_check(db):
    """E4b narrows the unit/currency question by the roles the TEMPLATE declared — a template-
    authored input to a decision, so the trace pins it (keyed by the template that declared it)."""
    _bank(db)
    roles = (("public.accounts.balance", "stock_col"), ("public.accounts.fees", "asof"))
    idea, rej = _validate(db, {"name": "fee_ratio",
                               "derives_from": ["public.accounts.balance", "public.accounts.fees"],
                               "aggregation": "ratio"},
                          ["public.accounts.balance", "public.accounts.fees"],
                          operand_roles=roles)
    assert rej is None
    pin = _pin(idea.grounding_trace, gt.TEMPLATE_OPERAND_ROLES, _TEMPLATE)
    assert pin.dependency_class is SuggestionDependencyClass.VALIDATION
    # the declaration really did narrow the decision (fees is not a measure -> nothing to mix)
    assert not any(r.code == "UNIT_CONSISTENT" for r in idea.requirements)
    assert _gaps(idea) == ()


def test_the_operand_roles_ride_in_binding_order_with_their_catalog(db):
    _bank(db)
    idea, _ = _validate(db, {"name": "fee_ratio",
                             "derives_from": ["public.accounts.balance", "public.accounts.fees"],
                             "aggregation": "ratio"},
                        ["public.accounts.balance", "public.accounts.fees"],
                        operand_roles=(("public.accounts.balance", "stock_col"),))
    assert idea.grounding_trace.ordered_operand_roles == (
        ("bank", "public.accounts.balance", "stock_col"),
        ("bank", "public.accounts.fees", ""))          # undeclared is EMPTY, never guessed


# ── the ordered join path is RETAINED, not re-searched ──────────────────────────────────────────
def _two_table(db, *, fact_key=None, status=None, acct_sensitivity=None):
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "data_type) VALUES ('bank', 'public.transactions.amount', 'column', 'transactions', "
               "'amount', 'numeric')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name) "
               "VALUES ('bank', 'public.transactions.acct_id', 'column', 'transactions', 'acct_id')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "is_grain, sensitivity) VALUES ('bank', 'public.accounts.account_id', 'column', "
               "'accounts', 'account_id', true, %s)", (acct_sensitivity,))
    db.execute("INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, "
               "authority, approved_join_fact_key, approved_join_status) VALUES ('bank', 'joins', "
               "'public.transactions.acct_id', 'public.accounts.account_id', 'N:1', 'operational', "
               "%s, %s)", (fact_key, status))
    _fresh(db)


_CROSS = {"name": "txn_per_acct", "derives_from": ["public.transactions.amount"],
          "aggregation": "count", "grain_table": "accounts"}


def test_the_selected_path_is_carried_leg_for_leg(db):
    _two_table(db)
    idea, rej = _validate(db, _CROSS, ["public.transactions.amount"])
    assert rej is None
    expected = join_outcome_relationship_path(
        classify_join_path(db, "bank", "accounts", "transactions", roles=()),
        catalog_source="bank")
    assert idea.grounding_trace.ordered_relationship_path == expected
    assert expected, "the fixture must actually traverse a join or the pin is vacuous"
    assert _pin(idea.grounding_trace, gt.JOIN_PATH,
                gt.column_dependency_key("bank", "public.transactions.amount"))
    assert _gaps(idea) == ()


def test_an_unverified_leg_is_carried_with_the_requirement_it_caused(db):
    _two_table(db, fact_key="ajf-1", status="DRAFT")
    idea, rej = _validate(db, _CROSS, ["public.transactions.amount"])
    assert rej is None and idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    leg = idea.grounding_trace.ordered_relationship_path[0]
    assert leg.safety_status == "unverified" and leg.review_status == "DRAFT"
    assert leg.realization_content_hash
    assert _gaps(idea) == ()


def test_no_consumer_could_rerun_the_search_and_get_a_different_answer(db):
    """The whole point of retention: the trace's legs ARE the planner's selection, so an explainer
    never needs to (and must never) walk the graph again."""
    _two_table(db, fact_key="ajf-1", status="VERIFIED")
    idea, _ = _validate(db, _CROSS, ["public.transactions.amount"])
    legs = idea.grounding_trace.ordered_relationship_path
    assert [leg.from_ref[1] for leg in legs] == ["public.accounts.account_id"]
    assert [leg.to_ref[1] for leg in legs] == ["public.transactions.acct_id"]
    assert legs[0].review_status == "VERIFIED" and legs[0].safety_status == "clearing"
    assert legs[0].cardinality == "1:N"    # AS TRAVERSED (accounts -> transactions), not as stored


# ── rejections carry the trace that explains them ───────────────────────────────────────────────
def test_a_rejection_carries_the_trace_of_what_had_been_read(db):
    _bank(db)
    idea, rej = _validate(db, {"name": "x", "derives_from": ["public.accounts.nope"],
                               "aggregation": "avg"}, ["public.accounts.balance"])
    assert idea is None and rej.code == RejectCode.UNGROUNDED
    assert rej.trace is not None and rej.trace.validation_status == "REJECTED"
    assert gt.READ_SCOPE in _kinds(rej.trace)
    assert recompute_trace_content_hash(rej.trace) == rej.trace.trace_content_hash


def test_a_no_join_path_rejection_names_the_path_it_could_not_find(db):
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "data_type) VALUES ('bank', 'public.transactions.amount', 'column', 'transactions', "
               "'amount', 'numeric')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "is_grain) VALUES ('bank', 'public.accounts.account_id', 'column', 'accounts', "
               "'account_id', true)")
    _fresh(db)
    idea, rej = _validate(db, _CROSS, ["public.transactions.amount"])
    assert idea is None and rej.code == RejectCode.NO_JOIN_PATH
    assert _pin(rej.trace, gt.JOIN_PATH,
                gt.column_dependency_key("bank", "public.transactions.amount"))
    assert rej.trace.ordered_relationship_path == ()   # there was no path — none is claimed


def test_a_denied_hop_rejection_claims_no_traversal(db):
    _two_table(db, acct_sensitivity="pii")
    idea, rej = _validate(db, _CROSS, ["public.transactions.amount"])
    assert idea is None and rej.code == RejectCode.JOIN_DENIED
    assert rej.trace.ordered_relationship_path == ()


def test_a_rejection_on_the_untraced_path_still_returns_the_bare_rejection(db):
    _bank(db)
    known, src_of = _kv(["public.accounts.balance"])
    idea, rej = _validate_idea(db, {"name": "x", "derives_from": ["nope"], "aggregation": "avg"},
                               known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.UNGROUNDED and rej.trace is None


@pytest.mark.parametrize("kind", [gt.READ_SCOPE, gt.GROUNDING_CANDIDATE_SET,
                                  gt.GOVERNED_IS_GRAIN, gt.GRAIN_COLUMN_LOOKUP])
def test_mutating_the_emitted_trace_kills_its_completeness(db, kind):
    """MUTATION over the REAL emitted trace (not a hand-built one): drop the type/grain/as-of/path
    dependency and the DESIGN_CHECKED claim can no longer be justified."""
    _bank(db)
    idea, _ = _validate(db, {"name": "avg_bal_per_acct",
                             "derives_from": ["public.accounts.balance"],
                             "aggregation": "avg", "grain_table": "accounts"},
                        ["public.accounts.balance"])
    assert _gaps(idea) == ()
    mutated = replace(idea.grounding_trace, dependency_pins=tuple(
        p for p in idea.grounding_trace.dependency_pins if p.dependency_kind != kind))
    assert trace_completeness_gaps(mutated, validation_status=idea.validation_status,
                                  requirements=idea.requirements) != ()
