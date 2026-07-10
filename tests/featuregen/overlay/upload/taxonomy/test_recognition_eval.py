"""Phase-1A Task 5 — the evaluation harness + false-narrowing metrics.

These tests exercise the harness MATH, not real-LLM quality: they drive ``evaluate`` with tiny
in-test ``LLMClient`` stubs scripted per hypothesis, so we can prove the harness reports **zero**
false-narrowing for a perfect (oracle) recogniser and **detects** false-narrowing when a recognised
scope drops an expert-relevant recipe.

``FakeLLM`` keys only on ``request.task`` (== ``RECOGNIZER_TASK``) and so cannot return a different
body per hypothesis; the harness calls one task over the whole gold set. So each stub here is a
hand-rolled ``LLMClient`` whose ``.call`` reads ``request.inputs["hypothesis"]`` and returns the
scripted recognition body for that hypothesis. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 5.
"""
from __future__ import annotations

from typing import Any

from featuregen.intake.llm import PROVIDER_OK, LLMRequest, LLMResult
from featuregen.intake.redaction import INPUT_KEY_INTENT
from featuregen.overlay.upload.taxonomy.gold_recognition import GOLD, GoldCase
from featuregen.overlay.upload.taxonomy.recognition_eval import evaluate

_BY_ID = {case.id: case for case in GOLD}


def _case(case_id: str) -> GoldCase:
    return _BY_ID[case_id]


def _classified(primary: str, *, secondary: tuple[str, ...] = ()) -> dict[str, Any]:
    """A well-formed CLASSIFIED body: one primary candidate (+ optional secondaries), each with a
    single evidence span so ``validate_recognition_output`` accepts it."""
    candidates: list[dict[str, Any]] = [
        {
            "use_case_id": primary,
            "relationship": "primary",
            "confidence": "high",
            "evidence_spans": ["scripted evidence span"],
            "rationale": "scripted",
        }
    ]
    candidates += [
        {
            "use_case_id": sec,
            "relationship": "secondary",
            "confidence": "medium",
            "evidence_spans": ["scripted evidence span"],
            "rationale": "scripted",
        }
        for sec in secondary
    ]
    return {"status": "classified", "candidates": candidates, "ambiguity_note": None}


_UNSCOPED_BODY: dict[str, Any] = {"status": "unscoped", "candidates": []}


class _ScriptedStub:
    """A minimal ``LLMClient`` that returns a scripted recognition body keyed on the *hypothesis*.

    Unlike ``FakeLLM`` (task-keyed), this can hand back a different, per-case recognition — exactly
    what the harness needs to be exercised across a gold subset. ``.call`` is deterministic, so the
    harness's second (stability) call returns the same body."""

    def __init__(self, script: dict[str, dict[str, Any]]) -> None:
        self._script = script

    def call(self, request: LLMRequest) -> LLMResult:
        # The recognizer routes through the audited seam, so the hypothesis rides INSIDE the reserved
        # redacted-intent text (the instruction), not a bare "hypothesis" key. Match on the scripted
        # hypothesis embedded in that instruction (each gold hypothesis is a unique string).
        intent = str(request.inputs.get(INPUT_KEY_INTENT, ""))
        body = next((b for hyp, b in self._script.items() if hyp in intent), None)
        if body is None:
            raise KeyError(f"no scripted recognition for intent: {intent[:80]!r}")
        return LLMResult(
            output=dict(body),
            self_reported_scores={},
            call_ref="",
            status=PROVIDER_OK,
        )


def _oracle_stub(subset: tuple[GoldCase, ...]) -> _ScriptedStub:
    """A perfect recogniser: for each case, the CORRECT classification (primary == expected_primary).
    Unscoped cases (no expected primary) get the correct ``unscoped`` body."""
    script: dict[str, dict[str, Any]] = {}
    for case in subset:
        if case.expected_primary is None:
            script[case.hypothesis] = dict(_UNSCOPED_BODY)
        else:
            script[case.hypothesis] = _classified(case.expected_primary)
    return _ScriptedStub(script)


# ── oracle: a perfect recogniser scores no false-narrowing and full recall ────────────────────────

# Straightforward single-leaf cases whose expert-relevant recipes are ALL retained by the primary
# leaf alone — so a correct primary is a fully-recalling scope.
_ORACLE_SUBSET = (_case("G01"), _case("G03"), _case("G04"), _case("G05"))


def test_oracle_reports_no_false_narrowing_and_full_recall(db) -> None:
    report = evaluate(db, _oracle_stub(_ORACLE_SUBSET), gold=_ORACLE_SUBSET)

    assert report.false_narrowing_count == 0
    assert report.false_narrowing_regulated == 0
    assert report.applicability_recall == 1.0
    assert report.primary_accuracy == 1.0
    assert report.top3_recall == 1.0
    # A deterministic stub yields the same scope on the second call → perfectly stable.
    assert report.stability == 1.0
    # No case dropped an expert-relevant recipe.
    assert all(cr.false_narrowed == () for cr in report.per_case)
    assert all(cr.primary_correct for cr in report.per_case)


# ── over-narrow: the harness DETECTS a scope that drops expert-relevant recipes ───────────────────

# G24 is a multi-leaf case: its expert-relevant recipes span credit.early_warning PLUS
# credit.monitoring.limit_management and portfolio_risk.concentration. A scope of the early_warning
# primary ALONE retains only the early-warning recipes and drops the rest — a false narrowing.
_G24 = _case("G24")
_G24_DROPPED = (
    "loan_to_value",
    "group_exposure_aggregation",
    "syndication_concentration",
)


def test_over_narrow_scope_is_detected_as_false_narrowing(db) -> None:
    subset = (_case("G01"), _G24)
    # G01 correct; G24 over-narrowed to its primary leaf only (drops the secondary-leaf recipes).
    stub = _ScriptedStub(
        {
            _case("G01").hypothesis: _classified(_case("G01").expected_primary),  # type: ignore[arg-type]
            _G24.hypothesis: _classified("credit.early_warning"),
        }
    )

    report = evaluate(db, stub, gold=subset)

    # The harness flags exactly the one narrowed case.
    assert report.false_narrowing_count == 1
    assert report.applicability_recall < 1.0

    g24_result = next(cr for cr in report.per_case if cr.case_id == "G24")
    assert g24_result.false_narrowed != ()
    # The dropped expert-relevant recipes surface on the case's result.
    assert set(g24_result.false_narrowed) == set(_G24_DROPPED)
    # G24 is not regulated, so the regulated-specific tally stays clear.
    assert report.false_narrowing_regulated == 0
    # G01 was recognised correctly and lost nothing.
    g01_result = next(cr for cr in report.per_case if cr.case_id == "G01")
    assert g01_result.false_narrowed == ()
    assert g01_result.primary_correct is True


# ── abstention precision: crediting only truly-unscoped abstentions ───────────────────────────────


def test_abstention_precision_penalises_abstaining_on_a_scoped_case(db) -> None:
    subset = (_case("G18"), _case("G01"))  # G18 is unscoped; G01 is a scoped straightforward case.

    # Abstains ONLY on the truly-unscoped G18, classifies G01 correctly → every abstention was right.
    right = _ScriptedStub(
        {
            _case("G18").hypothesis: dict(_UNSCOPED_BODY),
            _case("G01").hypothesis: _classified(_case("G01").expected_primary),  # type: ignore[arg-type]
        }
    )
    assert evaluate(db, right, gold=subset).abstention_precision == 1.0

    # Abstains on BOTH — including the scoped G01 — so only half the abstentions were warranted.
    over_abstain = _ScriptedStub(
        {
            _case("G18").hypothesis: dict(_UNSCOPED_BODY),
            _case("G01").hypothesis: dict(_UNSCOPED_BODY),
        }
    )
    assert evaluate(db, over_abstain, gold=subset).abstention_precision == 0.5
