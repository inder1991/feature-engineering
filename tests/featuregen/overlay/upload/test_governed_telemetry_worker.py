"""S1B-3 — the telemetry WORKER: claim one outbox item, replan its frozen inputs, observe, complete.

The producer half (``gate1``) persists a work item and plans nothing. This is the other half, and
everything it may consume is the work item plus the DB: no env, no LLM, no live catalog pointer.
Two properties are worth stating before the tests, because they are what the suite is really about:

* **Both outcomes are recorded.** A telemetry programme that records only successes answers "how
  good are the features we shipped?" and never "what could this platform not do?". So a REJECTED
  request leaves a row exactly as a resolved one does — and, when its refusal is somebody's missing
  crossing, a demand child underneath it.
* **Two demand sources, and the second is the one that matters today.** The platform's dominant real
  refusal is ``physical_cardinality_unavailable``: the governed bridge is sanctioned, but no
  executable realization revision is attached, so the roll-up cannot be costed. That refusal rides a
  path that RESOLVED source→target, which means it mints no rejected candidate and carries NO unmet
  hop. A worker keyed only on unmet hops would file zero demand for it — the single most common gap
  in the platform would be invisible to the queue that funds bridge work.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from tests.featuregen.overlay.upload.contract.test_governed_lens_requests import (
    RECIPE_ID,
    _intent_request,
    _no_bridge_far_realizer,
    _two_catalogs,
)

import featuregen.overlay.upload.governed_telemetry_worker as worker_module
from featuregen.overlay.upload.contract.gate1 import persist_intent
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.feature_metadata_snapshot import ensure_generation_run
from featuregen.overlay.upload.feature_planning_contracts import (
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.governed_observation_store import (
    claim_telemetry_work,
    enqueue_governed_telemetry,
)
from featuregen.overlay.upload.governed_scope_material import (
    frozen_telemetry_inputs,
    governed_scope_material,
    planning_request_from_frozen,
)
from featuregen.overlay.upload.governed_telemetry_worker import (
    MAX_REQUESTS_PER_ITEM,
    STALE_REGISTRY,
    UNRESOLVED_PLAN_CONTENT_HASH,
    contract_level_demands,
    run_governed_telemetry_once,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

_NOW = datetime(2026, 7, 14, tzinfo=UTC)
_OWNER = "telemetry-worker-1"


# ── harness ───────────────────────────────────────────────────────────────────────────────────


def _lineage(db, *, hypothesis: str = "churn follows a 90 day drop in activity"):
    """A real intent + a real generation run bound to it — 1120/1121 both enforce the lineage."""
    intent = submit_intent(hypothesis=hypothesis, actor="ds1")
    persist_intent(db, intent)
    run_id = f"grun_s1b3_{intent.intent_id[-8:]}"
    ensure_generation_run(db, run_id, {"subject": "ds1"}, {"intake_mode": "hypothesis"},
                          intent_id=intent.intent_id)
    return intent, run_id


def _enqueue(db, *, run_id, intent, recipe_ids=(), intent_requests=(), roles=(),
             hypothesis: str | None = None, snapshot_id="snap_s1b3"):
    frozen = frozen_telemetry_inputs(
        v2_eligible_ids=frozenset(recipe_ids), intent_requests=tuple(intent_requests),
        roles=roles, target_entity="account", anchor_catalog_source="ops",
        scope_material=governed_scope_material(db, roles=roles, target_entity="account"),
        metadata_snapshot_id=snapshot_id,
        redacted_hypothesis=(hypothesis if hypothesis is not None
                             else intent.redacted_hypothesis))
    return enqueue_governed_telemetry(db, generation_run_id=run_id,
                                      intent_id=intent.intent_id, frozen_inputs=frozen)


def _observations(db, run_id) -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM governed_planning_observation WHERE generation_run_id = %s "
                    "ORDER BY canonical_definition_id", (run_id,))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _demands(db, observation_id) -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM bridge_demand_observation WHERE observation_id = %s "
                    "ORDER BY demand_queue, relationship_id", (observation_id,))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _by_id(rows, definition_id) -> dict:
    return next(row for row in rows if row["canonical_definition_id"] == definition_id)


# ── 1. the frozen inputs: what the request path hands over ────────────────────────────────────


def test_scope_material_is_the_authorization_shape_and_carries_no_watermark(db):
    _two_catalogs(db)
    material = governed_scope_material(db, roles=(), target_entity="account")
    assert material["authorized_catalog_sources"] == ["ops", "rev"]
    assert material["target_entity"] == "account"
    assert material["read_scope_policy_version"] and material["role_resolution_version"]
    # the watermark-bearing half of a resolved scope must never be here: `scope_id` and the state
    # stamps re-mint on every ingest, and a demand queue grouped on them shatters per ingest
    assert "scope_id" not in material
    assert "catalog_state_stamps" not in material
    assert "resolved_at" not in material


def test_the_frozen_intent_payload_round_trips_by_hash(db):
    """The payload is the ONLY copy of an LLM intent's request — there is no registry to re-derive
    it from — so the round trip is proven by identity, not by eyeballing fields."""
    recipe = v2_recipe_by_id(RECIPE_ID)
    original = _intent_request(recipe)
    frozen = frozen_telemetry_inputs(
        v2_eligible_ids=(), intent_requests=(original,), target_entity="account",
        anchor_catalog_source="ops", scope_material={}, metadata_snapshot_id=None)
    ref = frozen["requests"][0]
    restored = planning_request_from_frozen(ref["payload"])
    assert restored == original
    assert planning_request_hash(restored) == ref["planning_request_hash"]


def test_a_recipe_is_frozen_as_a_reference_never_a_payload(db):
    frozen = frozen_telemetry_inputs(
        v2_eligible_ids={RECIPE_ID, "no_such_recipe"}, target_entity="account",
        anchor_catalog_source="ops", scope_material={}, metadata_snapshot_id=None)
    assert [ref["canonical_definition_id"] for ref in frozen["requests"]] == [RECIPE_ID]
    ref = frozen["requests"][0]
    assert "payload" not in ref            # in-code and re-derivable: freezing it would be a lie
    assert ref["origin"] == "recipe_v2"
    assert ref["planning_request_hash"] == planning_request_hash(
        planning_request_from_recipe(v2_recipe_by_id(RECIPE_ID)))


# ── 2. the end-to-end: enqueue → claim → plan → observe → complete ────────────────────────────


def test_one_item_records_both_outcomes_and_the_g3_realization_gap(db):
    """The whole producer→worker chain over the real registry recipe and the real bridge seed.

    The shipped recipe REJECTS at the G3 boundary (its measures cannot be costed over a governed
    bridge with no realization revision) and the measure-free intent RESOLVES — both in ONE item,
    which is the property that makes a resolution RATE meaningful."""
    _two_catalogs(db)
    intent, run_id = _lineage(db)
    recipe = v2_recipe_by_id(RECIPE_ID)
    twin = _intent_request(recipe)
    work_item_id = _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID],
                            intent_requests=[twin])

    summary = run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)
    assert summary is not None
    assert summary["work_item_id"] == work_item_id
    assert summary["status"] == "done"
    assert (summary["observations"], summary["resolved"], summary["rejected"]) == (2, 1, 1)
    assert db.execute("SELECT status, lease_owner FROM governed_telemetry_outbox "
                      "WHERE work_item_id = %s", (work_item_id,)).fetchone() == ("done", "")

    rows = _observations(db, run_id)
    assert len(rows) == 2
    assert {row["observation_mode"] for row in rows} == {"telemetry"}
    assert {row["intent_id"] for row in rows} == {intent.intent_id}
    assert {row["metadata_snapshot_id"] for row in rows} == {"snap_s1b3"}
    assert all(row["catalog_scope_material"]["authorized_catalog_sources"] == ["ops", "rev"]
               for row in rows)
    assert len({row["compiler_input_fingerprint"] for row in rows}) == 1

    # ── the RESOLVED row (the intent origin)
    resolved = _by_id(rows, twin.source_definition_id)
    assert resolved["definition_origin"] == "llm_intent"
    assert resolved["recipe_id"] is None                       # origin purity, in the ledger too
    assert resolved["resolution_status"] == "resolved"
    assert len(resolved["physical_plan_content_hash"]) == 64
    assert resolved["selected_physical_plan_id"].startswith("bp_")
    assert resolved["participating_catalogs"] == ["ops", "rev"]
    assert resolved["bridge_count"] == 1
    assert resolved["readiness"] == "CONCEPTUAL_ONLY"
    # every concept pin in this platform reads `absent` (a RECOMMENDATION-tier policy), so the
    # authoring floor is honestly UNMET rather than silently assumed
    assert resolved["authority_floor_status"] == "unmet"
    assert resolved["reason_codes"] == []
    assert _demands(db, resolved["observation_id"]) == []

    # ── the REJECTED row (the shipped recipe, at G3)
    rejected = _by_id(rows, RECIPE_ID)
    assert rejected["definition_origin"] == "recipe_v2"
    assert rejected["recipe_id"] == RECIPE_ID
    assert rejected["resolution_status"] == "physical_cardinality_unavailable"
    assert rejected["reason_codes"][0] == "physical_cardinality_unavailable"
    assert rejected["participating_catalogs"] == ["ops", "rev"]
    assert (rejected["hop_count"], rejected["bridge_count"]) == (1, 1)
    assert rejected["anchor_catalog_source"] == "ops"
    assert rejected["readiness"] == ""                 # no option, so no readiness fold to report
    # THE identity decision for an unresolved row: a plan content hash cannot exist without a
    # plan, and the type refuses blank — so the row carries an unmistakable SENTINEL, stored in
    # the column too, which keeps `governed_variant_id` recomputable from the row's own values.
    assert rejected["physical_plan_content_hash"] == UNRESOLVED_PLAN_CONTENT_HASH
    assert rejected["governed_variant_id"].startswith("gvar_")
    assert rejected["governed_variant_id"] != resolved["governed_variant_id"]

    # ── the SECOND demand source: a realization gap filed from plan-level fields alone
    demands = _demands(db, rejected["observation_id"])
    assert len(demands) == 1
    demand = demands[0]
    assert demand["demand_queue"] == "realization_gap"
    assert demand["verdict"] == "missing_realization"
    assert (demand["from_entity"], demand["to_entity"]) == ("transaction", "account")
    assert demand["position_catalog"] == "ops"
    assert demand["position_table_ref"] == "public.transactions"
    assert demand["near_side_key_refs"] == ["public.transactions.account_id"]
    assert demand["to_endpoint_hint"] == "rev::public.accounts.account_id"
    assert demand["realizers"] == [{"bridge_fact_key": "bfk_s1a4b",
                                    "catalog_source": "rev",
                                    "from_catalog_source": "ops",
                                    "from_key_ref": "public.transactions.account_id",
                                    "to_key_ref": "public.accounts.account_id",
                                    "realization_state": "no_executable_revision"}]
    assert demand["recipe_revision_hash"] == planning_request_from_recipe(
        v2_recipe_by_id(RECIPE_ID)).source_content_hash
    assert summary["demands"] == 1


def test_the_unmet_hop_source_files_the_bridge_demand_with_the_s1b2_fields(db):
    """The FIRST demand source, unchanged from S1B-2: a frontier that genuinely dead-ends carries a
    typed unmet hop, and the row keys off THAT HOP's verdict — never the rejection's headline.

    Driven through the measure-free request rather than the shipped recipe: on a seed with no
    bridge the SHIPPED operand set cannot even bind (``ops`` carries no amount/status here), so it
    refuses at ``missing_required_need`` and never reaches a frontier to dead-end on. The dead end
    is the point of this test, so the request that reaches one is the one that drives it."""
    _no_bridge_far_realizer(db)
    intent, run_id = _lineage(db)
    request = _intent_request(v2_recipe_by_id(RECIPE_ID))
    _enqueue(db, run_id=run_id, intent=intent, intent_requests=[request])

    summary = run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)
    assert summary["status"] == "done" and summary["rejected"] == 1

    row = _by_id(_observations(db, run_id), request.source_definition_id)
    assert row["resolution_status"] == "unsanctioned_bridge"
    demands = _demands(db, row["observation_id"])
    assert len(demands) == 1
    demand = demands[0]
    assert demand["demand_queue"] == "bridge_demand"
    assert demand["verdict"] == "unsanctioned_bridge"
    assert demand["relationship_id"] == "transaction_to_account"
    assert demand["relationship_version"] == "1.0.0"
    assert demand["position_catalog"] == "ops"
    assert demand["position_table_ref"] == "public.transactions"
    assert demand["hop_index"] == 0
    assert demand["near_side_key_refs"] == ["public.transactions.account_id"]
    assert demand["realizers"][0]["catalog_source"] == "arc"
    # the anchor was passed EXPLICITLY, so the store never had to look it up
    assert demand["recipe_revision_hash"]


def test_a_capacity_refusal_files_its_row_from_the_rejection_level(db):
    """``bounded_out_max_frontier_states`` is hop-less BY DESIGN (its reject is minted from the
    START state), so it can never arrive through ``unmet_hops``. Filed at the rejection level or
    not at all — driven directly, because the seed that exhausts a real frontier would be a
    fixture about the planner's bounds rather than about this adapter."""
    demands = contract_level_demands(
        {"reason_codes": ["bounded_out_max_frontier_states"], "evidence": [],
         "anchor_catalog_source": "ops"},
        recipe_revision_hash="rev-hash")
    assert demands == [{"verdict": "bounded_out_max_frontier_states",
                        "anchor_catalog_source": "ops",
                        "recipe_revision_hash": "rev-hash"}]


def test_a_refusal_that_is_nobodys_missing_crossing_files_no_demand(db):
    """A closed vocabulary in both directions: a contract refusal that is not a realization gap and
    not a capacity bound is a modelling problem, and modelling problems are not bridge demand."""
    assert contract_level_demands(
        {"reason_codes": ["additivity_source_conflict"], "evidence": []},
        recipe_revision_hash="rev-hash") == []


# ── 3. the registry moving under a work item ──────────────────────────────────────────────────


def test_a_moved_registry_definition_is_recorded_stale_and_never_planned(db, monkeypatch):
    """The freeze names a recipe by ID; the worker re-derives it. If the registry moved in between,
    planning the NEW definition would silently answer a different question under the old run's
    lineage — so the request is recorded as stale and skipped."""
    _two_catalogs(db)
    intent, run_id = _lineage(db)
    _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID])

    original = v2_recipe_by_id(RECIPE_ID)
    moved = dataclasses.replace(original, business_definition="edited after the enqueue")
    monkeypatch.setattr(worker_module, "v2_recipe_by_id", lambda rid: moved)

    planned: list = []
    real_plan = worker_module.governed_options_from_requests

    def _spy(conn, **kwargs):
        planned.extend(kwargs["requests"])
        return real_plan(conn, **kwargs)

    monkeypatch.setattr(worker_module, "governed_options_from_requests", _spy)

    summary = run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)
    assert summary["stale"] == 1 and summary["resolved"] == 0 and summary["rejected"] == 0
    assert planned == []                    # the moved definition never reached the planner
    row = _by_id(_observations(db, run_id), RECIPE_ID)
    assert row["resolution_status"] == STALE_REGISTRY
    assert row["physical_plan_content_hash"] == UNRESOLVED_PLAN_CONTENT_HASH
    # the row still names what the RUN froze, not what the registry now holds
    assert row["planning_request_hash"] == planning_request_hash(
        planning_request_from_recipe(original))


def test_a_recipe_that_left_the_registry_is_recorded_not_dropped(db, monkeypatch):
    _two_catalogs(db)
    intent, run_id = _lineage(db)
    _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID])
    monkeypatch.setattr(worker_module, "v2_recipe_by_id", lambda rid: None)

    summary = run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)
    assert summary["stale"] == 1
    assert _by_id(_observations(db, run_id), RECIPE_ID)["resolution_status"] == STALE_REGISTRY


# ── 4. the fence ──────────────────────────────────────────────────────────────────────────────


def test_a_lost_lease_discards_this_workers_writes(db, monkeypatch):
    """The fence is not decoration. A worker whose lease expired mid-flight has been overtaken: its
    completion is refused, and the observations it computed under a lease it no longer holds are
    rolled back rather than written beside the live worker's."""
    _two_catalogs(db)
    intent, run_id = _lineage(db)
    _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID])

    stale = claim_telemetry_work(db, owner="zombie")
    assert stale["lease_fence"] == 1
    db.execute("UPDATE governed_telemetry_outbox SET lease_expires_at = now() - interval '1 hour' "
               "WHERE work_item_id = %s", (stale["work_item_id"],))
    live = claim_telemetry_work(db, owner="live-worker")           # reclaims: fence 2
    assert live["lease_fence"] == 2

    monkeypatch.setattr(worker_module, "claim_telemetry_work", lambda conn, **kw: dict(stale))
    summary = run_governed_telemetry_once(db, owner="zombie", now=_NOW)

    assert summary["status"] == "lease_lost"
    assert summary["observations"] == 0
    assert _observations(db, run_id) == []          # discarded, not written
    assert db.execute("SELECT status, lease_owner FROM governed_telemetry_outbox "
                      "WHERE work_item_id = %s",
                      (stale["work_item_id"],)).fetchone() == ("leased", "live-worker")


def test_nothing_claimable_returns_none(db):
    assert run_governed_telemetry_once(db, owner=_OWNER, now=_NOW) is None


def test_a_planning_failure_fails_the_item_and_never_raises(db, monkeypatch):
    _two_catalogs(db)
    intent, run_id = _lineage(db)
    work_item_id = _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID])

    def _boom(*args, **kwargs):
        raise RuntimeError("the planner fell over")

    monkeypatch.setattr(worker_module, "governed_options_from_requests", _boom)
    summary = run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)

    assert summary["status"] == "failed"
    assert "the planner fell over" in summary["error"]
    assert _observations(db, run_id) == []
    status, last_error = db.execute(
        "SELECT status, last_error FROM governed_telemetry_outbox WHERE work_item_id = %s",
        (work_item_id,)).fetchone()
    assert status == "failed" and "the planner fell over" in last_error


# ── 5. the derived columns ────────────────────────────────────────────────────────────────────


def test_param_divergence_records_the_hypothesis_window_against_the_primary_variant(db):
    """R21, pure and provider-free: the person said 30 days, the recipe's primary variant binds
    something else. That is a finding about the SUGGESTION, and it costs nothing to measure."""
    _two_catalogs(db)
    intent, run_id = _lineage(db, hypothesis="attrition follows a 7-day drop in card activity")
    _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID])
    run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)

    row = _by_id(_observations(db, run_id), RECIPE_ID)
    primary = dict(planning_request_from_recipe(v2_recipe_by_id(RECIPE_ID)).parameter_values)
    assert int(primary["window"]) != 7                 # the fixture is a real divergence, measured
    assert row["param_divergence"] == [
        {"parameter": "window", "hypothesis_implied": "7d",
         "primary_value": f"{int(primary['window'])}d"}]


def test_a_hypothesis_that_agrees_with_the_variant_records_no_divergence(db):
    _two_catalogs(db)
    primary = dict(planning_request_from_recipe(v2_recipe_by_id(RECIPE_ID)).parameter_values)
    intent, run_id = _lineage(
        db, hypothesis=f"attrition follows a {int(primary['window'])} day drop in activity")
    _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID])
    run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)
    assert _by_id(_observations(db, run_id), RECIPE_ID)["param_divergence"] == []


def test_intent_corroboration_marks_the_recipe_row_not_the_intent(db):
    """An LLM intent that re-derives an authored recipe is CORROBORATION on the recipe's row —
    the same reading ``assemble_candidates`` gives it when it merges twins on a card."""
    _two_catalogs(db)
    intent, run_id = _lineage(db)
    recipe_request = planning_request_from_recipe(v2_recipe_by_id(RECIPE_ID))
    twin = dataclasses.replace(recipe_request, origin="llm_intent",
                               source_definition_id="intent:twin_of_the_recipe",
                               source_content_hash="twin-content-hash")
    _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID], intent_requests=[twin])
    run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)

    rows = _observations(db, run_id)
    assert _by_id(rows, RECIPE_ID)["intent_corroborated"] is True
    assert _by_id(rows, "intent:twin_of_the_recipe")["intent_corroborated"] is False


def test_an_unrelated_intent_does_not_corroborate(db):
    _two_catalogs(db)
    intent, run_id = _lineage(db)
    recipe = v2_recipe_by_id(RECIPE_ID)
    _enqueue(db, run_id=run_id, intent=intent, recipe_ids=[RECIPE_ID],
             intent_requests=[_intent_request(recipe)])   # a DIFFERENT operand set
    run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)
    assert _by_id(_observations(db, run_id), RECIPE_ID)["intent_corroborated"] is False


# ── 6. the cap ────────────────────────────────────────────────────────────────────────────────


def test_the_request_cap_drops_deterministically_and_counts_the_drop(db, monkeypatch):
    """A run may be eligible for hundreds of recipes; one work item may not become an unbounded
    planning job. The cap keeps INTENTS (this run's own proposals) and drops the tail of the
    sorted recipe ids — and it says how many, because silent truncation is how a resolution rate
    quietly becomes a rate over whatever fitted."""
    _two_catalogs(db)
    intent, run_id = _lineage(db)
    monkeypatch.setattr(worker_module, "MAX_REQUESTS_PER_ITEM", 2)

    recipe = v2_recipe_by_id(RECIPE_ID)
    twin = _intent_request(recipe)
    frozen = frozen_telemetry_inputs(
        v2_eligible_ids=(), intent_requests=(twin,), target_entity="account",
        anchor_catalog_source="ops",
        scope_material=governed_scope_material(db, roles=(), target_entity="account"),
        metadata_snapshot_id=None)
    extra = [{"canonical_definition_id": f"zz_recipe_{n}", "origin": "recipe_v2",
              "planning_request_hash": f"hash-{n}", "source_content_hash": f"content-{n}"}
             for n in range(3)]
    frozen["requests"] = [*frozen["requests"], *extra]
    enqueue_governed_telemetry(db, generation_run_id=run_id, intent_id=intent.intent_id,
                               frozen_inputs=frozen)

    summary = run_governed_telemetry_once(db, owner=_OWNER, now=_NOW)
    assert MAX_REQUESTS_PER_ITEM == 60          # the shipped bound; this test patches it to 2
    assert summary["dropped"] == 2
    assert summary["planned"] == 1              # the kept recipe id is unknown here, so stale
    kept = {row["canonical_definition_id"] for row in _observations(db, run_id)}
    assert kept == {twin.source_definition_id, "zz_recipe_0"}
