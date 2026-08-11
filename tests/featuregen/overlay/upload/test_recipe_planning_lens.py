"""SE-7 (part 1) — the V2 planning lens: authored-objective applicability, assembled candidates."""
from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_planning_lens import (
    BINDING_STATES,
    fold_binding_state,
    v2_applicability,
    v2_recipe_candidates,
)
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES, v2_recipe_by_id
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

SOURCE = "v2bank"
EXEMPLAR = v2_recipe_by_id("net_transaction_flow")   # 1 sibling at its authored objective


def _catalog(db) -> None:
    rows = [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD", definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "dc_flag", "text",
                      definition="debit/credit indicator"), "debit_credit_indicator"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp",
                      definition="when the transaction was booked"), "event_timestamp"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


# ── applicability: pure, over authored objectives — no tags, no crosswalk, no aliases ──────────

def test_unscoped_fails_open_to_all_primary():
    result = v2_applicability(ConfirmedScope(primary=None, unscoped=True))
    assert len(result.by_recipe) == len(V2_RECIPES)
    assert set(result.by_recipe.values()) == {"primary"}


def test_exact_scope_classifies_from_the_authored_primary():
    result = v2_applicability(ConfirmedScope(primary=EXEMPLAR.primary_objective))
    assert result.by_recipe[EXEMPLAR.recipe_id] == "primary"
    assert EXEMPLAR.recipe_id in result.eligible_ids
    # every recipe classified exactly once, and out-of-scope really means out
    assert len(result.by_recipe) == len(V2_RECIPES)
    out = [rid for rid, rel in result.by_recipe.items() if rel == "out_of_scope"]
    assert len(out) == len(V2_RECIPES) - len(result.eligible_ids)


def test_supporting_objectives_place_a_recipe_as_supporting_never_primary():
    supported = next(r for r in V2_RECIPES if r.supporting_objectives)
    scope = ConfirmedScope(primary=supported.supporting_objectives[0])
    result = v2_applicability(scope)
    assert result.by_recipe[supported.recipe_id] == "supporting"


# ── binding-state fold: fail-closed severity order over the shared verdict vocabulary ──────────

def test_fold_severity_order_is_blocked_then_ambiguous_then_missing():
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1

    required_roles = [op.role for op in EXEMPLAR.operands if op.required]
    a, b = required_roles[0], required_roles[1]

    def verdict(role, status):
        return OperandBindingVerdictV1(role=role, status=status)

    assert fold_binding_state(
        (verdict(a, "blocked"), verdict(b, "ambiguous")), EXEMPLAR) == "blocked"
    assert fold_binding_state(
        (verdict(a, "ambiguous"), verdict(b, "unresolved")), EXEMPLAR) == "ambiguous"
    assert fold_binding_state(
        (verdict(a, "unresolved"), verdict(b, "bound")), EXEMPLAR) == "missing"
    assert fold_binding_state(
        tuple(verdict(r, "bound") for r in required_roles), EXEMPLAR) == "bound"


def test_an_absent_optional_operand_degrades_it_never_blocks():
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1

    optional = next((op for op in EXEMPLAR.operands if not op.required), None)
    if optional is None:
        import pytest
        pytest.skip("exemplar has no optional operand")
    verdicts = tuple(
        OperandBindingVerdictV1(role=op.role,
                                status="unresolved" if op is optional else "bound")
        for op in EXEMPLAR.operands)
    assert fold_binding_state(verdicts, EXEMPLAR) == "bound"


# ── candidate assembly against a real catalog ──────────────────────────────────────────────────

def test_candidates_carry_the_complete_gate1_data(db):
    _catalog(db)
    scope = ConfirmedScope(primary=EXEMPLAR.primary_objective)
    candidates = v2_recipe_candidates(db, catalog_source=SOURCE, scope=scope)
    # The exemplar is the ONE primary at this objective and therefore ordered FIRST; the
    # supporting recipes (secondary-objective matches) follow it, never precede it.
    assert candidates[0].recipe_id == EXEMPLAR.recipe_id
    assert all(c.relationship == "supporting" for c in candidates[1:])
    candidate = candidates[0]
    assert candidate.relationship == "primary"
    assert candidate.recipe_revision_hash == canonical_recipe_v2_hash(EXEMPLAR)
    assert candidate.planning_request.origin == "recipe_v2"
    assert candidate.planning_request.source_content_hash == candidate.recipe_revision_hash
    assert {v.role for v in candidate.verdicts} == {op.role for op in EXEMPLAR.operands}
    assert candidate.binding_state in BINDING_STATES
    assert candidate.readiness == EXEMPLAR.readiness
    # temporal: compiled XOR blocked, never both and never neither
    assert bool(candidate.temporal_pit_text) != bool(candidate.temporal_blocker)
    # review store is empty: honestly not current, with the required roles named
    assert candidate.review_current is False
    assert "banking_sme" in candidate.review_missing_roles


def test_an_empty_catalog_yields_honest_missing_states_not_no_candidates(db):
    scope = ConfirmedScope(primary=EXEMPLAR.primary_objective)
    candidates = v2_recipe_candidates(db, catalog_source="empty_catalog", scope=scope)
    assert candidates[0].recipe_id == EXEMPLAR.recipe_id
    assert {c.binding_state for c in candidates} == {"missing"}   # honest, not absent
    required = {op.role for op in EXEMPLAR.operands if op.required}
    unresolved = {v.role for v in candidates[0].verdicts if v.status == "unresolved"}
    assert required <= unresolved
