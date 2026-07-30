"""Spec §4 / §4.2 — the entity population: a DECLARATION that governed facts may only validate.

`ENTITY_ASSIGNMENT` proves a column denotes an entity. `GRAIN` proves some columns identify rows.
**Neither proves that a table contains every member of the population** — nor that it retains
inactive members, nor that it is the authoritative record. `kyc_customers`, `card_customers` and
`customers` can look *identical* to the catalog: same entity, same unique `cif_id`, same
availability column. Choosing among them from those facts is inference, and the inference is
invisible when it is wrong — the spine simply has fewer rows, every feature silently drops those
customers, and every number that follows is plausible.

So the direction of authority is fixed and one-way:

    the DECLARATION chooses the source;  governed facts may only VALIDATE or REJECT it.

Nothing in this module inspects the catalog to find a table. Every catalog read is keyed by a ref
that came out of the declaration, which is why a test can record every statement issued and prove
the rival population is never read at all. A helper here that ranked or filtered tables would be
the defect this task exists to prevent, not a convenience.

**What the facts are allowed to say.** They can refute a declaration four ways, and each refusal
names the governed fact that did the refuting:

* the declared entity is not the governed `entity_assignment` on the keys (wrong entity);
* a declared key is not a governed `GRAIN` column, or the governed grain is WIDER than the declared
  keys in a way the snapshot policy does not collapse (the keys are provably not unique);
* the caller may not read a column the spine reads (`READ_SCOPE_INSUFFICIENT` — a different remedy
  from a wrong declaration, so a different code, and reported first so a denied read is never
  dressed up as a governance disagreement);
* the declared availability column carries no governed `AVAILABILITY_TIME`
  (`AVAILABILITY_TIME_NOT_GOVERNED`).

A HINT never validates anything. `build_graph` writes `is_grain` and `entity` straight from an
upload; those become governed only when the fact projection attaches the `*_fact_event_id` link
(`grain_fact_event_id`, `entity_status='VERIFIED'`). Authority is read through the shipped governed
readers — `read_operational_value` (C1) and `effective_entity` — never re-derived here.

**`SnapshotPolicy` (§4.2) is closed and load-bearing.** Rev 3 named it without designing it, which
left an implementation free to read the whole customer table: that produces duplicate spine keys,
or leaks customer versions created after `business_dt` into a past one. The four variants each say
exactly how a snapshot maps to a business date, and each declares which grain columns it COLLAPSES.
That single question — "does the policy reduce the governed grain to one row per key?" — is the
declaration-time half of PIT rules 2, 3 and 5, and it is checkable without reading a row:

    governed_grain ⊇ declared_keys       else the keys are not attested row identity
    governed_grain − declared_keys ⊆ collapsed_by(policy)   else duplicate spine keys

The other halves need actual rows and belong to the runtime gates in the generated pipeline
(`SPINE_DUPLICATE_KEY`, the tie-break gate, `SPINE_INCOMPLETE`) — §14 types them as
`ValidationGateCode` deliberately, and they are NOT reachable from here.

**No free-text predicate anywhere.** Any active/current-row selection is structured through
`ActivePopulation`; a SQL string would smuggle ungoverned business logic into the definition of who
the customers are, where no governance check could see it.

**Identity excludes provenance.** Only the semantic payload enters the contract hash. Two people
making the same declaration must produce the same contract, or one population would split into two
materialization groups. `declared_by` is an actor envelope THREADED from the request — this module
never builds one, and never asserts an authenticated identity.

Deferred (spec §4, recorded not assumed): a governed `ENTITY_POPULATION_SOURCE` fact would
SUPERSEDE this declaration, and existing declarations would then be checked against it, with
disagreement blocking materialization. None exists today.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from featuregen.contracts.db import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.overlay.upload.entity import GOVERNED_ENTITY, effective_entity
from featuregen.overlay.upload.governed_grain import governed_grain_columns
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.operational_facts import read_operational_value
from featuregen.overlay.upload.read_scope import allowed_sensitivities

__all__ = [
    "SNAPSHOT_POLICY_TYPES",
    "ActivePopulation",
    "ClaimAuthority",
    "CurrentSnapshot",
    "LatestAvailableAsOf",
    "PartitionMappedSnapshot",
    "PopulationSemantics",
    "SnapshotPolicy",
    "SnapshotPolicyKind",
    "SpineSourceDeclarationV1",
    "SpineSpec",
    "validate_spine_declaration",
]

#: `read_column_facts` renders the boolean fact flags as strings (`is_grain`, `is_as_of`).
_GOVERNED_TRUE = "true"
#: C1's operational status for a governed read.
_RESOLVED = "resolved"


class PopulationSemantics(StrEnum):
    """What the declarer CLAIMS the source contains. CLOSED (spec §4).

    The claim is not decoration: §4.1 checks it against reality. A grain key present in the
    aggregates and absent from the spine proves a `CURRENT_COMPLETE_POPULATION` claim false and
    blocks publication; under any narrower claim the same orphan is expected and is only counted.
    """

    CURRENT_COMPLETE_POPULATION = "current_complete_population"
    CURRENT_ACTIVE_ONLY = "current_active_only"
    HISTORICAL_AS_OF = "historical_as_of"


class SnapshotPolicyKind(StrEnum):
    """How a snapshot of the source maps to a business date. CLOSED (spec §4.2)."""

    CURRENT_SNAPSHOT = "current_snapshot"
    LATEST_AVAILABLE_AS_OF = "latest_available_as_of"
    PARTITION_MAPPED = "partition_mapped"
    ACTIVE_POPULATION = "active_population"


class ClaimAuthority(StrEnum):
    """Who stands behind a population claim.

    One member on purpose. §4 requires a completeness claim to be recorded as a HUMAN DECLARATION,
    never as a governed catalog fact, and labelled as such wherever it is surfaced — so there is no
    ``governed`` member for a future edit to reach for. When the deferred `ENTITY_POPULATION_SOURCE`
    fact arrives it adds one, and that addition is the visible moment the authority changed.
    """

    HUMAN_DECLARATION = "human_declaration"


@dataclass(frozen=True, slots=True)
class CurrentSnapshot:
    """The table IS the current population; it holds no history.

    ``observed_snapshot_ref`` is REQUIRED. A present-day table cannot honestly answer an arbitrary
    historical business date, so the vintage it was observed at is recorded and a mismatch is meant
    to refuse rather than quietly answer with today's rows. Recording it here is the declaration-time
    half; comparing it against a run's business date is run preparation's, because no business date
    exists at compile time (§3.3).

    It is an opaque declared identifier (a date or a version), NOT a column reference — a table with
    no history has nothing to reference. It enters identity: two vintages are two populations.
    """

    observed_snapshot_ref: str
    kind: ClassVar[SnapshotPolicyKind] = SnapshotPolicyKind.CURRENT_SNAPSHOT

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "observed_snapshot_ref": self.observed_snapshot_ref}

    def column_refs(self) -> tuple[str, ...]:
        return ()

    def collapsed_refs(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class LatestAvailableAsOf:
    """SCD-style history: for each key, the eligible row with the greatest effective time wins.

    Eligibility is the conjunction PIT rule 1 states — ``effective_time_ref`` at or before the
    business-date cutoff AND ``availability_ref`` at or before the SAME cutoff. Both halves matter:
    a customer record created after the business date must not appear, and neither may one that
    existed but had not yet arrived. Both are run-time filters in the generated code; what is
    checkable here is that both columns are declared, exist, and are governed.

    ``deterministic_tie_break_refs`` orders rows that share an effective time. It may be empty ONLY
    when the governed grain proves such a tie cannot happen — that is exactly the collapse check
    below, so no separate rule is needed. A tie that survives the declared ordering depends on
    actual rows and is a gate in the generated pipeline, not a compilation refusal.
    """

    effective_time_ref: str
    availability_ref: str
    deterministic_tie_break_refs: tuple[str, ...] = ()
    kind: ClassVar[SnapshotPolicyKind] = SnapshotPolicyKind.LATEST_AVAILABLE_AS_OF

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "effective_time_ref": self.effective_time_ref,
                "availability_ref": self.availability_ref,
                "deterministic_tie_break_refs": list(self.deterministic_tie_break_refs)}

    def column_refs(self) -> tuple[str, ...]:
        return (self.effective_time_ref, self.availability_ref,
                *self.deterministic_tie_break_refs)

    def collapsed_refs(self) -> tuple[str, ...]:
        # Availability FILTERS rows; it does not reduce many versions of a key to one, so it is
        # deliberately not collapsing. Calling it collapsing would let a table with per-arrival
        # rows pass as one-row-per-key.
        return (self.effective_time_ref, *self.deterministic_tie_break_refs)


@dataclass(frozen=True, slots=True)
class PartitionMappedSnapshot:
    """A business date selects partition values; within them the source is one row per key.

    ``ordered_partition_refs`` names the partition columns, in partition order, that the run's
    business date resolves to values for. The MAPPING itself — the transform, the timezone, whether
    a window widens the partition set for late arrivals — is not re-declared here: §3.4 puts it in
    the environment inventory's `PartitionMappingV1`, declared and never inferred. Two copies of one
    mapping is two mappings, and the second one is the one nobody governs.
    """

    ordered_partition_refs: tuple[str, ...]
    kind: ClassVar[SnapshotPolicyKind] = SnapshotPolicyKind.PARTITION_MAPPED

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value,
                "ordered_partition_refs": list(self.ordered_partition_refs)}

    def column_refs(self) -> tuple[str, ...]:
        return tuple(self.ordered_partition_refs)

    def collapsed_refs(self) -> tuple[str, ...]:
        return tuple(self.ordered_partition_refs)


@dataclass(frozen=True, slots=True)
class ActivePopulation:
    """Current rows restricted to a CLOSED set of declared status values.

    There is no implicit notion of "active" and no free-text predicate: the column and the values
    that count as active are both declared, so a reviewer can see the population rule without
    reading SQL. A status filter REMOVES members, so it narrows the population — it never collapses
    versions, and it cannot support a completeness claim.
    """

    status_ref: str
    allowed_status_values: tuple[str, ...]
    kind: ClassVar[SnapshotPolicyKind] = SnapshotPolicyKind.ACTIVE_POPULATION

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "status_ref": self.status_ref,
                "allowed_status_values": list(self.allowed_status_values)}

    def column_refs(self) -> tuple[str, ...]:
        return (self.status_ref,)

    def collapsed_refs(self) -> tuple[str, ...]:
        return ()


#: The CLOSED union. A policy object that is not one of these four is refused, never defaulted —
#: the default a missing variant would fall into is "read the whole table", which is the exact
#: behaviour §4.2 was written to eliminate.
SnapshotPolicy = CurrentSnapshot | LatestAvailableAsOf | PartitionMappedSnapshot | ActivePopulation

#: Kind -> variant. A test asserts this covers `SnapshotPolicyKind` exactly and one-to-one, so a
#: new kind cannot be added without a variant that says what it collapses.
SNAPSHOT_POLICY_TYPES: Mapping[SnapshotPolicyKind, type[SnapshotPolicy]] = {
    SnapshotPolicyKind.CURRENT_SNAPSHOT: CurrentSnapshot,
    SnapshotPolicyKind.LATEST_AVAILABLE_AS_OF: LatestAvailableAsOf,
    SnapshotPolicyKind.PARTITION_MAPPED: PartitionMappedSnapshot,
    SnapshotPolicyKind.ACTIVE_POPULATION: ActivePopulation,
}

#: Which policies a population claim may rest on, where the spec constrains it. `CURRENT_ACTIVE_ONLY`
#: REQUIRES `ActivePopulation` (§4.2 rule 4). The converse is enforced too: a status filter
#: structurally removes members, so it cannot back a COMPLETE claim, and a historical as-of claim
#: needs effective dating or a partition mapping rather than a status column. Both directions are
#: refusals, i.e. the fail-closed direction, and both are provable from the declaration alone.
_SEMANTICS_FOR_ACTIVE_POPULATION = PopulationSemantics.CURRENT_ACTIVE_ONLY


@dataclass(frozen=True, slots=True)
class SpineSourceDeclarationV1:
    """A human declaration of WHICH table is the entity population, and how a snapshot of it maps
    to a business date.

    Declared once per materialization contract, never per feature: two features in one group that
    disagreed about who the customers are would produce a table whose rows mean two things.

    The provenance fields (`declared_by`, `declaration_reason`, `recorded_at`,
    `declaration_record_id`) are carried but EXCLUDED from identity — see :meth:`identity_payload`.
    `declared_by` is threaded from the request; materialization never mints an actor.
    """

    entity: str
    source_table_ref: str
    ordered_key_refs: tuple[str, ...]
    population_semantics: PopulationSemantics
    availability_ref: str | None
    snapshot_policy: SnapshotPolicy
    declared_by: IdentityEnvelope
    declaration_reason: str
    declaration_version: int
    recorded_at: str
    declaration_record_id: str

    def identity_payload(self) -> dict[str, Any]:
        """The SEMANTIC payload — everything that makes this a different population, and nothing
        else.

        Provenance is excluded on purpose: if it were included, two people making the identical
        declaration would produce different contract hashes and therefore different groups, which
        contradicts the package-wide rule that hashes carry identity only.

        Deliberately TOTAL: no parsing, no catalog read, nothing that can raise. It is called while
        building a contract hash, where an exception would be indistinguishable from a governed
        refusal. Refs reach it already in the catalog's canonical form because
        :func:`validate_spine_declaration` refuses anything else.
        """
        return {
            "entity": self.entity,
            "source_table_ref": self.source_table_ref,
            "ordered_key_refs": list(self.ordered_key_refs),
            "population_semantics": self.population_semantics.value,
            "availability_ref": self.availability_ref,
            "snapshot_policy": self.snapshot_policy.identity_payload(),
            "declaration_version": self.declaration_version,
        }

    def provenance_payload(self) -> dict[str, Any]:
        """Who declared it, why, when and under which record — audit only, never identity."""
        return {
            "declared_by": {
                "subject": self.declared_by.subject,
                "actor_kind": self.declared_by.actor_kind,
                "auth_method": self.declared_by.auth_method,
                "role_claims": list(self.declared_by.role_claims),
                "on_behalf_of": self.declared_by.on_behalf_of,
            },
            "declaration_reason": self.declaration_reason,
            "recorded_at": self.recorded_at,
            "declaration_record_id": self.declaration_record_id,
        }


@dataclass(frozen=True, slots=True)
class SpineSpec:
    """A declaration the governed facts did not refute, plus what validating it established.

    ``read_set`` is every column the spine itself reads — keys, availability and the policy's own
    columns. Gate 2 (§1.3) authorizes it together with every expression's read set, as one
    group-wide decision.

    ``governed_grain_refs`` is recorded rather than recomputed later: it is the evidence behind the
    uniqueness verdict, and re-reading it downstream would be a second, possibly disagreeing,
    snapshot of a projection that moves.
    """

    declaration: SpineSourceDeclarationV1
    source_table_ref: str
    entity: str
    ordered_key_refs: tuple[str, ...]
    population_semantics: PopulationSemantics
    snapshot_policy: SnapshotPolicy
    availability_ref: str | None
    read_set: tuple[str, ...]
    governed_grain_refs: tuple[str, ...]
    roles_used: tuple[str, ...]
    completeness_claim_authority: ClaimAuthority

    def identity_payload(self) -> dict[str, Any]:
        return self.declaration.identity_payload()


def _reject(detail: str) -> MaterializationRefused:
    return MaterializationRefused(
        CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS, detail)


@dataclass(frozen=True, slots=True)
class _Located:
    """A declaration whose refs parsed, are canonical, and all sit on the declared table."""

    catalog_source: str
    schema: str
    table: str
    key_columns: tuple[str, ...]
    read_refs: tuple[str, ...]
    #: ref -> its column name, resolved ONCE. Later steps look it up instead of re-splitting the
    #: ref: a second parse is a second chance to disagree about which column was declared.
    columns: Mapping[str, str]
    collapsed_columns: frozenset[str]


def _canonical_table_ref(ref: str) -> tuple[str, str, str] | None:
    """``(source, schema, table)`` when ``ref`` is already a canonical TABLE ref, else ``None``.

    A non-canonical ref is not a cosmetic problem. Catalog lookups fold case, so
    ``public.CUSTOMERS.CIF_ID`` finds the same node — but it hashes differently, so one population
    would key two materialization contracts and silently split into two groups.
    """
    try:
        source, schema, table, column = parse_ref(ref)
    except ValueError:
        return None
    if column is not None or normalize_ref(source, schema, table) != ref:
        return None
    return source, schema, table


def _canonical_column_ref(ref: str) -> tuple[str, str, str, str] | None:
    """``(source, schema, table, column)`` when ``ref`` is already a canonical COLUMN ref."""
    try:
        source, schema, table, column = parse_ref(ref)
    except ValueError:
        return None
    if column is None or normalize_ref(source, schema, table, column) != ref:
        return None
    return source, schema, table, column


def _locate(declaration: SpineSourceDeclarationV1) -> _Located | MaterializationRefused:
    """Structural validation of the declaration ALONE — no catalog, no privilege, no inference."""
    policy = declaration.snapshot_policy
    if type(policy) not in set(SNAPSHOT_POLICY_TYPES.values()):
        return _reject(
            f"snapshot_policy is {type(policy).__name__}, which is not one of the "
            f"{len(SNAPSHOT_POLICY_TYPES)} declared variants: a policy this module cannot read "
            f"would fall through to reading the whole source table")

    if not declaration.entity.strip():
        return _reject("entity is blank: a population with no entity is not a population")
    if not declaration.declaration_reason.strip():
        return _reject("declaration_reason is blank: an unexplained population choice is not "
                       "reviewable, and review is the only control over this decision")
    if declaration.declaration_version < 1:
        return _reject(f"declaration_version {declaration.declaration_version} is below 1")

    declared_table = _canonical_table_ref(declaration.source_table_ref)
    if declared_table is None:
        return _reject(
            "source_table_ref is not a canonical TABLE ref in the catalog's own form "
            "(source::schema.table)")
    source, schema, table = declared_table

    if not declaration.ordered_key_refs:
        return _reject("ordered_key_refs is empty: the spine has no key to be unique on")
    if len(set(declaration.ordered_key_refs)) != len(declaration.ordered_key_refs):
        return _reject(f"ordered_key_refs repeats a ref ({len(declaration.ordered_key_refs)} refs, "
                       f"{len(set(declaration.ordered_key_refs))} distinct)")

    policy_refs = policy.column_refs()
    availability = declaration.availability_ref
    read_refs = (*declaration.ordered_key_refs,
                 *((availability,) if availability is not None else ()),
                 *policy_refs)
    columns: dict[str, str] = {}
    for ref in read_refs:
        parsed = _canonical_column_ref(ref)
        if parsed is None:
            return _reject(f"{ref} is not a canonical COLUMN ref in the catalog's own form")
        ref_source, ref_schema, ref_table, ref_column = parsed
        if (ref_source, ref_schema, ref_table) != (source, schema, table):
            return _reject(
                f"{ref} is not a column of the declared source {declaration.source_table_ref}: a "
                f"spine whose keys or time columns come from another table is not one population")
        columns[ref] = ref_column

    key_columns = tuple(columns[ref] for ref in declaration.ordered_key_refs)
    collapsed = frozenset(columns[ref] for ref in policy.collapsed_refs())
    overlap = sorted(collapsed.intersection(key_columns))
    if overlap:
        return _reject(
            f"the policy collapses spine key column(s) {', '.join(overlap)}: collapsing a key "
            f"collapses the population itself")

    coherence = _reject_incoherent_policy(declaration)
    if coherence is not None:
        return coherence

    # Deterministic, de-duplicated read set: the same column named twice is one read.
    ordered_read: dict[str, None] = {}
    for ref in read_refs:
        ordered_read.setdefault(ref, None)
    return _Located(catalog_source=source, schema=schema, table=table, key_columns=key_columns,
                    read_refs=tuple(ordered_read), columns=columns, collapsed_columns=collapsed)


def _reject_incoherent_policy(
    declaration: SpineSourceDeclarationV1,
) -> MaterializationRefused | None:
    """Contradictions inside the declaration itself — provable with no catalog at all."""
    policy = declaration.snapshot_policy
    semantics = declaration.population_semantics

    if semantics is PopulationSemantics.CURRENT_ACTIVE_ONLY \
            and not isinstance(policy, ActivePopulation):
        return _reject(
            f"population_semantics {semantics.value} requires an "
            f"{SnapshotPolicyKind.ACTIVE_POPULATION.value} policy (§4.2 rule 4): there is no "
            f"implicit notion of 'active', so the status column and its allowed values must be "
            f"declared — got {policy.kind.value}")

    if isinstance(policy, ActivePopulation):
        if semantics is not _SEMANTICS_FOR_ACTIVE_POPULATION:
            return _reject(
                f"an {policy.kind.value} policy filters members OUT, so it cannot back a "
                f"{semantics.value} claim; only "
                f"{_SEMANTICS_FOR_ACTIVE_POPULATION.value} is consistent with it")
        # Values are DATA, so they are counted here and never echoed into a refusal.
        if not policy.allowed_status_values:
            return _reject("allowed_status_values is empty: an active population with no active "
                           "status admits nobody")
        if len(set(policy.allowed_status_values)) != len(policy.allowed_status_values):
            return _reject(
                f"allowed_status_values repeats a value ({len(policy.allowed_status_values)} "
                f"values, {len(set(policy.allowed_status_values))} distinct)")

    if isinstance(policy, CurrentSnapshot) and not policy.observed_snapshot_ref.strip():
        return _reject(
            "observed_snapshot_ref is blank: a table with no history cannot honestly answer an "
            "arbitrary business date unless the vintage it was observed at is on the record")

    if isinstance(policy, LatestAvailableAsOf):
        if declaration.availability_ref != policy.availability_ref:
            return _reject(
                f"the {policy.kind.value} policy reads availability from "
                f"{policy.availability_ref} while the declaration names "
                f"{declaration.availability_ref}: PIT rule 1 filters on availability, and two "
                f"availability columns are two point-in-time semantics under one contract hash")
        if len(set(policy.deterministic_tie_break_refs)) \
                != len(policy.deterministic_tie_break_refs):
            return _reject("deterministic_tie_break_refs repeats a ref, so the ordering it "
                           "declares is not the ordering it applies")
        if policy.effective_time_ref in policy.deterministic_tie_break_refs:
            return _reject("the effective-time column is also a tie-break column, so ties on it "
                           "are broken by itself and remain unbroken")

    if isinstance(policy, PartitionMappedSnapshot):
        if not policy.ordered_partition_refs:
            return _reject("ordered_partition_refs is empty: a partition-mapped snapshot with no "
                           "partition column selects the whole table")
        if len(set(policy.ordered_partition_refs)) != len(policy.ordered_partition_refs):
            return _reject("ordered_partition_refs repeats a ref, so the partition order it "
                           "declares is not the order it applies")
    return None


def _resolve_nodes(
    conn: DbConn, located: _Located, roles: tuple[str, ...]
) -> dict[str, str] | MaterializationRefused:
    """Existence, then read scope, then ``{ref: the catalog's OWN object_ref}``.

    Existence comes first because a denied read on a column that does not exist is a meaningless
    answer that sends an operator to request a role they do not need. Read scope uses the SAME rule
    the join planner applies: an untagged node is visible to everyone; a tagged one only to roles
    that grant its tag.

    The stored `object_ref` is returned rather than the one built here because the two are not
    always the same string, and the shipped readers disagree about which one addresses a node:
    `column_authority._scalar` folds case, `entity.effective_entity` matches EXACTLY. Carrying the
    catalog's own value makes every read below hit the same node regardless of that asymmetry —
    the alternative is a spine that reports "no governed entity" for a catalog whose entity is
    perfectly governed, which is a refusal for the wrong reason.
    """
    object_refs = {_object_ref(located.table, column): ref
                   for ref, column in ((r, located.columns[r]) for r in located.read_refs)}
    table_node = _object_ref(located.table, None)
    rows = conn.execute(
        "SELECT lower(object_ref), object_ref, sensitivity FROM graph_node "
        "WHERE catalog_source = %s AND lower(object_ref) = ANY(%s)",
        (located.catalog_source, [*object_refs, table_node])).fetchall()
    found = {folded: (stored, sensitivity) for folded, stored, sensitivity in rows}

    missing = sorted(ref for object_ref, ref in object_refs.items() if object_ref not in found)
    if missing:
        return _reject(
            f"the governed catalog has no column node for {', '.join(missing)} on "
            f"{located.catalog_source}: a declaration may only name columns the catalog governs")
    if table_node not in found:
        return _reject(
            f"the governed catalog has no table node for {located.table} on "
            f"{located.catalog_source}")

    allowed = allowed_sensitivities(roles)
    hidden = sorted(object_refs.get(object_ref, object_ref)
                    for object_ref, (_stored, sensitivity) in found.items()
                    if sensitivity is not None and sensitivity not in allowed)
    if hidden:
        return MaterializationRefused(
            CompilationRefusalCode.READ_SCOPE_INSUFFICIENT,
            f"the supplied read scope hides {len(hidden)} of the spine's columns: "
            f"{', '.join(hidden)}. The spine's source and keys are part of what must be "
            f"authorized, so an unreadable population cannot be compiled over")
    return {ref: found[object_ref][0] for object_ref, ref in object_refs.items()}


def _object_ref(table: str, column: str | None) -> str:
    """The PUBLIC-FLATTENED graph_node key (`graph.build_graph` writes every ref under `public`),
    case-folded.

    Built the same way `column_authority.read_column_facts` builds it, so this module and the
    governed readers address the same node.
    """
    return ".".join(["public", table, *([column] if column else [])])


def _refuse_wrong_entity(
    conn: DbConn, declaration: SpineSourceDeclarationV1, located: _Located,
    nodes: Mapping[str, str],
) -> MaterializationRefused | None:
    """`ENTITY_ASSIGNMENT`, read through the shipped governed reader.

    At least ONE key must carry a GOVERNED assignment naming the declared entity — a file-declared
    tag is display context, not an attestation, and a spine validated by a hint is a spine validated
    by nobody. No key may carry a governed assignment naming a DIFFERENT entity: a composite key may
    legitimately leave some columns untagged, but a contradiction is a contradiction.
    """
    declared = declaration.entity.strip().lower()
    confirming: list[str] = []
    for ref in declaration.ordered_key_refs:
        read = effective_entity(conn, located.catalog_source, nodes[ref])
        if read.authority != GOVERNED_ENTITY or read.entity is None:
            continue
        if read.entity.strip().lower() == declared:
            confirming.append(ref)
        else:
            return _reject(
                f"{ref} carries a governed entity_assignment for a different entity than the "
                f"declared {declaration.entity!r}")
    if not confirming:
        return _reject(
            f"no spine key carries a GOVERNED entity_assignment for {declaration.entity!r}: a "
            f"file-declared entity is display context, and the population's entity may not rest "
            f"on it")
    return None


def _governed_grain_columns(conn: DbConn, located: _Located) -> tuple[str, ...]:
    """The declared table's governed `GRAIN` columns — scoped to that table, never a search.

    Two steps on purpose: the flat `is_grain` flag enumerates the CANDIDATES (it is the only
    per-table index there is), and C1's `read_operational_value` then decides which of them are
    actually GOVERNED. Enumerating on the flag alone would accept a file declaration; asking C1
    without the flag would mean guessing column names.

    The read itself now lives in `overlay.upload.governed_grain` so the contract compiler asks the
    SAME question of the same store (it needs the far side of a bridge to be a table's whole grain
    before a roll-up hop may claim many_to_one). This is a MOVE, not a reimplementation: one reader,
    two callers, so neither can drift into its own idea of what "governed" means.
    """
    return governed_grain_columns(
        conn, located.catalog_source, located.schema, located.table)


def _refuse_non_unique_keys(
    declaration: SpineSourceDeclarationV1, located: _Located, governed_grain: tuple[str, ...],
) -> MaterializationRefused | None:
    """The declaration-time half of "exactly one row per entity key" (§4.2 rules 2, 3 and 5).

    Both directions are needed. Keys the governed grain does not contain are not attested to
    identify anything. Grain columns the declared keys do not contain mean the source holds many
    rows per key — legal only when the snapshot policy collapses exactly those columns, which is
    what each variant's ``collapsed_refs`` states. Anything left over is a duplicate spine key, and
    a duplicate spine key duplicates every feature row built on it.
    """
    grain = set(governed_grain)
    declared = set(located.key_columns)
    unattested = sorted(declared - grain)
    if unattested:
        return _reject(
            f"no governed grain fact attests that {', '.join(unattested)} identifies rows of "
            f"{declaration.source_table_ref} (governed grain: "
            f"{', '.join(governed_grain) or 'none'}): several tables can carry the same key over "
            f"different populations, so the keys must be attested, not assumed")
    uncollapsed = sorted(grain - declared - located.collapsed_columns)
    if uncollapsed:
        return _reject(
            f"the governed grain of {declaration.source_table_ref} includes "
            f"{', '.join(uncollapsed)}, which the "
            f"{declaration.snapshot_policy.kind.value} policy does not collapse: the source holds "
            f"more than one row per spine key, so the spine would carry duplicate keys")
    return None


def _refuse_ungoverned_availability(
    conn: DbConn, declaration: SpineSourceDeclarationV1, located: _Located
) -> MaterializationRefused | None:
    """The spine's availability column participates in PIT filtering exactly as an expression's
    does (§4.2 rule 6), so it must be a governed `AVAILABILITY_TIME` column — not merely a
    timestamp someone believes is the arrival time."""
    ref = declaration.availability_ref
    if ref is None:
        return None
    read = read_operational_value(
        conn,
        normalize_ref(located.catalog_source, located.schema, located.table,
                      located.columns[ref]),
        "is_as_of")
    if read.status == _RESOLVED and read.value == _GOVERNED_TRUE:
        return None
    return MaterializationRefused(
        CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED,
        f"{ref} carries no governed availability_time fact (C1 status {read.status!r}), so the "
        f"spine's point-in-time filter would be applied on an unattested column")


def validate_spine_declaration(
    conn: DbConn,
    declaration: SpineSourceDeclarationV1 | None,
    *,
    roles: Iterable[str] = (),
) -> SpineSpec | MaterializationRefused:
    """Validate a declared entity population against the governed catalog — or refuse it.

    **Never selects a source.** Every catalog read below is keyed by a ref taken from
    ``declaration``; nothing here searches, ranks or filters tables. Two equally-qualified tables
    therefore both validate, each under its own declaration, which is the observable form of
    "facts validate, they never choose".

    A refusal is RETURNED, not raised: a refused spine is one governed verdict among the many a
    compilation collects. Checks run in this order, and the FIRST failure decides the code:

    1. no declaration at all — ``SPINE_SOURCE_NOT_DECLARED``;
    2. the declaration contradicts itself (needs no catalog, no privilege);
    3. the columns exist, then the caller may read them — ``READ_SCOPE_INSUFFICIENT``;
    4. governed `entity_assignment` agrees;
    5. governed `GRAIN` makes the keys unique under the policy;
    6. governed `availability_time` backs the declared availability column —
       ``AVAILABILITY_TIME_NOT_GOVERNED``.

    Only the reported code depends on that order; every branch refuses.
    """
    if declaration is None:
        return MaterializationRefused(
            CompilationRefusalCode.SPINE_SOURCE_NOT_DECLARED,
            "no spine source declaration: `entity_assignment` and `grain` prove a column denotes "
            "an entity and that some columns identify rows, never that a table holds every member "
            "of the population — so the source is declared, and picking one from those facts is "
            "the inference this refusal exists to prevent")

    roles_used = tuple(roles)
    located = _locate(declaration)
    if isinstance(located, MaterializationRefused):
        return located

    nodes = _resolve_nodes(conn, located, roles_used)
    if isinstance(nodes, MaterializationRefused):
        return nodes

    wrong_entity = _refuse_wrong_entity(conn, declaration, located, nodes)
    if wrong_entity is not None:
        return wrong_entity

    governed_grain = _governed_grain_columns(conn, located)
    non_unique = _refuse_non_unique_keys(declaration, located, governed_grain)
    if non_unique is not None:
        return non_unique

    ungoverned_availability = _refuse_ungoverned_availability(conn, declaration, located)
    if ungoverned_availability is not None:
        return ungoverned_availability

    return SpineSpec(
        declaration=declaration,
        source_table_ref=declaration.source_table_ref,
        entity=declaration.entity,
        ordered_key_refs=declaration.ordered_key_refs,
        population_semantics=declaration.population_semantics,
        snapshot_policy=declaration.snapshot_policy,
        availability_ref=declaration.availability_ref,
        read_set=located.read_refs,
        governed_grain_refs=tuple(
            normalize_ref(located.catalog_source, located.schema, located.table, column)
            for column in governed_grain),
        roles_used=roles_used,
        # §4: a completeness claim is a HUMAN declaration wherever it is surfaced — never a
        # governed catalog fact, whatever the facts happened to permit.
        completeness_claim_authority=ClaimAuthority.HUMAN_DECLARATION,
    )
