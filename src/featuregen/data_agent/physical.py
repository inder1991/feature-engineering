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
from typing import TYPE_CHECKING, Any

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

if TYPE_CHECKING:
    from featuregen.contracts import DbConn
    from featuregen.materialize.codes import MaterializationRefused
    from featuregen.materialize.inventory import ClusterInventoryV1

#: Refusal codes. Closed vocabulary — a governed refusal never surfaces as a bare ValueError with a
#: prose message the caller has to parse.
UNKNOWN_SCHEMA = "PHYSICAL_UNKNOWN_SCHEMA"
UNKNOWN_DATABASE = "PHYSICAL_UNKNOWN_DATABASE"
KIND_MISMATCH = "PHYSICAL_KIND_MISMATCH"
BINDING_NOT_A_TABLE = "BINDING_NOT_A_TABLE"
BUSINESS_TIME_NOT_PARTITIONED = "BINDING_BUSINESS_TIME_NOT_PARTITIONED"
BLANK_BINDING_FIELD = "BINDING_BLANK_IDENTITY_FIELD"

BINDING_CONTRACT_VERSION = "1.0.0"

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

    Every component is identity-bearing. ``database`` is included because two CLUSTERS or catalogs
    can hold the same schema name, and ``schema`` because two same-named tables can live in
    different schemas — the two collisions the public-flattened catalog key cannot express.

    This once said ``database`` was needed because "two same-named schemas can live in different
    Hive databases", implying a database/schema/table hierarchy. HiveQL has no such level: ``CREATE
    SCHEMA`` is an alias for ``CREATE DATABASE``, and a real HiveServer2 refuses a three-part name.
    So ``schema`` IS the Hive database, and ``database`` addresses the thing holding it. The
    distinction matters because it is why :meth:`HiveDialect.table_ref` renders two parts while the
    identity keeps four — an ADDRESS is not a SQL name.
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

    def identity_payload(self) -> dict[str, str | None]:
        """Canonical address payload used by binding revisions.

        The normalized values are intentional: Hive identifiers are case-insensitive, so a catalog
        recapture that changes only casing must not produce a new binding revision.
        """
        return {
            "catalog_source": _norm(self.catalog_source),
            "database": _norm(self.database),
            "schema": _norm(self.schema),
            "table": _norm(self.table),
            "object_kind": self.object_kind,
            "column": _norm(self.column) or None,
        }


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
        for name in ("binding_id", "catalog_logical_ref", "connection_id"):
            if not str(getattr(self, name)).strip():
                raise PhysicalBindingError(
                    BLANK_BINDING_FIELD, f"{name} must not be blank")
        try:
            source, schema, table, column = parse_ref(
                self.catalog_logical_ref.strip().lower())
        except ValueError as exc:
            raise PhysicalBindingError(
                BLANK_BINDING_FIELD,
                f"catalog_logical_ref is not a normalized catalog ref: {exc}") from exc
        if column is not None:
            raise PhysicalBindingError(
                BINDING_NOT_A_TABLE,
                "a dataset binding catalog_logical_ref must address a table, not a column")
        logical_ref = normalize_ref(source, schema, table)
        object.__setattr__(self, "binding_id", self.binding_id.strip())
        object.__setattr__(self, "catalog_logical_ref", logical_ref)
        object.__setattr__(self, "connection_id", self.connection_id.strip())
        object.__setattr__(
            self, "partition_columns",
            tuple(_norm(column) for column in self.partition_columns))
        object.__setattr__(
            self, "business_time_column",
            _norm(self.business_time_column) or None)
        object.__setattr__(
            self, "purposes",
            tuple(sorted({_norm(purpose) for purpose in self.purposes if _norm(purpose)})))
        if self.identity.object_kind != "table":
            raise PhysicalBindingError(
                BINDING_NOT_A_TABLE,
                "a dataset binding addresses a table; a column is reached through its table")
        if _norm(self.identity.catalog_source) != source:
            raise PhysicalBindingError(
                KIND_MISMATCH,
                "catalog_logical_ref and physical identity must name the same catalog_source")
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

    def content_payload(self) -> dict[str, Any]:
        """Stable declaration sealed by :attr:`content_hash`.

        ``binding_id`` is excluded: it names the durable binding stream, while this payload says
        what one revision of that stream declares. Observation time and credentials are absent.
        """
        return {
            "contract_version": BINDING_CONTRACT_VERSION,
            "catalog_logical_ref": self.catalog_logical_ref,
            "connection_id": self.connection_id,
            "physical_identity": self.identity.identity_payload(),
            "partition_columns": list(self.partition_columns),
            "business_time_column": self.business_time_column,
            "purposes": list(self.purposes),
        }

    @property
    def content_hash(self) -> str:
        return materialize_hash(self.content_payload())

    @property
    def binding_revision_id(self) -> str:
        """Revision identity is scoped to the durable binding, not just its reusable content."""
        return "pbr_" + materialize_hash({
            "binding_id": self.binding_id,
            "content_hash": self.content_hash,
        })


def record_binding_revision(
    conn: DbConn,
    binding: PhysicalDatasetBindingV1,
    *,
    recorded_by: str | None = None,
) -> str:
    """Append the binding revision if absent and return its deterministic revision id.

    Re-recording byte-identical content is idempotent. A changed address, layout declaration,
    connection, or purpose receives a new revision while the flat catalog ref remains unchanged.
    """
    from psycopg.types.json import Jsonb

    conn.execute(
        "INSERT INTO physical_dataset_binding_revision ("
        "  binding_revision_id, binding_id, content_hash, catalog_logical_ref, connection_id,"
        "  physical_id, binding_json, recorded_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (binding_revision_id) DO NOTHING",
        (
            binding.binding_revision_id,
            binding.binding_id,
            binding.content_hash,
            binding.catalog_logical_ref,
            binding.connection_id,
            binding.identity.table_id,
            Jsonb(binding.content_payload()),
            recorded_by,
        ),
    )
    return binding.binding_revision_id


def resolve_dataset_binding(
    conn: DbConn,
    inventory: ClusterInventoryV1,
    *,
    logical_table_ref: str,
    connection_id: str,
    binding_id: str | None = None,
    business_time_column: str | None = None,
    purposes: tuple[str, ...] = (),
) -> PhysicalDatasetBindingV1 | MaterializationRefused:
    """Resolve a flat logical table ref through the governed catalog and target inventory.

    This deliberately delegates to :func:`materialize.inputs.derive_requirement`, whose resolution
    order is catalog ``schema_name`` then declared environment mapping, else a typed refusal. The
    ref's ``public`` segment is never interpreted as a Hive schema.
    """
    from featuregen.materialize.inputs import PhysicalInputRequirement, derive_requirement

    source, schema, table, column = parse_ref(logical_table_ref.strip().lower())
    if column is not None:
        raise ValueError(
            f"logical_table_ref must address a table, got {logical_table_ref!r}")
    logical_ref = normalize_ref(source, schema, table)
    requirement = derive_requirement(conn, inventory, table_ref=logical_ref)
    if not isinstance(requirement, PhysicalInputRequirement):
        return requirement
    partition_columns = tuple(
        column_name for column_name, _physical_type in (requirement.partition_columns or ()))
    identity = PhysicalObjectIdentityV1(
        catalog_source=requirement.catalog_source,
        # In PhysicalObjectIdentityV1, database addresses the cluster/catalog holding the Hive
        # schema. ClusterInventoryV1.environment_id is exactly that stable environment address.
        database=inventory.environment_id,
        schema=requirement.schema,
        table=requirement.table,
        object_kind="table",
    )
    return PhysicalDatasetBindingV1(
        binding_id=(
            binding_id
            or f"{inventory.environment_id}:{requirement.catalog_source}:{requirement.schema}."
               f"{requirement.table}"
        ),
        catalog_logical_ref=logical_ref,
        connection_id=connection_id,
        identity=identity,
        partition_columns=partition_columns,
        business_time_column=business_time_column,
        purposes=purposes,
    )
