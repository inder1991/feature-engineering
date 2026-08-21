"""§0.10 step 3 — the fenced V2 generation lane.

**What this file holds, and what it deliberately does not.** These tests are about the LANE: the
claim, the fence, the lifecycle, and what each class of outcome does to the request and to the queue
row. They drive the real `process_generation_once` against the real `queue` and
`generation_request` tables, unmocked.

What they do NOT hold is a formula travelling the whole way through the lane to a sealed artifact.
That needs a build set whose selections carry READY drafts with replayable authoring traces, which
is §0.10 step 4's *"prove the production chain"* — and `test_end_to_end_v2.py` already proves the
chain itself, from an admitted V3 proposal to a sealed artifact, at the level below this one. What
IS proved here is that the lane reaches that chain and terminalizes correctly on what it finds: the
restore stage refusing a build set with no drafts is the wiring from claim → request → build set →
restore, running for real.

**The load-bearing tests are the three guards**, because they fail differently and only one of them
is the queue's:

1. the per-partition in-flight exclusion — a second worker cannot claim,
2. the `lease_fence` — a superseded worker's terminal write is refused,
3. `advance_request`'s compare-and-set — a worker whose lease expired while it was still ALIVE
   cannot move a request somebody else now owns.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.test_build_set_store import (
    ENV,
    _approval,
    _set,
)

from featuregen.materialize.generation_lane import (
    GENERATION_HANDLER,
    GenerationJobV2,
    decode_job,
    encode_job,
    enqueue_generation,
    generation_enabled,
    generation_message_id,
    process_generation_once,
)
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.overlay.upload.build_set_store import (
    GenerationStatusV1,
    read_request,
    request_generation,
)
from featuregen.runtime.queue import (
    GENERATION_QUEUE_HANDLERS,
    QueueIdempotencyConflict,
    claim_generation,
    claim_one,
)

GROUP = "customer_txn_features"


def _job(request_id: str = "req-1", **overrides) -> GenerationJobV2:
    """A decodable job. The declarations are minimal on purpose — these tests are about the lane,
    and a job that compiles is `test_end_to_end_v2`'s subject, not this file's."""
    from featuregen.materialize.contract import AvailabilityPromiseV1
    from tests.featuregen.materialize.test_pilot_v2 import CADENCE

    fields = dict(
        request_id=request_id,
        spine_declaration=None,
        cadence=CADENCE,
        availability_promise=AvailabilityPromiseV1(calendar_days=1),
        physical_type_policy="formula-v2/physical-types@1",
        empty_values={"posted_amount_30d": "0"},
        operand_facts={},
        engine_id="kedro-pyspark",
        compiled_at="2026-08-21T00:00:00Z",
        sealed_at="2026-08-21T00:00:00Z")
    fields.update(overrides)
    return GenerationJobV2(**fields)


def _inventory() -> ClusterInventoryV1:
    from tests.featuregen.materialize import fixtures

    return ClusterInventoryV1(
        environment_id=ENV, tables={}, logical_schema_map={},
        engine_versions=fixtures.ENGINE_VERSIONS, captured_at="2026-08-21T00:00:00Z")


@pytest.fixture
def enqueued(db):
    """A recorded build set, an approval, a REQUESTED request, and its job on the queue."""
    build_set, _ = _set(db, revision_id="bs-lane", members=("sel-a",), target="trr-lane")
    approval = _approval(db, build_set, ENV)
    request_id, created = request_generation(
        db, request_id="req-1", build_set_revision_id=build_set, environment_id=ENV,
        requested_by="user:ops", requested_at="2026-08-21T00:00:00Z",
        generation_authorization_revision_id=approval)
    assert created
    enqueue_generation(db, job=_job(request_id), environment_id=ENV, logical_group_name=GROUP)
    return request_id


# ══ THE PAYLOAD IS THE FROZEN WORK ITEM ════════════════════════════════════════════════════════
def test_a_job_ROUND_TRIPS_through_the_queue_payload():
    """What a worker reads back IS what was frozen. A payload that decoded to something else would
    generate a build nobody asked for under an id somebody is watching."""
    job = _job()

    read_back = decode_job(encode_job(job))

    assert read_back.request_id == job.request_id
    assert read_back.physical_type_policy == job.physical_type_policy
    assert read_back.empty_values == job.empty_values
    assert read_back.engine_id == job.engine_id
    assert read_back.compiled_at == job.compiled_at


def test_an_EMPTY_WINDOW_ANSWER_OF_NULL_SURVIVES_the_round_trip():
    """`None` means "publishes NULL", which is a different published answer from "0". A decoder that
    treated it as absent would silently pick one."""
    job = _job(empty_values={"posted_amount_30d": None})

    assert decode_job(encode_job(job)).empty_values == {"posted_amount_30d": None}


def test_a_payload_this_worker_cannot_read_WHOLE_is_refused():
    with pytest.raises(ValueError, match="version"):
        decode_job({**encode_job(_job()), "version": 99})


def test_a_payload_missing_a_DECLARATION_is_refused_not_defaulted():
    """The empty-window answers and the type policy are declarations. Defaulting either would
    publish a number the author never chose."""
    payload = encode_job(_job())
    del payload["physical_type_policy"]

    with pytest.raises(ValueError, match="physical_type_policy"):
        decode_job(payload)


# ══ THE PRODUCER ═══════════════════════════════════════════════════════════════════════════════
def test_the_HANDLER_SPELLING_agrees_with_the_queue(  ) -> None:
    """`runtime.queue` cannot import this module, so the name is spelled in both. If they drift, the
    general consumer becomes free to claim a governed generation and dead-letter it."""
    assert GENERATION_QUEUE_HANDLERS == {GENERATION_HANDLER}


def test_the_GENERAL_CONSUMER_can_never_claim_a_generation(enqueued, db) -> None:
    """`process_one` builds its context from a run-stream event id, which a generation has none of —
    so a claim it won would become a dead letter rather than a build."""
    assert claim_one(db, owner="general") is None


def test_ENQUEUEING_ONE_REQUEST_TWICE_IS_ONE_JOB(enqueued, db) -> None:
    """A double-click must not start a second compile. The message id is derived from the request,
    so the second enqueue finds the first."""
    first = db.execute("SELECT count(*) FROM queue WHERE handler = %s",
                       (GENERATION_HANDLER,)).fetchone()[0]
    enqueue_generation(db, job=_job(enqueued), environment_id=ENV, logical_group_name=GROUP)

    assert db.execute("SELECT count(*) FROM queue WHERE handler = %s",
                      (GENERATION_HANDLER,)).fetchone()[0] == first == 1


def test_ONE_REQUEST_ID_NAMING_DIFFERENT_WORK_IS_REFUSED(enqueued, db) -> None:
    """Not merely deduplicated — refused. One request id used for two different jobs would generate
    something nobody asked for under an id somebody is watching."""
    with pytest.raises(QueueIdempotencyConflict):
        enqueue_generation(
            db, job=_job(enqueued, physical_type_policy="formula-v2/physical-types@99"),
            environment_id=ENV, logical_group_name=GROUP)


def test_the_message_id_is_DERIVED_never_supplied():
    assert generation_message_id("req-1") == "generation:req-1"
    with pytest.raises(ValueError, match="request id"):
        generation_message_id("  ")


# ══ GUARD 1 — THE PARTITION ════════════════════════════════════════════════════════════════════
def test_a_SECOND_WORKER_CANNOT_CLAIM_A_LEASED_JOB(enqueued, db) -> None:
    """The queue's unique partial index on `(partition_key) WHERE status='leased'` is the guarantee;
    the claim's NOT IN is an optimization. Two generations of one group in one environment would
    race to publish the same columns."""
    first = claim_generation(db, owner="w1")
    assert first is not None

    assert claim_generation(db, owner="w2") is None


# ══ GUARD 2 — THE FENCE ════════════════════════════════════════════════════════════════════════
def test_a_STALE_FENCE_WRITE_IS_REFUSED(enqueued, db) -> None:
    """A worker whose claim was superseded cannot complete the job it thinks it holds."""
    import dataclasses

    from featuregen.runtime.queue import complete_generation

    claim = claim_generation(db, owner="w1")
    assert claim is not None
    stale = dataclasses.replace(claim, lease_fence=claim.lease_fence - 1)

    assert complete_generation(db, stale) is False
    assert complete_generation(db, claim) is True


# ══ GUARD 3 — THE LIFECYCLE'S COMPARE-AND-SET ══════════════════════════════════════════════════
def test_a_request_ANOTHER_WORKER_ALREADY_CLAIMED_is_not_taken(enqueued, db) -> None:
    """The guard the other two cannot give. A lease can expire while its holder is still alive and
    working, and that holder must not be able to move a request somebody else now owns.

    The lane releases the queue row for a later delivery rather than failing it: the work is not
    wrong, it is not ours.
    """
    from featuregen.overlay.upload.build_set_store import advance_request

    advance_request(db, enqueued, GenerationStatusV1.CLAIMED)      # somebody else got there first

    outcome = process_generation_once(db, owner="w2", inventory=_inventory())

    assert outcome.status == "unclaimable"
    assert read_request(db, enqueued).status is GenerationStatusV1.CLAIMED


# ══ WHAT EACH OUTCOME DOES ═════════════════════════════════════════════════════════════════════
def test_an_IDLE_LANE_COSTS_ONE_QUERY_and_says_so(db) -> None:
    assert process_generation_once(db, owner="w1", inventory=_inventory()).status == "idle"


def test_a_job_naming_a_request_NOBODY_RECORDED_fails_PERMANENTLY(db) -> None:
    """A redelivery would find the same absence, so retrying spends the attempt budget to reach the
    same answer later."""
    enqueue_generation(db, job=_job("req-ghost"), environment_id=ENV, logical_group_name=GROUP)

    outcome = process_generation_once(db, owner="w1", inventory=_inventory())

    assert outcome.status == "failed"
    assert db.execute("SELECT status FROM queue WHERE message_id = %s",
                      (generation_message_id("req-ghost"),)).fetchone()[0] == "dead"


def test_an_UNDECODABLE_PAYLOAD_dead_letters_rather_than_looping(db) -> None:
    from featuregen.runtime.queue import enqueue_checked

    enqueue_checked(db, message_id=generation_message_id("req-bad"),
                    partition_key=f"generation:{ENV}:{GROUP}", handler=GENERATION_HANDLER,
                    payload={"version": 99, "request_id": "req-bad"})

    outcome = process_generation_once(db, owner="w1", inventory=_inventory())

    assert outcome.status == "failed"
    assert "version" in outcome.detail


def test_the_LANE_REACHES_THE_CHAIN_and_refuses_what_it_finds(enqueued, db) -> None:
    """The wiring, running for real: claim → request → build set → RESTORE. The build set's one
    selection has no formula draft, so the restore stage refuses by name — which is the correct
    verdict AND the proof that the lane got that far.

    A governed refusal COMPLETES the queue row rather than failing it: a redelivery would reproduce
    it exactly, so retrying spends the budget to reach the same answer and dead-lettering would file
    a correct product answer as a platform fault.
    """
    outcome = process_generation_once(db, owner="w1", inventory=_inventory())

    assert outcome.status == "refused", outcome
    assert "draft" in outcome.detail.lower(), outcome.detail

    request = read_request(db, enqueued)
    assert request.status is GenerationStatusV1.REFUSED
    assert request.refusals, "a refusal that names nothing is not actionable"
    assert db.execute("SELECT status FROM queue WHERE message_id = %s",
                      (generation_message_id(enqueued),)).fetchone()[0] == "done"


def test_a_REDELIVERY_of_finished_work_does_not_generate_again(enqueued, db) -> None:
    """One request is one artifact. A second delivery of a terminal request completes the queue row
    and touches nothing."""
    first = process_generation_once(db, owner="w1", inventory=_inventory())
    assert first.status == "refused"

    db.execute("UPDATE queue SET status='ready', lease_owner=NULL, lease_expires_at=NULL "
               "WHERE message_id = %s", (generation_message_id(enqueued),))

    again = process_generation_once(db, owner="w2", inventory=_inventory())

    assert again.status == "unclaimable"
    assert "REFUSED" in again.detail


# ══ THE KILL SWITCH ════════════════════════════════════════════════════════════════════════════
def test_generation_is_OFF_unless_the_deployment_says_otherwise(monkeypatch) -> None:
    monkeypatch.delenv("FEATUREGEN_GENERATION_V2_ENABLED", raising=False)
    assert generation_enabled() is False

    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")
    assert generation_enabled() is True

    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "maybe")
    assert generation_enabled() is False
