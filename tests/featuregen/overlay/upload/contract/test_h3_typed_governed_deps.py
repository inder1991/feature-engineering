"""H3 review fix (I-1 + I-3) — TYPED governed-segment dependency markers.

The H3c read-set lineage recorded a governed cross-catalog plan's BRIDGE-FACT (``bfk_*``) and
REALIZATION (``{catalog}:{from}->{to}``) refs as ``graph_node`` dependencies. They are NOT graph nodes,
so the H2c read gate resolved them to ``MISSING`` at confirm → the ``_UNRESOLVED_AT_CONFIRM`` poison →
``dependencies_drifted`` was True immediately and FOREVER → no production governed contract could ever
serve a promoted stamp (the C-1 closure was VACUOUS in production: its tests used an empty join_path,
bypassing the ``routes/contract.py`` envelope→ordered_path rewrite that records these refs).

The fix records them as TYPED markers (``bridgefact:`` / ``realization:``) whose read-gate resolver
hashes the RIGHT authoritative state — the bridge's VERIFIED sanction in ``entity_bridge_edge`` and the
realization's cardinality/authority in ``graph_edge`` (mirroring the ``joinedge:`` pattern). They RESOLVE
at confirm (no poison → PROMOTABLE) and CHANGE on bridge-revocation / join-key drift (DETECTABLE).

The headline proof drives the REAL route (considered-set → draft → confirm, the envelope→ordered_path
rewrite), NOT an empty join_path.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_contract_live_cross_catalog import (
    DEP,
    FLAG,
    _approve,
    _flow_llm,
    _fresh_now,
    _governed_scoped_body,
    _inject_fixture_template,
)
from tests.featuregen.overlay.upload.contract.test_h3c_governed_lineage import _governed_draft
from tests.featuregen.overlay.upload.planner.test_plan import _NOW, _txn_template
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

from featuregen.api.app import create_app
from featuregen.api.deps import get_conn, get_feature_gen_conn
from featuregen.overlay.upload.catalog_realizations import derive_catalog_realizations
from featuregen.overlay.upload.contract.author import ContractDraft, _envelope_join_path
from featuregen.overlay.upload.contract.gate1 import _governed_cross_catalog_options
from featuregen.overlay.upload.contract.govern import (
    _apply_dependency_read_gate,
    _contract_dependency_items,
    confirm_contract,
    contract_read_status,
)
from featuregen.overlay.upload.contract.invalidation import (
    _MISSING,
    _bridge_fact_signature,
    _catalog_state_signature,
    _realization_signature,
    bridge_fact_marker,
    confirm_dependency_hash,
    current_dependency_hash,
    dependencies_drifted,
    realization_marker,
)

# A hypothetically-PROMOTED stamp: the read gate passes it through only while nothing drifted, and HARD-
# downgrades it the moment a dependency drifts. The natural governed cross-catalog contract is
# needs_external_validation (its cross-catalog join carries an external requirement), so we probe the
# promotion behaviour with this synthetic promoted stamp — the exact ``design_checked`` branch
# ``contract_read_status`` gates on.
_PROMOTED = ("design_checked", "DATA-CHECKED")
_DOWNGRADED = ("needs_external_validation", "UNVERIFIED")

#: The ``contract_metadata_dependency.item_hash`` a healthy VERIFIED, projected bridge produced
#: BEFORE the lifecycle-first fold — captured from ``origin/main`` at 265ce4be. Pinned as a literal
#: because that table is WORM (migration 1011 rejects UPDATE/DELETE): a signature change cannot be
#: migrated, it can only condemn every governed contract already confirmed against it.
_HEALTHY_VERIFIED_ITEM_HASH = (
    "30ca22189daa8e95e577cb95376ad6ff3100aa34d6901490a40c8db1e4e664ce")


@pytest.fixture
def client(db, monkeypatch):
    """A TestClient on the suite's rolled-back connection (mirrors test_no_permissive_path_when_live)."""
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")
    # This governed-dependency suite isolates H3 behavior; recognition lineage is covered by
    # Delivery 0 API tests and remains required by default in production.
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "legacy_unscoped")
    app = create_app(llm_client=_flow_llm())

    def _test_conn():
        yield db

    app.dependency_overrides[get_conn] = _test_conn
    app.dependency_overrides[get_feature_gen_conn] = _test_conn
    with TestClient(app) as c:
        yield c


def _enable_live(db, monkeypatch) -> None:
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(db)


def _inject_confirm_template(monkeypatch) -> None:
    """The route's ``/contract/confirm`` rebuilds the pinned governed plan through
    ``revalidate_governed_plan``, which falls back to the production ``ALL_TEMPLATES`` registry. In
    production the recipe IS in that registry; the planner-fixture recipe (``t_roll``) is not, so — with
    the I-2 fail-closed on a not-rebuildable plan — the confirm would 409. Inject the fixture recipe into
    the registry the confirm-time rebuild reads, exactly as ``_inject_fixture_template`` does for the
    considered-set stage, so the confirm REBUILDS + revalidates (the production path) and records the full
    read-set lineage."""
    monkeypatch.setattr("featuregen.overlay.upload.contract.governed_plan.ALL_TEMPLATES",
                        (_txn_template(),))


def _deps(db, contract_id):
    return {(r[0], r[1]) for r in db.execute(
        "SELECT catalog_source, logical_ref FROM contract_metadata_dependency WHERE contract_id = %s",
        (contract_id,)).fetchall()}


# ══════════ HEADLINE — the FULL route: promotable (poison gone) + bridge-revocation downgrade ══════════
def test_route_governed_confirm_is_promotable_and_bridge_revocation_downgrades(client, db, monkeypatch):
    """Over the REAL route (considered-set → draft → confirm, the envelope→ordered_path rewrite): the
    governed cross-catalog contract's bridge segment is recorded as a ``bridgefact:`` TYPED marker (NOT a
    raw ``bfk_*`` graph_node), so at confirm the read gate RESOLVES it (no poison) — the contract is
    PROMOTABLE. Rejecting the authoritative bridge lifecycle drifts the marker even when its stale
    projection row survives → a promoted stamp HARD-downgrades."""
    _enable_live(db, monkeypatch)
    _cross_seed(db)                      # ops + rev + a VERIFIED bridge → a resolvable cross-catalog plan
    _fresh_now(db, "ops", "rev")
    _inject_fixture_template(monkeypatch)
    _inject_confirm_template(monkeypatch)

    res = client.post("/contract/considered-set", json=_governed_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_source": "alternative",
        "chosen_option_id": "t_roll", "why": "governed cross-catalog",
        "expected_generation_run_id": body["generation_run_id"]}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = body["intent_id"]
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200, cr.text
    cid = cr.json()["contract_id"]

    # the bridge segment is a TYPED marker dep, NOT a raw bfk graph_node dep (the poison ref).
    deps = _deps(db, cid)
    bridge_markers = {ref for _cs, ref in deps if ref.startswith("bridgefact:")}
    assert bridge_markers, deps
    assert not any(ref == "bfk_cap" for _cs, ref in deps)   # the raw poison ref is gone

    # POISON GONE: nothing has drifted at confirm → a PROMOTED stamp is served (promotable).
    assert dependencies_drifted(db, cid) is False
    assert _apply_dependency_read_gate(db, cid, *_PROMOTED) == _PROMOTED

    # REVOKE authoritatively while deliberately leaving the fail-soft projection behind.
    _draft_bridge(db, "bfk_cap", status="REJECTED")
    assert db.execute(
        "SELECT count(*) FROM entity_bridge_edge WHERE fact_key='bfk_cap'").fetchone()[0] == 1

    # the marker now resolves MISSING → drift detected → the promoted stamp HARD-downgrades (I-3).
    assert dependencies_drifted(db, cid) is True
    assert _apply_dependency_read_gate(db, cid, *_PROMOTED) == _DOWNGRADED
    # contract_read_status folds the same gate on top of the projection stamp (fail-closed).
    assert contract_read_status(db, cid) == _DOWNGRADED


def test_route_governed_confirm_join_key_retype_downgrades(client, db, monkeypatch):
    """Same REAL-route governed confirm: retyping a physical JOIN KEY the plan reads drifts its read-set
    graph_node dep → a promoted stamp HARD-downgrades (the C-1 join-key coverage, now non-vacuous because
    the confirm is over the route's real ordered_path, not an empty join_path)."""
    _enable_live(db, monkeypatch)
    _cross_seed(db)
    _fresh_now(db, "ops", "rev")
    _inject_fixture_template(monkeypatch)
    _inject_confirm_template(monkeypatch)
    res = client.post("/contract/considered-set", json=_governed_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_source": "alternative",
        "chosen_option_id": "t_roll", "why": "",
        "expected_generation_run_id": body["generation_run_id"]}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = body["intent_id"]
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200, cr.text
    cid = cr.json()["contract_id"]

    assert ("ops", "public.transactions.account_id") in _deps(db, cid)   # a recorded join key
    assert dependencies_drifted(db, cid) is False
    assert _apply_dependency_read_gate(db, cid, *_PROMOTED) == _PROMOTED

    db.execute("UPDATE graph_node SET data_type = 'text' "
               "WHERE catalog_source = 'ops' AND object_ref = 'public.transactions.account_id'")
    assert dependencies_drifted(db, cid) is True
    assert _apply_dependency_read_gate(db, cid, *_PROMOTED) == _DOWNGRADED


# ══════════ govern-level: the route rewrite records a bridgefact marker that RESOLVES then drifts ══════
def test_govern_bridge_marker_resolves_at_confirm_then_drifts_on_revocation(db):
    """The govern layer applied to the SERVER route rewrite (``_envelope_join_path`` on the compiled
    ordered_path — the exact transform ``routes/contract.py`` applies): the confirmed governed contract's
    bridge segment lands as a ``bridgefact:`` dep, ``dependencies_drifted`` is False at confirm (RESOLVES,
    no poison), and authoritatively rejecting the bridge flips it True even if the projection remains."""
    _cross_seed(db)
    ideas, rej = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert len(ideas) == 1, rej
    env = ideas[0].plan_envelope
    draft = replace(_governed_draft(), join_path=tuple(_envelope_join_path(env.ordered_path)))
    c = confirm_contract(db, draft, actor="ds1", roles=(), now=_NOW,
                         plan_envelope=env, templates=(_txn_template(),))
    assert ("rev", "bridgefact:bfk_cap") in _deps(db, c.contract_id)   # TYPED marker recorded
    assert dependencies_drifted(db, c.contract_id) is False            # RESOLVES at confirm (no poison)
    _draft_bridge(db, "bfk_cap", status="REJECTED")
    assert db.execute(
        "SELECT count(*) FROM entity_bridge_edge WHERE fact_key='bfk_cap'").fetchone()[0] == 1
    assert dependencies_drifted(db, c.contract_id) is True             # revocation → drift


# ══════════ resolver unit tests — the bridge/realization signatures hash the RIGHT state ══════════════
def test_bridge_fact_signature_resolves_verified_and_missing_on_revoke(db):
    """Projection loss alone does not revoke a bridge; the authoritative lifecycle does."""
    _cross_seed(db)
    marker = bridge_fact_marker("bfk_cap")
    sig = _bridge_fact_signature(db, marker)
    assert sig != _MISSING and sig["status"] == "VERIFIED" and sig["bridge"] is True
    # routed identically through the public entry point the read gate uses:
    assert _catalog_state_signature(db, "rev", marker) == sig
    db.execute("DELETE FROM entity_bridge_edge WHERE fact_key = 'bfk_cap'")
    assert _bridge_fact_signature(db, marker) == sig
    _draft_bridge(db, "bfk_cap", status="REJECTED")
    assert _bridge_fact_signature(db, marker) == _MISSING


# ══════════ an AVAILABLE bridge must not be poisoned by the absence of a human review ═════════════
#
# `_bridge_fact_signature` reads `entity_bridge_edge`, which holds VERIFIED bridges ONLY. Since a
# link became available whether or not anybody had reviewed it, a governed plan could legitimately
# cross a DRAFT bridge — and then the read gate resolved that segment to MISSING at confirm, took the
# `_UNRESOLVED_AT_CONFIRM` poison, and the contract was permanently un-promotable. That is human
# review deciding contract eligibility through the back door, and it is the same defect the
# availability allow-list fixes on the traversal side.
#
# The bounded fix is additive: when there is NO projection row, fall back to the governed lifecycle.
# An AVAILABLE bridge resolves (no poison); an unavailable one stays MISSING. The VERIFIED-with-a-row
# path is untouched, byte for byte, so no stored `item_hash` is re-baselined.
#
# What is NOT done here — and is recorded in the module docstring — is the real target: keying on the
# DIRECTIONAL REALIZATION revision plus deterministic evidence rather than on a governance status at
# all. That needs a concept the codebase does not have yet.

def _draft_bridge(db, fact_key, *, status="DRAFT"):
    from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact

    from featuregen.events.registry import event_registry
    from featuregen.overlay.facts import register_overlay_event_types
    register_overlay_event_types(event_registry())
    return govern_bridge_fact(db, fact_key, entity="account", left_source="ops",
                              left_ref="public.transactions.account_id", right_source="rev",
                              right_ref="public.accounts.account_id", status=status)


def test_control_a_verified_bridge_signature_is_unchanged_byte_for_byte(db):
    """MUST-SURVIVE no-op control, and the no-re-baseline proof. The dict a VERIFIED, projected
    bridge hashes is EXACTLY what it was before the fallback existed — so every `item_hash` already
    stored at confirm still recomputes to itself and no promoted contract demotes on deploy."""
    _cross_seed(db)
    assert _bridge_fact_signature(db, bridge_fact_marker("bfk_cap")) == {
        "bridge": True, "status": "VERIFIED", "entity_id": "account",
        "left": "ops.public.transactions.account_id",
        "right": "rev.public.accounts.account_id"}


def test_an_unreviewed_bridge_resolves_instead_of_poisoning_the_contract(db):
    """THE defect. A DRAFT bridge is available and crossable, so a contract may legitimately depend
    on it — and must not be condemned to `_UNRESOLVED_AT_CONFIRM` for want of an approver."""
    _draft_bridge(db, "bfk_draft")
    marker = bridge_fact_marker("bfk_draft")

    sig = _bridge_fact_signature(db, marker)
    assert sig != _MISSING and sig["bridge"] is True and sig["status"] == "DRAFT"
    assert _catalog_state_signature(db, "rev", marker) == sig    # same via the read gate's entry point

    dep_row = {"contract_id": "c1", "catalog_source": "rev", "graph_ref": marker,
               "logical_ref": marker, "decision_id": None, "fact_id": None, "event_id": None}
    at_confirm = confirm_dependency_hash(
        db, contract_id="c1", catalog_source="rev", graph_ref=marker, logical_ref=marker,
        decision_id=None, fact_id=None, event_id=None)
    assert current_dependency_hash(db, dep_row) == at_confirm, "no poison — the contract is promotable"


@pytest.mark.parametrize("status", ["REVERIFY", "REJECTED", "STALE"])
def test_an_unavailable_bridge_still_resolves_to_missing(db, status):
    """The fallback is an AVAILABILITY fallback, not a blanket one. An expired, rejected or
    drift-staled bridge has no projection row AND no available lifecycle, so it stays MISSING —
    a contract that crossed it while it was live drifts, exactly as before."""
    _draft_bridge(db, "bfk_gone", status=status)
    assert _bridge_fact_signature(db, bridge_fact_marker("bfk_gone")) == _MISSING


# ══════════ the read gate folds the LIFECYCLE FIRST — a stale projection row cannot vouch ═════════
#
# `_bridge_fact_signature` read `entity_bridge_edge` and returned that row's state whenever a row
# existed, consulting the authoritative lifecycle only when there was NO row. But the projection is a
# cache and `demote_bridge_edges` is FAIL-SOFT, so the failure mode is exactly the one the gate exists
# to catch:
#
#     the bridge is rejected / expires / drift-stales
#       -> the edge demotion fails (or simply has not run)
#       -> the old VERIFIED row survives
#       -> the recomputed dependency hash still matches the one stored at confirm
#       -> a contract resting on a withdrawn bridge stays PROMOTABLE.
#
# Lifecycle first, then. REJECTED / STALE / expired-to-REVERIFY / UNREADABLE => MISSING, whatever the
# projection says. The healthy VERIFIED-with-a-row path is untouched byte for byte (controls below),
# so no stored `item_hash` re-baselines and no promoted contract demotes on deploy.
#
# Test strength: none of the three below can pass against a signature that reads the row first.

def _stale_row_behind(db, fact_key: str, *, status: str) -> None:
    """Drive `fact_key`'s lifecycle to `status` and leave a VERIFIED `entity_bridge_edge` row behind
    it — the shape a fail-soft demotion produces. The row's endpoints match the fixture's, so the two
    sources disagree about the STATUS alone."""
    _draft_bridge(db, fact_key, status=status)
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref,"
        " right_catalog_source, right_object_ref, status) "
        "VALUES (%s,'account','ops','public.transactions.account_id','rev',"
        " 'public.accounts.account_id','VERIFIED')", (fact_key,))


@pytest.mark.parametrize("status", ["REJECTED", "STALE", "REVERIFY"])
def test_a_surviving_verified_row_cannot_vouch_for_a_withdrawn_bridge(db, status):
    """THE fail-open. A VERIFIED projection row outliving its confirmation must not keep a contract
    promotable — the lifecycle is the authority and it says the bridge is gone."""
    _stale_row_behind(db, "bfk_soft", status=status)
    assert db.execute("SELECT status FROM entity_bridge_edge WHERE fact_key = 'bfk_soft'"
                      ).fetchone() == ("VERIFIED",), "the stale row really is there"

    marker = bridge_fact_marker("bfk_soft")
    assert _bridge_fact_signature(db, marker) == _MISSING
    assert _catalog_state_signature(db, "rev", marker) == _MISSING   # and via the gate's entry point


@pytest.mark.parametrize("status", ["REJECTED", "STALE", "REVERIFY"])
def test_a_contract_resting_on_a_withdrawn_bridge_drifts_even_if_the_row_survives(db, status):
    """The same fact stated where it bites: the recomputed hash no longer matches the one stored
    while the bridge was live, so the read gate downgrades."""
    _draft_bridge(db, "bfk_soft", status="VERIFIED")
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref,"
        " right_catalog_source, right_object_ref, status) "
        "VALUES ('bfk_soft','account','ops','public.transactions.account_id','rev',"
        " 'public.accounts.account_id','VERIFIED')")
    marker = bridge_fact_marker("bfk_soft")
    dep_row = {"contract_id": "c1", "catalog_source": "rev", "graph_ref": marker,
               "logical_ref": marker, "decision_id": None, "fact_id": None, "event_id": None}
    at_confirm = confirm_dependency_hash(
        db, contract_id="c1", catalog_source="rev", graph_ref=marker, logical_ref=marker,
        decision_id=None, fact_id=None, event_id=None)
    assert current_dependency_hash(db, dep_row) == at_confirm, "live and promotable to begin with"

    _draft_bridge(db, "bfk_soft", status=status)       # the withdrawal; the demotion never lands
    assert db.execute("SELECT count(*) FROM entity_bridge_edge WHERE fact_key = 'bfk_soft'"
                      ).fetchone()[0] == 1
    assert current_dependency_hash(db, dep_row) != at_confirm


def test_an_unreadable_fact_stream_returns_missing_even_with_a_verified_row(db, monkeypatch):
    """Fail CLOSED on an unreadable lifecycle. The gate cannot tell a sound bridge from a rejected
    one behind a failing read, so it must not let the projection answer for it."""
    from featuregen.overlay import store

    _cross_seed(db)
    marker = bridge_fact_marker("bfk_cap")
    assert _bridge_fact_signature(db, marker) != _MISSING, "the control: readable before the fault"

    def _boom(*_a, **_k):
        raise RuntimeError("fact stream unreadable")

    monkeypatch.setattr(store, "load_fact", _boom)
    assert _bridge_fact_signature(db, marker) == _MISSING


def test_control_b_the_healthy_verified_dependency_hash_is_not_re_baselined(db):
    """MUST-SURVIVE control, pinned to a LITERAL. `contract_metadata_dependency` is WORM (migration
    1011 rejects UPDATE and DELETE), so a signature change silently condemns every stored governed
    contract: the at-confirm hash can never be rewritten, and the recompute would stop matching it.

    The literal is what a healthy VERIFIED, projected bridge hashed BEFORE the lifecycle-first fold
    existed. If this fails, the change re-baselined history — which is a V2 dependency scheme plus
    recompilation into new contract versions, not an edit to this resolver."""
    _cross_seed(db)
    marker = bridge_fact_marker("bfk_cap")
    assert confirm_dependency_hash(
        db, contract_id="c1", catalog_source="rev", graph_ref=marker, logical_ref=marker,
        decision_id=None, fact_id=None, event_id=None) == _HEALTHY_VERIFIED_ITEM_HASH


def test_confirming_an_unreviewed_bridge_a_contract_crosses_registers_as_drift(db):
    """Stated, not hidden: the interim signature still carries a governance status, so a bridge going
    DRAFT -> VERIFIED changes it and the contract re-assesses. Conservative and honest for now; the
    directional-realization target removes governance status from the signature altogether."""
    _draft_bridge(db, "bfk_draft")
    marker = bridge_fact_marker("bfk_draft")
    dep_row = {"contract_id": "c1", "catalog_source": "rev", "graph_ref": marker,
               "logical_ref": marker, "decision_id": None, "fact_id": None, "event_id": None}
    at_confirm = confirm_dependency_hash(
        db, contract_id="c1", catalog_source="rev", graph_ref=marker, logical_ref=marker,
        decision_id=None, fact_id=None, event_id=None)
    assert current_dependency_hash(db, dep_row) == at_confirm, "not poisoned to begin with"

    _draft_bridge(db, "bfk_draft", status="VERIFIED")

    assert current_dependency_hash(db, dep_row) != at_confirm


def test_realization_signature_resolves_then_drifts_on_cardinality_and_drop(db):
    """``_realization_signature`` resolves a DERIVED realization's cardinality/authority from its declared
    ``graph_edge`` (a real hash at confirm → promotable), a cardinality retype CHANGES it (drift), and
    dropping the edge yields ``MISSING`` (drift). Uses the gold single-catalog accounts→customers N:1
    realization."""
    from featuregen.overlay.upload.planner.contract_gold import GATE_GOLD_SOURCE
    from featuregen.overlay.upload.planner.contract_gold import _seed as _gold_seed
    _gold_seed(db)
    (real,) = derive_catalog_realizations(db, GATE_GOLD_SOURCE).realizations
    marker = realization_marker(real.realization_id)

    at_confirm = confirm_dependency_hash(
        db, contract_id="c1", catalog_source=GATE_GOLD_SOURCE, graph_ref=marker, logical_ref=marker,
        decision_id=None, fact_id=None, event_id=None)
    dep_row = {"contract_id": "c1", "catalog_source": GATE_GOLD_SOURCE, "graph_ref": marker,
               "logical_ref": marker, "decision_id": None, "fact_id": None, "event_id": None}

    sig = _realization_signature(db, GATE_GOLD_SOURCE, marker)
    assert sig != _MISSING and sig["realization"] is True and sig["cardinality"]   # RESOLVES (no poison)
    assert current_dependency_hash(db, dep_row) == at_confirm                      # stable → not drifted

    # retype the realization's cardinality on its declared edge → the signature changes → drift.
    from_key, _, to_key = real.realization_id.partition(":")[2].partition("->")
    db.execute("UPDATE graph_edge SET cardinality = '1:N' WHERE catalog_source = %s "
               "AND kind = 'joins' AND from_ref = %s AND to_ref = %s",
               (GATE_GOLD_SOURCE, from_key, to_key))
    assert _realization_signature(db, GATE_GOLD_SOURCE, marker) != sig
    assert current_dependency_hash(db, dep_row) != at_confirm

    # drop the edge entirely → MISSING → drift.
    db.execute("DELETE FROM graph_edge WHERE catalog_source = %s AND kind = 'joins' "
               "AND from_ref = %s AND to_ref = %s", (GATE_GOLD_SOURCE, from_key, to_key))
    assert _realization_signature(db, GATE_GOLD_SOURCE, marker) == _MISSING
    assert current_dependency_hash(db, dep_row) != at_confirm


# ══════════ routing unit test — governed_segment steps map to the TYPED markers, never a raw graph_node ══
def test_contract_dependency_items_routes_governed_segments_to_typed_markers(db):
    """``_contract_dependency_items`` maps a ``governed_bridge`` segment to a ``bridgefact:`` marker and a
    realization segment (``intra_catalog_realization`` / ``semantic_rollup``) to a ``realization:`` marker,
    and drops the ref-less ``direct_catalog`` prefix — never recording the raw ``bfk_*`` / realization id
    as a graph_node dep (the poison shape)."""
    draft = ContractDraft(
        feature_name="probe", definition="d", grain_table=None, aggregation="sum", as_of_column=None,
        derives_from=["public.transactions.transaction_id"],
        derives_pairs=(("ops", "public.transactions.transaction_id"),),
        join_path=(
            {"kind": "governed_segment", "segment": "ops:direct_catalog:", "catalog_source": "ops",
             "segment_kind": "direct_catalog", "ref": ""},
            {"kind": "governed_segment", "segment": "ops:semantic_rollup:core:a->b",
             "catalog_source": "ops", "segment_kind": "semantic_rollup", "ref": "core:a->b"},
            {"kind": "governed_segment", "segment": "rev:governed_bridge:bfk_x", "catalog_source": "rev",
             "segment_kind": "governed_bridge", "ref": "bfk_x"},
        ))
    refs = {(cs, logical) for cs, _g, logical, _d, _f, _e in _contract_dependency_items(db, draft)}
    assert ("rev", "bridgefact:bfk_x") in refs
    assert ("ops", "realization:core:a->b") in refs
    assert not any(r in ("bfk_x", "core:a->b") for _cs, r in refs)   # no raw governed-segment ref
