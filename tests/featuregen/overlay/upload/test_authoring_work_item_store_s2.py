"""S2 — the generalized work item, the compatibility reader, and `FeatureDefinitionV1`.

The load-bearing claim is that this is NOT a backfill. `recipe_formula_shadow_work_item` is
write-once with UPDATE/DELETE revoked, and its NOT NULL columns are recipe-specific — a free-form
run could not produce a legal legacy row. So the legacy rows keep their exact meaning and a reader
unions the two shapes, labelling each by WHICH TABLE it came from rather than by a column somebody
could set wrongly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.materialize.admission import FeatureNamePlanError
from featuregen.overlay.upload.authoring_work_item_store import (
    AuthoringWorkItemV1,
    FeatureDefinitionV1,
    WorkItemOrigin,
    link_selection_to_definition,
    read_work_items,
    record_work_item,
    resolve_feature_definition,
)

REVISION = "cons-1"


def _legacy_lineage(db):
    """The legacy FK chain, seeded through the SHADOW suite's own helper rather than hand-rolled —
    a hand-rolled chain would be a fixture of my own shape rather than the one 1023 requires."""
    from tests.featuregen.overlay.upload.test_recipe_formula_shadow import _declare

    intent_id, run_id, revision_id, considered_hash, _ = _declare(db, "s2")
    return intent_id, run_id, revision_id, considered_hash


def _item(**overrides) -> AuthoringWorkItemV1:
    kwargs = dict(
        work_item_id="wi-1", origin=WorkItemOrigin.RECIPE, intent_id="int-1",
        considered_revision_id=REVISION, option_id="opt-1",
        expectation={"aggregation": "sum"}, expectation_hash="sha256:exp",
        binding_plan_hash="sha256:bind", frozen_configuration_hash="sha256:frozen",
        reviewed_blueprint_revision="candidate-1")
    kwargs.update(overrides)
    return AuthoringWorkItemV1(**kwargs)


def _definition(name: str = "posted_debit_amount_30d") -> FeatureDefinitionV1:
    return FeatureDefinitionV1(definition_id="fd-1", feature_name=name, entity="account",
                               grain_keys=("acct_id",))


# ══ the origin axis ══════════════════════════════════════════════════════════════════════════════
def test_only_a_RECIPE_item_stands_on_a_reviewed_blueprint():
    """Which is what lets C-A5's deterministic producer take it without a provider call."""
    assert WorkItemOrigin.RECIPE.authors_from_reviewed_blueprint
    assert not WorkItemOrigin.LLM_INTENT.authors_from_reviewed_blueprint
    assert not WorkItemOrigin.USER_DEFINITION.authors_from_reviewed_blueprint


def test_a_recipe_item_WITHOUT_a_blueprint_is_refused():
    with pytest.raises(ValueError, match="cannot take the deterministic path"):
        _item(reviewed_blueprint_revision=None)


def test_a_NON_recipe_item_claiming_a_blueprint_is_refused():
    """It would assert a review that never happened."""
    with pytest.raises(ValueError, match="assert a review that never happened"):
        _item(origin=WorkItemOrigin.LLM_INTENT, reviewed_blueprint_revision="candidate-1")


def test_a_free_form_item_needs_no_blueprint():
    assert _item(origin=WorkItemOrigin.USER_DEFINITION,
                 reviewed_blueprint_revision=None).origin is WorkItemOrigin.USER_DEFINITION


def test_the_origin_vocabulary_is_CLOSED():
    """An origin nothing recognises can be authored by no path, and would sit unprocessed."""
    assert {o.value for o in WorkItemOrigin} == {"recipe", "llm_intent", "user_definition"}


# ══ the compatibility reader unions BOTH shapes ══════════════════════════════════════════════════
def _legacy_row(db, work_item_id: str, *, recipe_id: str = "posted_debit_amount") -> str:
    """A row in the LEGACY table, written exactly as 1023 shaped it."""
    intent_id, run_id, revision_id, considered_hash = _legacy_lineage(db)
    db.execute(
        "INSERT INTO recipe_formula_shadow_work_item (work_item_id, idempotency_key, "
        "capture_entry_id, generation_run_id, intent_id, considered_revision_id, "
        "considered_content_hash, recipe_id, recipe_candidate_key, recipe_expectation_json, "
        "recipe_expectation_hash, binding_envelope_json, binding_envelope_hash, "
        "provider_input_json, provider_input_hash, frozen_configuration_json, "
        "frozen_configuration_hash, request_identity_json, request_read_scope_hash, payload_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s::jsonb, %s, "
        "%s::jsonb, %s, %s::jsonb, %s, %s)",
        (work_item_id, f"idem-{work_item_id}", f"cap-{work_item_id}", run_id, intent_id,
         revision_id, considered_hash, recipe_id, "legacy-candidate",
         json.dumps({"legacy": True}),
         "sha256:legacy-exp", json.dumps({}), "sha256:env", json.dumps({}), "sha256:pin",
         json.dumps({}), "sha256:frozen", json.dumps({}), "sha256:scope", "sha256:payload"))
    return revision_id


def test_THE_READER_UNIONS_LEGACY_AND_GENERALIZED_ROWS(db):
    revision = _legacy_row(db, "wi-legacy")
    record_work_item(db, _item(work_item_id="wi-new", origin=WorkItemOrigin.LLM_INTENT,
                               considered_revision_id=revision,
                               reviewed_blueprint_revision=None), idempotency_key="idem-new")

    items = read_work_items(db, considered_revision_id=revision)
    assert {i.work_item_id for i in items} == {"wi-legacy", "wi-new"}


def test_a_LEGACY_row_is_labelled_by_WHICH_TABLE_it_came_from(db):
    """Not by a column somebody could have set wrongly. Its `recipe_candidate_key` IS the reviewed
    blueprint it stood on, which is the fact the generalized shape records explicitly."""
    revision = _legacy_row(db, "wi-legacy")
    (item,) = read_work_items(db, considered_revision_id=revision)
    assert item.origin is WorkItemOrigin.RECIPE
    assert item.reviewed_blueprint_revision == "legacy-candidate"
    assert item.expectation == {"legacy": True}


def test_a_legacy_row_says_it_has_NO_OPTION_rather_than_inventing_one(db):
    """The legacy shape predates option-addressable selection. Naming the recipe is honest;
    inventing an option nobody served is not."""
    revision = _legacy_row(db, "wi-legacy")
    (item,) = read_work_items(db, considered_revision_id=revision)
    assert item.option_id == "legacy-recipe:posted_debit_amount"


def test_NOTHING_IS_BACKFILLED_the_legacy_table_is_untouched(db):
    """The whole reason for two tables: the legacy table is write-once by trigger, so a backfill
    could not run even if it were desirable."""
    import psycopg

    _legacy_row(db, "wi-legacy")
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE recipe_formula_shadow_work_item SET recipe_id = %s WHERE work_item_id = %s",
                   ("something_else", "wi-legacy"))


def test_the_new_table_is_write_once_TOO(db):
    """Matching the legacy guarantee rather than weakening it: a work item is what a run replays
    FROM, and one that can be edited makes the replay a record of something that never happened."""
    import psycopg

    record_work_item(db, _item(), idempotency_key="idem-1")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute("UPDATE authoring_work_item SET option_id = %s WHERE work_item_id = %s",
                   ("opt-other", "wi-1"))


def test_recording_is_IDEMPOTENT_on_its_key(db):
    record_work_item(db, _item(), idempotency_key="idem-1")
    record_work_item(db, _item(), idempotency_key="idem-1")
    assert len(read_work_items(db, considered_revision_id=REVISION)) == 1


def test_THE_DATABASE_enforces_the_blueprint_rule_too(db):
    """It survives a caller that bypasses the type."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO authoring_work_item (work_item_id, idempotency_key, origin, intent_id, "
            "considered_revision_id, option_id, expectation_json, expectation_hash, "
            "binding_plan_hash, frozen_configuration_hash) "
            "VALUES (%s, %s, 'recipe', %s, %s, %s, %s::jsonb, %s, %s, %s)",
            ("wi-bad", "idem-bad", "int-1", REVISION, "opt-1", "{}", "h", "b", "f"))


# ══ FeatureDefinitionV1 — created OR resolved ════════════════════════════════════════════════════
def test_TWO_SELECTIONS_AUTHORING_THE_SAME_FEATURE_SHARE_ONE_DEFINITION(db):
    """Create-and-resolve are one operation because the definition is identified by its content."""
    first = resolve_feature_definition(db, _definition())
    second = resolve_feature_definition(
        db, FeatureDefinitionV1(definition_id="fd-2", feature_name="posted_debit_amount_30d",
                                entity="account", grain_keys=("acct_id",)))
    assert first == second == "fd-1", "the second call resolved rather than created"


def test_a_DIFFERENT_feature_gets_its_own_definition(db):
    assert resolve_feature_definition(db, _definition()) != resolve_feature_definition(
        db, FeatureDefinitionV1(definition_id="fd-2", feature_name="posted_credit_amount_30d",
                                entity="account", grain_keys=("acct_id",)))


def test_the_name_is_folded_through_THE_ONE_normalizer():
    """A second normalizer is a second chance to disagree about which column a feature occupies."""
    assert _definition("Posted Debit Amount 30d").feature_name == "posted_debit_amount_30d"
    with pytest.raises(FeatureNamePlanError):
        _definition("9lives")


def test_two_definitions_cannot_share_a_NAME_at_one_entity(db):
    """They would publish to one column — the collision `hive_identifier` exists to prevent."""
    import psycopg

    resolve_feature_definition(db, _definition())
    with pytest.raises(psycopg.errors.UniqueViolation):
        resolve_feature_definition(
            db, FeatureDefinitionV1(definition_id="fd-2", feature_name="posted_debit_amount_30d",
                                    entity="account", grain_keys=("cif_id",)))


def test_a_definition_needs_an_entity_and_a_grain():
    with pytest.raises(ValueError, match="must name the entity"):
        FeatureDefinitionV1(definition_id="fd", feature_name="f", entity=" ",
                            grain_keys=("acct_id",))
    with pytest.raises(ValueError, match="describes no population"):
        FeatureDefinitionV1(definition_id="fd", feature_name="f", entity="account",
                            grain_keys=())


# ══ the selection→definition link ════════════════════════════════════════════════════════════════
def _seed_selection(db, revision_id: str = "fsr-1") -> str:
    # Migration 1116 makes `feature_selection_revision.considered_revision_id` a real foreign key,
    # so the revision this selection names has to exist. Seeding only.
    seed_run_chain(db, run_id="awis2", considered_revision_id=REVISION)
    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING", ("int-1", "h", "hypothesis"))
    db.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, provenance, "
        "target_logical_ref, target_type, horizon_days, content_hash) "
        "VALUES (%s, %s, 'prediction', 'human_confirmed', %s, 'boolean', 90, %s) "
        "ON CONFLICT DO NOTHING",
        ("trr-1", "int-1", "hdfc::public.txns.churned", "sha256:r"))
    db.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, "
        "binding_plan_hash, content_hash) VALUES (%s, 'trr-1', %s, %s, 'dec-1', 'p', 'b', 's') "
        "ON CONFLICT DO NOTHING",
        (revision_id, REVISION, f"opt-{revision_id}"))
    return revision_id


def test_a_selection_links_to_its_definition(db):
    selection = _seed_selection(db)
    definition_id = resolve_feature_definition(db, _definition())
    link_selection_to_definition(db, selection_revision_id=selection,
                                 definition_id=definition_id)
    row = db.execute(
        "SELECT definition_id FROM feature_selection_definition_link "
        "WHERE selection_revision_id = %s", (selection,)).fetchone()
    assert row[0] == definition_id


def test_A_SELECTION_RESOLVES_TO_ONE_DEFINITION_ONCE(db):
    """Re-authoring to a different definition would mean the feature a person chose became a
    different feature with nothing recording the change."""
    import psycopg

    selection = _seed_selection(db)
    first = resolve_feature_definition(db, _definition())
    other = resolve_feature_definition(
        db, FeatureDefinitionV1(definition_id="fd-2", feature_name="posted_credit_amount_30d",
                                entity="account", grain_keys=("acct_id",)))
    link_selection_to_definition(db, selection_revision_id=selection, definition_id=first)
    with pytest.raises(psycopg.errors.UniqueViolation):
        link_selection_to_definition(db, selection_revision_id=selection, definition_id=other)


# ══ the migration split the plan's 1073 needed ═══════════════════════════════════════════════════
def test_EACH_S2_DELIVERABLE_HAS_ITS_OWN_MIGRATION_FILE():
    """1073 carries three deliverables under one filename, and `apply_migrations` ledgers by stem
    AND byte checksum — whichever writes it first owns it, and the others cannot edit it once
    applied anywhere."""
    migrations = Path("src/featuregen/db/migrations")
    assert (migrations / "1084_typed_planning_request.sql").exists()
    assert (migrations / "1087_feature_definition.sql").exists()
    assert (migrations / "1088_generalized_authoring_work_item.sql").exists()
    assert not (migrations / "1073_.sql").exists()
    assert not list(migrations.glob("1073_*.sql")), "1073 is deliberately NOT written"


# ══ S2 acceptance — a candidate OUTSIDE the shadow top 12 authors ════════════════════════════════
def test_THE_TOP_12_GATE_IS_STRUCTURALLY_ABSENT_from_the_authoring_path():
    """S2's acceptance: "a candidate outside the shadow top 12 authors and admits".

    The legacy path creates work items for `capture_required` entries ONLY
    (`recipe_formula_shadow.py:1265`), and `capture_required = selected_for_initial_view and
    authorable` (`:593`) — so a candidate the initial view did not show never got a work item and
    therefore never authored. The generalized item carries no such field: the gate is not relaxed
    here, it does not exist, which is the difference between "we remembered to allow it" and "it
    cannot be forbidden".
    """
    import dataclasses

    names = {f.name for f in dataclasses.fields(AuthoringWorkItemV1)}
    for shadow_only in ("capture_required", "selected_for_initial_view", "canonical_rank",
                        "ranking_version"):
        assert shadow_only not in names, shadow_only


def test_A_CANDIDATE_RANKED_FAR_OUTSIDE_THE_INITIAL_VIEW_RECORDS_AND_READS_BACK(db):
    """The behavioural half. Nothing about rank reaches this store, so a 99th-ranked candidate is
    recorded and read back exactly as a 1st-ranked one is."""
    record_work_item(
        db, _item(work_item_id="wi-rank-99", option_id="opt-ranked-99"),
        idempotency_key="idem-rank-99")

    (item,) = read_work_items(db, considered_revision_id=REVISION)
    assert item.work_item_id == "wi-rank-99"
    assert item.origin is WorkItemOrigin.RECIPE


def test_the_SHADOW_capture_rule_is_untouched():
    """Scoped honestly: the shadow path keeps its own top-12 CAPTURE rule, which is a different
    concern (what the initial view shows) from what may be authored. This changes the authoring
    path only."""
    import inspect

    from featuregen.overlay.upload import recipe_formula_shadow

    source = inspect.getsource(recipe_formula_shadow.build_capture_entries)
    assert "item.selected_for_initial_view and authorable" in source
