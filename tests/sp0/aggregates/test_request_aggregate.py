from sp0.events.store import load_stream
from sp0.aggregates.request_aggregate import create_request_command
from sp0.aggregates.request_aggregate import create_run_command, duplicate_of_command
from tests.sp0._helpers import make_cmd


def test_create_request_mints_id_and_claims_concept(db):
    res = create_request_command(
        db, make_cmd("create_request", "request", None,
                     {"feature_concept": "Salary Irregularity", "intake_mode": "hypothesis"}))
    assert res.accepted and res.aggregate_id.startswith("req_")
    stream = load_stream(db, "request", res.aggregate_id)
    assert [e.type for e in stream] == ["REQUEST_CREATED"]
    assert stream[0].payload["concept_key"] == "salary-irregularity"
    claim = db.execute(
        "SELECT request_id FROM concept_claims WHERE concept_key = %s",
        ("salary-irregularity",),
    ).fetchone()
    assert claim[0] == res.aggregate_id


def test_second_request_on_same_concept_emits_duplicate_of(db):
    first = create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "Churn risk"}))
    second = create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "churn   RISK"}))
    types = [e.type for e in load_stream(db, "request", second.aggregate_id)]
    assert types == ["REQUEST_CREATED", "DUPLICATE_OF"]
    dup = load_stream(db, "request", second.aggregate_id)[-1]
    assert dup.payload["duplicate_of_request_id"] == first.aggregate_id


def _open_request(db):
    return create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "x"})).aggregate_id


def test_create_run_links_request(db):
    req = _open_request(db)
    r1 = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req}))
    r2 = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req}))
    assert r1.aggregate_id.startswith("run_") and r2.aggregate_id.startswith("run_")
    run_types = [e.type for e in load_stream(db, "run", r1.aggregate_id)]
    assert run_types == ["RUN_CREATED"]
    added = [e.payload["run_id"] for e in load_stream(db, "request", req) if e.type == "CANDIDATE_ADDED"]
    assert set(added) == {r1.aggregate_id, r2.aggregate_id}


def test_duplicate_of_links_existing_feature(db):
    req = _open_request(db)
    res = duplicate_of_command(
        db, make_cmd("duplicate_of", "request", req,
                     {"duplicate_of_feature_id": "feat_existing"}))
    dup = load_stream(db, "request", req)[-1]
    assert dup.type == "DUPLICATE_OF"
    assert dup.payload["duplicate_of_feature_id"] == "feat_existing"
