"""E4 — legacy entity_suggestion migration (real DB).

Covers the four brief guarantees:
  1. Legacy 'applied' suggestions are KEPT readable but DEMOTED to `legacy_file_declared`; a governed
     VERIFIED entity_assignment WINS the effective `graph_node.entity` (governed > legacy).
  2. The NEW apply routes through a governed entity_assignment fact (E1 propose→confirm, owner-or-admin
     four-eyes) — not the legacy status='applied' UPDATE — and E3 projects the governed entity.
  3. The one-time backfill PROPOSES DRAFT facts only (never auto-verifies) and is idempotent.
  4. A re-ingest / build_graph reapply of a legacy 'applied' tag does NOT overwrite a governed
     VERIFIED entity_assignment (governed wins).
"""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity
from tests.featuregen.overlay._helpers import seed_verified_via_command

from featuregen.contracts import Command
from featuregen.overlay.confirmation_commands import confirm_fact
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.entity import (
    apply_entity_suggestion,
    backfill_legacy_entity_assignments,
    effective_entity,
    list_entity_suggestions,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.semantic_bindings.projection import reproject_semantic_bindings

SOURCE = "deposits"
CUST_OBJ = "public.accounts.cust_ref"


def _ref(column: str = "cust_ref") -> CatalogObjectRef:
    # build_graph declares no schema for these rows -> schema_name NULL -> the fact ref schema is the
    # 'public' graph scope (the fact_key + E3 projection key on table+column, schema-independent).
    return CatalogObjectRef(SOURCE, "column", "public", "accounts", column)


def _seed_applied(db, *, column: str = "cust_ref", entity: str, obj: str | None = None) -> str:
    obj = obj or f"public.accounts.{column}"
    db.execute(
        "INSERT INTO entity_suggestion (catalog_source, object_ref, table_name, column_name, "
        "suggested_entity, status) VALUES (%s, %s, 'accounts', %s, %s, 'applied')",
        (SOURCE, obj, column, entity))
    return obj


# 1) legacy demoted to legacy_file_declared; governed VERIFIED entity_assignment WINS ---------------

def test_legacy_applied_reads_legacy_file_declared_and_governed_wins(db):
    rows = [CanonicalRow(SOURCE, "accounts", "cust_ref", "integer")]
    build_graph(db, SOURCE, rows)
    _seed_applied(db, entity="Customer")          # a pre-existing LEGACY human-confirmed tag
    build_graph(db, SOURCE, rows)                 # build_graph re-applies it (legacy_file_declared)

    # KEPT readable, but labelled non-governed both on the effective read AND the suggestion list.
    read = effective_entity(db, SOURCE, CUST_OBJ)
    assert read.entity == "Customer" and read.authority == "legacy_file_declared"
    applied = list_entity_suggestions(db, SOURCE, status="applied")
    assert applied and applied[0].authority == "legacy_file_declared"

    # A governed VERIFIED entity_assignment for the SAME column WINS the effective entity.
    seed_verified_via_command(db, ref=_ref(), fact_type="entity_assignment",
                              value={"entity_id": "customer"}, owner="user:owner")
    governed = effective_entity(db, SOURCE, CUST_OBJ)
    assert governed.entity == "customer" and governed.authority == "governed"   # governed > legacy


# 2) new apply is governed (E1 propose→confirm four-eyes) and E3 projects ---------------------------

def test_new_apply_proposes_then_confirms_governed_entity(db, catalog):
    build_graph(db, SOURCE, [CanonicalRow(SOURCE, "accounts", "cust_ref", "integer")])
    db.execute(
        "INSERT INTO entity_suggestion (catalog_source, object_ref, table_name, column_name, "
        "suggested_entity, status) VALUES (%s, %s, 'accounts', 'cust_ref', 'Customer', 'pending')",
        (SOURCE, CUST_OBJ))
    catalog.set_owner(_ref(), "user:owner")

    # apply -> PROPOSES a governed DRAFT (proposer = alice); the graph is NOT written yet.
    proposer = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
    res = apply_entity_suggestion(db, SOURCE, CUST_OBJ, actor=proposer)
    assert res.found and res.accepted and res.fact_key and res.proposed_event_id
    assert fold_overlay_state(load_fact(db, res.fact_key)).status == "DRAFT"
    assert effective_entity(db, SOURCE, CUST_OBJ).authority in (None, "file_declared")

    # a DISTINCT authorized owner confirms (four-eyes) -> VERIFIED -> E3 sync projection.
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    confirmed = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": _ref(), "fact_type": "entity_assignment", "target_event_id": res.proposed_event_id},
        owner, "ik-e4-confirm"))
    assert confirmed.accepted, confirmed.denied_reason

    governed = effective_entity(db, SOURCE, CUST_OBJ)
    assert governed.entity == "customer" and governed.authority == "governed"   # E3 projection


def test_apply_is_four_eyes_proposer_cannot_confirm(db, catalog):
    build_graph(db, SOURCE, [CanonicalRow(SOURCE, "accounts", "cust_ref", "integer")])
    db.execute(
        "INSERT INTO entity_suggestion (catalog_source, object_ref, table_name, column_name, "
        "suggested_entity, status) VALUES (%s, %s, 'accounts', 'cust_ref', 'Customer', 'pending')",
        (SOURCE, CUST_OBJ))
    catalog.set_owner(_ref(), "user:alice")
    alice = mint_test_identity(subject="user:alice", role_claims=("data_owner", "platform-admin"))
    res = apply_entity_suggestion(db, SOURCE, CUST_OBJ, actor=alice)
    assert res.accepted
    # the SAME principal may not confirm what she proposed (four-eyes), even as owner+admin.
    denied = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": _ref(), "fact_type": "entity_assignment", "target_event_id": res.proposed_event_id},
        alice, "ik-selfconfirm"))
    assert not denied.accepted and "four-eyes" in (denied.denied_reason or "")


# 3) backfill PROPOSES DRAFT facts only (never verifies) + idempotent ------------------------------

def test_backfill_proposes_drafts_only_and_is_idempotent(db, catalog):
    build_graph(db, SOURCE, [
        CanonicalRow(SOURCE, "accounts", "cust_ref", "integer"),
        CanonicalRow(SOURCE, "accounts", "acct_ref", "integer"),
        CanonicalRow(SOURCE, "accounts", "weird_ref", "integer")])
    _seed_applied(db, column="cust_ref", entity="Customer")
    _seed_applied(db, column="acct_ref", entity="Account")
    _seed_applied(db, column="weird_ref", entity="Frobnicate")   # not a known governed entity

    svc = mint_test_service_identity(subject="service:e4-migrate", role_claims=("overlay",),
                                     attestation="a")
    result = backfill_legacy_entity_assignments(db, actor=svc)
    assert result.proposed == 2                       # the two known-entity legacy tags
    assert result.skipped_unknown_entity == 1         # 'Frobnicate' -> needs human correction
    assert result.skipped_existing == 0

    # every proposed fact is a DRAFT (never auto-verified); the graph is untouched.
    for key in result.proposed_fact_keys:
        assert fold_overlay_state(load_fact(db, key)).status == "DRAFT"
    assert db.execute(
        "SELECT entity FROM graph_node WHERE catalog_source=%s AND object_ref=%s",
        (SOURCE, CUST_OBJ)).fetchone()[0] is None
    # the unknown-entity row never got a fact.
    assert load_fact(db, fact_key(_ref("weird_ref"), "entity_assignment")) == []

    # idempotent: a re-run proposes nothing new (each legacy row already has a fact).
    again = backfill_legacy_entity_assignments(db, actor=svc)
    assert again.proposed == 0 and again.skipped_existing == 2


# 4) a governed VERIFIED entity_assignment is NOT overwritten by a legacy build_graph reapply -------

def test_reingest_legacy_reapply_never_overwrites_governed(db):
    rows = [CanonicalRow(SOURCE, "accounts", "cust_ref", "integer")]
    build_graph(db, SOURCE, rows)
    seed_verified_via_command(db, ref=_ref(), fact_type="entity_assignment",
                              value={"entity_id": "customer"}, owner="user:owner")
    assert effective_entity(db, SOURCE, CUST_OBJ).entity == "customer"

    # A CONFLICTING legacy applied tag for the same column exists too.
    _seed_applied(db, entity="Household")

    # Re-ingest: build_graph WIPES graph_node (governed momentarily gone) + re-applies the legacy tag;
    # then the E3 reproject re-applies the governed binding on top (mirrors the ingest ordering).
    build_graph(db, SOURCE, rows)
    reproject_semantic_bindings(db, source=SOURCE)

    read = effective_entity(db, SOURCE, CUST_OBJ)
    assert read.entity == "customer" and read.authority == "governed"   # governed WON over legacy
    # the conflicting legacy value is PRESERVED as declared context (divergence signal), never lost.
    assert db.execute(
        "SELECT declared_entity FROM graph_node WHERE catalog_source=%s AND object_ref=%s",
        (SOURCE, CUST_OBJ)).fetchone()[0] == "Household"
