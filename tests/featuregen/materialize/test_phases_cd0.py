"""C-D0 — four phases with DURABLE boundaries, and the publisher-refusal bug fixed at the source.

The gate is *"the four signatures exist and no one of them can reach another's side effects"*. A
boolean "is separated" would be satisfied by four empty Protocols and prove nothing, so the
separation is expressed as a PARTITION over a named side-effect vocabulary and tested as one.
"""
from __future__ import annotations

import inspect

import pytest

from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.compile import chain as chain_module
from featuregen.materialize.compile.phases import (
    NON_TRANSACTIONAL,
    PHASE_SIDE_EFFECTS,
    ChainPhase,
    ExecuteVerification,
    GenerateArtifact,
    PhaseOutcomeV1,
    PublishVerifiedOutput,
    RequestVerification,
    SideEffect,
    owns,
    run_all_phases,
)


class _UnprovenReport:
    """The shape `_unproven_detail` reads — an L0 report that could not run."""

    report_id = "rep-1"
    findings = ()
    status = None


def _unproven_report() -> _UnprovenReport:
    return _UnprovenReport()


def _ok(phase: ChainPhase, ref: str = "ref-1") -> PhaseOutcomeV1:
    return PhaseOutcomeV1(phase=phase, succeeded=True, refusal_code="", detail="",
                          durable_reference=ref)


# ══ THE GATE — the four exist and their effects PARTITION ════════════════════════════════════════
def test_the_four_signatures_exist():
    for protocol, method in ((GenerateArtifact, "generate_artifact"),
                             (RequestVerification, "request_verification"),
                             (ExecuteVerification, "execute_verification"),
                             (PublishVerifiedOutput, "publish_verified_output")):
        assert hasattr(protocol, method)
    assert {p.value for p in ChainPhase} == {
        "generate_artifact", "request_verification", "execute_verification",
        "publish_verified_output"}


def test_NO_PHASE_CAN_REACH_ANOTHERS_SIDE_EFFECTS():
    """Expressed as a partition rather than a claim: every effect belongs to exactly one phase."""
    assert set(PHASE_SIDE_EFFECTS) == set(ChainPhase)
    owned = [e for effects in PHASE_SIDE_EFFECTS.values() for e in effects]
    assert len(owned) == len(set(owned)), "an effect owned by two phases is not a partition"
    assert set(owned) == set(SideEffect), "every effect must belong to some phase"


def test_PUBLISHER_SELECTION_BELONGS_ONLY_TO_PHASE_FOUR():
    """This placement is what makes the publisher-refusal bug UNREPRESENTABLE rather than merely
    fixed: a phase that has not selected a publisher cannot discard the selection's verdict."""
    assert owns(ChainPhase.PUBLISH_VERIFIED_OUTPUT, SideEffect.SELECT_PUBLISHER)
    for phase in (ChainPhase.GENERATE_ARTIFACT, ChainPhase.REQUEST_VERIFICATION,
                  ChainPhase.EXECUTE_VERIFICATION):
        assert not owns(phase, SideEffect.SELECT_PUBLISHER), phase


def test_PHASE_ONE_DOES_NOT_SUBMIT_OR_PUBLISH():
    """"Compile, render, run L0, persist the sealed artifact" — and stop. The artifact may sit
    unverified indefinitely, which is a requirement rather than an accident."""
    first = PHASE_SIDE_EFFECTS[ChainPhase.GENERATE_ARTIFACT]
    assert first == {SideEffect.SEAL_ARTIFACT, SideEffect.RECORD_GENERATION, SideEffect.RUN_L0}
    for forbidden in (SideEffect.SUBMIT_TO_CLUSTER, SideEffect.SELECT_PUBLISHER,
                      SideEffect.SWAP_ACTIVE_REVISION, SideEffect.RECORD_PUBLICATION):
        assert forbidden not in first


def test_the_NON_ROLLBACK_ABLE_effects_are_named():
    """The honest half of the boundary argument — a Hadoop submission, a sealed tree write and a
    pointer swap are not undone by a database rollback, whatever transaction wraps them."""
    assert NON_TRANSACTIONAL == {SideEffect.SUBMIT_TO_CLUSTER, SideEffect.SEAL_ARTIFACT,
                                 SideEffect.SWAP_ACTIVE_REVISION}
    assert SideEffect.SUBMIT_TO_CLUSTER in PHASE_SIDE_EFFECTS[ChainPhase.EXECUTE_VERIFICATION]


# ══ the facade composes WITHOUT wrapping ═════════════════════════════════════════════════════════
def test_THE_FACADE_OPENS_NO_TRANSACTION():
    """It takes no connection and opens none — composing the phases must stay available without
    turning them back into one rollback unit."""
    parameters = inspect.signature(run_all_phases).parameters
    assert "conn" not in parameters, "it cannot open a transaction without a connection"
    # the BODY, not the docstring — which necessarily discusses transactions
    body = inspect.getsource(run_all_phases).split('"""')[2]
    assert "transaction" not in body
    assert "with " not in body


def test_the_facade_returns_WHAT_HAPPENED_up_to_a_refusal():
    """Each phase commits its own work, so a later refusal does not unwind an earlier success."""

    class _Gen:
        def generate_artifact(self, **kw):
            return _ok(ChainPhase.GENERATE_ARTIFACT, "gen-1")

    class _Req:
        def request_verification(self, **kw):
            return PhaseOutcomeV1(
                phase=ChainPhase.REQUEST_VERIFICATION, succeeded=False,
                refusal_code="ARTIFACT_NOT_SEALED", detail="", durable_reference="")

    class _Exec:
        def execute_verification(self, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("phase 3 ran after phase 2 refused")

    class _Pub:
        def publish_verified_output(self, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("phase 4 ran after phase 2 refused")

    outcomes = run_all_phases(
        _Gen(), _Req(), _Exec(), _Pub(),
        generation_authorization_revision_id="gar-1", sealed_artifact_hash="sha256:a",
        requested_by="alice", environment_id="hdfc-local", expected_active_revision_id=None)

    assert [o.phase for o in outcomes] == [
        ChainPhase.GENERATE_ARTIFACT, ChainPhase.REQUEST_VERIFICATION]
    assert outcomes[0].succeeded, "phase 1's committed work is still reported"
    assert not outcomes[1].succeeded


def test_each_phase_RESUMES_FROM_the_previous_durable_reference():
    """The point of separate boundaries: a later phase names what an earlier one committed."""
    seen: dict[str, str] = {}

    class _Gen:
        def generate_artifact(self, **kw):
            return _ok(ChainPhase.GENERATE_ARTIFACT, "gen-1")

    class _Req:
        def request_verification(self, **kw):
            return _ok(ChainPhase.REQUEST_VERIFICATION, "vreq-7")

    class _Exec:
        def execute_verification(self, *, verification_request_id, attempt):
            seen["request"] = verification_request_id
            return _ok(ChainPhase.EXECUTE_VERIFICATION, "vor-9")

    class _Pub:
        def publish_verified_output(self, *, verified_output_revision_id, **kw):
            seen["verified"] = verified_output_revision_id
            return _ok(ChainPhase.PUBLISH_VERIFIED_OUTPUT, "active-3")

    outcomes = run_all_phases(
        _Gen(), _Req(), _Exec(), _Pub(),
        generation_authorization_revision_id="gar-1", sealed_artifact_hash="sha256:a",
        requested_by="alice", environment_id="hdfc-local", expected_active_revision_id="active-2")

    assert seen == {"request": "vreq-7", "verified": "vor-9"}
    assert len(outcomes) == 4


def test_PHASE_FOUR_TAKES_THE_ENVIRONMENT_AND_THE_EXPECTED_REVISION():
    """Both, because the active-revision read is environment-scoped and the CAS baseline must come
    from the caller's own environment."""
    parameters = inspect.signature(PublishVerifiedOutput.publish_verified_output).parameters
    assert {"environment_id", "expected_active_revision_id"} <= set(parameters)


# ══ the outcome cannot lie ═══════════════════════════════════════════════════════════════════════
def test_a_succeeded_outcome_cannot_carry_a_refusal_code():
    with pytest.raises(ValueError, match="advance past a phase that refused"):
        PhaseOutcomeV1(phase=ChainPhase.GENERATE_ARTIFACT, succeeded=True,
                       refusal_code="X", detail="", durable_reference="r")


def test_a_refusal_must_say_why():
    with pytest.raises(ValueError, match="indistinguishable from a crash"):
        PhaseOutcomeV1(phase=ChainPhase.GENERATE_ARTIFACT, succeeded=False,
                       refusal_code="  ", detail="", durable_reference="")


def test_a_SUCCESS_MUST_BE_NAMEABLE_AFTERWARDS():
    """Each phase commits; a success nothing can name is one the next phase cannot resume from."""
    with pytest.raises(ValueError, match="cannot resume from"):
        PhaseOutcomeV1(phase=ChainPhase.EXECUTE_VERIFICATION, succeeded=True,
                       refusal_code="", detail="", durable_reference=" ")


# ══ the interim fix in the LIVE chain ════════════════════════════════════════════════════════════
def test_A_REFUSED_PUBLISHER_SURVIVES_AN_L0_FAILURE():
    """The bug the extraction removes structurally, fixed at the source until it lands. `if not
    built:` is tested FIRST, so before this the publisher's verdict was discarded and the
    publication question looked unasked."""
    refusal = MaterializationRefused(
        code=CompilationRefusalCode.PUBLICATION_REFUSED
        if hasattr(CompilationRefusalCode, "PUBLICATION_REFUSED")
        else list(CompilationRefusalCode)[0],
        detail="no attested mechanism")

    attempt = chain_module._RunAttempt.build_unproven(
        _unproven_report(), l0=None, selection=refusal)
    assert attempt.publication_refusal is refusal, "the verdict is preserved, not re-typed"
    assert attempt.stopped_at is chain_module.ChainStage.VALIDATE_L0, "the run still stops at L0"


def test_a_successful_publisher_selection_records_NO_refusal():
    class _Selection:
        pass

    attempt = chain_module._RunAttempt.build_unproven(
        _unproven_report(), l0=None, selection=_Selection())
    assert attempt.publication_refusal is None


def test_the_two_refusal_fields_answer_DIFFERENT_questions():
    """`refusal` names what STOPPED the run; `publication_refusal` names what publication decided.
    Fusing them is what lost the verdict."""
    import dataclasses

    names = {f.name for f in dataclasses.fields(chain_module.CompiledGroup)}
    assert {"refusal", "publication_refusal"} <= names
