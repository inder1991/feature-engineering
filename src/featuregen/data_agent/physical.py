"""Physical identity and dataset binding — the address a data observation attaches to.

Two layers, deliberately (roadmap §3):

* :class:`PhysicalObjectIdentityV1` says **what exists** — the address of a real table or column in
  a real engine.
* :class:`PhysicalDatasetBindingV1` says **how an approved worker reads it** — which connection,
  which partitions, which business-time mapping — and *references* an identity rather than
  redefining one.

**Why this cannot reuse the catalog's ref.** ``object_ref.normalize_ref`` defaults a missing schema
to ``public`` (``object_ref.py:85``). That is correct for a display and attachment key, where a
stable string is needed and the schema may genuinely be unknown. It is wrong for an address: it
converts "we do not know the schema" into a confident, specific and possibly incorrect answer, and a
profile attached to the wrong table poisons every candidate, feature and analysis built on it.

So this module **refuses** what the catalog key substitutes. That is the whole point of it, and the
reason it exists as a separate contract rather than a field on the existing ref.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Refusal codes. Closed vocabulary — a governed refusal never surfaces as a bare ValueError with a
#: prose message the caller has to parse.
UNKNOWN_SCHEMA = "PHYSICAL_UNKNOWN_SCHEMA"
UNKNOWN_DATABASE = "PHYSICAL_UNKNOWN_DATABASE"
KIND_MISMATCH = "PHYSICAL_KIND_MISMATCH"
BINDING_NOT_A_TABLE = "BINDING_NOT_A_TABLE"
BUSINESS_TIME_NOT_PARTITIONED = "BINDING_BUSINESS_TIME_NOT_PARTITIONED"

_OBJECT_KINDS = ("table", "column")


class PhysicalBindingError(ValueError):
    """A physical identity or binding that cannot be formed. Carries a closed ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class UnknownSchema(PhysicalBindingError):
    """The address is incomplete. Deliberately its own type: this is the one refusal a caller is
    most likely to want to catch and route to "ask a human which schema", rather than treat as a
    programming error."""


def _norm(value: str | None) -> str:
    """Lower + strip. Hive identifiers are case-insensitive, so two spellings of one table must be
    one object — otherwise the same physical table accumulates two divergent profiles."""
    return (value or "").strip().lower()


@dataclass(frozen=True, slots=True)
class PhysicalObjectIdentityV1:
    """The address of one physical table or column.

    Every component is identity-bearing. ``database`` is included because two same-named schemas can
    live in different Hive databases, and ``schema`` because two same-named tables can live in
    different schemas — the two collisions the public-flattened catalog key cannot express.
    """

    catalog_source: str
    database: str
    schema: str
    table: str
    object_kind: str = "column"
    column: str | None = None

    def __post_init__(self) -> None:
        if not _norm(self.database):
            raise UnknownSchema(
                UNKNOWN_DATABASE,
                f"no database for {_norm(self.table) or '<table>'}: a physical address cannot be "
                "completed by assumption")
        if not _norm(self.schema):
            raise UnknownSchema(
                UNKNOWN_SCHEMA,
                f"no schema for {_norm(self.table) or '<table>'}: a physical address cannot be "
                "completed by assumption")
        if not _norm(self.table):
            raise PhysicalBindingError(UNKNOWN_SCHEMA, "no table")
        if self.object_kind not in _OBJECT_KINDS:
            raise PhysicalBindingError(
                KIND_MISMATCH, f"object_kind must be one of {_OBJECT_KINDS}, got {self.object_kind!r}")
        if self.object_kind == "column" and not _norm(self.column):
            raise PhysicalBindingError(KIND_MISMATCH, "a column identity requires a column")
        if self.object_kind == "table" and _norm(self.column):
            raise PhysicalBindingError(KIND_MISMATCH, "a table identity must not carry a column")

    @property
    def physical_id(self) -> str:
        """The stable address. A column's id EXTENDS its table's, so containment is a usable test —
        `table.physical_id in column.physical_id` — and a profile can be attributed upward without a
        second lookup."""
        parts = [_norm(self.catalog_source), _norm(self.database), _norm(self.schema),
                 _norm(self.table)]
        if self.object_kind == "column":
            parts.append(_norm(self.column))
        return "::".join(parts)

    @property
    def table_id(self) -> str:
        """This object's owning table address — itself, for a table identity."""
        return "::".join([_norm(self.catalog_source), _norm(self.database), _norm(self.schema),
                          _norm(self.table)])


@dataclass(frozen=True, slots=True)
class PhysicalDatasetBindingV1:
    """Authorization to read one physical TABLE, and the layout facts needed to read it cheaply.

    Holds **no credentials** — only ``connection_id``. A binding that could carry a secret is a
    binding that eventually appears in a log, a JSON column or an LLM prompt.
    """

    binding_id: str
    #: The catalog object this binding serves, as the catalog's own (flattened) logical ref. Kept as
    #: a plain string: the binding's job is to CONNECT the two identity systems, so it must be able
    #: to name a catalog object whose schema the catalog itself may have defaulted.
    catalog_logical_ref: str
    connection_id: str
    identity: PhysicalObjectIdentityV1
    partition_columns: tuple[str, ...] = ()
    #: Which partition column carries business time. This is what makes partition pruning possible,
    #: and pruning is a plan property rather than an executor optimisation (roadmap §3c).
    business_time_column: str | None = None
    purposes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.identity.object_kind != "table":
            raise PhysicalBindingError(
                BINDING_NOT_A_TABLE,
                "a dataset binding addresses a table; a column is reached through its table")
        if self.business_time_column is not None:
            declared = {_norm(c) for c in self.partition_columns}
            if _norm(self.business_time_column) not in declared:
                raise PhysicalBindingError(
                    BUSINESS_TIME_NOT_PARTITIONED,
                    f"business_time_column {self.business_time_column!r} is not a declared "
                    f"partition column {sorted(declared) or '[]'}: a business-time mapping that "
                    "cannot prune would silently produce a full scan")

    def column(self, name: str) -> PhysicalObjectIdentityV1:
        """The address of one column under this binding's table."""
        return PhysicalObjectIdentityV1(
            catalog_source=self.identity.catalog_source, database=self.identity.database,
            schema=self.identity.schema, table=self.identity.table,
            object_kind="column", column=name)
