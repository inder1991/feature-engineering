"""The fixture CONSTRUCTION CHECK — proof that the three worked features are not forgeries.

A hand-authored ``TypedFormulaV1`` is an assertion about what Child-1 resolves. Spec §13: *"a
fixture claiming ADDITIVE for a plain SUM is a forgery, since Child-1 resolves NON_ADDITIVE without
path_additive, and COUNT_DISTINCT resolves NON_ADDITIVE with logical type integer."* This file turns
that assertion into a test by driving the REAL orchestrator over the REAL governed catalog and
comparing what it produces with what ``fixtures.py`` claims.

If this file fails, the fixture is wrong — not the resolver.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import make_actor
from tests.featuregen.materialize.fixtures import (
    FEATURE_NAMES,
    authored_formula,
    intent_for,
    raw_proposal,
    rejected_raw_proposal,
    seed_materialize_catalog,
)

from featuregen.formula.author import AUTHOR_TASK
from featuregen.formula.authoring import run_authoring
from featuregen.formula.canonical import formula_content_hash
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.schema import AdditivityClass
from featuregen.intake.llm import FakeLLM, FakeResponse

_ACTOR = make_actor(subject="user:materialize-author", roles=("feature_engineer",))


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-HERMETIC (same rationale as ``formula/test_authoring.py``): the write-once trace rows a
    durable connection commits can physically never be cleaned up between runs."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


@pytest.fixture
def catalog(db):
    seed_materialize_catalog(db)
    return db


def _author(raw: dict) -> FakeLLM:
    return FakeLLM(script={AUTHOR_TASK: FakeResponse(
        output={"turn_type": "final_proposal", "final_proposal": raw})})


def _critic() -> FakeLLM:
    return FakeLLM(script={CRITIC_TASK: FakeResponse(output={"findings": []})})


def _run(db, name: str):
    return run_authoring(db, intent_for(name), _author(raw_proposal(name)), _critic(),
                         roles=(), actor=_ACTOR)


@pytest.mark.parametrize("name", FEATURE_NAMES)
def test_every_fixture_is_what_child1_really_resolves(catalog, name) -> None:
    """The hand-authored formula IS the one the real authoring path produces — structurally AND by
    content hash, which is the equality Gate 1 check 4 performs."""
    result = _run(catalog, name)

    assert result.authoring_disposition == "RESOLVED", result.authority_failures
    assert result.candidate_formula == authored_formula(name)
    assert result.candidate_formula_hash == formula_content_hash(authored_formula(name))


def test_a_plain_sum_is_non_additive(catalog) -> None:
    """The single most tempting forgery: a SUM *looks* additive. Child-1 says otherwise without a
    ``path_additive`` proof, and the resolver has no proof to give at resolve time."""
    result = _run(catalog, "total_debit_amount_30d")

    assert result.candidate_formula is not None
    assert result.candidate_formula.output.output_additivity is AdditivityClass.NON_ADDITIVE
    assert result.candidate_formula.output.output_type == "numeric"
    assert result.candidate_formula.output.external_type_required is False


def test_count_distinct_is_non_additive_and_integer(catalog) -> None:
    result = _run(catalog, "distinct_merchant_count_90d")

    assert result.candidate_formula is not None
    assert result.candidate_formula.output.output_additivity is AdditivityClass.NON_ADDITIVE
    assert result.candidate_formula.output.output_type == "integer"


def test_a_ratio_is_non_additive_and_decimal(catalog) -> None:
    result = _run(catalog, "cross_border_value_ratio_90d")

    assert result.candidate_formula is not None
    assert result.candidate_formula.output.output_additivity is AdditivityClass.NON_ADDITIVE
    assert result.candidate_formula.output.output_type == "decimal"
    assert result.candidate_formula.output.unit is None
    assert result.candidate_formula.output.currency is None


def test_the_rejected_proposal_really_is_rejected(catalog) -> None:
    """The negative fixture Gate 1's ``NOT_RESOLVED`` test depends on — and the proof of the trap:
    a REJECTED run carries no formula, yet its terminal trace event is ``COMPLETED``."""
    result = run_authoring(catalog, intent_for("total_debit_amount_30d"),
                           _author(rejected_raw_proposal()), _critic(), roles=(), actor=_ACTOR)

    assert result.authoring_disposition == "REJECTED"
    assert result.candidate_formula is None
