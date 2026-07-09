"""Connector CRUD + OpenMetadata preview/import API tests.

All OM traffic is served from recorded fixture pages injected through the FetchPage seam
(_build_fetch is monkeypatched) — no network. The token lives ONLY in an env var; tests assert
its value never appears in any response body.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api._helpers import AUTH, ENGINEER, OWNER, VIEWER
from tests.featuregen.connectors._fixtures import CARDS_TAG_MAP, fixture_fetch, fixture_pages

TOKEN_ENV = "FEATUREGEN_OM_TOKEN__CARDS_OM"
TOKEN_VALUE = "secret-bot-token-v-9"


@pytest.fixture(autouse=True)
def _om_seam(monkeypatch):
    """Fixture-backed transport + a configured token env var + the egress allowlist that every
    happy-path test needs (the connector base_url is https://om.internal.test)."""
    from featuregen.api.routes import connectors as routes

    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setenv("FEATUREGEN_OM_ALLOWED_HOSTS", "om.internal.test, om.other.test:8585")
    monkeypatch.setattr(routes, "_build_fetch", lambda base_url, token: fixture_fetch())


def _create(client, headers=OWNER, **overrides):
    body = {"name": "cards om", "base_url": "https://om.internal.test",
            "target_source": "cards", "tag_map": CARDS_TAG_MAP, **overrides}
    return client.post("/connectors", json=body, headers=headers)


# ---- CRUD -------------------------------------------------------------------------------------


def test_create_list_delete_connector(client):
    created = _create(client)
    assert created.status_code == 200
    cfg = created.json()
    assert cfg["token_env"] == "FEATUREGEN_OM_TOKEN__CARDS_OM"   # derived reference, no secret
    assert cfg["token_present"] is True
    assert cfg["created_by"] == "user:o"

    listed = client.get("/connectors", headers=VIEWER)
    assert [c["connector_id"] for c in listed.json()] == [cfg["connector_id"]]

    assert client.delete(f"/connectors/{cfg['connector_id']}", headers=OWNER).json() == \
        {"deleted": True}
    assert client.get("/connectors", headers=VIEWER).json() == []


def test_delete_unknown_connector_404(client):
    assert client.delete("/connectors/conn_missing", headers=OWNER).status_code == 404


def test_duplicate_name_409(client):
    assert _create(client).status_code == 200
    assert _create(client).status_code == 409


def test_plaintext_token_field_rejected(client):
    res = _create(client, token="raw-secret")     # extra field -> 422, never stored
    assert res.status_code == 422


def test_invalid_tag_map_value_rejected(client):
    res = _create(client, tag_map={"PII.Sensitive": "very_secret"})
    assert res.status_code == 400
    assert "tag_map" in res.json()["detail"]


def test_invalid_filter_key_rejected(client):
    assert _create(client, filters={"cluster": "x"}).status_code == 400


# ---- CRITICAL: egress allowlist + token namespace --------------------------------------------


def test_token_env_outside_namespace_rejected(client):
    """A token reference MUST name the connector-token namespace — otherwise a catalog:write user
    could point a config row at an arbitrary secret (a DSN, a KMS key) and egress it as a Bearer
    header. The prefix is named in the 400 so the operator knows the rule."""
    res = _create(client, token_env="FEATUREGEN_DSN")
    assert res.status_code == 400
    assert "FEATUREGEN_OM_TOKEN__" in res.json()["detail"]
    # the derived default (no token_env supplied) is always in-namespace and accepted
    assert _create(client).status_code == 200


def test_create_fails_closed_when_no_hosts_allowlisted(client, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_OM_ALLOWED_HOSTS")
    res = _create(client)
    assert res.status_code == 400
    assert res.json()["detail"] == \
        "no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS"


def test_create_rejects_host_not_on_allowlist(client):
    res = _create(client, base_url="https://attacker.example")
    assert res.status_code == 400
    assert "not allowlisted" in res.json()["detail"]
    assert "attacker.example" in res.json()["detail"]


def test_preview_fails_closed_when_allowlist_removed_after_create(client, monkeypatch):
    """The allowlist is re-checked on every pull, not just at create: a row that predates the
    allowlist (or one created before it was tightened) still cannot pull off an unlisted host."""
    cfg = _create(client).json()
    monkeypatch.delenv("FEATUREGEN_OM_ALLOWED_HOSTS")
    res = _preview(client, cfg["connector_id"])
    assert res.status_code == 400
    assert res.json()["detail"] == \
        "no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS"


def test_allowlisted_host_with_explicit_port_works(client):
    """host:port entries match exactly; om.other.test:8585 is on the allowlist."""
    res = _create(client, name="other om", base_url="https://om.other.test:8585")
    assert res.status_code == 200


def test_malformed_port_fails_closed_not_500(client):
    """A non-numeric port must not blow up port parsing into a 500 — it matches nothing (400)."""
    res = _create(client, base_url="https://om.internal.test:abc")
    assert res.status_code == 400
    assert "not allowlisted" in res.json()["detail"]


def test_rbac_config_requires_catalog_write(client):
    assert _create(client, headers=VIEWER).status_code == 403
    assert _create(client, headers=ENGINEER).status_code == 403
    cfg = _create(client).json()
    assert client.delete(f"/connectors/{cfg['connector_id']}", headers=VIEWER).status_code == 403


# ---- Preview ----------------------------------------------------------------------------------


def _preview(client, connector_id, headers=VIEWER):
    return client.post("/connectors/openmetadata/preview",
                       json={"connector_id": connector_id}, headers=headers)


def test_preview_dry_run_shape_and_verdicts(client, conn):
    cfg = _create(client).json()
    res = _preview(client, cfg["connector_id"])    # catalog_viewer may preview
    assert res.status_code == 200
    preview = res.json()
    assert set(preview) == {"summary", "tag_map", "tables", "brake", "as_of_suggestions",
                            "snapshot_hash"}
    assert preview["summary"] == {"tables": 3, "columns": 14, "new": 3, "changed": 0,
                                  "unchanged": 0, "removed": 0, "would_quarantine": 1,
                                  "semantics_pending": 13}
    assert preview["tag_map"] == [
        {"om_tag": "Confidential.Internal", "mapped_to": "", "unmapped": True, "count": 1},
        {"om_tag": "PII.Sensitive", "mapped_to": "pii", "unmapped": False, "count": 1},
    ]
    customers = next(t for t in preview["tables"] if t["table"] == "customers")
    assert customers["status"] == "new"
    assert customers["quarantine"][0]["column"] == "ssn"
    assert {"table": "accounts", "column": "opened_on",
            "hint": "partition column (TIME-UNIT)"} in preview["as_of_suggestions"]
    assert preview["brake"] == {"would_hold": False, "reason": None}
    # preview NEVER writes: no graph nodes, no quarantine rows, no drift snapshot
    for table in ("graph_node", "quarantine_row", "overlay_catalog_object"):
        assert conn.execute(
            f"SELECT count(*) FROM {table} WHERE catalog_source = 'cards'").fetchone()[0] == 0


def test_preview_unknown_connector_404(client):
    assert _preview(client, "conn_missing").status_code == 404


def test_preview_missing_token_400_names_the_env_var(client, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV)
    cfg = _create(client).json()
    res = _preview(client, cfg["connector_id"])
    assert res.status_code == 400
    assert TOKEN_ENV in res.json()["detail"]


def test_preview_maps_upstream_auth_and_unreachable(client, monkeypatch):
    from featuregen.api.routes import connectors as routes
    from featuregen.connectors.openmetadata import OMAuthRejected, OMUnreachable

    cfg = _create(client).json()

    def rejecting(base_url, token):
        def fetch(path, params):
            raise OMAuthRejected("OpenMetadata rejected the connector token (HTTP 401)")
        return fetch

    monkeypatch.setattr(routes, "_build_fetch", rejecting)
    assert _preview(client, cfg["connector_id"]).status_code == 401

    def unreachable(base_url, token):
        def fetch(path, params):
            raise OMUnreachable("OpenMetadata unreachable: connect timeout")
        return fetch

    monkeypatch.setattr(routes, "_build_fetch", unreachable)
    assert _preview(client, cfg["connector_id"]).status_code == 502


# ---- Import -----------------------------------------------------------------------------------


def _import(client, connector_id, snapshot_hash, headers=OWNER):
    return client.post("/connectors/openmetadata/import",
                       json={"connector_id": connector_id, "snapshot_hash": snapshot_hash},
                       headers=headers)


def test_import_runs_the_unchanged_ingest_pipeline(client, conn):
    cfg = _create(client).json()
    snapshot = _preview(client, cfg["connector_id"]).json()["snapshot_hash"]

    res = _import(client, cfg["connector_id"], snapshot)
    assert res.status_code == 200
    body = res.json()
    assert body["result"]["status"] == "ingested"
    assert body["result"]["quarantined"] == 1
    assert body["review_queue"] == {"quarantined": 1, "semantics_pending": 13}
    assert body["import_id"].startswith("omimp_")

    # the standard pipeline artifacts exist: graph, quarantine queue, drift watermark
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'cards' "
                        "AND kind = 'column'").fetchone()[0] == 13
    q = conn.execute("SELECT raw->>'column', reason FROM quarantine_row "
                     "WHERE catalog_source = 'cards'").fetchall()
    assert len(q) == 1 and q[0][0] == "ssn" and "unrecognized sensitivity" in q[0][1]
    assert conn.execute("SELECT count(*) FROM overlay_drift_watermark "
                        "WHERE catalog_source = 'cards'").fetchone()[0] == 1

    # the import record: approving human + the connector as the vehicle
    rec = conn.execute(
        "SELECT connector_id, snapshot_hash, approved_by, vehicle, result->>'status' "
        "FROM connector_import WHERE import_id = %s", (body["import_id"],)).fetchone()
    assert rec == (cfg["connector_id"], snapshot, "user:o", "openmetadata-connector", "ingested")


def test_import_snapshot_mismatch_409_and_nothing_ingested(client, conn, monkeypatch):
    from featuregen.api.routes import connectors as routes

    cfg = _create(client).json()
    stale = _preview(client, cfg["connector_id"]).json()["snapshot_hash"]

    # OM moves between preview and import: a column disappears from page 2
    page1, page2 = fixture_pages()
    del page2["data"][0]["columns"][2]
    monkeypatch.setattr(routes, "_build_fetch",
                        lambda base_url, token: fixture_fetch(page1, page2))

    res = _import(client, cfg["connector_id"], stale)
    assert res.status_code == 409
    assert "preview again" in res.json()["detail"]
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'cards'"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM connector_import").fetchone()[0] == 0


def test_import_held_by_brake_is_recorded_honestly(client, conn):
    """A pull that would remove most of the source is HELD by the same brake as a hostile upload;
    the import record still exists (audit of the attempt) with status 'held'."""
    from tests.featuregen._helpers import make_actor

    from featuregen.events.registry import event_registry
    from featuregen.overlay.facts import register_overlay_event_types
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.ingest import ingest_upload

    register_overlay_event_types(event_registry())
    actor = make_actor(subject="user:owner", roles=("data_owner",))
    big = [CanonicalRow(source="cards", table=f"legacy_{t}", column=f"col_{c}", type="text")
           for t in range(5) for c in range(5)]
    assert ingest_upload(conn, "cards", big, actor=actor).status == "ingested"

    cfg = _create(client).json()
    preview = _preview(client, cfg["connector_id"]).json()
    assert preview["brake"]["would_hold"] is True                # preview PREDICTED the hold

    res = _import(client, cfg["connector_id"], preview["snapshot_hash"])
    assert res.status_code == 200
    body = res.json()
    assert body["result"]["status"] == "held"
    assert body["review_queue"]["semantics_pending"] == 0        # nothing landed
    # the prior catalog is untouched by the held sync
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'cards' "
                        "AND kind = 'column'").fetchone()[0] == 25


def test_import_rbac_requires_catalog_write(client):
    cfg = _create(client).json()
    snapshot = _preview(client, cfg["connector_id"]).json()["snapshot_hash"]
    assert _import(client, cfg["connector_id"], snapshot, headers=VIEWER).status_code == 403
    assert _import(client, cfg["connector_id"], snapshot, headers=ENGINEER).status_code == 403


def test_token_value_never_serialized_in_any_response(client):
    cfg_res = _create(client, headers=AUTH)
    cfg = cfg_res.json()
    responses = [
        cfg_res,
        client.get("/connectors", headers=AUTH),
        _preview(client, cfg["connector_id"], headers=AUTH),
    ]
    preview = responses[-1].json()
    responses.append(_import(client, cfg["connector_id"], preview["snapshot_hash"], headers=AUTH))
    for res in responses:
        assert TOKEN_VALUE not in res.text
