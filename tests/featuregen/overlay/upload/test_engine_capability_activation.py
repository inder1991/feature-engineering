"""C2 — activation reads the engine capability registry instead of hardwiring "unsupported".

The seam under test: ``assemble_current_activation_state`` answers ``formula_schema_supported``
by resolving the recipe's REVIEWED expectation (the capture blueprint — the same object a review
event's hash covers) and classifying its demands against the kedro-pyspark advertisement. The
refusal must remain reachable: a capability seam that can only say yes is not one.
"""
from __future__ import annotations

import dataclasses

from featuregen.formula.capability_v2 import (
    EngineCapabilityV1,
    classify_demands_for_engine,
)
from featuregen.overlay.upload import recipe_formula_shadow
from featuregen.overlay.upload.semantic_option_decision import _formula_schema_supported

#: The one reviewed v2 expectation (A5) and a reviewed v1 anchor — both generations answer
#: through the SAME resolver, which is the point of A5's capture_blueprint_for.
EXEMPLAR = "posted_debit_amount"
REVIEWED_V1 = "merchant_mcc_diversity"
UNREVIEWED = "balance_slope"


def _offset_blueprint():
    """The exemplar's real derived blueprint, with one window shifted back a period — a demand
    the renderer does not advertise."""
    capture = recipe_formula_shadow.capture_blueprint_for(EXEMPLAR)
    assert capture is not None
    blueprint = capture.blueprint
    first = blueprint.expressions[0]
    shifted = dataclasses.replace(first, window=dataclasses.replace(
        first.window, offset_periods=1))
    return dataclasses.replace(blueprint, expressions=(shifted, *blueprint.expressions[1:]))


def test_a_supported_v2_formula_flips_formula_schema_supported():
    """The exemplar's one demand (``sum``, no offset, no horizon) is advertised — supported,
    and only this value and "unsupported" are entitled to speak about the engine (§6)."""
    assert _formula_schema_supported(EXEMPLAR) == "supported"


def test_a_reviewed_v1_expectation_answers_through_the_same_resolver():
    """The v1 anchor's ``count_distinct`` is advertised and v1 windows have neither fork — the
    platform's one executable formula generation reads as supported, through the exact resolver
    whose hash a review event covers (A5). D-7's grain disagreement is a DIFFERENT question and
    stays open; nothing here re-keys it."""
    assert _formula_schema_supported(REVIEWED_V1) == "supported"


def test_an_unreviewed_recipe_is_UNMEASURED_never_an_engine_claim():
    """§6: no review → nothing to measure. The old bool said "unsupported" here, which told the
    operator the ENGINE said no when the engine was never asked — a false statement that sent a
    governance problem to the engine team. The tri-state names the actual gap."""
    assert _formula_schema_supported(UNREVIEWED) == "unmeasured_unreviewed"


def test_an_offset_formula_is_unsupported_when_the_engine_does_not_advertise_offsets(
        monkeypatch):
    """The refusal stays reachable — through the REAL seam, not only the classifier: the
    exemplar's blueprint with one shifted window folds back to False."""
    engine = EngineCapabilityV1(engine_id="kedro-pyspark",
                                supported_aggregations=frozenset({"sum"}))
    assert classify_demands_for_engine(
        {"sum"}, uses_window_offset=True, uses_future_horizon=False,
        engine=engine) == "unsupported_engine"

    offset = _offset_blueprint()
    declared = recipe_formula_shadow.capture_blueprint_for(EXEMPLAR).declared_schema_version
    monkeypatch.setattr(
        recipe_formula_shadow, "capture_blueprint_for",
        lambda recipe_id: recipe_formula_shadow.CaptureBlueprintV1(declared, offset))
    assert _formula_schema_supported(EXEMPLAR) == "unsupported"


def test_an_unresolvable_expectation_fails_closed(monkeypatch):
    """The plan's "unparseable pinned fixture" case, corrected to the buildable seam: the pinned
    fixture lives under ``tests/`` and production never parses it (the pin test does). What
    production CAN hit is a resolver that raises or resolves nothing — both are UNMEASURED
    (§6: measurement absence, not an engine verdict), never an exception."""
    monkeypatch.setattr(recipe_formula_shadow, "capture_blueprint_for",
                        lambda recipe_id: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _formula_schema_supported(EXEMPLAR) == "unmeasured_engine"
    monkeypatch.setattr(recipe_formula_shadow, "capture_blueprint_for", lambda recipe_id: None)
    assert _formula_schema_supported(EXEMPLAR) == "unmeasured_engine"


def test_an_unknown_engine_is_unsupported_never_a_default():
    assert classify_demands_for_engine(
        {"sum"}, uses_window_offset=False, uses_future_horizon=False,
        engine=None) == "unsupported_engine"
    assert classify_demands_for_engine(
        {"avg"}, uses_window_offset=False, uses_future_horizon=False,
        engine=EngineCapabilityV1(engine_id="e", supported_aggregations=frozenset({"sum"})),
    ) == "unsupported_engine"
