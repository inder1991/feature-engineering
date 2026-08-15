"""``run_authoring_v2_replay`` — the replay-shaped v2 orchestrator the live worker can call.

The successor charter's increment 2. What is under test is the SEAM, not a model: every run here
is a recorded ``FakeLLM`` fixture, exactly as A3's are, because Anthropic billing is exhausted.

The tests are arranged so a trivially-wrong implementation fails one of them by name:

* an orchestrator that skipped a stage, or invented its own names, cannot replay at all —
  ``replay_trace._verify_stage_transition`` refuses the transition, and the stage list is asserted;
* one that folded a provider/billing failure into a grammar verdict fails the D-10 test;
* one that verified the **v1** frozen configuration would let a v1-frozen work item author a v2
  formula under the v1 prompt identity — ``test_a_v1_frozen_configuration_is_DRIFT_for_a_v2_run``;
* one that offered half the pair, or an artifact on a refused outcome, is caught by
  ``derive_disposition_v2``'s own guards and asserted here rather than left to them.
"""
from __future__ import annotations

import hashlib
import inspect

import pytest
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_DT,
    TABLE_REF,
)

from featuregen.formula import replay_authoring
from featuregen.formula.author import AUTHOR_TASK
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.frozen_configuration import (
    freeze_current_configuration,
    freeze_current_configuration_v2,
)
from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2, OperandFactsV2
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.formula.replay_trace import run_status
from featuregen.formula.result import IncoherentResultError
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import PROVIDER_NON_RETRYABLE, FakeLLM, FakeResponse

_INTENT = AuthoringIntent("posted debit", "accounts posting more debit value attrite", "account",
                          target_grain_keys=(REF_CIF,))


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-hermetic, ``test_authoring_v2``'s rationale: with an ambient ``FEATUREGEN_DSN`` the
    trace COMMITs on a fresh connection and its rows can physically never be cleaned up."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


# ── the shapes ───────────────────────────────────────────────────────────────────────────────────


def _window(**overrides) -> dict:
    return {"event_time_ref": REF_DT, "basis": "trailing", "length": 90, "unit": "day",
            "start_inclusive": "inclusive", "end_inclusive": "exclusive",
            "timezone": "Asia/Dubai", "empty_window": "null", "null_input": "ignore",
            "offset_periods": 0, **overrides}


def _expr(aggregation: str = "sum", operand: str | None = REF_AMT, **overrides) -> dict:
    return {"aggregation": aggregation, "operand": operand,
            "source_relation": {"table_ref": TABLE_REF}, "filter": None, "window": _window(),
            "aggregation_argument": None, "second_operand": None, "authority_refs": None,
            **overrides}


def _raw(body: dict | None = None, **overrides) -> dict:
    return {"formula_schema_version": 2, "operation_grammar_version": 1,
            "canonicalization_version": 1,
            "grain": {"entity": "account", "keys": [REF_CIF]},
            "body": body if body is not None else {
                "final_operation": "identity", "expr": _expr()},
            "parameters": [], "expected_output": None, "allocation_policy_ref": "",
            "decimal": {"precision": 38, "scale": 6, "rounding": "half_even",
                        "overflow": "error"},
            **overrides}


def _client(raw: dict | None = None, findings=None, **response_kwargs) -> FakeLLM:
    return FakeLLM(script={
        AUTHOR_TASK: FakeResponse(
            output=None if raw is None else {
                "turn_type": "final_proposal", "final_proposal": raw},
            **response_kwargs),
        CRITIC_TASK: FakeResponse(output={"findings": list(findings or [])}),
    })


def _monetary_facts(_proposal):
    """A governed, ref-keyed bundle — the shape ``FrozenRecipeReadContext.formula_facts_v2``
    returns, and the shape ``resolve_output_v2`` reads."""
    return {REF_AMT: OperandFactsV2(
        logical_type="decimal", unit="monetary", currency="fixed:AED")}, ()


def _run(db, *, raw: dict | None = None, client: FakeLLM | None = None, run_id: str,
         facts_reader=_monetary_facts, **kwargs):
    llm = client if client is not None else _client(raw if raw is not None else _raw())
    return run_authoring_v2_replay(
        db, _INTENT, llm, llm, actor=None, authoring_run_id=run_id,
        facts_reader=facts_reader,
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref}, **kwargs)


def _scripted_author(raw: dict):
    """An author stage that PERSISTS its turn, which the replay store requires: every
    ``AUTHOR_PROPOSAL_PARSED`` must follow an ``AUTHOR_TURN_n`` or the checkpoint refuses the
    transition. Used where a test must drive the loop without a provider call at all."""
    def _author(*args, **kwargs):
        from featuregen.formula.turns import AuthorTurnRecord, TurnKind

        kwargs["on_turn"](AuthorTurnRecord(
            index=0, kind=TurnKind.FINAL_PROPOSAL, llm_call_ref=None, tool_name=None,
            tool_result=None, output={"turn_type": "final_proposal", "final_proposal": raw},
            provider_calls=1, usage={"input_tokens": 10, "output_tokens": 5},
            tool_context_hash="empty-trail-hash"))
        return raw, []

    return _author


def _stages(db, run_id: str) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT stage FROM formula_authoring_trace_event WHERE authoring_run_id=%s ORDER BY seq",
        (run_id,)).fetchall()]


# ── the happy path ───────────────────────────────────────────────────────────────────────────────


def test_the_orchestrator_wires_author_parse_authority_critic_and_trace(db) -> None:
    """One governed v2 run, end to end, through the REPLAY store — and the stage list is v1's,
    because ``replay_trace`` enforces exactly that sequence and a v2 run that invented its own
    could never be resumed."""
    result = _run(db, run_id="far_v2_happy")

    assert result.authoring_disposition == "RESOLVED"
    assert result.candidate_proposal is not None
    assert result.candidate_output == FormulaOutputPolicyV2(
        output_type="decimal", unit="monetary", currency="fixed:AED",
        output_additivity=result.candidate_output.output_additivity,
        external_type_required=False)
    assert result.candidate_proposal_hash and len(result.candidate_proposal_hash) == 64
    assert result.disposition_policy_version == 1
    assert run_status(db, "far_v2_happy") == "completed"
    assert _stages(db, "far_v2_happy") == [
        "AUTHOR_TURN_0", "AUTHOR_PROPOSAL_PARSED", "EXPECTATION_VALIDATED",
        "CRITIC_COMPLETED", "OUTPUT_POLICY_RESOLVED", "TERMINAL"]


def test_the_terminal_artifact_is_the_PAIR_never_half_of_one(db) -> None:
    """A3's plan defect 2: there is no ``TypedFormulaV2`` to fuse the proposal and its policy, so
    the artifact is the pair and ``derive_disposition_v2`` refuses either half alone."""
    result = _run(db, run_id="far_v2_pair")
    assert (result.candidate_proposal is not None) and (result.candidate_output is not None)

    from featuregen.formula.result import AuthoringAxes
    from featuregen.formula.result_v2 import derive_disposition_v2

    resolved = AuthoringAxes("ok", "ok", "resolved", "not_provided", "clean", "ok")
    with pytest.raises(IncoherentResultError):
        derive_disposition_v2(resolved, authoring_run_id="far_v2_pair",
                              candidate_proposal=result.candidate_proposal)
    with pytest.raises(IncoherentResultError):
        derive_disposition_v2(resolved, authoring_run_id="far_v2_pair",
                              candidate_output=result.candidate_output)


def test_the_facts_reader_seam_is_keyed_by_ref_so_the_currency_survives(db) -> None:
    """The seam A3's plan defect 4 is about, at the orchestrator: hand it v1's PATH keying and the
    monetary output comes back with no currency — a policy assembled out of nothing."""
    path_keyed = _run(
        db, run_id="far_v2_pathkey",
        facts_reader=lambda _p: ({"body.expr": OperandFactsV2(
            logical_type="decimal", unit="monetary", currency="fixed:AED")}, ()))
    # Worse than a missing currency: with the operand resolving to EMPTY facts, the output type
    # itself has no governed authority, so the run needs external validation and there is no
    # authoritative policy at all. A path-keyed bundle silently un-governs the whole output.
    assert path_keyed.output_status == "external_requirement"
    assert path_keyed.candidate_output is None
    assert path_keyed.output_requirements == ("EXTERNAL_TYPE_VALIDATION_REQUIRED",)

    ref_keyed = _run(db, run_id="far_v2_refkey")
    assert ref_keyed.output_status == "resolved"
    assert ref_keyed.candidate_output.currency == "fixed:AED"


def test_a_fail_closed_governed_read_is_needs_authority_and_carries_no_policy(db) -> None:
    from featuregen.formula.result import AuthorityFailure

    result = _run(
        db, run_id="far_v2_failclosed",
        facts_reader=lambda _p: ({}, (AuthorityFailure("fork", REF_AMT, "unit"),)))
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.output_status == "needs_authority"
    assert result.candidate_output is None, "a guess must never be laundered into authority"
    assert result.candidate_proposal is not None, "there is nothing to review without it"
    assert [f.reason for f in result.authority_failures] == ["fork"]


# ── the refusals, each one honest about WHAT it refused ──────────────────────────────────────────


def test_a_provider_failure_is_technical_and_carries_no_artifact(db) -> None:
    result = _run(db, run_id="far_v2_provider", client=_client(None))
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.candidate_proposal is None and result.candidate_output is None
    assert run_status(db, "far_v2_provider") == "failed"


def test_a_billing_refusal_is_technical_never_a_capability_or_grammar_verdict(db) -> None:
    """D-10 and the ``3219a209`` precedent, restated at the REPLAY layer. A payment problem must
    never be written down as a durable statement about the v2 grammar: the audited seam returns
    ``output=None`` for a non-retryable provider refusal, and every ``output=None`` lands
    TECHNICAL with both verdict axes explicitly ``ok``."""
    billing = FakeLLM(script={
        AUTHOR_TASK: FakeResponse(output={}, provider_status=PROVIDER_NON_RETRYABLE),
        CRITIC_TASK: FakeResponse(output={"findings": []})})
    result = _run(db, run_id="far_v2_billing", client=billing)
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.capability_status == "ok" and result.structural_status == "ok"
    assert result.capability_reason is None
    assert result.candidate_proposal is None and result.candidate_output is None


def test_a_proposal_outside_the_v2_grammar_is_UNSUPPORTED_not_REJECTED(db) -> None:
    """``unsupported != invalid``: a well-formed request for an aggregate the grammar does not
    cover is a capability gap, not malformation. The RAW dict is read before parse for exactly
    this, because the shape gate would report it as an enum violation."""
    outside = _raw({"final_operation": "identity", "expr": _expr("median_of_medians")})
    result = _run(db, run_id="far_v2_unsupported", raw=outside)
    assert result.authoring_disposition == "UNSUPPORTED"
    assert result.structural_status == "unsupported_operation"
    assert run_status(db, "far_v2_unsupported") == "completed", (
        "v1's replay laws: an unsupported run COMPLETED — it is an answer, not a failure")


def test_a_cross_source_proposal_is_UNSUPPORTED_with_its_reason(db) -> None:
    cross = _raw({"final_operation": "difference", "minuend": _expr(),
                  "subtrahend": _expr(
                      operand="other::public.txns.txn_amt",
                      source_relation={"table_ref": "other::public.txns"},
                      window=_window(event_time_ref="other::public.txns.txn_dt"))})
    result = _run(db, run_id="far_v2_crosssource", raw=cross)
    assert result.authoring_disposition == "UNSUPPORTED"
    assert result.capability_status == "unsupported_capability"
    assert result.capability_reason.startswith("multiple_catalog_sources:")


def test_a_semantically_invalid_proposal_is_REJECTED_and_the_run_FAILED(db) -> None:
    """A body the WIRE admits and ``validate_semantics_v2`` refuses — here a grain key that names
    a table rather than a column. This is where v1's replay laws differ from ``authoring_v2``'s:
    the same disposition, and the replay store records the run ``failed`` rather than
    ``completed``, because ``run_status`` is what a recovering worker reads."""
    result = _run(db, run_id="far_v2_invalid",
                  raw=_raw(grain={"entity": "account", "keys": [TABLE_REF]}))
    assert result.authoring_disposition == "REJECTED"
    assert result.structural_status == "invalid_formula"
    assert result.candidate_proposal is None and result.candidate_output is None
    assert run_status(db, "far_v2_invalid") == "failed"


def test_the_wire_pins_the_version_so_a_v1_body_never_becomes_a_false_grammar_verdict(
        db, monkeypatch) -> None:
    """A3's finding, restated at the replay layer and asserted in BOTH halves.

    (a) The v2 turn schema pins ``formula_schema_version`` to 2, so a v1-declared body fails
    RESPONSE validation and the loop simply never gets a v2 proposal — the run ends TECHNICAL,
    which says nothing about the grammar. (b) The orchestrator's own version guard stays as
    defence in depth for a non-provider caller: fed the same body directly, it is
    ``invalid_formula`` — the wrong CONTRACT for the run that was opened, not a missing
    capability, because the v1 generation is fully supported elsewhere."""
    through_the_wire = _run(db, run_id="far_v2_wrongcontract_wire",
                            raw=_raw(formula_schema_version=1))
    assert through_the_wire.authoring_disposition == "TECHNICAL_FAILURE"
    assert through_the_wire.structural_status == "ok", "nothing parsed, so nothing is claimed"

    v1_body = _raw(formula_schema_version=1)
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.author_formula",
        lambda *a, **k: (v1_body, []))
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.critique",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a refused proposal must never reach the critic")))
    direct = _run(db, run_id="far_v2_wrongcontract_direct")
    assert direct.authoring_disposition == "REJECTED"
    assert direct.structural_status == "invalid_formula"


def test_an_expectation_violation_is_invalid_formula_and_names_its_codes(db) -> None:
    """The proposal-validator seam — the worker injects ``recipe_expectation_validator_v2`` here,
    and a substituted operand must never be laundered into a governed result."""
    result = _run(db, run_id="far_v2_expectation",
                  proposal_validator=lambda _p: ("OPERAND_NOT_PRESERVED",))
    assert result.authoring_disposition == "REJECTED"
    payload = db.execute(
        "SELECT payload FROM formula_authoring_trace_event "
        "WHERE authoring_run_id='far_v2_expectation' AND stage='TERMINAL'").fetchone()[0]
    assert payload["reason"] == "recipe_expectation_not_preserved"
    assert payload["violations"] == ["OPERAND_NOT_PRESERVED"]


def test_a_broken_critic_is_technical_never_clean(db) -> None:
    llm = FakeLLM(script={
        AUTHOR_TASK: FakeResponse(output={
            "turn_type": "final_proposal", "final_proposal": _raw()}),
        CRITIC_TASK: FakeResponse(output=None)})
    result = run_authoring_v2_replay(
        db, _INTENT, llm, llm, actor=None, authoring_run_id="far_v2_critic",
        facts_reader=_monetary_facts)
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.candidate_output is None


def test_a_blocking_critic_finding_prevents_auto_resolved(db) -> None:
    result = _run(db, run_id="far_v2_blocking",
                  client=_client(_raw(), findings=[{"code": "WINDOW_INTENT_MISMATCH",
                                                    "operand": REF_AMT, "detail": "no"}]))
    assert result.critic_status == "blocking"
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_output is not None, (
        "the output DID resolve — a blocking critic makes it reviewable, not unauthored")


# ── the frozen configuration ─────────────────────────────────────────────────────────────────────


_SETTINGS = {"provider": "fake", "model": "test"}


def test_a_v2_frozen_configuration_verifies_and_never_reaches_the_provider_on_drift(
        db, monkeypatch) -> None:
    frozen = freeze_current_configuration_v2(generation_settings=_SETTINGS)
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.current_formula_generation_settings",
        lambda: _SETTINGS)
    assert _run(db, run_id="far_v2_config_ok",
                frozen_configuration=frozen).authoring_disposition == "RESOLVED"

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.current_formula_generation_settings",
        lambda: {"provider": "fake", "model": "changed"})
    called = False

    def _never(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("a drifted configuration must never reach the provider")

    monkeypatch.setattr("featuregen.formula.replay_authoring_v2.author_formula", _never)
    drifted = _run(db, run_id="far_v2_config_drift", frozen_configuration=frozen)
    assert drifted.authoring_disposition == "TECHNICAL_FAILURE"
    assert not called
    assert run_status(db, "far_v2_config_drift") == "failed"


def test_a_v1_frozen_configuration_is_DRIFT_for_a_v2_run(db, monkeypatch) -> None:
    """THE reason ``freeze_current_configuration_v2`` had to exist. A work item frozen under the
    v1 author identity (``AUTHOR_INSTRUCTION`` / ``formula_author_turn``) is not a v2 work item's
    configuration, and authoring it as one would audit a v2 run under a prompt no v2 run uses —
    A3 made the two constants distinct for exactly this."""
    from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID

    v1_frozen = freeze_current_configuration(
        generation_settings=_SETTINGS, author_instruction=AUTHOR_INSTRUCTION,
        author_prompt_id=AUTHOR_PROMPT_ID)
    v2_frozen = freeze_current_configuration_v2(generation_settings=_SETTINGS)
    assert v1_frozen.configuration_hash != v2_frozen.configuration_hash
    assert v1_frozen.author.prompt_id != v2_frozen.author.prompt_id
    assert v1_frozen.author.output_schema_id != v2_frozen.author.output_schema_id
    assert v1_frozen.operation_grammar_hash != v2_frozen.operation_grammar_hash

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.current_formula_generation_settings",
        lambda: _SETTINGS)
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.author_formula",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a v1-frozen work item must never author a v2 formula")))
    result = _run(db, run_id="far_v2_v1config", frozen_configuration=v1_frozen)
    assert result.authoring_disposition == "TECHNICAL_FAILURE"


# ── replay ───────────────────────────────────────────────────────────────────────────────────────


def test_a_completed_run_replays_without_reissuing_any_provider_call(db, monkeypatch) -> None:
    """The whole point of the replay shape: a worker that lost its lease after the terminal must
    recover the SAME result — re-folded through ``derive_disposition_v2``, with the proposal
    re-parsed and its content hash re-verified — without asking a provider anything."""
    from featuregen.formula.critic import CriticReview

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.author_formula", _scripted_author(_raw()))
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.critique",
        lambda *a, **k: CriticReview((), "critic_hash_v2", False, None, 1, {}))
    first = _run(db, run_id="far_v2_replay_done")
    assert first.authoring_disposition == "RESOLVED"

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.author_formula",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a terminal run re-issued a provider call")))
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.critique",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a terminal run re-issued a critic call")))
    replayed = _run(db, run_id="far_v2_replay_done")
    assert replayed == first


def test_resume_after_the_parsed_proposal_repeats_no_provider_stage(db, monkeypatch) -> None:
    from featuregen.formula.control import LeaseFenceLost
    from featuregen.formula.turns import AuthorTurnRecord, TurnKind

    run_id = "far_v2_replay_resume"
    raw = _raw()
    calls = {"author": 0, "critic": 0}

    def _author(*args, **kwargs):
        calls["author"] += 1
        kwargs["on_turn"](AuthorTurnRecord(
            index=0, kind=TurnKind.FINAL_PROPOSAL, llm_call_ref=None, tool_name=None,
            tool_result=None, output={"turn_type": "final_proposal", "final_proposal": raw},
            provider_calls=1, usage={"input_tokens": 10, "output_tokens": 5},
            tool_context_hash="empty-trail-hash"))
        return raw, []

    monkeypatch.setattr("featuregen.formula.replay_authoring_v2.author_formula", _author)
    with pytest.raises(LeaseFenceLost):
        _run(db, run_id=run_id,
             proposal_validator=lambda _p: (_ for _ in ()).throw(
                 LeaseFenceLost("simulated handoff")))
    assert calls["author"] == 1

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.author_formula",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a completed author stage was repeated")))

    def _critique(*args, **kwargs):
        from featuregen.formula.critic import CriticReview

        calls["critic"] += 1
        return CriticReview((), "critic_hash_v2", False, None, 1, {})

    monkeypatch.setattr("featuregen.formula.replay_authoring_v2.critique", _critique)
    resumed = _run(db, run_id=run_id, proposal_validator=lambda _p: ())
    assert resumed.authoring_disposition == "RESOLVED"
    assert calls == {"author": 1, "critic": 1}

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.critique",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a completed critic stage was repeated")))
    replayed = _run(db, run_id=run_id, proposal_validator=lambda _p: ())
    assert replayed.candidate_proposal_hash == resumed.candidate_proposal_hash
    assert replayed.candidate_output == resumed.candidate_output


def test_a_tampered_terminal_proposal_refuses_to_replay(db, monkeypatch) -> None:
    """The replayed identity check: the restored artifact's content hash must equal the one the
    terminal recorded, or the run needs a human rather than a silent re-verdict.

    Driven at ``_restore_terminal_result`` because the trace store is WRITE-ONCE — a database
    trigger refuses ``UPDATE`` on ``formula_authoring_trace_event`` outright, which is a stronger
    guarantee than this test could have arranged and is recorded here rather than worked around."""
    from featuregen.formula.control import RecoveryRequiresReconciliation
    from featuregen.formula.replay_authoring_v2 import _restore_terminal_result, _terminal_payload
    from featuregen.formula.replay_trace import VerifiedCheckpoint

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.author_formula", _scripted_author(_raw()))
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.critique",
        lambda *a, **k: __import__(
            "featuregen.formula.critic", fromlist=["CriticReview"]).CriticReview(
                (), "critic_hash_v2", False, None, 1, {}))
    result = _run(db, run_id="far_v2_replay_tamper")
    with pytest.raises(Exception, match="write-once"), db.transaction():
        db.execute("UPDATE formula_authoring_trace_event SET stage='X' "
                   "WHERE authoring_run_id='far_v2_replay_tamper' AND stage='TERMINAL'")
    honest = _terminal_payload(result)
    output_material = db.execute(
        "SELECT payload FROM formula_authoring_trace_event "
        "WHERE authoring_run_id='far_v2_replay_tamper' AND stage='OUTPUT_POLICY_RESOLVED'"
    ).fetchone()[0]["result"]

    def _checkpoint(terminal: dict) -> VerifiedCheckpoint:
        return VerifiedCheckpoint(
            "far_v2_replay_tamper", "TERMINAL", 6, (), (), _raw(), None,
            output_material, terminal, 0)

    assert _restore_terminal_result(
        _checkpoint(honest), "far_v2_replay_tamper") == result

    tampered = {**honest, "candidate_proposal_hash": "deadbeef"}
    with pytest.raises(RecoveryRequiresReconciliation, match="content hash changed"):
        _restore_terminal_result(_checkpoint(tampered), "far_v2_replay_tamper")


# ── the v1 orchestrator is untouched ─────────────────────────────────────────────────────────────


def test_the_v1_replay_orchestrator_is_BYTE_IDENTICAL() -> None:
    """The frozen half, pinned the way A4 increment 1 pinned the v1 egress arm: live work items
    were sealed against these exact bytes, and a v2 sibling is not a licence to edit them.

    If this fails, the v1 orchestrator changed. That may be right — but it is a separate,
    argued change, not a side effect of the v2 work."""
    digest = hashlib.sha256(
        inspect.getsource(replay_authoring.run_authoring).encode("utf-8")).hexdigest()
    assert digest == "96c3dbc3dd83a5f8bf7a721107aff06aa51976de7f117c71f9dd9b603fc5af16"
