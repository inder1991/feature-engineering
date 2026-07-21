"""Phase 3C.2b-i-B · Task 1 — the SPIKE (GO/NO-GO), end-to-end on the REAL FTR export.

Drives ONE governed single-operand cross-catalog roll-up chain on the REAL
``FTR_Column_Mapping_final.csv`` (LOCAL-ONLY, git-excluded — read, never staged) + a representative
customer-master slice, with EVERY authority established through the real governance commands
(human-confirmed concept via the field-evidence confirm path; VERIFIED grain via
``propose_fact``/``confirm_fact``; real projected bridges via ``propose_bridge``/confirm/
``project_verified_bridge``). Proves ``map_a_outcome(result) == BDisposition.governed``.

THE TOPOLOGY FINDING (surfaced, not worked around): A's global entity graph has NO
``transaction -> customer`` edge — only ``transaction -> account`` + ``account -> customer``. So a
FLAT FTR transaction table cannot roll ``SUM(TRAN_AMT)`` to ``customer`` over a SINGLE CIF_ID
(customer) bridge — proven by ``test_flat_cif_bridge_does_not_reach_customer``, which fails CLOSED to
``structural_need_ungoverned`` (not a fake pass). The GO chain crosses TWO real VERIFIED bridges: an
``account`` bridge for the ``transaction -> account`` hop, then a ``customer`` bridge on CIF_ID for
the ``account -> customer`` hop.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.planner import b_slice_spike as spike
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.declarations import CompileBudget
from featuregen.overlay.upload.planner.multisource_contracts import MultiSourceReason
from featuregen.overlay.upload.planner.multisource_plan import plan_multi_source
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_NOW = datetime(2026, 7, 21, tzinfo=UTC)
_FRESH_WM = _NOW - timedelta(minutes=5)
_FRESH_WITHIN = timedelta(hours=24)

# The REAL FTR export at the repo root — LOCAL-ONLY / git-excluded. Read, NEVER staged.
_FTR_CSV_PATH = Path(__file__).resolve().parents[5] / "FTR_Column_Mapping_final.csv"

FTR_SRC = "ftr"
FTR_SCHEMA = "DPL_EIB_COMPLIANCE"
FTR_TABLE = "comp_financial_tran_repos_dly"   # DPL_EIB_COMPLIANCE.COMP_FINANCIAL_TRAN_REPOS_DLY, lowered
ACCT_SRC = "acctmaster"
CUST_SRC = "custmaster"

_AMT_REF = f"public.{FTR_TABLE}.tran_amt"


pytestmark = pytest.mark.skipif(
    not _FTR_CSV_PATH.exists(),
    reason=f"real FTR export not present at {_FTR_CSV_PATH} (LOCAL-ONLY spike fixture)")


@pytest.fixture
def ftr_csv() -> str:
    # utf-8-sig: the real export carries a UTF-8 BOM that, left in place, mis-parses the first
    # quoted header (BOM precedes the opening quote) — a file-decoding concern, not a rule.
    return _FTR_CSV_PATH.read_text(encoding="utf-8-sig")


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _data_owner() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _ingest_real_ftr_source(db, ftr_csv, service_actor, human_actor):
    """Real FTR ingest + human-confirmed concepts (TRAN_AMT->monetary_flow, FORACID->account_id,
    CIF_ID->customer_id) + a VERIFIED grain fact on TRAN_ID (transaction). Returns nothing; callers
    add the bridges/landing they need."""
    res = spike.ingest_ftr_glossary(db, ftr_csv, source=FTR_SRC, actor=_data_owner(), now=_NOW)
    assert res.status == "ingested", f"real FTR ingest did not clean-ingest: {res.status} / {res.reason}"
    for column, concept in (("tran_amt", "monetary_flow"), ("foracid", "account_id"),
                            ("cif_id", "customer_id")):
        spike.human_confirm_concept(db, source=FTR_SRC, schema=FTR_SCHEMA, table=FTR_TABLE,
                                    column=column, concept=concept, actor_subject="admin")
    spike.confirm_grain_fact(db, source=FTR_SRC, table=FTR_TABLE, columns=["tran_id"],
                             service_actor=service_actor, human_actor=human_actor)


def _stand_up_customer_table(db, source, table, columns_concepts, grain_columns,
                             service_actor, human_actor):
    """A representative table ingested through the real path, with human-confirmed concepts + a
    VERIFIED grain fact projected onto ``is_grain`` — all via real commands."""
    spike.ingest_representative_table(
        db, source, [CanonicalRow(source, table, col, "varchar") for col, _ in columns_concepts],
        actor=_data_owner(), now=_NOW)
    for column, concept in columns_concepts:
        spike.human_confirm_concept(db, source=source, schema=None, table=table, column=column,
                                    concept=concept, actor_subject="admin")
    spike.confirm_grain_fact(db, source=source, table=table, columns=grain_columns,
                             service_actor=service_actor, human_actor=human_actor)
    spike.project_grain_is_grain(db, source=source, table=table, now=_NOW)


def _fresh_watermarks(db, *sources):
    for src in sources:
        spike.set_fresh_watermark(db, src, _FRESH_WM)


def _budget() -> CompileBudget:
    return CompileBudget(remaining=64, deadline_monotonic=float("inf"), clock=time.monotonic)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# GO: the full chain two-axis-governs on the REAL FTR data.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_ftr_single_operand_rollup_two_axis_governed(db, ftr_csv, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    _seal()

    # (1) real FTR source + a representative account slice + a representative customer slice.
    _ingest_real_ftr_source(db, ftr_csv, service_actor, human_actor)
    _stand_up_customer_table(
        db, ACCT_SRC, "acct", [("foracid", "account_id"), ("cif_id", "customer_id")],
        grain_columns=["foracid"], service_actor=service_actor, human_actor=human_actor)
    _stand_up_customer_table(
        db, CUST_SRC, "cust", [("cif_id", "customer_id"), ("segment", "segment")],
        grain_columns=["cif_id"], service_actor=service_actor, human_actor=human_actor)

    # (2) two REAL VERIFIED bridges: transaction->account on FORACID, account->customer on CIF_ID.
    _, s_acct = spike.verify_bridge(
        db, entity_id="account", left=(FTR_SRC, FTR_TABLE, "foracid"),
        right=(ACCT_SRC, "acct", "foracid"),
        service_actor=service_actor, human_actor=human_actor, now=_NOW)
    assert s_acct == "projected"
    _, s_cust = spike.verify_bridge(
        db, entity_id="customer", left=(ACCT_SRC, "acct", "cif_id"),
        right=(CUST_SRC, "cust", "cif_id"),
        service_actor=service_actor, human_actor=human_actor, now=_NOW)
    assert s_cust == "projected"

    _fresh_watermarks(db, FTR_SRC, ACCT_SRC, CUST_SRC)

    # (3) server-derived scope + confirmed target_entity.
    ctx = spike.derive_request_context(
        authorized_catalogs=(FTR_SRC, ACCT_SRC, CUST_SRC), target_entity="customer")

    # (4) _vet gauntlet on the RAW proposal + raw/vetted preservation.
    raw = {"name": "total_tran_amt_per_customer", "derives_from": [_AMT_REF], "aggregation": "sum"}
    vet = spike.run_gauntlet_and_preserve(
        db, raw=raw, known={_AMT_REF}, src_of={_AMT_REF: {FTR_SRC}}, target_ref=None,
        now=_NOW, fresh_within=_FRESH_WITHIN, roles=("feature_engineer",))
    assert vet.rejection is None, f"gauntlet hard-rejected the operand: {vet.rejection}"
    assert vet.preserved, "raw operand was dropped/rewritten by the gauntlet (PROPOSAL_LOSSY)"
    assert vet.idea.validation_status in ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION")

    # (5) B-normalize ONE operand SUM(TRAN_AMT) — concept + source_binding from the REAL state.
    adapter = current_catalog_adapter()
    concept = spike.resolve_confirmed_concept(db, source=FTR_SRC, object_ref=_AMT_REF)
    assert concept == "monetary_flow", f"operand concept not resolved from confirmed evidence: {concept}"
    binding = spike.resolve_governed_source_binding(
        db, adapter, source=FTR_SRC, table=FTR_TABLE, source_grain_entity="transaction", now=_NOW)
    assert binding is not None and binding.source_grain_key_refs == (f"public.{FTR_TABLE}.tran_id",)
    intent = spike.build_single_operand_sum_intent(
        catalog_source=FTR_SRC, object_ref=_AMT_REF, concept=concept, source_binding=binding,
        target_entity=ctx.target_entity)

    # (6) bounded plan_multi_source -> BOTH axes resolved.
    result = plan_multi_source(db, adapter, intent=intent, scope=ctx.scope,
                               roles=("feature_engineer",), now=_NOW, budget=_budget())

    assert spike.two_axis_disposition(result) is BDisposition.governed, (
        f"chain did not two-axis-govern: assembly={result.result_status} "
        f"contract={result.contract_result_status} "
        f"plan={result.selected_plan_id} contract_id={result.selected_contract_id}")
    # both winning ids set (the two-axis invariant)
    assert result.selected_plan_id is not None
    assert result.selected_contract_id is not None


# ════════════════════════════════════════════════════════════════════════════════════════════════
# NO-GO (the honest topology finding): a FLAT FTR transaction table + a SINGLE CIF_ID (customer)
# bridge cannot reach a customer landing — the entity graph has no transaction->customer edge, so A
# fails CLOSED. This proves the GO chain's two-bridge shape is necessary, not decorative.
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_flat_cif_bridge_does_not_reach_customer(db, ftr_csv, service_actor, human_actor):
    ensure_upload_catalog_adapter()
    _seal()

    _ingest_real_ftr_source(db, ftr_csv, service_actor, human_actor)
    _stand_up_customer_table(
        db, CUST_SRC, "cust", [("cif_id", "customer_id"), ("segment", "segment")],
        grain_columns=["cif_id"], service_actor=service_actor, human_actor=human_actor)

    # A SINGLE real VERIFIED bridge on CIF_ID (customer), directly transaction-table -> customer.
    _, s_cust = spike.verify_bridge(
        db, entity_id="customer", left=(FTR_SRC, FTR_TABLE, "cif_id"),
        right=(CUST_SRC, "cust", "cif_id"),
        service_actor=service_actor, human_actor=human_actor, now=_NOW)
    assert s_cust == "projected"

    _fresh_watermarks(db, FTR_SRC, CUST_SRC)

    ctx = spike.derive_request_context(
        authorized_catalogs=(FTR_SRC, CUST_SRC), target_entity="customer")
    adapter = current_catalog_adapter()
    concept = spike.resolve_confirmed_concept(db, source=FTR_SRC, object_ref=_AMT_REF)
    binding = spike.resolve_governed_source_binding(
        db, adapter, source=FTR_SRC, table=FTR_TABLE, source_grain_entity="transaction", now=_NOW)
    intent = spike.build_single_operand_sum_intent(
        catalog_source=FTR_SRC, object_ref=_AMT_REF, concept=concept, source_binding=binding,
        target_entity=ctx.target_entity)

    result = plan_multi_source(db, adapter, intent=intent, scope=ctx.scope,
                               roles=("feature_engineer",), now=_NOW, budget=_budget())

    # fail-closed: no governed transaction->customer path (customer bridge only realizes
    # account->customer, from an account-grained position the flat transaction table never reaches).
    assert result.result_status is MultiSourceReason.no_governed_path
    assert spike.two_axis_disposition(result) is BDisposition.structural_need_ungoverned
    assert spike.two_axis_disposition(result) is not BDisposition.governed
