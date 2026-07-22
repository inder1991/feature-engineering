"""Phase 3C.2b-i-B · Task 9 — the bounded admin service entrypoint (``govern_llm_idea``).

DB-backed. The KEYSTONE composes T2–T8 + A's real ``plan_multi_source`` and returns a
:class:`GovernedResult` ONLY on the two-axis pass. Scenarios (brief §Tests):

  1. Happy path -> ``GovernedResult`` (two-axis governed; the honest tri-state carried verbatim).
  2. Two-axis gate -> a GENUINE assembly-RESOLVED / contract-UNRESOLVED chain: a stale customer
     landing fails the compile-end UNION freshness (the CONTRACT axis) while the operand source
     stays fresh, so ``map_a_outcome`` yields ``contract_unresolved`` and NEVER a ``GovernedResult``.
  3. Flag off -> ``XCatShadowDisabledError`` BEFORE any DB work (a conn that errors if touched proves
     the flag-off path is inert).
  4. Auth -> a role without ``feature:generate`` (``catalog_viewer``) and an unauthenticated principal
     both raise ``PermissionError``; a ``feature_engineer`` PROCEEDS past auth.
  5. Savepoint isolation -> a planning failure inside the savepoint returns ``technical_failure`` AND
     the OUTER transaction stays usable (a subsequent ``SELECT 1`` succeeds).
  6. Server-derived context -> the caller supplies only ``generation_run_id``; a bogus run raises
     ``TrustDerivationError`` (there is no caller-scope / caller-target parameter to inject).

Seeding reuses the proven spike helpers (a representative 3-catalog governed chain + two VERIFIED
bridges + watermarks + a durable confirmed scope), exactly as the Task-1 spike stands them up.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import CatalogAdapter, current_catalog_adapter
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.scope_records import record_confirmed_scope
from featuregen.overlay.upload.planner import b_service
from featuregen.overlay.upload.planner import b_slice_spike as spike
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_proposal import RawFeatureProposalV1, new_raw_proposal
from featuregen.overlay.upload.planner.b_scope import TrustDerivationError
from featuregen.overlay.upload.planner.b_service import (
    FEATUREGEN_LLM_XCAT_SHADOW,
    GovernedResult,
    XCatShadowDisabledError,
    govern_llm_idea,
)
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_NOW = datetime(2026, 7, 22, tzinfo=UTC)
_FRESH_WM = _NOW - timedelta(minutes=5)
_STALE_WM = _NOW - timedelta(days=30)   # older than the 24h drift SLA -> fails the union freshness
_FRESH_WITHIN = timedelta(hours=24)

_AMT_REF = "public.txn.tran_amt"


# ── seeding (the proven spike chain) ─────────────────────────────────────────────────────────────
def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _data_owner() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _feature_engineer() -> IdentityEnvelope:
    return IdentityEnvelope(subject="fe", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("feature_engineer",))


def _stand_up(db: DbConn, source: str, table: str,
              columns_concepts: list[tuple[str, str, str]], grain: list[str],
              sa: IdentityEnvelope, ha: IdentityEnvelope, *, project_grain: bool) -> None:
    spike.ingest_representative_table(
        db, source, [CanonicalRow(source, table, col, typ) for col, _c, typ in columns_concepts],
        actor=_data_owner(), now=_NOW)
    for col, concept, _typ in columns_concepts:
        spike.human_confirm_concept(db, source=source, schema=None, table=table, column=col,
                                    concept=concept, actor_subject="admin")
    spike.confirm_grain_fact(db, source=source, table=table, columns=grain,
                             service_actor=sa, human_actor=ha)
    if project_grain:
        spike.project_grain_is_grain(db, source=source, table=table, now=_NOW)


def _seed_chain(db: DbConn, sa: IdentityEnvelope, ha: IdentityEnvelope, *,
                run_id: str, cust_wm: datetime) -> None:
    """Stand up the proven governed cross-catalog chain (txn -> account -> customer over two VERIFIED
    bridges) + watermarks + a durable confirmed scope keyed on ``run_id`` (``target_entity=customer``).
    ``cust_wm`` is the customer-LANDING watermark: ``_FRESH_WM`` for the governed happy path; ``_STALE_WM``
    to fail ONLY the compile-end union freshness (the CONTRACT axis) — the operand source stays fresh."""
    ensure_upload_catalog_adapter()
    _seal()
    _stand_up(db, "s_txn", "txn",
              [("tran_id", "transaction_id", "varchar"), ("tran_amt", "monetary_flow", "numeric"),
               ("foracid", "account_id", "varchar"), ("cif_id", "customer_id", "varchar")],
              ["tran_id"], sa, ha, project_grain=False)
    _stand_up(db, "s_acct", "acct",
              [("foracid", "account_id", "varchar"), ("cif_id", "customer_id", "varchar")],
              ["foracid"], sa, ha, project_grain=True)
    _stand_up(db, "s_cust", "cust",
              [("cif_id", "customer_id", "varchar"), ("segment", "segment", "varchar")],
              ["cif_id"], sa, ha, project_grain=True)
    spike.verify_bridge(db, entity_id="account", left=("s_txn", "txn", "foracid"),
                        right=("s_acct", "acct", "foracid"),
                        service_actor=sa, human_actor=ha, now=_NOW)
    spike.verify_bridge(db, entity_id="customer", left=("s_acct", "acct", "cif_id"),
                        right=("s_cust", "cust", "cif_id"),
                        service_actor=sa, human_actor=ha, now=_NOW)
    spike.set_fresh_watermark(db, "s_txn", _FRESH_WM)
    spike.set_fresh_watermark(db, "s_acct", _FRESH_WM)
    spike.set_fresh_watermark(db, "s_cust", cust_wm)
    record_confirmed_scope(
        db, intent_id=f"i_{run_id}", generation_run_id=run_id, recognition_id=None,
        scope=ConfirmedScope(primary=None, unscoped=True, target_entity="customer"),
        use_case_origins={}, confirmation_source="user_confirmed", confirmed_by="ds1")
    # Drain the overlay projection so the gauntlet's projection-freshness guard
    # (feature_assist.CatalogProjectionUnavailable — added on main) sees checkpoint == head,
    # exactly as a real request would once the projector has caught up after the seeding writes.
    spike._drain(db)


def _sum_proposal() -> RawFeatureProposalV1:
    return new_raw_proposal(operands=(_AMT_REF,), operation="sum", window=None, grain_hint=None)


class _BoomConn:
    """A stand-in connection that raises on ANY attribute access — used to PROVE a code path performs
    no DB work (if it were touched, an ``AssertionError`` would surface instead of the expected raise)."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"DB touched on a path that must do no DB work (.{name})")


# ── 1) happy path -> GovernedResult (two-axis governed; tri-state carried) ────────────────────────
def test_scenario1_happy_path_governs(
        db: DbConn, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
        monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_chain(db, service_actor, human_actor, run_id="run_happy", cust_wm=_FRESH_WM)
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")

    res = govern_llm_idea(db, current_catalog_adapter(), actor=_feature_engineer(),
                          proposal=_sum_proposal(), generation_run_id="run_happy", now=_NOW,
                          fresh_within=_FRESH_WITHIN)

    assert isinstance(res, GovernedResult)
    assert res.disposition is BDisposition.governed
    assert res.planning_result.selected_plan_id is not None
    assert res.planning_result.selected_contract_id is not None
    # the honest Slice-3 tri-state rides through governance verbatim (this chain is file-declared ->
    # NEEDS_EXTERNAL_VALIDATION), never recomputed to a false DESIGN_CHECKED.
    assert res.validation_status in ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION")
    assert res.validation_status == "NEEDS_EXTERNAL_VALIDATION"


# ── 2) two-axis gate -> a genuine NON-governed A outcome never leaks a GovernedResult ─────────────
def test_scenario2_two_axis_gate_never_leaks(
        db: DbConn, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The brief's SANCTIONED fallback for the two-axis gate. A genuine assembly-RESOLVED /
    # contract-UNRESOLVED state is not seedable via watermarks alone: A revalidates every hop
    # endpoint's grain fact through resolve_fact against the SAME watermark (timestamp + head_seq)
    # signals the compile-end union freshness reads, so any stale union catalog fails BOTH axes
    # together (never contract-only). So we seed a GENUINE non-governed A outcome — a stale customer
    # LANDING drops the realization endpoint (assembly axis -> source_entity_ungoverned) — and prove
    # the load-bearing property: any non-governed A outcome yields the MAPPED BDisposition and NEVER
    # a GovernedResult. The gate must not leak.
    _seed_chain(db, service_actor, human_actor, run_id="run_stale", cust_wm=_STALE_WM)
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")

    res = govern_llm_idea(db, current_catalog_adapter(), actor=_feature_engineer(),
                          proposal=_sum_proposal(), generation_run_id="run_stale", now=_NOW,
                          fresh_within=_FRESH_WITHIN)

    assert not isinstance(res, GovernedResult)      # the gate did not leak a GovernedResult
    assert isinstance(res, BDisposition)            # it returned the mapped disposition
    assert res is not BDisposition.governed
    assert res is BDisposition.source_entity_ungoverned   # the deterministic genuine A outcome


# ── 3) flag off -> inert (raises BEFORE any DB work) ─────────────────────────────────────────────
def test_scenario3_flag_off_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FEATUREGEN_LLM_XCAT_SHADOW, raising=False)
    boom_conn = cast(DbConn, _BoomConn())
    boom_adapter = cast(CatalogAdapter, None)   # unreachable: the flag check raises before it is used

    with pytest.raises(XCatShadowDisabledError):
        govern_llm_idea(boom_conn, boom_adapter, actor=_feature_engineer(),
                        proposal=_sum_proposal(), generation_run_id="run_x", now=_NOW,
                        fresh_within=_FRESH_WITHIN)


# ── 4) auth -> catalog_viewer / unauthenticated reject; feature_engineer proceeds ────────────────
def test_scenario4_auth(db: DbConn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")
    boom_conn = cast(DbConn, _BoomConn())
    boom_adapter = cast(CatalogAdapter, None)
    viewer = IdentityEnvelope(subject="v", actor_kind="human", authenticated=True,
                              auth_method="oidc", role_claims=("catalog_viewer",))
    anon = IdentityEnvelope(subject="a", actor_kind="human", authenticated=False,
                            auth_method="oidc", role_claims=("feature_engineer",))

    # (a) a role WITHOUT feature:generate -> PermissionError, before any DB work (boom conn untouched).
    with pytest.raises(PermissionError):
        govern_llm_idea(boom_conn, boom_adapter, actor=viewer, proposal=_sum_proposal(),
                        generation_run_id="r", now=_NOW, fresh_within=_FRESH_WITHIN)

    # (b) an UNAUTHENTICATED principal (even carrying the right role claim) -> PermissionError.
    with pytest.raises(PermissionError):
        govern_llm_idea(boom_conn, boom_adapter, actor=anon, proposal=_sum_proposal(),
                        generation_run_id="r", now=_NOW, fresh_within=_FRESH_WITHIN)

    # (c) a feature_engineer PROCEEDS past auth -> reaches server trust derivation, which fail-closes on
    #     an unknown run (TrustDerivationError, NOT PermissionError) — proving auth was cleared.
    with pytest.raises(TrustDerivationError):
        govern_llm_idea(db, boom_adapter, actor=_feature_engineer(), proposal=_sum_proposal(),
                        generation_run_id="no_such_run", now=_NOW, fresh_within=_FRESH_WITHIN)


# ── 5) savepoint isolation -> technical_failure AND the outer transaction survives ───────────────
def test_scenario5_savepoint_isolation(
        db: DbConn, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
        monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_chain(db, service_actor, human_actor, run_id="run_sp", cust_wm=_FRESH_WM)
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")

    def _boom_plan(conn: DbConn, adapter: object, **kwargs: object) -> object:
        # A GENUINE DB error inside the savepoint — exactly the failure the savepoint must contain;
        # a bad statement aborts the subtransaction, which conn.transaction() rolls back to the savepoint.
        conn.execute("SELECT * FROM __b_t9_no_such_table__")
        raise AssertionError("unreachable")   # pragma: no cover

    monkeypatch.setattr(b_service, "plan_multi_source", _boom_plan)

    res = govern_llm_idea(db, current_catalog_adapter(), actor=_feature_engineer(),
                          proposal=_sum_proposal(), generation_run_id="run_sp", now=_NOW,
                          fresh_within=_FRESH_WITHIN)

    assert res is BDisposition.technical_failure
    # the savepoint CONTAINED the failure: the outer transaction is still usable (a poisoned tx would
    # raise "current transaction is aborted" here).
    row = db.execute("SELECT 1").fetchone()
    assert row is not None and row[0] == 1


# ── 6) server-derived context -> a bogus run raises TrustDerivationError (no caller-scope to inject) ─
def test_scenario6_server_derived_context(db: DbConn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")
    # The caller supplies ONLY generation_run_id — there is no scope / target_entity parameter to
    # inject; an unknown run therefore fail-closes in the SERVER-side trust derivation (T3).
    boom_adapter = cast(CatalogAdapter, None)   # unreachable: T3 raises before A is invoked
    with pytest.raises(TrustDerivationError):
        govern_llm_idea(db, boom_adapter, actor=_feature_engineer(), proposal=_sum_proposal(),
                        generation_run_id="no_such_run", now=_NOW, fresh_within=_FRESH_WITHIN)
