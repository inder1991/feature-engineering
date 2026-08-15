"""SUCCESSOR 2 increment 1 — the real ``MetastoreMetadata``: L1's three questions over SQL.

**What every test here is defending.** ``runprep`` states the hazard in its own source: *"'this
table has no partitions' and 'the metastore did not answer' are the same empty list"*. An adapter
that returned ``()`` for an unreachable endpoint, an unknown table or a denial would validate a
world nobody looked at — L1 would report every declared partition ABSENT (a governed
``GOVERNED_FACT_MISMATCH`` about a table that is fine), or ``runprep`` would resolve a ``FULL_SCAN``
to nothing and the run would compute a smaller number with no error anywhere. So the tests below
assert the TYPE of every outcome, never merely that something was raised, and each of the three
ambiguities has its own test with its own exception.

**The transport is faked at the driver seam, not mocked at the adapter.** ``_Engine`` is a DB-API
2.0 double: it records the exact statements the adapter sent, answers them with
``(column names, rows)`` and can fail one with any driver exception. Nothing here opens a socket,
imports a driver, needs a JVM or knows what a metastore is — which is the whole reason the adapter
may live in ``src/`` at all.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.metastore_sql import (
    FAULT_PATTERNS,
    MetastoreAnswerRefused,
    MetastoreEndpoint,
    MetastoreFault,
    MetastoreFaultError,
    MetastoreReadScopeUnanswerable,
    MetastoreSession,
    SqlMetastoreAdapter,
    quoted_identifier,
)
from featuregen.materialize.validation import ClusterUnreachable, MetastoreTableUnknown

ENDPOINT = MetastoreEndpoint(engine="hive", host="hive-endpoint", port=10000,
                             auth_mechanism="NONE", principal="featuregen")


# ── the DB-API double ────────────────────────────────────────────────────────────────────────────


class _DriverError(Exception):
    """What a DB-API driver raises: one exception type for every failure, message-classified.

    PyHive raises ``OperationalError`` for a dropped transport, an unknown table and a denial
    alike, which is exactly why the adapter classifies on the MESSAGE and why an unrecognised one
    has to fail closed rather than pick a neighbour.
    """


class _Cursor:
    def __init__(self, engine: _Engine) -> None:
        self._engine = engine
        self.description = None

    def execute(self, operation: str) -> None:
        self._engine.statements.append(operation)
        answer = self._engine.answer(operation)
        if isinstance(answer, BaseException):
            raise answer
        columns, rows = answer
        self.description = tuple((name, None) for name in columns) or None
        self._rows = tuple(rows)

    def fetchall(self):
        return self._rows

    def close(self) -> None:
        self._engine.closed_cursors += 1


class _Connection:
    def __init__(self, engine: _Engine) -> None:
        self._engine = engine

    def cursor(self) -> _Cursor:
        return _Cursor(self._engine)

    def close(self) -> None:
        self._engine.closed_connections += 1


class _Engine:
    """A metastore that answers the statements it was stocked with, and records every one.

    Unstocked, it APPLIES what it is told rather than ignoring it: a ``CREATE OR REPLACE VIEW`` is
    remembered against its target and a later ``SHOW CREATE TABLE`` of that target reports it. That
    is what makes the publication swap's read-back a real check in a test — a double that answered
    a canned string would confirm a swap it never saw — while a stocked answer still overrides it,
    which is how the *unconfirmed* cases below are staged.
    """

    def __init__(self, answers=None, *, connect_error: BaseException | None = None) -> None:
        self.answers = list((answers or {}).items())
        self.statements: list[str] = []
        self.views: dict[str, str] = {}
        self.connections = 0
        self.closed_cursors = 0
        self.closed_connections = 0
        self._connect_error = connect_error

    def connect(self, endpoint: MetastoreEndpoint) -> _Connection:
        assert endpoint is ENDPOINT or isinstance(endpoint, MetastoreEndpoint)
        self.connections += 1
        if self._connect_error is not None:
            raise self._connect_error
        return _Connection(self)

    def answer(self, statement: str):
        for fragment, answer in self.answers:
            if fragment in statement:
                return answer
        if statement.startswith(_CREATE_VIEW):
            target = statement[len(_CREATE_VIEW):].split(" AS ", 1)[0].strip()
            self.views[target] = statement
            return ((), ())
        if statement.startswith(_SHOW_CREATE):
            target = statement[len(_SHOW_CREATE):].strip()
            definition = self.views.get(target)
            return (("createtab_stmt",), () if definition is None else ((definition,),))
        return ((), ())


_CREATE_VIEW = "CREATE OR REPLACE VIEW "
_SHOW_CREATE = "SHOW CREATE TABLE "


def _adapter(answers=None, **kwargs) -> tuple[SqlMetastoreAdapter, _Engine]:
    engine = _Engine(answers, **kwargs)
    return SqlMetastoreAdapter(MetastoreSession(ENDPOINT, connect=engine.connect)), engine


# ── the endpoint ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [{"engine": "trino"}, {"host": "  "}, {"port": 0}, {"port": 70000}, {"port": "10000"},
     {"auth_mechanism": ""}, {"principal": ""}])
def test_an_endpoint_nobody_fully_stated_is_REFUSED_rather_than_defaulted(kwargs) -> None:
    """Half a configuration is not a posture. ``metastore=None`` is how a deployment says it cannot
    execute a run; a blank host or an unknown engine is a deployment that thinks it can."""
    stated = {"engine": "hive", "host": "h", "port": 10000,
              "auth_mechanism": "NONE", "principal": "p"}
    with pytest.raises(ValueError):
        MetastoreEndpoint(**{**stated, **kwargs})


def test_the_connection_is_opened_ONCE_and_lazily() -> None:
    """Constructing an adapter must not reach the cluster: ``lane_config_from_env`` builds one for
    every claimed job, and a connection per question would be a connection per table."""
    adapter, engine = _adapter({"SHOW PARTITIONS": (("partition",), ())})
    assert engine.connections == 0

    adapter.list_partitions(schema="banking", table="transactions")
    adapter.list_partitions(schema="banking", table="customers")

    assert engine.connections == 1
    assert engine.closed_cursors == 2, "every cursor is closed, including on the happy path"


def test_a_connect_failure_is_CLUSTER_UNREACHABLE_and_not_an_empty_world() -> None:
    adapter, _engine = _adapter(connect_error=OSError("connection refused"))
    with pytest.raises(ClusterUnreachable):
        adapter.list_partitions(schema="banking", table="transactions")


# ── identifiers ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hostile", ["tx`; DROP TABLE x", "tx WHERE 1=1", "tx-1", "", "a b"])
def test_an_identifier_that_is_not_one_is_REFUSED_before_a_statement_exists(hostile) -> None:
    """No SQL dialect binds an identifier as a parameter, so the only defence that does not depend
    on getting the quoting right is refusing anything that is not plainly an identifier."""
    adapter, engine = _adapter()
    with pytest.raises(ValueError):
        adapter.list_partitions(schema="banking", table=hostile)
    with pytest.raises(ValueError):
        adapter.describe_table(schema=hostile, table="transactions")
    assert engine.statements == [], "a hostile identifier reached the engine"


def test_a_hostile_ROLE_never_reaches_a_SHOW_GRANT() -> None:
    adapter, engine = _adapter()
    with pytest.raises(ValueError):
        adapter.can_read(schema="banking", table="transactions", roles=["r`; GRANT ALL"])
    assert engine.statements == []


def test_quoted_identifier_backquotes_what_it_accepts() -> None:
    assert quoted_identifier(" txn ", field_name="table") == "`txn`"


# ── question 1: which partitions does this table have ────────────────────────────────────────────


def test_partitions_come_back_in_the_metastore_s_own_column_order() -> None:
    """Ordered, because a multi-column partition is ADDRESSED positionally — a set that agreed
    about the values and disagreed about their order would name a different partition."""
    adapter, engine = _adapter({"SHOW PARTITIONS": (
        ("partition",), (("load_dt=2026-08-14/branch=BLR",), ("load_dt=2026-08-15/branch=BLR",)))})

    listed = adapter.list_partitions(schema="banking", table="transactions")

    assert listed == ((("load_dt", "2026-08-14"), ("branch", "BLR")),
                      (("load_dt", "2026-08-15"), ("branch", "BLR")))
    assert engine.statements == ["SHOW PARTITIONS `banking`.`transactions`"]


def test_a_partition_VALUE_is_percent_decoded_because_the_engine_encoded_it() -> None:
    """Hive escapes exactly the characters that would make its own listing ambiguous, ``%``
    included, so decoding is the inverse of the encoding rather than a guess — and NOT decoding
    would compare ``BLR%20MAIN`` against a declaration that never said that."""
    adapter, _engine = _adapter({"SHOW PARTITIONS": (
        ("partition",), (("branch=BLR%20MAIN/note=100%25",),))})

    assert adapter.list_partitions(schema="banking", table="transactions") == (
        (("branch", "BLR MAIN"), ("note", "100%")),)


def test_a_table_the_engine_says_is_NOT_PARTITIONED_is_the_one_legitimate_empty() -> None:
    """The engine ANSWERED. This is the only empty listing the adapter may produce, and it is the
    answer `MetastorePartitions` documents ("an empty sequence when it has none")."""
    adapter, _engine = _adapter({"SHOW PARTITIONS": _DriverError(
        "Error: Table banking.reference_rates is not a partitioned table")})

    assert adapter.list_partitions(schema="banking", table="reference_rates") == ()


def test_an_UNREACHABLE_metastore_is_never_an_empty_partition_list() -> None:
    """The mutant this whole module exists to fail: an adapter that answered ``()`` here would make
    L1 report every resolved partition ABSENT — a governed fact about a table nothing looked at."""
    adapter, _engine = _adapter({"SHOW PARTITIONS": _DriverError(
        "TTransportException: Could not connect to hive-endpoint:10000")})

    with pytest.raises(ClusterUnreachable):
        adapter.list_partitions(schema="banking", table="transactions")


def test_an_UNKNOWN_TABLE_is_typed_and_distinguishable_from_an_empty_one() -> None:
    adapter, _engine = _adapter({"SHOW PARTITIONS": _DriverError(
        "AnalysisException: Table or view not found: banking.transactions")})

    with pytest.raises(MetastoreTableUnknown):
        adapter.list_partitions(schema="banking", table="transactions")


def test_a_DENIED_listing_is_typed_and_is_not_ClusterUnreachable() -> None:
    """L1 asked ``can_read`` first and was told yes, so a denial here is the environment
    contradicting its own answer — not a verdict about a feature, and not an unreachable cluster."""
    adapter, _engine = _adapter({"SHOW PARTITIONS": _DriverError(
        "HiveAccessControlException: Permission denied: user [svc] does not have [SELECT]")})

    with pytest.raises(MetastoreAnswerRefused):
        adapter.list_partitions(schema="banking", table="transactions")


def test_an_UNRECOGNISED_driver_message_FAILS_CLOSED_and_names_itself() -> None:
    """The classification table is a table of MESSAGES and is therefore incomplete by construction.
    An unknown message must not be folded into its nearest neighbour: it raises, carrying the
    statement and the driver's own words, so the fix is one visible line."""
    adapter, _engine = _adapter({"SHOW PARTITIONS": _DriverError("java.lang.NoSuchMethodError: ǵ")})

    with pytest.raises(MetastoreFaultError) as raised:
        adapter.list_partitions(schema="banking", table="transactions")

    assert raised.value.fault is MetastoreFault.UNRECOGNISED
    assert "SHOW PARTITIONS" in raised.value.statement
    assert "NoSuchMethodError" in str(raised.value)


@pytest.mark.parametrize("printed", ["", "load_dt", "=2026-08-14", "/"])
def test_a_partition_the_adapter_cannot_PARSE_raises_rather_than_shrinking_the_listing(
        printed) -> None:
    """Dropping an unparseable row would shorten the listing, and a partition missing from a
    listing reads downstream as a partition missing from the table."""
    adapter, _engine = _adapter({"SHOW PARTITIONS": (("partition",), ((printed,),))})

    with pytest.raises(ValueError):
        adapter.list_partitions(schema="banking", table="transactions")


# ── question 2: which columns and physical types ─────────────────────────────────────────────────

_DESCRIBE = (
    ("col_name", "data_type", "comment"),
    (("cif_id", "string", ""),
     ("txn_amt", "decimal(18,2)", ""),
     ("", "", ""),
     ("# Partition Information", "", ""),
     ("# col_name", "data_type", "comment"),
     ("load_dt", "string", "")),
)


def test_describe_returns_DATA_and_PARTITION_columns_in_order() -> None:
    """L1's read set does not distinguish them — a read of a partition column would otherwise find
    no observed type at all — and the section markers `DESCRIBE` prints are not columns."""
    adapter, engine = _adapter({
        "SHOW TABLES": (("tab_name",), (("transactions",),)), "DESCRIBE": _DESCRIBE})

    assert adapter.describe_table(schema="banking", table="transactions") == (
        ("cif_id", "string"), ("txn_amt", "decimal(18,2)"), ("load_dt", "string"))
    assert engine.statements == ["SHOW TABLES IN `banking` LIKE 'transactions'",
                                 "DESCRIBE `banking`.`transactions`"]


def test_a_column_described_TWICE_is_one_column() -> None:
    adapter, _engine = _adapter({
        "SHOW TABLES": (("tab_name",), (("t",),)),
        "DESCRIBE": (("col_name", "data_type"),
                     (("load_dt", "string"), ("# Partition Information", ""),
                      ("load_dt", "string")))})

    assert adapter.describe_table(schema="banking", table="t") == (("load_dt", "string"),)


def test_a_table_the_engine_does_not_LIST_is_None_and_is_never_DESCRIBED() -> None:
    """``None`` is the seam's own word for *the table does not exist*, and it is reached by ASKING
    rather than by reading an absence out of a failure message."""
    adapter, engine = _adapter({"SHOW TABLES": (("tab_name",), ())})

    assert adapter.describe_table(schema="banking", table="transactions") is None
    assert engine.statements == ["SHOW TABLES IN `banking` LIKE 'transactions'"]


def test_a_schema_that_does_not_exist_is_a_table_that_does_not_exist() -> None:
    adapter, _engine = _adapter({"SHOW TABLES": _DriverError(
        "AnalysisException: Database 'nowhere' does not exist")})

    assert adapter.describe_table(schema="nowhere", table="transactions") is None


def test_an_UNREACHABLE_describe_is_not_a_table_that_does_not_exist() -> None:
    """The `None` in this seam MEANS "it is not there". Returning it for silence would file a
    governed COLUMN_ABSENT against every column of a table nobody looked at."""
    adapter, _engine = _adapter({"SHOW TABLES": _DriverError("Connection reset by peer")})

    with pytest.raises(ClusterUnreachable):
        adapter.describe_table(schema="banking", table="transactions")


def test_a_DENIED_describe_is_not_a_table_that_does_not_exist() -> None:
    adapter, _engine = _adapter({
        "SHOW TABLES": (("tab_name",), (("transactions",),)),
        "DESCRIBE": _DriverError("Permission denied: principal svc lacks SELECT")})

    with pytest.raises(MetastoreAnswerRefused):
        adapter.describe_table(schema="banking", table="transactions")


def test_a_column_described_with_NO_TYPE_refuses_rather_than_inventing_one() -> None:
    adapter, _engine = _adapter({
        "SHOW TABLES": (("tab_name",), (("t",),)),
        "DESCRIBE": (("col_name", "data_type"), (("cif_id", ""),))})

    with pytest.raises(ValueError, match="no physical type"):
        adapter.describe_table(schema="banking", table="t")


def test_a_LISTED_table_that_describes_NOTHING_refuses() -> None:
    """``MetastoreInventoryAdapter`` already states the rule for a capture: a table that exists has
    columns, so an empty description is an answer that did not happen."""
    adapter, _engine = _adapter({
        "SHOW TABLES": (("tab_name",), (("t",),)), "DESCRIBE": (("col_name", "data_type"), ())})

    with pytest.raises(ValueError, match="no columns"):
        adapter.describe_table(schema="banking", table="t")


# ── question 3: may these roles read it ──────────────────────────────────────────────────────────

_GRANT_COLUMNS = ("database", "table", "partition", "column", "principal_name", "principal_type",
                  "privilege", "grant_option", "grant_time", "grantor")


def _grant(privilege: str, *, table: str = "transactions"):
    return (_GRANT_COLUMNS,
            (("banking", table, "", "", "feature_engineer", "ROLE", privilege, False, 0, "admin"),))


def test_a_SELECT_grant_is_readable() -> None:
    adapter, engine = _adapter({"SHOW GRANT": _grant("SELECT")})

    assert adapter.can_read(schema="banking", table="transactions",
                            roles=["feature_engineer"]) is True
    assert engine.statements == [
        "SHOW GRANT ROLE `feature_engineer` ON TABLE `banking`.`transactions`"]


def test_a_grant_that_is_not_a_READ_privilege_is_not_readable() -> None:
    """A role holding INSERT cannot read the table, and folding "has some grant" into "may read"
    would answer L1's third question with a different question's answer."""
    adapter, _engine = _adapter({"SHOW GRANT": _grant("INSERT")})

    assert adapter.can_read(schema="banking", table="transactions",
                            roles=["feature_engineer"]) is False


def test_the_privilege_is_read_by_COLUMN_NAME_not_by_POSITION() -> None:
    """Engines print different column sets. A positional read would answer with whatever column
    happened to sit there — here, the table name."""
    adapter, _engine = _adapter({"SHOW GRANT": (
        ("privilege", "principal_name"), (("SELECT", "feature_engineer"),))})

    assert adapter.can_read(schema="banking", table="select", roles=["feature_engineer"]) is True


def test_a_result_with_NO_privilege_column_refuses_rather_than_summarising_it_as_no() -> None:
    adapter, _engine = _adapter({"SHOW GRANT": (("something_else",), (("SELECT",),))})

    with pytest.raises(ValueError, match="privilege"):
        adapter.can_read(schema="banking", table="transactions", roles=["feature_engineer"])


def test_no_grants_at_all_is_a_real_NO() -> None:
    adapter, _engine = _adapter({"SHOW GRANT": (_GRANT_COLUMNS, ())})

    assert adapter.can_read(schema="banking", table="transactions", roles=["auditor"]) is False


def test_NO_ROLES_is_False_and_asks_the_engine_nothing() -> None:
    """A deployment that authorized nobody must not read as one that authorized everybody."""
    adapter, engine = _adapter({"SHOW GRANT": _grant("ALL")})

    assert adapter.can_read(schema="banking", table="transactions", roles=[]) is False
    assert engine.statements == []


def test_ANY_of_the_roles_holding_a_read_privilege_is_enough_and_stops_asking() -> None:
    adapter, engine = _adapter({"SHOW GRANT ROLE `auditor`": (_GRANT_COLUMNS, ()),
                                "SHOW GRANT ROLE `feature_engineer`": _grant("ALL")})

    assert adapter.can_read(schema="banking", table="transactions",
                            roles=["auditor", "feature_engineer", "steward"]) is True
    assert len(engine.statements) == 2, "it kept asking after an answer of YES"


def test_an_endpoint_with_NO_AUTHORIZATION_MODEL_answers_neither_TRUE_nor_FALSE() -> None:
    """The two lies available are both worse than telling the deployment its endpoint cannot answer
    L1's third question: ``True`` is the unconfigured-allowlist-reads-as-everything defect
    ``data_agent.connection`` refuses by name, and ``False`` is a denial nobody issued."""
    adapter, _engine = _adapter({"SHOW GRANT": _DriverError(
        "Error: SHOW GRANT is not supported in current authorizer")})

    with pytest.raises(MetastoreReadScopeUnanswerable) as raised:
        adapter.can_read(schema="banking", table="transactions", roles=["feature_engineer"])

    assert "SQL-standard authorization" in str(raised.value)


def test_SPARKS_OWN_REJECTION_of_SHOW_GRANT_is_the_typed_unanswerable_not_an_unknown() -> None:
    """The kind sandbox's endpoint is a Spark Thrift Server, and this is the message it ACTUALLY
    returns — Spark's grammar carries a rule named ``unsupportedHiveNativeCommands`` whose members
    include ``SHOW GRANT``, so it parses the statement only to reject it.

    Before this pattern existed the message classified as ``UNRECOGNISED`` and RAISED, which meant
    the one condition this module has a designed answer for arrived at the operator as an unknown
    fault. Pinned with the engine's verbatim wording, error class and all.
    """
    adapter, _engine = _adapter({"SHOW GRANT": _DriverError(
        "org.apache.spark.sql.catalyst.parser.ParseException: "
        "[_LEGACY_ERROR_TEMP_0035] Operation not allowed: SHOW GRANT.(line 1, pos 0)")})

    with pytest.raises(MetastoreReadScopeUnanswerable):
        adapter.can_read(schema="banking", table="transactions", roles=["feature_engineer"])


def test_the_SPARK_pattern_is_NARROW_and_does_not_swallow_other_refusals() -> None:
    """``Operation not allowed:`` is Spark's generic phrase — it prefixes TRUNCATE-on-external-table
    and ALTER TABLE SET SERDE refusals too. Matching it alone would report those as *the endpoint
    has no authorization model*, which is the over-broad-pattern failure the table's own ordering
    comment warns about. So the pattern includes ``show grant``, and a different refusal must NOT
    reach ``READ_SCOPE_UNANSWERABLE``.
    """
    adapter, _engine = _adapter({"SHOW PARTITIONS": _DriverError(
        "[_LEGACY_ERROR_TEMP_0035] Operation not allowed: TRUNCATE TABLE on external tables.")})

    with pytest.raises(MetastoreFaultError) as raised:
        adapter.list_partitions(schema="banking", table="transactions")

    assert raised.value.fault is MetastoreFault.UNRECOGNISED, (
        "an unrelated Spark refusal must stay UNRECOGNISED and raise, not be read as an answer "
        "about read scope")


def test_an_UNREACHABLE_read_scope_check_is_ClusterUnreachable() -> None:
    adapter, _engine = _adapter({"SHOW GRANT": _DriverError("Session is closed")})

    with pytest.raises(ClusterUnreachable):
        adapter.can_read(schema="banking", table="transactions", roles=["feature_engineer"])


# ── the classification table itself ──────────────────────────────────────────────────────────────


def test_a_DENIAL_is_classified_before_an_ABSENCE() -> None:
    """Engines phrase a denial as an absence ("Table not found" for a table the caller may not
    see). Reading that as an absence would file a governed COLUMN_ABSENT about a table that is
    there, so the permission phrasings are tested first — which this pins as ORDER, not luck."""
    faults = [fault for fault, _patterns in FAULT_PATTERNS]
    assert faults.index(MetastoreFault.PERMISSION_DENIED) < faults.index(
        MetastoreFault.TABLE_UNKNOWN)
    assert faults.index(MetastoreFault.READ_SCOPE_UNANSWERABLE) < faults.index(
        MetastoreFault.PERMISSION_DENIED)

    adapter, _engine = _adapter({"SHOW PARTITIONS": _DriverError(
        "Permission denied: table not found for user [svc]")})
    with pytest.raises(MetastoreAnswerRefused):
        adapter.list_partitions(schema="banking", table="transactions")


def test_UNRECOGNISED_is_a_MEMBER_of_the_fault_vocabulary_and_not_a_gap() -> None:
    """It is the honest statement that this module does not know which claim to make, which is what
    lets the classification fail closed instead of defaulting to a convenient neighbour."""
    assert MetastoreFault.UNRECOGNISED not in {fault for fault, _p in FAULT_PATTERNS}


def test_the_adapter_satisfies_the_MetastoreMetadata_seam_exactly() -> None:
    """Three questions and no fourth: a seam that could fetch a row would be a seam through which
    the control plane could read feature data."""
    from featuregen.materialize.validation import MetastoreMetadata

    seam = {name for name in dir(MetastoreMetadata) if not name.startswith("_")}
    assert seam == {"list_partitions", "describe_table", "can_read"}
    adapter, _engine = _adapter()
    assert all(callable(getattr(adapter, name)) for name in seam)
