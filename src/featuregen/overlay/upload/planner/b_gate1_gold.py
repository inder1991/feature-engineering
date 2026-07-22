"""Phase 3C.2b-i-B · Task 10 — the immutable, PARTITIONED Gate-1 gold set (component qualification).

Two partitions, seeded + authored so the Task-10 gate (:mod:`b_gate1`) can decide whether the just-built
``govern_llm_idea`` (T9) is trustworthy enough for B to land shadow-only:

* :data:`CORRECTNESS_GOLD` — the CLEAN operational population. Each case carries an IMMUTABLE
  ``expected`` (:class:`BDisposition`) and a RAW proposal (not a pre-built intent — B starts from an
  untrusted LLM idea). A POSITIVE case MUST two-axis-govern to a :class:`GovernedResult` whose normalized
  intent carries the expected operand (``semantic_role=measure`` + ``path_strategy.aggregation=sum``),
  ``final_expression.operation=identity``, and the expected composite ``source_grain_key_refs`` (operand
  + operation + composite-grain preservation). A NEGATIVE case rejects with its EXACT ``BDisposition`` and
  NEVER a ``GovernedResult``.
* :data:`FAULT_CONTROLS` — the fault-observability partition (an injected DB error, a budget-truncated
  run). They pass when EXACTLY classified (``technical_failure`` / ``budget_truncated``) and are
  DELIBERATELY EXCLUDED from the clean population (a technical/truncation reading in the clean population
  is a gate FAILURE).

ALL authority is seeded through the REAL governance commands — the proven T9/spike chain (representative
ingest + human-confirmed concept via field-evidence, four-eyes VERIFIED grain, propose→confirm→project
VERIFIED bridges, drift watermarks, a durable confirmed scope). NEVER a direct concept/grain/bridge table
insert. Because the grain/bridge ``fact_key``s are DETERMINISTIC (ref+type), a case's governed outcome is
reproducible across runs on the same seeded authority — which is exactly what the gate's determinism
criterion verifies.

Cases are physically isolated by GLOBALLY-UNIQUE table names (the operand ``object_ref`` is
``public.<table>.<column>`` and the server identity map spans EVERY authorized catalog, so a shared table
name across two catalogs would collide into an ``AMBIGUOUS_CATALOG`` — which is precisely, and only, what
``neg_ambiguous_catalog`` deliberately seeds). The proposal-only negatives share ONE base governed source
(distinct table names, distinct concepts); the positives and the broken-authority negatives each get their
own tables. Under :data:`MAX_AUTHORIZED_CATALOGS_CONSIDERED` (16).

Out of scope by design (structurally unreachable in the demo — do NOT build): TREND/RECENCY positives
(fold to ``operation_deferred``), ``AUTHORITY_STATE_DRIFTED`` (needs the deferred worker/capture split),
pins / pin-bypass (no caller-injectable pin), and time-anchor cases (no supported aggregation needs
``SemanticRole.time``).

Shadow-only. Read-only over A / the T2–T9 modules / the governed stores; nothing here edits a reused
module. Frozen slotted dataclasses; no pydantic.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.scope_records import record_confirmed_scope
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner import b_slice_spike as spike
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_proposal import RawFeatureProposalV1, new_raw_proposal
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

# ── the fixed planning clock + freshness the gold is authored against ────────────────────────────
GOLD_NOW = datetime(2026, 7, 22, tzinfo=UTC)
FRESH_WM = GOLD_NOW - timedelta(minutes=5)      # inside the 24h drift SLA -> union freshness resolves
FRESH_WITHIN = timedelta(hours=24)
RUN_ID = "b_gate1_run"                           # the one durable confirmed-scope run every case shares

# Non-vacuity floor: at least this many DISTINCT positive shapes must two-axis-govern (a reject-all /
# no-op implementation cannot clear this).
B_GATE1_MIN_POSITIVE_SHAPES = 2

_SCHEMA = "public"


def _data_owner() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    """Register the same overlay config the T9 seeding seals (drift SLA 24h, no restricted profiler
    role) so the drift-freshness gate + grain gate route exactly as the proven chain."""
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


# ── low-level seed helpers (REAL governance commands only) ────────────────────────────────────────
def _stand_up(conn: DbConn, source: str, table: str,
              columns: list[tuple[str, str | None, str, bool]], grain: list[str] | None,
              *, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope, now: datetime,
              project_grain: bool) -> None:
    """Ingest a representative table + human-confirm each column's concept + (optionally) a VERIFIED
    four-eyes grain fact, through the REAL commands. ``columns`` is a list of
    ``(column, concept | None, type, is_as_of)``; a ``None`` concept skips concept confirmation (a
    column the case never resolves a concept for). ``grain`` is the grain key columns (``None`` seeds NO
    grain fact — the ungoverned-structure case)."""
    spike.ingest_representative_table(
        conn, source,
        [CanonicalRow(source, table, col, typ, as_of=as_of) for col, _c, typ, as_of in columns],
        actor=_data_owner(), now=now)
    for col, concept, _typ, _as_of in columns:
        if concept is not None:
            spike.human_confirm_concept(conn, source=source, schema=None, table=table, column=col,
                                        concept=concept, actor_subject="admin")
    if grain is not None:
        spike.confirm_grain_fact(conn, source=source, table=table, columns=grain,
                                 service_actor=service_actor, human_actor=human_actor)
        if project_grain:
            spike.project_grain_is_grain(conn, source=source, table=table, now=now)


def _record_concept_evidence(conn: DbConn, *, source: str, table: str, column: str, value: str,
                             lifecycle: EvidenceLifecycle = EvidenceLifecycle.ACTIVE) -> None:
    """Write ONE raw ``concept`` field-evidence row (HUMAN, CONFIRMED) at a chosen ``lifecycle``, through
    the production writer — the direct T5 seam the brief calls for the stale/conflict cases (a
    ``human_confirm_concept`` would project + could supersede a sibling row, which the conflict case must
    NOT do). The ``logical_ref`` is the SCHEMA-PRESERVING ref the adapter recovers for the operand."""
    ref = normalize_ref(source, _SCHEMA, table, column)
    record_field_evidence(
        conn, logical_ref=ref, field_name="concept", proposed_value=value,
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref=f"human:b-gate1:{value}", source_snapshot_id="b-gate1-snap",
        input_hash=field_input_hash(logical_ref=ref, field_name="concept", material=value),
        lifecycle=lifecycle)


def _proposal(*operands: str, operation: str) -> RawFeatureProposalV1:
    return new_raw_proposal(operands=operands, operation=operation, window=None, grain_hint=None)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Per-case seeders (each seeds ONE topology through the real commands; deduped by identity in
# ``seed_correctness_gold`` so the shared base is stood up exactly once).
# ══════════════════════════════════════════════════════════════════════════════════════════════════
def _seed_identity(conn: DbConn, *, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
                   now: datetime) -> None:
    """The T9 happy-path shape: a txn source (grain ``[tran_id]``, entity=transaction) -> account bridge
    -> customer bridge -> customer landing. Two VERIFIED bridges, two hops."""
    _stand_up(conn, "g_pi_txn", "pitxn",
              [("tran_id", "transaction_id", "varchar", False),
               ("tran_amt", "monetary_flow", "numeric", False),
               ("foracid", "account_id", "varchar", False),
               ("cif_id", "customer_id", "varchar", False)],
              ["tran_id"], service_actor=service_actor, human_actor=human_actor, now=now,
              project_grain=False)
    _stand_up(conn, "g_pi_acct", "piacct",
              [("foracid", "account_id", "varchar", False),
               ("cif_id", "customer_id", "varchar", False)],
              ["foracid"], service_actor=service_actor, human_actor=human_actor, now=now,
              project_grain=True)
    _stand_up(conn, "g_pi_cust", "picust",
              [("cif_id", "customer_id", "varchar", False),
               ("segment", "segment", "varchar", False)],
              ["cif_id"], service_actor=service_actor, human_actor=human_actor, now=now,
              project_grain=True)
    spike.verify_bridge(conn, entity_id="account", left=("g_pi_txn", "pitxn", "foracid"),
                        right=("g_pi_acct", "piacct", "foracid"),
                        service_actor=service_actor, human_actor=human_actor, now=now)
    spike.verify_bridge(conn, entity_id="customer", left=("g_pi_acct", "piacct", "cif_id"),
                        right=("g_pi_cust", "picust", "cif_id"),
                        service_actor=service_actor, human_actor=human_actor, now=now)
    for src in ("g_pi_txn", "g_pi_acct", "g_pi_cust"):
        spike.set_fresh_watermark(conn, src, FRESH_WM)


def _seed_composite(conn: DbConn, *, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
                    now: datetime) -> None:
    """A COMPOSITE-grain source: an account-grained txn table keyed ``[foracid, as_of_date]`` (one
    entity-linked key + a partition key) -> customer bridge -> customer landing. ONE hop, ONE bridge —
    a topology + grain shape distinct from the identity positive, proving composite grain-key
    preservation. The source grain entity folds to ``account`` (the sole entity-linked key)."""
    _stand_up(conn, "g_pc_txn", "pctxn",
              [("foracid", "account_id", "varchar", False),
               ("as_of_date", "as_of_date", "date", True),
               ("cif_id", "customer_id", "varchar", False),
               ("tran_amt", "monetary_flow", "numeric", False)],
              ["foracid", "as_of_date"], service_actor=service_actor, human_actor=human_actor,
              now=now, project_grain=True)
    _stand_up(conn, "g_pc_cust", "pccust",
              [("cif_id", "customer_id", "varchar", False),
               ("segment", "segment", "varchar", False)],
              ["cif_id"], service_actor=service_actor, human_actor=human_actor, now=now,
              project_grain=True)
    spike.verify_bridge(conn, entity_id="customer", left=("g_pc_txn", "pctxn", "cif_id"),
                        right=("g_pc_cust", "pccust", "cif_id"),
                        service_actor=service_actor, human_actor=human_actor, now=now)
    for src in ("g_pc_txn", "g_pc_cust"):
        spike.set_fresh_watermark(conn, src, FRESH_WM)


def _seed_base(conn: DbConn, *, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
               now: datetime) -> None:
    """The ONE shared governed base source for the proposal-only negatives: two numeric MEASURE columns
    (``bmeas1``/``bmeas2``), a NUMERIC-typed IDENTIFIER column (``bcustid`` -> the governed
    ``customer_id`` concept — it clears the numeric gauntlet but is COUNTED, the M1 cross-check), and an
    as-of column (``bas_of``) so a windowed op reaches the operation grammar rather than the temporal
    brake. NO grain fact (these cases reject before the source-binding step)."""
    _stand_up(conn, "g_base", "btxn",
              [("bmeas1", None, "numeric", False),
               ("bmeas2", None, "numeric", False),
               ("bcustid", "customer_id", "integer", False),
               ("bas_of", None, "date", True)],
              None, service_actor=service_actor, human_actor=human_actor, now=now,
              project_grain=False)
    spike.set_fresh_watermark(conn, "g_base", FRESH_WM)


def _seed_concept_not_in_registry(conn: DbConn, *, service_actor: IdentityEnvelope,
                                  human_actor: IdentityEnvelope, now: datetime) -> None:
    """A numeric measure column whose HUMAN/CONFIRMED concept is a bogus string absent from the registry
    -> the T5 registry check rejects ``concept_not_in_registry``."""
    _stand_up(conn, "g_cnr", "cnrtxn", [("amt", None, "numeric", False)], None,
              service_actor=service_actor, human_actor=human_actor, now=now, project_grain=False)
    _record_concept_evidence(conn, source="g_cnr", table="cnrtxn", column="amt",
                             value="totally_not_a_concept_xyz")
    spike.set_fresh_watermark(conn, "g_cnr", FRESH_WM)


def _seed_concept_conflict(conn: DbConn, *, service_actor: IdentityEnvelope,
                           human_actor: IdentityEnvelope, now: datetime) -> None:
    """TWO distinct ACTIVE HUMAN/CONFIRMED concept rows on one column -> ``concept_authority_conflict``."""
    _stand_up(conn, "g_cc", "cctxn", [("amt", None, "numeric", False)], None,
              service_actor=service_actor, human_actor=human_actor, now=now, project_grain=False)
    _record_concept_evidence(conn, source="g_cc", table="cctxn", column="amt", value="monetary_flow")
    _record_concept_evidence(conn, source="g_cc", table="cctxn", column="amt", value="monetary_stock")
    spike.set_fresh_watermark(conn, "g_cc", FRESH_WM)


def _seed_concept_stale(conn: DbConn, *, service_actor: IdentityEnvelope,
                        human_actor: IdentityEnvelope, now: datetime) -> None:
    """A SUPERSEDED accepted-pair concept row, none ACTIVE -> ``concept_authority_stale``."""
    _stand_up(conn, "g_cs", "cstxn", [("amt", None, "numeric", False)], None,
              service_actor=service_actor, human_actor=human_actor, now=now, project_grain=False)
    _record_concept_evidence(conn, source="g_cs", table="cstxn", column="amt", value="monetary_flow",
                             lifecycle=EvidenceLifecycle.SUPERSEDED)
    spike.set_fresh_watermark(conn, "g_cs", FRESH_WM)


def _seed_structural(conn: DbConn, *, service_actor: IdentityEnvelope,
                     human_actor: IdentityEnvelope, now: datetime) -> None:
    """A governed MEASURE (``monetary_flow``, clears the role cross-check) on a source with NO VERIFIED
    grain fact -> the T7 source-binding step rejects ``structural_need_ungoverned``."""
    _stand_up(conn, "g_su", "sutxn", [("amt", "monetary_flow", "numeric", False)], None,
              service_actor=service_actor, human_actor=human_actor, now=now, project_grain=False)
    spike.set_fresh_watermark(conn, "g_su", FRESH_WM)


def _seed_ambiguous(conn: DbConn, *, service_actor: IdentityEnvelope,
                    human_actor: IdentityEnvelope, now: datetime) -> None:
    """The SAME operand ``object_ref`` (``public.ambtxn.amt``) carried by TWO authorized catalogs, so the
    server identity map resolves it to two sources -> the gauntlet's ``AMBIGUOUS_CATALOG`` hard reject
    (folds to ``gauntlet_rejected``)."""
    for src in ("g_amb_a", "g_amb_b"):
        _stand_up(conn, src, "ambtxn", [("amt", None, "numeric", False)], None,
                  service_actor=service_actor, human_actor=human_actor, now=now, project_grain=False)
        spike.set_fresh_watermark(conn, src, FRESH_WM)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# The immutable case model + the CORRECTNESS gold.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class BGate1Case:
    """One immutable Gate-1 case: a RAW LLM proposal + the EXACT ``BDisposition`` the gate holds
    ``govern_llm_idea`` to. For a POSITIVE (``expected is governed``), ``shape`` names the authoritative
    shape it covers and ``expected_grain_key_refs`` is the composite source grain the normalized intent's
    operand must preserve verbatim. ``seed`` stands the case's authority up through the real commands
    (deduped by identity across cases that share a base)."""
    case_id: str
    is_positive: bool
    proposal: RawFeatureProposalV1
    expected: BDisposition
    seed: Callable[..., None]
    shape: str | None = None
    expected_grain_key_refs: tuple[str, ...] = ()


# ── POSITIVES (>= B_GATE1_MIN_POSITIVE_SHAPES distinct shapes; each MUST two-axis-govern) ──
_POS_IDENTITY = BGate1Case(
    case_id="pos_identity_single_measure", is_positive=True, shape="identity_single_measure",
    proposal=_proposal("public.pitxn.tran_amt", operation="sum"),
    expected=BDisposition.governed, seed=_seed_identity,
    expected_grain_key_refs=("public.pitxn.tran_id",))

_POS_COMPOSITE = BGate1Case(
    case_id="pos_composite_grain_landing", is_positive=True, shape="composite_grain_landing",
    proposal=_proposal("public.pctxn.tran_amt", operation="sum"),
    expected=BDisposition.governed, seed=_seed_composite,
    expected_grain_key_refs=("public.pctxn.foracid", "public.pctxn.as_of_date"))

# ── NEGATIVES (each rejects with its EXACT BDisposition, NEVER a GovernedResult) ──
_NEG_RATIO = BGate1Case(
    case_id="neg_operand_order_authority_missing", is_positive=False,
    proposal=_proposal("public.btxn.bmeas1", operation="ratio"),
    expected=BDisposition.operand_order_authority_missing, seed=_seed_base)

_NEG_OP_UNRECOGNIZED = BGate1Case(
    case_id="neg_operation_unrecognized", is_positive=False,
    proposal=_proposal("public.btxn.bmeas1", operation="frobnicate"),
    expected=BDisposition.operation_unrecognized, seed=_seed_base)

_NEG_OP_DEFERRED = BGate1Case(
    case_id="neg_operation_deferred", is_positive=False,
    proposal=_proposal("public.btxn.bmeas1", operation="recency"),
    expected=BDisposition.operation_deferred, seed=_seed_base)

_NEG_ROLE = BGate1Case(
    case_id="neg_role_not_aggregatable", is_positive=False,
    proposal=_proposal("public.btxn.bcustid", operation="sum"),
    expected=BDisposition.role_not_aggregatable, seed=_seed_base)

_NEG_TWO_OPERAND = BGate1Case(
    case_id="neg_unresolved_operand", is_positive=False,
    proposal=_proposal("public.btxn.bmeas1", "public.btxn.bmeas2", operation="sum"),
    expected=BDisposition.unresolved_operand, seed=_seed_base)

_NEG_LOSSY = BGate1Case(
    case_id="neg_proposal_lossy", is_positive=False,
    proposal=_proposal("public.btxn.bmeas1", "public.btxn.ghost_col", operation="sum"),
    expected=BDisposition.proposal_lossy, seed=_seed_base)

_NEG_CONCEPT_NOT_IN_REGISTRY = BGate1Case(
    case_id="neg_concept_not_in_registry", is_positive=False,
    proposal=_proposal("public.cnrtxn.amt", operation="sum"),
    expected=BDisposition.concept_not_in_registry, seed=_seed_concept_not_in_registry)

_NEG_CONCEPT_CONFLICT = BGate1Case(
    case_id="neg_concept_authority_conflict", is_positive=False,
    proposal=_proposal("public.cctxn.amt", operation="sum"),
    expected=BDisposition.concept_authority_conflict, seed=_seed_concept_conflict)

_NEG_CONCEPT_STALE = BGate1Case(
    case_id="neg_concept_authority_stale", is_positive=False,
    proposal=_proposal("public.cstxn.amt", operation="sum"),
    expected=BDisposition.concept_authority_stale, seed=_seed_concept_stale)

_NEG_STRUCTURAL = BGate1Case(
    case_id="neg_structural_need_ungoverned", is_positive=False,
    proposal=_proposal("public.sutxn.amt", operation="sum"),
    expected=BDisposition.structural_need_ungoverned, seed=_seed_structural)

_NEG_AMBIGUOUS = BGate1Case(
    case_id="neg_gauntlet_rejected_ambiguous_catalog", is_positive=False,
    proposal=_proposal("public.ambtxn.amt", operation="sum"),
    expected=BDisposition.gauntlet_rejected, seed=_seed_ambiguous)


CORRECTNESS_GOLD: tuple[BGate1Case, ...] = (
    _POS_IDENTITY, _POS_COMPOSITE,
    _NEG_RATIO, _NEG_OP_UNRECOGNIZED, _NEG_OP_DEFERRED, _NEG_ROLE, _NEG_TWO_OPERAND, _NEG_LOSSY,
    _NEG_CONCEPT_NOT_IN_REGISTRY, _NEG_CONCEPT_CONFLICT, _NEG_CONCEPT_STALE, _NEG_STRUCTURAL,
    _NEG_AMBIGUOUS,
)


def seed_correctness_gold(conn: DbConn, *, service_actor: IdentityEnvelope,
                          human_actor: IdentityEnvelope, now: datetime = GOLD_NOW,
                          cases: tuple[BGate1Case, ...] = CORRECTNESS_GOLD) -> None:
    """Seed every clean-population case's authority through the REAL commands, then record the ONE durable
    confirmed scope (``target_entity=customer``) every case's proposal derives its trust context from.
    Case seeders are deduped by identity so the shared base source is stood up exactly once."""
    ensure_upload_catalog_adapter()
    _seal()
    seen: set[int] = set()
    for case in cases:
        if id(case.seed) in seen:
            continue
        seen.add(id(case.seed))
        case.seed(conn, service_actor=service_actor, human_actor=human_actor, now=now)
    record_confirmed_scope(
        conn, intent_id=f"i_{RUN_ID}", generation_run_id=RUN_ID, recognition_id=None,
        scope=ConfirmedScope(primary=None, unscoped=True, target_entity="customer"),
        use_case_origins={}, confirmation_source="user_confirmed", confirmed_by="ds1")


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Fault-observability controls (EXCLUDED from the clean population).
# ══════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class BFaultControl:
    """A fault-observability control: a fully-governable proposal whose run is EXACTLY classified on the
    technical axis under an injected fault (``db_error`` -> the T9 savepoint catches a psycopg error;
    ``budget_truncation`` -> a spent compile budget). Driven under its OWN handling so it never pollutes
    the clean population. It reuses the identity positive's governed chain (already seeded); the fault is
    induced by the harness, not the gold."""
    control_id: str
    proposal: RawFeatureProposalV1
    injection: str
    expected: BDisposition


FAULT_CONTROLS: tuple[BFaultControl, ...] = (
    BFaultControl(control_id="fault_injected_db_error",
                  proposal=_proposal("public.pitxn.tran_amt", operation="sum"),
                  injection="db_error", expected=BDisposition.technical_failure),
    BFaultControl(control_id="fault_budget_truncated",
                  proposal=_proposal("public.pitxn.tran_amt", operation="sum"),
                  injection="budget_truncation", expected=BDisposition.budget_truncated),
)
