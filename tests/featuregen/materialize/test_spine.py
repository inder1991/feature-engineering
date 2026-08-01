"""Spec A Task 4 — the spine source DECLARATION and its closed `SnapshotPolicy`.

This task exists to stop one specific silent wrong answer: **choosing the entity population from
governed facts.** `ENTITY_ASSIGNMENT` and `GRAIN` prove that a column denotes an entity and that
some columns identify rows. They prove NOTHING about whether the table holds every member.
`kyc_customers`, `card_customers` and `customers` can be byte-identical to the catalog — same
entity, same unique `cif_id` — over three different populations. Picking one by inspecting facts
drops customers from every feature, forever, with no error anywhere.

So the tests below are shaped around two claims:

1. **The declaration chooses; facts may only validate or reject.** Proved twice — once by giving
   the validator two equally-qualified tables and showing the answer follows the declaration
   (:func:`test_facts_validate_but_never_choose`), and once by recording every SQL statement the
   validator issues and showing the rival table is never even READ
   (:func:`test_the_rival_table_is_never_read_at_all`). The second is the load-bearing one: a
   selection rule cannot hide in code that never looks at the alternatives.

2. **A `SnapshotPolicy` that reads the whole table is a defect, not a default** (spec §4.2). Its
   normative PIT rules split in two: the halves provable from governed metadata are enforced HERE,
   at declaration time, and the halves that depend on actual rows are named in the docstrings and
   belong to the runtime gates (`SPINE_DUPLICATE_KEY`, `SPINE_NON_DETERMINISTIC`, `SPINE_INCOMPLETE`
   — all `ValidationGateCode`, all Task 13/17).
"""
import dataclasses
import inspect

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize import spine
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.spine import (
    ActivePopulation,
    ClaimAuthority,
    CurrentSnapshot,
    LatestAvailableAsOf,
    PartitionMappedSnapshot,
    PopulationSemantics,
    SnapshotPolicyKind,
    SpineSourceDeclarationV1,
    SpineSpec,
    validate_spine_declaration,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

_SRC = "hdfc"
_ROLES = ("feature_engineer",)

# Refs are SCHEMA-FLATTENED (verified interfaces §17): `build_graph` writes every object_ref under
# `public`, and the real declared schema survives only in `graph_node.schema_name`. The plan's
# example literal `hdfc::banking.customers` is illustrative — spec §3.5 says to read every
# `hdfc::banking.…` in the spec as "the ref for that table", never as a physical schema. Mapping
# this onto the cluster's `banking` schema is Task 5's explicit governed step.
CUSTOMERS = f"{_SRC}::public.customers"
CUSTOMERS_CIF = f"{CUSTOMERS}.cif_id"
CUSTOMERS_ASOF = f"{CUSTOMERS}.load_ts"
CUSTOMERS_EFF = f"{CUSTOMERS}.effective_from"
CUSTOMERS_SEQ = f"{CUSTOMERS}.version_seq"
CUSTOMERS_STATUS = f"{CUSTOMERS}.status_cd"
CUSTOMERS_SNAP = f"{CUSTOMERS}.snapshot_dt"

# The rival: same entity, same unique cif_id, a DIFFERENT (KYC-only) population.
KYC = f"{_SRC}::public.kyc_customers"
KYC_CIF = f"{KYC}.cif_id"

_DECLARER = IdentityEnvelope(
    subject="user:asha", actor_kind="human", authenticated=False, auth_method="test",
    role_claims=("feature_engineer",))
_OTHER_DECLARER = IdentityEnvelope(
    subject="user:ben", actor_kind="human", authenticated=False, auth_method="test",
    role_claims=("feature_engineer",))


# ── the governed catalog ─────────────────────────────────────────────────────────────────────────
# Seeded through the REAL machinery (`build_graph`), then the governed FACT LINKS are attached the
# way `tests/featuregen/materialize/fixtures.py` does: a flat `is_grain` flag alone is only a FILE
# declaration, and `read_operational_value` grants governed authority only when the flag is true AND
# the `*_fact_event_id` link is non-null. That distinction is the whole point — a hint must never be
# able to validate the population definition.


def _govern_grain(db, *columns: str, table: str = "customers") -> None:
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id = 'ovf_evt_grain' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
        "AND column_name = ANY(%s)",
        (_SRC, table, list(columns)))


def _govern_availability(db, column: str, *, table: str = "customers") -> None:
    db.execute(
        "UPDATE graph_node SET availability_fact_event_id = 'ovf_evt_asof' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (_SRC, table, column))


def _govern_entity(db, column: str, *, table: str = "customers") -> None:
    """A VERIFIED `entity_assignment` as its projection records it (migration 1015): the effective
    `entity` plus `entity_status='VERIFIED'` and the two provenance links. Without the status the
    entity is only FILE-DECLARED, which `effective_entity` reports as non-governed."""
    db.execute(
        "UPDATE graph_node SET entity_status = 'VERIFIED', entity_fact_key = 'sbf-entity', "
        "entity_fact_event_id = 'ovf_evt_entity' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (_SRC, table, column))


def _healthy_projection(db) -> None:
    """`read_operational_value` fails CLOSED (GATE 3) on a lagged/degraded projection, so every
    governed read in a bare test database would otherwise degrade."""
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, now(), 'r', 0) ON CONFLICT (catalog_source) "
        "DO UPDATE SET last_completed_at = now()", (_SRC,))


@pytest.fixture
def two_candidate_customer_tables(db):
    """`customers` AND `kyc_customers`: identical to the catalog, different populations.

    Both carry a governed `entity_assignment` (customer) on a governed-grain, unique `cif_id`.
    Nothing in the catalog says which one is the authoritative population, because nothing CAN."""
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customers", "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(_SRC, "customers", "load_ts", "timestamp", as_of=True),
        CanonicalRow(_SRC, "customers", "status_cd", "text"),
        CanonicalRow(_SRC, "kyc_customers", "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(_SRC, "kyc_customers", "load_ts", "timestamp", as_of=True),
        CanonicalRow(_SRC, "kyc_customers", "status_cd", "text"),
    ])
    for table in ("customers", "kyc_customers"):
        _govern_grain(db, "cif_id", table=table)
        _govern_availability(db, "load_ts", table=table)
        _govern_entity(db, "cif_id", table=table)
    _healthy_projection(db)
    return db


def _declaration(**overrides) -> SpineSourceDeclarationV1:
    """A `CURRENT_COMPLETE_POPULATION` declaration over `customers`, observed at one snapshot."""
    fields = {
        "entity": "customer",
        "source_table_ref": CUSTOMERS,
        "ordered_key_refs": (CUSTOMERS_CIF,),
        "population_semantics": PopulationSemantics.CURRENT_COMPLETE_POPULATION,
        "availability_ref": CUSTOMERS_ASOF,
        "snapshot_policy": CurrentSnapshot(observed_snapshot_ref="2026-07-27"),
        "declared_by": _DECLARER,
        "declaration_reason": "the bank's master customer file",
        "declaration_version": 1,
        "recorded_at": "2026-07-27T09:00:00+00:00",
        "declaration_record_id": "spine-decl-0001",
    }
    fields.update(overrides)
    return SpineSourceDeclarationV1(**fields)


# ── 1/2: the declaration chooses; facts only validate ────────────────────────────────────────────


def test_facts_validate_but_never_choose(two_candidate_customer_tables):
    """`kyc_customers` ALSO has a governed unique `cif_id` and a governed `customer` entity. The
    SAME catalog validates BOTH declarations — so the source came from the declaration, not from
    the facts. If facts could choose, one of these two calls would have to disagree."""
    master = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(), roles=_ROLES)
    kyc = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(source_table_ref=KYC, ordered_key_refs=(KYC_CIF,),
                     availability_ref=f"{KYC}.load_ts",
                     population_semantics=PopulationSemantics.CURRENT_ACTIVE_ONLY,
                     snapshot_policy=ActivePopulation(status_ref=f"{KYC}.status_cd",
                                                      allowed_status_values=("A",))),
        roles=_ROLES)
    assert isinstance(master, SpineSpec)
    assert master.source_table_ref == CUSTOMERS
    # The rival validates too — the SAME facts back both answers, so they cannot be choosing.
    assert isinstance(kyc, SpineSpec)
    assert kyc.source_table_ref == KYC


class _RecordingCursor:
    def __init__(self, cursor, log):
        self._cursor, self._log = cursor, log

    def execute(self, sql, params=None, *a, **kw):
        self._log.append((sql, params))
        return self._cursor.execute(sql, params, *a, **kw)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _RecordingConn:
    """Every statement the validator issues, with its parameters. A source-SELECTION rule has to
    read the alternatives from somewhere, so "the rival never appears in any statement" is a
    stronger proof than any assertion about the returned value."""

    def __init__(self, conn):
        self._conn, self.log = conn, []

    def execute(self, sql, params=None, *a, **kw):
        self.log.append((sql, params))
        return self._conn.execute(sql, params, *a, **kw)

    def cursor(self, *a, **kw):
        return _RecordingCursor(self._conn.cursor(*a, **kw), self.log)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def mentions(self, needle: str) -> bool:
        return any(needle in str(sql) or needle in str(params) for sql, params in self.log)


def test_the_rival_table_is_never_read_at_all(two_candidate_customer_tables):
    spy = _RecordingConn(two_candidate_customer_tables)
    result = validate_spine_declaration(spy, _declaration(), roles=_ROLES)
    assert isinstance(result, SpineSpec)
    assert spy.log, "the validator must actually consult the governed catalog"
    assert spy.mentions("customers"), "it reads the DECLARED table"
    assert not spy.mentions("kyc_customers"), (
        "the rival population was read — a source-selection rule can only live in code that "
        "looks at the alternatives")


def test_every_catalog_read_is_SCOPED_to_the_declared_table(two_candidate_customer_tables):
    """The stronger form of the claim above, and the one that survives a rename: choosing a source
    means enumerating candidates, so no statement against the catalog graph may be unscoped. Every
    `graph_node` read must carry the DECLARED table (or its object refs) in its parameters."""
    spy = _RecordingConn(two_candidate_customer_tables)
    assert isinstance(validate_spine_declaration(spy, _declaration(), roles=_ROLES), SpineSpec)
    graph_reads = [(sql, params) for sql, params in spy.log if "graph_node" in str(sql)]
    assert graph_reads, "the declaration must actually be checked against the governed catalog"
    for sql, params in graph_reads:
        assert "customers" in str(params), (
            f"unscoped catalog read — it could enumerate populations: {sql!r}")


def test_the_returned_source_is_the_declared_string_byte_for_byte(two_candidate_customer_tables):
    """Not "a table that matches the declaration" — THE declared ref. Any re-derivation step is a
    place where a different table could be substituted."""
    decl = _declaration()
    result = validate_spine_declaration(two_candidate_customer_tables, decl, roles=_ROLES)
    assert isinstance(result, SpineSpec)
    assert result.source_table_ref is decl.source_table_ref
    assert result.ordered_key_refs == decl.ordered_key_refs


def test_the_module_never_ranks_or_picks_a_table():
    """A selection would need to order or limit candidates. Neither idiom may appear."""
    src = inspect.getsource(spine)
    for banned in ("ORDER BY", "LIMIT", "best", "candidate_table"):
        assert banned not in src, f"{banned!r}: facts validate the declaration, they never choose it"


def test_facts_may_REJECT_a_declaration(two_candidate_customer_tables):
    """`status_cd` is a real column of the declared table, but nothing attests it identifies rows —
    so the governed `GRAIN` fact REFUTES the claim that it is a spine key."""
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(ordered_key_refs=(CUSTOMERS_STATUS,)), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_a_FILE_DECLARED_grain_is_not_an_attestation(db):
    """`build_graph` writes `is_grain` from the upload. Without the governed `grain_fact_event_id`
    link that is a HINT, and a hint must never validate the population definition."""
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customers", "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(_SRC, "customers", "load_ts", "timestamp", as_of=True),
    ])
    _govern_entity(db, "cif_id")
    _govern_availability(db, "load_ts")
    _healthy_projection(db)          # deliberately NO _govern_grain
    result = validate_spine_declaration(db, _declaration(), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_a_FILE_DECLARED_entity_is_not_an_attestation(db):
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customers", "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(_SRC, "customers", "load_ts", "timestamp", as_of=True),
    ])
    _govern_grain(db, "cif_id")
    _govern_availability(db, "load_ts")
    _healthy_projection(db)          # deliberately NO _govern_entity
    result = validate_spine_declaration(db, _declaration(), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_the_WRONG_entity_is_rejected(two_candidate_customer_tables):
    result = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(entity="account"), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS
    assert "account" in result.detail


def test_a_key_belonging_to_ANOTHER_table_is_rejected(two_candidate_customer_tables):
    """A spine whose keys come from a table other than its source is not a population at all."""
    result = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(ordered_key_refs=(KYC_CIF,)), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_a_column_the_catalog_does_not_have_is_rejected(two_candidate_customer_tables):
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(ordered_key_refs=(f"{CUSTOMERS}.no_such_column",)), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_a_MIXED_CASE_catalog_is_the_same_catalog(db):
    """`build_graph` writes `object_ref`/`table_name` with the UPLOAD's casing, while a canonical
    `logical_ref` is case-folded — and the governed readers match on `lower(object_ref)`. If this
    module matched exactly it would report "no such column" for a catalog `read_operational_value`
    reads happily, and the two would disagree about whether the population exists at all."""
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "Customers", "CIF_ID", "text", is_grain=True, entity="customer"),
        CanonicalRow(_SRC, "Customers", "LOAD_TS", "timestamp", as_of=True),
    ])
    _govern_grain(db, "CIF_ID", table="Customers")
    _govern_availability(db, "LOAD_TS", table="Customers")
    _govern_entity(db, "CIF_ID", table="Customers")
    _healthy_projection(db)
    result = validate_spine_declaration(db, _declaration(), roles=_ROLES)
    assert isinstance(result, SpineSpec)
    assert result.governed_grain_refs == (CUSTOMERS_CIF,)


def test_missing_declaration_uses_a_GOVERNED_code_not_TypeError(db):
    result = validate_spine_declaration(db, None, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_SOURCE_NOT_DECLARED


# ── read scope: a denied read is not a fact rejection ────────────────────────────────────────────


def test_a_denied_read_is_READ_SCOPE_INSUFFICIENT_not_a_fact_rejection(two_candidate_customer_tables):
    """Two different remedies — grant a role, or fix the declaration — so two different codes.
    Reported BEFORE any fact verdict, so a caller who cannot see the column is never told that the
    catalog disagrees with a declaration they were not allowed to check."""
    two_candidate_customer_tables.execute(
        "UPDATE graph_node SET sensitivity = 'pii' WHERE catalog_source = %s "
        "AND object_ref = 'public.customers.cif_id'", (_SRC,))
    denied = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(), roles=_ROLES)
    granted = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(), roles=(*_ROLES, "pii_reader"))
    assert isinstance(denied, MaterializationRefused)
    assert denied.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert isinstance(granted, SpineSpec)          # same catalog, sufficient read scope


def test_the_roles_that_validated_the_spine_are_recorded(two_candidate_customer_tables):
    result = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(), roles=["feature_engineer"])
    assert isinstance(result, SpineSpec)
    assert result.roles_used == ("feature_engineer",)


# ── availability (§4.2 rule 6) ───────────────────────────────────────────────────────────────────


def test_an_UNGOVERNED_availability_ref_has_its_own_code(two_candidate_customer_tables):
    """`AVAILABILITY_TIME_NOT_GOVERNED` exists precisely for this: the declaration names an
    availability column that no governed fact attests."""
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(availability_ref=CUSTOMERS_STATUS), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_the_spine_availability_ref_is_carried_into_the_read_set(two_candidate_customer_tables):
    """Rule 6: the spine's availability participates in PIT filtering exactly as an expression's
    does, so it is part of what Gate 2 (§1.3) must authorize."""
    result = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(), roles=_ROLES)
    assert isinstance(result, SpineSpec)
    assert CUSTOMERS_ASOF in result.read_set
    assert CUSTOMERS_CIF in result.read_set


# ── §4.2 rules 2/3/5: the declaration-time half of "exactly one row per key" ─────────────────────


@pytest.fixture
def scd_customers(db):
    """An SCD-style master: the governed grain is `(cif_id, effective_from, version_seq)`, so the
    table holds MANY rows per customer and only a collapsing policy can make a spine out of it."""
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customers", "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(_SRC, "customers", "effective_from", "date", is_grain=True),
        CanonicalRow(_SRC, "customers", "version_seq", "int", is_grain=True),
        CanonicalRow(_SRC, "customers", "load_ts", "timestamp", as_of=True),
        CanonicalRow(_SRC, "customers", "status_cd", "text"),
    ])
    _govern_grain(db, "cif_id", "effective_from", "version_seq")
    _govern_availability(db, "load_ts")
    _govern_entity(db, "cif_id")
    _healthy_projection(db)
    return db


def _scd_policy(**overrides) -> LatestAvailableAsOf:
    fields = {"effective_time_ref": CUSTOMERS_EFF, "availability_ref": CUSTOMERS_ASOF,
              "deterministic_tie_break_refs": (CUSTOMERS_SEQ,)}
    fields.update(overrides)
    return LatestAvailableAsOf(**fields)


def test_a_policy_that_collapses_the_history_columns_is_accepted(scd_customers):
    result = validate_spine_declaration(
        scd_customers,
        _declaration(population_semantics=PopulationSemantics.HISTORICAL_AS_OF,
                     snapshot_policy=_scd_policy()), roles=_ROLES)
    assert isinstance(result, SpineSpec)
    assert set(result.governed_grain_refs) == {CUSTOMERS_CIF, CUSTOMERS_EFF, CUSTOMERS_SEQ}


def test_a_grain_column_NO_policy_collapses_gives_duplicate_spine_keys(scd_customers):
    """`CURRENT_SNAPSHOT` claims the table IS the population — one row per customer. The governed
    grain says otherwise. Refusing here is §4.2 rule 5 enforced at the only point where it can be
    proved without reading rows; the runtime `SPINE_DUPLICATE_KEY` gate is the backstop, not the
    first line."""
    result = validate_spine_declaration(scd_customers, _declaration(), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS
    assert "version_seq" in result.detail and "effective_from" in result.detail


def test_an_UNBROKEN_TIE_column_is_refused_at_declaration_time(scd_customers):
    """Rule 3's declaration-time half. Two rows can share a `(cif_id, effective_from)` — the
    governed grain says so, since `version_seq` is part of row identity. With no tie-break over it
    the "greatest effective_time wins" rule has no answer, and a spine whose population changes
    between runs is worse than one that refuses."""
    result = validate_spine_declaration(
        scd_customers,
        _declaration(population_semantics=PopulationSemantics.HISTORICAL_AS_OF,
                     snapshot_policy=_scd_policy(deterministic_tie_break_refs=())), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS
    assert "version_seq" in result.detail


def test_a_residual_tie_is_a_RUNTIME_gate_not_a_compilation_refusal():
    """`SPINE_NON_DETERMINISTIC` depends on the actual rows, so §14 types it as a gate inside the
    generated pipeline. It must not be reachable as a compilation refusal."""
    from featuregen.materialize.codes import ValidationGateCode

    assert ValidationGateCode.SPINE_NON_DETERMINISTIC.value == "SPINE_NON_DETERMINISTIC"
    assert "SPINE_NON_DETERMINISTIC" not in {c.value for c in CompilationRefusalCode}
    assert "SPINE_NON_DETERMINISTIC" not in inspect.getsource(spine)


def test_a_partition_mapped_snapshot_collapses_its_partition_columns(db):
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customers", "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(_SRC, "customers", "snapshot_dt", "date", is_grain=True),
        CanonicalRow(_SRC, "customers", "load_ts", "timestamp", as_of=True),
    ])
    _govern_grain(db, "cif_id", "snapshot_dt")
    _govern_availability(db, "load_ts")
    _govern_entity(db, "cif_id")
    _healthy_projection(db)
    result = validate_spine_declaration(
        db, _declaration(snapshot_policy=PartitionMappedSnapshot(
            ordered_partition_refs=(CUSTOMERS_SNAP,))), roles=_ROLES)
    assert isinstance(result, SpineSpec)


def test_a_key_that_is_ALSO_named_as_collapsing_is_rejected(scd_customers):
    """Collapsing a spine key would collapse the population itself."""
    result = validate_spine_declaration(
        scd_customers,
        _declaration(population_semantics=PopulationSemantics.HISTORICAL_AS_OF,
                     snapshot_policy=_scd_policy(effective_time_ref=CUSTOMERS_CIF)), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


# ── §4.2 rules 1 and 4: what each variant must carry ─────────────────────────────────────────────


def test_current_active_only_REQUIRES_an_ActivePopulation_policy(two_candidate_customer_tables):
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(population_semantics=PopulationSemantics.CURRENT_ACTIVE_ONLY), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_an_ActivePopulation_policy_cannot_back_a_COMPLETE_claim(two_candidate_customer_tables):
    """The converse of rule 4. A status filter structurally REMOVES members, so it cannot support
    a `CURRENT_COMPLETE_POPULATION` claim; §4.1 would only discover that at the first run, via
    orphan grain keys."""
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(snapshot_policy=ActivePopulation(
            status_ref=CUSTOMERS_STATUS, allowed_status_values=("A",))), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_an_ActivePopulation_needs_a_NON_EMPTY_closed_status_set(two_candidate_customer_tables):
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(population_semantics=PopulationSemantics.CURRENT_ACTIVE_ONLY,
                     snapshot_policy=ActivePopulation(
                         status_ref=CUSTOMERS_STATUS, allowed_status_values=())), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_an_ActivePopulation_declaration_is_accepted_when_it_is_coherent(
        two_candidate_customer_tables):
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(population_semantics=PopulationSemantics.CURRENT_ACTIVE_ONLY,
                     snapshot_policy=ActivePopulation(
                         status_ref=CUSTOMERS_STATUS, allowed_status_values=("A", "N"))),
        roles=_ROLES)
    assert isinstance(result, SpineSpec)
    assert CUSTOMERS_STATUS in result.read_set


def test_an_ActivePopulation_with_NO_availability_ref_COMPILES_and_the_gate_is_at_run_prep(
        two_candidate_customer_tables):
    """Documented ACCEPTANCE, not an oversight (review §2.1). §4.2 places no availability
    requirement on this policy, so the declaration compiles with `availability_ref=None` and the
    rendered spine carries no cutoff at all (pinned in test_render_nodes_compute). The
    point-in-time enforcement lives at RUN PREPARATION: `runprep.spine_input_request` refuses any
    business date the declaration's recorded vintage cannot answer. Requiring an availability_ref
    HERE would only half-close the leak — the status column is CURRENT-valued, so entities closed
    since a historical business date would still vanish from that date's population even under a
    rule-6 filter."""
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(availability_ref=None,
                     population_semantics=PopulationSemantics.CURRENT_ACTIVE_ONLY,
                     snapshot_policy=ActivePopulation(
                         status_ref=CUSTOMERS_STATUS, allowed_status_values=("A", "N"))),
        roles=_ROLES)
    assert isinstance(result, SpineSpec)
    assert result.availability_ref is None


def test_a_refusal_never_echoes_the_declared_status_VALUES(two_candidate_customer_tables):
    """Refusal detail carries counts, types, hashes and locations — never data values."""
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(snapshot_policy=ActivePopulation(
            status_ref=CUSTOMERS_STATUS, allowed_status_values=("SECRET_STATUS_TOKEN",))),
        roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert "SECRET_STATUS_TOKEN" not in result.detail


def test_a_CurrentSnapshot_must_record_WHICH_snapshot_it_observed(two_candidate_customer_tables):
    """A present-day table cannot honestly answer an arbitrary historical `business_dt`; recording
    the observed snapshot is what makes the mismatch detectable at run preparation instead of
    silently answering with today's rows."""
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(snapshot_policy=CurrentSnapshot(observed_snapshot_ref="  ")), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_LatestAvailableAsOf_requires_an_availability_ref_that_AGREES(scd_customers):
    """Rule 1 filters on BOTH the effective time and the availability, so the policy cannot run
    without an availability column — and two disagreeing availability columns are two different
    PIT semantics under one contract hash."""
    result = validate_spine_declaration(
        scd_customers,
        _declaration(population_semantics=PopulationSemantics.HISTORICAL_AS_OF,
                     availability_ref=None, snapshot_policy=_scd_policy()), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


# ── closed vocabularies ──────────────────────────────────────────────────────────────────────────


def test_population_semantics_is_closed():
    assert {p.value for p in PopulationSemantics} == {
        "current_complete_population", "current_active_only", "historical_as_of"}


def test_snapshot_policy_variants_are_closed():
    assert {k.value for k in SnapshotPolicyKind} == {
        "current_snapshot", "latest_available_as_of", "partition_mapped", "active_population"}


def test_every_policy_kind_has_exactly_one_variant_type():
    """A tripwire on the union: a new kind with no variant (or a variant with no kind) would let
    the validator fall through to a default, and a spine policy's default is the whole-table read
    §4.2 exists to stop."""
    assert set(spine.SNAPSHOT_POLICY_TYPES) == set(SnapshotPolicyKind)
    assert len({t for t in spine.SNAPSHOT_POLICY_TYPES.values()}) == len(SnapshotPolicyKind)
    for kind, variant in spine.SNAPSHOT_POLICY_TYPES.items():
        assert variant.kind is kind


def test_an_unrecognized_policy_object_is_refused_not_defaulted(two_candidate_customer_tables):
    class _Rogue:
        kind = "read_the_whole_table"

    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(snapshot_policy=_Rogue()), roles=_ROLES)   # type: ignore[arg-type]
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_duplicate_keys_are_never_DE_DUPLICATED():
    """§4.2 rule 5: duplicate spine keys are a blocking gate, not a de-duplication step. Dropping
    the duplicates would silently pick which version of a customer the population contains."""
    src = inspect.getsource(spine)
    for banned in ("dropDuplicates", "drop_duplicates", "DISTINCT", "distinct("):
        assert banned not in src, "a duplicate spine key blocks; it is never repaired"


def test_no_free_text_sql_field_exists():
    """Any active/current-row selection is structured. A free-text predicate would smuggle
    ungoverned business logic into the population definition where no check can see it."""
    types = [SpineSourceDeclarationV1, SpineSpec, *spine.SNAPSHOT_POLICY_TYPES.values()]
    for declared in types:
        names = {f.name for f in dataclasses.fields(declared)}
        assert not any(("sql" in n) or ("predicate" in n) or ("where" in n) for n in names), declared


def test_declared_by_is_never_minted_in_this_module():
    src = inspect.getsource(spine)
    assert "authenticated=True" not in src
    assert "IdentityEnvelope(" not in src, "the envelope is THREADED from the request, never built"


# ── identity vs provenance ───────────────────────────────────────────────────────────────────────


def test_provenance_is_EXCLUDED_from_identity():
    """Two people making the same semantic declaration must produce the SAME contract — otherwise
    an identical population would split into two materialization groups."""
    decl_a = _declaration()
    decl_b = _declaration(declared_by=_OTHER_DECLARER, declaration_reason="same table, my words",
                          recorded_at="2026-07-28T11:00:00+00:00",
                          declaration_record_id="spine-decl-0002")
    assert decl_a.identity_payload() == decl_b.identity_payload()
    assert decl_a.provenance_payload() != decl_b.provenance_payload()
    for banned in ("declared_by", "declaration_reason", "recorded_at", "declaration_record_id"):
        assert banned not in decl_a.identity_payload()


def test_identity_covers_every_semantic_field():
    """The inverse guard: a semantic field left OUT of identity would let two different populations
    share one contract hash — the silent-merge failure."""
    payload = _declaration().identity_payload()
    assert set(payload) == {"entity", "source_table_ref", "ordered_key_refs",
                            "population_semantics", "availability_ref", "snapshot_policy",
                            "declaration_version"}


@pytest.mark.parametrize("changed", [
    {"entity": "account"},
    {"source_table_ref": KYC},
    {"ordered_key_refs": (CUSTOMERS_STATUS,)},
    {"population_semantics": PopulationSemantics.HISTORICAL_AS_OF},
    {"availability_ref": None},
    {"snapshot_policy": CurrentSnapshot(observed_snapshot_ref="2026-07-28")},
    {"declaration_version": 2},
])
def test_every_semantic_change_moves_the_hash(changed):
    assert materialize_hash(_declaration().identity_payload()) != \
           materialize_hash(_declaration(**changed).identity_payload())


def test_the_identity_payload_hashes_with_the_ONE_hasher():
    digest = materialize_hash(_declaration().identity_payload())
    assert len(digest) == 64


def test_identity_payload_is_TOTAL_and_reads_nothing():
    """A pure accessor: no catalog, no parsing, nothing that can raise. It is called while building
    a contract hash, where a surprise exception would be indistinguishable from a governance
    refusal."""
    assert "conn" not in inspect.signature(SpineSourceDeclarationV1.identity_payload).parameters
    junk = _declaration(source_table_ref="not a ref at all", ordered_key_refs=("",))
    assert junk.identity_payload()["source_table_ref"] == "not a ref at all"


def test_a_non_canonical_ref_is_refused_before_it_can_fork_a_contract_hash(
        two_candidate_customer_tables):
    """`public.CUSTOMERS.CIF_ID` finds the same node (lookups fold case) but hashes differently, so
    one population would key two contracts. The catalog's own `normalize_ref` form is the check."""
    result = validate_spine_declaration(
        two_candidate_customer_tables,
        _declaration(ordered_key_refs=(f"{_SRC}::public.customers.CIF_ID",)), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_a_malformed_ref_is_a_GOVERNED_refusal_not_a_ValueError(two_candidate_customer_tables):
    result = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(source_table_ref="customers"), roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


# ── the boundary itself ──────────────────────────────────────────────────────────────────────────


def test_a_COMPLETE_population_claim_is_labelled_a_HUMAN_declaration(two_candidate_customer_tables):
    """§4: completeness is recorded as a human declaration, never as a governed catalog fact, and
    is labelled as such wherever it is surfaced. `SpineSpec` is a surface."""
    result = validate_spine_declaration(
        two_candidate_customer_tables, _declaration(), roles=_ROLES)
    assert isinstance(result, SpineSpec)
    assert result.population_semantics is PopulationSemantics.CURRENT_COMPLETE_POPULATION
    assert result.completeness_claim_authority is ClaimAuthority.HUMAN_DECLARATION
    assert "governed" not in {c.value for c in ClaimAuthority}


def test_the_spine_carries_no_business_dt_anywhere():
    """§3.3: `business_dt` is a RUN parameter. A spine declaration that carried one would put a run
    observation into the contract hash and change the generated project every business date."""
    for declared in (SpineSourceDeclarationV1, SpineSpec, *spine.SNAPSHOT_POLICY_TYPES.values()):
        assert not any("business_dt" in f.name for f in dataclasses.fields(declared)), declared
    assert "business_dt" not in inspect.signature(validate_spine_declaration).parameters


def test_the_spine_is_declared_per_CONTRACT_not_per_feature():
    """§4: declared once per materialization contract. A per-feature parameter would let two
    features in one group disagree about who the population is."""
    params = set(inspect.signature(validate_spine_declaration).parameters)
    assert not any("feature" in p or "ir" == p for p in params)


def test_refusals_are_RETURNED_not_raised(db):
    result = validate_spine_declaration(db, None, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert isinstance(result.code, CompilationRefusalCode)


def test_the_declaration_and_the_spec_are_frozen(two_candidate_customer_tables):
    decl = _declaration()
    with pytest.raises(Exception):
        decl.entity = "account"          # type: ignore[misc]
    result = validate_spine_declaration(two_candidate_customer_tables, decl, roles=_ROLES)
    assert isinstance(result, SpineSpec)
    with pytest.raises(Exception):
        result.source_table_ref = KYC    # type: ignore[misc]
