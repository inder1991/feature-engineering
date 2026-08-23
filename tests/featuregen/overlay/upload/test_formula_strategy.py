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
                deterministic_lane_available=True,
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


def test_A_REVIEWED_ENTRY_WITHOUT_A_FROZEN_CONTEXT_ROUTES_LLM_with_the_reason(db=None):
    """▲ A GAP is not a DEFECT. `blueprint_bindable` is false when THIS candidate has no frozen
    grounding context (a legacy revision, an ambiguous key, an engine binding that never bound) —
    the deterministic lane cannot EXECUTE for it, so it authors by LLM with the reason RECORDED.
    A blueprint that genuinely FAILS to bind surfaces at the WORKER as
    REVIEWED_BLUEPRINT_NOT_EXECUTABLE, where binding actually runs — pinned by the worker's own
    test_A_MOVED_BLUEPRINT_BLOCKS_BY_NAME_never_falls_back."""
    decision = resolve_formula_strategy(_reviewed(blueprint_bindable=False))

    assert decision.strategy is FormulaStrategy.LLM_AUTHORED
    assert "REVIEWED_LANE_UNAVAILABLE" in decision.warnings
    assert decision.blockers == ()


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


def test_A_REVIEWED_ENTRY_WITH_THE_LANE_OFF_ROUTES_LLM_with_the_reason_recorded():
    """▲ A PLATFORM capability gap is not a CANDIDATE defect. While the deterministic lane's
    grounding-context plumbing is unpersisted, a reviewed-V2 candidate authors by LLM — with the
    reason RECORDED, never silently, and never wearing REVIEWED_BLUEPRINT_NOT_EXECUTABLE, which is
    reserved for a blueprint that genuinely failed against this candidate."""
    decision = resolve_formula_strategy(_reviewed(deterministic_lane_available=False))

    assert decision.strategy is FormulaStrategy.LLM_AUTHORED
    assert "REVIEWED_LANE_UNAVAILABLE" in decision.warnings
    assert "REVIEWED_BLUEPRINT_NOT_EXECUTABLE" not in decision.blockers


def test_THE_LANE_POSTURE_IS_NOT_IN_THE_IDENTITY_but_the_strategy_it_changes_is():
    """The posture is a MEASURED deployment fact and stays out of the hash; it moves the identity
    only through the STRATEGY it selects — the front door, never a re-mint of an existing draft."""
    lane_on = resolve_formula_strategy(_reviewed())
    lane_off = resolve_formula_strategy(_reviewed(deterministic_lane_available=False))

    # Different strategies, so different identities — through the strategy axis.
    assert lane_on.strategy is not lane_off.strategy
    assert lane_on.strategy_identity_hash != lane_off.strategy_identity_hash


def test_every_code_the_resolver_can_emit_is_in_the_closed_vocabulary():
    """§5: the resolver's four warnings and the worker's refusal are REASON CODES, not private
    strings — each holds a row in REASON_FAMILIES, so the product test over the six-action
    disposition table covers them mechanically. The set here is the resolver's CLOSED warning
    channel; extending it means adding a vocabulary code first (the three-part commit)."""
    from featuregen.overlay.upload import semantic_eligibility_reasons as R

    for code in ("REVIEWED_LANE_UNAVAILABLE", "REVIEWED_EXPECTATION_LEGACY_VERSION",
                 "BLUEPRINT_DERIVED_NOT_REVIEWED", "LLM_AUTHORING_REQUIRED",
                 "REVIEWED_BLUEPRINT_NOT_EXECUTABLE"):
        assert code in R.REASON_FAMILIES, code
        assert getattr(R, code) == code
