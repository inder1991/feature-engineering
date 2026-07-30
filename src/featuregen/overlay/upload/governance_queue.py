"""The CROSS-CATALOG governance queue — one list of every pending decision, every catalog.

WHY IT EXISTS. Every governance listing in this system is keyed by a catalog slug
(``/sources/{source}/governance/…``), so the review screen opened on a text box asking the operator
to type ``cib``. There was no answer to "what is waiting for me?" — only "what is waiting in the
catalog you already named". This read model merges the three listings across every catalog the caller
may see, so the screen can open on the work.

THE ASYMMETRY IT ABSORBS. Only one of the three sibling listings can express "all catalogs":

* :func:`~featuregen.overlay.upload.bridge_governance.list_bridge_proposals` takes
  ``source=None`` — ONE call covers every catalog (a bridge is two-source, so it has to).
* ``list_open_approved_join_proposals`` and ``list_open_table_fact_proposals_governance`` REQUIRE a
  source, so they are iterated once per catalog.

THE CATALOG SET comes from :func:`~featuregen.overlay.upload.catalogs.list_visible_catalogs` — the
same read model behind ``GET /catalogs``. NOT from ``SELECT DISTINCT catalog_source FROM
overlay_proposal`` (``governance_analytics``), which structurally never sees an entity bridge
(``overlay/projection.py:50`` skips two-source facts) and carries no visibility column.

READ-SCOPING SURVIVES THE MERGE, and the merge can only NARROW.

* Bridges are scoped by the listing itself: ``roles`` are passed through and a bridge naming a
  sensitivity-hidden endpoint column is dropped there. The queue additionally requires BOTH of a
  bridge's catalogs to be in the visible set — a pure narrowing that closes the listing's
  KEEP-if-absent rule (an endpoint with no ``graph_node`` row is not "hidden", so an un-ingested
  endpoint could otherwise name a catalog the caller may see nothing in).
* Joins and table facts have NO role scoping of their own — their only scope is the source argument.
  So the queue's scope for them IS the visible catalog set: a catalog the caller cannot see is never
  passed to them, so none of its items can be reached. There is no code path that visits a catalog
  outside :func:`list_visible_catalogs`, and the "iterate the visible set" loop is the whole
  mechanism — deleting the scope would mean deleting the loop's source of catalogs.

Consequence, deliberate and fail-closed: a proposal on a catalog in which the caller can see NO
column is absent. The queue cannot name a catalog ``GET /catalogs`` would not name.

COUNTS ARE SCOPE-RELATIVE. Two operators legitimately see different numbers for the same catalog.
Nothing here is a catalog TOTAL, and the field carrying a per-catalog number is named
``items_visible_to_you`` so it cannot be read as one.

── THE VOCABULARY IS A PRODUCT DECISION ─────────────────────────────────────────────────────────

This surface is a REVIEW, ACCOUNTABILITY AND USAGE-MONITORING surface. It is NEVER an execution
gate. Human review does not control availability and does not control execution eligibility:
production eligibility comes from AUTOMATIC validation of the directional realization (is the
crossing's cardinality resolved by a governed grain?), which is reported on its own axis and moves
independently of what any human has said.

Confirmation means "a human agrees with this semantic relationship" — never "permission to execute".
So an unreviewed relationship is labelled ``Unreviewed — available for use``: the label itself
carries the availability truth, because a bare "unreviewed" reads as "blocked" and would be false.
Two axes, never fused into one verdict:

* ``state`` — the HUMAN axis (:data:`_STATE_LABEL`): unreviewed / partially endorsed / endorsed /
  stale / rejected, each carrying its availability consequence.
* ``production_eligibility`` — the AUTOMATIC axis (:func:`_bridge_eligibility`,
  :func:`_join_eligibility`): validated for production, or sandbox-only because the cardinality is
  unresolved. ``None`` when this item kind has nothing to derive it from — never a guess.

:data:`FORBIDDEN_PHRASES` records the wordings that must never appear in this payload, in any field,
label or comment. They are all false: they assert that review gates use.

── "ALREADY DEPENDED ON BY", AND WHY IT IS NOT "BLOCKS N" ───────────────────────────────────────

The premise of a "blocks N features" badge was false: a PROPOSED bridge is consumed immediately, so
nothing is blocked by the absence of a confirmation. The honest question is the opposite one — what
already depends on this? Answered ONLY from explicit lineage: a dependency counts only when a stored
plan or artifact DIRECTLY CONTAINS the bridge's ``fact_key``. A feature that merely spans two
catalogs is not evidence that it traverses THIS bridge, and is never counted.

THE CRITICAL RULE: never display zero when the system cannot measure it. Every plan-storage table is
empty on the live database today, so a naive count returns 0 everywhere and reads as "nothing
depends on this" when the truth is "nothing has been recorded". Measurability is therefore decided
PER CATEGORY, by a probe of that category's own store, and each category answers with one of three
states:

* ``counted``           — the store holds observations; this is how many name this bridge (0 is real)
* ``not_tracked_yet``   — the store holds no observation of this kind, OR no store records the fact
                          at all (``by_construction``). NEVER rendered as 0.
* ``unreadable``        — the store could not be read. Distinct from both of the above.

See :data:`_CATEGORIES` for the exact store, anchor and probe behind each of the five categories.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from featuregen.contracts import DbConn
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.overlay.upload.bridge_governance import list_bridge_proposals
from featuregen.overlay.upload.catalogs import list_visible_catalogs
from featuregen.overlay.upload.contract.invalidation import bridge_fact_marker
from featuregen.overlay.upload.governed_grain import load_governed_grains
from featuregen.overlay.upload.join_governance import list_open_approved_join_proposals
from featuregen.overlay.upload.table_fact_governance import (
    list_open_table_fact_proposals_governance,
)
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

_LIMIT_MAX = 500

#: The schema a bridge endpoint's ``object_ref`` is flattened to when the ref carries none
#: (``upload_catalog._SCHEMA`` / ``governed_grain._FLAT_SCHEMA``).
_DEFAULT_SCHEMA = "public"

ENTITY_BRIDGE = "entity_bridge"
APPROVED_JOIN = "approved_join"

#: The kinds, in the order the queue presents them. Bridges first: they are the cross-catalog
#: decisions no per-source screen could show, which is why this surface exists.
KIND_ORDER = (ENTITY_BRIDGE, APPROVED_JOIN, "grain", "availability_time")

#: Wordings that must NEVER appear anywhere in this payload — not in a value, a key, or a comment.
#: Every one of them asserts that human review gates use, which is false: review is accountability,
#: availability is automatic. Pinned by a test that scans the rendered response for each phrase.
FORBIDDEN_PHRASES = (
    "Blocks N features",
    "Approve to enable",
    "Waiting to become usable",
    "Production approval required",
)

# ── The human axis ───────────────────────────────────────────────────────────────────────────────
# Keyed by the folded status the sibling listings already display. Each label states the human
# position AND its availability consequence, because a bare "unreviewed" reads as "blocked".
#
# DRAFT/PARTIALLY_CONFIRMED are AVAILABLE and unreviewed — `cross_catalog_links` treats exactly
# those two as available, and `analysis/grounding` discloses them as "usable, unreviewed".
# REVERIFY/STALE are the states the platform will NOT consider (a lapsed confirmation, drift), so
# they are unavailable — and that is an AUTOMATIC consequence, not a withheld approval.
_STATE_LABEL: dict[str, tuple[str, str]] = {
    "PROPOSED": ("Unreviewed — available for use", "unreviewed_available"),
    "PARTIALLY_CONFIRMED": ("Partially endorsed — available for use",
                            "partially_endorsed_available"),
    "VERIFIED": ("Human endorsed", "human_endorsed"),
    "REVERIFY": ("Stale — unavailable", "stale_unavailable"),
    "STALE": ("Stale — unavailable", "stale_unavailable"),
    "REJECTED": ("Rejected", "rejected"),
}

# ── The automatic axis ───────────────────────────────────────────────────────────────────────────
_VALIDATED = "Automatically validated for production"
_SANDBOX_ONLY = "Cardinality unresolved — sandbox only"
#: Pass C's `CardinalityInferenceStatus` member that means the direction's cardinality was resolved
#: from a CONFIRMED grain — the only one of the four that is a resolved realization.
_GRAIN_RESOLVED = "inferred_from_confirmed_grain"


# ── Usage categories: store, anchor, probe ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class _Category:
    """One "already depended on by" category and the exact rule that decides its measurability.

    ``count_sql`` is None when NO store in this system records the fact at all — the category is
    then ``not_tracked_yet`` BY CONSTRUCTION, permanently, and no probe is run. ``probe_sql`` must
    return a single boolean: did the store record ANY observation of this kind? False means
    ``not_tracked_yet``; True licenses a real count, including a real 0.
    """
    name: str
    store: str
    basis: str
    probe_sql: str | None = None
    count_sql: str | None = None
    by_construction: str = ""


#: PLANNED CANDIDATES — every assembled cross-catalog candidate plan whose operand path crossed this
#: bridge. The anchor is `multisource_compile.crossing_audit_by_slot`'s per-segment record, persisted
#: verbatim into `multisource_assembly_shadow_operand_obs.crossings` as
#: {kind, catalog, table, bridge_fact_key|realization_ref, authority, confirmed_event_id}
#: (migration 1019). `planner/assembly.py`'s `bridge_fact_key` is what lands in that field.
#:
#: The probe is "does the store hold ANY operand observation", and that is the right population: a
#: single-catalog operand observation carries `crossings = []`, which is a REAL observation that the
#: operand crossed nothing. So once any observation exists, a 0 for this bridge is honest.
_PLANNED = _Category(
    name="planned_candidates",
    store="multisource_assembly_shadow_operand_obs.crossings[].bridge_fact_key",
    basis="an assembled candidate plan whose operand path crossed this bridge",
    probe_sql="SELECT EXISTS (SELECT 1 FROM multisource_assembly_shadow_operand_obs)",
    count_sql=(
        "SELECT c->>'bridge_fact_key' AS anchor, "
        "       count(DISTINCT (o.run_id, o.intent_id, o.plan_id)) AS n "
        "FROM multisource_assembly_shadow_operand_obs o "
        "CROSS JOIN LATERAL jsonb_array_elements(o.crossings) AS c "
        "WHERE jsonb_typeof(c) = 'object' AND c->>'bridge_fact_key' = ANY(%s) "
        "GROUP BY 1"),
)

#: SELECTED PLANS — the same anchor, restricted to the plan the intent actually SELECTED
#: (`multisource_assembly_shadow_intent_result.selected_plan_id`). A separate probe: a run can record
#: candidates while selecting nothing, in which case the SELECTION store has observed nothing and a 0
#: here would be a different claim than a 0 above.
_SELECTED = _Category(
    name="selected_plans",
    store=("multisource_assembly_shadow_intent_result.selected_plan_id"
           " + multisource_assembly_shadow_operand_obs.crossings[].bridge_fact_key"),
    basis="the plan an intent selected, whose operand path crossed this bridge",
    probe_sql=(
        "SELECT EXISTS (SELECT 1 FROM multisource_assembly_shadow_operand_obs o "
        "JOIN multisource_assembly_shadow_intent_result r ON r.run_id = o.run_id "
        "  AND r.intent_id = o.intent_id AND r.selected_plan_id = o.plan_id)"),
    count_sql=(
        "SELECT c->>'bridge_fact_key' AS anchor, "
        "       count(DISTINCT (o.run_id, o.intent_id, o.plan_id)) AS n "
        "FROM multisource_assembly_shadow_operand_obs o "
        "JOIN multisource_assembly_shadow_intent_result r ON r.run_id = o.run_id "
        "  AND r.intent_id = o.intent_id AND r.selected_plan_id = o.plan_id "
        "CROSS JOIN LATERAL jsonb_array_elements(o.crossings) AS c "
        "WHERE jsonb_typeof(c) = 'object' AND c->>'bridge_fact_key' = ANY(%s) "
        "GROUP BY 1"),
)

#: GENERATED ARTIFACTS — the materialization control plane (migration 1034) is the record of what was
#: rendered and run, and it identifies a generation by HASH only (`group_plan_hash`,
#: `generated_project_hash`). No table in that plane names a bridge `fact_key`, so there is nothing to
#: count and nothing a probe could license. NOT TRACKED YET BY CONSTRUCTION — never 0.
_GENERATED = _Category(
    name="generated_artifacts",
    store="materialization control plane (migration 1034)",
    basis="a rendered generation whose lineage names this bridge",
    by_construction=("the control plane identifies a generation by group/project HASH only; no "
                     "table in it records a bridge fact_key, so generated-artifact usage of a "
                     "specific bridge is not recorded anywhere"),
)

#: PUBLISHED FEATURES — this one IS measurable, contrary to the assumption that no publication
#: lineage exists. A confirmed governed contract persists a `governed_bridge` path segment as the
#: TYPED marker `bridgefact:<fact_key>` in `contract_metadata_dependency.logical_ref`
#: (`contract/govern.py:_contract_dependency_items` -> `contract/invalidation.bridge_fact_marker`),
#: and `feature_current_contract` points a REGISTERED feature at its current contract version. So
#: "a registered feature whose current governed contract depends on this bridge" is a direct,
#: fact_key-exact lineage read.
#:
#: Honest limit, recorded rather than hidden: this is FEATURE-LEVEL lineage, not physical publication
#: to a store. The physical plane is `_GENERATED`, which has no bridge anchor. Both stores must have
#: observations for a 0 to be honest — a pointer with no lineage rows, or lineage with no pointer,
#: cannot be matched against.
_PUBLISHED = _Category(
    name="published_features",
    store="feature_current_contract + contract_metadata_dependency.logical_ref (bridgefact:<key>)",
    basis="a registered feature whose CURRENT governed contract depends on this bridge",
    probe_sql=("SELECT EXISTS (SELECT 1 FROM feature_current_contract) "
               "AND EXISTS (SELECT 1 FROM contract_metadata_dependency)"),
    count_sql=(
        "SELECT d.logical_ref AS anchor, count(DISTINCT f.feature_id) AS n "
        "FROM feature_current_contract f "
        "JOIN contract_metadata_dependency d ON d.contract_id = f.contract_id "
        "WHERE d.logical_ref = ANY(%s) "
        "GROUP BY 1"),
)

#: DATA-AGENT ANALYSES — an `AnalysisPlan` carries `join_refs` (bridge fact_keys) but is NEVER
#: persisted; there is no analysis-run store at all. The one durable data-agent table that CAN name a
#: bridge fact_key is `analysis_learning_event.subject_refs`, and it is the wrong question twice
#: over: it records GAPS ABOUT a link (grounding's JOIN_IDENTITY_UNCONFIRMED / _UNAVAILABLE), not
#: analyses that used one, and it is structurally BLIND to a link that raised no finding — so
#: counting from it would report 0 for exactly the bridges that work. NOT TRACKED YET BY
#: CONSTRUCTION.
_DATA_AGENT = _Category(
    name="data_agent_analyses",
    store="none",
    basis="an analysis whose plan traversed this bridge",
    by_construction=("AnalysisPlan.join_refs is never persisted and there is no analysis-run "
                     "store; analysis_learning_event records only GAPS about a link and is blind "
                     "to a link that raised no finding, so it cannot answer this"),
)

#: The five categories, in presentation order.
_CATEGORIES: tuple[_Category, ...] = (_PLANNED, _SELECTED, _GENERATED, _PUBLISHED, _DATA_AGENT)

_COUNTED = "counted"
_NOT_TRACKED = "not_tracked_yet"
_UNREADABLE = "unreadable"
#: The exact display string for an unmeasurable category. The rule this enforces: never a 0.
NOT_TRACKED_DISPLAY = "not tracked yet"
UNREADABLE_DISPLAY = "unreadable"


# ── Payload shapes ───────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Usage:
    """One category's answer. ``count`` is None unless ``state == "counted"`` — the type itself makes
    "unmeasured" unrepresentable as a number, so no rendering path can turn it into 0."""
    category: str
    state: str
    count: int | None
    display: str
    store: str
    basis: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One pending governance decision, whatever its kind."""
    kind: str
    fact_key: str
    catalogs: tuple[str, ...]
    subject: str
    state: str
    state_code: str
    production_eligibility: str | None
    production_eligibility_code: str
    available_actions: tuple[str, ...]
    detail: dict[str, Any] = field(default_factory=dict)
    #: Bridges only — the cross-catalog crossing is the thing whose usage is worth monitoring.
    #: ``()`` for a single-catalog join / table fact, which has no bridge anchor to count from.
    already_depended_on_by: tuple[Usage, ...] = ()


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A listing or store that could not be read. Reported so an EMPTY queue and a BROKEN queue are
    never the same answer: ``items == []`` with ``complete=True`` means "nothing is waiting";
    ``items == []`` with ``complete=False`` means "we could not look"."""
    listing: str
    source: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class GovernanceQueue:
    items: tuple[QueueItem, ...]
    #: The catalogs this caller may see — the exact set the queue was built over.
    catalogs: tuple[str, ...]
    #: Per-catalog item counts. SCOPE-RELATIVE by construction: two operators legitimately see
    #: different numbers for the same catalog, so this is never presented as a catalog total.
    items_visible_to_you_by_catalog: tuple[tuple[str, int], ...]
    items_visible_to_you_by_kind: tuple[tuple[str, int], ...]
    unreadable: tuple[Unreadable, ...]
    #: False iff anything could not be read. An honest incompleteness flag, not an error.
    complete: bool
    truncated: bool
    counts_are_scope_relative: bool = True


# ── Usage measurement ────────────────────────────────────────────────────────────────────────────

def _probe(conn: DbConn, cat: _Category) -> bool | None:
    """Did ``cat``'s store record ANY observation? None when the store could not be read.

    Runs inside a SAVEPOINT: a failing probe (a missing table in a partially-migrated database)
    poisons the transaction otherwise, and a usage probe must never break the queue it annotates.
    """
    try:
        with conn.transaction():
            row = conn.execute(cat.probe_sql).fetchone()
    except Exception:  # noqa: BLE001 — an unreadable store is a REPORTED state, never a 500
        counters.incr("overlay.governance_queue.usage_probe_unreadable")
        logger.warning("governance queue: usage store %s unreadable — reported as unreadable",
                       cat.name, exc_info=True)
        return None
    return bool(row and row[0])


def _counts(conn: DbConn, cat: _Category, anchors: Sequence[str],
            by_anchor: Mapping[str, str]) -> dict[str, int] | None:
    """``fact_key -> count`` for ``cat``, or None when the count could not be read.

    ``anchors`` are the literal values stored in the anchor column (a raw fact_key for the crossing
    stores, a ``bridgefact:`` marker for the contract-lineage store) and ``by_anchor`` maps each back
    to its fact_key, so the caller never has to parse a marker apart."""
    try:
        with conn.transaction():
            rows = conn.execute(cat.count_sql, (list(anchors),)).fetchall()
    except Exception:  # noqa: BLE001 — same rule as the probe
        counters.incr("overlay.governance_queue.usage_count_unreadable")
        logger.warning("governance queue: usage count for %s unreadable", cat.name, exc_info=True)
        return None
    out: dict[str, int] = {}
    for anchor, n in rows:
        key = by_anchor.get(anchor)
        if key is not None:
            out[key] = int(n)
    return out


def _usage_for(cat: _Category, state: str, counts: Mapping[str, int] | None,
               fact_key: str) -> Usage:
    if state == _NOT_TRACKED:
        return Usage(category=cat.name, state=_NOT_TRACKED, count=None,
                     display=NOT_TRACKED_DISPLAY, store=cat.store, basis=cat.basis,
                     reason=cat.by_construction or "this store holds no observations yet")
    if state == _UNREADABLE or counts is None:
        return Usage(category=cat.name, state=_UNREADABLE, count=None, display=UNREADABLE_DISPLAY,
                     store=cat.store, basis=cat.basis, reason="the store could not be read")
    n = counts.get(fact_key, 0)
    return Usage(category=cat.name, state=_COUNTED, count=n, display=str(n), store=cat.store,
                 basis=cat.basis)


def bridge_usage(conn: DbConn, fact_keys: Sequence[str]) -> dict[str, tuple[Usage, ...]]:
    """``fact_key -> (Usage, …)`` for every category, measured ONCE for the whole batch.

    Per-category measurability, and nothing else, decides whether a number may be shown:

    * no ``count_sql``           -> ``not_tracked_yet`` (by construction; no probe is run)
    * probe unreadable           -> ``unreadable``
    * probe False (empty store)  -> ``not_tracked_yet``  — NEVER 0
    * probe True                 -> ``counted``, and a 0 here is a real measurement: the store holds
                                    observations and none of them names this bridge

    A count query that fails after a successful probe degrades that ONE category to ``unreadable``;
    the rest of the queue is unaffected.
    """
    if not fact_keys:
        return {}
    keys = list(dict.fromkeys(fact_keys))
    raw_by_anchor = {k: k for k in keys}
    marker_by_anchor = {bridge_fact_marker(k): k for k in keys}
    per_key: dict[str, list[Usage]] = {k: [] for k in keys}
    for cat in _CATEGORIES:
        if cat.count_sql is None:
            state, counts = _NOT_TRACKED, None
        else:
            observed = _probe(conn, cat)
            if observed is None:
                state, counts = _UNREADABLE, None
            elif not observed:
                state, counts = _NOT_TRACKED, None
            else:
                by_anchor = marker_by_anchor if cat is _PUBLISHED else raw_by_anchor
                counts = _counts(conn, cat, list(by_anchor), by_anchor)
                state = _COUNTED if counts is not None else _UNREADABLE
        for key in keys:
            per_key[key].append(_usage_for(cat, state, counts, key))
    return {k: tuple(v) for k, v in per_key.items()}


# ── Per-kind item construction ───────────────────────────────────────────────────────────────────

def _state(status: str) -> tuple[str, str]:
    """The sanctioned label + code for a folded status. An unrecognized status is reported verbatim
    with an ``unknown`` code rather than guessed at — a wrong availability claim is worse than a
    missing one."""
    return _STATE_LABEL.get(status, (status, "unknown"))


def _endpoint(view: Mapping[str, Any], side: str) -> tuple[str, str, str] | None:
    """``(catalog_source, table_object_ref, column)`` for one bridge endpoint, or None when the ref
    is malformed. ``table_object_ref`` is the flattened ``<schema>.<table>`` form ``graph_node`` and
    every bridge endpoint use — the form :func:`load_governed_grains` keys on."""
    ref = view.get(side)
    if not isinstance(ref, Mapping):
        return None
    catalog = str(ref.get("catalog_source") or "").strip().lower()
    table = str(ref.get("table") or "").strip().lower()
    column = str(ref.get("column") or "").strip().lower()
    if not (catalog and table and column):
        return None
    schema = str(ref.get("schema") or "").strip().lower() or _DEFAULT_SCHEMA
    return catalog, f"{schema}.{table}", column


def bridge_endpoint_grains(conn: DbConn, views: Iterable[Mapping[str, Any]], *,
                           now: datetime) -> dict[str, dict[str, tuple[str, ...]]]:
    """The GOVERNED grain of every bridge-endpoint table, read through the ONE governed-``GRAIN``
    reader — :func:`~featuregen.overlay.upload.governed_grain.load_governed_grains`, the SAME
    function ``declarations.build_compiler_context`` calls to fill ``governed_grain_by_table``.

    Shared, not mirrored. The queue and the planner now answer "what is this table's grain?" from one
    implementation, so they cannot drift into two opinions about what "governed" means — which is the
    defect this replaces: the queue used to read ``view["left_is_grain"]``, a flag
    ``cross_catalog_links`` ORs out of the current ``graph_node`` row and the derivation-time
    ``evidence_json``, both of which ``build_graph`` writes STRAIGHT FROM AN UPLOAD.
    ``governed_grain``'s own docstring forbids that read in as many words: *"is_grain = true alone is
    a claim an uploader made about their own file. That is why nothing here ever GRANTS grain from
    the flags."*

    Only ATTESTED grains land in the result (VERIFIED, unexpired at ``now``, ``is_unique`` true,
    schema-clean, present in ``graph_node``, agreeing with its own column projection). Every refusal
    — missing, not-yet-VERIFIED, STALE, expired, scope-only uniqueness, contradictory — is an ABSENT
    key, so a caller cannot mistake a refusal for an answer.

    Fail-closed on an unreadable store: an empty map means every crossing reports ``sandbox only``,
    which understates eligibility and never over-claims it."""
    targets: list[tuple[str, str]] = []
    for view in views:
        for side in ("left", "right"):
            endpoint = _endpoint(view, side)
            if endpoint is not None:
                targets.append((endpoint[0], endpoint[1]))
    if not targets:
        return {}
    try:
        return load_governed_grains(conn, targets, now=now)
    except Exception:  # noqa: BLE001 — no grain evidence is "sandbox only", never a blank queue
        counters.incr("overlay.governance_queue.governed_grain_unreadable")
        logger.warning("governance queue: the governed grain read is unreadable — every bridge "
                       "reports an unresolved cardinality", exc_info=True)
        return {}


def _bridge_eligibility(view: Mapping[str, Any],
                        grains: Mapping[str, Mapping[str, tuple[str, ...]]],
                        ) -> tuple[str | None, str]:
    """The AUTOMATIC production axis for a bridge, independent of every human decision.

    A crossing is production-eligible when a directional realization of it is resolved: an endpoint
    is the WHOLE governed grain of ITS OWN table, which is exactly what makes the hop that lands on
    that table many-to-one. This is ``declarations._segment_cardinality``'s rule, applied to the same
    evidence through the same reader:

    * **the endpoint's OWN table.** ``_segment_cardinality`` picks the endpoint whose catalog is the
      segment's — the FAR side, the table the hop lands on — and consults only THAT table's grain,
      because *"a grain confirmed on the near side is evidence for the opposite direction"*. A queue
      item carries no hop, and an ``EntityBridgeRef`` is unordered (left/right is storage order), so
      both directions are considered; but each endpoint is still weighed ONLY against the grain of
      the table it lives in. A grain governed on one side is never credited to the column on the
      other, whatever the two columns are called.
    * **the WHOLE grain.** ``grain == (column,)`` — set equality, as the planner has it. One member
      of a composite grain ``(a, b)`` derives nothing: joining on ``a`` alone lands on every ``b``
      for that ``a``, so a bridge onto ``a`` is exactly as unbounded as one onto no key at all.
    * **governed, not declared.** ``grains`` holds only attested grains (see
      :func:`bridge_endpoint_grains`); an absent key is a refusal and reads as no evidence.

    Neither endpoint at grain means fan-out is unbounded, which is a sandbox answer, not a withheld
    approval — the human axis says nothing here either way.

    No derivation evidence at all (the W4 case: ``bridge_propose`` skipped the ledger row) -> None.
    Absence of evidence is not evidence of unresolved cardinality."""
    if not view.get("evidence_present"):
        return None, "not_observed"
    for side in ("left", "right"):
        endpoint = _endpoint(view, side)
        if endpoint is None:
            continue
        catalog, table_object_ref, column = endpoint
        grain = grains.get(catalog, {}).get(table_object_ref)
        if grain is not None and tuple(grain) == (column,):
            return _VALIDATED, "grain_resolved"
    return _SANDBOX_ONLY, "cardinality_unresolved"


def _join_eligibility(view: Mapping[str, Any]) -> tuple[str | None, str]:
    """Same axis for a discovered join, off Pass C's ``cardinality_status``: only
    ``inferred_from_confirmed_grain`` is a resolved realization; ``missing_grain`` /
    ``ambiguous_both_grains`` / ``many_to_many_risk`` are not. Unparsed evidence -> None."""
    evidence = view.get("evidence")
    status = (evidence or {}).get("grain_status") if isinstance(evidence, Mapping) else None
    if not status:
        return None, "not_observed"
    if status == _GRAIN_RESOLVED:
        return _VALIDATED, "grain_resolved"
    return _SANDBOX_ONLY, "cardinality_unresolved"


def _ref_text(ref: Mapping[str, Any] | None) -> str:
    if not isinstance(ref, Mapping):
        return ""
    parts = [str(ref.get(k) or "") for k in ("catalog_source", "table", "column")]
    return ".".join(p for p in parts if p)


def _bridge_item(view: Mapping[str, Any], usage: tuple[Usage, ...],
                 grains: Mapping[str, Mapping[str, tuple[str, ...]]]) -> QueueItem:
    state, code = _state(str(view.get("status") or ""))
    eligibility, eligibility_code = _bridge_eligibility(view, grains)
    return QueueItem(
        kind=ENTITY_BRIDGE,
        fact_key=str(view["fact_key"]),
        catalogs=tuple(str(c) for c in (view.get("catalogs") or ())),
        subject=f"{_ref_text(view.get('left'))} <-> {_ref_text(view.get('right'))}",
        state=state, state_code=code,
        production_eligibility=eligibility, production_eligibility_code=eligibility_code,
        # Server-decided, four-eyes already applied by the listing (`actor` is threaded through).
        available_actions=tuple(view.get("available_actions") or ()),
        detail={"entity_id": view.get("entity_id"), "left": view.get("left"),
                "right": view.get("right"), "data_type_family": view.get("data_type_family"),
                "type_basis": view.get("type_basis"), "strength": view.get("strength"),
                "evidence_present": view.get("evidence_present"),
                "proposed_by": view.get("proposed_by"), "proposed_at": view.get("proposed_at"),
                "target_event_id": view.get("target_event_id")},
        already_depended_on_by=usage)


def _join_actions(view: Mapping[str, Any], actor: IdentityEnvelope | None) -> tuple[str, ...]:
    """The server-sanctioned actions for a discovered join. A dual join needs TWO DISTINCT
    platform-admins (``join_confirmation._confirm_approved_join`` denies a repeat subject with
    *"this owner already confirmed; awaiting the other owner"*, which the route renders as *"You
    already approved this — a different admin must confirm."*), so a caller already recorded in
    ``approvals`` is not offered ``confirm`` — a projection of the write-side rule, not a copy.

    THE KEY IS ``subject``. ``join_governance._approvals_from_stream`` builds each entry as
    ``{"subject": payload.get("by_owner"), …}``: ``by_owner`` is the EVENT payload's field name and
    the approvals VIEW renames it. Reading ``by_owner`` off the view made ``already`` a set of empty
    strings, so the membership test never matched and this function always returned
    ``("confirm", "reject")`` — advertising an action the write side answers with a 409, which is
    the one thing this projection exists to prevent."""
    if actor is None:
        return ("confirm", "reject")
    already = {str(a.get("subject") or "") for a in (view.get("approvals") or ())
               if isinstance(a, Mapping)}
    return ("reject",) if actor.subject in already else ("confirm", "reject")


def _join_item(view: Mapping[str, Any], source: str,
               actor: IdentityEnvelope | None) -> QueueItem:
    state, code = _state(str(view.get("status") or ""))
    eligibility, eligibility_code = _join_eligibility(view)
    return QueueItem(
        kind=APPROVED_JOIN,
        fact_key=str(view["fact_key"]),
        catalogs=(source,),
        subject=str(view.get("proposed_direction") or ""),
        state=state, state_code=code,
        production_eligibility=eligibility, production_eligibility_code=eligibility_code,
        available_actions=_join_actions(view, actor),
        detail={"from": view.get("from"), "to": view.get("to"),
                "cardinality": view.get("cardinality"), "approvals": view.get("approvals"),
                "tasks": view.get("tasks"),
                "evidence_parse_status": view.get("evidence_parse_status")})


def _table_fact_item(view: Mapping[str, Any], source: str) -> QueueItem:
    state, code = _state(str(view.get("status") or ""))
    kind = str(view.get("fact_type") or "")
    return QueueItem(
        kind=kind,
        fact_key=str(view["fact_key"]),
        catalogs=(source,),
        subject=f"{source}.{view.get('table') or ''}",
        state=state, state_code=code,
        # A table's grain / availability column is not a crossing: there is no directional
        # realization to validate, so this axis has nothing to say and says nothing.
        production_eligibility=None, production_eligibility_code="not_applicable",
        # SINGLE-confirmer (the proposer is the service enrichment actor, so four-eyes holds for any
        # human) — governance.py's table-fact routes take one platform-admin confirm to VERIFIED.
        available_actions=("confirm", "reject"),
        detail={"table": view.get("table"), "proposed_value": view.get("proposed_value"),
                "origin": view.get("origin"), "advisory": view.get("advisory"),
                "task_id": view.get("task_id"), "target_event_id": view.get("target_event_id"),
                "evidence_parse_status": view.get("evidence_parse_status")})


# ── The merge ────────────────────────────────────────────────────────────────────────────────────

def governance_queue(conn: DbConn, *, roles: Iterable[str] = (),
                     actor: IdentityEnvelope | None = None,
                     limit: int = 100, usage: bool = True,
                     now: datetime | None = None) -> GovernanceQueue:
    """Every pending governance decision the caller may see, across EVERY catalog — no source
    argument. See the module docstring for the scoping, vocabulary and measurability rules.

    ``roles`` are the caller's role claims and are ALWAYS applied: there is no unscoped path. They
    decide the catalog set (:func:`list_visible_catalogs`) and are passed to the bridge listing for
    its column-level endpoint scoping. ``roles=()`` is a real caller holding nothing.

    ``actor`` decides ``available_actions`` (four-eyes projection). ``limit`` is clamped to 1..500
    and bounds the MERGED list; ``truncated`` says whether anything was cut.

    ``now`` is the instant the governed grain read judges EXPIRY against (a VERIFIED grain past its
    ``expires_at`` is refused at read time, ahead of the async poller's STALE). It defaults to the
    wall clock; a caller passes one only to pin the boundary.

    A listing that cannot be read is recorded in ``unreadable`` and clears ``complete`` — it never
    raises and never silently yields an empty queue.
    """
    limit = max(1, min(int(limit), _LIMIT_MAX))
    now = now or datetime.now(UTC)
    role_claims = tuple(roles)
    unreadable: list[Unreadable] = []

    try:
        catalogs = tuple(c.source for c in list_visible_catalogs(conn, roles=role_claims))
    except Exception:  # noqa: BLE001 — no catalog set means no queue, reported as such
        counters.incr("overlay.governance_queue.catalogs_unreadable")
        logger.warning("governance queue: the catalog list is unreadable", exc_info=True)
        return GovernanceQueue(
            items=(), catalogs=(), items_visible_to_you_by_catalog=(),
            items_visible_to_you_by_kind=(),
            unreadable=(Unreadable("catalogs", None, "the catalog list could not be read"),),
            complete=False, truncated=False)

    visible = set(catalogs)
    by_kind: dict[str, list[QueueItem]] = {k: [] for k in KIND_ORDER}

    # ── bridges: ONE call, every catalog (source=None) ───────────────────────────────────────────
    bridge_views: list[dict] = []
    try:
        bridge_views = [v for v in list_bridge_proposals(
            conn, source=None, limit=limit, roles=role_claims, actor=actor)
            # NARROWING ONLY: the listing keeps a bridge whose endpoint has no graph_node row (a
            # column with no row carries no sensitivity requirement to leak). Requiring both
            # catalogs to be VISIBLE closes that, so the queue can never name a catalog
            # `GET /catalogs` would withhold. It can only remove rows, never add one.
            if {str(c).strip().lower() for c in (v.get("catalogs") or ())} <= visible]
    except Exception:  # noqa: BLE001
        counters.incr("overlay.governance_queue.bridges_unreadable")
        logger.warning("governance queue: the bridge listing is unreadable", exc_info=True)
        unreadable.append(Unreadable(ENTITY_BRIDGE, None, "the bridge listing could not be read"))

    usage_by_key: dict[str, tuple[Usage, ...]] = {}
    if usage and bridge_views:
        usage_by_key = bridge_usage(conn, [str(v["fact_key"]) for v in bridge_views])
    # The AUTOMATIC axis, from the SAME governed-grain reader the planner uses — one batched read for
    # every endpoint table, never a per-item re-derivation and never the uploader's own is_grain flag.
    grains = bridge_endpoint_grains(conn, bridge_views, now=now)
    for view in bridge_views:
        by_kind[ENTITY_BRIDGE].append(
            _bridge_item(view, usage_by_key.get(str(view["fact_key"]), ()), grains))

    # ── joins + table facts: source is REQUIRED, so iterate the VISIBLE catalogs ──────────────────
    seen: set[tuple[str, str]] = set()
    for source in catalogs:
        try:
            joins = list_open_approved_join_proposals(conn, source, limit=limit)
        except Exception:  # noqa: BLE001 — one catalog's listing, not the whole queue
            counters.incr("overlay.governance_queue.joins_unreadable")
            logger.warning("governance queue: the join listing for %s is unreadable", source,
                           exc_info=True)
            unreadable.append(
                Unreadable(APPROVED_JOIN, source, "the join listing could not be read"))
            joins = []
        for view in joins:
            key = (APPROVED_JOIN, str(view.get("fact_key") or ""))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            by_kind[APPROVED_JOIN].append(_join_item(view, source, actor))

        try:
            facts = list_open_table_fact_proposals_governance(conn, source, limit=limit)
        except Exception:  # noqa: BLE001
            counters.incr("overlay.governance_queue.table_facts_unreadable")
            logger.warning("governance queue: the table-fact listing for %s is unreadable", source,
                           exc_info=True)
            unreadable.append(
                Unreadable("table_fact", source, "the table-fact listing could not be read"))
            facts = []
        for view in facts:
            item = _table_fact_item(view, source)
            key = (item.kind, item.fact_key)
            if not item.fact_key or key in seen or item.kind not in by_kind:
                continue
            seen.add(key)
            by_kind[item.kind].append(item)

    ordered = [item for kind in KIND_ORDER for item in by_kind[kind]]
    items = tuple(ordered[:limit])
    per_catalog: dict[str, int] = dict.fromkeys(catalogs, 0)
    for item in items:
        for source in item.catalogs:
            slug = str(source).strip().lower()
            per_catalog[slug] = per_catalog.get(slug, 0) + 1
    per_kind = {kind: sum(1 for i in items if i.kind == kind) for kind in KIND_ORDER}
    return GovernanceQueue(
        items=items, catalogs=catalogs,
        items_visible_to_you_by_catalog=tuple(sorted(per_catalog.items())),
        items_visible_to_you_by_kind=tuple((k, per_kind[k]) for k in KIND_ORDER),
        unreadable=tuple(unreadable), complete=not unreadable,
        truncated=len(ordered) > len(items))
