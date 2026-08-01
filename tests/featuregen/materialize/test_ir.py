"""Spec A Task 7 — the per-FEATURE IR (§1.3, §2, §3) and the GROUP-WIDE Gate 2.

Two claims decide whether this task is right, and both of them are claims about what a test may
assert.

**1. Gate 2 is GROUP-WIDE, and the obvious test for it is wrong.** A public feature genuinely *is*
individually authorized, so "every IR is refused" asserts false semantics. What must fail is the
GROUP OPERATION over the union of every expression read set, the spine source and its keys, every
join step's endpoint and every availability column. One denied element anywhere returns a single
`READ_SCOPE_INSUFFICIENT`, and no contract, group plan or project is produced.

That last property is the one an earlier draft of the plan tested vacuously: it counted derived
contracts after calling `authorize_compilation` ALONE, which could never derive one anyway, so the
assertion held no matter what the code did. It is tested here at a level where a contract COULD have
been produced — a harness that runs §2's chain (compile → Gate 2 → downstream) with a POSITIVE
CONTROL in the same test, so the counter is proved reachable before it is asserted to be zero. The
downstream double also REFUSES to be called with anything but an `AuthorizedCompilation`, so "0"
means "never reached" rather than "reached and quietly did nothing".

**2. The IR carries, never re-derives, Child-1's output policy.** `FormulaOutputPolicyV1` is
resolved by Child-1 over C1 governed facts (interfaces §7). Spec A copies it through byte-for-byte;
recomputing it here is how an LLM's advisory guess would get laundered into governed authority. The
test asserts object IDENTITY (`is`), and a source-level test asserts the resolver is never imported.

Both element-class tests for join endpoints and availability columns DOCTOR an IR — they remove the
element from the expression read set — because Task 6 puts every join endpoint and every
availability column into the read set as well. Without doctoring, a Gate 2 that only ever looked at
`physical_read_set` would pass them, and the test would prove nothing about the other two sources.
Each doctored test carries its own control: with the tag removed, the same doctored group
authorizes.
"""
import dataclasses
import inspect
import io
import tokenize
from enum import StrEnum

import pytest
from tests.featuregen.materialize import fixtures

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.canonical import formula_content_hash
from featuregen.formula.schema import (
    DiffBody,
    Grain,
    RatioBody,
    UnaryBody,
    ZeroDenominator,
    body_expressions,
)
from featuregen.materialize import ir as ir_module
from featuregen.materialize.admission import AdmittedFeature
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import BODY_PATHS, ExpressionExecutionIR, RefRole
from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    EventTimePartition,
    PartitionTransform,
    TableLayout,
)
from featuregen.materialize.ir import (
    AuthorizedCompilation,
    FormulaExecutionIRV1,
    ReadElementKind,
    authorize_compilation,
    compile_ir,
    ir_hash,
)
from featuregen.materialize.spine import (
    CurrentSnapshot,
    LatestAvailableAsOf,
    PopulationSemantics,
    SpineSourceDeclarationV1,
)

_SRC = "hdfc"
_ROLES = ("feature_engineer",)
_TZ = "Asia/Kolkata"

# Refs are SCHEMA-FLATTENED (§3.5 / interfaces §17): every `object_ref` an upload writes sits under
# `public`, and the real schema survives only in `graph_node.schema_name`. `banking` below is what
# Task 5's resolver RESOLVES, never what a ref segment says.
TXN = fixtures.TABLE_REF
TXN_AMT = fixtures.REF_AMT
TXN_DT = fixtures.REF_DT
TXN_CIF = fixtures.REF_CIF
TXN_MERCHANT = fixtures.REF_MERCHANT
TXN_DR_CR = fixtures.REF_DR_CR
TXN_ACCT = f"{TXN}.acct_id"
TXN_POSTED = f"{TXN}.posted_ts"          # arrival time — DISTINCT from the event-time column

ACCOUNTS = f"{_SRC}::public.accounts"
ACCOUNTS_ID = f"{ACCOUNTS}.account_id"
ACCOUNTS_CIF = f"{ACCOUNTS}.cif_id"

CUSTOMERS = f"{_SRC}::public.customers"
CUSTOMERS_CIF = f"{CUSTOMERS}.cif_id"
CUSTOMERS_ASOF = f"{CUSTOMERS}.load_ts"
CUSTOMERS_STATUS = f"{CUSTOMERS}.status_cd"

PUBLIC_FEATURE = "distinct_merchant_count_90d"    # reads merchant_id, txn_dt, cif_id
DENIED_FEATURE = "total_debit_amount_30d"         # reads txn_amt, dr_cr_flag, status_cd, …
RATIO_FEATURE = "cross_border_value_ratio_90d"

_DECLARER = IdentityEnvelope(
    subject="user:asha", actor_kind="human", authenticated=False, auth_method="test",
    role_claims=("feature_engineer",))


def _code_of(module) -> str:
    """The module's executable text, lower-cased, with comments and docstrings stripped.

    Borrowed from `test_inputs.py` / `test_expression_ir.py`: a source assertion that also read the
    prose would be satisfied by deleting an explanation, and would fail for a docstring that names
    what it forbids.
    """
    kept: list[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(inspect.getsource(module)).readline)
    previous = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
            continue
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            kept.append(token.string)
        previous = token.type
    return " ".join(kept).lower()


# ── the governed catalog ─────────────────────────────────────────────────────────────────────────
# Seeded the way `test_expression_ir.py` and `test_spine.py` do — PUBLIC-FLATTENED object refs with
# the REAL schema in `schema_name`, then the governed FACT LINKS attached. The distinction is the
# whole point: `build_graph` writes `is_grain`/`is_as_of`/`entity` straight from an upload, and those
# flags become GOVERNED only when the projection attaches the `*_fact_event_id` link (and, for the
# entity, `entity_status='VERIFIED'`).


def _col(db, table, column, *, schema="banking", sensitivity=None, restriction=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "schema_name, sensitivity, effective_restriction) "
        "VALUES (%s, %s, 'column', %s, %s, %s, %s, %s)",
        (_SRC, f"public.{table}.{column}", table, column, schema, sensitivity, restriction))


def _table_node(db, table, *, schema="banking", sensitivity=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, schema_name, "
        "sensitivity) VALUES (%s, %s, 'table', %s, %s, %s)",
        (_SRC, f"public.{table}", table, schema, sensitivity))


def _edge(db, from_ref, to_ref, *, cardinality="N:1", fact_key="ajf"):
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) "
        "VALUES (%s, 'joins', %s, %s, %s, 'operational', %s, 'VERIFIED')",
        (_SRC, from_ref, to_ref, cardinality, fact_key))


def _govern_availability(db, table, column, *, basis="posted_at", event_id="ovf_evt_asof"):
    """The flat `is_as_of` flag, the projection's fact-event LINK, and the fact row the link points
    at — Task 6 dereferences all three, because `basis` lives only in the fact value."""
    import json

    from featuregen.overlay.identity import fact_key as _fact_key
    from featuregen.overlay.upload.upload_catalog import table_ref as _catalog_table_ref

    db.execute(
        "UPDATE graph_node SET is_as_of = true, availability_fact_event_id = %s "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (event_id, _SRC, table, column))
    db.execute(
        "INSERT INTO overlay_fact_state (fact_key, catalog_source, object_ref, fact_type, status, "
        "value, confirmed_at, confirmed_event_id, updated_seq) "
        "VALUES (%s, %s, %s, 'availability_time', 'VERIFIED', %s, now(), %s, 1) "
        "ON CONFLICT (fact_key) DO UPDATE SET value = EXCLUDED.value, "
        "confirmed_event_id = EXCLUDED.confirmed_event_id",
        (_fact_key(_catalog_table_ref(_SRC, table), "availability_time"), _SRC,
         f"public.{table}", json.dumps({"column": column, "basis": basis}), event_id))


def _govern_grain(db, table, column):
    db.execute(
        "UPDATE graph_node SET is_grain = true, grain_fact_event_id = 'ovf_evt_grain' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (_SRC, table, column))


def _govern_entity(db, table, column, *, entity="customer"):
    db.execute(
        "UPDATE graph_node SET entity = %s, entity_status = 'VERIFIED', "
        "entity_fact_key = 'sbf-entity', entity_fact_event_id = 'ovf_evt_entity' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (entity, _SRC, table, column))


def _tag(db, table, column, sensitivity):
    """Attach a READ-SCOPE tag (`graph_node.sensitivity`) — axis one of the two (interfaces §2)."""
    db.execute(
        "UPDATE graph_node SET sensitivity = %s WHERE catalog_source = %s AND object_ref = %s",
        (sensitivity, _SRC, f"public.{table}.{column}"))


def _floor(db, table, column, restriction):
    """Set the GOVERNED floor (`graph_node.effective_restriction`) — axis two — leaving the tag
    NULL. That pairing is the shipped FTR shape (migration 1032's header: a business glossary
    attests no sensitivity, so all 126 columns carry `sensitivity = NULL` while the concept cascade
    ruled 28 of them restricted/confidential). Migration 1032's GENERATED `visible_requires` column
    derives from BOTH axes, so seeding the floor alone is enough to populate it."""
    db.execute(
        "UPDATE graph_node SET effective_restriction = %s WHERE catalog_source = %s "
        "AND object_ref = %s",
        (restriction, _SRC, f"public.{table}.{column}"))


def seed_catalog(db):
    """`transactions` (the fact table), the two-hop path to `customers`, and the spine's facts.

    A module-level function rather than only a fixture body so a later task's tests can seed THE
    SAME governed catalog (Task 9's contracts classify the read set this catalog defines). A second
    copy of it would be a second chance to disagree about what the catalog is, and every assertion
    about a read set is only as good as the read set being the real one.
    """
    for column in ("txn_amt", "txn_dt", "posted_ts", "cif_id", "acct_id", "merchant_id",
                   "dr_cr_flag", "status_cd", "cross_border_flag"):
        _col(db, "transactions", column)
    for column in ("account_id", "cif_id"):
        _col(db, "accounts", column)
    for column in ("cif_id", "load_ts", "status_cd", "effective_from", "version_seq"):
        _col(db, "customers", column)
    for table in ("transactions", "accounts", "customers"):
        _table_node(db, table)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id", fact_key="ajf-txn-acct")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id", fact_key="ajf-acct-cust")

    # The fact table's availability column is DELIBERATELY not its event-time column: `txn_dt` is
    # when the transaction happened, `posted_ts` when the bank knew it. Keeping them apart is what
    # lets the availability-column test isolate `PitSpec.availability_ref` from the read set.
    _govern_availability(db, "transactions", "posted_ts")

    # The spine's governed facts (Task 4): a governed unique key, a governed entity, a governed
    # availability column.
    _govern_grain(db, "customers", "cif_id")
    _govern_entity(db, "customers", "cif_id")
    _govern_availability(db, "customers", "load_ts", event_id="ovf_evt_asof_cust")

    # `read_operational_value` fails CLOSED (GATE 3) on a lagged or degraded projection.
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, now(), 'r', 7) ON CONFLICT (catalog_source) "
        "DO UPDATE SET last_completed_at = now()", (_SRC,))
    return db


@pytest.fixture
def catalog(db):
    return seed_catalog(db)


# ── the declared inventory (Task 0/5) ────────────────────────────────────────────────────────────


def _layout(table, columns) -> TableLayout:
    return TableLayout(
        schema="banking", table=table, partition_columns=(("load_dt", "string"),),
        partition_mapping=EventTimePartition(
            time_ref=TXN_DT, partition_column="load_dt",
            transform=PartitionTransform.DATE_ISO, timezone=_TZ),
        columns=tuple((column, "string") for column in columns),
        location=f"hdfs://nn/warehouse/banking.db/{table}", rewritten_in_place=False)


INVENTORY = ClusterInventoryV1(
    environment_id="hdfc-local",
    tables={
        "banking.transactions": _layout("transactions", (
            "txn_amt", "txn_dt", "posted_ts", "cif_id", "acct_id", "merchant_id", "dr_cr_flag",
            "status_cd", "cross_border_flag")),
        "banking.accounts": _layout("accounts", ("account_id", "cif_id")),
        "banking.customers": _layout("customers", ("cif_id", "load_ts", "status_cd")),
    },
    logical_schema_map={TXN: "banking", ACCOUNTS: "banking", CUSTOMERS: "banking"},
    engine_versions=fixtures.ENGINE_VERSIONS,
    captured_at="2026-07-27T00:00:00Z")


# ── the declaration, the admitted features, and the compile helpers ──────────────────────────────


def _declaration(**overrides) -> SpineSourceDeclarationV1:
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


DECLARATION = _declaration()


def _admitted(name, *, formula=None, run_id="run-0001") -> AdmittedFeature:
    """An `AdmittedFeature` for one worked feature.

    Built directly rather than through `admit_artifacts`: Gate 1's proof against the immutable
    terminal event is Task 2's property and is tested there. What Task 7 consumes is the admitted
    ARTIFACT, and the hash is re-derived here with Child-1's own canonicalizer so the fixture cannot
    carry a hash that disagrees with its formula.
    """
    resolved = fixtures.authored_formula(name) if formula is None else formula
    return AdmittedFeature(
        feature_name=name, formula=resolved, formula_content_hash=formula_content_hash(resolved),
        intent=fixtures.intent_for(name), authoring_run_id=run_id)


def _compile(db, name="total_debit_amount_30d", *, formula=None, roles=_ROLES,
             spine_decl=DECLARATION, inventory=INVENTORY, run_id="run-0001"):
    return compile_ir(db, _admitted(name, formula=formula, run_id=run_id),
                      roles=roles, spine_decl=spine_decl, inventory=inventory)


def _ok(value):
    """Assert a compilation succeeded, and return it — so a refusal fails the test HERE."""
    assert isinstance(value, FormulaExecutionIRV1), value
    return value


def _spine_of(ir: FormulaExecutionIRV1):
    return ir.spine


def _refs_of(ir: FormulaExecutionIRV1) -> set[str]:
    return {ref.logical_ref for expr in ir.expressions for ref in expr.physical_read_set}


def _grain_on_customers(name):
    """The same worked formula, re-grained onto the SPINE's own key column.

    That is the only way to make an expression traverse `transactions → accounts → customers`, which
    is what puts real join steps (and their endpoints) into the IR.
    """
    formula = fixtures.authored_formula(name)
    return dataclasses.replace(formula, grain=Grain(entity="customer", keys=(CUSTOMERS_CIF,)))


def _without_ref(ir: FormulaExecutionIRV1, logical_ref: str) -> FormulaExecutionIRV1:
    """The same IR with one ref DELETED from every expression read set.

    Doctoring, and deliberate: Task 6 already folds join endpoints and the availability column into
    the read set, so a Gate 2 that only read `physical_read_set` would pass the endpoint and
    availability tests for the wrong reason. Removing the ref leaves the join plan and the PIT spec
    as the only remaining sources — which is exactly what §1.3 says the union must also cover.
    """
    expressions = tuple(
        dataclasses.replace(
            expr,
            physical_read_set=tuple(r for r in expr.physical_read_set
                                    if r.logical_ref != logical_ref))
        for expr in ir.expressions)
    assert _refs_of(ir) != _refs_of(dataclasses.replace(ir, expressions=expressions))
    return dataclasses.replace(ir, expressions=expressions)


# ══ §1.3 — Gate 2 is GROUP-WIDE ══════════════════════════════════════════════════════════════════


def test_gate_2_is_group_wide_not_per_feature(catalog):
    """A public feature IS individually authorized; the GROUP operation must fail.

    A per-feature signature would be wrong in a way that matters: asserting "every IR is refused"
    would assert false semantics, because the public feature really is authorized on its own. The
    first two assertions establish exactly that, so the third is a statement about the GROUP.
    """
    public = _ok(_compile(catalog, PUBLIC_FEATURE))
    _tag(catalog, "transactions", "dr_cr_flag", "pii")
    denied = _ok(_compile(catalog, DENIED_FEATURE))
    spine = _spine_of(public)

    assert isinstance(
        authorize_compilation(catalog, (public,), spine, roles=_ROLES), AuthorizedCompilation)
    assert isinstance(
        authorize_compilation(catalog, (denied,), spine, roles=_ROLES), MaterializationRefused)

    refused = authorize_compilation(catalog, (public, denied), spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT


def test_the_denied_element_refuses_the_group_from_ANY_position(catalog):
    """Order-independence, asserted because a loop that returned on the first IR — or kept only the
    last verdict — would pass a one-order test and silently authorize the other order."""
    public = _ok(_compile(catalog, PUBLIC_FEATURE))
    _tag(catalog, "transactions", "dr_cr_flag", "pii")
    denied = _ok(_compile(catalog, DENIED_FEATURE))
    spine = _spine_of(public)

    for group in ((public, denied), (denied, public)):
        refused = authorize_compilation(catalog, group, spine, roles=_ROLES)
        assert isinstance(refused, MaterializationRefused)
        assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT


def test_a_refusal_is_a_single_verdict_not_a_partial_authorization(catalog):
    """There is no partial authorization and no per-feature bypass (§1.3): the refusal is ONE
    typed verdict, and it is not an `AuthorizedCompilation` carrying the survivors."""
    public = _ok(_compile(catalog, PUBLIC_FEATURE))
    _tag(catalog, "transactions", "dr_cr_flag", "pii")
    _tag(catalog, "transactions", "txn_amt", "pii")
    denied = _ok(_compile(catalog, DENIED_FEATURE))

    refused = authorize_compilation(catalog, (public, denied), _spine_of(public), roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert not isinstance(refused, AuthorizedCompilation)
    assert "2 of" in refused.detail                     # counts, never data values
    assert TXN_DR_CR in refused.detail and TXN_AMT in refused.detail


class _Downstream:
    """A stand-in for §2's post-Gate-2 chain (contracts §5, group plan, rendered project §7).

    Tasks 8–12 do not exist yet, so the chain is modelled rather than imported — but the modelling
    is confined to WHAT happens after the gate, never to the gate itself. It refuses to be called
    with anything but an `AuthorizedCompilation`, which is what makes a count of zero mean "never
    reached" instead of "reached and quietly produced nothing".
    """

    def __init__(self) -> None:
        self.contracts_derived = 0
        self.projects_rendered = 0

    def _require_token(self, authorization: object) -> AuthorizedCompilation:
        if not isinstance(authorization, AuthorizedCompilation):
            raise TypeError(
                f"the downstream chain may only be entered with an AuthorizedCompilation, got "
                f"{type(authorization).__name__}")
        return authorization

    def derive_contracts(self, authorization: object) -> None:
        self.contracts_derived += len(self._require_token(authorization).irs)

    def render_project(self, authorization: object) -> None:
        self._require_token(authorization)
        self.projects_rendered += 1


def _run_chain(db, names, *, spy, roles=_ROLES, spine_decl=DECLARATION):
    """§2's chain, end to end: compile every feature, Gate 2 over the group, THEN downstream."""
    irs = []
    for name in names:
        compiled = _compile(db, name, roles=roles, spine_decl=spine_decl)
        if isinstance(compiled, MaterializationRefused):
            return compiled
        irs.append(compiled)
    authorization = authorize_compilation(db, tuple(irs), irs[0].spine, roles=roles)
    if isinstance(authorization, MaterializationRefused):
        return authorization
    spy.derive_contracts(authorization)
    spy.render_project(authorization)
    return authorization


def test_refusal_produces_no_contract_plan_or_project(catalog):
    """Asserted at a level where a contract COULD have been produced.

    The POSITIVE CONTROL runs first and on the same harness: the untagged group really does reach
    the downstream chain and really does raise both counters. Only then is the tagged group's zero
    meaningful — without the control, counting contracts after a call that can never derive one is
    an assertion that holds however the code behaves.
    """
    control = _Downstream()
    assert isinstance(
        _run_chain(catalog, (PUBLIC_FEATURE, DENIED_FEATURE), spy=control), AuthorizedCompilation)
    assert (control.contracts_derived, control.projects_rendered) == (2, 1)

    _tag(catalog, "transactions", "dr_cr_flag", "pii")
    spy = _Downstream()
    refused = _run_chain(catalog, (PUBLIC_FEATURE, DENIED_FEATURE), spy=spy)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert spy.contracts_derived == 0 and spy.projects_rendered == 0


# ══ §1.3 — what the union covers, one element class at a time ════════════════════════════════════


def test_the_union_covers_every_expression_read(catalog):
    """The operand of ONE feature in the group is enough to refuse the whole compilation."""
    group = (_ok(_compile(catalog, PUBLIC_FEATURE)), _ok(_compile(catalog, DENIED_FEATURE)))
    spine = group[0].spine
    assert isinstance(
        authorize_compilation(catalog, group, spine, roles=_ROLES), AuthorizedCompilation)

    _tag(catalog, "transactions", "txn_amt", "pii")
    refused = authorize_compilation(catalog, group, spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert TXN_AMT in refused.detail
    assert ReadElementKind.EXPRESSION_READ.value in refused.detail


def test_the_union_covers_the_SPINE_source_table_and_its_keys(catalog):
    """The spine is not an expression: nothing in any read set mentions the population's own table,
    so a Gate 2 built only from expression IRs would authorize a group over a population the caller
    may not read."""
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=_ROLES), AuthorizedCompilation)
    assert TXN_CIF in _refs_of(ir) and CUSTOMERS_CIF not in _refs_of(ir)

    _tag(catalog, "customers", "cif_id", "pii")
    refused = authorize_compilation(catalog, (ir,), ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    # EVERY reason the node is read, accumulated in first-seen order — not just the last one found.
    # "you may not read this" and "you may not read this AS THE POPULATION'S KEY" send an operator
    # to different remedies, and one node legitimately serves several.
    assert (f"{CUSTOMERS_CIF} ({ReadElementKind.SPINE_KEY.value}+"
            f"{ReadElementKind.SPINE_READ.value})") in refused.detail


def test_the_union_covers_the_spines_own_AVAILABILITY_column(catalog):
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    catalog.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' WHERE catalog_source = %s "
        "AND object_ref = %s", (_SRC, "public.customers.load_ts"))
    refused = authorize_compilation(catalog, (ir,), ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert CUSTOMERS_ASOF in refused.detail


def test_the_union_covers_the_spine_KEYS_and_AVAILABILITY_apart_from_its_read_set(catalog):
    """The keys and the availability column are their own element classes (§1.3).

    Task 4 builds `SpineSpec.read_set` as keys + availability + policy columns, so in a healthy
    spine all three sources name the same nodes — and a mutation that dropped either of the two
    named classes would be invisible. The spec names them separately, so they are DERIVED
    separately, and the only way to see that is to hand Gate 2 a spine whose read set was emptied.
    """
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    narrowed = dataclasses.replace(ir.spine, read_set=())
    assert isinstance(
        authorize_compilation(catalog, (ir,), narrowed, roles=_ROLES), AuthorizedCompilation)

    for table, column, ref, kind in (
            ("customers", "cif_id", CUSTOMERS_CIF, ReadElementKind.SPINE_KEY),
            ("customers", "load_ts", CUSTOMERS_ASOF, ReadElementKind.AVAILABILITY)):
        _tag(catalog, table, column, "pii")
        refused = authorize_compilation(catalog, (ir,), narrowed, roles=_ROLES)
        assert isinstance(refused, MaterializationRefused), (ref, kind)
        assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
        assert ref in refused.detail and kind.value in refused.detail
        catalog.execute(
            "UPDATE graph_node SET sensitivity = NULL WHERE catalog_source = %s "
            "AND object_ref = %s", (_SRC, f"public.{table}.{column}"))


def test_the_union_covers_a_spine_column_that_is_NEITHER_a_key_nor_the_availability(catalog):
    """`LatestAvailableAsOf` reads an effective-time column and a tie-break column that no other
    element class names. They pick which VERSION of a customer the spine holds, so reading them
    unauthorized is a read — and only `SpineSpec.read_set` accounts for them."""
    declaration = _declaration(
        population_semantics=PopulationSemantics.HISTORICAL_AS_OF,
        snapshot_policy=LatestAvailableAsOf(
            effective_time_ref=f"{CUSTOMERS}.effective_from", availability_ref=CUSTOMERS_ASOF,
            deterministic_tie_break_refs=(f"{CUSTOMERS}.version_seq",)))
    ir = _ok(_compile(catalog, PUBLIC_FEATURE, spine_decl=declaration))
    assert f"{CUSTOMERS}.effective_from" in ir.spine.read_set
    assert f"{CUSTOMERS}.effective_from" not in ir.spine.ordered_key_refs

    _tag(catalog, "customers", "effective_from", "restricted")
    refused = authorize_compilation(catalog, (ir,), ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert f"{CUSTOMERS}.effective_from" in refused.detail
    assert ReadElementKind.SPINE_READ.value in refused.detail


def test_a_read_scope_tag_in_ANOTHER_CATALOG_does_not_deny_this_group(catalog):
    """Nodes are addressed by `(catalog_source, object_ref)`, and two catalogs may hold the same
    table and column names over entirely different data. A gate that matched on the object ref
    alone would let one bank's tag refuse another bank's group — and, symmetrically, would consult
    the wrong catalog's tag for a node it is authorizing."""
    catalog.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "sensitivity) VALUES ('other', %s, 'column', 'transactions', 'txn_amt', 'pii')",
        ("public.transactions.txn_amt",))
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    assert TXN_AMT in _refs_of(ir)
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=_ROLES), AuthorizedCompilation)


def test_the_union_covers_the_spine_SOURCE_TABLE_node(catalog):
    """The table node carries its own read-scope tag, and the population's table is a read."""
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    catalog.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' WHERE catalog_source = %s "
        "AND object_ref = %s AND kind = 'table'", (_SRC, "public.customers"))
    refused = authorize_compilation(catalog, (ir,), ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert CUSTOMERS in refused.detail
    assert ReadElementKind.SPINE_SOURCE.value in refused.detail


def test_the_union_covers_every_JOIN_STEP_ENDPOINT(catalog):
    """Derived from the JOIN PLAN, not only from the read set.

    The IR is doctored to drop `accounts.cif_id` from the read set, leaving the plan's steps as the
    only place the endpoint appears — which is the case §1.3 names separately. The control at the
    end proves the doctored group is otherwise authorizable, so the refusal is about the tag.
    """
    ir = _ok(_compile(catalog, PUBLIC_FEATURE, formula=_grain_on_customers(PUBLIC_FEATURE)))
    endpoints = {ref for step in ir.expressions[0].join_plan.steps
                 for ref in (step.from_ref, step.to_ref)}
    assert "public.accounts.cif_id" in endpoints

    doctored = _without_ref(ir, ACCOUNTS_CIF)
    assert ACCOUNTS_CIF not in _refs_of(doctored)

    _tag(catalog, "accounts", "cif_id", "pii")
    refused = authorize_compilation(catalog, (doctored,), ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert ACCOUNTS_CIF in refused.detail
    assert ReadElementKind.JOIN_ENDPOINT.value in refused.detail

    catalog.execute("UPDATE graph_node SET sensitivity = NULL WHERE catalog_source = %s "
                    "AND object_ref = %s", (_SRC, "public.accounts.cif_id"))
    assert isinstance(
        authorize_compilation(catalog, (doctored,), ir.spine, roles=_ROLES), AuthorizedCompilation)


def test_the_union_covers_every_AVAILABILITY_column(catalog):
    """Derived from each expression's PIT spec, not only from the read set.

    `posted_ts` is the governed availability column and is NOT the event-time column, so with it
    doctored out of the read set the PIT spec is the only remaining source. Every row the feature
    aggregates is admitted or excluded by that column, so reading it unauthorized is a real read.
    """
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    assert {e.pit.availability_ref for e in ir.expressions} == {TXN_POSTED}
    doctored = _without_ref(ir, TXN_POSTED)

    _tag(catalog, "transactions", "posted_ts", "pii")
    refused = authorize_compilation(catalog, (doctored,), ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert TXN_POSTED in refused.detail
    assert ReadElementKind.AVAILABILITY.value in refused.detail

    catalog.execute("UPDATE graph_node SET sensitivity = NULL WHERE catalog_source = %s "
                    "AND object_ref = %s", (_SRC, "public.transactions.posted_ts"))
    assert isinstance(
        authorize_compilation(catalog, (doctored,), ir.spine, roles=_ROLES), AuthorizedCompilation)


def test_authorized_refs_is_the_COMPLETE_union_across_the_whole_group(catalog):
    """One token, one union: every element class of §1.3, from every feature."""
    group = (_ok(_compile(catalog, PUBLIC_FEATURE, formula=_grain_on_customers(PUBLIC_FEATURE))),
             _ok(_compile(catalog, DENIED_FEATURE)))
    authorization = authorize_compilation(catalog, group, group[0].spine, roles=_ROLES)
    assert isinstance(authorization, AuthorizedCompilation)
    refs = set(authorization.authorized_refs)

    assert {TXN, TXN_MERCHANT, TXN_DT, TXN_AMT, TXN_DR_CR} <= refs       # expression reads
    assert {TXN_ACCT, ACCOUNTS_ID, ACCOUNTS_CIF, CUSTOMERS_CIF} <= refs  # join endpoints
    assert TXN_POSTED in refs                                           # availability
    assert {CUSTOMERS, CUSTOMERS_CIF, CUSTOMERS_ASOF} <= refs           # spine source + keys
    assert list(authorization.authorized_refs) == sorted(set(authorization.authorized_refs))


# ══ §1.3 — the two sensitivity axes, and the roles that decide ═══════════════════════════════════


def test_a_role_that_grants_the_TAG_authorizes_the_group(catalog):
    """Gate 2 is a governed decision about ROLES, not a blanket ban on tagged columns."""
    _tag(catalog, "transactions", "txn_amt", "pii")
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=_ROLES), MaterializationRefused)
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=(*_ROLES, "pii_reader")),
        AuthorizedCompilation)


@pytest.mark.parametrize(("floor", "granting_role"),
                         [("restricted", "restricted_reader"),
                          ("confidential", "confidential_reader")])
def test_a_governed_floor_on_an_UNTAGGED_column_refuses_without_its_reader_role(
        catalog, floor, granting_role):
    """`sensitivity = NULL` + a governed floor is the shipped FTR shape (migration 1032's header:
    28/126 columns, including an Emirates ID number, carried a floor and NO file tag). Gate 2 reads
    `visible_requires`, which folds BOTH axes, so the governed floor gates compilation exactly like
    a tag — before this, such a column compiled for a caller with no reader role at all."""
    _floor(catalog, "transactions", "txn_amt", floor)
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    refused = authorize_compilation(catalog, (ir,), ir.spine, roles=())
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert TXN_AMT in refused.detail


@pytest.mark.parametrize(("floor", "granting_role"),
                         [("restricted", "restricted_reader"),
                          ("confidential", "confidential_reader")])
def test_the_floors_own_reader_role_clears_the_governed_floor(catalog, floor, granting_role):
    _floor(catalog, "transactions", "txn_amt", floor)
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=(granting_role,)),
        AuthorizedCompilation)


def test_a_PROHIBITED_floor_is_ungrantable_at_Gate_2(catalog):
    """`prohibited` always wins in `visible_requires` (migration 1032) and no role maps to it, so
    no read scope — not even every reader role at once — can compile over it. Before 1032 Gate 2
    read only the raw tag and AUTHORIZED this group, leaving §5.2 to refuse it later with
    `PROHIBITED_INPUT`; that classification refusal still exists (it re-reads the catalog at
    contract time), but the read-scope gate now fails closed first."""
    _floor(catalog, "transactions", "txn_amt", "prohibited")
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    every_reader = ("pii_reader", "restricted_reader", "confidential_reader")
    refused = authorize_compilation(catalog, (ir,), ir.spine, roles=(*_ROLES, *every_reader))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT


def test_a_TAGGED_column_keeps_its_own_gate_when_a_floor_is_also_present(catalog):
    """The two axes are folded by PRECEDENCE, never required jointly (migration 1032): where a file
    tag exists it keeps its own gate, so `pii_reader` — who can see this column today — does not
    lose access when the governed floor independently resolves `restricted`, and the floor's role
    alone does not unlock the tag."""
    _tag(catalog, "transactions", "txn_amt", "pii")
    _floor(catalog, "transactions", "txn_amt", "restricted")
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=("pii_reader",)),
        AuthorizedCompilation)
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=("restricted_reader",)),
        MaterializationRefused)


@pytest.mark.parametrize(("tag", "granting_role"),
                         [("pii", "pii_reader"), ("restricted", "restricted_reader")])
def test_each_read_scope_TAG_needs_its_OWN_granting_role(catalog, tag, granting_role):
    """The whole shipped vocabulary, and each member only by its own role — a gate that granted one
    tag's role for the other's columns would be a hard-coded exemption nobody declared.

    A tag OUTSIDE the vocabulary is deliberately not tested: migration 0993 constrains
    `graph_node.sensitivity` to exactly `SENSITIVITY_ROLES`' keys, so an unknown tag cannot reach
    the table at all, and the fail-closed comparison in `_hidden` is a guard rather than a reachable
    path. Recorded honestly instead of demonstrated with a doctored row.
    """
    _tag(catalog, "transactions", "txn_amt", tag)
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    other_role = "restricted_reader" if granting_role == "pii_reader" else "pii_reader"
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=(*_ROLES, other_role)),
        MaterializationRefused)
    assert isinstance(
        authorize_compilation(catalog, (ir,), ir.spine, roles=(*_ROLES, granting_role)),
        AuthorizedCompilation)


# ══ §1.3 — existence: compile must not emit a read nobody governs ════════════════════════════════


_EVERY_READER_ROLE = (*_ROLES, "pii_reader", "restricted_reader", "confidential_reader")


def _with_ref_renamed(ir: FormulaExecutionIRV1, old: str, new: str) -> FormulaExecutionIRV1:
    """The same IR with one read-set ref RENAMED — the shape of a hallucinated column.

    A typo'd operand keeps every other property of the read (its roles, its table) and changes only
    which column the catalog is asked about, which is exactly what an LLM-authored formula naming a
    plausible-but-absent column produces.
    """
    expressions = tuple(
        dataclasses.replace(
            expr,
            physical_read_set=tuple(
                dataclasses.replace(ref, logical_ref=new) if ref.logical_ref == old else ref
                for ref in expr.physical_read_set))
        for expr in ir.expressions)
    doctored = dataclasses.replace(ir, expressions=expressions)
    assert _refs_of(doctored) != _refs_of(ir)
    return doctored


def test_a_ref_the_catalog_does_not_govern_refuses_precisely(catalog):
    # No graph_node row used to mean "authorized, L1 will catch it" — but L1 is not on any
    # production path. A hallucinated column must die at compile, named as what it is (Task 12,
    # report §3.3), not as a role problem and not on the cluster. EVERY reader role is supplied
    # so the refusal cannot be about scope.
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    doctored = _with_ref_renamed(ir, TXN_AMT, f"{TXN}.txn_amt_typo")
    refused = authorize_compilation(catalog, (doctored,), ir.spine, roles=_EVERY_READER_ROLE)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.COLUMN_NOT_GOVERNED
    assert "public.transactions.txn_amt_typo" in refused.detail


def test_the_refusal_names_EVERY_ungoverned_ref_not_just_the_first(catalog):
    """One round trip, one verdict, the COMPLETE list — an operator fixing a refused group must not
    discover the second typo only after fixing the first."""
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    doctored = _with_ref_renamed(
        _with_ref_renamed(ir, TXN_AMT, f"{TXN}.txn_amt_typo"),
        TXN_DR_CR, f"{TXN}.dr_cr_flag_typo")
    refused = authorize_compilation(catalog, (doctored,), ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.COLUMN_NOT_GOVERNED
    assert "public.transactions.txn_amt_typo" in refused.detail
    assert "public.transactions.dr_cr_flag_typo" in refused.detail
    assert "2 of" in refused.detail


def test_existence_is_decided_BEFORE_read_scope(catalog):
    """When one group carries both an undescribed ref and a hidden one, `COLUMN_NOT_GOVERNED` wins
    (Task 12): no grantable role can make an undescribed column readable, so refusing on scope
    first would send an operator to request a privilege that cannot help."""
    _tag(catalog, "transactions", "dr_cr_flag", "pii")
    ir = _ok(_compile(catalog, DENIED_FEATURE))
    doctored = _with_ref_renamed(ir, TXN_AMT, f"{TXN}.txn_amt_typo")

    refused = authorize_compilation(catalog, (doctored,), ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.COLUMN_NOT_GOVERNED

    # The control: with the typo alone repaired, the SAME group refuses on scope — the ordering
    # above was a precedence between two live verdicts, not scope being unreachable.
    scoped = authorize_compilation(catalog, (ir,), ir.spine, roles=_ROLES)
    assert isinstance(scoped, MaterializationRefused)
    assert scoped.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT


# ══ §1.3 — calls the caller assembled wrongly ════════════════════════════════════════════════════


def test_an_empty_group_is_a_CALLER_error(catalog):
    """§14 has no member for "the call was assembled wrongly" — the line `plan_join` draws. An
    authorization token over no features would be a permit for nothing, waved at the next stage."""
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    with pytest.raises(ValueError, match="no features"):
        authorize_compilation(catalog, (), ir.spine, roles=_ROLES)


def test_irs_compiled_against_a_DIFFERENT_population_are_a_CALLER_error(catalog):
    """§4: the spine is declared once per materialization contract. A group whose IRs were compiled
    over one population, authorized against another, would authorize reads nobody will perform and
    perform reads nobody authorized."""
    here = _ok(_compile(catalog, PUBLIC_FEATURE))
    elsewhere = _ok(_compile(
        catalog, DENIED_FEATURE,
        spine_decl=_declaration(snapshot_policy=CurrentSnapshot(observed_snapshot_ref="2020-01-01"))))
    with pytest.raises(ValueError, match="spine"):
        authorize_compilation(catalog, (here, elsewhere), here.spine, roles=_ROLES)


# ══ §2 / §3 — compile_ir ═════════════════════════════════════════════════════════════════════════


def test_ratio_produces_two_expression_irs(catalog):
    """One PIT spec per EXPRESSION (§3): a ratio's halves are compiled independently, keyed by
    Child-1's own body paths."""
    ir = _ok(_compile(catalog, RATIO_FEATURE))
    assert {e.expr_path for e in ir.expressions} == {"body.numerator", "body.denominator"}
    assert all(isinstance(e, ExpressionExecutionIR) for e in ir.expressions)
    assert ir.zero_denominator is ZeroDenominator.NULL


def test_a_unary_body_produces_ONE_expression_ir(catalog):
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    assert [e.expr_path for e in ir.expressions] == ["body.expr"]
    assert ir.zero_denominator is None


def test_the_body_paths_are_CHILD_1s_own_vocabulary(catalog):
    """`BODY_PATHS` is a literal in `expression_ir`; this is the test that keeps it honest against
    the module that owns the vocabulary. A path this package invented would not be the one the
    authoring trace, the staging output and run preparation all use."""
    expr = fixtures.authored_formula(PUBLIC_FEATURE).body.expr
    bodies = (UnaryBody(expr=expr),
              RatioBody(numerator=expr, denominator=expr, zero_denominator=ZeroDenominator.NULL),
              DiffBody(minuend=expr, subtrahend=expr))
    assert {path for body in bodies for path, _expr in body_expressions(body)} == set(BODY_PATHS)


def test_output_policy_is_carried_never_rederived(catalog):
    """Child-1 resolves `FormulaOutputPolicyV1` over C1 governed facts (interfaces §7). Spec A
    copies it through byte-for-byte — object identity, not an equal-looking rebuild, because a
    rebuild is how an advisory guess gets laundered into governed authority."""
    admitted = _admitted(RATIO_FEATURE)
    ir = compile_ir(catalog, admitted, roles=_ROLES, spine_decl=DECLARATION, inventory=INVENTORY)
    assert isinstance(ir, FormulaExecutionIRV1)
    assert ir.output_policy == admitted.formula.output
    assert ir.output_policy is admitted.formula.output


def test_the_module_never_reaches_for_the_output_RESOLVER(catalog):
    """The strongest available form of "never re-derived": the resolver is not importable from
    here, so no future edit can quietly call it."""
    source = _code_of(ir_module)
    for banned in ("output_authority", "resolve_formula_output_policy", "exprfacts",
                   "partitionproof"):
        assert banned not in source


def test_an_expression_refusal_is_PROPAGATED_not_softened(catalog):
    """Task 6 owns expression compilation and Task 3 owns the join planner; their verdicts are
    carried through unchanged. A fan-out repaired here would silently pick an allocation rule."""
    catalog.execute("UPDATE graph_edge SET cardinality = '1:N' WHERE from_ref = %s",
                    ("public.transactions.acct_id",))
    refused = _compile(catalog, PUBLIC_FEATURE, formula=_grain_on_customers(PUBLIC_FEATURE))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED


def test_an_ungoverned_availability_REFUSES_the_feature(catalog):
    catalog.execute(
        "UPDATE graph_node SET availability_fact_event_id = NULL WHERE catalog_source = %s "
        "AND table_name = 'transactions'", (_SRC,))
    refused = _compile(catalog, PUBLIC_FEATURE)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_no_spine_declaration_REFUSES(catalog):
    refused = _compile(catalog, PUBLIC_FEATURE, spine_decl=None)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.SPINE_SOURCE_NOT_DECLARED


def test_a_spine_the_governed_facts_REJECT_refuses_the_feature(catalog):
    """Task 4's verdict, propagated: without a governed `GRAIN` the declared key is not attested to
    identify rows, and every feature would land on a spine that may hold duplicates."""
    catalog.execute(
        "UPDATE graph_node SET grain_fact_event_id = NULL WHERE catalog_source = %s "
        "AND object_ref = 'public.customers.cif_id'", (_SRC,))
    refused = _compile(catalog, PUBLIC_FEATURE)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_a_spine_column_the_caller_cannot_read_REFUSES_at_compile_time_too(catalog):
    """Task 4 checks the spine's own read scope; Gate 2 checks it again group-wide. Both, because
    the first is per declaration and the second is the union — and neither may be the only one."""
    _tag(catalog, "customers", "cif_id", "pii")
    refused = _compile(catalog, PUBLIC_FEATURE)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT


def test_a_formula_whose_grain_entity_is_not_the_SPINES_entity_REFUSES(catalog):
    """A feature computed at account grain cannot land on a customer population: the rows would be
    keyed by something the spine does not contain, and every join to it would be a guess."""
    formula = fixtures.authored_formula(PUBLIC_FEATURE)
    regrained = dataclasses.replace(formula, grain=Grain(entity="account", keys=(TXN_ACCT,)))
    refused = _compile(catalog, PUBLIC_FEATURE, formula=regrained)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED
    assert "account" in refused.detail and "customer" in refused.detail


def test_the_grain_and_the_spine_are_both_CARRIED_on_the_ir(catalog):
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    assert (ir.grain_entity, ir.grain_keys) == ("customer", (TXN_CIF,))
    assert ir.spine.source_table_ref == CUSTOMERS
    assert ir.feature_name == PUBLIC_FEATURE
    assert ir.formula_content_hash == formula_content_hash(fixtures.authored_formula(PUBLIC_FEATURE))


# ══ §3.3 / §14 — ir_hash is identity, and identity only ══════════════════════════════════════════


def test_ir_hash_excludes_run_time_values(catalog):
    """The generated project must be byte-identical on every business date, so no run observation
    may reach identity — and no provenance either, or two people would compile two artifacts from
    one governed decision."""
    payload = str(_ok(_compile(catalog, RATIO_FEATURE)).identity_payload())
    for banned in ("business_dt", "partition_specs", "input_snapshot_ids", "resolved_at",
                   "captured_at", "roles_used", "authoring_run_id", "run-0001", "feature_engineer",
                   "declared_by", "declaration_reason", "declaration_record_id", "recorded_at",
                   "user:asha", "approved_join_fact_key", "location", "rewritten_in_place"):
        assert banned not in payload, banned


def test_the_same_feature_compiles_to_the_same_hash_twice(catalog):
    assert ir_hash(_ok(_compile(catalog, RATIO_FEATURE))) == \
           ir_hash(_ok(_compile(catalog, RATIO_FEATURE)))


def test_two_different_features_hash_differently(catalog):
    assert ir_hash(_ok(_compile(catalog, PUBLIC_FEATURE))) != \
           ir_hash(_ok(_compile(catalog, DENIED_FEATURE)))


def test_the_feature_NAME_is_part_of_identity(catalog):
    """Two identical formulas published under two names are two columns, not one."""
    one = _ok(_compile(catalog, PUBLIC_FEATURE))
    two = dataclasses.replace(one, feature_name="merchant_breadth_90d")
    assert ir_hash(one) != ir_hash(two)


def test_the_AUTHORING_RUN_does_not_change_the_hash(catalog):
    """Provenance is carried and excluded: the same governed artifact authored twice is ONE
    computation, and letting the run id in would split a group by who happened to author it."""
    one = _ok(_compile(catalog, PUBLIC_FEATURE, run_id="run-0001"))
    two = _ok(_compile(catalog, PUBLIC_FEATURE, run_id="run-0002"))
    assert one.authoring_run_id != two.authoring_run_id
    assert ir_hash(one) == ir_hash(two)


def test_the_SPINE_declaration_is_part_of_identity(catalog):
    """A different population is a different number in every row, so it cannot be the same
    artifact — while the declaration's PROVENANCE stays out (Task 4's `identity_payload`)."""
    one = _ok(_compile(catalog, PUBLIC_FEATURE))
    two = _ok(_compile(catalog, PUBLIC_FEATURE, spine_decl=_declaration(
        snapshot_policy=CurrentSnapshot(observed_snapshot_ref="2020-01-01"))))
    assert ir_hash(one) != ir_hash(two)


def test_the_DECLARER_does_not_change_the_hash(catalog):
    other = IdentityEnvelope(
        subject="user:ben", actor_kind="human", authenticated=False, auth_method="test",
        role_claims=("feature_engineer",))
    one = _ok(_compile(catalog, PUBLIC_FEATURE))
    two = _ok(_compile(catalog, PUBLIC_FEATURE, spine_decl=_declaration(
        declared_by=other, declaration_reason="same population, different reviewer",
        declaration_record_id="spine-decl-0002", recorded_at="2026-07-28T09:00:00+00:00")))
    assert ir_hash(one) == ir_hash(two)


def test_the_OUTPUT_POLICY_is_part_of_identity(catalog):
    """The policy decides the published type and its nullability (§6), so two policies are two
    artifacts even when every expression is identical."""
    one = _ok(_compile(catalog, RATIO_FEATURE))
    two = dataclasses.replace(
        one, output_policy=dataclasses.replace(one.output_policy, output_type="numeric"))
    assert ir_hash(one) != ir_hash(two)


def test_the_ORDER_of_the_landing_keys_is_part_of_identity(catalog):
    """Grain key ORDER is semantic (interfaces §6) and becomes the landing key of the published
    row (§5.3). It is carried explicitly rather than left to be inferred from the read set, where
    a column read for another reason already sits — so two orders would otherwise hash alike.
    """
    formula = fixtures.authored_formula(PUBLIC_FEATURE)
    two_keys = dataclasses.replace(
        formula, grain=Grain(entity="customer", keys=(TXN_CIF, TXN_MERCHANT)))
    one = _ok(_compile(catalog, PUBLIC_FEATURE, formula=two_keys))
    reordered = dataclasses.replace(one, grain_keys=tuple(reversed(one.grain_keys)))
    assert ir_hash(reordered) != ir_hash(one)


def test_the_ZERO_DENOMINATOR_policy_is_part_of_identity(catalog):
    """A customer with no transactions gets NULL, 0 or an error depending on this one field, so two
    policies are two different columns of numbers — never one artifact under one hash."""
    one = _ok(_compile(catalog, RATIO_FEATURE))
    assert one.zero_denominator is ZeroDenominator.NULL
    two = dataclasses.replace(one, zero_denominator=ZeroDenominator.ZERO)
    assert ir_hash(one) != ir_hash(two)


def test_the_ORDER_of_the_expression_tuple_does_not_change_the_hash(catalog):
    """Expressions enter identity keyed by their BODY PATH, never by position.

    Task 6 found the same defect one level down: ordering by the sequence in which things happened
    to resolve — rather than by a stable key — silently changed the hash. A ratio's halves are
    named, so which of them the tuple holds first is not a fact about the computation.
    """
    ir = _ok(_compile(catalog, RATIO_FEATURE))
    reversed_ir = dataclasses.replace(ir, expressions=tuple(reversed(ir.expressions)))
    assert [e.expr_path for e in reversed_ir.expressions] != [e.expr_path for e in ir.expressions]
    assert ir_hash(reversed_ir) == ir_hash(ir)


def test_a_ratios_two_HALVES_are_distinguished_by_their_path(catalog):
    """The corollary: order does not matter BECAUSE the path is carried. Move the same two plans to
    each other's paths and the artifact is a different one."""
    ir = _ok(_compile(catalog, RATIO_FEATURE))
    numerator, denominator = ir.expressions
    swapped = dataclasses.replace(ir, expressions=(
        dataclasses.replace(numerator, expr_path=denominator.expr_path),
        dataclasses.replace(denominator, expr_path=numerator.expr_path)))
    assert ir_hash(swapped) != ir_hash(ir)


def test_a_moved_CATALOG_changes_the_hash(catalog):
    """`catalog_state_stamp` sits inside each requirement's identity (Task 5), and a catalog that
    moved may answer a governed question differently."""
    before = _ok(_compile(catalog, PUBLIC_FEATURE))
    catalog.execute(
        "UPDATE overlay_drift_watermark SET head_seq = 99 WHERE catalog_source = %s", (_SRC,))
    assert ir_hash(_ok(_compile(catalog, PUBLIC_FEATURE))) != ir_hash(before)


def test_the_roles_that_decided_the_compilation_do_not_change_the_hash(catalog):
    """Read scope decides WHETHER a compilation is allowed, never WHAT it computes."""
    one = _ok(_compile(catalog, PUBLIC_FEATURE, roles=_ROLES))
    two = _ok(_compile(catalog, PUBLIC_FEATURE, roles=(*_ROLES, "platform_admin")))
    assert ir_hash(one) == ir_hash(two)


# ══ shape and package-wide constraints ═══════════════════════════════════════════════════════════


def test_every_public_type_is_a_frozen_slotted_dataclass():
    for cls in (FormulaExecutionIRV1, AuthorizedCompilation):
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen
        assert hasattr(cls, "__slots__")


def test_the_ir_carries_no_run_time_fields():
    names = {f.name for f in dataclasses.fields(FormulaExecutionIRV1)}
    assert "expressions" in names and "output_policy" in names and "spine" in names
    assert not names & {"input_snapshots", "partition_specs", "business_dt", "resolved_at"}


def test_business_dt_is_unreachable_from_this_module():
    assert "business_dt" not in _code_of(ir_module)
    assert "business_dt" not in inspect.signature(compile_ir).parameters


def test_the_module_owns_no_traversal_and_no_resolution_of_its_own():
    """One planner, one resolver, one gate. A private search here would be free to disagree with the
    answer governance approved."""
    source = _code_of(ir_module)
    for banned in ("graph_edge", "classify_join_path", "resolve_physical_identity",
                   "derive_requirement", "dropduplicates"):
        assert banned not in source


def test_no_pyspark_and_no_identity_minting():
    source = _code_of(ir_module)
    assert "pyspark" not in source
    assert "authenticated=true" not in source.replace(" ", "")


def test_every_refusal_this_module_can_return_is_a_TASK_1_code(catalog):
    """§14: a governed refusal never surfaces as a bare exception or a raw string."""
    refused = _compile(catalog, PUBLIC_FEATURE, spine_decl=None)
    assert isinstance(refused, MaterializationRefused)
    assert isinstance(refused.code, CompilationRefusalCode)
    assert issubclass(ReadElementKind, StrEnum)


def test_a_body_outside_child_1s_discriminated_union_is_a_CALLER_error(catalog):
    """A body Child-1's grammar has no shape for is a forged object, not a governed verdict —
    Child-1's own `SchemaError` says so, and inventing a §14 code for it would fork the
    vocabulary."""
    from featuregen.formula.schema import SchemaError

    class _Body:
        final_operation = None

    formula = dataclasses.replace(fixtures.authored_formula(PUBLIC_FEATURE), body=_Body())
    admitted = AdmittedFeature(
        feature_name=PUBLIC_FEATURE, formula=formula, formula_content_hash="not-a-real-hash",
        intent=fixtures.intent_for(PUBLIC_FEATURE), authoring_run_id="run-0001")
    with pytest.raises(SchemaError):
        compile_ir(catalog, admitted, roles=_ROLES, spine_decl=DECLARATION, inventory=INVENTORY)


def test_an_expression_ir_is_reused_verbatim_never_rebuilt(catalog):
    """Task 6's IR is the unit of expression identity; a second construction here could disagree
    with the hash Task 6 computed for the same expression."""
    ir = _ok(_compile(catalog, PUBLIC_FEATURE))
    (expr,) = ir.expressions
    assert isinstance(expr, ExpressionExecutionIR)
    assert RefRole.SOURCE_TABLE in next(
        r.roles for r in expr.physical_read_set if r.logical_ref == TXN)
