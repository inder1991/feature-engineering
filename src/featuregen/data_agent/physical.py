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
    #: The catalog object this binding serves, as a normalized logical ref carrying the RESOLVED
    #: schema. Kept as a plain string: the binding's job is to CONNECT the two identity systems.
    #: Both resolvers derive it the same way (`resolve_dataset_binding`,
    #: `binding_store.resolve_table`) — a ref spelled from the caller's input instead would fork the
    #: content hash of one table bound through two paths, which is the identity split this module's
    #: header forbids. The flat catalog key stays recoverable as `catalog_source` + the table name.
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


# ── the ONE derivation of a DERIVED binding's identity (Release C Task 11 scope 0) ──────────────
#
# Two resolvers used to derive the same table's address two ways: `resolve_dataset_binding` took
# `database` from `ClusterInventoryV1.environment_id` and named the stream
# `identifier-endpoint:<env>:<ref>`, while `binding_store.resolve_table` took `database` from the
# connection registry and named the stream `derived-<catalog>-<table>`. Both feed
# `PhysicalObjectIdentityV1.table_id` and `binding_revision_id`, so ONE physical table acquired two
# addresses and two binding streams — and a relationship observation recorded against one was
# invisible to every reader holding the other (the observation store keys its current pointer on
# `left/right_binding_revision_id`, `store.py:440-455`).
#
# WHICH SOURCE IS HONEST FOR AN ADDRESS. `PhysicalObjectIdentityV1.database` "addresses the thing
# holding" the Hive schema — WHICH engine instance, so two clusters carrying the same schema name
# stay distinguishable. Two records could answer that:
#
# * the CONNECTION registry (`data_source_connection.database_name`) — an operator's durable,
#   governed declaration of the instance this deployment reads, written beside the host, port,
#   principal and allowlist, and already hard-matched to `settings.environment`
#   (`binding_store._connection_for`). It is also the record that AUTHORIZES the read, so the
#   address and the permission come from one place rather than two that can disagree;
# * a captured `ClusterInventoryV1.environment_id` — a label on ONE capture artifact, supplied per
#   materialization run, saying which environment was looked at. Two captures of the same cluster
#   labelled differently would fork the address of a table that never moved.
#
# The connection's declaration wins. The inventory (or, for the routing path, the connection id) is
# a FALLBACK consulted only where the registry is silent — which is exactly the case that must stay
# addressable rather than become a refusal.
#
# LIVE ROWS. Nothing is re-addressed by this choice: `binding_store.record_binding` (reached from
# `source_selector._pin_binding` -> `select_table_binding`) is the ONLY `src/` writer of
# `physical_dataset_binding_revision`, and it already derives `database` from the connection.
# `bridge_assessment.resolve_and_record_endpoint_binding` — the inventory-derived writer — has zero
# `src/` callers (tests only). So no stored revision changes and migration 1057 carries no
# re-addressing; it is spent on the crosswalk observation store instead.

def derived_binding_id(*, catalog_source: str, table: str) -> str:
    """The stream name for a binding DERIVED from configuration — one computation, both writers.

    Keyed on (catalog, table) and not on the schema, because that is the grammar the flat logical
    ref speaks (`ftr::tran_repos` names no schema) and the grammar both resolvers look tables up
    with. Ambiguity inside one catalog is refused at the seams that persist
    (`binding_store._assert_one_addressable_table`; `AMBIGUOUS_TABLE_NAME` on the inventory path)
    rather than papered over with a wider key here.

    An EXPLICIT binding keeps whatever `binding_id` its author gave it: an operator's per-table
    declaration is the documented exception mechanism, not a derivation.
    """
    return f"derived-{_norm(catalog_source)}-{_norm(table)}"


def address_database(conn: DbConn, *, connection_id: str, fallback: str | None = None) -> str:
    """The ``database`` component of a physical ADDRESS. See the block comment above.

    ``fallback`` is the caller's remaining evidence when the connection registry declares nothing —
    the inventory's ``environment_id`` on the materialization path, the connection id on the routing
    path. A blank answer from every source raises :data:`UNKNOWN_DATABASE` rather than completing the
    address by assumption, which is this module's whole rule.
    """
    declared = ""
    if _norm(connection_id):
        row = conn.execute(
            "SELECT database_name FROM data_source_connection WHERE connection_id = %s",
            (connection_id,)).fetchone()
        declared = _norm(row[0]) if row and row[0] else ""
    database = declared or _norm(fallback) or _norm(connection_id)
    if not database:
        raise UnknownSchema(
            UNKNOWN_DATABASE,
            f"no database for connection {connection_id!r}: neither the connection registry nor the "
            "caller could name the instance holding this schema, and a physical address cannot be "
            "completed by assumption")
    return database


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
    requested_ref = normalize_ref(source, schema, table)
    requirement = derive_requirement(conn, inventory, table_ref=requested_ref)
    if not isinstance(requirement, PhysicalInputRequirement):
        return requirement
    # The RESOLVED ref, not the one the caller happened to spell. `derive_requirement` resolves the
    # real schema from the catalog (the ref's `public` segment is never a Hive schema), and
    # `binding_store.resolve_table` — the only `src/` writer of these revisions — has always
    # composed the resolved schema here. Deriving it from the caller's spelling instead meant one
    # table bound through the bridge path and through the selection path carried two different
    # `catalog_logical_ref` values, hence two content hashes and two `pbr_` revisions for one
    # address. One derivation, and it converges onto the live writer's value, so nothing stored
    # moves (Release C Task 11 scope 0).
    logical_ref = normalize_ref(
        requirement.catalog_source, requirement.schema, requirement.table)
    partition_columns = tuple(
        column_name for column_name, _physical_type in (requirement.partition_columns or ()))
    identity = PhysicalObjectIdentityV1(
        catalog_source=requirement.catalog_source,
        # ONE derivation, shared with `binding_store.resolve_table` (see the block comment above
        # `derived_binding_id`). The inventory's environment is the FALLBACK, used only where the
        # connection registry declares no instance — which preserves this path's answer wherever
        # nothing else can speak.
        database=address_database(
            conn, connection_id=connection_id, fallback=inventory.environment_id),
        schema=requirement.schema,
        table=requirement.table,
        object_kind="table",
    )
    return PhysicalDatasetBindingV1(
        binding_id=(
            binding_id
            or derived_binding_id(
                catalog_source=requirement.catalog_source, table=requirement.table)
        ),
        catalog_logical_ref=logical_ref,
        connection_id=connection_id,
        identity=identity,
        partition_columns=partition_columns,
        business_time_column=business_time_column,
        purposes=purposes,
    )
