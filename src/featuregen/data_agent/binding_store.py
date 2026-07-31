"""Persist and resolve the address of a catalog source — the configuration nothing could supply.

`PhysicalDatasetBindingV1` was constructed nowhere in `src/`, only in tests, because the information
to build one existed nowhere: the catalog records a schema and no database, and there was no
connection registry. So a plan could be compiled and never addressed to a table, and
`ExecutionInputs` could not be assembled by any caller.

**Resolution goes through `authorize_binding`, always.** A stored binding is a record, not a
permission: only the CONNECTION knows which schemas were approved, and a binding that could authorize
itself would let anyone who can write a row widen what may be read. So every resolve re-checks the
allowlist and the active flag rather than trusting what was persisted — the row could have been
written before a schema was revoked.

**A missing binding is an ABSENCE, not an error.** Most deployments will have none, and the honest
response is that the plan cannot execute yet — which the preview already reports as
`EXECUTION_INPUTS_ABSENT`. Raising here would turn "not configured" into a fault.
"""

from __future__ import annotations

from featuregen.config import get_settings
from featuregen.data_agent.connection import (
    ConnectionError_,
    DataSourceConnectionV1,
    authorize_binding,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1


def record_connection(conn, connection: DataSourceConnectionV1, *, tier: str = "",
                      database_name: str = "") -> str:
    """Upsert one access grant. The credential is NOT stored — `secret_ref` is a pointer.

    `tier` and `database_name` live on the ROW rather than on DataSourceConnectionV1: the
    contract is the data-plane connection the executor opens, and routing is this layer's
    concern. Widening the dataclass would push a catalog-routing idea into a module whose job
    is opening a cursor.
    """
    conn.execute(
        "INSERT INTO data_source_connection (connection_id, environment_id, kind, host, port, "
        "  auth_mechanism, secret_ref, execution_principal, allowed_schemas, active, "
        "  tier, database_name) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (connection_id) DO UPDATE SET "
        "  environment_id = EXCLUDED.environment_id, kind = EXCLUDED.kind, host = EXCLUDED.host, "
        "  port = EXCLUDED.port, auth_mechanism = EXCLUDED.auth_mechanism, "
        "  secret_ref = EXCLUDED.secret_ref, execution_principal = EXCLUDED.execution_principal, "
        "  allowed_schemas = EXCLUDED.allowed_schemas, active = EXCLUDED.active, "
        "  tier = EXCLUDED.tier, database_name = EXCLUDED.database_name",
        (connection.connection_id, connection.environment_id, connection.kind, connection.host,
         connection.port, connection.auth_mechanism, connection.secret_ref,
         connection.execution_principal, sorted(connection.allowed_schemas),
         connection.active, tier, database_name))
    return connection.connection_id


def record_binding(conn, binding: PhysicalDatasetBindingV1) -> str:
    """Upsert one address. Keyed on (source, schema, table): a second binding for one physical table
    would let two callers reach it under different principals, with read order deciding which."""
    identity = binding.identity
    conn.execute(
        "INSERT INTO physical_dataset_binding (binding_id, catalog_source, catalog_logical_ref, "
        "  connection_id, database_name, schema_name, table_name, object_kind) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (catalog_source, lower(schema_name), lower(table_name)) DO UPDATE SET "
        "  binding_id = EXCLUDED.binding_id, catalog_logical_ref = EXCLUDED.catalog_logical_ref, "
        "  connection_id = EXCLUDED.connection_id, database_name = EXCLUDED.database_name, "
        "  object_kind = EXCLUDED.object_kind",
        (binding.binding_id, identity.catalog_source, binding.catalog_logical_ref,
         binding.connection_id, identity.database, identity.schema, identity.table,
         identity.object_kind))
    return binding.binding_id


def read_connection(conn, connection_id: str) -> DataSourceConnectionV1 | None:
    row = conn.execute(
        "SELECT connection_id, environment_id, kind, host, port, auth_mechanism, secret_ref, "
        "       execution_principal, allowed_schemas, active "
        "FROM data_source_connection WHERE connection_id = %s", (connection_id,)).fetchone()
    if row is None:
        return None
    return DataSourceConnectionV1(
        connection_id=row[0], environment_id=row[1], kind=row[2], host=row[3], port=int(row[4]),
        auth_mechanism=row[5], secret_ref=row[6], execution_principal=row[7],
        allowed_schemas=frozenset(row[8] or ()), active=bool(row[9]))


def resolve_binding(conn, *, catalog_source: str,
                    table: str) -> tuple[PhysicalDatasetBindingV1, DataSourceConnectionV1] | None:
    """The binding for one table, and the connection that authorizes it — or None if unconfigured.

    Lookup is by (source, table) because that is all a logical ref carries: `ftr::tran_repos.cif_id`
    names no schema. The schema comes back FROM the binding, which is the point — it is the piece the
    catalog cannot supply.

    Raises only when a binding exists and its connection REFUSES it: an inactive connection or a
    schema no longer on the allowlist is a governance answer, and swallowing it would silently fall
    back to "not configured" and hide a revoked grant.
    """
    row = conn.execute(
        "SELECT binding_id, catalog_logical_ref, connection_id, database_name, schema_name, "
        "       table_name, object_kind "
        "FROM physical_dataset_binding "
        "WHERE catalog_source = %s AND lower(table_name) = lower(%s)",
        (catalog_source, table)).fetchone()
    if row is None:
        return None
    binding = PhysicalDatasetBindingV1(
        binding_id=row[0], catalog_logical_ref=row[1], connection_id=row[2],
        identity=PhysicalObjectIdentityV1(
            catalog_source=catalog_source, database=row[3], schema=row[4], table=row[5],
            object_kind=row[6]))
    connection = read_connection(conn, binding.connection_id)
    if connection is None:
        raise ConnectionError_(
            "BINDING_CONNECTION_MISSING",
            f"binding {binding.binding_id!r} names connection {binding.connection_id!r}, which "
            "does not exist")
    # Re-checked on every resolve, never trusted from the row: the binding may have been written
    # before the schema was revoked.
    authorize_binding(connection, binding)
    return binding, connection


# ── one catalog is one engine ────────────────────────────────────────────────────────────────────

def declare_catalog_engine(conn, *, catalog_source: str, engine: str, tier: str,
                           declared_by: str) -> None:
    """Record where a whole catalog lives. Per CATALOG, not per table.

    A real EDP has thousands of tables and one engine. Binding each of them individually is correct
    and unmaintainable, and the routing key was always available more cheaply: the catalog knows its
    engine and tier, and every row already carries its own schema.
    """
    conn.execute(
        "INSERT INTO catalog_engine (catalog_source, engine, tier, declared_by) "
        "VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (catalog_source) DO UPDATE SET engine = EXCLUDED.engine, "
        "  tier = EXCLUDED.tier, declared_by = EXCLUDED.declared_by, declared_at = now()",
        (catalog_source, engine, tier, declared_by))


def read_catalog_engine(conn, catalog_source: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT engine, tier FROM catalog_engine WHERE catalog_source = %s",
        (catalog_source,)).fetchone()
    return (row[0], row[1]) if row else None


def _connection_for(conn, *, engine: str,
                    tier: str) -> tuple[DataSourceConnectionV1, str] | None:
    """The ACTIVE connection routing this (engine, tier) in THIS deployment's environment.

    The environment is a hard match, not a filter that can be relaxed: a UAT deployment resolving a
    production connection returns a perfectly plausible answer from the wrong data, and nothing in
    the result says which cluster it came from.
    """
    row = conn.execute(
        "SELECT connection_id, database_name FROM data_source_connection "
        "WHERE kind = %s AND tier = %s AND environment_id = %s AND active",
        (engine, tier, get_settings().environment)).fetchone()
    if row is None:
        return None
    connection = read_connection(conn, row[0])
    # `database_name` is read from the ROW, not the dataclass: DataSourceConnectionV1 is the contract
    # the executor opens a cursor with, and the instance label is routing metadata this layer owns.
    return (connection, row[1]) if connection else None


def _schema_of(conn, *, catalog_source: str, table: str) -> str | None:
    """The REAL schema the catalog recorded for this table — the namespace half of the address.

    `graph_node.schema_name` is what the upload declared (`DPL_EIB_COMPLIANCE`), as distinct from the
    flattened `public.<table>.<column>` operational ref. It is the name the warehouse actually knows.
    """
    row = conn.execute(
        "SELECT schema_name FROM graph_node "
        "WHERE catalog_source = %s AND lower(table_name) = lower(%s) AND schema_name IS NOT NULL "
        "LIMIT 1", (catalog_source, table)).fetchone()
    return row[0] if row else None


def resolve_table(conn, *, catalog_source: str,
                  table: str) -> tuple[PhysicalDatasetBindingV1, DataSourceConnectionV1] | None:
    """Address one table, deriving it from the catalog's engine unless a binding overrides.

    Order matters: an explicit per-table binding WINS. That is the exception mechanism — a table
    pointed at a snapshot, or mid-migration — and it must beat the general rule or it would not be
    an exception. Rare by design, so the existence of a row is itself informative.
    """
    explicit = resolve_binding(conn, catalog_source=catalog_source, table=table)
    if explicit is not None:
        return explicit

    declared = read_catalog_engine(conn, catalog_source)
    if declared is None:
        return None
    engine, tier = declared
    routed = _connection_for(conn, engine=engine, tier=tier)
    if routed is None:
        return None
    connection, database_name = routed
    schema = _schema_of(conn, catalog_source=catalog_source, table=table)
    if not schema:
        return None

    binding = PhysicalDatasetBindingV1(
        binding_id=f"derived-{catalog_source}-{table}".lower(),
        catalog_logical_ref=f"{catalog_source}::{schema}.{table}",
        connection_id=connection.connection_id,
        identity=PhysicalObjectIdentityV1(
            catalog_source=catalog_source,
            # Identity only — never rendered into SQL by either dialect. It says WHICH cluster, so
            # two environments holding the same schema name are distinguishable.
            database=database_name or connection.connection_id,
            schema=schema, table=table, object_kind="table"))
    # Derived, but still not self-authorizing: the connection's allowlist and active flag decide,
    # exactly as they do for a stored binding.
    authorize_binding(connection, binding)
    return binding, connection
