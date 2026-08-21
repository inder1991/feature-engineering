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

#: Ledger rows this build knowingly does not have, and WHY. Each one is a name that was applied to
#: a real database and then renamed in the source — the runner ignores a ledger row it does not
#: know, so these are stale bookkeeping, not schema this build is missing.
#:
#: ▲ An entry here is an ASSERTION that somebody looked. Anything not listed fails the check, which
#: is the point: a database genuinely ahead of the code looks exactly like this, and the difference
#: is whether a human has explained the row.
#: Each entry pins the EXACT checksum that was acknowledged, plus the migration that replaced it.
#: Accepting the name alone would clear any row that happened to share it — an unrelated database
#: carrying different SQL under the old name would pass the check that exists to notice exactly
#: that. The name is what a rename leaves behind; the checksum is what identifies the bytes.
ACKNOWLEDGED_LEDGER_ROWS: dict[str, tuple[str, str, str]] = {
    "1010_multisource_assembly_shadow": (
        "8ff56402d7a433d271e1830b3db743be3e6baa3020aad632c3aada659a55a4bc",
        "1019_multisource_assembly_shadow",
        "renamed by 86cc8b8f when main advanced to 1018; both names are in the ledger, applied "
        "2026-07-22, and 1019 is the one this build carries",
    ),
}


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


def _expected() -> dict[str, str]:
    """Every migration THIS BUILD would apply, name -> sha256 of its source.

    ▲ Built from the runner's OWN two sources — the `MIGRATIONS` tuple and `_sql_file_migrations()`
    — rather than by globbing the directory. Globbing saw only the SQL files, so drift in the 15
    Python-registered migrations was invisible, and a deleted or renamed file simply vanished from
    the comparison instead of being reported. Asking the runner what it would apply is the only
    reading that cannot disagree with what it will actually do.
    """
    from featuregen.db.migrations import MIGRATIONS, _sql_file_migrations

    return {
        name: hashlib.sha256(sql.encode()).hexdigest()
        for name, sql in [*MIGRATIONS, *_sql_file_migrations()]
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

    expected = _expected()

    drift = [
        (name, ledger[name][:12], digest[:12])
        for name, digest in sorted(expected.items())
        if name in ledger and ledger[name] != digest
    ]
    not_applied = sorted(name for name in expected if name not in ledger)
    # ▲ LEDGER ROWS THIS BUILD DOES NOT KNOW. This is the main-at-1089 / database-at-1096
    # divergence, and without it the verifier would report "no checksum drift" against `main` while
    # silently ignoring 1090-1096 — the database ahead of the code, which is the more dangerous
    # direction because nothing downstream would mention it either.
    unexpected, acknowledged = [], []
    for name, checksum in sorted(ledger.items()):
        if name in expected:
            continue
        known = ACKNOWLEDGED_LEDGER_ROWS.get(name)
        # The CHECKSUM must match, and the named replacement must actually be present — an
        # acknowledgement whose replacement this build does not carry is not an explanation.
        if known and known[0] == checksum and known[1] in expected:
            acknowledged.append(name)
        else:
            unexpected.append(name)

    print(f"ledger rows: {len(ledger)} | migrations this build would apply: {len(expected)}")
    print(f"not yet applied here: {len(not_applied)}"
          + (f" ({not_applied[:5]}…)" if not_applied else ""))

    for name in acknowledged:
        _checksum, replacement, why = ACKNOWLEDGED_LEDGER_ROWS[name]
        print(f"known extra ledger row: {name} -> {replacement} — {why}")

    if unexpected:
        print(f"\n▲ {len(unexpected)} UNEXPLAINED LEDGER ROWS THIS BUILD DOES NOT HAVE:")
        for name in unexpected[:10]:
            print(f"  {name}")
        print("\nThe database is AHEAD of this code. Deploying it here would meet a schema it does "
              "not know, and its migrate step would not reconcile the difference. Merge the "
              "migration lineage, or point this build at its own database.")

    if drift:
        print("\nCHECKSUM DRIFT — the next deploy will REFUSE to run:")
        for name, recorded, actual in drift:
            print(f"  {name}: ledger {recorded}… != source {actual}…")
        print("\nA migration is IMMUTABLE once applied. Correct it with a NEW migration; do not "
              "edit the applied file and do not move the ledger to match.")

    if drift or unexpected:
        return 1

    print("\nno checksum drift, and no ledger rows this build does not know")
    return 0


if __name__ == "__main__":
    sys.exit(main())
