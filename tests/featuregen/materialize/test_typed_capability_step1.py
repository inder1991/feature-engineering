"""Capability by typed signature, bound to the build that produced it.

Step 1 of the V2-only plan. Two properties, and one promise kept.

The MODEL: a capability row says "(kind, variant) under build B". Keyed on the kind alone it could
not say what is actually true — `sum` renders and `avg` does not, and both are AGGREGATE. A table
that can only answer at the kind level is either lying about `avg` or slandering `sum`, which is why
nothing ever wrote a row into it.

The FINGERPRINT: an execution proof is a claim about a BUILD. Without binding it to one, changing
the renderer leaves the proof green while the code it was about no longer exists.

The PROMISE: this step changes what V2 admission checks. It must NOT change the readiness answer
shown for the recipe catalogue, which reads a different capability source entirely.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.execution_proof_store import (
    SOLE_VARIANT,
    advertised_operators,
    record_renderer_dispatch,
    renderer_build_hash,
    set_execution_proof,
)
from featuregen.materialize.operator_graph_v2 import OperatorKindV2

ENGINE = "kedro-pyspark"


def _dispatch_all(db, *, build: str | None = None, extra=()):
    record_renderer_dispatch(db, engine_id=ENGINE, build_hash=build, dispatchable={
        **{(kind.value, SOLE_VARIANT): True for kind in OperatorKindV2},
        **{sig: True for sig in extra}})


# ══ THE MODEL SAYS WHAT IS TRUE ═════════════════════════════════════════════════════════════════
def test_ONE_VARIANT_CAN_BE_SUPPORTED_WHILE_ANOTHER_IS_NOT(db):
    """The state the old model could not express, and the reason this step exists.

    `sum` renders, `avg` does not, and both are AGGREGATE. A kind-level row had no honest value to
    hold — which is why the table sat empty in every environment rather than holding a wrong one.
    """
    _dispatch_all(db, extra={("aggregate", "sum"), ("aggregate", "avg")})
    from tests.featuregen.materialize.test_admission_v2_s13 import _proof_hash_for

    proof = _proof_hash_for(db)
    set_execution_proof(db, engine_id=ENGINE, operator_kind="aggregate",
                        operator_variant="sum", proof_hash=proof)

    advertised = set(advertised_operators(db, engine_id=ENGINE))
    assert ("aggregate", "sum") in advertised
    assert ("aggregate", "avg") not in advertised, "an unproved variant was advertised"


# ══ THE PROOF IS BOUND TO THE BUILD ═════════════════════════════════════════════════════════════
def test_A_PROOF_DOES_NOT_SURVIVE_THE_BUILD_IT_WAS_ABOUT(db):
    """The half the previous shape had no answer for at all.

    A proof says "we ran this and the number was right" — a claim about a BUILD. Change the renderer
    and, without a fingerprint, the claim silently becomes a statement about code that no longer
    exists. Here a moved renderer simply has no rows yet, so the operator is unsupported: exactly
    true, and true without a sweep having to remember to invalidate anything.
    """
    from tests.featuregen.materialize.test_admission_v2_s13 import _proof_hash_for

    _dispatch_all(db, extra={("aggregate", "sum")})
    set_execution_proof(db, engine_id=ENGINE, operator_kind="aggregate",
                        operator_variant="sum", proof_hash=_proof_hash_for(db))
    assert ("aggregate", "sum") in set(advertised_operators(db, engine_id=ENGINE))

    # The renderer changes. Nothing is deleted; the question is simply asked of a different build.
    moved = "rbh-a-renderer-that-did-not-exist-before"
    assert advertised_operators(db, engine_id=ENGINE, build_hash=moved) == ()


def test_a_proof_cannot_be_attached_to_a_build_that_never_dispatched_it(db):
    """Previously invisible, because the build was not part of the key."""
    from tests.featuregen.materialize.test_admission_v2_s13 import _proof_hash_for

    _dispatch_all(db, extra={("aggregate", "sum")})
    with pytest.raises(ValueError, match="no capability row"):
        set_execution_proof(db, engine_id=ENGINE, operator_kind="aggregate",
                            operator_variant="sum", proof_hash=_proof_hash_for(db),
                            build_hash="rbh-some-other-build")


def test_the_build_hash_is_DERIVED_from_what_the_renderer_can_emit(db):
    """Derived from the dispatch surface, not from module bytes.

    A comment or a docstring must not invalidate every proof in the system; a changed set of
    emittable operations must. Asserted as stability here — the negative half (that it MOVES when
    the surface moves) belongs with the renderer work in step 8.
    """
    assert renderer_build_hash() == renderer_build_hash()
    assert renderer_build_hash().startswith("rbh-")


# ══ THE PROMISE: THE RECIPE CATALOGUE DOES NOT MOVE ════════════════════════════════════════════
def test_STEP_1_DOES_NOT_CHANGE_THE_RECIPE_READINESS_ANSWER():
    """The cross-effect flagged before this step landed, pinned so it stays true.

    Recipe readiness is computed from the IN-CODE advertised set (`engine_capability_for`), not from
    the capability table this step reshapes. So the reshape must leave it untouched — a capability
    refactor that silently re-labels 317 recipes would be a product change wearing an infrastructure
    commit message.
    """
    from featuregen.materialize.engine_capability import engine_capability_for
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    assert len(V2_RECIPES) == 317
    assert sorted(engine_capability_for("kedro-pyspark").supported_aggregations) == [
        "count_distinct", "count_non_null", "count_rows", "sum"]
