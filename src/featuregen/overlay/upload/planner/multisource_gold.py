"""Phase 3C.2b-i-A · Task 12 — the PARTITIONED multi-source assembly gold set (spec §10/§11).

Two immutable partitions, seeded + authored so the Task-12 gate (:mod:`multisource_gate`) can decide
whether the governed multi-source assembler is trustworthy:

* :data:`CORRECTNESS_GOLD` — the CLEAN operational population. Every case carries an IMMUTABLE
  ``expected`` outcome. A POSITIVE case MUST resolve to the EXACT expected ``physical_landing``
  (incl. composite ``grain_key_refs``), per-slot ``path_strategy`` (preserved verbatim), and
  ``final_expression``; it covers ``MULTISOURCE_GOLD_MIN_SHAPES`` (6) distinct authoritative shapes
  (identity, ``RATIO`` with a ``take_latest`` denominator, ``DIFFERENCE``, ``TREND``,
  ``COUNT_DISTINCT``, a composite-grain landing). A NEGATIVE case rejects with the EXACT expected
  disposition code (``NO_GOVERNED_PATH`` / ``REALIZATION_ENDPOINT_UNGOVERNED`` /
  ``SOURCE_BINDING_UNGOVERNED`` / ``NO_COMMON_PHYSICAL_GRAIN`` / ``AMBIGUOUS_PHYSICAL_GRAIN`` /
  ``AGGREGATION_UNSAFE_ON_PATH`` / ``TEMPORAL_PATHS_INCOMPATIBLE`` / ``ORDERING_ANCHOR_MISSING`` /
  ``OPERAND_SHAPE_INVALID`` / ``UNSUPPORTED_PATH_AGGREGATION`` — plus a concept-collision pin-bypass
  that also fails ``SOURCE_BINDING_UNGOVERNED``).
* :data:`FAULT_CONTROLS` — the fault-observability partition (an injected DB error, a budget-truncated
  run). They pass when EXACTLY classified (``technical_failure`` / ``budget_truncated``) and are
  DELIBERATELY EXCLUDED from the clean operational population (a technical/truncation reading in the
  clean population is a gate FAILURE, per spec §10).

:func:`seed_gold` seeds VERIFIED ``grain`` facts, VERIFIED ``entity_bridge`` facts, VERIFIED
intra-catalog joins (via file-declared ``joins_to``), drift watermarks, and projection checkpoints
through the **REAL governance write paths** (``propose_fact`` → task open → ``confirm_fact`` → drain
the projection; ``entity_bridge_edge`` for VERIFIED bridges; ``overlay_drift_watermark`` for drift) —
never a hand-set column. Because ``fact_key`` is DETERMINISTIC (ref+type), the resulting keys are
STABLE across runs, so the gate's double-run determinism keys on stable authored fact_keys (never a
per-event id).

Every case shares just two catalogs (``cb`` source-side, ``wl`` landing-side) with per-case TABLE
NAMES — the frontier anchors bridges/realizations on EXACT object_refs (``assembly.py`` rollup/
reposition), so distinct table names keep the cases physically isolated while staying well under
``MAX_AUTHORIZED_CATALOGS_CONSIDERED``. Identities are supplied by the caller (a service proposer +
a platform-admin human confirmer) — the four-eyes governance flow the grain gate requires; this
module never mints a privileged principal itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.identity import fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.task_read import get_task_proposal
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import (
    OPERATION_POLICY_VERSION,
    AdditivityClass,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalExpressionV1,
    FinalOperation,
    GovernedSourceBindingV1,
    MultiSourcePlannerIntentV1,
    OperandSlotV1,
    PathAggregation,
    PathStrategyV1,
    PhysicalLandingV1,
    SemanticRole,
)
from featuregen.overlay.upload.planner.multisource_shadow_store import (
    SemanticOutcome,
    TechnicalStatus,
)
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref
from featuregen.projections.runner import run_projection

# The fixed planning clock the gold is authored against: drift watermarks are seeded FRESH relative
# to it (well inside the 60-min SLA), so the gate must plan at this same ``now`` for the union
# freshness observation to resolve on every positive case.
GOLD_NOW = datetime(2026, 7, 19, tzinfo=UTC)

# The two catalogs every case shares (source-side / landing-side); cases are isolated by TABLE NAME.
CB = "cb"
WL = "wl"


def grain_fact_key(catalog: str, table: str) -> str:
    """The DETERMINISTIC grain ``fact_key`` for a table (ref+type) — the SAME key the governance write
    path keys the grain fact on and the source-endpoint revalidation compares the binding against."""
    return fact_key(table_ref(catalog, table), "grain")


# ── low-level seed helpers (REAL governance write paths only) ─────────────────────────────────────
def _graph(conn, source: str, rows_concepts: list[tuple[CanonicalRow, str]]) -> None:
    rows = [r for r, _ in rows_concepts]
    build_graph(conn, source, rows, concepts={content_hash(r): c for r, c in rows_concepts})


def _bridge(conn, fk: str, entity: str, lc: str, lref: str, rc: str, rref: str) -> None:
    conn.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, confirmed_event_id, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'VERIFIED')",
        (fk, entity, lc, lref, rc, rref, f"evt-{fk}"))


def _drain(conn) -> None:
    while run_projection(conn, OverlayProjection()) >= 500:
        pass


def _grain(conn, source: str, table: str, columns: list[str], *,
           service_actor: IdentityEnvelope, human_actor: IdentityEnvelope) -> None:
    """A VERIFIED ``grain`` fact via the four-eyes governance flow (propose → task → confirm → drain).
    ``resolve_fact`` reads the ``overlay_fact_state`` read model the projection populates, so the drain
    is mandatory — a skipped drain would leave a later endpoint revalidation reading a stale model."""
    ref = table_ref(source, table)
    value = {"columns": columns, "is_unique": True}
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        service_actor, f"gold-propose-{source}-{table}"))
    if not res.accepted:
        raise RuntimeError(f"gold grain propose denied for {source}.{table}: {res.denied_reason}")
    key = fact_key(ref, "grain")
    row = conn.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open' "
        "ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
    if row is None:
        raise RuntimeError(f"gold grain task missing for {source}.{table}")
    proposal = get_task_proposal(conn, row[0], human_actor)
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": proposal["target_event_id"],
         "value": value},
        human_actor, f"gold-confirm-{proposal['target_event_id']}"))
    if not res.accepted:
        raise RuntimeError(f"gold grain confirm denied for {source}.{table}: {res.denied_reason}")
    _drain(conn)


def _watermark(conn, source: str, at: datetime) -> None:
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s,%s,'gold_drift',0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = EXCLUDED.last_completed_at", (source, at))


# ── CanonicalRow shorthands ───────────────────────────────────────────────────────────────────────
def _col(table: str, name: str, *, grain: bool = False, joins_to: str = "", card: str = "",
         additivity: str = "", as_of: bool = False, catalog: str = CB) -> CanonicalRow:
    return CanonicalRow(catalog, table, name, "integer" if not as_of else "date", is_grain=grain,
                        joins_to=joins_to, cardinality=card, additivity=additivity, as_of=as_of)


# ── the seeder ────────────────────────────────────────────────────────────────────────────────────
def seed_gold(conn, *, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
              now: datetime = GOLD_NOW) -> None:
    """Seed every correctness + fault-control topology into ``cb``/``wl`` through the real governance
    write paths (deterministic fact_keys). ``now`` sets the drift watermark freshness; keep it equal to
    the gate's planning ``now`` (``GOLD_NOW``) so the positive cases' union freshness resolves.

    Idempotent by CONVENTION only within a single rolled-back fixture transaction: the caller seeds,
    the shadow harness plans + rolls the fixture back, and the caller re-seeds for the second run — the
    deterministic fact_keys make the two runs fingerprint-identical."""
    ensure_upload_catalog_adapter()
    fresh = now - timedelta(minutes=5)

    # ``build_graph`` REPLACES a catalog's whole graph on each call, so EVERY table of a catalog is
    # accumulated and the graph is built ONCE per catalog (a per-topology build would wipe the earlier
    # tables). Grain FACTS + bridges live in their own tables and are seeded after the graphs exist.
    cb_rows: list[tuple[CanonicalRow, str]] = []
    wl_rows: list[tuple[CanonicalRow, str]] = []
    bridges: list[tuple[str, str, str, str, str, str]] = []
    grains: list[tuple[str, str, list[str]]] = []

    def _fk(table: str, column: str, target: str, *, catalog: str = CB) -> CanonicalRow:
        return _col(table, column, joins_to=target, card="N:1", catalog=catalog)

    # ── MAIN topology (identity / difference / count_distinct / concept-collision) ──
    cb_rows += [
        (_col("txn", "transaction_id", grain=True), "transaction_id"),
        (_col("txn", "account_id"), "account_id"),
        (CanonicalRow(CB, "txn", "amount", "numeric"), "monetary_flow")]
    wl_rows += [
        (_col("acc", "account_id", grain=True, catalog=WL), "account_id"),
        (_fk("acc", "customer_id", "cust.customer_id", catalog=WL), "customer_id"),
        (_col("cust", "customer_id", grain=True, catalog=WL), "customer_id")]
    bridges.append(("gbfk_main", "account", CB, "public.txn.account_id", WL, "public.acc.account_id"))
    grains += [(CB, "txn", ["transaction_id"]), (WL, "acc", ["account_id"]), (WL, "cust", ["customer_id"])]

    # ── TAKE-LATEST topology (ratio / trend) — intra-cb rollup + reposition bridge across account ──
    cb_rows += [
        (_col("txn_tl", "transaction_id", grain=True), "transaction_id"),
        (_fk("txn_tl", "account_id", "iacc_tl.account_id"), "account_id"),
        (CanonicalRow(CB, "txn_tl", "amount", "numeric"), "monetary_flow"),
        (CanonicalRow(CB, "txn_tl", "balance", "numeric"), "monetary_stock"),
        (CanonicalRow(CB, "txn_tl", "asof", "date", as_of=True, additivity="semi_additive"), "as_of_date"),
        (_col("iacc_tl", "account_id", grain=True), "account_id")]
    wl_rows += [
        (_col("acc_tl", "account_id", grain=True, catalog=WL), "account_id"),
        (_fk("acc_tl", "customer_id", "cust_tl.customer_id", catalog=WL), "customer_id"),
        (_col("cust_tl", "customer_id", grain=True, catalog=WL), "customer_id")]
    bridges.append(("gbfk_tl", "account", CB, "public.iacc_tl.account_id", WL, "public.acc_tl.account_id"))
    grains += [(CB, "txn_tl", ["transaction_id"]), (CB, "iacc_tl", ["account_id"]),
               (WL, "acc_tl", ["account_id"]), (WL, "cust_tl", ["customer_id"])]

    # ── COMPOSITE-grain landing topology (wl.cust_co keyed by (customer_id, cust_asof)) ──
    cb_rows += [
        (_col("txn_co", "transaction_id", grain=True), "transaction_id"),
        (_col("txn_co", "account_id"), "account_id"),
        (CanonicalRow(CB, "txn_co", "amount", "numeric"), "monetary_flow")]
    wl_rows += [
        (_col("acc_co", "account_id", grain=True, catalog=WL), "account_id"),
        (_fk("acc_co", "customer_id", "cust_co.customer_id", catalog=WL), "customer_id"),
        (_col("cust_co", "customer_id", grain=True, catalog=WL), "customer_id"),
        (CanonicalRow(WL, "cust_co", "cust_asof", "date", as_of=True), "as_of_date")]
    bridges.append(("gbfk_co", "account", CB, "public.txn_co.account_id", WL, "public.acc_co.account_id"))
    grains += [(CB, "txn_co", ["transaction_id"]), (WL, "acc_co", ["account_id"]),
               (WL, "cust_co", ["customer_id", "cust_asof"])]

    # ── UNGOVERNED-LANDING topology (wl.cust_ul has NO grain fact) → realization_endpoint_ungoverned ──
    cb_rows += [
        (_col("txn_ul", "transaction_id", grain=True), "transaction_id"),
        (_col("txn_ul", "account_id"), "account_id"),
        (CanonicalRow(CB, "txn_ul", "amount", "numeric"), "monetary_flow")]
    wl_rows += [
        (_col("acc_ul", "account_id", grain=True, catalog=WL), "account_id"),
        (_fk("acc_ul", "customer_id", "cust_ul.customer_id", catalog=WL), "customer_id"),
        (_col("cust_ul", "customer_id", grain=True, catalog=WL), "customer_id")]
    bridges.append(("gbfk_ul", "account", CB, "public.txn_ul.account_id", WL, "public.acc_ul.account_id"))
    # deliberately NO grain fact on wl.cust_ul (the landing endpoint is ungoverned)
    grains += [(CB, "txn_ul", ["transaction_id"]), (WL, "acc_ul", ["account_id"])]

    # ── SOURCE-UNGOVERNED topology (cb.txn_su has NO grain fact) → source_binding_ungoverned ──
    cb_rows += [
        (_col("txn_su", "transaction_id", grain=True), "transaction_id"),
        (_col("txn_su", "account_id"), "account_id"),
        (CanonicalRow(CB, "txn_su", "amount", "numeric"), "monetary_flow")]
    wl_rows += [
        (_col("acc_su", "account_id", grain=True, catalog=WL), "account_id"),
        (_fk("acc_su", "customer_id", "cust_su.customer_id", catalog=WL), "customer_id"),
        (_col("cust_su", "customer_id", grain=True, catalog=WL), "customer_id")]
    bridges.append(("gbfk_su", "account", CB, "public.txn_su.account_id", WL, "public.acc_su.account_id"))
    # deliberately NO grain fact on cb.txn_su (the source endpoint is ungoverned)
    grains += [(WL, "acc_su", ["account_id"]), (WL, "cust_su", ["customer_id"])]

    # ── NO-BRIDGE topology (cb.txn_nb has an account FK but no VERIFIED bridge) → no_governed_path ──
    cb_rows += [
        (_col("txn_nb", "transaction_id", grain=True), "transaction_id"),
        (_col("txn_nb", "account_id"), "account_id"),
        (CanonicalRow(CB, "txn_nb", "amount", "numeric"), "monetary_flow")]
    grains += [(CB, "txn_nb", ["transaction_id"])]

    # ── NON-ADDITIVE topology (amount non_additive) → aggregation_unsafe_on_path (sum over fan-in) ──
    cb_rows += [
        (_col("txn_na", "transaction_id", grain=True), "transaction_id"),
        (_col("txn_na", "account_id"), "account_id"),
        (CanonicalRow(CB, "txn_na", "amount", "numeric", additivity="non_additive"), "monetary_flow")]
    wl_rows += [
        (_col("acc_na", "account_id", grain=True, catalog=WL), "account_id"),
        (_fk("acc_na", "customer_id", "cust_na.customer_id", catalog=WL), "customer_id"),
        (_col("cust_na", "customer_id", grain=True, catalog=WL), "customer_id")]
    bridges.append(("gbfk_na", "account", CB, "public.txn_na.account_id", WL, "public.acc_na.account_id"))
    grains += [(CB, "txn_na", ["transaction_id"]), (WL, "acc_na", ["account_id"]),
               (WL, "cust_na", ["customer_id"])]

    # ── TEMPORAL-INCOMPATIBLE topology: two measures whose CONCEPT pit_roles differ (event vs as_of).
    # A MEASURE need's temporal role derives from its concept's pit_role (need_metadata), so the two
    # paths carry DISTINCT pit anchors at the common landing → temporal_paths_incompatible. The date
    # columns are declared ADDITIVE so the per-path SUM aggregation is sound (control reaches the
    # cross-path temporal check rather than short-circuiting on aggregation). ──
    cb_rows += [
        (_col("txn_tp", "transaction_id", grain=True), "transaction_id"),
        (_col("txn_tp", "account_id"), "account_id"),
        (CanonicalRow(CB, "txn_tp", "orig_dt", "date", additivity="additive"), "origination_date"),
        (CanonicalRow(CB, "txn_tp", "asof_dt", "date", additivity="additive"), "as_of_date")]
    wl_rows += [
        (_col("acc_tp", "account_id", grain=True, catalog=WL), "account_id"),
        (_fk("acc_tp", "customer_id", "cust_tp.customer_id", catalog=WL), "customer_id"),
        (_col("cust_tp", "customer_id", grain=True, catalog=WL), "customer_id")]
    bridges.append(("gbfk_tp", "account", CB, "public.txn_tp.account_id", WL, "public.acc_tp.account_id"))
    grains += [(CB, "txn_tp", ["transaction_id"]), (WL, "acc_tp", ["account_id"]),
               (WL, "cust_tp", ["customer_id"])]

    # ── NO-COMMON topology: two operands land on DISTINCT customer tables → no_common_physical_grain ──
    for sfx in ("nca", "ncb"):
        cb_rows += [
            (_col(f"txn_{sfx}", "transaction_id", grain=True), "transaction_id"),
            (_col(f"txn_{sfx}", "account_id"), "account_id"),
            (CanonicalRow(CB, f"txn_{sfx}", "amount", "numeric"), "monetary_flow")]
        wl_rows += [
            (_col(f"acc_{sfx}", "account_id", grain=True, catalog=WL), "account_id"),
            (_fk(f"acc_{sfx}", "customer_id", f"cust_{sfx}.customer_id", catalog=WL), "customer_id"),
            (_col(f"cust_{sfx}", "customer_id", grain=True, catalog=WL), "customer_id")]
        bridges.append((f"gbfk_{sfx}", "account", CB, f"public.txn_{sfx}.account_id",
                        WL, f"public.acc_{sfx}.account_id"))
        grains += [(CB, f"txn_{sfx}", ["transaction_id"]), (WL, f"acc_{sfx}", ["account_id"]),
                   (WL, f"cust_{sfx}", ["customer_id"])]

    # ── AMBIGUOUS topology: ONE source reaches two EQUAL-authority landings via two bridges →
    # ambiguous_physical_grain (a top-semantic-rank tie across distinct landings). ──
    cb_rows += [
        (_col("txn_am", "transaction_id", grain=True), "transaction_id"),
        (_col("txn_am", "account_id"), "account_id"),
        (CanonicalRow(CB, "txn_am", "amount", "numeric"), "monetary_flow")]
    for leg in ("amx", "amy"):
        wl_rows += [
            (_col(f"acc_{leg}", "account_id", grain=True, catalog=WL), "account_id"),
            (_fk(f"acc_{leg}", "customer_id", f"cust_{leg}.customer_id", catalog=WL), "customer_id"),
            (_col(f"cust_{leg}", "customer_id", grain=True, catalog=WL), "customer_id")]
        bridges.append((f"gbfk_{leg}", "account", CB, "public.txn_am.account_id",
                        WL, f"public.acc_{leg}.account_id"))
        grains += [(WL, f"acc_{leg}", ["account_id"]), (WL, f"cust_{leg}", ["customer_id"])]
    grains += [(CB, "txn_am", ["transaction_id"])]

    # ── build each catalog's graph ONCE, then the VERIFIED bridges, VERIFIED grains, drift watermarks ──
    _graph(conn, CB, cb_rows)
    _graph(conn, WL, wl_rows)
    for bridge in bridges:
        _bridge(conn, *bridge)
    for catalog, table, columns in grains:
        _grain(conn, catalog, table, columns, service_actor=service_actor, human_actor=human_actor)
    _watermark(conn, CB, fresh)
    _watermark(conn, WL, fresh)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Authored intents + immutable expected outcomes.
# ══════════════════════════════════════════════════════════════════════════════════════════════════


def _strategy(aggregation: PathAggregation, additivity: AdditivityClass,
              anchor: str | None = None) -> PathStrategyV1:
    return PathStrategyV1(aggregation=aggregation, output_type="numeric",
                          output_additivity=additivity, external_type_required=False,
                          ordering_anchor_concept=anchor)


def _operand(*, slot_id: str, role: SemanticRole, table: str, column: str, concept: str,
             strategy: PathStrategyV1, grain_fk: str | None = None) -> OperandSlotV1:
    object_ref = f"public.{table}.{column}"
    return OperandSlotV1(
        slot_id=slot_id, semantic_role=role, catalog_source=CB, object_ref=object_ref,
        authoritative_concept=concept, path_strategy=strategy,
        source_binding=GovernedSourceBindingV1(
            source_grain_entity="transaction",
            source_grain_key_refs=(f"public.{table}.transaction_id",),
            grain_fact_key=grain_fk if grain_fk is not None else grain_fact_key(CB, table)))


def _intent(*, target: str = "customer", operands: tuple[OperandSlotV1, ...],
            operation: FinalOperation, ordered: tuple[str, ...],
            time_slot: str | None = None, window: str | None = None,
            output_additivity: AdditivityClass) -> MultiSourcePlannerIntentV1:
    return MultiSourcePlannerIntentV1(
        target_entity=target, operands=operands,
        final_expression=FinalExpressionV1(
            operation=operation, ordered_slot_ids=ordered, time_slot_id=time_slot,
            window=window, output_additivity=output_additivity),
        operation_policy_version=OPERATION_POLICY_VERSION)


def _landing(table: str, *columns: str) -> PhysicalLandingV1:
    return PhysicalLandingV1(catalog=WL, table_ref=f"public.{table}",
                             grain_key_refs=tuple(f"public.{table}.{c}" for c in columns))


@dataclass(frozen=True, slots=True)
class GoldCaseV1:
    """One immutable gold case: its authored intent + the EXACT outcome the gate holds the assembler to.

    ``expected_outcome`` is the assembly-axis ``SemanticOutcome`` (``resolved`` for positives; the exact
    reject code for negatives). For a POSITIVE, ``shape`` names the authoritative shape it covers and
    ``expected_landing`` is the exact physical grain every operand must converge to (composite refs
    intact) — the per-slot ``path_strategy`` + ``final_expression`` expectation IS the intent's own
    (preserved verbatim), so the gate proves preservation by comparing the persisted evidence to it."""
    case_id: str
    is_positive: bool
    intent: MultiSourcePlannerIntentV1
    expected_outcome: SemanticOutcome
    shape: str | None = None
    expected_landing: PhysicalLandingV1 | None = None


# ── POSITIVE cases (≥ MULTISOURCE_GOLD_MIN_SHAPES distinct shapes; each MUST resolve exactly) ──
_POS_IDENTITY = GoldCaseV1(
    case_id="pos_identity", is_positive=True, shape="identity_single_measure",
    expected_outcome=SemanticOutcome.resolved, expected_landing=_landing("cust", "customer_id"),
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.additive))

_POS_RATIO = GoldCaseV1(
    case_id="pos_ratio_take_latest", is_positive=True, shape="ratio_take_latest_denominator",
    expected_outcome=SemanticOutcome.resolved, expected_landing=_landing("cust_tl", "customer_id"),
    intent=_intent(
        operands=(
            _operand(slot_id="num", role=SemanticRole.numerator, table="txn_tl", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),
            _operand(slot_id="den", role=SemanticRole.denominator, table="txn_tl", column="balance",
                     concept="monetary_stock",
                     strategy=_strategy(PathAggregation.take_latest, AdditivityClass.semi_additive,
                                        anchor="as_of_date"))),
        operation=FinalOperation.ratio, ordered=("num", "den"),
        output_additivity=AdditivityClass.non_additive))

_POS_DIFFERENCE = GoldCaseV1(
    case_id="pos_difference", is_positive=True, shape="difference",
    expected_outcome=SemanticOutcome.resolved, expected_landing=_landing("cust", "customer_id"),
    intent=_intent(
        operands=(
            _operand(slot_id="mnd", role=SemanticRole.minuend, table="txn", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),
            _operand(slot_id="sbt", role=SemanticRole.subtrahend, table="txn", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive))),
        operation=FinalOperation.difference, ordered=("mnd", "sbt"),
        output_additivity=AdditivityClass.non_additive))

_POS_TREND = GoldCaseV1(
    case_id="pos_trend", is_positive=True, shape="trend",
    expected_outcome=SemanticOutcome.resolved, expected_landing=_landing("cust_tl", "customer_id"),
    intent=_intent(
        operands=(
            _operand(slot_id="mea", role=SemanticRole.measure, table="txn_tl", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),
            _operand(slot_id="tim", role=SemanticRole.time, table="txn_tl", column="asof",
                     concept="as_of_date",
                     strategy=_strategy(PathAggregation.take_latest, AdditivityClass.not_applicable,
                                        anchor="as_of_date"))),
        operation=FinalOperation.trend, ordered=("mea",), time_slot="tim", window="P3M",
        output_additivity=AdditivityClass.non_additive))

_POS_COUNT_DISTINCT = GoldCaseV1(
    case_id="pos_count_distinct", is_positive=True, shape="count_distinct",
    expected_outcome=SemanticOutcome.resolved, expected_landing=_landing("cust", "customer_id"),
    intent=_intent(
        operands=(_operand(slot_id="cnt", role=SemanticRole.counted, table="txn", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.count_distinct, AdditivityClass.additive)),),
        operation=FinalOperation.count_distinct, ordered=("cnt",),
        output_additivity=AdditivityClass.additive))

_POS_COMPOSITE = GoldCaseV1(
    case_id="pos_composite_grain", is_positive=True, shape="composite_grain_landing",
    expected_outcome=SemanticOutcome.resolved,
    expected_landing=_landing("cust_co", "customer_id", "cust_asof"),
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn_co", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.additive))

# ── NEGATIVE cases (each rejects with its EXACT disposition code) ──
_NEG_NO_GOVERNED_PATH = GoldCaseV1(
    case_id="neg_no_governed_path", is_positive=False,
    expected_outcome=SemanticOutcome.no_governed_path,
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn_nb", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.additive))

_NEG_ENDPOINT_UNGOVERNED = GoldCaseV1(
    case_id="neg_realization_endpoint_ungoverned", is_positive=False,
    expected_outcome=SemanticOutcome.realization_endpoint_ungoverned,
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn_ul", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.additive))

_NEG_SOURCE_UNGOVERNED = GoldCaseV1(
    case_id="neg_source_binding_ungoverned", is_positive=False,
    expected_outcome=SemanticOutcome.source_binding_ungoverned,
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn_su", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.additive))

# Concept-collision pin-bypass: the source cb.txn IS governed, but the binding pins the COLLIDING grain
# fact_key of a DIFFERENT governed table (wl.cust) — an attempt to borrow another concept's grain
# authority for this operand. The source-endpoint revalidation compares the claimed fact_key to the
# source table's REAL one and rejects → source_binding_ungoverned (fail-closed, spec §11).
_NEG_CONCEPT_COLLISION = GoldCaseV1(
    case_id="neg_concept_collision_pin_bypass", is_positive=False,
    expected_outcome=SemanticOutcome.source_binding_ungoverned,
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive),
                           grain_fk=grain_fact_key(WL, "cust")),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.additive))

_NEG_NO_COMMON = GoldCaseV1(
    case_id="neg_no_common_physical_grain", is_positive=False,
    expected_outcome=SemanticOutcome.no_common_physical_grain,
    intent=_intent(
        operands=(
            _operand(slot_id="mnd", role=SemanticRole.minuend, table="txn_nca", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),
            _operand(slot_id="sbt", role=SemanticRole.subtrahend, table="txn_ncb", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive))),
        operation=FinalOperation.difference, ordered=("mnd", "sbt"),
        output_additivity=AdditivityClass.non_additive))

_NEG_AMBIGUOUS = GoldCaseV1(
    case_id="neg_ambiguous_physical_grain", is_positive=False,
    expected_outcome=SemanticOutcome.ambiguous_physical_grain,
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn_am", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.additive))

_NEG_AGGREGATION_UNSAFE = GoldCaseV1(
    case_id="neg_aggregation_unsafe_on_path", is_positive=False,
    expected_outcome=SemanticOutcome.aggregation_unsafe_on_path,
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn_na", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.additive))

# Two measures with DIFFERENT concept pit_roles (origination_date=event vs as_of_date=as_of) land the
# same grain with DISTINCT pit anchors → temporal_paths_incompatible. Date columns declared additive so
# the per-path SUM is sound and control reaches the cross-path temporal coherence check.
_NEG_TEMPORAL_INCOMPAT = GoldCaseV1(
    case_id="neg_temporal_paths_incompatible", is_positive=False,
    expected_outcome=SemanticOutcome.temporal_paths_incompatible,
    intent=_intent(
        operands=(
            _operand(slot_id="mnd", role=SemanticRole.minuend, table="txn_tp", column="orig_dt",
                     concept="origination_date",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),
            _operand(slot_id="sbt", role=SemanticRole.subtrahend, table="txn_tp", column="asof_dt",
                     concept="as_of_date",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive))),
        operation=FinalOperation.difference, ordered=("mnd", "sbt"),
        output_additivity=AdditivityClass.non_additive))

# ── shape-only negatives (reject in validate_operation_shape BEFORE any DB read) ──
_NEG_ORDERING_ANCHOR_MISSING = GoldCaseV1(
    case_id="neg_ordering_anchor_missing", is_positive=False,
    expected_outcome=SemanticOutcome.ordering_anchor_missing,
    intent=_intent(
        operands=(
            _operand(slot_id="num", role=SemanticRole.numerator, table="txn", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),
            _operand(slot_id="den", role=SemanticRole.denominator, table="txn", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.take_latest, AdditivityClass.additive,
                                        anchor=None))),
        operation=FinalOperation.ratio, ordered=("num", "den"),
        output_additivity=AdditivityClass.non_additive))

_NEG_OPERAND_SHAPE_INVALID = GoldCaseV1(
    case_id="neg_operand_shape_invalid", is_positive=False,
    expected_outcome=SemanticOutcome.operand_shape_invalid,
    intent=_intent(
        operands=(
            _operand(slot_id="m1", role=SemanticRole.measure, table="txn", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),
            _operand(slot_id="m2", role=SemanticRole.measure, table="txn", column="amount",
                     concept="monetary_flow",
                     strategy=_strategy(PathAggregation.sum, AdditivityClass.additive))),
        operation=FinalOperation.identity, ordered=("m1",),
        output_additivity=AdditivityClass.additive))

_NEG_UNSUPPORTED_AGG = GoldCaseV1(
    case_id="neg_unsupported_path_aggregation", is_positive=False,
    expected_outcome=SemanticOutcome.unsupported_path_aggregation,
    intent=_intent(
        operands=(_operand(slot_id="m", role=SemanticRole.measure, table="txn", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.stddev, AdditivityClass.non_additive)),),
        operation=FinalOperation.identity, ordered=("m",),
        output_additivity=AdditivityClass.non_additive))


CORRECTNESS_GOLD: tuple[GoldCaseV1, ...] = (
    _POS_IDENTITY, _POS_RATIO, _POS_DIFFERENCE, _POS_TREND, _POS_COUNT_DISTINCT, _POS_COMPOSITE,
    _NEG_NO_GOVERNED_PATH, _NEG_ENDPOINT_UNGOVERNED, _NEG_SOURCE_UNGOVERNED, _NEG_CONCEPT_COLLISION,
    _NEG_NO_COMMON, _NEG_AMBIGUOUS, _NEG_AGGREGATION_UNSAFE, _NEG_TEMPORAL_INCOMPAT,
    _NEG_ORDERING_ANCHOR_MISSING, _NEG_OPERAND_SHAPE_INVALID, _NEG_UNSUPPORTED_AGG,
)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Fault-observability controls (EXCLUDED from the clean operational population, spec §10).
# ══════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class FaultControlV1:
    """A fault-observability control: an intent whose run is EXACTLY classified on the technical axis
    (never a semantic disposition), driven under its own run id so it never pollutes the clean gate
    population. ``injection`` names how the fault is induced (an injected DB error / a spent run
    budget) — the mechanics live in the harness the gate test drives, not in the gold."""
    control_id: str
    intent: MultiSourcePlannerIntentV1
    injection: str
    expected_technical_status: TechnicalStatus


def _fault_intent(slot: str) -> MultiSourcePlannerIntentV1:
    return _intent(
        operands=(_operand(slot_id=slot, role=SemanticRole.measure, table="txn", column="amount",
                           concept="monetary_flow",
                           strategy=_strategy(PathAggregation.sum, AdditivityClass.additive)),),
        operation=FinalOperation.identity, ordered=(slot,),
        output_additivity=AdditivityClass.additive)


FAULT_CONTROLS: tuple[FaultControlV1, ...] = (
    FaultControlV1(control_id="fault_injected_db_error", intent=_fault_intent("m"),
                   injection="db_error", expected_technical_status=TechnicalStatus.technical_failure),
    FaultControlV1(control_id="fault_budget_truncated", intent=_fault_intent("m"),
                   injection="budget_truncation",
                   expected_technical_status=TechnicalStatus.budget_truncated),
)
