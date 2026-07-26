from __future__ import annotations

from tests.featuregen.formula.factories import default_output
from tests.featuregen.formula.test_parse import raw_unary_proposal

from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID
from featuregen.formula.authoring import run_authoring
from featuregen.formula.critic import CriticReview
from featuregen.formula.frozen_configuration import freeze_current_configuration
from featuregen.formula.trace import run_status
from featuregen.formula.turns import AuthoringIntent


def test_orchestrator_wires_author_parse_authority_critic_and_trace(db, monkeypatch) -> None:
    monkeypatch.setattr(
        "featuregen.formula.authoring.author_formula",
        lambda *a, **k: (raw_unary_proposal(), []),
    )
    monkeypatch.setattr(
        "featuregen.formula.authoring.critique",
        lambda *a, **k: CriticReview((), "critic_hash", False, None, 1, {}),
    )
    monkeypatch.setattr(
        "featuregen.formula.authoring.resolve_formula_output_policy",
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
        "featuregen.formula.authoring.author_formula",
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


def test_author_exception_is_terminal_technical(db, monkeypatch) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("featuregen.formula.authoring.author_formula", _raise)
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

    monkeypatch.setattr("featuregen.formula.authoring.author_formula", _author)
    monkeypatch.setattr(
        "featuregen.formula.authoring.current_formula_generation_settings",
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
