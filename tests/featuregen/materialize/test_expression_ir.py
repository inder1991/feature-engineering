"""Spec A Task 6 — the per-EXPRESSION compiled plan (§2.1, §3, §3.3, §3.5).

Three claims shape every test below.

**1. One PIT spec per EXPRESSION, never per formula.** Child-1 gives every `AggregateExpression` its
own `source_relation`, `filter` AND `window` (verified interfaces §6), so a legal ratio may read two
tables, on two event-time columns, over two windows. An IR with one PIT spec per formula cannot
represent that grammar — it would silently apply one expression's window to the other and return a
number. `test_a_ratios_two_expressions_are_compiled_INDEPENDENTLY` builds exactly that ratio.

**2. The IR is STATIC.** It carries `input_requirements` (Task 5) and never a run-time snapshot, and
`business_dt` is unreachable from the module. A partition VALUE resolved from a business date
reaching `identity_payload()` would change the generated project every day, which is the one thing a
hash-identified artifact must not do.

**3. Reference completeness replaces a stage that does not exist.** Plan rev 3 named a
`FormulaPlannerIntentV1`; it is not in the repository, so §2.1 keeps the guarantee as a TEST instead:
every `logical_ref` reachable from the expression is either in the physical read set or explicitly
classified NON-PHYSICAL with a stated reason. Anything else is `UNACCOUNTED_LOGICAL_REF`. The
discriminating case is a `FilterPredicate`: it has **no `right_ref`** (§6), so a comparison literal
that merely LOOKS like a ref must never become a column read.
"""
import dataclasses
import inspect

import pytest

from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    EmptyWindowResult,
    FilterBool,
    FilterBoolOp,
    FilterPredicate,
    FilterPredicateOp,
    Inclusivity,
    LiteralType,
    NullInput,
    ParameterRef,
    SourceRelation,
    TypedLiteral,
    WindowBasis,
    WindowPolicy,
    WindowUnit,
)
from featuregen.materialize import expression_ir
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import (
    AvailabilityBasis,
    ExpressionExecutionIR,
    NonPhysicalReason,
    PitSpec,
    RefRole,
    compile_expression,
    expression_ir_hash,
    logical_refs_in,
)
from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    EventTimePartition,
    PartitionTransform,
    TableLayout,
)
from featuregen.overlay.identity import fact_key as _fact_key
from featuregen.overlay.upload.upload_catalog import table_ref as _catalog_table_ref

_SRC = "hdfc"
_ROLES = ("feature_engineer",)
_TZ = "Asia/Kolkata"

# Refs are SCHEMA-FLATTENED (§3.5 / interfaces §17): every `object_ref` an upload produces sits
# under `public`, and the real schema survives only in `graph_node.schema_name`. The spec's
# `hdfc::banking.…` examples are shorthand for "the ref for that table" — the physical `banking`
# below is what Task 5's resolver RESOLVES, never what a ref segment says.
TXN = f"{_SRC}::public.transactions"
TXN_AMT = f"{TXN}.txn_amt"
TXN_DT = f"{TXN}.txn_dt"
TXN_CIF = f"{TXN}.cif_id"
TXN_ACCT = f"{TXN}.acct_id"
TXN_DR_CR = f"{TXN}.dr_cr_flag"
TXN_STATUS = f"{TXN}.status_cd"

ACCOUNTS = f"{_SRC}::public.accounts"
ACCOUNTS_ID = f"{ACCOUNTS}.account_id"
ACCOUNTS_CIF = f"{ACCOUNTS}.cif_id"

CUSTOMERS = f"{_SRC}::public.customers"
CUSTOMERS_CIF = f"{CUSTOMERS}.cif_id"

# The SECOND fact table, so a ratio can genuinely read two tables on two clocks over two windows.
CARDS = f"{_SRC}::public.card_txns"
CARDS_AMT = f"{CARDS}.card_amt"
CARDS_DT = f"{CARDS}.card_dt"
CARDS_CIF = f"{CARDS}.cif_id"


def _code_of(module) -> str:
    """The module's executable text, lower-cased, with comments and docstrings stripped.

    Borrowed from `test_inputs.py`: a source assertion that also read the prose would be satisfied
    by deleting an explanation, and would fail for a docstring that names what it forbids.
    """
    import io
    import tokenize

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
# Seeded the way `test_joins.py` / `test_inputs.py` do: PUBLIC-FLATTENED object refs, the REAL
# schema in `schema_name`. Governed availability needs BOTH halves of the shipped contract — the
# flat `is_as_of` flag AND the `availability_fact_event_id` link — plus the `overlay_fact_state` row
# the projection dereferenced to write that link, since `basis`/`lag_hours` live ONLY in the fact
# value (`table_fact_projection` projects the fact's `column` and nothing else).


def _col(db, table, column, *, schema="banking", sensitivity=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "schema_name, sensitivity) VALUES (%s, %s, 'column', %s, %s, %s, %s)",
        (_SRC, f"public.{table}.{column}", table, column, schema, sensitivity))


def _edge(db, from_ref, to_ref, *, cardinality="N:1", fact_key="ajf", status="VERIFIED"):
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) "
        "VALUES (%s, 'joins', %s, %s, %s, 'operational', %s, %s)",
        (_SRC, from_ref, to_ref, cardinality, fact_key, status))


def _watermark(db, *, head_seq=7):
    """`read_operational_value` fails CLOSED (GATE 3) on a lagged/degraded projection."""
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, now(), 'r', %s) ON CONFLICT (catalog_source) "
        "DO UPDATE SET last_completed_at = now(), head_seq = EXCLUDED.head_seq", (_SRC, head_seq))


def _flag_as_of(db, table, column, *, event_id="ovf_evt_asof"):
    db.execute(
        "UPDATE graph_node SET is_as_of = true, availability_fact_event_id = %s "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (event_id, _SRC, table, column))


def _fact_row(db, table, value, *, event_id="ovf_evt_asof", status="VERIFIED"):
    """The VERIFIED `availability_time` fact as the overlay read model holds it."""
    import json

    key = _fact_key(_catalog_table_ref(_SRC, table), "availability_time")
    db.execute(
        "INSERT INTO overlay_fact_state (fact_key, catalog_source, object_ref, fact_type, status, "
        "value, confirmed_at, confirmed_event_id, updated_seq) "
        "VALUES (%s, %s, %s, 'availability_time', %s, %s, now(), %s, 1) "
        "ON CONFLICT (fact_key) DO UPDATE SET value = EXCLUDED.value, status = EXCLUDED.status, "
        "confirmed_event_id = EXCLUDED.confirmed_event_id",
        (key, _SRC, f"public.{table}", status, json.dumps(value), event_id))


def _govern_availability(db, table, column, *, basis="posted_at", lag_hours=None,
                         event_id="ovf_evt_asof"):
    value = {"column": column, "basis": basis}
    if lag_hours is not None:
        value["lag_hours"] = lag_hours
    _flag_as_of(db, table, column, event_id=event_id)
    _fact_row(db, table, value, event_id=event_id)


@pytest.fixture
def catalog(db):
    """`transactions` (grain key on the table itself), `card_txns`, and the two-hop dimension path."""
    for column in ("txn_amt", "txn_dt", "cif_id", "acct_id", "dr_cr_flag", "status_cd"):
        _col(db, "transactions", column)
    for column in ("card_amt", "card_dt", "cif_id"):
        _col(db, "card_txns", column)
    _col(db, "accounts", "account_id")
    _col(db, "accounts", "cif_id")
    _col(db, "customers", "cif_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id", fact_key="ajf-txn-acct")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id", fact_key="ajf-acct-cust")
    _govern_availability(db, "transactions", "txn_dt")
    _govern_availability(db, "card_txns", "card_dt", basis="ingested_at",
                         event_id="ovf_evt_asof_cards")
    _watermark(db)
    return db


# ── the declared inventory ───────────────────────────────────────────────────────────────────────


def _layout(table, columns, *, partition_column="load_dt", time_ref=TXN_DT) -> TableLayout:
    return TableLayout(
        schema="banking", table=table,
        partition_columns=((partition_column, "string"),),
        partition_mapping=EventTimePartition(
            time_ref=time_ref, partition_column=partition_column,
            transform=PartitionTransform.DATE_ISO, timezone=_TZ),
        columns=tuple((column, "string") for column in columns),
        location=f"hdfs://nn/warehouse/banking.db/{table}", rewritten_in_place=False)


def _inventory(*, tables=None, schema_map=None) -> ClusterInventoryV1:
    declared = {
        "banking.transactions": _layout(
            "transactions", ("txn_amt", "txn_dt", "cif_id", "acct_id", "dr_cr_flag", "status_cd")),
        "banking.card_txns": _layout("card_txns", ("card_amt", "card_dt", "cif_id"),
                                     time_ref=CARDS_DT),
        "banking.accounts": _layout("accounts", ("account_id", "cif_id")),
        "banking.customers": _layout("customers", ("cif_id",)),
    }
    if tables is not None:
        declared = {key: declared[key] for key in tables}
    return ClusterInventoryV1(
        environment_id="hdfc-local", tables=declared,
        logical_schema_map=schema_map if schema_map is not None else {
            TXN: "banking", CARDS: "banking", ACCOUNTS: "banking", CUSTOMERS: "banking"},
        captured_at="2026-07-27T00:00:00Z")


INVENTORY = _inventory()


# ── expressions ──────────────────────────────────────────────────────────────────────────────────


def _window(length=30, *, event_time_ref=TXN_DT, timezone=_TZ, unit=WindowUnit.DAY) -> WindowPolicy:
    return WindowPolicy(
        event_time_ref=event_time_ref, basis=WindowBasis.TRAILING, length=length, unit=unit,
        start_inclusive=Inclusivity.INCLUSIVE, end_inclusive=Inclusivity.EXCLUSIVE,
        timezone=timezone, empty_window=EmptyWindowResult.NULL, null_input=NullInput.IGNORE)


def _expr(*, aggregation=AggregateFunction.SUM, operand=TXN_AMT, table=TXN, filter_node=None,
          window=None) -> AggregateExpression:
    return AggregateExpression(
        aggregation=aggregation, operand=operand, source_relation=SourceRelation(table_ref=table),
        filter=filter_node, window=window if window is not None else _window())


def _posted_debit_filter():
    """`dr_cr_flag = 'D' AND status_cd = 'posted'` — the only column refs are the two `left`s."""
    return FilterBool(op=FilterBoolOp.AND, children=(
        FilterPredicate(op=FilterPredicateOp.EQUAL, left=TXN_DR_CR,
                        right_literal=TypedLiteral(type=LiteralType.STRING, value="D")),
        FilterPredicate(op=FilterPredicateOp.EQUAL, left=TXN_STATUS,
                        right_literal=TypedLiteral(type=LiteralType.STRING, value="posted")),
    ))


def _compile(db, *, expr=None, expr_path="body.expr", grain_keys=(TXN_CIF,), roles=_ROLES,
             inventory=INVENTORY):
    return compile_expression(
        db, expr_path=expr_path, expr=expr if expr is not None else _expr(),
        grain_keys=grain_keys, roles=roles, inventory=inventory)


def _refs(ir: ExpressionExecutionIR) -> set[str]:
    return {ref.logical_ref for ref in ir.physical_read_set}


def _roles_of(ir: ExpressionExecutionIR, logical_ref: str) -> tuple[RefRole, ...]:
    (found,) = [ref for ref in ir.physical_read_set if ref.logical_ref == logical_ref]
    return found.roles


# ══ the physical read set ════════════════════════════════════════════════════════════════════════


def test_the_read_set_holds_operand_filter_left_event_time_grain_and_the_source_table(catalog):
    ir = _compile(catalog, expr=_expr(filter_node=_posted_debit_filter()))
    assert isinstance(ir, ExpressionExecutionIR)
    assert _refs(ir) == {TXN, TXN_AMT, TXN_DT, TXN_DR_CR, TXN_STATUS, TXN_CIF}
    assert _roles_of(ir, TXN_AMT) == (RefRole.OPERAND,)
    assert _roles_of(ir, TXN_DR_CR) == (RefRole.FILTER_LEFT,)
    assert _roles_of(ir, TXN) == (RefRole.SOURCE_TABLE,)


def test_one_ref_in_two_roles_is_ONE_read_carrying_BOTH(catalog):
    """`txn_dt` orders the window AND is the governed availability column. Two reads of one column
    would let a later stage project it twice, or drop one role's meaning silently."""
    ir = _compile(catalog)
    assert isinstance(ir, ExpressionExecutionIR)
    assert len([r for r in ir.physical_read_set if r.logical_ref == TXN_DT]) == 1
    assert set(_roles_of(ir, TXN_DT)) == {RefRole.EVENT_TIME, RefRole.AVAILABILITY}


def test_the_physical_schema_is_the_RESOLVED_one_never_the_refs_own_segment(catalog):
    """Every ref says `public`; the rows live in `banking`. Parsing the ref would read a DIFFERENT
    table and produce plausible numbers (§3.5)."""
    ir = _compile(catalog)
    assert isinstance(ir, ExpressionExecutionIR)
    assert {ref.schema for ref in ir.physical_read_set} == {"banking"}
    assert {ref.table for ref in ir.physical_read_set} == {"transactions"}


def test_an_unresolvable_physical_schema_REFUSES(catalog):
    """Task 5's verdict is propagated, not softened: no attested schema and no declared mapping
    means the compiler does not know which table the numbers come from."""
    catalog.execute("UPDATE graph_node SET schema_name = NULL WHERE catalog_source = %s", (_SRC,))
    result = _compile(catalog, inventory=_inventory(schema_map={}))
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED


def test_a_table_absent_from_the_inventory_REFUSES(catalog):
    """Absent is UNKNOWN, never `unpartitioned` — Task 5's distinction, carried through."""
    result = _compile(catalog, inventory=_inventory(tables=("banking.customers",)))
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN


# ══ joins: every endpoint is READ, and every refusal is PROPAGATED ═══════════════════════════════


def test_a_grain_key_on_the_source_table_needs_no_hops(catalog):
    ir = _compile(catalog)
    assert isinstance(ir, ExpressionExecutionIR)
    assert ir.join_plan.steps == ()
    assert ir.join_plan.fans_out is False


def test_every_join_ENDPOINT_column_is_in_the_read_set(catalog):
    """A join key is a column the query reads. Gate 2 authorizes the read set, so an endpoint left
    out of it would be joined on without ever being authorized."""
    ir = _compile(catalog, grain_keys=(CUSTOMERS_CIF,))
    assert isinstance(ir, ExpressionExecutionIR)
    assert {TXN_ACCT, ACCOUNTS_ID, ACCOUNTS_CIF, CUSTOMERS_CIF} <= _refs(ir)
    assert _roles_of(ir, TXN_ACCT) == (RefRole.JOIN_KEY,)
    assert set(_roles_of(ir, CUSTOMERS_CIF)) == {RefRole.GRAIN_KEY, RefRole.JOIN_KEY}


def test_every_table_on_the_join_PATH_gets_its_own_input_requirement(catalog):
    """`accounts` is never named by the caller — it is exactly the table whose layout nobody else
    would check, and it is read all the same."""
    ir = _compile(catalog, grain_keys=(CUSTOMERS_CIF,))
    assert isinstance(ir, ExpressionExecutionIR)
    assert [(r.schema, r.table) for r in ir.input_requirements] == [
        ("banking", "transactions"), ("banking", "accounts"), ("banking", "customers")]


def test_a_fan_out_toward_the_grain_REFUSES_the_expression(catalog):
    """`plan_join` refuses it; this task must not repair or downgrade the verdict (§3.2)."""
    catalog.execute("UPDATE graph_edge SET cardinality = '1:N' WHERE from_ref = %s",
                    ("public.transactions.acct_id",))
    result = _compile(catalog, grain_keys=(CUSTOMERS_CIF,))
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED


def test_an_unknown_cardinality_REFUSES_the_expression(catalog):
    catalog.execute("UPDATE graph_edge SET cardinality = NULL WHERE from_ref = %s",
                    ("public.accounts.cif_id",))
    result = _compile(catalog, grain_keys=(CUSTOMERS_CIF,))
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_an_ungoverned_path_to_the_grain_REFUSES(catalog):
    catalog.execute("DELETE FROM graph_edge WHERE catalog_source = %s", (_SRC,))
    result = _compile(catalog, grain_keys=(CUSTOMERS_CIF,))
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED


def test_the_module_owns_no_traversal_of_its_own(catalog):
    """One planner, one answer. A private search over `graph_edge` here would be free to disagree
    with the path governance approved."""
    source = _code_of(expression_ir)
    for banned in ("graph_edge", "classify_join_path", "dropduplicates", "drop_duplicates"):
        assert banned not in source


# ══ the PIT spec is PER EXPRESSION ═══════════════════════════════════════════════════════════════


def test_a_ratios_two_expressions_are_compiled_INDEPENDENTLY(catalog):
    """The claim that decides whether this task is right (§3 / interfaces §6): a legal ratio reads
    two tables, on two event-time columns, over two windows, with two availability bases. One PIT
    spec per FORMULA could not express it — it would apply one window to both halves."""
    numerator = _compile(catalog, expr_path="body.numerator", expr=_expr(window=_window(90)))
    denominator = _compile(
        catalog, expr_path="body.denominator", grain_keys=(CARDS_CIF,),
        expr=_expr(operand=CARDS_AMT, table=CARDS,
                   window=_window(30, event_time_ref=CARDS_DT, timezone="UTC")))
    assert isinstance(numerator, ExpressionExecutionIR)
    assert isinstance(denominator, ExpressionExecutionIR)

    assert numerator.pit.event_time_ref == TXN_DT
    assert denominator.pit.event_time_ref == CARDS_DT
    assert (numerator.pit.window_length, denominator.pit.window_length) == (90, 30)
    assert (numerator.pit.window_timezone, denominator.pit.window_timezone) == (_TZ, "UTC")
    assert numerator.pit.availability_basis is AvailabilityBasis.POSTED_AT
    assert denominator.pit.availability_basis is AvailabilityBasis.INGESTED_AT
    assert [(r.schema, r.table) for r in numerator.input_requirements] == \
           [("banking", "transactions")]
    assert [(r.schema, r.table) for r in denominator.input_requirements] == \
           [("banking", "card_txns")]
    assert expression_ir_hash(numerator) != expression_ir_hash(denominator)


def test_two_windows_that_differ_only_in_LENGTH_hash_differently(catalog):
    thirty = _compile(catalog, expr=_expr(window=_window(30)))
    ninety = _compile(catalog, expr=_expr(window=_window(90)))
    assert expression_ir_hash(thirty) != expression_ir_hash(ninety)


def test_two_windows_that_differ_only_in_UNIT_hash_differently(catalog):
    """A calendar month is not 30 days; collapsing the unit into a day count is the §8 rule 2
    mistake, and it must not be invisible to identity."""
    days = _compile(catalog, expr=_expr(window=_window(3, unit=WindowUnit.DAY)))
    months = _compile(catalog, expr=_expr(window=_window(3, unit=WindowUnit.MONTH)))
    assert expression_ir_hash(days) != expression_ir_hash(months)


def test_the_same_expression_compiles_to_the_same_hash_twice(catalog):
    first = _compile(catalog, expr=_expr(filter_node=_posted_debit_filter()))
    second = _compile(catalog, expr=_expr(filter_node=_posted_debit_filter()))
    assert expression_ir_hash(first) == expression_ir_hash(second)


def test_the_body_path_is_part_of_identity(catalog):
    """A ratio of an expression with itself still stages two outputs; one hash for both would let
    the assembly step read one where it meant the other."""
    numerator = _compile(catalog, expr_path="body.numerator")
    denominator = _compile(catalog, expr_path="body.denominator")
    assert expression_ir_hash(numerator) != expression_ir_hash(denominator)


def test_an_expr_path_outside_the_body_vocabulary_is_a_CALLER_error(catalog):
    """§14 has no member for "the call was assembled wrongly" — the same line `plan_join` draws."""
    with pytest.raises(ValueError, match="body.expr"):
        _compile(catalog, expr_path="body.whatever")


# ══ availability is GOVERNED, and its basis comes from the FACT ══════════════════════════════════


def test_the_availability_basis_and_column_come_from_the_governed_FACT(catalog):
    ir = _compile(catalog)
    assert isinstance(ir, ExpressionExecutionIR)
    assert ir.pit.availability_ref == TXN_DT
    assert ir.pit.availability_basis is AvailabilityBasis.POSTED_AT
    assert ir.pit.availability_lag_hours is None


def test_event_time_plus_lag_carries_its_LAG(catalog):
    """`lag_hours` is REQUIRED iff basis is `event_time_plus_lag` (interfaces §4); rendering the
    gate without it would apply no lag at all and admit rows that had not arrived."""
    _govern_availability(catalog, "transactions", "txn_dt", basis="event_time_plus_lag",
                         lag_hours=36)
    ir = _compile(catalog)
    assert isinstance(ir, ExpressionExecutionIR)
    assert ir.pit.availability_basis is AvailabilityBasis.EVENT_TIME_PLUS_LAG
    assert ir.pit.availability_lag_hours == "36"


def test_no_availability_column_at_all_REFUSES(catalog):
    catalog.execute(
        "UPDATE graph_node SET is_as_of = false, availability_fact_event_id = NULL "
        "WHERE catalog_source = %s AND table_name = 'transactions'", (_SRC,))
    result = _compile(catalog)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_a_FILE_DECLARED_as_of_column_is_not_GOVERNED(catalog):
    """`build_graph` writes `is_as_of` straight from the upload. A spreadsheet column tagged
    "as of" is a hint; a point-in-time gate resting on it rests on nobody."""
    catalog.execute(
        "UPDATE graph_node SET availability_fact_event_id = NULL "
        "WHERE catalog_source = %s AND table_name = 'transactions'", (_SRC,))
    result = _compile(catalog)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_two_governed_availability_columns_REFUSE(catalog):
    """`AVAILABILITY_TIME` names ONE column (interfaces §4), so two governed as-of columns can only
    be a stale projection. The detail is asserted because it is what makes this test discriminating:
    drop the ambiguity check and compilation still refuses — on whichever column sorted FIRST
    disagreeing with the fact — which is the same code for a different, and wrong, reason."""
    _flag_as_of(catalog, "transactions", "cif_id")
    result = _compile(catalog)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED
    assert "2 columns carry a governed availability_time fact" in result.detail


def test_a_DEGRADED_catalog_projection_REFUSES(catalog):
    """GATE 3, inherited rather than re-implemented: `read_operational_value` fails CLOSED when the
    load-bearing `overlay` projection is poisoned or lagged, because every governed read behind it
    is then potentially stale. Without that call the flat `is_as_of` flag plus its link would still
    look governed, and the point-in-time gate would rest on a read model nobody trusts."""
    catalog.execute(
        "INSERT INTO projection_degraded (projection_name, aggregate, aggregate_id, reason, "
        "poison_seq) VALUES ('overlay', 'overlay_fact', 'f1', 'poisoned', 1)")
    result = _compile(catalog)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_a_fact_PAYLOAD_that_is_not_the_one_the_projection_linked_REFUSES(catalog):
    """The projection writes the fact's `confirmed_event_id` onto the column; the payload is read
    back THROUGH that link. A row whose id disagrees is a different fact, and reading its basis
    would be a second, unguarded opinion about which clock gates the rows."""
    catalog.execute("UPDATE overlay_fact_state SET confirmed_event_id = 'ovf_evt_other' "
                    "WHERE object_ref = 'public.transactions'")
    result = _compile(catalog)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_a_fact_naming_a_DIFFERENT_column_than_the_projection_flagged_REFUSES(catalog):
    """A stale projection: the flag says one column, the fact says another. Trusting either half
    alone applies the gate to a column nobody governed for it."""
    _fact_row(catalog, "transactions", {"column": "cif_id", "basis": "posted_at"})
    result = _compile(catalog)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_a_fact_value_that_fails_its_GOVERNED_schema_REFUSES(catalog):
    """`event_time_plus_lag` without `lag_hours` is undefined; the overlay's own schema says so."""
    _fact_row(catalog, "transactions",
              {"column": "txn_dt", "basis": "event_time_plus_lag"})
    result = _compile(catalog)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_a_non_VERIFIED_fact_row_REFUSES(catalog):
    _fact_row(catalog, "transactions", {"column": "txn_dt", "basis": "posted_at"},
              status="DRAFT")
    result = _compile(catalog)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED


def test_a_refusal_never_echoes_a_data_value(catalog):
    """Refusal detail carries counts, types, hashes and locations — never data (global constraint)."""
    expr = _expr(filter_node=FilterPredicate(
        op=FilterPredicateOp.EQUAL, left=TXN_STATUS,
        right_literal=TypedLiteral(type=LiteralType.STRING, value="a-secret-status-code")))
    catalog.execute(
        "UPDATE graph_node SET availability_fact_event_id = NULL "
        "WHERE catalog_source = %s AND table_name = 'transactions'", (_SRC,))
    result = _compile(catalog, expr=expr)
    assert isinstance(result, MaterializationRefused)
    assert "a-secret-status-code" not in result.detail


# ══ §2.1 reference completeness ══════════════════════════════════════════════════════════════════


def test_a_comparison_literal_that_LOOKS_like_a_ref_is_never_read(catalog):
    """A `FilterPredicate` has NO `right_ref` (interfaces §6): its only column reference is `left`.
    So a literal whose text happens to be ref-shaped is a VALUE, and reading it as a column would
    join a table the formula never mentioned."""
    expr = _expr(filter_node=FilterPredicate(
        op=FilterPredicateOp.EQUAL, left=TXN_STATUS,
        right_literal=TypedLiteral(type=LiteralType.STRING, value=ACCOUNTS_ID)))
    ir = _compile(catalog, expr=expr)
    assert isinstance(ir, ExpressionExecutionIR)
    assert ACCOUNTS_ID not in _refs(ir)
    (classified,) = ir.non_physical_refs
    assert classified.reason is NonPhysicalReason.COMPARISON_LITERAL
    assert classified.location.endswith("right_literal.value")


def test_a_ref_shaped_run_PARAMETER_name_is_classified_not_read(catalog):
    expr = _expr(filter_node=FilterPredicate(
        op=FilterPredicateOp.EQUAL, left=TXN_STATUS, right_param=ParameterRef(name=ACCOUNTS_ID)))
    ir = _compile(catalog, expr=expr)
    assert isinstance(ir, ExpressionExecutionIR)
    assert ACCOUNTS_ID not in _refs(ir)
    (classified,) = ir.non_physical_refs
    assert classified.reason is NonPhysicalReason.RUN_PARAMETER


def test_a_ref_reached_only_through_NESTED_filter_children_is_still_read(catalog):
    """The walk is exhaustive, not a two-level peek: a predicate three levels down is a real read."""
    deep = FilterBool(op=FilterBoolOp.AND, children=(
        FilterPredicate(op=FilterPredicateOp.EQUAL, left=TXN_DR_CR,
                        right_literal=TypedLiteral(type=LiteralType.STRING, value="D")),
        FilterBool(op=FilterBoolOp.OR, children=(
            FilterPredicate(op=FilterPredicateOp.IS_NULL, left=TXN_STATUS),
            FilterPredicate(op=FilterPredicateOp.IS_NOT_NULL, left=TXN_ACCT),
        )),
    ))
    ir = _compile(catalog, expr=_expr(filter_node=deep))
    assert isinstance(ir, ExpressionExecutionIR)
    assert {TXN_DR_CR, TXN_STATUS, TXN_ACCT} <= _refs(ir)


def test_a_column_of_a_relation_the_expression_does_NOT_read_is_UNACCOUNTED(catalog):
    """Child-1 requires the operand, the event-time ref and every filter `left` to be contained in
    the expression's own `source_relation` — which is the ONLY reason those refs need no per-column
    resolution here. Verified, not assumed: an operand on another table would otherwise be recorded
    with the SOURCE table's physical schema and table, i.e. as a column of a table it is not in, and
    the generated projection would select it from the wrong relation."""
    result = _compile(catalog, expr=_expr(operand=ACCOUNTS_ID))
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.UNACCOUNTED_LOGICAL_REF
    assert "body.expr.operand" in result.detail


def test_a_filter_LEFT_outside_the_source_relation_is_UNACCOUNTED(catalog):
    expr = _expr(filter_node=FilterPredicate(
        op=FilterPredicateOp.IS_NULL, left=ACCOUNTS_CIF))
    result = _compile(catalog, expr=expr)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.UNACCOUNTED_LOGICAL_REF
    assert "body.expr.filter.left" in result.detail


def test_a_ref_in_a_slot_NOBODY_consumes_is_UNACCOUNTED(catalog):
    """The guarantee the removed `FormulaPlannerIntentV1` stage was supposed to give. A ref-shaped
    string reachable from the expression that no compilation step consumed is a compilation DEFECT
    — including, one day, a new field added to `AggregateExpression` that this compiler never
    learned to read."""
    result = _compile(catalog, expr=_expr(window=_window(timezone=ACCOUNTS_ID)))
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.UNACCOUNTED_LOGICAL_REF
    assert "window.timezone" in result.detail          # the LOCATION, so it can be fixed


def test_the_walk_reports_a_ref_ONCE_PER_LOCATION(catalog):
    """Accounting is per location, not per string: two slots holding the same ref are two claims."""
    found = logical_refs_in(_expr(filter_node=_posted_debit_filter()), "body.expr")
    assert [location for location, _ref in found] == [
        "body.expr.operand",
        "body.expr.source_relation.table_ref",
        "body.expr.filter.children[0].left",
        "body.expr.filter.children[1].left",
        "body.expr.window.event_time_ref",
    ]                                       # declaration order of `AggregateExpression`'s fields


def test_the_walk_reports_only_what_the_REF_PARSER_accepts(catalog):
    """A timezone, a status code and every `StrEnum` vocabulary word are reachable strings too. The
    walk asks `parse_ref` — the grammar the readers use — rather than a private pattern, so a walk
    that yielded every string would report `Asia/Kolkata` as an unaccounted reference and refuse
    every formula that names a timezone."""
    found = set(dict(logical_refs_in(_expr(filter_node=_posted_debit_filter()),
                                     "body.expr")).values())
    assert found == {TXN, TXN_AMT, TXN_DT, TXN_DR_CR, TXN_STATUS}
    assert _TZ not in found and "sum" not in found and "posted" not in found


# ══ the IR is STATIC ═════════════════════════════════════════════════════════════════════════════


def test_the_ir_carries_input_requirements_and_no_snapshots(catalog):
    """Asserted BY NAME: `input_requirements` is Task 5's static half. An `input_snapshots` field
    would put a run observation into a generation-time artifact."""
    names = {f.name for f in dataclasses.fields(ExpressionExecutionIR)}
    assert "input_requirements" in names
    assert "input_snapshots" not in names and "partition_specs" not in names
    ir = _compile(catalog)
    assert isinstance(ir, ExpressionExecutionIR)
    assert all(not hasattr(r, "partition_specs") for r in ir.input_requirements)


def test_business_dt_is_unreachable_from_this_module():
    assert "business_dt" not in _code_of(expression_ir)
    assert "business_dt" not in inspect.signature(compile_expression).parameters


def test_identity_excludes_run_time_values_and_provenance(catalog):
    """Provenance out (two readers with different roles compile ONE artifact); run-time values out
    (the generated project must be identical on every business date)."""
    ir = _compile(catalog, grain_keys=(CUSTOMERS_CIF,))
    assert isinstance(ir, ExpressionExecutionIR)
    payload = str(ir.identity_payload())
    for banned in ("business_dt", "partition_specs", "input_snapshot", "resolved_at",
                   "captured_at", "roles_used", "approved_join_fact_key", "ajf-txn-acct",
                   "feature_engineer", "location", "rewritten_in_place"):
        assert banned not in payload, banned


def test_the_roles_that_decided_the_plan_do_not_change_the_hash(catalog):
    """Read scope decides WHETHER a path is visible, never WHAT the computation is. Two authorized
    readers must produce one artifact, or a group would split by who compiled it."""
    one = _compile(catalog, grain_keys=(CUSTOMERS_CIF,), roles=("feature_engineer",))
    two = _compile(catalog, grain_keys=(CUSTOMERS_CIF,),
                   roles=("feature_engineer", "platform_admin"))
    assert isinstance(one, ExpressionExecutionIR) and isinstance(two, ExpressionExecutionIR)
    assert one.join_plan.roles_used != two.join_plan.roles_used
    assert expression_ir_hash(one) == expression_ir_hash(two)


def test_a_moved_catalog_DOES_change_the_hash(catalog):
    """`catalog_state_stamp` is inside the requirement's identity (Task 5): a catalog that moved
    may answer differently, so the compiled artifact is a different artifact."""
    before = _compile(catalog)
    _watermark(catalog, head_seq=99)
    after = _compile(catalog)
    assert expression_ir_hash(before) != expression_ir_hash(after)


def test_the_filter_tree_is_the_FORMULAS_OWN_canonical_form(catalog):
    """One canonicalizer. A second rendering here could disagree with `formula_content_hash` about
    what the filter is, and then two hashes would name one filter."""
    from featuregen.formula.canonical import filter_plain

    ir = _compile(catalog, expr=_expr(filter_node=_posted_debit_filter()))
    assert isinstance(ir, ExpressionExecutionIR)
    assert ir.filter_tree == filter_plain(_posted_debit_filter(), "body.expr.filter")


def test_no_filter_means_no_filter_tree(catalog):
    ir = _compile(catalog)
    assert isinstance(ir, ExpressionExecutionIR)
    assert ir.filter_tree is None


def test_the_aggregation_is_CARRIED_never_rederived(catalog):
    ir = _compile(catalog, expr=_expr(aggregation=AggregateFunction.COUNT_DISTINCT))
    assert isinstance(ir, ExpressionExecutionIR)
    assert ir.aggregation is AggregateFunction.COUNT_DISTINCT


def test_count_rows_has_no_operand_and_still_compiles(catalog):
    """`operand is None` IFF `COUNT_ROWS` (interfaces §6) — the read set simply has no operand."""
    ir = _compile(catalog, expr=_expr(aggregation=AggregateFunction.COUNT_ROWS, operand=None))
    assert isinstance(ir, ExpressionExecutionIR)
    assert _refs(ir) == {TXN, TXN_DT, TXN_CIF}


# ══ shape ════════════════════════════════════════════════════════════════════════════════════════


def test_every_public_type_is_a_frozen_slotted_dataclass():
    for cls in (ExpressionExecutionIR, PitSpec, expression_ir.PhysicalRef,
                expression_ir.NonPhysicalRef):
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen
        assert hasattr(cls, "__slots__")


def test_no_pyspark_and_no_identity_minting():
    source = _code_of(expression_ir)
    assert "pyspark" not in source
    assert "authenticated=true" not in source.replace(" ", "")
