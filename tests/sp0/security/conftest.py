from __future__ import annotations

import psycopg
import pytest


@pytest.fixture(autouse=True)
def _isolate_security_chain(_dsn):
    """Give each security-audit test a pristine tamper-evident chain.

    The brief's tests assert the first appended row is ``seq = 1`` and that
    ``security_audit`` starts empty. The function-scoped ``conn`` fixture rolls back
    each test's writes, but that is not enough here:

    * ``global_seq_seq`` is a shared, non-transactional sequence, so ``seq`` keeps
      advancing across tests in the session — the row a test inserts is no longer
      ``seq = 1`` once an earlier test has consumed values, breaking the literal
      ``WHERE seq = 1`` tamper assertion.
    * ``test_concurrent_appends_keep_single_chain`` commits on its OWN connections;
      those rows survive the ``conn`` rollback and would pollute later tests.

    So before (and after) each test we truncate the committed chain and restart the
    sequence, giving the next append ``seq = 1``. This only adjusts test isolation;
    the production code under test is untouched.
    """
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("TRUNCATE security_audit")
        c.execute("ALTER SEQUENCE global_seq_seq RESTART WITH 1")
    yield
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("TRUNCATE security_audit")
