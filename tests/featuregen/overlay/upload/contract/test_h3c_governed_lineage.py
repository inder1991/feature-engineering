"""Delivery H3c — the governed contract's FULL physical read set as role-labelled lineage + the
confirm-time plan rebuild/revalidation (stable ids) + the 3B.3c recipe-only gating + the startup 3C.2
artifact check.

The headline closure: a GOVERNED (planner-authored) contract's join keys / bridge keys / anchors — the
compiler's ``build_physical_read_set`` — are now persisted as ``contract_input_column`` +
``contract_metadata_dependency`` rows, so the H2c read gate downgrades a promoted governed contract when
ANY of them drifts (the H2 review's C-1 gap, closed for planner-authored contracts). Confirm also
REBUILDS the pinned plan and requires the SAME physical_plan_id + planner_declaration_id + a ``current``
freshness verdict — drift → ``GovernedPlanDrift`` (the route 409s). ``physical_plan_id`` MINTING is never
touched: the rebuild reproduces the SAME id for an unchanged snapshot (that reproduction is the check).
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.overlay.upload.planner.test_plan import _NOW, _txn_template
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.gate1 import _governed_cross_catalog_options
from featuregen.overlay.upload.contract.govern import confirm_contract
from featuregen.overlay.upload.contract.governed_plan import (
    GovernedPlanDrift,
    revalidate_governed_plan,
)
from featuregen.overlay.upload.contract.invalidation import dependencies_drifted


# ── shared fixture: a resolved governed cross-catalog plan + its envelope ──────────────────────────────
def _governed_idea(db):
    """Seed ops+rev+a VERIFIED bridge+fresh watermarks, run the governed planner, and return the single
    resolved governed FeatureIdea (carrying the exact compiled plan envelope)."""
    _cross_seed(db)
    ideas, rejections = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert len(ideas) == 1 and not rejections
    return ideas[0]


def _governed_draft(name="governed_rollup"):
    """A draft whose derives_pairs carry ONLY the ingredient (transaction_id) — deliberately NOT the
    join/bridge KEYS (account_id). Without the H3c read-set lineage those keys would be invisible to the
    read gate; with it they are recorded, so drift on them downgrades (the C-1 closure)."""
    return ContractDraft(
        feature_name=name, definition="Rolled-up transaction count per account.", grain_table=None,
        aggregation="sum", as_of_column=None,
        derives_from=["public.transactions.transaction_id"],
        derives_pairs=(("ops", "public.transactions.transaction_id"),))


def _deps(db, contract_id):
    return {(r[0], r[1]) for r in db.execute(
        "SELECT catalog_source, logical_ref FROM contract_metadata_dependency WHERE contract_id = %s",
        (contract_id,)).fetchall()}


def _inputs(db, contract_id):
    return db.execute(
        "SELECT source, physical_ref, role, item_hash FROM contract_input_column "
        "WHERE contract_id = %s ORDER BY role, physical_ref", (contract_id,)).fetchall()


# ══════════ TEST 1 — the FULL read set is persisted as role-labelled lineage (input + dependency) ══════
def test_governed_confirm_persists_full_read_set_as_role_labelled_lineage(db):
    idea = _governed_idea(db)
    env = idea.plan_envelope
    c = confirm_contract(db, _governed_draft(), actor="ds1", roles=(), now=_NOW,
                         plan_envelope=env, templates=(_txn_template(),))

    # the physical read set for this plan is: transaction_id (ingredient+join_key) + account_id in BOTH
    # catalogs (bridge_key). Every STRUCTURAL column lands as a role-labelled 'join_key' input row.
    inputs = _inputs(db, c.contract_id)
    join_key_rows = [r for r in inputs if r[2] == "join_key"]
    join_key_refs = {(r[0], r[1]) for r in join_key_rows}
    assert ("ops", "public.transactions.account_id") in join_key_refs       # bridge key (NOT in derives)
    assert ("rev", "public.accounts.account_id") in join_key_refs           # far bridge endpoint
    assert ("ops", "public.transactions.transaction_id") in join_key_refs   # source entity key
    assert all(r[3] for r in join_key_rows)                                  # every row has an item_hash

    # …and each is ALSO a contract_metadata_dependency row (the read gate's drift surface).
    deps = _deps(db, c.contract_id)
    assert ("ops", "public.transactions.account_id") in deps
    assert ("rev", "public.accounts.account_id") in deps

    # the planner provenance is persisted on the governed contract row (was NULL pre-H3c).
    row = db.execute("SELECT generation_source, recipe_id, physical_plan_id, planner_declaration_id "
                     "FROM contract WHERE contract_id = %s", (c.contract_id,)).fetchone()
    assert row == ("recipe", "t_roll", env.physical_plan_id, env.contract_id)


# ══════════ TEST 2 — the read gate now COVERS a planner join-key drift (the C-1 closure) ══════════════
def test_read_gate_covers_governed_join_key_drift(db):
    idea = _governed_idea(db)
    c = confirm_contract(db, _governed_draft(), actor="ds1", roles=(), now=_NOW,
                         plan_envelope=idea.plan_envelope, templates=(_txn_template(),))

    # the bridge/join key account_id is a recorded dependency and nothing has drifted yet.
    assert ("ops", "public.transactions.account_id") in _deps(db, c.contract_id)
    assert dependencies_drifted(db, c.contract_id) is False

    # RETYPE the join key in place (a dropped/retyped join key). No INVALIDATED event is emitted.
    db.execute("UPDATE graph_node SET data_type = 'text' "
               "WHERE catalog_source = 'ops' AND object_ref = 'public.transactions.account_id'")
    # the read gate SEES the planner lineage: the join-key drift is detected (C-1 gap closed).
    assert dependencies_drifted(db, c.contract_id) is True


def test_without_read_set_lineage_the_join_key_drift_is_invisible(db):
    """The CONTRAST that proves the closure is the read-set lineage (not derives): the SAME draft
    confirmed WITHOUT a plan_envelope records no join-key dependency, so drifting the join key is
    undetectable — exactly the C-1 gap H3c fills for governed contracts."""
    _cross_seed(db)   # same catalog, but a plain (non-governed) confirm — no read-set lineage
    c = confirm_contract(db, _governed_draft("ungoverned_rollup"), actor="ds1", roles=(), now=_NOW)

    assert ("ops", "public.transactions.account_id") not in _deps(db, c.contract_id)  # never recorded
    db.execute("UPDATE graph_node SET data_type = 'text' "
               "WHERE catalog_source = 'ops' AND object_ref = 'public.transactions.account_id'")
    assert dependencies_drifted(db, c.contract_id) is False   # blind to the join-key drift (the gap)


# ══════════ TEST 3 — confirm-time revalidation: stable ids required, drift → GovernedPlanDrift ═════════
def test_unchanged_snapshot_confirms_and_reproduces_the_same_ids(db):
    idea = _governed_idea(db)
    env = idea.plan_envelope
    # the rebuild reproduces the EXACT pinned ids (physical_plan_id MINTING untouched — COMPARE, not mint)
    plan, read_set = revalidate_governed_plan(db, env, roles=(), now=_NOW, templates=(_txn_template(),))
    assert plan.physical_plan_id == env.physical_plan_id
    assert plan.contract_id == env.contract_id
    assert read_set.columns   # the full read set is returned for lineage persistence
    # …and the governed confirm succeeds end-to-end.
    c = confirm_contract(db, _governed_draft(), actor="ds1", roles=(), now=_NOW,
                         plan_envelope=env, templates=(_txn_template(),))
    assert c.contract_id


def test_tampered_physical_plan_id_is_rejected_at_confirm(db):
    idea = _governed_idea(db)
    # freshness stays current (fingerprints untouched); only the pinned physical_plan_id is wrong, so the
    # rebuild reproduces the REAL id != the tampered one → drift. This isolates the STABLE-ID check.
    bad = dataclasses.replace(idea.plan_envelope, physical_plan_id="bp_tampered000000")
    with pytest.raises(GovernedPlanDrift):
        confirm_contract(db, _governed_draft(), actor="ds1", roles=(), now=_NOW,
                         plan_envelope=bad, templates=(_txn_template(),))
    # fail-closed BEFORE any write: no contract row was created.
    assert db.execute("SELECT count(*) FROM contract").fetchone()[0] == 0


def test_tampered_declaration_id_is_rejected_at_confirm(db):
    idea = _governed_idea(db)
    bad = dataclasses.replace(idea.plan_envelope, contract_id="cc_tampered0000000")
    with pytest.raises(GovernedPlanDrift):
        confirm_contract(db, _governed_draft(), actor="ds1", roles=(), now=_NOW,
                         plan_envelope=bad, templates=(_txn_template(),))


def test_snapshot_drift_between_generation_and_confirm_is_rejected(db):
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.enrich import content_hash
    from featuregen.overlay.upload.graph import build_graph
    idea = _governed_idea(db)
    # change a classifier input on ops (the FK column's concept) AFTER generation — the plan drifted.
    rows = [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "customer_id"),  # was account_id
    ]
    build_graph(db, "ops", [r for r, _ in rows], concepts={content_hash(r): c for r, c in rows})
    with pytest.raises(GovernedPlanDrift):
        confirm_contract(db, _governed_draft(), actor="ds1", roles=(), now=_NOW,
                         plan_envelope=idea.plan_envelope, templates=(_txn_template(),))


# ══════════ TEST 4 — 3B.3c recipe-only gating + planner_applicability recording ═══════════════════════
def test_single_catalog_recipe_records_not_applicable_single_catalog(db):
    """A single-catalog recipe (grounded template) preserves recipe_id and records
    ``not_applicable_single_catalog`` — it rides the single-catalog validator (``_validate_idea``), never
    the 3B.3c compile (``plan_bindings``). Tested at ``_template_candidates`` (the grounding lens that
    server-stamps the applicability) with a fixture recipe, so it does not depend on which registry
    family grounds on a given catalog."""
    from tests.featuregen.overlay.upload.planner.test_plan import _catalog, _tmpl

    from featuregen.overlay.upload.contract.gate1 import _template_candidates
    _catalog(db, "core")   # customer_id grain + a monetary_stock balance — grounds t_bal
    ideas, _rej, grounded_ids, _rejected, _bq = _template_candidates(
        db, catalog_source="core", roles=(), target_ref=None, now=_NOW, templates=(_tmpl(),))
    assert ideas and grounded_ids == frozenset({"t_bal"})   # the single-catalog recipe survives grounding
    for f in ideas:
        assert f.generation_source == "recipe"
        assert f.recipe_id == "t_bal"                        # recipe_id preserved through Gate #1
        assert f.planner_applicability == "not_applicable_single_catalog"
        assert f.plan_envelope is None                       # single-catalog: NO governed plan (no 3B.3c)


def test_single_catalog_run_never_invokes_3b3c_compile(db, monkeypatch):
    """STRUCTURAL: a single-catalog (``catalog_source`` set) considered-set build takes the grounding
    lens, NEVER the entity-scoped governed lens — so ``_governed_cross_catalog_options`` (the ONLY caller
    of the 3B.3c ``plan_bindings`` compile) is never reached. The boom-that-never-fires is the proof."""
    from tests.featuregen.overlay.upload.planner.test_plan import _catalog

    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.upload.contract.gate1 import build_considered_set
    from featuregen.overlay.upload.contract.intake import submit_intent
    from featuregen.overlay.upload.feature_assist import SetsReport
    _catalog(db, "core")
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1.recommend_feature_sets_report",
                        lambda *a, **k: SetsReport(sets=[], rejections=[]))
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1._governed_cross_catalog_options",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("3B.3c must not run on a single-catalog run")))
    client = FakeLLM(script={"overlay.feature.recommend_set": FakeResponse(output={
        "recommended_lens": "templates", "reasoning": "advisory"})})
    intent = submit_intent(hypothesis="balances shape churn", actor="ds1")
    cs = build_considered_set(db, intent, client, catalog_source="core", entity=None, now=_NOW)
    assert cs is not None   # completed without ever entering the governed 3B.3c lens


def test_free_form_idea_is_nonrecipe_and_never_enters_3b3c():
    """A free-form / user-defined FeatureIdea carries the ``not_applicable_nonrecipe`` default and no
    recipe_id — it can never resolve a Template, so it never reaches the 3B.3c compile."""
    from featuregen.overlay.upload.feature_assist import FeatureIdea
    f = FeatureIdea("free", "", ["public.t.a"], "sum", "t",
                    derives_pairs=(("bank", "public.t.a"),))
    assert f.planner_applicability == "not_applicable_nonrecipe"
    assert f.generation_source == "llm_freeform"
    assert f.recipe_id is None and f.plan_envelope is None


# ══════════ TEST 5 — the startup / runtime 3C.2 artifact verifier ═════════════════════════════════════
FLAG = "FEATUREGEN_INTENT_LIVE_CROSS_CATALOG"
KEY = "FEATUREGEN_INTENT_GATE_PUBLIC_KEY"
ART = "FEATUREGEN_INTENT_GATE_ARTIFACT"


def test_startup_check_flag_off_is_inert(monkeypatch):
    from featuregen.overlay.upload.contract.live_activation import startup_artifact_check
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv(KEY, raising=False)
    assert startup_artifact_check() is True   # flag off → nothing gated


def test_startup_check_flag_on_no_key_is_inert(monkeypatch):
    """No-key posture: signed-gate enforcement is not deployed → the prong is inert (True); the durable
    activation interlock alone gates. Explicit + logged, never a silent fail-open."""
    from featuregen.overlay.upload.contract.live_activation import startup_artifact_check
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.delenv(KEY, raising=False)
    assert startup_artifact_check() is True


def test_startup_check_flag_on_with_key_but_absent_artifact_fails_closed(monkeypatch, caplog):
    """A trusted key IS configured (signed-gate enforcement deployed) but there is no artifact → the
    startup check fails closed (False) and logs the loud warning — the early fail-closed signal."""
    import logging

    from featuregen.overlay.upload.contract.live_activation import startup_artifact_check
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(KEY, "-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----")
    monkeypatch.delenv(ART, raising=False)
    with caplog.at_level(logging.WARNING):
        assert startup_artifact_check() is False
    assert any("FAIL-CLOSED" in r.message or "fail" in r.message.lower() for r in caplog.records)
