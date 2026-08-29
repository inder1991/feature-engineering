"""B2 — R3's physical-plan adoption chain and the join-validation-policy store (migration 1136).

What these tests pin:

* the guard-policy store, and with it B1's formerly UNCHECKED pin: a physical plan naming a policy
  nobody declared is now refused at write;
* R3 property by property — append-only, user-confirmed, ENVIRONMENT-SCOPED (a sandbox and a
  production adoption coexisting on one selection), the partial-unique root, one successor per
  predecessor, the semantic-only content hash, and idempotent re-confirmation;
* the CAS: the two partial-unique indexes are what decide a race, and a writer whose head read went
  stale loses to them rather than overwriting;
* the adoption's own agreement checks — a plan built for another context, a PRE-PLAN option, a plan
  that realizes a different meaning than the one the person chose;
* a SUPERSEDED adoption may not start a new build.

▲ HOW THE RACE IS EXPRESSED. The suite's connection never commits, so a second session could not see
its rows, and every table here is append-only — a committed fixture row could never be cleaned up.
Both halves of a race are therefore written on one connection: the WINNER's row (which is what the
loser collides with) and the LOSER's write, either as the raw insert a stale writer performs or,
through the store, with its head read stalled at the value it saw. The mechanism under test — the
partial-unique index that decides which write lands — is exactly the same one either way.
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.featuregen.overlay.upload.planner._binding_seeds import (
    execution_context,
    join_policy,
    logical_plan,
    physical_plan,
    render_profile,
    seed_draft,
    seed_option,
    seed_selection,
    seed_target_reading,
)

from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.planner import adoption_store
from featuregen.overlay.upload.planner.adoption_store import (
    ADOPTION_ID_PREFIX,
    AdoptionConflict,
    AdoptionDefect,
    adoption_chain,
    confirm_physical_plan_adoption,
    current_physical_plan_adoption,
    load_physical_plan_adoption,
)
from featuregen.overlay.upload.planner.binding_chain import (
    BindingChainDefect,
    bind_build_member_combined,
    bind_considered_option_plan,
    bind_formula_draft_plan,
    bind_selection_formula_plan,
)
from featuregen.overlay.upload.planner.identity_store import (
    IdentityPersistenceDefect,
    ensure_logical_feature_plan,
    ensure_physical_execution_plan,
    ensure_render_profile,
    logical_digest,
)
from featuregen.overlay.upload.planner.join_policy_store import (
    JOIN_VALIDATION_POLICY_ID_PREFIX,
    JoinPolicyPersistenceDefect,
    JoinPolicyStoreConflict,
    ensure_join_validation_policy,
    load_join_validation_policy,
    require_join_validation_policy,
)
from featuregen.overlay.upload.selection_formula_binding import record_selection_formula_binding

TABLES = ("join_validation_policy_revision", "selection_physical_plan_adoption_revision")


def _planned_selection(db, *, suffix="1", planned=True):
    """A selection whose option carries a logical plan binding — the shape an adoption needs."""
    considered, option = seed_option(db, considered=f"cr_{suffix}", option=f"opt_{suffix}",
                                     planned=planned)
    reading = seed_target_reading(db, revision_id=f"trr_{suffix}", intent_id=f"intent_{suffix}")
    plan = logical_plan(operation=f"sum_window_delta_{suffix}")
    plan_id = ensure_logical_feature_plan(db, plan=plan).revision_id
    if planned:
        bind_considered_option_plan(db, considered_revision_id=considered, option_id=option,
                                    logical_plan_revision_id=plan_id)
    selection = seed_selection(db, reading=reading, considered=considered, option=option,
                               revision_id=f"fsr_{suffix}")
    return {"selection": selection, "digest": logical_digest(plan), "considered": considered,
            "option": option, "reading": reading}


def _physical(db, *, digest, context, **over):
    policy = ensure_join_validation_policy(db, policy=join_policy(**over))
    return ensure_physical_execution_plan(
        db, plan=physical_plan(context_id=context, logical_digest_ref=digest, policy_id=policy))


@pytest.mark.parametrize("table", TABLES)
def test_the_1136_tables_exist(db, table) -> None:
    assert db.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0] is not None


# ── the guard policy: the pin B1 could only store ─────────────────────────────────────────────
def test_the_guard_policy_round_trips_and_is_one_row(db) -> None:
    policy = join_policy()
    revision_id = ensure_join_validation_policy(db, policy=policy)
    assert revision_id == policy.revision_id
    assert revision_id.startswith(JOIN_VALIDATION_POLICY_ID_PREFIX)
    assert load_join_validation_policy(db, revision_id) == policy
    assert ensure_join_validation_policy(db, policy=policy) == revision_id
    assert db.execute(
        "SELECT count(*) FROM join_validation_policy_revision").fetchone()[0] == 1


def test_the_same_policy_declared_by_someone_else_is_the_same_revision(db) -> None:
    """The content hash is SEMANTIC ONLY, so the declarer never forks a policy — and the first
    declaration's provenance is the one kept, because the insert does nothing on conflict."""
    first = ensure_join_validation_policy(db, policy=join_policy())
    second = ensure_join_validation_policy(
        db, policy=join_policy(declared_by="user:someone-else",
                               declared_at="2026-08-25T00:00:00Z"))
    assert first == second
    stored = db.execute(
        "SELECT declared_by FROM join_validation_policy_revision WHERE revision_id = %s",
        (first,)).fetchone()
    assert stored[0] == "user:ascoe"


def test_a_tampered_policy_row_refuses_to_load(db) -> None:
    forged = "5" * 64
    db.execute(
        "INSERT INTO join_validation_policy_revision (revision_id, content, content_hash, "
        "  declared_by, declared_at) VALUES (%s, %s, %s, %s, %s)",
        (JOIN_VALIDATION_POLICY_ID_PREFIX + forged,
         Jsonb(join_policy(minimum_coverage_ratio=0.5).content_payload()), forged,
         "user:ascoe", "2026-08-24T00:00:00Z"))
    with pytest.raises(JoinPolicyStoreConflict) as excinfo:
        load_join_validation_policy(db, JOIN_VALIDATION_POLICY_ID_PREFIX + forged)
    assert "content verification" in str(excinfo.value)


def test_requiring_an_undeclared_policy_refuses(db) -> None:
    with pytest.raises(JoinPolicyPersistenceDefect) as excinfo:
        require_join_validation_policy(db, JOIN_VALIDATION_POLICY_ID_PREFIX + "4" * 64)
    assert "never declared" in str(excinfo.value)


def test_a_physical_plan_may_no_longer_pin_a_policy_nobody_declared(db) -> None:
    """B1's honest unchecked pin, closed. The write path is B1's; the refusal is B2's."""
    context = execution_context(db)
    plan = logical_plan()
    ensure_logical_feature_plan(db, plan=plan)
    with pytest.raises(JoinPolicyPersistenceDefect) as excinfo:
        ensure_physical_execution_plan(
            db, plan=physical_plan(context_id=context, logical_digest_ref=logical_digest(plan),
                                   policy_id=JOIN_VALIDATION_POLICY_ID_PREFIX + "4" * 64))
    assert "never declared" in str(excinfo.value)
    assert db.execute(
        "SELECT count(*) FROM physical_execution_plan_revision").fetchone()[0] == 0


# ── the adoption chain ────────────────────────────────────────────────────────────────────────
def test_the_first_confirmation_is_the_root_and_the_head(db) -> None:
    s = _planned_selection(db)
    context = execution_context(db)
    physical = _physical(db, digest=s["digest"], context=context)
    adoption, created = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=physical, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    assert created is True
    assert adoption.supersedes_adoption_revision_id is None
    assert adoption.adoption_revision_id.startswith(ADOPTION_ID_PREFIX)
    assert load_physical_plan_adoption(db, adoption.adoption_revision_id) == adoption
    assert current_physical_plan_adoption(
        db, selection_revision_id=s["selection"],
        execution_context_revision_id=context) == adoption


def test_re_confirming_the_same_plan_is_the_same_revision_and_one_row(db) -> None:
    """R3's semantic-only hash, made observable: the actor and the clock differ and nothing moves."""
    s = _planned_selection(db)
    context = execution_context(db)
    physical = _physical(db, digest=s["digest"], context=context)
    first, created_first = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=physical, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    again, created_again = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=physical, confirmed_by="user:someone-else",
        confirmed_at="2026-08-24T18:00:00Z")
    assert created_first is True
    assert created_again is False
    assert again.adoption_revision_id == first.adoption_revision_id
    assert again.confirmed_by == "user:ascoe"
    assert db.execute(
        "SELECT count(*) FROM selection_physical_plan_adoption_revision").fetchone()[0] == 1


def test_adopting_a_second_plan_supersedes_the_first(db) -> None:
    s = _planned_selection(db)
    context = execution_context(db)
    first_plan = _physical(db, digest=s["digest"], context=context)
    second_plan = _physical(db, digest=s["digest"], context=context,
                            minimum_coverage_ratio=0.75)
    assert first_plan != second_plan
    root, _ = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=first_plan, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    successor, created = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=second_plan, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T10:00:00Z")
    assert created is True
    assert successor.supersedes_adoption_revision_id == root.adoption_revision_id
    assert current_physical_plan_adoption(
        db, selection_revision_id=s["selection"],
        execution_context_revision_id=context) == successor
    assert adoption_chain(db, selection_revision_id=s["selection"],
                          execution_context_revision_id=context) == (root, successor)


# ── R3: ENVIRONMENT SCOPE ─────────────────────────────────────────────────────────────────────
def test_a_sandbox_and_a_production_adoption_coexist_on_one_selection(db) -> None:
    """Two chains, two roots, two heads, neither aware of the other. Adopting in sandbox does not
    touch what production is generating, and neither has to be superseded to let the other exist."""
    s = _planned_selection(db)
    sandbox = execution_context(db, environment_id="env-uat", tier=ExecutionTier.SANDBOX)
    production = execution_context(db, environment_id="env-prod", tier=ExecutionTier.PRODUCTION)
    assert sandbox != production

    sandbox_plan = _physical(db, digest=s["digest"], context=sandbox)
    production_plan = _physical(db, digest=s["digest"], context=production)
    sandbox_adoption, _ = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=sandbox,
        physical_plan_revision_id=sandbox_plan, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    production_adoption, created = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=production,
        physical_plan_revision_id=production_plan, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:05:00Z")

    assert created is True
    # BOTH are roots — the second did not supersede the first.
    assert sandbox_adoption.supersedes_adoption_revision_id is None
    assert production_adoption.supersedes_adoption_revision_id is None
    assert current_physical_plan_adoption(
        db, selection_revision_id=s["selection"],
        execution_context_revision_id=sandbox) == sandbox_adoption
    assert current_physical_plan_adoption(
        db, selection_revision_id=s["selection"],
        execution_context_revision_id=production) == production_adoption


# ── R3: the CAS ───────────────────────────────────────────────────────────────────────────────
def test_a_second_root_for_one_scope_is_unrepresentable(db) -> None:
    """The partial-unique ROOT index: two first-ever confirmations, one winner."""
    s = _planned_selection(db)
    context = execution_context(db)
    physical = _physical(db, digest=s["digest"], context=context)
    other = _physical(db, digest=s["digest"], context=context, minimum_coverage_ratio=0.75)
    confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=physical, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO selection_physical_plan_adoption_revision (adoption_revision_id, "
            "  selection_revision_id, execution_context_revision_id, physical_plan_revision_id, "
            "  supersedes_adoption_revision_id, confirmed_by, confirmed_at, content_hash) "
            "VALUES (%s, %s, %s, %s, NULL, %s, %s, %s)",
            (ADOPTION_ID_PREFIX + "a" * 64, s["selection"], context, other, "user:racer",
             "2026-08-24T09:00:00Z", "a" * 64))


def test_a_second_successor_of_one_predecessor_is_unrepresentable(db) -> None:
    """The partial-unique SUCCESSOR index: the chain never forks, so "which adoption is current"
    never depends on which fork a reader walked."""
    s = _planned_selection(db)
    context = execution_context(db)
    first_plan = _physical(db, digest=s["digest"], context=context)
    second_plan = _physical(db, digest=s["digest"], context=context, minimum_coverage_ratio=0.75)
    root, _ = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=first_plan, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=second_plan, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T10:00:00Z")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO selection_physical_plan_adoption_revision (adoption_revision_id, "
            "  selection_revision_id, execution_context_revision_id, physical_plan_revision_id, "
            "  supersedes_adoption_revision_id, confirmed_by, confirmed_at, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (ADOPTION_ID_PREFIX + "b" * 64, s["selection"], context, first_plan,
             root.adoption_revision_id, "user:racer", "2026-08-24T10:00:01Z", "b" * 64))


def test_a_confirmation_whose_head_read_went_stale_loses_and_says_so(db, monkeypatch) -> None:
    """The same race, through the STORE — the shape a second session actually takes.

    A racer is a writer whose head read was true when it was made and false by the time it wrote.
    That is exactly what the stalled reader below produces: the store computes its successor from
    the head it saw, the successor index refuses the write because the winner already holds that
    slot, and the read-back finds nothing — which the store reports as AdoptionConflict rather than
    as a silent no-op or an overwrite."""
    s = _planned_selection(db)
    context = execution_context(db)
    first_plan = _physical(db, digest=s["digest"], context=context)
    second_plan = _physical(db, digest=s["digest"], context=context, minimum_coverage_ratio=0.75)
    third_plan = _physical(db, digest=s["digest"], context=context, minimum_coverage_ratio=0.5)
    root, _ = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=first_plan, confirmed_by="user:winner",
        confirmed_at="2026-08-24T09:00:00Z")
    winner, _ = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=second_plan, confirmed_by="user:winner",
        confirmed_at="2026-08-24T10:00:00Z")

    monkeypatch.setattr(adoption_store, "current_physical_plan_adoption",
                        lambda *args, **kwargs: root)
    with pytest.raises(AdoptionConflict) as excinfo:
        confirm_physical_plan_adoption(
            db, selection_revision_id=s["selection"], execution_context_revision_id=context,
            physical_plan_revision_id=third_plan, confirmed_by="user:loser",
            confirmed_at="2026-08-24T10:00:01Z")
    assert "moved the adoption chain" in str(excinfo.value)
    # The winner is untouched, and the loser wrote nothing.
    assert db.execute(
        "SELECT count(*) FROM selection_physical_plan_adoption_revision").fetchone()[0] == 2
    monkeypatch.undo()
    assert current_physical_plan_adoption(
        db, selection_revision_id=s["selection"],
        execution_context_revision_id=context) == winner


# ── what an adoption must be able to prove ────────────────────────────────────────────────────
def test_adopting_a_plan_built_for_another_context_refuses(db) -> None:
    s = _planned_selection(db)
    sandbox = execution_context(db, environment_id="env-uat")
    production = execution_context(db, environment_id="env-prod",
                                   tier=ExecutionTier.PRODUCTION)
    sandbox_plan = _physical(db, digest=s["digest"], context=sandbox)
    with pytest.raises(AdoptionDefect) as excinfo:
        confirm_physical_plan_adoption(
            db, selection_revision_id=s["selection"],
            execution_context_revision_id=production, physical_plan_revision_id=sandbox_plan,
            confirmed_by="user:ascoe", confirmed_at="2026-08-24T09:00:00Z")
    assert "cannot be adopted into" in str(excinfo.value)


def test_adopting_for_a_pre_plan_option_refuses(db) -> None:
    s = _planned_selection(db, planned=False)
    context = execution_context(db)
    physical = _physical(db, digest=s["digest"], context=context)
    with pytest.raises(AdoptionDefect) as excinfo:
        confirm_physical_plan_adoption(
            db, selection_revision_id=s["selection"], execution_context_revision_id=context,
            physical_plan_revision_id=physical, confirmed_by="user:ascoe",
            confirmed_at="2026-08-24T09:00:00Z")
    assert "PRE-PLAN option" in str(excinfo.value)


def test_adopting_a_plan_for_another_meaning_refuses(db) -> None:
    s = _planned_selection(db, suffix="1")
    other = logical_plan(operation="mean_window_delta")
    ensure_logical_feature_plan(db, plan=other)
    context = execution_context(db)
    physical = _physical(db, digest=logical_digest(other), context=context)
    with pytest.raises(AdoptionDefect) as excinfo:
        confirm_physical_plan_adoption(
            db, selection_revision_id=s["selection"], execution_context_revision_id=context,
            physical_plan_revision_id=physical, confirmed_by="user:ascoe",
            confirmed_at="2026-08-24T09:00:00Z")
    assert "re-aim what a person chose" in str(excinfo.value)


def test_adopting_for_a_selection_nobody_made_refuses(db) -> None:
    context = execution_context(db)
    with pytest.raises(AdoptionDefect) as excinfo:
        confirm_physical_plan_adoption(
            db, selection_revision_id="fsr_missing", execution_context_revision_id=context,
            physical_plan_revision_id="pxp_" + "4" * 64, confirmed_by="user:ascoe",
            confirmed_at="2026-08-24T09:00:00Z")
    assert "does not exist" in str(excinfo.value)


def test_adopting_a_plan_nobody_persisted_refuses(db) -> None:
    s = _planned_selection(db)
    context = execution_context(db)
    with pytest.raises(AdoptionDefect) as excinfo:
        confirm_physical_plan_adoption(
            db, selection_revision_id=s["selection"], execution_context_revision_id=context,
            physical_plan_revision_id="pxp_" + "4" * 64, confirmed_by="user:ascoe",
            confirmed_at="2026-08-24T09:00:00Z")
    assert "never persisted" in str(excinfo.value)


def test_a_tampered_adoption_row_refuses_to_load(db) -> None:
    s = _planned_selection(db)
    context = execution_context(db)
    physical = _physical(db, digest=s["digest"], context=context)
    forged = "7" * 64
    db.execute(
        "INSERT INTO selection_physical_plan_adoption_revision (adoption_revision_id, "
        "  selection_revision_id, execution_context_revision_id, physical_plan_revision_id, "
        "  supersedes_adoption_revision_id, confirmed_by, confirmed_at, content_hash) "
        "VALUES (%s, %s, %s, %s, NULL, %s, %s, %s)",
        (ADOPTION_ID_PREFIX + forged, s["selection"], context, physical, "user:ascoe",
         "2026-08-24T09:00:00Z", forged))
    with pytest.raises(AdoptionConflict) as excinfo:
        load_physical_plan_adoption(db, ADOPTION_ID_PREFIX + forged)
    assert "does not reproduce its own identity" in str(excinfo.value)


# ── a superseded adoption may not start a new build ───────────────────────────────────────────
def test_a_superseded_adoption_cannot_be_bound_into_a_build(db) -> None:
    s = _planned_selection(db)
    draft = seed_draft(db, considered=s["considered"], option=s["option"])
    bind_formula_draft_plan(db, formula_draft_id=draft)
    record_selection_formula_binding(db, selection_revision_id=s["selection"],
                                     formula_draft_id=draft)
    bind_selection_formula_plan(db, selection_revision_id=s["selection"], formula_draft_id=draft)

    context = execution_context(db)
    first_plan = _physical(db, digest=s["digest"], context=context)
    second_plan = _physical(db, digest=s["digest"], context=context, minimum_coverage_ratio=0.75)
    root, _ = confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=first_plan, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=second_plan, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T10:00:00Z")
    profile = ensure_render_profile(db, profile=render_profile())
    with pytest.raises(BindingChainDefect) as excinfo:
        bind_build_member_combined(
            db, selection_revision_id=s["selection"], formula_draft_id=draft,
            physical_adoption_revision_id=root.adoption_revision_id,
            render_profile_revision_id=profile)
    assert "has been superseded" in str(excinfo.value)


# ── append-only, both tables ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("statement", ["UPDATE {t} SET recorded_at = now()", "DELETE FROM {t}",
                                       "TRUNCATE TABLE {t}"])
def test_the_1136_tables_are_append_only(db, table, statement) -> None:
    s = _planned_selection(db)
    context = execution_context(db)
    physical = _physical(db, digest=s["digest"], context=context)
    confirm_physical_plan_adoption(
        db, selection_revision_id=s["selection"], execution_context_revision_id=context,
        physical_plan_revision_id=physical, confirmed_by="user:ascoe",
        confirmed_at="2026-08-24T09:00:00Z")
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        db.execute(statement.format(t=table))
    assert "append-only" in str(excinfo.value)


def test_the_physical_plan_pin_is_a_typed_refusal_before_any_sql(db) -> None:
    """Store discipline: a malformed pin never reaches the database."""
    s = _planned_selection(db)
    context = execution_context(db)
    with pytest.raises(AdoptionDefect) as excinfo:
        confirm_physical_plan_adoption(
            db, selection_revision_id=s["selection"], execution_context_revision_id=context,
            physical_plan_revision_id="not-a-plan-id", confirmed_by="user:ascoe",
            confirmed_at="2026-08-24T09:00:00Z")
    assert "R3's POST names the physical plan revision itself" in str(excinfo.value)


def test_the_identity_store_still_refuses_an_unpersisted_logical_plan(db) -> None:
    """A regression guard on the wiring: adding the policy check did not displace B1's own."""
    context = execution_context(db)
    policy = ensure_join_validation_policy(db, policy=join_policy())
    with pytest.raises(IdentityPersistenceDefect) as excinfo:
        ensure_physical_execution_plan(
            db, plan=physical_plan(context_id=context, logical_digest_ref="9" * 64,
                                   policy_id=policy))
    assert "was never persisted" in str(excinfo.value)
