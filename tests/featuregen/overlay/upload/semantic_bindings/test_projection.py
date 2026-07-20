"""E3 — the VERIFIED semantic-binding projection (migration 1015 + sync + reproject + replay).

Exercises the operational projection of a CONFIRMED (VERIFIED) governed semantic fact:
entity_assignment -> graph_node.entity (+ declared_entity preserved + provenance + search_doc), and
currency_binding -> a semantic_binding_edge. Covers the seven brief tests: migration shape, the two
confirm-time projections, the non-VERIFIED demotion (+ the status='VERIFIED' 2nd gate), re-upload
survival (governed WINS, conflict recorded not overwritten), dependency-staling demotion, and
reset()+replay parity with the synchronous path.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen._helpers import mint_test_service_identity
from tests.featuregen.overlay._helpers import StubCatalog, seed_verified_via_command

from featuregen.overlay.catalog import CatalogObject, register_catalog_adapter
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.expiry import fire_due_overlay_expiries
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.semantic_bindings.projection import (
    SemanticBindingProjection,
    reproject_semantic_bindings,
    verified_currency_binding,
    verified_entity_of,
)
from featuregen.projections.runner import run_projection

SOURCE = "fixture"
# The subject lives in table "party" (deliberately NOT "customers": "customers" stems to the same
# lexeme as the entity "customer", which would pollute the search_doc discriminator below).
ENTITY_OBJ = "public.party.cust_id"
MEASURE_OBJ = "public.trades.notional"
CCY_OBJ = "public.trades.ccy"
SVC = mint_test_service_identity(subject="service:drift", role_claims=("overlay",), attestation="a")


# --- ref / value builders ------------------------------------------------------------------------

def _entity_col() -> CatalogObjectRef:
    return CatalogObjectRef(SOURCE, "column", "sales", "party", "cust_id")


def _measure_col() -> CatalogObjectRef:
    return CatalogObjectRef(SOURCE, "column", "sales", "trades", "notional")


def _ccy_ref() -> dict:
    return {"catalog_source": SOURCE, "object_kind": "column", "schema": "sales",
            "table": "trades", "column": "ccy"}


def _build_entity_graph(conn, *, file_entity: str | None) -> None:
    """Build the party.cust_id column node with the FILE-declared entity (or none)."""
    build_graph(conn, SOURCE, [CanonicalRow(source=SOURCE, table="party", column="cust_id",
                                            type="text", entity=file_entity or "")])


def _drain(conn) -> None:
    while run_projection(conn, OverlayProjection()) >= 500:
        pass


def _node(conn, obj_ref=ENTITY_OBJ):
    return conn.execute(
        "SELECT entity, declared_entity, entity_fact_key, entity_fact_event_id, entity_status "
        "FROM graph_node WHERE catalog_source = %s AND object_ref = %s", (SOURCE, obj_ref)).fetchone()


def _matches(conn, obj_ref, term: str) -> bool:
    """Whether the node's search_doc matches a full-text term (stem-safe)."""
    return conn.execute(
        "SELECT search_doc @@ plainto_tsquery('english', %s) FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s", (term, SOURCE, obj_ref)).fetchone()[0]


# --- 1) migration shape --------------------------------------------------------------------------

def test_migration_1015_columns_and_indexes_present(db):
    cols = {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'graph_node'").fetchall()}
    assert {"declared_entity", "entity_fact_key", "entity_fact_event_id", "entity_status"} <= cols
    edge_cols = {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'semantic_binding_edge'").fetchall()}
    assert {"fact_key", "catalog_source", "kind", "from_ref", "to_ref", "confirmed_event_id",
            "status", "projected_at"} <= edge_cols
    idx = {r[0] for r in db.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename IN ('semantic_binding_edge', 'graph_node')"
    ).fetchall()}
    assert {"semantic_binding_edge_from_idx", "semantic_binding_edge_to_idx",
            "semantic_binding_edge_status_idx", "graph_node_entity_fact_key_idx"} <= idx


# --- 2) confirm VERIFIED entity_assignment -> projected ------------------------------------------

def test_confirm_entity_assignment_projects_entity_with_provenance_and_declared(db):
    _build_entity_graph(db, file_entity="account")   # the file declares "account"
    key, confirmed = seed_verified_via_command(
        db, ref=_entity_col(), fact_type="entity_assignment",
        value={"entity_id": "customer"}, owner="user:alice")   # confirm-time sync projection runs

    entity, declared, fk, fev, status = _node(db)
    assert entity == "customer"          # the governed effective entity
    assert declared == "account"         # the file's display entity preserved as labelled context
    assert fk == key and status == "VERIFIED" and fev == confirmed   # provenance links
    assert _matches(db, ENTITY_OBJ, "customer")   # search_doc rebuilt around the governed tag
    assert verified_entity_of(db, SOURCE, ENTITY_OBJ) == "customer"  # 2nd gate passes


# --- 3) confirm VERIFIED currency_binding -> semantic_binding_edge --------------------------------

def test_confirm_currency_binding_projects_verified_edge(db):
    key, confirmed = seed_verified_via_command(
        db, ref=_measure_col(), fact_type="currency_binding",
        value={"currency_column": _ccy_ref()}, owner="user:alice")

    edge = verified_currency_binding(db, key)
    assert edge is not None
    assert edge["kind"] == "currency_binding" and edge["status"] == "VERIFIED"
    assert edge["from_ref"] == MEASURE_OBJ and edge["to_ref"] == CCY_OBJ
    assert edge["confirmed_event_id"] == confirmed and edge["catalog_source"] == SOURCE


# --- 4) non-VERIFIED transition -> demotion (+ status='VERIFIED' 2nd gate) ------------------------

def test_expiry_demotes_entity_restoring_declared_and_clearing_provenance(db):
    _build_entity_graph(db, file_entity="account")
    seed_verified_via_command(db, ref=_entity_col(), fact_type="entity_assignment",
                              value={"entity_id": "customer"}, owner="user:alice")
    assert _node(db)[0] == "customer"

    # Fire the armed expiry timer -> VERIFIED -> REVERIFY; the async hook demotes immediately.
    fire_due_overlay_expiries(db, now=datetime.now(UTC) + timedelta(days=4000))

    entity, declared, fk, fev, status = _node(db)
    assert entity == "account"                        # file display context RESTORED (no data loss)
    assert declared is None and fk is None and fev is None and status is None   # provenance cleared
    assert _matches(db, ENTITY_OBJ, "account") and not _matches(db, ENTITY_OBJ, "customer")
    assert verified_entity_of(db, SOURCE, ENTITY_OBJ) is None   # 2nd gate now fails closed


def test_expiry_demotes_currency_edge_and_second_gate_hides_it(db):
    key, _ = seed_verified_via_command(db, ref=_measure_col(), fact_type="currency_binding",
                                       value={"currency_column": _ccy_ref()}, owner="user:alice")
    assert verified_currency_binding(db, key) is not None

    fire_due_overlay_expiries(db, now=datetime.now(UTC) + timedelta(days=4000))

    assert verified_currency_binding(db, key) is None   # 2nd gate: status='VERIFIED' no longer holds
    row = db.execute("SELECT status FROM semantic_binding_edge WHERE fact_key = %s", (key,)).fetchone()
    assert row is not None and row[0] == "REVERIFY"     # row kept for audit, stamped non-VERIFIED


# --- 5) re-upload survival + conflicting divergence (VERIFIED WINS) -------------------------------

def test_reupload_reproject_keeps_binding_and_records_conflict_without_overwrite(db):
    _build_entity_graph(db, file_entity="account")
    key, _ = seed_verified_via_command(db, ref=_entity_col(), fact_type="entity_assignment",
                                       value={"entity_id": "customer"}, owner="user:alice")
    assert _node(db)[0] == "customer"

    # A re-upload whose file now declares a DIFFERENT entity ("household"). build_graph WIPES the
    # source's graph_node (entity + the governed columns) — the governed binding is momentarily gone.
    _build_entity_graph(db, file_entity="household")
    assert _node(db)[0] == "household" and _node(db)[4] is None   # wiped: file value, no provenance

    # The build_graph reproject re-applies the VERIFIED binding from the FACT.
    reproject_semantic_bindings(db, source=SOURCE)

    entity, declared, fk, _fev, status = _node(db)
    assert entity == "customer"        # VERIFIED WINS — the re-upload did NOT overwrite the governed value
    assert declared == "household"     # the conflicting file value preserved -> the divergence signal
    assert fk == key and status == "VERIFIED"          # governed binding SURVIVED the re-upload
    assert verified_entity_of(db, SOURCE, ENTITY_OBJ) == "customer"


# --- 6) dependency staling: drop the target -> stale -> demote ------------------------------------

def test_dropping_currency_target_stales_and_demotes_the_edge(db):
    objs = [CatalogObject(object_ref=r, object_kind="column", schema="sales", table=t, column=c,
                          data_type="text", native_oid=None)
            for r, t, c in (("sales.trades.notional", "trades", "notional"),
                            ("sales.trades.ccy", "trades", "ccy"))]
    cat = StubCatalog(objects=objs, catalog_source=SOURCE)
    cat.set_owner(_measure_col(), "user:alice")
    register_catalog_adapter(cat)
    key = fact_key(_measure_col(), "currency_binding")
    seed_verified_via_command(db, ref=_measure_col(), fact_type="currency_binding",
                              value={"currency_column": _ccy_ref()}, owner="user:alice")
    register_catalog_adapter(cat)   # seed swapped in its own default adapter — restore ours
    assert verified_currency_binding(db, key) is not None

    detect_catalog_changes(db, cat, actor=SVC, open_reverify=False)   # snapshot the current objects
    cat._objects = [o for o in objs if o.column != "ccy"]            # DROP the currency target column
    changes = detect_catalog_changes(db, cat, actor=SVC, open_reverify=False)   # -> drop -> STALE
    assert any(ch.kind == "drop" and ch.object_ref == "sales.trades.ccy" for ch in changes)

    assert verified_currency_binding(db, key) is None   # drift-staled fact -> projection demoted
    row = db.execute("SELECT status FROM semantic_binding_edge WHERE fact_key = %s", (key,)).fetchone()
    assert row[0] == "STALE"


# --- 7) replay parity: reset() + full replay == synchronous state --------------------------------

def _snapshot(conn) -> dict:
    """The stable projection state (clock-independent) both drivers must produce identically."""
    node = conn.execute(
        "SELECT entity, declared_entity, entity_fact_key, entity_fact_event_id, entity_status "
        "FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (SOURCE, ENTITY_OBJ)).fetchone()
    edges = conn.execute(
        "SELECT fact_key, catalog_source, kind, from_ref, to_ref, confirmed_event_id, status "
        "FROM semantic_binding_edge ORDER BY fact_key").fetchall()
    return {"node": node, "edges": edges}


def test_reset_and_replay_reproduces_identical_state(db):
    _build_entity_graph(db, file_entity="account")
    seed_verified_via_command(db, ref=_entity_col(), fact_type="entity_assignment",
                              value={"entity_id": "customer"}, owner="user:alice")
    seed_verified_via_command(db, ref=_measure_col(), fact_type="currency_binding",
                              value={"currency_column": _ccy_ref()}, owner="user:alice")
    _drain(db)
    synchronous = _snapshot(db)
    assert synchronous["node"][0] == "customer" and len(synchronous["edges"]) == 1

    # Wipe the read model and rebuild it PURELY from the event stream.
    proj = SemanticBindingProjection()
    proj.rebuild(db)

    assert _snapshot(db) == synchronous   # replay parity: byte-for-byte the synchronous state
