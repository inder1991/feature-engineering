"""Child-1 Task 12 — the KEY-GATED real-provider evaluation (gold gate c). NOT a CI test.

The other two gates are hermetic and prove different things. ``test_authoring.py`` drives a FakeLLM
that says whatever the test scripts, so it proves PLUMBING and can never prove quality.
``test_gold_gate.py`` scores a GIVEN proposal against a hand-authored identity, so it proves the
deterministic half — parse, semantics, capability, C1 authority, the fold, the §E hash — but the
proposal itself is curated, so it never asks whether a real model can AUTHOR one.

This file asks exactly that, against a live provider, and scores the answer with the SAME
:func:`~featuregen.formula.gold.score_gold` and the SAME :data:`GOLD_GATE_V1` thresholds. It is an
ENABLEMENT gate (§J): run it deliberately, before trusting the loop against a model or a prompt
change — never on every commit. TWO independent locks keep it out of default runs:

* ``pytestmark = pytest.mark.eval`` — ``pyproject.toml``'s ``addopts`` carry ``-m 'not eval'``, so a
  plain ``pytest`` never collects it;
* ``skipif`` on ``ANTHROPIC_API_KEY`` — so even an explicit ``-m eval`` skips cleanly without a key.

Run it::

    ANTHROPIC_API_KEY=... FEATUREGEN_LLM_PROVIDER=anthropic \\
      uv run pytest -m eval tests/featuregen/formula/test_provider_eval.py -q

    # more repeats per case (default 2) — variance is the thing being measured:
    FEATUREGEN_PROVIDER_EVAL_REPEATS=5 ... uv run pytest -m eval ...

WHAT IT COVERS, and WHY the cases differ from the curated ones. A curated fixture pins the identity
of a GIVEN proposal; a free-authoring model is not told the deployment's NON-SEMANTIC policy fields
(window bounds, timezone, decimal precision/scale, empty-window and null-input behaviour), so
holding it to a byte-identical ``formula_content_hash`` would measure prompt luck, not authoring
quality. So the exact-match and per-operation-coverage bars stay the CURATED gate's job (their
denominators are empty here, which is why ``PROVIDER_GATE`` only relaxes ``required_operations`` —
every other threshold is ``GOLD_GATE_V1`` verbatim), and what THIS gate measures is:

* all six §B operations authored from an intent, each expected to reach RESOLVED;
* OPERAND PRESERVATION — the columns the intent names survive into the authored artifact. The
  failure mode no structural gate can see is a formula that quietly aggregates a different column;
* two FALSE-RESOLVE TRAPS — an operand whose type the catalog has NOT governed, and an intent whose
  column does not exist at all. Both are perfectly authorable and neither may reach RESOLVED;
* BLOCKING-CRITIC RECALL — the real LLM-2, given ONLY the intent and a curated wrong-slot proposal
  (never the author's reasoning), must block it. The proposal is valid, single-source and fully
  resolvable; the only thing wrong with it is that it does not answer the question;
* REPEATED RUNS — every case runs ``FEATUREGEN_PROVIDER_EVAL_REPEATS`` times and each run is a
  separate observation, so a model that is right four times in five scores 0.8, not 1.0.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest
from tests.featuregen._helpers import make_actor
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_FEE,
    REF_MERCHANT,
    REF_STATUS,
    REF_UNGOVERNED,
    seed_authoring_catalog,
)

from featuregen.formula import authoring
from featuregen.formula.authoring import run_authoring
from featuregen.formula.gold import (
    GOLD_GATE_V1,
    REQUIRED_OPERATIONS,
    GoldCase,
    GoldOutcome,
    gate_failures,
    score_gold,
)
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm

pytestmark = pytest.mark.eval

_ACTOR = make_actor(subject="user:provider-eval", roles=("feature_engineer",))
_REPEATS = int(os.environ.get("FEATUREGEN_PROVIDER_EVAL_REPEATS", "2"))
_WRONG_SLOT_FIXTURE = Path(__file__).with_name("gold_fixtures") / "10_blocking_critic_wrong_slot.json"

_WINDOW_HINT = (
    " Use a trailing 90-day window anchored on the transaction date, at customer grain.")


def _intent(name: str, hypothesis: str) -> AuthoringIntent:
    return AuthoringIntent(name=name, hypothesis=hypothesis + _WINDOW_HINT,
                           target_entity="customer", target_grain_keys=(REF_CIF,))


#: The six §B operations, each expected to be AUTHORED from an intent and reach RESOLVED, plus the
#: operands the intent names (which must survive into whatever artifact the run carries).
_POSITIVE_CASES: tuple[GoldCase, ...] = (
    GoldCase(
        case_id="provider_sum", operation="sum",
        intent=_intent("txn_amt_sum_90d",
                       "Total value of a customer's transactions predicts churn."),
        expected_disposition="RESOLVED",
        expected_axes={"output_status": "resolved", "technical_status": "ok"},
        expected_operands=(REF_AMT, REF_CIF)),
    GoldCase(
        case_id="provider_count_rows", operation="count_rows",
        intent=_intent("txn_count_90d",
                       "How many transactions a customer makes predicts churn. Count the "
                       "transaction ROWS themselves, not the values of any column."),
        expected_disposition="RESOLVED",
        expected_axes={"output_status": "resolved", "technical_status": "ok"},
        expected_operands=(REF_CIF,)),
    GoldCase(
        case_id="provider_count_non_null", operation="count_non_null",
        intent=_intent("merchant_attributed_txn_count_90d",
                       "How many of a customer's transactions carry a known merchant "
                       "(a non-null merchant identifier) predicts churn."),
        expected_disposition="RESOLVED",
        expected_axes={"output_status": "resolved", "technical_status": "ok"},
        expected_operands=(REF_MERCHANT, REF_CIF)),
    GoldCase(
        case_id="provider_count_distinct", operation="count_distinct",
        intent=_intent("distinct_merchant_count_90d",
                       "How many DISTINCT merchants a customer transacts with predicts churn."),
        expected_disposition="RESOLVED",
        expected_axes={"output_status": "resolved", "technical_status": "ok"},
        expected_operands=(REF_MERCHANT, REF_CIF)),
    GoldCase(
        case_id="provider_ratio", operation="ratio",
        intent=_intent("posted_amount_share_90d",
                       "The SHARE of a customer's transaction value that is posted (status code "
                       "'posted'), as a ratio of posted value to total value, predicts churn."),
        expected_disposition="RESOLVED",
        expected_axes={"output_status": "resolved", "technical_status": "ok"},
        expected_operands=(REF_AMT, REF_STATUS, REF_CIF)),
    GoldCase(
        case_id="provider_difference", operation="difference",
        intent=_intent("net_txn_amount_90d",
                       "A customer's transaction value NET OF FEES — total transaction amount "
                       "minus total fee amount — predicts churn."),
        expected_disposition="RESOLVED",
        expected_axes={"output_status": "resolved", "technical_status": "ok"},
        expected_operands=(REF_AMT, REF_FEE, REF_CIF)),
)

#: The FALSE-RESOLVE traps. Both are perfectly authorable; neither may reach RESOLVED, because the
#: catalog has granted no type authority over what they aggregate.
_TRAP_CASES: tuple[GoldCase, ...] = (
    GoldCase(
        case_id="provider_trap_ungoverned_type", operation="sum",
        intent=_intent("ungoverned_amount_sum_90d",
                       "The total of the ungoverned_amt column on transactions predicts churn."),
        expected_disposition="NEEDS_REVIEW",
        expected_axes={"output_status": "external_requirement"},
        expected_operands=(REF_UNGOVERNED,),
        adversarial=True),
    GoldCase(
        case_id="provider_trap_absent_column", operation="sum",
        intent=_intent("loan_balance_sum_90d",
                       "A customer's total outstanding LOAN BALANCE predicts churn. (There is no "
                       "such column in this catalog.)"),
        expected_disposition="NEEDS_REVIEW",
        expected_axes={"technical_status": "ok"},
        adversarial=True),
)

#: BLOCKING-CRITIC RECALL. The author is replaced by the CURATED wrong-slot proposal so the REAL
#: LLM-2 is the only thing under test: given the intent and that proposal (and never the author's
#: reasoning), does it block? The proposal is valid, single-source and resolves cleanly — a critic
#: that clears it has added nothing.
_CRITIC_CASE = GoldCase(
    case_id="provider_critic_wrong_slot", operation="ratio",
    intent=_intent("fee_share_of_value_90d",
                   "Fees as a SHARE OF transaction value — total fees divided by total transaction "
                   "value — predicts churn."),
    expected_disposition="NEEDS_REVIEW",
    expected_axes={"critic_status": "blocking", "output_status": "resolved"},
    expected_operands=(REF_AMT, REF_FEE, REF_CIF),
    expects_blocking_critic=True, adversarial=True)

_ALL_CASES: tuple[GoldCase, ...] = _POSITIVE_CASES + _TRAP_CASES + (_CRITIC_CASE,)

#: ``GOLD_GATE_V1`` verbatim except ``required_operations``: that bar counts operations proven by an
#: EXACT curated hash match, which a free-authoring run has no curated identity to match (module
#: docstring). The exact-match and classification rates are likewise vacuous here — their
#: denominators are empty — so the thresholds this gate actually applies are §J's four pinned ones
#: (false-resolve 0, operand-preservation 1.0, blocking-critic recall 1.0, technical failures in the
#: clean population 0) plus disposition accuracy.
PROVIDER_GATE = dataclasses.replace(
    GOLD_GATE_V1, name="gold_gate_v1_provider", required_operations=frozenset())


def _clients():
    """The author and the critic get SEPARATE client objects — LLM-2 is a second opinion, and the
    seam that makes it one is a distinct context on a distinct client, not a shared conversation."""
    config = ClaudeConfig.from_env()
    if not config.enabled:
        config = dataclasses.replace(config, enabled=True)
    return build_claude_llm(config), build_claude_llm(config)


def _wrong_slot_proposal() -> dict:
    return json.loads(_WRONG_SLOT_FIXTURE.read_text(encoding="utf-8"))["proposal"]


def _run(db, case: GoldCase) -> GoldOutcome:
    author, critic = _clients()
    with pytest.MonkeyPatch.context() as mp:
        if case.expects_blocking_critic:
            curated = _wrong_slot_proposal()
            mp.setattr(authoring, "author_formula",
                       lambda *a, _raw=curated, **k: (_raw, []))
        result = run_authoring(db, case.intent, author, critic, roles=(), actor=_ACTOR)
    return GoldOutcome(case=case, result=result)


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="live provider required")
def test_the_authoring_loop_meets_gold_gate_v1_against_a_live_provider(db, monkeypatch) -> None:
    """Author every case ``_REPEATS`` times against the real provider and score the whole run.

    A red result names the case and the metric (``score.failures``), because the useful output of an
    enablement gate is WHICH behaviour regressed, not that a number moved."""
    monkeypatch.setenv("FEATUREGEN_LLM_PROVIDER", "anthropic")
    seed_authoring_catalog(db)

    outcomes = [_run(db, case) for _ in range(_REPEATS) for case in _ALL_CASES]
    score = score_gold(outcomes)

    assert score.observations == _REPEATS * len(_ALL_CASES)
    assert gate_failures(score, PROVIDER_GATE) == (), (
        "gold gate v1 NOT met against the live provider:\n  "
        + "\n  ".join(score.failures))
    # §J's four pinned thresholds, asserted literally as well as through the gate, so a relaxation
    # of GOLD_GATE_V1 cannot silently relax THIS test too.
    assert score.false_resolve_rate == 0.0
    assert score.operand_preservation_rate == 1.0
    assert score.blocking_critic_recall == 1.0
    assert score.technical_failure_rate_clean == 0.0


def test_the_provider_case_set_covers_every_required_operation() -> None:
    """Hermetic (no key, no DB): the case set itself must exercise all six §B operations and carry
    at least one blocking-critic case and one false-resolve trap. An eval that quietly stopped
    covering an operation would otherwise still report a perfect score."""
    assert {case.operation for case in _POSITIVE_CASES} == set(REQUIRED_OPERATIONS)
    assert sum(case.expects_blocking_critic for case in _ALL_CASES) >= 1
    assert sum(case.adversarial for case in _ALL_CASES) >= 2
    assert all(case.expected_disposition != "RESOLVED"
               for case in _TRAP_CASES + (_CRITIC_CASE,))
    assert _REPEATS >= 1


def test_the_provider_gate_relaxes_only_the_curated_identity_bar() -> None:
    """The provider gate must not become a weaker gate by accident: every §J threshold is
    ``GOLD_GATE_V1``'s, and ``required_operations`` is the ONLY field that differs."""
    differing = {f.name for f in dataclasses.fields(PROVIDER_GATE)
                 if getattr(PROVIDER_GATE, f.name) != getattr(GOLD_GATE_V1, f.name)}
    assert differing == {"name", "required_operations"}
    assert PROVIDER_GATE.max_false_resolve_rate == 0.0
    assert PROVIDER_GATE.min_operand_preservation_rate == 1.0
    assert PROVIDER_GATE.min_blocking_critic_recall == 1.0
    assert PROVIDER_GATE.max_technical_failure_rate_clean == 0.0
    assert PROVIDER_GATE.min_blocking_critic_cases >= 1


def test_this_suite_can_never_run_in_default_ci() -> None:
    """Belt AND braces, asserted rather than documented: the module carries the ``eval`` marker that
    ``pyproject.toml``'s ``addopts = -m 'not eval'`` excludes, AND the live test is ``skipif``-gated
    on the API key. Either lock alone would do; a regression in one must not silently arm the file."""
    import tomllib
    pyproject = next(
        parent / "pyproject.toml" for parent in Path(__file__).resolve().parents
        if (parent / "pyproject.toml").exists())
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert "-m 'not eval'" in config["tool"]["pytest"]["ini_options"]["addopts"]
    assert pytestmark.name == "eval"
    marks = getattr(test_the_authoring_loop_meets_gold_gate_v1_against_a_live_provider,
                    "pytestmark", [])
    assert any(mark.name == "skipif" for mark in marks)
