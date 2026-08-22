"""C3 — effective readiness and requirements are FOLDED at the durable write, not asserted.

Three properties: the re-fold can DEMOTE (drift is the whole point of the current layer),
``requirements_closed`` is a real contract-keyed read of the validation store, and — the
milestone — all four §0.3 materialization codes can clear together on a seeded option, ending
in ``activation_decision(..., "execute_materialization").allowed is True`` (§0.5 item 3).
"""
from __future__ import annotations

import dataclasses
import json

from tests.featuregen.overlay.upload.test_reviewed_expectation_seam import (
    EXEMPLAR,
    UNREVIEWED,
    _candidate,
)

from featuregen.overlay.upload import feature_metadata_snapshot, semantic_option_decision
from featuregen.overlay.upload.activation_policy import (
    FrozenOptionFactsV1,
    activation_decision,
)
from featuregen.overlay.upload.contract.gate1 import FeatureIdea
from featuregen.overlay.upload.feature_metadata_snapshot import SnapshotFreshness
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.recipe_review import record_review_event
from featuregen.overlay.upload.recipe_review_validity import required_reviewer_roles
from featuregen.overlay.upload.semantic_option_decision import (
    _requirements_closed,
    assemble_current_activation_state,
    decision_facts_for_candidate,
    load_frozen_option_facts,
    persist_option_decisions,
)


def _frozen(recipe_id: str, **overrides) -> FrozenOptionFactsV1:
    base = FrozenOptionFactsV1(
        binding_state="bound", generation_source="recipe",
        computation_kind="deterministic_formula", readiness="FORMULA_BLOCKED",
        review_current=True, source_definition_id=recipe_id,
        has_reviewed_formula_expectation=False)
    return dataclasses.replace(base, **overrides)


def test_effective_readiness_is_refolded_not_inherited(conn):
    """A frozen ``MATERIALIZATION_READY`` whose recipe is UNREVIEWED folds DOWN and blocks —
    the frozen claim is not inherited, and the demotion carries the fold's own named blocker."""
    frozen = _frozen(UNREVIEWED, readiness="MATERIALIZATION_READY")
    current = assemble_current_activation_state(conn, frozen=frozen, snapshot_id=None)
    assert current.effective_readiness == "FORMULA_BLOCKED"
    codes = {b.code for b in
             activation_decision(frozen, current, "execute_materialization").blockers}
    assert "READINESS_NOT_MATERIALIZATION_READY" in codes


def test_a_serving_time_blocker_carries_into_the_refold(conn):
    """The durable write cannot re-measure temporal/binding facts — the serving fold's measured
    blockers ride the frozen row and keep the re-fold honest."""
    frozen = _frozen(EXEMPLAR, has_reviewed_formula_expectation=True,
                     readiness_blockers=("REQUIRED_OPERAND_MISSING",))
    current = assemble_current_activation_state(conn, frozen=frozen, snapshot_id=None)
    assert current.effective_readiness == "FORMULA_BLOCKED"


def test_the_refold_rests_at_authorable_while_gold_is_unrecorded(conn):
    """The honest ceiling today: reviewed + engine-supported but NO recorded gold/provider
    evaluation (none has ever run — A3's deferral) folds to ``FORMULA_AUTHORABLE``, not READY."""
    frozen = _frozen(EXEMPLAR, has_reviewed_formula_expectation=True)
    current = assemble_current_activation_state(conn, frozen=frozen, snapshot_id=None)
    assert current.effective_readiness == "FORMULA_AUTHORABLE"


def _seed_contract_with_requirements(db, key: str, codes: list[str]) -> str:
    contract_id = f"contract-{key}"
    db.execute("INSERT INTO feature (feature_id, name, lifecycle_state) "
               "VALUES (%s, %s, 'governed') ON CONFLICT DO NOTHING",
               (f"feat-{key}", f"feat-{key}"))
    db.execute("INSERT INTO contract (contract_id, feature_id, feature_name, version) "
               "VALUES (%s, %s, %s, 1)", (contract_id, f"feat-{key}", f"feat-{key}"))
    for index, code in enumerate(codes):
        db.execute(
            "INSERT INTO feature_validation_requirement (requirement_id, contract_id, "
            "requirement_schema_version, metadata_input_fingerprint, code, content_hash) "
            "VALUES (%s, %s, 'v1', 'fp', %s, %s)",
            (f"req-{key}-{index}", contract_id, code, f"ch-{key}-{index}"))
    return contract_id


def _pass_requirement(db, contract_id: str, requirement_id: str, marker: int) -> None:
    db.execute(
        "INSERT INTO feature_contract_validation_event (event_id, contract_id, "
        "event_type, payload) VALUES (%s, %s, 'EXTERNAL_PASSED', %s)",
        (f"evt-{contract_id}-{marker}", contract_id,
         json.dumps({"requirement_id": requirement_id})))


def test_requirements_closed_requires_a_recorded_result_per_code(db):
    """Every frozen code needs a CURRENT pass on the named contract; a missing contract, a
    missing code, or a half-passed pair all fail closed. Empty codes are honestly closed."""
    contract_id = _seed_contract_with_requirements(db, "rc", ["CURRENCY_CONSISTENT", "PII_SCAN"])
    codes = ("CURRENCY_CONSISTENT", "PII_SCAN")

    assert _requirements_closed(db, None, codes) is False          # no contract named
    assert _requirements_closed(db, contract_id, codes) is False   # nothing recorded
    _pass_requirement(db, contract_id, "req-rc-0", 1)
    assert _requirements_closed(db, contract_id, codes) is False   # half recorded
    _pass_requirement(db, contract_id, "req-rc-1", 2)
    assert _requirements_closed(db, contract_id, codes) is True    # every code recorded
    assert _requirements_closed(db, contract_id, ()) is True       # nothing outstanding
    # ...and through the assembler, keyed by the same contract.
    frozen = _frozen(EXEMPLAR, outstanding_requirement_codes=codes)
    current = assemble_current_activation_state(
        db, frozen=frozen, snapshot_id=None, contract_id=contract_id)
    assert current.requirements_closed is True


def test_all_four_materialization_codes_can_clear_together(db, monkeypatch):
    """THE MILESTONE (§0.5 item 3): a seeded option clears the whole ladder and
    ``execute_materialization`` is ALLOWED.

    What is REAL here: the frozen row (real writers), the review events (all three required
    roles at the frozen revision), the execution/authoring floors (seeded ``field_evidence``
    read through the real resolver pins), the engine verdict (C1/C2, no patching), the
    activation fold itself. What is SEEDED, each named where the plan pre-authorized seeding:
    the gold/provider evaluation (a store and a validity reader now exist, but no certifiable run does: the reviewed corpus holds one clean case — `_gold_evaluation_recorded` derives `False`, formerly the
    documented hook) and snapshot freshness (scaffolding for the contract rung — snapshot
    minting rides the generation pipeline, not this test)."""
    definition = v2_recipe_by_id(EXEMPLAR)
    candidate = dataclasses.replace(
        _candidate(EXEMPLAR), binding_state="bound", review_current=True,
        binding_plan={"plan_kind": "single_source", "catalog_source": "bank",
                      "source_table": "accounts", "population_ref": "accounts",
                      "read_set": ["public.accounts.acct_id"],
                      "role_bindings": {"grain": "public.accounts.acct_id"},
                      "pit": "as-of state at the cutoff", "output_grain": "account",
                      "window": None})
    idea = FeatureIdea(name=EXEMPLAR, description="", derives_from=[], aggregation=None,
                       grain_table=None, source_definition_id=EXEMPLAR)
    facts = decision_facts_for_candidate(candidate, idea, None, "ctx")
    db.execute("INSERT INTO contract_intent (intent_id,hypothesis,intake_mode,"
               "redacted_hypothesis) VALUES ('intent-ms','h','hypothesis','h') "
               "ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO feature_generation_run (generation_run_id,intent_id,actor) "
               "VALUES ('genrun-ms','intent-ms','{}'::jsonb) ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO contract_considered_revision (considered_revision_id,intent_id,"
               "generation_run_id,considered_json,considered_content_hash,"
               "canonicalization_version) VALUES ('rev-ms','intent-ms','genrun-ms',"
               "'{}'::jsonb,'hash-ms','test-v1')")
    assert persist_option_decisions(
        db, considered_revision_id="rev-ms", generation_run_id="genrun-ms",
        metadata_snapshot_id=None, facts_by_option_id={"opt-ms": facts}) == 1
    frozen = load_frozen_option_facts(db, considered_revision_id="rev-ms", option_id="opt-ms")
    assert frozen is not None and frozen.has_reviewed_formula_expectation is True

    # The review, by every role the recipe itself names — real events, real fold.
    for index, role in enumerate(required_reviewer_roles(definition)):
        record_review_event(db, recipe_id=EXEMPLAR,
                            recipe_revision_hash=frozen.recipe_revision_hash,
                            decision="approved", reviewer=f"reviewer-{index}",
                            reviewer_role=role)

    # The floors: one human confirmation per bound ref, read through the REAL resolver pins.
    db.execute(
        "INSERT INTO field_evidence (evidence_id, logical_ref, field_name, proposed_value, "
        "proposed_value_hash, producer, strength, lifecycle, producer_ref, "
        "source_snapshot_id, input_hash) VALUES "
        "('ev-ms', 'bank::public.accounts.acct_id', 'concept', '\"account_identifier\"', "
        "'vh', 'human', 'confirmed', 'active', 'test', 'snap', 'ih')")

    monkeypatch.setattr(semantic_option_decision, "_gold_evaluation_recorded",
                        lambda conn, recipe_id: True)
    monkeypatch.setattr(feature_metadata_snapshot, "compare_snapshot_to_current",
                        lambda conn, snapshot_id: SnapshotFreshness("current", None, None))

    current = assemble_current_activation_state(
        db, frozen=frozen, snapshot_id="snap-ms")
    assert current.effective_readiness == "MATERIALIZATION_READY"
    assert current.formula_schema_supported is True
    assert current.requirements_closed is True or frozen.validation_status == "DESIGN_CHECKED"
    assert current.execution_authority_evaluated and current.execution_floor_met

    decision = activation_decision(frozen, current, "execute_materialization")
    assert decision.blockers == (), [b.code for b in decision.blockers]
    assert decision.allowed is True
