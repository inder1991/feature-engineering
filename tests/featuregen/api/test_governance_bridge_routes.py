"""Governance-review redesign / Task 2 — the entity-bridge confirm / reject HTTP surface.

Nine cross-catalog links sit PROPOSED on the live catalog with no route that can decide them. Task 1
built the listing; this is the write side: ``GET /sources/{source}/governance/entity-bridges`` plus
``POST /governance/entity-bridges/{fact_key}/confirm`` and ``.../reject``, mirroring the
semantic-binding surface (``require_confirmer``, the fact_type-validated + read-scoped 404 bridge,
idempotency key, target-event CAS, the REAL overlay commands, and the deny-path RETURN-not-raise).

Four properties this suite exists to pin, all of which distinguish a real implementation from a
plausible-looking one:

* **The confirm actually PROJECTS.** ``confirm_fact`` has projection hooks for ``approved_join`` and
  the semantic bindings and NONE for ``entity_bridge`` (``confirmation_commands.py``), so the route
  is the ONLY thing that ever writes ``entity_bridge_edge``. A route that skips the projection still
  reaches VERIFIED and still returns 200 — and the bridge is never operational. The decisive test
  therefore asserts the ROW, with a non-null ``confirmed_event_id`` and ``status='VERIFIED'``.

* **W1 — ``project_verified_bridge`` has NO default for ``now``**, unlike its semantic-binding twin,
  and passes it straight into a ``timestamptz NOT NULL DEFAULT now()`` column. A column DEFAULT
  applies when the column is OMITTED, never when NULL is explicitly supplied, so ``now=None`` errors.
  The route must pass a real timestamp; the projection test would fail if it did not.

* **W2 — it has neither a savepoint nor a never-raises guarantee**, unlike its twin (*"a projection
  fault must not roll back the confirm"*). Unwrapped, a projection fault AFTER a successful
  ``confirm_fact`` propagates, rolls back the request transaction, and LOSES the confirmation — the
  reviewer sees a 500 and the fact is not VERIFIED. ``test_a_projection_fault_...`` injects exactly
  the W1 fault and asserts the fact is VERIFIED with ``"pending"`` reported.

* **A denied command RETURNS 409, never raises** (audit I-3): raising rolls back the
  ``COMMAND_DENIED`` row and releases the advisory lock. Proven under ``prod_tx_client``, which
  reproduces production commit/rollback semantics at savepoint granularity — under the plain
  ``client`` override the row would survive either way and the test would assert nothing.

**A VERIFIED bridge offers no actions, and reject is not one of them.** ``_AWAITING_CONFIRMATION``
excludes VERIFIED, so ``reject_fact`` DENIES a VERIFIED fact. A withdrawal needs the peer's shape (a
sanctioned VERIFIED -> REVERIFY transition first) and is a separate governance increment; the queue
must not advertise a button the server answers with a denial. Pinned by
``test_rejecting_a_verified_bridge_is_denied``. The demote is exercised where it genuinely applies:
a bridge that was VERIFIED (so an edge exists) and has since drifted STALE, which IS still decidable.

Seeding drives the REAL ``propose_bridge`` on the suite's rolled-back ``conn`` — the same connection
the TestClient's ``get_conn`` override serves — so the API reads exactly what the seed wrote.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_bridge_candidates import _load, _two_catalog_customer

from featuregen.api.deps import get_conn
from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.config import (
    _clear_overlay_config,
    overlay_config_from_env,
    register_overlay_config,
)
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.identity import EntityBridgeRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_projection import project_verified_bridge
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _h(user: str, roles: str = "platform-admin") -> dict:
    return {"X-User": user, "X-Roles": roles}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


@pytest.fixture
def overlay_env(conn):
    """The seeding preconditions (this module lives under tests/featuregen/api/, outside the overlay
    conftests): the OVERLAY_FACT_* event schemas + the upload-context catalog adapter that
    propose_fact -> resolve_authority needs."""
    register_overlay_event_types(event_registry())
    register_overlay_config(overlay_config_from_env({}))
    ensure_upload_catalog_adapter()
    return conn


def _seed_bridge(conn, *, actor=_ENRICH_ACTOR) -> tuple[EntityBridgeRef, str]:
    """The `core.customer_master.customer_id <-> crm.customers.customer_id` bridge, PROPOSED via the
    real derive + propose path. Default proposer is the SERVICE actor (R1: the system proposes, a
    human disposes), so four-eyes trivially holds for any human confirmer."""
    _two_catalog_customer(conn)
    cand = derive_bridge_candidates(conn)[0]
    key = propose_bridge(conn, cand, actor=actor, now=_NOW)
    return EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref), key


@pytest.fixture
def seeded_bridge(overlay_env):
    return _seed_bridge(overlay_env)


def _edge(conn, key: str):
    return conn.execute(
        "SELECT entity_id, left_catalog_source, left_object_ref, right_catalog_source, "
        "       right_object_ref, confirmed_event_id, status, projected_at "
        "FROM entity_bridge_edge WHERE fact_key = %s", (key,)).fetchone()


def _confirm(client, key: str, *, user: str = "priya", roles: str = "platform-admin", note=None):
    return client.post(f"/governance/entity-bridges/{key}/confirm",
                       json={"note": note} if note else {}, headers=_h(user, roles))


def _hide(conn, source: str, table: str, column: str, *, tag: str = "pii") -> None:
    conn.execute(
        "UPDATE graph_node SET sensitivity = %s WHERE catalog_source = %s AND object_ref = %s",
        (tag, source, f"public.{table}.{column}"))


def _stale(conn, key: str) -> None:
    """Drift a VERIFIED bridge to STALE via the SAME event `detect_catalog_changes` appends, so the
    fold under test is the production one. STALE is in `_AWAITING_CONFIRMATION`: the decision is
    genuinely reopened, and the edge projected at confirm time is still live."""
    state = fold_overlay_state(load_fact(conn, key))
    append_overlay_event(
        conn, fact_key=key, type="OVERLAY_FACT_STALED",
        payload={"catalog_change_ref": "chg-1",
                 "stales_confirmed_event_id": state.confirmed_event_id},
        actor=mint_test_identity(subject="user:admin1", role_claims=("platform-admin",)),
        expected_version=load_fact(conn, key)[-1].stream_version)


# ── (1) the listing route ─────────────────────────────────────────────────────────────────────────

def test_get_lists_the_bridge_from_either_endpoint(client, seeded_bridge):
    """`EntityBridgeRef` identity is UNORDERED, so opening the crm side must find the same bridge the
    core side does — a reviewer who works one catalog must not be blind to a bridge filed the other
    way round."""
    _ref, key = seeded_bridge
    for source in ("core", "crm"):
        r = client.get(f"/sources/{source}/governance/entity-bridges", headers=_h("priya"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == source and body["next_cursor"] is None
        (p,) = body["proposals"]
        assert p["fact_key"] == key
        assert p["status"] == "PROPOSED"
        assert p["available_actions"] == ["confirm", "reject"]
        assert p["target_event_id"] is not None
        assert sorted(p["catalogs"]) == ["core", "crm"]


def test_get_excludes_a_source_that_is_neither_endpoint(client, seeded_bridge):
    r = client.get("/sources/some-other-source/governance/entity-bridges", headers=_h("priya"))
    assert r.status_code == 200 and r.json()["proposals"] == []


def test_get_is_read_scoped_and_the_write_routes_404_on_a_hidden_endpoint(
        client, seeded_bridge, conn):
    """The listing must not become an EXISTENCE ORACLE, and the drop must not be cosmetic: a caller
    who cannot SEE the bridge must not be able to CONFIRM it either. One hidden endpoint (of two) is
    enough."""
    _ref, key = seeded_bridge
    _hide(conn, "crm", "customers", "customer_id")

    admin = _h("priya")                                    # platform-admin, NO pii_reader
    r = client.get("/sources/core/governance/entity-bridges", headers=admin)
    assert r.status_code == 200 and r.json()["proposals"] == []      # no count/id/existence leak
    for action, body in (("confirm", {}), ("reject", {"category": "not_the_same_entity"})):
        r = client.post(f"/governance/entity-bridges/{key}/{action}", json=body, headers=admin)
        assert r.status_code == 404, (action, r.text)       # hidden == missing on WRITE, pre-dispatch

    pii_admin = _h("priya", roles="platform-admin,pii_reader")
    r = client.get("/sources/core/governance/entity-bridges", headers=pii_admin)
    assert [p["fact_key"] for p in r.json()["proposals"]] == [key]


def test_non_confirmer_is_refused_by_the_dependency_on_every_route(client, seeded_bridge):
    """`require_confirmer` gates all three routes on the raw `platform-admin` CLAIM. A functional
    read-only role never reaches the domain layer."""
    _ref, key = seeded_bridge
    viewer = _h("v", roles="catalog_viewer")
    assert client.get("/sources/core/governance/entity-bridges",
                      headers=viewer).status_code == 403
    assert client.post(f"/governance/entity-bridges/{key}/confirm", json={},
                       headers=viewer).status_code == 403
    assert client.post(f"/governance/entity-bridges/{key}/reject",
                       json={"category": "not_the_same_entity"},
                       headers=viewer).status_code == 403


def test_list_limit_bounds_422(client):
    assert client.get("/sources/core/governance/entity-bridges?limit=0",
                      headers=_h("priya")).status_code == 422
    assert client.get("/sources/core/governance/entity-bridges?limit=501",
                      headers=_h("priya")).status_code == 422


# ── (2) THE decisive test: confirm projects the operational edge ───────────────────────────────────

def test_confirm_verifies_and_writes_the_operational_edge(client, seeded_bridge, conn):
    """`confirm_fact` has projection hooks for approved_join and the semantic bindings and NONE for
    entity_bridge, so this ROUTE is the only thing that ever writes `entity_bridge_edge`. A route
    that reaches VERIFIED and skips the projection returns an identical 200 over a bridge that is
    never operational — so the assertion is the ROW, not the status.

    It also pins W1: `project_verified_bridge` has no default for `now` and feeds it into a
    `timestamptz NOT NULL DEFAULT now()` column, so a route passing `now=None` errors here."""
    ref, key = seeded_bridge
    r = _confirm(client, key, note="same customer id both sides")
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "VERIFIED", "operational_projection": "projected"}
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"

    row = _edge(conn, key)
    assert row is not None, "the bridge is VERIFIED but NOT OPERATIONAL — no entity_bridge_edge row"
    (entity, lsrc, lref, rsrc, rref, confirmed_event_id, status, projected_at) = row
    assert entity == "customer"
    assert (lsrc, lref) == ("core", "public.customer_master.customer_id")
    assert (rsrc, rref) == ("crm", "public.customers.customer_id")
    assert status == "VERIFIED"
    assert confirmed_event_id, "confirmed_event_id is NULL — the edge cannot cite its confirmation"
    assert confirmed_event_id == fold_overlay_state(load_fact(conn, key)).confirmed_event_id
    assert projected_at is not None      # W1: a real timestamp reached the NOT NULL column
    # the confirmed bridge is still listed (VERIFIED) and now offers NOTHING to do
    r = client.get("/sources/core/governance/entity-bridges", headers=_h("priya"))
    (p,) = r.json()["proposals"]
    assert p["status"] == "VERIFIED" and p["available_actions"] == []


def test_confirm_is_idempotent_under_the_same_key(client, seeded_bridge, conn):
    """A repeat confirm by the same admin is a benign 409 (VERIFIED is not awaiting confirmation):
    no second CONFIRMED event, no duplicate edge, and the edge that IS there is untouched."""
    _ref, key = seeded_bridge
    assert _confirm(client, key).status_code == 200
    first = _edge(conn, key)

    r = _confirm(client, key)
    assert r.status_code == 409, r.text
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    assert len([e for e in load_fact(conn, key) if e.type == "OVERLAY_FACT_CONFIRMED"]) == 1
    assert conn.execute("SELECT count(*) FROM entity_bridge_edge WHERE fact_key = %s",
                        (key,)).fetchone()[0] == 1
    assert _edge(conn, key) == first


def test_confirm_404s_on_a_fact_of_another_type_without_writing_events(client, overlay_env):
    """The generic-approval hole: a fact_key naming some OTHER governed fact must not be confirmable
    through the bridge route, and the 404 must land BEFORE any command dispatch."""
    _load(overlay_env, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True),
         "customer_id")])
    from featuregen.contracts import Command
    from featuregen.overlay.commands import propose_fact
    from featuregen.overlay.identity import CatalogObjectRef, proposal_fingerprint
    ref = CatalogObjectRef("core", "column", "public", "customer_master", "customer_id")
    value = {"entity_id": "customer"}
    res = propose_fact(overlay_env, Command("propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_assignment", "proposed_value": value},
        _ENRICH_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    other_key = fact_key(ref, "entity_assignment")
    before = len(load_fact(overlay_env, other_key))

    r = _confirm(client, other_key)
    assert r.status_code == 404, r.text
    assert len(load_fact(overlay_env, other_key)) == before   # untouched


def test_unknown_fact_key_404(client, overlay_env):
    assert _confirm(client, "no-such-fact").status_code == 404


# ── (3) W2: a projection fault must NOT roll back the confirm ──────────────────────────────────────

def test_a_projection_fault_leaves_the_fact_verified_and_reports_pending(
        client, seeded_bridge, conn, monkeypatch):
    """W2. `project_verified_bridge` has no savepoint and no never-raises guarantee, unlike its twin
    (*"a projection fault must not roll back the confirm"*). Unwrapped, a fault AFTER a successful
    `confirm_fact` propagates, the request transaction rolls back, and the CONFIRMATION IS LOST — the
    reviewer sees a 500 and the fact is not VERIFIED.

    The injected fault is the REAL W1 defect (`now=None` into a NOT NULL column), so this is a
    genuine in-transaction database error that POISONS the transaction — nothing short of a savepoint
    rollback can recover it. Passing requires: 200, `"pending"` reported honestly, and the fact
    VERIFIED with its CONFIRMED event intact."""
    import featuregen.api.routes.governance as gov

    def _faulty(conn_, ref, *, now):          # noqa: ARG001 — the whole point is to ignore `now`
        return project_verified_bridge(conn_, ref, now=None)

    monkeypatch.setattr(gov, "project_verified_bridge", _faulty)

    _ref, key = seeded_bridge
    r = _confirm(client, key)
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "VERIFIED", "operational_projection": "pending"}
    # the confirm SURVIVED the poisoned projection
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    assert len([e for e in load_fact(conn, key) if e.type == "OVERLAY_FACT_CONFIRMED"]) == 1
    assert _edge(conn, key) is None          # honest: "pending" means NOT operational
    # and the connection is usable — the savepoint rollback un-poisoned the transaction
    assert conn.execute("SELECT 1").fetchone()[0] == 1


# ── (4) four-eyes: the proposer cannot confirm, and the denial is RETURNED ─────────────────────────

def _denial_rows(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT reason FROM security_audit WHERE event_type = 'COMMAND_DENIED' ORDER BY seq"
    ).fetchall()]


@pytest.fixture
def prod_tx_client(client, conn):
    """`client` with get_conn re-overridden to PRODUCTION transaction semantics at savepoint
    granularity: a route that RETURNS releases the savepoint (deps.get_conn: commit), a route that
    RAISES rolls back to it (deps.get_conn: rollback). The plain `client` override does NEITHER, so
    under it a COMMAND_DENIED row stays visible whether or not production would have rolled it back
    — THIS override is what makes denial-audit durability (audit I-3) observable at all."""
    conn.execute("SELECT 1")   # open the outer tx so conn.transaction() nests as a SAVEPOINT

    def _prod_like():
        with conn.transaction():
            yield conn

    client.app.dependency_overrides[get_conn] = _prod_like
    return client


def test_the_proposer_cannot_confirm_and_the_denial_audit_survives(prod_tx_client, overlay_env,
                                                                   conn):
    """Four-eyes: a human who proposed a bridge may not confirm it. The 409 must be RETURNED, not
    raised — raising rolls the `COMMAND_DENIED` row back and releases the advisory lock, so an
    insider probe leaves zero evidence (audit I-3, already paid for once in this repo)."""
    proposer = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
    _ref, key = _seed_bridge(overlay_env, actor=proposer)

    r = prod_tx_client.post(f"/governance/entity-bridges/{key}/confirm", json={},
                            headers=_h("priya"))
    assert r.status_code == 409, r.text
    assert r.json() == {"detail": "four-eyes: a proposer may not confirm the same fact"}
    assert fold_overlay_state(load_fact(conn, key)).status == "DRAFT"
    assert _edge(conn, key) is None
    assert _denial_rows(conn) == ["four-eyes: a proposer may not confirm the same fact"]

    # a DIFFERENT admin confirms exactly as before
    r = prod_tx_client.post(f"/governance/entity-bridges/{key}/confirm", json={},
                            headers=_h("dev"))
    assert r.status_code == 200, r.text
    assert _edge(conn, key) is not None


def test_the_listing_withholds_confirm_from_the_proposer(client, overlay_env):
    """`available_actions` is SERVER-decided: the queue must not offer a button the write side
    denies. The same caller who gets the 409 above is not offered `confirm`."""
    proposer = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
    _ref, key = _seed_bridge(overlay_env, actor=proposer)
    r = client.get("/sources/core/governance/entity-bridges", headers=_h("priya"))
    (p,) = r.json()["proposals"]
    assert p["fact_key"] == key and p["available_actions"] == ["reject"]
    # a different admin IS offered the confirm
    r = client.get("/sources/core/governance/entity-bridges", headers=_h("dev"))
    assert r.json()["proposals"][0]["available_actions"] == ["confirm", "reject"]


# ── (5) reject ────────────────────────────────────────────────────────────────────────────────────

def test_reject_a_proposed_bridge_records_the_category(client, seeded_bridge, conn):
    _ref, key = seeded_bridge
    r = client.post(f"/governance/entity-bridges/{key}/reject",
                    json={"category": "not_the_same_entity", "note": "crm ids are not core ids"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "REJECTED", "category": "not_the_same_entity",
                        "operational_projection": "not_applicable"}
    assert fold_overlay_state(load_fact(conn, key)).status == "REJECTED"
    rejected = [e for e in load_fact(conn, key) if e.type == "OVERLAY_FACT_REJECTED"][-1]
    assert rejected.payload["category"] == "not_the_same_entity"
    assert rejected.payload["reason"] == "crm ids are not core ids"
    # REJECTED is terminal — it leaves the queue
    r = client.get("/sources/core/governance/entity-bridges", headers=_h("priya"))
    assert r.json()["proposals"] == []


def test_rejecting_a_drifted_bridge_demotes_the_live_edge(client, seeded_bridge, conn):
    """The demote that genuinely matters. `reject_fact` has demotion hooks for approved_join and the
    semantic bindings and NONE for entity_bridge, so a rejected-but-still-projected bridge would stay
    traversable by the planner. STALE is in `_AWAITING_CONFIRMATION` (drift reopens the decision) and
    nothing demotes on stale, so this is the reachable shape: a live edge under a rejectable fact."""
    _ref, key = seeded_bridge
    assert _confirm(client, key).status_code == 200
    assert _edge(conn, key) is not None
    _stale(conn, key)
    assert fold_overlay_state(load_fact(conn, key)).status == "STALE"

    r = client.post(f"/governance/entity-bridges/{key}/reject",
                    json={"category": "needs_data_check"}, headers=_h("dev"))
    assert r.status_code == 200, r.text
    assert r.json()["operational_projection"] == "demoted"
    assert fold_overlay_state(load_fact(conn, key)).status == "REJECTED"
    assert _edge(conn, key) is None, "a REJECTED bridge is still projected — planner can traverse it"


def test_rejecting_a_verified_bridge_is_denied(client, seeded_bridge, conn):
    """The plan claimed rejecting a VERIFIED bridge demotes the edge. It does NOT:
    `_AWAITING_CONFIRMATION` excludes VERIFIED, so `reject_fact` DENIES it. The listing therefore
    offers NO action on VERIFIED, and the route agrees — a real withdrawal needs the peer's shape (a
    sanctioned VERIFIED -> REVERIFY transition first) and is a separate governance increment."""
    _ref, key = seeded_bridge
    assert _confirm(client, key).status_code == 200
    r = client.post(f"/governance/entity-bridges/{key}/reject",
                    json={"category": "needs_data_check"}, headers=_h("dev"))
    assert r.status_code == 409, r.text
    assert "not awaiting confirmation" in r.json()["detail"]
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    assert _edge(conn, key) is not None, "a denied reject must not touch the projection"


def test_bad_reject_category_422(client, seeded_bridge):
    _ref, key = seeded_bridge
    r = client.post(f"/governance/entity-bridges/{key}/reject", json={"category": "because"},
                    headers=_h("priya"))
    assert r.status_code == 422


def test_over_length_note_422(client, seeded_bridge):
    _ref, key = seeded_bridge
    r = _confirm(client, key, note="x" * 1001)
    assert r.status_code == 422
