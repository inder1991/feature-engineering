"""Phase 3C.2b-i-B · Task 4 — the SAFETY BRAKE: gauntlet + preservation + tri-state + categorization.

DB-backed (real ``db`` connection). Proves ``run_gauntlet_and_preserve`` runs the PRODUCTION
deterministic gauntlet (``feature_assist._validate_idea``) on the raw LLM proposal and then:

  * retains the Slice-3 tri-state (``DESIGN_CHECKED`` / ``NEEDS_EXTERNAL_VALIDATION``) verbatim;
  * catches the silent operand drop the gauntlet performs at ``feature_assist.py:474`` — an operand
    NOT in the server-authorized ``known`` set vanishes before validation, and this task surfaces it
    as ``proposal_lossy`` instead of silently accepting a rewritten feature;
  * carries the hard-reject ``RejectCode`` forward on ``gauntlet_rejected``;
  * splits the vetted refs into computation operands vs structural refs with a disjoint, total cover.

Seeding mirrors the sibling gauntlet suites (``test_feature_assist``/``_hitl``): ``build_graph`` for the
read-scope-gated ``graph_node`` columns. Drift freshness (the STALE gate's input) is written by the REAL
production drift writer ``detect_catalog_changes`` over an ``UploadCatalog`` adapter (BM2) — not a raw
``overlay_drift_watermark`` INSERT — so the freshness input is genuine end to end.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import RejectCode
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_gauntlet import (
    GauntletRejectionV1,
    VettedProposal,
    run_gauntlet_and_preserve,
)
from featuregen.overlay.upload.planner.b_proposal import new_raw_proposal
from featuregen.overlay.upload.planner.b_scope import IdentityEntryV1, IdentityMapV1
from featuregen.overlay.upload.upload_catalog import UploadCatalog

_NOW = datetime(2026, 7, 21, tzinfo=UTC)
_FRESH_WITHIN = timedelta(hours=24)
_SRC = "bank"

_AMOUNT = "public.txns.amount"
_TXN_DATE = "public.txns.txn_date"
_TXN_ID = "public.txns.txn_id"


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _identity_map(source: str, *object_refs: str) -> IdentityMapV1:
    """A real T3 ``IdentityMapV1`` mapping each authorized operand ``object_ref -> (source,)``. This is
    the exact type ``derive_request_context`` returns; hand-building it keeps the T4 test focused on the
    gauntlet while consuming the genuine trust-input type (never a fake)."""
    return IdentityMapV1(entries=tuple(
        IdentityEntryV1(object_ref=ref, catalog_sources=(source,))
        for ref in sorted(object_refs)))


def _measure_only_rows() -> list[CanonicalRow]:
    return [CanonicalRow(_SRC, "txns", "amount", "numeric")]


def _multiref_rows() -> list[CanonicalRow]:
    """A measure + a governed-later grain + a point-in-time column, so a windowed roll-up resolves a
    measure_ref, a grain_ref, AND a time_ref (exercises full-coverage categorization)."""
    return [
        CanonicalRow(_SRC, "txns", "amount", "numeric"),
        CanonicalRow(_SRC, "txns", "txn_date", "timestamp", as_of=True),
        CanonicalRow(_SRC, "txns", "txn_id", "integer", is_grain=True),
    ]


def _write_watermark(db, rows: list[CanonicalRow], *, at: datetime) -> None:
    """Advance the drift watermark via the REAL production writer (BM2). ``detect_catalog_changes``
    snapshots the ``UploadCatalog`` fingerprint and writes ``overlay_drift_watermark.last_completed_at``
    atomically — the exact freshness input ``_validate_idea``'s STALE gate reads via ``drift_watermark``.
    A raw INSERT is deliberately avoided here; full real-scan freshness is exercised end-to-end in
    T10/T12."""
    detect_catalog_changes(db, UploadCatalog(_SRC, rows), actor=_actor(), now=at)


# ── 1) PASS — NEEDS_EXTERNAL_VALIDATION: tri-state retained; measure + grain/time categorized ─────
def test_pass_needs_external_validation_preserves_and_categorizes(db) -> None:
    rows = _multiref_rows()
    build_graph(db, _SRC, rows)
    _write_watermark(db, rows, at=_NOW)  # FRESH

    proposal = new_raw_proposal(
        operands=(_AMOUNT,), operation="sum_90d", window="90d", grain_hint="txns")
    out = run_gauntlet_and_preserve(
        db, proposal=proposal, identity_map=_identity_map(_SRC, _AMOUNT), target_ref=None,
        roles=("feature_engineer",), now=_NOW, fresh_within=_FRESH_WITHIN)

    assert isinstance(out, VettedProposal)
    # build_graph-seeded grain/as-of are file-declared (hint authority), so the honest tri-state is
    # NEEDS_EXTERNAL_VALIDATION with typed requirements RETAINED (never recomputed here).
    assert out.idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert out.idea.requirements != ()
    assert (_SRC, _AMOUNT) in out.computation_operands
    struct_refs = {ref for _cat, ref in out.structural_refs}
    assert _TXN_ID in struct_refs        # grain -> structural
    assert _TXN_DATE in struct_refs      # point-in-time -> structural


# ── 1b) PASS — DESIGN_CHECKED: a clean numeric non-additive-unsafe op keeps zero requirements ──────
def test_pass_design_checked_clean(db) -> None:
    rows = _measure_only_rows()
    build_graph(db, _SRC, rows)
    _write_watermark(db, rows, at=_NOW)

    # AVG is numeric-requiring (amount is numeric -> passes) but NOT additive-unsafe and NOT windowed,
    # and there is no grain hint -> no requirements -> a clean DESIGN_CHECKED.
    proposal = new_raw_proposal(operands=(_AMOUNT,), operation="avg", window=None, grain_hint=None)
    out = run_gauntlet_and_preserve(
        db, proposal=proposal, identity_map=_identity_map(_SRC, _AMOUNT), target_ref=None,
        roles=(), now=_NOW, fresh_within=_FRESH_WITHIN)

    assert isinstance(out, VettedProposal)
    assert out.idea.validation_status == "DESIGN_CHECKED"
    assert out.idea.requirements == ()
    assert out.computation_operands == ((_SRC, _AMOUNT),)
    assert out.structural_refs == ()


# ── 2) HARD REJECT — LEAKAGE carried forward as a RejectCode on gauntlet_rejected ─────────────────
def test_hard_reject_leakage_carries_reject_code(db) -> None:
    rows = _measure_only_rows()
    build_graph(db, _SRC, rows)
    # LEAKAGE fires before the freshness gate, so no watermark is needed.

    proposal = new_raw_proposal(operands=(_AMOUNT,), operation="sum", window=None, grain_hint=None)
    out = run_gauntlet_and_preserve(
        db, proposal=proposal, identity_map=_identity_map(_SRC, _AMOUNT), target_ref=_AMOUNT,
        roles=(), now=_NOW, fresh_within=_FRESH_WITHIN)

    assert isinstance(out, GauntletRejectionV1)
    assert out.disposition is BDisposition.gauntlet_rejected
    assert out.reject_code == RejectCode.LEAKAGE
    assert out.message  # audit-safe, code/ref only


# ── 2b) HARD REJECT — STALE (BM2: freshness via the real detect_catalog_changes scan) ─────────────
def test_hard_reject_stale_via_real_drift_scan(db) -> None:
    rows = _measure_only_rows()
    build_graph(db, _SRC, rows)
    # A GENUINE stale watermark: the real drift writer stamps last_completed_at 10 days in the past,
    # older than `now - fresh_within`, so _validate_idea's STALE gate rejects.
    _write_watermark(db, rows, at=_NOW - timedelta(days=10))

    proposal = new_raw_proposal(operands=(_AMOUNT,), operation="avg", window=None, grain_hint=None)
    out = run_gauntlet_and_preserve(
        db, proposal=proposal, identity_map=_identity_map(_SRC, _AMOUNT), target_ref=None,
        roles=(), now=_NOW, fresh_within=_FRESH_WITHIN)

    assert isinstance(out, GauntletRejectionV1)
    assert out.disposition is BDisposition.gauntlet_rejected
    assert out.reject_code == RejectCode.STALE


# ── 3) DROPPED OPERAND — the silent drop at feature_assist.py:474 becomes proposal_lossy ──────────
def test_dropped_operand_is_caught_as_proposal_lossy(db) -> None:
    rows = _measure_only_rows()
    build_graph(db, _SRC, rows)
    _write_watermark(db, rows, at=_NOW)  # survivors must pass freshness

    ghost = "public.txns.ghost"  # never authorized -> silently dropped by the gauntlet at :474
    proposal = new_raw_proposal(
        operands=(_AMOUNT, ghost), operation="avg", window=None, grain_hint=None)
    out = run_gauntlet_and_preserve(
        db, proposal=proposal, identity_map=_identity_map(_SRC, _AMOUNT), target_ref=None,
        roles=(), now=_NOW, fresh_within=_FRESH_WITHIN)

    # The survivor (amount) validates, but the raw proposal was lossy — NOT silently accepted.
    assert isinstance(out, GauntletRejectionV1)
    assert out.disposition is BDisposition.proposal_lossy
    assert out.reject_code is None
    assert ghost in out.message


# ── 4) CATEGORIZATION full coverage — every vetted ref in exactly one disjoint category ───────────
def test_categorization_is_disjoint_and_total(db) -> None:
    rows = _multiref_rows()
    build_graph(db, _SRC, rows)
    _write_watermark(db, rows, at=_NOW)

    proposal = new_raw_proposal(
        operands=(_AMOUNT,), operation="sum_90d", window="90d", grain_hint="txns")
    out = run_gauntlet_and_preserve(
        db, proposal=proposal, identity_map=_identity_map(_SRC, _AMOUNT), target_ref=None,
        roles=("feature_engineer",), now=_NOW, fresh_within=_FRESH_WITHIN)

    assert isinstance(out, VettedProposal)
    comp = set(out.computation_operands)
    struct = set(out.structural_refs)
    assert comp.isdisjoint(struct)                       # disjoint at the (catalog, ref) grain

    # The vetted-ref set the idea resolved: measures ∪ grain ∪ time ∪ grouping.
    vetted = {ref for _c, ref in out.idea.measure_refs}
    if out.idea.grain_ref is not None:
        vetted.add(out.idea.grain_ref[1])
    if out.idea.time_ref is not None:
        vetted.add(out.idea.time_ref[1])
    vetted |= {ref for _c, ref in out.idea.grouping_refs}

    comp_refs = {ref for _c, ref in comp}
    struct_refs = {ref for _c, ref in struct}
    assert comp_refs.isdisjoint(struct_refs)             # each vetted ref lands in exactly one
    assert comp_refs | struct_refs == vetted             # together they cover the whole resolved set
    assert vetted == {_AMOUNT, _TXN_ID, _TXN_DATE}
