"""One caller before 1101 destroys the binding's zero-row NOT NULL branch (spec §7/§13).

`selection_formula_binding` is created by migration 1101 and every build set member points at one.
While 1101 has NOT been applied, a build set can still be *written* — and each one written is a row
the pin will later have to constrain. That is exactly the situation 1101 was designed never to be
in: it takes its NOT NULL immediately because the live table has zero rows, and a single build set
declared through the API beforehand converts that clean migration into a nullable column plus a
backfill of rows nobody can bind after the fact. So the API withholds the declaration, server-side,
for as long as the pin is missing.

**The guard is self-retiring**, and both halves of that are tested here:

* pre-pin — the pin table simulated away — both POST routes answer `409
  BUILD_SET_DECLARATION_WITHHELD_PRE_PIN`, and they answer it BEFORE any validation or lookup;
* post-pin — the ordinary state of every test database, since 1101 is a committed migration on this
  branch — the SAME bodies fall straight through into normal validation. No code changes between
  the two; the schema is the whole of the switch.

▲ **Bodies are pydantic-VALID on purpose.** FastAPI validates the request model before a handler
runs, so an empty body would be a 422 that never reached the guard at all and the 409 could not be
observed. Each body here parses cleanly and is wrong further in — an undeclared declaration, an
authorization that does not exist — which is what makes the pairing load-bearing: the identical
request is a 409 pre-pin and a 422/404 post-pin, so the tests pin the guard's PRECEDENCE as well as
its presence.

The V2 switch must be ON for these routes to exist at all (they 404 otherwise).
"""
from __future__ import annotations

import pytest

SETS = "/build-sets"
GENERATIONS = "/build-sets/generations"
PRE_PIN = "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN"


@pytest.fixture
def enabled(monkeypatch):
    """The deployment switch these routes are gated behind — off, and every path is a 404, which
    would make a missing guard indistinguishable from a refused one."""
    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")


@pytest.fixture
def engineer_headers():
    """The role that MAY generate; the guard sits inside the handler, so the request has to get
    past the permission dependency to reach it."""
    return {"X-User": "sam", "X-Roles": "feature_engineer"}


def _drop_the_pin(conn):
    """Simulate the pre-1101 deployment INSIDE the test transaction.

    1101 is a committed migration on this branch, so `selection_formula_binding` exists in every
    test database and the pre-pin branch would otherwise be unreachable. Postgres DDL is
    transactional and the `conn` fixture rolls its transaction back, so the table is gone only for
    the remainder of this test. Nothing else in the schema references it (grepped across the
    migrations), so CASCADE drops only 1101's own objects — one of which reaches OUTSIDE the table:
    1101's `build_set_member_formula_pinned_v1` FK on `build_set_member` goes with it, so the DROP
    takes an ACCESS EXCLUSIVE lock on `build_set_member` too. Harmless while the suite runs
    serially; under xdist against a SHARED database it would block every concurrent reader of that
    table until this test's transaction rolls back.
    """
    conn.execute("DROP TABLE selection_formula_binding CASCADE")


def _declare_body():
    """Valid to pydantic, undecodable further in: `declaration` carries no `version`, so
    `decode_declaration` refuses it and the post-pin answer is a 422."""
    return {"target_reading_revision_id": "trr-pre-pin",
            "selection_formula_binding_ids": ["sfb-pre-pin"],
            "declaration": {}}


def _generation_body():
    """Valid to pydantic, unresolvable further in: no such authorization exists, so the post-pin
    answer is a 404."""
    return {"build_set_revision_id": "bs-pre-pin",
            "generation_authorization_revision_id": "gar-pre-pin",
            "physical_type_policy": "formula-v2/physical-types@1",
            "empty_values": {},
            "engine_id": "kedro-pyspark"}


# ══ PRE-PIN: THE DECLARATION IS WITHHELD ═══════════════════════════════════════════════════════
def test_DECLARING_A_BUILD_SET_IS_REFUSED_WHILE_THE_PIN_IS_MISSING(
        client, conn, enabled, engineer_headers) -> None:
    """409, not 422: the body is fine and the SCHEMA is what forbids the write, so the answer is a
    conflict with the deployment's state rather than a complaint about the request."""
    _drop_the_pin(conn)

    response = client.post(SETS, json=_declare_body(), headers=engineer_headers)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == PRE_PIN
    # It names the migration. "Withheld" without "until what" leaves an operator with no next step,
    # and the next step here is applying a specific migration.
    assert "1101" in detail["message"]


def test_REQUESTING_A_BUILD_IS_REFUSED_WHILE_THE_PIN_IS_MISSING(
        client, conn, enabled, engineer_headers) -> None:
    """▲ BOTH producers, not just the declaring one. Guarding only `POST /build-sets` would leave
    the second write path — which reads a set's members and queues real compute against them —
    running against a schema that cannot bind a member to a formula."""
    _drop_the_pin(conn)

    response = client.post(GENERATIONS, json=_generation_body(), headers=engineer_headers)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == PRE_PIN


def test_THE_REFUSED_DECLARATION_WRITES_NOTHING(client, conn, enabled, engineer_headers) -> None:
    """▲ Asserted on the TABLE, not on the status code. A guard that ran after `record_build_set`
    would return the same 409 while having already written the exact row the pin cannot constrain —
    which is the entire failure this task exists to prevent."""
    before = conn.execute("SELECT count(*) FROM build_set_revision").fetchone()[0]
    _drop_the_pin(conn)

    client.post(SETS, json=_declare_body(), headers=engineer_headers)

    assert conn.execute("SELECT count(*) FROM build_set_revision").fetchone()[0] == before


# ══ POST-PIN: THE GUARD RETIRES ITSELF ═════════════════════════════════════════════════════════
def test_DECLARING_FALLS_THROUGH_TO_NORMAL_VALIDATION_ONCE_THE_PIN_EXISTS(
        client, enabled, engineer_headers) -> None:
    """The default state of every test database, and of any deployment that has applied 1101: the
    guard passes with zero code change.

    The SAME body as the pre-pin test, deliberately — it is a 409 there and a 422 here, so this
    pair proves the guard is both present and ordered ahead of the declaration decode. Without the
    pairing, a guard that always refused and a guard that never refused would each pass one test.
    """
    response = client.post(SETS, json=_declare_body(), headers=engineer_headers)

    assert response.status_code == 422, response.text
    assert PRE_PIN not in response.text


def test_REQUESTING_FALLS_THROUGH_TO_NORMAL_LOOKUP_ONCE_THE_PIN_EXISTS(
        client, enabled, engineer_headers) -> None:
    """Same body as the pre-pin generations test: a 409 there, a plain 404 for the missing
    authorization here."""
    response = client.post(GENERATIONS, json=_generation_body(), headers=engineer_headers)

    assert response.status_code == 404, response.text
    assert PRE_PIN not in response.text
