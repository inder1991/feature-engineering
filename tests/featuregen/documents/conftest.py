from __future__ import annotations

import pytest

from featuregen.contracts import IdentityEnvelope, ProvenanceEnvelope


@pytest.fixture
def db(conn):
    """Alias to the established Phase 01 `conn` fixture (real PG15+ connection,
    rolled back on teardown). The shared migration harness applies the canonical
    `featuregen/db/migrations.py` once per session via the `_dsn` fixture in the root
    conftest; this just exposes it under the name the Phase 02 tests use."""
    return conn


@pytest.fixture
def actor() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="service:intake-agent",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("intake",),
    )


@pytest.fixture
def provenance() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="DRAFT_CONTRACT",
        schema_version=1,
        producing_component="featuregen-test@0.0.0",
    )
