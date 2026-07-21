"""Delivery F / Task F2b — the SEMANTIC relationship subsection on the asset read model.

Fills what F0 stubbed ``{"status": "unavailable", ...}`` with real Delivery-E data: VERIFIED
entity/currency edges (E3 projections), the D1 current-set candidate history + the declared≠governed
divergence signal, and the server-calculated governance actions the CALLER may run per binding
(reusing E2's owner-or-admin available-actions authz). Exercised at the domain level
(``build_asset_detail``) with the overlay harness so a REAL fact stream + catalog adapter back the
governance-action computation; the ROUTE surface + envelope are covered in
``tests/featuregen/api/test_assets.py``.

Read-scoped + fail-closed, mirroring the peer sections: a hidden endpoint is OMITTED (no count), a
caller without catalog:read gets the subsection named in ``unavailable_sections`` (no hidden count),
and VERIFIED is rendered distinctly from proposed.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb
from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity
from tests.featuregen.overlay._helpers import seed_verified_via_command

from featuregen.contracts import Command
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.identity import CatalogObjectRef, fact_key, proposal_fingerprint
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

SVC = mint_test_service_identity(subject="service:overlay", role_claims=("overlay",),
                                 attestation="sig")
READER = mint_test_identity(subject="user:reader", role_claims=("catalog_viewer",))


# ── builders / helpers ────────────────────────────────────────────────────────────────────────────

def _measure_ref(source, table="trades", column="notional") -> CatalogObjectRef:
    return CatalogObjectRef(source, "column", "public", table, column)


def _cb_value(source, table="trades", column="ccy") -> dict:
    return {"currency_column": {"catalog_source": source, "object_kind": "column",
                                "schema": "public", "table": table, "column": column}}


def _trades_graph(conn, source):
    build_graph(conn, source, [CanonicalRow(source, "trades", "notional", "numeric"),
                               CanonicalRow(source, "trades", "ccy", "text")])


def _semantic(conn, source, object_ref, *, roles, identity):
    body = build_asset_detail(conn, source=source, object_ref=object_ref, roles=roles,
                              identity=identity, include=["relationships"])
    return body["relationships"]["semantic"]


def _verified_currency_edge(conn, source, from_ref, to_ref, *, fk, ev):
    conn.execute(
        "INSERT INTO semantic_binding_edge (fact_key, catalog_source, kind, from_ref, to_ref, "
        "confirmed_event_id, status) VALUES (%s, %s, 'currency_binding', %s, %s, %s, 'VERIFIED')",
        (fk, source, from_ref, to_ref, ev))


def _govern_entity(conn, source, object_ref, *, entity, declared, fk="e-fk", ev="e-ev"):
    conn.execute(
        "UPDATE graph_node SET entity=%s, declared_entity=%s, entity_status='VERIFIED', "
        "entity_fact_key=%s, entity_fact_event_id=%s WHERE catalog_source=%s AND object_ref=%s",
        (entity, declared, fk, ev, source, object_ref))


def _link_candidate(conn, *, source, fk, proposed_event_id, disposition, reason_codes,
                    subject="public.trades.notional", target="public.trades.ccy",
                    table_ref="public.trades", cset="cset-1", cand="cand-1"):
    """A minimal D1 candidate set + candidate + current-set pointer + proposal link (raw SQL — the
    WORM triggers block UPDATE/DELETE, never INSERT) so the read model's candidate/provenance joins
    have rows to surface."""
    conn.execute(
        "INSERT INTO semantic_binding_candidate_set (candidate_set_id, catalog_source, "
        "table_graph_ref, ingestion_run_id, attempt_no, metadata_input_fingerprint, task_version, "
        "prompt_version, schema_version, config_version, completion_status, content_hash) "
        "VALUES (%s, %s, %s, 'run-1', 1, 'fp-1', 'v1', 'p1', 'sv1', 'cv1', 'complete', 'ch1') "
        "ON CONFLICT DO NOTHING", (cset, source, table_ref))
    conn.execute(
        "INSERT INTO semantic_binding_candidate (candidate_id, candidate_set_id, catalog_source, "
        "subject_graph_ref, subject_logical_ref, binding_kind, target_graph_ref, target_logical_ref, "
        "proposed_value, disposition, reason_codes, evidence_json, input_hash, model_version, "
        "prompt_version, schema_version, config_version) "
        "VALUES (%s, %s, %s, %s, %s, 'currency_binding', %s, %s, NULL, %s, %s, '{}', 'ih1', 'm1', "
        "'p1', 'sv1', 'cv1')",
        (cand, cset, source, subject, f"{source}::{subject}", target, f"{source}::{target}",
         disposition, Jsonb(reason_codes)))
    conn.execute(
        "INSERT INTO current_semantic_binding_candidate_set (catalog_source, table_graph_ref, "
        "candidate_set_id, metadata_input_fingerprint, status) VALUES (%s, %s, %s, 'fp-1', 'current')",
        (source, table_ref, cset))
    conn.execute(
        "INSERT INTO semantic_binding_candidate_proposal (candidate_id, fact_key, proposed_event_id) "
        "VALUES (%s, %s, %s)", (cand, fk, proposed_event_id))


# ── (1) VERIFIED entity + currency edges, marked VERIFIED with provenance (distinct from proposed) ──

def test_verified_entity_and_currency_edges_rendered_verified(overlay_conn):
    conn = overlay_conn
    _trades_graph(conn, "t1")
    _govern_entity(conn, "t1", "public.trades.notional", entity="customer", declared="customer")
    _verified_currency_edge(conn, "t1", "public.trades.notional", "public.trades.ccy",
                            fk="c-fk", ev="c-ev")

    sem = _semantic(conn, "t1", "public.trades.notional", roles=READER.role_claims, identity=READER)
    assert sem["status"] == "available"
    by_kind = {e["kind"]: e for e in sem["verified_edges"]}
    assert by_kind["entity_assignment"]["status"] == "VERIFIED"
    assert by_kind["entity_assignment"]["entity"] == "customer"
    assert by_kind["entity_assignment"]["fact_key"] == "e-fk"
    assert by_kind["entity_assignment"]["confirmed_event_id"] == "e-ev"
    assert by_kind["currency_binding"]["status"] == "VERIFIED"
    assert by_kind["currency_binding"]["from_ref"] == "public.trades.notional"
    assert by_kind["currency_binding"]["to_ref"] == "public.trades.ccy"
    assert by_kind["currency_binding"]["fact_key"] == "c-fk"
    assert by_kind["currency_binding"]["confirmed_event_id"] == "c-ev"
    # VERIFIED edges are NOT candidates and NOT divergences — the lists are distinct.
    assert sem["candidates"] == [] and sem["divergences"] == []


# ── (1b) M-5: a VERIFIED edge whose OTHER endpoint has NO graph_node row is OMITTED (fail-closed) ───

def test_m5_currency_edge_to_absent_endpoint_is_omitted(overlay_conn):
    conn = overlay_conn
    # Only `notional` has a graph_node row; the edge points at a column with NO node at all.
    build_graph(conn, "t1b", [CanonicalRow("t1b", "trades", "notional", "numeric")])
    _verified_currency_edge(conn, "t1b", "public.trades.notional", "public.trades.ghost",
                            fk="c-fk", ev="c-ev")

    # Fail-closed: the missing endpoint must NOT be admitted via a NULL LEFT-JOIN sensitivity — the
    # whole edge is omitted (no count, no id), so the anchor shows NO verified currency edge.
    sem = _semantic(conn, "t1b", "public.trades.notional", roles=READER.role_claims, identity=READER)
    assert sem["status"] == "available"
    assert [e for e in sem["verified_edges"] if e["kind"] == "currency_binding"] == []


# ── (2) A DRAFT/candidate binding is shown as PROPOSED (disposition + reason codes), NOT verified ───

def test_draft_candidate_shown_as_proposed_not_verified(overlay_conn):
    conn = overlay_conn
    _trades_graph(conn, "t2")
    ref, value = _measure_ref("t2"), _cb_value("t2")
    res = propose_fact(conn, Command("propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "currency_binding", "proposed_value": value},
        SVC, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    fk = fact_key(ref, "currency_binding")
    _link_candidate(conn, source="t2", fk=fk, proposed_event_id=res.produced_event_ids[0],
                    disposition="strong", reason_codes=["name_match", "concept_currency"])

    sem = _semantic(conn, "t2", "public.trades.notional", roles=READER.role_claims, identity=READER)
    assert sem["verified_edges"] == []          # a DRAFT is NOT a verified edge
    assert len(sem["candidates"]) == 1
    c = sem["candidates"][0]
    assert c["binding_kind"] == "currency_binding"
    assert c["disposition"] == "strong"
    assert c["reason_codes"] == ["name_match", "concept_currency"]
    assert c["fact_status"] == "PROPOSED"       # DRAFT folded → PROPOSED (E2), distinct from VERIFIED
    assert c["fact_key"] == fk
    assert c["subject_graph_ref"] == "public.trades.notional"
    assert c["target_graph_ref"] == "public.trades.ccy"
    assert c["available_actions"] == []         # a read-only caller cannot act on the DRAFT

    # An authorized caller (governance queue: owner_of→None under the upload-context adapter, so a
    # platform-admin is the authority) DOES get the DRAFT's confirm/reject commands.
    admin = mint_test_identity(subject="user:admin",
                               role_claims=("platform_admin", "platform-admin"))
    admin_sem = _semantic(conn, "t2", "public.trades.notional",
                          roles=admin.role_claims, identity=admin)
    assert admin_sem["candidates"][0]["available_actions"] == ["confirm", "reject"]


# ── (3) A divergence (declared_entity ≠ governed entity) is surfaced as a divergence signal ─────────

def test_divergence_declared_entity_differs_from_governed(overlay_conn):
    conn = overlay_conn
    build_graph(conn, "t3", [CanonicalRow("t3", "t", "c", "text")])
    _govern_entity(conn, "t3", "public.t.c", entity="customer", declared="account")

    sem = _semantic(conn, "t3", "public.t.c", roles=READER.role_claims, identity=READER)
    assert len(sem["divergences"]) == 1
    d = sem["divergences"][0]
    assert d["kind"] == "entity_divergence"
    assert d["declared_entity"] == "account"    # the file's conflicting value, preserved
    assert d["governed_entity"] == "customer"   # governed VERIFIED-wins value
    assert d["object_ref"] == "public.t.c"
    # the governed entity still stands as the VERIFIED edge (governed wins; file kept as declared).
    assert sem["verified_edges"][0]["entity"] == "customer"


def test_no_divergence_when_declared_matches_governed(overlay_conn):
    conn = overlay_conn
    build_graph(conn, "t3b", [CanonicalRow("t3b", "t", "c", "text")])
    _govern_entity(conn, "t3b", "public.t.c", entity="Customer", declared="customer")  # case-only
    sem = _semantic(conn, "t3b", "public.t.c", roles=READER.role_claims, identity=READER)
    assert sem["divergences"] == []             # case-insensitive equal → no divergence


# ── (4) Governance actions: owner/admin see actions per binding; a read-only caller sees NONE ───────

def test_governance_actions_owner_admin_yes_readonly_none(overlay_conn):
    conn = overlay_conn
    _trades_graph(conn, "t4")
    seed_verified_via_command(conn, ref=_measure_ref("t4"), fact_type="currency_binding",
                              value=_cb_value("t4"), owner="user:owner")
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    admin = mint_test_identity(subject="user:admin",
                               role_claims=("platform_admin", "platform-admin"))

    def _edge(identity):
        return _semantic(conn, "t4", "public.trades.notional",
                         roles=identity.role_claims, identity=identity)["verified_edges"][0]

    # The owner (owner-of the table) and a platform admin see the VERIFIED edit commands.
    assert _edge(owner)["available_actions"] == ["reverify", "withdraw", "correct"]
    assert _edge(admin)["available_actions"] == ["reverify", "withdraw", "correct"]
    # A read-only caller sees the edge but NO actions — the UI cannot advertise it as editable.
    assert _edge(READER)["available_actions"] == []


# ── (5) Read-scope: no catalog:read → subsection withheld (explicit, no hidden count) ──────────────

def test_semantic_withheld_without_catalog_read_permission(overlay_conn):
    conn = overlay_conn
    build_graph(conn, "t5", [CanonicalRow("t5", "t", "c", "text")])
    # access_admin holds ONLY iam:manage — no catalog:read (mirrors the route gate, section-level).
    body = build_asset_detail(conn, source="t5", object_ref="public.t.c",
                              roles=("access_admin",), include=["relationships"])
    assert body["relationships"]["semantic"] == {"status": "unavailable"}     # explicit, no lists
    assert body["unavailable_sections"] == ["relationships.semantic"]          # no hidden count


# ── (6) An anchor with NO semantic data → explicit empty-but-available (not "unavailable") ──────────

def test_empty_but_available_when_no_semantic_data(overlay_conn):
    conn = overlay_conn
    build_graph(conn, "t6", [CanonicalRow("t6", "t", "c", "text")])
    body = build_asset_detail(conn, source="t6", object_ref="public.t.c",
                              roles=("catalog_viewer",), include=["relationships"])
    assert body["relationships"]["semantic"] == {
        "status": "available", "verified_edges": [], "candidates": [], "divergences": []}
    assert "relationships.semantic" not in body["unavailable_sections"]


def test_table_anchor_gets_empty_but_available_semantic(overlay_conn):
    conn = overlay_conn
    _trades_graph(conn, "t7")
    _verified_currency_edge(conn, "t7", "public.trades.notional", "public.trades.ccy",
                            fk="c7", ev="e7")
    # A TABLE anchor has no direct binding of its own — explicit empty, never a stub or a fake edge.
    body = build_asset_detail(conn, source="t7", object_ref="public.trades",
                              roles=("catalog_viewer",), include=["relationships"])
    assert body["relationships"]["semantic"] == {
        "status": "available", "verified_edges": [], "candidates": [], "divergences": []}
