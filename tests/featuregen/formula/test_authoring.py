"""Child-1 Task 12 — the authoring ORCHESTRATOR: deterministic plumbing/conformance (gold gate a).

This file proves the WIRING and nothing else. Every provider call is a scripted ``FakeLLM``, so
what is under test is that each of the eleven prior components is invoked, in order, with the
contract it declares — and that the five review carry-forwards hold:

1. ``per_expr_facts`` is a ``Mapping[str, ExprFacts]`` keyed by the INTERNAL BODY PATHS
   (``body.expr`` / ``body.numerator`` / ``body.denominator`` / ``body.minuend`` /
   ``body.subtrahend``) and ``grain_facts`` by grain-key ref, each built from REAL
   ``read_operational_value`` reads;
2. a ``FormulaOutputPolicyV1`` carrying ``external_type_required=True`` NEVER reaches RESOLVED —
   it maps to ``output_status="external_requirement"``, so an externally-unvalidated type cannot be
   laundered into authority by an axis fold that never sees the policy;
3. ``AuthoringResult`` is constructed ONLY by ``derive_disposition`` (asserted over the AST, so
   prose cannot satisfy it);
4. a ``(None, turns)`` author outcome and an ``is_technical_failure`` critic are TECHNICAL, never a
   fabricated proposal or a clean critic;
5. the trace gets STARTED first, strictly monotone seqs, exactly one terminal, and metadata-only
   payloads whose ``payload_hash`` recomputes from the read-back jsonb.

FakeLLM proves PLUMBING, never QUALITY — the quality gates are ``test_gold_gate.py`` (curated
fixtures) and the key-gated ``test_provider_eval.py``.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import itertools
import json
from pathlib import Path

import pytest
from tests.featuregen._helpers import make_actor
from tests.featuregen.formula.authoring_fixtures import (
    CANARY,
    REF_AMT,
    REF_CIF,
    REF_DT,
    REF_FEE,
    REF_UNGOVERNED,
    SOURCE,
    TABLE_REF,
    seed_authoring_catalog,
)

from featuregen.formula import authoring
from featuregen.formula._jcs import dumps as jcs_dumps
from featuregen.formula.author import AUTHOR_TASK
from featuregen.formula.authoring import (
    AUTHORING_MAX_TURNS,
    authoring_intent_hash,
    classify_output_policy,
    forbids_authored_artifact,
    run_authoring,
)
from featuregen.formula.canonical import formula_content_hash
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.output_authority import (
    ExprFacts,
    ExternalRequirement,
    InvalidOutput,
    NeedsAuthority,
)
from featuregen.formula.result import (
    AuthoringAxes,
    IncoherentResultError,
    derive_disposition,
)
from featuregen.formula.schema import (
    AdditivityClass,
    FormulaOutputPolicyV1,
    TypedFormulaV1,
)
from featuregen.formula.trace import TraceEventKind, run_status
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.operational_facts import read_operational_value

_ACTOR = make_actor(subject="user:formula-author", roles=("feature_engineer",))

_INTENT = AuthoringIntent(
    name="txn_amt_sum_90d",
    hypothesis="Total transaction amount over a trailing 90-day window predicts churn.",
    target_entity="customer",
    target_grain_keys=(REF_CIF,),
)


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-HERMETIC (same rationale as ``test_trace.py``): with an ambient ``FEATUREGEN_DSN`` the
    trace would COMMIT on the durable fresh connection, and ``authoring_trace_event`` rows can
    physically never be cleaned up (that is the write-once guarantee), so a second run of the suite
    would fail on stale keys and stay red until the database was dropped."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


# ── raw proposal builders (the shapes a scripted author "emits") ─────────────────────────────────

def _window(event_time_ref: str = REF_DT) -> dict:
    return {"event_time_ref": event_time_ref, "basis": "trailing", "length": 90, "unit": "day",
            "start_inclusive": "inclusive", "end_inclusive": "exclusive",
            "timezone": "Asia/Dubai", "empty_window": "null", "null_input": "ignore"}


def _expr(aggregation: str = "sum", operand: str | None = REF_AMT, *,
          table_ref: str = TABLE_REF, filter_node: dict | None = None,
          event_time_ref: str = REF_DT) -> dict:
    return {"aggregation": aggregation, "operand": operand,
            "source_relation": {"table_ref": table_ref}, "filter": filter_node,
            "window": _window(event_time_ref)}


def _raw(body: dict | None = None, *, expected_output: dict | None = None,
         grain_keys: tuple[str, ...] = (REF_CIF,)) -> dict:
    return {"formula_schema_version": 1, "operation_grammar_version": 1,
            "canonicalization_version": 1,
            "grain": {"entity": "customer", "keys": list(grain_keys)},
            "body": body if body is not None else {"final_operation": "identity", "expr": _expr()},
            "parameters": [],
            "decimal": {"precision": 38, "scale": 6, "rounding": "half_even",
                        "overflow": "error"},
            "expected_output": expected_output}


def _final_turn(raw: dict) -> dict:
    return {"turn_type": "final_proposal", "final_proposal": raw}


def _author_client(raw: dict) -> FakeLLM:
    """A one-turn author: the FakeLLM task-key fallback matches on ``request.task`` alone, so the
    very first turn is the final proposal (multi-turn runs need exact input-hash scripting)."""
    return FakeLLM(script={AUTHOR_TASK: FakeResponse(output=_final_turn(raw))})


def _critic_client(findings: list[dict] | None = None, **kwargs) -> FakeLLM:
    return FakeLLM(script={
        CRITIC_TASK: FakeResponse(output={"findings": list(findings or [])}, **kwargs)})


def _run(db, raw: dict | None = None, *, findings=None, intent: AuthoringIntent = _INTENT,
         critic: FakeLLM | None = None, author: FakeLLM | None = None, roles=()):
    return run_authoring(
        db, intent,
        author if author is not None else _author_client(raw if raw is not None else _raw()),
        critic if critic is not None else _critic_client(findings),
        roles=roles, actor=_ACTOR)


# ── trace readers ────────────────────────────────────────────────────────────────────────────────

def _events(db, run_id: str) -> list[tuple]:
    return db.execute(
        "SELECT seq, kind, llm_call_ref, payload, payload_hash FROM authoring_trace_event "
        "WHERE authoring_run_id = %s ORDER BY seq", (run_id,)).fetchall()


# ── the signature is VERBATIM ────────────────────────────────────────────────────────────────────

def test_run_authoring_signature_is_verbatim() -> None:
    """The brief's signature exactly: no extra knobs smuggled into the public surface (bounds like
    ``max_turns`` are module constants, not per-call parameters)."""
    params = inspect.signature(run_authoring).parameters
    assert list(params) == ["conn", "intent", "author_client", "critic_client", "roles", "actor"]
    assert params["roles"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["actor"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["conn"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


# ── the chain: every stage, in order ─────────────────────────────────────────────────────────────

def test_every_stage_is_invoked_in_the_declared_order(db, monkeypatch) -> None:
    """open_run -> author -> parse(+validate) -> capability -> C1 output authority -> critic ->
    derive_disposition -> terminal event. Each stage is wrapped in place and records itself."""
    seed_authoring_catalog(db)
    order: list[str] = []

    def spy(name: str, real):
        def wrapped(*args, **kwargs):
            order.append(name)
            return real(*args, **kwargs)
        return wrapped

    for name in ("open_authoring_run", "author_formula", "parse_proposal_v1",
                 "classify_formula_capability", "resolve_formula_output_policy", "critique",
                 "derive_disposition"):
        monkeypatch.setattr(authoring, name, spy(name, getattr(authoring, name)))
    real_append = authoring.append_event

    def append_spy(conn, run_id, kind, **kwargs):
        if kind in (TraceEventKind.COMPLETED, TraceEventKind.FAILED):
            order.append("append_terminal_event")
        return real_append(conn, run_id, kind, **kwargs)
    monkeypatch.setattr(authoring, "append_event", append_spy)

    _run(db)

    assert order == ["open_authoring_run", "author_formula", "parse_proposal_v1",
                     "classify_formula_capability", "resolve_formula_output_policy", "critique",
                     "derive_disposition", "append_terminal_event"]


def test_the_happy_path_resolves_to_an_authoritative_typed_formula(db) -> None:
    """A grounded SUM over a GOVERNED numeric type, a clean critic and no advisory expectation →
    RESOLVED, carrying a real TypedFormulaV1 whose hash is the §E content hash."""
    seed_authoring_catalog(db)
    result = _run(db)

    assert result.authoring_disposition == "RESOLVED"
    assert (result.structural_status, result.capability_status, result.output_status) == (
        "ok", "ok", "resolved")
    assert (result.expectation_status, result.critic_status, result.technical_status) == (
        "not_provided", "clean", "ok")
    assert isinstance(result.candidate_formula, TypedFormulaV1)
    assert result.candidate_proposal is None
    assert result.candidate_formula_hash == formula_content_hash(result.candidate_formula)
    # the AUTHORITATIVE output comes from C1 via Task 6 — never from the LLM's advisory guess
    assert result.candidate_formula.output == FormulaOutputPolicyV1(
        output_type="numeric", unit=None, currency=None,
        output_additivity=AdditivityClass.NON_ADDITIVE, external_type_required=False)
    assert result.candidate_formula.output_policy_version == 1
    assert run_status(db, result.authoring_run_id) == "completed"


def test_author_and_critic_are_separate_clients_and_separate_contexts(db) -> None:
    """LLM-2 is a genuine second opinion: its own client, its own audited call, and a context with
    no slot for the author's tool trail."""
    seed_authoring_catalog(db)
    author, critic = _author_client(_raw()), _critic_client()
    assert author is not critic
    result = _run(db, author=author, critic=critic)

    rows = db.execute(
        "SELECT task, redacted_input FROM llm_call WHERE run_id = %s",
        (result.authoring_run_id,)).fetchall()
    # Keyed by TASK, never ordered by ``created_at``: both rows are written inside one request
    # transaction and Postgres' ``now()`` is transaction-scoped, so their timestamps are IDENTICAL
    # and the row order is whatever the scan happens to produce. The ORDERING claim is proved from
    # the trace below, where seq is the orchestrator's own monotone contract.
    by_task = {row[0]: row[1] for row in rows}
    assert len(rows) == 2 and set(by_task) == {AUTHOR_TASK, CRITIC_TASK}
    critic_meta = by_task[CRITIC_TASK]["catalog_metadata"]
    assert set(critic_meta) == {"authoring_intent", "proposal", "operand_columns"}
    assert "tool_trail" not in json.dumps(critic_meta)
    assert CANARY not in json.dumps(rows)               # no catalog free text egressed anywhere
    # the author's call is evidenced BEFORE the critic's — the critic reviews an authored proposal
    kinds = [event[1] for event in _events(db, result.authoring_run_id)]
    assert kinds.index("LLM_CALL_RECORDED") < kinds.index("CRITIC_RECORDED")


# ── carry-forward #1: the per_expr_facts bundle shape ────────────────────────────────────────────

@pytest.mark.parametrize(
    ("body", "expected_paths"),
    [
        ({"final_operation": "identity", "expr": _expr()}, {"body.expr"}),
        ({"final_operation": "ratio", "numerator": _expr(), "denominator": _expr(operand=REF_FEE),
          "zero_denominator": "null"}, {"body.numerator", "body.denominator"}),
        ({"final_operation": "difference", "minuend": _expr(),
          "subtrahend": _expr(operand=REF_FEE)}, {"body.minuend", "body.subtrahend"}),
    ],
)
def test_per_expr_facts_are_keyed_by_the_internal_body_paths(db, monkeypatch, body,
                                                             expected_paths) -> None:
    """Task 6 keys ``per_expr_facts`` by the §A INTERNAL EXPRESSION PATHS and ``grain_facts`` by
    grain-key ref. A bundle keyed by anything else silently resolves against ``ExprFacts()`` —
    every governed fact missing, every policy a guess."""
    seed_authoring_catalog(db)
    captured: dict = {}
    real = authoring.resolve_formula_output_policy

    def capture(proposal, *, per_expr_facts, grain_facts, now):
        captured["per_expr_facts"] = per_expr_facts
        captured["grain_facts"] = grain_facts
        return real(proposal, per_expr_facts=per_expr_facts, grain_facts=grain_facts, now=now)
    monkeypatch.setattr(authoring, "resolve_formula_output_policy", capture)

    _run(db, _raw(body))

    assert set(captured["per_expr_facts"]) == expected_paths
    assert all(isinstance(f, ExprFacts) for f in captured["per_expr_facts"].values())
    assert set(captured["grain_facts"]) == {REF_CIF}


def test_expr_facts_carry_the_real_c1_operational_reads(db, monkeypatch) -> None:
    """The bundle is built from ``read_operational_value`` — the ONE governed authority read — not
    from the author's tool trail and not from the flat graph columns."""
    seed_authoring_catalog(db)
    captured: dict = {}
    real = authoring.resolve_formula_output_policy

    def capture(proposal, *, per_expr_facts, grain_facts, now):
        captured.update(per_expr_facts=per_expr_facts, grain_facts=grain_facts)
        return real(proposal, per_expr_facts=per_expr_facts, grain_facts=grain_facts, now=now)
    monkeypatch.setattr(authoring, "resolve_formula_output_policy", capture)

    _run(db)

    facts = captured["per_expr_facts"]["body.expr"]
    assert facts.output_type == read_operational_value(db, REF_AMT, "logical_representation")
    assert facts.additivity == read_operational_value(db, REF_AMT, "additivity")
    assert facts.unit == read_operational_value(db, REF_AMT, "unit")
    assert facts.currency == read_operational_value(db, REF_AMT, "currency")
    assert facts.output_type.status == "resolved"                 # the seeded GOVERNED type
    assert captured["grain_facts"][REF_CIF] == read_operational_value(db, REF_CIF, "is_grain")


def test_count_rows_reads_no_operand_facts_but_still_reads_the_grain(db, monkeypatch) -> None:
    """COUNT_ROWS carries no operand (§A), so its ``ExprFacts`` is empty — but §C still requires the
    grain to be readable, so ``grain_facts`` must be populated."""
    seed_authoring_catalog(db)
    captured: dict = {}
    real = authoring.resolve_formula_output_policy

    def capture(proposal, *, per_expr_facts, grain_facts, now):
        captured.update(per_expr_facts=per_expr_facts, grain_facts=grain_facts)
        return real(proposal, per_expr_facts=per_expr_facts, grain_facts=grain_facts, now=now)
    monkeypatch.setattr(authoring, "resolve_formula_output_policy", capture)

    body = {"final_operation": "identity", "expr": _expr("count_rows", None)}
    result = _run(db, _raw(body))

    assert captured["per_expr_facts"]["body.expr"] == ExprFacts()
    assert set(captured["grain_facts"]) == {REF_CIF}
    assert result.candidate_formula.output.output_type == "integer"   # §C: counts are dimensionless


# ── carry-forward #2: external_type_required must never reach RESOLVED ───────────────────────────

@pytest.mark.parametrize("external_type_required", [True, False])
def test_external_type_required_decides_the_output_axis(external_type_required) -> None:
    """The PURE mapping, isolated: the §F fold sees only the axes, never the policy, so this is the
    single place an externally-unvalidated type can be laundered into RESOLVED."""
    policy = FormulaOutputPolicyV1(
        output_type="numeric", unit=None, currency=None,
        output_additivity=AdditivityClass.NON_ADDITIVE,
        external_type_required=external_type_required)
    resolution = classify_output_policy(policy)
    assert resolution.status == ("external_requirement" if external_type_required else "resolved")
    if external_type_required:
        assert resolution.policy is None                  # no authoritative policy is carried
        assert [r.requirement for r in resolution.requirements] == ["EXTERNAL_TYPE_VALIDATION_REQUIRED"]
    else:
        assert resolution.policy is policy


def test_an_ungoverned_operand_type_never_reaches_resolved(db) -> None:
    """END TO END. ``ungoverned_amt`` is numeric but has NO governed ``logical_representation``
    decision, so §C resolves the SUM with ``external_type_required=True``. That is NOT a resolved
    output: the run is NEEDS_REVIEW carrying the validated PROPOSAL — never a TypedFormulaV1, which
    would assert an authority the catalog has not granted."""
    seed_authoring_catalog(db)
    body = {"final_operation": "identity", "expr": _expr(operand=REF_UNGOVERNED)}
    result = _run(db, _raw(body))

    assert result.output_status == "external_requirement"
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_formula is None and result.candidate_formula_hash is None
    assert result.candidate_proposal is not None
    assert [r.requirement for r in result.output_requirements] == [
        "EXTERNAL_TYPE_VALIDATION_REQUIRED"]


def test_no_axis_combination_reaches_resolved_with_an_external_requirement() -> None:
    """The structural guarantee behind the end-to-end case: ``external_requirement`` is in the §F
    unresolved set, so NO combination of the other five axes can fold it to RESOLVED."""
    for axes in _all_axes():
        if axes.output_status == "external_requirement":
            assert _fold(axes) != "RESOLVED"


def test_a_non_cancelling_ratio_is_a_typed_external_requirement(db) -> None:
    """§C: a RATIO whose units cannot cancel is a TYPED requirement, not an indiscriminate
    NEEDS_AUTHORITY — and, like every unresolved output, it carries the proposal, not a formula."""
    seed_authoring_catalog(db)
    db.execute("UPDATE graph_node SET unit = 'dollars' WHERE catalog_source = %s "
               "AND object_ref = 'public.txns.txn_amt'", (SOURCE,))
    db.execute("UPDATE graph_node SET unit = 'count' WHERE catalog_source = %s "
               "AND object_ref = 'public.txns.fee_amt'", (SOURCE,))
    body = {"final_operation": "ratio", "numerator": _expr(),
            "denominator": _expr(operand=REF_FEE), "zero_denominator": "null"}
    result = _run(db, _raw(body))

    assert result.output_status == "external_requirement"
    assert [r.requirement for r in result.output_requirements] == ["UNIT_PROVISIONING_REQUIRED"]
    assert result.candidate_formula is None
    assert result.authoring_disposition == "NEEDS_REVIEW"


def test_a_hard_failed_c1_read_is_needs_authority_with_attribution(db) -> None:
    """§C fail-closed: a REQUIRED field whose C1 read is fork / hash_mismatch /
    projection_unavailable → NEEDS_AUTHORITY, carrying WHICH operand and WHICH field failed."""
    seed_authoring_catalog(db)
    # tamper the projected value out from under the governed logical_representation decision
    db.execute("UPDATE graph_node SET data_type = 'tampered' WHERE catalog_source = %s "
               "AND object_ref = 'public.txns.txn_amt'", (SOURCE,))
    assert read_operational_value(db, REF_AMT, "logical_representation").status == "hash_mismatch"

    result = _run(db)

    assert result.output_status == "needs_authority"
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_formula is None and result.candidate_proposal is not None
    assert [(f.operand, f.field) for f in result.authority_failures] == [
        ("body.expr", "logical_representation")]
    assert result.authority_failures[0].reason


def test_incompatible_difference_units_are_rejected_not_merely_unsupported(db) -> None:
    """§C: subtracting dollars from euros is a structurally impossible output → invalid_output →
    REJECTED. ``unsupported != invalid``, and this is the invalid side."""
    seed_authoring_catalog(db)
    db.execute("UPDATE graph_node SET unit = 'dollars' WHERE catalog_source = %s "
               "AND object_ref = 'public.txns.txn_amt'", (SOURCE,))
    db.execute("UPDATE graph_node SET unit = 'euros' WHERE catalog_source = %s "
               "AND object_ref = 'public.txns.fee_amt'", (SOURCE,))
    body = {"final_operation": "difference", "minuend": _expr(),
            "subtrahend": _expr(operand=REF_FEE)}
    result = _run(db, _raw(body))

    assert result.output_status == "invalid_output"
    assert result.authoring_disposition == "REJECTED"
    assert result.candidate_formula is None


# ── carry-forward #3: AuthoringResult is built ONLY by derive_disposition ────────────────────────

def test_authoring_result_is_never_constructed_directly(db) -> None:
    """Asserted over the AST so a docstring cannot satisfy it: the coherence RAISE-guards (RESOLVED
    requires a formula; unresolved-output NEEDS_REVIEW forbids one and carries the proposal; the
    hash is always derived) live in ``derive_disposition``, and a hand-built result bypasses ALL of
    them."""
    source = Path(authoring.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "AuthoringResult" not in called
    assert "derive_disposition" in called
    assert "AuthoringResult" not in {  # nor imported, so it cannot be constructed at all
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names}


def _all_axes():
    from typing import get_args

    from featuregen.formula.result import (
        CapabilityStatus,
        CriticStatus,
        ExpectationStatus,
        OutputStatus,
        StructuralStatus,
        TechnicalStatus,
    )
    for combo in itertools.product(
            get_args(StructuralStatus), get_args(CapabilityStatus), get_args(OutputStatus),
            get_args(ExpectationStatus), get_args(CriticStatus), get_args(TechnicalStatus)):
        yield AuthoringAxes(*combo)


_A_FORMULA_ARGS = None


def _fold(axes: AuthoringAxes) -> str:
    """The §F disposition for ``axes``, obtained from ``derive_disposition`` itself by offering the
    artifact sets in coherence order — the ONLY oracle that cannot drift from the real fold."""
    from tests.featuregen.formula.factories import base_formula

    from featuregen.formula.parse import parse_proposal_v1
    global _A_FORMULA_ARGS
    if _A_FORMULA_ARGS is None:
        _A_FORMULA_ARGS = (base_formula(), parse_proposal_v1(_raw()))
    formula, proposal = _A_FORMULA_ARGS
    for kwargs in ({}, {"candidate_formula": formula}, {"candidate_proposal": proposal}):
        try:
            return derive_disposition(
                axes, authoring_run_id="arun_oracle", **kwargs).authoring_disposition
        except IncoherentResultError:
            continue
    raise AssertionError(f"no coherent artifact set for {axes}")   # pragma: no cover


def test_forbids_authored_artifact_agrees_with_the_fold_on_every_axis_combination() -> None:
    """The orchestrator must know, BEFORE calling ``derive_disposition``, whether the disposition
    will forbid an authored artifact (so it can pass the right one). That predicate mirrors the §F
    precedence, so it is pinned here against the real fold over the FULL axis cross-product — it
    can never drift silently."""
    checked = 0
    for axes in _all_axes():
        expected = _fold(axes) in ("UNSUPPORTED", "REJECTED", "TECHNICAL_FAILURE")
        assert forbids_authored_artifact(axes) is expected, axes
        checked += 1
    assert checked == 3 * 2 * 4 * 3 * 3 * 2


# ── carry-forward #4: technical outcomes never fabricate ─────────────────────────────────────────

def test_an_author_that_never_finalizes_is_a_technical_failure(db) -> None:
    """``author_formula`` returns ``(None, turns)`` on max_turns exhaustion / budget exceed / egress
    block / a malformed turn. The orchestrator maps that to ``technical_status`` — never to a
    fabricated proposal, and never onward to the critic."""
    seed_authoring_catalog(db)
    # shape-valid (turn_type alone satisfies the schema) but the discriminator has no slot
    author = FakeLLM(script={AUTHOR_TASK: FakeResponse(output={"turn_type": "final_proposal"})})
    result = _run(db, author=author)

    assert result.technical_status == "technical_failure"
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.candidate_formula is None and result.candidate_proposal is None
    assert result.structural_status == "ok"          # nothing was parsed, so nothing is claimed
    assert run_status(db, result.authoring_run_id) == "failed"
    tasks = [r[0] for r in db.execute(
        "SELECT task FROM llm_call WHERE run_id = %s", (result.authoring_run_id,)).fetchall()]
    assert CRITIC_TASK not in tasks                  # the critic is never asked about nothing


def test_an_exhausted_author_run_is_technical_and_fully_traced(db) -> None:
    """The other ``(None, turns)`` shape: every turn taken is still recorded with its audit ref."""
    seed_authoring_catalog(db)
    author = FakeLLM(script={AUTHOR_TASK: FakeResponse(
        output={"turn_type": "tool_call",
                "tool_call": {"tool_name": "list_supported_operations", "arguments": {}}})})
    result = _run(db, author=author)

    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    kinds = [e[1] for e in _events(db, result.authoring_run_id)]
    assert kinds[0] == "STARTED" and kinds[-1] == "FAILED"
    assert kinds.count("LLM_CALL_RECORDED") == AUTHORING_MAX_TURNS
    assert kinds.count("TOOL_CALLED") == AUTHORING_MAX_TURNS


def test_a_broken_critic_is_technical_never_clean(db) -> None:
    """§G fail-closed: a malformed critic response is ``is_technical_failure=True``. A broken critic
    must never fold toward auto-RESOLVED, so it becomes the TECHNICAL axis — which dominates."""
    seed_authoring_catalog(db)
    critic = FakeLLM(script={CRITIC_TASK: FakeResponse(output={"findings": "not-a-list"})})
    result = _run(db, critic=critic)

    assert result.technical_status == "technical_failure"
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.candidate_formula is None          # a technical outcome carries NO formula
    assert result.critic_findings_hash               # ...but the findings hash is still recorded
    assert run_status(db, result.authoring_run_id) == "failed"


def test_a_blocking_critic_finding_prevents_auto_resolved(db) -> None:
    """§G: any blocking finding → ``critic_status="blocking"`` → NEEDS_REVIEW. The output DID
    resolve, so this is the REVIEWABLE shape: the authored formula + hash are carried [c7-r3]."""
    seed_authoring_catalog(db)
    result = _run(db, findings=[{"code": "MISSING_REQUIRED_OPERAND", "operand": REF_AMT,
                                 "detail": "the intent's amount concept is not aggregated"}])

    assert result.critic_status == "blocking"
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert isinstance(result.candidate_formula, TypedFormulaV1)
    assert result.candidate_formula_hash == formula_content_hash(result.candidate_formula)
    assert result.candidate_proposal is None


def test_an_advisory_critic_finding_still_resolves(db) -> None:
    """Advisory findings are recorded but never block (§G fixed severity map)."""
    seed_authoring_catalog(db)
    result = _run(db, findings=[{"code": "WEAK_PROXY", "operand": REF_AMT, "detail": "proxy"}])
    assert result.critic_status == "advisory"
    assert result.authoring_disposition == "RESOLVED"


# ── structural / capability: unsupported != invalid ──────────────────────────────────────────────

def test_a_cross_source_proposal_is_unsupported_not_rejected(db) -> None:
    """v1 capability is a SINGLE catalog source. A multi-source body is valid-but-unsupported →
    UNSUPPORTED (never REJECTED), and carries no authored artifact."""
    seed_authoring_catalog(db)
    other = _expr("count_rows", None, table_ref="elsewhere::public.events",
                  event_time_ref="elsewhere::public.events.event_dt")
    body = {"final_operation": "ratio", "numerator": _expr(), "denominator": other,
            "zero_denominator": "null"}
    result = _run(db, _raw(body))

    assert result.capability_status == "unsupported_capability"
    assert result.authoring_disposition == "UNSUPPORTED"
    assert result.structural_status == "ok"          # it is well formed — just out of v1 scope
    assert result.candidate_formula is None
    assert result.capability_reason


def test_an_over_deep_filter_is_rejected(db) -> None:
    """MAX_FILTER_DEPTH is a §A SEMANTIC rule (not a JSON-Schema bound), so an over-deep filter is
    shape-valid, reaches ``validate_semantics``, and is INVALID → REJECTED."""
    seed_authoring_catalog(db)
    node = {"kind": "predicate", "op": "is_not_null", "left": REF_STATUS_REF}
    for _ in range(5):
        node = {"kind": "bool", "op": "not", "children": [node]}
    result = _run(db, _raw({"final_operation": "identity",
                            "expr": _expr(filter_node=node)}))

    assert result.structural_status == "invalid_formula"
    assert result.authoring_disposition == "REJECTED"
    assert result.candidate_formula is None and result.candidate_proposal is None


REF_STATUS_REF = "authored::public.txns.status_cd"


def test_an_out_of_vocabulary_aggregation_is_unsupported_not_invalid(db, monkeypatch) -> None:
    """``unsupported != invalid``: ``avg`` is OUT of the §B vocabulary, so a proposal naming it is
    UNSUPPORTED, never REJECTED.

    The author's wire schema pins the aggregation enum, so such a proposal cannot arrive THROUGH a
    provider call — the seam rejects it as a malformed turn (a technical outcome) long before parse.
    The author is therefore stubbed to hand the orchestrator that raw dict directly, which is
    precisely the seam this plumbing suite exists to test: how ``run_authoring`` maps an author
    outcome onto the §F axes."""
    seed_authoring_catalog(db)
    raw = _raw({"final_operation": "identity", "expr": _expr("avg")})
    monkeypatch.setattr(authoring, "author_formula", lambda *a, **k: (raw, []))
    result = _run(db)

    assert result.structural_status == "unsupported_operation"
    assert result.authoring_disposition == "UNSUPPORTED"
    assert result.candidate_formula is None


def test_a_genuinely_malformed_proposal_is_invalid_not_unsupported(db, monkeypatch) -> None:
    """The other side of the same boundary: a proposal missing a required object is INVALID."""
    seed_authoring_catalog(db)
    raw = _raw()
    del raw["decimal"]
    monkeypatch.setattr(authoring, "author_formula", lambda *a, **k: (raw, []))
    result = _run(db)

    assert result.structural_status == "invalid_formula"
    assert result.authoring_disposition == "REJECTED"


# ── the advisory expectation axis ────────────────────────────────────────────────────────────────

def test_an_advisory_expectation_that_contradicts_the_governed_policy_needs_review(db) -> None:
    """``expected_output`` is the LLM's ADVISORY guess; the authority is C1's. A contradiction never
    changes the output — it raises the expectation axis, which forces human review while still
    carrying the authoritative formula."""
    seed_authoring_catalog(db)
    raw = _raw(expected_output={"output_type": "percentage", "unit": "euros", "currency": "EUR"})
    result = _run(db, raw)

    assert result.expectation_status == "mismatch"
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_formula.output.output_type == "numeric"   # governed, not "percentage"
    assert result.candidate_formula.output.unit is None               # governed, not "euros"


def test_a_matching_advisory_expectation_resolves(db) -> None:
    seed_authoring_catalog(db)
    result = _run(db, _raw(expected_output={"output_type": "numeric", "unit": None,
                                            "currency": None}))
    assert result.expectation_status == "match"
    assert result.authoring_disposition == "RESOLVED"


# ── carry-forward #5: trace ordering + payload discipline ────────────────────────────────────────

def test_the_trace_starts_first_is_monotone_and_has_exactly_one_terminal(db) -> None:
    """The DB enforces "nothing after a terminal" but NOT "STARTED first" nor seq monotonicity —
    those are the orchestrator's convention, so they are asserted here."""
    seed_authoring_catalog(db)
    result = _run(db)
    events = _events(db, result.authoring_run_id)

    seqs = [e[0] for e in events]
    kinds = [e[1] for e in events]
    assert seqs == list(range(len(seqs)))                    # monotone, gapless, from zero
    assert kinds[0] == "STARTED"
    assert kinds.count("STARTED") == 1
    terminals = [k for k in kinds if k in ("COMPLETED", "FAILED")]
    assert terminals == ["COMPLETED"]                        # exactly one, and it is LAST
    assert kinds[-1] == "COMPLETED"
    assert "CRITIC_RECORDED" in kinds


def test_the_manifest_is_written_before_any_provider_call(db, monkeypatch) -> None:
    """§H manifest-first: the run row exists before the author's first audited call, so a crash can
    never leave audited provider calls with no manifest saying which run made them."""
    seed_authoring_catalog(db)
    seen: list[str] = []
    real_open = authoring.open_authoring_run

    def open_spy(conn, **kwargs):
        seen.append("manifest")
        return real_open(conn, **kwargs)

    real_author = authoring.author_formula

    def author_spy(conn, *args, **kwargs):
        run_id = kwargs["authoring_run_id"]
        row = conn.execute("SELECT intent_hash FROM authoring_run WHERE authoring_run_id = %s",
                           (run_id,)).fetchone()
        seen.append("author" if row is None else "author_after_manifest")
        return real_author(conn, *args, **kwargs)

    monkeypatch.setattr(authoring, "open_authoring_run", open_spy)
    monkeypatch.setattr(authoring, "author_formula", author_spy)
    _run(db)
    assert seen == ["manifest", "author_after_manifest"]


def test_every_payload_hash_recomputes_from_the_stored_jsonb(db) -> None:
    """Tamper-evidence: ``payload_hash`` is the sha256 over the payload's RFC 8785 (JCS) bytes, so
    it must recompute from the READ-BACK jsonb, not merely from what the writer held."""
    seed_authoring_catalog(db)
    result = _run(db)
    for seq, _kind, _ref, payload, payload_hash in _events(db, result.authoring_run_id):
        assert hashlib.sha256(jcs_dumps(payload)).hexdigest() == payload_hash, seq
        assert isinstance(payload, dict)                     # jsonb_typeof = 'object'


def test_trace_payloads_are_metadata_only(db) -> None:
    """The §H discipline ``formula.tools`` already enforces on egress, applied to what is STORED:
    canonical redacted tool results + hashes, dispositions, reason codes — never raw catalog data
    values or catalog free text."""
    seed_authoring_catalog(db)
    author = _author_client(_raw())
    result = _run(db, author=author)
    dumped = json.dumps([e[3] for e in _events(db, result.authoring_run_id)])
    assert CANARY not in dumped
    assert "definition" not in dumped
    # nor does the model's free-text critic detail ride the trace — codes and the hash suffice
    result2 = _run(db, findings=[{"code": "WEAK_PROXY", "operand": REF_AMT,
                                  "detail": "a distinctive detail string"}])
    dumped2 = json.dumps([e[3] for e in _events(db, result2.authoring_run_id)])
    assert "a distinctive detail string" not in dumped2
    assert "WEAK_PROXY" in dumped2


def test_a_multi_turn_run_records_the_tool_call_and_its_canonical_result(db) -> None:
    """§H: TOOL_CALLED / TOOL_RESULT_RECORDED, with the CANONICAL tool result + its hash — the same
    bytes the author threaded into the next turn's metadata."""
    from featuregen.formula.author import (
        AUTHOR_INSTRUCTION,
        AUTHOR_PROMPT_ID,
        build_turn_metadata,
        tool_trail_entry,
    )
    from featuregen.formula.tools import run_tool
    from featuregen.intake.llm import compute_input_hash
    from featuregen.intake.redaction import RedactionResult, build_llm_inputs

    seed_authoring_catalog(db)
    args = {"logical_ref": REF_AMT}
    tool_result = run_tool(db, "get_column_metadata", args, roles=())

    def hash_for(trail):
        redaction = RedactionResult(text=AUTHOR_INSTRUCTION, redaction_version="metadata-only",
                                    redacted_spans=(), disposition="ok")
        return compute_input_hash(build_llm_inputs(
            redaction, catalog_metadata=build_turn_metadata(_INTENT, trail),
            raw_input_classification="clean"))

    author = FakeLLM()
    author.script(task=AUTHOR_TASK, prompt_id=AUTHOR_PROMPT_ID, input_hash=hash_for([]),
                  responses=[FakeResponse(output={
                      "turn_type": "tool_call",
                      "tool_call": {"tool_name": "get_column_metadata", "arguments": args}})])
    author.script(task=AUTHOR_TASK, prompt_id=AUTHOR_PROMPT_ID,
                  input_hash=hash_for([tool_trail_entry(1, "get_column_metadata", tool_result)]),
                  responses=[FakeResponse(output=_final_turn(_raw()))])

    result = _run(db, author=author)
    assert result.authoring_disposition == "RESOLVED"
    events = _events(db, result.authoring_run_id)
    by_kind = {e[1]: e for e in events}
    assert by_kind["TOOL_CALLED"][3]["tool_name"] == "get_column_metadata"
    recorded = by_kind["TOOL_RESULT_RECORDED"][3]
    assert recorded["result"] == json.loads(json.dumps(tool_result))
    assert recorded["result_hash"] == hashlib.sha256(jcs_dumps(tool_result)).hexdigest()
    # every LLM_CALL_RECORDED links the immutable audit row of the turn it evidences
    refs = [e[2] for e in events if e[1] == "LLM_CALL_RECORDED"]
    assert len(refs) == 2 and all(refs)


def test_an_orchestrator_sql_failure_leaves_the_run_incomplete_never_completed(
        db, monkeypatch) -> None:
    """``append_event`` RAISES on an already-aborted caller transaction, so a terminal write cannot
    be attempted there. The deliberate choice: the exception propagates and the run stays
    INCOMPLETE — the honest outcome ``run_status`` derives from event ABSENCE — rather than being
    swallowed into a false "completed"."""
    seed_authoring_catalog(db)
    run_ids: list[str] = []
    real_open = authoring.open_authoring_run

    def open_spy(conn, **kwargs):
        run_id = real_open(conn, **kwargs)
        run_ids.append(run_id)
        return run_id

    def exploding_read(conn, logical_ref, field_name):
        conn.execute("SELECT 1 FROM a_table_that_does_not_exist")   # aborts the caller's txn
        raise AssertionError("unreachable")                          # pragma: no cover

    monkeypatch.setattr(authoring, "open_authoring_run", open_spy)
    monkeypatch.setattr(authoring, "read_operational_value", exploding_read)

    with pytest.raises(Exception) as excinfo:
        _run(db)
    assert "a_table_that_does_not_exist" in str(excinfo.value)
    db.rollback()                                   # the request handler's own recovery
    assert run_status(db, run_ids[0]) == "incomplete"


def test_the_manifest_stamps_every_policy_version(db) -> None:
    """§H: the manifest carries the versions the run was decided under, so a later policy bump can
    never be mistaken for the one that produced this verdict."""
    seed_authoring_catalog(db)
    result = _run(db)
    row = db.execute("SELECT intent_hash, versions FROM authoring_run WHERE authoring_run_id = %s",
                     (result.authoring_run_id,)).fetchone()
    assert row[0] == authoring_intent_hash(_INTENT)
    assert row[1] == {
        "formula_schema_version": 1, "operation_grammar_version": 1, "output_policy_version": 1,
        "canonicalization_version": 1, "capability_policy_version": 1, "critic_policy_version": 1,
        "disposition_policy_version": 1, "authoring_orchestrator_version": 1}


def test_the_intent_hash_is_content_addressed() -> None:
    other = AuthoringIntent(name=_INTENT.name, hypothesis="a different hypothesis",
                            target_entity=_INTENT.target_entity,
                            target_grain_keys=_INTENT.target_grain_keys)
    assert len(authoring_intent_hash(_INTENT)) == 64
    assert authoring_intent_hash(_INTENT) == authoring_intent_hash(_INTENT)
    assert authoring_intent_hash(_INTENT) != authoring_intent_hash(other)


# ── the non-success arms of Task 6 all map to a typed axis ───────────────────────────────────────

@pytest.mark.parametrize(
    ("resolved", "expected_status"),
    [
        (NeedsAuthority("forked_decision_head"), "needs_authority"),
        (ExternalRequirement("UNIT_PROVISIONING_REQUIRED"), "external_requirement"),
        (InvalidOutput("incompatible_unit"), "invalid_output"),
    ],
)
def test_every_task6_arm_maps_to_its_output_axis(resolved, expected_status) -> None:
    assert classify_output_policy(resolved).status == expected_status
    assert classify_output_policy(resolved).policy is None


def test_an_unknown_output_arm_fails_closed() -> None:
    """Task 6's return union is closed today; if it ever widens, an unrecognized arm must fail
    CLOSED (needs_authority) rather than fall through to a resolution nothing granted."""
    resolution = classify_output_policy(object())
    assert resolution.status == "needs_authority"
    assert resolution.policy is None


def test_the_fail_closed_status_set_matches_task_6s(db) -> None:
    """The orchestrator only ATTRIBUTES a fail-closed read (Task 6 owns the verdict), but it must
    attribute the same set — a drifted copy would report the wrong operand, or none at all."""
    from featuregen.formula import output_authority
    assert authoring._C1_HARD_FAIL_STATUSES == output_authority._HARD_FAIL_STATUSES
