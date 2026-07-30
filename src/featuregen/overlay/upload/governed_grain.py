"""The ONE governed-`GRAIN` reader: what a table's COMPLETE, CURRENT, VERIFIED grain is.

Two consumers, one implementation. ``materialize.spine`` asks "is THIS declared column attested row
identity?"; ``overlay.upload.planner.declarations`` asks "WHAT is the far table's whole grain, so I
can tell whether a bridge endpoint is all of it?". A second copy of either read is a second chance to
disagree about what "governed" means, so both live here.

TWO EVIDENCE STORES, AND WHICH ONE IS AUTHORITY FOR WHICH QUESTION
-----------------------------------------------------------------
A confirmed grain exists in two places, and they are not interchangeable:

* ``overlay_fact_state`` — the overlay read model the ``OverlayProjection`` maintains from the
  four-eyes event stream. It carries the fact's OWN value: the complete ``columns`` list and
  ``is_unique``. ``table_fact_projection`` calls this "the load-bearing truth".
* ``graph_node.is_grain`` + ``grain_fact_event_id`` — a SECOND, lossy projection of that truth onto
  the columns, written by ``table_fact_projection``. It keeps only "this column is one of them", so
  it can express neither COMPLETENESS (a set of per-column flags one confirm behind the fact stream
  looks exactly like a narrower grain) nor UNIQUENESS (§18.3 records that ``is_unique`` is dropped —
  the same gap ``expression_ir`` records for ``AVAILABILITY_TIME``'s ``basis``).

So for "what IS the grain" the authority is the FACT, exactly as ``planner.b_source_grain`` already
insists: *"the grain (its key columns) comes ONLY from the VERIFIED grain fact — never from
``graph_node.is_grain`` / any advisory file flag"*. The flags cannot be that authority, and not only
because they are lossy: ``table_fact_projection`` runs at end-of-ingest and at the table-fact confirm
route, so a governed grain confirmed by any other path has a VERIFIED fact and NO link yet. Gating on
the link would refuse a genuinely governed grain because a downstream cache is behind.

THE TWO-STEP READ, AND THE JOB IT DOES HERE
-------------------------------------------
:func:`governed_grain_columns` is ``spine._governed_grain_columns`` moved here verbatim, and its
discipline is unchanged:

    the flat ``is_grain`` flag enumerates the CANDIDATES (it is the only per-table index there is),
    and C1's ``read_operational_value`` then decides which of them are actually GOVERNED.
    Enumerating on the flag alone would accept a file declaration; asking C1 without the flag would
    mean guessing column names.

``build_graph`` writes ``graph_node.is_grain`` straight from an upload and ``table_fact_projection``
deliberately SPARES those file-declared columns when it clears, so ``is_grain = true`` alone is a
claim an uploader made about their own file. That is why nothing here ever GRANTS grain from the
flags. In :func:`read_governed_grain` the two-step is a CONTRADICTION DETECTOR and nothing else: when
the second projection has run, what it says must MATCH the fact, or one of the two is stale and the
grain is refused. It can only ever narrow the answer to "no attested grain" — never widen it.

``resolve_fact`` is deliberately not called, for the reason ``expression_ir._availability_fact_value``
already records: it requires a registered ``CatalogAdapter`` (``current_catalog_adapter`` RAISES
without one, and ``declarations.build_compiler_context`` has none to give) and it would RE-DECIDE
authority — catalog precedence, expiry, drift — a second, possibly disagreeing opinion about a
question the projection already answered. Note the boundary that leaves: DRIFT freshness is NOT
decided here. Each caller already owns that axis (the compiler's ``revalidate_freshness`` + contract
freshness status; the spine's own callers), and a third opinion could only disagree with them.

WHAT A GRAIN FACT DOES NOT SAY
------------------------------
It attests PHYSICAL UNIQUENESS: these columns identify at most one row of this table. It says nothing
about MEANING — not that a column in another catalog denotes the same thing, not that a join between
them is semantically sound. Bridge semantic trust is a separate axis with its own authority (the
bridge fact and its lifecycle), and execution eligibility is a third. A caller that folded any two of
them together would be reporting one axis's evidence under another's name.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from featuregen.contracts.db import DbConn
from featuregen.overlay.facts import GRAIN, FactValidationError, validate_fact_value
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.operational_facts import read_operational_value
from featuregen.overlay.upload.upload_catalog import table_ref

#: C1's operational (governed) status, and the rendered boolean a governed ``is_grain`` flag carries.
_RESOLVED = "resolved"
_GOVERNED_TRUE = "true"

#: The schema ``graph_node`` object_refs are flattened to (``graph._SCHEMA``). ``read_column_facts``
#: rebuilds its lookup key as ``public.<table>.<column>`` whatever schema the ``logical_ref`` carries,
#: so on the ``is_grain`` path the schema changes nothing — it is still threaded through
#: :func:`normalize_ref` rather than concatenated, because that is how the writers keyed the row.
_FLAT_SCHEMA = "public"

#: The only servable overlay status (``resolve.py``).
_VERIFIED = "VERIFIED"


class GrainRefusal(StrEnum):
    """WHY a table has no usable grain evidence — one reason per failing step, so a caller (and a
    test) can tell "nobody governed a grain here" from "a grain is governed but the evidence
    contradicts itself". The two ask a human for completely different things."""

    #: No ``overlay_fact_state`` row for this table's grain fact: nobody has confirmed one. Covers the
    #: ordinary ungoverned table AND the case where a grain is confirmed on the OTHER side of a
    #: bridge — evidence about one table is not evidence about another.
    no_verified_grain_fact = "no_verified_grain_fact"
    #: Recorded but not VERIFIED (PROPOSED / REVERIFY / STALE / REJECTED / …), including the state an
    #: EXPIRED fact lands in once the poller has run.
    fact_not_verified = "fact_not_verified"
    #: VERIFIED but past ``expires_at`` at the caller's ``now`` — the window between expiry and the
    #: async poller's STALE, mirroring ``resolve_fact``'s read-time expiry guard.
    fact_expired = "fact_expired"
    #: The stored value is not an object, is empty, or fails the overlay's own governed ``GRAIN``
    #: schema.
    fact_value_invalid = "fact_value_invalid"
    #: ``is_unique`` is not true: these columns are the grain, but nothing attests that they identify
    #: at most one row. Uniqueness that holds only inside some scope needs the scoping PREDICATE to
    #: be usable, and ``FACT_VALUE_SCHEMAS[GRAIN]`` is closed (``additionalProperties: False``) with
    #: nowhere to state one — so the predicate cannot be read and the claim cannot be relied on.
    uniqueness_not_attested = "uniqueness_not_attested"
    #: A grain-key column the fact names is not in ``graph_node`` (mirrors ``b_source_grain``'s
    #: membership check): the fact describes a table shape the catalog does not have.
    grain_columns_absent = "grain_columns_absent"
    #: The second projection HAS run and names a DIFFERENT column set than the fact. Either could be
    #: the stale one, and a grain that is wider than it looks is the exact shape of a silent fan-out.
    fact_projection_disagreement = "fact_projection_disagreement"


@dataclass(frozen=True, slots=True)
class GovernedGrain:
    """A table's COMPLETE, CURRENT, VERIFIED, UNIQUE grain. ``columns`` is lowercased and sorted so a
    caller compares SETS without re-deciding case or order; ``fact_event_id`` is the confirmation the
    answer came from (``None`` only for the catalog-authoritative path, which records no event)."""

    columns: tuple[str, ...]
    fact_event_id: str | None


@dataclass(frozen=True, slots=True)
class GrainUnattested:
    """No usable grain evidence, with the reason and a human-readable detail. RETURNED, never raised:
    "this table has no attested grain" is an ordinary governed answer, not an error."""

    refusal: GrainRefusal
    detail: str = ""


def governed_grain_columns(
    conn: DbConn, catalog_source: str, schema: str, table: str
) -> tuple[str, ...]:
    """The table's governed ``GRAIN`` columns as the SECOND projection has them — scoped to that
    table, never a search.

    Two steps on purpose: the flat ``is_grain`` flag enumerates the CANDIDATES (it is the only
    per-table index there is), and C1's ``read_operational_value`` then decides which of them are
    actually GOVERNED. Enumerating on the flag alone would accept a file declaration; asking C1
    without the flag would mean guessing column names.

    An EMPTY result means "this projection grants nothing", which is not the same as "the table has
    no grain" — see the module docstring on why :func:`read_governed_grain` treats it as absence of
    corroboration rather than as counter-evidence.
    """
    rows = conn.execute(
        "SELECT lower(column_name) FROM graph_node "
        "WHERE catalog_source = %s AND lower(table_name) = %s AND kind = 'column' "
        "  AND is_grain = true",
        (catalog_source, table.strip().lower())).fetchall()
    governed = []
    for (column,) in rows:
        ref = normalize_ref(catalog_source, schema, table, column)
        read = read_operational_value(conn, ref, "is_grain")
        if read.status == _RESOLVED and read.value == _GOVERNED_TRUE:
            governed.append(column)
    return tuple(sorted(governed))


def _graph_columns(conn: DbConn, catalog_source: str, table: str) -> frozenset[str]:
    """Every column ``graph_node`` carries for this table, lowercased."""
    rows = conn.execute(
        "SELECT lower(column_name) FROM graph_node "
        "WHERE catalog_source = %s AND lower(table_name) = %s AND kind = 'column'",
        (catalog_source, table.strip().lower())).fetchall()
    return frozenset(row[0] for row in rows)


def read_governed_grain(
    conn: DbConn, catalog_source: str, table: str, *, now: datetime,
    schema: str = _FLAT_SCHEMA,
) -> GovernedGrain | GrainUnattested:
    """This table's COMPLETE, CURRENT, VERIFIED, UNIQUE grain — or why there is none.

    The fact is the authority (see the module docstring); the two-step projection read is a
    contradiction detector that can only refuse. Fail-closed on every branch: a caller holding a
    :class:`GovernedGrain` may rely on "these columns, ALL of them, identify at most one row of this
    table right now"; a caller holding anything else knows only that nobody has established that.
    """
    key = fact_key(table_ref(catalog_source, table), GRAIN)
    row = conn.execute(
        "SELECT status, value, confirmed_event_id, expires_at FROM overlay_fact_state "
        "WHERE fact_key = %s", (key,)).fetchone()
    if row is None:
        return GrainUnattested(
            GrainRefusal.no_verified_grain_fact,
            f"no grain fact is recorded for {catalog_source}.{table}: nobody has confirmed what "
            f"identifies its rows, and a file-declared is_grain flag is the uploader's claim about "
            f"their own file, not an attestation")
    status, value, confirmed_event_id, expires_at = row
    if status != _VERIFIED:
        return GrainUnattested(
            GrainRefusal.fact_not_verified,
            f"the grain fact of {catalog_source}.{table} is {status!r}, and {_VERIFIED} is the only "
            f"servable overlay status")
    if expires_at is not None and expires_at <= now:
        return GrainUnattested(
            GrainRefusal.fact_expired,
            f"the grain fact of {catalog_source}.{table} expired at {expires_at.isoformat()} (now "
            f"{now.isoformat()}): between expiry and the async poller's STALE the read model still "
            f"says VERIFIED, so the read-time guard refuses it here")
    if not isinstance(value, Mapping):
        return GrainUnattested(
            GrainRefusal.fact_value_invalid,
            f"the grain fact value of {catalog_source}.{table} is a {type(value).__name__}, not an "
            f"object")
    try:
        validate_fact_value(GRAIN, dict(value), None)
    except (FactValidationError, TypeError, ValueError) as exc:
        return GrainUnattested(
            GrainRefusal.fact_value_invalid,
            f"the grain fact value of {catalog_source}.{table} fails its governed schema ({exc})")
    if value["is_unique"] is not True:
        return GrainUnattested(
            GrainRefusal.uniqueness_not_attested,
            f"the grain fact of {catalog_source}.{table} names {tuple(value['columns'])} but does "
            f"NOT attest that they are unique: uniqueness holding only within some scope needs the "
            f"scoping predicate to be usable, and the governed grain value has nowhere to state one")

    columns = tuple(sorted({str(c).strip().lower() for c in value["columns"]}))
    absent = tuple(c for c in columns if c not in _graph_columns(conn, catalog_source, table))
    if absent:
        return GrainUnattested(
            GrainRefusal.grain_columns_absent,
            f"the grain fact of {catalog_source}.{table} names {absent}, which graph_node does not "
            f"carry: the fact describes a table shape this catalog does not have")

    projected = governed_grain_columns(conn, catalog_source, schema, table)
    if projected and projected != columns:
        return GrainUnattested(
            GrainRefusal.fact_projection_disagreement,
            f"the grain fact of {catalog_source}.{table} names {columns} but its governed column "
            f"projection says {projected}: one of the two is stale, and a grain that is wider than "
            f"it looks is the exact shape of an unnoticed fan-out")
    return GovernedGrain(columns=columns, fact_event_id=confirmed_event_id)


def load_governed_grains(
    conn: DbConn, targets: Iterable[tuple[str, str]], *, now: datetime,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Batch the grain read for a KNOWN set of tables: ``{catalog -> {table_object_ref -> columns}}``.

    ``targets`` is an iterable of ``(catalog_source, table_object_ref)`` — flattened
    ``public.<table>`` refs, the form ``graph_node`` and every bridge endpoint use. The caller names
    the tables; nothing here searches for one, so a batch cannot quietly widen into a scan.

    Only ATTESTED grains land in the result. An absent key therefore means "no usable grain evidence"
    for whatever reason :class:`GrainRefusal` recorded, which is the shape a consumer that must fail
    closed wants: it cannot accidentally read a refusal as an answer. Deduplicated, so one table
    reached by two bridges is read once — this is called once per run, and the per-column C1 reads
    inside it are exactly what a per-plan re-read would multiply.
    """
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for catalog_source, table_ref_str in sorted(set(targets)):
        table = table_ref_str.rsplit(".", 1)[-1]
        if not table:
            continue
        grain = read_governed_grain(conn, catalog_source, table, now=now)
        if isinstance(grain, GovernedGrain):
            out.setdefault(catalog_source, {})[table_ref_str] = grain.columns
    return out
