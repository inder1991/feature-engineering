"""Stage 1B — the governed planning observation, bridge-demand and review stores (migrations
1120 + 1121, extended by 1132).

Every governed planning request leaves ONE row here, whatever it did: resolved, unresolved,
bounded out, safety-rejected. That is the point. A telemetry programme that records only successes
answers "how good are the features we shipped?" and never "what could this platform not do, and
what would it take?" — and the second question is the one that funds bridge work.

Three writes and two reads:

* ``record_planning_observations`` — the per-request row, keyed
  ``(generation_run_id, observation_mode, governed_variant_id)`` so a LIVE observation and its
  TELEMETRY twin coexist honestly while a replay within one mode resolves to the row already there.
* ``record_bridge_demand`` — the rejection-specific child. Its ``demand_identity_hash`` is the full
  sha256 of the unmet hop's material, pinned by ``GRAPH_VERSION`` and ``PLANNER_VERSION`` so a
  graph or planner change cannot masquerade as the same demand. ``to_endpoint_hint`` is advisory and
  is deliberately NOT in that material.
* ``enqueue_governed_telemetry`` / ``claim_telemetry_work`` / ``complete_telemetry_work`` — the
  outbox, so telemetry planning runs off the request path.
* ``observation_queues`` — the three demand queues, AGGREGATED OVER THE FULL FILTERED POPULATION
  and only then paginated. Latest-N-then-group would let a busy week hide a standing gap.
* ``resolution_summary`` — the wave-1 report's numerator source.
* ``demand_satisfaction`` (A7) — the CURRENT-UNRESOLVED view. "Satisfied" is a PROJECTION over this
  append-only history and never a delete, a flag or an in-place update: a met demand keeps every
  filing it ever had, because how long a gap stood and how many teams hit it IS the funding
  argument. Only which side of the read it lands on changes.

``distinct_intents`` and ``distinct_runs`` are reported SEPARATELY on purpose: five retries of one
intent is one team asking once, and a queue that counts it as five demands ranks noise. The headline
count beside them is ``demand_rows`` — see :data:`_COUNTS` for why it is not called "observations".
"""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.idgen import mint_id
from featuregen.overlay.upload.object_ref import qualify_object_ref
from featuregen.overlay.upload.planner.contracts import (
    PLANNER_VERSION,
    PlanResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.taxonomy.entity_registry import GRAPH_VERSION

#: The observation modes 1120's CHECK admits.
OBSERVATION_MODES = frozenset({"live", "telemetry"})

#: The resolution status of a request the telemetry worker refused to plan because the registry
#: moved under the work item. Not a planner verdict — 1120 leaves ``resolution_status`` free text
#: precisely so an ADAPTER-level outcome can be recorded honestly instead of dressed as one.
#: Lives HERE (not in the worker) because both the writer (``governed_telemetry_worker``) and the
#: read side (``governed_planning_report``) need it, and the report should not pull the worker's
#: whole planning import graph into the API route chain for one constant.
STALE_REGISTRY = "stale_registry"

#: A7/R6 — a demand-QUEUE CATEGORY, and deliberately NOT a member of the action vocabulary in
#: ``semantic_eligibility_reasons``. The crossing is sanctioned and a realization EXISTS (A4c's
#: provisional sandbox revision); what is missing is the cardinality EVIDENCE that makes it
#: costable. Registering it as an eligibility reason would give it a six-action disposition row and
#: start BLOCKING acts — which the owner's matrix forbids, since unknown cardinality is previewable
#: under guards. Its spelling follows this column's convention (the lowercase, ReasonCode-shaped
#: strings already stored beside it), not the plan's prose casing; the constant carries the plan's
#: name so a reader can find it from either side.
CARDINALITY_EVIDENCE_REQUIRED = "cardinality_evidence_required"

#: Verdict -> queue. CLOSED: a rejection whose reason is not one of these is not a demand at all
#: (a concept mismatch is a modelling problem, not a missing bridge), and the store drops it rather
#: than filing it somewhere plausible. 1120 + 1132 carry the same mapping as a CHECK, so the
#: routing is structural: the queue a demand lands in is a function of its verdict, not of its
#: caller. The two realization verdicts share ONE queue on purpose — "build a realization" and
#: "measure the cardinality of the one that exists" are two states of the same person's work, and
#: a fourth queue would split the ranking rather than the work.
DEMAND_VERDICT_QUEUES: MappingProxyType[str, str] = MappingProxyType({
    ReasonCode.unsanctioned_bridge.value: "bridge_demand",
    ReasonCode.missing_realization.value: "realization_gap",
    CARDINALITY_EVIDENCE_REQUIRED: "realization_gap",
    ReasonCode.bounded_out_max_bridges.value: "planner_capacity",
    ReasonCode.bounded_out_max_frontier_states.value: "planner_capacity",
})

#: Capacity rows DROP whatever hop reached them, by ruling: their identity material is reduced and
#: their hop columns stay at the schema's defaults. Hashing the hop the frontier happened to be
#: sitting on when it ran out of budget would mint a fresh "demand" per run — the demand is the
#: CAPACITY, not that hop.
#:
#: The two members differ in what there is to drop, and S1B-2 made the difference real:
#:   * ``bounded_out_max_bridges`` — the search DID reach a specific crossing it could not spend a
#:     bridge on, so its rejected plan carries a fully populated ``unmet_hop``. This set is what
#:     discards it here. (Both JSONB evidence columns are still written from the rejection, so a
#:     capacity row keeps its realizers/near-side sample even though its hop identity is reduced.)
#:   * ``bounded_out_max_frontier_states`` — genuinely has no hop at all: that reject is minted from
#:     the START state, which realized nothing, so the planner carries ``unmet_hop=None``.
CAPACITY_VERDICTS = frozenset({
    ReasonCode.bounded_out_max_bridges.value,
    ReasonCode.bounded_out_max_frontier_states.value,
})

#: What counts in the numerator of a resolution RATE. Strict: ``resolved_with_ambiguity`` means the
#: planner produced several equally-good plans and could not choose, which is not a clean success —
#: it stays visible in ``by_origin_status``, where a reader can add it back deliberately.
RESOLVED_STATUSES = frozenset({PlanResolutionStatus.resolved.value})

#: The "recent" half of the recent/historical split, measured back from ``as_of``.
RECENT_WINDOW_DAYS = 7
#: How many endpoint suggestions a queue group carries. A sample, not a catalogue.
SUGGESTED_ENDPOINT_SAMPLE = 5

_QUEUE_NAMES = ("bridge_demand", "realization_gap", "planner_capacity")

# ── the observation row's column contract ──────────────────────────────────────────────────────
# Order here IS the INSERT's column order. Required columns have no schema default and no sensible
# invention; the rest fall back to exactly 1120's defaults.
_OBSERVATION_REQUIRED = (
    "definition_origin", "canonical_definition_id", "governed_variant_id",
    "planning_request_hash", "target_entity", "resolution_status",
)
_OBSERVATION_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType({
    "recipe_id": None,
    "parameter_binding_hash": "",
    "physical_plan_content_hash": "",
    "selected_physical_plan_id": "",
    "contract_id": None,
    "primary_objective": "",
    "anchor_catalog_source": "",
    "reason_codes": (),
    "participating_catalogs": (),
    "hop_count": 0,
    "bridge_count": 0,
    "authority_floor_status": "",
    "safety_status": "",
    "readiness": "",
    "intent_corroborated": False,
    "param_divergence": (),
    "catalog_scope_material": MappingProxyType({}),
    "metadata_snapshot_id": None,
    "compiler_input_fingerprint": "",
})
_OBSERVATION_COLUMNS = (*_OBSERVATION_REQUIRED, *_OBSERVATION_DEFAULTS)
_OBSERVATION_JSONB = frozenset({
    "reason_codes", "participating_catalogs", "param_divergence", "catalog_scope_material",
})

_HOP_TEXT_FIELDS = ("relationship_id", "relationship_version", "from_entity", "to_entity",
                    "position_catalog", "position_table_ref")


# ── the telemetry outbox (1121) ────────────────────────────────────────────────────────────────


def enqueue_governed_telemetry(conn: DbConn, *, generation_run_id: str, intent_id: str | None,
                               frozen_inputs: dict) -> str:
    """Queue one telemetry replan against inputs frozen NOW, so the worker replans what the run
    saw rather than what the catalog looks like whenever it gets round to it."""
    work_item_id = mint_id("gto")
    conn.execute(
        "INSERT INTO governed_telemetry_outbox "
        "(work_item_id, generation_run_id, intent_id, frozen_inputs) VALUES (%s, %s, %s, %s)",
        (work_item_id, generation_run_id, intent_id, Jsonb(dict(frozen_inputs))))
    return work_item_id


def claim_telemetry_work(conn: DbConn, *, owner: str, lease_seconds: int = 300) -> dict | None:
    """Atomically lease the oldest claimable item — queued, or leased on a lease that has expired.

    The claim is ``runtime/queue.py``'s, shape for shape (``FOR UPDATE SKIP LOCKED`` over an
    ordered single-row CTE, then one UPDATE that flips the status, stamps the owner, extends the
    lease and bumps both the attempt count and a monotonically increasing fence). Returns the whole
    row as a dict, or None when nothing is claimable.

    ``FOR UPDATE SKIP LOCKED`` is load-bearing and is pinned by a two-connection test: without it a
    concurrent claimer BLOCKS on the row another worker is mid-claim on, and then completes the
    UPDATE against the row it already selected — two workers holding one item.

    **The returned ``lease_fence`` is the caller's completion token.** It rises on every claim
    (including a reclaim after expiry), so a worker that hands its fence back to
    ``complete_telemetry_work`` can only terminalize the lease it actually holds.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "WITH c AS ("
            "  SELECT work_item_id FROM governed_telemetry_outbox"
            "   WHERE status = 'queued'"
            "      OR (status = 'leased'"
            "          AND (lease_expires_at IS NULL OR lease_expires_at <= now()))"
            "   ORDER BY recorded_at, work_item_id"
            "   FOR UPDATE SKIP LOCKED LIMIT 1"
            ") "
            "UPDATE governed_telemetry_outbox q "
            "   SET status = 'leased', lease_owner = %s,"
            "       lease_expires_at = now() + make_interval(secs => %s),"
            "       attempt_count = q.attempt_count + 1,"
            "       lease_fence = q.lease_fence + 1 "
            "  FROM c WHERE q.work_item_id = c.work_item_id "
            "RETURNING q.*",
            (owner, lease_seconds))
        row = cur.fetchone()
    return dict(row) if row is not None else None


def complete_telemetry_work(conn: DbConn, *, work_item_id: str, owner: str, fence: int,
                            ok: bool, error: str = "") -> bool:
    """FENCED terminalization: completes the item only if this caller still holds its lease.

    Returns True when the completion took, False when it did not — and a False is INFORMATION, not
    a nuisance. It is how a worker whose lease expired mid-flight LEARNS that somebody else has the
    item now, instead of overwriting a colleague's verdict and carrying on believing it finished
    the work. A caller that ignores the return value is the bug this guard exists to make visible.

    Three ways it returns False, all of them the same statement — *you do not hold this lease*:
      * the item was reclaimed after your lease expired (the fence moved past yours);
      * somebody else owns it (``lease_owner`` differs);
      * it is already terminal (``status`` is no longer ``leased``), so a second complete cannot
        rewrite a recorded ``done`` into a ``failed`` or move a ``completed_at``.

    Deliberately NOT guarded on ``lease_expires_at > now()``, which is where this parts company
    with ``runtime/queue.py``'s otherwise identical completion. The fence already refuses a
    RECLAIMED item, and that is the case that corrupts. Refusing on expiry alone would additionally
    throw away finished work whenever a slow-but-uncontested worker overran its lease — punishing
    slowness by discarding the result, which is not what the lease is for.

    ``failed`` is terminal, not a retry: an item that failed for a reason worth re-running is
    re-enqueued deliberately, with its own row and its own frozen inputs, rather than silently
    re-attempted under inputs nobody re-checked.
    """
    row = conn.execute(
        "UPDATE governed_telemetry_outbox "
        "   SET status = %s, last_error = %s, completed_at = now(),"
        "       lease_owner = '', lease_expires_at = NULL "
        " WHERE work_item_id = %s AND status = 'leased'"
        "   AND lease_owner = %s AND lease_fence = %s "
        "RETURNING work_item_id",
        ("done" if ok else "failed", "" if ok else error,
         work_item_id, owner, fence)).fetchone()
    return row is not None


# ── the observations (1120) ────────────────────────────────────────────────────────────────────


def record_planning_observations(conn: DbConn, *, generation_run_id: str, intent_id: str | None,
                                 observation_mode: str,
                                 rows: Sequence[dict]) -> tuple[str, ...]:
    """Append one observation per planned request; returns the row ids IN INPUT ORDER.

    Idempotent per ``(run, mode, governed_variant_id)``: a replay resolves to the id already
    stored, so a caller that links derived records to these ids never links to a ghost.
    """
    if observation_mode not in OBSERVATION_MODES:
        raise ValueError(
            f"observation_mode must be one of {sorted(OBSERVATION_MODES)!r}, "
            f"got {observation_mode!r}")
    placeholders = ", ".join(["%s"] * (4 + len(_OBSERVATION_COLUMNS)))
    statement = (
        "INSERT INTO governed_planning_observation "
        "(observation_id, generation_run_id, intent_id, observation_mode, "
        + ", ".join(_OBSERVATION_COLUMNS) + ") "
        f"VALUES ({placeholders}) "
        "ON CONFLICT (generation_run_id, observation_mode, governed_variant_id) DO NOTHING "
        "RETURNING observation_id")
    minted: list[str] = []
    for row in rows:
        values = _observation_values(row)
        params: list[Any] = [mint_id("gpo"), generation_run_id, intent_id, observation_mode]
        params.extend(Jsonb(values[column]) if column in _OBSERVATION_JSONB else values[column]
                      for column in _OBSERVATION_COLUMNS)
        found = conn.execute(statement, params).fetchone()
        if found is None:
            # DO NOTHING reports success for a row it did not write — read the standing id back.
            found = conn.execute(
                "SELECT observation_id FROM governed_planning_observation "
                " WHERE generation_run_id = %s AND observation_mode = %s "
                "   AND governed_variant_id = %s",
                (generation_run_id, observation_mode, values["governed_variant_id"])).fetchone()
        minted.append(found[0])
    return tuple(minted)


def _observation_values(row: dict) -> dict[str, Any]:
    """Fill 1120's defaults, refuse a column this table does not have, refuse a blank mandatory."""
    unknown = sorted(set(row) - set(_OBSERVATION_COLUMNS))
    if unknown:
        raise ValueError(
            f"unknown governed_planning_observation column(s): {unknown!r}. A dropped column is a "
            "silently incomplete observation, so this refuses rather than ignores.")
    blank = [column for column in _OBSERVATION_REQUIRED
             if not str(row.get(column) or "").strip()]
    if blank:
        raise ValueError(f"governed_planning_observation requires non-blank {blank!r}")
    values = {column: row.get(column, default)
              for column, default in _OBSERVATION_DEFAULTS.items()}
    values.update({column: row[column] for column in _OBSERVATION_REQUIRED})
    for column in _OBSERVATION_JSONB:
        value = values[column]
        values[column] = dict(value) if isinstance(value, MappingProxyType) else value
    return values


def demand_hop_columns(rejection: dict) -> dict[str, Any]:
    """The seven hop columns one rejection contributes, at 1120's defaults where it has none.

    CAPACITY rows DROP whatever hop reached them (see :data:`CAPACITY_VERDICTS`), so they come back
    at the schema's defaults rather than carrying the hop the frontier happened to be sitting on.
    """
    if str(rejection.get("verdict") or "") in CAPACITY_VERDICTS:
        return {**dict.fromkeys(_HOP_TEXT_FIELDS, ""), "hop_index": -1}
    hop: dict[str, Any] = {field: str(rejection.get(field) or "") for field in _HOP_TEXT_FIELDS}
    # An EXPLICIT None means the same thing as an absent key — "no hop index" — and the
    # planner-side adapter is the likely source of one. `int(None)` would raise instead.
    raw_hop_index = rejection.get("hop_index")
    hop["hop_index"] = -1 if raw_hop_index is None else int(raw_hop_index)
    return hop


def demand_identity_hash(rejection: dict, *, anchor_catalog_source: str) -> str:
    """THE demand identity — the FULL sha256 hex of the unmet hop's material, never a prefix.

    **Public because it is the one-writer law's load-bearing half (R5).** The unique key
    ``(observation_id, demand_identity_hash)`` makes a repeated demand ONE row only for writers
    that agree on the hash; a second writer that re-derived the material slightly differently would
    mint a second, equally valid-looking row for the same crossing, and Postgres would see two
    demands where a person sees one. So the recipe lives here, exactly once, and a writer that will
    not call it is a defect CI can see (``test_exactly_one_module_writes_the_demand_table``).

    The material is pinned by ``GRAPH_VERSION`` and ``PLANNER_VERSION`` so a graph or planner change
    cannot masquerade as the same demand; ``to_endpoint_hint`` is advisory and deliberately absent.
    ``anchor_catalog_source`` is material for CAPACITY verdicts only — those have no hop, and the
    anchor is the one thing that makes one capacity demand different from another's.
    """
    verdict = str(rejection.get("verdict") or "")
    revision_hash = str(rejection.get("recipe_revision_hash") or "")
    if verdict in CAPACITY_VERDICTS:
        material = (revision_hash, verdict, anchor_catalog_source, GRAPH_VERSION, PLANNER_VERSION)
    else:
        hop = demand_hop_columns(rejection)
        material = (revision_hash, hop["relationship_id"], hop["relationship_version"],
                    hop["from_entity"], hop["to_entity"], hop["position_catalog"],
                    hop["position_table_ref"], str(hop["hop_index"]), verdict,
                    GRAPH_VERSION, PLANNER_VERSION)
    return hashlib.sha256("|".join(material).encode()).hexdigest()


def record_bridge_demand(conn: DbConn, *, observation_id: str,
                         rejections: Sequence[dict]) -> tuple[str, ...]:
    """Append one demand per DEMAND-SHAPED rejection; returns the ids of the rows that exist after,
    **each id exactly once**.

    A rejection whose verdict is not in ``DEMAND_VERDICT_QUEUES`` yields no row and no error: the
    planner rejects for many reasons, and only some of them are somebody's unbuilt bridge. The
    closed set is checked HERE, so only known verdicts ever reach SQL — 1120+1132's CHECK is the
    second line, for a caller that bypasses this store.

    **The one-writer law (R5), in this function.** Demand records ONCE per planning OCCURRENCE and
    repeated occurrences stay countable, and the two halves need different mechanisms:

    * ACROSS calls and across writers, the unique key ``(observation_id, demand_identity_hash)``
      is the guard — an occurrence is exactly one observation (1120 keys observations by
      ``(run, mode, governed_variant_id)``), so a second file against the same occurrence resolves
      to the standing row.
    * WITHIN one call, the identity set below is the guard, and it is not cosmetic: both production
      writers report ``len(record_bridge_demand(...))`` as "demands filed", and two rejections that
      differ only in an advisory field are ONE demand. Returning its id twice inflated the reported
      count while the ledger stayed correct — a telemetry number nobody could reconcile against the
      table it claimed to describe.

    Recurrence is untouched by both: a second RUN is a second observation, hence a second row under
    the same ``demand_identity_hash``, which is what ``observation_queues`` counts.
    """
    minted: list[str] = []
    seen: dict[str, str] = {}
    anchor_fallback: str | None = None
    for rejection in rejections:
        verdict = str(rejection.get("verdict") or "")
        queue = DEMAND_VERDICT_QUEUES.get(verdict)
        if queue is None:
            continue
        revision_hash = str(rejection.get("recipe_revision_hash") or "")
        anchor = str(rejection.get("anchor_catalog_source") or "")
        if verdict in CAPACITY_VERDICTS and not anchor:
            if anchor_fallback is None:
                found = conn.execute(
                    "SELECT anchor_catalog_source FROM governed_planning_observation "
                    " WHERE observation_id = %s", (observation_id,)).fetchone()
                anchor_fallback = found[0] if found is not None else ""
            anchor = anchor_fallback
        hop = demand_hop_columns(rejection)
        identity_hash = demand_identity_hash(rejection, anchor_catalog_source=anchor)
        if identity_hash in seen:
            continue        # one occurrence, one demand — see the one-writer law above
        found = conn.execute(
            "INSERT INTO bridge_demand_observation "
            "(demand_id, observation_id, demand_queue, demand_identity_hash, recipe_revision_hash, "
            " relationship_id, relationship_version, from_entity, to_entity, position_catalog, "
            " position_table_ref, hop_index, verdict, realizers, near_side_key_refs, "
            " to_endpoint_hint) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (observation_id, demand_identity_hash) DO NOTHING "
            "RETURNING demand_id",
            (mint_id("bdo"), observation_id, queue, identity_hash, revision_hash,
             hop["relationship_id"], hop["relationship_version"], hop["from_entity"],
             hop["to_entity"], hop["position_catalog"], hop["position_table_ref"],
             hop["hop_index"], verdict, Jsonb(list(rejection.get("realizers") or [])),
             Jsonb(list(rejection.get("near_side_key_refs") or [])),
             str(rejection.get("to_endpoint_hint") or ""))).fetchone()
        if found is None:
            found = conn.execute(
                "SELECT demand_id FROM bridge_demand_observation "
                " WHERE observation_id = %s AND demand_identity_hash = %s",
                (observation_id, identity_hash)).fetchone()
        seen[identity_hash] = found[0]
        minted.append(found[0])
    return tuple(minted)


# ── the reads ──────────────────────────────────────────────────────────────────────────────────

_BASE_CTE = (
    "WITH base AS ("
    "  SELECT d.demand_queue, d.relationship_id, d.from_entity, d.to_entity,"
    "         d.position_catalog, d.position_table_ref, d.realizers, d.near_side_key_refs,"
    "         d.recorded_at, d.demand_id, o.intent_id, o.generation_run_id, o.observation_mode"
    "    FROM bridge_demand_observation d"
    "    JOIN governed_planning_observation o ON o.observation_id = d.observation_id"
    "   WHERE d.recorded_at <= %s"
    ")")

#: The queue rollup's counts. The headline is ``demand_rows`` and not ``observations`` because that
#: is what ``count(*)`` over ``base`` actually counts: ONE observation carrying an unmet hop AND a
#: contract-level realization gap files TWO demand rows, by S1B-3's co-occurrence ruling. Calling
#: that "2 observations" would double-count the run that produced it and inflate every queue that
#: reports it. The mode and recency splits carry the SAME noun for the same reason — they are
#: splits OF the demand rows, and `live_observations` beside `demand_rows` would reinstate the
#: very confusion the rename removes. Only `distinct_intents` / `distinct_runs` count something
#: else, and they say so in their names.
_COUNTS = (
    " count(*) AS demand_rows,"
    " count(DISTINCT intent_id) AS distinct_intents,"
    " count(DISTINCT generation_run_id) AS distinct_runs,"
    " count(*) FILTER (WHERE observation_mode = 'live') AS live_demand_rows,"
    " count(*) FILTER (WHERE observation_mode = 'telemetry') AS telemetry_demand_rows,"
    " count(*) FILTER (WHERE recorded_at >= %s - make_interval(days => %s))"
    "     AS recent_demand_rows,"
    " count(*) FILTER (WHERE recorded_at < %s - make_interval(days => %s))"
    "     AS historical_demand_rows")

_COUNT_KEYS = ("demand_rows", "distinct_intents", "distinct_runs", "live_demand_rows",
               "telemetry_demand_rows", "recent_demand_rows", "historical_demand_rows")


def observation_queues(conn: DbConn, *, limit: int = 100, cursor: str | None = None,
                       as_of: datetime | None = None) -> dict:
    """The three demand queues, grouped and counted over the FULL filtered population.

    Grouping is two-level: ``(relationship_id, from_entity, to_entity)`` — the crossing somebody
    wants — with ``(position_catalog, position_table_ref)`` nested underneath, which is where an
    engineer would actually build it. Pagination is a cursor over the group key, never an offset:
    the aggregate is computed over everything first, so a group on page 3 carries the same totals
    it would have carried on page 1.
    """
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit!r}")
    resolved_as_of = as_of if as_of is not None else conn.execute("SELECT now()").fetchone()[0]
    has_cursor = cursor is not None
    cursor_key = _decode_cursor(cursor) if has_cursor else ("", "", "", "")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _BASE_CTE
            + ", grouped AS (SELECT demand_queue, relationship_id, from_entity, to_entity,"
            + _COUNTS
            + " FROM base GROUP BY 1, 2, 3, 4) "
              "SELECT * FROM grouped "
              " WHERE NOT %s::boolean"
              "    OR (demand_queue, relationship_id, from_entity, to_entity)"
              "       > (%s::text, %s::text, %s::text, %s::text) "
              " ORDER BY demand_queue, relationship_id, from_entity, to_entity "
              " LIMIT %s",
            (resolved_as_of, resolved_as_of, RECENT_WINDOW_DAYS, resolved_as_of,
             RECENT_WINDOW_DAYS, has_cursor, *cursor_key, limit + 1))
        top_rows = cur.fetchall()

    has_more = len(top_rows) > limit
    top_rows = top_rows[:limit]
    result: dict[str, Any] = {
        "as_of": resolved_as_of,
        "limit": limit,
        "cursor": cursor,
        "next_cursor": None,
        "queues": {name: [] for name in _QUEUE_NAMES},
    }
    if not top_rows:
        return result

    last_key = _group_key(top_rows[-1])
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _BASE_CTE
            + ", nested AS (SELECT demand_queue, relationship_id, from_entity, to_entity,"
              " position_catalog, position_table_ref,"
            + _COUNTS
            + ", jsonb_agg(realizers ORDER BY recorded_at, demand_id) AS realizer_lists,"
              "  jsonb_agg(near_side_key_refs ORDER BY recorded_at, demand_id) AS near_side_lists"
              " FROM base GROUP BY 1, 2, 3, 4, 5, 6) "
              "SELECT * FROM nested "
              " WHERE (NOT %s::boolean"
              "        OR (demand_queue, relationship_id, from_entity, to_entity)"
              "           > (%s::text, %s::text, %s::text, %s::text))"
              "   AND (demand_queue, relationship_id, from_entity, to_entity)"
              "       <= (%s::text, %s::text, %s::text, %s::text) "
              " ORDER BY 1, 2, 3, 4, 5, 6",
            (resolved_as_of, resolved_as_of, RECENT_WINDOW_DAYS, resolved_as_of,
             RECENT_WINDOW_DAYS, has_cursor, *cursor_key, *last_key))
        nested_rows = cur.fetchall()

    nested_by_key: dict[tuple[str, ...], list[dict]] = {}
    for row in nested_rows:
        entry = {"position_catalog": row["position_catalog"],
                 "position_table_ref": row["position_table_ref"],
                 **{key: row[key] for key in _COUNT_KEYS},
                 "suggested_endpoints": _sample_endpoints(
                     row["realizer_lists"], row["near_side_lists"],
                     row["position_catalog"] or "")}
        nested_by_key.setdefault(_group_key(row), []).append(entry)

    for row in top_rows:
        key = _group_key(row)
        nested = nested_by_key.get(key, [])
        group = {"relationship_id": row["relationship_id"],
                 "from_entity": row["from_entity"],
                 "to_entity": row["to_entity"],
                 **{count_key: row[count_key] for count_key in _COUNT_KEYS},
                 "suggested_endpoints": _dedupe(
                     endpoint for entry in nested for endpoint in entry["suggested_endpoints"]),
                 "nested": nested}
        result["queues"][row["demand_queue"]].append(group)
    if has_more:
        result["next_cursor"] = _encode_cursor(last_key)
    return result


#: The projection's per-identity payload, in the order it is SELECTed. Named once so the SQL, the
#: row builder and the caller cannot drift.
_SATISFACTION_KEYS = ("demand_identity_hash", "demand_queue", "verdict", "relationship_id",
                      "relationship_version", "from_entity", "to_entity", "position_catalog",
                      "position_table_ref", "occurrences", "distinct_runs", "distinct_intents",
                      "first_seen", "last_seen", "satisfied_at")


def demand_satisfaction(conn: DbConn, *, as_of: datetime | None = None,
                        queue: str | None = None) -> dict:
    """The CURRENT-UNRESOLVED view over an append-only ledger: which demands are still outstanding.

    **Satisfaction is a projection, never a write.** ``bridge_demand_observation`` is append-only by
    trigger, and it stays that way: nothing here marks, deletes or supersedes a row. A demand that
    was met keeps every filing it ever had — that history is the only record of how long the gap
    stood and how many teams hit it, which is precisely what a funding argument is made of. What
    changes is which side of this read it lands on.

    **The evidence.** A filing is SETTLED when the SAME governed variant, in the SAME lane, later
    planned successfully: an observation with a status in :data:`RESOLVED_STATUSES`, recorded after
    the filing and at or before ``as_of``. That is positive evidence rather than an absence — a
    resolved observation files no demand children (both writers skip them), so "it planned, and
    nothing was missing" is exactly what a resolved row asserts.

    Three deliberate conservatisms, each of which prefers reporting a met demand as outstanding
    over the reverse — a governance queue that quietly drops real work is worse than one that
    carries a stale row a person can close:

    * **per (variant, lane).** A demand is satisfied only when EVERY (governed_variant_id,
      observation_mode) pair that ever filed it has since resolved. One variant still blocked keeps
      the demand open even if another got past it.
    * **the lanes never cross.** A telemetry replan that succeeded says nothing about what the
      request path could do, so ``observation_mode`` is part of the key.
    * **re-filing re-opens.** A crossing that broke again is outstanding again — the last filing
      simply has no later resolution behind it — and both filings stay countable.

    Returns ``{"as_of", "resolved_statuses", "unresolved": [...], "satisfied": [...], "totals"}``,
    each entry carrying the crossing, its ``occurrences`` / ``distinct_runs`` /
    ``distinct_intents``, its ``first_seen`` / ``last_seen``, and ``satisfied_at`` (the moment the
    LAST outstanding filing was settled; ``None`` while any filing still stands).

    **Known limit, stated rather than papered over:** ``GRAPH_VERSION`` / ``PLANNER_VERSION`` are
    inside ``demand_identity_hash``, so an engine bump re-keys demand — the pre-bump identity stops
    recurring and the post-bump one starts. Both remain visible here (the old one as UNRESOLVED,
    since no resolution stands behind it), so a version bump never silently empties the queue; it
    does leave a retired identity beside its successor until somebody closes it.
    """
    resolved_as_of = as_of if as_of is not None else conn.execute("SELECT now()").fetchone()[0]
    statuses = sorted(RESOLVED_STATUSES)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "WITH filed AS ("
            # `occurred_at` is the OBSERVATION's instant, not the demand child's: R5 records demand
            # per planning OCCURRENCE, and the child is written inside that occurrence's own
            # transaction. The window filter stays on the child, matching `observation_queues`.
            "  SELECT d.demand_identity_hash, d.demand_queue, d.verdict, d.relationship_id,"
            "         d.relationship_version, d.from_entity, d.to_entity, d.position_catalog,"
            "         d.position_table_ref, o.recorded_at AS occurred_at, o.generation_run_id,"
            "         o.intent_id, o.governed_variant_id, o.observation_mode"
            "    FROM bridge_demand_observation d"
            "    JOIN governed_planning_observation o ON o.observation_id = d.observation_id"
            "   WHERE d.recorded_at <= %s AND (%s::text IS NULL OR d.demand_queue = %s::text)"
            "), settled AS ("
            # One correlated lookup per FILING (not per identity): the earliest later success of
            # that filing's own variant AND lane. A filing with none is what keeps a demand open.
            "  SELECT f.*, (SELECT min(o2.recorded_at)"
            "                 FROM governed_planning_observation o2"
            "                WHERE o2.governed_variant_id = f.governed_variant_id"
            "                  AND o2.observation_mode = f.observation_mode"
            "                  AND o2.resolution_status = ANY(%s)"
            "                  AND o2.recorded_at > f.occurred_at"
            "                  AND o2.recorded_at <= %s) AS settled_at"
            "    FROM filed f"
            ") "
            "SELECT demand_identity_hash, demand_queue, verdict, relationship_id,"
            "       relationship_version, from_entity, to_entity, position_catalog,"
            "       position_table_ref, count(*) AS occurrences,"
            "       count(DISTINCT generation_run_id) AS distinct_runs,"
            "       count(DISTINCT intent_id) AS distinct_intents,"
            "       min(occurred_at) AS first_seen, max(occurred_at) AS last_seen,"
            # bool_and over "this filing is settled": one unsettled filing keeps the demand open,
            # and max(settled_at) is then the moment the LAST of them was settled.
            "       CASE WHEN bool_and(settled_at IS NOT NULL) THEN max(settled_at) END"
            "            AS satisfied_at "
            "  FROM settled GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9 "
            " ORDER BY demand_queue, relationship_id, from_entity, to_entity,"
            "          demand_identity_hash",
            (resolved_as_of, queue, queue, statuses, resolved_as_of))
        rows = cur.fetchall()

    unresolved = [{key: row[key] for key in _SATISFACTION_KEYS}
                  for row in rows if row["satisfied_at"] is None]
    satisfied = [{key: row[key] for key in _SATISFACTION_KEYS}
                 for row in rows if row["satisfied_at"] is not None]
    return {
        "as_of": resolved_as_of,
        "resolved_statuses": statuses,
        "queue": queue,
        "unresolved": unresolved,
        "satisfied": satisfied,
        "totals": {
            "demand_identities": len(rows),
            "unresolved": len(unresolved),
            "satisfied": len(satisfied),
            "unresolved_demand_rows": sum(entry["occurrences"] for entry in unresolved),
            "satisfied_demand_rows": sum(entry["occurrences"] for entry in satisfied),
        },
    }


def resolution_summary(conn: DbConn, *, as_of: datetime | None = None) -> dict:
    """Per-origin and per-anchor-catalog resolution, over every observation at or before ``as_of``.

    ``by_origin_status`` is the full contingency table (nothing is collapsed away); the RATES use
    ``RESOLVED_STATUSES`` only, so "resolution rate" never quietly means "produced something".
    """
    resolved_as_of = as_of if as_of is not None else conn.execute("SELECT now()").fetchone()[0]
    statuses = sorted(RESOLVED_STATUSES)

    by_origin_status = [
        {"definition_origin": origin, "resolution_status": status, "observations": count}
        for origin, status, count in conn.execute(
            "SELECT definition_origin, resolution_status, count(*) "
            "  FROM governed_planning_observation WHERE recorded_at <= %s "
            " GROUP BY 1, 2 ORDER BY 1, 2", (resolved_as_of,)).fetchall()]

    by_origin = [_rate_row("definition_origin", row) for row in conn.execute(
        "SELECT definition_origin, count(*),"
        "       count(*) FILTER (WHERE resolution_status = ANY(%s)) "
        "  FROM governed_planning_observation WHERE recorded_at <= %s "
        " GROUP BY 1 ORDER BY 1", (statuses, resolved_as_of)).fetchall()]

    by_anchor = [_rate_row("anchor_catalog_source", row) for row in conn.execute(
        "SELECT anchor_catalog_source, count(*),"
        "       count(*) FILTER (WHERE resolution_status = ANY(%s)) "
        "  FROM governed_planning_observation WHERE recorded_at <= %s "
        " GROUP BY 1 ORDER BY 1", (statuses, resolved_as_of)).fetchall()]

    total, resolved = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE resolution_status = ANY(%s)) "
        "  FROM governed_planning_observation WHERE recorded_at <= %s",
        (statuses, resolved_as_of)).fetchone()
    return {
        "as_of": resolved_as_of,
        "resolved_statuses": statuses,
        "by_origin_status": by_origin_status,
        "by_origin": by_origin,
        "by_anchor_catalog": by_anchor,
        "totals": {"observations": total, "resolved": resolved,
                   "resolution_rate": _rate(resolved, total)},
    }


# ── helpers ────────────────────────────────────────────────────────────────────────────────────


def _rate_row(key: str, row: tuple) -> dict:
    value, observations, resolved = row
    return {key: value, "observations": observations, "resolved": resolved,
            "resolution_rate": _rate(resolved, observations)}


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def _group_key(row: dict) -> tuple[str, str, str, str]:
    return (row["demand_queue"], row["relationship_id"], row["from_entity"], row["to_entity"])


def _encode_cursor(key: Sequence[str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str, str, str]:
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        queue, relationship, from_entity, to_entity = (str(part) for part in decoded)
    except Exception as exc:                                        # noqa: BLE001 — opaque token
        raise ValueError(f"unusable observation_queues cursor {cursor!r}") from exc
    return (queue, relationship, from_entity, to_entity)


def _sample_endpoints(realizer_lists, near_side_lists, position_catalog: str) -> list[str]:
    """Realizers first, then near-side key refs — as FLAT, catalog-qualified ``logical_ref``
    strings, deduplicated and capped.

    ONE shape, deliberately. The two JSONB evidence columns arrive heterogeneous: a realizer is an
    object (and two producers write two different objects — S1B-2's ``RealizerFactV1`` and S1B-3's
    realization-gap entry), while a near-side key ref is a bare, catalog-LESS ``public.t.c`` string.
    Handing a consumer a list that is sometimes an object and sometimes a string makes every reader
    of this queue write the same normalization, and a catalog-less ref is ambiguous across catalogs
    anyway — ``public.accounts.account_id`` names a different column in every catalog that has one.

    The catalog each side is qualified BY is the fact that makes the sample useful:

    * a REALIZER is qualified by its own ``catalog_source`` — the far catalog that already does this
      crossing, which is where an engineer looks to see how;
    * a NEAR-SIDE key ref is qualified by the group's ``position_catalog`` — the catalog the demand
      is positioned in, which is where the bridge would be anchored.

    A realizer's TO-side ``to_key_ref`` is preferred over its ``to_object_ref``: the key COLUMN is
    what a bridge connects, and it is the field both producers populate. An entry that names neither
    a catalog nor a ref contributes nothing rather than a half-built string.
    """
    endpoints = [_realizer_endpoint(item)
                 for group in (realizer_lists or []) for item in (group or [])]
    endpoints += [_qualified(position_catalog, item)
                  for group in (near_side_lists or []) for item in (group or [])]
    return _dedupe(endpoint for endpoint in endpoints if endpoint)


def _realizer_endpoint(realizer) -> str:
    """A realizer is an OBJECT by contract — both producers write one. Anything else names no
    catalog, and a ref with no catalog is not an endpoint, so it contributes nothing."""
    if not isinstance(realizer, dict):
        return ""
    ref = str(realizer.get("to_key_ref") or realizer.get("to_object_ref") or "")
    return _qualified(str(realizer.get("catalog_source") or ""), ref)


def _qualified(catalog_source: str, object_ref) -> str:
    """``catalog::schema.table.column``, or "" when either half is missing or unusable. Never
    raises: an endpoint SAMPLE is advisory, and one malformed evidence entry must not take a
    governance queue down."""
    if not isinstance(object_ref, str) or not object_ref.strip() or not catalog_source.strip():
        return ""
    try:
        return qualify_object_ref(catalog_source, object_ref)
    except ValueError:
        return ""


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    sample: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        sample.append(item)
        if len(sample) == SUGGESTED_ENDPOINT_SAMPLE:
            break
    return sample


__all__ = [
    "CAPACITY_VERDICTS",
    "CARDINALITY_EVIDENCE_REQUIRED",
    "DEMAND_VERDICT_QUEUES",
    "OBSERVATION_MODES",
    "RECENT_WINDOW_DAYS",
    "RESOLVED_STATUSES",
    "STALE_REGISTRY",
    "SUGGESTED_ENDPOINT_SAMPLE",
    "claim_telemetry_work",
    "complete_telemetry_work",
    "demand_hop_columns",
    "demand_identity_hash",
    "demand_satisfaction",
    "enqueue_governed_telemetry",
    "observation_queues",
    "record_bridge_demand",
    "record_planning_observations",
    "resolution_summary",
]
