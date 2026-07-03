"""`python -m featuregen ...` — the production entrypoint the platform was missing (review
BLOCKER #3 for `worker`; the Task-9 review flagged the absent production migration runner for
`migrate`).

Subcommands:
  * `worker`  -> run the durable-runtime daemon (queue / relay / timers / projections) forever.
  * `migrate` -> apply all schema migrations idempotently (the deploy-time DDL runner).

`main(argv)` returns an int exit code (it never calls sys.exit itself) so it is directly testable;
the `__main__` guard translates the code into a process exit.
"""

from __future__ import annotations

import argparse
import os

import psycopg

from featuregen.db.migrations import apply_migrations
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "worker":
        run_forever(_require_dsn(args.dsn), interval=args.interval)
        return 0
    if args.command == "migrate":
        return _run_migrate(_require_dsn(args.dsn))
    return 2  # unreachable: argparse enforces a known subcommand


if __name__ == "__main__":
    raise SystemExit(main())
