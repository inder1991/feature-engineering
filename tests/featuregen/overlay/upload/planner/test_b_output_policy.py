"""Phase 3C.2b-i-B · Task 8 — governed output-field derivation (DB-backed).

Proves the fail-closed derivation of ``output_type`` / ``external_type_required`` / output additivity
from GOVERNED reads (never the flat display column alone). The demo/FTR path — a numeric column with
NO governed ``logical_representation`` — must land at ``external_type_required=True`` /
``output_type="unknown"`` (the honest NEEDS_EXTERNAL_VALIDATION), while a governed numeric type clears
it. Additivity is derived per aggregation so the result stays coherent with A's compile checker.
"""
from datetime import UTC, datetime
from types import SimpleNamespace

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.field_revalidation import flag_pending_revalidation
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.b_output_policy import (
    OUTPUT_POLICY_VERSION,
    OutputPolicyV1,
    derive_output_additivity,
    resolve_output_policy,
)
from featuregen.overlay.upload.planner.contracts import AdditivityClass
from featuregen.overlay.upload.planner.multisource_compile import _output_additivity_coherent
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalExpressionV1,
    FinalOperation,
    PathAggregation,
    PathStrategyV1,
)

_SRC = "b_ftr"
_TABLE = "txn"
_COL = "tran_amt"
_OBJ = f"public.{_TABLE}.{_COL}"
_REF = normalize_ref(_SRC, None, _TABLE, _COL)
_NOW = datetime(2026, 7, 22, tzinfo=UTC)


# ── seeding helpers (real ingest/governance writers) ─────────────────────────────────────────────
def _build_node(db, *, data_type="numeric"):
    """Create the column graph_node the same way ingest does (data_type is the OPERATIONAL value)."""
    build_graph(db, _SRC, [CanonicalRow(_SRC, _TABLE, _COL, data_type)])


def _govern_logical_representation(db, *, value="numeric"):
    """Make ``logical_representation`` GOVERNED via the real evidence writer at PARSER/SUPPORTED (an
    operational-rule signal) + resolve_and_project, so ``is_feature_eligible`` is True."""
    record_field_evidence(
        db, logical_ref=_REF, field_name="logical_representation", proposed_value=value,
        producer=EvidenceProducer.PARSER, strength=AssertionStrength.SUPPORTED,
        producer_ref="parser:b-t8", source_snapshot_id="b-t8-snap",
        input_hash=field_input_hash(
            logical_ref=_REF, field_name="logical_representation", material=value))
    resolve_and_project(db, source=_SRC, logical_refs=[_REF])


def _govern_additivity(db, *, value):
    """Make ``additivity`` GOVERNED via SOURCE/ATTESTED (a behavioural operational-rule signal)."""
    record_field_evidence(
        db, logical_ref=_REF, field_name="additivity", proposed_value=value,
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="source:b-t8", source_snapshot_id="b-t8-snap",
        input_hash=field_input_hash(logical_ref=_REF, field_name="additivity", material=value))
    resolve_and_project(db, source=_SRC, logical_refs=[_REF])


def _resolve(db, *, aggregation=PathAggregation.sum, concept_additivity=AdditivityClass.additive):
    return resolve_output_policy(
        db, catalog_source=_SRC, object_ref=_OBJ, aggregation=aggregation,
        concept_additivity=concept_additivity, now=_NOW)


# ── 7. ungoverned type (the real FTR / demo path) ────────────────────────────────────────────────
def test_ungoverned_type_requires_external_validation(db):
    _build_node(db, data_type="numeric")   # a NUMERIC hint value, but NO governing decision
    policy = _resolve(db)
    assert isinstance(policy, OutputPolicyV1)
    assert policy.external_type_required is True
    assert policy.output_type == "unknown"


# ── 8. governed numeric type ─────────────────────────────────────────────────────────────────────
def test_governed_numeric_type_is_not_external(db):
    _build_node(db, data_type="numeric")
    _govern_logical_representation(db, value="numeric")
    policy = _resolve(db)
    assert policy.external_type_required is False
    assert policy.output_type == "numeric"


def test_pending_revalidation_type_falls_back_to_external(db):
    """A governed type PENDING revalidation (projection-lag / material-change analog) is NOT-governed."""
    _build_node(db, data_type="numeric")
    _govern_logical_representation(db, value="numeric")
    flag_pending_revalidation(
        db, logical_ref=_REF, field_name="logical_representation",
        reason="material_changed", source_snapshot_id="b-t8-snap", now=_NOW)
    policy = _resolve(db)
    assert policy.external_type_required is True
    assert policy.output_type == "unknown"


# ── 9. additivity derivation (unit) ──────────────────────────────────────────────────────────────
def test_derive_output_additivity_table():
    a = AdditivityClass
    assert derive_output_additivity(a.additive, PathAggregation.sum) is a.additive
    assert derive_output_additivity(a.semi_additive, PathAggregation.sum) is a.non_additive
    assert derive_output_additivity(a.non_additive, PathAggregation.sum) is a.non_additive
    assert derive_output_additivity(a.unknown, PathAggregation.sum) is a.unknown
    assert derive_output_additivity(a.not_applicable, PathAggregation.sum) is a.unknown
    for base in (a.additive, a.semi_additive, a.non_additive, a.unknown, a.not_applicable):
        assert derive_output_additivity(base, PathAggregation.count) is a.additive
        assert derive_output_additivity(base, PathAggregation.count_distinct) is a.additive
    assert derive_output_additivity(a.additive, PathAggregation.min) is a.non_additive
    assert derive_output_additivity(a.additive, PathAggregation.max) is a.non_additive


# ── 10. governed additivity precedence over the passed concept additivity ────────────────────────
def test_governed_additivity_overrides_concept(db):
    _build_node(db, data_type="numeric")
    _govern_additivity(db, value="non_additive")
    # concept says additive, but the GOVERNED non_additive wins -> SUM(non_additive) -> non_additive.
    policy = _resolve(db, concept_additivity=AdditivityClass.additive)
    assert policy.output_additivity is AdditivityClass.non_additive


def test_ungoverned_additivity_falls_back_to_concept(db):
    _build_node(db, data_type="numeric")   # additivity NOT governed
    policy = _resolve(db, concept_additivity=AdditivityClass.additive)
    assert policy.output_additivity is AdditivityClass.additive   # SUM(concept additive) -> additive


# ── 11. coherence with A's compile checker ───────────────────────────────────────────────────────
def test_demo_sum_additive_is_coherent_with_a(db):
    _build_node(db, data_type="numeric")
    _govern_logical_representation(db, value="numeric")
    policy = _resolve(db, concept_additivity=AdditivityClass.additive)
    assert policy.output_additivity is AdditivityClass.additive
    # Feed the derived value into BOTH the path strategy and the single-operand IDENTITY expression
    # (as T9/the caller copies it) and assert A's REAL predicate accepts it.
    ps = PathStrategyV1(
        aggregation=PathAggregation.sum, output_type=policy.output_type,
        output_additivity=policy.output_additivity,
        external_type_required=policy.external_type_required, ordering_anchor_concept=None)
    fe = FinalExpressionV1(
        operation=FinalOperation.identity, ordered_slot_ids=("m",), time_slot_id=None,
        window=None, output_additivity=policy.output_additivity)
    plan = SimpleNamespace(final_expression=fe, operand_paths=(SimpleNamespace(path_strategy=ps),))
    assert _output_additivity_coherent(plan) is True


def test_output_policy_version_pinned():
    assert OUTPUT_POLICY_VERSION == "3c2bib.outpol.1.0.0"
