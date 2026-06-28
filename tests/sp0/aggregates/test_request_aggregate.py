from sp0.events.store import load_stream
from sp0.aggregates.request_aggregate import create_request_command
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
