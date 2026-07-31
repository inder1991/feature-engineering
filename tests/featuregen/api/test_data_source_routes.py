"""Configuring where each catalog's data lives.

`/integrations` next door manages METADATA connectors — where the catalog description comes from,
where a bad one costs you an import. These grant READ ACCESS TO A WAREHOUSE under a named principal,
so the write bar is the raw `platform-admin` claim, not `catalog:write`.

Reads stay open to `catalog:read`: the analysis workspace's gap messages point at exactly this
configuration, and someone diagnosing "PHYSICAL_BINDING_ABSENT" needs to see what is routed.
"""
from __future__ import annotations

import pytest


def _h(roles: str = "platform-admin", user: str = "priya") -> dict:
    return {"X-User": user, "X-Roles": roles}


def _connection(**over) -> dict:
    body = {
        "connection_id": "edp-hive", "engine": "hive", "tier": "edp",
        "host": "hiveserver2.internal", "port": 10000, "auth_mechanism": "kerberos",
        "secret_ref": "vault://featuregen/edp-hive", "execution_principal": "svc_ro",
        "allowed_schemas": ["DPL_EIB_COMPLIANCE"], "database_name": "edp_cluster", "active": True,
    }
    body.update(over)
    return body


@pytest.fixture
def catalog(conn):
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "  schema_name) VALUES ('ftr','public.tran_repos.cif_id','column','tran_repos','cif_id',"
        "  'DPL_EIB_COMPLIANCE') ON CONFLICT (catalog_source, object_ref) DO NOTHING")
    return conn


# ── the routes exist and answer ──────────────────────────────────────────────────────────────────

def test_connections_can_be_created_and_listed(client, catalog):
    assert client.put("/data-sources/connections/edp-hive", json=_connection(),
                      headers=_h()).status_code == 200
    body = client.get("/data-sources/connections", headers=_h("catalog_viewer")).json()
    (row,) = body["connections"]
    assert (row["engine"], row["tier"]) == ("hive", "edp")
    assert row["usable_here"] is True


def test_a_catalog_engine_is_declared_and_listed(client, catalog):
    assert client.put("/data-sources/catalogs/ftr", json={"engine": "hive", "tier": "edp"},
                      headers=_h()).status_code == 200
    (row,) = client.get("/data-sources/catalogs", headers=_h("catalog_viewer")).json()["catalogs"]
    assert (row["catalog_source"], row["engine"], row["tier"]) == ("ftr", "hive", "edp")
    # The declarer is the AUTHENTICATED subject, prefixed by the identity layer — recorded because
    # "who said this catalog lives on hive/edp" is the question asked when a number looks wrong.
    assert row["declared_by"] == "user:priya"


def test_an_UNDECLARED_catalog_is_still_listed(client, catalog):
    """An operator needs to see what is NOT routed. A list of only the configured ones hides exactly
    the thing they came to fix."""
    (row,) = client.get("/data-sources/catalogs", headers=_h("catalog_viewer")).json()["catalogs"]
    assert row["catalog_source"] == "ftr"
    assert row["engine"] is None


# ── who may change it ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("roles", ["catalog_viewer", "data_owner", "feature_engineer"])
def test_writing_a_connection_requires_the_platform_admin_claim(client, catalog, roles):
    """This grants read access to a warehouse under a named principal — a different act from
    curating a catalog, and `catalog:write` is not a high enough bar for it."""
    r = client.put("/data-sources/connections/edp-hive", json=_connection(), headers=_h(roles))
    assert r.status_code == 403


def test_declaring_a_catalog_engine_requires_it_too(client, catalog):
    r = client.put("/data-sources/catalogs/ftr", json={"engine": "hive", "tier": "edp"},
                   headers=_h("data_owner"))
    assert r.status_code == 403


def test_reading_is_open_to_catalog_read(client, catalog):
    """Someone diagnosing a gap message must be able to see what is routed."""
    assert client.get("/data-sources/connections",
                      headers=_h("catalog_viewer")).status_code == 200


# ── no credential passes through ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["hunter2", "kerberos-ticket-abc", "/etc/keytab"])
def test_a_LITERAL_secret_is_refused_not_stored(client, catalog, bad):
    """The cost of being wrong once is a credential in every backup of this database, forever. So a
    secret_ref must LOOK like a reference and anything else is refused rather than trusted."""
    r = client.put("/data-sources/connections/edp-hive", json=_connection(secret_ref=bad),
                   headers=_h())
    assert r.status_code == 422
    assert "never the secret itself" in r.json()["detail"]


@pytest.mark.parametrize("ref", ["vault://x/y", "env://HIVE_PW", "aws-secrets://prod/hive"])
def test_a_reference_is_accepted(client, catalog, ref):
    assert client.put("/data-sources/connections/edp-hive", json=_connection(secret_ref=ref),
                      headers=_h()).status_code == 200


# ── the guards that stop a plausible mis-configuration ───────────────────────────────────────────

def test_an_engine_with_no_dialect_is_refused(client, catalog):
    """A connection to an engine nothing can render SQL for is a route to nowhere that looks
    configured."""
    r = client.put("/data-sources/connections/x", json=_connection(connection_id="x",
                                                                  engine="teradata"), headers=_h())
    assert r.status_code == 422
    assert "teradata" in r.json()["detail"]


def test_declaring_an_engine_for_a_catalog_nobody_uploaded_is_refused(client, catalog):
    """A typo with a plausible shape: it would sit there looking configured and route nothing."""
    r = client.put("/data-sources/catalogs/typo", json={"engine": "hive", "tier": "edp"},
                   headers=_h())
    assert r.status_code == 404


def test_a_SECOND_active_route_for_one_engine_tier_is_a_409(client, catalog):
    """Two would mean the same catalog resolving to different clusters depending on read order — and
    in a bank the two candidates are UAT and production."""
    client.put("/data-sources/connections/edp-hive", json=_connection(), headers=_h())
    r = client.put("/data-sources/connections/other",
                   json=_connection(connection_id="other"), headers=_h())
    assert r.status_code == 409
    assert "read order" in r.json()["detail"]


def test_the_environment_comes_from_the_DEPLOYMENT_not_the_caller(client, catalog):
    """A route is created where it is used, so a UAT screen cannot mint a production row by typing a
    different word into a form."""
    from featuregen.config import get_settings

    body = client.put("/data-sources/connections/edp-hive", json=_connection(),
                      headers=_h()).json()
    assert body["environment"] == get_settings().environment


def test_the_listing_says_which_routes_are_usable_HERE(client, catalog):
    """A row from another environment is visible but inert. Saying so beats leaving someone to
    discover it from a gap message."""
    client.put("/data-sources/connections/edp-hive", json=_connection(), headers=_h())
    catalog.execute("UPDATE data_source_connection SET environment_id = 'prod'")
    (row,) = client.get("/data-sources/connections", headers=_h("catalog_viewer")).json()["connections"]
    assert row["usable_here"] is False
