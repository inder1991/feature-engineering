"""Phase 3C.2b-i-B · Task 3 — server-side trust derivation round-trips (real ``db`` connection).

Proves the authority guarantee is STRUCTURAL: ``derive_request_context`` derives the three trust
inputs (authorized ``CatalogScopeV1`` + confirmed non-null ``target_entity`` + the server-built
``object_ref -> authorized-catalog`` identity map) SERVER-SIDE from the authenticated roles + the
durable confirmed scope — a caller-claimed catalog is un-injectable because the function exposes no
caller-catalog / caller-target-entity parameter.

Seeds through the SAME real paths the sibling suites use: ``build_graph`` + an
``overlay_drift_watermark`` row for the read-scope-gated ``graph_node`` columns (mirrors
``test_scope.py``), and ``record_confirmed_scope`` for the durable confirmed scope (mirrors
``test_scope_records.py``). No raw-INSERT stands in for a real command.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.scope_records import record_confirmed_scope
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.b_scope import (
    RequestContextV1,
    TrustDerivationError,
    derive_request_context,
)
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

_NOW = datetime(2026, 7, 21, tzinfo=UTC)


def _seed_catalog(db, source: str, rows: list[CanonicalRow], *, watermark: bool = True) -> None:
    """Real ``graph_node`` seeding (``build_graph``) + a drift watermark, mirroring ``test_scope``.
    A catalog with no watermark is authorized-OUT (``resolve_catalog_scope`` omits it), so it stands
    in for an UNauthorized catalog whose columns must never reach the identity map."""
    build_graph(db, source, rows)
    if watermark:
        db.execute(
            "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
            "head_seq) VALUES (%s, %s, 'r', 5) ON CONFLICT (catalog_source) DO UPDATE SET "
            "last_completed_at = %s",
            (source, _NOW, _NOW))


def _persist_scope(db, run_id: str, target_entity: str | None) -> None:
    """Persist a durable confirmed scope for ``run_id`` carrying ``target_entity`` via the REAL
    ``record_confirmed_scope`` writer. Unscoped keeps it minimal (no use-case children); the
    confirmed dimensions persist regardless, so ``scope_for_run`` rebuilds the ``target_entity``."""
    scope = ConfirmedScope(primary=None, unscoped=True, target_entity=target_entity)
    record_confirmed_scope(
        db, intent_id=f"intent_{run_id}", generation_run_id=run_id, recognition_id=None,
        scope=scope, use_case_origins={}, confirmation_source="user_confirmed", confirmed_by="ds1")


# ── Test 1: server roster wins — a caller-claimed catalog is un-injectable ────────────────────────
def test_server_roster_wins_caller_claimed_catalog_ignored(db) -> None:
    _seed_catalog(db, "y", [CanonicalRow("y", "t", "amt", "numeric")])
    # An UNauthorized catalog (no watermark -> omitted from the scope): its column must NOT appear,
    # because the identity map is built ONLY over scope.authorized_catalog_sources.
    _seed_catalog(db, "z", [CanonicalRow("z", "t", "secret_amt", "numeric")], watermark=False)
    _persist_scope(db, "run_1", "customer")

    ctx = derive_request_context(db, roles=(), generation_run_id="run_1", now=_NOW)

    assert isinstance(ctx, RequestContextV1)
    # The catalog for a bare operand came from the SERVER scan, and the function exposes no caller
    # catalog parameter — so the roster is un-injectable.
    assert "public.t.amt" in ctx.identity_map.known
    assert ctx.identity_map.sources_for("public.t.amt") == ("y",)
    # The unauthorized (no-watermark) catalog is omitted from the scope AND its column absent.
    assert "z" not in ctx.scope.authorized_catalog_sources
    assert "public.t.secret_amt" not in ctx.identity_map.known
    # An unknown operand resolves to no catalog (never guessed).
    assert ctx.identity_map.sources_for("public.nope.nope") == ()
    assert ctx.target_entity == "customer"


# ── Test 2: overbroad scope rejected — the sensitivity gate is load-bearing ───────────────────────
def test_overbroad_scope_rejected_role_gate_is_load_bearing(db) -> None:
    # A catalog whose ONLY column is restricted-sensitivity: with no restricted_reader it has no
    # read-scope-visible column, so resolve_catalog_scope never authorizes it.
    _seed_catalog(db, "restricted_cat",
                  [CanonicalRow("restricted_cat", "t", "ssn", "varchar", sensitivity="restricted")])
    _persist_scope(db, "run_r", "customer")

    # (a) roles LACK restricted_reader -> the catalog is gated out and its column is absent.
    denied = derive_request_context(db, roles=(), generation_run_id="run_r", now=_NOW)
    assert "restricted_cat" not in denied.scope.authorized_catalog_sources
    assert "public.t.ssn" not in denied.identity_map.known

    # (b) the SAME confirmed scope with restricted_reader present -> now authorized and mapped,
    #     proving the gate is load-bearing (the only thing that changed is the role).
    reader = derive_request_context(
        db, roles=("restricted_reader",), generation_run_id="run_r", now=_NOW)
    assert "restricted_cat" in reader.scope.authorized_catalog_sources
    assert "public.t.ssn" in reader.identity_map.known
    assert reader.identity_map.sources_for("public.t.ssn") == ("restricted_cat",)


# ── Test 3: a non-null, in-vocabulary confirmed target_entity is required ──────────────────────────
def test_missing_confirmed_scope_rejects_fail_closed(db) -> None:
    with pytest.raises(TrustDerivationError) as exc:
        derive_request_context(db, roles=(), generation_run_id="no_such_run", now=_NOW)
    assert exc.value.reason == "confirmed_scope_missing"


def test_null_target_entity_rejects(db) -> None:
    _persist_scope(db, "run_null", None)   # confirmed scope present, but target_entity unconfirmed
    with pytest.raises(TrustDerivationError) as exc:
        derive_request_context(db, roles=(), generation_run_id="run_null", now=_NOW)
    assert exc.value.reason == "target_entity_unconfirmed"


def test_out_of_vocabulary_target_entity_rejects(db) -> None:
    _persist_scope(db, "run_bogus", "not_a_governed_entity")
    with pytest.raises(TrustDerivationError) as exc:
        derive_request_context(db, roles=(), generation_run_id="run_bogus", now=_NOW)
    assert exc.value.reason == "target_entity_unconfirmed"


def test_valid_confirmed_target_entity_succeeds(db) -> None:
    _seed_catalog(db, "y", [CanonicalRow("y", "t", "amt", "numeric")])
    _persist_scope(db, "run_ok", "customer")
    ctx = derive_request_context(db, roles=(), generation_run_id="run_ok", now=_NOW)
    assert ctx.target_entity == "customer"


# ── Test 4: the identity map is deterministic (sorted, reproducible) ───────────────────────────────
def test_identity_map_is_deterministic(db) -> None:
    _seed_catalog(db, "y", [CanonicalRow("y", "t", "amt", "numeric"),
                            CanonicalRow("y", "t", "id", "integer", is_grain=True)])
    _seed_catalog(db, "x", [CanonicalRow("x", "t2", "bal", "numeric")])
    _persist_scope(db, "run_det", "customer")

    a = derive_request_context(db, roles=(), generation_run_id="run_det", now=_NOW)
    b = derive_request_context(db, roles=(), generation_run_id="run_det", now=_NOW)

    assert a.identity_map.entries == b.identity_map.entries
    refs = [e.object_ref for e in a.identity_map.entries]
    assert refs == sorted(refs)                       # deterministically sorted by object_ref
    assert set(refs) == {"public.t.amt", "public.t.id", "public.t2.bal"}
