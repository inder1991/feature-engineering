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

import json

import pytest
from tests.featuregen.overlay.upload.conftest import overlay_conn  # noqa: F401 — fixture
from tests.featuregen.overlay.upload.test_suggestions import (  # noqa: F401 — fixture
    _JOIN_SOURCE,
    _MEASURE_TABLE,
    SOURCE,
    TABLE,
    _join_edge,
    ftr_catalog,
    join_catalog,
)

from featuregen.api.routes import suggestions as suggestions_route
from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.commands import register_overlay_commands
from featuregen.overlay.config import _clear_overlay_config
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
)
from featuregen.overlay.upload.join_path import MAX_HOPS_CEILING, MAX_HOPS_DEFAULT

PATH = f"/catalog/{SOURCE}/tables/{TABLE}/suggestions"


def _h(roles: str = "feature_engineer", user: str = "u") -> dict:
    return {"X-User": user, "X-Roles": roles}


def _capture(seen: dict):
    """Stand in for the engine and record EVERY argument the route threads into it. A route that
    silently dropped `roles` — or that quietly widened the join neighbourhood past the page default
    — would still return a perfectly well-shaped 200, so both are asserted at this seam."""
    def _engine(conn, *, catalog_source, table, roles, max_hops):
        seen.update(catalog_source=catalog_source, table=table, roles=roles, max_hops=max_hops)
        return {"catalog_source": catalog_source, "table": table, "table_known": False,
                "summary": {"suggested": 0, "clean_ready": 0, "needs_review": 0, "entities": 0},
                "groups": [], "rejections": [],
                "neighbourhood": {"tables_considered": 0, "tables_available": 0, "truncated": False,
                                  "max_hops": max_hops, "limit_reason": None}}
    return _engine


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
    monkeypatch.setattr(suggestions_route, "suggest_features_for_table", _capture(seen))
    r = client.get("/catalog/src/tables/txns/suggestions",
                   headers=_h(roles="feature_engineer,pii_reader"))
    assert r.status_code == 200, r.text
    assert seen["roles"] == ("feature_engineer", "pii_reader")
    assert seen["catalog_source"] == "src" and seen["table"] == "txns"


def test_requires_the_read_permission(client):
    """Guarded by `catalog:read` (`require_catalog_read`), pinned here so the choice is reviewable.

    A role WITHOUT catalog:read is denied (access_admin holds iam:manage only). `data_owner` PASSES
    by design: this surface exists so a curator can see what still needs curating on a table they
    own — its empty states are their to-dos. Reaching it via `feature:read` would have required
    granting data_owner the feature registry, the contract reads and the lineage features layer too,
    and would have broken `test_data_owner_can_upload_but_not_read_features_or_generate`."""
    assert client.get(PATH, headers=_h(roles="access_admin")).status_code == 403
    assert client.get(PATH, headers=_h(roles="data_owner")).status_code != 403


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


def test_an_automatic_page_load_gets_the_capped_neighbourhood(client, monkeypatch):
    """The DEFECT's contract at the HTTP edge: a plain page load — no query string — must ask for the
    capped default, never the transitive walk. Pinned at the seam, because the widening is invisible
    in the response shape."""
    seen: dict = {}
    monkeypatch.setattr(suggestions_route, "suggest_features_for_table", _capture(seen))
    assert client.get("/catalog/src/tables/txns/suggestions", headers=_h()).status_code == 200
    assert seen["max_hops"] == MAX_HOPS_DEFAULT == 1


def test_a_deliberate_request_may_expand_but_not_without_a_bound(client, monkeypatch):
    """Multi-hop is NOT disabled — an explicit caller can ask for a wider neighbourhood — but the ask
    is bounded: past `MAX_HOPS_CEILING` the request is refused (422) rather than served, so no client
    can talk the server back into an unbounded walk. The table cap and column budget apply either
    way, so expansion changes which tables are eligible, never how many are admitted."""
    seen: dict = {}
    monkeypatch.setattr(suggestions_route, "suggest_features_for_table", _capture(seen))
    assert client.get("/catalog/src/tables/txns/suggestions?max_hops=2",
                      headers=_h()).status_code == 200
    assert seen["max_hops"] == 2
    assert client.get(f"/catalog/src/tables/txns/suggestions?max_hops={MAX_HOPS_CEILING + 1}",
                      headers=_h()).status_code == 422
    assert client.get("/catalog/src/tables/txns/suggestions?max_hops=0",
                      headers=_h()).status_code == 422


def test_the_neighbourhood_metadata_reaches_the_client(client, ftr_catalog):  # noqa: F811
    """The screen may not silently show a subset, so what the widening left out travels on the
    payload — over HTTP, from the real engine, not just in-process."""
    body = client.get(PATH, headers=_h()).json()
    assert body["neighbourhood"] == {"tables_considered": 0, "tables_available": 0,
                                     "truncated": False, "max_hops": 1, "limit_reason": None}


def test_the_read_runs_under_a_statement_timeout(client, conn):
    """One request grounds the WHOLE template registry against the catalog, so its cost scales with
    catalog WIDTH (~1.7k SELECTs on a 2-table fixture). The engine is not this route's to optimise,
    but a wide catalog must fail FAST rather than pin a connection for minutes: the request
    transaction carries a bounded `statement_timeout`. Read back off the SAME still-open transaction
    the request ran on — `SET LOCAL` is transaction-scoped."""
    r = client.get("/catalog/no_such_catalog/tables/no_such_table/suggestions", headers=_h())
    assert r.status_code == 200, r.text
    assert conn.execute("SHOW statement_timeout").fetchone()[0] == "30s"


# ── Task 2: per-table contract negotiation (freeze 0F-12) ───────────────────────────────────────
def test_v1_is_the_default_and_the_explicit_v1_request_is_the_same_bytes(client, ftr_catalog):  # noqa: F811
    """V1 stays the default for the whole of Release A: an existing client that never learned about
    `contract_version` sees exactly what it saw before, and asking for v1 explicitly is the same
    payload — not a re-rendering that happens to agree today."""
    implicit = client.get(PATH, headers=_h())
    explicit = client.get(f"{PATH}?contract_version=1", headers=_h())
    assert implicit.status_code == explicit.status_code == 200
    assert implicit.json() == explicit.json()
    assert set(implicit.json()) == {"catalog_source", "table", "table_known", "summary", "groups",
                                    "rejections", "neighbourhood"}


def test_v2_must_be_asked_for_deliberately(client, ftr_catalog):  # noqa: F811
    """The frontend opts in; the server never upgrades a caller who did not ask. Release A serves
    it on demand, so there is no projection to report and no search facets to fill."""
    body = client.get(f"{PATH}?contract_version=2", headers=_h()).json()
    assert body["read_mode"] == "on_demand"
    assert body["projection"] is None and body["facets"] == {} and body["next_cursor"] is None
    assert body["read_scope_key"]
    collection = body["collection"]
    assert collection["anchor_catalog_source"] == SOURCE
    assert collection["anchor_table_ref"] == TABLE and collection["table_known"] is True
    assert collection["summary"]["suggested"] == len(body["hits"]) >= 1
    assert "clean_ready" not in collection["summary"]        # the misleading V1 name is gone
    suggestion = body["hits"][0]["suggestion"]
    assert suggestion["schema_version"] == "feature-suggestion-v2"
    assert suggestion["suggestion_id"] and suggestion["suggestion_revision_id"]
    assert suggestion["generation_source"] == "recipe"


def test_the_v2_body_matches_its_declared_response_model_exactly(client, ftr_catalog):  # noqa: F811
    """The OpenAPI model is not decoration: it is validated against the REAL body with unknown keys
    FORBIDDEN, so a field added to the contract without updating the published schema — or a schema
    field the body never sends — fails here rather than misleading a client."""
    body = client.get(f"{PATH}?contract_version=2", headers=_h()).json()
    assert suggestions_route.FeatureSuggestionPageV2Response.model_validate(body)


def test_the_v2_body_validates_when_a_relationship_warning_is_on_it(
        client, conn, join_catalog):  # noqa: F811
    """THE SAME CHECK, OVER A CATALOG THAT HAS JOINS. `ftr_catalog` holds no join edges at all, so
    no relationship warning can reach the model above and the three relationship codes were
    published, rendered and shipped without a single response-model validation. They emitted a
    NESTED `[[[src, a], [src, b]]]` instead of the declared flat `[[src, ref], …]`, which this test
    fails on `operand_refs.0.0` and which the card turns into a blank screen.

    The edge is FILE-DECLARED (no approved-join fact — the default for an upload-declared
    relationship) with no cardinality, so both `RELATIONSHIP_UNCONFIRMED` and
    `DIRECTIONAL_CARDINALITY_UNAVAILABLE` are on the page."""
    _join_edge(conn, fact_key=None, status=None, cardinality=None)
    path = f"/catalog/{_JOIN_SOURCE}/tables/{_MEASURE_TABLE}/suggestions?contract_version=2"
    r = client.get(path, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    warnings = [w for hit in body["hits"] for w in hit["suggestion"]["warnings"]]
    codes = {w["code"] for w in warnings}
    assert {"RELATIONSHIP_UNCONFIRMED", "DIRECTIONAL_CARDINALITY_UNAVAILABLE"} <= codes, codes
    assert suggestions_route.FeatureSuggestionPageV2Response.model_validate(body)
    # ...and, said directly: every entry is a FLAT (catalog_source, ref) pair of strings, one arity
    # for every code on the page.
    for warning in warnings:
        for ref in warning["operand_refs"]:
            assert [type(part) for part in ref] == [str, str], warning


def test_the_v1_body_matches_its_declared_response_model_exactly(client, ftr_catalog):  # noqa: F811
    body = client.get(PATH, headers=_h()).json()
    assert suggestions_route.TableSuggestionsV1Response.model_validate(body)


def test_v3_is_the_v2_page_plus_additive_execution_truth(client, ftr_catalog):  # noqa: F811
    """BR-8. Contract v3 must be asked for deliberately, and what it adds is exactly three things:
    the page's own declared version, one `execution` block per hit, and the readiness tally. The
    rest of the page is the v2 payload UNCHANGED — proven by deleting the additions and comparing
    equal — so v3 can never drift into a re-rendering that happens to agree today."""
    v2 = client.get(f"{PATH}?contract_version=2", headers=_h()).json()
    v3 = client.get(f"{PATH}?contract_version=3", headers=_h()).json()
    assert v3["contract_version"] == 3
    assert set(v3) - set(v2) == {"contract_version", "readiness_counts"}

    tally: dict[str, int] = {}
    for hit in v3["hits"]:
        block = hit["suggestion"].pop("execution")
        state = block["execution_readiness"]
        tally[state] = tally.get(state, 0) + 1
        # Every card declares what it IS: today's whole legacy registry projects conceptual-only
        # ideas (UNASSESSED — "nobody decided yet", carried with ZERO blockers because an idea is
        # not a failure) except the reviewed expectation anchors, which enter BR-7's fold.
        assert block["recipe_contract_version"] == "legacy-template"
        if block["execution_readiness"] == "UNASSESSED":
            assert block["computation_kind"] == "conceptual_pattern"
            assert block["readiness_blockers"] == []
        else:
            assert block["computation_kind"] == "deterministic_formula"
            assert all(b["code"] and b["group"] for b in block["readiness_blockers"])
    assert v3["readiness_counts"] == tally and sum(tally.values()) == len(v3["hits"])

    del v3["contract_version"], v3["readiness_counts"]
    assert v3 == v2


def test_v2_carries_no_execution_key_ever(client, ftr_catalog):  # noqa: F811
    """The frozen side of BR-8: v1 and v2 clients see not one new byte. The v1 default already has
    its own byte-stability test above; this pins the v2 page."""
    v2 = client.get(f"{PATH}?contract_version=2", headers=_h()).json()
    assert "contract_version" not in v2 and "readiness_counts" not in v2
    assert all("execution" not in hit["suggestion"] for hit in v2["hits"])


def test_the_v3_body_matches_its_declared_response_model_exactly(client, ftr_catalog):  # noqa: F811
    body = client.get(f"{PATH}?contract_version=3", headers=_h()).json()
    assert suggestions_route.FeatureSuggestionPageV3Response.model_validate(body)


def test_an_unknown_table_stays_a_200_payload_state_in_v2(client, ftr_catalog):  # noqa: F811
    """The honesty rule survives the new contract: "this catalog does not hold that table" is data,
    never an error, and the requested string is echoed verbatim."""
    r = client.get(f"/catalog/{SOURCE}/tables/no_such_table/suggestions?contract_version=2",
                   headers=_h())
    assert r.status_code == 200, r.text
    collection = r.json()["collection"]
    assert collection["table_known"] is False
    assert collection["anchor_table_ref"] == "no_such_table"
    assert r.json()["hits"] == [] and collection["neighbourhood"]["max_hops"] == 1


@pytest.mark.parametrize("version", [0, 4, 99, -1])
def test_an_unsupported_integer_version_is_a_typed_422(client, version):
    """The typed error contract (0F-12). The bound is deliberately NOT on the query parameter: a
    FastAPI `le=2` would reject the request before the handler ran, so this machine-readable code
    could never be emitted and the body would be FastAPI's list-`detail` instead."""
    r = client.get(f"/catalog/src/tables/txns/suggestions?contract_version={version}",
                   headers=_h())
    assert r.status_code == 422, r.text
    body = r.json()
    assert set(body) == {"detail", "error_code"}
    assert body["error_code"] == "SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION"
    assert isinstance(body["detail"], str) and str(version) in body["detail"]


def test_a_non_integer_version_keeps_fastapis_own_validation_error(client):
    """The frozen BOUNDARY. A type failure is caught before any handler code runs, so its shape is
    FastAPI's — `detail` as a LIST — and it sits deliberately OUTSIDE the typed contract. Claiming
    otherwise would mean intercepting framework validation to fake a code we never produced."""
    r = client.get("/catalog/src/tables/txns/suggestions?contract_version=two", headers=_h())
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
    assert "error_code" not in r.json()
    # ...and the same is true of every other parameter bound, e.g. max_hops
    bad_hops = client.get("/catalog/src/tables/txns/suggestions?max_hops=0", headers=_h())
    assert bad_hops.status_code == 422 and isinstance(bad_hops.json()["detail"], list)


def test_an_unsupported_version_does_no_work_at_all(client, monkeypatch):
    """The refusal is the FIRST thing the handler does: a rejected version must not ground a
    registry, bind a timeout or touch the catalog."""
    called: list = []
    monkeypatch.setattr(suggestions_route, "suggest_features_for_table",
                        lambda *a, **kw: called.append(a))
    monkeypatch.setattr(suggestions_route, "suggest_features_page_v2",
                        lambda *a, **kw: called.append(a))
    assert client.get("/catalog/src/tables/txns/suggestions?contract_version=7",
                      headers=_h()).status_code == 422
    assert called == []


def test_the_v2_read_threads_the_sessions_own_read_scope(client, monkeypatch):
    """Same load-bearing constraint as V1, at the new seam: the scope comes from the authenticated
    session's role claims — never a request parameter, and never a client-supplied scope key."""
    seen: dict = {}

    def _engine(conn, *, catalog_source, table, roles, max_hops):
        seen.update(catalog_source=catalog_source, table=table, roles=roles, max_hops=max_hops)
        return suggestions_route.unknown_table_page_v2(
            catalog_source=catalog_source, requested_table=table, roles=roles,
            neighbourhood=_zero_neighbourhood())

    monkeypatch.setattr(suggestions_route, "suggest_features_page_v2", _engine)
    r = client.get("/catalog/src/tables/txns/suggestions?contract_version=2",
                   headers=_h(roles="feature_engineer,pii_reader"))
    assert r.status_code == 200, r.text
    assert seen["roles"] == ("feature_engineer", "pii_reader")
    assert "pii_reader" not in r.text          # the key is opaque; claims never travel back


def _zero_neighbourhood():
    from featuregen.overlay.upload.join_path import JoinNeighbourhood

    return JoinNeighbourhood(tables=(), tables_considered=0, tables_available=0, truncated=False,
                             max_hops=MAX_HOPS_DEFAULT, limit_reason=None)


def test_the_read_runs_on_the_repeatable_read_connection(client):
    """Rule 19. The V2 page is a MULTI-statement read — resolve, neighbourhood, ground, context —
    and its counts, groups, neighbourhood metadata and revision ids must describe ONE snapshot. The
    app already has that dependency; this route now uses it rather than a second isolation story."""
    from fastapi.routing import APIRoute

    from featuregen.api.deps import get_feature_gen_conn

    route = next(r for r in suggestions_route.router.routes
                 if isinstance(r, APIRoute) and r.path.endswith("/tables/{table}/suggestions"))

    def _calls(dependant):
        yield dependant.call
        for sub in dependant.dependencies:
            yield from _calls(sub)

    assert get_feature_gen_conn in set(_calls(route.dependant))


def test_the_openapi_operation_publishes_both_contracts_and_the_typed_error(client):
    """OPENAPI SNAPSHOT. Three claims a client integrates against: the parameter exists and is
    deliberately unbounded above (so the handler owns the refusal), the 200 advertises BOTH
    payloads, and the 422 advertises the typed error body."""
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/catalog/{catalog_source}/tables/{table}/suggestions"]["get"]
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert set(parameters) >= {"catalog_source", "table", "max_hops", "contract_version"}
    # THE SCOPE IS NOT NEGOTIABLE IN THE REQUEST. There is no QUERY parameter through which a
    # caller can supply a scope key, a role list or a tenant — grounding reads the authenticated
    # session and nothing else, so a widened scope cannot be requested, only granted. (The
    # `x-roles` HEADER that also appears here is the platform-wide dev auth stub, off by default
    # in production via FEATUREGEN_AUTH_STUB and owned by `deps.get_identity`, not by this route.)
    assert not [name for name, p in parameters.items()
                if p["in"] == "query"
                and any(word in name for word in ("scope", "role", "tenant", "visib"))]
    version = parameters["contract_version"]["schema"]
    assert version.get("default") == 1
    assert "maximum" not in version and "exclusiveMaximum" not in version
    assert parameters["max_hops"]["schema"]["maximum"] == MAX_HOPS_CEILING

    assert set(operation["responses"]) >= {"200", "422"}
    error = operation["responses"]["422"]["content"]["application/json"]["schema"]
    assert error["$ref"].endswith("/SuggestionsErrorResponse")
    assert set(spec["components"]["schemas"]["SuggestionsErrorResponse"]["properties"]) == {
        "detail", "error_code"}
    published = json.dumps(operation["responses"]["200"])
    assert "TableSuggestionsV1Response" in published
    assert "FeatureSuggestionPageV2Response" in published


def test_the_v2_route_is_still_get_only_and_still_gated(client):
    for method in ("post", "put", "delete"):
        r = getattr(client, method)(f"{PATH}?contract_version=2", headers=_h())
        assert r.status_code == 405, f"{method} -> {r.status_code}"
    assert client.get(f"{PATH}?contract_version=2",
                      headers=_h(roles="access_admin")).status_code == 403


def test_the_two_contracts_describe_the_same_read(client, ftr_catalog):  # noqa: F811
    """The migration guarantee, end to end over HTTP: the V2 page carries every card the V1 payload
    does, under the same names and the same counts."""
    v1 = client.get(PATH, headers=_h()).json()
    v2 = client.get(f"{PATH}?contract_version=2", headers=_h()).json()
    assert v2["collection"]["summary"]["suggested"] == v1["summary"]["suggested"]
    assert v2["collection"]["summary"]["design_checked"] == v1["summary"]["clean_ready"]
    assert v2["collection"]["summary"]["groups"] == v1["summary"]["entities"]
    assert {h["suggestion"]["name"] for h in v2["hits"]} == {
        s["name"] for g in v1["groups"] for s in g["suggestions"]}
    assert [r["code"] for r in v2["collection"]["rejections"]] == [
        r["code"] for r in v1["rejections"]]


@pytest.mark.parametrize("contract_version", [1, 2])
def test_a_lagged_projection_is_a_retryable_503_not_an_opaque_500(client, monkeypatch,
                                                                  contract_version):
    """FROM THE LIVE CLUSTER (2026-08-09): the page showed "Could not load suggestions: Internal
    Server Error" while the backend logged CATALOG_PROJECTION_UNAVAILABLE.

    The refusal itself is CORRECT and stays: suggestions are computed on demand, and the gauntlet
    reads GOVERNED values (grain, join authority) through `_governed_read`, which fails closed
    rather than reason from a projection it knows is behind. What was wrong is only how it left the
    building — an unhandled exception became a 500, which reads as a crash and tells the caller
    nothing about the one thing that matters: this is TEMPORARY, and retrying is the right move.

    `contract.py` already maps this exception to a retryable 503 (the mapping `_governed_read`'s own
    docstring promises: "which the feature-gen route maps to a retryable 503"). This route reaches
    the identical `_governed_read` and had no such mapping, so BOTH payload contracts are pinned
    here — v1 and v2 run different engine entrypoints and would have to regress separately.
    """
    def _lagged(*a, **k):
        raise CatalogProjectionUnavailable(
            CATALOG_PROJECTION_UNAVAILABLE,
            "load-bearing projection 'overlay' is LAGGED: checkpoint 53 < event head 54")

    target = ("suggest_features_page_v2" if contract_version == 2
              else "suggest_features_for_table")
    monkeypatch.setattr(f"featuregen.api.routes.suggestions.{target}", _lagged)

    r = client.get(PATH, params={"contract_version": contract_version}, headers=_h())
    assert r.status_code == 503
    # The caller must be able to tell WHY, and that waiting is the fix.
    assert "LAGGED" in r.json()["detail"]
