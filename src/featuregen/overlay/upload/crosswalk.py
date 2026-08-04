"""Mapping-table crosswalk CONTRACTS (Release C Task 10, profile plan §6.6).

A crosswalk says "customer number maps to CIF through this table". It is NOT a bridge: a bridge
is direct equality between two identifier endpoints, and its fact keys and directional realization
identities are load-bearing (§4-correction-8). Nothing in this module touches them — a crosswalk is
a SEPARATE definition that references the same endpoint and realization TYPES.

**What this module deliberately is not.** There is no execution here, and no path to one:

* :class:`CrosswalkDefinitionRevisionV1` is environment-independent. It accepts LOGICAL, UNBOUND
  endpoints only; an endpoint carrying a physical binding is a typed refusal
  (:data:`ENDPOINT_PHYSICALLY_BOUND`), because a definition that pinned an environment would be an
  execution contract wearing a definition's name.
* :class:`CrosswalkExecutionRevisionV1` is a composition SHAPE — constructible and hash-stable so
  Task 11/12 have something to observe against, with **no producer and no admission path in this
  task**. Nothing in this repo builds one; nothing stores one; no reader consults one. It exists so
  that the identity a later task must pin is frozen now rather than invented under deadline.
* This module imports :func:`featuregen.materialize.canonical.materialize_hash` and NOTHING else
  from ``materialize`` (D1 requires that hasher; a planner/renderer import would be the execution
  path this task must not have). ``test_crosswalk_contracts`` asserts exactly that.

**HOME (D5, ratified 2026-08-03).** ``overlay/upload`` — the same home as §6.4/§6.5. Not
re-litigated here.

**HASHING (D1).** Every identity in this module is RFC-8785 JCS via ``materialize_hash``. The
bridge family keeps its own ``field_evidence.canonical_hash`` scheme and is NOT re-keyed: importing
this module must leave a derived ``bridge_fact_key`` byte-identical (pinned by test).

**ORIENTATION.** An identifier relationship is unordered; only a realization is directional (the
`bridge_realization` docstring, and migration 1036 which converged stored bridge rows onto ONE
orientation after a swapped write silently produced a second row for the same link). A crosswalk
inherits that doctrine: :class:`CrosswalkDefinitionRevisionV1` canonicalizes its two endpoints at
construction, so ``A -> map -> B`` and ``B -> map -> A`` are the SAME definition with byte-identical
ids and the leg pair tuples travelling with their endpoints. The field names ``source_endpoint`` /
``target_endpoint`` are §6.6's; after canonicalization they name the two SIDES, never a claim about
traversal direction. Direction is established by observation (Task 11) and executed by a plan
(Task 12) — never asserted by the order somebody typed two refs in.

**KNOWN LIMITATION — non-``public`` mapping datasets (recorded, not silently hit).** A crosswalk
whose mapping dataset lives outside the flat ``public`` namespace is UNREPRESENTABLE downstream
today: ``bridge_realization._column_ref`` (``:93-95``) refuses a non-``public`` schema, so no
execution could ever be composed for such a definition. :class:`LogicalMappingPairV1` is
schema-PRESERVING by contract (§6.6 — it is not the flat-public ``ColumnPairV1``, and it will not
need rewriting when that limitation lifts), but the DEFINITION refuses the case with the typed code
:data:`MAPPING_DATASET_SCHEMA_UNSUPPORTED` at authoring time. That is deliberate: a crash deep in a
future compiler, months after the definition was reviewed, is the dishonest alternative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.bridge_assessment import (
    EvidenceRefV1,
    IdentifierEndpointV1,
)
from featuregen.overlay.upload.bridge_realization import (
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    SafetyStatus,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

#: Bumping this re-keys every crosswalk definition, so it moves only with a real shape change.
CROSSWALK_CONTRACT_VERSION = "1.0.0"

#: Both crosswalk identity levels share the ``cwd_`` family prefix (CHECK-pinned by migration
#: 1050); the COLUMN says which level a value is, exactly as ``pbr_`` names a family rather than a
#: single table's key.
CROSSWALK_ID_PREFIX = "cwd_"

# ── typed refusals ──────────────────────────────────────────────────────────────────────────────

#: A crosswalk endpoint carried a physical binding. A definition is environment-independent by
#: contract (§6.6); binding revisions belong to execution.
ENDPOINT_PHYSICALLY_BOUND = "endpoint_physically_bound"
#: The mapping dataset (or a mapping-side pair member) is outside the flat ``public`` namespace.
#: See the module docstring: representable here, unrepresentable downstream, refused honestly.
MAPPING_DATASET_SCHEMA_UNSUPPORTED = "mapping_dataset_schema_unsupported"
#: Fewer than two distinct sides, or a mapping dataset that IS one of the endpoints.
CROSSWALK_ENDPOINTS_NOT_DISTINCT = "crosswalk_endpoints_not_distinct"
#: A leg carried no ordered pairs, repeated a member, or named a column outside its own dataset.
CROSSWALK_LEG_MALFORMED = "crosswalk_leg_malformed"
#: A leg pin claimed a shape its resolution owner cannot produce (Task 11: same-catalog legs
#: resolve through ``plan_join``, cross-catalog legs through ONE bridge realization revision — a
#: pin that pretends every leg is an entity bridge is refused).
JOIN_LEG_PIN_MALFORMED = "join_leg_pin_malformed"
#: An execution shape claimed deterministic safety without the observations that could establish it.
CROSSWALK_EXECUTION_SHAPE_INVALID = "crosswalk_execution_shape_invalid"
#: A STORE-level refusal no retry can fix: a blank actor, a negative expected pointer version, or a
#: payload past a storage bound. Distinct from a CAS conflict, which means "try again".
CROSSWALK_WRITE_INVALID = "crosswalk_write_invalid"

CROSSWALK_REFUSAL_CODES: frozenset[str] = frozenset({
    ENDPOINT_PHYSICALLY_BOUND,
    MAPPING_DATASET_SCHEMA_UNSUPPORTED,
    CROSSWALK_ENDPOINTS_NOT_DISTINCT,
    CROSSWALK_LEG_MALFORMED,
    JOIN_LEG_PIN_MALFORMED,
    CROSSWALK_EXECUTION_SHAPE_INVALID,
    CROSSWALK_WRITE_INVALID,
})


class CrosswalkContractError(ValueError):
    """A crosswalk contract refusal carrying its CLOSED-vocabulary ``code``.

    A ``ValueError`` so a route maps it to a 400 the same way ``BridgeContractError`` is mapped;
    the ``code`` is what a caller branches on, never the message text."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CROSSWALK_REFUSAL_CODES:
            raise ValueError(f"unknown crosswalk refusal code {code!r}")
        super().__init__(message)
        self.code = code


# ── logical, schema-preserving pairs ────────────────────────────────────────────────────────────

def _logical_column_ref(value: str) -> str:
    """Normalize a COLUMN ref, PRESERVING its schema (§6.6 — this is not ``ColumnPairV1``)."""
    try:
        source, schema, table, column = parse_ref(value.strip().lower())
    except ValueError as exc:
        raise CrosswalkContractError(CROSSWALK_LEG_MALFORMED, str(exc)) from exc
    if column is None:
        raise CrosswalkContractError(
            CROSSWALK_LEG_MALFORMED, f"a crosswalk mapping pair requires a column ref, got {value!r}")
    return normalize_ref(source, schema, table, column)


def _logical_table_ref(value: str) -> str:
    """Normalize a TABLE ref, PRESERVING its schema."""
    try:
        source, schema, table, column = parse_ref(value.strip().lower())
    except ValueError as exc:
        raise CrosswalkContractError(CROSSWALK_LEG_MALFORMED, str(exc)) from exc
    if column is not None:
        raise CrosswalkContractError(
            CROSSWALK_LEG_MALFORMED, f"a mapping dataset ref must not carry a column, got {value!r}")
    return normalize_ref(source, schema, table, None)


@dataclass(frozen=True, slots=True)
class LogicalMappingPairV1:
    """One ordered equality member of a crosswalk leg: an ENDPOINT column and a MAPPING column.

    Schema-preserving on both sides by contract. It deliberately does NOT reuse the bridge-only
    flat-public :class:`bridge_realization.ColumnPairV1`, whose grammar is a statement about what
    the bridge EXECUTION path can address, not about what a catalog can mean."""

    endpoint_member_ref: str
    mapping_member_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint_member_ref", _logical_column_ref(self.endpoint_member_ref))
        object.__setattr__(self, "mapping_member_ref", _logical_column_ref(self.mapping_member_ref))
        if self.endpoint_member_ref == self.mapping_member_ref:
            raise CrosswalkContractError(
                CROSSWALK_LEG_MALFORMED,
                f"a mapping pair must join two different columns, got {self.endpoint_member_ref!r} twice")

    def identity_payload(self) -> dict[str, str]:
        return {
            "endpoint_member_ref": self.endpoint_member_ref,
            "mapping_member_ref": self.mapping_member_ref,
        }


def _table_of(column_ref: str) -> str:
    source, schema, table, _column = parse_ref(column_ref)
    return normalize_ref(source, schema, table, None)


def _assert_public(ref: str, *, what: str) -> None:
    _source, schema, _table, _column = parse_ref(ref)
    if schema != "public":
        raise CrosswalkContractError(
            MAPPING_DATASET_SCHEMA_UNSUPPORTED,
            f"{what} {ref!r} is outside the flat 'public' namespace. The crosswalk DEFINITION can "
            "represent it, but no execution could ever be composed for it: bridge realization "
            "column refs refuse a non-public schema (bridge_realization.py:93-95). Refused here, "
            "at authoring time, rather than as a crash inside a future compiler")


def _assert_leg(
    pairs: tuple[LogicalMappingPairV1, ...], *, endpoint: IdentifierEndpointV1,
    mapping_dataset_ref: str, leg: str,
) -> None:
    if not pairs:
        raise CrosswalkContractError(
            CROSSWALK_LEG_MALFORMED, f"the {leg} leg must carry at least one ordered mapping pair")
    endpoint_refs = [pair.endpoint_member_ref for pair in pairs]
    mapping_refs = [pair.mapping_member_ref for pair in pairs]
    if len(set(endpoint_refs)) != len(endpoint_refs) or len(set(mapping_refs)) != len(mapping_refs):
        raise CrosswalkContractError(
            CROSSWALK_LEG_MALFORMED, f"the {leg} leg must not repeat either tuple member")
    members = {member.logical_column_ref for member in endpoint.members}
    if not set(endpoint_refs) <= members:
        raise CrosswalkContractError(
            CROSSWALK_LEG_MALFORMED,
            f"the {leg} leg names columns that are not members of endpoint "
            f"{endpoint.logical_table_ref!r}")
    for mapping_ref in mapping_refs:
        _assert_public(mapping_ref, what="crosswalk mapping column")
        if _table_of(mapping_ref) != mapping_dataset_ref:
            raise CrosswalkContractError(
                CROSSWALK_LEG_MALFORMED,
                f"the {leg} leg names {mapping_ref!r}, which is not a column of the mapping dataset "
                f"{mapping_dataset_ref!r}")


@dataclass(frozen=True, slots=True)
class CrosswalkDefinitionRevisionV1:
    """One crosswalk: two identifier endpoints related THROUGH a mapping dataset (§6.6).

    **Identity (two levels, both environment-independent).**

    * ``definition_id`` — the two endpoint LOGICAL identities (canonically ordered, see the module
      docstring) plus the mapping dataset. What the crosswalk IS.
    * ``revision_id`` — additionally the ordered leg pairs, the mapping temporal policy revision and
      the evidence. What this VERSION of it says. Human review is deliberately absent from both:
      a review changes accountability, never identity (§5.8, and the bridge family's Decision G).

    **Unbound by construction.** Endpoints must carry no ``physical_binding``: physical identities
    and binding revisions live in :class:`CrosswalkExecutionRevisionV1`, which this task does not
    produce. A definition therefore says nothing about any environment, and re-uploading a catalog
    cannot re-key one.
    """

    source_endpoint: IdentifierEndpointV1
    mapping_dataset_ref: str
    source_to_mapping_pairs: tuple[LogicalMappingPairV1, ...]
    mapping_to_target_pairs: tuple[LogicalMappingPairV1, ...]
    target_endpoint: IdentifierEndpointV1
    mapping_temporal_policy_revision_id: str | None = None
    evidence_refs: tuple[EvidenceRefV1, ...] = ()
    definition_id: str = field(init=False, default="")
    revision_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        mapping_ref = _logical_table_ref(self.mapping_dataset_ref)
        _assert_public(mapping_ref, what="crosswalk mapping dataset")
        object.__setattr__(self, "mapping_dataset_ref", mapping_ref)

        for endpoint in (self.source_endpoint, self.target_endpoint):
            if endpoint.physical_binding is not None or endpoint.binding_revision_id is not None:
                raise CrosswalkContractError(
                    ENDPOINT_PHYSICALLY_BOUND,
                    f"crosswalk endpoint {endpoint.logical_table_ref!r} carries a physical binding. "
                    "A definition is environment-independent; bindings belong to the execution "
                    "revision, which this task deliberately has no producer for")

        source_pairs = tuple(self.source_to_mapping_pairs)
        target_pairs = tuple(self.mapping_to_target_pairs)
        if not all(isinstance(pair, LogicalMappingPairV1)
                   for pair in (*source_pairs, *target_pairs)):
            raise CrosswalkContractError(
                CROSSWALK_LEG_MALFORMED,
                "crosswalk legs accept only LogicalMappingPairV1 values — the flat-public "
                "ColumnPairV1 is the bridge execution grammar, not a definition grammar")

        source_identity = self.source_endpoint.logical_identity_payload()
        target_identity = self.target_endpoint.logical_identity_payload()
        if source_identity == target_identity:
            raise CrosswalkContractError(
                CROSSWALK_ENDPOINTS_NOT_DISTINCT,
                "a crosswalk relates two DIFFERENT identifier endpoints")
        for endpoint in (self.source_endpoint, self.target_endpoint):
            if endpoint.logical_table_ref == mapping_ref:
                raise CrosswalkContractError(
                    CROSSWALK_ENDPOINTS_NOT_DISTINCT,
                    f"the mapping dataset {mapping_ref!r} is also an endpoint; that is a direct "
                    "relationship, which belongs to the bridge family, not a crosswalk")

        _assert_leg(source_pairs, endpoint=self.source_endpoint,
                    mapping_dataset_ref=mapping_ref, leg="source_to_mapping")
        _assert_leg(target_pairs, endpoint=self.target_endpoint,
                    mapping_dataset_ref=mapping_ref, leg="mapping_to_target")

        # CANONICAL ORIENTATION (module docstring / migration 1036 doctrine). The pair tuples travel
        # WITH their endpoint, so a caller that names the sides the other way round produces a
        # byte-identical revision instead of a second record for the same crosswalk.
        source_key = materialize_hash(source_identity)
        target_key = materialize_hash(target_identity)
        if target_key < source_key:
            swapped_source, swapped_target = self.target_endpoint, self.source_endpoint
            object.__setattr__(self, "source_endpoint", swapped_source)
            object.__setattr__(self, "target_endpoint", swapped_target)
            source_pairs, target_pairs = target_pairs, source_pairs
            source_identity, target_identity = target_identity, source_identity

        object.__setattr__(self, "source_to_mapping_pairs", source_pairs)
        object.__setattr__(self, "mapping_to_target_pairs", target_pairs)

        policy = self.mapping_temporal_policy_revision_id
        if policy is not None:
            policy = policy.strip()
            if not policy:
                raise CrosswalkContractError(
                    CROSSWALK_LEG_MALFORMED,
                    "a blank temporal policy revision id declares nothing; use None")
            object.__setattr__(self, "mapping_temporal_policy_revision_id", policy)

        evidence = tuple(sorted(
            self.evidence_refs, key=lambda ref: materialize_hash(ref.identity_payload())))
        object.__setattr__(self, "evidence_refs", evidence)

        definition_id = CROSSWALK_ID_PREFIX + materialize_hash({
            "contract_version": CROSSWALK_CONTRACT_VERSION,
            "endpoints": [source_identity, target_identity],
            "mapping_dataset_ref": mapping_ref,
        })
        object.__setattr__(self, "definition_id", definition_id)
        object.__setattr__(self, "revision_id", CROSSWALK_ID_PREFIX + materialize_hash({
            "definition_id": definition_id,
            "source_to_mapping_pairs": [pair.identity_payload() for pair in source_pairs],
            "mapping_to_target_pairs": [pair.identity_payload() for pair in target_pairs],
            "mapping_temporal_policy_revision_id": self.mapping_temporal_policy_revision_id,
            "evidence_refs": [ref.identity_payload() for ref in evidence],
        }))

    @property
    def source_ref(self) -> str:
        """The display ref of the source side — its single member, else its table (the
        ``semantic_context._endpoint_ref`` rule, so both surfaces agree)."""
        return _endpoint_display_ref(self.source_endpoint)

    @property
    def target_ref(self) -> str:
        return _endpoint_display_ref(self.target_endpoint)

    def content_payload(self) -> dict[str, object]:
        """The full stored projection — what a store read-back re-hashes to verify (§D1)."""
        return {
            "contract_version": CROSSWALK_CONTRACT_VERSION,
            "source_endpoint": self.source_endpoint.logical_identity_payload(),
            "target_endpoint": self.target_endpoint.logical_identity_payload(),
            "mapping_dataset_ref": self.mapping_dataset_ref,
            "source_to_mapping_pairs": [p.identity_payload() for p in self.source_to_mapping_pairs],
            "mapping_to_target_pairs": [p.identity_payload() for p in self.mapping_to_target_pairs],
            "mapping_temporal_policy_revision_id": self.mapping_temporal_policy_revision_id,
            "evidence_refs": [ref.identity_payload() for ref in self.evidence_refs],
        }

    def column_refs(self) -> tuple[str, ...]:
        """Every logical COLUMN ref this definition names — endpoints and both mapping sides.

        The read-scope surface: a caller who cannot see one of these columns may not see the
        crosswalk at all (Release-B whole-payload discipline)."""
        refs = {member.logical_column_ref
                for endpoint in (self.source_endpoint, self.target_endpoint)
                for member in endpoint.members}
        for pair in (*self.source_to_mapping_pairs, *self.mapping_to_target_pairs):
            refs.add(pair.endpoint_member_ref)
            refs.add(pair.mapping_member_ref)
        return tuple(sorted(refs))

    def dataset_refs(self) -> tuple[str, ...]:
        """The three TABLE refs a crosswalk names: both endpoints and the mapping dataset."""
        return tuple(sorted({
            self.source_endpoint.logical_table_ref,
            self.target_endpoint.logical_table_ref,
            self.mapping_dataset_ref,
        }))


def _endpoint_display_ref(endpoint: IdentifierEndpointV1) -> str:
    if len(endpoint.members) == 1:
        return endpoint.members[0].logical_column_ref
    return endpoint.logical_table_ref


# ── execution SHAPE (no producer, no admission path — see the module docstring) ─────────────────

class JoinLegKind(StrEnum):
    """Which owner resolves a leg (Task 11): ``plan_join`` or ONE bridge realization revision."""

    SAME_CATALOG = "same_catalog"
    CROSS_CATALOG = "cross_catalog"


@dataclass(frozen=True, slots=True)
class JoinLegPinV1:
    """One resolved crosswalk leg, pinned (D5 amended form).

    The amended form supersedes semantic's ``leg_plan_hashes``: a bare plan hash is not enough for
    replay or explainability, so a pin names the plan, the side-specific BINDING revisions, the
    governed join fact keys, the realization revisions and the dependency snapshots.

    ``dependency_snapshot_ids`` is plural DELIBERATELY, and it is not a re-pluralization of the
    per-realization singular (``bridge_realization.dependency_snapshot_id``): a pin is per LEG, and
    a leg may resolve through more than one governed join, so the tuple holds one singular value per
    realization it pinned. A same-catalog leg pins none of the bridge-family fields — pretending
    every leg is an entity bridge is the defect Task 11 names explicitly."""

    kind: JoinLegKind
    plan_hash: str
    binding_revision_ids: tuple[str, ...] = ()
    fact_keys: tuple[str, ...] = ()
    realization_revision_ids: tuple[str, ...] = ()
    dependency_snapshot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.plan_hash.strip():
            raise CrosswalkContractError(
                JOIN_LEG_PIN_MALFORMED, "a join leg pin must name the plan it resolved")
        for name in ("binding_revision_ids", "fact_keys", "realization_revision_ids",
                     "dependency_snapshot_ids"):
            values = tuple(str(v).strip() for v in getattr(self, name))
            if any(not v for v in values):
                raise CrosswalkContractError(
                    JOIN_LEG_PIN_MALFORMED, f"{name} must not contain a blank id")
            object.__setattr__(self, name, values)
        if not self.binding_revision_ids:
            raise CrosswalkContractError(
                JOIN_LEG_PIN_MALFORMED,
                "a leg reads at least one side, so it pins at least one binding revision")
        bridge_fields = (*self.fact_keys, *self.realization_revision_ids)
        if self.kind is JoinLegKind.SAME_CATALOG and bridge_fields:
            raise CrosswalkContractError(
                JOIN_LEG_PIN_MALFORMED,
                "a same-catalog leg resolves through plan_join, not through a bridge realization; "
                "pinning fact keys or realization revisions on one pretends every leg is an entity "
                "bridge")
        if self.kind is JoinLegKind.CROSS_CATALOG and not (
                self.fact_keys and self.realization_revision_ids):
            raise CrosswalkContractError(
                JOIN_LEG_PIN_MALFORMED,
                "a cross-catalog leg resolves through a governed bridge realization, so it pins "
                "both its fact key and its realization revision")

    def identity_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "plan_hash": self.plan_hash,
            "binding_revision_ids": list(self.binding_revision_ids),
            "fact_keys": list(self.fact_keys),
            "realization_revision_ids": list(self.realization_revision_ids),
            "dependency_snapshot_ids": list(self.dependency_snapshot_ids),
        }


@dataclass(frozen=True, slots=True)
class CrosswalkExecutionRevisionV1:
    """The composition carrier for a two-leg crosswalk execution — SHAPE ONLY, this task.

    **There is no producer and no admission path in this task.** Nothing constructs this outside
    its own tests; no store persists it; no reader consults it; ``FEATUREGEN_CROSSWALK_EXECUTION``
    gates the EXECUTION that a later task builds, and discovery is deliberately not behind it. The
    class exists so the identity Task 11 observes against and Task 12 compiles from is frozen while
    it is still cheap to get right.

    It REUSES the bridge family's ``SafetyStatus``, ``ExecutionTier`` (via the scope),
    ``RealizationApplicabilityScopeV1`` and ``DirectionalCardinalityVerdictV1``. This is a
    composition carrier, not a competing safety model — the one structural rule it enforces is that
    deterministic safety cannot be CLAIMED without the observations that could establish it. Human
    review is intentionally absent from the identity, as it is everywhere else in this family."""

    crosswalk_definition_revision_id: str
    mapping_binding_revision_id: str
    source_leg: JoinLegPinV1
    target_leg: JoinLegPinV1
    applicability_scope: RealizationApplicabilityScopeV1
    combined_cardinality: DirectionalCardinalityVerdictV1
    safety_status: SafetyStatus
    mapping_temporal_policy_revision_id: str | None = None
    leg_observation_revision_ids: tuple[str, ...] = ()
    composition_observation_revision_id: str | None = None
    execution_revision_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        for name in ("crosswalk_definition_revision_id", "mapping_binding_revision_id"):
            if not getattr(self, name).strip():
                raise CrosswalkContractError(
                    CROSSWALK_EXECUTION_SHAPE_INVALID, f"{name} must not be blank")
        if self.source_leg.identity_payload() == self.target_leg.identity_payload():
            raise CrosswalkContractError(
                CROSSWALK_EXECUTION_SHAPE_INVALID,
                "the two legs of a crosswalk execution must be distinct; one leg rendered twice is "
                "the 'render endpoint equality' mutation Task 13 must kill")
        observations = tuple(str(v).strip() for v in self.leg_observation_revision_ids)
        if any(not v for v in observations):
            raise CrosswalkContractError(
                CROSSWALK_EXECUTION_SHAPE_INVALID,
                "leg_observation_revision_ids must not contain a blank id")
        object.__setattr__(self, "leg_observation_revision_ids", observations)
        if self.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED and not (
                self.composition_observation_revision_id and len(observations) >= 2):
            raise CrosswalkContractError(
                CROSSWALK_EXECUTION_SHAPE_INVALID,
                "deterministic validation requires an observation per leg AND one composed "
                "observation after temporal filtering; two safe legs do not compose into a safe "
                "crosswalk (§6.6). Claiming it without them is inference wearing a measurement's "
                "name")
        object.__setattr__(self, "execution_revision_id", "cwx_" + materialize_hash({
            "contract_version": CROSSWALK_CONTRACT_VERSION,
            "crosswalk_definition_revision_id": self.crosswalk_definition_revision_id,
            "mapping_binding_revision_id": self.mapping_binding_revision_id,
            "source_leg": self.source_leg.identity_payload(),
            "target_leg": self.target_leg.identity_payload(),
            "mapping_temporal_policy_revision_id": self.mapping_temporal_policy_revision_id,
            "applicability_scope": self.applicability_scope.identity_payload(),
            "leg_observation_revision_ids": list(observations),
            "composition_observation_revision_id": self.composition_observation_revision_id,
            "combined_cardinality": self.combined_cardinality.identity_payload(),
            "safety_status": self.safety_status.value,
        }))

    @property
    def execution_tier(self) -> ExecutionTier:
        """The tier is the SCOPE's — there is no second tier vocabulary here."""
        return self.applicability_scope.execution_tier
