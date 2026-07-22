"""Delivery C0 Task 6 — full end-to-end snapshot LINEAGE from considered-set to the confirmed contract.

The C0-T5 builder tests (test_considered_set_snapshot.py) prove a REPEATABLE READ considered-set build
persists a real snapshot + lineage, and that ``considered_snapshot_lineage`` reloads the SERVER value.
The API-level test (api/test_contract_considered_snapshot.py) proves draft/confirm reload that lineage
over HTTP — but under the READ COMMITTED shared API connection it seeds the lineage by hand (no real
builder snapshot). This suite closes the gap between them: it drives the WHOLE chain on ONE REPEATABLE
READ feature-generation connection — considered-set → Gate #1 choice → draft → confirm — and asserts a
SINGLE snapshot's lineage (the run + immutable snapshot the builder actually minted) threads unchanged
from ``contract_considered`` all the way to a registered, versioned governed contract (Slice-3 intact).

Also (TEST 3): two considered-set builds over identical committed catalog state seal the SAME
``content_hash`` (distinct snapshot_ids) — determinism THROUGH the considered-set path (read-scope hash
+ candidate-ref derivation), a surface the C0-T3 builder-level determinism test does not exercise.
"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from tests.featuregen._helpers import mint_test_identity

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import draft_contract
from featuregen.overlay.upload.contract.gate1 import (
    build_considered_set,
    chosen_feature,
    considered_snapshot_lineage,
    intent_target_ref,
    record_gate1_choice,
)
from featuregen.overlay.upload.contract.govern import confirm_contract
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.contract.review import author_contract
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=UTC)
_TARGET = "public.accounts.churned"


def _actor():
    """An authenticated feature-engineer identity — draft/author take an ``IdentityEnvelope`` (the
    audited LLM seam reads ``actor.subject``), so a bare subject string is not enough there."""
    return mint_test_identity(subject="user:ds1", role_claims=("feature_engineer",))


def _rr(db) -> None:
    """Pin REPEATABLE READ BEFORE the first query — the C0-T2 feature-generation connection the route
    uses. The snapshot is taken ONLY on a REPEATABLE READ connection."""
    db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def _bank(db) -> None:
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "churned", "boolean")])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (NOW, NOW))


def _client() -> FakeLLM:
    """Serves every task the whole considered-set → draft → confirm chain dispatches: set generation +
    recommendation, the contract-draft narrative, and a clean critique (empty findings → the
    critique→refine loop returns clean on the first pass, no refine)."""
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Average 90-day end-of-day ledger balance per account."}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


# ── TEST 2 — the whole chain threads ONE snapshot's lineage through to the confirmed contract ───────
def test_snapshot_lineage_threads_from_considered_set_to_confirmed_contract(db):
    _rr(db)
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition="90-day average balance per account", actor="ds1")
    client = _client()

    # 1. Considered set on the RR feature-gen connection: mints the run, snapshots the in-scope catalog
    #    state, records the lineage on contract_considered (the builder's real snapshot — not seeded).
    cs = build_considered_set(db, intent, client, catalog_source="bank", target_ref=_TARGET,
                              now=NOW, generation_run_id="fgr_e2e")
    assert cs.anchor is not None and cs.anchor.name == "avg_balance_90d"

    considered = db.execute(
        "SELECT generation_run_id, snapshot_id, snapshot_content_hash "
        "FROM contract_considered WHERE intent_id = %s", (intent.intent_id,)).fetchone()
    run_id, snap_id, snap_hash = considered
    assert run_id == "fgr_e2e"
    assert snap_id and snap_hash

    # 2. A real immutable snapshot header + items exist for that run (the balance ref the set derives
    #    from is captured), and the run manifest FK-parent exists.
    header = db.execute(
        "SELECT snapshot_id, content_hash FROM catalog_metadata_snapshot "
        "WHERE generation_run_id = %s", ("fgr_e2e",)).fetchone()
    assert header == (snap_id, snap_hash)
    assert db.execute("SELECT 1 FROM feature_generation_run WHERE generation_run_id = %s",
                      ("fgr_e2e",)).fetchone() is not None
    (n_items,) = db.execute(
        "SELECT count(*) FROM catalog_metadata_snapshot_item "
        "WHERE snapshot_id = %s AND catalog_source = 'bank' AND graph_ref = %s",
        (snap_id, "public.accounts.balance")).fetchone()
    assert n_items > 0

    # 3. Gate #1 choice → draft: draft reloads the SERVER lineage (never a client id). Reconstruct the
    #    chosen feature from the server considered set exactly as the /contract/draft route does.
    actor = _actor()
    record_gate1_choice(db, intent.intent_id, chosen_source="anchor",
                        chosen_option_id="avg_balance_90d", actor=actor, why="best fit")
    feature = chosen_feature(db, intent.intent_id, "anchor", "avg_balance_90d")
    assert feature is not None
    target = intent_target_ref(db, intent.intent_id)   # SERVER truth
    assert target == _TARGET

    draft = draft_contract(db, feature, client, roles=actor.role_claims, target_ref=target,
                           actor=actor)
    draft, unresolved = author_contract(db, draft, client, now=NOW, actor=actor)
    lineage_at_draft = considered_snapshot_lineage(db, intent.intent_id)
    assert lineage_at_draft == {
        "generation_run_id": run_id, "snapshot_id": snap_id, "content_hash": snap_hash}

    # 4. Confirm reloads the SAME server lineage and registers a versioned governed contract (Slice-3
    #    flow intact) — the lineage the regulator can bind the governing write to is unchanged from the
    #    one the considered set was authored against.
    lineage_at_confirm = considered_snapshot_lineage(db, intent.intent_id)
    assert lineage_at_confirm == lineage_at_draft
    contract = confirm_contract(db, draft, actor=actor.subject, roles=actor.role_claims, now=NOW,
                                target_ref=target, intent_id=intent.intent_id)
    assert contract.version == 1
    assert contract.feature_id.startswith("feat")
    assert contract.contract_id.startswith("contract")

    # The registered contract row is bound to the same intent (its audit anchor back to the snapshot).
    row = db.execute(
        "SELECT feature_id, version, intent_id FROM contract WHERE contract_id = %s",
        (contract.contract_id,)).fetchone()
    assert row == (contract.feature_id, 1, intent.intent_id)


# ── TEST 3 — determinism THROUGH the considered-set path (not just at the builder) ──────────────────
def test_two_considered_sets_over_identical_state_seal_the_same_content_hash(db):
    """Two considered-set builds over the SAME committed catalog produce snapshots with an identical
    ``content_hash`` (distinct ``snapshot_id``s). C0-T3 proves this at the builder level directly; this
    proves the considered-set path's read-scope-hash + candidate-ref derivation is deterministic too."""
    _rr(db)
    _bank(db)
    client = _client()

    hashes: list[str] = []
    ids: list[str] = []
    for i in range(2):
        intent = submit_intent(hypothesis="customers churn when their balance drops",
                               definition="90-day average balance per account", actor="ds1")
        build_considered_set(db, intent, client, catalog_source="bank", target_ref=_TARGET,
                             now=NOW, generation_run_id=f"fgr_det_{i}")
        row = db.execute(
            "SELECT snapshot_id, snapshot_content_hash FROM contract_considered WHERE intent_id = %s",
            (intent.intent_id,)).fetchone()
        ids.append(row[0])
        hashes.append(row[1])

    assert ids[0] != ids[1]          # a fresh snapshot id per build
    assert hashes[0] == hashes[1]    # identical committed state ⇒ identical seal (deterministic)


# ── TEST 4 (MF-3) — a broaden AFTER confirm cannot repoint the confirmed contract's snapshot binding ──
def test_broaden_after_confirm_does_not_repoint_contract_snapshot_binding(db):
    """MF-3: the confirmed contract is bound to ITS snapshot on the write-once contract row. A subsequent
    considered-set rebuild for the SAME intent (a broaden) repoints the MUTABLE
    contract_considered.snapshot_id S1->S2, but the already-confirmed contract's ``metadata_snapshot_id``
    must NOT move — a regulator reconstructs the authored-against snapshot from the contract, not the
    mutable considered-set pointer."""
    _rr(db)
    _bank(db)
    client = _client()
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition="90-day average balance per account", actor="ds1")

    # 1. Considered set S1 → Gate #1 choice → draft → confirm. The contract binds to S1's snapshot.
    build_considered_set(db, intent, client, catalog_source="bank", target_ref=_TARGET, now=NOW,
                         generation_run_id="fgr_s1")
    s1 = db.execute("SELECT snapshot_id FROM contract_considered WHERE intent_id = %s",
                    (intent.intent_id,)).fetchone()[0]
    assert s1

    actor = _actor()
    record_gate1_choice(db, intent.intent_id, chosen_source="anchor",
                        chosen_option_id="avg_balance_90d", actor=actor, why="best fit")
    feature = chosen_feature(db, intent.intent_id, "anchor", "avg_balance_90d")
    assert feature is not None
    target = intent_target_ref(db, intent.intent_id)
    draft = draft_contract(db, feature, client, roles=actor.role_claims, target_ref=target, actor=actor)
    draft, _ = author_contract(db, draft, client, now=NOW, actor=actor)
    contract = confirm_contract(db, draft, actor=actor.subject, roles=actor.role_claims, now=NOW,
                                target_ref=target, intent_id=intent.intent_id)

    bound = db.execute(
        "SELECT metadata_snapshot_id, metadata_content_hash FROM contract WHERE contract_id = %s",
        (contract.contract_id,)).fetchone()
    assert bound[0] == s1 and bound[1]   # the confirmed contract is durably bound to S1's snapshot

    # 2. Broaden: rebuild the considered set for the SAME intent → the MUTABLE pointer repoints S1->S2.
    build_considered_set(db, intent, client, catalog_source="bank", target_ref=_TARGET, now=NOW,
                         generation_run_id="fgr_s2")
    s2 = db.execute("SELECT snapshot_id FROM contract_considered WHERE intent_id = %s",
                    (intent.intent_id,)).fetchone()[0]
    assert s2 and s2 != s1   # the considered-set pointer moved to a fresh snapshot

    # 3. The already-confirmed contract's binding is UNCHANGED — immutable against the later broaden.
    still = db.execute("SELECT metadata_snapshot_id FROM contract WHERE contract_id = %s",
                       (contract.contract_id,)).fetchone()[0]
    assert still == s1
