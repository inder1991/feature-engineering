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

from featuregen.data_agent.connection import (
    ConnectionError_,
    DataSourceConnectionV1,
    authorize_binding,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1


def record_connection(conn, connection: DataSourceConnectionV1) -> str:
    """Upsert one access grant. The credential is NOT stored — `secret_ref` is a pointer."""
    conn.execute(
        "INSERT INTO data_source_connection (connection_id, environment_id, kind, host, port, "
        "  auth_mechanism, secret_ref, execution_principal, allowed_schemas, active) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (connection_id) DO UPDATE SET "
        "  environment_id = EXCLUDED.environment_id, kind = EXCLUDED.kind, host = EXCLUDED.host, "
        "  port = EXCLUDED.port, auth_mechanism = EXCLUDED.auth_mechanism, "
        "  secret_ref = EXCLUDED.secret_ref, execution_principal = EXCLUDED.execution_principal, "
        "  allowed_schemas = EXCLUDED.allowed_schemas, active = EXCLUDED.active",
        (connection.connection_id, connection.environment_id, connection.kind, connection.host,
         connection.port, connection.auth_mechanism, connection.secret_ref,
         connection.execution_principal, sorted(connection.allowed_schemas), connection.active))
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
