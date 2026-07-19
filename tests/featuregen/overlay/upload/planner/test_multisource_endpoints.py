"""Phase 3C.2b-i-A · Task 4 — GovernedEndpointV1 grain-fact endpoint revalidation (spec §3.1).

An endpoint is GOVERNED by a VERIFIED ``grain`` fact, NOT by advisory ``graph_node.is_grain`` (the
frontier's derivation the Task-1 spike proved must be superseded). Grain facts are seeded through the
REAL governance write path (``propose_fact`` (service) -> ``_confirm_grain`` (platform-admin human)),
exactly as the reuse spike does — never hand-set columns / flags.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload.conftest import _confirm_grain

from featuregen.contracts.envelopes import Command
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.multisource_contracts import GovernedEndpointV1
from featuregen.overlay.upload.planner.multisource_endpoints import governed_endpoint
from featuregen.overlay.upload.upload_catalog import (
    ensure_upload_catalog_adapter,
    table_ref,
)

_NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _seed(db, source, rows_concepts):
    """Seed a catalog's physical graph through the REAL ingest graph builder (never hand-set rows)."""
    rows = [r for r, _ in rows_concepts]
    build_graph(db, source, rows, concepts={content_hash(r): c for r, c in rows_concepts})


def _seed_verified_grain(db, source, table, columns, *, service_actor, human_actor):
    """A VERIFIED ``grain`` fact on the table via the REAL governance write path (the spike pattern):
    ``propose_fact`` (service) opens the platform-admin gate, ``_confirm_grain`` (human) confirms +
    drains the projection so ``resolve_fact`` reads VERIFIED."""
    ref = table_ref(source, table)
    res = propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain",
         "proposed_value": {"columns": columns, "is_unique": True}},
        service_actor, f"propose-grain-{source}-{table}"))
    assert res.accepted, res.denied_reason
    _confirm_grain(db, source, table, columns, actor=human_actor)


@pytest.fixture
def adapter():
    """The sealed-config upload adapter ``resolve_fact`` consults (grain is a data fact)."""
    ensure_upload_catalog_adapter()
    return current_catalog_adapter()


def test_verified_grain_fact_yields_qualified_validated_endpoint(
        db, adapter, service_actor, human_actor):
    _seed(db, "wealth", [
        (CanonicalRow("wealth", "customers", "customer_id", "integer", is_grain=True),
         "customer_id"),
        (CanonicalRow("wealth", "customers", "segment", "varchar"), "segment"),
    ])
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)

    endpoint = governed_endpoint(db, adapter, catalog="wealth",
                                 table_ref="public.customers", now=_NOW)

    assert isinstance(endpoint, GovernedEndpointV1)
    assert endpoint.catalog == "wealth"
    assert endpoint.table_ref == "public.customers"
    # short columns qualified to the table ref + validated against graph_node.column_name
    assert endpoint.grain_key_refs == ("public.customers.customer_id",)
    # keyed on the DETERMINISTIC grain fact_key (ref+type), never a per-event id
    assert endpoint.grain_fact_key == fact_key(table_ref("wealth", "customers"), "grain")


def test_advisory_is_grain_without_verified_fact_yields_none(db, adapter):
    # transaction_id is file-declared is_grain (advisory), but NO governed grain fact was confirmed.
    _seed(db, "core_banking", [
        (CanonicalRow("core_banking", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core_banking", "transactions", "amount", "numeric"), "monetary_flow"),
    ])

    endpoint = governed_endpoint(db, adapter, catalog="core_banking",
                                 table_ref="public.transactions", now=_NOW)

    assert endpoint is None  # fail-closed: advisory is_grain does not govern the endpoint


def test_composite_grain_fact_yields_multi_element_refs(db, adapter, service_actor, human_actor):
    _seed(db, "risk", [
        (CanonicalRow("risk", "positions", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("risk", "positions", "as_of_date", "date", is_grain=True), "as_of_date"),
        (CanonicalRow("risk", "positions", "exposure", "numeric"), "monetary_stock"),
    ])
    _seed_verified_grain(db, "risk", "positions", ["account_id", "as_of_date"],
                         service_actor=service_actor, human_actor=human_actor)

    endpoint = governed_endpoint(db, adapter, catalog="risk",
                                 table_ref="public.positions", now=_NOW)

    assert isinstance(endpoint, GovernedEndpointV1)
    # composite grain -> multi-element grain_key_refs, in fact-column order
    assert endpoint.grain_key_refs == (
        "public.positions.account_id", "public.positions.as_of_date")
    assert endpoint.grain_fact_key == fact_key(table_ref("risk", "positions"), "grain")
