"""The run-detail projection (spec §12): milestones, two-axis authoring, and an honest rail.

Envelopes are built directly rather than minted (see `test_projection_list`): the projection takes
an envelope as data and verifies nothing, and the seeded chain must carry the SAME subject spelling
the caller does.
"""
import pytest
from psycopg.types.json import Jsonb
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.projection import _AUTHOR_SEVERITY, RAIL_FROM_DRAFT_STATE, run_detail

_ADMIN = IdentityEnvelope(subject="a", actor_kind="human", authenticated=True,
                          auth_method="test", role_claims=("platform_admin",))

_DRAFT_STATES = {"REQUESTED", "AUTHORING", "CRITIC_REVIEW", "VALIDATING",
                 "ADMISSION", "READY", "BLOCKED", "FAILED", "CANCELLED"}  # 1090's CHECK, verbatim


def _seed_draft(db, considered_revision_id, draft_id, option_id, state):
    """One `formula_draft` in `state`, satisfying 1090's state-dependent CHECKs.

    Three of them bite: READY must carry a real formula (`{}` is refused explicitly), BLOCKED must
    name at least one blocker, and `failure_reason` must be present for FAILED and absent for
    everything else. `requested_at` is TEXT in 1090, not timestamptz."""
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, formula_content_hash, formula_json, blockers, "
        "failure_reason, requested_by, requested_at) "
        "VALUES (%s, %s, %s, 'p', 'c', 'a', '', %s, %s, %s, %s, %s, %s, 'u1', "
        "'2026-08-23T00:00:00Z')",
        (draft_id, considered_revision_id, option_id, f"fih-{draft_id}", state,
         "fch" if state == "READY" else None,
         Jsonb({"op": "sum"}) if state == "READY" else None,
         Jsonb([{"code": "X"}] if state == "BLOCKED" else []),
         "boom" if state == "FAILED" else None))


def _seed_retired_ready_draft(db, run_id):
    """One READY draft on `run_id`'s candidate, retired afterwards — the §6.7 two-axis case.

    `CANDIDATE_SUPERSEDED` is one of 1096's three CHECK values, and `reason`/`retired_by`/
    `retired_at` are its real column names — both verified against the migration file."""
    c = seed_run_chain(db, run_id=run_id)
    _seed_draft(db, c["considered_revision_id"], f"{run_id}-d1", "o1", "READY")
    db.execute("INSERT INTO formula_draft_retirement (formula_draft_id, reason, retired_by, "
               "retired_at) VALUES (%s, 'CANDIDATE_SUPERSEDED', 'u1', now())", (f"{run_id}-d1",))
    return c


def _drop_the_pin(db):
    """Simulate the pre-1101 deployment INSIDE the test transaction.

    1101 is a committed migration on this branch, so `selection_formula_binding` exists in every
    test database and the pre-pin branch would otherwise be unreachable. Postgres DDL is
    transactional and the `db` fixture rolls its transaction back, so the table is gone only for
    the remainder of this test. Nothing else in the schema references it (grepped across the
    migrations), so CASCADE drops only 1101's own objects — one of which reaches OUTSIDE the table:
    1101's `build_set_member_formula_pinned_v1` FK on `build_set_member` goes with it, so the DROP
    takes an ACCESS EXCLUSIVE lock on `build_set_member` too. Harmless while the suite runs
    serially; under xdist against a SHARED database it would block every concurrent reader of that
    table until this test's transaction rolls back."""
    db.execute("DROP TABLE selection_formula_binding CASCADE")


@pytest.fixture
def generation_on(monkeypatch):
    """The deployment switch `POST /build-sets` is gated behind, ON.

    Default-OFF is the honest state of every test and dev database, and with it off the rail's
    `GENERATE_PREVIEW` answer is decided before the pin is ever consulted — so a test that means to
    exercise the PIN half has to turn the switch on first, exactly as the route's own tests do.
    """
    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")


@pytest.fixture
def sandbox_switches_off(monkeypatch):
    """BOTH switches `EXECUTE_SANDBOX`'s only entrance rides, explicitly OFF.

    Default-OFF is what every test and dev deployment actually is, but a test that asserts the
    DEFAULT must not INHERIT it from whatever the developer's shell exports: with either flag set
    outside the process the assertion would invert on that one machine and nowhere else. Deleting
    them states the deployment the test is describing.
    """
    monkeypatch.delenv("FEATUREGEN_MATERIALIZE_ENABLED", raising=False)
    monkeypatch.delenv("FEATUREGEN_VERIFICATION_V2_ENABLED", raising=False)


def _rail(detail):
    return {s["stage"]: s for s in detail["rail"]}


def test_rail_mapping_is_total_over_1090s_check():
    assert set(RAIL_FROM_DRAFT_STATE) == _DRAFT_STATES
    assert RAIL_FROM_DRAFT_STATE["READY"] == "SUCCEEDED"
    assert RAIL_FROM_DRAFT_STATE["BLOCKED"] == "BLOCKED"      # product result, not outage (1090)
    # The fold's severity list must RANK every rail value the mapping can produce, or the day a
    # tenth draft state is added `.index` raises inside a read-only projection. Pinned here, beside
    # the mapping it ranks, so the two are extended together.
    assert set(_AUTHOR_SEVERITY) == set(RAIL_FROM_DRAFT_STATE.values())


def test_detail_shows_sockets_and_two_axes(db, generation_on, sandbox_switches_off):
    _seed_retired_ready_draft(db, "rd-a")
    _drop_the_pin(db)
    out = run_detail(db, _ADMIN, "rd-a")
    d = out["authoring"][0]
    assert d["rail_state"] == "SUCCEEDED" and d["eligibility"] == "withdrawn"   # two axes, §6.7
    assert d["state"] == "READY" and d["retirement_reason"] == "CANDIDATE_SUPERSEDED"
    by_stage = _rail(out)
    for stage in ("EXECUTE_SANDBOX", "PUBLISH_SANDBOX", "MATERIALIZE_PRODUCTION",
                  "PUBLISH_PRODUCTION", "TRAIN_MODEL", "GENERATE_PREVIEW"):
        assert by_stage[stage]["state"] == "UNAVAILABLE"
    assert by_stage["GENERATE_PREVIEW"]["reason_code"] == "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN"
    # Every socket's REASON, not just its state: UNAVAILABLE alone is not the honest label the spec
    # asks for — the code is what tells an operator which person and which remedy. Asserting the
    # states only lets a socket carry the wrong reason (TRAIN_MODEL is an unbuilt SUBSYSTEM, not a
    # missing worker) and still pass.
    #
    # THREE of these five are now DERIVED, and this map is the mutation check: reverting to the old
    # static tuple restores `WORKER_NOT_IMPLEMENTED` for EXECUTE_SANDBOX (whose §9.0 lane exists)
    # and `STATE_MACHINE_NOT_BUILT` for the production pair (whose machines 1113/1114 built), and
    # this assertion fails on all three. The two that remain literal are literal because they are
    # still TRUE: no sandbox publication worker and no training subsystem exist to derive from.
    _sockets = ("EXECUTE_SANDBOX", "PUBLISH_SANDBOX", "MATERIALIZE_PRODUCTION",
                "PUBLISH_PRODUCTION", "TRAIN_MODEL")
    assert {s: by_stage[s]["reason_code"] for s in _sockets} == {
        "EXECUTE_SANDBOX": "MATERIALIZATION_DISABLED",
        "PUBLISH_SANDBOX": "WORKER_NOT_IMPLEMENTED",
        "MATERIALIZE_PRODUCTION": "ACTION_UNAVAILABLE",
        "PUBLISH_PRODUCTION": "ACTION_UNAVAILABLE",
        "TRAIN_MODEL": "SUBSYSTEM_NOT_BUILT"}
    # The rail is these NINE stages and no others. Every other assertion here indexes `by_stage` by
    # name, so an extra stage — a lane the UI would render with nothing behind it — survives them
    # all. The length check closes the dict's own blind spot: a duplicated stage collapses silently.
    assert set(by_stage) == {"CHOOSE_CANDIDATES", "AUTHOR_FORMULA", "BIND_SELECTIONS",
                             "GENERATE_PREVIEW", "EXECUTE_SANDBOX", "PUBLISH_SANDBOX",
                             "MATERIALIZE_PRODUCTION", "PUBLISH_PRODUCTION", "TRAIN_MODEL"}
    assert len(out["rail"]) == 9


def test_preview_is_not_started_once_the_pin_exists(db, generation_on, sandbox_switches_off):
    """The other half of the DERIVED availability rule (spec §7 [R3.1]).

    Without this, a `pin_exists` that always answered False — a hardcoded UNAVAILABLE — would pass
    the whole file. `GENERATE_PREVIEW` is the one stage whose availability moves with the
    deployment; the five sockets do not, so they stay UNAVAILABLE here too.

    NOT_STARTED needs BOTH conditions, which is why the switch fixture is here: this is the only
    combination in which the entrance actually opens."""
    _seed_retired_ready_draft(db, "rd-p")
    by_stage = _rail(run_detail(db, _ADMIN, "rd-p"))
    assert by_stage["GENERATE_PREVIEW"] == {"stage": "GENERATE_PREVIEW", "state": "NOT_STARTED",
                                            "reason_code": None}
    assert by_stage["EXECUTE_SANDBOX"]["state"] == "UNAVAILABLE"


def test_preview_unavailable_while_generation_is_switched_off(db, monkeypatch):
    """The switch half of the fold, in the state EVERY test and dev database is actually in.

    1101 is a committed migration, so the pin exists here; `FEATUREGEN_GENERATION_V2_ENABLED` is
    default-OFF, so `POST /build-sets` — `GENERATE_PREVIEW`'s only entrance — answers 404 on this
    deployment. A rail that read the pin alone reported NOT_STARTED into that 404: the false rail
    spec §7 [R3.1] forbids, inviting a person into a stage they cannot reach.

    The reason code must be the SWITCH's, not the pin's. Both answers are UNAVAILABLE, so the state
    alone cannot tell the two apart, and they send an operator to different remedies — flip a
    deployment switch, or apply a migration.
    """
    monkeypatch.delenv("FEATUREGEN_GENERATION_V2_ENABLED", raising=False)
    _seed_retired_ready_draft(db, "rd-off")
    assert _rail(run_detail(db, _ADMIN, "rd-off"))["GENERATE_PREVIEW"] == {
        "stage": "GENERATE_PREVIEW", "state": "UNAVAILABLE", "reason_code": "GENERATION_DISABLED"}


def test_execute_sandbox_names_the_lane_switch_once_the_surface_is_on(db, sandbox_switches_off,
                                                                     monkeypatch):
    """The SECOND half of `EXECUTE_SANDBOX`'s fold, and the only test that can pin its precedence.

    The stage's only entrance is `POST /feature-execution/verifications`, and two independent
    switches shut it: the router-level `materialization_enabled` (which 404s every path on that
    surface) and the lane's own `verification_enabled` (default OFF, the switch the worker tick
    reads before it will process a single request).

    Both answers are UNAVAILABLE, so the state cannot tell them apart — and they send an operator
    to DIFFERENT deployment switches. A fold that named only one of them would have half the
    deployments told to flip a flag that changes nothing.
    """
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_ENABLED", "1")
    _seed_retired_ready_draft(db, "rd-vs")
    assert _rail(run_detail(db, _ADMIN, "rd-vs"))["EXECUTE_SANDBOX"] == {
        "stage": "EXECUTE_SANDBOX", "state": "UNAVAILABLE",
        "reason_code": "VERIFICATION_DISABLED"}


def test_execute_sandbox_is_not_started_once_both_switches_are_on(db, monkeypatch):
    """The falsifier for the whole derivation: without it, a hardcoded UNAVAILABLE passes.

    NOT_STARTED is the honest answer here even though this deployment has no execution substrate
    configured (`verification_lane._EXECUTOR is None`). That absence is not unavailability: the
    entrance ACCEPTS the request, the worker claims it, and the attempt ends FAILED with the
    posture named. An attempt with an outcome is a stage that ran — spec §7 [R3.1] forbids
    NOT_STARTED over an entrance that REFUSES, and this entrance does not refuse.
    """
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_ENABLED", "1")
    monkeypatch.setenv("FEATUREGEN_VERIFICATION_V2_ENABLED", "1")
    _seed_retired_ready_draft(db, "rd-vo")
    by_stage = _rail(run_detail(db, _ADMIN, "rd-vo"))
    assert by_stage["EXECUTE_SANDBOX"] == {"stage": "EXECUTE_SANDBOX", "state": "NOT_STARTED",
                                           "reason_code": None}
    # The production pair does NOT move with a deployment switch — it moves with the ACTION POLICY,
    # and no environment variable reaches that. A derivation that read the switches for all three
    # would open production here, which is precisely §0.1.0's prohibition.
    assert by_stage["MATERIALIZE_PRODUCTION"]["reason_code"] == "ACTION_UNAVAILABLE"
    assert by_stage["PUBLISH_PRODUCTION"]["reason_code"] == "ACTION_UNAVAILABLE"


def test_production_sockets_read_the_action_policy_not_a_stored_string(db, sandbox_switches_off,
                                                                      monkeypatch):
    """The production pair's availability is ASKED of `action_available`, never remembered.

    A stored `ACTION_UNAVAILABLE` reads identically today and becomes a lie on the day §21.0's
    production governance lands — the same class of stale label this task deleted three of. Moving
    the policy's answer must move the rail, so the policy is moved here and the rail is read.
    """
    monkeypatch.setattr("featuregen.runs.projection.action_available", lambda action: True)
    _seed_retired_ready_draft(db, "rd-pa")
    by_stage = _rail(run_detail(db, _ADMIN, "rd-pa"))
    for stage in ("MATERIALIZE_PRODUCTION", "PUBLISH_PRODUCTION"):
        assert by_stage[stage] == {"stage": stage, "state": "NOT_STARTED", "reason_code": None}
    # The two acts are asked SEPARATELY — one call per action, with that action's own name. A
    # derivation that asked once and reused the answer would report both on one act's policy.
    asked: list[str] = []
    monkeypatch.setattr("featuregen.runs.projection.action_available",
                        lambda action: asked.append(str(action)) or False)
    run_detail(db, _ADMIN, "rd-pa")
    assert asked == ["MATERIALIZE_PRODUCTION", "PUBLISH_PRODUCTION"]


def test_milestones_and_identity_are_derived_from_evidence(db):
    """Milestones are evidence-derived (spec §7): `CHOOSE_CANDIDATES` from 1025's rows.

    Also pins the two rail entries that are NOT constants: `CHOOSE_CANDIDATES` moves from
    NOT_STARTED to SUCCEEDED because a choice row exists, and `AUTHOR_FORMULA` stays NOT_STARTED
    while no draft does."""
    c = seed_run_chain(db, run_id="rd-m")
    empty = run_detail(db, _ADMIN, "rd-m")
    assert empty["milestones"]["choose_candidates"] == []
    assert _rail(empty)["CHOOSE_CANDIDATES"]["state"] == "NOT_STARTED"
    assert _rail(empty)["AUTHOR_FORMULA"]["state"] == "NOT_STARTED"   # no drafts yet
    # No `feature_run_identity` row: the chain is seeded but Task 4's writer never ran.
    assert empty["pre_spine"] is True and empty["identity"] is None
    assert empty["intent"] == {"intent_id": c["intent_id"], "hypothesis": "h"}
    assert empty["owner_subject"] == "u1"                             # pre-spine: actor subject

    db.execute(
        "INSERT INTO contract_gate1_choice_revision (choice_id, intent_id, generation_run_id, "
        "considered_revision_id, considered_content_hash, option_id, "
        "canonical_candidate_identity_hash, actor) "
        "VALUES ('rd-m-c1', %s, 'rd-m', %s, 'cch', 'o1', 'cid', %s)",
        (c["intent_id"], c["considered_revision_id"], Jsonb({"subject": "u1"})))
    out = run_detail(db, _ADMIN, "rd-m")
    chosen = out["milestones"]["choose_candidates"]
    assert [(m["option_id"], m["considered_revision_id"]) for m in chosen] == [
        ("o1", c["considered_revision_id"])]
    # Serialized, never handed back as a `datetime`: the projection's output is JSON.
    assert set(chosen[0]) == {"option_id", "considered_revision_id", "chosen_at"}
    assert isinstance(chosen[0]["chosen_at"], str) and chosen[0]["chosen_at"].startswith("20")
    assert out["milestones"]["bind_selections"] == []       # the 1101 binding, not yet written
    assert _rail(out)["CHOOSE_CANDIDATES"]["state"] == "SUCCEEDED"


def test_identity_row_makes_the_run_post_spine(db):
    from featuregen.runs.run_identity import record_run_identity

    c = seed_run_chain(db, run_id="rd-i")
    h = record_run_identity(db, "rd-i", IdentityEnvelope(
        subject="u1", actor_kind="human", authenticated=True, auth_method="test", role_claims=()))
    out = run_detail(db, _ADMIN, "rd-i")
    assert out["pre_spine"] is False
    assert out["identity"] == {"run_identity_hash": h,
                               "considered_revision_id": c["considered_revision_id"],
                               "metadata_snapshot_id": c["snapshot_id"]}


def test_author_formula_folds_worst_of_never_alphabetical(db):
    """The worst-of fold uses the SEVERITY order, not `sorted()`'s default (spec §12).

    The falsifying pair is CANCELLED against a state that sorts BEFORE it alphabetically but ranks
    BELOW it in severity: `ADMISSION` -> IN_PROGRESS. Alphabetical answers CANCELLED; the severity
    list answers IN_PROGRESS. The BLOCKED case is checked separately because BLOCKED happens to
    win under both orders, so on its own it proves nothing about the ordering."""
    c = seed_run_chain(db, run_id="rd-w")
    _seed_draft(db, c["considered_revision_id"], "rd-w-d1", "o1", "CANCELLED")
    _seed_draft(db, c["considered_revision_id"], "rd-w-d2", "o2", "ADMISSION")
    out = run_detail(db, _ADMIN, "rd-w")
    assert _rail(out)["AUTHOR_FORMULA"]["state"] == "IN_PROGRESS"
    # Neither draft was retired: eligibility is the OTHER axis and reports `current` regardless of
    # how bad the outcome axis reads.
    assert [d["eligibility"] for d in out["authoring"]] == ["current", "current"]
    assert [d["retirement_reason"] for d in out["authoring"]] == [None, None]

    c2 = seed_run_chain(db, run_id="rd-x")
    _seed_draft(db, c2["considered_revision_id"], "rd-x-d1", "o1", "READY")
    _seed_draft(db, c2["considered_revision_id"], "rd-x-d2", "o2", "BLOCKED")
    assert _rail(run_detail(db, _ADMIN, "rd-x"))["AUTHOR_FORMULA"]["state"] == "BLOCKED"


def test_owner_sees_their_own_run(db):
    """The POSITIVE half of the visibility splice, and the only test that can catch its param order.

    `visibility_where`'s params bind at the splice point, which is SECOND here. Binding them first
    does not raise — it compares the run id against the subject and the subject against the run id,
    matching nothing — so every None-expecting test in this file would still pass. Only a caller
    who MUST see a run falsifies it."""
    seed_run_chain(db, run_id="rd-o", subject="u1")
    owner = IdentityEnvelope(subject="u1", actor_kind="human", authenticated=True,
                             auth_method="test", role_claims=("feature_engineer",))
    out = run_detail(db, owner, "rd-o")
    assert out is not None and out["generation_run_id"] == "rd-o"


def test_invisible_run_returns_none(db):
    seed_run_chain(db, run_id="rd-b", subject="someone-else")
    other = IdentityEnvelope(subject="u9", actor_kind="human", authenticated=True,
                             auth_method="test", role_claims=("feature_engineer",))
    assert run_detail(db, other, "rd-b") is None


def test_absent_run_returns_none(db):
    """Absence and denial are indistinguishable to the caller — both None, both 404 at the route."""
    assert run_detail(db, _ADMIN, "no-such-run") is None
