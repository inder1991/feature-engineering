"""Delivery C0 Task 3 — the immutable feature-generation metadata snapshot BUILDER (the core of C0).

Under REPEATABLE READ (pinned at connect time by C0-T2's feature-generation connection), this reads
the in-scope catalog objects' operational facts from ONE torn-free committed view via the
authority-aware adapter (``column_authority.read_column_facts`` — authority correctness lives THERE,
never re-implemented here), persists them as an immutable, hashed snapshot (migration 1006 tables,
C0-T1), and returns an in-memory :class:`SnapshotContext`. Downstream feature validation (C2–C4)
reads facts FROM that context, never re-querying live ``graph_node`` — so feature generation is
reproducible and drift-aware: a regulator can prove EXACTLY what catalog state a contract was
authored against.

Hashing is stable and canonical (``field_evidence.canonical_hash`` — ``json.dumps(sort_keys=True,
separators=(",", ":"))`` then SHA-256): an ``item_hash`` seals one consumed value/authority/
provenance; the ``content_hash`` seals the whole snapshot (sorted unique item hashes + read scope +
the pinned versions + isolation level). Same committed state ⇒ same ``content_hash``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.column_authority import (
    OperationalColumnFacts,
    logical_ref_of,
    read_column_facts,
)
from featuregen.overlay.upload.field_resolution import (
    FIELD_POLICY_VERSION,
    RESOLVER_VERSION,
)
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.source_profile import SOURCE_CAPABILITY_PROFILE_VERSION
from featuregen.overlay.upload.templates import _Col, _load_columns

# The standard governed+hint field set the snapshot captures — mirrors the fields
# ``column_authority`` actually MODELS (its ``_VALUE_COLUMN`` keys). NOTE: the field key for the
# numeric-usable operational type is ``logical_representation`` (whose value is read from the flat
# ``data_type`` column); ``declared_type`` is the separate raw-declared hint. A caller may pass a
# custom ``fields`` list; any field NOT in :data:`_KNOWN_FIELDS` RAISES (D6 — ``read_column_facts``
# would fall back to a bare ``None``/hint for it, and silently dropping it handed the caller a
# snapshot that quietly lacked a field they asked to pin).
_DEFAULT_FIELDS: tuple[str, ...] = (
    "additivity",
    "logical_representation",
    "is_grain",
    "is_as_of",
    "unit",
    "currency",
    "entity",
    "declared_type",
)
_KNOWN_FIELDS: frozenset[str] = frozenset(_DEFAULT_FIELDS)
# How a field's ``read_column_facts`` provenance maps onto the snapshot-item link columns: a
# decision-governed field's provenance is a ``*_decision_id`` (→ ``decision_event_id``); a
# fact-governed field's is a ``*_fact_event_id`` (→ ``fact_event_id``); hint fields carry neither.
_DECISION_FIELDS: frozenset[str] = frozenset({"additivity", "logical_representation"})
_FACT_FIELDS: frozenset[str] = frozenset({"is_grain", "is_as_of"})
# The GOVERNED-clearing fields (decision + fact governed): read through C1 (read_operational_value)
# so a drifted graph value seals as NON-governed. The rest (unit/currency/entity/declared_type) are
# hints by policy and stay on read_column_facts.
_C1_GOVERNED_FIELDS: frozenset[str] = _DECISION_FIELDS | _FACT_FIELDS
# C-A3c: the measure ANNOTATIONS. Not C1-governed (no decision-id column, so `read_operational_value`
# can never resolve them), but load-bearing under `field_policies`' `_SOURCE_OR_HUMAN` rule — so they
# are read through the VERIFIED-decision seam and seal their real status. Duplicated as literals
# rather than imported from `formula.measure_facts` because `operational_facts` imports this module
# (cycle); a test pins the two definitions equal.
_MEASURE_FIELDS: frozenset[str] = frozenset({"unit", "currency"})

_ISOLATION_LEVEL = "repeatable read"

# ── Task 0.6 Seam 4 (D6): the six snapshot pin KINDS. ``column_field`` is the only kind with a
# builder/comparator today; the other five are RESERVED vocabulary whose builders arrive with the
# profile/serving/temporal deliveries. No DB change: ``item_kind`` is an existing text column.
#
# STILL RESERVED AFTER RELEASE-B TASK 9, deliberately. Task 9's own rule is "use the six shared
# snapshot kinds WHEN feature/materialization consumes the same decision". Analysis now consumes
# all six — it seals them into `AnalysisExecutionIRV2` and revalidates them before every run
# (`analysis/sealed_plan.py`) — but the FEATURE side does not until the Phase-G wiring merge, and a
# pin with one producer and no consumer is the inert mechanism this programme keeps rediscovering.
# The compat-safe hash rule below is already in place, so the builders land additively: no
# migration, and not one stored snapshot re-hashed.
ITEM_KIND_COLUMN_FIELD = "column_field"
ITEM_KINDS: frozenset[str] = frozenset({
    ITEM_KIND_COLUMN_FIELD, "dataset_profile", "serving_policy", "source_selection",
    "physical_binding", "temporal_policy", "row_selection",
    # SE-2: the frozen Layer-A generation context's identity pin (one item per catalog run).
    "generation_semantic_context",
})

# Freshness reason (D6): a stored item whose kind has no comparator registered in THIS build — a
# typed refusal ("this build cannot verify that pin"), never generic drift and never a crash.
SNAPSHOT_KIND_UNSUPPORTED = "SNAPSHOT_KIND_UNSUPPORTED"


def snapshot_item_hash(item_kind: str, material: Mapping[str, object]) -> str:
    """The hash-compat rule (D6): ``column_field`` keeps the EXACT legacy computation — ``item_kind``
    EXCLUDED — so every already-stored snapshot's item/content hashes stay valid; every NEW kind
    hashes ``item_kind`` INTO its material, so two kinds with identical payloads can never collide.
    Cross-kind collision is thereby impossible without touching a single legacy row."""
    if item_kind not in ITEM_KINDS:
        raise ValueError(f"unknown snapshot item kind {item_kind!r}; register it in ITEM_KINDS")
    if item_kind == ITEM_KIND_COLUMN_FIELD:
        return canonical_hash(dict(material))
    if "item_kind" in material:
        raise ValueError("item material must not carry its own 'item_kind' key")
    return canonical_hash({**material, "item_kind": item_kind})

# The version constants captured into the snapshot HEADER, and their column mapping:
#   policy_version   ← FIELD_POLICY_VERSION              (the field-policy the reads obeyed)
#   registry_version ← RESOLVER_VERSION                  (the resolve/project registry version)
#   config_version   ← SOURCE_CAPABILITY_PROFILE_VERSION (the source-capability profile version)


class SnapshotIsolationError(RuntimeError):
    """The build was handed a connection NOT running under REPEATABLE READ — a caller bug: the
    feature-generation connection (C0-T2) pins the level at connect time, so a torn/inconsistent
    read must fail loudly here, never silently degrade to READ COMMITTED."""


# The machine-readable code the C0-T5 route surfaces when the readiness gate refuses to snapshot.
# It is a WHOLE-snapshot abort (not a per-candidate gauntlet ``RejectCode`` in feature_assist.py),
# so it lives here beside the gate that raises it; the route imports it from this module. NOTE: it
# is a "projected catalog truth is unavailable" signal — it must NEVER be reinterpreted as an
# external-data / NEEDS_EXTERNAL_VALIDATION check.
CATALOG_PROJECTION_UNAVAILABLE = "CATALOG_PROJECTION_UNAVAILABLE"

# The named projections whose materialized read models the snapshot's authority-aware adapter reads
# (``read_column_facts`` consumes the overlay read model + the field decisions the overlay
# projection resolves — there is NO separate field-decision named checkpoint today). Semantic/
# validation projections (Deliveries E/C4) EXTEND this tuple later; the gate only CHECKS projections
# that exist now, and fails CLOSED on any that don't (see :func:`check_projection_readiness`).
_LOAD_BEARING_PROJECTIONS: tuple[str, ...] = ("overlay",)


class CatalogProjectionUnavailable(RuntimeError):
    """A load-bearing catalog projection is LAGGED (its checkpoint sits behind the event head) or
    DEGRADED (poisoned) — so the snapshot (C0-T3) would seal STALE projected truth. Feature
    generation must ABORT rather than silently snapshot a lagged view. Carries the machine
    ``code`` (:data:`CATALOG_PROJECTION_UNAVAILABLE`) + a human ``detail`` so the C0-T5 route can
    surface exactly which projection was unavailable and why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _readiness_probe(conn: DbConn, projections: Sequence[str]) -> dict[str, dict]:
    """Everything :func:`check_projection_readiness` needs, in ONE round trip.

    The gate's three inputs per projection — does it have a tracked checkpoint, is it poisoned, and
    how far behind the event head is it — were four separate queries (head, exists, degraded,
    checkpoint), which is fine for the once-per-feature-run snapshot gate but not for a READ
    surface that calls the detection-only sibling on every request. Same predicates, same
    fail-closed semantics, one statement: a LEFT JOIN over the requested names so an UNTRACKED
    projection comes back as a row with a NULL checkpoint (present but unknown) rather than as a
    missing row indistinguishable from "not asked about".

    ``{name: {"checkpoint": int | None, "degraded": bool, "head": int}}``."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT p.name AS name, c.checkpoint_seq AS checkpoint, "
            "       (SELECT COALESCE(max(global_seq), 0) FROM events) AS head, "
            "       EXISTS (SELECT 1 FROM projection_degraded d "
            "               WHERE d.projection_name = p.name) AS degraded "
            "FROM unnest(%s::text[]) AS p(name) "
            "LEFT JOIN projection_checkpoints c ON c.projection_name = p.name",
            (list(projections),))
        return {r["name"]: r for r in cur.fetchall()}


def check_projection_readiness(
    conn: DbConn, *, projections: Sequence[str] = _LOAD_BEARING_PROJECTIONS
) -> dict[str, int]:
    """Readiness gate for the feature-generation snapshot (C0-T4). Over the SAME three health
    predicates ``projections.runner`` models (checkpoint tracked, not poisoned, not behind the
    event head), verify EVERY load-bearing projection is READY, and return its
    ``{projection_name: checkpoint_seq}`` watermarks (the seq the read model is current as-of) for
    pinning into the snapshot header. The predicates are read in ONE statement
    (:func:`_readiness_probe`) rather than four, because the detection-only sibling
    :func:`projection_lag_marker` runs this gate on every catalog READ.

    Fail CLOSED. Raise :class:`CatalogProjectionUnavailable` (code
    :data:`CATALOG_PROJECTION_UNAVAILABLE`) if ANY load-bearing projection:
      * has NO tracked checkpoint — unknown/untracked, never treated as caught-up (``projection_lag``
        would read a missing row as lag 0, falsely 'ready', when the head is also 0), OR
      * is DEGRADED/poisoned (a marker in ``projection_degraded``, the same ledger
        ``runner._mark_degraded`` writes — a degraded read model is untrustworthy), OR
      * is LAGGED — its checkpoint sits below the event head (= the resolve.py lag-guard posture: a
        just-committed event the projection has not yet applied would make the read model STALE)."""
    probe = _readiness_probe(conn, projections)   # ONE round trip for the whole gate
    watermarks: dict[str, int] = {}
    for name in projections:
        row = probe.get(name)
        head = 0 if row is None else int(row["head"])   # COALESCE(max(global_seq), 0) FROM events
        if row is None or row["checkpoint"] is None:
            raise CatalogProjectionUnavailable(
                CATALOG_PROJECTION_UNAVAILABLE,
                f"load-bearing projection {name!r} has no tracked checkpoint (untracked/unknown)",
            )
        if row["degraded"]:
            raise CatalogProjectionUnavailable(
                CATALOG_PROJECTION_UNAVAILABLE,
                f"load-bearing projection {name!r} is DEGRADED (poisoned read model)",
            )
        checkpoint = int(row["checkpoint"])
        if checkpoint < head:
            raise CatalogProjectionUnavailable(
                CATALOG_PROJECTION_UNAVAILABLE,
                f"load-bearing projection {name!r} is LAGGED: checkpoint {checkpoint} "
                f"< event head {head}",
            )
        watermarks[name] = checkpoint
    return watermarks


@dataclass(frozen=True, slots=True)
class ProjectionLagV1:
    """A typed, honest marker that a READ was served while a load-bearing projection was behind.

    Semantic Task 6, scoped deliberately narrow: DETECTION AND DISCLOSURE, not a new refusal. The
    feature path aborts on lag because it SEALS a snapshot that must be re-executable; a catalog
    READ has no such contract — refusing to show a column because a projection is three events
    behind would be a worse answer than showing it with the lag named. What must never happen is
    the silent version: serving a newer semantic value under an older context hash as though the
    two agreed.

    ``code`` is the existing :data:`CATALOG_PROJECTION_UNAVAILABLE`; ``detail`` is the gate's own
    sentence, naming which projection and why. Nothing here is re-derived — the marker is exactly
    what :func:`check_projection_readiness` already computes."""

    status: str
    code: str
    detail: str

    def as_dict(self) -> dict:
        return {"status": self.status, "code": self.code, "detail": self.detail}


#: The marker a READY read carries. Present ALWAYS (never omitted on the happy path): an absent key
#: is indistinguishable from an older client that never had one, so "we checked and it was fine"
#: has to be said out loud for "we checked and it was not" to mean anything.
PROJECTION_READY = ProjectionLagV1(status="ready", code="", detail="")


def projection_lag_marker(
    conn: DbConn, *, projections: Sequence[str] = _LOAD_BEARING_PROJECTIONS
) -> ProjectionLagV1:
    """The DETECTION-ONLY sibling of :func:`check_projection_readiness`: same gate, same verdict,
    reported instead of raised.

    Reuses the readiness gate verbatim (never re-implements projection health) so a read surface
    and the feature path can never disagree about whether the catalog is caught up. Returns
    :data:`PROJECTION_READY` when it is, and a ``lagged`` marker carrying the gate's own code and
    detail when it is not."""
    try:
        check_projection_readiness(conn, projections=projections)
    except CatalogProjectionUnavailable as exc:
        return ProjectionLagV1(status="lagged", code=exc.code, detail=exc.detail)
    return PROJECTION_READY


@dataclass(frozen=True, slots=True)
class SnapshotItem:
    """One persisted snapshot row, held in memory so the context serves facts WITHOUT a re-query.

    Scalars only (no dict fields) so the frozen dataclass stays hashable/tuple-friendly."""

    catalog_source: str
    graph_ref: str
    logical_ref: str | None
    physical_ref: str | None
    item_kind: str
    field_or_fact_type: str
    value: str | None
    authority: str
    provenance: str | None
    # The sealed C1 operational status (operational_facts.OperationalValue.status). ``authority`` is
    # "governed" IFF ``status == "resolved"`` (a hash-verified load-bearing value); every other status
    # (no_decision/no_value/not_operational/conflict/fork/hash_mismatch/retired) seals as a hint, so a
    # DRIFTED graph value can never seal as governed. Hint-by-policy fields seal "not_operational".
    status: str
    decision_event_id: str | None
    fact_event_id: str | None
    item_hash: str

    def facts(self) -> OperationalColumnFacts:
        return OperationalColumnFacts(
            value=self.value, authority=self.authority, provenance=self.provenance
        )


@dataclass(frozen=True, slots=True)
class SnapshotContext:
    """An IMMUTABLE handle to a persisted snapshot. ``facts(...)`` is served from ``_index`` (built
    once at snapshot time) so C2–C4 read the frozen snapshot, NEVER live ``graph_node``."""

    snapshot_id: str
    content_hash: str
    generation_run_id: str
    _items: tuple[SnapshotItem, ...]
    _index: Mapping[tuple[str, str, str], OperationalColumnFacts]

    def items(self) -> tuple[SnapshotItem, ...]:
        return self._items

    def facts(
        self, catalog_source: str, object_ref: str, field: str
    ) -> OperationalColumnFacts | None:
        """The snapshotted facts for one ``(catalog_source, object_ref, field)`` — from memory, no
        DB hit. ``None`` when that field/ref was not captured (skipped or not requested)."""
        return self._index.get((catalog_source, object_ref, field))


@dataclass(frozen=True, slots=True)
class SnapshotFreshness:
    status: str
    reason: str | None
    current_content_hash: str | None


# ── Delivery H3b — the C0-snapshot column adapter for the planner ───────────────────────────────────
# The planner's candidate discovery reads catalog columns via ``templates._load_columns`` — a LIVE
# ``graph_node`` read. On the feature-generation path (REPEATABLE READ, C0-T2) the read is instead
# served from THIS frozen capture, so the planner and downstream feature validation (C2–C4) observe
# ONE torn-free committed catalog state — the SAME view the metadata :class:`SnapshotContext` above
# seals. Risk-4: the capture is produced by the IDENTICAL ``_load_columns`` SELECT, so the ``_Col``
# set + every binding-relevant attribute is byte-equal to a live read for the captured state; the
# ingredient bindings, the sorted refs and the hashed material are unchanged, and ``physical_plan_id``
# stays byte-identical. Additive: with no capture the planner keeps the live read (see
# ``planner.declarations.build_compiler_context``).
@dataclass(frozen=True, slots=True)
class ColumnSnapshot:
    """A frozen, in-memory capture of each in-scope catalog's read-scoped ``_load_columns`` result,
    taken ONCE (ideally on the REPEATABLE READ feature-generation connection). ``columns(...)`` serves
    those ``_Col`` rows from memory, so a ``graph_node`` mutation AFTER capture NEVER leaks into plan
    discovery — the planner reads the snapshot, not live. Read scope (``roles``/``allowed_sensitivities``)
    is baked in at capture and re-asserted on read (fail closed)."""

    roles: tuple[str, ...]
    _columns_by_catalog: Mapping[str, tuple[_Col, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_columns_by_catalog",
            MappingProxyType({k: tuple(v) for k, v in self._columns_by_catalog.items()}))

    def columns(self, catalog_source: str, roles: Iterable[str]) -> list[_Col]:
        """The frozen read-scoped columns for ``catalog_source`` — the drop-in replacement for a live
        ``_load_columns(conn, catalog_source, roles)``. Fail CLOSED when the requested read scope differs
        from the capture's: a different ``roles`` set would see a different sensitivity-filtered column
        set, so serving the wrong scope would be a leak / a hash divergence. A catalog not in the
        capture returns ``[]`` (it was outside the snapshot's scope)."""
        if tuple(roles) != self.roles:
            raise ValueError(
                f"ColumnSnapshot captured for roles {self.roles!r} cannot serve "
                f"roles {tuple(roles)!r} — read scope must match the capture")
        return list(self._columns_by_catalog.get(catalog_source, ()))


def capture_column_snapshot(
    conn: DbConn, catalogs: Iterable[str], roles: Iterable[str] = ()
) -> ColumnSnapshot:
    """Capture the planner's frozen C0 column source: run the IDENTICAL read-scoped ``_load_columns``
    SELECT once per catalog and freeze the result. Meant to run on the REPEATABLE READ feature-
    generation connection (C0-T2) so the capture and the C0 metadata snapshot observe ONE torn-free
    committed ``graph_node`` view. Because it delegates to ``_load_columns`` itself, the captured
    ``_Col`` rows are byte-identical to a live read for the same state (Risk-4)."""
    roles = tuple(roles)
    return ColumnSnapshot(
        roles=roles,
        _columns_by_catalog={src: tuple(_load_columns(conn, src, roles)) for src in catalogs})


def _assert_repeatable_read(conn: DbConn) -> str:
    """Assert (before the first catalog read) the connection runs under REPEATABLE READ; return the
    live isolation string. ``SHOW`` is the first statement, so it starts the tx at the pinned level
    (C0-T2 sets ``isolation_level`` before any query); a non-isolated conn reports its real level."""
    level = conn.execute("SHOW transaction_isolation").fetchone()[0]
    if level != _ISOLATION_LEVEL:
        raise SnapshotIsolationError(
            f"feature-generation snapshot requires a REPEATABLE READ connection, got {level!r}; "
            "use the C0-T2 feature-generation connection (isolation pinned at connect time)"
        )
    return level


def ensure_generation_run(
    conn: DbConn,
    generation_run_id: str,
    actor: dict,
    flags: dict,
    *,
    intent_id: str | None = None,
) -> None:
    """Ensure the durable generation-run manifest exists (idempotent). ``actor`` is NOT NULL in the
    schema; a missing actor defaults to ``{}``. ON CONFLICT DO NOTHING keeps re-builds under one
    run harmless (the manifest may accrete context; it is NOT write-once)."""
    row = conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (generation_run_id) DO UPDATE "
        "SET intent_id=COALESCE(feature_generation_run.intent_id, EXCLUDED.intent_id) "
        "WHERE feature_generation_run.intent_id IS NULL "
        "OR EXCLUDED.intent_id IS NULL "
        "OR feature_generation_run.intent_id=EXCLUDED.intent_id "
        "RETURNING generation_run_id",
        (generation_run_id, intent_id, Jsonb(actor), Jsonb(flags)),
    ).fetchone()
    if row is None:
        raise ValueError("generation run is already bound to a different intent")


def _physical_ref(conn: DbConn, catalog_source: str, logical_ref: str) -> str | None:
    """A light read of the object's declared (pre-flatten) schema for provenance. Keyed by the same
    PUBLIC-flattened graph_node object_ref ``read_column_facts`` uses. NULL when unavailable — never
    block the snapshot on it."""
    _source, _schema, table, column = parse_ref(logical_ref)
    flat_ref = ".".join(["public", table, *([column] if column else [])])
    row = conn.execute(
        "SELECT schema_name FROM graph_node "
        "WHERE catalog_source = %s AND lower(object_ref) = %s AND kind = 'column'",
        (catalog_source, flat_ref.lower()),
    ).fetchone()
    return row[0] if row is not None and row[0] else None


def _build_item(
    conn: DbConn, catalog_source: str, object_ref: str, field: str
) -> SnapshotItem:
    """Read one field's operational facts and seal them into an item. ``item_hash`` = canonical
    SHA-256 of the exact consumed ``{catalog_source, graph_ref, field, value, authority, provenance,
    status}`` — the sealed ``status`` covers the C1 authority, so a DRIFTED graph value seals as
    NON-governed, never as governed.

    A GOVERNED field (:data:`_C1_GOVERNED_FIELDS`) is read through C1 (``read_operational_value``) —
    the tamper-gated read (fork / hash-verify vs the approved decision / projection-health): only a
    hash-verified ``status == "resolved"`` head seals ``authority="governed"`` with its value +
    provenance link; a drifted / forked / hash-mismatched read seals ``authority="hint"`` (value
    None, provenance dropped). A projection-lagged read ABORTS the whole snapshot (the same abort the
    up-front :func:`check_projection_readiness` gate raises) — never seal a stale projected value. The
    hint-by-policy fields (unit/currency/entity/declared_type) stay on ``read_column_facts``."""
    logical_ref = logical_ref_of(conn, catalog_source, object_ref)
    facts = read_column_facts(conn, logical_ref, field)
    if field in _C1_GOVERNED_FIELDS:
        # Function-local import: operational_facts imports check_projection_readiness /
        # CatalogProjectionUnavailable from THIS module, so a module-top import would be a cycle.
        from featuregen.overlay.upload.operational_facts import read_operational_value
        ov = read_operational_value(conn, logical_ref, field)
        if ov.status == "projection_unavailable":
            raise CatalogProjectionUnavailable(
                CATALOG_PROJECTION_UNAVAILABLE,
                ov.conflict_status or "load-bearing catalog projection unavailable")
        governed = ov.status == "resolved"
        value = ov.value
        authority = "governed" if governed else "hint"
        status = ov.status
        provenance = facts.provenance if governed else None
    elif field in _MEASURE_FIELDS:
        # C-A3c. These used to seal `status="not_operational"` UNCONDITIONALLY — "hint by policy —
        # never governed" — which is a statement about `read_column_facts`, not about whether a
        # load-bearing decision exists. It does: `field_policies` gives unit/currency
        # `operational_rule=_SOURCE_OR_HUMAN`, so a source-ATTESTED or human-CONFIRMED decision IS
        # load-bearing. Sealing "not_operational" over it made every downstream reader blank the
        # value, so a per-row-currency operand looked NON-MONETARY and the mixed-currency
        # protection never engaged. The VALUE was already in the sealed item; only the status lied.
        #
        # `authority` deliberately stays "hint": that is `column_authority`'s classification of the
        # field and it is unchanged. What moves is `status`, which is the axis every fact reader
        # actually gates on. A measure item may therefore now be `authority="hint"` with
        # `status="resolved"` — a combination the governed branch above cannot produce, and which
        # is exactly what a verified measure ANNOTATION is.
        from featuregen.overlay.upload.operational_facts import read_verified_decision_value
        ov = read_verified_decision_value(conn, logical_ref, field)
        if ov.status == "projection_unavailable":
            raise CatalogProjectionUnavailable(
                CATALOG_PROJECTION_UNAVAILABLE,
                ov.conflict_status or "load-bearing catalog projection unavailable")
        verified = ov.status == "resolved"
        value = ov.value if verified else facts.value
        authority = facts.authority
        status = ov.status
        provenance = facts.provenance
    else:
        value = facts.value
        authority = facts.authority
        status = "not_operational"   # hint by policy — never governed
        provenance = facts.provenance
    decision_event_id = provenance if field in _DECISION_FIELDS else None
    fact_event_id = provenance if field in _FACT_FIELDS else None
    item_hash = snapshot_item_hash(
        ITEM_KIND_COLUMN_FIELD,
        {
            "catalog_source": catalog_source,
            "graph_ref": object_ref,
            "field": field,
            "value": value,
            "authority": authority,
            "provenance": provenance,
            "status": status,
        },
    )
    return SnapshotItem(
        catalog_source=catalog_source,
        graph_ref=object_ref,
        logical_ref=logical_ref,
        physical_ref=_physical_ref(conn, catalog_source, logical_ref),
        item_kind=ITEM_KIND_COLUMN_FIELD,
        field_or_fact_type=field,
        value=value,
        authority=authority,
        provenance=provenance,
        status=status,
        decision_event_id=decision_event_id,
        fact_event_id=fact_event_id,
        item_hash=item_hash,
    )


# item_kind -> the CURRENT-state item rebuilder ``compare_snapshot_to_current`` dispatches on (D6).
# Only ``column_field`` has one today; the five reserved kinds gain theirs with their builders — a
# stored kind absent here is the typed :data:`SNAPSHOT_KIND_UNSUPPORTED` refusal.
def _compare_generation_context(conn, catalog_source, graph_ref, field):
    """SE-2's freshness dispatch — imported lazily (the context module imports THIS one for the
    item shape, so a module-level import here would cycle)."""
    from featuregen.overlay.upload.generation_semantic_context import (
        compare_generation_context_item,
    )

    return compare_generation_context_item(conn, catalog_source, graph_ref, field)


_KIND_COMPARATORS = {ITEM_KIND_COLUMN_FIELD: _build_item,
                     "generation_semantic_context": _compare_generation_context}


def build_metadata_snapshot(
    conn: DbConn,
    *,
    generation_run_id: str,
    refs: Sequence[tuple[str, str]],
    read_scope_hash: str,
    actor: dict | None = None,
    flags: dict | None = None,
    fields: Sequence[str] | None = None,
    extra_items: Sequence[SnapshotItem] = (),
) -> SnapshotContext:
    """Persist an immutable, hashed snapshot of the in-scope catalog state and return a
    :class:`SnapshotContext` (see module docstring).

    ``refs`` is a sequence of ``(catalog_source, object_ref)``; ``fields`` defaults to the standard
    governed+hint set (:data:`_DEFAULT_FIELDS`). Requesting a field ``read_column_facts`` does not
    MODEL RAISES ``ValueError`` naming it (D6 — never silently dropped, never fabricated). MUST be
    given a REPEATABLE READ connection (raises :class:`SnapshotIsolationError` otherwise)."""
    requested = tuple(fields) if fields is not None else _DEFAULT_FIELDS
    unknown_fields = [f for f in requested if f not in _KNOWN_FIELDS]
    if unknown_fields:   # pure input validation — refuse BEFORE any read or write
        raise ValueError(
            f"unknown snapshot field(s) {unknown_fields!r}: read_column_facts does not model them "
            f"(known: {sorted(_KNOWN_FIELDS)})")
    isolation_level = _assert_repeatable_read(conn)   # before the first catalog read
    # Readiness gate (C0-T4): a load-bearing projection that is LAGGED or DEGRADED would make the
    # authority adapter read STALE projected truth. Refuse BEFORE writing anything (no run manifest,
    # no snapshot) and let CatalogProjectionUnavailable propagate to the C0-T5 route. The returned
    # watermarks pin the checkpoint the read model is current as-of into the snapshot header.
    projection_watermarks = check_projection_readiness(conn)
    ensure_generation_run(conn, generation_run_id, actor or {}, flags or {})

    selected = list(requested)

    items: list[SnapshotItem] = []
    seen_hashes: set[str] = set()
    for catalog_source, object_ref in refs:
        for field in selected:
            item = _build_item(conn, catalog_source, object_ref, field)
            if item.item_hash in seen_hashes:
                continue   # an identical item appears once (mirrors the DB UNIQUE dedup)
            seen_hashes.add(item.item_hash)
            items.append(item)
    # SE-2: caller-supplied pre-built items (the generation-context identity pin) join the SAME
    # sealed set — hashed into content_hash below, verified by their registered kind comparator.
    # Absent extra items, this loop is a no-op: byte-identical to the pre-SE-2 snapshot.
    for item in extra_items:
        if item.item_kind not in ITEM_KINDS:
            raise ValueError(f"unknown extra snapshot item kind {item.item_kind!r}")
        if item.item_hash in seen_hashes:
            continue
        seen_hashes.add(item.item_hash)
        items.append(item)

    content_hash = canonical_hash(
        {
            "item_hashes": sorted(seen_hashes),
            "read_scope_hash": read_scope_hash,
            "policy_version": FIELD_POLICY_VERSION,
            "registry_version": RESOLVER_VERSION,
            "config_version": SOURCE_CAPABILITY_PROFILE_VERSION,
            "isolation_level": isolation_level,
        }
    )

    snapshot_id = mint_id("snap")
    # MF-1: INSERT ALL ITEMS FIRST, then the header LAST. The BEFORE INSERT seal on
    # catalog_metadata_snapshot_item (migration 1006) refuses any item once a header row exists for its
    # snapshot_id, so the header write is what SEALS the set — during the build the header is absent, so
    # the items are allowed. The item -> header FK is DEFERRABLE INITIALLY DEFERRED, so the items may
    # reference the not-yet-inserted header within this transaction (validated at COMMIT). content_hash
    # was computed over the item hashes above — before either write — so it still seals the same set.
    for item in items:
        conn.execute(
            "INSERT INTO catalog_metadata_snapshot_item "
            "(snapshot_id, catalog_source, graph_ref, logical_ref, physical_ref, item_kind, "
            "field_or_fact_type, value_json, authority_json, decision_event_id, fact_key, "
            "fact_event_id, item_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (snapshot_id, item_hash) DO NOTHING",
            (snapshot_id, item.catalog_source, item.graph_ref, item.logical_ref,
             item.physical_ref, item.item_kind, item.field_or_fact_type,
             Jsonb({"value": item.value}),
             Jsonb({"authority": item.authority, "provenance": item.provenance,
                    "status": item.status}),
             item.decision_event_id, None, item.fact_event_id, item.item_hash),
        )
    conn.execute(   # the header LAST — this write seals the item set (MF-1); item_count stamps its size
        "INSERT INTO catalog_metadata_snapshot "
        "(snapshot_id, generation_run_id, read_scope_hash, isolation_level, projection_watermarks, "
        "policy_version, registry_version, config_version, content_hash, item_count) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (snapshot_id, generation_run_id, read_scope_hash, isolation_level,
         Jsonb(projection_watermarks),
         FIELD_POLICY_VERSION, RESOLVER_VERSION, SOURCE_CAPABILITY_PROFILE_VERSION, content_hash,
         len(items)),
    )

    index = {
        (item.catalog_source, item.graph_ref, item.field_or_fact_type): item.facts()
        for item in items
    }
    return SnapshotContext(
        snapshot_id=snapshot_id,
        content_hash=content_hash,
        generation_run_id=generation_run_id,
        _items=tuple(items),
        _index=MappingProxyType(index),
    )


def compare_snapshot_to_current(conn: DbConn, snapshot_id: str) -> SnapshotFreshness:
    """Re-derive a sealed snapshot's items under current readers and classify catalog drift."""
    header = conn.execute(
        "SELECT read_scope_hash,isolation_level,policy_version,registry_version,config_version,"
        "content_hash,item_count FROM catalog_metadata_snapshot WHERE snapshot_id=%s",
        (snapshot_id,),
    ).fetchone()
    if header is None:
        return SnapshotFreshness("unverifiable", "SNAPSHOT_MISSING", None)
    (
        read_scope_hash,
        isolation_level,
        policy_version,
        registry_version,
        config_version,
        stored_content_hash,
        item_count,
    ) = header
    if (
        policy_version != FIELD_POLICY_VERSION
        or registry_version != RESOLVER_VERSION
        or config_version != SOURCE_CAPABILITY_PROFILE_VERSION
    ):
        return SnapshotFreshness("drifted", "SNAPSHOT_VERSION_DRIFT", None)
    rows = conn.execute(
        "SELECT catalog_source,graph_ref,field_or_fact_type,item_kind,item_hash "
        "FROM catalog_metadata_snapshot_item WHERE snapshot_id=%s "
        "ORDER BY catalog_source,graph_ref,field_or_fact_type,item_hash",
        (snapshot_id,),
    ).fetchall()
    if len(rows) != item_count:
        return SnapshotFreshness("unverifiable", "SNAPSHOT_ITEM_COUNT_MISMATCH", None)
    current_hashes: list[str] = []
    for catalog_source, graph_ref, field, item_kind, stored_item_hash in rows:
        # D6 kind dispatch: a stored kind THIS build has no comparator for is a typed refusal —
        # "cannot verify that pin" — never reported as drift and never allowed to crash.
        comparator = _KIND_COMPARATORS.get(item_kind)
        if comparator is None:
            return SnapshotFreshness("unverifiable", SNAPSHOT_KIND_UNSUPPORTED, None)
        try:
            current = comparator(conn, catalog_source, graph_ref, field)
        except Exception:
            return SnapshotFreshness("unverifiable", "CURRENT_CATALOG_READ_FAILED", None)
        current_hashes.append(current.item_hash)
        if current.item_hash != stored_item_hash:
            return SnapshotFreshness("drifted", "SNAPSHOT_ITEM_DRIFT", None)
    current_content_hash = canonical_hash({
        "item_hashes": sorted(current_hashes),
        "read_scope_hash": read_scope_hash,
        "policy_version": FIELD_POLICY_VERSION,
        "registry_version": RESOLVER_VERSION,
        "config_version": SOURCE_CAPABILITY_PROFILE_VERSION,
        "isolation_level": isolation_level,
    })
    if current_content_hash != stored_content_hash:
        return SnapshotFreshness(
            "unverifiable", "SNAPSHOT_CONTENT_HASH_MISMATCH", current_content_hash)
    return SnapshotFreshness("current", None, current_content_hash)
