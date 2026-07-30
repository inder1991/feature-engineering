"""`GET /governance/queue` — the cross-catalog governance queue, end to end through the real route.

Before this route there was no answer to "what is waiting for me?", only "what is waiting in the
catalog you already named": every governance listing is `/sources/{source}/governance/…`, so the
review screen opened on an empty text input. This asserts the merged surface over HTTP.

Four properties, each of which a plausible-looking implementation gets wrong:

* **ONE REQUEST, NO SOURCE, ALL THREE KINDS.** Only `list_bridge_proposals` accepts `source=None`;
  the join and table-fact listings REQUIRE a source and must be iterated per catalog. The request
  carries no source at all.

* **THE MERGE DOES NOT WIDEN VISIBILITY.** Read scope comes from the SESSION's `role_claims`, never
  the request. A caller who cannot see a catalog gets none of its items — asserted as absence from
  the raw response TEXT (no slug, no count, no id anywhere), and asserted against the SAME seeding
  under a granting role so the absence is provably the scope predicate.

* **THE VOCABULARY.** The queue is a review / accountability / usage-monitoring surface, never an
  execution gate. `test_no_forbidden_phrase_appears_in_the_response` scans the rendered JSON for each
  phrase that would claim otherwise; a single label regression fails it.

* **NEVER ZERO WHEN IT CANNOT MEASURE.** Every plan-storage table is empty on the live database, so
  an implementation that counts naively answers 0 for every usage category and the screen reads
  "nothing depends on this bridge". The response must say `not tracked yet` instead — as a string,
  over the wire, which is what the screen will render.

Seeding drives the REAL propose paths on the suite's rolled-back `conn` — the same connection the
TestClient's `get_conn` override serves — so the API reads exactly what the seed wrote.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.conftest import _confirm_grain
from tests.featuregen.overlay.upload.passc.conftest import SERVICE_ACTOR
from tests.featuregen.overlay.upload.test_bridge_candidates import _load
from tests.featuregen.overlay.upload.test_join_governance import _seed_join_with_evidence

from featuregen.contracts.envelopes import Command
from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.config import (
    _clear_overlay_config,
    overlay_config_from_env,
    register_overlay_config,
)
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.governance_queue import FORBIDDEN_PHRASES
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

#: The confirmer claim every listing this queue merges is gated on.
ADMIN = {"X-User": "priya", "X-Roles": "platform-admin"}
#: The same confirmer, additionally holding the data-sensitivity role.
ADMIN_PII = {"X-User": "priya", "X-Roles": "platform-admin,pii_reader"}
#: A caller WITHOUT the confirmer claim.
VIEWER = {"X-User": "v", "X-Roles": "catalog_viewer"}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


@pytest.fixture
def overlay_env(conn):
    """The seeding preconditions (this module lives under tests/featuregen/api/, outside the overlay
    conftests): the OVERLAY_FACT_* schemas + the upload-context catalog adapter that
    `propose_fact` -> `resolve_authority` needs."""
    register_overlay_event_types(event_registry())
    register_overlay_config(overlay_config_from_env({}))
    ensure_upload_catalog_adapter()
    return conn


def _two_catalogs(conn, *, right_sensitivity: str | None = None) -> None:
    _load(conn, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True),
         "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text"), "categorical"),
    ])
    _load(conn, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True,
                      sensitivity=right_sensitivity), "customer_id"),
    ])


def _seed_bridge(conn, *, roles: tuple[str, ...] = ()) -> str:
    """`derive_bridge_candidates` is itself read-scoped, so deriving a bridge onto a pii endpoint
    needs the privileged scope an ingest would run under. The READ under test is separate."""
    return propose_bridge(conn, derive_bridge_candidates(conn, roles=roles)[0],
                          actor=SERVICE_ACTOR, now=_NOW)


def _seed_grain(conn, source: str, table: str) -> str:
    ref = table_ref(source, table)
    value = {"columns": ["customer_id"], "is_unique": True}
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return fact_key(ref, "grain")


@pytest.fixture
def seeded(client, overlay_env):
    """One bridge (core <-> crm), one discovered join (`src`) and one grain proposal (`core`)."""
    _two_catalogs(overlay_env)
    bridge = _seed_bridge(overlay_env)
    grain = _seed_grain(overlay_env, "core", "customer_master")
    _load(overlay_env, "src", [
        (CanonicalRow("src", "transactions", "cif_id", "text"), "customer_id"),
        (CanonicalRow("src", "customers", "cif_id", "text", is_grain=True), "customer_id"),
    ])
    _, join = _seed_join_with_evidence(overlay_env)
    # `conn` IS the connection the TestClient's `get_conn` override serves, so a row inserted through
    # it is a row the route reads.
    return {"client": client, "conn": overlay_env, "bridge": bridge, "join": join, "grain": grain}


def _verify_grain(conn, source: str, table: str, columns: list[str]) -> None:
    """A COMPLETE, VERIFIED, unique grain through the REAL four-eyes flow (service proposes,
    platform-admin confirms, projection drains) — the ONLY thing that makes a grain governed."""
    _seed_grain(conn, source, table)
    _confirm_grain(conn, source, table, columns,
                   actor=mint_test_identity(subject="user:admin",
                                            role_claims=("platform-admin",)))


def _get(client, headers=ADMIN, **params):
    return client.get("/governance/queue", headers=headers, params=params)


def _usage(item, category: str) -> dict:
    return next(u for u in item["already_depended_on_by"] if u["category"] == category)


# ── The merge ────────────────────────────────────────────────────────────────────────────────────

def test_one_request_with_no_source_returns_all_three_kinds(seeded):
    res = _get(seeded["client"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert {i["kind"] for i in body["items"]} == {"entity_bridge", "approved_join", "grain"}
    assert {i["fact_key"] for i in body["items"]} >= {seeded["bridge"], seeded["join"],
                                                     seeded["grain"]}
    assert {"core", "crm", "src"} <= set(body["catalogs"])
    assert body["complete"] is True and body["unreadable"] == []


def test_per_catalog_counts_are_named_scope_relative_never_a_total(seeded):
    """Two operators legitimately see different numbers for the same catalog, so no field may be
    called or read as a catalog total. The name carries the meaning."""
    res = _get(seeded["client"])
    body = res.json()
    assert body["counts_are_scope_relative"] is True
    per_catalog = body["items_visible_to_you_by_catalog"]
    assert per_catalog["core"] >= 2 and per_catalog["src"] >= 1
    assert "total" not in res.text.lower()


def test_a_bridge_item_carries_the_server_decided_actions_and_both_catalogs(seeded):
    body = _get(seeded["client"]).json()
    bridge = next(i for i in body["items"] if i["kind"] == "entity_bridge")
    assert set(bridge["catalogs"]) == {"core", "crm"}
    # The service actor proposed it, so four-eyes holds for this human confirmer.
    assert bridge["available_actions"] == ["confirm", "reject"]


def test_an_empty_database_is_an_empty_complete_queue_not_a_404(client, overlay_env):
    res = _get(client)
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == [] and body["catalogs"] == []
    assert body["complete"] is True


def test_the_queue_requires_the_confirmer_claim(seeded):
    """A merged view may not be reachable by a caller who could not reach its parts: every listing it
    merges is gated on the raw `platform-admin` claim."""
    assert _get(seeded["client"], headers=VIEWER).status_code == 403


# ── Read scope: the session decides, and the merge does not widen ─────────────────────────────────

def test_a_caller_who_cannot_see_a_catalog_gets_none_of_its_items(client, overlay_env):
    """THE EXISTENCE ORACLE at the merge. `crm` is pii top to bottom. The join / table-fact listings
    have no role scoping of their own, so the only thing keeping `crm`'s grain proposal out of an
    unprivileged response is the queue refusing to visit a catalog outside the caller's scope."""
    _two_catalogs(overlay_env, right_sensitivity="pii")
    bridge = _seed_bridge(overlay_env, roles=("pii_reader",))
    hidden_grain = _seed_grain(overlay_env, "crm", "customers")
    open_grain = _seed_grain(overlay_env, "core", "customer_master")

    blind = _get(client, headers=ADMIN)
    assert blind.status_code == 200
    body = blind.json()
    assert "crm" not in body["catalogs"]
    assert "crm" not in blind.text            # no slug, no count, nowhere in the payload
    assert hidden_grain not in blind.text     # not even the id
    assert bridge not in blind.text           # a bridge naming a hidden catalog is dropped whole
    assert open_grain in blind.text

    # The SAME seeding under the granting session DOES return all three, so the absences above are
    # the scope predicate and not an empty fixture.
    priv = _get(client, headers=ADMIN_PII)
    assert priv.status_code == 200
    assert "crm" in priv.json()["catalogs"]
    for key in (bridge, hidden_grain, open_grain):
        assert key in priv.text


def test_scope_comes_from_the_session_and_cannot_be_asked_for(client, overlay_env):
    """There is no request parameter that widens scope: a query string naming the hidden catalog, or
    naming roles, changes nothing."""
    _two_catalogs(overlay_env, right_sensitivity="pii")
    _seed_bridge(overlay_env, roles=("pii_reader",))
    _seed_grain(overlay_env, "crm", "customers")
    for params in ({}, {"source": "crm"}, {"roles": "pii_reader"}, {"catalog": "crm"}):
        res = client.get("/governance/queue", headers=ADMIN, params=params)
        assert res.status_code == 200, res.text
        assert "crm" not in res.json()["catalogs"], params


# ── The vocabulary ───────────────────────────────────────────────────────────────────────────────

def test_no_forbidden_phrase_appears_in_the_response(seeded):
    """The queue is a review, accountability and usage-monitoring surface — never an execution gate.
    Each forbidden phrase asserts the opposite, that a confirmation is permission to execute."""
    text = _get(seeded["client"]).text.lower()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase.lower() not in text, phrase
    for word in ("blocks", "blocked", "approve to enable", "waiting to become usable",
                 "production approval"):
        assert word not in text, word


def test_an_unreviewed_relationship_is_presented_as_available_for_use(seeded):
    body = _get(seeded["client"]).json()
    bridge = next(i for i in body["items"] if i["kind"] == "entity_bridge")
    assert bridge["state"] == "Unreviewed — available for use"
    assert bridge["state_code"] == "unreviewed_available"


def test_production_eligibility_is_reported_independently_of_review(seeded):
    """The seeded bridge is UNREVIEWED and automatically validated at the same time — which is the
    product point: review does not gate production, automatic validation of the directional
    realization does. What licenses the automatic half is the GOVERNED grain of `crm.customers`,
    confirmed here through the real four-eyes flow: `crm`'s endpoint column is that whole grain, so
    the compiler resolves the same crossing as `bridge_far_grain`."""
    _verify_grain(seeded["conn"], "crm", "customers", ["customer_id"])
    body = _get(seeded["client"]).json()
    bridge = next(i for i in body["items"] if i["kind"] == "entity_bridge")
    assert bridge["state_code"] == "unreviewed_available"
    assert bridge["production_eligibility"] == "Automatically validated for production"
    assert bridge["production_eligibility_code"] == "grain_resolved"


def test_an_uploaders_own_is_grain_flag_is_never_production_validation_over_the_wire(seeded):
    """The OVER-CLAIM, at the wire. Both endpoints are `is_grain` in their own uploads and no grain
    fact is VERIFIED, so the screen must read "sandbox only" — the response used to say
    "Automatically validated for production" while the compiler refused the identical crossing as
    `bridge_unattested`."""
    body = _get(seeded["client"]).json()
    bridge = next(i for i in body["items"] if i["kind"] == "entity_bridge")
    assert bridge["production_eligibility"] == "Cardinality unresolved — sandbox only"
    assert bridge["production_eligibility_code"] == "cardinality_unresolved"


# ── Usage: never zero when it cannot measure ──────────────────────────────────────────────────────

def test_every_usage_category_reports_not_tracked_yet_over_the_wire(seeded):
    """The string the screen will render. Every plan-storage table is empty, so a naive count would
    put a 0 on the screen and read as "nothing depends on this bridge"."""
    body = _get(seeded["client"]).json()
    bridge = next(i for i in body["items"] if i["kind"] == "entity_bridge")
    categories = {u["category"] for u in bridge["already_depended_on_by"]}
    assert categories == {"planned_candidates", "selected_plans", "generated_artifacts",
                          "published_features", "data_agent_analyses"}
    for u in bridge["already_depended_on_by"]:
        assert u["state"] == "not_tracked_yet", u["category"]
        assert u["count"] is None
        assert u["display"] == "not tracked yet"
    # ...and no bare "0" reached the payload for this bridge at all.
    assert '"count": 0' not in _get(seeded["client"]).text


def test_a_populated_store_reports_a_real_zero_while_another_stays_not_tracked(seeded):
    """The decisive pair, over HTTP and in ONE response: `planned_candidates` has observations (of a
    DIFFERENT bridge) so its 0 is a measurement; `selected_plans` has none so it must not show one."""
    _record_candidate(seeded["conn"], run="r1", intent="i1", plan="p1",
                      bridge_key="bfk_someone_else", selected=None)
    body = _get(seeded["client"]).json()
    bridge = next(i for i in body["items"] if i["kind"] == "entity_bridge")
    planned = _usage(bridge, "planned_candidates")
    assert planned["state"] == "counted" and planned["count"] == 0 and planned["display"] == "0"
    selected = _usage(bridge, "selected_plans")
    assert selected["state"] == "not_tracked_yet" and selected["count"] is None


def test_the_two_by_construction_categories_carry_their_reason(seeded):
    body = _get(seeded["client"]).json()
    bridge = next(i for i in body["items"] if i["kind"] == "entity_bridge")
    for name in ("generated_artifacts", "data_agent_analyses"):
        u = _usage(bridge, name)
        assert u["state"] == "not_tracked_yet" and u["reason"]


# ── Unreachable is not empty ─────────────────────────────────────────────────────────────────────

def test_an_unreadable_listing_is_reported_rather_than_rendered_as_empty(seeded, monkeypatch):
    """`items: [] , complete: true` means "nothing is waiting". `complete: false` with a populated
    `unreadable` means "we could not look" — a different answer, and a 200 either way."""
    from featuregen.overlay.upload import governance_queue as mod

    def _boom(*_a, **_k):
        raise RuntimeError("the table-fact listing is down")

    monkeypatch.setattr(mod, "list_open_table_fact_proposals_governance", _boom)
    res = _get(seeded["client"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["complete"] is False
    assert any(u["listing"] == "table_fact" for u in body["unreadable"])
    # The rest of the queue still rendered: one broken listing is not a blank screen.
    assert {i["kind"] for i in body["items"]} >= {"entity_bridge", "approved_join"}


def _record_candidate(conn, *, run: str, intent: str, plan: str, bridge_key: str | None,
                      selected: str | None) -> None:
    """One minimal legal migration-1019 (dispatch, intent_result, candidate, operand_obs) chain."""
    crossings = ("[]" if bridge_key is None else
                 '[{"kind": "governed_bridge", "catalog": "core", "table": "customer_master", '
                 f'"bridge_fact_key": "{bridge_key}", "realization_ref": null, '
                 '"authority": "verified_bridge", "confirmed_event_id": null}]')
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_dispatch (run_id, expected_intent_ids, "
        " expected_count, versions, versions_hash, shadow_flag, producer_commit, payload_hash, "
        " payload_schema_version) VALUES (%s, '[]'::jsonb, 0, '{}'::jsonb, 'vh', true, 'c', 'ph', "
        " 'v1') ON CONFLICT (run_id) DO NOTHING", (run,))
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_intent_result (run_id, intent_id, "
        " semantic_outcome, compile_completeness, technical_status, capture_status, "
        " normalized_intent_hash, selected_plan_id, payload_hash, payload_schema_version) "
        "VALUES (%s, %s, 'resolved', 'complete', 'ok', 'persisted', 'nh', %s, 'ph', 'v1') "
        "ON CONFLICT (run_id, intent_id) DO NOTHING", (run, intent, selected))
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_candidate (run_id, intent_id, plan_id, "
        " physical_landing, contract_input_hash, contract_output_hash, read_set_hash, "
        " replay_envelope_hash, rank, declaration_evidence, payload_schema_version) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'a', 'b', 'c', 'd', 0, '{}'::jsonb, 'v1')",
        (run, intent, plan))
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_operand_obs (run_id, intent_id, plan_id, slot_id, "
        " pin, role, path_strategy, governed_endpoints, source_binding, crossings, "
        " payload_schema_version) "
        "VALUES (%s, %s, %s, 'slot1', '{}'::jsonb, 'measure', '{}'::jsonb, '[]'::jsonb, "
        " '{}'::jsonb, %s::jsonb, 'v1')", (run, intent, plan, crossings))
