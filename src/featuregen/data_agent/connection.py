"""Governed source connections.

A connection says **which engine, reachable how, as whom, and over which schemas**. It never says
*with what credential*: it holds a `secret_ref` naming where the credential lives, and the credential
is resolved at the point of use. A field that could hold a secret eventually holds one, and then it
is in a log, a JSON column, an error message or an LLM prompt.

It also owns the **allowlist**, which is the boundary a binding cannot cross. `observation.py`
validates a plan against its binding; only the connection knows which schemas were approved for it,
so `authorize_binding` is where those two halves meet. This mirrors the shipped
`profiler_command` rule that a caller's self-attested allowlist cannot authorize a scan — an
unconfigured allowlist fails closed rather than reading as "everything".

No driver is imported here. The engine client lives behind a factory so the control-plane package
keeps no data-plane runtime dependency, and so an ODS engine is an addition rather than a rewrite.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

#: Refusal codes — closed vocabulary.
SECRET_IN_REFERENCE = "CONNECTION_SECRET_IN_REFERENCE"
NO_SECRET_REFERENCE = "CONNECTION_NO_SECRET_REFERENCE"
UNSUPPORTED_KIND = "CONNECTION_UNSUPPORTED_KIND"
NO_PRINCIPAL = "CONNECTION_NO_EXECUTION_PRINCIPAL"
DRIVER_MISSING = "CONNECTION_DRIVER_NOT_INSTALLED"
NOT_ACTIVE = "CONNECTION_NOT_ACTIVE"
SCHEMA_NOT_ALLOWED = "CONNECTION_SCHEMA_NOT_IN_ALLOWLIST"
WRONG_CONNECTION = "CONNECTION_BINDING_MISMATCH"

#: Engines Release 1 supports. `postgres` is both the local validation path and the starting point
#: for an ODS adapter; other engines are added deliberately, not discovered inside a driver.
SUPPORTED_KINDS = ("hive", "postgres")

#: Shapes that are a secret rather than a reference TO one. Not exhaustive, and not meant to be —
#: it is a tripwire for the obvious mistakes, and the contract is the docstring above.
_SECRET_SHAPES = (
    re.compile(r"password\s*=", re.I),
    re.compile(r"^\s*-----BEGIN ", re.I),
    re.compile(r"://[^/\s]*:[^@/\s]+@"),          # user:password@host in any URL
    re.compile(r"^ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\."),   # a JWT
)


class ConnectionError_(ValueError):
    """A connection or authorization that cannot be formed. Named with a trailing underscore to
    avoid shadowing the builtin."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class DataSourceConnectionV1:
    """How an approved reader reaches one engine."""

    connection_id: str
    environment_id: str
    kind: str
    host: str
    port: int
    auth_mechanism: str
    #: WHERE the credential lives — e.g. `vault://...`. Never the credential.
    secret_ref: str
    #: The account the read runs as. Part of the evidence, because it decides what the read could
    #: see: two profiles of one table under different principals are not interchangeable.
    execution_principal: str
    #: Server-owned. Empty means nothing is authorized (fail closed), never "everything".
    allowed_schemas: frozenset[str] = frozenset()
    active: bool = True

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_KINDS:
            raise ConnectionError_(
                UNSUPPORTED_KIND,
                f"kind {self.kind!r} is not one of {SUPPORTED_KINDS}; an unknown engine must fail "
                "here, not inside a driver")
        if not (self.secret_ref or "").strip():
            raise ConnectionError_(NO_SECRET_REFERENCE, "a connection needs a secret REFERENCE")
        for shape in _SECRET_SHAPES:
            if shape.search(self.secret_ref):
                raise ConnectionError_(
                    SECRET_IN_REFERENCE,
                    "secret_ref looks like a credential rather than a reference to one")
        if not (self.execution_principal or "").strip():
            raise ConnectionError_(
                NO_PRINCIPAL, "a connection must record the principal its reads run as")

    def permits_schema(self, schema: str) -> bool:
        return (schema or "").strip().lower() in {s.strip().lower() for s in self.allowed_schemas}


def authorize_binding(connection: DataSourceConnectionV1,
                      binding) -> DataSourceConnectionV1:
    """Check a binding against its connection. Returns the connection so callers can chain.

    This is the check `observation.py` structurally cannot make: a binding validates its own shape,
    but only the connection knows which schemas were approved for it.
    """
    if binding.connection_id != connection.connection_id:
        raise ConnectionError_(
            WRONG_CONNECTION,
            f"binding names connection {binding.connection_id!r}, not "
            f"{connection.connection_id!r}")
    if not connection.active:
        raise ConnectionError_(
            NOT_ACTIVE, f"connection {connection.connection_id!r} is not active")
    schema = binding.identity.schema
    if not connection.permits_schema(schema):
        allowed = sorted(connection.allowed_schemas) or "[] (nothing is authorized)"
        raise ConnectionError_(
            SCHEMA_NOT_ALLOWED,
            f"schema {schema!r} is not on connection {connection.connection_id!r}'s allowlist "
            f"{allowed}")
    return connection


#: Which package provides each engine's DB-API driver. Named so a missing dependency is a typed
#: refusal telling an operator what to install, rather than an ImportError from inside a driver.
#:
#: Lazily imported: the control-plane package must carry no data-plane runtime dependency, and a
#: deployment that only reaches PostgreSQL should not need a Hive client installed.
_DRIVERS: dict[str, tuple[str, str]] = {
    "postgres": ("psycopg", "psycopg"),
    # PyHive is the common HiveServer2 client. impyla or JayDeBeApi work equally well through the
    # `connect=` seam below — all three implement DB-API 2.0, which is the whole point of routing
    # the executor through a cursor.
    "hive": ("pyhive.hive", "PyHive"),
}


def open_connection(connection: DataSourceConnectionV1, *,
                    secret_resolver: Callable[[str], str],
                    connect: Callable[..., object] | None = None):
    """Open a live connection for one governed spec.

    The credential is fetched from `secret_resolver` at the point of use and is never stored on the
    spec, returned on the connection object, or included in a refusal message.

    `connect` lets a caller supply the callable — used by tests, and the seam a future engine
    adapter plugs into without touching the driver table above.
    """
    if not connection.active:
        raise ConnectionError_(
            NOT_ACTIVE, f"connection {connection.connection_id!r} is not active")

    secret = secret_resolver(connection.secret_ref)

    if connect is None:
        module_name, package = _DRIVERS[connection.kind]
        try:
            module = __import__(module_name, fromlist=["connect"])
        except ImportError as exc:
            # Deliberately does not echo the secret or the exception's own text, which on some
            # drivers includes the connection string.
            raise ConnectionError_(
                DRIVER_MISSING,
                f"no driver for kind {connection.kind!r}: install {package} "
                f"(import of {module_name!r} failed)") from None
        connect = module.connect

    return connect(host=connection.host, port=connection.port,
                   username=connection.execution_principal, password=secret,
                   auth=connection.auth_mechanism)
