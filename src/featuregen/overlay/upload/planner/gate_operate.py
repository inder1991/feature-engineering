"""Phase-3C.1 — the read-only gate-operationalization harness: select an immutable batch of shadow
runs (fail-closed on provenance), and run the controlled machine checks. No durable writes; the
controlled drivers seed fixtures inside a rolled-back transaction and never touch the real catalog."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_realizations import derive_catalog_realizations
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contract_eval import (
    EvalReport,
    StabilityResult,
    double_compile_stable,
    evaluate,
)
from featuregen.overlay.upload.planner.contract_gold import (
    GOLD_CASES,
    compile_gold_case,
    run_gold_case,
)
from featuregen.overlay.upload.planner.fingerprint import _VERSIONS, compiler_input_fingerprint
from featuregen.overlay.upload.planner.replay import CurrentEvidenceV1, StoredEvidenceV1, compare
from featuregen.overlay.upload.templates import _load_columns


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


class _Rollback(Exception):
    pass


@contextlib.contextmanager
def _rolled_back(conn):
    """Run a controlled driver's fixture seeding inside a transaction that is ALWAYS rolled back — the
    computed Python result survives (it is in memory); the seeded catalog rows never persist."""
    try:
        with conn.transaction():
            yield
            raise _Rollback
    except _Rollback:
        pass


def run_gold_suite(conn) -> EvalReport:
    """Every GOLD_CASES case vs the expert answer key — the false-resolve teeth. Rolled back."""
    triples: list = []
    with _rolled_back(conn):
        triples = [run_gold_case(conn, case) for case in GOLD_CASES]
    return evaluate(triples)


def run_double_compile(conn) -> StabilityResult:
    """Compile each frozen gold fixture TWICE and compare — proves the classifier is deterministic
    (identity-comparable verdicts only; empty => unstable). Rolled back."""
    first: list = []
    second: list = []
    for case in GOLD_CASES:
        with _rolled_back(conn):
            first.append(compile_gold_case(conn, case))
        with _rolled_back(conn):
            second.append(compile_gold_case(conn, case))
    return double_compile_stable(first, second)


_DRIFT_SEED = [
    (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
]


def _fingerprint(conn) -> str:
    from types import SimpleNamespace
    mini = SimpleNamespace(
        columns_by_catalog={"core": {c.object_ref: c for c in _load_columns(conn, "core", ())}},
        realizations_by_catalog={"core": derive_catalog_realizations(conn, "core").realizations})
    return compiler_input_fingerprint(mini, "core")


def run_drift_checks(conn) -> float:
    """Fraction of controlled mutation classes the replay comparator detects (must be 1.0). Each class
    mutates a seeded catalog and asserts compare(...) is NOT `current`. Rolled back."""
    from featuregen.overlay.upload.planner.contracts import ReplayFreshness
    detected = 0
    classes = ("additivity_rebuild", "version_bump")
    for cls in classes:
        with _rolled_back(conn):
            build_graph(conn, "core", [r for r, _ in _DRIFT_SEED],
                        concepts={content_hash(r): cn for r, cn in _DRIFT_SEED})
            stored = StoredEvidenceV1(fingerprints={"core": _fingerprint(conn)},
                                      head_seqs={"core": 3}, versions=_VERSIONS)
            if cls == "additivity_rebuild":
                mutated = [
                    (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
                    (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_flow"),  # was stock
                ]
                build_graph(conn, "core", [r for r, _ in mutated],
                            concepts={content_hash(r): cn for r, cn in mutated})
                cur = CurrentEvidenceV1(fingerprints={"core": _fingerprint(conn)},
                                        head_seqs={"core": 3}, checkpoint=100, versions=_VERSIONS)
            else:  # version_bump: the producer/compiler version set changed
                cur = CurrentEvidenceV1(fingerprints={"core": _fingerprint(conn)}, head_seqs={"core": 3},
                                        checkpoint=100, versions=(*_VERSIONS, ("extra", "9.9.9")))
            if compare(stored, cur) is not ReplayFreshness.current:
                detected += 1
    return detected / len(classes)
