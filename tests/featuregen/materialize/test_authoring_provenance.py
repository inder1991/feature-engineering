"""How a formula was authored, derived from evidence — and refused when the evidence cannot say.

▲ The case worth reading first is `test_a_RECIPE_ORIGIN_RUN_IS_STILL_LLM_AUTHORED`. It pins the
distinction the whole production-certificate design turns on: where the IDEA came from is not how
the FORMULA was written.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.authoring_provenance import (
    REVIEWED_RECIPE_BLUEPRINT,
    AuthoringMethodUndecidable,
    derive_authoring_method,
)


def test_NO_EVIDENCE_AT_ALL_IS_REFUSED_not_defaulted(db):
    """▲ Never a fallback. A default would be applied exactly when the truth is unclear — which is
    when choosing the wrong production certificate does the damage."""
    with pytest.raises(AuthoringMethodUndecidable, match="establishes no authoring method"):
        derive_authoring_method(db, "far-nothing-was-ever-recorded")


def test_UNRECONCILED_PROVIDER_CALLS_DO_NOT_EVIDENCE_LLM_AUTHORING(db, monkeypatch):
    """Present is not enough; RECONCILED is the bar. An unreconciled dispatch cannot say what was
    sent to the provider, so it cannot establish that a provider authored anything."""
    import featuregen.materialize.authoring_provenance as mod

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_eval._dispatch_identity",
        lambda conn, run_id: (["a-1"], ["c-1"], ["l-1"]))
    monkeypatch.setattr(
        "featuregen.overlay.upload.dispatch_audit.formula_dispatches_reconciled",
        lambda conn, run_id: False)

    with pytest.raises(AuthoringMethodUndecidable, match="cannot say what was sent"):
        mod.derive_authoring_method(db, "far-unreconciled")


def test_BOTH_KINDS_OF_EVIDENCE_IS_A_REFUSAL_not_a_preference(db, monkeypatch):
    """▲ A run whose trace disagrees with itself. Picking either method would certify against
    evidence contradicting the choice, so neither is picked."""
    import featuregen.materialize.authoring_provenance as mod

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_eval._dispatch_identity",
        lambda conn, run_id: (["a-1"], ["c-1"], ["l-1"]))
    monkeypatch.setattr(
        "featuregen.overlay.upload.dispatch_audit.formula_dispatches_reconciled",
        lambda conn, run_id: True)
    monkeypatch.setattr(
        mod, "derive_authoring_method", mod.derive_authoring_method)  # keep the real one

    class _Both:
        def execute(self, *_a, **_k):
            class R:
                def fetchone(self_inner): return (2,)
            return R()

    with pytest.raises(AuthoringMethodUndecidable, match="disagrees with itself"):
        mod.derive_authoring_method(_Both(), "far-both")


def test_UNRECONCILED_CALLS_PLUS_A_BYPASS_IS_A_REFUSAL_not_a_reviewed_blueprint(db, monkeypatch):
    """▲ The gap between the two tests above, and it sealed the STRONGEST claim on the WEAKEST
    evidence.

    The contradiction guard used to be gated on `llm_evidence`, which requires RECONCILED dispatches.
    So a run that attempted a provider, failed to reconcile, AND recorded a reviewed bypass set
    `llm_evidence = False`, skipped the guard, and fell through to `REVIEWED_RECIPE_BLUEPRINT` —
    into an append-only provenance table, from a run whose provider calls could not be accounted for.

    Unreachable while no production caller supplies `reviewed_blueprint`; the deterministic lane is
    what makes both paths live at once. Widened before that lane exists.
    """
    import featuregen.materialize.authoring_provenance as mod

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_eval._dispatch_identity",
        lambda conn, run_id: (["a-1"], ["c-1"], ["l-1"]))
    monkeypatch.setattr(
        "featuregen.overlay.upload.dispatch_audit.formula_dispatches_reconciled",
        lambda conn, run_id: False)

    class _BypassRecorded:
        def execute(self, *_a, **_k):
            class R:
                def fetchone(self_inner): return (1,)
            return R()

    with pytest.raises(AuthoringMethodUndecidable, match="disagrees with itself"):
        mod.derive_authoring_method(_BypassRecorded(), "far-unreconciled-plus-bypass")


def test_the_REFUSAL_NAMES_WHICH_SIDE_WAS_UNRECONCILED(db, monkeypatch):
    """An operator reading this refusal has to know whether the provider half reconciled — the two
    cases have different remedies, and one message for both sends them to the wrong one."""
    import featuregen.materialize.authoring_provenance as mod

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_eval._dispatch_identity",
        lambda conn, run_id: (["a-1"], ["c-1"], ["l-1"]))
    monkeypatch.setattr(
        "featuregen.overlay.upload.dispatch_audit.formula_dispatches_reconciled",
        lambda conn, run_id: False)

    class _BypassRecorded:
        def execute(self, *_a, **_k):
            class R:
                def fetchone(self_inner): return (1,)
            return R()

    with pytest.raises(AuthoringMethodUndecidable, match="UNRECONCILED"):
        mod.derive_authoring_method(_BypassRecorded(), "far-names-the-side")


def test_a_REVIEW_BYPASSED_TRACE_IS_A_REVIEWED_BLUEPRINT(db, monkeypatch):
    """The stage a reviewed blueprint takes INSTEAD of a critic turn is what evidences it."""
    import featuregen.materialize.authoring_provenance as mod

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_eval._dispatch_identity",
        lambda conn, run_id: ([], [], []))
    monkeypatch.setattr(
        "featuregen.overlay.upload.dispatch_audit.formula_dispatches_reconciled",
        lambda conn, run_id: False)

    class _Bypassed:
        def execute(self, *_a, **_k):
            class R:
                def fetchone(self_inner): return (1,)
            return R()

    got = mod.derive_authoring_method(_Bypassed(), "far-blueprint")
    assert got.authoring_method == REVIEWED_RECIPE_BLUEPRINT
    assert len(got.evidence_hash) == 64


def test_the_EVIDENCE_HASH_MOVES_WITH_THE_EVIDENCE(db, monkeypatch):
    """It is what makes the method CHECKABLE rather than asserted — a reader re-derives it from the
    same run and compares. A hash that ignored the evidence would certify nothing."""
    import featuregen.materialize.authoring_provenance as mod

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_eval._dispatch_identity",
        lambda conn, run_id: ([], [], []))
    monkeypatch.setattr(
        "featuregen.overlay.upload.dispatch_audit.formula_dispatches_reconciled",
        lambda conn, run_id: False)

    def _bypass(count):
        class _C:
            def execute(self, *_a, **_k):
                class R:
                    def fetchone(self_inner): return (count,)
                return R()
        return _C()

    assert (mod.derive_authoring_method(_bypass(1), "far-x").evidence_hash
            != mod.derive_authoring_method(_bypass(3), "far-x").evidence_hash)
