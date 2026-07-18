"""Phase-3C.2a Task 6 — draft rebinding to the governed plan envelope + freshness recheck.

A chosen governed feature (carrying a compiled ``plan_envelope``) drafts its EXACT ``ordered_path`` —
never a recomputed permissive ``find_cross_catalog_path`` — and is refused (``StalePlan``) when that plan
has drifted. A cross-catalog feature that reached drafting with NO governed envelope is fail-closed
(``CrossCatalogPlanRequired``). A single-catalog feature with no envelope drafts exactly as before.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.planner.test_plan import _NOW, _txn_template
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import (
    CrossCatalogPlanRequired,
    StalePlan,
    draft_contract,
)
from featuregen.overlay.upload.contract.gate1 import _governed_cross_catalog_options
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.graph import build_graph


def _client() -> FakeLLM:
    return FakeLLM(script={"overlay.contract.draft": FakeResponse(output={"definition": "x"})})


def _governed_idea(db) -> FeatureIdea:
    """A REAL governed cross-catalog FeatureIdea — carries a compiled plan envelope and spans >1 catalog
    (built via the same production helper the live Gate-#1 lens uses, so the envelope is genuine)."""
    ideas, rejections = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert ideas and ideas[0].plan_envelope is not None, rejections
    assert len({cs for cs, _ref in ideas[0].derives_pairs}) > 1   # genuinely cross-catalog
    return ideas[0]


# ── (a) a governed feature drafts EXACTLY its envelope's ordered_path; _join_path is never consulted ──
def test_governed_feature_drafts_from_envelope_ordered_path(db, monkeypatch):
    _cross_seed(db)
    feature = _governed_idea(db)

    def _must_not_call(*a, **k):
        raise AssertionError("a governed feature must NOT recompute a permissive join path")

    monkeypatch.setattr("featuregen.overlay.upload.contract.author._join_path", _must_not_call)
    draft = draft_contract(db, feature, _client(), roles=())
    # the invariant: the drafted join path reconstructs the envelope's ordered_path EXACTLY
    assert tuple(s["segment"] for s in draft.join_path) == feature.plan_envelope.ordered_path
    assert draft.join_path                                   # a real cross-catalog plan has >=1 segment


# ── (b) a drifted governed plan raises StalePlan (no substitute path) ──────────────────────────────────
def test_governed_feature_with_drifted_plan_raises_stale(db):
    _cross_seed(db)
    feature = _governed_idea(db)
    # drift a classifier input on ops (mirror test_plan_envelope): the FK column's concept changes so the
    # recomputed compiler-input fingerprint no longer matches the plan's pinned stamp.
    rows = [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "customer_id"),  # was account_id
    ]
    build_graph(db, "ops", [r for r, _ in rows], concepts={content_hash(r): c for r, c in rows})
    with pytest.raises(StalePlan):
        draft_contract(db, feature, _client(), roles=())


# ── (b') the recheck runs under the PASSED roles — a governed feature is not spuriously drifted ─────────
def test_recheck_uses_the_passed_roles(db):
    _cross_seed(db)
    feature = _governed_idea(db)   # compiled under roles=()
    # the SAME role set the plan compiled under -> current -> drafts (no spurious StalePlan)
    draft = draft_contract(db, feature, _client(), roles=())
    assert tuple(s["segment"] for s in draft.join_path) == feature.plan_envelope.ordered_path


# ── (c) a cross-catalog feature with NO envelope: FLAG-ON fail-closes, FLAG-OFF draws permissive path ──
def _ungoverned_cross_feature(db) -> FeatureIdea:
    build_graph(db, "deposits", [
        CanonicalRow("deposits", "accounts", "cust_ref", "integer", entity="Customer"),
        CanonicalRow("deposits", "accounts", "balance", "numeric")])
    build_graph(db, "cards", [
        CanonicalRow("cards", "card_accounts", "cust_id", "integer", entity="Customer"),
        CanonicalRow("cards", "card_accounts", "spend", "numeric")])
    feature = FeatureIdea("cross", "", ["public.accounts.balance", "public.card_accounts.spend"],
                          "avg", "accounts",
                          derives_pairs=(("deposits", "public.accounts.balance"),
                                         ("cards", "public.card_accounts.spend")))
    assert feature.plan_envelope is None            # a cross-catalog LLM idea has no governed plan
    return feature


def test_cross_catalog_without_envelope_is_rejected_at_draft_when_live(db):
    # FLAG-ON (is_live=True): fail-closed — never a permissive find_cross_catalog_path.
    feature = _ungoverned_cross_feature(db)
    with pytest.raises(CrossCatalogPlanRequired):
        draft_contract(db, feature, _client(), roles=(), is_live=True)


def test_cross_catalog_without_envelope_draws_permissive_path_when_not_live(db):
    # FLAG-OFF (is_live default False): behaviour-neutral — the permissive entity-bridged path is authored
    # via find_cross_catalog_path exactly as before 3C.2a; HTTP 200, no CrossCatalogPlanRequired.
    feature = _ungoverned_cross_feature(db)
    draft = draft_contract(db, feature, _client(), roles=())
    assert any(step.get("kind") == "entity" and step.get("via") == "Customer"
               for step in draft.join_path)   # accounts --entity(Customer)--> card_accounts


# ── (d) a single-catalog feature with no envelope drafts EXACTLY as before (behaviour-neutral) ─────────
def test_single_catalog_feature_drafts_as_before(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("bank", "transactions", "amount", "numeric"),
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True)])
    feature = FeatureIdea("txn_count", "", ["public.transactions.amount"], "count", "accounts",
                          derives_pairs=(("bank", "public.transactions.amount"),))
    assert feature.plan_envelope is None
    draft = draft_contract(db, feature, _client(), roles=())
    assert draft.join_path                                   # the column-level path is still authored
    step = draft.join_path[0]
    assert step["kind"] == "join"                            # the permissive single-catalog shape, unchanged
    assert "accounts" in (step["from"] + step["to"])
