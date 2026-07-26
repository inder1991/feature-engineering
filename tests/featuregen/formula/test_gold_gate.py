"""Child-1 Task 12 — the CURATED-FIXTURE gold gate (gate b), plus the §J scorer's own unit tests.

The plumbing suite (``test_authoring.py``) proves the wiring with a FakeLLM; it can never prove
QUALITY, because a FakeLLM says whatever the test tells it to. This suite adds the half that can:
an IMMUTABLE set of hand-authored ``(intent, expected TypedFormulaV1)`` fixtures in
``gold_fixtures/*.json``, each pinning the exact ``formula_content_hash`` a correctly-authored run
must reproduce.

WHY THESE FIXTURES ARE NOT TAUTOLOGICAL — the whole point of gate (b). Nothing in
``gold_fixtures/*.json`` was produced by the code under test:

* the ``canonical_json`` string is the §E canonical document written OUT BY HAND (JCS key order
  applied manually, refs already normalized, associative AND children ordered by their own hand-run
  child digests) — not dumped from ``featuregen.formula.canonical``;
* ``expected_formula_hash`` is ``sha256`` of exactly those hand-authored bytes, computed with plain
  ``hashlib`` — not with ``formula_content_hash``;
* the curated catalog refs are the orchestrator fixture's, deliberately NOT the ones
  ``tests/featuregen/formula/factories.py`` builds, so a fixture cannot have been copied out of a
  code factory (asserted below).

So the gate compares TWO INDEPENDENT derivations of the same identity: the hand-applied §E rules and
the shipped canonicalizer. If they disagree, one of them is wrong — which is exactly the question a
frozen identity contract needs answered, and exactly the question a fixture generated from the code
could never ask.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from tests.featuregen._helpers import make_actor
from tests.featuregen.formula.authoring_fixtures import seed_authoring_catalog

from featuregen.formula import authoring
from featuregen.formula.author import AUTHOR_TASK
from featuregen.formula.authoring import run_authoring
from featuregen.formula.canonical import canonical_json
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.gold import (
    GOLD_GATE_V1,
    REQUIRED_OPERATIONS,
    GoldCase,
    GoldGate,
    GoldOutcome,
    gate_failures,
    load_gold_cases,
    score_gold,
)
from featuregen.formula.result import AuthoringAxes, derive_disposition
from featuregen.formula.schema import AdditivityClass, FormulaOutputPolicyV1
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import FakeLLM, FakeResponse

_FIXTURE_DIR = Path(__file__).with_name("gold_fixtures")
_ACTOR = make_actor(subject="user:gold-gate", roles=("feature_engineer",))


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-HERMETIC: the write-once trace rows a durable run leaves behind can never be cleaned up
    (see ``test_trace.py``'s fixture of the same name)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


def _fixture_documents() -> list[dict]:
    docs = [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(_FIXTURE_DIR.glob("*.json"))]
    assert docs, "the curated gold set is empty"
    return docs


_DOCUMENTS = _fixture_documents()
_CASES: dict[str, GoldCase] = {
    case.case_id: case for case in load_gold_cases(_DOCUMENTS)}


def _run_case(db, doc: dict) -> GoldOutcome:
    """Author ONE curated case end to end and pair the result with its immutable expectation.

    The author is a scripted FakeLLM emitting the fixture's proposal, and the critic a scripted
    FakeLLM emitting the fixture's findings — so what is measured is everything BETWEEN them: parse,
    semantics, capability, the C1 output authority, the fold, and the §E identity.

    The ``delivery: direct`` patch is scoped to THIS case (its own ``MonkeyPatch`` context), not to
    the test: a stub that leaked into the next case would silently author every remaining fixture
    from the same raw dict."""
    seed_authoring_catalog(db)
    case = _CASES[doc["case_id"]]
    critic = FakeLLM(script={
        CRITIC_TASK: FakeResponse(output={"findings": list(doc.get("critic_findings") or [])})})
    with pytest.MonkeyPatch.context() as mp:
        if doc.get("delivery") == "direct":
            # The author's wire schema pins the operation enums, so this proposal can never arrive
            # THROUGH a provider call (see the fixture's own note). The raw dict is handed to the
            # orchestrator directly — the seam actually under test for this case.
            mp.setattr(authoring, "author_formula",
                       lambda *a, _raw=doc["proposal"], **k: (_raw, []))
            author = FakeLLM()
        else:
            author = FakeLLM(script={AUTHOR_TASK: FakeResponse(
                output={"turn_type": "final_proposal", "final_proposal": doc["proposal"]})})
        result = run_authoring(db, case.intent, author, critic, roles=(), actor=_ACTOR)
    return GoldOutcome(case=case, result=result)


# ── the fixtures are self-consistent WITHOUT the code under test ─────────────────────────────────

@pytest.mark.parametrize("doc", _DOCUMENTS, ids=lambda d: d["case_id"])
def test_the_curated_hash_is_sha256_of_the_curated_canonical_bytes(doc) -> None:
    """Half one of the independence proof, and it touches no production code at all: the pinned
    hash is the sha256 of the pinned canonical TEXT, computed here with plain ``hashlib``. A fixture
    whose hash was quietly refreshed from ``formula_content_hash`` while its canonical bytes stayed
    stale would fail HERE, before any authoring runs."""
    canonical = doc.get("canonical_json")
    if canonical is None:
        assert doc["expected_formula_hash"] is None    # no curated identity, no curated hash
        return
    assert doc["expected_formula_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert json.loads(canonical)                       # ...and it really is JSON


def test_the_fixtures_are_not_derived_from_the_code_factories() -> None:
    """Half two: the curated set stands on its own catalog, NOT on
    ``tests/featuregen/formula/factories.py``'s. A fixture copied out of a factory would carry that
    module's refs; none do. (The factories exist for canonicalization unit tests — reusing their
    output as a gold expectation would make the gate assert the code agrees with itself.)"""
    from tests.featuregen.formula import factories
    blob = json.dumps(_DOCUMENTS)
    for factory_ref in (factories.TABLE_REF, factories.AMOUNT_REF, factories.EVENT_TIME_REF,
                        factories.CIF_KEY_REF):
        assert factory_ref not in blob
    assert not list(_FIXTURE_DIR.glob("*.py"))         # curated DATA only — no code in the set


def test_the_curated_set_covers_every_required_operation() -> None:
    """§J: at least one curated POSITIVE per operation. A gold set missing one proves nothing about
    it, and ``gate_failures`` refuses that vacuous pass — this asserts the set itself is complete."""
    positives = {doc["operation"] for doc in _DOCUMENTS if doc.get("expected_formula_hash")}
    assert positives == set(REQUIRED_OPERATIONS)
    assert any(doc.get("expects_blocking_critic") for doc in _DOCUMENTS)
    assert {doc["expected_disposition"] for doc in _DOCUMENTS} >= {
        "RESOLVED", "UNSUPPORTED", "REJECTED", "NEEDS_REVIEW"}


# ── the curated cases, authored end to end ───────────────────────────────────────────────────────

@pytest.mark.parametrize("doc", _DOCUMENTS, ids=lambda d: d["case_id"])
def test_each_curated_case_authors_to_its_curated_expectation(db, doc) -> None:
    """The gate proper: a correctly-authored formula's ``formula_content_hash`` EQUALS the fixture's,
    and its canonical bytes are the hand-authored ones — two independent derivations agreeing."""
    outcome = _run_case(db, doc)
    result = outcome.result

    assert result.authoring_disposition == doc["expected_disposition"]
    for axis, expected in (doc.get("expected_axes") or {}).items():
        assert getattr(result, axis) == expected, axis

    expected_policy = doc.get("expected_output_policy")
    if expected_policy is None:
        assert result.candidate_formula is None
    else:
        assert result.candidate_formula is not None
        assert dataclasses.asdict(result.candidate_formula.output) == expected_policy

    if doc.get("canonical_json") is not None:
        assert canonical_json(result.candidate_formula) == doc["canonical_json"]
        assert result.candidate_formula_hash == doc["expected_formula_hash"]

    requirements = doc.get("expected_output_requirements")
    if requirements is not None:
        assert [r.requirement for r in result.output_requirements] == requirements


def test_the_whole_curated_set_passes_gold_gate_v1(db) -> None:
    """§J's pinned v1 bar over the WHOLE set — every threshold must hold at once (false-resolve 0,
    operand-preservation 1.0, exact matches 1.0 covering all six operations, classifications 1.0,
    blocking-critic recall 1.0, technical failures in the clean population 0)."""
    outcomes = [_run_case(db, doc) for doc in _DOCUMENTS]
    score = score_gold(outcomes)

    assert gate_failures(score, GOLD_GATE_V1) == (), score.failures
    assert score.failures == ()
    assert score.false_resolve_rate == 0.0
    assert score.operand_preservation_rate == 1.0
    assert score.exact_match_rate == 1.0
    assert score.classification_rate == 1.0
    assert score.blocking_critic_recall == 1.0
    assert score.technical_failure_rate_clean == 0.0
    assert score.operations_exactly_matched == REQUIRED_OPERATIONS
    assert score.observations == len(_DOCUMENTS)


# ── the scorer itself (pure, no DB) ──────────────────────────────────────────────────────────────

def _case(**kwargs) -> GoldCase:
    base = {
        "case_id": "c", "operation": "sum",
        "intent": AuthoringIntent(name="n", hypothesis="h", target_entity="customer"),
        "expected_disposition": "RESOLVED",
    }
    return GoldCase(**{**base, **kwargs})


def _result(disposition: str, **kwargs):
    """A real ``AuthoringResult`` for a scorer test — built through ``derive_disposition``, so even
    the fixtures of the scorer's own tests cannot be incoherent."""
    axes = {
        "RESOLVED": AuthoringAxes("ok", "ok", "resolved", "not_provided", "clean", "ok"),
        "NEEDS_REVIEW": AuthoringAxes("ok", "ok", "resolved", "not_provided", "blocking", "ok"),
        "UNSUPPORTED": AuthoringAxes("ok", "unsupported_capability", "resolved", "not_provided",
                                     "clean", "ok"),
        "REJECTED": AuthoringAxes("invalid_formula", "ok", "resolved", "not_provided", "clean",
                                  "ok"),
        "TECHNICAL_FAILURE": AuthoringAxes("ok", "ok", "resolved", "not_provided", "clean",
                                          "technical_failure"),
    }[disposition]
    from tests.featuregen.formula.factories import base_formula
    needs_formula = disposition in ("RESOLVED", "NEEDS_REVIEW")
    return derive_disposition(
        axes, authoring_run_id="arun_scorer",
        candidate_formula=base_formula() if needs_formula else None, **kwargs)


def test_a_false_resolve_is_caught_and_named() -> None:
    score = score_gold([GoldOutcome(case=_case(expected_disposition="UNSUPPORTED"),
                                    result=_result("RESOLVED"))])
    assert score.false_resolve_rate == 1.0
    assert score.false_resolves == 1
    assert any("FALSE RESOLVE" in f for f in score.failures)
    assert gate_failures(score) != ()


def test_a_dropped_operand_is_caught() -> None:
    """The failure no structural gate can see: a formula that aggregates a DIFFERENT column."""
    case = _case(expected_operands=("authored::public.txns.txn_amt",))
    score = score_gold([GoldOutcome(case=case, result=_result("RESOLVED"))])
    assert score.operand_preservation_rate == 0.0
    assert any("operands dropped" in f for f in score.failures)


def test_a_preserved_operand_scores_one() -> None:
    from tests.featuregen.formula import factories
    case = _case(expected_operands=(factories.AMOUNT_REF, factories.CIF_KEY_REF))
    score = score_gold([GoldOutcome(case=case, result=_result("RESOLVED"))])
    assert score.operand_preservation_rate == 1.0


def test_operand_preservation_is_case_insensitive_on_refs() -> None:
    """A ref differing only in casing is the SAME column (§E normalization) — not a dropped one."""
    from tests.featuregen.formula import factories
    case = _case(expected_operands=(factories.AMOUNT_REF.upper(),))
    score = score_gold([GoldOutcome(case=case, result=_result("RESOLVED"))])
    assert score.operand_preservation_rate == 1.0


def test_a_wrong_hash_fails_exact_match() -> None:
    case = _case(expected_formula_hash="0" * 64)
    score = score_gold([GoldOutcome(case=case, result=_result("RESOLVED"))])
    assert score.exact_match_rate == 0.0
    assert score.operations_exactly_matched == frozenset()
    assert any("formula_content_hash" in f for f in score.failures)


def test_the_same_disposition_from_the_wrong_axis_is_not_a_correct_classification() -> None:
    """``unsupported_operation`` and ``unsupported_capability`` BOTH fold to UNSUPPORTED. A curated
    case that means one must not be satisfied by the other — so the axes are checked, not just the
    disposition."""
    case = _case(expected_disposition="UNSUPPORTED",
                 expected_axes={"structural_status": "unsupported_operation"})
    score = score_gold([GoldOutcome(case=case, result=_result("UNSUPPORTED"))])
    assert score.classification_rate == 0.0
    assert score.disposition_accuracy_rate == 0.0
    assert any("structural_status" in f for f in score.failures)


def test_a_missed_blocking_critic_fails_recall() -> None:
    case = _case(expected_disposition="RESOLVED", expects_blocking_critic=True, adversarial=True)
    score = score_gold([GoldOutcome(case=case, result=_result("RESOLVED"))])
    assert score.blocking_critic_recall == 0.0
    assert any("did NOT block" in f for f in score.failures)


def test_a_technical_failure_on_a_clean_case_fails_the_gate() -> None:
    case = _case(expected_disposition="TECHNICAL_FAILURE", adversarial=False)
    score = score_gold([GoldOutcome(case=case, result=_result("TECHNICAL_FAILURE"))])
    assert score.technical_failure_rate_clean == 1.0
    assert gate_failures(score) != ()
    # ...but the same outcome on an ADVERSARIAL case is outside the clean population
    adversarial = score_gold([GoldOutcome(case=_case(expected_disposition="TECHNICAL_FAILURE",
                                                     adversarial=True),
                                          result=_result("TECHNICAL_FAILURE"))])
    assert adversarial.technical_failure_rate_clean == 0.0


def test_an_empty_gold_set_can_never_pass_the_gate() -> None:
    """Every rate is vacuously perfect over nothing. The coverage bars are what stop that being a
    pass — a gold gate that green-lights an EMPTY curated set measures nothing at all."""
    score = score_gold([])
    assert score.observations == 0
    assert score.exact_match_rate == 1.0 and score.blocking_critic_recall == 1.0
    misses = gate_failures(score)
    assert any("no exact positive match" in m for m in misses)
    assert any("blocking-critic case" in m for m in misses)


def test_repeated_runs_are_scored_as_separate_observations() -> None:
    """The provider evaluation runs each case repeatedly; a model that is right 1 time in 2 must
    score 0.5, not 1.0 — metrics are over OBSERVATIONS, never de-duplicated to cases."""
    case = _case(expected_formula_hash="0" * 64)
    from tests.featuregen.formula.factories import base_formula

    from featuregen.formula.canonical import formula_content_hash
    good = _case(expected_formula_hash=formula_content_hash(base_formula()))
    score = score_gold([GoldOutcome(case=good, result=_result("RESOLVED")),
                        GoldOutcome(case=case, result=_result("RESOLVED"))])
    assert score.exact_match_cases == 2
    assert score.exact_match_rate == 0.5
    assert gate_failures(score) != ()


def test_gold_gate_v1_pins_the_spec_thresholds() -> None:
    """§J's v1 numbers are a POLICY, not an implementation detail: they are asserted literally so a
    silent loosening is a test change a reviewer must see."""
    assert GOLD_GATE_V1.name == "gold_gate_v1"
    assert GOLD_GATE_V1.max_false_resolve_rate == 0.0
    assert GOLD_GATE_V1.min_operand_preservation_rate == 1.0
    assert GOLD_GATE_V1.min_blocking_critic_recall == 1.0
    assert GOLD_GATE_V1.max_technical_failure_rate_clean == 0.0
    assert GOLD_GATE_V1.min_exact_match_rate == 1.0
    assert GOLD_GATE_V1.min_classification_rate == 1.0
    assert GOLD_GATE_V1.required_operations == frozenset(
        {"sum", "count_rows", "count_non_null", "count_distinct", "ratio", "difference"})
    assert isinstance(GOLD_GATE_V1, GoldGate)
    with pytest.raises(dataclasses.FrozenInstanceError):
        GOLD_GATE_V1.max_false_resolve_rate = 0.5


def test_an_unused_import_guard_keeps_the_policy_shape() -> None:
    """``FormulaOutputPolicyV1``/``AdditivityClass`` are the vocabulary the curated
    ``expected_output_policy`` blocks are written in; this pins the field set so a fixture cannot
    silently stop asserting one of them."""
    fields = {f.name for f in dataclasses.fields(FormulaOutputPolicyV1)}
    assert fields == {"output_type", "unit", "currency", "output_additivity",
                      "external_type_required"}
    for doc in _DOCUMENTS:
        policy = doc.get("expected_output_policy")
        if policy is not None:
            assert set(policy) == fields
            assert policy["output_additivity"] in {c.value for c in AdditivityClass}
