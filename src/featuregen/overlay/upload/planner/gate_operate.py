"""Phase-3C.1 — the read-only gate-operationalization harness: select an immutable batch of shadow
runs (fail-closed on provenance), and run the controlled machine checks. No durable writes; the
controlled drivers seed fixtures inside a rolled-back transaction and never touch the real catalog."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Honest denominator over the requested range: how many runs were dispatched, how many qualified,
    and — for every run that did NOT qualify — the reason it was excluded (fail-closed)."""

    dispatched_in_range: int
    qualifying: int
    excluded: dict[str, int]


@dataclass(frozen=True, slots=True)
class WindowSelection:
    run_ids: tuple[str, ...]
    coverage: CoverageReport


def select_window(conn, *, cohort: str, since: datetime, until: datetime) -> WindowSelection:
    """The immutable batch for a gate run: dispatched in ``[since, until)``, produced by ``cohort`` (never
    the ``unset`` sentinel), with ALL FOUR intent flags provably TRUE. A run missing any flag (FALSE) or
    whose flag is NULL (unprovable — legacy / pre-3C.1) is EXCLUDED and counted. Reproducible: the same
    args over the write-once store return the identical set + coverage."""
    rows = conn.execute(
        "SELECT generation_run_id, producer_commit, compile_flag, telemetry_flag,"
        " scoped_applicability_flag, ranking_flag FROM planner_shadow_dispatch"
        " WHERE created_at >= %s AND created_at < %s ORDER BY generation_run_id", (since, until)).fetchall()
    run_ids: list[str] = []
    excluded: dict[str, int] = {"wrong_cohort": 0, "flag_unprovable": 0, "flag_off": 0}
    for rid, commit, compile_f, telem_f, scoped_f, rank_f in rows:
        if commit != cohort or cohort == "unset":
            excluded["wrong_cohort"] += 1
            continue
        flags = (compile_f, telem_f, scoped_f, rank_f)
        if any(f is None for f in flags):
            excluded["flag_unprovable"] += 1
            continue
        if not all(flags):
            excluded["flag_off"] += 1
            continue
        run_ids.append(rid)
    return WindowSelection(
        run_ids=tuple(run_ids),
        coverage=CoverageReport(dispatched_in_range=len(rows), qualifying=len(run_ids), excluded=excluded))
