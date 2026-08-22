"""Which method authors a formula, decided from evidence.

▲ The case worth reading first is `test_DERIVABILITY_GRANTS_NOTHING`. Ninety blueprints are
derivable and one is reviewed, and a resolver that confuses them seals eighty-nine unreviewed method
claims into an append-only table.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.overlay.upload.formula_strategy import (
    FormulaStrategy,
    FormulaStrategyFactsV1,
    resolve_formula_strategy,
)

DETERMINISTIC = dict(candidate_origin="recipe", computation_kind="deterministic_formula",
                     considered_revision_id="crev-1", option_id="opt-a")


def _facts(**over) -> FormulaStrategyFactsV1:
    return FormulaStrategyFactsV1(**{**DETERMINISTIC, **over})


def _reviewed(**over) -> FormulaStrategyFactsV1:
    """A current, reviewed, V2 registry entry that binds — the one deterministic case."""
    base = dict(expectation_ref="posted_debit_amount", expectation_generation="v2",
                reviewed_expectation_current=True, blueprint_bindable=True,
                reviewed_blueprint_revision="rev-1", reviewed_blueprint_hash="h-1")
    return _facts(**{**base, **over})


# ══ THE ONE THAT MATTERS ═══════════════════════════════════════════════════════════════════════
def test_DERIVABILITY_GRANTS_NOTHING(db=None):
    """▲ `derive_blueprint_v2` is pure and consults no review — it derives for 90 of 317 recipes,
    and the reviewed registry has ONE entry. A resolver that branched on derivability would claim a
    review no derivation can supply, into an append-only provenance table."""
    decision = resolve_formula_strategy(_facts(blueprint_derivable=True, blueprint_bindable=True))

    assert decision.strategy is FormulaStrategy.LLM_AUTHORED
    assert "BLUEPRINT_DERIVED_NOT_REVIEWED" in decision.warnings


def test_FLIPPING_DERIVABILITY_DOES_NOT_MOVE_A_REGISTRY_DECISION():
    """The invariant stated as a test, because it is the one a future refactor breaks silently."""
    reviewed = _reviewed()
    flipped = dataclasses.replace(reviewed, blueprint_derivable=not reviewed.blueprint_derivable)

    assert (resolve_formula_strategy(reviewed).strategy
            == resolve_formula_strategy(flipped).strategy)
    assert (resolve_formula_strategy(reviewed).strategy_identity_hash
            == resolve_formula_strategy(flipped).strategy_identity_hash), \
        "a MEASURED fact in the identity re-mints it when the measurement flaps, and buys the answer again"


# ══ THE ROUTING TABLE ══════════════════════════════════════════════════════════════════════════
def test_A_CURRENT_REVIEWED_V2_ENTRY_THAT_BINDS_IS_DETERMINISTIC():
    decision = resolve_formula_strategy(_reviewed())

    assert decision.strategy is FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT
    assert decision.blockers == () and decision.warnings == ()


def test_A_REVIEWED_ENTRY_THAT_FAILS_TO_BIND_IS_BLOCKED_not_fallen_back():
    """▲ NO SILENT LLM FALLBACK. It would hide a broken reviewed blueprint, change the cost, and
    change which certificate production needs."""
    decision = resolve_formula_strategy(_reviewed(blueprint_bindable=False))

    assert decision.strategy is FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT
    assert "REVIEWED_BLUEPRINT_NOT_EXECUTABLE" in decision.blockers


def test_A_FORMULA_V1_REVIEWED_ENTRY_ROUTES_TO_THE_LLM():
    """Two of the three reviewed recipes are Formula V1, so union membership cannot select the V3
    deterministic producer."""
    decision = resolve_formula_strategy(
        _reviewed(expectation_generation="v1", reviewed_blueprint_revision=None,
                  reviewed_blueprint_hash=None))

    assert decision.strategy is FormulaStrategy.LLM_AUTHORED
    assert "REVIEWED_EXPECTATION_LEGACY_VERSION" in decision.warnings


def test_NO_REVIEWED_ENTRY_ROUTES_TO_THE_LLM():
    decision = resolve_formula_strategy(_facts())

    assert decision.strategy is FormulaStrategy.LLM_AUTHORED
    assert "LLM_AUTHORING_REQUIRED" in decision.warnings


@pytest.mark.parametrize("generation", [None, "unknown", "v3", ""])
def test_AN_UNKNOWN_GENERATION_FAILS_CLOSED(generation):
    """▲ It never becomes v2 by default. A generation the platform cannot name is not one it can
    certify a deterministic producer against."""
    decision = resolve_formula_strategy(
        _reviewed(expectation_generation=generation))

    assert decision.strategy is FormulaStrategy.LLM_AUTHORED


@pytest.mark.parametrize("kind,expected", [
    ("conceptual_pattern", FormulaStrategy.NON_FORMULA),
    ("governed_model_output", FormulaStrategy.MODEL_WORKFLOW),
])
def test_NON_FORMULA_KINDS_NEVER_ADVERTISE_CODE_GENERATION(kind, expected):
    decision = resolve_formula_strategy(_facts(computation_kind=kind))
    assert decision.strategy is expected


def test_A_NON_FORMULA_KIND_WINS_OVER_A_REVIEWED_ENTRY():
    """Order matters: a conceptual pattern with a stale reviewed entry is still not a formula."""
    decision = resolve_formula_strategy(
        _reviewed(computation_kind="conceptual_pattern"))
    assert decision.strategy is FormulaStrategy.NON_FORMULA


# ══ THE OVERRIDE IS EVIDENCE, NOT A LABEL ══════════════════════════════════════════════════════
def test_AN_OVERRIDE_ROUTES_TO_THE_LLM_and_says_so():
    """▲ "Try AI formula" reaches here as a SERVER-AUTHORED revision the resolver consumes, never as
    a strategy the client sent. The resolver still decides — which is what keeps method selection
    server-owned while the retry stays possible."""
    decision = resolve_formula_strategy(_reviewed(method_override_revision_id="ovr-1"))

    assert decision.strategy is FormulaStrategy.LLM_AUTHORED
    assert "METHOD_OVERRIDDEN_TO_LLM" in decision.warnings


def test_AN_OVERRIDE_DOES_NOT_OVERRIDE_A_NON_FORMULA():
    """There is no formula to retry. An override that could turn a conceptual pattern into an LLM
    formula would be a client turning "not a computation" into one."""
    decision = resolve_formula_strategy(
        _facts(computation_kind="conceptual_pattern", method_override_revision_id="ovr-1"))
    assert decision.strategy is FormulaStrategy.NON_FORMULA


# ══ IDENTITY ═══════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field,value", [
    ("expectation_ref", "something-else"),
    ("expectation_generation", "v1"),
    ("reviewed_blueprint_hash", "h-other"),
    ("provider_contract_hash", "pc-other"),
    ("catalog_snapshot_hash", "snap-other"),
    ("method_override_revision_id", "ovr-9"),
])
def test_EVERY_EVIDENCE_FIELD_MOVES_THE_IDENTITY(field, value):
    """It is folded into the DRAFT identity, so a field that did not move it would let two different
    decisions share one identity — and the money guard would serve the wrong one."""
    base = _reviewed()
    moved = dataclasses.replace(base, **{field: value})

    assert (resolve_formula_strategy(base).strategy_identity_hash
            != resolve_formula_strategy(moved).strategy_identity_hash)


def test_THE_SAME_EVIDENCE_IS_THE_SAME_IDENTITY():
    assert (resolve_formula_strategy(_reviewed()).strategy_identity_hash
            == resolve_formula_strategy(_reviewed()).strategy_identity_hash)
