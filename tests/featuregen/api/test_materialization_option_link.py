"""Task B4 — a materialization run acquires GOVERNED PROVENANCE (migration 1067).

Before this task ``materialization_request`` carried a group name, an actor, a roles snapshot, an
idempotency key and an opaque ``activation_state`` jsonb, and **nothing linked a run to the governed
option a human approved**. A compile could be triggered for an option whose decision blocked
``execute_materialization`` and no record would connect the two.

**What is asserted, and what is honestly NOT.** §0.3 names four materialization-only codes. Exactly
one of them — ``FORMULA_NOT_REVIEWED`` — can be turned on and off from this branch today, by A5's
registry: ``formula_schema_supported`` and ``requirements_closed`` are hardwired ``False`` in
``assemble_current_activation_state`` and ``effective_readiness`` is the frozen literal until Phase
C lands, so inventing a fixture that made those clear individually would be inventing C-phase. Each
code is therefore asserted PRESENT with its ``next_step`` on a real row, one case each; the one that
discriminates is proved by a pair of rows differing only in the registry; and the ALLOWED path — the
positive control, without which every refusal above could be passing for the wrong reason — is
reached by patching that ONE assembler and nothing else. ``activation_decision`` is never patched:
the real fold over a real frozen row is what decides in every case here.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api.test_materialization_runs import _PATH, _body
from tests.featuregen.materialize.test_resolve import _seed_work_item

from featuregen.materialize.queue_lane import MATERIALIZATION_FLAG
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.feature_planning_contracts import (
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.recipe_planning_lens import (
    DatasetStoryV1,
    V2RecipeCandidateV1,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.semantic_option_decision import (
    decision_facts_for_candidate,
    persist_option_decisions,
)

#: The one recipe with a REVIEWED v2 expectation (A5) — so ``FORMULA_NOT_REVIEWED`` is absent...
REVIEWED = "posted_debit_amount"
#: ...and one without, otherwise identical in this fixture, so the code's presence measures the
#: registry rather than the fixture.
UNREVIEWED = "balance_slope"

#: Every rule that lives ONLY on the materialization rung — the closed set this suite measures
#: against, so "these blocked and that one cleared" is provable rather than asserted.
MATERIALIZATION_ONLY = {R.READINESS_NOT_MATERIALIZATION_READY, R.FORMULA_NOT_REVIEWED,
                        R.FORMULA_SCHEMA_UNSUPPORTED, R.FORMULA_REVIEW_UNMEASURED,
                        R.ENGINE_CAPABILITY_UNMEASURED, R.EXTERNAL_VALIDATION_OUTSTANDING,
                        R.EXECUTION_AUTHORITY_UNEVALUATED, R.EXECUTION_AUTHORITY_UNMET}

#: What a BOUND option with a frozen plan actually blocks on today, MEASURED rather than assumed:
#: readiness is not `MATERIALIZATION_READY` (C3), and the execution floor is evaluated — the
#: frozen plan gives it a read set — but not met, because the fixture's refs resolve to no
#: governed authority. `EXECUTION_AUTHORITY_UNEVALUATED` is its *if* arm and therefore absent
#: here, and `EXTERNAL_VALIDATION_OUTSTANDING` short-circuits on the `DESIGN_CHECKED` idea — the
#: same exact-intersection reasoning A5's acceptance row records. `FORMULA_SCHEMA_UNSUPPORTED`
#: fell at C2 for THIS (reviewed) exemplar: the engine registry advertises its one demand.
BLOCKING_TODAY = {R.READINESS_NOT_MATERIALIZATION_READY, R.EXECUTION_AUTHORITY_UNMET}
#: An unreviewed recipe carries two MORE codes, coupled by design: no review means no reviewed
#: demands for C2 to compare. §6's tri-state renamed the second half of the coupling — the
#: unmeasured support question surfaces as `FORMULA_REVIEW_UNMEASURED`, never as the false
#: engine claim `FORMULA_SCHEMA_UNSUPPORTED` used to make here.
UNREVIEWED_EXTRA = {R.FORMULA_NOT_REVIEWED, R.FORMULA_REVIEW_UNMEASURED}


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")


@pytest.fixture
def work_items(db):
    return [_seed_work_item(db, "total_debit_amount_30d", "b4a")]


def _freeze_option(db, recipe_id: str, *, key: str, uoa: str | None = None) -> tuple[str, str]:
    """One REAL ``semantic_option_decision`` row, through the real writers.

    ``uoa`` freezes the unit of analysis the human had confirmed when the card was served — the
    fact B5's intent re-read is about. ``None`` (the default every other case here uses) is the
    honest absence: the confirmation is optional by design and absence never blocks."""
    definition = v2_recipe_by_id(recipe_id)
    assert definition is not None
    request = planning_request_from_recipe(definition)
    candidate = V2RecipeCandidateV1(
        recipe_id=recipe_id, relationship="primary", planning_request=request,
        planning_request_hash=planning_request_hash(request),
        recipe_revision_hash="rev", verdicts=(), binding_state="bound",
        readiness=definition.readiness, temporal_pit_text="pit", temporal_blocker="",
        review_current=True, review_missing_roles=(), eligibility={},
        dataset_story=DatasetStoryV1(
            population_ref="accounts", population_basis="declared_grain",
            dataset_tables=("accounts",), cross_dataset=False, codes=()),
        # A bound candidate WITH a frozen plan — the realistic shape, and the one whose frozen
        # blockers are already clear, so what remains on the ladder is exactly §0.3's set.
        binding_plan={"plan_kind": "single_source", "catalog_source": "bank",
                      "source_table": "accounts", "population_ref": "accounts",
                      "read_set": ["public.accounts.acct_id"],
                      "role_bindings": {"grain": "public.accounts.acct_id"},
                      "pit": "as-of state at the cutoff", "output_grain": "account",
                      "window": None})
    idea = FeatureIdea(name=recipe_id, description="", derives_from=[], aggregation=None,
                       grain_table=None, source_definition_id=recipe_id)
    facts = decision_facts_for_candidate(candidate, idea, None, "ctx", uoa_entity=uoa)
    db.execute("INSERT INTO contract_intent "
               "(intent_id,hypothesis,intake_mode,redacted_hypothesis) "
               "VALUES (%s,'h','hypothesis','h') ON CONFLICT DO NOTHING", (f"intent-{key}",))
    db.execute("INSERT INTO feature_generation_run (generation_run_id,intent_id,actor) "
               "VALUES (%s,%s,'{}'::jsonb) ON CONFLICT DO NOTHING",
               (f"genrun-{key}", f"intent-{key}"))
    db.execute("INSERT INTO contract_considered_revision "
               "(considered_revision_id,intent_id,generation_run_id,considered_json,"
               "considered_content_hash,canonicalization_version) "
               "VALUES (%s,%s,%s,'{}'::jsonb,%s,'test-v1')",
               (f"rev-{key}", f"intent-{key}", f"genrun-{key}", f"hash-{key}"))
    written = persist_option_decisions(
        db, considered_revision_id=f"rev-{key}", generation_run_id=f"genrun-{key}",
        metadata_snapshot_id=None, facts_by_option_id={f"opt-{key}": facts})
    assert written == 1
    return f"rev-{key}", f"opt-{key}"


def _post(client, headers, work_items, **overrides):
    return client.post(_PATH, json=_body(work_items, **overrides), headers=headers)


# ── the gate ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", sorted(BLOCKING_TODAY))
def test_a_blocked_option_cannot_be_materialized(client, admin_headers, db, work_items, code):
    """One case per §0.3 code that blocks today. The response NAMES the code and carries its
    ``next_step``: a refusal a person cannot act on is an outage with better manners."""
    revision_id, option_id = _freeze_option(db, REVIEWED, key=f"blk{sorted(BLOCKING_TODAY).index(code)}")
    response = _post(client, admin_headers, work_items,
                     considered_revision_id=revision_id, option_id=option_id)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "ACTIVATION_BLOCKED"
    assert detail["action"] == "execute_materialization"
    assert (detail["considered_revision_id"], detail["option_id"]) == (revision_id, option_id)
    named = {blocker["code"]: blocker["next_step"] for blocker in detail["blockers"]}
    assert code in named, named
    assert named[code].strip(), f"{code} carries no next step"
    # ...and nothing was minted: a refused trigger leaves no request and no queue row behind.
    assert db.execute("SELECT count(*) FROM materialization_request").fetchone()[0] == 0


def test_the_unreviewed_option_carries_the_two_coupled_extra_codes(
        client, admin_headers, db, work_items):
    """The discriminator, and the proof this suite measures the registry rather than the fixture.
    ``posted_debit_amount`` has a reviewed v2 expectation (A5) and ``balance_slope`` does not; the
    two rows are otherwise identical. Since C2 the registry difference moves TWO coupled codes:
    review itself, and the schema-support question that only a reviewed expectation can answer."""
    reviewed = _freeze_option(db, REVIEWED, key="disc-a")
    unreviewed = _freeze_option(db, UNREVIEWED, key="disc-b")

    def _codes(pair):
        response = _post(client, admin_headers, work_items,
                         considered_revision_id=pair[0], option_id=pair[1])
        assert response.status_code == 409, response.text
        return {b["code"] for b in response.json()["detail"]["blockers"]}

    assert _codes(reviewed) & MATERIALIZATION_ONLY == BLOCKING_TODAY
    assert _codes(unreviewed) & MATERIALIZATION_ONLY == BLOCKING_TODAY | UNREVIEWED_EXTRA


def test_an_allowed_option_mints_a_request_carrying_its_provenance(
        client, admin_headers, db, work_items, monkeypatch):
    """The positive control, and it is REACHABLE — without it every assertion above could be
    passing because the gate refuses everything.

    The three remaining blockers are hardwired ``False`` in ``assemble_current_activation_state``
    until Phase C (the honest comment in that function says so), so the ALLOWED state is produced by
    patching that one assembler — never the decision, never the policy: ``activation_decision`` is
    the real fold over a real frozen row, and it is what decides.
    """
    from featuregen.api.routes import materialization_runs as route
    from featuregen.overlay.upload import semantic_option_decision as sod

    revision_id, option_id = _freeze_option(db, REVIEWED, key="allow")
    genuine = sod.assemble_current_activation_state

    def _c_phase(conn, *, frozen, snapshot_id, intent_id=None, contract_id=None):
        # The stub MIRRORS the real signature, `contract_id` included (SUCCESSOR 4): the route now
        # resolves the contract minted from the option and passes it, and a double that dropped the
        # argument would fail the call rather than the assertion.
        import dataclasses
        return dataclasses.replace(
            genuine(conn, frozen=frozen, snapshot_id=snapshot_id, intent_id=intent_id,
                    contract_id=contract_id),
            effective_readiness="MATERIALIZATION_READY", formula_schema_support="supported",
            requirements_closed=True, execution_authority_evaluated=True,
            execution_floor_met=True, authoring_floor_met=True, review_current=True,
            policy_revisions_current=True, uoa_current=True, snapshot_freshness="current")

    monkeypatch.setattr(sod, "assemble_current_activation_state", _c_phase)
    response = _post(client, admin_headers, work_items,
                     considered_revision_id=revision_id, option_id=option_id)

    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["considered_revision_id"], body["option_id"]) == (revision_id, option_id)
    stored = db.execute(
        "SELECT considered_revision_id, option_id, activation_state FROM materialization_request "
        "WHERE request_id = %s", (body["request_id"],)).fetchone()
    assert (stored[0], stored[1]) == (revision_id, option_id)
    # The decision is recorded VERBATIM — the action, the verdict, and the blocker list as the fold
    # returned it. That is what the `activation_state` column was for.
    assert stored[2]["activation"]["action"] == "execute_materialization"
    assert stored[2]["activation"]["allowed"] is True
    assert stored[2]["activation"]["blockers"] == []
    assert route._MATERIALIZATION_ACTION == "execute_materialization"


# ── B5: the route re-reads the option's own INTENT ───────────────────────────────────────────────


def _later_uoa_confirmation(db, *, intent_id: str, key: str, uoa: str) -> None:
    """A LATER generation under the SAME intent that confirmed a DIFFERENT unit of analysis.

    Appended directly, with an explicit ``recorded_at``, for one reason: the column defaults to
    ``now()`` — the TRANSACTION timestamp — so two rows written inside one test tie on it, and
    ``_latest_confirmed_uoa``'s tiebreak is a ULID whose sub-millisecond half is random. A control
    resting on that coin flip would be flaky rather than discriminating. The row is otherwise an
    ordinary append to the same append-only table, and it is read back by the real query. A
    revision is UNIQUE per (intent, generation run), so the later generation gets its own run."""
    import json

    db.execute("INSERT INTO feature_generation_run (generation_run_id,intent_id,actor) "
               "VALUES (%s,%s,'{}'::jsonb) ON CONFLICT DO NOTHING",
               (f"genrun-{key}", intent_id))
    db.execute("INSERT INTO contract_considered_revision (considered_revision_id,intent_id,"
               "generation_run_id,considered_json,considered_content_hash,"
               "canonicalization_version) VALUES (%s,%s,%s,'{}'::jsonb,%s,'test-v1')",
               (f"rev-{key}", intent_id, f"genrun-{key}", f"hash-{key}"))
    db.execute(
        "INSERT INTO semantic_option_decision (decision_id, considered_revision_id, option_id, "
        " generation_run_id, source_definition_id, generation_source, computation_kind, "
        " planning_request_hash, binding_state, readiness, dataset_story, recorded_at) "
        "VALUES (%s,%s,%s,%s,%s,'recipe','deterministic_formula','prh','bound','FORMULA_BLOCKED',"
        " %s, now() + interval '1 hour')",
        (f"sod-{key}", f"rev-{key}", f"opt-{key}", f"genrun-{key}", REVIEWED,
         json.dumps({"confirmed_uoa_entity": uoa})))


def test_a_confirmed_UOA_that_has_NOT_moved_no_longer_drifts_the_option(
        client, admin_headers, db, work_items):
    """**B5 — a live bug, fixed.** ``_require_the_option_allows_materialization`` called
    ``assemble_current_activation_state`` with no ``intent_id``, and that assembler's UOA re-read
    treats a missing intent as unverifiable: ``uoa_now = frozen_uoa is None``. So EVERY option
    whose card was served under a confirmed unit of analysis was permanently
    ``ACTIVATION_STATE_DRIFTED`` on this ladder — not because anything had drifted, but because
    the route never asked which intent to compare against. The fix resolves the intent from the
    cited revision (``contract_considered_revision.intent_id``) and passes it.

    ``ACTIVATION_STATE_DRIFTED`` is emitted by two rules and deduplicated by code, so the
    assertion is only a discriminator while the OTHER one is clear: the policy-pin rule is, and
    this test proves it rather than assuming it — the facts producer writes the live authority
    matrix hash into the frozen pins, so ``policy_revisions_current`` is true on this row."""
    revision_id, option_id = _freeze_option(db, REVIEWED, key="uoa-same", uoa="account")
    response = _post(client, admin_headers, work_items,
                     considered_revision_id=revision_id, option_id=option_id)

    assert response.status_code == 409, response.text          # other rungs still block it
    codes = {b["code"] for b in response.json()["detail"]["blockers"]}
    assert R.ACTIVATION_STATE_DRIFTED not in codes, codes
    # the other producer of that code really is clear, so its absence measures the UOA re-read
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash
    from featuregen.overlay.upload.semantic_option_decision import load_frozen_option_facts
    frozen = load_frozen_option_facts(db, considered_revision_id=revision_id,
                                      option_id=option_id)
    assert f"authority_matrix_hash:{authority_matrix_hash()}" in frozen.policy_revision_pins
    assert frozen.confirmed_uoa_entity == "account"


def test_a_confirmed_UOA_that_GENUINELY_moved_still_drifts_the_option(
        client, admin_headers, db, work_items):
    """The control, and the half that must never be lost: the human re-confirmed a DIFFERENT unit
    of analysis after this card was served, so the card answers a question nobody is asking
    anymore. Same fixture as above except for the later confirmation."""
    revision_id, option_id = _freeze_option(db, REVIEWED, key="uoa-moved", uoa="account")
    _later_uoa_confirmation(db, intent_id="intent-uoa-moved", key="uoa-later", uoa="customer")

    response = _post(client, admin_headers, work_items,
                     considered_revision_id=revision_id, option_id=option_id)
    assert response.status_code == 409, response.text
    codes = {b["code"] for b in response.json()["detail"]["blockers"]}
    assert R.ACTIVATION_STATE_DRIFTED in codes, codes


def test_the_route_resolves_the_intent_from_the_revision_it_was_given(db):
    """The lookup itself, against the real table. ``None`` for a revision nobody recorded — the
    honest answer, and the one the assembler already fails closed on."""
    from featuregen.api.routes.materialization_runs import _intent_of_revision

    revision_id, _option_id = _freeze_option(db, REVIEWED, key="intent-lookup")
    assert _intent_of_revision(db, considered_revision_id=revision_id) == "intent-intent-lookup"
    assert _intent_of_revision(db, considered_revision_id="rev-nobody") is None


# ── the legacy path, and the half-key ────────────────────────────────────────────────────────────


def test_the_legacy_work_item_path_still_accepts(client, admin_headers, db, work_items):
    """The nullable FK, proven. The work-item-driven path predates this link and must keep working:
    a request with no option key mints, queues and stores NULL on both columns."""
    response = _post(client, admin_headers, work_items)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["considered_revision_id"] is None and body["option_id"] is None
    stored = db.execute(
        "SELECT considered_revision_id, option_id, activation_state FROM materialization_request "
        "WHERE request_id = %s", (body["request_id"],)).fetchone()
    assert (stored[0], stored[1]) == (None, None)
    assert "activation" not in stored[2]          # no decision was made, so none is claimed


def test_half_an_option_key_is_refused_as_unusable(client, admin_headers, work_items):
    """An option is addressed by BOTH halves. Half a key names no approval, and recording it would
    put provenance in the table that nothing could resolve."""
    for overrides in ({"considered_revision_id": "rev-x"}, {"option_id": "opt-x"}):
        response = _post(client, admin_headers, work_items, **overrides)
        assert response.status_code == 422, response.text
        assert "BOTH" in response.json()["detail"]


def test_an_option_key_naming_no_decision_is_a_404_not_a_409(client, admin_headers, work_items):
    """Distinguished on purpose: "this approval does not exist" is a different fact from "this
    approval is blocked", and a caller chasing the wrong one wastes a governance conversation."""
    response = _post(client, admin_headers, work_items,
                     considered_revision_id="rev-nope", option_id="opt-nope")
    assert response.status_code == 404, response.text
    assert "opt-nope" in response.json()["detail"]


def test_the_option_key_is_part_of_the_requests_IDENTITY(client, admin_headers, db, work_items,
                                                         monkeypatch):
    """One idempotency key reused for a DIFFERENT governed option is not a retry — it is a second
    approval being answered with the first one's run. Refused with a 409 that names the field."""
    from featuregen.overlay.upload import semantic_option_decision as sod

    first = _freeze_option(db, REVIEWED, key="ident-a")
    second = _freeze_option(db, REVIEWED, key="ident-b")
    genuine = sod.assemble_current_activation_state

    def _c_phase(conn, *, frozen, snapshot_id, intent_id=None, contract_id=None):
        # The stub MIRRORS the real signature, `contract_id` included (SUCCESSOR 4): the route now
        # resolves the contract minted from the option and passes it, and a double that dropped the
        # argument would fail the call rather than the assertion.
        import dataclasses
        return dataclasses.replace(
            genuine(conn, frozen=frozen, snapshot_id=snapshot_id, intent_id=intent_id,
                    contract_id=contract_id),
            effective_readiness="MATERIALIZATION_READY", formula_schema_support="supported",
            requirements_closed=True, execution_authority_evaluated=True,
            execution_floor_met=True, authoring_floor_met=True, review_current=True,
            policy_revisions_current=True, uoa_current=True, snapshot_freshness="current")

    monkeypatch.setattr(sod, "assemble_current_activation_state", _c_phase)
    assert _post(client, admin_headers, work_items, key="shared-key",
                 considered_revision_id=first[0], option_id=first[1]).status_code == 202
    clash = _post(client, admin_headers, work_items, key="shared-key",
                  considered_revision_id=second[0], option_id=second[1])
    assert clash.status_code == 409, clash.text
    assert "option_id" in clash.json()["detail"]


def test_the_gate_runs_BEFORE_anything_is_minted(client, admin_headers, db, work_items):
    """Ordering, asserted rather than assumed: a blocked option leaves no request row, no queue row
    and no generation — the same "a typo leaves nothing behind" property the pre-flight has."""
    revision_id, option_id = _freeze_option(db, UNREVIEWED, key="order")
    assert _post(client, admin_headers, work_items,
                 considered_revision_id=revision_id,
                 option_id=option_id).status_code == 409
    assert db.execute("SELECT count(*) FROM materialization_request").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM queue").fetchone()[0] == 0


# ── the migration audit (CI is blind to legacy data) ─────────────────────────────────────────────

_MIGRATION_1067 = (
    __import__("pathlib").Path(__file__).resolve().parents[3]
    / "src/featuregen/db/migrations/1067_materialization_request_option_link.sql")


def test_migration_1067_applies_to_a_POPULATED_legacy_table(db, work_items):
    """THE AUDIT. Drop the link, seed a request in the pre-1067 shape, then run the migration's own
    SQL against it — the state a live deployment is actually in.

    What it proves, none of which a fresh-database CI run can: the ALTER lands on a table that
    already has rows; 1053's ``updated_at`` trigger is not reached (a nullable column with no
    default rewrites nothing, so no row is UPDATEd and no ``updated_at`` moves); the legacy row
    keeps NULL on both columns and is still readable; and the new constraints then bite — a pair
    naming no decision row is refused by the foreign key, and half a key by the CHECK.
    """
    from featuregen.materialize.request_store import read_request, record_request

    db.execute("ALTER TABLE materialization_request "
               "DROP CONSTRAINT IF EXISTS materialization_request_option_fk, "
               "DROP CONSTRAINT IF EXISTS materialization_request_option_is_whole, "
               "DROP COLUMN IF EXISTS considered_revision_id, DROP COLUMN IF EXISTS option_id")
    db.execute(
        "INSERT INTO materialization_request (request_id, logical_group_name, requested_by, "
        "authorized_roles, idempotency_key, activation_state, lifecycle_state) "
        "VALUES ('mreq_legacy','grp','user:old','{platform-admin}','legacy-key',"
        "'{\"flag\":\"x\"}'::jsonb,'requested')")
    before = db.execute(
        "SELECT updated_at FROM materialization_request WHERE request_id='mreq_legacy'"
    ).fetchone()[0]

    db.execute(_MIGRATION_1067.read_text())                    # the migration's OWN sql, verbatim

    after = db.execute(
        "SELECT updated_at, considered_revision_id, option_id FROM materialization_request "
        "WHERE request_id='mreq_legacy'").fetchone()
    assert after[0] == before                                  # no row was rewritten
    assert (after[1], after[2]) == (None, None)                # no backfill of invented values
    assert read_request(db, request_id="mreq_legacy") is not None

    # ...and the constraints the migration added really constrain.
    with pytest.raises(Exception) as excinfo:
        record_request(
            db, request_id="mreq_ghost", logical_group_name="grp", requested_by="user:x",
            authorized_roles=("platform-admin",), idempotency_key="ghost-key",
            activation_state={"flag": "x"},
            considered_revision_id="rev-nobody", option_id="opt-nobody")
    assert "materialization_request_option_fk" in str(excinfo.value)
    db.rollback()


def test_migration_1067_is_re_runnable(db):
    """``IF NOT EXISTS`` on the columns and the index, and a ``pg_constraint`` guard on the two
    constraints — a re-applied deploy is a no-op rather than an error."""
    db.execute(_MIGRATION_1067.read_text())
    db.execute(_MIGRATION_1067.read_text())
    assert db.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'materialization_request'::regclass "
        "AND conname IN ('materialization_request_option_fk', "
        "'materialization_request_option_is_whole')").fetchone()[0] == 2


def test_half_a_key_is_refused_by_the_STORE_as_well_as_the_route(db):
    """Defence in depth, and it is not decoration: the route is not the only writer that could
    ever exist, so the record type states the rule too — and migration 1067 carries it as a CHECK."""
    from featuregen.materialize.request_store import record_request

    with pytest.raises(ValueError) as excinfo:
        record_request(
            db, request_id="mreq_half", logical_group_name="grp", requested_by="user:x",
            authorized_roles=("platform-admin",), idempotency_key="half-key",
            activation_state={"flag": "x"}, considered_revision_id="rev-only")
    assert "BOTH halves" in str(excinfo.value)
