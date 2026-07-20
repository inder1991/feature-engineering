"""Entity layer — the cross-domain anchor.

A feature is *the customer's* feature, not *a deposits* feature, only if the platform knows which
columns across different catalogs denote the same business entity. Columns carry a declared `entity`
tag (`Customer`, `Account`); this module reads that tag out of the graph to expose entity membership
**across catalogs** — the raw material for cross-source join paths and cross-domain candidate gathering.

Entity membership is derived from `graph_node.entity`; human-confirmed suggestions are persisted in
`entity_suggestion` (advisory until applied). Reads are read-scoped (an entity key column may be sensitive).

WIRED: `find_cross_catalog_path` authors cross-catalog join paths (`contract/author.py`); the
suggest→confirm flow (`suggest_entities`/`apply_entity_suggestion`) is exposed at `/entity/*`.

Delivery E4 — legacy migration. A legacy `entity_suggestion.status='applied'` tag is a
human-confirmed but UNGOVERNED entity that predates E1/E3. It is KEPT readable but DEMOTED to
`legacy_file_declared` authority: a governed VERIFIED `entity_assignment` (E1/E3) ALWAYS wins the
effective `graph_node.entity`. A NEW apply now PROPOSES a governed `entity_assignment` fact (E1
propose→confirm, owner-or-admin four-eyes) instead of the retired legacy `status='applied'` UPDATE;
`backfill_legacy_entity_assignments` proposes review-only DRAFT facts for pre-existing applied rows.
`build_graph` still re-applies legacy 'applied' tags (as `legacy_file_declared`) so they survive
re-upload, but never over a governed value (governed wins).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.facts import ENTITY_ASSIGNMENT
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.overlay.upload.enrich_llm import audited_enrich_call
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities

# A blank / unknown / list-stringified entity suggestion is not applied.
_KNOWN_ENTITYISH = 40   # max plausible entity-name length

# ── E4 authority labels for the effective `graph_node.entity` (a consumer distinguishes them) ──
GOVERNED_ENTITY = "governed"                    # a VERIFIED entity_assignment (E1/E3) governs it
LEGACY_FILE_DECLARED = "legacy_file_declared"   # a legacy 'applied' suggestion — non-governed
FILE_DECLARED = "file_declared"                 # the raw file-declared entity (no apply, no govern)


@dataclass(frozen=True, slots=True)
class EntityColumn:
    entity: str
    catalog_source: str
    table: str
    object_ref: str


def list_entities(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT entity FROM graph_node WHERE entity IS NOT NULL ORDER BY entity").fetchall()
    return [r[0] for r in rows]


def entity_of(conn, catalog_source: str, object_ref: str) -> str | None:
    row = conn.execute(
        "SELECT entity FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (catalog_source, object_ref)).fetchone()
    return row[0] if row else None


def entity_key_columns(conn, entity: str, *, roles: Iterable[str] = ()) -> list[EntityColumn]:
    """Every column that denotes `entity`, ACROSS all catalogs (read-scoped). These are the keys a
    cross-source join hangs on — e.g. deposits.cust_ref and cards.cust_id both → Customer."""
    rows = conn.execute(
        "SELECT catalog_source, table_name, object_ref FROM graph_node "
        "WHERE kind = 'column' AND entity = %s "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
        "ORDER BY catalog_source, object_ref",
        (entity, allowed_sensitivities(roles))).fetchall()
    return [EntityColumn(entity=entity, catalog_source=r[0], table=r[1], object_ref=r[2])
            for r in rows]


@dataclass(frozen=True, slots=True)
class EntityBridge:
    entity: str
    from_ref: str          # the from-table's entity key column
    to_ref: str            # the to-table's entity key column


def _table_entity_keys(conn, catalog_source: str, table: str,
                       roles: Iterable[str]) -> dict[str, str]:
    rows = conn.execute(
        "SELECT entity, object_ref FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s AND table_name = %s AND entity IS NOT NULL "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s))",
        (catalog_source, table, allowed_sensitivities(roles))).fetchall()
    return {r[0]: r[1] for r in rows}


def cross_join_via_entity(conn, from_source: str, from_table: str, to_source: str, to_table: str, *,
                          roles: Iterable[str] = ()) -> EntityBridge | None:
    """Bridge two tables in (possibly) different catalogs via a shared entity — the cross-domain join
    primitive. Returns the entity + the key columns to join on, or None if they share no entity. The
    link is declared/entity-resolved, NOT value-verified (no DB), so callers surface it for human
    confirmation before a feature that uses it is registered."""
    from_keys = _table_entity_keys(conn, from_source, from_table, roles)
    to_keys = _table_entity_keys(conn, to_source, to_table, roles)
    for entity, from_ref in from_keys.items():
        if entity in to_keys:
            return EntityBridge(entity=entity, from_ref=from_ref, to_ref=to_keys[entity])
    return None


def suggest_entity(conn, client, *, table: str, column: str, type: str, concept: str | None = None,
                   actor=None, dispatch_audit: DispatchAuditContext | None = None) -> str | None:
    """ADVISORY: ask the LLM which business entity an id-like column denotes (Customer, Account, ...),
    from metadata only (name/type/concept — no data). A SUGGESTION for a human to confirm before it's
    written as the column's entity — never auto-applied (a wrong entity mis-links catalogs). Returns
    the suggested entity name, or None on failure / empty / implausible output.

    ``dispatch_audit`` (C5-T5): the caller's ingestion-attribution context (``suggest_entities``
    builds one per column when it has a run id); ``None`` is byte-identical to today."""
    raw = audited_enrich_call(
        conn, client, task="overlay.enrich.entity", prompt_id="overlay_entity_v1",
        schema_id="overlay_entity",
        catalog_metadata={"table": table, "column": column, "type": type, "concept": concept or ""},
        out_key="entity",
        instruction="Which business entity (e.g. Customer, Account) does this id-like column denote, "
                    "if any? Reply with the entity name only, or empty if it denotes none.",
        actor=actor, dispatch_audit=dispatch_audit)
    if not raw or len(raw) > _KNOWN_ENTITYISH or "\n" in raw or raw.startswith("["):
        return None
    return raw


_ID_SUFFIXES = ("_id", "_ref", "_key", "_no", "_num", "_code", "_fk")
_NON_ID_TYPES = ("numeric", "float", "double", "decimal", "boolean", "bool", "date", "timestamp",
                 "time", "json", "jsonb")


def _is_id_like(column_name: str, data_type: str | None) -> bool:
    n = (column_name or "").lower()
    if not (n == "id" or n.endswith(_ID_SUFFIXES)):
        return False
    return (data_type or "").lower() not in _NON_ID_TYPES   # ids are int/text/uuid, never numeric/ts


@dataclass(frozen=True, slots=True)
class EntitySuggestion:
    object_ref: str
    table: str
    column: str
    suggested_entity: str
    status: str
    # E4: the authority a consumer should attribute to this suggestion. An 'applied' row is a legacy,
    # NON-GOVERNED tag → `legacy_file_declared`; a 'pending'/'dismissed' row is not applied → None.
    authority: str | None = None


def suggest_entities(conn, client, catalog_source: str, *, roles: Iterable[str] = (),
                     actor=None, ingestion_run_id: str | None = None) -> int:
    """For each id-like column in this catalog that has NO entity yet, ask the LLM (advisory) which
    entity it denotes and store a PENDING suggestion — never auto-applied. Read-scoped. On-demand
    (NOT in the ingest hot path). Returns the number of suggestions written. Re-running refreshes
    pending rows but never clobbers an already-applied one.

    ``ingestion_run_id`` (C5-T5): when a caller runs this in service of an ingestion run, each
    per-column dispatch is pre-audited + attributed to that run and the column subject (stage
    ``entity``). The on-demand API route passes nothing — ``None`` dispatches unattributed,
    byte-for-byte as before."""
    cols = conn.execute(
        "SELECT object_ref, table_name, column_name, data_type, concept FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s AND entity IS NULL "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s))",
        (catalog_source, allowed_sensitivities(roles))).fetchall()
    written = 0
    for object_ref, table, column, data_type, concept in cols:
        if not _is_id_like(column, data_type):
            continue
        ctx = None
        if ingestion_run_id is not None:
            ctx = DispatchAuditContext(
                ingestion_run_id=ingestion_run_id, stage="entity",
                subjects=({"catalog_source": catalog_source, "object_ref": object_ref,
                           "logical_ref": normalize_ref(catalog_source, None, table, column),
                           "field_names": [column]},))
        suggested = suggest_entity(conn, client, table=table, column=column, type=data_type,
                                   concept=concept, actor=actor, dispatch_audit=ctx)
        if not suggested:
            continue
        conn.execute(
            "INSERT INTO entity_suggestion (catalog_source, object_ref, table_name, column_name, "
            "suggested_entity, status) VALUES (%s, %s, %s, %s, %s, 'pending') "
            "ON CONFLICT (catalog_source, object_ref) DO UPDATE SET "
            "suggested_entity = EXCLUDED.suggested_entity "
            "WHERE entity_suggestion.status <> 'applied'",   # don't disturb a confirmed tag
            (catalog_source, object_ref, table, column, suggested))
        written += 1
    return written


def list_entity_suggestions(conn, catalog_source: str, *, status: str = "pending",
                            roles: Iterable[str] = ()) -> list[EntitySuggestion]:
    """Entity suggestions for a catalog (default PENDING), READ-SCOPED: a suggestion on a column whose
    sensitivity the caller's roles can't see is withheld (consistent with search/graph). E4: an
    'applied' row is LABELLED `legacy_file_declared` (a legacy, non-governed tag — a governed VERIFIED
    entity_assignment wins the effective entity), so a consumer can tell it apart from a governed one."""
    rows = conn.execute(
        "SELECT s.object_ref, s.table_name, s.column_name, s.suggested_entity, s.status "
        "FROM entity_suggestion s "
        "LEFT JOIN graph_node n ON n.object_ref = s.object_ref AND n.catalog_source = s.catalog_source "
        "WHERE s.catalog_source = %s AND s.status = %s "
        "  AND (n.sensitivity IS NULL OR n.sensitivity = ANY(%s)) ORDER BY s.object_ref",
        (catalog_source, status, allowed_sensitivities(roles))).fetchall()
    return [EntitySuggestion(r[0], r[1], r[2], r[3], r[4],
                             authority=LEGACY_FILE_DECLARED if r[4] == "applied" else None)
            for r in rows]


@dataclass(frozen=True, slots=True)
class EntityRead:
    """The effective business entity for a column node + the AUTHORITY behind it (E4). A consumer
    keys governance decisions off `authority`: `governed` (a VERIFIED entity_assignment) is the ONLY
    governed value; `legacy_file_declared` / `file_declared` are non-governed display context."""
    entity: str | None
    authority: str | None   # GOVERNED_ENTITY | LEGACY_FILE_DECLARED | FILE_DECLARED | None


def effective_entity(conn, catalog_source: str, object_ref: str) -> EntityRead:
    """Read the effective `graph_node.entity` WITH its authority (E4). A governed VERIFIED
    entity_assignment WINS (`entity_status='VERIFIED'` → `governed`); else a legacy 'applied'
    suggestion labels the tag `legacy_file_declared`; else a raw file-declared entity is
    `file_declared`; else `(None, None)`. This is the read that lets a consumer never mistake a
    legacy tag for a governed one."""
    row = conn.execute(
        "SELECT entity, entity_status FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s",
        (catalog_source, object_ref)).fetchone()
    if row is None:
        return EntityRead(None, None)
    entity, entity_status = row
    if entity is not None and entity_status == "VERIFIED":
        return EntityRead(entity, GOVERNED_ENTITY)          # governed WINS
    legacy = conn.execute(
        "SELECT 1 FROM entity_suggestion "
        "WHERE catalog_source = %s AND object_ref = %s AND status = 'applied'",
        (catalog_source, object_ref)).fetchone()
    if legacy is not None:
        return EntityRead(entity, LEGACY_FILE_DECLARED)     # kept readable, NON-governed
    if entity is not None:
        return EntityRead(entity, FILE_DECLARED)
    return EntityRead(None, None)


def _column_ref(conn, catalog_source: str, object_ref: str, table: str,
                column: str) -> CatalogObjectRef:
    """The E1 `CatalogObjectRef` for a column's `entity_assignment` fact. The real (pre-flatten)
    schema comes from `graph_node.schema_name` (`build_graph` stores it); a schema-less technical
    upload falls back to 'public' (the flattened graph scope). The fact_key + E3 projection key on
    table+column (schema-independent), so the fallback only affects owner routing (owner-or-admin)."""
    schema_row = conn.execute(
        "SELECT schema_name FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (catalog_source, object_ref)).fetchone()
    schema = schema_row[0] if schema_row and schema_row[0] else "public"
    return CatalogObjectRef(catalog_source=catalog_source, object_kind="column",
                            schema=schema, table=table, column=column)


def _governed_entity_id(suggested_entity: str) -> str:
    """Map a legacy suggested entity onto the CLOSED `known_entities()` vocabulary, case-insensitively
    (a legacy LLM tag may be 'Customer' while the governed vocabulary is 'customer'). Returns the
    canonical known-entity value when one matches, else the VERBATIM value — the E1 write gate is the
    single authority on validity and rejects an unknown entity (never silently rewrites meaning)."""
    from featuregen.overlay.upload.taxonomy.dimensions import known_entities
    known = known_entities()
    if suggested_entity in known:
        return suggested_entity
    norm = (suggested_entity or "").strip().lower()
    return norm if norm in known else suggested_entity


@dataclass(frozen=True, slots=True)
class EntityApplyResult:
    """Outcome of a governed apply. `found=False` → no pending suggestion (route → 404); otherwise a
    DRAFT `entity_assignment` was proposed (`accepted`), awaiting a DISTINCT owner/admin confirmer."""
    found: bool
    accepted: bool
    fact_key: str | None
    proposed_event_id: str | None
    denied_reason: str | None = None


def _propose_entity_assignment(conn, ref: CatalogObjectRef, suggested_entity: str, *, actor,
                               idempotency_key: str) -> EntityApplyResult:
    """PROPOSE a governed `entity_assignment` DRAFT fact for `ref` via E1's `propose_fact` (the SAME
    command D2 uses) — never confirms/verifies. Reused by the apply route and the backfill tool."""
    from featuregen.contracts import Command
    from featuregen.overlay.proposal_commands import propose_fact
    cmd = Command(
        action="propose_fact", aggregate="overlay_fact", aggregate_id=None,
        args={"ref": ref, "fact_type": ENTITY_ASSIGNMENT,
              "proposed_value": {"entity_id": _governed_entity_id(suggested_entity)}},
        actor=actor, idempotency_key=idempotency_key)
    result = propose_fact(conn, cmd)
    if not result.accepted:
        return EntityApplyResult(found=True, accepted=False, fact_key=result.aggregate_id or None,
                                 proposed_event_id=None, denied_reason=result.denied_reason)
    return EntityApplyResult(found=True, accepted=True, fact_key=result.aggregate_id,
                             proposed_event_id=result.produced_event_ids[0])


def apply_entity_suggestion(conn, catalog_source: str, object_ref: str, *,
                            actor) -> EntityApplyResult:
    """E4: a human applies a PENDING suggestion by PROPOSING a GOVERNED `entity_assignment` fact (E1
    propose→confirm, owner-or-admin four-eyes) — NOT the retired legacy `status='applied'` UPDATE.
    The applying human is the PROPOSER; a DISTINCT authorized owner/admin confirms it via E2's
    surface, and E3 then projects the governed entity onto `graph_node.entity`. Idempotent: a
    re-apply while a non-terminal `entity_assignment` fact already exists is denied by `propose_fact`
    (`accepted=False`). `found=False` when there is no pending suggestion (the route → 404).

    Requires a registered catalog adapter (owner routing); the API route ensures the upload-context
    adapter. The legacy 'applied' history is untouched — this never writes `status='applied'`."""
    row = conn.execute(
        "SELECT table_name, column_name, suggested_entity FROM entity_suggestion "
        "WHERE catalog_source = %s AND object_ref = %s AND status = 'pending'",
        (catalog_source, object_ref)).fetchone()
    if row is None:
        return EntityApplyResult(found=False, accepted=False, fact_key=None,
                                 proposed_event_id=None)
    table, column, suggested = row
    ref = _column_ref(conn, catalog_source, object_ref, table, column)
    return _propose_entity_assignment(
        conn, ref, suggested, actor=actor,
        idempotency_key=f"entity-apply:{fact_key(ref, ENTITY_ASSIGNMENT)}:{actor.subject}")


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """Outcome of the one-time legacy→governed backfill (review-only). `proposed` DRAFT facts await a
    human confirm; `skipped_existing` already had an entity_assignment fact (idempotent re-run);
    `skipped_unknown_entity` had a legacy value outside `known_entities()` (needs human correction)."""
    proposed: int
    skipped_existing: int
    skipped_unknown_entity: int
    proposed_fact_keys: tuple[str, ...]


def backfill_legacy_entity_assignments(conn, *, actor,
                                       catalog_source: str | None = None) -> BackfillResult:
    """One-time, IDEMPOTENT, REVIEW-ONLY migration of legacy 'applied' `entity_suggestion` rows to
    governed facts. Reads every legacy `status='applied'` row (optionally one `catalog_source`) and
    PROPOSES an `entity_assignment` DRAFT fact for it via E1 (`propose_fact`) — it NEVER
    confirms/verifies (a human confirms via E2's four-eyes surface; E3 then projects). Idempotent: a
    suggestion that already has ANY `entity_assignment` fact (proposed / verified / rejected) is
    SKIPPED, so a re-run creates nothing new. A legacy value outside `known_entities()` is skipped
    (needs human correction) and reported — never proposed. Requires a registered catalog adapter."""
    from featuregen.overlay.store import load_fact
    sql = ("SELECT catalog_source, object_ref, table_name, column_name, suggested_entity "
           "FROM entity_suggestion WHERE status = 'applied'")
    params: list[str] = []
    if catalog_source is not None:
        sql += " AND catalog_source = %s"
        params.append(catalog_source)
    sql += " ORDER BY catalog_source, object_ref"
    rows = conn.execute(sql, tuple(params)).fetchall()

    proposed = skipped_existing = skipped_unknown = 0
    keys: list[str] = []
    for csource, object_ref, table, column, suggested in rows:
        ref = _column_ref(conn, csource, object_ref, table, column)
        key = fact_key(ref, ENTITY_ASSIGNMENT)
        if load_fact(conn, key):            # already has an entity_assignment fact → idempotent skip
            skipped_existing += 1
            continue
        outcome = _propose_entity_assignment(
            conn, ref, suggested, actor=actor, idempotency_key=f"e4-backfill:{key}")
        if outcome.accepted:
            proposed += 1
            keys.append(key)
        elif outcome.denied_reason and "not a known entity" in outcome.denied_reason:
            skipped_unknown += 1
        else:                               # concurrent proposal / sticky reject → already tracked
            skipped_existing += 1
    return BackfillResult(proposed=proposed, skipped_existing=skipped_existing,
                          skipped_unknown_entity=skipped_unknown,
                          proposed_fact_keys=tuple(keys))


def dismiss_entity_suggestion(conn, catalog_source: str, object_ref: str) -> bool:
    row = conn.execute(
        "UPDATE entity_suggestion SET status = 'dismissed' "
        "WHERE catalog_source = %s AND object_ref = %s AND status = 'pending' RETURNING object_ref",
        (catalog_source, object_ref)).fetchone()
    return row is not None


from collections import deque  # noqa: E402

from featuregen.overlay.upload.join_path import _invert, _table_of  # noqa: E402


@dataclass(frozen=True, slots=True)
class CrossStep:
    kind: str            # "join" (intra-catalog FK) | "entity" (cross-catalog bridge)
    from_source: str
    from_table: str
    to_source: str
    to_table: str
    detail: str          # cardinality (join) or entity name (entity bridge)


def _cross_adjacency(conn, roles: Iterable[str]) -> dict:
    """(catalog_source, table) adjacency over BOTH intra-catalog join edges and cross-catalog entity
    bridges — the graph a cross-catalog path traverses."""
    adj: dict[tuple[str, str], list] = {}

    def link(a, b, step):
        adj.setdefault(a, []).append((b, step))

    for src, fr, to, card in conn.execute(
            # authority='operational' (Task 7): a governed-seam display-only edge is excluded from
            # cross-catalog feature-construction adjacency (the confirmed approved_join fact governs).
            # Governed edge filter (Pass C Task 8): a fact-LINKED edge is adjacent only while its
            # approved_join fact is VERIFIED; a declared edge (fact_key NULL) is untouched.
            "SELECT catalog_source, from_ref, to_ref, cardinality FROM graph_edge "
            "WHERE kind = 'joins' AND authority = 'operational' "
            "AND (approved_join_fact_key IS NULL OR approved_join_status = 'VERIFIED')").fetchall():
        a, b = (src, _table_of(fr)), (src, _table_of(to))
        if a == b:
            continue
        link(a, b, CrossStep("join", src, a[1], src, b[1], card or ""))
        # the reverse hop INVERTS the fan (M7): a reverse N:1 is really 1:N — else a human confirms a
        # cross-catalog path that claims a fan-out hop fans in safely (double-count hazard).
        link(b, a, CrossStep("join", src, b[1], src, a[1], _invert(card) or ""))

    for entity in list_entities(conn):
        tables = sorted({(k.catalog_source, k.table) for k in entity_key_columns(conn, entity, roles=roles)})
        for i in range(len(tables)):
            for j in range(i + 1, len(tables)):
                a, b = tables[i], tables[j]
                link(a, b, CrossStep("entity", a[0], a[1], b[0], b[1], entity))
                link(b, a, CrossStep("entity", b[0], b[1], a[0], a[1], entity))
    return adj


def find_cross_catalog_path(conn, from_source: str, from_table: str, to_source: str, to_table: str, *,
                            roles: Iterable[str] = ()) -> list[CrossStep] | None:
    """Shortest path between two tables in (possibly different) catalogs, traversing intra-catalog
    joins and cross-catalog entity bridges. [] when start == goal; None if unreachable. Entity-bridge
    hops are declared/entity-resolved (no-DB) — callers surface them for human confirmation."""
    start, goal = (from_source, from_table), (to_source, to_table)
    if start == goal:
        return []
    adj = _cross_adjacency(conn, roles)
    queue: deque = deque([(start, [])])
    seen = {start}
    while queue:
        node, path = queue.popleft()
        for nbr, step in adj.get(node, []):
            if nbr in seen:
                continue
            new_path = path + [step]
            if nbr == goal:
                return new_path
            seen.add(nbr)
            queue.append((nbr, new_path))
    return None
