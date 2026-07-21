"""Tier-1 polish Task 3 — read-only readiness routes (audit I-5: the views existed, no route).

`GET /sources/{source}/readiness/relationships` exposes `compute_relationship_readiness` (the
five-value per-table relationship diagnostic); `GET /sources/{source}/readiness` exposes
`compute_readiness` (the blocker-based FeatureReadiness verdict — CATALOG scope, or TABLE when
`?subset` narrows). Both are READ-ONLY and gated by `catalog:read` (`require_catalog_read`):
catalog_viewer passes, access_admin (iam:manage only) is 403'd.

Seeding mirrors `test_governance_routes`' dual-admin happy path — sealed production config +
graph_node rows (the SP-1.5 referent gate) + a fresh drift watermark, with BOTH confirms driven
through the REAL governance routes — so the VERIFIED approved_join these routes report was
produced by the production propose+confirm path. Universe membership (the `field_decision_event`
refs `_scoped_refs` selects from) comes from `record_field_decision`, mirroring
`test_readiness_join_dim`.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.passc.conftest import SERVICE_ACTOR, _drain
from tests.featuregen.overlay.upload.passc.test_sealed_join_confirm_referents import (
    _seed_graph_nodes,
)
from tests.featuregen.overlay.upload.test_join_governance import _seed_join_with_evidence

from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.config import (
    _clear_overlay_config,
    overlay_config_from_env,
    register_overlay_config,
)
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.field_decision import record_field_decision
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.upload_catalog import (
    UploadCatalog,
    ensure_upload_catalog_adapter,
)


def _h(roles: str = "catalog_viewer", user: str = "u") -> dict:
    return {"X-User": user, "X-Roles": roles}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    """The seeding registers the upload-context adapter and the app lifespan seals an overlay
    config — both PROCESS globals. Clear them after every test in this module so nothing leaks
    into a suite that expects the fail-closed RuntimeError."""
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


@pytest.fixture
def overlay_env(conn):
    """The seeding preconditions the overlay conftests normally provide (this module lives under
    tests/featuregen/api/): the OVERLAY_FACT_* event schemas (the root harness resets the registry
    per test) and the upload-context catalog adapter (`propose_fact` needs one)."""
    register_overlay_event_types(event_registry())
    ensure_upload_catalog_adapter()
    return conn


def _seed_universe_table(conn, table: str, column: str) -> None:
    """Put `table` into the readiness universe (the `field_decision_event` refs `_scoped_refs`
    selects from) with one minimal recorded decision — the `test_readiness_join_dim` shape."""
    record_field_decision(
        conn, logical_ref=normalize_ref("src", None, table, column), field_name="concept",
        event_type="resolved", selected_evidence_ids=(), evidence_set_hash="h0",
        display_value_hash=None, load_bearing_value_hash=None, conflict_status="none",
        reason_codes=(), field_policy_version="fp-test", resolver_version="rv-test",
        actor_ref=None, supersedes_event_id=None)


@pytest.fixture
def verified_join_source(client, overlay_env, conn) -> str:
    """Source 'src' with a VERIFIED approved_join (transactions.cif_id <-> customers.cif_id),
    dual-confirmed through the REAL governance routes under the sealed production config, plus
    readiness-universe membership for both endpoint tables."""
    # Seal from an EMPTY env AFTER the client exists (the app lifespan seals its own env-based
    # config at TestClient startup; register_overlay_config is last-writer-wins).
    register_overlay_config(overlay_config_from_env({}))
    _ref, key = _seed_join_with_evidence(conn)
    _seed_graph_nodes(conn)   # BOTH endpoints must exist in graph_node for the sealed referent gate
    # A fresh drift watermark, the way production writes it (ingest runs detect_catalog_changes).
    detect_catalog_changes(
        conn,
        UploadCatalog("src", [CanonicalRow("src", "transactions", "cif_id", "text"),
                              CanonicalRow("src", "customers", "cif_id", "text", is_grain=True)]),
        actor=SERVICE_ACTOR, open_reverify=False)
    for admin in ("priya", "rahman"):   # two DISTINCT platform-admins -> VERIFIED
        r = client.post(f"/governance/joins/{key}/confirm", json={},
                        headers=_h(roles="platform-admin", user=admin))
        assert r.status_code == 200, r.text
    _drain(conn)   # overlay_proposal read model caught up (candidate source (a))
    _seed_universe_table(conn, "transactions", "cif_id")
    _seed_universe_table(conn, "customers", "cif_id")
    return "src"


# ── (1) GET /sources/{source}/readiness/relationships ────────────────────────────────────────────


def test_relationships_route_reports_confirmed_tables(client, verified_join_source):
    r = client.get(f"/sources/{verified_join_source}/readiness/relationships", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()   # valid JSON — no serialization 500
    assert body["source"] == "src"
    by_table = {x["table"]: x for x in body["relationships"]}
    assert set(by_table) == {"transactions", "customers"}
    for x in by_table.values():
        assert x["status"] == "confirmed"        # the VERIFIED join, on BOTH endpoint tables
        assert x["schema"] == "public"
        (pair,) = x["confirmed_pairs"]
        assert "cif_id" in pair
        assert x["proposed_pairs"] == [] and x["conflicting_pairs"] == []


def test_relationships_route_excludes_other_sources(client, verified_join_source):
    r = client.get("/sources/some-other-source/readiness/relationships", headers=_h())
    assert r.status_code == 200
    assert r.json() == {"source": "some-other-source", "relationships": []}


def test_relationships_route_subset_narrows(client, verified_join_source):
    r = client.get(f"/sources/{verified_join_source}/readiness/relationships?subset=transactions",
                   headers=_h())
    assert r.status_code == 200
    assert [x["table"] for x in r.json()["relationships"]] == ["transactions"]


# ── (2) GET /sources/{source}/readiness ──────────────────────────────────────────────────────────


def test_readiness_route_returns_feature_readiness(client, verified_join_source):
    r = client.get(f"/sources/{verified_join_source}/readiness", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()   # valid JSON — no serialization 500
    assert body["scope"] == "catalog"           # no subset -> CATALOG scope
    assert body["operational_status"] in ("ready", "blocked")
    for k in ("blocking_requirements", "review_requirements", "advisory_gaps", "summary_scores"):
        assert k in body
    # The join dimension reflects REAL state (Task 2): a VERIFIED join is satisfied — it appears
    # in NEITHER actionable list.
    actionable = body["blocking_requirements"] + body["review_requirements"]
    assert not any(q["requirement_id"].startswith("join:") for q in actionable)
    # Grain/availability have no Pass B facts here -> still blocking (proves the verdict is real,
    # not an empty universe).
    assert any(q["requirement_id"].startswith("grain:") for q in body["blocking_requirements"])
    assert body["operational_status"] == "blocked"


def test_readiness_route_subset_scopes_to_table(client, verified_join_source):
    r = client.get(f"/sources/{verified_join_source}/readiness?subset=transactions", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "table"             # subset -> TABLE scope
    reqs = body["blocking_requirements"] + body["review_requirements"]
    assert reqs, "the narrowed table still has requirements"
    assert not any("customers" in q["requirement_id"] for q in reqs)


def test_readiness_route_malformed_subset_is_422_not_500(client, verified_join_source):
    # _scoped_refs rejects a >2-part selector with ValueError — the route must map it to 422.
    for path in (f"/sources/{verified_join_source}/readiness?subset=a.b.c",
                 f"/sources/{verified_join_source}/readiness/relationships?subset=a.b.c"):
        assert client.get(path, headers=_h()).status_code == 422


# ── (3) RBAC: catalog:read required (identity/permissions.py ROLE_PERMISSIONS) ───────────────────


def test_readiness_routes_require_catalog_read(client):
    for path in ("/sources/src/readiness", "/sources/src/readiness/relationships"):
        # access_admin holds ONLY iam:manage — no catalog:read -> 403
        assert client.get(path, headers=_h(roles="access_admin")).status_code == 403
        # catalog_viewer holds catalog:read -> 200 (an empty source is a valid, trivial verdict)
        assert client.get(path, headers=_h(roles="catalog_viewer")).status_code == 200


# ── (4) READ-SCOPE: the standalone routes thread the caller's roles (audit finding [6]) ───────────


def test_readiness_route_is_read_scoped_on_hidden_columns(client, conn):
    """Audit finding [6]: the standalone /readiness route no longer NAMES a hidden pii column for a
    non-pii caller (its field/advisory requirement is pruned — no requirement_id / advisory gap /
    count leaks it), yet a pii_reader still sees it. Proves the route threads role_claims into
    compute_readiness (roles=None=UNSCOPED before the fix)."""
    import json
    # A visible column + a pii-hidden sibling, each with a decided policy field (readiness universe).
    for col, sensitivity in (("amount", None), ("ssn", "pii")):
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, sensitivity) VALUES ('src', %s, 'column', 'people', %s, 'text', %s)",
            (f"public.people.{col}", col, sensitivity))
        _seed_universe_table(conn, "people", col)

    def _names_ssn(headers) -> bool:
        r = client.get("/sources/src/readiness", headers=headers)
        assert r.status_code == 200, r.text
        return "ssn" in json.dumps(r.json())

    assert not _names_ssn(_h(roles="catalog_viewer"))            # non-pii: the pii column is absent
    assert _names_ssn(_h(roles="catalog_viewer,pii_reader"))     # pii_reader: it is named
