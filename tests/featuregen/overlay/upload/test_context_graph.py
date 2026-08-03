"""Context Graph V1 (semantic Task 7) — served as a DOSSIER SECTION, with honest edges.

Every pitfall the adversarial review recorded for Task 7 is asserted here:

* **No separate route.** The context is a section of `build_asset_detail`, assembled inside its one
  repeatable-read transaction, and its bytes move the dossier's `consistency_token`. A `/context`
  endpoint would have served a snapshot no token covers.
* **Lineage truncation is accounted per kind**, not a bare boolean, and `_prune_to_neighbourhood`'s
  drops are counted — they were silent before.
* **A review badge never implies executability.** `executable_now` comes only from the revalidating
  reader; the pure `production_eligible` predicate stays on the realization as stored history.
* **Structural containment carries an explicit structural basis and NO evidence.**
* **Release-C-only identities are never invented for a bridge.**
* **Missing ownership/usage/data-product context is `not_supplied`** — never zero, never inferred.
* **Read scope is threaded**, so a hidden sibling never enters the roster or the graph.
"""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.context_graph import (
    CONTEXT_GRAPH_VERSION,
    NOT_SUPPLIED_CONTEXT,
    STRUCTURAL_AUTHORITY,
)
from featuregen.overlay.upload.graph import build_graph

ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform_admin",))
ANALYST = mint_test_identity(subject="user:analyst", role_claims=("data_scientist",))

_SRC = "ctxgraph"
_ANCHOR = "public.trades.notional"


def _seed(conn, source=_SRC, *, restricted_sibling: bool = False):
    rows = [
        CanonicalRow(source, "trades", "notional", "numeric",
                     definition="Notional value of the trade."),
        CanonicalRow(source, "trades", "trade_id", "text", is_grain=True),
        CanonicalRow(source, "trades", "booked_at", "timestamp", as_of=True),
        # A plain descriptive sibling: not grain, not as-of, on no edge — so the lineage
        # neighbourhood prunes it. That drop is exactly what used to happen with no accounting.
        CanonicalRow(source, "trades", "venue", "text"),
    ]
    if restricted_sibling:
        rows.append(CanonicalRow(source, "trades", "client_national_id", "text",
                                 sensitivity="restricted"))
    build_graph(conn, source, rows)


def _evidence(conn, source, field, value, *, producer="source", strength="attested",
             ref=_ANCHOR):
    record_field_evidence(
        conn, logical_ref=f"{source}::{ref}", field_name=field, proposed_value=value,
        producer=producer, strength=strength, producer_ref="upload",
        source_snapshot_id="snap", input_hash=f"h-{field}-{value}-{producer}")


def _context(conn, source=_SRC, object_ref=_ANCHOR, identity=ADMIN):
    body = build_asset_detail(conn, source=source, object_ref=object_ref,
                              roles=list(identity.role_claims), identity=identity,
                              include=["context"])
    assert body is not None
    return body["context"]


# ── it is a SECTION, inside the dossier's one snapshot ──────────────────────────────────────────


def test_context_is_a_dossier_section_not_a_separate_route(overlay_conn):
    _seed(overlay_conn)
    body = build_asset_detail(overlay_conn, source=_SRC, object_ref=_ANCHOR,
                              roles=list(ADMIN.role_claims), identity=ADMIN)
    assert "context" in body["included_sections"]
    assert body["context"]["version"] == CONTEXT_GRAPH_VERSION
    # A default dossier build carries it, so the Context tab needs no second request — and
    # therefore no second, untokened snapshot.
    assert body["consistency_token"]


def test_context_bytes_move_the_consistency_token(overlay_conn):
    """The ETag must cover the context. If it did not, two clients could cache the same token over
    materially different context and never know."""
    _seed(overlay_conn)
    before = build_asset_detail(overlay_conn, source=_SRC, object_ref=_ANCHOR,
                                roles=list(ADMIN.role_claims), identity=ADMIN)
    _evidence(overlay_conn, _SRC, "concept", "monetary_flow", producer="llm", strength="proposed")
    after = build_asset_detail(overlay_conn, source=_SRC, object_ref=_ANCHOR,
                               roles=list(ADMIN.role_claims), identity=ADMIN)
    assert before["context"] != after["context"]
    assert before["consistency_token"] != after["consistency_token"]


# ── edges: authority, currentness, evidence, why ────────────────────────────────────────────────


def test_structural_contains_edge_carries_a_structural_basis_and_no_evidence(overlay_conn):
    _seed(overlay_conn)
    context = _context(overlay_conn)
    contains = [e for e in context["edges"] if e["kind"] == "contains"]
    assert contains, "the anchor's table must appear as a containment edge"
    for edge in contains:
        assert edge["authority"] == STRUCTURAL_AUTHORITY
        assert edge["evidence_ids"] == []
        assert edge["producer"] is None and edge["strength"] is None
        assert edge["why"]


def test_every_edge_states_authority_currentness_and_a_why(overlay_conn):
    _seed(overlay_conn)
    _evidence(overlay_conn, _SRC, "concept", "monetary_flow", producer="llm", strength="proposed")
    context = _context(overlay_conn)
    assert context["edges"]
    for edge in context["edges"]:
        assert edge["authority"], f"edge {edge['kind']} has no authority basis"
        assert isinstance(edge["current"], bool)
        assert edge["why"], f"edge {edge['kind']} has no explanation"
        assert isinstance(edge["evidence_ids"], list)


def test_the_concept_edge_carries_the_real_d2_triple_not_just_a_label(overlay_conn):
    """An `llm/proposed` concept is USABLE context (the no-blocked rule) — shown with its real
    producer/strength/lifecycle so nothing downstream has to guess, and labelled `llm_proposed`
    for display only."""
    _seed(overlay_conn)
    _evidence(overlay_conn, _SRC, "concept", "monetary_flow", producer="llm", strength="proposed")
    overlay_conn.execute(
        "UPDATE graph_node SET concept = 'monetary_flow' WHERE catalog_source = %s "
        "AND object_ref = %s", (_SRC, _ANCHOR))
    context = _context(overlay_conn)
    classified = [e for e in context["edges"] if e["kind"] == "classified_as"]
    assert len(classified) == 1
    assert (classified[0]["producer"], classified[0]["strength"]) == ("llm", "proposed")
    assert classified[0]["lifecycle"] == "active"
    assert classified[0]["authority"] == "llm_proposed"
    assert classified[0]["evidence_ids"]


def test_concept_ancestry_is_chained_as_is_a_edges(overlay_conn):
    _seed(overlay_conn)
    _evidence(overlay_conn, _SRC, "concept", "monetary_flow", producer="llm", strength="proposed")
    overlay_conn.execute(
        "UPDATE graph_node SET concept = 'monetary_flow' WHERE catalog_source = %s "
        "AND object_ref = %s", (_SRC, _ANCHOR))
    context = _context(overlay_conn)
    assert context["concept_path"][0] == "monetary_flow"
    if len(context["concept_path"]) > 1:
        is_a = [e for e in context["edges"] if e["kind"] == "is_a"]
        assert len(is_a) == len(context["concept_path"]) - 1
        # An ancestry edge is a fact about the REGISTRY, not an assertion anybody made about this
        # column — so it carries a structural basis and no evidence.
        for edge in is_a:
            assert edge["authority"] == STRUCTURAL_AUTHORITY
            assert edge["evidence_ids"] == []


# ── relationships: availability, review and executability kept apart ────────────────────────────


def test_a_context_with_no_links_says_so_rather_than_implying_none_exist(overlay_conn):
    _seed(overlay_conn)
    context = _context(overlay_conn)
    assert context["relationships"] == []
    # The honest code — read as "not in this bundle for this caller", never as a data-quality
    # verdict (the closed vocabulary's own contract).
    assert "relationship_context_absent" in context["uncertainty"]["missing_context"]


def _reviewed_and_eligible_link():
    """A link a human VERIFIED, whose stored realization is production-eligible — the exact shape
    that must still not read as executable without a live revalidation."""
    from featuregen.overlay.upload.semantic_context import (
        DirectionalRealizationContextV1,
        RelationshipContextV1,
        RelationshipKind,
    )

    realization = DirectionalRealizationContextV1(
        realization_revision_id="brr_1", from_ref="ftr::public.a", to_ref="cib::public.b",
        lifecycle="active", safety_status="deterministically_validated", cardinality="1:1",
        scope_id="scope-1", sandbox_eligible=True, production_eligible=True)
    return RelationshipContextV1(
        relationship_ref="bfk_1", kind=RelationshipKind.DIRECT_EQUALITY.value,
        left_ref="ftr::public.a.id", right_ref="cib::public.b.id",
        availability="available", review_status="human_verified",
        assessment_revision_id="bca_1", realizations=(realization,),
        producer="taxonomy", strength="confirmed", lifecycle="active", current=True,
        evidence_ids=("ev_1",))


def test_a_human_verified_review_does_not_make_a_link_executable(overlay_conn, monkeypatch):
    """The review's sharpest Task-7 rule. `production_eligible` is a PURE predicate over the
    stored record — it cannot see a dependency withdrawn since — so "executable now" is asked of
    the revalidating reader and of nothing else. Here that reader says no; the payload must agree,
    however verified the review and however eligible the stored realization."""
    from featuregen.overlay.upload import context_graph as cg

    monkeypatch.setattr(cg, "_executable_realization_ids", lambda conn, ref: frozenset())
    payload = cg._relationship_dict(overlay_conn, _reviewed_and_eligible_link())

    assert payload["review_status"] == "human_verified"
    assert payload["realizations"][0]["production_eligible"] is True   # stored history, unchanged
    assert payload["executable_now"] is False
    assert payload["realizations"][0]["executable_now"] is False


def test_executable_now_follows_the_revalidating_reader_when_it_says_yes(overlay_conn,
                                                                        monkeypatch):
    from featuregen.overlay.upload import context_graph as cg

    monkeypatch.setattr(cg, "_executable_realization_ids", lambda conn, ref: frozenset({"brr_1"}))
    payload = cg._relationship_dict(overlay_conn, _reviewed_and_eligible_link())
    assert payload["executable_now"] is True
    assert payload["realizations"][0]["executable_now"] is True


def test_a_revalidation_fault_never_reads_as_executable(overlay_conn, monkeypatch):
    """Fail CLOSED: if the revalidating reader raises, the honest answer is "nothing is executable",
    never the pure predicate silently standing in for it."""
    from featuregen.overlay.upload import bridge_store
    from featuregen.overlay.upload import context_graph as cg

    def _boom(*_args, **_kwargs):
        raise RuntimeError("dependency store unavailable")

    monkeypatch.setattr(bridge_store, "executable_bridge_realizations", _boom)
    assert cg._executable_realization_ids(overlay_conn, "bfk_1") == frozenset()


def test_a_revalidation_DB_fault_leaves_the_TRANSACTION_usable(overlay_conn, monkeypatch):
    """Failing closed is only half the guard. A `RuntimeError` degrades cleanly, but a DATABASE
    fault leaves PostgreSQL's transaction ABORTED — every later statement then raises
    `InFailedSqlTransaction`, so the honest "nothing is executable" is immediately followed by the
    whole dossier dying. The guarded read runs inside a SAVEPOINT so the rollback is scoped to it."""
    from featuregen.overlay.upload import bridge_store
    from featuregen.overlay.upload import context_graph as cg

    def _bad_read(conn, **_kwargs):
        conn.execute("SELECT no_such_column_zz FROM graph_node")

    monkeypatch.setattr(bridge_store, "executable_bridge_realizations", _bad_read)
    assert cg._executable_realization_ids(overlay_conn, "bfk_1") == frozenset()
    assert overlay_conn.execute("SELECT 1").fetchone() == (1,)


def test_a_context_FAULT_degrades_the_SECTION_not_the_dossier(overlay_conn, monkeypatch):
    """The F0 contract says a section fault degrades to `unavailable`. It did — and then took the
    dossier down anyway, because catching the exception does not un-abort the transaction the fault
    aborted. `relationships` and `readiness` are assembled AFTER `context` in the same
    repeatable-read transaction, so they died with `InFailedSqlTransaction` on a fault they had
    nothing to do with."""
    from featuregen.overlay.upload import context_graph as cg

    def _bad_read(conn, **_kwargs):
        conn.execute("SELECT no_such_column_zz FROM graph_node")

    _seed(overlay_conn)
    monkeypatch.setattr(cg, "build_context_section", _bad_read)
    body = build_asset_detail(overlay_conn, source=_SRC, object_ref=_ANCHOR,
                              roles=list(ADMIN.role_claims), identity=ADMIN)
    assert body is not None
    assert body["context"] == {"status": "unavailable"}
    # The sections that come AFTER it still answer.
    assert {"relationships", "readiness"} <= set(body["included_sections"])
    assert body["relationships"] is not None
    assert body["readiness"] is not None


def test_availability_never_encodes_safety(overlay_conn, monkeypatch):
    """D3 deleted the four-way `discoverable|sandbox_only|executable|unavailable` merge: a link's
    `availability` carries only the two LinkAvailability words, with review and deterministic
    safety as separate fields."""
    from featuregen.overlay.upload import context_graph as cg

    monkeypatch.setattr(cg, "_executable_realization_ids", lambda conn, ref: frozenset())
    payload = cg._relationship_dict(overlay_conn, _reviewed_and_eligible_link())
    assert payload["availability"] in {"available", "unavailable"}
    assert payload["review_status"] == "human_verified"
    assert payload["realizations"][0]["safety_status"] == "deterministically_validated"


def test_fan_out_is_never_shown_without_its_direction_and_scope(overlay_conn, monkeypatch):
    from featuregen.overlay.upload import context_graph as cg

    monkeypatch.setattr(cg, "_executable_realization_ids", lambda conn, ref: frozenset())
    realization = cg._relationship_dict(
        overlay_conn, _reviewed_and_eligible_link())["realizations"][0]
    assert realization["cardinality"] == "1:1"
    # A cardinality without its direction and applicability scope is a number nobody can act on.
    assert realization["from_ref"] and realization["to_ref"]
    assert realization["scope_id"] == "scope-1"


def test_release_c_only_identities_are_never_invented_for_a_bridge(overlay_conn):
    """`definition_revision_id` / `execution_revision_id` / ordered-leg pins exist only after
    Release C. D3 drops them from the contract entirely, so no payload may carry them."""
    _seed(overlay_conn)
    context = _context(overlay_conn)
    blob = repr(context)
    for invented in ("definition_revision_id", "execution_revision_id", "leg_plan_hashes",
                     "leg_realization_revision_ids", "applicability_scope_hash"):
        assert invented not in blob


# ── truncation: per-kind accounting, never a silent cut ─────────────────────────────────────────


def test_truncation_reports_per_kind_omissions(overlay_conn):
    _seed(overlay_conn)
    context = _context(overlay_conn)
    truncation = context["truncation"]
    assert set(truncation) == {"truncated", "omitted"}
    assert truncation["truncated"] is False
    # The lineage neighbourhood prunes a non-participating sibling column; that used to vanish with
    # no accounting at all. It is now COUNTED and namespaced, so it can never be mistaken for the
    # roster the section does carry.
    assert all(k.startswith("lineage_") or k in {"related_column", "relationship"}
               for k in truncation["omitted"])


def test_lineage_truncation_report_distinguishes_a_budget_cut_from_a_prune(overlay_conn):
    from datetime import UTC, datetime, timedelta

    from featuregen.overlay.upload.lineage import lineage_graph

    _seed(overlay_conn)
    graph = lineage_graph(overlay_conn, _SRC, _ANCHOR, now=datetime(2026, 8, 1, tzinfo=UTC),
                          fresh_within=timedelta(days=36500), roles=list(ADMIN.role_claims))
    assert graph is not None
    # Nothing hit the node cap, so `truncated` is False — its shipped meaning, unchanged.
    assert graph["truncated"] is False
    assert graph["truncation"]["truncated"] is False
    # …but the prune DID drop something, and now says what.
    assert graph["truncation"]["omitted"]

    # A BUDGET cut needs a neighbour to refuse: the anchor unit installs complete even past the
    # cap by design (a table is never shown with a partial column list).
    build_graph(overlay_conn, "ctxcap", [
        CanonicalRow("ctxcap", "trades", "venue_id", "text", joins_to="venues.venue_id"),
        CanonicalRow("ctxcap", "venues", "venue_id", "text"),
        CanonicalRow("ctxcap", "venues", "venue_name", "text"),
    ])
    tiny = lineage_graph(overlay_conn, "ctxcap", "public.trades.venue_id",
                         now=datetime(2026, 8, 1, tzinfo=UTC),
                         fresh_within=timedelta(days=36500), roles=list(ADMIN.role_claims),
                         max_nodes=1)
    assert tiny["truncated"] is True
    assert tiny["truncation"]["truncated"] is True
    # The refused unit AND the edge that would have dangled from it are both counted, so "this
    # column has no joins" and "its joins did not fit" are distinguishable.
    assert tiny["truncation"]["omitted"].get("join") == 1
    assert tiny["truncation"]["omitted"].get("column")


# ── profiles ────────────────────────────────────────────────────────────────────────────────────


def test_profile_identity_is_absent_and_named_when_the_flag_is_off(overlay_conn):
    """`FEATUREGEN_DATASET_PROFILES` off ⟹ no profile node, and the bundle says
    `dataset_profile_absent` rather than the section fabricating an empty profile."""
    _seed(overlay_conn)
    context = _context(overlay_conn)
    assert context["profiles"]["dataset_profile_hash"] is None
    assert context["profiles"]["catalog_profile_revision_id"] is None
    assert not [n for n in context["nodes"] if n["kind"] == "dataset_profile"]
    assert "dataset_profile_absent" in context["uncertainty"]["missing_context"]


def test_profile_node_carries_both_identities_when_the_flag_is_on(overlay_conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    _seed(overlay_conn)
    _evidence(overlay_conn, _SRC, "table_role", "bridge", ref="public.trades")
    from featuregen.overlay.upload.field_resolution import resolve_and_project

    resolve_and_project(overlay_conn, source=_SRC, logical_refs=[f"{_SRC}::public.trades"])
    context = _context(overlay_conn)

    assert context["profiles"]["dataset_profile_hash"]
    profile_nodes = [n for n in context["nodes"] if n["kind"] == "dataset_profile"]
    assert len(profile_nodes) == 1
    detail = profile_nodes[0]["detail"]
    assert detail["dataset_profile_hash"] == context["profiles"]["dataset_profile_hash"]
    # The legacy canonical `bridge` role displays as `crosswalk` — one adapter, everywhere.
    assert detail["data_role"]["value"] == "crosswalk"
    edges = [e for e in context["edges"] if e["kind"] == "profiled_by"]
    assert len(edges) == 1 and edges[0]["authority"] == STRUCTURAL_AUTHORITY


# ── absence, read scope ─────────────────────────────────────────────────────────────────────────


def test_ownership_usage_and_data_product_are_not_supplied_never_zero(overlay_conn):
    _seed(overlay_conn)
    context = _context(overlay_conn)
    assert context["not_supplied"] == list(NOT_SUPPLIED_CONTEXT)
    assert context["uncertainty"]["not_supplied"] == list(NOT_SUPPLIED_CONTEXT)
    # Nothing renders these as a count, an empty list of owners, or an inferred owner.
    assert "owners" not in context and "usage_count" not in context


def test_a_restricted_sibling_never_enters_the_context_of_a_visible_column(overlay_conn):
    """The bundle is read-scoped (D11) and this section inherits that scope — it must not widen it.
    A restricted column's NAME is exactly what must not leak into another column's context."""
    _seed(overlay_conn, restricted_sibling=True)
    context = _context(overlay_conn, identity=ANALYST)
    blob = repr(context)
    assert "client_national_id" not in blob
    assert all(c["column"] != "client_national_id" for c in context["related_columns"])


def test_a_table_anchor_gets_structural_context_and_says_why_there_is_no_bundle(overlay_conn):
    _seed(overlay_conn)
    context = _context(overlay_conn, object_ref="public.trades")
    assert context["status"] == "table"
    assert "note" in context
    # No fabricated column-grain meaning on a table asset.
    assert "source_meaning" not in context and "concept_path" not in context
    assert context["not_supplied"] == list(NOT_SUPPLIED_CONTEXT)


def test_the_graph_never_draws_an_edge_to_a_node_it_does_not_carry(overlay_conn):
    _seed(overlay_conn)
    _evidence(overlay_conn, _SRC, "concept", "monetary_flow", producer="llm", strength="proposed")
    context = _context(overlay_conn)
    ids = {n["id"] for n in context["nodes"]}
    for edge in context["edges"]:
        assert edge["from"] in ids and edge["to"] in ids


def test_source_and_resolved_meaning_are_reported_separately(overlay_conn):
    """The tab shows what the SOURCE said and what the platform RESOLVED as two things. Collapsing
    them is how a source assertion and a model proposal become indistinguishable."""
    _seed(overlay_conn)
    _evidence(overlay_conn, _SRC, "definition", "Notional value of the trade.")
    context = _context(overlay_conn)
    source_fields = {v["field"] for v in context["source_meaning"]}
    assert "definition" in source_fields
    assert all(v["resolution_status"] == "declared" for v in context["source_meaning"])
    assert {v["field"] for v in context["resolved_meaning"]}
