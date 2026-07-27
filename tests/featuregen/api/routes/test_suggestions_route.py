"""P4 v1 Task 3 — the read-only suggestions route.

`GET /catalog/{catalog_source}/tables/{table}/suggestions` exposes
:func:`overlay.upload.suggestions.suggest_features_for_table` over HTTP. The route's own job is
exactly three things, and each is tested here: the read GUARD, threading the caller's session roles
as the READ SCOPE, and passing the engine's payload through unchanged. It is GET-only — v1 writes
nothing, so there is no verb on this path that could govern or accept anything.

The 200 runs the REAL engine over the REAL FTR fixture (`ftr_catalog`, imported from the Task-1
suite) so the shape asserted here is the shape the engine actually produces, not one this test
invented. That fixture normally runs under the overlay conftests; this module lives under
tests/featuregen/api/, so `_overlay_env` supplies the same process preconditions (overlay commands +
event schemas) and clears the process globals afterwards — the `test_readiness_routes` precedent.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.conftest import overlay_conn  # noqa: F401 — fixture
from tests.featuregen.overlay.upload.test_suggestions import (  # noqa: F401 — fixture
    SOURCE,
    TABLE,
    ftr_catalog,
)

from featuregen.api.routes import suggestions as suggestions_route
from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.commands import register_overlay_commands
from featuregen.overlay.config import _clear_overlay_config
from featuregen.overlay.facts import register_overlay_event_types

PATH = f"/catalog/{SOURCE}/tables/{TABLE}/suggestions"


def _h(roles: str = "feature_engineer", user: str = "u") -> dict:
    return {"X-User": user, "X-Roles": roles}


@pytest.fixture(autouse=True)
def _overlay_env():
    """The overlay preconditions the fixture's propose/confirm path needs (the overlay conftests are
    not in scope here), plus teardown of the two PROCESS globals it registers — the catalog adapter
    and the sealed overlay config — so nothing leaks into the rest of the api suite."""
    register_overlay_commands()
    register_overlay_event_types(event_registry())
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


def test_returns_the_engines_suggestions_for_the_table(client, ftr_catalog):  # noqa: F811
    """The real payload over HTTP: the engine's counts, entity groups and cards, unchanged."""
    r = client.get(PATH, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["catalog_source"] == SOURCE and body["table"] == TABLE
    summary = body["summary"]
    assert summary["suggested"] >= 1
    assert summary["clean_ready"] + summary["needs_review"] == summary["suggested"]
    assert summary["entities"] == len(body["groups"])
    group = body["groups"][0]
    assert group["entity_ref"] and group["entity_label"]
    card = group["suggestions"][0]
    assert card["name"] and card["description"]
    assert card["validation_status"] in ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION")
    assert card["recipe"] and card["recipe_parts"]["operation"]
    assert card["uses"] and isinstance(card["requirements"], list)


def test_read_scope_roles_come_from_the_session(client, monkeypatch):
    """The load-bearing constraint: suggestions must never reveal a column the caller cannot see, so
    the engine is called with the SESSION's role claims (`identity.role_claims`) — never a default,
    never a request parameter. Captured at the seam, because a route that silently dropped `roles`
    would still return a perfectly well-shaped 200."""
    seen: dict = {}

    def _capture(conn, *, catalog_source, table, roles):
        seen.update(catalog_source=catalog_source, table=table, roles=roles)
        return {"catalog_source": catalog_source, "table": table, "table_known": False,
                "summary": {"suggested": 0, "clean_ready": 0, "needs_review": 0, "entities": 0},
                "groups": [], "rejections": []}

    monkeypatch.setattr(suggestions_route, "suggest_features_for_table", _capture)
    r = client.get("/catalog/src/tables/txns/suggestions",
                   headers=_h(roles="feature_engineer,pii_reader"))
    assert r.status_code == 200, r.text
    assert seen["roles"] == ("feature_engineer", "pii_reader")
    assert seen["catalog_source"] == "src" and seen["table"] == "txns"


def test_requires_the_read_permission(client):
    """Guarded by `feature:read` (`require_feature_read`) — the most conservative EXISTING read
    guard, pinned here so the choice is reviewable: access_admin (iam:manage only) is denied, and so
    is data_owner, who holds catalog:read+write but builds no features. catalog_viewer and
    feature_engineer both hold feature:read and pass."""
    assert client.get(PATH, headers=_h(roles="access_admin")).status_code == 403
    assert client.get(PATH, headers=_h(roles="data_owner")).status_code == 403


def test_the_route_is_get_only(client):
    """v1 is strictly read-only — there is no verb on this path that could accept or govern."""
    for method in ("post", "put", "delete"):
        r = getattr(client, method)(PATH, headers=_h())
        assert r.status_code == 405, f"{method} -> {r.status_code}"


def test_unknown_table_is_reported_as_unknown_not_as_an_empty_catalog(client, ftr_catalog):  # noqa: F811
    """A table this catalog does not hold has no suggestions — still data, not a server error — but
    it must say WHICH kind of nothing it is. An empty payload alone is read by the screen as "this
    table's columns carry no business concepts": a confident, false diagnosis of a table that does
    not exist. `table_known` is the fourth state that keeps that message honest."""
    r = client.get(f"/catalog/{SOURCE}/tables/no_such_table/suggestions", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table_known"] is False
    assert body["summary"] == {"suggested": 0, "clean_ready": 0, "needs_review": 0, "entities": 0}
    assert body["groups"] == [] and body["rejections"] == []
    assert client.get(PATH, headers=_h()).json()["table_known"] is True


def test_the_read_runs_under_a_statement_timeout(client, conn):
    """One request grounds the WHOLE template registry against the catalog, so its cost scales with
    catalog WIDTH (~1.7k SELECTs on a 2-table fixture). The engine is not this route's to optimise,
    but a wide catalog must fail FAST rather than pin a connection for minutes: the request
    transaction carries a bounded `statement_timeout`. Read back off the SAME still-open transaction
    the request ran on — `SET LOCAL` is transaction-scoped."""
    r = client.get("/catalog/no_such_catalog/tables/no_such_table/suggestions", headers=_h())
    assert r.status_code == 200, r.text
    assert conn.execute("SHOW statement_timeout").fetchone()[0] == "30s"
