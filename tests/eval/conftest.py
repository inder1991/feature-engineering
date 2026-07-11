import pytest


@pytest.fixture
def db(conn):
    """Alias the repo-root `conn` fixture (a real PG connection whose writes are rolled back on
    teardown) under the `db` name the enrichment briefs use. The eval suite lives outside
    tests/featuregen/, so it re-exposes the alias here rather than inheriting that package's
    conftest. Migrations are applied once per session via the root `_dsn` fixture."""
    return conn
