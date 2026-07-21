"""Phase 3C.2b-i-B · Task 1 — the SPIKE: thin orchestration helpers for the GO/NO-GO.

These helpers prove ONE governed single-operand cross-catalog roll-up chain end-to-end on the REAL
FTR transaction export (``FTR_Column_Mapping_final.csv``), driving EVERY authority through the REAL
four-eyes / field-evidence governance commands — never a hand-set graph_node column or a raw fact
INSERT. The chain the ``test_b_slice_spike`` suite drives with these helpers:

  real FTR ingest + a representative customer-master slice (real ingest path)
   -> human-confirmed CONCEPT (record_field_evidence(HUMAN, CONFIRMED) -> resolve_and_project)
   -> VERIFIED GRAIN fact (propose_fact -> confirm_fact -> drain -> project is_grain)
   -> real projected BRIDGE (propose_bridge -> confirm_fact -> project_verified_bridge)
   -> server-derived CatalogScopeV1 + confirmed target_entity="customer"
   -> _vet gauntlet (safety + Slice-3 tri-state) + raw/vetted preservation
   -> B-normalize ONE operand SUM(TRAN_AMT) -> MultiSourcePlannerIntentV1
        (concept resolved from the REAL confirmed graph_node.concept; source_binding from the REAL
         VERIFIED grain fact; FinalExpression IDENTITY(SUM))
   -> bounded plan_multi_source -> map_a_outcome(result) == BDisposition.governed

The helpers are THIN: they orchestrate the reused real commands and never reimplement or weaken a
rule. Every authority is resolved from the DB (the real confirmed state), not injected as a literal.
Read-only over A / the engine; nothing here edits a reused module.

NOTE (the documented topology finding, surfaced by the spike): A's GLOBAL entity graph
(``taxonomy/entity_registry``) has NO ``transaction -> customer`` edge — only the five fixed
roll-ups (``transaction -> account``, ``account -> customer``, ...). So a FLAT FTR transaction table
grained on ``transaction`` cannot roll ``SUM(TRAN_AMT)`` to a ``customer`` landing over a SINGLE
customer/CIF_ID bridge: a CIF_ID (customer) bridge only realizes the ``account -> customer`` hop, and
reaching ``account`` from the transaction source requires an account crossing first. The GO chain
therefore crosses TWO real VERIFIED bridges — an ``account`` bridge on the account key for the
``transaction -> account`` hop, then a ``customer`` bridge on CIF_ID for the ``account -> customer``
hop — both established through the real ``propose_bridge -> confirm -> project_verified_bridge`` path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.identity import CatalogObjectRef, EntityBridgeRef, fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.task_read import get_task_proposal
from featuregen.overlay.upload.bridge_candidates import BridgeCandidateV1
from featuregen.overlay.upload.bridge_projection import project_verified_bridge
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import _validate_idea
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.b_dispositions import BDisposition, map_a_outcome
from featuregen.overlay.upload.planner.contracts import (
    OPERATION_POLICY_VERSION,
    AdditivityClass,
    CatalogScopeV1,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalExpressionV1,
    FinalOperation,
    GovernedSourceBindingV1,
    MultiSourcePlannerIntentV1,
    MultiSourcePlanningResultV1,
    OperandSlotV1,
    PathAggregation,
    PathStrategyV1,
    SemanticRole,
)
from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref
from featuregen.overlay.upload.upload_catalog import table_ref
from featuregen.projections.runner import run_projection

_SCHEMA = "public"


# ── ingest (real path) ─────────────────────────────────────────────────────────────────────────
def ingest_ftr_glossary(conn, csv_text: str, *, source: str, actor: IdentityEnvelope,
                        now: datetime):
    """Ingest the REAL FTR glossary export through the real reader + ``ingest_upload`` (no LLM
    client — deterministic; concepts are NOT enriched here, they are human-confirmed downstream)."""
    upload = to_glossary_upload(read_ftr_glossary(csv_text, source=source))
    return ingest_upload(conn, source, upload.rows, actor=actor, now=now, client=None,
                         glossary=upload)


def ingest_representative_table(conn, source: str, rows: list[CanonicalRow], *,
                                actor: IdentityEnvelope, now: datetime):
    """Ingest a REPRESENTATIVE (real-typed) technical upload through the real ``ingest_upload`` path.
    The customer-master slice enters exactly as an operational upload does; its concepts + grain are
    then established through the same real governance commands the FTR source uses."""
    return ingest_upload(conn, source, rows, actor=actor, now=now, client=None)


# ── authority: human-confirmed CONCEPT (field-evidence confirm path) ─────────────────────────────
def human_confirm_concept(conn, *, source: str, schema: str | None, table: str, column: str,
                          concept: str, actor_subject: str,
                          snapshot_id: str = "b-spike-snap") -> str:
    """Establish a column's CONCEPT at ``(HUMAN, CONFIRMED)`` and project it onto ``graph_node.concept``
    via the REAL field-evidence confirm path — never a hand-set column. Returns the logical_ref.

    The concept policy's operational rule is ``AnyOf((SOURCE/ATTESTED, HUMAN/CONFIRMED))`` with
    ``PREFER_CONFIRMED``, so ONE human-confirmed evidence row is the demo cohort's authority; nothing
    attests ``concept`` today (plan §Authority). ``resolve_and_project`` then writes the display value
    onto ``graph_node.concept``, which the planner reads via ``_load_columns`` and the physics read via
    ``key_entity`` / ``object_grain`` (both derive the entity from the concept's registry entity_link)."""
    ref = normalize_ref(source, schema, table, column)
    record_field_evidence(
        conn, logical_ref=ref, field_name="concept", proposed_value=concept,
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref=f"human:{actor_subject}", source_snapshot_id=snapshot_id,
        input_hash=field_input_hash(logical_ref=ref, field_name="concept", material=concept))
    resolve_and_project(conn, source=source, logical_refs=[ref])
    return ref


# ── authority: VERIFIED GRAIN fact (four-eyes) + operational projection ───────────────────────────
def _drain(conn) -> None:
    """Drive the overlay projection to catch-up so ``resolve_fact`` reads the VERIFIED read model."""
    while run_projection(conn, OverlayProjection()) >= 500:
        pass


def confirm_grain_fact(conn, *, source: str, table: str, columns: list[str],
                       service_actor: IdentityEnvelope, human_actor: IdentityEnvelope) -> str:
    """Confirm a VERIFIED ``grain`` fact through the real four-eyes flow (service ``propose_fact`` ->
    platform-admin ``confirm_fact`` -> drain). Returns the deterministic grain ``fact_key`` — the SAME
    key A's source-endpoint revalidation compares the operand's ``source_binding`` against."""
    ref = table_ref(source, table)
    value = {"columns": columns, "is_unique": True}
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        service_actor, f"b-spike-propose-grain-{source}-{table}"))
    assert res.accepted, f"grain propose denied for {source}.{table}: {res.denied_reason}"
    key = fact_key(ref, "grain")
    row = conn.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open' "
        "ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
    assert row is not None, f"no open grain gate task for {source}.{table}"
    proposal = get_task_proposal(conn, row[0], human_actor)
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": proposal["target_event_id"],
         "value": value},
        human_actor, f"b-spike-confirm-grain-{proposal['target_event_id']}"))
    assert res.accepted, f"grain confirm denied for {source}.{table}: {res.denied_reason}"
    _drain(conn)
    return key


def project_grain_is_grain(conn, *, source: str, table: str, now: datetime) -> None:
    """Land the VERIFIED grain onto ``graph_node.is_grain`` via the real SPECIALIZED_FACT projection
    (``project_table_facts_for_ref``), so the assembly physics' ``object_grain`` reads a governed
    grain — not an advisory file flag. Idempotent clear-then-set from ``resolve_fact``."""
    project_table_facts_for_ref(conn, source=source, table=table, now=now)


# ── authority: real projected BRIDGE (propose -> confirm -> project) ──────────────────────────────
def _column_ref(catalog: str, table: str, column: str) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=catalog, object_kind="column", schema=_SCHEMA,
                            table=table, column=column)


def verify_bridge(conn, *, entity_id: str, left: tuple[str, str, str], right: tuple[str, str, str],
                  service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
                  now: datetime, data_type_family: str = "string",
                  right_is_grain: bool = True) -> tuple[str, str]:
    """Propose -> confirm -> project a VERIFIED ``entity_bridge`` between two cross-catalog columns via
    the REAL commands (``propose_bridge`` service-proposes onto the overlay_fact spine, a platform-admin
    ``confirm_fact`` verifies under four-eyes, ``project_verified_bridge`` writes the active edge).
    ``left``/``right`` are ``(catalog, table, column)``. Returns ``(fact_key, projection_status)``."""
    left_ref = _column_ref(*left)
    right_ref = _column_ref(*right)
    candidate = BridgeCandidateV1(
        candidate_id=f"b-spike:{entity_id}:{left[0]}.{left[1]}.{left[2]}->{right[0]}.{right[1]}.{right[2]}",
        entity_id=entity_id, left_ref=left_ref, right_ref=right_ref,
        data_type_family=data_type_family, left_is_grain=False, right_is_grain=right_is_grain)
    propose_bridge(conn, candidate, actor=service_actor, now=now)
    bref = EntityBridgeRef(entity_id, left_ref, right_ref)
    key = fact_key(bref, "entity_bridge")
    target = _cas_target(fold_overlay_state(load_fact(conn, key)))
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": bref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target},
        human_actor, f"b-spike-confirm-bridge-{target}"))
    assert res.accepted, f"bridge confirm denied for {entity_id}: {res.denied_reason}"
    status = project_verified_bridge(conn, bref, now=now)
    return key, status


# ── drift watermark (freshness for the compile-end union / two-axis contract) ────────────────────
def set_fresh_watermark(conn, source: str, at: datetime) -> None:
    """A FRESH drift watermark so the compile-end union freshness resolves (the CONTRACT axis) and the
    grain projection re-serves. Not a governance shortcut — the same watermark the drift scan writes."""
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s,%s,'b_spike_drift',0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = EXCLUDED.last_completed_at", (source, at))


# ── server-derived request context (T3 seam) ─────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RequestContextV1:
    """The SERVER-derived trust inputs (a spike stand-in for T3's ``derive_request_context``): the
    authorized ``CatalogScopeV1`` and the confirmed non-null ``target_entity``. Caller injection of a
    catalog for a bare operand is never trusted downstream — the scope is the authorization boundary."""
    scope: CatalogScopeV1
    target_entity: str


def derive_request_context(*, authorized_catalogs: tuple[str, ...], target_entity: str,
                           resolved_at: str = "2026-07-21T00:00:00Z") -> RequestContextV1:
    """Build the authorized ``CatalogScopeV1`` + confirmed ``target_entity`` from the authenticated
    catalog roster. ``target_entity`` must be a non-empty confirmed entity."""
    assert target_entity, "confirmed target_entity must be non-empty"
    scope = CatalogScopeV1(
        scope_id="b-spike", authorized_catalog_sources=tuple(authorized_catalogs),
        catalog_state_stamps=(), omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at=resolved_at,
        catalog_consideration_truncated=False)
    return RequestContextV1(scope=scope, target_entity=target_entity)


# ── gauntlet + preservation (Slice-3 tri-state; run on the raw proposal) ─────────────────────────
@dataclass(frozen=True, slots=True)
class VetOutcomeV1:
    """The gauntlet result the spike carries forward: the vetted ``FeatureIdea`` (with its Slice-3
    ``validation_status`` + ``requirements``), the safety ``Rejection`` (``None`` on pass), and the
    raw/vetted PRESERVATION verdict — ``True`` iff every raw operand ``object_ref`` survived into the
    vetted operand set (no operand silently dropped or rewritten)."""
    idea: object | None
    rejection: object | None
    preserved: bool


def run_gauntlet_and_preserve(conn, *, raw: dict, known: set[str], src_of: dict[str, set[str]],
                              target_ref: str | None, now: datetime, fresh_within,
                              roles=()) -> VetOutcomeV1:
    """Run the existing ``_vet`` gauntlet (``feature_assist._validate_idea`` — the tri-state gauntlet
    ``_vet`` wraps: leakage / drift-freshness / read-scope / join authority + Slice-3 tri-state) on the
    RAW proposal, then a raw/vetted preservation check. A hard-fail returns the ``Rejection`` and
    ``preserved=False``; a pass returns the vetted ``FeatureIdea`` (carrying ``validation_status`` +
    ``requirements``) and whether every raw operand ``derives_from`` survived."""
    idea, rejection = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within,
                                     roles=roles)
    if rejection is not None or idea is None:
        return VetOutcomeV1(idea=None, rejection=rejection, preserved=False)
    vetted_refs = {ref for _cat, ref in idea.measure_refs}
    if idea.grain_ref is not None:
        vetted_refs.add(idea.grain_ref[1])
    if idea.time_ref is not None:
        vetted_refs.add(idea.time_ref[1])
    preserved = set(raw.get("derives_from", ())) <= vetted_refs
    return VetOutcomeV1(idea=idea, rejection=None, preserved=preserved)


# ── B normalization: resolve authority from the REAL confirmed state ─────────────────────────────
def resolve_confirmed_concept(conn, *, source: str, object_ref: str) -> str | None:
    """The operand's authoritative concept, RESOLVED from the real confirmed ``graph_node.concept``
    (populated by the human-confirmed field-evidence projection) — never a hard-coded literal."""
    row = conn.execute(
        "SELECT concept FROM graph_node WHERE catalog_source=%s AND object_ref=%s AND kind='column'",
        (source, object_ref)).fetchone()
    return row[0] if row is not None else None


def resolve_governed_source_binding(conn, adapter: CatalogAdapter, *, source: str, table: str,
                                    source_grain_entity: str,
                                    now: datetime) -> GovernedSourceBindingV1 | None:
    """The operand's ``GovernedSourceBindingV1`` derived from the REAL VERIFIED grain fact: the grain
    key columns (qualified to the table ref) + the deterministic grain ``fact_key`` A's source-endpoint
    revalidation checks. ``None`` when the source table has no VERIFIED grain fact (fail-closed)."""
    ref = table_ref(source, table)
    grain = resolve_fact(conn, adapter, ref, "grain", now=now)
    if grain.value is None:
        return None
    columns = grain.value.get("columns") or []
    if not columns:
        return None
    key_refs = tuple(f"{_SCHEMA}.{table}.{col}" for col in columns)
    return GovernedSourceBindingV1(
        source_grain_entity=source_grain_entity, source_grain_key_refs=key_refs,
        grain_fact_key=fact_key(ref, "grain"))


def build_single_operand_sum_intent(*, catalog_source: str, object_ref: str, concept: str,
                                    source_binding: GovernedSourceBindingV1,
                                    target_entity: str) -> MultiSourcePlannerIntentV1:
    """B-normalize the FIXED operand ``SUM(<measure>)`` into a single-operand
    ``MultiSourcePlannerIntentV1``: one MEASURE slot summed additively, IDENTITY final expression, no
    time/window (RECENCY/TREND deferred). ``concept`` is the real confirmed concept; ``source_binding``
    is the real VERIFIED-grain binding."""
    operand = OperandSlotV1(
        slot_id="m", semantic_role=SemanticRole.measure, catalog_source=catalog_source,
        object_ref=object_ref, authoritative_concept=concept,
        path_strategy=PathStrategyV1(
            aggregation=PathAggregation.sum, output_type="numeric",
            output_additivity=AdditivityClass.additive, external_type_required=False,
            ordering_anchor_concept=None),
        source_binding=source_binding)
    return MultiSourcePlannerIntentV1(
        target_entity=target_entity, operands=(operand,),
        final_expression=FinalExpressionV1(
            operation=FinalOperation.identity, ordered_slot_ids=("m",), time_slot_id=None,
            window=None, output_additivity=AdditivityClass.additive),
        operation_policy_version=OPERATION_POLICY_VERSION)


# ── two-axis disposition (T0 mapping) ────────────────────────────────────────────────────────────
def two_axis_disposition(result: MultiSourcePlanningResultV1) -> BDisposition:
    """Fold A's run-level result onto a ``BDisposition`` via the T0 two-axis mapping. ``governed`` iff
    BOTH the assembly axis AND the contract axis resolved with both winning ids set."""
    return map_a_outcome(result)
