"""Spec §3.1–3.2 — the join ADAPTER: a governed ``JoinOutcome`` becomes a ``JoinPlan`` or a refusal.

This module owns no traversal. ``overlay.upload.join_path.classify_join_path`` is the governed
planner — the one place that knows which edges exist, which are backed by a VERIFIED
``approved_join`` fact, and which hops a reader's roles may see. A second opinion here (a private
search over ``graph_edge``, a scan for link tables, a name-prefix guess) would be free to disagree
with the answer governance approved, so there is none: this module calls the planner, then decides
whether the path it returned may be COMPUTED ON.

**Everything below is a refusal, and that is the point.** A join defect does not raise an error at
run time; it produces a number. A duplicated transaction row inflates a SUM, and the report still
renders. So each rule here refuses a plan that could be *silently wrong*:

1. ``UNVERIFIED`` / ``DENIED`` / ``NO_PATH`` — three distinct governed outcomes, three distinct
   codes. ``find_join_path`` collapses all three to ``None``, which would tell an operator to fix
   the wrong thing: grant a role, get a join approved, or model a path that does not exist.
2. **Unknown cardinality is refused.** ``graph_edge.cardinality`` is nullable and ``JoinStep``
   carries ``str | None``. Refusing only the hops that SAY ``1:N`` is a fail-open: an unattested
   edge may BE ``1:N``. "We do not know" is not "it is safe".
3. **Fan-out is refused, never repaired.** Row de-duplication and pre-aggregation are not
   equivalent to each other, they differ per operation (SUM vs COUNT DISTINCT vs RATIO), and — the
   decisive reason — a joint account whose transaction is attributable to two customers is a
   BUSINESS ALLOCATION decision. Encoding one here would silently pick an allocation rule nobody
   approved. Until a governed allocation policy exists, refusal is the honest outcome (§3.2).
4. **Two physical schemas sharing a table name are refused.** The planner indexes nodes by BARE
   table name, so two physical tables with one name collapse into a single node and a path can be
   stitched through the wrong table.

**Why this takes resolved physical identities.** Logical refs are schema-flattened: ``build_graph``
writes every ``object_ref`` under ``public`` and the real declared schema survives only in
``graph_node.schema_name`` (reference §17). So the ambiguity can never appear as two schemas *in
the refs* — a rule written against that would be unreachable rather than discriminating — and a
physical schema may not be parsed out of a ref. Resolution is Task 5's explicit governed step
(``PHYSICAL_SCHEMA_NOT_RESOLVED`` when it fails); this adapter receives its result as
:class:`PhysicalIdentity` and stays pure.

What it does read from the catalog is ``schema_name`` for the tables ON the path — the hops the
caller never named and therefore could not resolve. That is CONFLICT DETECTION, not resolution: a
``NULL`` here is unknown, not a rival candidate, and Task 5 owns the verdict on unresolvable
schemas. Only two *known, different* schemas for one table name refuse.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from featuregen.contracts.db import DbConn
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.overlay.upload.bridge_realization import (
    BridgeJoinRealizationRevisionV1,
    BridgeRealizationCurrentV1,
    Cardinality,
    CardinalityBasis,
    ExecutionTier,
    StructuredPredicateV1,
    eligible_for_production,
)
from featuregen.overlay.upload.bridge_store import CurrentBridgeRealizationV1
from featuregen.overlay.upload.crosswalk import JoinLegPinV1
from featuregen.overlay.upload.crosswalk_admission import AdmittedCrosswalkV1
from featuregen.overlay.upload.crosswalk_observation import (
    COMPOSED_DIRECTIONS,
    MAPPING_TO_TARGET,
    SOURCE_TO_MAPPING,
    SOURCE_TO_TARGET,
)
from featuregen.overlay.upload.join_path import (
    JoinOutcome,
    JoinStep,
    classify_join_path,
    table_of_ref,
)
from featuregen.overlay.upload.temporal_policy import PREDICATE_KINDS, PREDICATE_OPERATORS

__all__ = [
    "CROSSWALK_EXECUTION_AUTHORITY",
    "RENDERED_OPERATORS",
    "CROSSWALK_EXECUTION_OUTCOME",
    "CrossCatalogJoinStepV1",
    "CrosswalkJoinStepV1",
    "JoinPlan",
    "MappingRowPredicateV1",
    "PhysicalIdentity",
    "plan_cross_catalog_join",
    "plan_crosswalk_join",
    "plan_join",
]


@dataclass(frozen=True, slots=True)
class PhysicalIdentity:
    """A table as it exists on the CLUSTER — the output of Task 5's physical resolution (§3.5).

    Defined here rather than in Task 5 so the task ordering works (T5 imports it). ``schema`` is the
    resolved physical schema, NOT the ``public`` segment of a logical ref; the two are different
    things and conflating them reads a different table than the catalog governs.
    """

    catalog_source: str
    schema: str
    table: str


@dataclass(frozen=True, slots=True)
class JoinPlan:
    """A governed path that may be computed on.

    ``fans_out`` is always ``False`` by construction — a fanning path is REFUSED, never returned —
    and is recorded rather than assumed so a consumer reads an explicit answer instead of inferring
    one from the absence of a warning. ``outcome_kind`` likewise always holds
    ``JoinOutcome.OPERATIONAL``: it names the governed outcome that admitted this plan, in the
    planner's own vocabulary (no parallel enum is minted for it).

    ``roles_used`` is load-bearing, not provenance decoration: roles decide whether a hop is DENIED,
    so the same catalog yields different plans for different readers and a plan that did not record
    them could not be re-checked.
    """

    steps: tuple[JoinStep | CrossCatalogJoinStepV1, ...]
    outcome_kind: str
    roles_used: tuple[str, ...]
    fans_out: bool


class _CardinalityVerdict(StrEnum):
    """What one hop does to row counts in the direction of travel."""

    FANS_IN = "fans_in"      # each source row matches at most one destination row — safe
    FANS_OUT = "fans_out"    # one source row matches many — multiplies rows, refused
    UNKNOWN = "unknown"      # unattested or unrecognized — may BE fanning, so also refused


#: The governed cardinality tokens, split by what they do TOWARD THE DESTINATION. Together they are
#: exactly ``canonical._VALID_CARDINALITY``; a test asserts that, so a new token added to the
#: vocabulary must be given a direction here instead of silently degrading to UNKNOWN.
_FANS_IN = frozenset({"N:1", "1:1"})
_FANS_OUT = frozenset({"1:N"})


_DIRECTIONAL_CARDINALITY = {
    Cardinality.ONE_TO_ONE: "1:1",
    Cardinality.MANY_TO_ONE: "N:1",
    Cardinality.ONE_TO_MANY: "1:N",
    Cardinality.MANY_TO_MANY: "N:N",
}


#: The bases on which a directional cardinality claim counts as ATTESTED — exactly the two the
#: shipped producers establish deterministically: ``infer_metadata_cardinality`` concludes
#: GOVERNED_KEY from declared complete keys, and deterministic profile admission concludes
#: EXACT_PROFILE from an exact observed scan. The other members (approximate profile, metadata
#: inference, none) record HOW WELL the direction is known rather than establishing it, and the
#: store rehydrates the basis independently of the claim — so a MANY_TO_ONE on an unattested basis
#: is refused, because "we do not know" is not "it is safe" (rule 2 above).
_ATTESTED_CARDINALITY_BASES = frozenset({
    CardinalityBasis.GOVERNED_KEY,
    CardinalityBasis.EXACT_PROFILE,
})


@dataclass(frozen=True, slots=True)
class CrossCatalogJoinStepV1:
    """One cross-catalog join, including every tuple member and closed predicate.

    ``from_ref``/``to_ref`` expose the first pair for compatibility with generic diagnostics only;
    execution must consume ``column_pairs`` as one composite equality, never render one table join
    per member.
    """

    from_catalog_source: str
    to_catalog_source: str
    from_ref: str
    to_ref: str
    column_pairs: tuple[tuple[str, str], ...]
    cardinality: str
    predicates: tuple[StructuredPredicateV1, ...]
    realization_id: str
    realization_revision_id: str
    bridge_fact_key: str
    dependency_snapshot_id: str
    evidence_revision_ids: tuple[str, ...]
    approved_join_fact_key: str | None = None
    approved_join_status: str | None = None
    authority: str = "directional_realization"

    def identity_payload(self) -> dict[str, object]:
        return {
            "from_catalog_source": self.from_catalog_source,
            "to_catalog_source": self.to_catalog_source,
            "column_pairs": [list(pair) for pair in self.column_pairs],
            "cardinality": self.cardinality,
            "predicates": [
                predicate.identity_payload() for predicate in self.predicates],
            "realization_revision_id": self.realization_revision_id,
            "dependency_snapshot_id": self.dependency_snapshot_id,
        }


#: The governed outcome/authority words a two-leg crosswalk traversal travels under. Minted here
#: rather than reusing ``DIRECTIONAL_REALIZATION_OPERATIONAL`` because the renderer's authority check
#: is what stops a crosswalk step being read as a bridge step and vice versa — one word for one
#: kind of governed approval.
CROSSWALK_EXECUTION_OUTCOME = "CROSSWALK_EXECUTION_OPERATIONAL"
CROSSWALK_EXECUTION_AUTHORITY = "crosswalk_execution"

#: The row-predicate vocabularies, IMPORTED from the contract that owns them rather than restated.
#: Release B decides which rows of a dataset answer a question; this module renders that decision
#: and never invents one, so the two sets must be the same set — a second copy here is how a
#: governed predicate silently stops being applied. The adapter below still fails CLOSED on a member
#: it has no rendered form for, which is what an EXTENSION of the vocabulary would hit.
_MAPPING_ROW_OPERATORS = PREDICATE_OPERATORS
_MAPPING_ROW_KINDS = PREDICATE_KINDS

#: How each governed operator renders. A member of the vocabulary that is absent here refuses.
RENDERED_OPERATORS: dict[str, str] = {
    "<": "<", "<=": "<=", ">": ">", ">=": ">=", "=": "==",
}


@dataclass(frozen=True, slots=True)
class MappingRowPredicateV1:
    """One predicate of the pinned ``DatasetRowSelectionV1``, in the shape a renderer can emit.

    Built from ``DatasetRowSelectionV1.predicate_payloads`` by :func:`mapping_row_predicates`, which
    refuses an unknown ``kind``/``operator`` rather than passing it through. The compiler never
    decides WHICH rows of the mapping table answer the question — Release B's temporal resolver
    does, and its answer arrives here already made and already pinned into the measurement the
    crosswalk was admitted on.

    ``parameter_ref`` names a run parameter (never a literal date — ``DatasetRowSelectionV1``
    refuses one), so the rendered filter binds a value the run supplies.
    """

    kind: str
    column_ref: str
    operator: str
    parameter_ref: str | None = None

    def identity_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "column_ref": self.column_ref, "operator": self.operator,
                "parameter_ref": self.parameter_ref}


def mapping_row_predicates(
    payloads: Iterable[Mapping[str, object]],
) -> tuple[MappingRowPredicateV1, ...] | MaterializationRefused:
    """Adapt a pinned row selection's payloads, or refuse an operator this slice cannot render."""
    built: list[MappingRowPredicateV1] = []
    for index, payload in enumerate(payloads):
        kind = str(payload.get("kind", ""))
        operator = str(payload.get("operator", ""))
        column_ref = str(payload.get("column_ref", ""))
        parameter = payload.get("parameter_ref")
        renderable = operator == "is_true" or operator in RENDERED_OPERATORS
        if kind not in _MAPPING_ROW_KINDS or operator not in _MAPPING_ROW_OPERATORS or (
                not renderable):
            return _refuse(
                CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
                f"the pinned mapping row selection's predicate {index} is "
                f"{kind!r}/{operator!r}, which this compiler has no rendered form for: the mapping "
                f"row rule decides WHICH mapping rows the uniqueness was measured over, so a "
                f"predicate that cannot be rendered would execute the traversal over a different "
                f"row set than the one that was measured")
        if not column_ref.strip():
            return _refuse(
                CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
                f"the pinned mapping row selection's predicate {index} names no column")
        built.append(MappingRowPredicateV1(
            kind=kind, column_ref=column_ref.strip(), operator=operator,
            parameter_ref=None if parameter is None else str(parameter)))
    return tuple(built)


@dataclass(frozen=True, slots=True)
class CrosswalkJoinStepV1:
    """ONE leg of a governed two-leg mapping-table traversal (Release C Task 12).

    A crosswalk is not a bridge and this is not a ``CrossCatalogJoinStepV1``: a bridge step carries
    ONE realization that is itself the governed safety decision, while a crosswalk leg is one half
    of a composition whose safety decision is the COMPOSED measurement. ``JoinLegPinV1`` refuses to
    pretend every leg is an entity bridge, and so does this: a same-catalog leg pins no fact key and
    no realization revision, and the fields for them are empty rather than fabricated.

    **``cardinality`` is the COMPOSED verdict, and both legs carry the same value.** This is the
    whole reason a two-leg traversal is plannable at all (see ``expression_ir._plan_to_grain``):
    ``expression_ir`` used to refuse a grain assembled from two joins because it "would need two
    [fan-out verdicts], which no single plan can state", and a composed observation states exactly
    one — measured over the joined shape, never multiplied out of the two legs' bounds. So neither
    step claims a per-leg cardinality nobody measured; each carries the direction's one verdict, and
    the renderer's amplification gate spans the pair rather than checking each half in isolation.
    """

    leg: str                       # the DEFINITION's side name — source_to_mapping / mapping_to_target
    direction: str                 # the REQUESTED traversal — source_to_target / target_to_source
    from_catalog_source: str
    to_catalog_source: str
    from_ref: str
    to_ref: str
    column_pairs: tuple[tuple[str, str], ...]
    cardinality: str
    crosswalk_definition_revision_id: str
    crosswalk_execution_revision_id: str
    mapping_dataset_ref: str
    mapping_binding_revision_id: str
    #: True on the leg that ARRIVES at the mapping dataset — the one the row filter applies to.
    arrives_at_mapping: bool
    leg_kind: str
    leg_plan_hash: str
    leg_read_set_hash: str
    leg_binding_revision_ids: tuple[str, ...] = ()
    leg_fact_keys: tuple[str, ...] = ()
    leg_realization_revision_ids: tuple[str, ...] = ()
    leg_dependency_snapshot_ids: tuple[str, ...] = ()
    mapping_row_predicates: tuple[MappingRowPredicateV1, ...] = ()
    mapping_temporal_policy_revision_id: str | None = None
    mapping_row_selection_hash: str | None = None
    composed_observation_revision_id: str | None = None
    leg_measurement_ids: tuple[str, ...] = ()
    authority: str = CROSSWALK_EXECUTION_AUTHORITY
    #: Present so generic diagnostics that read a step's approved-join fields do not have to know
    #: which kind they hold. A crosswalk leg is never backed by one.
    approved_join_fact_key: str | None = None
    approved_join_status: str | None = None

    def identity_payload(self) -> dict[str, object]:
        return {
            "leg": self.leg,
            "direction": self.direction,
            "from_catalog_source": self.from_catalog_source,
            "to_catalog_source": self.to_catalog_source,
            "column_pairs": [list(pair) for pair in self.column_pairs],
            "cardinality": self.cardinality,
            "crosswalk_definition_revision_id": self.crosswalk_definition_revision_id,
            "crosswalk_execution_revision_id": self.crosswalk_execution_revision_id,
            "mapping_dataset_ref": self.mapping_dataset_ref,
            "mapping_binding_revision_id": self.mapping_binding_revision_id,
            "leg_kind": self.leg_kind,
            "leg_plan_hash": self.leg_plan_hash,
            "leg_read_set_hash": self.leg_read_set_hash,
            "leg_binding_revision_ids": list(self.leg_binding_revision_ids),
            "leg_fact_keys": list(self.leg_fact_keys),
            "leg_realization_revision_ids": list(self.leg_realization_revision_ids),
            "leg_dependency_snapshot_ids": list(self.leg_dependency_snapshot_ids),
            "mapping_row_predicates": [
                predicate.identity_payload() for predicate in self.mapping_row_predicates],
            "mapping_temporal_policy_revision_id": self.mapping_temporal_policy_revision_id,
            "mapping_row_selection_hash": self.mapping_row_selection_hash,
            "composed_observation_revision_id": self.composed_observation_revision_id,
            "leg_measurement_ids": list(self.leg_measurement_ids),
        }


def _cardinality_verdict(cardinality: str | None) -> _CardinalityVerdict:
    if cardinality in _FANS_IN:
        return _CardinalityVerdict.FANS_IN
    if cardinality in _FANS_OUT:
        return _CardinalityVerdict.FANS_OUT
    return _CardinalityVerdict.UNKNOWN


def _fold(identifier: str) -> str:
    """Case-fold an SQL identifier for comparison (``object_ref._norm``'s fold).

    Unquoted SQL identifiers are case-insensitive and the catalog stores them lower-cased, so
    ``Banking`` and ``banking`` are ONE schema. Folding only ever happens for comparison and lookup
    — :class:`PhysicalIdentity` is never rewritten, because what the inventory declared is what the
    generated project should render.
    """
    return identifier.strip().lower()


def _refuse(code: CompilationRefusalCode, detail: str) -> MaterializationRefused:
    return MaterializationRefused(code, detail)


def _tables_on(steps: tuple[JoinStep, ...], *endpoints: PhysicalIdentity) -> list[str]:
    """Every bare table name the plan touches, in traversal order, endpoints included.

    ``table_of_ref`` is the planner's OWN accessor: asking the question differently here would
    check a different graph than the one that was traversed.
    """
    ordered: dict[str, None] = {_fold(endpoints[0].table): None} if endpoints else {}
    for step in steps:
        ordered.setdefault(_fold(table_of_ref(step.from_ref)), None)
        ordered.setdefault(_fold(table_of_ref(step.to_ref)), None)
    for endpoint in endpoints[1:]:
        ordered.setdefault(_fold(endpoint.table), None)
    return list(ordered)


def _schema_candidates(
    conn: DbConn, catalog_source: str, tables: list[str], endpoints: tuple[PhysicalIdentity, ...]
) -> dict[str, set[str]]:
    """Folded physical schemas each table name could mean: the catalog's attestations plus what the
    caller resolved for the endpoints. A ``NULL``/blank ``schema_name`` contributes nothing — it is
    unknown, not a rival candidate."""
    candidates: dict[str, set[str]] = {table: set() for table in tables}
    rows = conn.execute(
        "SELECT lower(table_name), schema_name FROM graph_node "
        "WHERE catalog_source = %s AND lower(table_name) = ANY(%s) "
        "  AND schema_name IS NOT NULL AND schema_name <> ''",
        (catalog_source, tables)).fetchall()
    for table_name, schema_name in rows:
        candidates.setdefault(table_name, set()).add(_fold(schema_name))
    for endpoint in endpoints:
        if endpoint.schema and endpoint.schema.strip():
            candidates.setdefault(_fold(endpoint.table), set()).add(_fold(endpoint.schema))
    return candidates


def plan_join(
    conn: DbConn,
    *,
    catalog_source: str,
    from_identity: PhysicalIdentity,
    to_identity: PhysicalIdentity,
    roles: Iterable[str] = (),
) -> JoinPlan | MaterializationRefused:
    """Plan the governed traversal from ``from_identity`` to ``to_identity``, or refuse it.

    A refusal is RETURNED, not raised: a refused join is one governed verdict among the many a
    compilation collects. The single exception is a caller error — identities from a catalog other
    than ``catalog_source`` — which raises ``ValueError`` because no governed metadata is involved
    and the closed §14 vocabulary has no member for "the call was assembled wrongly" (the same
    distinction ``admission.FeatureNamePlanError`` draws).

    Checks run in this order, and the FIRST failure decides the code:

    1. the governed outcome (is there an approved, visible path at all?);
    2. table-name ambiguity (is this path even about the tables we think it is? — a hop between
       two phantom-merged tables makes every later question meaningless);
    3. per hop, in traversal order: unknown cardinality, then fan-out.

    Only the reported code depends on that order; every branch refuses.
    """
    source = _fold(catalog_source)
    for name, identity in (("from_identity", from_identity), ("to_identity", to_identity)):
        if _fold(identity.catalog_source) != source:
            raise ValueError(
                f"{name}.catalog_source {identity.catalog_source!r} does not match "
                f"catalog_source {catalog_source!r}: the governed planner is single-catalog, and "
                f"planning inside one catalog while the caller meant another would read the "
                f"wrong tables")

    roles_used = tuple(roles)
    # BARE table names: `table_of_ref` returns the second dotted segment, so a schema-qualified
    # destination never matches a node and would come back NO_PATH. The physical schema stays on
    # the identity, where the ambiguity check below uses it.
    outcome = classify_join_path(conn, catalog_source, _fold(from_identity.table),
                                 _fold(to_identity.table), roles=roles_used)

    if outcome.kind != JoinOutcome.OPERATIONAL:
        return _refuse_outcome(outcome, from_identity, to_identity)

    ambiguity = _refuse_ambiguity(conn, catalog_source, outcome.steps,
                                  (from_identity, to_identity))
    if ambiguity is not None:
        return ambiguity

    for step in outcome.steps:
        verdict = _cardinality_verdict(step.cardinality)
        if verdict is _CardinalityVerdict.UNKNOWN:
            return _refuse(
                CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
                f"hop {step.from_ref} -> {step.to_ref} has cardinality {step.cardinality!r}: an "
                f"unattested hop may fan 1:N toward the destination, which would multiply rows and "
                f"inflate every aggregate over them")
        if verdict is _CardinalityVerdict.FANS_OUT:
            return _refuse(
                CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED,
                f"hop {step.from_ref} -> {step.to_ref} fans {step.cardinality} toward "
                f"{to_identity.schema}.{to_identity.table}: allocating one row across many "
                f"destination rows is a governed business decision, and no allocation policy "
                f"exists — the path is refused, not repaired")

    return JoinPlan(steps=outcome.steps, outcome_kind=outcome.kind, roles_used=roles_used,
                    fans_out=False)


def _matches_physical_identity(
    identity: PhysicalIdentity,
    revision: BridgeJoinRealizationRevisionV1,
    *,
    from_side: bool,
) -> bool:
    endpoint = revision.from_endpoint if from_side else revision.to_endpoint
    binding = endpoint.physical_binding
    return (
        binding is not None
        and _fold(identity.catalog_source) == _fold(binding.identity.catalog_source)
        and _fold(identity.schema) == _fold(binding.identity.schema)
        and _fold(identity.table) == _fold(binding.identity.table)
    )


def plan_cross_catalog_join(
    realization: CurrentBridgeRealizationV1,
    *,
    from_identity: PhysicalIdentity,
    to_identity: PhysicalIdentity,
    roles: Iterable[str] = (),
) -> JoinPlan | MaterializationRefused:
    """Adapt one current directional realization into an executable cross-catalog join.

    The caller must obtain ``realization`` from ``executable_bridge_realizations`` and revalidate
    it immediately before execution.  This pure adapter still repeats the structural safety checks
    so a forged carrier, reversed direction, unknown cardinality or fan-out cannot reach the IR.
    Human review is intentionally absent.
    """
    revision = realization.revision
    current: BridgeRealizationCurrentV1 = realization.current
    if not eligible_for_production(revision, current):
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"directional realization {revision.realization_revision_id} is not active and "
            "deterministically validated",
        )
    if not _matches_physical_identity(from_identity, revision, from_side=True):
        return _refuse(
            CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED,
            "the requested cross-catalog source does not match the realization's pinned "
            f"from binding revision {revision.from_endpoint.binding_revision_id}",
        )
    if not _matches_physical_identity(to_identity, revision, from_side=False):
        return _refuse(
            CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED,
            "the requested cross-catalog target does not match the realization's pinned "
            f"to binding revision {revision.to_endpoint.binding_revision_id}",
        )
    if revision.cardinality.value is None:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"directional realization {revision.realization_revision_id} has unknown cardinality",
        )
    if revision.cardinality.value in {
        Cardinality.ONE_TO_MANY,
        Cardinality.MANY_TO_MANY,
    }:
        return _refuse(
            CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED,
            f"directional realization {revision.realization_revision_id} is "
            f"{revision.cardinality.value.value} toward {to_identity.catalog_source}; this slice "
            "has no governed row-allocation policy",
        )
    if revision.has_unresolved_requirements:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"directional realization {revision.realization_revision_id} has unresolved "
            "additional-key requirements",
        )
    if revision.cardinality_basis not in _ATTESTED_CARDINALITY_BASES:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"directional realization {revision.realization_revision_id} claims "
            f"{revision.cardinality.value.value} on basis {revision.cardinality_basis.value!r}, "
            "which is not an attested basis (governed_key, exact_profile): the basis records how "
            "well the direction is actually known, and a claim on an unattested basis may BE "
            "fanning — \"we do not know\" is not \"it is safe\"",
        )

    pairs = tuple(
        (
            pair.from_logical_column_ref,
            pair.to_logical_column_ref,
        )
        for pair in revision.column_pairs
    )
    step = CrossCatalogJoinStepV1(
        from_catalog_source=revision.from_endpoint.logical_table_ref.split("::", 1)[0],
        to_catalog_source=revision.to_endpoint.logical_table_ref.split("::", 1)[0],
        from_ref=pairs[0][0],
        to_ref=pairs[0][1],
        column_pairs=pairs,
        cardinality=_DIRECTIONAL_CARDINALITY[revision.cardinality.value],
        predicates=revision.predicates,
        realization_id=revision.realization_id,
        realization_revision_id=revision.realization_revision_id,
        bridge_fact_key=revision.bridge_fact_key,
        dependency_snapshot_id=revision.dependency_snapshot_id,
        evidence_revision_ids=tuple(ref.evidence_id for ref in revision.evidence_refs),
    )
    return JoinPlan(
        steps=(step,),
        outcome_kind="DIRECTIONAL_REALIZATION_OPERATIONAL",
        roles_used=tuple(roles),
        fans_out=False,
    )


#: What a composed directional verdict may say for a traversal that is allowed to be COMPUTED ON.
#: Same rule as every other join in this module: fanning is refused rather than repaired, and
#: "we do not know" is not "it is safe".
_CROSSWALK_FANS_IN = frozenset({Cardinality.ONE_TO_ONE, Cardinality.MANY_TO_ONE})


def plan_crosswalk_join(
    admitted: AdmittedCrosswalkV1,
    *,
    direction: str,
    from_identity: PhysicalIdentity,
    to_identity: PhysicalIdentity,
    mapping_identity: PhysicalIdentity,
    execution_tier: ExecutionTier,
    roles: Iterable[str] = (),
) -> JoinPlan | MaterializationRefused:
    """Adapt ONE admitted crosswalk into TWO governed join steps, or refuse the traversal.

    **What replaced the either/or refusal, and why it is not a bypass.** ``expression_ir`` refused a
    grain reached through two joins with a stated reason: each join "would need [its own] fan-out
    verdict, which no single plan can state". That was true of two INDEPENDENT joins and it is still
    true of them. It is not true of a crosswalk, because Task 11 measures the composition itself:
    ``CrosswalkExecutionObservationV1.source_to_target_max_matches`` is measured over the JOINED
    shape ("never the product of the leg maxima, which is a bound"), and
    ``crosswalk_admission._direction_verdict`` turns it into ONE directional verdict per direction.
    So the plan does state one fan-out verdict for the traversal it is planning — the refusal's own
    condition, satisfied. The two legs travel under that one verdict and neither claims a per-leg
    cardinality nobody measured.

    **The two directions are gated independently**, exactly as they are admitted: a mapping table
    that is 1:1 forward and N:1 in reverse is ordinary, and the execution revision's
    ``combined_cardinality`` is the FORWARD verdict by construction
    (``admitted_crosswalk_execution`` fills it from ``decision.forward``), so a reverse traversal is
    gated on ``decision.reverse`` and refuses on its own evidence.

    ``execution_tier`` is the applicability scope this compilation reads at. A SANDBOX-scoped
    execution refuses at ``PRODUCTION``; that is the correct answer, not a bug.
    """
    if direction not in COMPOSED_DIRECTIONS:
        raise ValueError(
            f"direction must be one of {COMPOSED_DIRECTIONS}, got {direction!r}: which way a "
            "traversal runs decides which measured verdict gates it, and guessing would gate the "
            "reverse traversal on the forward direction's evidence")
    execution = admitted.execution
    definition = admitted.definition
    verdict = admitted.verdict_for(direction)
    roles_used = tuple(roles)

    scope_tier = execution.applicability_scope.execution_tier
    if execution_tier is ExecutionTier.PRODUCTION and scope_tier is not ExecutionTier.PRODUCTION:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"crosswalk execution {execution.execution_revision_id} is admitted only under the "
            f"{scope_tier.value} applicability scope {execution.applicability_scope.scope_id!r} "
            "and this is a production compilation: a mapping proved safe for sandbox data is not "
            "thereby proved safe for production data")
    admissible = (verdict.production_admissible if execution_tier is ExecutionTier.PRODUCTION
                  else verdict.sandbox_admissible)
    if not admissible:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"the {direction} direction of crosswalk {definition.definition_id} is "
            f"{verdict.safety_status.value} and not admissible at the {execution_tier.value} tier: "
            f"{', '.join(verdict.reason_codes) or '(no reason recorded)'}")

    if execution.composition_observation_revision_id is None:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"crosswalk {definition.definition_id} carries no composed observation, so nothing "
            "states the fan-out of the composition. Two safe legs do not compose into a safe "
            "crosswalk, and a plan built without the composed verdict is exactly the two-verdict "
            "gap the old either/or refusal named")
    composed = verdict.cardinality.value
    if composed is None:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"the {direction} direction of crosswalk {definition.definition_id} has an unknown "
            "composed cardinality: an unmeasured composition may BE fanning, and \"we do not "
            "know\" is not \"it is safe\"")
    if composed not in _CROSSWALK_FANS_IN:
        return _refuse(
            CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED,
            f"the {direction} direction of crosswalk {definition.definition_id} composes "
            f"{composed.value} toward {to_identity.schema}.{to_identity.table}: allocating one row "
            "across many is a governed business decision and no allocation policy exists — the "
            "traversal is refused, not deduplicated")

    pinned_policy = execution.mapping_temporal_policy_revision_id
    selection = admitted.mapping_row_selection
    if pinned_policy is not None and selection is None:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"crosswalk {definition.definition_id} pins mapping temporal policy {pinned_policy} "
            "and no resolved row selection was supplied: the mapping rows this traversal reads "
            "would be whatever the table holds, which is a different row set from the one its "
            "uniqueness was measured over")
    row_predicates: tuple[MappingRowPredicateV1, ...] = ()
    if selection is not None:
        adapted = mapping_row_predicates(selection.predicate_payloads)
        if isinstance(adapted, MaterializationRefused):
            return adapted
        row_predicates = adapted

    forward = direction == SOURCE_TO_TARGET
    # The DEFINITION's sides are canonically ordered and its pair tuples travel with their endpoint,
    # so which side a traversal STARTS from decides which pin and which pair tuple each leg uses.
    if forward:
        first_pin, second_pin = execution.source_leg, execution.target_leg
        first_pairs = tuple((pair.endpoint_member_ref, pair.mapping_member_ref)
                            for pair in definition.source_to_mapping_pairs)
        second_pairs = tuple((pair.mapping_member_ref, pair.endpoint_member_ref)
                             for pair in definition.mapping_to_target_pairs)
        first_leg, second_leg = SOURCE_TO_MAPPING, MAPPING_TO_TARGET
    else:
        first_pin, second_pin = execution.target_leg, execution.source_leg
        first_pairs = tuple((pair.endpoint_member_ref, pair.mapping_member_ref)
                            for pair in definition.mapping_to_target_pairs)
        second_pairs = tuple((pair.mapping_member_ref, pair.endpoint_member_ref)
                             for pair in definition.source_to_mapping_pairs)
        first_leg, second_leg = MAPPING_TO_TARGET, SOURCE_TO_MAPPING

    mapping_catalog = definition.mapping_dataset_ref.split("::", 1)[0]
    cardinality = _DIRECTIONAL_CARDINALITY[composed]
    shared = {
        "direction": direction,
        "cardinality": cardinality,
        "crosswalk_definition_revision_id": definition.revision_id,
        "crosswalk_execution_revision_id": execution.execution_revision_id,
        "mapping_dataset_ref": definition.mapping_dataset_ref,
        "mapping_binding_revision_id": execution.mapping_binding_revision_id,
        "mapping_temporal_policy_revision_id": pinned_policy,
        "mapping_row_selection_hash": None if selection is None else selection.content_hash,
        "composed_observation_revision_id": execution.composition_observation_revision_id,
        "leg_measurement_ids": tuple(execution.leg_measurement_ids),
    }
    steps = (
        CrosswalkJoinStepV1(
            leg=first_leg,
            from_catalog_source=from_identity.catalog_source,
            to_catalog_source=mapping_catalog,
            from_ref=first_pairs[0][0], to_ref=first_pairs[0][1],
            column_pairs=first_pairs,
            arrives_at_mapping=True,
            # The row filter belongs to the leg that ARRIVES at the mapping dataset, because that is
            # the frame it scopes — and it is emitted before that frame's uniqueness gate, so the
            # rows whose uniqueness is checked are the rows the join will use.
            mapping_row_predicates=row_predicates,
            **_leg_pin_fields(first_pin), **shared),
        CrosswalkJoinStepV1(
            leg=second_leg,
            from_catalog_source=mapping_catalog,
            to_catalog_source=to_identity.catalog_source,
            from_ref=second_pairs[0][0], to_ref=second_pairs[0][1],
            column_pairs=second_pairs,
            arrives_at_mapping=False,
            **_leg_pin_fields(second_pin), **shared),
    )
    unresolved = _refuse_unpinned_mapping(mapping_identity, definition.mapping_dataset_ref)
    if unresolved is not None:
        return unresolved
    return JoinPlan(steps=steps, outcome_kind=CROSSWALK_EXECUTION_OUTCOME,
                    roles_used=roles_used, fans_out=False)


def _leg_pin_fields(pin: JoinLegPinV1) -> dict[str, object]:
    """One ``JoinLegPinV1`` flattened onto a step — the pin's own values, never re-derived."""
    return {
        "leg_kind": pin.kind.value,
        "leg_plan_hash": pin.plan_hash,
        "leg_read_set_hash": pin.read_set_hash,
        "leg_binding_revision_ids": tuple(pin.binding_revision_ids),
        "leg_fact_keys": tuple(pin.fact_keys),
        "leg_realization_revision_ids": tuple(pin.realization_revision_ids),
        "leg_dependency_snapshot_ids": tuple(pin.dependency_snapshot_ids),
    }


def _refuse_unpinned_mapping(
    mapping_identity: PhysicalIdentity, mapping_dataset_ref: str
) -> MaterializationRefused | None:
    """The mapping dataset must have RESOLVED onto the cluster before a leg may travel through it.

    Task 5 owns resolution and refuses on its own when it cannot answer; this only refuses the
    case resolution cannot see — a caller passing an identity for some other table. The mapping
    dataset is the one table on a crosswalk nobody named in the formula, so it is exactly the one
    whose mis-resolution nothing else would catch.
    """
    expected = mapping_dataset_ref.split("::", 1)
    if len(expected) != 2:
        return _refuse(
            CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED,
            f"the mapping dataset ref {mapping_dataset_ref!r} names no catalog source")
    catalog, remainder = expected
    bare_table = remainder.split(".")[-1]
    if (_fold(mapping_identity.catalog_source), _fold(mapping_identity.table)) != (
            _fold(catalog), _fold(bare_table)):
        return _refuse(
            CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED,
            f"the resolved mapping identity {mapping_identity.catalog_source}::"
            f"{mapping_identity.schema}.{mapping_identity.table} is not the crosswalk's mapping "
            f"dataset {mapping_dataset_ref!r}: the mapping table is the one relation on a crosswalk "
            "that no formula names, so a traversal resolved onto the wrong one would join real "
            "rows through a table nobody governed for it")
    return None


def _refuse_outcome(
    outcome: JoinOutcome, from_identity: PhysicalIdentity, to_identity: PhysicalIdentity
) -> MaterializationRefused:
    """Map a non-OPERATIONAL governed outcome onto its own refusal code."""
    route = (f"{from_identity.schema}.{from_identity.table} -> "
             f"{to_identity.schema}.{to_identity.table}")
    if outcome.kind == JoinOutcome.UNVERIFIED:
        keys = ", ".join(outcome.fact_keys) or "(none recorded)"
        return _refuse(
            CompilationRefusalCode.JOIN_PATH_NOT_VERIFIED,
            f"{route}: the only path runs through {len(outcome.endpoints)} hop(s) whose "
            f"approved_join fact is not VERIFIED — fact keys: {keys}")
    if outcome.kind == JoinOutcome.DENIED:
        hops = "; ".join(f"{f} -> {t}" for f, t in outcome.endpoints) or "(none recorded)"
        return _refuse(
            CompilationRefusalCode.JOIN_PATH_DENIED_BY_READ_SCOPE,
            f"{route}: every path crosses a hop hidden from the supplied read scope — "
            f"endpoints: {hops}")
    if outcome.kind == JoinOutcome.NO_PATH:
        return _refuse(
            CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED,
            f"{route}: no governed join path connects these tables in this catalog")
    # Fail closed on an outcome kind this adapter has never been taught: an unrecognized verdict is
    # not an approval, and treating it as one is how a governance extension silently loses effect.
    return _refuse(
        CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED,
        f"{route}: the planner returned outcome kind {outcome.kind!r}, which this adapter does not "
        f"recognize as governed approval")


def _refuse_ambiguity(
    conn: DbConn,
    catalog_source: str,
    steps: tuple[JoinStep, ...],
    endpoints: tuple[PhysicalIdentity, ...],
) -> MaterializationRefused | None:
    """Refuse when one bare table name on the path could mean two physical tables.

    Checked across EVERY step, not just the endpoints: an intermediate table is never named by the
    caller, so it is precisely the one whose ambiguity nobody else can catch.
    """
    tables = _tables_on(steps, *endpoints)
    candidates = _schema_candidates(conn, catalog_source, tables, endpoints)
    ambiguous = [(table, sorted(schemas)) for table, schemas in candidates.items()
                 if len(schemas) > 1]
    if not ambiguous:
        return None
    ambiguous.sort()
    described = "; ".join(f"{table} -> {', '.join(schemas)}" for table, schemas in ambiguous)
    return _refuse(
        CompilationRefusalCode.AMBIGUOUS_TABLE_NAME,
        f"the governed planner indexes nodes by bare table name, and these names resolve to more "
        f"than one physical schema: {described}. A path stitched through them could read a "
        f"different table than the one governed")
