from featuregen.events.store import load_stream
from featuregen.aggregates.request_aggregate import create_request_command
from featuregen.aggregates.request_aggregate import create_run_command, duplicate_of_command
from featuregen.aggregates.request_aggregate import select_candidate_command
from tests.featuregen._helpers import make_cmd


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


def test_create_run_under_nonexistent_request_rejected(db):
    res = create_run_command(
        db, make_cmd("create_run", "request", "req_does_not_exist",
                     {"request_id": "req_does_not_exist"}))
    assert not res.accepted
    assert res.produced_event_ids == ()
    assert load_stream(db, "request", "req_does_not_exist") == []


def test_select_candidate_rejects_non_candidate_run(db):
    req = _open_request(db)
    create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req}))
    before = [e.type for e in load_stream(db, "request", req)]
    res = select_candidate_command(
        db, make_cmd("select_candidate", "request", req,
                     {"selections": ({"run_id": "run_not_a_candidate"},)}))
    assert not res.accepted
    assert res.produced_event_ids == ()
    # no new events were appended to the request stream
    assert [e.type for e in load_stream(db, "request", req)] == before


def test_duplicate_of_links_existing_feature(db):
    req = _open_request(db)
    res = duplicate_of_command(
        db, make_cmd("duplicate_of", "request", req,
                     {"duplicate_of_feature_id": "feat_existing"}))
    dup = load_stream(db, "request", req)[-1]
    assert dup.type == "DUPLICATE_OF"
    assert dup.payload["duplicate_of_feature_id"] == "feat_existing"


def test_select_candidate_mints_feature_and_closes_siblings(db):
    req = _open_request(db)
    a = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    b = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    res = select_candidate_command(
        db, make_cmd("select_candidate", "request", req,
                     {"selections": ({"run_id": a},), "candidates_explored_count": 7}))
    assert res.accepted
    feature_created = [e for e in load_stream(db, "feature", _feature_of(db, req))]
    # selected run a is bound; sibling b is rejected
    assert any(e.type == "RUN_REJECTED" for e in load_stream(db, "run", b))
    assert not any(e.type == "RUN_REJECTED" for e in load_stream(db, "run", a))
    sel = [e for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"][0]
    assert sel.payload["selected_run_id"] == a
    assert sel.payload["candidates_explored_count"] == 7
    assert sel.provenance.candidates_explored_count == 7
    # provenance.artifact_type is a §3.7 stage/artifact enum value, NOT an event-type name
    assert sel.provenance.artifact_type == "APPROVAL_RECORD"


def _feature_of(db, req):
    sel = [e for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"][0]
    return sel.payload["feature_id"]


def test_select_candidate_binds_existing_feature_no_feature_created(db):
    req = _open_request(db)
    a = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    select_candidate_command(
        db, make_cmd("select_candidate", "request", req,
                     {"selections": ({"run_id": a, "feature_id": "feat_existing"},)}))
    assert not any(e.type == "FEATURE_CREATED" for e in load_stream(db, "feature", "feat_existing"))
    sel = [e for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"][0]
    assert sel.payload["feature_id"] == "feat_existing"


def test_one_request_yields_multiple_features(db):
    req = _open_request(db)
    a = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    b = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    select_candidate_command(
        db, make_cmd("select_candidate", "request", req,
                     {"selections": ({"run_id": a}, {"run_id": b})}))
    features = {e.payload["feature_id"]
               for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"}
    assert len(features) == 2
    for e in load_stream(db, "run", a) + load_stream(db, "run", b):
        assert e.type != "RUN_REJECTED"
