"""Per-stage ingestion status — the report/recorder seam (first-release hardening design #22).

``ingest_upload`` runs a dozen-plus stages (validation, brake, fact assertion, enrichment × 3,
Pass B/C, drift, projections, glossary wiring, quarantine), several of which catch PER-ITEM
failures internally — so the single outer "ingested" was never evidence that every stage (or every
item within a stage) succeeded. Each stage now records an honest typed outcome:

* ``StageReport`` — one stage's outcome (state from the design-#22 taxonomy, a short machine
  ``reason_code``, a small ``detail`` dict of counts — never row data, never secrets).
* ``StageRecorder`` — BUFFERS reports in memory during ingest and flushes them to the
  ``ingestion_run_stage`` child table (migration 0996) only when the route terminalizes the run.
  The recorder never writes mid-ingest: stage rows commit WITH the run's terminal state, and the
  ingestion response body is untouched (flag-off byte-for-byte). Retrieval is the existing
  ``GET /ingestion-runs/{id}``.
* ``record_stage`` — the defensive call-site wrapper: a ``None`` recorder is a no-op (direct
  ``ingest_upload`` callers are byte-for-byte unchanged) and a recorder failure is warned, never
  raised — status reporting can NEVER affect the ingest it describes.

Durability mirrors ``ingestion_run``: ``flush`` runs on the given (request) connection inside a
savepoint so a flush fault cannot poison the ingest transaction; ``flush_durable`` is the route's
exception-path variant on a fresh committing connection (fallback to the request conn in the
DSN-less test harness). The fresh connection performs bare INSERTs and NEVER takes an advisory
lock (the program-audit I-3 self-deadlock class).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from featuregen.config import get_settings
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

# The design-#22 stage-state taxonomy — mirrors (and is enforced by) the 0996 CHECK constraint.
STAGE_STATES = frozenset({
    "disabled", "not_applicable", "skipped_no_client", "not_run", "running", "waiting",
    "retrying", "succeeded", "partial", "failed", "deferred", "lagged", "cancelled",
    "audit_degraded"})

# The CANONICAL ordered stage list (#13 gap B) — every stage a run can report, in execution
# order, defined ONCE. ``connector_pull`` exists only for connector-origin runs (recorded by the
# integrations import route); ``parse`` is recorded by the routes; ``manifest_finalization`` is
# appended by the flush itself when the run terminalizes. ``INGEST_STAGES`` is the slice
# ``ingest_upload`` owns — the stages an early exit must account for.
CANONICAL_STAGES: tuple[str, ...] = (
    "connector_pull", "parse",
    "validation", "brake", "fact_assertion", "drift", "glossary_classification",
    "enrich_concept", "enrich_definition", "enrich_domain", "graph_persistence",
    "governed_joins", "pass_c", "pass_b", "glossary_evidence", "projection_drain",
    "table_fact_projection", "join_projection", "semantic_binding_projection", "join_drift",
    "quarantine", "manifest_finalization")
INGEST_STAGES: tuple[str, ...] = CANONICAL_STAGES[2:-1]
# Stages that only exist for a glossary upload: at an early exit of a NON-glossary upload they
# stay honestly ``not_applicable`` — never invented as ``not_run``.
_GLOSSARY_STAGES = frozenset({"glossary_classification", "glossary_evidence"})


@dataclass(frozen=True, slots=True)
class StageReport:
    """One stage's honest outcome. ``detail`` is a SMALL dict of counts/flags (asserted,
    quarantined, unresolved …) — never row data, never secrets. ``attempt`` is assigned by the
    recorder so repeated records of one stage append (1, 2, …) instead of clobbering."""
    stage: str
    state: str
    reason_code: str | None = None
    detail: dict | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempt: int = 1


class StageRecorder:
    """Accumulates ``StageReport``s IN MEMORY; the route flushes them at terminalize time.

    Not thread-safe (one recorder per request, like the request connection it flushes on).
    ``flush`` drains the buffer on success, so a success-path flush followed by an exception-path
    ``flush_durable`` never double-writes; per-stage attempt numbering survives the drain."""

    def __init__(self) -> None:
        self._reports: list[StageReport] = []
        self._attempts: dict[str, int] = {}

    @property
    def reports(self) -> tuple[StageReport, ...]:
        return tuple(self._reports)

    def has(self, stage: str) -> bool:
        """Whether ``stage`` was ever recorded (flushed or still buffered)."""
        return stage in self._attempts

    def record(self, stage: str, state: str, *, reason_code: str | None = None,
               detail: dict | None = None, started_at: datetime | None = None) -> None:
        """Buffer one stage outcome. Raises on a state outside the taxonomy (the 0996 CHECK would
        reject it at flush time anyway — failing loud HERE keeps call sites honest; ingest call
        sites go through ``record_stage``, which contains the raise). ``started_at`` is the
        instant the stage BEGAN (depth review #13 gap A) — call sites capture it before the stage
        runs; a marker record (disabled / not_applicable / skipped / not_run) never started and
        leaves it None."""
        if state not in STAGE_STATES:
            raise ValueError(f"{state!r} is not a stage state "
                             f"(expected one of {sorted(STAGE_STATES)})")
        attempt = self._attempts.get(stage, 0) + 1
        self._attempts[stage] = attempt
        self._reports.append(StageReport(
            stage=stage, state=state, reason_code=reason_code, detail=detail,
            started_at=started_at, completed_at=datetime.now(UTC), attempt=attempt))

    def flush(self, conn, ingestion_run_id: str, *, now: datetime) -> int:
        """Write the buffered reports to ``ingestion_run_stage`` and drain the buffer; returns the
        number written. The flush rides terminalize, so it appends the run's FINAL stage itself —
        ``manifest_finalization: succeeded`` (#13 gap C) — atomically with the batch: either the
        whole account lands finalized, or nothing does. DEFENSIVE + savepointed: a flush fault
        (FK, constraint, connection) is warned and contained — it neither raises nor poisons the
        caller's transaction, and the buffer is KEPT so a later durable flush can still land the
        stages."""
        if not self._reports:
            return 0
        final_attempt = self._attempts.get("manifest_finalization", 0) + 1
        reports = [*self._reports, StageReport(
            stage="manifest_finalization", state="succeeded", started_at=now,
            completed_at=now, attempt=final_attempt)]
        try:
            with conn.transaction():   # savepoint: a fault must not abort the ingest tx
                for r in reports:
                    conn.execute(
                        "INSERT INTO ingestion_run_stage (ingestion_run_id, stage, attempt, "
                        "state, started_at, completed_at, reason_code, detail) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (ingestion_run_id, r.stage, r.attempt, r.state, r.started_at,
                         r.completed_at or now, r.reason_code,
                         Jsonb(r.detail) if r.detail is not None else None))
        except Exception:  # noqa: BLE001 — status reporting must never affect the run it describes
            logger.warning("stage-report flush failed for ingestion run %s — the run itself is "
                           "unaffected", ingestion_run_id, exc_info=True)
            # #13 gap D: NOT a silent swallow — count it and best-effort mark the run's stage
            # account as degraded (a single manifest_finalization: audit_degraded marker).
            self._mark_flush_degraded(conn, ingestion_run_id, now=now)
            return 0
        # The finalization attempt advances only when it actually WROTE, so a contained failure
        # followed by a later (durable) flush re-uses the number the failed batch never landed.
        self._attempts["manifest_finalization"] = final_attempt
        self._reports.clear()
        return len(reports)

    _DEGRADED_MARKER_SQL = (
        "INSERT INTO ingestion_run_stage (ingestion_run_id, stage, attempt, state, "
        "started_at, completed_at, reason_code) VALUES "
        "(%s, 'manifest_finalization', %s, 'audit_degraded', %s, %s, 'stage_flush_failed')")

    def _mark_flush_degraded(self, conn, ingestion_run_id: str, *, now: datetime) -> None:
        """The stage flush itself failed, so the run's account is (partly) lost — record THAT
        (#13 gap D) instead of swallowing: increment the ``flush_degraded`` counter and
        best-effort write ONE ``manifest_finalization: audit_degraded`` row. Fresh committing
        connection first (the durable-audit pattern: one bare INSERT, NEVER an advisory lock —
        the program-audit I-3 self-deadlock class); no DSN (the test harness) or a failed
        connect degrades to a fresh savepoint on the given connection (usable again — the failed
        batch's savepoint already rolled back). A lost marker is logged, never raised, and the
        buffer stays KEPT either way so a later durable flush can still land the real account."""
        counters.incr("overlay.stage_report.flush_degraded")
        attempt = self._attempts.get("manifest_finalization", 0) + 1
        params = (ingestion_run_id, attempt, now, now)
        dsn = get_settings().dsn
        if dsn:
            try:
                with psycopg.connect(dsn) as fresh:   # own tx, committed on `with` exit
                    fresh.execute(self._DEGRADED_MARKER_SQL, params)
                self._attempts["manifest_finalization"] = attempt
                return
            except Exception:  # noqa: BLE001 — best-effort by contract
                logger.exception("degraded stage-flush marker failed on a fresh connection; "
                                 "falling back to the request connection")
        try:
            with conn.transaction():   # savepoint: the marker must not poison the caller's tx
                conn.execute(self._DEGRADED_MARKER_SQL, params)
            self._attempts["manifest_finalization"] = attempt
        except Exception:  # noqa: BLE001 — the marker is best-effort; the log line remains
            logger.warning("degraded stage-flush marker lost for ingestion run %s",
                           ingestion_run_id, exc_info=True)

    def flush_durable(self, ingestion_run_id: str, *, now: datetime, fallback_conn=None) -> None:
        """``flush`` on a FRESH independent connection — the route's exception path, where the
        request transaction is rolling back and would take the stage rows down with it. Best-effort
        (the ``terminalize_run_durable`` ladder): no DSN / failed connect/flush degrades to
        ``fallback_conn`` when given (the test harness); a lost flush is logged, never raised."""
        if not self._reports:
            return
        dsn = get_settings().dsn
        if dsn:
            try:
                with psycopg.connect(dsn) as conn:   # own tx, committed on `with` exit
                    if self.flush(conn, ingestion_run_id, now=now):
                        return
            except Exception:  # noqa: BLE001 — never mask the failure that brought us here
                logger.exception(
                    "durable stage-report flush failed; falling back to the request connection")
        if fallback_conn is not None:
            self.flush(fallback_conn, ingestion_run_id, now=now)


def record_skipped_downstream(recorder: StageRecorder | None, *, reason_code: str,
                              is_glossary: bool) -> None:
    """At an ingest EARLY EXIT (structural rejection / brake hold / all-quarantined rejection),
    mark every ingest stage NOT yet recorded as ``not_run`` with the exit's reason, so the run's
    stage account is COMPLETE — a reader sees "enrich_concept: not_run", never a missing row
    (#13 gap B). The glossary stages of a non-glossary upload stay ``not_applicable`` (a stage
    that could never have run is not "skipped by the exit"). Defensive like every recorder seam:
    a ``None`` recorder no-ops and each record goes through ``record_stage`` (failure-contained)."""
    if recorder is None:
        return
    for stage in INGEST_STAGES:
        if recorder.has(stage):
            continue
        if stage in _GLOSSARY_STAGES and not is_glossary:
            record_stage(recorder, stage, "not_applicable")
        else:
            record_stage(recorder, stage, "not_run", reason_code=reason_code)


def record_stage(recorder: StageRecorder | None, stage: str, state: str, *,
                 reason_code: str | None = None, detail: dict | None = None,
                 started_at: datetime | None = None) -> None:
    """The ingest-side seam: record a stage outcome on an OPTIONAL recorder. ``None`` is a no-op
    (direct callers / flag-off byte-for-byte), and ANY recorder failure is warned and swallowed —
    status reporting can never fail (or even perturb) the ingest it describes."""
    if recorder is None:
        return
    try:
        recorder.record(stage, state, reason_code=reason_code, detail=detail,
                        started_at=started_at)
    except Exception:  # noqa: BLE001 — defensive by contract (design #22)
        logger.warning("stage-report record failed for stage %r (state %r) — ingest unaffected",
                       stage, state, exc_info=True)
