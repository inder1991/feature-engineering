"""The run-detail projection (spec §12): milestones, two-axis authoring, and an honest rail.

Envelopes are built directly rather than minted (see `test_projection_list`): the projection takes
an envelope as data and verifies nothing, and the seeded chain must carry the SAME subject spelling
the caller does.
"""
import pytest
from psycopg.types.json import Jsonb
from tests.featuregen.materialize.crosswalk_fixtures import bind_ready_formula
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.projection import _AUTHOR_SEVERITY, RAIL_FROM_DRAFT_STATE, run_detail

_ADMIN = IdentityEnvelope(subject="a", actor_kind="human", authenticated=True,
                          auth_method="test", role_claims=("platform_admin",))

_DRAFT_STATES = {"REQUESTED", "AUTHORING", "CRITIC_REVIEW", "VALIDATING",
                 "ADMISSION", "READY", "BLOCKED", "FAILED", "CANCELLED"}  # 1090's CHECK, verbatim


def _seed_draft(db, considered_revision_id, draft_id, option_id, state, *,
                identity=None, at="2026-08-23T00:00:00Z"):
    """One `formula_draft` in `state`, satisfying 1090's state-dependent CHECKs.

    Three of them bite: READY must carry a real formula (`{}` is refused explicitly), BLOCKED must
    name at least one blocker, and `failure_reason` must be present for FAILED and absent for
    everything else. `requested_at` is TEXT in 1090, not timestamptz.

    `identity` defaults to one per draft — the shape every test wanted while 1090's unique index
    covered every row. A RETRY seeds two drafts with the SAME identity, which 1107 permits exactly
    when the earlier one bought nothing (FAILED/CANCELLED are outside its partial index), and `at`
    is what orders the attempts."""
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, formula_content_hash, formula_json, blockers, "
        "failure_reason, requested_by, requested_at) "
        "VALUES (%s, %s, %s, 'p', 'c', 'a', '', %s, %s, %s, %s, %s, %s, 'u1', %s)",
        (draft_id, considered_revision_id, option_id, identity or f"fih-{draft_id}", state,
         "fch" if state == "READY" else None,
         Jsonb({"op": "sum"}) if state == "READY" else None,
         Jsonb([{"code": "X"}] if state == "BLOCKED" else []),
         "boom" if state == "FAILED" else None, at))


def _seed_retired_ready_draft(db, run_id):
    """One READY draft on `run_id`'s candidate, retired afterwards — the §6.7 two-axis case.

    `CANDIDATE_SUPERSEDED` is one of 1096's three CHECK values, and `reason`/`retired_by`/
    `retired_at` are its real column names — both verified against the migration file."""
    c = seed_run_chain(db, run_id=run_id)
    _seed_draft(db, c["considered_revision_id"], f"{run_id}-d1", "o1", "READY")
    db.execute("INSERT INTO formula_draft_retirement (formula_draft_id, reason, retired_by, "
               "retired_at) VALUES (%s, 'CANDIDATE_SUPERSEDED', 'u1', now())", (f"{run_id}-d1",))
    return c


def _seed_choice(db, chain, option_id):
    """One gate-1 choice on this run's candidate — a row of `BIND_SELECTIONS`'s denominator.

    1025's lineage trigger checks the whole chain in one predicate, `considered_content_hash`
    included, so the hash is `seed_run_chain`'s own `'cch'` rather than a fresh string: a mismatch
    raises out of the INSERT instead of writing a row nobody can explain."""
    db.execute(
        "INSERT INTO contract_gate1_choice_revision (choice_id, intent_id, generation_run_id, "
        "considered_revision_id, considered_content_hash, option_id, "
        "canonical_candidate_identity_hash, actor) "
        "VALUES (%s, %s, %s, %s, 'cch', %s, %s, %s)",
        (f"{chain['run_id']}-c-{option_id}", chain["intent_id"], chain["run_id"],
         chain["considered_revision_id"], option_id, f"cid-{option_id}",
         Jsonb({"subject": "u1"})))


def _seed_binding(db, chain, option_id):
    """One SELECTION on this run's candidate with a READY formula PINNED to it (migration 1101).

    The binding is what `BIND_SELECTIONS` counts, and it reaches the run through the selection's
    considered revision — so the selection must name THIS run's revision or the row belongs to
    another run and the milestone must not see it.

    `bind_ready_formula` is the shipped fixture for the pin itself (it reads the candidate facts
    back out of the selection, because 1101's composite keys refuse a draft that disagrees with the
    choice it is pinned to). Only its parents are seeded here: the target reading a selection hangs
    off, and the selection."""
    reading_id, selection_id = f"{chain['run_id']}-trr", f"{chain['run_id']}-sel-{option_id}"
    db.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s, %s, 'exploration', 'h') ON CONFLICT DO NOTHING",
        (reading_id, chain["intent_id"]))
    db.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, binding_plan_hash, "
        "content_hash) VALUES (%s, %s, %s, %s, %s, 'sha256:asked', 'sha256:plan', %s)",
        (selection_id, reading_id, chain["considered_revision_id"], option_id,
         f"dec-{selection_id}", f"ch-{selection_id}"))
    return bind_ready_formula(db, selection_id)


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
    d = out["authoring"]["history"][0]
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


def test_the_switch_answers_before_the_pin_when_BOTH_are_shut(db, monkeypatch):
    """The PRECEDENCE cell — the one combination in which the two conditions disagree.

    Every other test in this file exercises the fold with at most one condition false, and a fold
    that answered the pin first would pass all of them: with the switch off and the pin PRESENT it
    still reaches `GENERATION_DISABLED`, and with the switch on and the pin absent it still reaches
    the pre-pin code. Only here — switch off AND pin absent — do the two branches give different
    answers, so this is the only test that can pin the order and kill the branch-swap mutant.

    The order is not a preference. A switched-off deployment does not have this surface AT ALL: the
    router-level dependency 404s every path on it, so naming the missing pin would send a person to
    an operator who would apply a migration and watch nothing change. Only where the surface exists
    does the pin decide.
    """
    monkeypatch.delenv("FEATUREGEN_GENERATION_V2_ENABLED", raising=False)
    _seed_retired_ready_draft(db, "rd-sp")
    _drop_the_pin(db)
    assert _rail(run_detail(db, _ADMIN, "rd-sp"))["GENERATE_PREVIEW"] == {
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
    # Both readings exist even with nothing to read: a run with no drafts has no attempts AND no
    # current answer, and the two empty lists say so without the client testing for a missing key.
    assert empty["authoring"] == {"current": [], "history": []}
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


def _bind_stage(detail):
    return _rail(detail)["BIND_SELECTIONS"]


def test_the_binding_milestone_accumulates_against_the_runs_choices(db):
    """`BIND_SELECTIONS` counts pins against the run's own choices (spec §7, R4.4's parked debt).

    ▲ THE MUTATION THIS KILLS is the one the stage shipped with: a constant `NOT_STARTED`, which
    told a person nothing had been bound while two formulas were pinned to their choices. The
    milestone is evidence-derived like `CHOOSE_CANDIDATES`, and the evidence is 1101's binding.

    ▲ AND IT MUST NOT ROUND UP. Two of five bound is IN_PROGRESS — a fold that answered SUCCEEDED
    on "any binding at all" would report the stage done with three choices carrying no formula,
    which is the false rail in the direction that costs the most: the next stage's entrance is the
    one that then refuses.

    The denominator is the CHOICE count because it is the only frozen-set-free honest one: nothing
    freezes "the selections this run intends to bind", and a selection revision is written when
    somebody selects — so counting selections would grow the denominator with the numerator and the
    milestone would read complete at every moment.
    """
    c = seed_run_chain(db, run_id="rd-bs")
    for n in range(5):
        _seed_choice(db, c, f"o{n}")
    _seed_binding(db, c, "o0")
    _seed_binding(db, c, "o1")

    out = run_detail(db, _ADMIN, "rd-bs")
    assert _bind_stage(out) == {"stage": "BIND_SELECTIONS", "state": "IN_PROGRESS",
                                "reason_code": None, "detail": "2 of 5 bound — accumulating"}
    # The milestone's own evidence, not just a number on the rail: the same list the choices
    # milestone keeps beside it, so a person can see WHICH choices carry a formula.
    bound = out["milestones"]["bind_selections"]
    assert sorted(b["option_id"] for b in bound) == ["o0", "o1"]
    assert set(bound[0]) == {"binding_id", "selection_revision_id", "formula_draft_id",
                             "considered_revision_id", "option_id", "recorded_at"}
    assert bound[0]["considered_revision_id"] == c["considered_revision_id"]
    # Serialized, never a `datetime` — the projection's output is JSON (the choices milestone's
    # own rule, and the two lists are read by the same client).
    assert isinstance(bound[0]["recorded_at"], str) and bound[0]["recorded_at"].startswith("20")


def test_the_binding_milestone_succeeds_only_at_parity_and_never_at_zero(db):
    """SUCCEEDED means every choice carries a formula — and `0 == 0` is not that.

    Two mutants die here. Dropping the `> 0` guard makes an untouched run — no choices, no
    bindings — read SUCCEEDED, which is a stage claiming completion for work nobody has started.
    Loosening parity to `>=` is caught by the accumulating test above.
    """
    c = seed_run_chain(db, run_id="rd-bp")
    _seed_choice(db, c, "o1")
    _seed_choice(db, c, "o2")
    assert _bind_stage(run_detail(db, _ADMIN, "rd-bp")) == {
        "stage": "BIND_SELECTIONS", "state": "NOT_STARTED", "reason_code": None, "detail": None}

    _seed_binding(db, c, "o1")
    _seed_binding(db, c, "o2")
    assert _bind_stage(run_detail(db, _ADMIN, "rd-bp")) == {
        "stage": "BIND_SELECTIONS", "state": "SUCCEEDED", "reason_code": None,
        "detail": "2 of 2 bound"}

    empty = seed_run_chain(db, run_id="rd-bz")
    assert _bind_stage(run_detail(db, _ADMIN, empty["run_id"]))["state"] == "NOT_STARTED"


def test_a_binding_with_no_choice_on_the_record_is_counted_without_a_denominator(db):
    """The world the platform is ACTUALLY in: `contract_gate1_choice_revision` has zero live rows.

    A denominator of nothing is not a denominator, so "1 of 0 bound" is not written — the count
    stands alone, and the stage stays IN_PROGRESS because there is no record against which it could
    honestly claim to be finished. The same branch covers a run holding MORE bindings than choices
    (one candidate selected under two target readings): a denominator smaller than the numerator
    has stopped describing the work.
    """
    c = seed_run_chain(db, run_id="rd-bd")
    _seed_binding(db, c, "o1")
    assert _bind_stage(run_detail(db, _ADMIN, "rd-bd")) == {
        "stage": "BIND_SELECTIONS", "state": "IN_PROGRESS", "reason_code": None,
        "detail": "1 bound"}


def test_one_choice_pinned_twice_is_one_choice_bound(db):
    """The numerator is counted in CANDIDATES, because the denominator is (1025's own key).

    1101's uniqueness is `(selection, draft)`, so a selection re-pinned to a second formula — two
    build sets declared either side of a re-authoring — is two rows for ONE choice. Counting rows
    would report two of two bound over a run where a person's second choice still carries nothing:
    a milestone reading COMPLETE with half the work missing, which is the exact failure this task
    exists to remove, arrived at from the other direction.

    Both pins stay in the evidence list. They are two facts about what was declared, and folding
    them there would destroy a record; the fold belongs to the count alone.
    """
    c = seed_run_chain(db, run_id="rd-bt")
    _seed_choice(db, c, "o1")
    _seed_choice(db, c, "o2")
    _seed_binding(db, c, "o1")
    bind_ready_formula(db, "rd-bt-sel-o1", draft_id="rd-bt-second-draft")

    out = run_detail(db, _ADMIN, "rd-bt")
    assert _bind_stage(out) == {"stage": "BIND_SELECTIONS", "state": "IN_PROGRESS",
                                "reason_code": None, "detail": "1 of 2 bound — accumulating"}
    assert len(out["milestones"]["bind_selections"]) == 2


def test_the_binding_milestone_is_unavailable_before_the_pin_lands(db):
    """Pre-1101 there is no store a binding could live in, so the milestone cannot RUN.

    NOT_STARTED would be the false rail §7 [R3.1] forbids — nothing a person did could ever move
    it — and the reason code is the pin's own, the same string `GENERATE_PREVIEW` answers with,
    because it is the same absent table seen from a second surface.

    ▲ It is also the guard's mutation check. Deleting the pre-pin branch does not merely mislabel
    the stage: the count query raises `UndefinedTable` out of a read-only projection, and this test
    (and the socket test, which drops the pin too) fails on the exception rather than the value.
    """
    c = seed_run_chain(db, run_id="rd-bnp")
    _seed_choice(db, c, "o1")
    _drop_the_pin(db)
    out = run_detail(db, _ADMIN, "rd-bnp")
    assert _bind_stage(out) == {"stage": "BIND_SELECTIONS", "state": "UNAVAILABLE",
                                "reason_code": "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN",
                                "detail": None}
    assert out["milestones"]["bind_selections"] == []


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
    assert [d["eligibility"] for d in out["authoring"]["history"]] == ["current", "current"]
    assert [d["retirement_reason"] for d in out["authoring"]["history"]] == [None, None]

    c2 = seed_run_chain(db, run_id="rd-x")
    _seed_draft(db, c2["considered_revision_id"], "rd-x-d1", "o1", "READY")
    _seed_draft(db, c2["considered_revision_id"], "rd-x-d2", "o2", "BLOCKED")
    assert _rail(run_detail(db, _ADMIN, "rd-x"))["AUTHOR_FORMULA"]["state"] == "BLOCKED"


def test_a_failed_attempt_is_history_and_the_retry_is_the_current_answer(db):
    """The 1107 world, read back: many drafts per identity, so ATTEMPT and ANSWER are two readings.

    1090's money guard covered every row, so one identity meant one draft and a fold over "the
    drafts" and a fold over "the answers" were the same fold. 1107 narrowed the index to
    `state NOT IN ('FAILED','CANCELLED')` — a failure bought nothing and must not hold the slot —
    so a governed retry now writes a SECOND row against the same identity and the two folds part
    company.

    ▲ THE MUTATION THIS KILLS: the rail folding `history` instead of `current` answers FAILED here,
    which is the run reporting a stage as broken while its actual answer is a formula. The failed
    attempt is not deleted or rewritten — it is history, and history is the other reading.
    """
    c = seed_run_chain(db, run_id="rd-h")
    _seed_draft(db, c["considered_revision_id"], "rd-h-d1", "o1", "FAILED",
                identity="rd-h-identity", at="2026-08-23T00:00:01Z")
    _seed_draft(db, c["considered_revision_id"], "rd-h-d2", "o1", "READY",
                identity="rd-h-identity", at="2026-08-23T00:00:02Z")
    out = run_detail(db, _ADMIN, "rd-h")

    assert [(r["formula_draft_id"], r["state"], r["rail_state"])
            for r in out["authoring"]["history"]] == [
        ("rd-h-d1", "FAILED", "FAILED"), ("rd-h-d2", "READY", "SUCCEEDED")]
    # The current answer is the one row that HOLDS the identity slot, and `resolved` says the
    # platform actually bought something. Both halves of the candidate key ride each row: the
    # option id alone is not the candidate.
    assert out["authoring"]["current"] == [{
        "formula_draft_id": "rd-h-d2", "considered_revision_id": c["considered_revision_id"],
        "option_id": "o1", "state": "READY", "rail_state": "SUCCEEDED",
        "eligibility": "current", "retirement_reason": None, "resolved": True}]
    assert _rail(out)["AUTHOR_FORMULA"]["state"] == "SUCCEEDED"
    # `resolved` belongs to the CURRENT reading only. Stamping it onto every history row would
    # invite a reader to ask of a superseded attempt a question only the latest one answers.
    assert all("resolved" not in r for r in out["authoring"]["history"])


def test_a_candidate_whose_every_attempt_bought_nothing_still_has_a_current_reading(db):
    """No answer is not the same as no attempt (spec §R4.4.1).

    With every draft for a candidate FAILED or CANCELLED, nothing holds the identity slot — and a
    `current` reading built from slot-holders alone would be EMPTY, which folds the rail to
    NOT_STARTED and tells a person the platform never tried. It tried twice. The most recent
    terminal attempt is therefore the current reading, flagged `resolved: False`: this is where the
    candidate stands, and it stands on nothing bought.
    """
    c = seed_run_chain(db, run_id="rd-n")
    _seed_draft(db, c["considered_revision_id"], "rd-n-d1", "o1", "FAILED",
                identity="rd-n-identity", at="2026-08-23T00:00:01Z")
    _seed_draft(db, c["considered_revision_id"], "rd-n-d2", "o1", "CANCELLED",
                identity="rd-n-identity", at="2026-08-23T00:00:02Z")
    out = run_detail(db, _ADMIN, "rd-n")

    assert [r["formula_draft_id"] for r in out["authoring"]["history"]] == ["rd-n-d1", "rd-n-d2"]
    assert [(r["formula_draft_id"], r["state"], r["resolved"])
            for r in out["authoring"]["current"]] == [("rd-n-d2", "CANCELLED", False)]
    assert _rail(out)["AUTHOR_FORMULA"]["state"] == "CANCELLED"


def test_the_current_reading_is_per_candidate_and_the_rail_folds_only_those(db):
    """The grouping key is the CANDIDATE — `(considered_revision_id, option_id)` — not the run.

    Two candidates, each retried once. Every naive fold gets this wrong in a different direction: a
    fold over the whole history answers FAILED (both attempts that failed are still on the record);
    a fold over the FIRST row per candidate answers FAILED too; a fold that grouped by run alone
    would report ONE answer for a run that has two candidates and two.

    ▲ THE IDS SORT BACKWARDS on purpose. `requested_at` is the attempt order and the draft id is
    only the tie-breaker, so these four ids are lexically the REVERSE of the order they were asked
    for. The old `ORDER BY d.formula_draft_id` then fails twice over: the history reads inside out,
    and "latest per candidate" picks the two failures — a rail reading FAILED over a run whose
    candidates are done and in flight.
    """
    c = seed_run_chain(db, run_id="rd-g")
    ccr = c["considered_revision_id"]
    _seed_draft(db, ccr, "rd-g-z1", "o1", "FAILED", identity="rd-g-i1", at="2026-08-23T00:00:01Z")
    _seed_draft(db, ccr, "rd-g-y2", "o2", "FAILED", identity="rd-g-i2", at="2026-08-23T00:00:02Z")
    _seed_draft(db, ccr, "rd-g-b3", "o1", "READY", identity="rd-g-i1", at="2026-08-23T00:00:03Z")
    _seed_draft(db, ccr, "rd-g-a4", "o2", "REQUESTED", identity="rd-g-i2",
                at="2026-08-23T00:00:04Z")
    out = run_detail(db, _ADMIN, "rd-g")

    # History is every attempt in the order they were REQUESTED, interleaved across candidates.
    assert [r["formula_draft_id"] for r in out["authoring"]["history"]] == [
        "rd-g-z1", "rd-g-y2", "rd-g-b3", "rd-g-a4"]
    assert [(r["option_id"], r["formula_draft_id"], r["resolved"])
            for r in out["authoring"]["current"]] == [
        ("o1", "rd-g-b3", True), ("o2", "rd-g-a4", True)]
    # Worst-of over the CURRENT answers: one candidate is done, the other is still being authored.
    assert _rail(out)["AUTHOR_FORMULA"]["state"] == "IN_PROGRESS"


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
