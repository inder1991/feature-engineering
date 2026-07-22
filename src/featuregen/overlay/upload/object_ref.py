"""The single normalized object reference shared by every per-object store (spec §5.1).

A ``logical_ref`` is a stable, schema-preserving string identity for one source object — a column,
or a table when ``column`` is absent. It is THE join key every Phase-1 producer writes evidence
against (``field_evidence``), the resolver logs decisions under (``field_decision``), and the entity
graph will later key nodes on (``graph_node``). Because it is written by many producers across many
uploads, it MUST be deterministic and round-trippable: the same ``(source, schema, table, column)``
always normalizes to the same string, and the string parses back into its components.

Schema is PRESERVED (two objects with the same table name in different schemas are distinct
identities), and defaults to ``"public"`` when a source omits it — so a schema-less upload and an
explicit ``public`` upload resolve to ONE identity rather than two. Components are normalized
(stripped + lower-cased, matching ``overlay.identity._norm``) so ``Accounts`` and ``accounts`` are
the same object; unquoted SQL identifiers already fold to lower case.
"""
from __future__ import annotations

_DEFAULT_SCHEMA = "public"

# `source::schema.table[.column]`. The `::` scheme separator keeps `source` unambiguous from the
# dotted, schema-qualified path (the same dotted convention `overlay.identity.display_object_ref`
# uses), so a ref round-trips via `parse_ref`.
_SOURCE_SEP = "::"
_PATH_SEP = "."


def _norm(value: str) -> str:
    """Normalize one ref component: strip surrounding whitespace and lower-case (matching
    ``overlay.identity._norm``) so case / padding differences never split one object into two."""
    return value.strip().lower()


def is_reserved_source_name(source: str) -> bool:
    """True iff ``source`` (after the ``_norm`` fold) is a RESERVED internal-fixture name — the
    double-underscore-wrapped ``__…__`` form (e.g. ``__gate_gold__``).

    The 3C.1 gate console seeds its gold / drift fixtures under a reserved source so its real
    ``build_graph`` (``DELETE FROM graph_node WHERE catalog_source = …``) and its watermark writes
    can NEVER touch — or lock, and thereby deadlock — a real customer catalog that happens to share
    the fixture's old name (a bank naming its core-banking extract ``core``). A USER upload naming a
    reserved source is rejected at the write boundary (:func:`normalize_source_name` +
    ``validate_rows``), so the two name spaces can never collide."""
    normalized = source.strip().lower()
    return len(normalized) >= 4 and normalized.startswith("__") and normalized.endswith("__")


def normalize_source_name(source: str) -> str:
    """Strip + lower-case a catalog source id (the ``_norm`` fold every identity component gets) AND
    fail closed on a name that is not a single URL path segment, or that is a RESERVED name.

    ``source`` is ONE path segment across the whole API (``/sources/{source}/...``,
    ``/catalog/assets/{source}/{object_ref:path}``, ``/uploads`` Form field). A '/' or a '%' in it
    would (percent-)decode across the route boundary — uvicorn percent-decodes ``%2F`` to ``/``
    BEFORE routing — and mis-split ``{source}/{object_ref:path}``, reading or writing a DIFFERENT
    source. Reject both at the WRITE boundary rather than loosening any route. A ``__…__`` RESERVED
    name (:func:`is_reserved_source_name`) is also rejected, so a user upload can never share a
    catalog with the gate console's internal fixtures. Raises ``ValueError`` on an empty name, one
    containing '/' or '%', or a reserved name."""
    normalized = source.strip().lower()
    if not normalized:
        raise ValueError("source is required")
    if "/" in normalized or "%" in normalized:
        raise ValueError(
            "source must be a single path segment: '/' and '%' are not allowed in a source name")
    if is_reserved_source_name(normalized):
        raise ValueError(
            "source name is reserved: names wrapped in double underscores (e.g. '__gate_gold__') "
            "are reserved for internal fixtures and cannot be used as a catalog source")
    return normalized


def normalize_ref(
    source: str, schema: str | None, table: str, column: str | None = None
) -> str:
    """Build the stable, schema-preserving ``logical_ref`` for a source object (spec §5.1).

    ``schema`` defaults to ``"public"`` when absent (``None`` or blank). ``column`` absent yields a
    TABLE ref; present yields a COLUMN ref under that table. Deterministic and round-trippable — the
    same inputs always produce the same string, and :func:`parse_ref` recovers the components."""
    schema_part = _norm(schema) if schema and schema.strip() else _DEFAULT_SCHEMA
    parts = [schema_part, _norm(table)]
    if column and column.strip():
        parts.append(_norm(column))
    return f"{_norm(source)}{_SOURCE_SEP}{_PATH_SEP.join(parts)}"


def parse_ref(logical_ref: str) -> tuple[str, str, str, str | None]:
    """Inverse of :func:`normalize_ref`: recover ``(source, schema, table, column)`` from a ref.

    ``column`` is ``None`` for a table ref. Raises ``ValueError`` on a string that was not produced
    by :func:`normalize_ref` (missing the source separator, or an unexpected path arity)."""
    source, sep, path = logical_ref.partition(_SOURCE_SEP)
    if not sep or not path:
        raise ValueError(f"not a normalized logical_ref: {logical_ref!r}")
    parts = path.split(_PATH_SEP)
    if len(parts) == 2:
        schema, table = parts
        return source, schema, table, None
    if len(parts) == 3:
        schema, table, column = parts
        return source, schema, table, column
    raise ValueError(f"not a normalized logical_ref: {logical_ref!r}")
