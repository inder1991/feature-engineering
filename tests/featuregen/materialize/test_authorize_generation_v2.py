"""Step 6 — both gates, in order, over one build set's planned IRs.

`full_read_set_leakage_gate_v2` and `authorize_compilation_v2` were both built and neither had a
production caller; V1 has no compile-time leakage gate at all. This is the wiring, and the ORDER is
the substance.

What these tests hold:

1. **Leakage is decided BEFORE read scope**, because they are facts about different things: leakage
   is a property of the FEATURE and wrong for everyone, read scope is a property of the CALLER and
   may be right for someone else.
2. **An exploration build makes NO leakage claim**, and "no claim" is not the same value as "passed".
3. **The claim travels with the result**, so a caller reporting a pass carries its disclaimer.
4. **The whole group refuses**, never the offending member — a group is published as one row per key.

Driven off REALLY COMPILED IRs through `test_chain_v2_s6`'s helpers, reused rather than re-created:
a hand-built planned IR would prove only that these types accept objects shaped the way this file
shapes them.
"""
from __future__ import annotations

import pytest
from tests.featuregen.materialize.test_chain_v2_s6 import (  # reused, not re-created
    FX_RATE,
    FX_REF,
    _fx_read,
    _planned,
)
from tests.featuregen.materialize.test_ir import (
    _ROLES,
    CUSTOMERS,
    DECLARATION,
    INVENTORY,
    TXN_AMT,
    _admitted,
    _col,
    _table_node,
    _tag,
    compile_ir,
    seed_catalog,
)

from featuregen.materialize.authorize_generation_v2 import (
    AuthorizedGenerationV2,
    authorize_generation_v2,
)
from featuregen.materialize.boundary_v2 import (
    AuthorizedCompilationV2,
    KnowledgeTimeBasisV2,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.generation_authorization import GenerationAuthorizationV1
from featuregen.overlay.upload.selection_revisions import TargetModeV1

TARGET = f"{CUSTOMERS}.status_cd"


# The seeding is shared with `test_chain_v2_s6`; the FIXTURES are declared here rather than imported
# from it. A fixture imported by name shadows the test parameter that requests it, and hoisting one
# called `catalog` into a conftest would silently re-bind the eight other materialize modules that
# already request a `catalog` — a rename nobody asked for, in files this change does not touch.
@pytest.fixture
def catalog(db):
    seed_catalog(db)
    for column in ("rate", "quote_dt", "ccy"):
        _col(db, "fx_rates", column)
    _table_node(db, "fx_rates")
    return db


@pytest.fixture
def v1_ir(catalog):
    return compile_ir(catalog, _admitted("total_debit_amount_30d"), roles=_ROLES,
                      spine_decl=DECLARATION, inventory=INVENTORY)


def _authorization(*, mode: TargetModeV1 = TargetModeV1.PREDICTION,
                   target_ref: str | None = TARGET) -> GenerationAuthorizationV1:
    return GenerationAuthorizationV1(
        environment_id="hdfc-local", logical_group_name="account__daily",
        build_set_revision_id="bs-0001", target_mode=mode,
        target_ref=target_ref if mode is TargetModeV1.PREDICTION else None)


def _run(catalog, planned, *, authorization=None, roles=_ROLES, spine=None, **kwargs):
    return authorize_generation_v2(
        catalog, planned, spine=spine if spine is not None else planned[0].ir.spine,
        authorization=authorization if authorization is not None else _authorization(),
        roles=roles, **kwargs)


# ══ BOTH GATES PASS ════════════════════════════════════════════════════════════════════════════
def test_a_CLEAN_BUILD_is_authorized_and_carries_BOTH_verdicts(catalog, v1_ir):
    """"Authorized" and "proved not to leak" are different statements, and a caller holding only a
    Gate 2 token cannot tell which it has — so the leakage verdict travels with it."""
    planned = _planned(v1_ir)
    result = _run(catalog, [planned])

    assert isinstance(result, AuthorizedGenerationV2), result
    assert isinstance(result.token, AuthorizedCompilationV2)
    assert result.leakage is not None and result.leakage.admitted is True


def test_the_TOKEN_STILL_HOLDS_THE_SAME_OBJECTS(catalog, v1_ir):
    """C-C2's ordering must survive this wiring: nothing is re-planned between the gates, so the
    read set the gates decided over is the tuple the renderer executes."""
    planned = _planned(v1_ir)
    result = _run(catalog, [planned])

    assert isinstance(result, AuthorizedGenerationV2), result
    assert result.token.planned[0] is planned
    assert result.token.planned[0].read_set is planned.read_set


# ══ LEAKAGE ═══════════════════════════════════════════════════════════════════════════════════
def test_A_FEATURE_READING_ITS_OWN_TARGET_REFUSES(catalog, v1_ir):
    """A model trained on this scores beautifully in backtest and fails in production."""
    result = _run(catalog, [_planned(v1_ir)],
                  authorization=_authorization(target_ref=TXN_AMT))

    assert isinstance(result, MaterializationRefused), result
    assert result.code is CompilationRefusalCode.TARGET_LEAKAGE_DETECTED
    assert TXN_AMT in result.detail


def test_the_refusal_names_WHICH_FEATURE_AND_WHICH_PATH(catalog, v1_ir):
    """"Something leaks" is not actionable. A column can be read both as an operand and as a
    policy's column, and an author told only the ref would close one door with the other open."""
    result = _run(catalog, [_planned(v1_ir, converted=True)],
                  authorization=_authorization(target_ref=FX_RATE))

    assert isinstance(result, MaterializationRefused), result
    assert "posted_debit_amount_30d" in result.detail
    assert FX_REF in result.detail                       # attributed to the POLICY that reads it


def test_a_POST_CUTOFF_POLICY_READ_REFUSES(catalog, v1_ir):
    """The basis, not the promise: a rate read at current state tells you something that became
    true after the cutoff however generous the declared availability is."""
    planned = _planned(
        v1_ir, converted=True,
        policy_reads=(_fx_read(basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE),))
    result = _run(catalog, [planned])

    assert isinstance(result, MaterializationRefused), result
    assert result.code is CompilationRefusalCode.TARGET_LEAKAGE_DETECTED
    assert "does not change that" in result.detail


def test_the_WHOLE_GROUP_REFUSES_not_the_leaking_member(catalog, v1_ir):
    """A group is published as one row per key, so dropping the offender and building the rest
    would publish a table whose columns were authorized under different rules."""
    clean = _planned(v1_ir, name="clean_feature")
    leaky = _planned(v1_ir, name="leaky_feature")
    result = _run(catalog, [clean, leaky],
                  authorization=_authorization(target_ref=TXN_AMT))

    assert isinstance(result, MaterializationRefused), result
    assert "clean_feature" in result.detail and "leaky_feature" in result.detail


# ══ THE ORDER — THE SUBSTANCE OF THIS MODULE ═══════════════════════════════════════════════════
def test_LEAKAGE_IS_DECIDED_BEFORE_READ_SCOPE(catalog, v1_ir):
    """A build that fails BOTH gates reports the LEAKAGE.

    They are facts about different things. Read scope is about the CALLER — someone else may be
    permitted, and the remedy is a grant. Leakage is about the FEATURE — it is wrong for everyone,
    and the remedy is a different feature. Deciding read scope first would tell one operator "you
    may not read this column" and another "this feature leaks" about the same build, and only the
    second gets fixed once rather than per person.
    """
    _tag(catalog, "fx_rates", "rate", "restricted")      # would refuse READ_SCOPE_INSUFFICIENT
    planned = _planned(v1_ir, converted=True)            # ...and the rate column IS the target
    result = _run(catalog, [planned], authorization=_authorization(target_ref=FX_RATE))

    assert isinstance(result, MaterializationRefused), result
    assert result.code is CompilationRefusalCode.TARGET_LEAKAGE_DETECTED


def test_a_NON_LEAKING_build_still_meets_GATE_2(catalog, v1_ir):
    """The discriminator: leakage passing does not authorize anything by itself."""
    _tag(catalog, "fx_rates", "rate", "restricted")
    result = _run(catalog, [_planned(v1_ir, converted=True)])

    assert isinstance(result, MaterializationRefused), result
    assert result.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert FX_RATE in result.detail


# ══ EXPLORATION MAKES NO CLAIM ═════════════════════════════════════════════════════════════════
def test_an_EXPLORATION_BUILD_MAKES_NO_LEAKAGE_CLAIM(catalog, v1_ir):
    """No target, so nothing to leak — that is what the mode means. A passing verdict here would be
    a claim nobody earned, and the gate itself refuses a blank target rather than inventing one."""
    result = _run(catalog, [_planned(v1_ir)],
                  authorization=_authorization(mode=TargetModeV1.EXPLORATION))

    assert isinstance(result, AuthorizedGenerationV2), result
    assert result.leakage is None
    assert "No leakage claim" in result.leakage_claim


def test_a_PREDICTION_RESULT_WITHOUT_A_VERDICT_IS_UNCONSTRUCTIBLE(catalog, v1_ir):
    """Two fields that can disagree eventually do: it was authorized FOR a target, so "was the
    target read" is a question that was asked or the authorization is not one."""
    planned = _planned(v1_ir)
    token = _run(catalog, [planned]).token
    with pytest.raises(ValueError, match="carries no leakage verdict"):
        AuthorizedGenerationV2(token=token, authorization=_authorization(), leakage=None)


def test_an_EXPLORATION_RESULT_WITH_A_VERDICT_IS_UNCONSTRUCTIBLE(catalog, v1_ir):
    """The other direction, which a one-sided check would let through."""
    result = _run(catalog, [_planned(v1_ir)])
    with pytest.raises(ValueError, match="claims a check nobody could have performed"):
        AuthorizedGenerationV2(
            token=result.token,
            authorization=_authorization(mode=TargetModeV1.EXPLORATION),
            leakage=result.leakage)


# ══ WHAT A PASS PROVES TRAVELS WITH IT ═════════════════════════════════════════════════════════
def test_THE_NARROW_CLAIM_IS_CARRIED_not_documented(catalog, v1_ir):
    """Invariant 18. The failure mode of a leakage gate is a caller reading "admitted" as "safe",
    so the disclaimer is on the object rather than in a docstring nobody opened."""
    result = _run(catalog, [_planned(v1_ir)])

    assert "Does NOT prove" in result.leakage_claim
    assert "deterministic function of the target" in result.leakage_claim


# ══ CALLER ERRORS ══════════════════════════════════════════════════════════════════════════════
def test_an_EMPTY_BUILD_SET_is_a_caller_error(catalog, v1_ir):
    """Not a governed verdict: an empty group compiles nothing, and a token over it would authorize
    every empty group equally."""
    with pytest.raises(ValueError, match="no planned features"):
        authorize_generation_v2(catalog, [], spine=v1_ir.spine,
                                authorization=_authorization(), roles=_ROLES)
