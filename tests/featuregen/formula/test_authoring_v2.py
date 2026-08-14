"""Task A3 — the Formula-v2 authoring ORCHESTRATOR: deterministic plumbing/conformance.

Modelled on ``test_authoring.py`` and proving the same thing for the v2 sibling: the WIRING, with
every provider call a scripted ``FakeLLM``. What is under test is that each stage is invoked with
the contract it declares, and that D-3's four invariants hold over the v2 types:

1. every :class:`AuthoringResultV2` is built by ``derive_disposition_v2`` (asserted over the AST,
   so prose cannot satisfy it);
2. a proposal outside the v2 GRAMMAR is UNSUPPORTED, never REJECTED;
3. a provider failure — **including a billing refusal** — is TECHNICAL, never a capability or a
   schema verdict, and carries no authored artifact;
4. the v1 orchestrator is byte-identical: same result, same trace, same versions manifest.

⟨LLM⟩ **The live half of A3 is deliberately absent.** Anthropic billing is exhausted (charter
D-10), so this suite drives the orchestrator through recorded-fixture clients only; A3 is shipped
*unverified against a live provider*. Test 3 is the guard that makes that deferral safe rather than
convenient: whatever a real provider does when the account cannot pay, the platform records a
technical failure and never a verdict about the v2 grammar.
"""
from __future__ import annotations

import ast
import itertools
from pathlib import Path
from typing import get_args

import pytest
from tests.featuregen._helpers import make_actor
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_DT,
    REF_FEE,
    REF_UNGOVERNED,
    TABLE_REF,
    seed_authoring_catalog,
)

from featuregen.formula import authoring_v2
from featuregen.formula.author import (
    AUTHOR_INSTRUCTION,
    AUTHOR_INSTRUCTION_V2,
    AUTHOR_PROMPT_ID,
    AUTHOR_PROMPT_ID_V2,
    AUTHOR_TASK,
    AUTHOR_TURN_CONTRACT_V1,
    AUTHOR_TURN_CONTRACT_V2,
)
from featuregen.formula.authoring_v2 import (
    AUTHORING_VERSIONS_V2,
    classify_output_policy_v2,
    forbids_authored_artifact_v2,
    run_authoring_v2,
)
from featuregen.formula.canonical_v2 import proposal_content_hash_v2
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2, InvalidOutputV2
from featuregen.formula.result import (
    AuthoringAxes,
    CapabilityStatus,
    CriticStatus,
    ExpectationStatus,
    IncoherentResultError,
    OutputStatus,
    StructuralStatus,
    TechnicalStatus,
)
from featuregen.formula.result import _fold as _fold_v1
from featuregen.formula.result_v2 import derive_disposition_v2
from featuregen.formula.schema import AdditivityClass
from featuregen.formula.trace import run_status
from featuregen.formula.turns import AuthoringIntent
from featuregen.formula.turns_v2 import AUTHOR_TURN_SCHEMA_ID_V2, AUTHOR_TURN_V2_SCHEMA
from featuregen.intake.llm import PROVIDER_NON_RETRYABLE, FakeLLM, FakeResponse

_ACTOR = make_actor(subject="user:formula-author-v2", roles=("feature_engineer",))

_INTENT = AuthoringIntent(
    name="txn_amt_sum_90d_v2",
    hypothesis="Total transaction amount over a trailing 90-day window predicts churn.",
    target_entity="customer",
    target_grain_keys=(REF_CIF,),
)


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-HERMETIC, the same rationale ``test_authoring.py`` states: with an ambient
    ``FEATUREGEN_DSN`` the trace COMMITs on the durable fresh connection and
    ``authoring_trace_event`` rows can physically never be cleaned up."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


# ── raw v2 proposal builders (the shapes a scripted author "emits") ──────────────────────────────

def _window_v2(event_time_ref: str = REF_DT, **overrides) -> dict:
    return {"event_time_ref": event_time_ref, "basis": "trailing", "length": 90, "unit": "day",
            "start_inclusive": "inclusive", "end_inclusive": "exclusive",
            "timezone": "Asia/Dubai", "empty_window": "null", "null_input": "ignore",
            "offset_periods": 0, **overrides}


def _expr_v2(aggregation: str = "sum", operand: str | None = REF_AMT, *,
             table_ref: str = TABLE_REF, event_time_ref: str = REF_DT, **overrides) -> dict:
    return {"aggregation": aggregation, "operand": operand,
            "source_relation": {"table_ref": table_ref}, "filter": None,
            "window": _window_v2(event_time_ref), "aggregation_argument": None,
            "second_operand": None, "authority_refs": None, **overrides}


def _raw_v2(body: dict | None = None, *, expected_output: dict | None = None,
            grain_keys: tuple[str, ...] = (REF_CIF,), version: int = 2) -> dict:
    return {"formula_schema_version": version, "operation_grammar_version": 1,
            "canonicalization_version": 1,
            "grain": {"entity": "customer", "keys": list(grain_keys)},
            "body": body if body is not None else {
                "final_operation": "identity", "expr": _expr_v2()},
            "parameters": [],
            "decimal": {"precision": 38, "scale": 6, "rounding": "half_even",
                        "overflow": "error"},
            "expected_output": expected_output,
            "allocation_policy_ref": ""}


def _author_client(raw: dict) -> FakeLLM:
    return FakeLLM(script={AUTHOR_TASK: FakeResponse(
        output={"turn_type": "final_proposal", "final_proposal": raw})})


def _critic_client(findings: list[dict] | None = None, **kwargs) -> FakeLLM:
    return FakeLLM(script={
        CRITIC_TASK: FakeResponse(output={"findings": list(findings or [])}, **kwargs)})


def _run(db, raw: dict | None = None, *, findings=None, intent: AuthoringIntent = _INTENT,
         critic: FakeLLM | None = None, author: FakeLLM | None = None, roles=()):
    return run_authoring_v2(
        db, intent,
        author if author is not None else _author_client(raw if raw is not None else _raw_v2()),
        critic if critic is not None else _critic_client(findings),
        roles=roles, actor=_ACTOR)


def _events(db, run_id: str) -> list[tuple]:
    return db.execute(
        "SELECT seq, kind, payload FROM authoring_trace_event "
        "WHERE authoring_run_id = %s ORDER BY seq", (run_id,)).fetchall()


# ── invariant 1: the result is never constructed by hand ─────────────────────────────────────────

def _type_checking_imports(tree: ast.Module) -> set[str]:
    guarded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom):
                    guarded.update(alias.asname or alias.name for alias in child.names)
    return guarded


def _runtime_imports(tree: ast.Module) -> set[str]:
    every = {alias.asname or alias.name
             for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
             for alias in node.names}
    return every - _type_checking_imports(tree)


def test_run_authoring_v2_never_constructs_AuthoringResult() -> None:
    """D-3 invariant 2, restated over the v2 types and asserted over the AST: the coherence
    raise-guards live in ``derive_disposition_v2``, and a hand-built result bypasses ALL of them.
    Neither result type may exist at runtime in this module."""
    tree = ast.parse(Path(authoring_v2.__file__).read_text(encoding="utf-8"))
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "AuthoringResultV2" not in called
    assert "AuthoringResult" not in called
    assert "derive_disposition_v2" in called

    runtime = _runtime_imports(tree)
    assert "AuthoringResultV2" not in runtime
    assert "AuthoringResult" not in runtime
    assert "AuthoringResultV2" in _type_checking_imports(tree)   # the annotation is real
    assert not hasattr(authoring_v2, "AuthoringResultV2")


def test_the_v2_fold_is_the_v1_fold_over_every_axis_combination() -> None:
    """The §F precedence is RESTATED in ``result_v2``, never re-decided. Pinned over the complete
    cross-product so the restatement cannot drift into a second policy."""
    combos = list(itertools.product(
        get_args(StructuralStatus), get_args(CapabilityStatus), get_args(OutputStatus),
        get_args(ExpectationStatus), get_args(CriticStatus), get_args(TechnicalStatus)))
    assert len(combos) == 432
    for combo in combos:
        assert _fold_v1(AuthoringAxes(*combo)) == _fold_v2_of(AuthoringAxes(*combo))


def _fold_v2_of(axes: AuthoringAxes) -> str:
    from featuregen.formula.result_v2 import _fold_v2
    return _fold_v2(axes)


def test_forbids_authored_artifact_v2_agrees_with_the_v2_fold() -> None:
    """The orchestrator's pre-check must agree with the real fold on every combination — it decides
    which artifact to OFFER before ``derive_disposition_v2`` decides what may be carried."""
    for combo in itertools.product(
            get_args(StructuralStatus), get_args(CapabilityStatus), get_args(OutputStatus),
            get_args(ExpectationStatus), get_args(CriticStatus), get_args(TechnicalStatus)):
        axes = AuthoringAxes(*combo)
        expected = _fold_v2_of(axes) in ("UNSUPPORTED", "REJECTED", "TECHNICAL_FAILURE")
        assert forbids_authored_artifact_v2(axes) is expected


# ── the happy path ───────────────────────────────────────────────────────────────────────────────

def test_a_governed_v2_proposal_resolves_to_the_authored_pair(db) -> None:
    """v2 has no ``TypedFormulaV2``: the authored artifact is the PAIR (validated proposal +
    C1-resolved output policy), and a RESOLVED result carries both halves plus the derived hash."""
    seed_authoring_catalog(db)
    raw = _raw_v2()
    result = _run(db, raw)

    assert result.authoring_disposition == "RESOLVED"
    assert result.output_status == "resolved"
    assert result.candidate_proposal is not None
    assert result.candidate_output is not None
    assert result.candidate_output.output_type == "numeric"
    assert result.candidate_output.output_additivity is AdditivityClass.ADDITIVE
    assert result.candidate_proposal_hash == proposal_content_hash_v2(result.candidate_proposal)
    assert run_status(db, result.authoring_run_id) == "completed"

    kinds = [e[1] for e in _events(db, result.authoring_run_id)]
    assert kinds[0] == "STARTED" and kinds[-1] == "COMPLETED"
    assert kinds.count("CRITIC_RECORDED") == 1


def test_the_run_is_audited_under_the_v2_turn_schema(db) -> None:
    """The audit must be able to tell a v2 run from a v1 one. A distinct registered schema id (and
    a distinct prompt id) is the only thing that makes that possible after the fact."""
    seed_authoring_catalog(db)
    result = _run(db)
    rows = db.execute(
        "SELECT task, prompt_id, output_schema_id FROM llm_call WHERE run_id = %s ORDER BY task",
        (result.authoring_run_id,)).fetchall()
    author_rows = [r for r in rows if r[0] == AUTHOR_TASK]
    assert author_rows and all(r[2] == AUTHOR_TURN_SCHEMA_ID_V2 for r in author_rows)
    assert all(r[1] == AUTHOR_PROMPT_ID_V2 for r in author_rows)
    assert AUTHOR_PROMPT_ID_V2 != AUTHOR_PROMPT_ID
    assert AUTHOR_INSTRUCTION_V2 != AUTHOR_INSTRUCTION


def test_the_versions_manifest_stamps_the_v2_policy_versions(db) -> None:
    """Every rule version a run is decided under is stamped BEFORE any provider call, so a later
    policy bump can never be mistaken for the one that produced this verdict."""
    seed_authoring_catalog(db)
    result = _run(db)
    versions = db.execute(
        "SELECT versions FROM authoring_run WHERE authoring_run_id = %s",
        (result.authoring_run_id,)).fetchone()[0]
    assert versions == AUTHORING_VERSIONS_V2
    assert versions["formula_schema_version"] == 2


# ── invariant 4: unsupported != invalid ──────────────────────────────────────────────────────────

def test_a_v2_proposal_outside_the_grammar_is_UNSUPPORTED_not_REJECTED(db) -> None:
    """An aggregate outside the v2 vocabulary is out of the GRAMMAR, not malformed. Reporting
    "invalid" for a well-formed request the grammar does not cover would tell the requester the
    proposal was broken when the platform simply cannot express it yet."""
    seed_authoring_catalog(db)
    raw = _raw_v2({"final_operation": "identity", "expr": _expr_v2(aggregation="geomean")})
    result = _run(db, raw)

    assert result.structural_status == "unsupported_operation"
    assert result.authoring_disposition == "UNSUPPORTED"
    assert result.candidate_output is None and result.candidate_proposal is None


def test_a_combiner_outside_the_grammar_is_UNSUPPORTED_not_REJECTED(db) -> None:
    """The second half of the same law, and the one v1 could never actually reach: ``turns_v2``
    relaxes ``final_operation`` on the wire, so an unknown COMBINER arrives here instead of dying
    in a response-schema repair loop."""
    seed_authoring_catalog(db)
    raw = _raw_v2({"final_operation": "harmonic_mean", "expr": _expr_v2()})
    result = _run(db, raw)

    assert result.structural_status == "unsupported_operation"
    assert result.authoring_disposition == "UNSUPPORTED"


def test_a_genuinely_malformed_v2_proposal_is_INVALID_not_unsupported(db) -> None:
    """The converse: a shape the v2 gate rejects (a grain key that is not a column ref) is
    ``invalid_formula`` -> REJECTED. Nothing about the grammar is missing."""
    seed_authoring_catalog(db)
    raw = _raw_v2(grain_keys=("not-a-logical-ref",))
    result = _run(db, raw)

    assert result.structural_status == "invalid_formula"
    assert result.authoring_disposition == "REJECTED"


def test_a_v1_declared_proposal_is_INVALID_not_unsupported() -> None:
    """The v2-only structural rule, at the unit that owns it. A proposal declaring
    ``formula_schema_version: 1`` on a run opened under the v2 contract is the WRONG CONTRACT, not
    a missing capability — v1 is fully supported elsewhere, and calling it "unsupported" would
    report a grammar gap that does not exist."""
    from featuregen.formula.authoring_v2 import _parse_v2

    status, proposal = _parse_v2(_raw_v2(version=1))
    assert status == "invalid_formula"
    assert proposal is None


def test_the_wire_pins_the_version_so_a_v1_body_never_reaches_the_fold(db) -> None:
    """...and via a PROVIDER that rule is unreachable, which is the stronger guarantee and is
    recorded here rather than assumed: ``proposal_v2.schema.json`` pins
    ``formula_schema_version`` to a const, the v2 turn schema carries that pin, so a v1-declared
    body fails RESPONSE validation and the run ends TECHNICAL — the honest "the loop never got a
    v2 proposal", never a false verdict about the v2 grammar. ``_parse_v2``'s version guard stays
    as defence in depth for any non-provider caller."""
    from jsonschema import Draft202012Validator

    errors = list(Draft202012Validator(AUTHOR_TURN_V2_SCHEMA).iter_errors(
        {"turn_type": "final_proposal", "final_proposal": _raw_v2(version=1)}))
    assert [e.json_path for e in errors] == ["$.final_proposal.formula_schema_version"]

    seed_authoring_catalog(db)
    result = _run(db, _raw_v2(version=1))
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.capability_status == "ok" and result.structural_status == "ok"


def test_a_cross_source_v2_proposal_is_UNSUPPORTED(db) -> None:
    """The v2 grammar carries v1's single-source rule verbatim: a formula spanning two catalog
    sources is a capability gap, and the reason names both sources."""
    seed_authoring_catalog(db)
    raw = _raw_v2({"final_operation": "difference",
                   "minuend": _expr_v2(),
                   "subtrahend": _expr_v2(operand="other::public.txns.txn_amt",
                                          table_ref="other::public.txns",
                                          event_time_ref="other::public.txns.txn_dt")})
    result = _run(db, raw)

    assert result.capability_status == "unsupported_capability"
    assert result.authoring_disposition == "UNSUPPORTED"
    assert result.capability_reason is not None
    assert result.capability_reason.startswith("multiple_catalog_sources:")


# ── invariant 3: technical outcomes never fabricate ──────────────────────────────────────────────

def test_a_provider_failure_is_technical_and_carries_no_formula(db) -> None:
    """``author_formula`` returning ``(None, turns)`` is TECHNICAL — no proposal is fabricated and
    the critic is never asked about something that does not exist."""
    seed_authoring_catalog(db)
    author = FakeLLM(script={AUTHOR_TASK: FakeResponse(output={"turn_type": "final_proposal"})})
    result = _run(db, author=author)

    assert result.technical_status == "technical_failure"
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.candidate_output is None and result.candidate_proposal is None
    assert result.structural_status == "ok"        # nothing parsed, so nothing is claimed
    assert run_status(db, result.authoring_run_id) == "failed"
    tasks = [r[0] for r in db.execute(
        "SELECT task FROM llm_call WHERE run_id = %s", (result.authoring_run_id,)).fetchall()]
    assert CRITIC_TASK not in tasks


def test_a_billing_refusal_is_technical_never_a_capability_or_schema_verdict(db) -> None:
    """Charter D-10 and the ``3219a209`` precedent, as a test: a non-retryable provider refusal —
    what an exhausted account returns — is a TECHNICAL failure. It must never be recorded as
    ``unsupported_capability`` (the platform cannot express this) or ``invalid_formula`` (the
    request was malformed): both would be durable, false statements about the v2 grammar derived
    from a payment problem."""
    seed_authoring_catalog(db)
    author = FakeLLM(script={AUTHOR_TASK: FakeResponse(
        output={}, provider_status=PROVIDER_NON_RETRYABLE)})
    result = _run(db, author=author)

    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.technical_status == "technical_failure"
    assert result.capability_status == "ok"          # NOT unsupported_capability
    assert result.structural_status == "ok"          # NOT invalid_formula
    assert result.capability_reason is None
    assert result.candidate_output is None and result.candidate_proposal is None


def test_a_broken_critic_is_technical_never_clean(db) -> None:
    """Fail-closed: a malformed critic response is a technical failure, which dominates the fold.
    A broken critic can never fold toward auto-RESOLVED."""
    seed_authoring_catalog(db)
    critic = FakeLLM(script={CRITIC_TASK: FakeResponse(output={"findings": "not-a-list"})})
    result = _run(db, critic=critic)

    assert result.technical_status == "technical_failure"
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.candidate_output is None
    assert result.critic_findings_hash                 # ...the findings hash is still recorded


def test_a_blocking_critic_finding_prevents_auto_resolved(db) -> None:
    """Any blocking finding -> NEEDS_REVIEW. The output DID resolve, so this is the REVIEWABLE
    shape and the pair is carried."""
    seed_authoring_catalog(db)
    result = _run(db, findings=[{"code": "MISSING_REQUIRED_OPERAND", "operand": REF_AMT}])

    assert result.critic_status == "blocking"
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_proposal is not None and result.candidate_output is not None


# ── invariant 1 (authority): an ungoverned type never reaches RESOLVED ───────────────────────────

def test_an_ungoverned_operand_type_never_reaches_resolved(db) -> None:
    """The load-bearing case, unchanged from v1: ``external_type_required=True`` means the output
    type could not be granted GOVERNED authority, so it is an ``external_requirement``, not a
    resolution. The fold never sees the policy, so this mapping is the single place an
    externally-unvalidated type could be laundered into RESOLVED."""
    seed_authoring_catalog(db)
    result = _run(db, _raw_v2({"final_operation": "identity",
                               "expr": _expr_v2(operand=REF_UNGOVERNED)}))

    assert result.output_status == "external_requirement"
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_output is None                  # no authority, so no policy carried
    assert result.candidate_proposal is not None            # ...but the proposal IS reviewable
    assert result.output_requirements == ("EXTERNAL_TYPE_VALIDATION_REQUIRED",)


@pytest.mark.parametrize("external_type_required, expected", [(False, "resolved"),
                                                              (True, "external_requirement")])
def test_external_type_required_decides_the_output_axis(external_type_required, expected) -> None:
    policy = FormulaOutputPolicyV2(
        output_type="numeric", unit="", currency="",
        output_additivity=AdditivityClass.ADDITIVE,
        external_type_required=external_type_required)
    resolution = classify_output_policy_v2(policy)
    assert resolution.status == expected
    assert (resolution.policy is policy) is (not external_type_required)


def test_a_refused_output_is_invalid_output_and_carries_nothing(db) -> None:
    """``resolve_output_v2``'s refusals (MIXED_UNITS / CURRENCY_CONVERSION_UNDECLARED) are
    ``invalid_output`` -> REJECTED. A refusal is a verdict, and a verdict carries no artifact."""
    resolution = classify_output_policy_v2(InvalidOutputV2("MIXED_UNITS"))
    assert resolution.status == "invalid_output"
    assert resolution.policy is None

    seed_authoring_catalog(db)
    raw = _raw_v2({"final_operation": "difference",
                   "minuend": _expr_v2(operand=REF_AMT),
                   "subtrahend": _expr_v2("count_rows", operand=None)})
    result = _run(db, raw)
    assert result.output_status == "invalid_output"
    assert result.authoring_disposition == "REJECTED"
    assert result.candidate_output is None and result.candidate_proposal is None


# ── the expectation axis ─────────────────────────────────────────────────────────────────────────

def test_an_advisory_expectation_that_contradicts_the_governed_policy_needs_review(db) -> None:
    """The proposal's ``expected_output`` is the model's ADVISORY guess. A disagreement with the
    governed policy is a review signal, never a substitution."""
    seed_authoring_catalog(db)
    result = _run(db, _raw_v2(expected_output={"output_type": "integer"}))

    assert result.expectation_status == "mismatch"
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_output is not None and result.candidate_output.output_type == "numeric"


def test_a_matching_advisory_expectation_resolves(db) -> None:
    seed_authoring_catalog(db)
    result = _run(db, _raw_v2(expected_output={"output_type": "Numeric"}))
    assert result.expectation_status == "match"
    assert result.authoring_disposition == "RESOLVED"


# ── the coherence guards ─────────────────────────────────────────────────────────────────────────

def test_half_a_pair_is_not_an_artifact() -> None:
    """v2's restated honesty core. A resolved output needs BOTH halves; an unresolved one may carry
    the proposal and must NOT carry a policy."""
    resolved = AuthoringAxes("ok", "ok", "resolved", "not_provided", "clean", "ok")
    policy = FormulaOutputPolicyV2("numeric", "", "", AdditivityClass.ADDITIVE, False)
    with pytest.raises(IncoherentResultError):
        derive_disposition_v2(resolved, authoring_run_id="arun_x", candidate_output=policy)
    with pytest.raises(IncoherentResultError):
        derive_disposition_v2(resolved, authoring_run_id="arun_x", candidate_proposal=None,
                              candidate_output=None)

    unresolved = AuthoringAxes("ok", "ok", "needs_authority", "not_provided", "clean", "ok")
    with pytest.raises(IncoherentResultError):
        derive_disposition_v2(unresolved, authoring_run_id="arun_x", candidate_output=policy)

    refused = AuthoringAxes("ok", "ok", "invalid_output", "not_provided", "clean", "ok")
    with pytest.raises(IncoherentResultError):
        derive_disposition_v2(refused, authoring_run_id="arun_x", candidate_output=policy)


def test_an_axis_outside_the_vocabulary_fails_closed() -> None:
    """An unrecognized status would fall through every precedence arm and reach RESOLVED."""
    with pytest.raises(IncoherentResultError):
        derive_disposition_v2(
            AuthoringAxes("ok", "ok", "gibberish", "not_provided", "clean", "ok"),  # type: ignore
            authoring_run_id="arun_x")


# ── the v1 path is untouched ─────────────────────────────────────────────────────────────────────

def test_the_v1_orchestrator_is_byte_identical(db) -> None:
    """D-3: the v1 path is not touched, not deprecated and not deleted. ``author_formula``'s new
    ``turn_contract`` keyword DEFAULTS to v1, so every existing caller requests the same schema
    under the same prompt identity with the same instruction bytes."""
    assert AUTHOR_TURN_CONTRACT_V1.instruction == AUTHOR_INSTRUCTION
    assert AUTHOR_TURN_CONTRACT_V1.prompt_id == AUTHOR_PROMPT_ID
    assert AUTHOR_TURN_CONTRACT_V1.schema_id == "formula_author_turn"
    assert AUTHOR_TURN_CONTRACT_V2.schema_id == AUTHOR_TURN_SCHEMA_ID_V2
    assert AUTHOR_TURN_CONTRACT_V2.schema is AUTHOR_TURN_V2_SCHEMA

    import inspect

    from featuregen.formula.author import author_formula
    signature = inspect.signature(author_formula)
    assert signature.parameters["turn_contract"].default is AUTHOR_TURN_CONTRACT_V1

    # ...and the v1 orchestrator still produces a v1 result over the same catalog.
    from tests.featuregen.formula.test_authoring import _raw as _raw_v1

    from featuregen.formula.authoring import run_authoring
    seed_authoring_catalog(db)
    v1_intent = AuthoringIntent(
        name="txn_amt_sum_90d", hypothesis=_INTENT.hypothesis,
        target_entity="customer", target_grain_keys=(REF_CIF,))
    result = run_authoring(
        db, v1_intent, _author_client(_raw_v1()), _critic_client(), roles=(), actor=_ACTOR)
    assert result.authoring_disposition == "RESOLVED"
    assert result.candidate_formula is not None          # v1's fused artifact, unchanged
    assert not hasattr(result, "candidate_output")


def test_the_two_generations_do_not_share_a_schema_identity() -> None:
    """A v2 run audited under the v1 identity would be indistinguishable after the fact, and a
    frozen provider contract could not tell them apart."""
    assert AUTHOR_TURN_CONTRACT_V1.schema_id != AUTHOR_TURN_CONTRACT_V2.schema_id
    assert AUTHOR_TURN_CONTRACT_V1.prompt_id != AUTHOR_TURN_CONTRACT_V2.prompt_id
    assert AUTHOR_TURN_CONTRACT_V1.schema != AUTHOR_TURN_CONTRACT_V2.schema


def test_the_critic_reviews_both_generations_from_the_same_closed_context(db) -> None:
    """The critic is version-NEUTRAL by design: a finding says "this operand is not what the intent
    asked for", which is a statement about the CATALOG. Both generations walk into the same
    three-key payload and the same closed §G code set."""
    from featuregen.formula.critic import build_critic_metadata, proposal_column_refs
    from featuregen.formula.parse_v2 import parse_proposal_v2

    seed_authoring_catalog(db)
    proposal = parse_proposal_v2(_raw_v2({
        "final_operation": "ratio",
        "numerator": _expr_v2(operand=REF_AMT),
        "denominator": _expr_v2(operand=REF_FEE),
        "zero_denominator": "null"}))
    assert proposal_column_refs(proposal) == tuple(sorted({REF_AMT, REF_FEE, REF_CIF, REF_DT}))

    metadata = build_critic_metadata(db, _INTENT, proposal, roles=())
    assert set(metadata) == {"authoring_intent", "proposal", "operand_columns"}
    assert metadata["proposal"]["formula_schema_version"] == 2
