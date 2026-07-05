from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query

from featuregen.api.deps import get_conn, get_identity
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.graph import JoinEdge, column_joins
from featuregen.overlay.upload.join_path import JoinStep, find_join_path

router = APIRouter()


@router.get("/columns/{object_ref}/joins")
def joins_for_column(
    object_ref: str,
    source: str,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> list[JoinEdge]:
    return column_joins(conn, source, object_ref)


@router.get("/join-path")
def join_path(
    source: str,
    to: str,
    from_table: Annotated[str, Query(alias="from")],
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> list[JoinStep] | None:
    """Steps oriented to the traversal direction (a reverse N:1 hop reads 1:N — M7).
    null = unreachable; [] = same table."""
    return find_join_path(conn, source, from_table, to)
