"""B1: persistence for the three-layer identity contracts and the composed digest chain
(migration 1134).

Step 3 built the identity contracts as code-only types. THIS module makes them durable, so B2's
binding chain can reference a logical plan, a physical execution plan, a render profile, a
generation configuration and a member output contract BY ID, and so the seven-stage digest chain
leaves a record at every composed stage.

**The digest is the content hash, and cannot drift from it.** Every layer's digest function in
:mod:`~featuregen.overlay.upload.planner.identity_chain` is exactly
``materialize_hash(contract.content_payload())``, so the published join point and the family's
``content_hash`` are one value; migration 1134 pins them equal with a named CHECK and pins
``revision_id = '<prefix>' || content_hash`` with another. The behavioural half is here: every
load REBUILDS the typed contract from the stored payload, RECOMPUTES the digest through step 3's
own function, and refuses a row that cannot reproduce its own identity
(:class:`IdentityStoreConflict`). A stored digest that could drift from its content is the defect
this whole chain exists to prevent, so the guard runs on the load path, not only at write.

**The layers are structurally independent (R2/R9).** A physical plan pins a logical DIGEST — a
value it copies, never a back-reference that could re-aim meaning. Persisting a second physical
plan against one logical plan touches no logical row; the suite pins that, including the bytes and
the ``recorded_at``.

**Provenance is recorded, never hashed (R9's staleness law).** Hypothesis text, planning-request
hash, chooser revision, menu hash and display text ride
:class:`~featuregen.overlay.upload.planner.logical_plan_v2.LogicalPlanProvenanceV1` and land in a
SEPARATE append-only table, many per plan: the same meaning reached from a different hypothesis is
the same feature (one plan row) and both hypotheses are kept. A provenance COLUMN would have kept
only the first writer's — the content-addressed insert is ``ON CONFLICT DO NOTHING``. A load that
names no provenance returns the EMPTY side-car: honest absence, never another caller's hypothesis
attached to a plan that never carried it.

**Rebuilding is the inverse of canonicalization.** ``*_from_payload`` here are the exact inverses
of step 3's ``content_payload()``/``identity_payload()``; they live in the persistence layer so the
step-3 contract modules stay untouched by this task. Because the canonical payload SORTS the
order-insensitive fields (operand bindings, selected parameters, policy identities, template
versions, engine settings, source bindings), a rebuilt contract is the CANONICAL REPRESENTATIVE of
its identity: same digest, same bytes, and equal to the original whenever the original was declared
in canonical order. Ordered fields (output grain, relationship path, segments, column pairs,
predicates) keep their order, because their order IS identity.

**Foreign keys: none, deliberately** (A4's platform discovery, applied a third time). An FK onto an
append-only table makes Postgres refuse a TRUNCATE with ``FeatureNotSupported`` BEFORE the table's
own BEFORE TRUNCATE raiser fires, so the append-only guard stops proving itself. Every table 1134
creates is append-only, so nothing carries an FK; in its place this store LOADS AND VERIFIES the
referenced row before writing (A4's "pins revisions that exist, never ones it would have to
invent"), and append-only rows never disappear, so a verified reference cannot decay.
``join_validation_policy_revision_id`` is stored with NO existence check: its store is 1136's
(B2/B2b) and does not exist yet — an honest unchecked pin, never a fabricated FK.

**Concurrency (the family's known caveat, unfixed by design).** The ensure/read-back idiom can
raise :class:`IdentityStoreConflict` under REPEATABLE READ when two writers race on a first-ever
row: the loser's ``ON CONFLICT DO NOTHING`` sees the winner's row as invisible and its read-back
finds nothing. Fail-noisy, never corrupting, and shared with A3/A4/B0a — matched here rather than
forked.

Store discipline: ``conn`` positional, everything else keyword-only; typed refusals
(:class:`IdentityPersistenceDefect`) BEFORE any SQL; content-addressed idempotency
(``ON CONFLICT DO NOTHING`` + content-verified read-back).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.bridge_realization import (
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
)
from featuregen.overlay.upload.bridge_store import _predicate_from_json
from featuregen.overlay.upload.execution_context import load_execution_context_revision
from featuregen.overlay.upload.planner.identity_chain import (
    build_compilation_digest,
    formula_binding_digest,
    generation_configuration_digest,
    logical_digest,
    member_compile_digest,
    member_execution_input_digest,
    physical_digest,
    render_digest,
    sealed_artifact_identity,
)
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    LOGICAL_PLAN_CONTRACT,
    LogicalFeaturePlanV2,
    LogicalOperandBindingV1,
    LogicalPlanProvenanceV1,
    LogicalRelationshipSegmentV1,
    LogicalTemporalJoinSemanticsV1,
)
from featuregen.overlay.upload.planner.physical_plan_v1 import (
    PHYSICAL_PLAN_CONTRACT,
    JoinKeyNormalizationPolicy,
    PhysicalExecutionPlanV1,
    PhysicalJoinSegmentV1,
    PhysicalTemporalJoinBindingV1,
)
from featuregen.overlay.upload.planner.render_profile import (
    GENERATION_CONFIGURATION_CONTRACT,
    MEMBER_OUTPUT_CONTRACT,
    RENDER_PROFILE_CONTRACT,
    GenerationConfigurationV1,
    MemberOutputContractV1,
    RenderProfileV1,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

if TYPE_CHECKING:
    from featuregen.contracts import DbConn

__all__ = [
    "GENERATION_CONFIGURATION_ID_PREFIX",
    "IDENTITY_DIGEST_ID_PREFIX",
    "IDENTITY_DIGEST_STAGES",
    "LOGICAL_PLAN_ID_PREFIX",
    "LOGICAL_PROVENANCE_ID_PREFIX",
    "MEMBER_OUTPUT_CONTRACT_ID_PREFIX",
    "PHYSICAL_PLAN_ID_PREFIX",
    "RENDER_PROFILE_ID_PREFIX",
    "STAGE_BUILD_COMPILATION",
    "STAGE_FORMULA_BINDING",
    "STAGE_MEMBER_COMPILE",
    "STAGE_MEMBER_EXECUTION_INPUT",
    "STAGE_SEALED_ARTIFACT",
    "IdentityDigestRecordV1",
    "IdentityPersistenceDefect",
    "IdentityStoreConflict",
    "LogicalPlanRecordV1",
    "ensure_generation_configuration",
    "ensure_logical_feature_plan",
    "ensure_member_output_contract",
    "ensure_physical_execution_plan",
    "ensure_render_profile",
    "generation_configuration_from_payload",
    "load_generation_configuration",
    "load_identity_digest_record",
    "load_logical_feature_plan",
    "load_logical_plan_provenance",
    "load_member_output_contract",
    "load_physical_execution_plan",
    "load_render_profile",
    "logical_plan_from_payload",
    "logical_plan_provenance_from_payload",
    "logical_plan_provenance_ids",
    "member_output_contract_from_payload",
    "physical_plan_from_payload",
    "record_identity_digest",
    "render_profile_from_payload",
    "resolve_identity_digest",
]

#: Deterministic id prefixes (the ``ecx_``/``brsnap_``/``dtp_``/``jvp_`` family).
LOGICAL_PLAN_ID_PREFIX = "lfp_"
LOGICAL_PROVENANCE_ID_PREFIX = "lpp_"
PHYSICAL_PLAN_ID_PREFIX = "pxp_"
RENDER_PROFILE_ID_PREFIX = "rpf_"
GENERATION_CONFIGURATION_ID_PREFIX = "gcf_"
MEMBER_OUTPUT_CONTRACT_ID_PREFIX = "moc_"
IDENTITY_DIGEST_ID_PREFIX = "idg_"

#: The CLOSED chain-stage vocabulary — the five COMPOSED stages of the seven-stage chain. Stage 1
#: (``logical_digest``) is a layer, not a composition, and stage 6 (``project_digest``) is minted
#: by ``materialize/generate_v2.py`` over the actually-rendered files; neither is recorded here.
#: Widening this tuple means widening migration 1134's named CHECK — a new migration, and a review
#: gate.
STAGE_FORMULA_BINDING = "formula_binding"
STAGE_MEMBER_EXECUTION_INPUT = "member_execution_input"
STAGE_MEMBER_COMPILE = "member_compile"
STAGE_BUILD_COMPILATION = "build_compilation"
STAGE_SEALED_ARTIFACT = "sealed_artifact"
IDENTITY_DIGEST_STAGES: tuple[str, ...] = (
    STAGE_FORMULA_BINDING,
    STAGE_MEMBER_EXECUTION_INPUT,
    STAGE_MEMBER_COMPILE,
    STAGE_BUILD_COMPILATION,
    STAGE_SEALED_ARTIFACT,
)

_PROVENANCE_CONTRACT = "logical_plan_provenance_v1"
_PROVENANCE_RECORD_CONTRACT = "logical_plan_provenance_record_v1"
_DIGEST_RECORD_CONTRACT = "identity_digest_record_v1"

_HEX_DIGITS = frozenset("0123456789abcdef")


class IdentityPersistenceDefect(ValueError):
    """A refused persistence request: a foreign type, a malformed payload, an unknown chain stage,
    a stage whose inputs are wrong, or a pin naming something that was never persisted — raised
    BEFORE any write."""


class IdentityStoreConflict(RuntimeError):
    """The store and the table disagree — a row that cannot rebuild its own contract, cannot
    reproduce its own digest, or an ensure whose read-back found nothing. Corruption, never
    served."""


# ──────────────────────────────────────────────────────────────────────────────────────────────
# rebuilding: the exact inverse of step 3's canonical payloads
# ──────────────────────────────────────────────────────────────────────────────────────────────
def _mapping(raw: object, *, what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise IdentityPersistenceDefect(f"{what} must be a JSON object, got {type(raw).__name__}")
    return raw


def _contract(payload: Mapping[str, Any], expected: str, *, what: str) -> Mapping[str, Any]:
    if payload.get("contract") != expected:
        raise IdentityPersistenceDefect(
            f"{what} must carry contract {expected!r}, got {payload.get('contract')!r} — a payload "
            "is rebuilt as the contract it declares, never as the one a caller hoped for")
    return payload


def _field(payload: Mapping[str, Any], key: str, *, what: str) -> Any:
    if key not in payload:
        raise IdentityPersistenceDefect(f"{what} is missing required field {key!r}")
    return payload[key]


def _sequence(raw: object, *, what: str) -> Sequence[Any]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise IdentityPersistenceDefect(f"{what} must be a JSON array")
    return raw


def _pair(raw: object, *, what: str) -> tuple[Any, Any]:
    items = _sequence(raw, what=what)
    if len(items) != 2:
        raise IdentityPersistenceDefect(f"{what} must be a two-element array")
    return items[0], items[1]


def logical_plan_provenance_from_payload(payload: Mapping[str, Any]) -> LogicalPlanProvenanceV1:
    """Rebuild the provenance side-car. Absent fields stay empty — provenance is never invented."""
    payload = _contract(_mapping(payload, what="provenance payload"), _PROVENANCE_CONTRACT,
                        what="provenance payload")
    return LogicalPlanProvenanceV1(
        hypothesis_text=payload.get("hypothesis_text", ""),
        planning_request_hash=payload.get("planning_request_hash", ""),
        chooser_revision_id=payload.get("chooser_revision_id", ""),
        menu_content_hash=payload.get("menu_content_hash", ""),
        display_text=payload.get("display_text", ""),
    )


def _provenance_payload(provenance: LogicalPlanProvenanceV1) -> dict[str, Any]:
    return {
        "contract": _PROVENANCE_CONTRACT,
        "hypothesis_text": provenance.hypothesis_text,
        "planning_request_hash": provenance.planning_request_hash,
        "chooser_revision_id": provenance.chooser_revision_id,
        "menu_content_hash": provenance.menu_content_hash,
        "display_text": provenance.display_text,
    }


def _temporal_semantics_from_payload(payload: Mapping[str, Any]) -> LogicalTemporalJoinSemanticsV1:
    payload = _mapping(payload, what="temporal semantics payload")
    return LogicalTemporalJoinSemanticsV1(
        effective_time_basis=_field(payload, "effective_time_basis", what="temporal semantics"),
        knowledge_time_basis=_field(payload, "knowledge_time_basis", what="temporal semantics"),
        driving_time_role=_field(payload, "driving_time_role", what="temporal semantics"),
        interval_boundary_policy=_field(
            payload, "interval_boundary_policy", what="temporal semantics"),
        unmatched_row_meaning=_field(payload, "unmatched_row_meaning", what="temporal semantics"),
        static_link_meaning=_field(payload, "static_link_meaning", what="temporal semantics"),
    )


def logical_plan_from_payload(
    payload: Mapping[str, Any],
    *,
    provenance: LogicalPlanProvenanceV1 | None = None,
) -> LogicalFeaturePlanV2:
    """Rebuild a :class:`LogicalFeaturePlanV2` from its canonical payload.

    ``provenance`` is supplied separately because it never appears in the payload (R9); omitting it
    yields the EMPTY side-car."""
    payload = _contract(_mapping(payload, what="logical plan payload"), LOGICAL_PLAN_CONTRACT,
                        what="logical plan payload")
    what = "logical plan payload"
    return LogicalFeaturePlanV2(
        canonical_definition_content_hash=_field(
            payload, "canonical_definition_content_hash", what=what),
        canonical_definition_revision_id=_field(
            payload, "canonical_definition_revision_id", what=what),
        operation=_field(payload, "operation", what=what),
        operand_bindings=tuple(
            LogicalOperandBindingV1(
                role=_field(_mapping(binding, what="operand binding"), "role",
                            what="operand binding"),
                logical_column_ref=_field(binding, "logical_column_ref", what="operand binding"),
                governed_semantic_revision_id=_field(
                    binding, "governed_semantic_revision_id", what="operand binding"),
            )
            for binding in _sequence(_field(payload, "operand_bindings", what=what),
                                     what="operand_bindings")),
        output_grain_key_refs=tuple(
            _sequence(_field(payload, "output_grain_key_refs", what=what),
                      what="output_grain_key_refs")),
        selected_parameters=tuple(
            _pair(item, what="selected parameter")
            for item in _sequence(_field(payload, "selected_parameters", what=what),
                                  what="selected_parameters")),
        relationship_path=tuple(
            LogicalRelationshipSegmentV1(
                left_endpoint_refs=tuple(_sequence(
                    _field(_mapping(segment, what="relationship segment"), "left_endpoint_refs",
                           what="relationship segment"),
                    what="left_endpoint_refs")),
                right_endpoint_refs=tuple(_sequence(
                    _field(segment, "right_endpoint_refs", what="relationship segment"),
                    what="right_endpoint_refs")),
                temporal_semantics=_temporal_semantics_from_payload(
                    _field(segment, "temporal_semantics", what="relationship segment")),
            )
            for segment in _sequence(_field(payload, "relationship_path", what=what),
                                     what="relationship_path")),
        formula_policy_identities=tuple(
            _pair(item, what="formula policy identity")
            for item in _sequence(_field(payload, "formula_policy_identities", what=what),
                                  what="formula_policy_identities")),
        provenance=provenance if provenance is not None else LogicalPlanProvenanceV1(),
    )


def _cardinality_from_payload(raw: object) -> DirectionalCardinalityVerdictV1:
    """``unknown`` is spelled ``DirectionalCardinalityVerdictV1.unknown()``, never ``None``."""
    if raw == "unknown":
        return DirectionalCardinalityVerdictV1.unknown()
    if not isinstance(raw, str):
        raise IdentityPersistenceDefect(f"directional_cardinality must be a string, got {raw!r}")
    try:
        return DirectionalCardinalityVerdictV1(Cardinality(raw))
    except ValueError as exc:
        raise IdentityPersistenceDefect(
            f"directional_cardinality {raw!r} is not 'unknown' or a settled Cardinality") from exc


def _normalization_from_payload(payload: Mapping[str, Any]) -> JoinKeyNormalizationPolicy:
    payload = _mapping(payload, what="key normalization payload")
    what = "key normalization payload"
    return JoinKeyNormalizationPolicy(
        whitespace=_field(payload, "whitespace", what=what),
        case_handling=_field(payload, "case_handling", what=what),
        leading_zeros=_field(payload, "leading_zeros", what=what),
        declared_type_coercions=tuple(
            _pair(item, what="declared type coercion")
            for item in _sequence(_field(payload, "declared_type_coercions", what=what),
                                  what="declared_type_coercions")),
        blank_key_behavior=_field(payload, "blank_key_behavior", what=what),
        nulls_never_match=_field(payload, "nulls_never_match", what=what),
        composite_key_ordering=_field(payload, "composite_key_ordering", what=what),
    )


def _temporal_binding_from_payload(payload: Mapping[str, Any]) -> PhysicalTemporalJoinBindingV1:
    payload = _mapping(payload, what="temporal binding payload")
    what = "temporal binding payload"
    return PhysicalTemporalJoinBindingV1(
        dataset_temporal_policy_revision_id=_field(
            payload, "dataset_temporal_policy_revision_id", what=what),
        effective_from_column_ref=_field(payload, "effective_from_column_ref", what=what),
        effective_to_column_ref=_field(payload, "effective_to_column_ref", what=what),
        availability_or_knowledge_time_column_ref=_field(
            payload, "availability_or_knowledge_time_column_ref", what=what),
        cutoff_parameter_ref=_field(payload, "cutoff_parameter_ref", what=what),
        source_binding_revision_id=_field(payload, "source_binding_revision_id", what=what),
        tie_break_column_refs=tuple(
            _sequence(_field(payload, "tie_break_column_refs", what=what),
                      what="tie_break_column_refs")),
    )


def _segment_from_payload(payload: Mapping[str, Any]) -> PhysicalJoinSegmentV1:
    payload = _mapping(payload, what="physical join segment payload")
    what = "physical join segment payload"
    return PhysicalJoinSegmentV1(
        realization_revision_id=_field(payload, "realization_revision_id", what=what),
        column_pairs=tuple(
            ColumnPairV1(*_pair(item, what="column pair"))
            for item in _sequence(_field(payload, "column_pairs", what=what),
                                  what="column_pairs")),
        # The bridge package owns its predicate vocabulary; its parser is reused, never forked.
        predicates=tuple(
            _predicate_from_json(dict(_mapping(item, what="predicate")))
            for item in _sequence(_field(payload, "predicates", what=what), what="predicates")),
        directional_cardinality=_cardinality_from_payload(
            _field(payload, "directional_cardinality", what=what)),
        realization_content_hash=_field(payload, "realization_content_hash", what=what),
        realization_dependency_hash=_field(payload, "realization_dependency_hash", what=what),
        key_normalization=_normalization_from_payload(
            _field(payload, "key_normalization", what=what)),
        temporal_binding=_temporal_binding_from_payload(
            _field(payload, "temporal_binding", what=what)),
    )


def physical_plan_from_payload(payload: Mapping[str, Any]) -> PhysicalExecutionPlanV1:
    """Rebuild a :class:`PhysicalExecutionPlanV1` from its canonical payload."""
    payload = _contract(_mapping(payload, what="physical plan payload"), PHYSICAL_PLAN_CONTRACT,
                        what="physical plan payload")
    what = "physical plan payload"
    return PhysicalExecutionPlanV1(
        # The canonical payload spells the pinned logical identity `logical_digest`.
        logical_digest_ref=_field(payload, "logical_digest", what=what),
        execution_context_revision_id=_field(payload, "execution_context_revision_id", what=what),
        source_binding_revisions=tuple(
            _pair(item, what="source binding revision")
            for item in _sequence(_field(payload, "source_binding_revisions", what=what),
                                  what="source_binding_revisions")),
        segments=tuple(
            _segment_from_payload(item)
            for item in _sequence(_field(payload, "segments", what=what), what="segments")),
        join_validation_policy_revision_id=_field(
            payload, "join_validation_policy_revision_id", what=what),
    )


def render_profile_from_payload(payload: Mapping[str, Any]) -> RenderProfileV1:
    """Rebuild a :class:`RenderProfileV1` from its canonical payload."""
    payload = _contract(_mapping(payload, what="render profile payload"), RENDER_PROFILE_CONTRACT,
                        what="render profile payload")
    what = "render profile payload"
    return RenderProfileV1(
        engine=_field(payload, "engine", what=what),
        compiler_version=_field(payload, "compiler_version", what=what),
        renderer_version=_field(payload, "renderer_version", what=what),
        template_versions=tuple(
            _pair(item, what="template version")
            for item in _sequence(_field(payload, "template_versions", what=what),
                                  what="template_versions")),
    )


def generation_configuration_from_payload(
    payload: Mapping[str, Any],
) -> GenerationConfigurationV1:
    """Rebuild a :class:`GenerationConfigurationV1` from its canonical payload."""
    payload = _contract(_mapping(payload, what="generation configuration payload"),
                        GENERATION_CONFIGURATION_CONTRACT,
                        what="generation configuration payload")
    what = "generation configuration payload"
    return GenerationConfigurationV1(
        population_spine_ref=_field(payload, "population_spine_ref", what=what),
        target_mode=_field(payload, "target_mode", what=what),
        target_ref=_field(payload, "target_ref", what=what),
        cadence=_field(payload, "cadence", what=what),
        physical_type_policy=_field(payload, "physical_type_policy", what=what),
        policy_realization_revision_ids=tuple(
            _sequence(_field(payload, "policy_realization_revision_ids", what=what),
                      what="policy_realization_revision_ids")),
        engine_settings=tuple(
            _pair(item, what="engine setting")
            for item in _sequence(_field(payload, "engine_settings", what=what),
                                  what="engine_settings")),
    )


def member_output_contract_from_payload(payload: Mapping[str, Any]) -> MemberOutputContractV1:
    """Rebuild a :class:`MemberOutputContractV1` from its canonical payload."""
    payload = _contract(_mapping(payload, what="member output contract payload"),
                        MEMBER_OUTPUT_CONTRACT, what="member output contract payload")
    what = "member output contract payload"
    return MemberOutputContractV1(
        output_feature_name=_field(payload, "output_feature_name", what=what),
        output_column_name=_field(payload, "output_column_name", what=what),
        empty_window_value=_field(payload, "empty_window_value", what=what),
        not_applicable_value=_field(payload, "not_applicable_value", what=what),
        null_input_behavior=_field(payload, "null_input_behavior", what=what),
        physical_type=_field(payload, "physical_type", what=what),
        decimal_scale=_field(payload, "decimal_scale", what=what),
        rounding_policy=_field(payload, "rounding_policy", what=what),
        overflow_policy=_field(payload, "overflow_policy", what=what),
    )


# ──────────────────────────────────────────────────────────────────────────────────────────────
# the layer stores
# ──────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class LogicalPlanRecordV1:
    """What a logical-plan write produced: the content-addressed plan row and — when the caller
    carried one — the provenance record minted beside it. ``provenance_id`` is ``None`` for an
    empty side-car: nothing was recorded, and nothing is invented."""

    revision_id: str
    logical_digest: str
    provenance_id: str | None


def _instance(value: object, expected: type, *, what: str) -> Any:
    if not isinstance(value, expected):
        raise IdentityPersistenceDefect(
            f"{what} must be a {expected.__name__}, got {type(value).__name__}")
    return value


def _rebuild(loader, payload, *, revision_id: str, what: str):
    """Rebuild a stored payload into its typed contract, turning any malformed row into store
    corruption — a row that cannot become its own contract is never served."""
    try:
        return loader(payload)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise IdentityStoreConflict(
            f"{what} {revision_id} could not be rebuilt from its stored payload: {exc}") from exc


def _verify(*, revision_id: str, prefix: str, recomputed: str, stored_digest: str,
            stored_hash: str, stored_content: Any, rebuilt_content: Any, what: str) -> None:
    """The drift guard, in the one order that makes each failure legible: the DIGEST first (the
    join point disagreeing with its content is the defect this chain exists to prevent), then the
    canonical bytes, then the id."""
    if recomputed != stored_digest:
        raise IdentityStoreConflict(
            f"{what} {revision_id} has a stored digest that its stored content does not produce "
            f"(stored {stored_digest}, recomputed {recomputed}) — a digest that can drift from "
            "its content is never served")
    if stored_content != rebuilt_content:
        raise IdentityStoreConflict(
            f"{what} {revision_id} is not stored in its canonical serialization")
    if stored_hash != recomputed or revision_id != f"{prefix}{recomputed}":
        raise IdentityStoreConflict(f"{what} {revision_id} fails content verification")


def ensure_logical_feature_plan(conn: DbConn, *,
                                plan: LogicalFeaturePlanV2) -> LogicalPlanRecordV1:
    """Persist one logical plan (R9's MEANING) and, beside it, its provenance side-car.

    Content-addressed: the same meaning always answers the SAME ``revision_id`` and never a second
    row, whatever hypothesis reached it. Each distinct provenance is kept as its own append-only
    record — R9's law is that provenance never rekeys, not that it is discarded."""
    _instance(plan, LogicalFeaturePlanV2, what="plan")
    content = plan.content_payload()
    digest = logical_digest(plan)
    revision_id = f"{LOGICAL_PLAN_ID_PREFIX}{digest}"
    conn.execute(
        "INSERT INTO logical_feature_plan_revision "
        "  (revision_id, logical_digest, content, content_hash) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (revision_id, digest, Jsonb(content), digest))
    if load_logical_feature_plan(conn, revision_id) is None:
        raise IdentityStoreConflict(f"logical feature plan {revision_id} did not persist")

    provenance_id: str | None = None
    if plan.provenance != LogicalPlanProvenanceV1():
        provenance_payload = _provenance_payload(plan.provenance)
        provenance_hash = materialize_hash({
            "contract": _PROVENANCE_RECORD_CONTRACT,
            "revision_id": revision_id,
            "provenance": provenance_payload,
        })
        provenance_id = f"{LOGICAL_PROVENANCE_ID_PREFIX}{provenance_hash}"
        conn.execute(
            "INSERT INTO logical_plan_provenance_record "
            "  (provenance_id, revision_id, content, content_hash) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (provenance_id) DO NOTHING",
            (provenance_id, revision_id, Jsonb(provenance_payload), provenance_hash))
        if load_logical_plan_provenance(conn, provenance_id) is None:
            raise IdentityStoreConflict(
                f"logical plan provenance {provenance_id} did not persist")
    return LogicalPlanRecordV1(revision_id=revision_id, logical_digest=digest,
                               provenance_id=provenance_id)


def load_logical_feature_plan(conn: DbConn, revision_id: str, *,
                              provenance_id: str | None = None) -> LogicalFeaturePlanV2 | None:
    """Load and CONTENT-VERIFY one logical plan; ``None`` when absent, corruption raises.

    ``provenance_id`` attaches one recorded side-car (it must belong to THIS plan); omitting it
    yields the empty side-car — honest absence, never another caller's hypothesis."""
    row = conn.execute(
        "SELECT logical_digest, content, content_hash FROM logical_feature_plan_revision "
        "WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    provenance = LogicalPlanProvenanceV1()
    if provenance_id is not None:
        stored = conn.execute(
            "SELECT revision_id FROM logical_plan_provenance_record WHERE provenance_id = %s",
            (provenance_id,)).fetchone()
        if stored is None:
            raise IdentityStoreConflict(
                f"logical plan provenance {provenance_id} does not exist")
        if stored[0] != revision_id:
            raise IdentityStoreConflict(
                f"logical plan provenance {provenance_id} belongs to {stored[0]}, not "
                f"{revision_id} — provenance is loaded against the plan it was recorded for")
        provenance = load_logical_plan_provenance(conn, provenance_id)
    plan = _rebuild(lambda payload: logical_plan_from_payload(payload, provenance=provenance),
                    row[1], revision_id=revision_id, what="logical feature plan")
    _verify(revision_id=revision_id, prefix=LOGICAL_PLAN_ID_PREFIX,
            recomputed=logical_digest(plan), stored_digest=row[0], stored_hash=row[2],
            stored_content=row[1], rebuilt_content=plan.content_payload(),
            what="logical feature plan")
    return plan


def load_logical_plan_provenance(conn: DbConn,
                                 provenance_id: str) -> LogicalPlanProvenanceV1 | None:
    """Load one provenance side-car; ``None`` when absent, corruption raises."""
    row = conn.execute(
        "SELECT revision_id, content, content_hash FROM logical_plan_provenance_record "
        "WHERE provenance_id = %s", (provenance_id,)).fetchone()
    if row is None:
        return None
    provenance = _rebuild(logical_plan_provenance_from_payload, row[1],
                          revision_id=provenance_id, what="logical plan provenance")
    recomputed = materialize_hash({
        "contract": _PROVENANCE_RECORD_CONTRACT,
        "revision_id": row[0],
        "provenance": _provenance_payload(provenance),
    })
    if recomputed != row[2] or provenance_id != f"{LOGICAL_PROVENANCE_ID_PREFIX}{recomputed}":
        raise IdentityStoreConflict(
            f"logical plan provenance {provenance_id} fails content verification")
    return provenance


def logical_plan_provenance_ids(conn: DbConn, revision_id: str) -> tuple[str, ...]:
    """Every provenance recorded against one plan, in a stable order — the several hypotheses that
    reached ONE feature."""
    rows = conn.execute(
        "SELECT provenance_id FROM logical_plan_provenance_record WHERE revision_id = %s "
        "ORDER BY provenance_id", (revision_id,)).fetchall()
    return tuple(str(row[0]) for row in rows)


def ensure_physical_execution_plan(conn: DbConn, *, plan: PhysicalExecutionPlanV1) -> str:
    """Persist one physical execution plan (R2's HOW) and return its ``revision_id``.

    Store validation stands in for the foreign keys this substrate cannot carry: the pinned logical
    plan and the pinned execution-context revision must both already exist — a physical plan
    realizing a meaning nobody persisted, or adopted in a context nobody minted, is a dangling
    identity. Writing it touches NO logical row (R2/R9)."""
    _instance(plan, PhysicalExecutionPlanV1, what="plan")
    if load_logical_feature_plan(
            conn, f"{LOGICAL_PLAN_ID_PREFIX}{plan.logical_digest_ref}") is None:
        raise IdentityPersistenceDefect(
            f"the logical plan {plan.logical_digest_ref} this physical plan realizes was never "
            "persisted — a physical plan pins a meaning that exists, never one it would have to "
            "invent")
    if load_execution_context_revision(conn, plan.execution_context_revision_id) is None:
        raise IdentityPersistenceDefect(
            f"the execution context {plan.execution_context_revision_id!r} was never persisted "
            "(migration 1130 / task A3 owns it) — a physical plan pins the exact context it was "
            "adopted in")
    content = plan.content_payload()
    digest = physical_digest(plan)
    revision_id = f"{PHYSICAL_PLAN_ID_PREFIX}{digest}"
    conn.execute(
        "INSERT INTO physical_execution_plan_revision "
        "  (revision_id, physical_digest, logical_digest, execution_context_revision_id, "
        "   join_validation_policy_revision_id, content, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (revision_id) DO NOTHING",
        (revision_id, digest, plan.logical_digest_ref, plan.execution_context_revision_id,
         plan.join_validation_policy_revision_id, Jsonb(content), digest))
    if load_physical_execution_plan(conn, revision_id) is None:
        raise IdentityStoreConflict(f"physical execution plan {revision_id} did not persist")
    return revision_id


def load_physical_execution_plan(conn: DbConn,
                                 revision_id: str) -> PhysicalExecutionPlanV1 | None:
    """Load and CONTENT-VERIFY one physical execution plan; ``None`` when absent."""
    row = conn.execute(
        "SELECT physical_digest, content, content_hash FROM physical_execution_plan_revision "
        "WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    plan = _rebuild(physical_plan_from_payload, row[1], revision_id=revision_id,
                    what="physical execution plan")
    _verify(revision_id=revision_id, prefix=PHYSICAL_PLAN_ID_PREFIX,
            recomputed=physical_digest(plan), stored_digest=row[0], stored_hash=row[2],
            stored_content=row[1], rebuilt_content=plan.content_payload(),
            what="physical execution plan")
    return plan


def ensure_render_profile(conn: DbConn, *, profile: RenderProfileV1) -> str:
    """Persist one render profile and return its ``revision_id``."""
    _instance(profile, RenderProfileV1, what="profile")
    digest = render_digest(profile)
    revision_id = f"{RENDER_PROFILE_ID_PREFIX}{digest}"
    conn.execute(
        "INSERT INTO render_profile_revision "
        "  (revision_id, render_profile_digest, content, content_hash) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (revision_id, digest, Jsonb(profile.content_payload()), digest))
    if load_render_profile(conn, revision_id) is None:
        raise IdentityStoreConflict(f"render profile {revision_id} did not persist")
    return revision_id


def load_render_profile(conn: DbConn, revision_id: str) -> RenderProfileV1 | None:
    """Load and CONTENT-VERIFY one render profile; ``None`` when absent."""
    row = conn.execute(
        "SELECT render_profile_digest, content, content_hash FROM render_profile_revision "
        "WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    profile = _rebuild(render_profile_from_payload, row[1], revision_id=revision_id,
                       what="render profile")
    _verify(revision_id=revision_id, prefix=RENDER_PROFILE_ID_PREFIX,
            recomputed=render_digest(profile), stored_digest=row[0], stored_hash=row[2],
            stored_content=row[1], rebuilt_content=profile.content_payload(),
            what="render profile")
    return profile


def ensure_generation_configuration(conn: DbConn, *,
                                    configuration: GenerationConfigurationV1) -> str:
    """Persist one build-scoped generation configuration and return its ``revision_id``."""
    _instance(configuration, GenerationConfigurationV1, what="configuration")
    digest = generation_configuration_digest(configuration)
    revision_id = f"{GENERATION_CONFIGURATION_ID_PREFIX}{digest}"
    conn.execute(
        "INSERT INTO generation_configuration_revision "
        "  (revision_id, generation_configuration_digest, content, content_hash) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (revision_id) DO NOTHING",
        (revision_id, digest, Jsonb(configuration.content_payload()), digest))
    if load_generation_configuration(conn, revision_id) is None:
        raise IdentityStoreConflict(f"generation configuration {revision_id} did not persist")
    return revision_id


def load_generation_configuration(conn: DbConn,
                                  revision_id: str) -> GenerationConfigurationV1 | None:
    """Load and CONTENT-VERIFY one generation configuration; ``None`` when absent."""
    row = conn.execute(
        "SELECT generation_configuration_digest, content, content_hash "
        "FROM generation_configuration_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    if row is None:
        return None
    configuration = _rebuild(generation_configuration_from_payload, row[1],
                             revision_id=revision_id, what="generation configuration")
    _verify(revision_id=revision_id, prefix=GENERATION_CONFIGURATION_ID_PREFIX,
            recomputed=generation_configuration_digest(configuration), stored_digest=row[0],
            stored_hash=row[2], stored_content=row[1],
            rebuilt_content=configuration.content_payload(),
            what="generation configuration")
    return configuration


def ensure_member_output_contract(conn: DbConn, *, contract: MemberOutputContractV1) -> str:
    """Persist one member output contract — the SINGLE owner of every per-feature output decision
    — and return its ``revision_id``.

    It carries no digest column of its own: the identity chain gives it no named digest; it rides
    ``member_execution_input_digest``. Its content hash IS its identity."""
    _instance(contract, MemberOutputContractV1, what="contract")
    content = contract.content_payload()
    content_hash = materialize_hash(content)
    revision_id = f"{MEMBER_OUTPUT_CONTRACT_ID_PREFIX}{content_hash}"
    conn.execute(
        "INSERT INTO member_output_contract_revision (revision_id, content, content_hash) "
        "VALUES (%s, %s, %s) ON CONFLICT (revision_id) DO NOTHING",
        (revision_id, Jsonb(content), content_hash))
    if load_member_output_contract(conn, revision_id) is None:
        raise IdentityStoreConflict(f"member output contract {revision_id} did not persist")
    return revision_id


def load_member_output_contract(conn: DbConn, revision_id: str) -> MemberOutputContractV1 | None:
    """Load and CONTENT-VERIFY one member output contract; ``None`` when absent."""
    row = conn.execute(
        "SELECT content, content_hash FROM member_output_contract_revision "
        "WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    contract = _rebuild(member_output_contract_from_payload, row[0], revision_id=revision_id,
                        what="member output contract")
    rebuilt = contract.content_payload()
    _verify(revision_id=revision_id, prefix=MEMBER_OUTPUT_CONTRACT_ID_PREFIX,
            recomputed=materialize_hash(rebuilt), stored_digest=row[1], stored_hash=row[1],
            stored_content=row[0], rebuilt_content=rebuilt, what="member output contract")
    return contract


# ──────────────────────────────────────────────────────────────────────────────────────────────
# the composed digest chain
# ──────────────────────────────────────────────────────────────────────────────────────────────
def _hex(inputs: Mapping[str, Any], key: str) -> str:
    value = inputs[key]
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX_DIGITS:
        raise IdentityPersistenceDefect(
            f"chain input {key!r} must be a 64-char lowercase sha256 hex digest, got {value!r}")
    return value


def _text_value(value: object, *, what: str) -> str:
    """Validate — never coerce — one non-empty string input, and STRIP it.

    Stripping here is not cosmetic: ``identity_chain``'s ``_non_empty`` strips before it hashes, so
    a stored input that kept its whitespace would produce the same digest under a different
    ``digest_id`` — one identity, two content-addressed rows, and the second write landing as a raw
    UNIQUE violation instead of a typed refusal. Every text input entering the chain goes through
    this one door."""
    if not isinstance(value, str) or not value.strip():
        raise IdentityPersistenceDefect(
            f"{what} must be a non-empty string, got {value!r}")
    return value.strip()


def _text(inputs: Mapping[str, Any], key: str) -> str:
    return _text_value(inputs[key], what=f"chain input {key!r}")


def _formula_binding_stage(inputs: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    payload = {
        "logical_digest": _hex(inputs, "logical_digest"),
        "formula_content_hash": _text(inputs, "formula_content_hash"),
        "formula_method_identity": _text(inputs, "formula_method_identity"),
    }
    return payload, formula_binding_digest(
        payload["logical_digest"], payload["formula_content_hash"],
        payload["formula_method_identity"])


def _member_execution_input_stage(inputs: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    contract = member_output_contract_from_payload(
        _mapping(inputs["member_output_contract"], what="member_output_contract"))
    payload = {
        "formula_binding_digest": _hex(inputs, "formula_binding_digest"),
        "physical_digest": _hex(inputs, "physical_digest"),
        "member_output_contract": contract.content_payload(),
    }
    return payload, member_execution_input_digest(
        payload["formula_binding_digest"], payload["physical_digest"], contract)


def _member_compile_stage(inputs: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    bindings = tuple(
        (_text_value(occurrence, what="policy occurrence ref"),
         _text_value(realization, what=f"policy realization for occurrence {occurrence!r}"))
        for occurrence, realization in (
            _pair(item, what="policy occurrence binding")
            for item in _sequence(inputs["policy_occurrence_bindings"],
                                  what="policy_occurrence_bindings")))
    payload = {
        "member_execution_input_digest": _hex(inputs, "member_execution_input_digest"),
        "ir_hash": _text(inputs, "ir_hash"),
        "policy_occurrence_bindings": [list(binding) for binding in bindings],
    }
    return payload, member_compile_digest(
        payload["member_execution_input_digest"], payload["ir_hash"], bindings)


def _build_compilation_stage(inputs: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    members = tuple(
        _hex({"member_compile_digest": item}, "member_compile_digest")
        for item in _sequence(inputs["ordered_member_compile_digests"],
                              what="ordered_member_compile_digests"))
    payload = {
        "target_and_spine_revision": _text(inputs, "target_and_spine_revision"),
        "ordered_member_compile_digests": list(members),          # ORDER is identity
        "generation_configuration_digest": _hex(inputs, "generation_configuration_digest"),
    }
    return payload, build_compilation_digest(
        payload["target_and_spine_revision"], members,
        payload["generation_configuration_digest"])


def _sealed_artifact_stage(inputs: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    payload = {
        "build_compilation_digest": _hex(inputs, "build_compilation_digest"),
        "render_profile_digest": _hex(inputs, "render_profile_digest"),
        # generate_v2 mints project_digest over the actually-rendered files; its format is its
        # owner's contract, so it is validated non-empty, never as this chain's hex.
        "project_digest": _text(inputs, "project_digest"),
    }
    return payload, sealed_artifact_identity(
        payload["build_compilation_digest"], payload["render_profile_digest"],
        payload["project_digest"])


#: stage -> (required input keys, the step-3 computation over them).
_STAGES: dict[str, tuple[frozenset[str], Any]] = {
    STAGE_FORMULA_BINDING: (
        frozenset({"logical_digest", "formula_content_hash", "formula_method_identity"}),
        _formula_binding_stage),
    STAGE_MEMBER_EXECUTION_INPUT: (
        frozenset({"formula_binding_digest", "physical_digest", "member_output_contract"}),
        _member_execution_input_stage),
    STAGE_MEMBER_COMPILE: (
        frozenset({"member_execution_input_digest", "ir_hash", "policy_occurrence_bindings"}),
        _member_compile_stage),
    STAGE_BUILD_COMPILATION: (
        frozenset({"target_and_spine_revision", "ordered_member_compile_digests",
                   "generation_configuration_digest"}),
        _build_compilation_stage),
    STAGE_SEALED_ARTIFACT: (
        frozenset({"build_compilation_digest", "render_profile_digest", "project_digest"}),
        _sealed_artifact_stage),
}


@dataclass(frozen=True, slots=True)
class IdentityDigestRecordV1:
    """One COMPOSED stage of the identity chain, mirroring the 1134 row.

    The record never ACCEPTS a digest: it computes one from ``inputs`` through step 3's own
    function, so a record whose digest disagrees with its inputs cannot be constructed at all.
    ``inputs`` are canonical and self-contained — the digest is recomputable from them with no
    further reads, which is what lets every load re-check the stored value."""

    stage: str
    inputs: Mapping[str, Any]
    recorded_at: datetime | None = None
    digest: str = field(init=False, default="")
    content_hash: str = field(init=False, default="")
    digest_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise IdentityPersistenceDefect(
                f"stage must be one of {list(IDENTITY_DIGEST_STAGES)} (the closed set of COMPOSED "
                f"chain stages), got {self.stage!r}")
        required, compute = _STAGES[self.stage]
        inputs = _mapping(self.inputs, what=f"{self.stage} inputs")
        present = frozenset(inputs)
        if present != required:
            raise IdentityPersistenceDefect(
                f"stage {self.stage!r} takes exactly the inputs {sorted(required)}; missing "
                f"{sorted(required - present)}, unexpected {sorted(present - required)} — a chain "
                "stage never hashes a field nobody declared, and never omits one")
        try:
            payload, digest = compute(inputs)
        except IdentityPersistenceDefect:
            raise
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise IdentityPersistenceDefect(
                f"stage {self.stage!r} inputs are malformed: {exc}") from exc
        object.__setattr__(self, "inputs", payload)
        object.__setattr__(self, "digest", digest)
        content_hash = materialize_hash({
            "contract": _DIGEST_RECORD_CONTRACT,
            "stage": self.stage,
            "digest": digest,
            "inputs": payload,
        })
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "digest_id", f"{IDENTITY_DIGEST_ID_PREFIX}{content_hash}")


def _require_loaded(loaded: object, *, what: str) -> None:
    """``None`` from a verifying loader means the pinned identity was never persisted.

    The loaders raise :class:`IdentityStoreConflict` on a row that cannot reproduce its own
    identity, so a corrupt predecessor STOPS the write rather than being pinned by it."""
    if loaded is None:
        raise IdentityPersistenceDefect(
            f"{what} was never persisted — a chain record pins identities that exist, never ones "
            "it would have to invent")


def _check_predecessors(conn: DbConn, record: IdentityDigestRecordV1) -> None:
    """Every input this store OWNS must already be persisted AND loadable. No foreign key can
    express it (the tables are append-only, and an FK would replace their TRUNCATE guard); a
    VERIFYING LOAD does, and does more than an FK could — it proves the referenced row can still
    rebuild its own contract and reproduce its own digest, so a chain never pins a row that could
    never be served.

    Every check addresses its row by the DERIVABLE PRIMARY KEY (``'<prefix>' || digest``, and for
    the chain records the unique ``(stage, digest)``), so these are index lookups, never scans over
    the digest columns.

    Externally minted identities — the formula content hash and method identity, the IR hash, the
    target/spine revision, ``project_digest`` — are NOT checked here: their owners are elsewhere,
    and inventing a check would be inventing an authority."""
    inputs = record.inputs
    if record.stage == STAGE_FORMULA_BINDING:
        _require_loaded(
            load_logical_feature_plan(
                conn, f"{LOGICAL_PLAN_ID_PREFIX}{inputs['logical_digest']}"),
            what=f"the logical plan {inputs['logical_digest']} (input 'logical_digest')")
    elif record.stage == STAGE_MEMBER_EXECUTION_INPUT:
        _require_loaded(
            resolve_identity_digest(conn, stage=STAGE_FORMULA_BINDING,
                                    digest=inputs["formula_binding_digest"]),
            what=(f"the formula binding {inputs['formula_binding_digest']} "
                  "(input 'formula_binding_digest')"))
        _require_loaded(
            load_physical_execution_plan(
                conn, f"{PHYSICAL_PLAN_ID_PREFIX}{inputs['physical_digest']}"),
            what=f"the physical plan {inputs['physical_digest']} (input 'physical_digest')")
        _require_loaded(
            load_member_output_contract(
                conn,
                f"{MEMBER_OUTPUT_CONTRACT_ID_PREFIX}"
                f"{materialize_hash(inputs['member_output_contract'])}"),
            what="the member output contract (input 'member_output_contract')")
    elif record.stage == STAGE_MEMBER_COMPILE:
        _require_loaded(
            resolve_identity_digest(conn, stage=STAGE_MEMBER_EXECUTION_INPUT,
                                    digest=inputs["member_execution_input_digest"]),
            what=(f"the member execution input {inputs['member_execution_input_digest']} "
                  "(input 'member_execution_input_digest')"))
    elif record.stage == STAGE_BUILD_COMPILATION:
        for digest in inputs["ordered_member_compile_digests"]:
            _require_loaded(
                resolve_identity_digest(conn, stage=STAGE_MEMBER_COMPILE, digest=digest),
                what=(f"the member compile {digest} (input "
                      "'ordered_member_compile_digests')"))
        _require_loaded(
            load_generation_configuration(
                conn,
                f"{GENERATION_CONFIGURATION_ID_PREFIX}"
                f"{inputs['generation_configuration_digest']}"),
            what=(f"the generation configuration {inputs['generation_configuration_digest']} "
                  "(input 'generation_configuration_digest')"))
    elif record.stage == STAGE_SEALED_ARTIFACT:
        _require_loaded(
            resolve_identity_digest(conn, stage=STAGE_BUILD_COMPILATION,
                                    digest=inputs["build_compilation_digest"]),
            what=(f"the build compilation {inputs['build_compilation_digest']} "
                  "(input 'build_compilation_digest')"))
        _require_loaded(
            load_render_profile(
                conn, f"{RENDER_PROFILE_ID_PREFIX}{inputs['render_profile_digest']}"),
            what=(f"the render profile {inputs['render_profile_digest']} "
                  "(input 'render_profile_digest')"))


def record_identity_digest(conn: DbConn, *, stage: str,
                           inputs: Mapping[str, Any]) -> IdentityDigestRecordV1:
    """Compute, persist and return ONE composed stage of the identity chain.

    The digest is COMPUTED here from the declared inputs — never accepted from a caller — so a
    stored digest cannot disagree with the content it summarizes. Content-addressed: the same
    inputs answer the same ``digest_id`` and one row."""
    record = IdentityDigestRecordV1(stage=stage, inputs=inputs)
    _check_predecessors(conn, record)
    conn.execute(
        "INSERT INTO identity_digest_record (digest_id, stage, digest, inputs, content_hash) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (digest_id) DO NOTHING",
        (record.digest_id, record.stage, record.digest, Jsonb(dict(record.inputs)),
         record.content_hash))
    stored = load_identity_digest_record(conn, record.digest_id)
    if stored is None:
        raise IdentityStoreConflict(f"identity digest record {record.digest_id} did not persist")
    return stored


def load_identity_digest_record(conn: DbConn, digest_id: str) -> IdentityDigestRecordV1 | None:
    """Load one chain record, RECOMPUTING its stage from the stored inputs; ``None`` when absent.

    This is the guard that makes a stored digest un-driftable: a row whose digest is not what its
    own inputs produce is corruption and is never served."""
    row = conn.execute(
        "SELECT stage, digest, inputs, content_hash, recorded_at FROM identity_digest_record "
        "WHERE digest_id = %s", (digest_id,)).fetchone()
    if row is None:
        return None
    return _verified_digest_record(row, digest_id=digest_id)


def resolve_identity_digest(conn: DbConn, *, stage: str,
                            digest: str) -> IdentityDigestRecordV1 | None:
    """Walk the chain BACKWARDS: the one record that produced this digest at this stage.

    ``UNIQUE (stage, digest)`` in 1134 is what makes "the one record" true — a digest can never
    name two rival input sets."""
    if stage not in _STAGES:
        raise IdentityPersistenceDefect(
            f"stage must be one of {list(IDENTITY_DIGEST_STAGES)}, got {stage!r}")
    row = conn.execute(
        "SELECT stage, digest, inputs, content_hash, recorded_at, digest_id "
        "FROM identity_digest_record WHERE stage = %s AND digest = %s",
        (stage, digest)).fetchone()
    if row is None:
        return None
    return _verified_digest_record(row, digest_id=str(row[5]))


def _verified_digest_record(row, *, digest_id: str) -> IdentityDigestRecordV1:
    stage, stored_digest, inputs, stored_hash, recorded_at = row[0], row[1], row[2], row[3], row[4]
    try:
        record = IdentityDigestRecordV1(stage=stage, inputs=inputs, recorded_at=recorded_at)
    except IdentityPersistenceDefect as exc:
        raise IdentityStoreConflict(
            f"identity digest record {digest_id} could not be rebuilt from its stored "
            f"inputs: {exc}") from exc
    if record.digest != stored_digest:
        raise IdentityStoreConflict(
            f"identity digest record {digest_id} has a stored digest that its stored inputs do "
            f"not produce (stored {stored_digest}, recomputed {record.digest}) — a digest that "
            "can drift from its inputs is never served")
    if record.content_hash != stored_hash or record.digest_id != digest_id:
        raise IdentityStoreConflict(
            f"identity digest record {digest_id} fails content verification")
    return record
