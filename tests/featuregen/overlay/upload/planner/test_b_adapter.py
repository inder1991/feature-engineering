"""Phase 3C.2b-i-B · Task 9 — the normalization adapter (``normalize_feature_idea``).

DB-backed. Scenario 7 (brief §Tests) — the reject PASSTHROUGHS: each governance step short-circuits
with the FIRST failing step's :class:`BDisposition`, never a half-built intent. Exercised here:

  * a hard GAUNTLET reject folds to ``gauntlet_rejected``;
  * THE M1 CONCEPT-DRIVEN ROLE CROSS-CHECK — a ``sum`` asked of a governed IDENTIFIER (a COUNTED
    concept, not a MEASURE) is ``role_not_aggregatable``, never trusting operand position;
  * an ORDERED op (``ratio``) is ``operand_order_authority_missing`` (order is never inferred);
  * a non-single computation-operand shape is ``unresolved_operand``.

NB on "leakage": the adapter fixes ``target_ref=None`` (an unsupervised roll-up has no label column),
so ``RejectCode.LEAKAGE`` (target-overlap) is structurally UNREACHABLE through this entrypoint. The
leakage-FAMILY reject we CAN drive is ``NO_POINT_IN_TIME`` ("future-leakage risk"): a windowed op on a
table with no governed as-of column. It folds to the same ``gauntlet_rejected`` — the point of the
subcase is that ANY hard gauntlet reject passes straight through.

Seeding reuses the proven spike helpers (representative ingest + a human-confirmed concept + a fresh
watermark + a durable confirmed scope), then derives the SERVER-side request context (T3) that carries
the operand identity map the adapter's gauntlet consumes.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.scope_records import record_confirmed_scope
from featuregen.overlay.upload.planner import b_slice_spike as spike
from featuregen.overlay.upload.planner.b_adapter import normalize_feature_idea
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_proposal import RawFeatureProposalV1, new_raw_proposal
from featuregen.overlay.upload.planner.b_scope import RequestContextV1, derive_request_context
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_NOW = datetime(2026, 7, 22, tzinfo=UTC)
_FRESH_WM = _NOW - timedelta(minutes=5)
_FRESH_WITHIN = timedelta(hours=24)

_SRC = "s_adp"
_TABLE = "t"
_AMOUNT = "public.t.amount"
_AMOUNT2 = "public.t.amount2"
_CUST_NUM = "public.t.cust_num"


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _data_owner() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seed(db: DbConn) -> RequestContextV1:
    """One representative source with two numeric MEASURE columns (``amount``/``amount2``) and a
    NUMERIC-typed IDENTIFIER column (``cust_num`` -> the governed ``customer_id`` concept, so it clears
    the numeric-type gauntlet but is COUNTED, not MEASURE), a fresh watermark, and a durable confirmed
    scope. Returns the SERVER-derived request context the adapter consumes (no caller-injected scope)."""
    ensure_upload_catalog_adapter()
    _seal()
    columns = [("amount", "monetary_flow", "numeric"),
               ("amount2", "monetary_flow", "numeric"),
               ("cust_num", "customer_id", "integer")]
    spike.ingest_representative_table(
        db, _SRC, [CanonicalRow(_SRC, _TABLE, col, typ) for col, _c, typ in columns],
        actor=_data_owner(), now=_NOW)
    # Only cust_num needs a resolvable concept — it is the sole subcase that reaches the role step.
    spike.human_confirm_concept(db, source=_SRC, schema=None, table=_TABLE, column="cust_num",
                                concept="customer_id", actor_subject="admin")
    spike.set_fresh_watermark(db, _SRC, _FRESH_WM)
    record_confirmed_scope(
        db, intent_id="i_adp", generation_run_id="run_adp", recognition_id=None,
        scope=ConfirmedScope(primary=None, unscoped=True, target_entity="customer"),
        use_case_origins={}, confirmation_source="user_confirmed", confirmed_by="ds1")
    return derive_request_context(db, roles=("feature_engineer",), generation_run_id="run_adp",
                                  now=_NOW)


def test_scenario7_reject_passthroughs(db: DbConn) -> None:
    ctx = _seed(db)
    adapter = current_catalog_adapter()

    def _norm(proposal: RawFeatureProposalV1) -> object:
        return normalize_feature_idea(db, adapter, proposal=proposal, ctx=ctx,
                                      roles=("feature_engineer",), now=_NOW,
                                      fresh_within=_FRESH_WITHIN)

    # (i) GAUNTLET reject passthrough — a windowed op on a table with no governed as-of column is a
    #     NO_POINT_IN_TIME ("future-leakage risk") hard reject; ANY hard reject folds to gauntlet_rejected.
    leaky = new_raw_proposal(operands=(_AMOUNT,), operation="sum_90d", window="90d", grain_hint=None)
    assert _norm(leaky) is BDisposition.gauntlet_rejected

    # (ii) THE M1 CROSS-CHECK — SUM asked of a governed identifier (customer_id -> COUNTED, not MEASURE)
    #      -> role_not_aggregatable. The operand's role is concept-driven, never positional.
    nonmeasure = new_raw_proposal(operands=(_CUST_NUM,), operation="sum", window=None, grain_hint=None)
    assert _norm(nonmeasure) is BDisposition.role_not_aggregatable

    # (iii) an ORDERED op (ratio) — operand order (numerator/denominator) must never be inferred, so it
    #       is deferred: operand_order_authority_missing.
    ratio = new_raw_proposal(operands=(_AMOUNT,), operation="ratio", window=None, grain_hint=None)
    assert _norm(ratio) is BDisposition.operand_order_authority_missing

    # (iv) the supported ops are single-operand; two computation operands -> unresolved_operand
    #      (this slice has no cross-catalog combine).
    two = new_raw_proposal(operands=(_AMOUNT, _AMOUNT2), operation="sum", window=None, grain_hint=None)
    assert _norm(two) is BDisposition.unresolved_operand
