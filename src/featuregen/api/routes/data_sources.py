"""Where each catalog's data lives — the routing an operator configures.

`/integrations` next door manages METADATA connectors: where the catalog description comes from, and
the blast radius of a bad one is a bad import. These routes are a different kind of thing that
belongs on the same screen. A data-source connection grants read access to a warehouse under a named
principal, so it sits behind `require_confirmer` — the raw `platform-admin` claim — not
`catalog:write`.

**Reads are open to `catalog:read`, writes are not.** Anyone working the analysis workspace needs to
see whether a catalog is routed and to where, because that is what the gap messages point at. Nobody
but an administrator may change it.

**No credential passes through here.** `secret_ref` is a pointer into a secret manager, resolved at
the point of use. The route rejects anything that looks like a literal secret rather than trusting
the caller, because the cost of being wrong once is a credential in a database backup forever.
"""

from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, require_catalog_read, require_confirmer
from featuregen.config import get_settings
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.data_agent.binding_store import (
    declare_catalog_engine,
    read_catalog_engine,
    record_connection,
)
from featuregen.data_agent.connection import DataSourceConnectionV1
from featuregen.overlay.upload.identifier_scope import (
    DECLARED_SCOPE_BASES,
    declare_catalog_semantic_scope,
    read_catalog_semantic_scope,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

#: Engines a dialect exists for. Closed, because a connection to an engine nothing can render SQL for
#: is a route to nowhere that looks configured.
ENGINES: tuple[str, ...] = ("hive", "oracle", "postgres")

#: A `secret_ref` must be a REFERENCE. These prefixes are what a reference looks like; anything else
#: is refused rather than stored, because a literal pasted here would survive in every backup.
_SECRET_SCHEMES: tuple[str, ...] = ("vault://", "env://", "aws-secrets://", "azure-kv://", "gcp-sm://")


class ConnectionIn(BaseModel):
    connection_id: str = Field(min_length=1, max_length=64)
    engine: str
    #: OPEN vocabulary — edp/ods is the bank's taxonomy, not this system's.
    tier: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    auth_mechanism: str = Field(min_length=1)
    secret_ref: str = Field(min_length=1)
    execution_principal: str = Field(min_length=1)
    #: The schemas this connection may read. Empty authorizes nothing, which is a legitimate
    #: half-configured state and never an implicit "all".
    allowed_schemas: list[str] = Field(default_factory=list)
    database_name: str = ""
    active: bool = True


class CatalogEngineIn(BaseModel):
    engine: str
    tier: str = Field(min_length=1, max_length=64)


def _validate_engine(engine: str) -> None:
    if engine not in ENGINES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown engine {engine!r}; this deployment can render SQL for {list(ENGINES)}")


def _validate_secret_ref(ref: str) -> None:
    if not any(ref.startswith(scheme) for scheme in _SECRET_SCHEMES):
        raise HTTPException(
            status_code=422,
            detail=(f"secret_ref must be a reference into a secret manager "
                    f"({', '.join(_SECRET_SCHEMES)}), never the secret itself — a literal stored "
                    "here would survive in every backup of this database"))


@router.get("/data-sources/connections", dependencies=[Depends(require_catalog_read)])
def list_connections(conn: _Conn) -> dict:
    """Every route, and this deployment's own environment.

    The environment is returned because it is the thing that makes a row usable or not, and a screen
    listing connections without it invites someone to wonder why a configured route does nothing.
    """
    rows = conn.execute(
        "SELECT connection_id, environment_id, kind, tier, host, port, auth_mechanism, "
        "       secret_ref, execution_principal, allowed_schemas, database_name, active "
        "FROM data_source_connection ORDER BY kind, tier, connection_id").fetchall()
    return {
        "environment": get_settings().environment,
        "engines": list(ENGINES),
        "connections": [
            {"connection_id": r[0], "environment": r[1], "engine": r[2], "tier": r[3],
             "host": r[4], "port": r[5], "auth_mechanism": r[6], "secret_ref": r[7],
             "execution_principal": r[8], "allowed_schemas": list(r[9] or ()),
             "database_name": r[10], "active": r[11],
             # A route in another environment is visible but INERT here. Saying so beats leaving
             # someone to discover it from a gap message.
             "usable_here": r[11] and r[1] == get_settings().environment}
            for r in rows],
    }


@router.put("/data-sources/connections/{connection_id}")
def put_connection(connection_id: str, body: ConnectionIn, conn: _Conn,
                   identity: Annotated[IdentityEnvelope, Depends(require_confirmer)]) -> dict:
    """Create or replace one route. Administrator only — this grants read access to a warehouse."""
    if connection_id != body.connection_id:
        raise HTTPException(status_code=422, detail="connection_id in the path and body must match")
    _validate_engine(body.engine)
    _validate_secret_ref(body.secret_ref)
    try:
        record_connection(
            conn,
            DataSourceConnectionV1(
                connection_id=body.connection_id,
                # Always THIS deployment's environment: a route is created where it is used, so a
                # UAT screen cannot mint a production row by typing a different word.
                environment_id=get_settings().environment,
                kind=body.engine, host=body.host, port=body.port,
                auth_mechanism=body.auth_mechanism, secret_ref=body.secret_ref,
                execution_principal=body.execution_principal,
                allowed_schemas=frozenset(body.allowed_schemas), active=body.active),
            tier=body.tier, database_name=body.database_name)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail=(f"another ACTIVE connection already routes {body.engine}/{body.tier} in "
                    f"{get_settings().environment}; two would mean the same catalog resolving to "
                    "different clusters depending on read order")) from None
    return {"connection_id": body.connection_id, "environment": get_settings().environment}


@router.get("/data-sources/catalogs", dependencies=[Depends(require_catalog_read)])
def list_catalog_engines(conn: _Conn) -> dict:
    """Which engine each catalog lives on, for every catalog the upload path knows about.

    Catalogs with no declaration are listed too, with a null engine: an operator needs to see what is
    NOT routed, and a list of only the configured ones hides exactly the thing they came to fix.
    """
    sources = [r[0] for r in conn.execute(
        "SELECT DISTINCT catalog_source FROM graph_node ORDER BY catalog_source").fetchall()]
    declared = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT catalog_source, engine, tier, declared_by FROM catalog_engine").fetchall()}
    return {"catalogs": [
        {"catalog_source": s,
         "engine": declared.get(s, (None, None, None))[0],
         "tier": declared.get(s, (None, None, None))[1],
         "declared_by": declared.get(s, (None, None, None))[2]}
        for s in sources]}


@router.put("/data-sources/catalogs/{catalog_source}")
def put_catalog_engine(catalog_source: str, body: CatalogEngineIn, conn: _Conn,
                       identity: Annotated[IdentityEnvelope, Depends(require_confirmer)]) -> dict:
    """Declare where a whole catalog's data lives. One catalog is one engine."""
    _validate_engine(body.engine)
    if not conn.execute("SELECT 1 FROM graph_node WHERE catalog_source = %s LIMIT 1",
                        (catalog_source,)).fetchone():
        # Declaring an engine for a catalog nobody has uploaded is a typo with a plausible shape:
        # it would sit there looking configured and route nothing.
        raise HTTPException(status_code=404, detail=f"no catalog {catalog_source!r} has been uploaded")
    declare_catalog_engine(conn, catalog_source=catalog_source, engine=body.engine,
                           tier=body.tier, declared_by=identity.subject)
    declared = read_catalog_engine(conn, catalog_source)
    return {"catalog_source": catalog_source, "engine": declared[0], "tier": declared[1]}


# ── the catalog SEMANTIC scope: which institution issues this catalog's identifiers ──────────────
# `Concept.namespace` names an identifier SCHEME; cross-catalog equality also needs the ISSUER
# (semantic Task 2 — scheme alone is never proof). One declaration per catalog, like the engine
# declaration above; no institution name is ever hardcoded — the operator supplies it here.


class SemanticScopeIn(BaseModel):
    issuer_scope: str = Field(min_length=1, max_length=64)
    basis: str = "catalog_scope"


def _require_uploaded_catalog(conn: psycopg.Connection, catalog_source: str) -> None:
    if not conn.execute("SELECT 1 FROM graph_node WHERE catalog_source = %s LIMIT 1",
                        (catalog_source,)).fetchone():
        # Same shape as the engine declaration: configuring a catalog nobody uploaded is a typo
        # that would sit there looking configured.
        raise HTTPException(status_code=404, detail=f"no catalog {catalog_source!r} has been uploaded")


@router.get("/data-sources/catalogs/{catalog_source}/semantic-scope",
            dependencies=[Depends(require_catalog_read)])
def get_catalog_semantic_scope(catalog_source: str, conn: _Conn) -> dict:
    """One catalog's declared issuer scope. An UNDECLARED scope returns nulls, never an invented
    issuer — 'unresolved' is honest state the bridge path treats as advisory, not an error."""
    _require_uploaded_catalog(conn, catalog_source)
    scope = read_catalog_semantic_scope(conn, catalog_source)
    return {
        "catalog_source": catalog_source,
        "issuer_scope": scope.issuer_scope if scope else None,
        "basis": scope.basis if scope else None,
        "declared_by": scope.declared_by if scope else None,
    }


@router.put("/data-sources/catalogs/{catalog_source}/semantic-scope")
def put_catalog_semantic_scope(
        catalog_source: str, body: SemanticScopeIn, conn: _Conn,
        identity: Annotated[IdentityEnvelope, Depends(require_confirmer)]) -> dict:
    """Declare which institution issues this catalog's catalog-scoped identifiers. Administrator
    only: the declaration changes which cross-catalog joins are even proposable."""
    if body.basis not in DECLARED_SCOPE_BASES:
        raise HTTPException(
            status_code=422,
            detail=(f"semantic-scope basis must be one of {list(DECLARED_SCOPE_BASES)}; "
                    "'unresolved' is the honest absence of a declaration, never declared"))
    _require_uploaded_catalog(conn, catalog_source)
    declare_catalog_semantic_scope(
        conn, catalog_source=catalog_source, issuer_scope=body.issuer_scope, basis=body.basis,
        declared_by=identity.subject)
    scope = read_catalog_semantic_scope(conn, catalog_source)
    return {"catalog_source": catalog_source, "issuer_scope": scope.issuer_scope,
            "basis": scope.basis}
