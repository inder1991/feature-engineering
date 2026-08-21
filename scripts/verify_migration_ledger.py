"""Compare every SQL migration on disk against a database's recorded checksum.

▲ WHY THIS EXISTS. `db/migrations.apply_migrations` records a sha256 of each migration's SOURCE and
RAISES on any ledger row whose checksum no longer matches its file — deliberately, because silently
skipping an already-applied migration whose source changed would leave the schema wrong. The kind
deployment's init container runs `python -m featuregen migrate`, so drift does not degrade the
service: it stops the deploy, repeatedly.

On 2026-08-21 exactly that happened, unnoticed, because migrations 1094 and 1095 were edited AFTER
being applied. Run this before any deploy.

    python scripts/verify_migration_ledger.py "postgresql://..."
    kubectl exec -n featuregen <pg-pod> -- psql -U postgres -d featuregen -tAc \
      "SELECT name||'|'||checksum FROM schema_migrations" > /tmp/led.txt
    python scripts/verify_migration_ledger.py --ledger-file /tmp/led.txt

Exits non-zero on drift, so it can gate a pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "src/featuregen/db/migrations"


def _ledger_from_file(path: pathlib.Path) -> dict[str, str]:
    return dict(
        line.split("|", 1)
        for line in path.read_text().split("\n")
        if "|" in line
    )


def _ledger_from_dsn(dsn: str) -> dict[str, str]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        return {
            name: checksum
            for name, checksum in conn.execute(
                "SELECT name, checksum FROM schema_migrations").fetchall()
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dsn", nargs="?", help="database DSN to read schema_migrations from")
    parser.add_argument("--ledger-file", type=pathlib.Path,
                        help="a 'name|checksum' dump, for reading a ledger through kubectl exec")
    args = parser.parse_args(argv)

    if args.ledger_file:
        ledger = _ledger_from_file(args.ledger_file)
    elif args.dsn:
        ledger = _ledger_from_dsn(args.dsn)
    else:
        parser.error("give a DSN or --ledger-file")
        return 2

    drift, missing = [], []
    files = sorted(MIGRATIONS.glob("*.sql"))
    for path in files:
        digest = hashlib.sha256(path.read_text().encode()).hexdigest()
        if path.stem not in ledger:
            missing.append(path.stem)
        elif ledger[path.stem] != digest:
            drift.append((path.stem, ledger[path.stem][:12], digest[:12]))

    print(f"ledger rows: {len(ledger)} | sql migrations on disk: {len(files)}")
    # A file with no ledger row is NORMAL for anything not yet applied to this database, so it is
    # reported and never treated as a failure. Drift is the deploy-stopping condition.
    print(f"not yet applied here: {len(missing)}" + (f" ({missing[:5]}…)" if missing else ""))
    if drift:
        print("\nCHECKSUM DRIFT — the next deploy will REFUSE to run:")
        for name, recorded, actual in drift:
            print(f"  {name}: ledger {recorded}… != source {actual}…")
        print("\nA migration is IMMUTABLE once applied. Correct it with a NEW migration; do not "
              "edit the applied file and do not move the ledger to match.")
        return 1

    print("\nno checksum drift")
    return 0


if __name__ == "__main__":
    sys.exit(main())
