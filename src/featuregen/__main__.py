"""`python -m featuregen ...` — the production entrypoint the platform was missing (review
BLOCKER #3 for `worker`; the Task-9 review flagged the absent production migration runner for
`migrate`).

Subcommands:
  * `worker`         -> run the durable-runtime daemon (queue / relay / timers / projections) forever.
  * `migrate`        -> apply all schema migrations idempotently (the deploy-time DDL runner).
  * `pointer-repair` -> H2d: rebuild feature->current-contract pointers (legacy backfill, or a single
                        feature with --feature-id). Deterministic + idempotent + advisory-locked.
  * `backfill-projections` -> rebuild migration 1052's display projections (`graph_node.data_role`
                        and the TABLE search-document prose slots) for an ALREADY-uploaded catalog.
  * `propose-concept-parents` -> Task 9b one-off: propose an `is_a` parent for the registry concepts
                        that declare none, and EMIT a source patch. Never edits `concepts.py`, never
                        touches the database, and `--offline` completes with no provider call.

`main(argv)` returns an int exit code (it never calls sys.exit itself) so it is directly testable;
the `__main__` guard translates the code into a process exit.
"""

from __future__ import annotations

import argparse
import os

import psycopg

from featuregen.db.migrations import apply_migrations
from featuregen.overlay.upload.contract.pointer_repair import (
    backfill_feature_pointers,
    repair_feature_pointer,
)
from featuregen.runtime.observability import log
from featuregen.runtime.worker import _safe_dsn, run_forever


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="featuregen", description="FeatureGen platform runtime.")
    sub = parser.add_subparsers(dest="command", required=True)

    worker = sub.add_parser("worker", help="run the durable-runtime worker daemon")
    worker.add_argument("--dsn", default=os.environ.get("FEATUREGEN_DSN"))
    worker.add_argument("--interval", type=float, default=1.0, help="seconds between ticks")

    migrate = sub.add_parser("migrate", help="apply schema migrations (idempotent)")
    migrate.add_argument("--dsn", default=os.environ.get("FEATUREGEN_DSN"))

    repair = sub.add_parser(
        "pointer-repair",
        help="rebuild feature->current-contract pointers (H2d): legacy backfill, or --feature-id "
             "to deterministically repair one feature's pointer")
    repair.add_argument("--dsn", default=os.environ.get("FEATUREGEN_DSN"))
    repair.add_argument("--feature-id", default=None,
                        help="repair only this feature; omit to backfill ALL legacy pointers")

    backfill = sub.add_parser(
        "backfill-projections",
        help="rebuild migration 1052's display projections (graph_node.data_role + the TABLE "
             "search-document prose slots) for catalogs uploaded before it")
    backfill.add_argument("--dsn", default=os.environ.get("FEATUREGEN_DSN"))
    backfill.add_argument("--source", default=None,
                          help="rebuild only this catalog source; omit for every catalog")

    parents = sub.add_parser(
        "propose-concept-parents",
        help="Task 9b one-off: propose an is_a parent for the concepts that declare none and emit "
             "a source patch (never edits concepts.py, never connects to the database)")
    parents.add_argument("--out", default=None,
                         help="write the patch here; omit to print it on stdout")
    parents.add_argument("--offline", action="store_true",
                         help="replay the curated OFFLINE_PROPOSALS instead of calling a provider")

    return parser


def _require_dsn(dsn: str | None) -> str:
    if not dsn:
        raise SystemExit("no DSN: pass --dsn or set FEATUREGEN_DSN")
    return dsn


def _run_migrate(dsn: str) -> int:
    """Apply migrations inside one committing transaction (apply_migrations commits). Idempotent:
    already-applied unchanged migrations are skipped, drift raises."""
    with psycopg.connect(dsn) as conn:
        apply_migrations(conn)
    log("migrate.done", dsn=_safe_dsn(dsn))
    return 0


def _run_pointer_repair(dsn: str, feature_id: str | None) -> int:
    """H2d admin/recovery: rebuild the feature->current-contract pointer(s). With --feature-id, a
    DETERMINISTIC single-feature repair (rebuild from the highest valid confirmed version + refresh the
    compat projection); otherwise the ONE-TIME legacy backfill across every feature missing a pointer.
    Both are idempotent + advisory-locked. One committing transaction (mirrors `migrate`)."""
    with psycopg.connect(dsn) as conn:
        if feature_id is not None:
            repaired = repair_feature_pointer(conn, feature_id)
            conn.commit()
            log("pointer-repair.done", dsn=_safe_dsn(dsn), feature_id=feature_id, repaired=repaired)
        else:
            installed = backfill_feature_pointers(conn)
            conn.commit()
            log("pointer-repair.backfill.done", dsn=_safe_dsn(dsn), installed=installed)
    return 0


def _run_backfill_projections(dsn: str, source: str | None) -> int:
    """Make migration 1052's surfaces reachable on a catalog that was uploaded before it.

    ``graph_node.data_role`` (the search FACET reads literal graph columns and nothing else) and the
    TABLE search-document prose slots are written only during an upload. ``rebuild_search_docs`` was
    written as the named backfill seam and had NO production caller, and the ``data_role``
    projection had none either — so on a live deployment both stayed empty until somebody happened
    to re-upload. This is the caller.

    **GATE A's post-deploy smoke checklist runs this ONCE**, after `migrate` and before the
    Release-A flags are presented for approval: the new facet and the table-prose matching are
    part of what that gate is asked to sign off, and they read as broken (an empty facet, a table
    findable by nothing) until this has run against the existing catalogs.

    Per source: re-project every TABLE ref that still carries active field evidence — each inside
    its own savepoint, so one table's fault leaves the rest projected — then rebuild that catalog's
    whole search-document set through the ONE `graph._SEARCH_DOC` expression, so a backfilled
    document and a freshly-inserted one are identical by construction.

    Idempotent in what it PROJECTS (run twice, the flat columns and documents are byte-identical);
    the append-only decision log gains one RESOLVED event per resolved field per run, exactly as a
    re-upload does. Commits ONCE at the end (mirrors `migrate`), so a failed run leaves the catalog
    exactly as it was.

    Exit code: 0 when every ref projected, 1 when ANY did — a partial backfill reported as success
    would tell an operator the catalog is consistent when it is not. An unknown ``--source`` is also
    1: "0 rows, exit 0" reads a typo as a completed backfill.
    """
    from featuregen.overlay.upload.backfill_projections import (
        UnknownCatalogSource,
        backfill_projections,
    )

    with psycopg.connect(dsn) as conn:
        try:
            reports = backfill_projections(
                conn, sources=None if source is None else [source])
        except UnknownCatalogSource as exc:
            conn.rollback()
            log("backfill-projections.unknown-source", level="error", dsn=_safe_dsn(dsn),
                detail=str(exc))
            return 1
        conn.commit()

    failed = sum(r.table_refs_failed for r in reports)
    for report in reports:
        log("backfill-projections.source", level="warning" if not report.ok else "info",
            dsn=_safe_dsn(dsn), **report.as_dict())
    log("backfill-projections.done", level="error" if failed else "info", dsn=_safe_dsn(dsn),
        catalogs=len(reports),
        table_refs_projected=sum(r.table_refs_projected for r in reports),
        table_refs_failed=failed,
        search_docs_rebuilt=sum(r.search_docs_rebuilt for r in reports))
    return 1 if failed else 0


def _run_propose_concept_parents(out: str | None, offline: bool) -> int:
    """Task 9b: emit the `is_a=` additions for the concepts that declare no parent.

    NO database and NO catalog — the registry is a Python module, so the only input is
    platform-authored vocabulary and nothing customer-owned can reach a provider. It EMITS a patch
    and never edits `concepts.py`: the registry is source code, so this lands as a reviewed code
    change rather than as a governance approval step.

    `--offline` replays the curated proposal set with no provider call at all; without it, a Claude
    client is built from the environment exactly as `worker` does, and a disabled/unconfigured LLM
    is a refusal (exit 1) rather than a silent empty patch.

    Every proposal — model-generated or curated — passes `keep_valid` before it is rendered, so the
    patch cannot contain an unresolved parent, a self-parent, an overwrite of one of the authored
    parents, or a cycle (including one formed jointly by two proposals in the same run).
    `_validate_registry` remains the import-time backstop after the patch is applied.
    """
    from featuregen.overlay.upload.propose_concept_parents import (
        OFFLINE_PROPOSALS,
        keep_valid,
        parentless_records,
        propose_parents_with_reasons,
        render_patch,
    )

    records = parentless_records()
    if offline:
        proposed = len(OFFLINE_PROPOSALS)
        kept, dropped = keep_valid(OFFLINE_PROPOSALS, records)
    else:
        from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm

        llm_config = ClaudeConfig.from_env()
        if not llm_config.enabled:
            log("propose-concept-parents.no-llm", level="error",
                detail="no LLM configured; set the provider env or pass --offline")
            return 1
        kept, dropped = propose_parents_with_reasons(build_claude_llm(llm_config), list(records))
        proposed = len(kept) + len(dropped)

    patch = render_patch(kept, dropped)
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(patch)
    else:
        print(patch, end="")
    reasons: dict[str, int] = {}
    for reason in dropped.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    log("propose-concept-parents.done", source="offline" if offline else "llm",
        parentless=len(records), proposed=proposed, kept=len(kept), dropped=len(dropped),
        out=out or "-", **{f"dropped_{k}": v for k, v in sorted(reasons.items())})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "worker":
        from featuregen.intake.llm import register_llm_client
        from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm

        llm_config = ClaudeConfig.from_env()
        if llm_config.enabled:
            register_llm_client(build_claude_llm(llm_config))
        run_forever(_require_dsn(args.dsn), interval=args.interval)
        return 0
    if args.command == "migrate":
        return _run_migrate(_require_dsn(args.dsn))
    if args.command == "pointer-repair":
        return _run_pointer_repair(_require_dsn(args.dsn), args.feature_id)
    if args.command == "backfill-projections":
        return _run_backfill_projections(_require_dsn(args.dsn), args.source)
    if args.command == "propose-concept-parents":
        return _run_propose_concept_parents(args.out, args.offline)
    return 2  # unreachable: argparse enforces a known subcommand


if __name__ == "__main__":
    raise SystemExit(main())
