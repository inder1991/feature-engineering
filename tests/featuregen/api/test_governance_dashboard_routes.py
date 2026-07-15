"""Phase 4 Task 2 — read-only governance dashboard routes over the Task-1 read model.

`GET /governance/dashboard` exposes `compute_governance_dashboard(source=None)` plus the
per-source `list_source_governance_summaries` list; `GET /sources/{source}/governance/dashboard`
exposes the single-source dashboard. Both are READ-ONLY and gated by `catalog:read`
(`require_catalog_read`): catalog_viewer passes, access_admin (iam:manage only) is 403'd.

Seeding mirrors `test_readiness_routes` (which mirrors `test_governance_routes`' dual-admin happy
path): sealed production config + graph_node rows (the SP-1.5 referent gate) + a fresh drift
watermark, with BOTH confirms driven through the REAL governance routes — so the VERIFIED
approved_join these dashboards count was produced by the production propose+confirm path.
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
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.upload_catalog import (
    UploadCatalog,
    ensure_upload_catalog_adapter,
)

_FACT_TYPES = {"approved_join", "grain", "availability_time"}


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


@pytest.fixture
def verified_join_source(client, overlay_env, conn) -> str:
    """Source 'src' with a VERIFIED approved_join (transactions.cif_id <-> customers.cif_id),
    dual-confirmed through the REAL governance routes under the sealed production config."""
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
    return "src"


# ── (1) GET /governance/dashboard (cross-source + sources list) ──────────────────────────────────


def test_cross_source_dashboard(client, verified_join_source):
    r = client.get("/governance/dashboard", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()   # valid JSON — no serialization 500
    assert body["scope"] == "catalog" and body["source"] is None
    for k in ("fact_types", "sources", "queue_health", "calibration_seed", "recent_activity"):
        assert k in body
    by_type = {ft["fact_type"]: ft for ft in body["fact_types"]}
    assert set(by_type) == _FACT_TYPES   # always all three, even where zero
    assert by_type["approved_join"]["confirmed"] >= 1   # the dual-confirmed VERIFIED join
    (summary,) = [s for s in body["sources"] if s["source"] == verified_join_source]
    assert summary["confirmed"] >= 1


def test_per_source_dashboard(client, verified_join_source):
    r = client.get(f"/sources/{verified_join_source}/governance/dashboard", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "source" and body["source"] == verified_join_source
    assert "sources" not in body   # the source list is a catalog-level shape only
    by_type = {ft["fact_type"]: ft for ft in body["fact_types"]}
    assert by_type["approved_join"]["confirmed"] >= 1


def test_per_source_normalizes_source(client, verified_join_source):
    r = client.get("/sources/%20SRC%20/governance/dashboard", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "src"   # strip+lower normalization
    assert {ft["fact_type"]: ft for ft in body["fact_types"]}["approved_join"]["confirmed"] >= 1


# ── (2) Unknown source is an all-zeros dashboard, NOT 404 ────────────────────────────────────────


def test_unknown_source_is_zeros_not_404(client):
    r = client.get("/sources/nope/governance/dashboard", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "nope"
    assert {ft["fact_type"] for ft in body["fact_types"]} == _FACT_TYPES
    for ft in body["fact_types"]:
        assert ft["pending"] == 0 and ft["confirmed"] == 0 and ft["rejected"] == 0
    assert body["queue_health"]["open_depth"] == 0


# ── (3) RBAC: catalog:read required (identity/permissions.py ROLE_PERMISSIONS) ───────────────────


def test_dashboard_routes_require_catalog_read(client):
    for path in ("/governance/dashboard", "/sources/src/governance/dashboard"):
        # access_admin holds ONLY iam:manage — no catalog:read -> 403
        assert client.get(path, headers=_h(roles="access_admin")).status_code == 403
        # catalog_viewer holds catalog:read -> 200 (an empty catalog is a valid zero dashboard)
        assert client.get(path, headers=_h(roles="catalog_viewer")).status_code == 200
