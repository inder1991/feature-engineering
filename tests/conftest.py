from __future__ import annotations

import os

import psycopg
import pytest
from pytest_postgresql import factories

from featuregen.db.migrations import apply_migrations
from featuregen.events.registry import reset_event_registry

# Deterministic HMAC key for the tamper-evident security-audit chain (§6.2, BLOCKER #4).
# security.audit fails CLOSED when FEATUREGEN_AUDIT_HMAC_KEY is unset, so the whole suite —
# not just security tests, but every intake/overlay/authz/privacy test that appends a
# denial to the security stream — needs a key present. Set at import time (config reads the
# env lazily) so it is available before any fixture or collection-time code runs. setdefault
# lets a real environment override win. Individual tests may monkeypatch.delenv to exercise
# the fail-closed path.
os.environ.setdefault("FEATUREGEN_AUDIT_HMAC_KEY", "test-audit-hmac-key-deterministic")

# Brief default is a reachable PostgreSQL 15+ at `postgresql:///featuregen_test`, read from
# FEATUREGEN_TEST_DSN. On machines without a running server (CI / this dev box) we instead
# launch an EPHEMERAL PostgreSQL cluster via pytest-postgresql, which boots a throwaway
# cluster from the on-PATH `postgres` binary (PG 18 here, >= the 15 floor). Set
# FEATUREGEN_TEST_DSN to point the suite at an external server.
_ENV_DSN = os.environ.get("FEATUREGEN_TEST_DSN")

# Ephemeral cluster process fixture. Defining it registers the fixture but does NOT
# launch anything unless it is requested (only when _ENV_DSN is None, below).
postgresql_proc = factories.postgresql_proc()


def _conninfo(proc, dbname: str) -> str:
    parts = [
        f"host={proc.host}",
        f"port={proc.port}",
        f"user={proc.user}",
        f"dbname={dbname}",
    ]
    if proc.password:
        parts.append(f"password={proc.password}")
    return " ".join(parts)


@pytest.fixture(scope="session")
def _dsn(request) -> str:
    """Resolve the test DSN (env-provided or ephemeral) and apply migrations once."""
    if _ENV_DSN is not None:
        dsn = _ENV_DSN
    else:
        proc = request.getfixturevalue("postgresql_proc")
        admin_dsn = _conninfo(proc, "postgres")
        with psycopg.connect(admin_dsn, autocommit=True) as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", ("featuregen_test",))
                if cur.fetchone() is None:
                    cur.execute("CREATE DATABASE featuregen_test")
        dsn = _conninfo(proc, "featuregen_test")
    with psycopg.connect(dsn) as setup_conn:
        apply_migrations(setup_conn)
    return dsn


@pytest.fixture
def conn(_dsn):
    """A real PG15+ connection; each test's writes are rolled back on teardown."""
    connection = psycopg.connect(_dsn)
    try:
        yield connection
        connection.rollback()
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_event_registry()
    yield
    reset_event_registry()


@pytest.fixture(autouse=True)
def _reset_repair_registry():
    """The projection repair registry is a process-global; reset it around each test so a
    projection registered for repair in one test never bleeds into another (mirrors the counters
    and integration-caller resets)."""
    from featuregen.projections import runner

    runner._REPAIR_REGISTRY.clear()
    yield
    runner._REPAIR_REGISTRY.clear()


@pytest.fixture(autouse=True)
def _default_worker_identity_authority(monkeypatch):
    """THE SUITE RUNS AGAINST LOCAL IAM unless a test says otherwise.

    `resolve_current_principal` defaults to the DEPLOYMENT's configured resolver (B0a), so an
    exported `FEATUREGEN_WORKER_IDENTITY_RESOLVER` on a developer's shell would silently answer for
    every frozen-principal recheck in the suite — measured: `=garbage` turns four
    `test_principal_scope` tests red, and a *working* external resolver would be worse, since the
    tests would pass while proving something about that deployment rather than about the platform.

    Cleared rather than `setdefault`-ed (the `FEATUREGEN_AUDIT_HMAC_KEY` idiom above) because the
    default this suite must exercise is the variable's ABSENCE — local IAM. Tests that want another
    authority `monkeypatch.setenv` it themselves, which still wins inside their own test.
    """
    monkeypatch.delenv("FEATUREGEN_WORKER_IDENTITY_RESOLVER", raising=False)


@pytest.fixture(autouse=True)
def _no_real_transient_backoff(request, monkeypatch):
    """THE SUITE NEVER SLEEPS. `drive_structured_call` waits before a PROVIDER_TRANSIENT re-call
    (`llm._TRANSIENT_BACKOFF_BASE_S`), so without this every test that scripts a transient fault
    would pay real wall-clock — 1s + 2s for a two-retry chain — and a future test that happens to
    hit that path would silently make CI slower with nothing pointing at why.

    The wait is zeroed, NOT removed: `sleep(0.0)` is still called, so a test can still observe THAT
    the driver waited, only not for how long.

    Zeroing the schedule everywhere would also make a broken schedule invisible, so the tests that
    assert the real one opt out with `@pytest.mark.real_backoff`. They pay no wall-clock either —
    they inject a recording `sleep=` and never hand the wait to `time.sleep`."""
    if "real_backoff" in request.keywords:
        return
    from featuregen.intake import llm

    monkeypatch.setattr(llm, "transient_backoff_s", lambda retries_used: 0.0)
