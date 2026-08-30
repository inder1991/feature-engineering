"""The read-only suggestions route.

`GET /catalog/{catalog_source}/tables/{table}/suggestions` exposes
:func:`overlay.upload.suggestions.suggest_features_page_v2`, projected onto CONTRACT v4, over
HTTP. The route's own job is exactly three things, and each is tested here: the read GUARD,
threading the caller's session roles as the READ SCOPE, and passing the engine's payload through
unchanged. It is GET-only — this surface writes nothing, so there is no verb on this path that
could govern or accept anything.

E4 CUTOVER (2026-08-14): contract versions 1, 2 and 3 are DELETED. v4 is the only version this
route serves and the default an omitted `contract_version` resolves to; 1/2/3 now earn the same
typed 422 as 99, naming `[4]`. The per-version tests below were rewritten accordingly: the ones
that asserted a v1/v2/v3 BODY are gone with those bodies, and the ones that asserted the ROUTE's
own behaviour (guard, read scope, hop bound, timeout, GET-only, refusal) moved onto v4 — that
behaviour never belonged to a version.

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
    — would still return a perfectly well-shaped 200, so both are asserted at this seam.

    Returns the real unknown-table PAGE object (never a hand-rolled dict): the route serializes
    whatever comes back through `page_to_json_v3`, so a stub of the wrong type would fail as a
    500 instead of asserting the seam."""
    from featuregen.overlay.upload.join_path import MAX_HOPS_DEFAULT as _default
    from featuregen.overlay.upload.suggestion_contract import unknown_table_page_v2
    from featuregen.overlay.upload.suggestions import JoinNeighbourhood

    def _engine(conn, *, catalog_source, table, roles, max_hops):
        seen.update(catalog_source=catalog_source, table=table, roles=roles, max_hops=max_hops)
        return unknown_table_page_v2(
            catalog_source=catalog_source, requested_table=table, roles=roles,
            neighbourhood=JoinNeighbourhood(
                tables=(), tables_considered=0, tables_available=0, truncated=False,
                max_hops=_default if max_hops is None else max_hops, limit_reason=None))
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
    """The real payload over HTTP with NO version asked for: the default resolves to v4 (the only
    contract), and it carries the engine's counts, entity groups, cards and semantic block."""
    r = client.get(PATH, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contract_version"] == 4
    collection = body["collection"]
    assert collection["anchor_catalog_source"] == SOURCE
    assert collection["anchor_table_ref"] == TABLE and collection["table_known"] is True
    summary = collection["summary"]
    assert summary["suggested"] == len(body["hits"]) >= 1
    assert summary["design_checked"] + summary["needs_external_validation"] == summary["suggested"]
    suggestion = body["hits"][0]["suggestion"]
    assert suggestion["name"] and suggestion["display_name"]
    assert suggestion["validation_status"] in ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION")
    assert suggestion["recipe"] and suggestion["recipe_parts"]["operation"]
    assert suggestion["operands"] and isinstance(suggestion["requirements"], list)
    assert body["semantic"]["table"] == TABLE


def test_read_scope_roles_come_from_the_session(client, monkeypatch):
    """The load-bearing constraint: suggestions must never reveal a column the caller cannot see, so
    the engine is called with the SESSION's role claims (`identity.role_claims`) — never a default,
    never a request parameter. Captured at the seam, because a route that silently dropped `roles`
    would still return a perfectly well-shaped 200."""
    seen: dict = {}
    monkeypatch.setattr(suggestions_route, "suggest_features_page_v2", _capture(seen))
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
    """Strictly read-only — there is no verb on this path that could accept or govern."""
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
    collection = r.json()["collection"]
    assert collection["table_known"] is False
    assert collection["summary"] == {"suggested": 0, "design_checked": 0,
                                     "needs_external_validation": 0, "groups": 0}
    assert collection["groups"] == [] and collection["rejections"] == []
    assert r.json()["hits"] == []
    assert client.get(PATH, headers=_h()).json()["collection"]["table_known"] is True


def test_an_automatic_page_load_gets_the_capped_neighbourhood(client, monkeypatch):
    """The DEFECT's contract at the HTTP edge: a plain page load — no query string — must ask for the
    capped default, never the transitive walk. Pinned at the seam, because the widening is invisible
    in the response shape."""
    seen: dict = {}
    monkeypatch.setattr(suggestions_route, "suggest_features_page_v2", _capture(seen))
    assert client.get("/catalog/src/tables/txns/suggestions", headers=_h()).status_code == 200
    assert seen["max_hops"] == MAX_HOPS_DEFAULT == 1


def test_a_deliberate_request_may_expand_but_not_without_a_bound(client, monkeypatch):
    """Multi-hop is NOT disabled — an explicit caller can ask for a wider neighbourhood — but the ask
    is bounded: past `MAX_HOPS_CEILING` the request is refused (422) rather than served, so no client
    can talk the server back into an unbounded walk. The table cap and column budget apply either
    way, so expansion changes which tables are eligible, never how many are admitted."""
    seen: dict = {}
    monkeypatch.setattr(suggestions_route, "suggest_features_page_v2", _capture(seen))
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
    assert body["collection"]["neighbourhood"] == {
        "tables_considered": 0, "tables_available": 0,
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


# ── contract negotiation after the E4 cutover (freeze 0F-12, one version left) ─────────────────
@pytest.mark.parametrize("retired", [1, 2, 3])
def test_the_retired_contract_versions_are_refused_by_number(client, retired):
    """v1, v2 and v3 are DELETED, not deprecated: asking for one earns the same typed 422 as
    asking for 99, and the message names the set this deployment serves so a stale client learns
    what to ask for instead of silently mis-reading a body it did not expect."""
    r = client.get(f"{PATH}?contract_version={retired}", headers=_h())
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error_code"] == "SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION"
    assert "[4]" in body["detail"] and str(retired) in body["detail"]


def test_v4_is_the_default_and_the_explicit_v4_request_is_the_same_bytes(client, ftr_catalog):  # noqa: F811
    """An omitted version must resolve to something this deployment can serve, and asking for it
    explicitly must be the SAME payload — not a re-rendering that happens to agree today."""
    implicit = client.get(PATH, headers=_h())
    explicit = client.get(f"{PATH}?contract_version=4", headers=_h())
    assert implicit.status_code == explicit.status_code == 200
    assert implicit.json() == explicit.json()


def test_the_v4_body_matches_its_declared_response_model_exactly(client, ftr_catalog):  # noqa: F811
    """The OpenAPI model is not decoration: it is validated against the REAL body with unknown keys
    FORBIDDEN, so a field added to the contract without updating the published schema — or a schema
    field the body never sends — fails here rather than misleading a client."""
    body = client.get(PATH, headers=_h()).json()
    assert suggestions_route.FeatureSuggestionPageV4Response.model_validate(body)


def test_the_v4_body_validates_when_a_relationship_warning_is_on_it(
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
    path = f"/catalog/{_JOIN_SOURCE}/tables/{_MEASURE_TABLE}/suggestions"
    r = client.get(path, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    warnings = [w for hit in body["hits"] for w in hit["suggestion"]["warnings"]]
    codes = {w["code"] for w in warnings}
    assert {"RELATIONSHIP_UNCONFIRMED", "DIRECTIONAL_CARDINALITY_UNAVAILABLE"} <= codes, codes
    assert suggestions_route.FeatureSuggestionPageV4Response.model_validate(body)
    # ...and, said directly: every entry is a FLAT (catalog_source, ref) pair of strings, one arity
    # for every code on the page.
    for warning in warnings:
        for ref in warning["operand_refs"]:
            assert [type(part) for part in ref] == [str, str], warning


def test_the_execution_truth_rides_on_every_hit(client, ftr_catalog):  # noqa: F811
    """BR-8's execution block, inherited by v4 from the page shape v3 introduced. It is no longer
    a VERSION a client can ask for, so what is pinned here is that it still travels — and that
    the page-level tallies still add up to the per-hit facts."""
    body = client.get(PATH, headers=_h()).json()
    tally: dict[str, int] = {}
    selection_required = 0
    for hit in body["hits"]:
        block = hit["suggestion"]["execution"]
        state = block["execution_readiness"]
        tally[state] = tally.get(state, 0) + 1
        selection_required += bool(block["output_selection_required"])
        # BR-17: every recipe-generated card grounds in the ACTIVE V2 registry — the alias map
        # resolves its template, the replacements are named, and readiness comes from BR-7's
        # fold over the replacement definitions. UNASSESSED survives only as the fallback for a
        # template the map does not know, which a real page never contains.
        assert block["recipe_contract_version"] == "recipe-contract-v2"
        assert block["v2_replacements"], hit["suggestion"]["template_id"]
        assert block["execution_readiness"] != "UNASSESSED"
        assert all(b["code"] and b["group"] for b in block["readiness_blockers"])
        # Multi-output honesty: a card spanning several atoms says so, and every atom carries
        # its OWN state — the headline is a ceiling, never an inheritance.
        assert block["output_selection_required"] == (len(block["v2_replacements"]) > 1)
        assert [r["recipe_id"] for r in block["replacement_readiness"]] \
            == block["v2_replacements"]
    assert body["readiness_counts"] == tally and sum(tally.values()) == len(body["hits"])
    assert body["output_selection_required_count"] == selection_required


def test_v4_carries_the_engines_verdicts(client, ftr_catalog):  # noqa: F811
    """SE-13. The `semantic` block — the SAME engine the hypothesis Workbench serves from, run
    unscoped over the same frozen context and anchored to this table. The equality against a v3
    body that used to prove it additive went with v3; what remains is every claim about the
    block's own content, which is what a reader of this page actually depends on."""
    v4 = client.get(f"{PATH}?contract_version=4", headers=_h()).json()
    assert v4["contract_version"] == 4
    semantic = v4["semantic"]
    assert semantic["semantic_context_hash"]
    assert semantic["table"] == TABLE
    assert "review_validity" in semantic["order_basis"] or semantic["order_basis"]
    for entry in semantic["ranked"] + semantic["actionable"]:
        assert entry["recipe_id"]
        assert entry["binding_state"] in ("bound", "ambiguous", "missing", "blocked")
        assert entry["planning_request_hash"]
        for verdict in entry["verdicts"]:
            assert verdict["status"] in ("bound", "ambiguous", "unresolved", "blocked")
    # Every RANKED entry is bound and anchored: at least one bound operand lives on this table.
    for entry in semantic["ranked"]:
        assert entry["binding_state"] == "bound"
        assert any(v["selected_ref"] and v["selected_ref"].split(".")[-2] == TABLE
                   for v in entry["verdicts"] if v["status"] == "bound")


def test_both_table_spellings_reach_the_same_engine_block(client, ftr_catalog):  # noqa: F811
    """The deep link carries the schema-qualified `object_ref` (`public.comp_fin_tran`) while the
    engine's own key is the bare table name, and `_resolve_table` has always accepted BOTH for the
    deterministic half.

    The engine block did not: it anchored on the raw parameter, so a schema-qualified caller got a
    populated `hits` list beside an EMPTY `semantic` block — the two surfaces of one page
    disagreeing about one table, and disagreeing SILENTLY, which reads as "the engine found
    nothing here" rather than "you spelled the table differently". Found on the deployed cluster
    (2026-08-15): `bo_cib_customer` answered 23 ranked / 854 actionable where
    `public.bo_cib_customer` answered 0 / 0 with 9 hits on both.

    Both spellings now resolve through the SAME function, so the answers are identical entry for
    entry — which is the SE-13 guarantee stated over the caller's spelling instead of assuming it.
    """
    bare = client.get(f"{PATH}?contract_version=4", headers=_h()).json()
    qualified = client.get(
        f"/catalog/{SOURCE}/tables/public.{TABLE}/suggestions?contract_version=4",
        headers=_h()).json()

    assert bare["semantic"]["ranked"] + bare["semantic"]["actionable"], (
        "the fixture's engine block is non-empty to compare against")
    for lens in ("ranked", "actionable"):
        assert ([e["recipe_id"] for e in qualified["semantic"][lens]]
                == [e["recipe_id"] for e in bare["semantic"][lens]]), lens
    # The deterministic half agreed on both spellings all along — that pre-existing agreement is
    # what made the engine block's silence a DEFECT rather than a different question being asked.
    assert len(qualified["hits"]) == len(bare["hits"])


def test_an_unknown_table_anchors_nothing_rather_than_everything(client, ftr_catalog):  # noqa: F811
    """The resolver's `None` (no such table FOR THIS CALLER) must anchor nothing. The failure to
    avoid is a spelling nobody can resolve quietly matching every candidate in the catalog."""
    body = client.get(
        f"/catalog/{SOURCE}/tables/no_such_table/suggestions?contract_version=4",
        headers=_h()).json()
    assert body["semantic"]["ranked"] == []
    assert body["semantic"]["actionable"] == []


def test_the_page_and_the_workbench_agree_on_binding_validity(
        client, conn, ftr_catalog):  # noqa: F811
    """The SE-13 acceptance, pinned: the deterministic page and the hypothesis Workbench run ONE
    engine over ONE frozen context, so the same recipe cannot be 'bindable here' and 'blocked
    there'. Compared against the lens called directly with the same roles."""
    from featuregen.overlay.upload.recipe_planning_lens import v2_recipe_candidates
    from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

    semantic = client.get(f"{PATH}?contract_version=4", headers=_h()).json()["semantic"]
    direct = {c.recipe_id: c.binding_state for c in v2_recipe_candidates(
        conn, catalog_source=SOURCE, roles=("feature_engineer",),
        scope=ConfirmedScope(primary=None, unscoped=True))}
    entries = semantic["ranked"] + semantic["actionable"]
    assert entries, "the anchored engine produced entries for this table"
    for entry in entries:
        assert direct[entry["recipe_id"]] == entry["binding_state"], entry["recipe_id"]


def _adjudicate(conn, object_ref: str, concept: str) -> None:
    """Settle ONE tie the way the `needs_setup` lane tells an operator to settle it: a
    human-CONFIRMED concept on one of the tied columns. The binder prefers the eligible tier
    over the provisional one, so the tie dissolves without a tie-break verdict.

    This is why the fixture is not edited instead. Nothing binds on FTR because the catalog is
    tie-pathological — `as_of_date` is carried by three columns and `account_id` by two — so
    "give the fixture a bindable candidate" is not a matter of ADDING data; it is a matter of
    somebody deciding, which is exactly the remedy the lane prescribes."""
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.column_authority import logical_ref_of

    logical = logical_ref_of(conn, SOURCE, object_ref)
    record_field_evidence(
        conn, logical_ref=logical, field_name="concept", proposed_value=concept,
        producer="human", strength="confirmed", producer_ref="user:sme",
        source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                    material=concept))


def test_the_cards_are_the_same_carrier(client, conn, ftr_catalog):  # noqa: F811
    """D4 hardens the SE-13 parity: not just "binding states agree" — the CARDS are the SAME
    carrier. Every bound entry's `card` is the projected FeatureIdea serialized by gate1's
    OWN serializer (proven by re-projecting and comparing equal), so the page and the
    Workbench cannot render two different stories about one candidate.

    T2 extends the parity to the OTHER outcome: a candidate the projection holds out carries a
    `needs_setup` entry instead, from the same projection, so "no card here" is one answer both
    surfaces give for the same reason rather than an absence each explains its own way.

    The two adjudications below exist to keep the CARD half non-vacuous: without them this
    fixture binds nothing and the card loop iterates zero times, which is a parity claim proven
    over the empty set."""
    from featuregen.overlay.upload.candidate_assembly import assemble_candidates
    from featuregen.overlay.upload.contract.gate1 import _idea_json
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_planning_lens import v2_recipe_candidates
    from featuregen.overlay.upload.semantic_projection import project_assembled_set
    from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

    # `custody_holding_dynamics` is two ambiguous operands away from binding; settling both
    # ties is what turns it into a card. (Measured: 36 of this fixture's 66 unbound required
    # operands are `ambiguous`, i.e. the concept is PRESENT and undecided.)
    _adjudicate(conn, f"public.{TABLE}.acct_id", "account_id")
    _adjudicate(conn, f"public.{TABLE}.as_of_dt", "as_of_date")

    semantic = client.get(f"{PATH}?contract_version=4", headers=_h()).json()["semantic"]
    entries = semantic["ranked"] + semantic["actionable"]
    assert entries, "the anchored engine produced entries for this table"

    context = build_generation_semantic_context(
        conn, catalog_source=SOURCE, roles=("feature_engineer",))
    candidates = v2_recipe_candidates(
        conn, catalog_source=SOURCE, roles=("feature_engineer",),
        scope=ConfirmedScope(primary=None, unscoped=True), context=context)
    anchored = [c for c in candidates if any(
        v.selected_ref and v.selected_ref.split(".")[-2] == TABLE
        for v in c.verdicts if v.status == "bound")]
    projection = project_assembled_set(assemble_candidates(anchored),
                                       catalog_source=SOURCE)
    expected = {idea.source_definition_id: _idea_json(idea)
                for idea in (*projection.ideas, *projection.actionable_ideas)
                if idea.source_definition_id}
    expected_setup = {entry.source_definition_id: entry.to_json()
                      for entry in projection.needs_setup}
    carded = [e for e in entries if e.get("card")]
    assert carded, "the adjudications gave the card half something to compare"
    for entry in carded:
        card = entry["card"]
        key = card.get("source_definition_id")
        assert key in expected, key
        assert card == expected[key], \
            "one candidate, one carrier — the page serves gate1's own serialization"
    # T2's half of the same parity: a held-out candidate carries the projection's own entry.
    held = [e for e in entries if e.get("needs_setup")]
    assert held, "this fixture still holds candidates out, and says so"
    for entry in held:
        assert not entry["card"], "a candidate is served OR held out, never both"
        assert entry["needs_setup"] == expected_setup[
            entry["needs_setup"]["source_definition_id"]]
    # LEDGERED GAP, deliberately not asserted: an entry may carry NEITHER carrier. A candidate
    # the typed gauntlet REFUSED is in `assembled.ranked` (it bound) but the projection returns
    # it as a rejection, and this block surfaces no rejections at all — the product gap
    # `contract.py`'s own three-section comment names. Closing it here would mean giving the
    # V1 rejection wire shape a definition id to key on, and `cs.rejections` is hashed into the
    # considered revision, so that is an identity move on a wire shape and belongs with T9/T10
    # rather than riding in on a card-honesty fix. The universal that used to stand here
    # ("every entry carries a card or a reason") over-claimed exactly that gap.


def test_an_unknown_table_stays_a_200_payload_state(client, ftr_catalog):  # noqa: F811
    """The honesty rule survives the cutover: "this catalog does not hold that table" is data,
    never an error, and the requested string is echoed verbatim."""
    r = client.get(f"/catalog/{SOURCE}/tables/no_such_table/suggestions", headers=_h())
    assert r.status_code == 200, r.text
    collection = r.json()["collection"]
    assert collection["table_known"] is False
    assert collection["anchor_table_ref"] == "no_such_table"
    assert r.json()["hits"] == [] and collection["neighbourhood"]["max_hops"] == 1


@pytest.mark.parametrize("version", [0, 5, 99, -1])
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
    monkeypatch.setattr(suggestions_route, "suggest_features_page_v2",
                        lambda *a, **kw: called.append(a))
    assert client.get("/catalog/src/tables/txns/suggestions?contract_version=7",
                      headers=_h()).status_code == 422
    assert called == []


def test_the_page_read_threads_the_sessions_own_read_scope(client, monkeypatch):
    """The load-bearing constraint at the page seam: the scope comes from the authenticated
    session's role claims — never a request parameter, and never a client-supplied scope key."""
    seen: dict = {}

    def _engine(conn, *, catalog_source, table, roles, max_hops):
        seen.update(catalog_source=catalog_source, table=table, roles=roles, max_hops=max_hops)
        return suggestions_route.unknown_table_page_v2(
            catalog_source=catalog_source, requested_table=table, roles=roles,
            neighbourhood=_zero_neighbourhood())

    monkeypatch.setattr(suggestions_route, "suggest_features_page_v2", _engine)
    r = client.get("/catalog/src/tables/txns/suggestions",
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


def test_the_openapi_operation_publishes_the_contract_and_the_typed_error(client):
    """OPENAPI SNAPSHOT. Three claims a client integrates against: the parameter exists and is
    deliberately unbounded above (so the handler owns the refusal), the 200 advertises the ONE
    payload, and the 422 advertises the typed error body."""
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
    assert version.get("default") == 4
    assert "maximum" not in version and "exclusiveMaximum" not in version
    assert parameters["max_hops"]["schema"]["maximum"] == MAX_HOPS_CEILING

    assert set(operation["responses"]) >= {"200", "422"}
    error = operation["responses"]["422"]["content"]["application/json"]["schema"]
    assert error["$ref"].endswith("/SuggestionsErrorResponse")
    assert set(spec["components"]["schemas"]["SuggestionsErrorResponse"]["properties"]) == {
        "detail", "error_code"}
    published = json.dumps(operation["responses"]["200"])
    assert "FeatureSuggestionPageV4Response" in published
    # The retired contracts are not published as servable payloads any more.
    assert "TableSuggestionsV1Response" not in published


def test_the_route_is_still_get_only_and_still_gated(client):
    for method in ("post", "put", "delete"):
        r = getattr(client, method)(f"{PATH}?contract_version=4", headers=_h())
        assert r.status_code == 405, f"{method} -> {r.status_code}"
    assert client.get(f"{PATH}?contract_version=4",
                      headers=_h(roles="access_admin")).status_code == 403


def test_a_lagged_projection_is_a_retryable_503_not_an_opaque_500(client, monkeypatch):
    """FROM THE LIVE CLUSTER (2026-08-09): the page showed "Could not load suggestions: Internal
    Server Error" while the backend logged CATALOG_PROJECTION_UNAVAILABLE.

    The refusal itself is CORRECT and stays: suggestions are computed on demand, and the gauntlet
    reads GOVERNED values (grain, join authority) through `_governed_read`, which fails closed
    rather than reason from a projection it knows is behind. What was wrong is only how it left the
    building — an unhandled exception became a 500, which reads as a crash and tells the caller
    nothing about the one thing that matters: this is TEMPORARY, and retrying is the right move.

    `contract.py` already maps this exception to a retryable 503 (the mapping `_governed_read`'s own
    docstring promises: "which the feature-gen route maps to a retryable 503"). This route reaches
    the identical `_governed_read` and had no such mapping. It used to be pinned for v1 AND v2,
    which ran different engine entrypoints; after the E4 cutover there is one entrypoint left.
    """
    def _lagged(*a, **k):
        raise CatalogProjectionUnavailable(
            CATALOG_PROJECTION_UNAVAILABLE,
            "load-bearing projection 'overlay' is LAGGED: checkpoint 53 < event head 54")

    monkeypatch.setattr(
        "featuregen.api.routes.suggestions.suggest_features_page_v2", _lagged)

    r = client.get(PATH, headers=_h())
    assert r.status_code == 503
    # The caller must be able to tell WHY, and that waiting is the fix.
    assert "LAGGED" in r.json()["detail"]
