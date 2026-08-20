from __future__ import annotations

import pytest
from tests.featuregen.formula.factories import default_output
from tests.featuregen.formula.test_parse import raw_unary_proposal

from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID
from featuregen.formula.control import LeaseFenceLost
from featuregen.formula.critic import CriticReview
from featuregen.formula.frozen_configuration_v1 import freeze_current_configuration
from featuregen.formula.replay_authoring import run_authoring
from featuregen.formula.replay_trace import run_status
from featuregen.formula.turns import AuthoringIntent, AuthorTurnRecord, TurnKind


def test_orchestrator_wires_author_parse_authority_critic_and_trace(db, monkeypatch) -> None:
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.author_formula",
        lambda *a, **k: (raw_unary_proposal(), []),
    )
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.critique",
        lambda *a, **k: CriticReview((), "critic_hash", False, None, 1, {}),
    )
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.resolve_formula_output_policy",
        lambda *a, **k: default_output(),
    )
    result = run_authoring(
        db,
        AuthoringIntent("spend", "customer spend", "customer"),
        object(),
        object(),
        actor=None,
    )
    assert result.authoring_disposition == "NEEDS_REVIEW"
    # The fixture's advisory unit is "ratio", while the authoritative output has no unit.
    assert result.expectation_status == "mismatch"
    assert result.candidate_formula is not None
    assert result.candidate_formula_hash
    assert run_status(db, result.authoring_run_id) == "completed"


def test_author_failure_is_terminal_technical(db, monkeypatch) -> None:
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.author_formula",
        lambda *a, **k: (None, []),
    )
    result = run_authoring(
        db,
        AuthoringIntent("spend", "customer spend", "customer"),
        object(),
        object(),
        actor=None,
    )
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert run_status(db, result.authoring_run_id) == "failed"
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.author_formula",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("terminal author call was repeated")),
    )
    replayed = run_authoring(
        db,
        AuthoringIntent("spend", "customer spend", "customer"),
        object(),
        object(),
        actor=None,
        authoring_run_id=result.authoring_run_id,
    )
    assert replayed == result


def test_author_exception_is_terminal_technical(db, monkeypatch) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("featuregen.formula.replay_authoring.author_formula", _raise)
    result = run_authoring(
        db,
        AuthoringIntent("spend", "customer spend", "customer"),
        object(),
        object(),
        actor=None,
    )
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert run_status(db, result.authoring_run_id) == "failed"


def test_frozen_configuration_drift_blocks_before_author_dispatch(db, monkeypatch) -> None:
    frozen = freeze_current_configuration(
        generation_settings={"provider": "fake", "model": "test"},
        author_instruction=AUTHOR_INSTRUCTION,
        author_prompt_id=AUTHOR_PROMPT_ID,
    )
    called = False

    def _author(*args, **kwargs):
        nonlocal called
        called = True
        return raw_unary_proposal(), []

    monkeypatch.setattr("featuregen.formula.replay_authoring.author_formula", _author)
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.current_formula_generation_settings",
        lambda: {"provider": "fake", "model": "changed"},
    )
    result = run_authoring(
        db,
        AuthoringIntent("spend", "customer spend", "customer"),
        object(),
        object(),
        actor=None,
        frozen_configuration=frozen,
    )
    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert not called
    assert run_status(db, result.authoring_run_id) == "failed"


def test_resume_after_parsed_proposal_repeats_no_provider_stage(db, monkeypatch) -> None:
    run_id = "far_resume_after_proposal"
    raw = raw_unary_proposal()
    calls = {"author": 0, "critic": 0}

    def _author(*args, **kwargs):
        calls["author"] += 1
        kwargs["on_turn"](AuthorTurnRecord(
            index=0,
            kind=TurnKind.FINAL_PROPOSAL,
            llm_call_ref=None,
            tool_name=None,
            tool_result=None,
            output={"turn_type": "final_proposal", "final_proposal": raw},
            provider_calls=1,
            usage={"input_tokens": 10, "output_tokens": 5},
            tool_context_hash="empty-trail-hash",
        ))
        return raw, []

    def _interrupt(_proposal):
        raise LeaseFenceLost("simulated handoff")

    monkeypatch.setattr("featuregen.formula.replay_authoring.author_formula", _author)
    with pytest.raises(LeaseFenceLost):
        run_authoring(
            db,
            AuthoringIntent("spend", "customer spend", "customer"),
            object(),
            object(),
            actor=None,
            authoring_run_id=run_id,
            proposal_validator=_interrupt,
        )
    assert calls["author"] == 1

    def _author_must_not_repeat(*args, **kwargs):
        raise AssertionError("completed author stage was repeated")

    def _critic(*args, **kwargs):
        calls["critic"] += 1
        return CriticReview((), "critic_hash", False, None, 1, {})

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.author_formula", _author_must_not_repeat)
    monkeypatch.setattr("featuregen.formula.replay_authoring.critique", _critic)
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.resolve_formula_output_policy",
        lambda *a, **k: default_output(),
    )
    result = run_authoring(
        db,
        AuthoringIntent("spend", "customer spend", "customer"),
        object(),
        object(),
        actor=None,
        authoring_run_id=run_id,
        proposal_validator=lambda _proposal: (),
    )
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert calls == {"author": 1, "critic": 1}

    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.critique",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("completed critic stage was repeated")),
    )
    replayed = run_authoring(
        db,
        AuthoringIntent("spend", "customer spend", "customer"),
        object(),
        object(),
        actor=None,
        authoring_run_id=run_id,
        proposal_validator=lambda _proposal: (),
    )
    assert replayed.authoring_disposition == result.authoring_disposition
    assert replayed.candidate_formula_hash == result.candidate_formula_hash
