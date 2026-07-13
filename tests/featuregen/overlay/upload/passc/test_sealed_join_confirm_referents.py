"""Task 0 — sealed-runtime referent validation for the Pass C dual-join confirm (SP-1.5).

The deployed API SEALS the overlay config at startup (api/app.py `register_overlay_config`),
which arms the SP-1.5 referent gate at the dual-join SECOND confirm unconditionally. On the
upload confirm path the only registered adapter is the connectionless sentinel
`UploadContextAdapter` (catalog_source='upload:context', fingerprint()=={}), so `referent_gap`
fail-closed EVERY real join ("referent catalog_source 'src' is not served by this adapter") —
no join could reach VERIFIED in a sealed deployment. The fix routes referent EXISTENCE to the
authoritative structural source per catalog mode: `graph_node` (the built graph) under the
sentinel, the adapter fingerprint otherwise (`join_referents.check_referents_exist`).

These tests SEAL the config explicitly (the overlay conftest clears it around every test — the
exact reason Phase 3A missed the hole) and seed `graph_node` rows the way `build_graph` writes
them (public-flattened `object_ref` = 'public.table[.column]', kind 'table'|'column').
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.passc.conftest import SERVICE_ACTOR, _drain, _join_value
from tests.featuregen.overlay.upload.test_join_governance import _seed_join_with_evidence

from featuregen.contracts.envelopes import Command
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.config import (
    _clear_overlay_config,
    overlay_config_from_env,
    register_overlay_config,
)
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.join_referents import (
    check_referents_exist,
    graph_referent_gap,
)
from featuregen.overlay.upload.upload_catalog import UploadCatalog

# ── Sealed deployment posture ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def sealed_config():
    """Seal the process-wide OverlayConfig — the DEPLOYED posture (api/app.py registers one at
    startup). Built from an EMPTY env mapping so ambient OVERLAY_* vars cannot skew the test. The
    overlay conftest's autouse `_reset_overlay_config` clears the global around every test (which is
    why the Phase 3A suites never saw the sealed-runtime hole); clear again on teardown anyway."""
    register_overlay_config(overlay_config_from_env({}))
    yield
    _clear_overlay_config()


# ── Graph seeding (mirrors build_graph's inserts: public-flattened object_ref) ───────────────────

_FROM_COL = "public.transactions.cif_id"
_TO_COL = "public.customers.cif_id"


def _seed_graph_nodes(conn, source: str = "src") -> None:
    """Insert the graph_node rows `build_graph` would write for the seeded join's two endpoints:
    a 'table' node per table (object_ref 'public.<table>') and a 'column' node per endpoint
    (object_ref 'public.<table>.<column>'). search_doc is nullable — minimal columns suffice."""
    for table in ("transactions", "customers"):
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name) "
            "VALUES (%s, %s, 'table', %s, NULL) ON CONFLICT DO NOTHING",
            (source, f"public.{table}", table))
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type) VALUES (%s, %s, 'column', %s, 'cif_id', 'text') ON CONFLICT DO NOTHING",
            (source, f"public.{table}.cif_id", table))


def _delete_graph_column(conn, object_ref: str, source: str = "src") -> None:
    conn.execute(
        "DELETE FROM graph_node WHERE catalog_source=%s AND object_ref=%s", (source, object_ref))


# ── Command helpers (non-asserting: deny paths return the CommandResult) ─────────────────────────


def _confirm(conn, ref, key, actor):
    target = _cas_target(fold_overlay_state(load_fact(conn, key)))
    return confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "use_case": None, "target_event_id": target},
        actor, f"confirm-{actor.subject}-{target}"))


def _status(conn, key) -> str:
    return fold_overlay_state(load_fact(conn, key)).status


# ── 1. THE production hole, closed: sealed config + sentinel adapter -> VERIFIED ─────────────────


def test_sealed_config_dual_join_reaches_verified(
    passc_conn, sealed_config, human_admin_1, human_admin_2
):
    """Sealed config + UploadContextAdapter + graph_node rows for BOTH endpoints: admin1 ->
    PARTIALLY_CONFIRMED, distinct admin2 -> VERIFIED. Pre-fix this denies admin2 with
    "referent catalog_source 'src' is not served by this adapter" (the sentinel's empty
    fingerprint serves no real source) — the sealed-runtime bug."""
    ref, key = _seed_join_with_evidence(passc_conn)
    _seed_graph_nodes(passc_conn)

    first = _confirm(passc_conn, ref, key, human_admin_1)
    assert first.accepted, first.denied_reason
    assert _status(passc_conn, key) == "PARTIALLY_CONFIRMED"

    second = _confirm(passc_conn, ref, key, human_admin_2)
    assert second.accepted, second.denied_reason
    assert _status(passc_conn, key) == "VERIFIED"


# ── 2/3. A referent that VANISHES between the two confirms still fail-closes ─────────────────────


@pytest.mark.parametrize("missing_ref", [_FROM_COL, _TO_COL], ids=["from_column", "to_column"])
def test_vanished_endpoint_denies_second_confirm(
    passc_conn, sealed_config, human_admin_1, human_admin_2, missing_ref
):
    """Delete ONE endpoint's column node after admin1's partial: admin2 is denied, the fact stays
    PARTIALLY_CONFIRMED, and the deny reason names the exact missing endpoint (source + full
    table.column path — the guard is KEPT, only its structural source changed)."""
    ref, key = _seed_join_with_evidence(passc_conn)
    _seed_graph_nodes(passc_conn)

    first = _confirm(passc_conn, ref, key, human_admin_1)
    assert first.accepted, first.denied_reason

    _delete_graph_column(passc_conn, missing_ref)
    second = _confirm(passc_conn, ref, key, human_admin_2)
    assert not second.accepted
    assert f"join referent missing from graph: src.{missing_ref}" in second.denied_reason
    assert _status(passc_conn, key) == "PARTIALLY_CONFIRMED"


# ── 4. No false pass on a same-named column under a DIFFERENT source ─────────────────────────────


def test_same_named_nodes_under_other_source_do_not_pass(
    passc_conn, sealed_config, human_admin_1, human_admin_2
):
    """graph_node has the tables/columns ONLY under a different catalog_source: the existence
    check must NOT match on name alone — the second confirm is denied and the direct graph check
    reports a gap."""
    ref, key = _seed_join_with_evidence(passc_conn)
    _seed_graph_nodes(passc_conn, source="other-src")     # same names, wrong source

    gap = graph_referent_gap(passc_conn, ref, "approved_join", _join_value(ref))
    assert gap is not None and gap.startswith("join referent missing from graph: src.")

    first = _confirm(passc_conn, ref, key, human_admin_1)
    assert first.accepted, first.denied_reason
    second = _confirm(passc_conn, ref, key, human_admin_2)
    assert not second.accepted
    assert "join referent missing from graph: src." in second.denied_reason
    assert _status(passc_conn, key) == "PARTIALLY_CONFIRMED"


# ── 5. A NON-sentinel adapter still takes the ORIGINAL referent_gap path (delegation) ────────────


class _FingerprintAdapter:
    """A minimal real-source adapter: a genuine catalog_source + a fingerprint keyed on the display
    object_refs it serves (the exact shape `referent_gap` checks against). NOT the sentinel."""

    def __init__(self, catalog_source: str, present: set[str]) -> None:
        self.catalog_source = catalog_source
        self._present = present

    def fingerprint(self):
        return dict.fromkeys(self._present, object())

    def list_objects(self):
        return []

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def owner_of(self, ref):
        return None


def _bare_ref():
    return ApprovedJoinRef(
        from_ref=CatalogObjectRef("src", "column", "public", "transactions", "cif_id"),
        to_ref=CatalogObjectRef("src", "column", "public", "customers", "cif_id"),
        column_pairs=(ColumnPair("cif_id", "cif_id"),),
        cardinality="N:1")


_ALL_REFERENTS = {"public.transactions", "public.customers", _FROM_COL, _TO_COL}


def test_normal_adapter_delegates_to_referent_gap(db):
    """A non-sentinel adapter with the referents IN its fingerprint passes — with an EMPTY graph,
    so a graph fallback would have denied: the dispatcher used the adapter, byte-for-byte the
    existing behavior."""
    ref = _bare_ref()
    adapter = _FingerprintAdapter("src", _ALL_REFERENTS)
    assert check_referents_exist(db, adapter, ref, "approved_join", _join_value(ref)) is None


def test_normal_adapter_empty_fingerprint_keeps_original_denial(db):
    """A non-sentinel adapter with an EMPTY fingerprint yields referent_gap's ORIGINAL denial —
    with the graph fully seeded, so the graph path would have PASSED: proof the dispatcher
    delegates rather than falling through to graph_node."""
    ref = _bare_ref()
    _seed_graph_nodes(db)                                  # graph says yes; adapter must still win
    gap = check_referents_exist(
        db, _FingerprintAdapter("src", set()), ref, "approved_join", _join_value(ref))
    assert gap is not None and gap.startswith("referent no longer in catalog:")


# ── 6/7. The MISSED third gate (whole-branch review): the drift-STALE first-partial re-confirm ────


def _verify_join(conn, ref, key, admin1, admin2) -> None:
    """Drive the seeded join to VERIFIED under the sealed config (graph_node must be seeded)."""
    first = _confirm(conn, ref, key, admin1)
    assert first.accepted, first.denied_reason
    second = _confirm(conn, ref, key, admin2)
    assert second.accepted, second.denied_reason
    assert _status(conn, key) == "VERIFIED"


def _drift_stale_join(conn, key) -> None:
    """STALE the VERIFIED join EXACTLY the way production does (ingest.py's drift path):
    `detect_catalog_changes` over a re-upload that TYPE-CHANGES the from-endpoint but keeps every
    endpoint PRESENT. The dependency index the drift scan reads is an overlay-projection read
    model, so drain first (ingest drains before the diff, same reason)."""
    _drain(conn)
    rows_v1 = [CanonicalRow("src", "transactions", "cif_id", "text"),
               CanonicalRow("src", "customers", "cif_id", "text", is_grain=True)]
    detect_catalog_changes(conn, UploadCatalog("src", rows_v1), actor=SERVICE_ACTOR,
                           open_reverify=False)            # establish the snapshot (adds only)
    rows_v2 = [CanonicalRow("src", "transactions", "cif_id", "varchar"),
               CanonicalRow("src", "customers", "cif_id", "text", is_grain=True)]
    changes = detect_catalog_changes(conn, UploadCatalog("src", rows_v2), actor=SERVICE_ACTOR,
                                     open_reverify=False)  # type change -> dependents STALE
    assert any(c.kind == "type_change" and c.object_ref == _FROM_COL for c in changes)
    assert _status(conn, key) == "STALE"


def test_drift_staled_join_reconfirm_reaches_partially_confirmed(
    passc_conn, sealed_config, human_admin_1, human_admin_2
):
    """THE production hole at join_confirmation.py:94 (whole-branch review, FIX 1): a VERIFIED
    dual join drift-STALEs (type change — every endpoint STILL in graph_node), then a
    platform-admin re-confirms. The `state.status == "STALE"` first-partial gate must route
    existence through `check_referents_exist` like the other two sentinel-affected sites; the raw
    `referent_gap(sentinel, ...)` denies with "referent catalog_source 'src' is not served by this
    adapter" — the drift-STALEd join could NEVER re-verify in a sealed deployment."""
    ref, key = _seed_join_with_evidence(passc_conn)
    _seed_graph_nodes(passc_conn)
    _verify_join(passc_conn, ref, key, human_admin_1, human_admin_2)
    _drift_stale_join(passc_conn, key)

    res = _confirm(passc_conn, ref, key, human_admin_1)    # a STALE cycle starts with no partials
    assert res.accepted, res.denied_reason
    assert _status(passc_conn, key) == "PARTIALLY_CONFIRMED"


def test_drift_staled_join_reconfirm_still_denied_when_endpoint_missing(
    passc_conn, sealed_config, human_admin_1, human_admin_2
):
    """The guard is PRESERVED, only its structural source changed: the same drift-STALEd join
    whose from-endpoint is MISSING from graph_node is still denied at the first re-confirm, with
    the graph-based reason naming the exact missing endpoint."""
    ref, key = _seed_join_with_evidence(passc_conn)
    _seed_graph_nodes(passc_conn)
    _verify_join(passc_conn, ref, key, human_admin_1, human_admin_2)
    _drift_stale_join(passc_conn, key)
    _delete_graph_column(passc_conn, _FROM_COL)

    res = _confirm(passc_conn, ref, key, human_admin_1)
    assert not res.accepted
    assert "stale re-confirm blocked" in res.denied_reason
    assert f"join referent missing from graph: src.{_FROM_COL}" in res.denied_reason
    assert _status(passc_conn, key) == "STALE"
