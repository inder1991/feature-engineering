"""The COMPOSED crosswalk observation (Release C Task 11, profile plan §9).

**What the two-endpoint store cannot say, and why this exists.** A crosswalk is three tables. Each
LEG of it is an ordinary pair, and each leg is therefore observed by the shipped V2 machinery
untouched (``data_agent.relationship_observation`` + ``data_agent.store``) — this module does not
re-implement, wrap or widen that store, and nothing here rewrites a row it already holds. What no
number of two-endpoint observations can state is the COMPOSITION, and the adversarial review named
the three gaps precisely (A5/A8):

1. **Composed cross-leg fan-out.** How many TARGET rows one SOURCE row reaches through the mapping
   is not a leg property and is not the product of the two leg maxima — that product is an upper
   BOUND, and a bound recorded in a measurement's field is an inference wearing a measurement's
   name. It is measured over the joined shape and recorded here, in BOTH named directions.
2. **Atomic multi-leg as-of.** Two legs profiled at two instants describe two different worlds; a
   composition over them is a claim nobody observed. One instant, both legs, or the composition
   REFUSES (:data:`CROSSWALK_OBSERVATION_NOT_ATOMIC`).
3. **Lossy partition/predicate pinning.** ``EndpointTupleObservationV2.partitions_read`` keeps the
   partition VALUES and loses the column; ``RelationshipObservationV2.predicate_ids`` keeps the ids
   and loses the content. Neither can be replayed. The composed record carries the FULL pins —
   partitions with their column, predicates with their operator and values. The V2 store's own rows
   are deliberately NOT retrofitted: they are shipped, immutable and content-addressed, and
   rewriting them to close a gap in a new record is how stored history stops meaning what it said.

**Directions are SIDES, never traversal order** (the Task-10 caution, migration 1036's lesson):
``source`` and ``target`` name the crosswalk definition's canonically-ordered endpoints. A crosswalk
that is 1:1 forward and N:1 reverse is a real and common shape, and the two directions are recorded
and admitted independently.

**WHY A LEG MEASUREMENT IS EMBEDDED AND ITS V2 ROW ID IS OPTIONAL (verified constraint, not a
preference).** ``relationship_observation_revision.realization_revision_id`` is ``NOT NULL`` with a
foreign key to ``bridge_join_realization_revision`` (migration 1038:7-8). A CROSS-catalog leg has a
governed bridge realization by construction, so its observation IS an ordinary row in that store and
is written there unchanged. A SAME-catalog leg does not: it resolves through ``plan_join``, and
``JoinLegPinV1`` refuses a same-catalog pin that carries realization revisions precisely because
"pretending every leg is an entity bridge" is the defect Task 11 names. Storing such a leg in the
two-endpoint store would require FABRICATING a bridge realization — a claim that a governed direct
link exists between an endpoint and a mapping column, which discovery deliberately did not make, and
which ``record_relationship_observation`` could then DEMOTE on a conflict.

So every leg carries its FULL measurement here (:class:`CrosswalkLegObservationV1`), and
``v2_observation_revision_id`` names the shipped row when — and only when — the leg was also
recorded there. Both legs are measured by the SAME shipped V2 probe; what differs is whether the
result additionally belongs in a store keyed to the bridge family. Where both exist, the store
VERIFIES they agree rather than letting two places hold one truth.

**A sample may disprove uniqueness and never establish it** — the V2 rule
(``relationship_observation.uniqueness_verdict``), reused rather than re-derived.

**HOME** is ``overlay/upload`` (D5, ratified): the same home as the §6.4/§6.5 selection/temporal
contracts and as ``crosswalk.py``. **HASHING** is RFC-8785 JCS via ``materialize_hash`` (D1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from featuregen.data_agent.relationship_observation import (
    RelationshipMethod,
    RelationshipObservationV2,
    RowCoverage,
)
from featuregen.materialize.canonical import materialize_hash

#: Bumping this re-keys every composed observation, so it moves only with a real shape change.
CROSSWALK_OBSERVATION_CONTRACT_VERSION = "1.0.0"

#: Composed observations share the ``cwo_`` family prefix (CHECK-pinned by migration 1057).
CROSSWALK_OBSERVATION_ID_PREFIX = "cwo_"

# ── typed refusals ──────────────────────────────────────────────────────────────────────────────

#: The two leg observations pin different as-of instants (or different execution principals). A
#: composition over two worlds is a claim nobody observed.
CROSSWALK_OBSERVATION_NOT_ATOMIC = "crosswalk_observation_not_atomic"
#: A field a composed record cannot be read without: a blank id, a negative count, a fan-out that
#: contradicts the rows it was measured over.
CROSSWALK_OBSERVATION_MALFORMED = "crosswalk_observation_malformed"
#: One leg observation was offered as both legs, or a leg observation does not belong to the
#: definition revision being composed.
CROSSWALK_OBSERVATION_LEG_MISMATCH = "crosswalk_observation_leg_mismatch"

CROSSWALK_OBSERVATION_REFUSAL_CODES: frozenset[str] = frozenset({
    CROSSWALK_OBSERVATION_NOT_ATOMIC,
    CROSSWALK_OBSERVATION_MALFORMED,
    CROSSWALK_OBSERVATION_LEG_MISMATCH,
})


class CrosswalkObservationError(ValueError):
    """A composed-observation refusal carrying its CLOSED-vocabulary ``code``."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CROSSWALK_OBSERVATION_REFUSAL_CODES:
            raise ValueError(f"unknown crosswalk observation refusal code {code!r}")
        super().__init__(message)
        self.code = code


class CrosswalkObservationCaveat(StrEnum):
    """CLOSED vocabulary of what a composed measurement could not do — never a silent omission.

    A caveat is not a failure and not a refusal: the measurement HAPPENED and its numbers are real.
    It says which question the numbers do not answer, so a reader is never left to assume the
    strongest reading. The no-blocked rule applies verbatim: "nobody declared a row policy yet" is
    a different state from "this cannot work"."""

    #: No governed temporal policy exists for the mapping dataset, so the mapping rows were measured
    #: UNFILTERED. Uniqueness measured over full SCD history is a statement about the history, not
    #: about the current mapping — recording it silently as though it were the latter is the
    #: "measure uniqueness before time filtering" mutation Task 13 must kill.
    MAPPING_TEMPORAL_POLICY_ABSENT = "mapping_temporal_policy_absent"
    #: A policy exists but the resolver refused it for this request (typically: it reads rows AS OF
    #: an instant and the measurement named no cutoff). Distinct from ABSENT: somebody HAS declared
    #: how this dataset stores history, and the fix is a different one.
    MAPPING_TEMPORAL_POLICY_UNRESOLVED = "mapping_temporal_policy_unresolved"
    #: The mapping dataset is partitioned and the measurement read a subset of its partitions, so
    #: every count below is a statement about that subset.
    MAPPING_PARTITION_SCOPED = "mapping_partition_scoped"
    #: The SOURCE endpoint was read over a subset of its partitions. Named separately from the
    #: mapping's because a crosswalk reads three tables and a subset on ANY of them scopes every
    #: composed number — the endpoint sides had no caveat at all until this was found, so a probe
    #: over one region's accounts reported its uniqueness as though it had read the bank.
    SOURCE_PARTITION_SCOPED = "source_partition_scoped"
    #: The TARGET endpoint was read over a subset of its partitions.
    TARGET_PARTITION_SCOPED = "target_partition_scoped"
    #: At least one leg was observed with sampled/partial coverage or an approximate method, so the
    #: composition may DISPROVE uniqueness but can never establish it.
    LEG_EVIDENCE_NOT_EXACT = "leg_evidence_not_exact"


#: Every caveat that means "these numbers describe a partition SUBSET". A reader asks
#: :attr:`CrosswalkExecutionObservationV1.partition_scoped` rather than testing three names, and
#: admission asks the same question of the CONTENT so a missing caveat cannot widen a claim.
PARTITION_SCOPE_CAVEATS: frozenset[str] = frozenset({
    CrosswalkObservationCaveat.MAPPING_PARTITION_SCOPED.value,
    CrosswalkObservationCaveat.SOURCE_PARTITION_SCOPED.value,
    CrosswalkObservationCaveat.TARGET_PARTITION_SCOPED.value,
})


#: Operators whose CONTENT is the operator itself — "the row the source flags as current" binds no
#: value and needs none. Everything else must pin values or the parameter ref it bound to, because
#: an operator and a column alone cannot say what was selected.
_NULLARY_OPERATORS: frozenset[str] = frozenset({"is_true", "is_false", "is_null", "is_not_null"})


# ── the full pins the V2 rows cannot carry ──────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ObservedPartitionPinV1:
    """One partition restriction, WITH its column.

    ``EndpointTupleObservationV2.partitions_read`` is a bare value tuple: given ``('2026-07-01',)``
    a replayer cannot know whether that was ``business_dt`` or ``load_dt``, and picking wrong reads
    a different set of rows while reporting the same pin."""

    dataset_ref: str
    column: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("dataset_ref", "column"):
            if not str(getattr(self, name)).strip():
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED, f"a partition pin must name its {name}")
            object.__setattr__(self, name, str(getattr(self, name)).strip().lower())
        values = tuple(str(value) for value in self.values)
        if not values:
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                f"a partition pin on {self.dataset_ref!r} must select at least one value; an empty "
                "selector reads as 'no restriction', which is a different measurement")
        object.__setattr__(self, "values", values)

    def identity_payload(self) -> dict[str, object]:
        return {"dataset_ref": self.dataset_ref, "column": self.column,
                "values": list(self.values)}


@dataclass(frozen=True, slots=True)
class ObservedPredicatePinV1:
    """One governed predicate, WITH its content.

    ``RelationshipObservationV2.predicate_ids`` keeps only the id. Two policy revisions can share an
    id and select different values, so an id alone cannot say what was measured."""

    predicate_id: str
    dataset_ref: str
    column: str
    operator: str
    values: tuple[str, ...] = ()
    #: For a predicate whose right-hand side is a runtime PARAMETER rather than a literal set (the
    #: Release-B rule: a governed decision carries the cutoff's REF, never its value).
    parameter_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("predicate_id", "dataset_ref", "column", "operator"):
            if not str(getattr(self, name)).strip():
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED, f"a predicate pin must name its {name}")
            object.__setattr__(self, name, str(getattr(self, name)).strip().lower())
        object.__setattr__(self, "values", tuple(str(value) for value in self.values))
        if self.parameter_ref is not None:
            object.__setattr__(self, "parameter_ref", str(self.parameter_ref).strip() or None)
        if (not self.values and self.parameter_ref is None
                and self.operator not in _NULLARY_OPERATORS):
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                f"predicate {self.predicate_id!r} pins neither values nor a parameter ref, so "
                "nothing records what it actually selected")

    def identity_payload(self) -> dict[str, object]:
        return {"predicate_id": self.predicate_id, "dataset_ref": self.dataset_ref,
                "column": self.column, "operator": self.operator, "values": list(self.values),
                "parameter_ref": self.parameter_ref}


# ── the mapping table's own measurement ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MappingTupleObservationV1:
    """The mapping dataset, measured on BOTH of its tuples, AFTER the temporal filter.

    Two tuples and not one: a mapping row carries a source-side tuple and a target-side tuple, and
    they have independent uniqueness. The `duplicate`/`max_rows` pairs are what decide whether a
    traversal in each direction multiplies rows."""

    physical_id: str
    binding_revision_id: str
    binding_content_hash: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    row_count: int
    non_null_row_count: int
    distinct_source_tuple_count: int
    distinct_target_tuple_count: int
    distinct_pair_count: int
    duplicate_source_tuple_count: int
    duplicate_target_tuple_count: int
    max_rows_per_source_tuple: int
    max_rows_per_target_tuple: int
    partitions_read: tuple[ObservedPartitionPinV1, ...] = ()

    def __post_init__(self) -> None:
        for name in ("physical_id", "binding_revision_id", "binding_content_hash"):
            if not str(getattr(self, name)).strip():
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED,
                    f"a mapping observation must name its {name}")
        for name in ("source_columns", "target_columns"):
            columns = tuple(str(value).strip().lower() for value in getattr(self, name))
            if not columns or any(not column for column in columns):
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED,
                    f"a mapping observation must name its {name}")
            object.__setattr__(self, name, columns)
        if set(self.source_columns) & set(self.target_columns):
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                "a mapping row's two tuples must not share a column; one column serving both sides "
                "is a direct-equality bridge, not a crosswalk")
        for name in ("row_count", "non_null_row_count", "distinct_source_tuple_count",
                     "distinct_target_tuple_count", "distinct_pair_count",
                     "duplicate_source_tuple_count", "duplicate_target_tuple_count",
                     "max_rows_per_source_tuple", "max_rows_per_target_tuple"):
            if int(getattr(self, name)) < 0:
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED, f"{name} must not be negative")
        if self.non_null_row_count > self.row_count:
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                "a mapping observation cannot hold more non-null rows than rows")
        object.__setattr__(self, "partitions_read", tuple(self.partitions_read))

    @property
    def null_row_count(self) -> int:
        return max(0, self.row_count - self.non_null_row_count)

    @property
    def source_tuple_unique(self) -> bool:
        """Observed 1-row-per-source-tuple. NOT a verdict on its own — see
        :meth:`CrosswalkExecutionObservationV1.uniqueness_verdict`, which applies the exactness
        rule the V2 store owns."""
        return (self.non_null_row_count > 0
                and self.null_row_count == 0
                and self.max_rows_per_source_tuple <= 1
                and self.distinct_source_tuple_count == self.non_null_row_count)

    @property
    def target_tuple_unique(self) -> bool:
        return (self.non_null_row_count > 0
                and self.null_row_count == 0
                and self.max_rows_per_target_tuple <= 1
                and self.distinct_target_tuple_count == self.non_null_row_count)

    def identity_payload(self) -> dict[str, object]:
        return {
            "physical_id": self.physical_id,
            "binding_revision_id": self.binding_revision_id,
            "binding_content_hash": self.binding_content_hash,
            "source_columns": list(self.source_columns),
            "target_columns": list(self.target_columns),
            "row_count": self.row_count,
            "non_null_row_count": self.non_null_row_count,
            "distinct_source_tuple_count": self.distinct_source_tuple_count,
            "distinct_target_tuple_count": self.distinct_target_tuple_count,
            "distinct_pair_count": self.distinct_pair_count,
            "duplicate_source_tuple_count": self.duplicate_source_tuple_count,
            "duplicate_target_tuple_count": self.duplicate_target_tuple_count,
            "max_rows_per_source_tuple": self.max_rows_per_source_tuple,
            "max_rows_per_target_tuple": self.max_rows_per_target_tuple,
            "partitions_read": [pin.identity_payload() for pin in self.partitions_read],
        }


#: The two NAMED composed directions. They are SIDES of the canonically-ordered definition, never a
#: claim about which way somebody traversed.
SOURCE_TO_TARGET = "source_to_target"
TARGET_TO_SOURCE = "target_to_source"
COMPOSED_DIRECTIONS: tuple[str, str] = (SOURCE_TO_TARGET, TARGET_TO_SOURCE)

#: The two legs, named after the definition's own field names (§6.6).
SOURCE_TO_MAPPING = "source_to_mapping"
MAPPING_TO_TARGET = "mapping_to_target"
CROSSWALK_LEGS: tuple[str, str] = (SOURCE_TO_MAPPING, MAPPING_TO_TARGET)


# ── which applicability scope a measurement was taken under ─────────────────────────────────────
#
# MIGRATION 1057 PERSISTS `scope_id` AND NOTHING ELSE ABOUT THE SCOPE. The execution TIER, the
# environment and the purposes live on the caller's `RealizationApplicabilityScopeV1`, which is a
# value object nobody writes down — so two scopes that share a `scope_id` string and differ in TIER
# are indistinguishable in the store, and a measurement taken under a SANDBOX probe answers a
# PRODUCTION question with no check anywhere able to notice. (Proved by review: recorded under
# scope_id="probe-scope"/SANDBOX, read back under the same scope_id at PRODUCTION, attached and
# admitted.)
#
# The fix that needs no DDL: bind the scope's WHOLE identity into the one value that IS persisted.
# A measurement composed with :func:`scope_binding_id` records `<scope_id>#scope:<hash>`, so a
# reader can tell that the writer DECLARED which scope it ran under rather than only naming one. A
# bare `scope_id` still matches — Release C's own rows and any pre-existing 1057 row carry one —
# but it matches with no declared identity, and `crosswalk_assembly` refuses to serve such a
# measurement as production evidence.
#
# WHAT THIS DOES AND DOES NOT ESTABLISH. It closes accidental COLLISION: two scopes that share a
# name and differ in tier stop being one row. It does NOT close FORGERY, and cannot: the binding is
# a pure function of a public value object, so a producer can compute one for a scope it never
# executed under. Neither would the durable fix — a persisted `execution_tier` column is written by
# the same caller and is exactly as forgeable — which is why that fix (DEFERRED-WORK A.45) is
# justified by READABILITY and audit rather than by trust. Closing forgery needs a trusted
# execution attestation, which nothing in this repository produces.

#: Separates the human-readable scope name from the scope-identity digest. `#` cannot appear in a
#: hash and is not used by any scope_id in the repository, so a bound id is unambiguous.
SCOPE_BINDING_SEPARATOR = "#scope:"


def scope_binding_id(scope) -> str:
    """The ``scope_id`` a measurement records when it DECLARES the whole scope it ran under.

    ``scope`` is a :class:`~featuregen.overlay.upload.bridge_realization.
    RealizationApplicabilityScopeV1`; it is taken structurally (only ``identity_payload()`` and
    ``scope_id`` are read) so the observation contract does not have to import the bridge family.
    Structural typing is also why the separator is re-checked here rather than left to
    ``RealizationApplicabilityScopeV1.__post_init__``: a duck-typed caller never passes through it,
    and a scope whose NAME already spells a binding would mint ``name#scope:h1#scope:h2`` — a
    double-bound id whose prefix is itself a valid-looking bound id for another scope (ATTACK-2).
    """
    if SCOPE_BINDING_SEPARATOR in scope.scope_id:
        raise ValueError(
            f"scope_id {scope.scope_id!r} already contains {SCOPE_BINDING_SEPARATOR!r}: a scope "
            "NAME may not spell a binding, because a bare measurement recorded under that name "
            "would read as one that declared its whole scope identity")
    digest = materialize_hash(scope.identity_payload())[:16]
    return f"{scope.scope_id}{SCOPE_BINDING_SEPARATOR}{digest}"


def observation_scope_matches(recorded_scope_id: str, scope) -> bool:
    """Does a RECORDED ``scope_id`` name this applicability scope — bound or bare?

    Both spellings match, because a bare id is what every measurement recorded before the binding
    existed and dropping those rows on the floor would turn a real measurement into "unmeasured",
    which is a worse lie than the one being fixed. What the two spellings do NOT share is a
    declared scope identity — see :func:`observation_scope_is_proved`.
    """
    return recorded_scope_id in (scope.scope_id, scope_binding_id(scope))


def observation_scope_is_proved(recorded_scope_id: str, scope) -> bool:
    """Did the WRITER bind a full scope identity into what it recorded, matching this scope?

    **This is a claim about the writer, not a proof about the run, and the distinction is the whole
    honest reading of the mechanism.** :func:`scope_binding_id` is a public pure function, so any
    producer can compute the binding for a scope it never actually executed under and record it —
    the mechanism closes accidental COLLISION (two scopes sharing a name and differing in tier, the
    defect that let a sandbox probe answer a production question), not FORGERY. Nothing available
    without a trusted execution attestation would close forgery either: a persisted
    ``execution_tier`` column would be written by the same caller and be exactly as forgeable, which
    is why the durable fix in DEFERRED-WORK A.45 is about READABILITY and audit rather than about
    trust.

    False for a bare ``scope_id`` match, which establishes only that the two share a NAME: the tier
    is not persisted, so a sandbox probe and a production run both called ``"probe-scope"`` are one
    row as far as any reader can tell.
    """
    return recorded_scope_id == scope_binding_id(scope)


@dataclass(frozen=True, slots=True)
class CrosswalkLegObservationV1:
    """One measured LEG — the endpoint tuple against the mapping tuple.

    A faithful projection of what the shipped V2 probe measures for a pair (:meth:`from_v2` is the
    only builder in the measurement path), so a cross-catalog leg's embedded copy and its row in the
    two-endpoint store are the same numbers by construction. See the module docstring for why the
    row id is optional and the measurement is not."""

    leg: str
    leg_kind: str
    endpoint_dataset_ref: str
    endpoint_binding_revision_id: str
    endpoint_columns: tuple[str, ...]
    mapping_columns: tuple[str, ...]
    endpoint_row_count: int
    endpoint_non_null_row_count: int
    endpoint_distinct_tuple_count: int
    endpoint_duplicate_tuple_count: int
    endpoint_max_rows_per_tuple: int
    matched_endpoint_distinct: int
    unmatched_endpoint_distinct: int
    endpoint_orphan_rows: int
    joined_row_count: int
    #: How many MAPPING rows one endpoint row reaches, and the reverse. Per-leg; the COMPOSED
    #: fan-out across both legs is measured separately and is not their product.
    max_mapping_matches_per_endpoint_row: int
    max_endpoint_matches_per_mapping_row: int
    normalization_ids: tuple[str, ...]
    endpoint_source_snapshot_id: str
    mapping_source_snapshot_id: str
    #: The instant this leg was read at, and by whom. Carried per LEG because that is what the
    #: atomicity gate compares: two legs read at two instants describe two different worlds.
    as_of: str | None
    execution_principal: str
    method: str
    row_coverage: RowCoverage
    complete: bool
    partitions_read: tuple[ObservedPartitionPinV1, ...] = ()
    predicates: tuple[ObservedPredicatePinV1, ...] = ()
    #: The shipped two-endpoint row this leg was ALSO written as. Present iff a governed bridge
    #: realization backs the leg (a cross-catalog leg); ``None`` is the honest value otherwise, NOT
    #: a gap to be filled by fabricating a realization.
    v2_observation_revision_id: str | None = None
    #: The realization the V2 row is scoped to. Present exactly when the row id is.
    realization_revision_id: str | None = None

    def __post_init__(self) -> None:
        if self.leg not in CROSSWALK_LEGS:
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                f"leg must be one of {CROSSWALK_LEGS}, got {self.leg!r}")
        for name in ("endpoint_dataset_ref", "endpoint_binding_revision_id",
                     "endpoint_source_snapshot_id", "mapping_source_snapshot_id", "leg_kind",
                     "execution_principal"):
            if not str(getattr(self, name)).strip():
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED, f"a leg observation must name its {name}")
            object.__setattr__(self, name, str(getattr(self, name)).strip())
        for name in ("endpoint_columns", "mapping_columns"):
            columns = tuple(str(v).strip().lower() for v in getattr(self, name))
            if not columns or any(not column for column in columns):
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED, f"a leg observation must name its {name}")
            object.__setattr__(self, name, columns)
        if len(self.endpoint_columns) != len(self.mapping_columns):
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                "a leg joins two tuples of equal arity; unequal arity is not a leg")
        try:
            RelationshipMethod(self.method)
        except ValueError as exc:
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                f"leg method {self.method!r} is not a RelationshipMethod value") from exc
        object.__setattr__(self, "row_coverage", RowCoverage(self.row_coverage))
        for name in ("endpoint_row_count", "endpoint_non_null_row_count",
                     "endpoint_distinct_tuple_count", "endpoint_duplicate_tuple_count",
                     "endpoint_max_rows_per_tuple", "matched_endpoint_distinct",
                     "unmatched_endpoint_distinct", "endpoint_orphan_rows", "joined_row_count",
                     "max_mapping_matches_per_endpoint_row",
                     "max_endpoint_matches_per_mapping_row"):
            if int(getattr(self, name)) < 0:
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED, f"{name} must not be negative")
        object.__setattr__(self, "normalization_ids",
                           tuple(str(v) for v in self.normalization_ids))
        object.__setattr__(self, "partitions_read", tuple(self.partitions_read))
        object.__setattr__(self, "predicates", tuple(self.predicates))
        for name in ("v2_observation_revision_id", "realization_revision_id"):
            value = getattr(self, name)
            object.__setattr__(self, name, str(value).strip() if value else None)
        if bool(self.v2_observation_revision_id) != bool(self.realization_revision_id):
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_LEG_MISMATCH,
                "a leg recorded in the two-endpoint store names BOTH its observation revision and "
                "the realization it is scoped to; one without the other cannot be looked up")

    @classmethod
    def from_v2(
        cls, observation: RelationshipObservationV2, *, leg: str, leg_kind: str,
        mapping_side: str, persisted: bool,
        partitions_read: tuple[ObservedPartitionPinV1, ...] = (),
        predicates: tuple[ObservedPredicatePinV1, ...] = (),
    ) -> CrosswalkLegObservationV1:
        """Project one shipped V2 pair observation onto its crosswalk leg.

        ``mapping_side`` says which SIDE of the pair probe was the mapping table (``left``/
        ``right``), so the endpoint's numbers stay the endpoint's — the side assignment the V2
        contract preserves and D4 forbids collapsing.

        ``persisted`` is the caller's statement that this observation was written to the 1038 store.
        It is a parameter and not an inference because only the writer knows whether the write was
        offered and accepted, and a record that GUESSED would name a row that may not exist.
        """
        if mapping_side not in {"left", "right"}:
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                f"mapping_side must be left or right, got {mapping_side!r}")
        mapping_is_right = mapping_side == "right"
        endpoint = observation.left if mapping_is_right else observation.right
        mapping = observation.right if mapping_is_right else observation.left
        return cls(
            leg=leg,
            leg_kind=leg_kind,
            endpoint_dataset_ref=endpoint.physical_id,
            endpoint_binding_revision_id=endpoint.binding_revision_id,
            endpoint_columns=tuple(endpoint.columns),
            mapping_columns=tuple(mapping.columns),
            endpoint_row_count=endpoint.row_count,
            endpoint_non_null_row_count=endpoint.non_null_row_count,
            endpoint_distinct_tuple_count=endpoint.distinct_tuple_count,
            endpoint_duplicate_tuple_count=endpoint.duplicate_tuple_count,
            endpoint_max_rows_per_tuple=endpoint.max_rows_per_tuple,
            matched_endpoint_distinct=(observation.matched_left_distinct if mapping_is_right
                                       else observation.matched_right_distinct),
            unmatched_endpoint_distinct=(observation.unmatched_left_distinct if mapping_is_right
                                         else observation.unmatched_right_distinct),
            endpoint_orphan_rows=(observation.left_orphan_rows if mapping_is_right
                                  else observation.right_orphan_rows),
            joined_row_count=observation.joined_row_count,
            max_mapping_matches_per_endpoint_row=(
                observation.max_right_matches_per_left_row if mapping_is_right
                else observation.max_left_matches_per_right_row),
            max_endpoint_matches_per_mapping_row=(
                observation.max_left_matches_per_right_row if mapping_is_right
                else observation.max_right_matches_per_left_row),
            normalization_ids=tuple(observation.normalization_ids),
            endpoint_source_snapshot_id=(observation.left_source_snapshot_id if mapping_is_right
                                         else observation.right_source_snapshot_id),
            mapping_source_snapshot_id=(observation.right_source_snapshot_id if mapping_is_right
                                        else observation.left_source_snapshot_id),
            as_of=observation.snapshot_or_as_of,
            execution_principal=observation.execution_principal,
            method=observation.method,
            row_coverage=observation.row_coverage,
            complete=observation.complete,
            partitions_read=partitions_read,
            predicates=predicates,
            v2_observation_revision_id=(
                observation.observation_revision_id if persisted else None),
            realization_revision_id=observation.realization_revision_id if persisted else None,
        )

    @property
    def leg_observation_id(self) -> str:
        """This leg measurement's own content-addressed identity.

        EVERY leg has one, whether or not the two-endpoint store could hold it — which is what lets
        the execution revision satisfy its "an observation per leg" rule for the ordinary shape
        (one same-catalog leg, one cross-catalog leg) without fabricating a bridge realization to
        manufacture a second V2 row."""
        return "clo_" + materialize_hash(self.identity_payload())

    def identity_payload(self) -> dict[str, object]:
        return {
            "leg": self.leg,
            "leg_kind": self.leg_kind,
            "endpoint_dataset_ref": self.endpoint_dataset_ref,
            "endpoint_binding_revision_id": self.endpoint_binding_revision_id,
            "endpoint_columns": list(self.endpoint_columns),
            "mapping_columns": list(self.mapping_columns),
            "endpoint_row_count": self.endpoint_row_count,
            "endpoint_non_null_row_count": self.endpoint_non_null_row_count,
            "endpoint_distinct_tuple_count": self.endpoint_distinct_tuple_count,
            "endpoint_duplicate_tuple_count": self.endpoint_duplicate_tuple_count,
            "endpoint_max_rows_per_tuple": self.endpoint_max_rows_per_tuple,
            "matched_endpoint_distinct": self.matched_endpoint_distinct,
            "unmatched_endpoint_distinct": self.unmatched_endpoint_distinct,
            "endpoint_orphan_rows": self.endpoint_orphan_rows,
            "joined_row_count": self.joined_row_count,
            "max_mapping_matches_per_endpoint_row": self.max_mapping_matches_per_endpoint_row,
            "max_endpoint_matches_per_mapping_row": self.max_endpoint_matches_per_mapping_row,
            "normalization_ids": list(self.normalization_ids),
            "endpoint_source_snapshot_id": self.endpoint_source_snapshot_id,
            "mapping_source_snapshot_id": self.mapping_source_snapshot_id,
            "as_of": self.as_of,
            "execution_principal": self.execution_principal,
            "method": self.method,
            "row_coverage": self.row_coverage.value,
            "complete": self.complete,
            "partitions_read": [pin.identity_payload() for pin in self.partitions_read],
            "predicates": [pin.identity_payload() for pin in self.predicates],
            "v2_observation_revision_id": self.v2_observation_revision_id,
            "realization_revision_id": self.realization_revision_id,
        }


@dataclass(frozen=True, slots=True)
class CrosswalkExecutionObservationV1:
    """One measured composition of a crosswalk definition revision.

    Identity is the CONTENT — every pin, every count, the instant, the principal — under
    ``materialize_hash``, so two measurements that observed the same thing are one row and a
    measurement that differed anywhere is a different row. Nothing about admission, safety or review
    lives here: this record says what was SEEN, and
    ``crosswalk_admission.evaluate_crosswalk_admission`` is the only thing that turns seeing into a
    verdict."""

    crosswalk_definition_revision_id: str
    #: The two per-LEG measurements this composition stands on, BY SIDE — full measurements, each
    #: naming its shipped two-endpoint row when it has one (module docstring). ``source_leg`` is the
    #: ``source_to_mapping`` leg and ``target_leg`` the ``mapping_to_target`` leg of the definition's
    #: canonically-ordered sides.
    source_leg: CrosswalkLegObservationV1
    target_leg: CrosswalkLegObservationV1
    mapping_binding_revision_id: str
    mapping: MappingTupleObservationV1
    scope_id: str
    #: The ONE instant both legs and the mapping were read at. ``None`` only when no leg pinned one.
    as_of: str | None
    matched_source_distinct: int
    unmatched_source_distinct: int
    matched_target_distinct: int
    unmatched_target_distinct: int
    composed_row_count: int
    #: MEASURED over the joined shape, never the product of the leg maxima (which is a bound).
    source_to_target_max_matches: int
    target_to_source_max_matches: int
    partitions: tuple[ObservedPartitionPinV1, ...]
    predicates: tuple[ObservedPredicatePinV1, ...]
    execution_principal: str
    method: str
    row_coverage: RowCoverage
    complete: bool
    observed_at: datetime
    mapping_temporal_policy_revision_id: str | None = None
    #: The content hash of the ``DatasetRowSelectionV1`` actually applied to the mapping rows. A
    #: policy REVISION id says which rule; this says which resolved row rule ran.
    mapping_row_selection_hash: str | None = None
    caveats: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    producer: str = "profiler"
    strength: str = "supported"
    observation_revision_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        for name in ("crosswalk_definition_revision_id", "mapping_binding_revision_id",
                     "scope_id", "execution_principal"):
            if not str(getattr(self, name)).strip():
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED, f"{name} must not be blank")
            object.__setattr__(self, name, str(getattr(self, name)).strip())
        if self.source_leg.leg != SOURCE_TO_MAPPING or self.target_leg.leg != MAPPING_TO_TARGET:
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_LEG_MISMATCH,
                f"a composition carries the {SOURCE_TO_MAPPING!r} leg as its source leg and the "
                f"{MAPPING_TO_TARGET!r} leg as its target leg; swapping them silently inverts every "
                "direction this record reports")
        if self.source_leg.identity_payload() == self.target_leg.identity_payload():
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_LEG_MISMATCH,
                "one leg measurement offered as both legs is the 'omit one leg' mutation wearing a "
                "composition's shape")
        try:
            RelationshipMethod(self.method)
        except ValueError as exc:
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                f"method {self.method!r} is not a RelationshipMethod value — the composed record "
                "keeps the V2 axes verbatim (D4)") from exc
        object.__setattr__(self, "row_coverage", RowCoverage(self.row_coverage))
        for name in ("matched_source_distinct", "unmatched_source_distinct",
                     "matched_target_distinct", "unmatched_target_distinct",
                     "composed_row_count", "source_to_target_max_matches",
                     "target_to_source_max_matches"):
            if int(getattr(self, name)) < 0:
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED, f"{name} must not be negative")
        if self.composed_row_count == 0 and (
                self.source_to_target_max_matches or self.target_to_source_max_matches):
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                "a composition that joined no rows cannot have observed a fan-out; a maximum over "
                "an empty join is zero, not an unmeasured blank")
        object.__setattr__(self, "partitions", tuple(self.partitions))
        object.__setattr__(self, "predicates", tuple(self.predicates))
        caveats = tuple(sorted({str(value).strip() for value in self.caveats if str(value).strip()}))
        for caveat in caveats:
            CrosswalkObservationCaveat(caveat)          # closed vocabulary, raises on a stranger
        object.__setattr__(self, "caveats", caveats)
        object.__setattr__(self, "failures", tuple(str(value) for value in self.failures))
        if self.as_of is not None:
            as_of = str(self.as_of).strip()
            if not as_of:
                raise CrosswalkObservationError(
                    CROSSWALK_OBSERVATION_MALFORMED,
                    "a blank as-of instant declares nothing; use None")
            object.__setattr__(self, "as_of", as_of)
        object.__setattr__(
            self, "observation_revision_id",
            CROSSWALK_OBSERVATION_ID_PREFIX + materialize_hash(self.content_payload()))

    # ── content ─────────────────────────────────────────────────────────────────────────────────

    def content_payload(self) -> dict[str, object]:
        """Everything the identity covers — what a store read-back re-hashes to verify (D1)."""
        return {
            "contract_version": CROSSWALK_OBSERVATION_CONTRACT_VERSION,
            "crosswalk_definition_revision_id": self.crosswalk_definition_revision_id,
            "source_leg": self.source_leg.identity_payload(),
            "target_leg": self.target_leg.identity_payload(),
            "mapping_binding_revision_id": self.mapping_binding_revision_id,
            "mapping": self.mapping.identity_payload(),
            "scope_id": self.scope_id,
            "as_of": self.as_of,
            "matched_source_distinct": self.matched_source_distinct,
            "unmatched_source_distinct": self.unmatched_source_distinct,
            "matched_target_distinct": self.matched_target_distinct,
            "unmatched_target_distinct": self.unmatched_target_distinct,
            "composed_row_count": self.composed_row_count,
            "source_to_target_max_matches": self.source_to_target_max_matches,
            "target_to_source_max_matches": self.target_to_source_max_matches,
            "partitions": [pin.identity_payload() for pin in self.partitions],
            "predicates": [pin.identity_payload() for pin in self.predicates],
            "execution_principal": self.execution_principal,
            "method": self.method,
            "row_coverage": self.row_coverage.value,
            "complete": self.complete,
            "observed_at": self.observed_at.isoformat(),
            "mapping_temporal_policy_revision_id": self.mapping_temporal_policy_revision_id,
            "mapping_row_selection_hash": self.mapping_row_selection_hash,
            "caveats": list(self.caveats),
            "failures": list(self.failures),
            "producer": self.producer,
            "strength": self.strength,
        }

    @property
    def current_scope_key(self) -> str:
        """Same crosswalk revision / scope / pins / principal — the counts may advance.

        Deliberately parallel to ``RelationshipObservationV2.current_scope_key``: what identifies
        "the current answer to THIS question" excludes the answer itself, so a re-measurement of the
        same question replaces rather than accumulates."""
        return materialize_hash({
            "contract_version": CROSSWALK_OBSERVATION_CONTRACT_VERSION,
            "crosswalk_definition_revision_id": self.crosswalk_definition_revision_id,
            "scope_id": self.scope_id,
            "mapping_binding_revision_id": self.mapping_binding_revision_id,
            "mapping_source_columns": list(self.mapping.source_columns),
            "mapping_target_columns": list(self.mapping.target_columns),
            "mapping_temporal_policy_revision_id": self.mapping_temporal_policy_revision_id,
            "partitions": [pin.identity_payload() for pin in self.partitions],
            "predicates": [pin.identity_payload() for pin in self.predicates],
            "as_of": self.as_of,
            "execution_principal": self.execution_principal,
        })

    # ── verdicts the store and admission READ (never re-derive) ─────────────────────────────────

    @property
    def leg_observation_revision_ids(self) -> tuple[str, ...]:
        """The PERSISTED two-endpoint rows (``rob_``) this composition names, in leg order.

        SHORTER THAN TWO is a legitimate answer, not a gap: only a leg backed by a governed bridge
        realization can be stored there (module docstring), so the ordinary
        one-same-catalog/one-cross-catalog shape yields exactly one.

        CONSUMED BY ``crosswalk_admission.admitted_crosswalk_execution`` for the execution
        revision's field of the same name — which is the field's real meaning, and is deliberately
        NOT what that revision's deterministic-validation gate keys on. That gate reads
        :attr:`leg_measurement_ids`, because requiring two persisted rows would demand a fabricated
        bridge realization for a leg that has none."""
        return tuple(leg.v2_observation_revision_id
                     for leg in (self.source_leg, self.target_leg)
                     if leg.v2_observation_revision_id)

    @property
    def leg_measurement_ids(self) -> tuple[str, str]:
        """Both legs' own content-addressed identities, in leg order. ALWAYS two — see
        :attr:`CrosswalkLegObservationV1.leg_observation_id`.

        CONSUMED BY ``crosswalk_admission.admitted_crosswalk_execution`` for the execution
        revision's ``leg_measurement_ids``, which its deterministic-validation gate keys on."""
        return (self.source_leg.leg_observation_id, self.target_leg.leg_observation_id)

    @property
    def partition_scoped(self) -> bool:
        """Did ANY side read a partition SUBSET? Then every number here describes that subset.

        Read from the CONTENT — the composed pins and all three tables' own pins — and not only
        from :attr:`caveats`, because a record assembled by hand (or by a future writer that forgot
        the caveat) would otherwise be admitted as though it had read everything. The caveats are
        consulted too, so a record that says it was scoped is believed even where the pins it
        carried were not preserved.

        This is what ``crosswalk_admission`` fuses with the applicability scope's
        ``partition_scope_ref``; the bridge family has kept the same rule since it shipped."""
        return bool(self.partitions
                    or self.mapping.partitions_read
                    or self.source_leg.partitions_read
                    or self.target_leg.partitions_read
                    or (set(self.caveats) & PARTITION_SCOPE_CAVEATS))

    @property
    def exact_and_complete(self) -> bool:
        return (self.complete
                and self.row_coverage is RowCoverage.FULL
                and self.method == RelationshipMethod.EXACT.value)

    def uniqueness_verdict(self, direction: str) -> str:
        """``unique``/``not_unique``/``unknown`` for one NAMED direction.

        The V2 asymmetry, verbatim: a duplicate or a null DISPROVES however it was found, while
        finding none proves nothing unless the probe was exact, complete and full-coverage. The
        traversal ``source_to_target`` lands on the mapping's TARGET tuple, so that tuple's
        duplicates are what would multiply rows."""
        if direction not in COMPOSED_DIRECTIONS:
            raise CrosswalkObservationError(
                CROSSWALK_OBSERVATION_MALFORMED,
                f"direction must be one of {COMPOSED_DIRECTIONS}, got {direction!r}")
        forward = direction == SOURCE_TO_TARGET
        observed_max = (self.source_to_target_max_matches if forward
                        else self.target_to_source_max_matches)
        landing_duplicates = (self.mapping.duplicate_target_tuple_count if forward
                              else self.mapping.duplicate_source_tuple_count)
        if landing_duplicates > 0 or self.mapping.null_row_count > 0 or observed_max > 1:
            return "not_unique"
        if self.composed_row_count == 0:
            return "unknown"
        if not self.exact_and_complete:
            return "unknown"
        return "unique"

    @property
    def source_distinct_containment_ratio(self) -> float:
        total = self.matched_source_distinct + self.unmatched_source_distinct
        return self.matched_source_distinct / total if total else 0.0

    @property
    def target_distinct_containment_ratio(self) -> float:
        total = self.matched_target_distinct + self.unmatched_target_distinct
        return self.matched_target_distinct / total if total else 0.0


# ── composition (the atomic as-of gate) ─────────────────────────────────────────────────────────

def compose_crosswalk_observation(
    *,
    crosswalk_definition_revision_id: str,
    source_leg: CrosswalkLegObservationV1,
    target_leg: CrosswalkLegObservationV1,
    mapping: MappingTupleObservationV1,
    scope_id: str,
    matched_source_distinct: int,
    unmatched_source_distinct: int,
    matched_target_distinct: int,
    unmatched_target_distinct: int,
    composed_row_count: int,
    source_to_target_max_matches: int,
    target_to_source_max_matches: int,
    observed_at: datetime,
    partitions: tuple[ObservedPartitionPinV1, ...] = (),
    predicates: tuple[ObservedPredicatePinV1, ...] = (),
    mapping_temporal_policy_revision_id: str | None = None,
    mapping_row_selection_hash: str | None = None,
    caveats: tuple[str, ...] = (),
    failures: tuple[str, ...] = (),
    complete: bool = True,
) -> CrosswalkExecutionObservationV1:
    """Build the composed record from two persisted leg observations. REFUSES a non-atomic pair.

    The atomicity rule is the whole reason this is a function rather than a constructor call: the
    two legs must have been read at ONE instant, by ONE principal. Two legs an hour apart describe
    two states of the bank, and every composed number over them — coverage, fan-out, orphans — is
    about a world that never existed. There is no "close enough" tolerance, deliberately: a
    tolerance is a policy, and a policy belongs where somebody can review it, not inside an identity.

    Method, coverage and completeness are the WEAKEST of the two legs, never the composer's
    optimism: a composition can be no more exact than the least exact thing it stands on.
    """
    if source_leg.as_of != target_leg.as_of:
        raise CrosswalkObservationError(
            CROSSWALK_OBSERVATION_NOT_ATOMIC,
            f"the source leg was read as of {source_leg.as_of!r} and the target leg as of "
            f"{target_leg.as_of!r}. A composition over two instants describes a state of "
            "the bank that never existed; re-measure both legs at one instant")
    if source_leg.execution_principal != target_leg.execution_principal:
        raise CrosswalkObservationError(
            CROSSWALK_OBSERVATION_NOT_ATOMIC,
            "the two legs were read by different execution principals, so they may have seen "
            "different rows of the same tables; a composition cannot say which")

    method = (RelationshipMethod.EXACT.value
              if source_leg.method == RelationshipMethod.EXACT.value
              and target_leg.method == RelationshipMethod.EXACT.value
              else RelationshipMethod.APPROXIMATE.value)
    coverage = _weakest_coverage(source_leg.row_coverage, target_leg.row_coverage)
    all_caveats = set(caveats)
    if method != RelationshipMethod.EXACT.value or coverage is not RowCoverage.FULL:
        all_caveats.add(CrosswalkObservationCaveat.LEG_EVIDENCE_NOT_EXACT.value)
    # EVERY side, not just the mapping. A crosswalk reads three tables, and a partition subset on
    # any one of them scopes the composed coverage, the composed fan-out and both uniqueness
    # verdicts alike. The endpoint sides raised nothing at all until this was found.
    if mapping.partitions_read:
        all_caveats.add(CrosswalkObservationCaveat.MAPPING_PARTITION_SCOPED.value)
    if source_leg.partitions_read:
        all_caveats.add(CrosswalkObservationCaveat.SOURCE_PARTITION_SCOPED.value)
    if target_leg.partitions_read:
        all_caveats.add(CrosswalkObservationCaveat.TARGET_PARTITION_SCOPED.value)
    if mapping_temporal_policy_revision_id is None and not (
            all_caveats & {CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value,
                           CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value}):
        # NEVER silently pretend the mapping was current. Which of the two codes applies is the
        # measurement pass's to say (absent policy vs a policy the resolver refused); with neither
        # supplied the honest default is the weaker claim.
        all_caveats.add(CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value)

    return CrosswalkExecutionObservationV1(
        crosswalk_definition_revision_id=crosswalk_definition_revision_id,
        source_leg=source_leg,
        target_leg=target_leg,
        mapping_binding_revision_id=mapping.binding_revision_id,
        mapping=mapping,
        scope_id=scope_id,
        as_of=source_leg.as_of,
        matched_source_distinct=matched_source_distinct,
        unmatched_source_distinct=unmatched_source_distinct,
        matched_target_distinct=matched_target_distinct,
        unmatched_target_distinct=unmatched_target_distinct,
        composed_row_count=composed_row_count,
        source_to_target_max_matches=source_to_target_max_matches,
        target_to_source_max_matches=target_to_source_max_matches,
        partitions=partitions,
        predicates=predicates,
        execution_principal=source_leg.execution_principal,
        method=method,
        row_coverage=coverage,
        complete=bool(complete and source_leg.complete and target_leg.complete),
        observed_at=observed_at,
        mapping_temporal_policy_revision_id=mapping_temporal_policy_revision_id,
        mapping_row_selection_hash=mapping_row_selection_hash,
        caveats=tuple(sorted(all_caveats)),
        failures=failures,
    )


_COVERAGE_RANK = {RowCoverage.FULL: 2, RowCoverage.SAMPLED: 1, RowCoverage.PARTIAL: 0}


def _weakest_coverage(left: RowCoverage, right: RowCoverage) -> RowCoverage:
    return left if _COVERAGE_RANK[left] <= _COVERAGE_RANK[right] else right


# ── JSON projection (what the store persists and reads back) ────────────────────────────────────

def crosswalk_observation_to_json(
    observation: CrosswalkExecutionObservationV1,
) -> dict[str, object]:
    return observation.content_payload()


def crosswalk_observation_from_json(payload: dict) -> CrosswalkExecutionObservationV1:
    """Rebuild from a stored payload. A payload that no longer satisfies the contract RAISES."""
    mapping_payload = payload["mapping"]
    mapping = MappingTupleObservationV1(
        physical_id=str(mapping_payload["physical_id"]),
        binding_revision_id=str(mapping_payload["binding_revision_id"]),
        binding_content_hash=str(mapping_payload["binding_content_hash"]),
        source_columns=tuple(str(v) for v in mapping_payload["source_columns"]),
        target_columns=tuple(str(v) for v in mapping_payload["target_columns"]),
        row_count=int(mapping_payload["row_count"]),
        non_null_row_count=int(mapping_payload["non_null_row_count"]),
        distinct_source_tuple_count=int(mapping_payload["distinct_source_tuple_count"]),
        distinct_target_tuple_count=int(mapping_payload["distinct_target_tuple_count"]),
        distinct_pair_count=int(mapping_payload["distinct_pair_count"]),
        duplicate_source_tuple_count=int(mapping_payload["duplicate_source_tuple_count"]),
        duplicate_target_tuple_count=int(mapping_payload["duplicate_target_tuple_count"]),
        max_rows_per_source_tuple=int(mapping_payload["max_rows_per_source_tuple"]),
        max_rows_per_target_tuple=int(mapping_payload["max_rows_per_target_tuple"]),
        partitions_read=tuple(
            _partition_from_json(pin) for pin in mapping_payload["partitions_read"]),
    )
    return CrosswalkExecutionObservationV1(
        crosswalk_definition_revision_id=str(payload["crosswalk_definition_revision_id"]),
        source_leg=_leg_from_json(payload["source_leg"]),
        target_leg=_leg_from_json(payload["target_leg"]),
        mapping_binding_revision_id=str(payload["mapping_binding_revision_id"]),
        mapping=mapping,
        scope_id=str(payload["scope_id"]),
        as_of=payload.get("as_of"),
        matched_source_distinct=int(payload["matched_source_distinct"]),
        unmatched_source_distinct=int(payload["unmatched_source_distinct"]),
        matched_target_distinct=int(payload["matched_target_distinct"]),
        unmatched_target_distinct=int(payload["unmatched_target_distinct"]),
        composed_row_count=int(payload["composed_row_count"]),
        source_to_target_max_matches=int(payload["source_to_target_max_matches"]),
        target_to_source_max_matches=int(payload["target_to_source_max_matches"]),
        partitions=tuple(_partition_from_json(pin) for pin in payload["partitions"]),
        predicates=tuple(_predicate_from_json(pin) for pin in payload["predicates"]),
        execution_principal=str(payload["execution_principal"]),
        method=str(payload["method"]),
        row_coverage=RowCoverage(payload["row_coverage"]),
        complete=bool(payload["complete"]),
        observed_at=datetime.fromisoformat(str(payload["observed_at"])),
        mapping_temporal_policy_revision_id=payload.get("mapping_temporal_policy_revision_id"),
        mapping_row_selection_hash=payload.get("mapping_row_selection_hash"),
        caveats=tuple(payload.get("caveats") or ()),
        failures=tuple(payload.get("failures") or ()),
        producer=str(payload.get("producer", "profiler")),
        strength=str(payload.get("strength", "supported")),
    )


def _leg_from_json(payload: dict) -> CrosswalkLegObservationV1:
    return CrosswalkLegObservationV1(
        leg=str(payload["leg"]),
        leg_kind=str(payload["leg_kind"]),
        endpoint_dataset_ref=str(payload["endpoint_dataset_ref"]),
        endpoint_binding_revision_id=str(payload["endpoint_binding_revision_id"]),
        endpoint_columns=tuple(str(v) for v in payload["endpoint_columns"]),
        mapping_columns=tuple(str(v) for v in payload["mapping_columns"]),
        endpoint_row_count=int(payload["endpoint_row_count"]),
        endpoint_non_null_row_count=int(payload["endpoint_non_null_row_count"]),
        endpoint_distinct_tuple_count=int(payload["endpoint_distinct_tuple_count"]),
        endpoint_duplicate_tuple_count=int(payload["endpoint_duplicate_tuple_count"]),
        endpoint_max_rows_per_tuple=int(payload["endpoint_max_rows_per_tuple"]),
        matched_endpoint_distinct=int(payload["matched_endpoint_distinct"]),
        unmatched_endpoint_distinct=int(payload["unmatched_endpoint_distinct"]),
        endpoint_orphan_rows=int(payload["endpoint_orphan_rows"]),
        joined_row_count=int(payload["joined_row_count"]),
        max_mapping_matches_per_endpoint_row=int(
            payload["max_mapping_matches_per_endpoint_row"]),
        max_endpoint_matches_per_mapping_row=int(
            payload["max_endpoint_matches_per_mapping_row"]),
        normalization_ids=tuple(str(v) for v in payload["normalization_ids"]),
        endpoint_source_snapshot_id=str(payload["endpoint_source_snapshot_id"]),
        mapping_source_snapshot_id=str(payload["mapping_source_snapshot_id"]),
        as_of=payload.get("as_of"),
        execution_principal=str(payload["execution_principal"]),
        method=str(payload["method"]),
        row_coverage=RowCoverage(payload["row_coverage"]),
        complete=bool(payload["complete"]),
        partitions_read=tuple(_partition_from_json(pin) for pin in payload["partitions_read"]),
        predicates=tuple(_predicate_from_json(pin) for pin in payload["predicates"]),
        v2_observation_revision_id=payload.get("v2_observation_revision_id"),
        realization_revision_id=payload.get("realization_revision_id"))


def _partition_from_json(payload: dict) -> ObservedPartitionPinV1:
    return ObservedPartitionPinV1(
        dataset_ref=str(payload["dataset_ref"]), column=str(payload["column"]),
        values=tuple(str(v) for v in payload["values"]))


def _predicate_from_json(payload: dict) -> ObservedPredicatePinV1:
    return ObservedPredicatePinV1(
        predicate_id=str(payload["predicate_id"]), dataset_ref=str(payload["dataset_ref"]),
        column=str(payload["column"]), operator=str(payload["operator"]),
        values=tuple(str(v) for v in payload.get("values") or ()),
        parameter_ref=payload.get("parameter_ref"))
