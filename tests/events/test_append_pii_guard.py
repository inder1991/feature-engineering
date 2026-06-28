from __future__ import annotations

import pytest

from sp0.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from sp0.events.registry import event_registry
from sp0.events.store import append_event
from sp0.privacy.classification import InlinePIIError


def _idv() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="user:raj",
        actor_kind="human",
        authenticated=True,
        auth_method="oidc",
        role_claims=("ds",),
    )


def _prov() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
    )


def _new(run_id: str, payload: dict) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type="RUN_STARTED",
        schema_version=1,
        payload=payload,
        actor=_idv(),
        provenance=_prov(),
        run_id=run_id,
    )


def _register() -> None:
    event_registry().register_schema(
        "RUN_STARTED",
        1,
        {"type": "object", "additionalProperties": True},
        owner="sp0",
    )


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"note": "customer SSN 123-45-6789 churns when ..."},
        {"nested": {"deep": ["-----BEGIN RSA PRIVATE KEY-----\nMIIE"]}},
        {"cred": "AKIAIOSFODNN7EXAMPLE"},
        {"card": "4111 1111 1111 1111"},  # Luhn-valid test PAN
    ],
)
def test_append_rejects_inline_pii_or_secret(conn, bad_payload):
    _register()
    with pytest.raises(InlinePIIError):
        append_event(conn, _new("run_pii", bad_payload), expected_version=0, table_version=1)


def test_append_accepts_reference_only_payload(conn):
    _register()
    env = append_event(
        conn,
        _new("run_clean", {"raw_input_ref": "blob_01HZABC", "count": 4, "hash": "sha256:abc"}),
        expected_version=0,
        table_version=1,
    )
    assert env.stream_version == 1
