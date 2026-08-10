"""STEP 0 — `GET /catalog/{catalog_source}/recipe-funnel`, the read-only funnel diagnostic.

The route's own job is three things, mirroring the suggestions route's discipline: the read GUARD
(`catalog:read`, same as every `/catalog/...` read), threading the SESSION's roles as the read
scope, and serializing the engine's `RecipeFunnelV1` unchanged. GET-only — a diagnostic writes
nothing. Engine behaviour (statuses, unmet sweep, histogram, stopwatch) is pinned in
`tests/featuregen/overlay/upload/test_recipe_funnel.py`; nothing here re-tests it.
"""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.overlay.upload.templates import ALL_TEMPLATES

PATH = "/catalog/deposits/recipe-funnel"


def test_requires_the_read_permission(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    denied = client.get(PATH, headers={"X-User": "t", "X-Roles": "access_admin"})
    assert denied.status_code == 403
    assert client.get(PATH, headers=AUTH).status_code == 200


def test_the_route_is_get_only(client):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(PATH, headers=AUTH).status_code == 405


def test_serves_the_funnel_shape_with_one_entry_per_template(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.get(PATH, headers=AUTH).json()
    assert set(body) == {"catalog_source", "registry_total", "grounded", "entries",
                        "blocked_concepts", "elapsed_ms"}
    assert body["catalog_source"] == "deposits"
    assert body["registry_total"] == len(ALL_TEMPLATES)
    assert len(body["entries"]) == len(ALL_TEMPLATES)
    entry = body["entries"][0]
    assert set(entry) == {"template_id", "status", "reason_codes", "unmet"}
    # unmet entries are {role, concept} objects — the histogram's raw material, named not encoded.
    for e in body["entries"]:
        for u in e["unmet"]:
            assert set(u) == {"role", "concept"}
    # the histogram arrives in its wire order (most-blocking first) and re-aggregates exactly.
    recount: dict[str, int] = {}
    for e in body["entries"]:
        for u in e["unmet"]:
            recount[u["concept"]] = recount.get(u["concept"], 0) + 1
    assert {b["concept"]: b["blocked"] for b in body["blocked_concepts"]} == recount
    assert body["elapsed_ms"] >= 0


def test_an_unknown_catalog_is_an_empty_shelf_not_an_error(client):
    """A catalog with no columns grounds nothing — every required need is unmet. That is the honest
    answer to 'what can this catalog produce', and it must not 500 or 404: the funnel's whole point
    is explaining absence."""
    body = client.get("/catalog/no_such_catalog/recipe-funnel", headers=AUTH).json()
    assert body["grounded"] == 0
    assert body["registry_total"] == len(ALL_TEMPLATES)
