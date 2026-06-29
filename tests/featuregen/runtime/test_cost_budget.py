from __future__ import annotations

from decimal import Decimal

from featuregen.runtime.cost_budget import (
    CostCeilings,
    check_cost_breaker,
    record_cost,
    request_cost,
    trip_cost_breaker,
)


def test_record_cost_accumulates(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1")
    assert record_cost(conn, "run_1", Decimal("2.5")) == Decimal("2.5000")
    assert record_cost(conn, "run_1", Decimal("1.0")) == Decimal("3.5000")


def test_request_cost_sums_runs(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", cost=Decimal("4"))
    insert_run_state(conn, run_id="run_2", request_id="req_1", cost=Decimal("6"))
    assert request_cost(conn, "req_1") == Decimal("10.0000")


def test_breaker_trips_per_run(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", cost=Decimal("100"))
    out = check_cost_breaker(conn, "run_1", ceilings=CostCeilings(per_run=Decimal("100")))
    assert out.tripped is True and out.ceiling == "per_run"


def test_breaker_trips_per_request(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", cost=Decimal("60"))
    insert_run_state(conn, run_id="run_2", request_id="req_1", cost=Decimal("60"))
    out = check_cost_breaker(conn, "run_1", ceilings=CostCeilings(per_request=Decimal("100")))
    assert out.tripped is True and out.ceiling == "per_request"


def test_breaker_trips_on_candidates(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", candidates=5)
    out = check_cost_breaker(conn, "run_1", ceilings=CostCeilings(max_candidates=5))
    assert out.tripped is True and out.ceiling == "max_candidates"


def test_breaker_not_tripped(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", cost=Decimal("1"))
    out = check_cost_breaker(conn, "run_1", ceilings=CostCeilings(per_run=Decimal("100")))
    assert out.tripped is False


def test_trip_auto_parks_idempotently(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1")
    assert trip_cost_breaker(conn, "run_1", ceiling="per_run") is True
    assert trip_cost_breaker(conn, "run_1", ceiling="per_run") is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE message_id='cost-breaker:run_1:per_run'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT handler FROM queue WHERE message_id='cost-breaker:run_1:per_run'")
        assert cur.fetchone()[0] == "runtime.auto_park"
