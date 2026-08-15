"""Spec §11.2 / §3.3 — the DEPLOYMENT's metastore adapter: L1's three questions, over SQL.

**Why this may live in ``src/`` at all, when ``probe.PublicationTarget`` may not.** The package law
is that ``src/`` never imports ``pyspark`` — the control plane does not acquire the artifact's
engines. Nothing here does: the transport is a DB-API 2.0 cursor, the driver is imported lazily by
name at connect time, and the default suite injects a fake ``connect`` and never opens a socket. So
this is an implementation and not a Protocol for the same reason
:class:`~featuregen.materialize.submit.LocalClusterSubmitter` is one — its imports are the standard
library, and what it needs from the environment is an address rather than a JVM.

**ONE TRANSPORT, NOT TWO.** :class:`MetastoreSession` owns the single connection, and both
adapters — :class:`SqlMetastoreAdapter` (L1's three questions) and
:class:`~featuregen.materialize.publish_sql.SqlPublicationSwap` (G-3's pointer switch) — are
constructed from the same session. The alternative considered and rejected was reading the Hive
metastore's own backing database directly (on the kind sandbox it is a PostgreSQL schema, which the
worker pod can already reach): it would answer the *storage* of the metastore rather than the
*metastore*, it cannot answer read scope at all, and a pointer switch written as hand-rolled
``TBLS``/``SDS`` UPDATEs is exactly the "separate metadata write and pointer flip" §10.3's probe
would then be evidence about neither of. The engine's own SQL endpoint answers all three questions
and performs the swap as the engine's own atomic operation — which is the act the probe attests.
This is also the vehicle :func:`featuregen.data_agent.connection.open_connection` already uses to
reach a Hive-dialect engine (DB-API 2.0, lazily imported driver, injectable ``connect``); that
object is not reused because it is a GOVERNED SOURCE connection carrying a schema allowlist and a
secret reference, and the metastore endpoint is a deployment fact rather than a governed source.

**EVERY AMBIGUITY IS A TYPED OUTCOME, and an empty list is never one of them.** ``runprep`` already
states the hazard in its own words — *"'this table has no partitions' and 'the metastore did not
answer' are the same empty list"* — so this module never returns one unless the engine positively
said so. Every driver error is classified against a closed, documented table into
:class:`MetastoreFault`; an unrecognised message is a fault in its own right and **raises**, because
guessing which of "unreachable", "absent" and "denied" a strange message meant is how a wrong world
gets validated. The four outcomes and where each lands:

* ``UNREACHABLE`` → :class:`~featuregen.materialize.validation.ClusterUnreachable`, which is the
  ONE exception L1 converts into ``status="error"`` with zero findings.
* ``TABLE_UNKNOWN`` → :class:`~featuregen.materialize.validation.MetastoreTableUnknown`, which L1
  reports as the ``COLUMN_ABSENT`` "the table does not exist" finding it already has for a
  ``describe_table`` that answered ``None``.
* ``PERMISSION_DENIED`` on a metadata question the seam does not have a verdict slot for →
  :class:`MetastoreAnswerRefused`. It propagates: L1 asked ``can_read`` FIRST and was told yes, so a
  listing that is then denied is the environment contradicting itself, not a finding about a
  feature.
* ``READ_SCOPE_UNANSWERABLE`` → :class:`MetastoreReadScopeUnanswerable`. An engine with no
  authorization model cannot say whether a role may read a table, and the two lies available —
  ``True`` ("everything is readable", the unconfigured-allowlist-means-everything defect
  ``data_agent`` refuses by name) and ``False`` (a denial nobody issued) — are both worse than a
  deployment being told its endpoint cannot answer L1's third question.

**Identifiers are validated, never escaped.** No DDL or ``SHOW`` statement can take an identifier as
a bound parameter, so every identifier this module interpolates is checked against a strict pattern
first and refused otherwise. Quoting a hostile identifier would be a second-best defence; refusing
one is the only one that does not depend on getting the quoting right.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import unquote

from featuregen.materialize.validation import ClusterUnreachable, MetastoreTableUnknown

__all__ = [
    "FAULT_PATTERNS",
    "METASTORE_DRIVERS",
    "MetastoreAnswerRefused",
    "MetastoreEndpoint",
    "MetastoreFault",
    "MetastoreFaultError",
    "MetastoreReadScopeUnanswerable",
    "MetastoreSession",
    "SqlMetastoreAdapter",
    "quoted_identifier",
]


# ── the transport ────────────────────────────────────────────────────────────────────────────────


class SqlCursor(Protocol):
    """The DB-API 2.0 cursor surface this module uses, and nothing wider.

    ``description`` is part of it deliberately: :meth:`SqlMetastoreAdapter.can_read` reads the
    ``privilege`` column of a ``SHOW GRANT`` result BY NAME, because its position differs between
    engines and a positional read would silently answer with whatever column happened to be there.
    """

    @property
    def description(self) -> Sequence[Sequence[Any]] | None: ...

    def execute(self, operation: str) -> Any: ...

    def fetchall(self) -> Sequence[Sequence[Any]]: ...

    def close(self) -> None: ...


class SqlConnection(Protocol):
    """The DB-API 2.0 connection surface. No ``commit``: everything here is DDL or metadata."""

    def cursor(self) -> SqlCursor: ...

    def close(self) -> None: ...


#: Which package provides each engine's DB-API driver, and what to tell an operator who has not
#: installed it. A CLOSED table keyed by engine name rather than a dotted path read from the
#: environment: a deployment that could name any importable module could name any importable
#: module, and this is the process that talks to the governed cluster.
METASTORE_DRIVERS: dict[str, tuple[str, str]] = {
    # HiveServer2 / the Spark Thrift Server both speak this protocol; PyHive is the common client,
    # and impyla or JayDeBeApi work identically through the `connect` seam below.
    "hive": ("pyhive.hive", "PyHive"),
}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
#: A role name may carry a dash or a dot (``featuregen-reader``, ``svc.featuregen``) where a SQL
#: identifier may not; it is still checked, because it reaches a ``SHOW GRANT`` statement.
_ROLE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


def quoted_identifier(value: str, *, field_name: str) -> str:
    """One identifier, validated then back-quoted — or ``ValueError``.

    Validated rather than escaped: a ``SHOW`` or ``ALTER`` statement cannot bind an identifier as a
    parameter, so the only defence that does not rest on getting the quoting right is refusing
    anything that is not plainly an identifier.
    """
    if not isinstance(value, str) or not _IDENTIFIER.match(value.strip()):
        raise ValueError(
            f"{field_name} is not a plain identifier ({value!r}): it is interpolated into a "
            f"statement the engine runs, no SQL dialect can bind an identifier as a parameter, and "
            f"quoting a hostile one would be a defence that depends on the quoting being right")
    return f"`{value.strip()}`"


class MetastoreFault(StrEnum):
    """What a driver error MEANT — a closed vocabulary, and ``UNRECOGNISED`` is a member.

    Every other member is a claim about the environment. ``UNRECOGNISED`` is the honest statement
    that this module does not know which claim to make, and it is why the classification can fail
    closed instead of defaulting to whichever neighbour is most convenient.
    """

    UNREACHABLE = "unreachable"
    TABLE_UNKNOWN = "table_unknown"
    NOT_PARTITIONED = "not_partitioned"
    PERMISSION_DENIED = "permission_denied"
    READ_SCOPE_UNANSWERABLE = "read_scope_unanswerable"
    UNRECOGNISED = "unrecognised"


#: The classification table, IN ORDER — first match wins, matched case-insensitively against
#: ``str(error)``. Ordered because engines phrase a denial as an absence ("Table not found" for a
#: table the caller may not see), so the permission and read-scope phrasings are tested before the
#: absence ones; reading a denial as an absence would report a governed ``COLUMN_ABSENT`` about a
#: table that is there.
#:
#: This is a table of MESSAGES, which is brittle, and the brittleness is answered by
#: ``UNRECOGNISED`` raising rather than by pretending the table is complete. A message this does not
#: know names itself in the refusal, so the fix is one line here and it is visible.
FAULT_PATTERNS: tuple[tuple[MetastoreFault, tuple[str, ...]], ...] = (
    (MetastoreFault.UNREACHABLE, (
        "could not open client transport", "could not connect", "connection refused",
        "connection reset", "broken pipe", "no route to host", "name or service not known",
        "temporary failure in name resolution", "timed out", "timeout", "session is closed",
        "socket is closed", "transport is closed", "connection is closed", "network is unreachable",
        "gssapi", "kerberos", "authentication failed")),
    (MetastoreFault.READ_SCOPE_UNANSWERABLE, (
        "show grant is not supported", "authorization is not enabled",
        "current authorizer does not support", "not supported in current authorizer",
        "unsupported command", "grant is not supported")),
    (MetastoreFault.PERMISSION_DENIED, (
        "permission denied", "not authorized", "no privilege", "does not have privilege",
        "insufficient privileges", "access denied", "hiveaccesscontrolexception")),
    (MetastoreFault.NOT_PARTITIONED, (
        "is not a partitioned table", "not a partitioned table", "table is not partitioned")),
    # Deliberately broad, and LAST. Engines interpolate the object's name into the middle of the
    # phrase ("Database 'risk' does not exist"), so the patterns are the parts that do not move.
    # Breadth is safe only because every narrower fault above has already been tested: a denial
    # phrased as an absence was classified two entries ago.
    (MetastoreFault.TABLE_UNKNOWN, (
        "does not exist", "not found", "no such table", "no such database", "unknown table",
        "unknown database")),
)


class MetastoreFaultError(RuntimeError):
    """A classified failure of ONE statement, carrying the fault and the statement that produced it.

    The driver's own exception is chained rather than reformatted: an operator needs the engine's
    words, and this module needs a type it can route on.
    """

    def __init__(self, fault: MetastoreFault, statement: str, detail: str) -> None:
        super().__init__(f"{fault.value}: {statement} — {detail}")
        self.fault = fault
        self.statement = statement


class MetastoreAnswerRefused(Exception):
    """The environment refused a metadata question the seam has no verdict slot for.

    Not a finding and not ``ClusterUnreachable``: L1 asks ``can_read`` first, so a listing that is
    then denied is the environment contradicting its own answer, and turning that into a
    ``PARTITION_ABSENT`` would report a governed fact about a table nothing observed.
    """


class MetastoreReadScopeUnanswerable(Exception):
    """The endpoint has no authorization model, so *may these roles read it* has no answer here.

    Deliberately not a ``bool``. ``True`` would be the unconfigured-allowlist-reads-as-everything
    defect ``data_agent.connection`` refuses by name; ``False`` would be a denial nobody issued and
    would fail every L1 in the deployment with a governed ``READ_DENIED`` about nothing.
    """


def _classify(error: BaseException) -> MetastoreFault:
    text = str(error).lower()
    for fault, patterns in FAULT_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return fault
    return MetastoreFault.UNRECOGNISED


@dataclass(frozen=True, slots=True)
class MetastoreEndpoint:
    """WHERE the engine's SQL endpoint is, and AS WHOM this process talks to it.

    ``principal`` is evidence rather than decoration, exactly as
    ``data_agent.DataSourceConnectionV1.execution_principal`` is: two metadata reads under two
    principals are not interchangeable, and the swap is performed as this one. No field defaults —
    a port or a mechanism nobody stated is a deployment nobody decided.
    """

    engine: str
    host: str
    port: int
    auth_mechanism: str
    principal: str

    def __post_init__(self) -> None:
        if self.engine not in METASTORE_DRIVERS:
            raise ValueError(
                f"engine {self.engine!r} is not one of {sorted(METASTORE_DRIVERS)}: an unknown "
                f"engine must fail here rather than inside a driver")
        for name, value in (("host", self.host), ("auth_mechanism", self.auth_mechanism),
                            ("principal", self.principal)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"the metastore endpoint's {name} is blank ({value!r}): an endpoint nobody "
                    f"fully stated is not a posture, it is half a configuration")
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 0 < self.port < 65536:
            raise ValueError(
                f"the metastore endpoint's port is {self.port!r}: a port is a whole number in "
                f"1..65535, and there is no default worth guessing")


def _open(endpoint: MetastoreEndpoint) -> SqlConnection:
    """Open the real driver connection — the ONLY place a driver is imported.

    Lazily, and by the closed table's name, so a deployment that has not installed the client is a
    typed refusal naming the package rather than an ``ImportError`` out of the middle of a run.
    """
    module_name, package = METASTORE_DRIVERS[endpoint.engine]
    try:
        module = __import__(module_name, fromlist=["connect"])
    except ImportError:
        raise ValueError(
            f"no driver for metastore engine {endpoint.engine!r}: install {package} (import of "
            f"{module_name!r} failed). The control plane carries no engine client as a hard "
            f"dependency, so this is a deployment step rather than a defect") from None
    connection: SqlConnection = module.connect(
        host=endpoint.host, port=endpoint.port, username=endpoint.principal,
        auth=endpoint.auth_mechanism.strip().upper())
    return connection


@dataclass(slots=True)
class MetastoreSession:
    """ONE DB-API connection to the engine, opened on first use and shared by both adapters.

    ``connect`` is the seam. Injected, it is how the default suite proves every outcome of this
    module without a metastore, a JVM or a socket; omitted, it opens the real driver connection for
    :attr:`endpoint`. That is the same shape ``data_agent.connection.open_connection`` uses, and it
    is what keeps *one transport* a property rather than a convention: a deployment builds one of
    these and hands it to both adapters, so the questions L1 asks and the swap G-3 performs cannot
    be answered by two different clients seeing two different worlds.
    """

    endpoint: MetastoreEndpoint
    connect: Callable[[MetastoreEndpoint], SqlConnection] = _open
    _connection: SqlConnection | None = field(default=None, init=False, repr=False)

    def _live(self) -> SqlConnection:
        if self._connection is None:
            try:
                self._connection = self.connect(self.endpoint)
            except ValueError:
                raise
            except Exception as error:                 # the driver's own connect failure
                raise ClusterUnreachable(
                    f"the metastore endpoint {self.endpoint.host}:{self.endpoint.port} did not "
                    f"accept a connection ({type(error).__name__}: {error})") from error
        return self._connection

    def table(self, statement: str) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
        """Run one statement and return ``(column names, rows)`` — the only way rows are read.

        Column names come from the cursor's ``description`` so a caller can read a result column BY
        NAME. They are ``()`` when the driver reports none, which is a legitimate answer for DDL and
        an unusable one for a caller that needs a name — the caller decides, because only it knows
        which.

        Raises:
            ClusterUnreachable: the endpoint could not be reached or the session was lost.
            MetastoreFaultError: any other classified failure, INCLUDING ``UNRECOGNISED``.
        """
        cursor = self._live().cursor()
        try:
            try:
                cursor.execute(statement)
                rows = tuple(tuple(row) for row in cursor.fetchall())
            except (ClusterUnreachable, MetastoreFaultError):
                raise
            except Exception as error:
                fault = _classify(error)
                if fault is MetastoreFault.UNREACHABLE:
                    raise ClusterUnreachable(
                        f"the metastore did not answer {statement!r} "
                        f"({type(error).__name__}: {error})") from error
                raise MetastoreFaultError(
                    fault, statement, f"{type(error).__name__}: {error}") from error
            description = cursor.description
            names = tuple(str(column[0]) for column in description) if description else ()
        finally:
            try:
                cursor.close()
            except Exception:                          # a cursor that cannot be closed is not a
                pass                                   # verdict about anything this module asked
        return names, rows

    def statement(self, statement: str) -> None:
        """Run one statement for its EFFECT. Same classification, no rows."""
        self.table(statement)

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:                          # closing is housekeeping, never a verdict
                pass


# ── L1's three questions ─────────────────────────────────────────────────────────────────────────

#: The privileges that make a table readable. ``SELECT`` and ``ALL`` only: a role holding
#: ``INSERT`` on a table cannot read it, and folding "has some grant" into "may read" would answer
#: L1's third question with a different question's answer.
_READ_PRIVILEGES = frozenset({"select", "all"})

#: The result column ``can_read`` reads by NAME. Hive prints ten columns and the privilege is the
#: seventh; Spark prints fewer; a positional read would answer with whatever column was there.
_PRIVILEGE_COLUMN = "privilege"


@dataclass(frozen=True, slots=True)
class SqlMetastoreAdapter:
    """:class:`~featuregen.materialize.validation.MetastoreMetadata`, over one SQL session.

    Three questions and no fourth: nothing here returns a row of feature data, and the statements
    are the engine's own metadata commands rather than queries over the tables themselves.
    """

    session: MetastoreSession

    # ── which partitions does this table have (§3.3 and §11.2 both ask it) ───────────────────────

    def list_partitions(self, *, schema: str, table: str
                        ) -> tuple[tuple[tuple[str, str], ...], ...]:
        """Every partition of one physical table, in the metastore's own column order.

        Returns ``()`` ONLY when the engine positively answered that there are none — an empty
        listing of a partitioned table, or a table the engine says is not partitioned at all.
        ``runprep`` refuses a ``FULL_SCAN`` on an empty listing precisely because it cannot tell
        those apart from silence, so silence must never arrive here as an empty tuple.

        Raises:
            ClusterUnreachable: the endpoint could not be asked.
            MetastoreTableUnknown: the engine says this table does not exist.
            MetastoreAnswerRefused: the engine refused the listing.
            MetastoreFaultError: an unrecognised failure — never folded into an empty listing.
            ValueError: the engine printed a partition this module cannot parse.
        """
        statement = (f"SHOW PARTITIONS {quoted_identifier(schema, field_name='schema')}."
                     f"{quoted_identifier(table, field_name='table')}")
        try:
            _names, rows = self.session.table(statement)
        except MetastoreFaultError as fault:
            if fault.fault is MetastoreFault.NOT_PARTITIONED:
                # The engine ANSWERED: this table has no partitions to list. That is the empty
                # sequence the seam documents, and it is the one empty this module may produce.
                return ()
            raise self._routed(fault, schema, table) from fault
        return tuple(_partition_of(str(row[0]), f"{schema}.{table}") for row in rows if row)

    # ── which columns and physical types does it have ───────────────────────────────────────────

    def describe_table(self, *, schema: str, table: str) -> tuple[tuple[str, str], ...] | None:
        """Ordered ``(column, physical type)``, DATA and partition columns together — or ``None``.

        ``None`` is the seam's own word for *the table does not exist*, and it is reached by ASKING
        (``SHOW TABLES … LIKE``) rather than by reading an absence out of a failure message. Data
        and partition columns come back together because L1's read set does not distinguish them:
        a read of a partition column would otherwise find no observed type at all.
        """
        quoted_schema = quoted_identifier(schema, field_name="schema")
        quoted_table = quoted_identifier(table, field_name="table")
        try:
            _names, listed = self.session.table(
                f"SHOW TABLES IN {quoted_schema} LIKE '{table.strip()}'")
        except MetastoreFaultError as fault:
            if fault.fault is MetastoreFault.TABLE_UNKNOWN:
                return None                            # no such schema ⇒ no such table
            raise self._routed(fault, schema, table) from fault
        if not listed:
            return None

        try:
            names, rows = self.session.table(f"DESCRIBE {quoted_schema}.{quoted_table}")
        except MetastoreFaultError as fault:
            if fault.fault is MetastoreFault.TABLE_UNKNOWN:
                return None                            # dropped between the two statements
            raise self._routed(fault, schema, table) from fault
        described = _described_columns(rows, f"{schema}.{table}")
        if not described:
            raise ValueError(
                f"the engine describes no columns for {schema}.{table} while listing it as a "
                f"table: a table that exists has columns, so an empty description is an answer "
                f"that did not happen (the rule `MetastoreInventoryAdapter._layout` already "
                f"applies to a capture)")
        del names
        return described

    # ── may these roles read it ─────────────────────────────────────────────────────────────────

    def can_read(self, *, schema: str, table: str, roles: Sequence[str]) -> bool:
        """Whether any of ``roles`` holds a READ privilege on this table — a metadata question.

        ``True`` requires a grant the engine printed. An empty ``roles`` is ``False`` without asking
        anything: no role can hold a privilege, and a deployment that authorized nobody must not
        read as one that authorized everybody.

        Raises:
            MetastoreReadScopeUnanswerable: the endpoint has no authorization model to ask. Neither
                boolean is available then, and both are lies — see the class docstring.
            ClusterUnreachable / MetastoreTableUnknown / MetastoreAnswerRefused: as elsewhere.
        """
        quoted = (f"{quoted_identifier(schema, field_name='schema')}."
                  f"{quoted_identifier(table, field_name='table')}")
        for role in roles:
            if not isinstance(role, str) or not _ROLE_NAME.match(role.strip()):
                raise ValueError(
                    f"role {role!r} is not a plain role name: it is interpolated into a SHOW GRANT "
                    f"statement, and this module refuses an identifier it cannot validate rather "
                    f"than quoting one it cannot vouch for")
            statement = f"SHOW GRANT ROLE `{role.strip()}` ON TABLE {quoted}"
            try:
                names, rows = self.session.table(statement)
            except MetastoreFaultError as fault:
                if fault.fault is MetastoreFault.READ_SCOPE_UNANSWERABLE:
                    raise MetastoreReadScopeUnanswerable(
                        f"the endpoint answered {statement!r} with "
                        f"{fault}: it has no authorization model, so whether {role!r} may read "
                        f"{schema}.{table} has no answer here. Configure SQL-standard authorization "
                        f"on the endpoint, or do not configure this adapter — L1's third question "
                        f"is not one a deployment may answer by assumption") from fault
                raise self._routed(fault, schema, table) from fault
            if _grants_read(names, rows, statement):
                return True
        return False

    # ── routing ─────────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _routed(fault: MetastoreFaultError, schema: str, table: str) -> Exception:
        """The typed outcome a classified fault becomes. Every branch is a DIFFERENT exception."""
        if fault.fault is MetastoreFault.TABLE_UNKNOWN:
            return MetastoreTableUnknown(
                f"the engine says {schema}.{table} does not exist ({fault})")
        if fault.fault in (MetastoreFault.PERMISSION_DENIED,
                           MetastoreFault.READ_SCOPE_UNANSWERABLE):
            return MetastoreAnswerRefused(
                f"the engine refused a metadata question about {schema}.{table} ({fault}): L1 was "
                f"told these roles MAY read it, so a refusal here is the environment contradicting "
                f"its own answer rather than a finding about a feature")
        return fault                                   # UNRECOGNISED / NOT_PARTITIONED: unrouted


def _partition_of(printed: str, where: str) -> tuple[tuple[str, str], ...]:
    """``"load_dt=2026-08-14/branch=BLR"`` → ``(("load_dt", "2026-08-14"), ("branch", "BLR"))``.

    Values are percent-DECODED, which is the exact inverse of what the engine did: Hive escapes the
    characters that would make this string ambiguous — ``/``, ``=`` and ``%`` itself among them —
    so decoding cannot corrupt a value that contains one, and NOT decoding would compare an escaped
    value against a declaration that never was.

    A row this cannot parse RAISES. Dropping it would shrink the listing, and a partition missing
    from a listing reads downstream as a partition missing from the table.
    """
    parts = [segment for segment in printed.strip().split("/") if segment]
    if not parts:
        raise ValueError(
            f"the engine printed an empty partition for {where}: a listing entry names a "
            f"partition, and an entry that names nothing cannot be compared with one that was "
            f"resolved")
    columns: list[tuple[str, str]] = []
    for segment in parts:
        name, separator, value = segment.partition("=")
        if not separator or not name.strip():
            raise ValueError(
                f"the engine printed the partition {printed!r} for {where}, whose segment "
                f"{segment!r} is not `column=value`: this module refuses a listing it cannot read "
                f"rather than returning the half of it that parsed")
        columns.append((unquote(name.strip()), unquote(value)))
    return tuple(columns)


def _described_columns(rows: Sequence[Sequence[Any]], where: str) -> tuple[tuple[str, str], ...]:
    """``DESCRIBE``'s rows as ordered ``(name, physical type)``, first occurrence winning.

    ``DESCRIBE`` prints the data columns, then a blank separator, then ``# Partition Information``
    and the partition columns again. Comment and blank rows are skipped and the partition columns
    are kept — L1's read set does not distinguish the two — while a name already seen is not
    repeated, because a column listed twice is one column described twice.
    """
    described: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        name = "" if not row or row[0] is None else str(row[0]).strip()
        if not name or name.startswith("#"):
            continue
        if name.lower().startswith("detailed table information"):
            break
        if len(row) < 2 or row[1] is None or not str(row[1]).strip():
            raise ValueError(
                f"the engine described column {name!r} of {where} with no physical type: a type "
                f"this module invented would be compared against the inventory's declaration and "
                f"could only ever agree or disagree by accident")
        folded = name.lower()
        if folded in seen:
            continue
        seen.add(folded)
        described.append((name, str(row[1]).strip()))
    return tuple(described)


def _grants_read(names: Sequence[str], rows: Sequence[Sequence[Any]], statement: str) -> bool:
    """Whether a ``SHOW GRANT`` result carries a READ privilege, read by COLUMN NAME.

    No rows is ``False`` — the engine answered, and it named no grant. Rows with no locatable
    ``privilege`` column RAISE: a result whose shape this module cannot read is not a result it may
    summarise as "no", and guessing a position is how a grant on one column gets read out of
    another.
    """
    if not rows:
        return False
    folded = [str(name).strip().lower() for name in names]
    if _PRIVILEGE_COLUMN not in folded:
        raise ValueError(
            f"{statement!r} returned {len(rows)} row(s) whose columns are {list(names)}: no "
            f"{_PRIVILEGE_COLUMN!r} column, so which of them states the privilege is exactly what "
            f"is unknown, and reading a position would answer with whatever column was there")
    index = folded.index(_PRIVILEGE_COLUMN)
    for row in rows:
        if len(row) > index and str(row[index]).strip().lower() in _READ_PRIVILEGES:
            return True
    return False
