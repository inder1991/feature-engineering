"""Two-tier connector API tests: integration CRUD + service discovery + sync CRUD + preview/import.

All OM traffic is served from recorded fixture pages injected through the FetchPage seam
(_build_fetch is monkeypatched) — no network. The token lives ONLY in an env var; tests assert its
value never appears in any response body, including discovery and the import record.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api._helpers import AUTH, ENGINEER, OWNER, VIEWER
from tests.featuregen.connectors._fixtures import (
    CARDS_SERVICE,
    CARDS_TAG_MAP,
    fixture_fetch,
    fixture_pages,
)

TOKEN_ENV = "FEATUREGEN_OM_TOKEN__CORP_OM"
TOKEN_VALUE = "secret-bot-token-v-9"


@pytest.fixture(autouse=True)
def _om_seam(monkeypatch):
    """Fixture-backed transport (serving both discovery + tables) + a configured token env var + the
    egress allowlist every happy-path test needs (base_url is https://om.internal.test)."""
    from featuregen.api.routes import integrations as routes

    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setenv("FEATUREGEN_OM_ALLOWED_HOSTS", "om.internal.test, om.other.test:8585")
    monkeypatch.setattr(routes, "_build_fetch", lambda base_url, token: fixture_fetch())


# ---- helpers ---------------------------------------------------------------------------------


def _create_integration(client, headers=OWNER, **overrides):
    body = {"name": "corp om", "base_url": "https://om.internal.test",
            "tag_map": CARDS_TAG_MAP, **overrides}
    return client.post("/integrations", json=body, headers=headers)

def _integration_id(client, **overrides):
    return _create_integration(client, **overrides).json()["integration_id"]

def _create_sync(client, integration_id, headers=OWNER, **overrides):
    body = {"service_name": CARDS_SERVICE, "target_source": "cards", **overrides}
    return client.post(f"/integrations/{integration_id}/syncs", json=body, headers=headers)

def _preview(client, sync_id, headers=VIEWER):
    return client.post(f"/syncs/{sync_id}/preview", headers=headers)

def _import(client, sync_id, snapshot_hash, local_baseline_hash="", headers=OWNER):
    return client.post(f"/syncs/{sync_id}/import",
                       json={"snapshot_hash": snapshot_hash,
                             "local_baseline_hash": local_baseline_hash}, headers=headers)

def _configured_sync(client):
    """An integration + a cards sync on mysql_prod, returning (integration_id, sync_id)."""
    iid = _integration_id(client)
    sid = _create_sync(client, iid).json()["sync_id"]
    return iid, sid


# ---- Migration -------------------------------------------------------------------------------


def test_migration_creates_two_tier_tables_and_drops_flat(conn):
    for tbl in ("integration", "integration_sync", "integration_import"):
        assert conn.execute("SELECT to_regclass(%s)", (tbl,)).fetchone()[0] is not None
    for gone in ("connector_config", "connector_import"):
        assert conn.execute("SELECT to_regclass(%s)", (gone,)).fetchone()[0] is None


# ---- Integration CRUD ------------------------------------------------------------------------


def test_create_list_get_delete_integration(client):
    created = _create_integration(client)
    assert created.status_code == 200
    integ = created.json()
    assert integ["integration_id"].startswith("intg_")
    assert integ["token_env"] == TOKEN_ENV          # derived reference, no secret
    assert integ["token_present"] is True
    assert integ["created_by"] == "user:o"
    assert "token" not in integ                      # only the reference + presence flag

    listed = client.get("/integrations", headers=VIEWER)
    assert [i["integration_id"] for i in listed.json()] == [integ["integration_id"]]

    got = client.get(f"/integrations/{integ['integration_id']}", headers=VIEWER)
    assert got.json()["name"] == "corp om"

    assert client.delete(f"/integrations/{integ['integration_id']}", headers=OWNER).json() == \
        {"deleted": True}
    assert client.get("/integrations", headers=VIEWER).json() == []


def test_get_unknown_integration_404(client):
    assert client.get("/integrations/intg_missing", headers=VIEWER).status_code == 404
    assert client.delete("/integrations/intg_missing", headers=OWNER).status_code == 404


def test_duplicate_integration_name_409(client):
    assert _create_integration(client).status_code == 200
    assert _create_integration(client).status_code == 409


def test_plaintext_token_field_rejected(client):
    assert _create_integration(client, token="raw-secret").status_code == 422


def test_invalid_tag_map_value_rejected(client):
    res = _create_integration(client, tag_map={"PII.Sensitive": "very_secret"})
    assert res.status_code == 400
    assert "tag_map" in res.json()["detail"]


def test_patch_integration_updates_and_revalidates(client):
    integ = _create_integration(client).json()
    iid = integ["integration_id"]

    ok = client.patch(f"/integrations/{iid}",
                      json={"tag_map": {"PII.Sensitive": "restricted"}}, headers=OWNER)
    assert ok.status_code == 200
    assert ok.json()["tag_map"] == {"PII.Sensitive": "restricted"}

    # re-validation on PATCH: off-allowlist host, off-namespace token, bad tag value all 400
    assert client.patch(f"/integrations/{iid}", json={"base_url": "https://attacker.example"},
                        headers=OWNER).status_code == 400
    assert client.patch(f"/integrations/{iid}", json={"token_env": "FEATUREGEN_DSN"},
                        headers=OWNER).status_code == 400
    assert client.patch(f"/integrations/{iid}", json={"tag_map": {"X": "nope"}},
                        headers=OWNER).status_code == 400


def test_patch_integration_name_collision_409(client):
    a = _create_integration(client).json()["integration_id"]
    _create_integration(client, name="other om")
    assert client.patch(f"/integrations/{a}", json={"name": "other om"},
                        headers=OWNER).status_code == 409


def test_delete_integration_cascades_syncs(client, conn):
    iid, sid = _configured_sync(client)
    assert conn.execute("SELECT count(*) FROM integration_sync WHERE integration_id = %s",
                        (iid,)).fetchone()[0] == 1
    assert client.delete(f"/integrations/{iid}", headers=OWNER).status_code == 200
    assert conn.execute("SELECT count(*) FROM integration_sync WHERE integration_id = %s",
                        (iid,)).fetchone()[0] == 0


# ---- CRITICAL: egress allowlist + token namespace --------------------------------------------


def test_token_env_outside_namespace_rejected(client):
    """A token reference MUST name the connector-token namespace — otherwise a catalog:write user
    could point a row at an arbitrary secret (a DSN, a KMS key) and egress it as a Bearer header."""
    res = _create_integration(client, token_env="FEATUREGEN_DSN")
    assert res.status_code == 400
    assert "FEATUREGEN_OM_TOKEN__" in res.json()["detail"]
    assert _create_integration(client).status_code == 200   # derived default is in-namespace


def test_create_fails_closed_when_no_hosts_allowlisted(client, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_OM_ALLOWED_HOSTS")
    res = _create_integration(client)
    assert res.status_code == 400
    assert res.json()["detail"] == \
        "no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS"


def test_create_rejects_host_not_on_allowlist(client):
    res = _create_integration(client, base_url="https://attacker.example")
    assert res.status_code == 400
    assert "not allowlisted" in res.json()["detail"]
    assert "attacker.example" in res.json()["detail"]


def test_allowlisted_host_with_explicit_port_works(client):
    assert _create_integration(client, name="other om",
                               base_url="https://om.other.test:8585").status_code == 200


def test_malformed_port_fails_closed_not_500(client):
    res = _create_integration(client, base_url="https://om.internal.test:abc")
    assert res.status_code == 400
    assert "not allowlisted" in res.json()["detail"]


def test_rbac_integration_writes_require_catalog_write(client):
    assert _create_integration(client, headers=VIEWER).status_code == 403
    assert _create_integration(client, headers=ENGINEER).status_code == 403
    iid = _integration_id(client)
    assert client.patch(f"/integrations/{iid}", json={"name": "x"},
                        headers=VIEWER).status_code == 403
    assert client.delete(f"/integrations/{iid}", headers=VIEWER).status_code == 403


# ---- Service discovery -----------------------------------------------------------------------


def _services(client, integration_id, headers=VIEWER):
    return client.get(f"/integrations/{integration_id}/services", headers=headers)


def test_discovery_lists_services_with_synced_flags(client):
    iid = _integration_id(client)
    sid = _create_sync(client, iid).json()["sync_id"]     # mysql_prod is now synced

    res = _services(client, iid)
    assert res.status_code == 200
    by_name = {s["service_name"]: s for s in res.json()}
    assert by_name["mysql_prod"] == {"service_name": "mysql_prod", "service_type": "Mysql",
                                     "fqn": "mysql_prod", "synced": True, "sync_id": sid}
    assert by_name["snowflake_dwh"]["service_type"] == "Snowflake"
    assert by_name["snowflake_dwh"]["synced"] is False
    assert by_name["snowflake_dwh"]["sync_id"] is None
    assert by_name["bq_marketing"]["service_type"] == "BigQuery"


def test_discovery_requires_catalog_read(client):
    iid = _integration_id(client)
    assert _services(client, iid, headers=VIEWER).status_code == 200


def test_discovery_unknown_integration_404(client):
    assert _services(client, "intg_missing").status_code == 404


def test_discovery_re_checks_allowlist(client, monkeypatch):
    iid = _integration_id(client)
    monkeypatch.delenv("FEATUREGEN_OM_ALLOWED_HOSTS")
    res = _services(client, iid)
    assert res.status_code == 400
    assert res.json()["detail"] == \
        "no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS"


def test_discovery_maps_upstream_auth_and_unreachable(client, monkeypatch):
    from featuregen.api.routes import integrations as routes
    from featuregen.connectors.openmetadata import OMAuthRejected, OMUnreachable

    iid = _integration_id(client)

    def rejecting(base_url, token):
        def fetch(path, params):
            raise OMAuthRejected("OpenMetadata rejected the connector token (HTTP 401)")
        return fetch

    monkeypatch.setattr(routes, "_build_fetch", rejecting)
    assert _services(client, iid).status_code == 401

    def unreachable(base_url, token):
        def fetch(path, params):
            raise OMUnreachable("OpenMetadata unreachable: connect timeout")
        return fetch

    monkeypatch.setattr(routes, "_build_fetch", unreachable)
    assert _services(client, iid).status_code == 502


def test_discovery_missing_token_400_names_the_env_var(client, monkeypatch):
    iid = _integration_id(client)
    monkeypatch.delenv(TOKEN_ENV)
    res = _services(client, iid)
    assert res.status_code == 400
    assert TOKEN_ENV in res.json()["detail"]


# ---- Sync CRUD -------------------------------------------------------------------------------


def test_create_list_get_patch_delete_sync(client):
    iid = _integration_id(client)
    created = _create_sync(client, iid, database_filter="cards_db", schema_filter="public")
    assert created.status_code == 200
    sync = created.json()
    assert sync["sync_id"].startswith("sync_")
    assert sync["service_name"] == "mysql_prod"
    assert sync["target_source"] == "cards"
    assert sync["database_filter"] == "cards_db"
    assert sync["last_import_at"] is None
    assert sync["created_by"] == "user:o"

    listed = client.get(f"/integrations/{iid}/syncs", headers=VIEWER)
    assert [s["sync_id"] for s in listed.json()] == [sync["sync_id"]]

    got = client.get(f"/integrations/{iid}/syncs/{sync['sync_id']}", headers=VIEWER)
    assert got.json()["sync_id"] == sync["sync_id"]

    patched = client.patch(f"/integrations/{iid}/syncs/{sync['sync_id']}",
                          json={"target_source": "cards2", "table_naming": "schema_table"},
                          headers=OWNER)
    assert patched.status_code == 200
    assert patched.json()["target_source"] == "cards2"
    assert patched.json()["table_naming"] == "schema_table"

    assert client.delete(f"/integrations/{iid}/syncs/{sync['sync_id']}",
                        headers=OWNER).json() == {"deleted": True}
    assert client.get(f"/integrations/{iid}/syncs", headers=VIEWER).json() == []


def test_sync_target_source_required(client):
    iid = _integration_id(client)
    assert _create_sync(client, iid, target_source="  ").status_code == 400


def test_sync_ids_stripped_before_store(client):
    """#16: service/source ids are stripped BEFORE they are stored — ' cards ' and 'cards' must be
    ONE catalog, and a padded service name must occupy the same one-sync-per-service slot."""
    iid = _integration_id(client)
    created = _create_sync(client, iid, service_name=f" {CARDS_SERVICE} ",
                           target_source=" cards ")
    assert created.status_code == 200
    sync = created.json()
    assert sync["service_name"] == CARDS_SERVICE
    assert sync["target_source"] == "cards"
    # the padded create claimed the exact service name: an unpadded duplicate is refused
    assert _create_sync(client, iid).status_code == 409


def test_patch_sync_strips_ids(client):
    """#16 (patch path): edited ids are stripped before they replace the stored ones."""
    iid, sid = _configured_sync(client)
    res = client.patch(f"/integrations/{iid}/syncs/{sid}",
                       json={"target_source": " retail ", "service_name": " svc_2 "},
                       headers=OWNER)
    assert res.status_code == 200
    assert res.json()["target_source"] == "retail"
    assert res.json()["service_name"] == "svc_2"


def test_sync_target_source_lowercased_service_name_case_preserved(client):
    """#16 follow-up: target_source IS the catalog identity, and identity is strip+LOWER everywhere
    else (object_ref._norm) — a 'Cards' sync must feed the SAME catalog as 'cards', or its imports
    bypass the large-change brake as a fresh catalog while facts key on the lowered stream. The
    service_name is only STRIPPED: it names an external OpenMetadata service, where case may matter."""
    iid = _integration_id(client)
    created = _create_sync(client, iid, service_name="MySQL_Prod", target_source=" Cards ")
    assert created.status_code == 200
    sync = created.json()
    assert sync["target_source"] == "cards"
    assert sync["service_name"] == "MySQL_Prod"


def test_patch_sync_lowercases_target_source_only(client):
    """#16 follow-up (patch path): same rule as create — catalog identity folds case, the OM
    service name keeps it."""
    iid, sid = _configured_sync(client)
    res = client.patch(f"/integrations/{iid}/syncs/{sid}",
                       json={"target_source": " Retail ", "service_name": " Svc_2 "},
                       headers=OWNER)
    assert res.status_code == 200
    assert res.json()["target_source"] == "retail"
    assert res.json()["service_name"] == "Svc_2"


def test_one_sync_per_service_409(client):
    iid = _integration_id(client)
    assert _create_sync(client, iid).status_code == 200
    dup = _create_sync(client, iid)
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]
    # a DIFFERENT service on the same integration is fine
    assert _create_sync(client, iid, service_name="snowflake_dwh",
                        target_source="deposits").status_code == 200


def test_sync_create_does_not_depend_on_discovery(client, monkeypatch):
    """OM discovery being down must not block configuring a sync — the service_name can be typed by
    hand. The create path never calls OM."""
    from featuregen.api.routes import integrations as routes
    from featuregen.connectors.openmetadata import OMUnreachable

    iid = _integration_id(client)

    def down(base_url, token):
        def fetch(path, params):
            raise OMUnreachable("OpenMetadata unreachable")
        return fetch

    monkeypatch.setattr(routes, "_build_fetch", down)
    assert _services(client, iid).status_code == 502          # discovery is indeed down
    assert _create_sync(client, iid, service_name="typed_by_hand").status_code == 200


def test_sync_invalid_tag_map_override_rejected(client):
    iid = _integration_id(client)
    assert _create_sync(client, iid,
                        tag_map_override={"PII.Sensitive": "nope"}).status_code == 400


def test_create_sync_under_unknown_integration_404(client):
    assert _create_sync(client, "intg_missing").status_code == 404


def test_rbac_sync_writes_require_catalog_write(client):
    iid = _integration_id(client)
    assert _create_sync(client, iid, headers=VIEWER).status_code == 403
    assert _create_sync(client, iid, headers=ENGINEER).status_code == 403
    sid = _create_sync(client, iid).json()["sync_id"]
    # PATCH is a write too: a catalog_viewer-only identity cannot edit a sync.
    assert client.patch(f"/integrations/{iid}/syncs/{sid}",
                        json={"target_source": "cards2"}, headers=VIEWER).status_code == 403
    assert client.delete(f"/integrations/{iid}/syncs/{sid}", headers=VIEWER).status_code == 403


# ---- Preview ---------------------------------------------------------------------------------


def test_preview_dry_run_shape_and_verdicts(client, conn):
    _, sid = _configured_sync(client)
    res = _preview(client, sid)                     # catalog_viewer may preview
    assert res.status_code == 200
    preview = res.json()
    assert set(preview) == {"summary", "tag_map", "tables", "brake", "as_of_suggestions",
                            "collisions", "dropped_joins", "snapshot_hash",
                            "local_baseline_hash"}
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
    # preview NEVER writes
    for table in ("graph_node", "quarantine_row", "overlay_catalog_object"):
        assert conn.execute(
            f"SELECT count(*) FROM {table} WHERE catalog_source = 'cards'").fetchone()[0] == 0


def test_preview_merges_tag_map_override_over_integration_default(client):
    """Effective tag map = integration default MERGED with the sync override (override wins per
    tag). The integration maps PII.Sensitive->pii; the sync ALSO ignores Confidential.Internal, so
    ssn stops quarantining — proving both inheritance and override in one pull."""
    iid = _integration_id(client)
    sid = _create_sync(client, iid,
                       tag_map_override={"Confidential.Internal": ""}).json()["sync_id"]
    preview = _preview(client, sid).json()
    assert preview["summary"]["would_quarantine"] == 0        # ssn now ignored, not quarantined
    panel = {e["om_tag"]: e for e in preview["tag_map"]}
    assert panel["PII.Sensitive"]["mapped_to"] == "pii"       # inherited from the integration
    assert panel["Confidential.Internal"] == {                # added by the sync override
        "om_tag": "Confidential.Internal", "mapped_to": "", "unmapped": False, "count": 1}


def test_preview_override_wins_over_integration_for_same_tag(client):
    iid = _integration_id(client, tag_map={"PII.Sensitive": "restricted"})
    sid = _create_sync(client, iid,
                       tag_map_override={"PII.Sensitive": "pii"}).json()["sync_id"]
    panel = {e["om_tag"]: e for e in _preview(client, sid).json()["tag_map"]}
    assert panel["PII.Sensitive"]["mapped_to"] == "pii"       # sync override wins, not 'restricted'


def test_preview_unknown_sync_404(client):
    assert _preview(client, "sync_missing").status_code == 404


def test_preview_missing_token_400_names_the_env_var(client, monkeypatch):
    _, sid = _configured_sync(client)
    monkeypatch.delenv(TOKEN_ENV)
    res = _preview(client, sid)
    assert res.status_code == 400
    assert TOKEN_ENV in res.json()["detail"]


def test_preview_re_checks_allowlist(client, monkeypatch):
    _, sid = _configured_sync(client)
    monkeypatch.delenv("FEATUREGEN_OM_ALLOWED_HOSTS")
    res = _preview(client, sid)
    assert res.status_code == 400
    assert res.json()["detail"] == \
        "no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS"


def test_preview_maps_upstream_auth_and_unreachable(client, monkeypatch):
    from featuregen.api.routes import integrations as routes
    from featuregen.connectors.openmetadata import OMAuthRejected, OMUnreachable

    _, sid = _configured_sync(client)

    def rejecting(base_url, token):
        def fetch(path, params):
            raise OMAuthRejected("OpenMetadata rejected the connector token (HTTP 401)")
        return fetch

    monkeypatch.setattr(routes, "_build_fetch", rejecting)
    assert _preview(client, sid).status_code == 401

    def unreachable(base_url, token):
        def fetch(path, params):
            raise OMUnreachable("OpenMetadata unreachable: connect timeout")
        return fetch

    monkeypatch.setattr(routes, "_build_fetch", unreachable)
    assert _preview(client, sid).status_code == 502


# ---- Import ----------------------------------------------------------------------------------


def test_import_runs_the_unchanged_ingest_pipeline(client, conn):
    iid, sid = _configured_sync(client)
    pv = _preview(client, sid).json()
    snapshot = pv["snapshot_hash"]

    # approval against an UNCHANGED local baseline succeeds (#13)
    res = _import(client, sid, snapshot, pv["local_baseline_hash"])
    assert res.status_code == 200
    body = res.json()
    assert body["result"]["status"] == "ingested"
    assert body["result"]["quarantined"] == 1
    # #25 HONESTY: semantics_pending is an informational COUNT of landed columns awaiting owner
    # confirmation — the import creates NO review records for them, so the response must not
    # claim a routed queue. (Quarantined rows, reported inside result, DO land in the real
    # quarantine review queue.)
    assert body["semantics_pending"] == 13
    assert "review_queue" not in body
    assert body["import_id"].startswith("omimp_")

    # the standard pipeline artifacts exist
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'cards' "
                        "AND kind = 'column'").fetchone()[0] == 13
    q = conn.execute("SELECT raw->>'column', reason FROM quarantine_row "
                     "WHERE catalog_source = 'cards'").fetchall()
    assert len(q) == 1 and q[0][0] == "ssn" and "unrecognized sensitivity" in q[0][1]
    assert conn.execute("SELECT count(*) FROM overlay_drift_watermark "
                        "WHERE catalog_source = 'cards'").fetchone()[0] == 1

    # the import record: sync + integration ids, approving human, connector as vehicle
    rec = conn.execute(
        "SELECT sync_id, integration_id, snapshot_hash, approved_by, vehicle, result->>'status' "
        "FROM integration_import WHERE import_id = %s", (body["import_id"],)).fetchone()
    assert rec == (sid, iid, snapshot, "user:o", "openmetadata-connector", "ingested")

    # last_import_at was stamped on the sync
    got = client.get(f"/integrations/{iid}/syncs/{sid}", headers=VIEWER).json()
    assert got["last_import_at"] is not None


def test_import_snapshot_mismatch_409_and_nothing_ingested(client, conn, monkeypatch):
    from featuregen.api.routes import integrations as routes

    _, sid = _configured_sync(client)
    pv = _preview(client, sid).json()

    # OM moves between preview and import: a column disappears from page 2
    page1, page2 = fixture_pages()
    del page2["data"][0]["columns"][2]
    monkeypatch.setattr(routes, "_build_fetch",
                        lambda base_url, token: fixture_fetch(page1, page2))

    res = _import(client, sid, pv["snapshot_hash"], pv["local_baseline_hash"])
    assert res.status_code == 409
    assert "preview again" in res.json()["detail"]
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'cards'"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM integration_import").fetchone()[0] == 0


def test_import_local_catalog_drift_409_and_nothing_imported(client, conn):
    """#13 (TOCTOU): the preview's diff was computed against a LOCAL catalog baseline; if another
    upload changes that source between preview and approval, the reviewed diff is stale — approval
    must demand a re-preview, mirroring the remote snapshot-hash 409."""
    from tests.featuregen._helpers import make_actor

    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.ingest import ingest_upload

    _, sid = _configured_sync(client)
    pv = _preview(client, sid).json()

    # the LOCAL catalog moves between preview and approval (an upload lands for the same source)
    actor = make_actor(subject="user:owner", roles=("data_owner",))
    upload = [CanonicalRow(source="cards", table="promotions", column="promo_id", type="bigint",
                           is_grain=True, definition="promotion key")]
    assert ingest_upload(conn, "cards", upload, actor=actor).status == "ingested"

    res = _import(client, sid, pv["snapshot_hash"], pv["local_baseline_hash"])
    assert res.status_code == 409
    assert "preview again" in res.json()["detail"]
    assert "catalog" in res.json()["detail"]
    # nothing imported: the catalog still holds ONLY the interleaved upload's column
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'cards' "
                        "AND kind = 'column'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM integration_import").fetchone()[0] == 0


def test_import_held_by_brake_is_recorded_honestly(client, conn):
    """A pull that would remove most of the source is HELD by the same brake as a hostile upload;
    the import record still exists (audit of the attempt) with status 'held'."""
    from tests.featuregen._helpers import make_actor

    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.ingest import ingest_upload

    actor = make_actor(subject="user:owner", roles=("data_owner",))
    big = [CanonicalRow(source="cards", table=f"legacy_{t}", column=f"col_{c}", type="text")
           for t in range(5) for c in range(5)]
    assert ingest_upload(conn, "cards", big, actor=actor).status == "ingested"

    iid, sid = _configured_sync(client)
    preview = _preview(client, sid).json()
    assert preview["brake"]["would_hold"] is True                # preview PREDICTED the hold

    res = _import(client, sid, preview["snapshot_hash"], preview["local_baseline_hash"])
    assert res.status_code == 200
    body = res.json()
    assert body["result"]["status"] == "held"
    assert body["semantics_pending"] == 0                        # nothing landed
    # the prior catalog is untouched by the held sync
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'cards' "
                        "AND kind = 'column'").fetchone()[0] == 25
    # the attempt is still audited
    assert conn.execute("SELECT result->>'status' FROM integration_import WHERE sync_id = %s",
                        (sid,)).fetchone()[0] == "held"
    # HONESTY: a held import wrote nothing, so it must NOT advance last_import_at
    got = client.get(f"/integrations/{iid}/syncs/{sid}", headers=VIEWER).json()
    assert got["last_import_at"] is None


def test_import_rbac_requires_catalog_write(client):
    _, sid = _configured_sync(client)
    snapshot = _preview(client, sid).json()["snapshot_hash"]
    assert _import(client, sid, snapshot, headers=VIEWER).status_code == 403
    assert _import(client, sid, snapshot, headers=ENGINEER).status_code == 403


def test_import_unknown_sync_404(client):
    assert _import(client, "sync_missing", "deadbeef").status_code == 404


# ---- Ingestion-run manifest (connector origin, design #3) --------------------------------------

RUN_HEADER = "X-Ingestion-Run-Id"


def _get_run(client, run_id):
    return client.get(f"/ingestion-runs/{run_id}", headers=VIEWER)


def test_import_records_ingested_connector_run_linked_from_import_row(client, conn):
    """A successful import leaves an 'ingested' connector-origin run, and integration_import points
    AT the run (the run is created FIRST, before the pull — never the reverse)."""
    _, sid = _configured_sync(client)
    pv = _preview(client, sid).json()
    res = _import(client, sid, pv["snapshot_hash"], pv["local_baseline_hash"])
    assert res.status_code == 200
    run_id = res.headers[RUN_HEADER]
    assert run_id.startswith("ingrun_")

    run = _get_run(client, run_id).json()
    assert run["status"] == "ingested"
    assert run["origin_type"] == "connector"
    assert run["catalog_source"] == "cards"
    assert run["filename"] is None                       # no file: the source is the OM pull
    assert run["file_sha256"] is None
    assert run["actor_subject"] == "user:o"              # the approving human, per the spec
    # review FIX 4: the import route's gate is require_catalog_write too — recorded at open
    assert run["authorization_decision"] == "granted:catalog_write"
    assert run["row_count"] == 14
    assert run["quarantined_count"] == 1
    assert run["fingerprint_algo_version"] == "gn-v1"
    # first import into an empty catalog: pre is the empty-graph hash, post the built graph
    assert run["pre_source_fingerprint"] != run["post_source_fingerprint"]
    assert run["completed_at"] is not None
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "ingested"]

    assert conn.execute(
        "SELECT ingestion_run_id FROM integration_import WHERE import_id = %s",
        (res.json()["import_id"],)).fetchone()[0] == run_id


def test_import_response_body_unchanged_run_id_rides_the_header_only(client):
    """Compatibility: the import response BODY is byte-for-byte what it was before the manifest —
    the run id rides the response header ONLY."""
    _, sid = _configured_sync(client)
    pv = _preview(client, sid).json()
    res = _import(client, sid, pv["snapshot_hash"], pv["local_baseline_hash"])
    assert set(res.json()) == {"result", "import_id", "semantics_pending"}
    assert res.headers[RUN_HEADER] not in res.text


def test_failed_pull_still_records_a_failed_run_with_no_import_row(client, conn, monkeypatch):
    """The design's reversed dependency: a pull that dies BEFORE ingest gets no integration_import
    row, but its run exists (durable 'failed'), retrievable via the header id."""
    from featuregen.api.routes import integrations as routes
    from featuregen.connectors.openmetadata import OMUnreachable

    _, sid = _configured_sync(client)

    def unreachable(base_url, token):
        def fetch(path, params):
            raise OMUnreachable("OpenMetadata unreachable: connect timeout")
        return fetch

    monkeypatch.setattr(routes, "_build_fetch", unreachable)
    res = _import(client, sid, "deadbeef")
    assert res.status_code == 502
    run_id = res.headers[RUN_HEADER]

    run = _get_run(client, run_id).json()
    assert run["status"] == "failed"
    assert run["origin_type"] == "connector"
    assert run["redacted_failure_code"] == "OMUnreachable"   # the CLASS, never the message
    assert "connect timeout" not in str(run)
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "failed"]
    assert run["status_history"][-1]["reason_code"] == "http_502"
    assert conn.execute("SELECT count(*) FROM integration_import").fetchone()[0] == 0


def test_snapshot_mismatch_409_records_failed_run(client, monkeypatch):
    from featuregen.api.routes import integrations as routes

    _, sid = _configured_sync(client)
    pv = _preview(client, sid).json()
    page1, page2 = fixture_pages()
    del page2["data"][0]["columns"][2]           # OM moves between preview and import
    monkeypatch.setattr(routes, "_build_fetch",
                        lambda base_url, token: fixture_fetch(page1, page2))

    res = _import(client, sid, pv["snapshot_hash"], pv["local_baseline_hash"])
    assert res.status_code == 409
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert run["status"] == "failed"
    assert run["status_history"][-1]["reason_code"] == "http_409"


def test_held_import_records_held_run(client, conn):
    """The run's terminal status mirrors ingest_upload's verdict: a brake-held import is 'held'."""
    from tests.featuregen._helpers import make_actor

    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.ingest import ingest_upload

    actor = make_actor(subject="user:owner", roles=("data_owner",))
    big = [CanonicalRow(source="cards", table=f"legacy_{t}", column=f"col_{c}", type="text")
           for t in range(5) for c in range(5)]
    assert ingest_upload(conn, "cards", big, actor=actor).status == "ingested"

    _, sid = _configured_sync(client)
    pv = _preview(client, sid).json()
    res = _import(client, sid, pv["snapshot_hash"], pv["local_baseline_hash"])
    assert res.json()["result"]["status"] == "held"
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert run["status"] == "held"
    assert run["row_count"] == 14
    # nothing landed, so the source's graph state did not move around the run
    assert run["pre_source_fingerprint"] == run["post_source_fingerprint"]


def test_connector_ingest_fault_500_carries_run_header_and_failed_run(client, monkeypatch):
    """Review FIX 3: a raw (non-HTTPException) fault inside the import — e.g. an OCC
    ConcurrencyError from ingest_upload surfacing as a 500 — must still carry the
    X-Ingestion-Run-Id header (via the exception's headers + the app-level handler, body
    byte-for-byte the default 500) so the caller can link the failure to its durable 'failed'
    run. Pre-fix the except path terminalized the run but re-raised header-less."""
    from fastapi.testclient import TestClient

    from featuregen.api.routes import integrations as routes
    from featuregen.contracts.errors import ConcurrencyError

    _, sid = _configured_sync(client)
    pv = _preview(client, sid).json()

    def _raise(*args, **kwargs):
        raise ConcurrencyError("expected_version 3 != stream_version 4")

    monkeypatch.setattr(routes, "ingest_upload", _raise)
    with TestClient(client.app, raise_server_exceptions=False) as raw_client:
        res = _import(raw_client, sid, pv["snapshot_hash"], pv["local_baseline_hash"])
    assert res.status_code == 500
    run_id = res.headers[RUN_HEADER]
    assert run_id.startswith("ingrun_")
    assert res.text == "Internal Server Error"    # body-compat: the default 500, untouched

    run = _get_run(client, run_id).json()
    assert run["status"] == "failed"
    assert run["redacted_failure_code"] == "ConcurrencyError"     # the CLASS, never the message
    assert "stream_version" not in str(run)
    assert run["status_history"][-1]["reason_code"] == "unhandled_exception"


def test_migration_0995_import_run_link_column_is_nullable(conn):
    row = conn.execute(
        "SELECT is_nullable, data_type FROM information_schema.columns "
        "WHERE table_name = 'integration_import' AND column_name = 'ingestion_run_id'").fetchone()
    assert row == ("YES", "text")


def test_token_value_never_serialized_in_any_response(client):
    integ_res = _create_integration(client, headers=AUTH)
    iid = integ_res.json()["integration_id"]
    sync_res = _create_sync(client, iid, headers=AUTH)
    sid = sync_res.json()["sync_id"]
    responses = [
        integ_res,
        client.get("/integrations", headers=AUTH),
        client.get(f"/integrations/{iid}", headers=AUTH),
        sync_res,
        _services(client, iid, headers=AUTH),
        _preview(client, sid, headers=AUTH),
    ]
    preview = responses[-1].json()
    responses.append(_import(client, sid, preview["snapshot_hash"],
                             preview["local_baseline_hash"], headers=AUTH))
    for res in responses:
        assert TOKEN_VALUE not in res.text
