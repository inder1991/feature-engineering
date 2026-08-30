"""B2 — the TOTAL binding chain (migration 1135).

What these tests pin:

* the four tables exist, and the chain carries ONE logical identity from the served option to the
  build member;
* TOTALITY, the plan's law, per table and by the mechanism 1135 chose for it — the five refusals:
  option-without-logical-binding, draft-without-binding, selection-binding-across-plans,
  previewable-selection-without-confirmed-adoption, build-member-without-combined-binding;
* the RELATIONAL closure: a composite foreign key, not a validation, is what makes a draft, a
  selection and a build member unable to name different plans;
* the PLAN LEG is a verifying load — a 1134 row that cannot reproduce its own digest STOPS the
  binding (B1's probe shape), which is more than an FK could have proven;
* LEGACY rows are explicitly PRE-PLAN and are refused for cross-catalog generation, never
  back-filled;
* append-only, and content/key-addressed idempotency.

▲ THE DEFERRED TRIGGERS AND THIS HARNESS. The suite's connection never commits (the root `conn`
fixture rolls back on teardown), so a constraint trigger that fires at COMMIT would never fire at
all. `commit_checks` runs `SET CONSTRAINTS ALL IMMEDIATE`, which performs exactly the check COMMIT
would perform, at a point a test can observe.
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.featuregen.overlay.upload.planner._binding_seeds import (
    commit_checks,
    execution_context,
    join_policy,
    logical_plan,
    physical_plan,
    render_profile,
    seed_build_set,
    seed_draft,
    seed_option,
    seed_selection,
    seed_target_reading,
)

from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.planner.adoption_store import confirm_physical_plan_adoption
from featuregen.overlay.upload.planner.binding_chain import (
    COMBINED_BINDING_ID_PREFIX,
    BindingChainConflict,
    BindingChainDefect,
    assert_build_environment_match,
    bind_build_member_combined,
    bind_considered_option_plan,
    bind_formula_draft_plan,
    bind_selection_formula_plan,
    load_combined_binding,
    load_considered_option_plan_binding,
    load_formula_draft_plan_binding,
    load_selection_formula_plan_binding,
    pin_build_set_member,
    require_planned_selection,
)
from featuregen.overlay.upload.planner.identity_store import (
    LOGICAL_PLAN_ID_PREFIX,
    IdentityStoreConflict,
    ensure_logical_feature_plan,
    ensure_physical_execution_plan,
    ensure_render_profile,
    logical_digest,
)
from featuregen.overlay.upload.planner.join_policy_store import ensure_join_validation_policy
from featuregen.overlay.upload.selection_formula_binding import record_selection_formula_binding

TABLES = (
    "considered_option_plan_binding",
    "formula_draft_plan_binding",
    "selection_formula_plan_binding",
    "build_member_combined_binding",
)


# ── the chain, built once, in the order the journey builds it ─────────────────────────────────
def _logical(db, **over):
    plan = logical_plan(**over)
    return ensure_logical_feature_plan(db, plan=plan).revision_id, logical_digest(plan)


def _chain(db, *, planned=True, environment_id="env-uat", suffix="1", reading=None,
           tier=ExecutionTier.SANDBOX):
    """Option -> plan -> draft -> selection -> adoption -> combined binding, all the way through."""
    considered, option = seed_option(db, considered=f"cr_{suffix}", option=f"opt_{suffix}",
                                     planned=planned)
    if reading is None:
        reading = seed_target_reading(db, revision_id=f"trr_{suffix}",
                                      intent_id=f"intent_{suffix}")
    plan_id, digest = _logical(db, operation=f"sum_window_delta_{suffix}")
    bind_considered_option_plan(db, considered_revision_id=considered, option_id=option,
                                logical_plan_revision_id=plan_id)
    draft = seed_draft(db, considered=considered, option=option, draft_id=f"fd_{suffix}",
                       formula_content_hash=f"fch_{suffix}")
    bind_formula_draft_plan(db, formula_draft_id=draft)
    selection = seed_selection(db, reading=reading, considered=considered, option=option,
                               revision_id=f"fsr_{suffix}")
    record_selection_formula_binding(db, selection_revision_id=selection, formula_draft_id=draft)
    bind_selection_formula_plan(db, selection_revision_id=selection, formula_draft_id=draft)

    context = execution_context(db, environment_id=environment_id, tier=tier)
    policy = ensure_join_validation_policy(db, policy=join_policy())
    physical = ensure_physical_execution_plan(
        db, plan=physical_plan(context_id=context, logical_digest_ref=digest, policy_id=policy))
    adoption, _ = confirm_physical_plan_adoption(
        db, selection_revision_id=selection, execution_context_revision_id=context,
        physical_plan_revision_id=physical, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    profile = ensure_render_profile(db, profile=render_profile())
    combined = bind_build_member_combined(
        db, selection_revision_id=selection, formula_draft_id=draft,
        physical_adoption_revision_id=adoption.adoption_revision_id,
        render_profile_revision_id=profile)
    return {
        "considered": considered, "option": option, "reading": reading, "logical_plan": plan_id,
        "digest": digest, "draft": draft, "selection": selection, "context": context,
        "physical": physical, "adoption": adoption, "profile": profile, "combined": combined,
    }


@pytest.mark.parametrize("table", TABLES)
def test_the_1135_tables_exist(db, table) -> None:
    assert db.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0] is not None


def test_one_logical_identity_reaches_the_build_member(db) -> None:
    """The claim in one assertion: the meaning the option was a plan for is the meaning the build
    member generates, with nothing in between free to change it."""
    c = _chain(db)
    option_binding = load_considered_option_plan_binding(
        db, considered_revision_id=c["considered"], option_id=c["option"])
    draft_binding = load_formula_draft_plan_binding(db, c["draft"])
    selection_binding = load_selection_formula_plan_binding(
        db, selection_revision_id=c["selection"], formula_draft_id=c["draft"])
    combined = c["combined"]

    assert option_binding.logical_digest == c["digest"]
    assert draft_binding.logical_digest == c["digest"]
    assert selection_binding.logical_digest == c["digest"]
    assert combined.logical_digest == c["digest"]
    assert combined.combined_binding_id.startswith(COMBINED_BINDING_ID_PREFIX)
    assert combined.physical_plan_revision_id == c["physical"]
    assert combined.render_profile_revision_id == c["profile"]
    assert combined.execution_context_revision_id == c["context"]
    commit_checks(db)


def test_the_legacy_hashes_ride_as_provenance_and_are_never_a_digest(db) -> None:
    """Round 10's ruling, made visible: the pins are CARRIED, and they are not the identity.

    `planning_request_hash` and `binding_plan_hash` are copied from the option and the selection
    rows so a request stays traceable; neither is ever compared against `logical_digest`, which
    hashes a completely different payload."""
    c = _chain(db)
    selection_binding = load_selection_formula_plan_binding(
        db, selection_revision_id=c["selection"], formula_draft_id=c["draft"])
    assert selection_binding.planning_request_hash == "prh_1"
    assert selection_binding.binding_plan_hash == "bph_1"
    assert selection_binding.planning_request_hash != selection_binding.logical_digest
    assert selection_binding.binding_plan_hash != selection_binding.logical_digest
    # And they are the SELECTION's own columns, not values a caller supplied.
    stored = db.execute(
        "SELECT planning_request_hash, binding_plan_hash FROM feature_selection_revision "
        "WHERE revision_id = %s", (c["selection"],)).fetchone()
    assert tuple(stored) == (selection_binding.planning_request_hash,
                             selection_binding.binding_plan_hash)


# ── REFUSAL 1: an option that declares itself planned must carry a logical plan ───────────────
def test_a_planned_option_without_a_logical_binding_refuses_at_commit(db) -> None:
    seed_option(db, planned=True)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        commit_checks(db)
    assert "requires a logical plan binding" in str(excinfo.value)


def test_a_pre_plan_option_without_a_logical_binding_is_fine(db) -> None:
    """The other half of the same law: legacy, single-catalog options are NOT broken. They are
    pre-plan, the marker is false, and nothing is back-filled."""
    seed_option(db, planned=False)
    commit_checks(db)
    assert load_considered_option_plan_binding(
        db, considered_revision_id="cr_1", option_id="opt_1") is None


# ── REFUSAL 2: a draft for a planned option must carry a plan binding ─────────────────────────
def test_a_draft_for_a_planned_option_without_a_binding_refuses_at_commit(db) -> None:
    considered, option = seed_option(db, planned=True)
    plan_id, _ = _logical(db)
    bind_considered_option_plan(db, considered_revision_id=considered, option_id=option,
                                logical_plan_revision_id=plan_id)
    seed_draft(db)                                    # authored, never bound
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        commit_checks(db)
    assert "has no plan binding" in str(excinfo.value)


def test_a_draft_for_a_pre_plan_option_is_refused_by_the_store_not_invented(db) -> None:
    seed_option(db, planned=False)
    seed_draft(db)
    with pytest.raises(BindingChainDefect) as excinfo:
        bind_formula_draft_plan(db, formula_draft_id="fd_1")
    assert "PRE-PLAN option" in str(excinfo.value)


# ── REFUSAL 3: a selection may not be bound across plans ──────────────────────────────────────
def test_a_selection_binding_naming_another_plan_is_unrepresentable(db) -> None:
    """The composite foreign key, not a check in Python: the row cannot exist at all.

    The store cannot even produce this defect — it DERIVES the digest from the draft's binding — so
    the attempt is made in raw SQL, which is exactly the caller a worker-side validation would have
    missed."""
    considered, option = seed_option(db, planned=True)
    reading = seed_target_reading(db)
    plan_id, digest = _logical(db)
    bind_considered_option_plan(db, considered_revision_id=considered, option_id=option,
                                logical_plan_revision_id=plan_id)
    draft = seed_draft(db)
    bind_formula_draft_plan(db, formula_draft_id=draft)
    selection = seed_selection(db, reading=reading)
    record_selection_formula_binding(db, selection_revision_id=selection, formula_draft_id=draft)

    other_id, other_digest = _logical(db, operation="mean_window_delta")
    assert other_digest != digest
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO selection_formula_plan_binding (selection_revision_id, formula_draft_id, "
            "  logical_plan_revision_id, logical_digest, planning_request_hash, binding_plan_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (selection, draft, other_id, other_digest, "prh_1", "bph_1"))


def test_a_draft_binding_naming_another_plan_than_its_option_is_unrepresentable(db) -> None:
    """The same closure one link up: a draft's plan must BE its option's plan."""
    considered, option = seed_option(db, planned=True)
    plan_id, digest = _logical(db)
    bind_considered_option_plan(db, considered_revision_id=considered, option_id=option,
                                logical_plan_revision_id=plan_id)
    draft = seed_draft(db)
    other_id, other_digest = _logical(db, operation="mean_window_delta")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO formula_draft_plan_binding (formula_draft_id, considered_revision_id, "
            "  option_id, logical_plan_revision_id, logical_digest, planning_request_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (draft, considered, option, other_id, other_digest, "prh_1"))


def test_a_plan_binding_without_1101s_pin_is_refused(db) -> None:
    """A plan binding EXTENDS the (selection, formula) pin; it never stands in for it."""
    considered, option = seed_option(db, planned=True)
    reading = seed_target_reading(db)
    plan_id, _ = _logical(db)
    bind_considered_option_plan(db, considered_revision_id=considered, option_id=option,
                                logical_plan_revision_id=plan_id)
    draft = seed_draft(db)
    bind_formula_draft_plan(db, formula_draft_id=draft)
    selection = seed_selection(db, reading=reading)
    with pytest.raises(BindingChainDefect) as excinfo:
        bind_selection_formula_plan(db, selection_revision_id=selection, formula_draft_id=draft)
    assert "not pinned to each other" in str(excinfo.value)


# ── REFUSAL 4: a previewable selection needs its CONFIRMED adoption ───────────────────────────
def test_a_combined_binding_naming_no_real_adoption_refuses_at_commit(db) -> None:
    """A combined binding IS the declaration that a selection may be previewed. 1136's deferred
    trigger is what makes "previewable without a confirmed adoption" impossible: the store refuses
    it too, but the store is not the guarantee."""
    c = _chain(db)
    forged = "spa_" + "9" * 64
    db.execute(
        "INSERT INTO build_member_combined_binding (combined_binding_id, selection_revision_id, "
        "  formula_draft_id, logical_digest, physical_plan_revision_id, physical_digest, "
        "  physical_adoption_revision_id, execution_context_revision_id, "
        "  render_profile_revision_id, render_profile_digest, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        ("cmb_" + "7" * 64, c["selection"], c["draft"], c["digest"], c["physical"],
         c["physical"][4:], forged, c["context"], c["profile"], c["profile"][4:], "7" * 64))
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        commit_checks(db)
    assert "does not exist at COMMIT" in str(excinfo.value)


def test_the_store_refuses_an_adoption_that_does_not_exist(db) -> None:
    c = _chain(db)
    with pytest.raises(BindingChainDefect) as excinfo:
        bind_build_member_combined(
            db, selection_revision_id=c["selection"], formula_draft_id=c["draft"],
            physical_adoption_revision_id="spa_" + "9" * 64,
            render_profile_revision_id=c["profile"])
    assert "does not exist" in str(excinfo.value)


# ── REFUSAL 5: a build member of a planned selection must name its combined binding ───────────
def test_a_member_of_a_planned_selection_without_a_combined_binding_refuses_immediately(db) -> None:
    """▲ IMMEDIATE, not at COMMIT (1140). The member's FK onto its combined binding is not
    DEFERRABLE and 1092 forbids UPDATE, so a member can only ever be written after its binding
    exists — the deferral 1135 took bought nothing and queued pending events that made ALTER TABLE
    refuse. Checking now also makes the refusal name the statement that caused it."""
    c = _chain(db)
    build_set = seed_build_set(db, reading=c["reading"])
    binding_id = db.execute(
        "SELECT binding_id FROM selection_formula_binding WHERE selection_revision_id = %s",
        (c["selection"],)).fetchone()[0]
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        db.execute(
            "INSERT INTO build_set_member (revision_id, position, selection_revision_id, "
            "  selection_formula_binding_id) VALUES (%s, 0, %s, %s)",
            (build_set, c["selection"], binding_id))
    assert "no combined binding at" in str(excinfo.value)


def test_altering_build_set_member_after_inserting_one_now_works(db) -> None:
    """The cost 1135 paid for a deferral it did not need, and 1140 stopped paying. A pending
    constraint-trigger event makes Postgres refuse ALTER TABLE with ObjectInUse; with the plain
    trigger there is no pending event, and the law still fired on the insert above."""
    c = _chain(db)
    build_set = seed_build_set(db, reading=c["reading"])
    pin_build_set_member(db, build_set_revision_id=build_set, position=0,
                         selection_revision_id=c["selection"],
                         combined_binding_id=c["combined"].combined_binding_id)
    db.execute("ALTER TABLE build_set_member ADD COLUMN IF NOT EXISTS b2_probe text")
    db.execute("ALTER TABLE build_set_member DROP COLUMN IF EXISTS b2_probe")


def test_a_member_that_names_its_combined_binding_commits(db) -> None:
    c = _chain(db)
    build_set = seed_build_set(db, reading=c["reading"])
    pin_build_set_member(db, build_set_revision_id=build_set, position=0,
                         selection_revision_id=c["selection"],
                         combined_binding_id=c["combined"].combined_binding_id)
    commit_checks(db)
    stored = db.execute(
        "SELECT combined_binding_id FROM build_set_member WHERE revision_id = %s AND position = 0",
        (build_set,)).fetchone()
    assert stored[0] == c["combined"].combined_binding_id


def test_a_member_may_not_name_a_binding_for_another_selection(db) -> None:
    """1101's shape, one level up: the composite foreign key forces the member and its binding to
    name ONE selection."""
    c = _chain(db)
    build_set = seed_build_set(db, reading=c["reading"])
    other_reading = seed_target_reading(db, revision_id="trr_other", intent_id="intent_other")
    other_selection = seed_selection(db, reading=other_reading, revision_id="fsr_other")
    binding_id = db.execute(
        "SELECT binding_id FROM selection_formula_binding WHERE selection_revision_id = %s",
        (c["selection"],)).fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO build_set_member (revision_id, position, selection_revision_id, "
            "  selection_formula_binding_id, combined_binding_id) VALUES (%s, 0, %s, %s, %s)",
            (build_set, other_selection, binding_id, c["combined"].combined_binding_id))


# ── the two orphans 1135 let through, closed by 1140 ─────────────────────────────────────────
def test_a_member_written_before_its_selection_was_planned_blocks_the_planning(db) -> None:
    """REVIEWER CONSTRUCTION (A) — the orphan built by ORDER, not by a missing check.

    Write the member first, using exactly the statement `build_set_store` uses today. The selection
    is not planned yet, so the member law is satisfied. THEN plan the selection. Under 1135 that
    produced a permanently unrepairable orphan: a planned selection with a member carrying no
    combined binding, and both tables append-only. 1140 refuses the PLANNING — the last moment at
    which anything can still be done — and says what to do instead."""
    considered, option = seed_option(db, planned=True)
    reading = seed_target_reading(db)
    plan_id, _ = _logical(db)
    bind_considered_option_plan(db, considered_revision_id=considered, option_id=option,
                                logical_plan_revision_id=plan_id)
    draft = seed_draft(db)
    bind_formula_draft_plan(db, formula_draft_id=draft)
    selection = seed_selection(db, reading=reading)
    pin = record_selection_formula_binding(
        db, selection_revision_id=selection, formula_draft_id=draft)[0]

    build_set = seed_build_set(db, reading=reading)
    db.execute(
        "INSERT INTO build_set_member (revision_id, position, selection_revision_id, "
        "  selection_formula_binding_id) VALUES (%s, 0, %s, %s)",
        (build_set, selection, pin.binding_id))          # legal: the selection is not planned yet

    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        bind_selection_formula_plan(db, selection_revision_id=selection, formula_draft_id=draft)
    message = str(excinfo.value)
    assert "cannot now be bound to a logical plan" in message
    assert "Declare a NEW build set" in message


def test_a_governed_selection_may_not_skip_its_plan_binding(db) -> None:
    """REVIEWER CONSTRUCTION (B) — the link that was simply never made.

    A planned option, bound; a draft, bound; 1101's pin recorded — and then nothing. Under 1135
    every commit check passed, the member law's precondition was the very link that was skipped, and
    the store went on to report this governed selection as PRE-PLAN and refuse it for cross-catalog
    generation: a governed choice silently misclassified as legacy. 1140 makes the selection link
    total, inheriting the requirement from the draft exactly as the draft inherits it from its
    option."""
    considered, option = seed_option(db, planned=True)
    reading = seed_target_reading(db)
    plan_id, _ = _logical(db)
    bind_considered_option_plan(db, considered_revision_id=considered, option_id=option,
                                logical_plan_revision_id=plan_id)
    draft = seed_draft(db)
    bind_formula_draft_plan(db, formula_draft_id=draft)
    selection = seed_selection(db, reading=reading)
    record_selection_formula_binding(db, selection_revision_id=selection, formula_draft_id=draft)

    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        commit_checks(db)
    message = str(excinfo.value)
    assert "has no plan binding at COMMIT" in message
    assert "misclassified as legacy" in message


def test_a_pre_plan_pin_still_commits_untouched(db) -> None:
    """The other half of the same law: 1140 fires only when the DRAFT is plan-bound, so every
    legacy (selection, formula) pin in the platform still commits with nothing required of it."""
    seed_option(db, planned=False)
    reading = seed_target_reading(db)
    draft = seed_draft(db)
    selection = seed_selection(db, reading=reading)
    record_selection_formula_binding(db, selection_revision_id=selection, formula_draft_id=draft)
    commit_checks(db)
    assert load_selection_formula_plan_binding(
        db, selection_revision_id=selection, formula_draft_id=draft) is None


# ── the arming check: a dormant law is a defect, not a default ───────────────────────────────
def test_binding_an_unmarked_option_refuses_so_a_dormant_law_cannot_ship(db) -> None:
    """1135's option AND draft laws are both gated on `requires_logical_plan_binding`, and the
    marker is one-shot: 1063 refuses UPDATE and the production writer ends in ON CONFLICT DO
    NOTHING, so an unmarked option can never be armed later. Without this refusal the whole chain
    could be wired, run green, and enforce nothing."""
    seed_option(db, planned=False)
    plan_id, _ = _logical(db)
    with pytest.raises(BindingChainDefect) as excinfo:
        bind_considered_option_plan(db, considered_revision_id="cr_1", option_id="opt_1",
                                    logical_plan_revision_id=plan_id)
    message = str(excinfo.value)
    assert "DORMANT" in message
    assert "can never be set afterwards" in message
    assert load_considered_option_plan_binding(
        db, considered_revision_id="cr_1", option_id="opt_1") is None


# ── the build environment (R3's last property) ────────────────────────────────────────────────
def test_a_build_set_may_not_mix_execution_contexts(db) -> None:
    """The structural half of "build members' adoptions match the build environment": one build,
    one environment, enforced at COMMIT rather than hoped for."""
    first = _chain(db, suffix="1", environment_id="env-uat")
    # A SECOND feature, chosen under the same reading, but adopted in PRODUCTION.
    second = _chain(db, suffix="2", environment_id="env-prod", reading=first["reading"],
                    tier=ExecutionTier.PRODUCTION)
    assert first["context"] != second["context"]

    build_set = seed_build_set(db, reading=first["reading"])
    pin_build_set_member(db, build_set_revision_id=build_set, position=0,
                         selection_revision_id=first["selection"],
                         combined_binding_id=first["combined"].combined_binding_id)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        pin_build_set_member(db, build_set_revision_id=build_set, position=1,
                             selection_revision_id=second["selection"],
                             combined_binding_id=second["combined"].combined_binding_id)
    assert "execution contexts" in str(excinfo.value)


def test_the_build_environment_must_match_the_environment_being_generated_for(db) -> None:
    """The half DDL cannot state: a build's environment lives on `generation_request`, so the match
    against a REQUEST is a store check with a named refusal."""
    c = _chain(db, environment_id="env-uat")
    build_set = seed_build_set(db, reading=c["reading"])
    pin_build_set_member(db, build_set_revision_id=build_set, position=0,
                         selection_revision_id=c["selection"],
                         combined_binding_id=c["combined"].combined_binding_id)
    assert_build_environment_match(db, build_set_revision_id=build_set, environment_id="env-uat")
    with pytest.raises(BindingChainDefect) as excinfo:
        assert_build_environment_match(db, build_set_revision_id=build_set,
                                       environment_id="env-prod")
    assert "may not borrow" in str(excinfo.value)


# ── PRE-PLAN rows are refused, never back-filled ──────────────────────────────────────────────
def test_a_pre_plan_selection_is_refused_for_cross_catalog_generation(db) -> None:
    seed_option(db, planned=False)
    reading = seed_target_reading(db)
    selection = seed_selection(db, reading=reading)
    with pytest.raises(BindingChainDefect) as excinfo:
        require_planned_selection(db, selection)
    message = str(excinfo.value)
    assert "PRE-PLAN selection" in message
    assert "not a plan" in message


def test_a_planned_selection_answers_its_binding(db) -> None:
    c = _chain(db)
    assert require_planned_selection(db, c["selection"]).logical_digest == c["digest"]


# ── the plan leg is a VERIFYING LOAD, not an existence probe ──────────────────────────────────
def test_binding_an_option_to_a_corrupt_logical_plan_row_refuses(db) -> None:
    """B1's probe shape. The row satisfies every CHECK 1134 can express — the id derives from the
    hash, the digest equals the hash — and STILL cannot reproduce its own identity, because its
    stored content is not what that hash summarizes. An FK would have accepted it."""
    seed_option(db, planned=True)
    forged = "5" * 64
    tampered = logical_plan(operation="mean_window_delta").content_payload()
    db.execute(
        "INSERT INTO logical_feature_plan_revision (revision_id, logical_digest, content, "
        "  content_hash) VALUES (%s, %s, %s, %s)",
        (LOGICAL_PLAN_ID_PREFIX + forged, forged, Jsonb(tampered), forged))
    with pytest.raises(IdentityStoreConflict) as excinfo:
        bind_considered_option_plan(db, considered_revision_id="cr_1", option_id="opt_1",
                                    logical_plan_revision_id=LOGICAL_PLAN_ID_PREFIX + forged)
    assert "digest" in str(excinfo.value)


def test_binding_an_option_to_a_plan_nobody_persisted_refuses(db) -> None:
    seed_option(db, planned=True)
    with pytest.raises(BindingChainDefect) as excinfo:
        bind_considered_option_plan(db, considered_revision_id="cr_1", option_id="opt_1",
                                    logical_plan_revision_id=LOGICAL_PLAN_ID_PREFIX + "4" * 64)
    assert "never persisted" in str(excinfo.value)


def test_binding_an_option_nobody_recorded_refuses(db) -> None:
    plan_id, _ = _logical(db)
    with pytest.raises(BindingChainDefect) as excinfo:
        bind_considered_option_plan(db, considered_revision_id="cr_missing", option_id="opt_1",
                                    logical_plan_revision_id=plan_id)
    assert "never recorded" in str(excinfo.value)


# ── idempotency and the one-meaning rule ──────────────────────────────────────────────────────
def test_binding_twice_is_one_row(db) -> None:
    c = _chain(db)
    again = bind_considered_option_plan(
        db, considered_revision_id=c["considered"], option_id=c["option"],
        logical_plan_revision_id=c["logical_plan"])
    assert again.logical_digest == c["digest"]
    for table in TABLES:
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    combined_again = bind_build_member_combined(
        db, selection_revision_id=c["selection"], formula_draft_id=c["draft"],
        physical_adoption_revision_id=c["adoption"].adoption_revision_id,
        render_profile_revision_id=c["profile"])
    assert combined_again.combined_binding_id == c["combined"].combined_binding_id
    assert db.execute(
        "SELECT count(*) FROM build_member_combined_binding").fetchone()[0] == 1


def test_an_option_cannot_be_re_aimed_at_a_second_meaning(db) -> None:
    c = _chain(db)
    other_id, _ = _logical(db, operation="mean_window_delta")
    with pytest.raises(BindingChainConflict) as excinfo:
        bind_considered_option_plan(db, considered_revision_id=c["considered"],
                                    option_id=c["option"], logical_plan_revision_id=other_id)
    assert "already bound" in str(excinfo.value)


def test_the_combined_binding_verifies_its_own_identity_on_load(db) -> None:
    c = _chain(db)
    forged = "3" * 64
    db.execute(
        "INSERT INTO build_member_combined_binding (combined_binding_id, selection_revision_id, "
        "  formula_draft_id, logical_digest, physical_plan_revision_id, physical_digest, "
        "  physical_adoption_revision_id, execution_context_revision_id, "
        "  render_profile_revision_id, render_profile_digest, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (COMBINED_BINDING_ID_PREFIX + forged, c["selection"], c["draft"], c["digest"],
         c["physical"], c["physical"][4:], c["adoption"].adoption_revision_id, c["context"],
         c["profile"], c["profile"][4:], forged))
    with pytest.raises(BindingChainConflict) as excinfo:
        load_combined_binding(db, COMBINED_BINDING_ID_PREFIX + forged)
    assert "does not reproduce its own identity" in str(excinfo.value)


# ── append-only ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("statement", ["UPDATE {t} SET recorded_at = now()", "DELETE FROM {t}"])
def test_the_binding_tables_are_append_only(db, table, statement) -> None:
    _chain(db)
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        db.execute(statement.format(t=table))
    assert "append-only" in str(excinfo.value)


@pytest.mark.parametrize("table", TABLES)
def test_a_truncate_raiser_here_could_never_fire_which_is_why_there_is_none(db, table) -> None:
    """A4's discovery, pinned rather than restated. Every binding table is FK-referenced by the next
    link in the chain, so Postgres refuses a TRUNCATE with FeatureNotSupported BEFORE any BEFORE
    TRUNCATE trigger would run. Adding a raiser would have looked like a guard and proved nothing;
    this test is what stops a later reader from "fixing" the omission.

    ▲ AND IT REDDENS IF THAT STOPS BEING TRUE — if a link's foreign key were ever dropped, the
    TRUNCATE would succeed and this test would fail, which is the moment to decide again."""
    _chain(db)
    commit_checks(db)      # flush the deferred triggers; a pending event masks the FK refusal
    with pytest.raises(psycopg.errors.FeatureNotSupported) as excinfo:
        db.execute(f"TRUNCATE TABLE {table}")
    assert "foreign key" in str(excinfo.value)
