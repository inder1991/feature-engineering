"""Phase-3C.2a Task 5 — the LIVE governed cross-catalog lens in ``build_considered_set``.

On a flag-on-and-activation-approved entity-scoped run the governed PLANNER is the authority for
cross-catalog features: its resolved plans surface as options carrying a governed plan envelope +
structured provenance, and its unresolved plans surface as rejections. With the flag off the whole
branch is skipped — byte-identical to today.

The E4 cutover (2026-08-14) settled the OTHER half of that guarantee by construction. The free-form
generator that used to propose ungoverned cross-catalog candidates — the things
``_reject_cross_catalog_llm`` and the cross-catalog anchor drop existed to catch — is deleted, so an
entity-scoped run has no ungoverned source at all: no option and no anchor can arrive without a
governed plan behind it. The filter itself is still covered as a pure function below; the
integration tests now assert the stronger fact, that there is nothing left for it to remove.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from psycopg.rows import dict_row
from tests.featuregen.overlay.upload.planner.test_plan import (
    _NOW,
    _far_realizer_split,
    _freshness,
    _seed,
    _split,
    _txn_template,
)
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

import featuregen.overlay.upload.contract.gate1 as gate1
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import (
    GOVERNED_CROSS_CATALOG_PLAN_REQUIRED,
    _governed_cross_catalog_options,
    _reject_cross_catalog_llm,
    build_considered_set,
)
from featuregen.overlay.upload.contract.governed_identity import GovernedVariantIdentityV1
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet
from featuregen.overlay.upload.feature_metadata_snapshot import ensure_generation_run
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.templates import Need


def _minimal(db):
    """A tiny single-table catalog so the intake / snapshot writes have a graph to read."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive")])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (_NOW, _NOW))


def _recommend_set_client() -> FakeLLM:
    return FakeLLM(script={"overlay.feature.recommend_set": FakeResponse(output={
        "recommended_lens": "templates", "reasoning": "advisory"})})


# ── (b) a resolved governed plan → a governed option (helper) ─────────────────────────────────────────
def test_helper_surfaces_resolved_governed_plan_as_option(db):
    _cross_seed(db)   # ops + rev + a VERIFIED bridge + fresh watermarks -> a resolved cross-catalog plan
    ideas, rejections, evidence = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert len(ideas) == 1 and not rejections
    # ONE evidence entry per PLANNED recipe, resolved included — the store's invariant is a row per
    # request whatever it did, and a lane that reported only its failures would contribute a
    # pure-failure denominator to a resolution rate shared with the telemetry lane.
    assert len(evidence) == 1
    assert evidence[0]["recipe_id"] == "t_roll"
    assert evidence[0]["resolution_status"] == "resolved"
    assert evidence[0]["reason_codes"] == [] and evidence[0]["unmet_hops"] == []
    # a resolved row carries a REAL plan hash, never the unresolved sentinel
    content_hash = evidence[0]["physical_plan_content_hash"]
    assert len(content_hash) == 64 and content_hash == content_hash.lower()
    assert evidence[0]["anchor_catalog_source"]
    idea = ideas[0]
    assert idea.origin == "governed_planner"
    assert idea.path_authority == "governed_cross_catalog"
    assert idea.plan_envelope is not None            # the exact compiled plan carried forward
    assert idea.plan_envelope.physical_plan_id
    # the option genuinely spans >1 catalog (the whole point of a governed cross-catalog plan)
    assert len({cs for cs, _ref in idea.derives_pairs}) > 1


# ── (c) an unresolved governed plan → a rejection (helper) ────────────────────────────────────────────
def test_helper_unresolved_governed_plan_becomes_a_rejection(db):
    _split(db)                 # ops + rev but NO bridge -> the account roll-up cannot complete
    _freshness(db, "ops", "rev")
    ideas, rejections, evidence = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert ideas == []
    assert len(rejections) == 1
    rej = rejections[0]
    assert rej["lens"] == "governed" and rej["recipe_id"] == "t_roll"
    assert isinstance(rej["reason"], str) and rej["reason"]   # carries a primary reason code
    # S1B-4: the SERVED rejection stays exactly these three keys — the planner evidence rides on
    # the third return value, which no route serializes. `cs.rejections` IS in the Gate-#1
    # response body (`routes/contract.py`), so widening it would publish bridge fact keys and
    # physical object refs to every caller of the considered set.
    assert set(rej) == {"lens", "reason", "recipe_id"}
    assert len(evidence) == 1 and evidence[0]["recipe_id"] == "t_roll"
    assert evidence[0]["reason"] == rej["reason"]          # one reason, one precedence
    assert "unmet_hops" in evidence[0] and "evidence" in evidence[0]


# ── (d)/(e) the LLM cross-catalog filter (pure) ───────────────────────────────────────────────────────
def test_reject_cross_catalog_llm_removes_multi_catalog_and_keeps_single():
    cross = FeatureIdea("cross_feat", "", ["a", "b"], "sum", None,
                        derives_pairs=(("ops", "public.t.a"), ("rev", "public.u.b")))
    single = FeatureIdea("single_feat", "", ["a"], "sum", None,
                         derives_pairs=(("ops", "public.t.a"),))
    filtered, rejections = _reject_cross_catalog_llm([FeatureSet("monetary", [cross, single])])
    surviving = {f.name for s in filtered for f in s.features}
    assert "single_feat" in surviving              # single-catalog untouched
    assert "cross_feat" not in surviving           # cross-catalog removed from its FeatureSet
    assert any(r["name"] == "cross_feat" and r["reason"] == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED
               for r in rejections)


# ── (d)/(e) integration: an entity-scoped run has NO ungoverned option to filter ──────────────────────
def test_no_ungoverned_option_can_reach_a_live_entity_scoped_run(db):
    """The filter's guarantee, now structural. This test used to inject a cross-catalog and a
    single-catalog candidate through the free-form generator and watch ``_reject_cross_catalog_llm``
    remove the first. The E4 cutover (2026-08-14) deleted that generator, so there is no source of
    ungoverned candidates on this branch at all — every lens on the returned set is one the governed
    planner authored. That is what the filter was protecting, asserted at its stronger form."""
    intent = submit_intent(hypothesis="an entity-scoped hypothesis", actor="ds1")
    # target_entity=None + templates=() keeps the governed-options lens out, so anything present
    # would have to have come from somewhere ungoverned.
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None,
                              is_live=True, target_entity=None, templates=(), now=_NOW)
    assert cs.alternatives == []   # nothing ungoverned was even proposed
    # …so there is nothing for the filter to reject either: the rejection is the trace of a candidate
    # that WAS generated and then removed, and none can be generated here any more.
    assert not any(r.get("reason") == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED for r in cs.rejections)


# ── (b) integration: build_considered_set surfaces the governed option under the flag ─────────────────
def test_build_considered_set_surfaces_governed_option_when_live(db):
    _cross_seed(db)
    intent = submit_intent(hypothesis="roll transactions up to the account", actor="ds1")
    cs = build_considered_set(
        db, intent, _recommend_set_client(), catalog_source=None, is_live=True,
        target_entity="account", templates=(_txn_template(),), applicability=None, now=_NOW)
    governed = [f for s in cs.alternatives for f in s.features if f.origin == "governed_planner"]
    assert len(governed) == 1
    assert governed[0].path_authority == "governed_cross_catalog"
    assert governed[0].plan_envelope is not None
    # authority rides on the IDEA, never the lens name
    assert all(s.lens != "governed" for s in cs.alternatives)


# ── 3C.2a CRITICAL: no ungoverned DEFINITION-MODE anchor is customer-visible when live (fail-closed) ──
def test_no_ungoverned_definition_anchor_on_a_live_entity_scoped_run(db):
    """The anchor half of the fail-closed guarantee. An entity-scoped run has NO single catalog to
    plan over, so a definition anchor built there could span >1 catalog with no governed physical
    plan — it had to be dropped and surfaced as a rejection. Since the E4 cutover (2026-08-14) the
    anchor comes from the engine's extraction, which needs a frozen catalog context: with no
    ``catalog_source`` there is no context, so there is NO anchor to drop. Honest absence rather than
    a free-form guess — and the customer-visible outcome the drop existed to produce."""
    intent = submit_intent(hypothesis="an entity-scoped hypothesis",
                           definition="a cross-catalog definition", actor="ds1")
    # target_entity=None + templates=() keeps the governed-options lens out of the way.
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None,
                              is_live=True, target_entity=None, templates=(), now=_NOW)
    assert intent.intake_mode == "definition"   # the anchor path really was the one exercised
    assert cs.anchor is None                    # no ungoverned anchor is choosable at Gate #1


# ── (a) flag off → the governed branch never runs (byte-identical to today) ───────────────────────────
def test_flag_off_skips_the_governed_branch_entirely(db, monkeypatch):
    _minimal(db)

    def _boom(*a, **k):
        raise AssertionError("the governed branch must not run when is_live is False")

    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1._governed_cross_catalog_options", _boom)
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1._reject_cross_catalog_llm", _boom)
    intent = submit_intent(hypothesis="an entity-scoped hypothesis", actor="ds1")
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None,
                              is_live=False, target_entity="account", now=_NOW)
    # Neither _boom fired: no plan_bindings compile, no cross-catalog filter. The set itself is empty
    # — an entity-only run has no candidate source at all since the E4 cutover (2026-08-14) — so what
    # this pins is the skip, proven by the booms that never raised.
    assert cs.alternatives == []


# =============================================================================================
# S1B-4 — the LIVE lane records its own governed evidence.
#
# S1B-3 gave the ENGINE arm a telemetry replan off the request path. This branch has no engine and
# no work item: it plans inline, on the request path, and until now threw its planner evidence away
# the moment it turned a result into a three-key rejection dict. One observation per rejection, in
# mode "live", with the demand children the SAME two-source function files for the telemetry lane.
# =============================================================================================

RUN_ID = "grun_s1b4_live"


def _unsanctioned_bridge_seeds(db):
    """A run that genuinely has NO governed plan and dead-ends on a crossing somebody could build.

    ``_far_realizer_split`` on its own is not that run: ``rev`` carries the transaction grain AND
    declares the transaction -> account join, so ``rev`` binds the whole recipe intra-catalog and
    the run RESOLVES. The unmet hop is still sitting on the ``ops`` candidate — but a resolved run
    files no demand, in this lane or the telemetry one, because nobody is missing anything.

    Adding a measure only ``ops`` carries is what makes it a demand: ``rev`` can no longer serve the
    recipe, ``ops`` is the only source, and ``ops`` dead-ends at ``transaction -> account`` with
    ``rev`` demonstrably realizing that hop one catalog away — ``unsanctioned_bridge``, the exact
    shape the bridge_demand queue exists to surface."""
    _far_realizer_split(db)
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("ops", "transactions", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
    ])
    _freshness(db, "ops", "rev")
    return _txn_template(extra_needs=(Need(role="amt", concept="monetary_flow"),))


def _live_build(db, *, generation_run_id: str | None, templates, mint_run: bool = True):
    intent = submit_intent(hypothesis="roll transactions up to the account", actor="ds1")
    gate1.persist_intent(db, intent)
    if generation_run_id is not None and mint_run:
        ensure_generation_run(db, generation_run_id, {"subject": "ds1"},
                              {"intake_mode": "hypothesis"}, intent_id=intent.intent_id)
    cs = build_considered_set(
        db, intent, _recommend_set_client(), catalog_source=None, is_live=True,
        target_entity="account", templates=templates, generation_run_id=generation_run_id,
        now=_NOW)
    return intent, cs


def _rows(db, table: str, order: str) -> list[dict]:
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT * FROM {table} ORDER BY {order}")   # noqa: S608 — literal constants
        return [dict(row) for row in cur.fetchall()]


def _observations(db) -> list[dict]:
    return _rows(db, "governed_planning_observation", "observation_id")


def _demands(db) -> list[dict]:
    return _rows(db, "bridge_demand_observation", "demand_id")


def test_a_live_governed_rejection_leaves_an_observation_and_its_bridge_demand(db):
    tmpl = _unsanctioned_bridge_seeds(db)
    intent, cs = _live_build(db, generation_run_id=RUN_ID, templates=(tmpl,))
    assert cs.alternatives == []          # the run really did fail to plan

    (observation,) = _observations(db)
    assert observation["observation_mode"] == "live"
    assert (observation["generation_run_id"], observation["intent_id"]) == (RUN_ID,
                                                                           intent.intent_id)
    assert observation["canonical_definition_id"] == "t_roll"
    assert observation["resolution_status"] == "unsanctioned_bridge"
    assert observation["target_entity"] == "account"
    # the S1B-3 unresolved-row identity scheme, one scheme and two writers: the sentinel is stored
    # in the row's own column as well as hashed into the id, so the id stays recomputable.
    assert observation["physical_plan_content_hash"] == "unresolved"
    assert observation["planning_request_hash"] == "legacy_template"
    assert observation["governed_variant_id"] == GovernedVariantIdentityV1(
        canonical_definition_id="t_roll", definition_origin="recipe_v2",
        planning_request_hash="legacy_template",
        physical_plan_content_hash="unresolved").governed_variant_id
    # the scope this run PLANNED under, in the same shape the telemetry lane records
    material = observation["catalog_scope_material"]
    assert material["replan_matched"] is True
    assert material["frozen"]["authorized_catalog_sources"] == ["ops", "rev"]
    assert material["frozen"]["target_entity"] == "account"

    (demand,) = _demands(db)
    assert demand["observation_id"] == observation["observation_id"]
    assert demand["demand_queue"] == "bridge_demand"
    assert demand["verdict"] == "unsanctioned_bridge"
    assert (demand["from_entity"], demand["to_entity"]) == ("transaction", "account")
    assert demand["relationship_id"] == "transaction_to_account"
    assert (demand["position_catalog"], demand["position_table_ref"]) == ("ops",
                                                                          "public.transactions")
    # the far realizer that makes this a BRIDGE demand rather than a realization gap
    assert demand["realizers"] == [{"catalog_source": "rev",
                                    "to_object_ref": "public.accounts",
                                    "from_key_ref": "public.transactions.account_id",
                                    "to_key_ref": "public.accounts.account_id"}]
    assert demand["near_side_key_refs"] == ["public.transactions.account_id"]


def test_a_resolved_live_run_records_a_resolved_row_and_no_demand(db):
    """The store's invariant on this lane: EVERY governed planning request leaves a row, whatever
    it did — and a resolved one files no demand, because nothing is missing.

    This used to assert that a resolved run recorded NOTHING, which quietly made this lane a
    failures-only writer. ``resolution_summary`` divides resolved rows by all rows across both
    lanes, so a refusals-only lane reports 0% forever (never a measurement) and drags the shared
    platform number down by exactly the planning it did successfully — which the bridge-demand
    panel then shows a human as its headline denominator."""
    _cross_seed(db)
    _, cs = _live_build(db, generation_run_id=RUN_ID, templates=(_txn_template(),))
    assert [f.name for s in cs.alternatives for f in s.features] == ["t_roll"]

    (observation,) = _observations(db)
    assert observation["observation_mode"] == "live"
    assert observation["resolution_status"] == "resolved"
    assert observation["canonical_definition_id"] == "t_roll"
    # a REAL plan hash, not the unresolved sentinel — there IS a plan, and the two lanes must agree
    # about what a resolved row looks like
    assert observation["physical_plan_content_hash"] != "unresolved"
    assert len(observation["physical_plan_content_hash"]) == 64
    assert observation["selected_physical_plan_id"].startswith("bp_")
    assert observation["contract_id"]
    assert observation["anchor_catalog_source"] in {"ops", "rev"}
    assert observation["participating_catalogs"]
    # a resolved row records no refusal: a reason code here would read as a refusal that resolved
    assert observation["reason_codes"] == []
    # ...and nothing is missing, so nobody's demand was filed
    assert _demands(db) == []


def test_an_envelope_that_cannot_project_is_a_rejection_on_BOTH_surfaces(db, monkeypatch):
    """Resolved-vs-refused is decided ONCE (``idea is not None``) and handed to the evidence
    builder. The envelope-None seam is where two predicates used to diverge: the idea builder
    fails closed when a resolved contract cannot project an envelope (served: rejected), while the
    evidence's own ``_selected_resolved_plan`` check knew nothing about envelopes and minted a
    RESOLVED ledger row carrying the real plan hash of a plan the serving refused to serve. One
    decision, two surfaces, no disagreement possible."""
    _cross_seed(db)                       # resolves — then the envelope projection is broken
    monkeypatch.setattr(gate1, "plan_envelope_from_result", lambda result: None)
    _, cs = _live_build(db, generation_run_id=RUN_ID, templates=(_txn_template(),))

    assert cs.alternatives == []          # served: fail closed, no envelope -> no option
    (rej,) = [r for r in cs.rejections if r.get("lens") == "governed"]
    (observation,) = _observations(db)
    # the ledger row is a REJECTION row: the same reason the served set gave, and the unresolved
    # sentinel — never "resolved" beside a real plan hash
    assert observation["resolution_status"] == rej["reason"]
    assert observation["physical_plan_content_hash"] == "unresolved"


def test_the_resolution_rate_over_a_mixed_live_run_is_the_real_one(db):
    """The defect this closes, measured end to end: one run, one resolved recipe and one refused,
    and the summary the panel renders reports 50% rather than 0%."""
    from featuregen.overlay.upload.governed_observation_store import resolution_summary

    refusing = _unsanctioned_bridge_seeds(db)                 # dead-ends: `ops` is the only source
    resolving = replace(_txn_template(), id="t_ok")           # `rev` serves this one intra-catalog
    _live_build(db, generation_run_id=RUN_ID, templates=(refusing, resolving))

    by_status = {row["canonical_definition_id"]: row["resolution_status"]
                 for row in _observations(db)}
    assert by_status == {"t_roll": "unsanctioned_bridge", "t_ok": "resolved"}
    # exactly one demand, from the refusal — the resolved recipe files none
    assert [row["verdict"] for row in _demands(db)] == ["unsanctioned_bridge"]

    summary = resolution_summary(db)
    assert summary["totals"] == {"observations": 2, "resolved": 1, "resolution_rate": 0.5}
    (origin,) = summary["by_origin"]
    assert (origin["definition_origin"], origin["resolution_rate"]) == ("recipe_v2", 0.5)


@pytest.mark.parametrize("run_id", [None, "grun_never_minted"])
def test_a_live_run_with_no_minted_generation_run_records_nothing(db, run_id):
    """Two ways to have no run: the caller passed none (a direct gate1 unit call), or named one that
    was never minted (1120's FK refuses it). Both leave an empty ledger and a SERVED considered set
    — a ledger write is never worth a 500, and a refused write must not leave the request's
    transaction unusable either."""
    tmpl = _unsanctioned_bridge_seeds(db)
    _, cs = _live_build(db, generation_run_id=run_id, templates=(tmpl,), mint_run=False)
    assert cs.intent_id                        # the build completed and returned its set
    assert _observations(db) == [] and _demands(db) == []
    assert db.execute("SELECT 1").fetchone()[0] == 1     # ...and the transaction still works


def test_a_poisoned_observation_store_never_fails_the_build(db, monkeypatch):
    tmpl = _unsanctioned_bridge_seeds(db)

    def _explode(conn, **kwargs):
        conn.execute("SELECT 1 FROM a_table_that_does_not_exist")

    monkeypatch.setattr(
        "featuregen.overlay.upload.governed_observation_store.record_planning_observations",
        _explode)
    _, cs = _live_build(db, generation_run_id=RUN_ID, templates=(tmpl,))
    assert cs.intent_id
    assert _observations(db) == []
    # the savepoint held: the request transaction is still usable after the poisoned write
    assert db.execute("SELECT 1").fetchone()[0] == 1
