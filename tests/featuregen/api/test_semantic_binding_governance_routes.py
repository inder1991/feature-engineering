"""Delivery E / Task E2 — the semantic-binding governance API routes (list / confirm / reject /
reverify / withdraw / correct).

Mirrors the join + table-fact governance surfaces (``test_governance_routes.py``): all routes gated
by ``require_confirmer`` (raw ``platform-admin`` claim), the fact_type-VALIDATED 404 bridge (a non-
semantic fact_key 404s BEFORE any command dispatch), the idempotency-keyed / target-event-CAS
confirm/reject dispatching the REAL overlay ``confirm_fact`` / ``reject_fact`` (never writing fact
state directly), and the deny-path RETURN-not-raise (commit the ``COMMAND_DENIED`` audit row).

E2 adds, on top of the peer shape: the GET lists BOTH pending AND VERIFIED bindings with
``available_actions`` per binding (the asset-UI editability key), and the three VERIFIED-binding
actions (reverify / withdraw / correct) reuse the sanctioned expiry/reverify transition + the
overlay commands. E3's projection is exercised end-to-end: confirm → ``graph_node.entity`` /
``semantic_binding_edge`` set; withdraw → demoted.

Seeding drives the REAL ``propose_fact`` from the service enrichment actor on the suite's
rolled-back ``conn`` (the same connection the TestClient's ``get_conn`` override serves), so the API
reads exactly what the seed wrote. Owner routing is the upload-context adapter (``owner_of -> None``
→ the platform-admin governance queue), so a platform-admin is the owner-or-admin authority (E1
``admin_confirmable``) and four-eyes holds because the proposer is the service actor.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload.passc.conftest import SERVICE_ACTOR

from featuregen.contracts import Command
from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.catalog_changes import _write_watermark
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.config import (
    _clear_overlay_config,
    overlay_config_from_env,
    register_overlay_config,
)
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.identity import CatalogObjectRef, fact_key, proposal_fingerprint
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.semantic_bindings.projection import verified_currency_binding
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter


def _h(user: str, roles: str = "platform-admin") -> dict:
    return {"X-User": user, "X-Roles": roles}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


@pytest.fixture
def sealed_config(client):
    """Seal the process-wide OverlayConfig from an EMPTY env (the production posture — the app
    lifespan seals its own at TestClient startup, so this MUST depend on `client` and re-seal after;
    it arms resolve_fact's drift-freshness guard, so the projection-landing tests also write a FRESH
    watermark). Mirrors test_governance_routes.py."""
    register_overlay_config(overlay_config_from_env({}))
    yield
    _clear_overlay_config()


@pytest.fixture
def overlay_env(conn):
    """The seeding preconditions (this module lives under tests/featuregen/api/, outside the overlay
    conftests): the OVERLAY_FACT_* event schemas + the upload-context catalog adapter that
    propose_fact -> resolve_authority needs."""
    register_overlay_event_types(event_registry())
    ensure_upload_catalog_adapter()
    return conn


# ── seed helpers (real propose path, service proposer → platform-admin governance queue) ──────────

def _entity_ref(source="src", table="party", column="cust_id") -> CatalogObjectRef:
    return CatalogObjectRef(source, "column", "public", table, column)


def _measure_ref(source="src", table="trades", column="notional") -> CatalogObjectRef:
    return CatalogObjectRef(source, "column", "public", table, column)


def _ccy_value(source="src", table="trades", column="ccy") -> dict:
    return {"currency_column": {"catalog_source": source, "object_kind": "column",
                                "schema": "public", "table": table, "column": column}}


def _seed_entity_draft(conn, source="src", entity="customer"):
    ref = _entity_ref(source=source)
    value = {"entity_id": entity}
    res = propose_fact(conn, Command("propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_assignment", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return ref, fact_key(ref, "entity_assignment")


def _seed_currency_draft(conn, source="src"):
    ref = _measure_ref(source=source)
    value = _ccy_value(source=source)
    res = propose_fact(conn, Command("propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "currency_binding", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return ref, fact_key(ref, "currency_binding")


@pytest.fixture
def seeded_entity(overlay_env):
    return _seed_entity_draft(overlay_env)


@pytest.fixture
def seeded_currency(overlay_env):
    return _seed_currency_draft(overlay_env)


def _confirm(client, key, user="priya", **body):
    return client.post(f"/governance/semantic-bindings/{key}/confirm", json=body, headers=_h(user))


# ── (1) GET lists pending + VERIFIED with actions; read-scoped ───────────────────────────────────

def test_get_lists_pending_and_verified_with_actions(client, seeded_currency, conn):
    _ref, dkey = seeded_currency  # a pending currency binding
    # a SECOND, distinct binding taken to VERIFIED so the list carries a pending AND a VERIFIED row
    _eref, ekey = _seed_entity_draft(conn, source="src")
    assert _confirm(client, ekey).status_code == 200

    r = client.get("/sources/src/governance/semantic-bindings", headers=_h("priya"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "src" and body["next_cursor"] is None
    by_key = {p["fact_key"]: p for p in body["proposals"]}
    assert dkey in by_key and ekey in by_key
    assert by_key[dkey]["status"] == "PROPOSED"
    assert by_key[dkey]["available_actions"] == ["confirm", "reject"]
    assert by_key[dkey]["target_event_id"] is not None
    assert "evidence" in by_key[dkey] and "reason_codes" in by_key[dkey]
    assert by_key[ekey]["status"] == "VERIFIED"
    assert by_key[ekey]["available_actions"] == ["reverify", "withdraw", "correct"]


def test_get_excludes_other_sources(client, seeded_currency):
    r = client.get("/sources/some-other-source/governance/semantic-bindings", headers=_h("priya"))
    assert r.status_code == 200 and r.json()["proposals"] == []


def test_get_non_admin_403(client):
    r = client.get("/sources/src/governance/semantic-bindings",
                   headers=_h("v", roles="catalog_viewer"))
    assert r.status_code == 403


# ── (2) confirm DRAFT → VERIFIED (E3 projects) + idempotent-repeat 409 ────────────────────────────

def test_confirm_currency_verifies_and_projects_edge(client, sealed_config, seeded_currency, conn):
    _ref, key = seeded_currency
    _write_watermark(conn, "src", datetime.now(UTC))   # fresh: within the default 60m SLA
    r = _confirm(client, key, note="ccy ok")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] == "projected"
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    # E3 wrote the operational currency edge (status='VERIFIED' 2nd gate passes)
    assert verified_currency_binding(conn, key) is not None
    # a repeat confirm is a benign 409 (already VERIFIED — not awaiting): no double-verify
    r = _confirm(client, key)
    assert r.status_code == 409
    # a VERIFIED binding leaves the pending set (still listed, now VERIFIED with edit actions)
    r = client.get("/sources/src/governance/semantic-bindings", headers=_h("priya"))
    (p,) = [x for x in r.json()["proposals"] if x["fact_key"] == key]
    assert p["status"] == "VERIFIED"
    assert p["available_actions"] == ["reverify", "withdraw", "correct"]


def test_confirm_entity_projects_graph_node_entity(client, sealed_config, seeded_entity, conn):
    _ref, key = seeded_entity
    # the file declares "account"; the governed "customer" must WIN + preserve "account" as declared
    build_graph(conn, "src", [CanonicalRow(source="src", table="party", column="cust_id",
                                           type="text", entity="account")])
    _write_watermark(conn, "src", datetime.now(UTC))   # fresh watermark: the projection may land
    r = _confirm(client, key)
    assert r.status_code == 200, r.text
    assert r.json()["governance_status"] == "VERIFIED"
    assert r.json()["operational_projection"] == "projected"
    entity, declared, fk, status = conn.execute(
        "SELECT entity, declared_entity, entity_fact_key, entity_status FROM graph_node "
        "WHERE catalog_source = 'src' AND object_ref = 'public.party.cust_id'").fetchone()
    assert entity == "customer" and declared == "account"     # governed WINS, file preserved
    assert fk == key and status == "VERIFIED"


# ── (3) reject DRAFT → REJECTED (demote) ─────────────────────────────────────────────────────────

def test_reject_records_category_and_demotes(client, seeded_entity, conn):
    _ref, key = seeded_entity
    r = client.post(f"/governance/semantic-bindings/{key}/reject",
                    json={"category": "wrong_entity", "note": "not a customer id"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "REJECTED", "category": "wrong_entity",
                        "operational_projection": "demoted"}
    assert fold_overlay_state(load_fact(conn, key)).status == "REJECTED"
    rejected = [e for e in load_fact(conn, key) if e.type == "OVERLAY_FACT_REJECTED"]
    assert rejected[-1].payload["category"] == "wrong_entity"
    assert rejected[-1].payload["reason"] == "not a customer id"


# ── (4) four-eyes via correct: a human proposer may not confirm the same value ────────────────────

def test_correct_opens_new_proposal_and_self_confirm_is_refused(client, seeded_currency, conn):
    _ref, key = seeded_currency
    assert _confirm(client, key).status_code == 200          # priya (service-proposed) → VERIFIED
    # priya CORRECTS the currency column → a NEW proposal, proposed BY priya
    r = client.post(f"/governance/semantic-bindings/{key}/correct",
                    json={"value": _ccy_value(column="settle_ccy"), "note": "fix ccy"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "PROPOSED" and body["requires_distinct_confirmer"] is True
    assert body["operational_projection"] == "demoted"
    # priya (the proposer) may NOT also confirm — four-eyes
    r = _confirm(client, key, user="priya")
    assert r.status_code == 409 and "four-eyes" in r.json()["detail"]
    # a DISTINCT admin confirms → VERIFIED with the corrected value
    r = _confirm(client, key, user="rahman")
    assert r.status_code == 200, r.text
    assert r.json()["governance_status"] == "VERIFIED"
    state = fold_overlay_state(load_fact(conn, key))
    assert state.value == _ccy_value(column="settle_ccy")


# ── (5) reverify / withdraw on a VERIFIED binding ────────────────────────────────────────────────

def test_reverify_reopens_the_cycle(client, sealed_config, seeded_currency, conn):
    _ref, key = seeded_currency
    _write_watermark(conn, "src", datetime.now(UTC))
    assert _confirm(client, key).status_code == 200
    assert verified_currency_binding(conn, key) is not None
    r = client.post(f"/governance/semantic-bindings/{key}/reverify", json={}, headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "REVERIFY", "operational_projection": "demoted"}
    assert fold_overlay_state(load_fact(conn, key)).status == "REVERIFY"
    assert verified_currency_binding(conn, key) is None      # demoted until re-confirmed


def test_withdraw_demotes_the_binding(client, sealed_config, seeded_currency, conn):
    _ref, key = seeded_currency
    _write_watermark(conn, "src", datetime.now(UTC))
    assert _confirm(client, key).status_code == 200
    assert verified_currency_binding(conn, key) is not None
    r = client.post(f"/governance/semantic-bindings/{key}/withdraw",
                    json={"category": "no_longer_valid", "note": "retire"}, headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "REJECTED", "category": "no_longer_valid",
                        "operational_projection": "demoted"}
    assert fold_overlay_state(load_fact(conn, key)).status == "REJECTED"
    assert verified_currency_binding(conn, key) is None      # 2nd gate hides the demoted edge


def test_reverify_withdraw_correct_on_non_verified_are_conflicts(client, seeded_currency):
    _ref, key = seeded_currency  # a DRAFT — not VERIFIED
    for action, body in (("reverify", {}), ("withdraw", {"category": "no_longer_valid"}),
                         ("correct", {"value": _ccy_value(column="settle_ccy")})):
        r = client.post(f"/governance/semantic-bindings/{key}/{action}", json=body,
                        headers=_h("priya"))
        assert r.status_code == 409, (action, r.text)
        assert "not VERIFIED" in r.json()["detail"]


# ── (6) fact_type gate + unknown fact + request validation + authz ───────────────────────────────

def test_routes_404_on_a_non_semantic_fact_without_writing_events(client, overlay_env):
    ref = _measure_ref()
    value = {"columns": ["notional"], "is_unique": False}
    from featuregen.overlay.upload.upload_catalog import table_ref
    tref = table_ref("src", "t")
    gvalue = {"columns": ["cif_id"], "is_unique": True}
    res = propose_fact(overlay_env, Command("propose_fact", "overlay_fact", None,
        {"ref": tref, "fact_type": "grain", "proposed_value": gvalue},
        SERVICE_ACTOR, proposal_fingerprint(gvalue)))
    assert res.accepted, res.denied_reason
    grain_key = fact_key(tref, "grain")
    before = len(load_fact(overlay_env, grain_key))
    del ref, value  # (defensive builders; the grain fact is what proves the gate)
    for action, body in (("confirm", {}), ("reject", {"category": "wrong_entity"}),
                         ("reverify", {}), ("withdraw", {"category": "no_longer_valid"}),
                         ("correct", {"value": _ccy_value()})):
        r = client.post(f"/governance/semantic-bindings/{grain_key}/{action}", json=body,
                        headers=_h("priya"))
        assert r.status_code == 404, (action, r.text)
    assert len(load_fact(overlay_env, grain_key)) == before  # 404 before any dispatch — untouched


def test_unknown_fact_key_404(client):
    for action, body in (("confirm", {}), ("reject", {"category": "wrong_entity"}),
                         ("reverify", {}), ("withdraw", {"category": "superseded"}),
                         ("correct", {"value": _ccy_value()})):
        r = client.post(f"/governance/semantic-bindings/no-such-key/{action}", json=body,
                        headers=_h("priya"))
        assert r.status_code == 404, action


def test_non_admin_403_on_all_routes(client):
    viewer = _h("v", roles="catalog_viewer")
    assert client.post("/governance/semantic-bindings/any/confirm", json={},
                       headers=viewer).status_code == 403
    assert client.post("/governance/semantic-bindings/any/reject",
                       json={"category": "wrong_entity"}, headers=viewer).status_code == 403
    assert client.post("/governance/semantic-bindings/any/reverify", json={},
                       headers=viewer).status_code == 403
    assert client.post("/governance/semantic-bindings/any/withdraw",
                       json={"category": "superseded"}, headers=viewer).status_code == 403
    assert client.post("/governance/semantic-bindings/any/correct",
                       json={"value": {}}, headers=viewer).status_code == 403


def test_bad_category_422(client):
    # "wrong_direction" is a valid JOIN category — the semantic reject enum must reject it
    r = client.post("/governance/semantic-bindings/any/reject",
                    json={"category": "wrong_direction"}, headers=_h("priya"))
    assert r.status_code == 422
    r = client.post("/governance/semantic-bindings/any/withdraw",
                    json={"category": "wrong_grain_columns"}, headers=_h("priya"))
    assert r.status_code == 422


def test_over_length_note_422(client):
    long_note = "x" * 1001
    assert client.post("/governance/semantic-bindings/any/confirm", json={"note": long_note},
                       headers=_h("priya")).status_code == 422
    assert client.post("/governance/semantic-bindings/any/reject",
                       json={"category": "wrong_entity", "note": long_note},
                       headers=_h("priya")).status_code == 422


def test_list_limit_bounds_422(client):
    assert client.get("/sources/src/governance/semantic-bindings?limit=0",
                      headers=_h("priya")).status_code == 422
    assert client.get("/sources/src/governance/semantic-bindings?limit=501",
                      headers=_h("priya")).status_code == 422
