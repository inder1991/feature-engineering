"""Governance analytics read model (Phase 4, Task 1) — READ-ONLY dashboard rollups.

Folds the governed pipeline's three stores into the one dashboard shape the governance API
(Task 2) serves and the React dashboard (Task 3) renders:

* **Enumerate + fold** — the ``overlay_proposal`` read model enumerates every governed fact
  (``approved_join`` / ``grain`` / ``availability_time``, ANY status); each fact's LIVE status is
  folded from its event stream (``fold_overlay_state(load_fact(conn, fact_key)).status``) — a
  lagging read-model ``status`` column is never trusted for liveness (the readiness.py pattern).
  The one loaded stream also yields the reject ``category`` (the last ``OVERLAY_FACT_REJECTED``
  event's payload) and the CONFIRMED/REJECTED timestamps for recent activity — no double load.
* **Queue health** — open ``human_tasks`` depth + age buckets (``lt_1d``/``1_7d``/``gt_7d``).
  ``human_tasks`` has no ``catalog_source`` column, so EVERY scope — the catalog view included —
  filters by the scope's enumerated governed fact_keys. Non-governed open tasks (e.g. the 3B.2B
  ``entity_bridge`` gate tasks, which never enter ``overlay_proposal``) are excluded, so the
  headline ``open_depth`` always reconciles with the rollups + the per-source rows.
* **Calibration seed** — the ``pass_c_candidate_evidence`` ledger (bucket + evidence_json) joined
  with the FOLDED outcome per fact_key: per-bucket confirm rates, and reject categories attributed
  to the top-``score_delta`` positive signal. The Phase-4 calibration/HITL input.

FAIL-SOFT (the module invariant): every per-row read — a fact stream, a ledger evidence_json, a
task timestamp — is guarded; a corrupt row is skipped + counted
(``overlay.governance_analytics.<kind>``) and NEVER blanks the dashboard.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from featuregen.contracts import DbConn
from featuregen.overlay.facts import OVERLAY_FACT_CONFIRMED, OVERLAY_FACT_REJECTED
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

# The governed fact types this dashboard reports — always all three, zeros when none exist.
_GOVERNED_FACT_TYPES = ("approved_join", "grain", "availability_time")

# Folded-status buckets (Global Constraints mapping). REVERIFY/STALE are demotions awaiting
# re-confirmation — surfaced separately so "pending" stays the first-decision queue.
_PENDING = frozenset({"DRAFT", "PARTIALLY_CONFIRMED"})
_NEEDS_ATTENTION = frozenset({"REVERIFY", "STALE"})

_RECENT_ACTIVITY_DAYS = 7
_DAY_SECONDS = 86_400.0
_AGE_BUCKETS = ("lt_1d", "1_7d", "gt_7d")


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


# ── Produced shapes (Tasks 2/3 depend on these exact names) ──────────────────────────────────────


@dataclass(frozen=True)
class FactTypeRollup:
    """Counts for ONE governed fact type, keyed off each fact's folded status."""
    fact_type: str
    pending: int = 0
    confirmed: int = 0
    rejected: int = 0
    needs_attention: int = 0
    rejected_by_category: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class QueueHealth:
    """Open human-task depth + age distribution for the scope."""
    open_depth: int
    oldest_pending_age_seconds: int | None
    age_buckets: dict[str, int]


@dataclass(frozen=True)
class CalibrationSeed:
    """The Pass-C ledger joined with folded outcomes (approved_join only)."""
    confirm_rate_by_bucket: dict[str, dict]
    reject_category_by_top_signal: dict[str, dict[str, int]]


@dataclass(frozen=True)
class RecentActivity:
    """CONFIRMED/REJECTED decision volume inside the trailing window."""
    days: int
    confirmed: int
    rejected: int


@dataclass(frozen=True)
class SourceSummary:
    """One source's compact governance posture (the catalog-level source list)."""
    source: str
    pending: int
    confirmed: int
    rejected: int
    oldest_pending_age_seconds: int | None


@dataclass(frozen=True)
class GovernanceDashboard:
    scope: str                       # "source" | "catalog"
    source: str | None               # normalized, None for the cross-source view
    generated_at: str                # ISO timestamp
    fact_types: tuple[FactTypeRollup, ...]
    queue_health: QueueHealth
    calibration_seed: CalibrationSeed
    recent_activity: RecentActivity


# ── Internal collection (shared by both public functions) ────────────────────────────────────────


@dataclass(frozen=True)
class _FactRecord:
    """One enumerated governed fact with everything the rollups need off its ONE loaded stream."""
    fact_key: str
    fact_type: str
    source: str                          # normalized catalog_source
    status: str                          # folded live status
    reject_category: str | None          # last REJECTED event's payload category (raw; nullable)
    confirmed_at: tuple[datetime, ...]   # occurred_at of every OVERLAY_FACT_CONFIRMED
    rejected_at: tuple[datetime, ...]    # occurred_at of every OVERLAY_FACT_REJECTED


def _collect_fact_records(conn: DbConn, source_norm: str | None) -> list[_FactRecord]:
    """Enumerate the governed facts from ``overlay_proposal`` and fold each stream ONCE.

    Fail-soft per row: an unreadable/streamless fact is skipped + counted — never raised."""
    records: list[_FactRecord] = []
    for fact_type in _GOVERNED_FACT_TYPES:
        rows = conn.execute(
            "SELECT fact_key, catalog_source FROM overlay_proposal WHERE fact_type = %s",
            (fact_type,),
        ).fetchall()
        for fk, csource in rows:
            try:
                src = _norm(csource)
                if source_norm is not None and src != source_norm:
                    continue
                stream = load_fact(conn, fk)
                status = fold_overlay_state(stream).status
                if status is None:
                    counters.incr("overlay.governance_analytics.fact_unreadable")
                    logger.warning(
                        "governance analytics: fact %s has no foldable stream — skipped", fk)
                    continue
                reject_category = None
                rejected_events = [e for e in stream if e.type == OVERLAY_FACT_REJECTED]
                if status == "REJECTED" and rejected_events:
                    reject_category = rejected_events[-1].payload.get("category")
                records.append(_FactRecord(
                    fact_key=fk, fact_type=fact_type, source=src, status=status,
                    reject_category=reject_category,
                    confirmed_at=tuple(e.occurred_at for e in stream
                                       if e.type == OVERLAY_FACT_CONFIRMED),
                    rejected_at=tuple(e.occurred_at for e in rejected_events),
                ))
            except Exception:  # noqa: BLE001 — ONE corrupt row must never blank the dashboard
                counters.incr("overlay.governance_analytics.fact_unreadable")
                logger.warning("governance analytics: fact %s unreadable — skipped",
                               fk, exc_info=True)
    return records


def _rollups(records: list[_FactRecord]) -> tuple[FactTypeRollup, ...]:
    """One rollup per governed fact type — ALWAYS all three, zeros when none exist."""
    out: list[FactTypeRollup] = []
    for fact_type in _GOVERNED_FACT_TYPES:
        pending = confirmed = rejected = needs_attention = 0
        by_category: dict[str, int] = {}
        for rec in records:
            if rec.fact_type != fact_type:
                continue
            if rec.status in _PENDING:
                pending += 1
            elif rec.status == "VERIFIED":
                confirmed += 1
            elif rec.status == "REJECTED":
                rejected += 1
                category = rec.reject_category or "uncategorized"
                by_category[category] = by_category.get(category, 0) + 1
            elif rec.status in _NEEDS_ATTENTION:
                needs_attention += 1
        out.append(FactTypeRollup(fact_type, pending, confirmed, rejected, needs_attention,
                                  by_category))
    return tuple(out)


def _queue_health(conn: DbConn, fact_keys: list[str], now: datetime) -> QueueHealth:
    """Open-task depth + age buckets for the scope's ENUMERATED governed fact_keys — every scope
    (catalog included) filters by fact_key, because ``human_tasks`` has no catalog_source column
    AND holds non-governed tasks (e.g. entity_bridge gates) that must never inflate the governed
    queue. An EMPTY list short-circuits to zeros (no governed facts -> no queue)."""
    if not fact_keys:
        rows = []
    else:
        rows = conn.execute(
            "SELECT created_at FROM human_tasks WHERE status = 'open' AND fact_key = ANY(%s)",
            (fact_keys,),
        ).fetchall()
    depth = 0
    oldest: float | None = None
    buckets = dict.fromkeys(_AGE_BUCKETS, 0)
    for (created_at,) in rows:
        try:
            age = max((now - created_at).total_seconds(), 0.0)
        except Exception:  # noqa: BLE001 — a bad timestamp skips its row, not the queue
            counters.incr("overlay.governance_analytics.task_unreadable")
            logger.warning("governance analytics: open task with unusable created_at — skipped",
                           exc_info=True)
            continue
        depth += 1
        if oldest is None or age > oldest:
            oldest = age
        if age < _DAY_SECONDS:
            buckets["lt_1d"] += 1
        elif age <= 7 * _DAY_SECONDS:
            buckets["1_7d"] += 1
        else:
            buckets["gt_7d"] += 1
    return QueueHealth(open_depth=depth,
                       oldest_pending_age_seconds=int(oldest) if oldest is not None else None,
                       age_buckets=buckets)


def _top_signal(evidence_json) -> str | None:
    """The top-``score_delta`` positive signal's name, or None when unparseable/absent."""
    if not isinstance(evidence_json, dict):
        return None
    signals = evidence_json.get("positive_signals")
    if not isinstance(signals, list):
        return None
    best_name: str | None = None
    best_delta: float | None = None
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        name = signal.get("signal_name")
        delta = signal.get("score_delta")
        if not isinstance(name, str) or isinstance(delta, bool) \
                or not isinstance(delta, int | float):
            continue
        if best_delta is None or delta > best_delta:
            best_name, best_delta = name, delta
    return best_name


def _calibration_seed(
    conn: DbConn, source_norm: str | None, join_records: dict[str, _FactRecord]
) -> CalibrationSeed:
    """Join the Pass-C ledger's fact-bearing rows with the ALREADY-FOLDED join outcomes
    (``join_records`` — never re-fold). Only decided outcomes (VERIFIED/REJECTED) tally; a
    REJECTED join with a structured category attributes it to the evidence's top positive
    signal. Empty dicts when there is no ledger data for the scope."""
    rows = conn.execute(
        "SELECT catalog_source, fact_key, bucket, evidence_json FROM pass_c_candidate_evidence"
        " WHERE fact_key IS NOT NULL",
    ).fetchall()
    tallies: dict[str, dict[str, int]] = {}
    by_top_signal: dict[str, dict[str, int]] = {}
    for csource, fk, bucket, evidence_json in rows:
        try:
            if source_norm is not None and _norm(csource) != source_norm:
                continue
            record = join_records.get(fk)
            if record is None:
                continue  # ledger points outside the enumerated read model — nothing to correlate
            if record.status == "VERIFIED":
                outcome = "confirmed"
            elif record.status == "REJECTED":
                outcome = "rejected"
            else:
                continue  # still in flight — no outcome to calibrate against
            tally = tallies.setdefault(str(bucket), {"confirmed": 0, "rejected": 0})
            tally[outcome] += 1
            if outcome == "rejected" and record.reject_category:
                top = _top_signal(evidence_json)
                if top is None:
                    counters.incr("overlay.governance_analytics.evidence_unparseable")
                    logger.warning("governance analytics: ledger evidence for fact %s has no "
                                   "parseable top signal — reject attribution skipped", fk)
                else:
                    categories = by_top_signal.setdefault(top, {})
                    categories[record.reject_category] = \
                        categories.get(record.reject_category, 0) + 1
        except Exception:  # noqa: BLE001 — one bad ledger row must never blank the seed
            counters.incr("overlay.governance_analytics.evidence_unparseable")
            logger.warning("governance analytics: ledger row for fact %s unreadable — skipped",
                           fk, exc_info=True)
    confirm_rate_by_bucket: dict[str, dict] = {}
    for bucket, tally in tallies.items():
        total = tally["confirmed"] + tally["rejected"]
        confirm_rate_by_bucket[bucket] = {
            "confirmed": tally["confirmed"],
            "rejected": tally["rejected"],
            "rate": (tally["confirmed"] / total) if total else None,
        }
    return CalibrationSeed(confirm_rate_by_bucket=confirm_rate_by_bucket,
                           reject_category_by_top_signal=by_top_signal)


def _recent_activity(
    records: list[_FactRecord], now: datetime, days: int = _RECENT_ACTIVITY_DAYS
) -> RecentActivity:
    """Decision volume in the trailing window, off the already-loaded streams' timestamps."""
    cutoff = now - timedelta(days=days)
    confirmed = rejected = 0
    for rec in records:
        try:
            confirmed += sum(1 for ts in rec.confirmed_at if ts >= cutoff)
            rejected += sum(1 for ts in rec.rejected_at if ts >= cutoff)
        except Exception:  # noqa: BLE001 — an uncomparable timestamp skips its fact, not the tally
            counters.incr("overlay.governance_analytics.activity_unreadable")
            logger.warning("governance analytics: fact %s has uncomparable event timestamps — "
                           "recent activity skipped", rec.fact_key, exc_info=True)
    return RecentActivity(days=days, confirmed=confirmed, rejected=rejected)


# ── Public API ───────────────────────────────────────────────────────────────────────────────────


def compute_governance_dashboard(
    conn: DbConn, *, source: str | None = None, now: datetime | None = None
) -> GovernanceDashboard:
    """The read-only governance dashboard for one source (``source`` given, normalized
    strip+lower) or the whole catalog (``source=None``). An unknown source yields an all-zeros
    dashboard, NOT an error — the UI renders an empty state."""
    now = now or datetime.now(UTC)
    source_norm = _norm(source) if source else None
    records = _collect_fact_records(conn, source_norm)
    join_records = {r.fact_key: r for r in records if r.fact_type == "approved_join"}
    return GovernanceDashboard(
        scope="source" if source else "catalog",
        source=source_norm,
        generated_at=now.isoformat(),
        fact_types=_rollups(records),
        # BOTH scopes pass the enumerated governed fact_keys (`records` is already cross-source
        # when source_norm is None): the catalog headline counts ONLY governed-fact tasks and
        # reconciles with the rollups + the sum of the per-source queues.
        queue_health=_queue_health(conn, [r.fact_key for r in records], now),
        calibration_seed=_calibration_seed(conn, source_norm, join_records),
        recent_activity=_recent_activity(records, now),
    )


def list_source_governance_summaries(
    conn: DbConn, *, now: datetime | None = None
) -> tuple[SourceSummary, ...]:
    """One compact SourceSummary per distinct ``overlay_proposal`` catalog_source (normalized;
    sorted). Shares the enumerate+fold collection with ``compute_governance_dashboard`` — one
    pass over all sources, grouped, rather than a re-fold per source."""
    now = now or datetime.now(UTC)
    sources = sorted({
        _norm(row[0])
        for row in conn.execute(
            "SELECT DISTINCT catalog_source FROM overlay_proposal").fetchall()
        if _norm(row[0])
    })
    records = _collect_fact_records(conn, None)
    by_source: dict[str, list[_FactRecord]] = {}
    for rec in records:
        by_source.setdefault(rec.source, []).append(rec)
    summaries: list[SourceSummary] = []
    for src in sources:
        recs = by_source.get(src, [])
        health = _queue_health(conn, [r.fact_key for r in recs], now)
        summaries.append(SourceSummary(
            source=src,
            pending=sum(1 for r in recs if r.status in _PENDING),
            confirmed=sum(1 for r in recs if r.status == "VERIFIED"),
            rejected=sum(1 for r in recs if r.status == "REJECTED"),
            oldest_pending_age_seconds=health.oldest_pending_age_seconds,
        ))
    return tuple(summaries)
