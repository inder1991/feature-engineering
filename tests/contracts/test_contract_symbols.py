from __future__ import annotations

from datetime import datetime, timezone

from featuregen.contracts import (
    Command,
    CommandResult,
    ConcurrencyError,
    Disposition,
    EventEnvelope,
    GateTaskSpec,
    GuardOutcome,
    Handler,
    HandlerContext,
    HandlerResult,
    IdentityEnvelope,
    NewDocument,
    NewEvent,
    NewExternalCommand,
    NewTimer,
    PredicateRegistry,
    Projection,
    ProjectionApplyError,
    ProvenanceEnvelope,
    SchemaRegistry,
    SchemaValidationError,
    SignalResult,
)


def _identity() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="user:raj",
        actor_kind="human",
        authenticated=True,
        auth_method="oidc",
        role_claims=("data_scientist",),
    )


def _provenance() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="CONFIRMED_CONTRACT",
        schema_version=1,
        producing_component="featuregen-test@0.1.0",
    )


def test_identity_envelope_is_frozen():
    idv = _identity()
    assert idv.role_claims == ("data_scientist",)
    import dataclasses

    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        idv.subject = "user:eve"  # type: ignore[misc]


def test_event_envelope_round_constructs():
    now = datetime.now(timezone.utc)
    env = EventEnvelope(
        event_id="evt_1",
        global_seq=1,
        aggregate="run",
        aggregate_id="run_1",
        stream_version=1,
        type="RUN_STARTED",
        schema_version=1,
        table_version=1,
        actor=_identity(),
        payload={"k": "v"},
        provenance=_provenance(),
        occurred_at=now,
        recorded_at=now,
        run_id="run_1",
    )
    assert env.run_id == "run_1"
    assert env.feature_id is None


def test_new_event_defaults():
    ne = NewEvent(
        aggregate="run",
        aggregate_id="run_1",
        type="RUN_STARTED",
        schema_version=1,
        payload={},
        actor=_identity(),
        provenance=_provenance(),
        run_id="run_1",
    )
    assert ne.occurred_at is None
    assert ne.caused_by is None


def test_disposition_values():
    assert Disposition.OK == "ok"
    assert Disposition.RETRYABLE == "retryable"
    assert Disposition.PERMANENT == "permanent"


def test_projection_apply_error_carries_aggregate():
    err = ProjectionApplyError("run", "run_9", "bad event")
    assert err.aggregate == "run"
    assert err.aggregate_id == "run_9"
    assert err.reason == "bad event"


def test_protocols_and_exceptions_importable():
    assert issubclass(ConcurrencyError, Exception)
    assert issubclass(SchemaValidationError, Exception)
    # Protocols import without error and are usable as types.
    for proto in (Projection, Handler, SchemaRegistry, PredicateRegistry):
        assert proto is not None
    # Downstream-phase declarations import without error.
    for sym in (
        NewDocument,
        NewExternalCommand,
        NewTimer,
        HandlerResult,
        HandlerContext,
        Command,
        CommandResult,
        GuardOutcome,
        GateTaskSpec,
        SignalResult,
    ):
        assert sym is not None
