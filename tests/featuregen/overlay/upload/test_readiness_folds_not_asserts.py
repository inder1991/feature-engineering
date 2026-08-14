"""Task A6 — readiness FOLDS instead of asserting.

Before this task ``V2RecipeCandidateV1.readiness`` carried the definition's authored literal and
``fold_readiness`` was never called on the serving path at all: a recipe the registry called
``FORMULA_AUTHORABLE`` said so on every catalog, including one where not a single operand bound.
Now the candidate's readiness is BR-7's fold over what this run actually measured, and the
authored literal survives as an ASSERTION the fold must not contradict, checked at
registry-validation time.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from featuregen.overlay.upload import recipe_formula_expectations_v2 as expectations_v2
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_planning_lens import (
    BLOCKER_OPERAND_NOT_BOUND,
    binding_blockers,
    v2_recipe_candidates,
)
from featuregen.overlay.upload.recipe_readiness import (
    READINESS_LADDER,
    exceeds_fold,
    fold_definition_readiness,
)
from featuregen.overlay.upload.recipe_registry_v2 import (
    V2_RECIPES,
    RecipeContractError,
    v2_recipe_by_id,
)
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope
from featuregen.overlay.upload.taxonomy.coverage import execution_readiness_of

SOURCE = "a6bank"
#: Binds completely against the fixture catalog below, and its ONLY fold blocker is the missing
#: reviewed expectation — which is what makes it the monotonicity probe.
BOUND_RECIPE = v2_recipe_by_id("net_transaction_flow")
BOUND_REF = "retail:net_transaction_flow"
PINNED_FIXTURE = ("30_posted_debit_amount_exemplar.json",
                  "093de7a0e954122f2e0e5706eea9af65ec18b42ddb7b29f4dbd9860911930bbc")

#: A6's measurement, pinned. The registry's authored literals turn out to be EXACTLY what the
#: fold produces from the same definitions — see the acceptance row: the number did not move,
#: and that is the evidence.
EXPECTED_REGISTRY_DISTRIBUTION = {
    "FORMULA_BLOCKED": 295, "CONCEPTUAL_ONLY": 19, "FORMULA_AUTHORABLE": 3}
#: The blockers behind those states — new evidence, because nothing served them before.
EXPECTED_REGISTRY_BLOCKERS = {
    "no_reviewed_formula_expectation": 295,     # every unreviewed deterministic recipe
    "model_feature_spec_owns_readiness": 8,     # governed model outputs — BR-7A owns them
    "gold_evaluation_unproven": 3,              # the three anchors, resting at AUTHORABLE
}


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


def _candidates(db, catalog_source: str):
    scope = ConfirmedScope(primary=BOUND_RECIPE.primary_objective)
    return v2_recipe_candidates(db, catalog_source=catalog_source, scope=scope)


# ── the registry law: the authored literal is an assertion, not the answer ─────────────────────

def test_every_registry_recipe_folds_to_at_least_its_authored_readiness():
    """All 317. A definition may under-claim (the fold can rank it higher than it says); it may
    never over-claim, which is exactly what the import-time law refuses."""
    over = [(r.recipe_id, r.readiness, fold_definition_readiness(r).state)
            for r in V2_RECIPES
            if exceeds_fold(r.readiness, fold_definition_readiness(r).state)]
    assert over == []
    assert len(V2_RECIPES) == 317


def test_the_registry_readiness_distribution_is_pinned():
    """The measurement A6 owes. It is also the finding: the authored literals ARE the fold's
    answer, recipe for recipe — the registry has never drifted from its own declarations. The
    blocker counts are the part that was never served before."""
    folded = [fold_definition_readiness(r) for r in V2_RECIPES]
    assert Counter(f.state for f in folded) == EXPECTED_REGISTRY_DISTRIBUTION
    assert Counter(r.readiness for r in V2_RECIPES) == EXPECTED_REGISTRY_DISTRIBUTION
    assert {r.recipe_id for r, f in zip(V2_RECIPES, folded, strict=True)
            if r.readiness != f.state} == set()
    assert Counter(c for f in folded for c in f.blockers) == EXPECTED_REGISTRY_BLOCKERS


def test_the_authored_literal_can_never_exceed_the_fold():
    """Mutate one definition upward and registry validation refuses it BY NAME, at import time.
    ``balance_slope`` has no reviewed expectation, so claiming FORMULA_AUTHORABLE is precisely
    the claim nothing measured."""
    from featuregen.overlay.upload.recipe_registry_v2 import validate_v2_registry

    inflated = replace(v2_recipe_by_id("balance_slope"), readiness="FORMULA_AUTHORABLE")
    with pytest.raises(RecipeContractError, match="fold to 'FORMULA_BLOCKED'"):
        validate_v2_registry((inflated,))
    # The honest literal for that same definition passes — the law refuses the CLAIM, not the
    # recipe.
    validate_v2_registry((v2_recipe_by_id("balance_slope"),))


def test_RETIRED_is_a_terminal_not_a_rung():
    """The ladder tuple ends with RETIRED, so a naive index comparison would read it as the
    highest state and let any definition claim it. It compares only to itself."""
    assert READINESS_LADDER[-1] == "RETIRED"
    assert exceeds_fold("RETIRED", "MATERIALIZATION_READY") is True
    assert exceeds_fold("MATERIALIZATION_READY", "RETIRED") is True
    assert exceeds_fold("RETIRED", "RETIRED") is False
    assert exceeds_fold("NOT_A_STATE", "FORMULA_BLOCKED") is True


def test_one_fold_answers_for_every_surface():
    """The coverage report and the definition fold are the SAME call now — a third opinion is
    what A6 removed, and a divergence here would be that opinion coming back."""
    for recipe in V2_RECIPES:
        assert execution_readiness_of(recipe.recipe_id) == fold_definition_readiness(
            recipe).state
    assert execution_readiness_of("not_a_registry_recipe") == "UNASSESSED"


# ── the serving path: what the candidate actually says now ─────────────────────────────────────

def test_a_candidate_that_did_not_bind_is_no_longer_served_its_authored_literal(db):
    """The behaviour change, on the empty catalog: ``posted_debit_amount`` is the one recipe the
    registry authors as FORMULA_AUTHORABLE, and against a catalog where nothing binds it now
    says FORMULA_BLOCKED and names BR-5's own reason — the first time an operand verdict has
    ever reached the readiness ladder. Before A6 the card claimed FORMULA_AUTHORABLE here."""
    exemplar = v2_recipe_by_id("posted_debit_amount")
    assert exemplar.readiness == "FORMULA_AUTHORABLE"
    scope = ConfirmedScope(primary=exemplar.primary_objective)
    candidates = v2_recipe_candidates(db, catalog_source="empty_catalog", scope=scope)
    served = next(c for c in candidates if c.recipe_id == exemplar.recipe_id)
    assert served.binding_state == "missing"
    assert served.readiness == "FORMULA_BLOCKED"
    assert "REQUIRED_OPERAND_MISSING" in served.readiness_blockers
    assert "no_reviewed_formula_expectation" not in served.readiness_blockers   # it IS reviewed


def test_readiness_moves_when_an_expectation_is_registered(db, monkeypatch):
    """The monotonicity ``fold_readiness`` promises, observed through the serving path: the same
    recipe, the same catalog, the same bindings — one registry entry is the only thing that
    changed, and the candidate moves FORMULA_BLOCKED -> FORMULA_AUTHORABLE. The entry is
    TEST-SCOPED; growing the real registry is the operator's act (A5, D-2)."""
    _catalog(db)
    before = [c for c in _candidates(db, SOURCE) if c.recipe_id == BOUND_RECIPE.recipe_id]
    assert before and all(c.binding_state == "bound" for c in before)
    assert {c.readiness for c in before} == {"FORMULA_BLOCKED"}
    assert {c.readiness_blockers for c in before} == {("no_reviewed_formula_expectation",)}

    monkeypatch.setattr(expectations_v2, "RECIPE_FORMULA_V2_EXPECTATIONS",
                        {BOUND_REF: PINNED_FIXTURE})
    after = [c for c in _candidates(db, SOURCE) if c.recipe_id == BOUND_RECIPE.recipe_id]
    assert {c.readiness for c in after} == {"FORMULA_AUTHORABLE"}
    assert {c.readiness_blockers for c in after} == {("gold_evaluation_unproven",)}
    assert [c.variant_key for c in after] == [c.variant_key for c in before]


def test_registering_an_expectation_cannot_lift_a_candidate_that_did_not_bind(db, monkeypatch):
    """Monotonicity is not a promotion: the registry clears ONE blocker, and a candidate whose
    required operands are unresolved keeps the ones it earned."""
    monkeypatch.setattr(expectations_v2, "RECIPE_FORMULA_V2_EXPECTATIONS",
                        {BOUND_REF: PINNED_FIXTURE})
    served = next(c for c in _candidates(db, "empty_catalog")
                  if c.recipe_id == BOUND_RECIPE.recipe_id)
    assert served.readiness == "FORMULA_BLOCKED"
    assert served.readiness_blockers == ("REQUIRED_OPERAND_MISSING",)


# ── the blocker projection itself ──────────────────────────────────────────────────────────────

def test_only_required_operands_contribute_binding_blockers():
    """``fold_binding_state``'s severity law, restated for the ladder: an absent OPTIONAL
    operand degrades a feature, it never blocks it."""
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1

    definition = BOUND_RECIPE
    required = next(op.role for op in definition.operands if op.required)
    optional = next((op.role for op in definition.operands if not op.required), None)
    verdicts = (OperandBindingVerdictV1(role=required, status="unresolved",
                                        reason_codes=("REQUIRED_OPERAND_MISSING",)),)
    assert binding_blockers(verdicts, definition) == ("REQUIRED_OPERAND_MISSING",)
    if optional is not None:
        ignored = (OperandBindingVerdictV1(role=optional, status="unresolved",
                                           reason_codes=("SOMETHING",)),)
        assert binding_blockers(ignored, definition) == ()


def test_a_required_operand_that_did_not_bind_always_contributes_a_blocker():
    """The fail-closed fallback. No path in today's binder produces it, and that is the point:
    a verdict that forgot to explain itself must never PROMOTE a candidate by omission."""
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1

    required = next(op.role for op in BOUND_RECIPE.operands if op.required)
    silent = (OperandBindingVerdictV1(role=required, status="blocked", reason_codes=()),)
    assert binding_blockers(silent, BOUND_RECIPE) == (BLOCKER_OPERAND_NOT_BOUND,)
