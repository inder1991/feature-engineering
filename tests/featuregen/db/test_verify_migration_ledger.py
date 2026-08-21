"""The ledger verifier — the check that stops a deploy that cannot start.

It exists because migrations 1094-1096 drifted unnoticed on 2026-08-21 (see
`docs/architecture/2026-08-21-migration-checksum-drift-incident.md`). A verifier nobody tests is the
same shape of problem as a runbook step nobody runs, so these exercise it.
"""
from __future__ import annotations

import hashlib

from scripts.verify_migration_ledger import _expected, main


def _ledger_file(tmp_path, rows: dict[str, str]):
    path = tmp_path / "led.txt"
    path.write_text("\n".join(f"{name}|{csum}" for name, csum in rows.items()))
    return path


def test_the_EXPECTED_SET_COMES_FROM_THE_RUNNERS_OWN_SOURCES():
    """▲ Built from `MIGRATIONS` + `_sql_file_migrations()`, not from globbing the directory.

    Globbing saw only the SQL files, so drift in the Python-registered migrations was invisible and
    a deleted or renamed file vanished from the comparison instead of being reported.
    """
    from featuregen.db.migrations import MIGRATIONS

    expected = _expected()
    python_registered = {name for name, _ in MIGRATIONS}

    assert python_registered, "there are Python-registered migrations to miss"
    assert python_registered <= set(expected), "they must be in the compared set"
    assert len(expected) > len(python_registered), "and so must the SQL files"


def test_a_MATCHING_LEDGER_PASSES(tmp_path, capsys):
    assert main(["--ledger-file", str(_ledger_file(tmp_path, _expected()))]) == 0
    assert "no checksum drift" in capsys.readouterr().out


def test_DRIFT_IN_A_PYTHON_REGISTERED_MIGRATION_IS_CAUGHT(tmp_path, capsys):
    """The half the globbing version could not see at all."""
    from featuregen.db.migrations import MIGRATIONS

    name = MIGRATIONS[0][0]
    ledger = {**_expected(), name: hashlib.sha256(b"something else").hexdigest()}

    assert main(["--ledger-file", str(_ledger_file(tmp_path, ledger))]) == 1
    out = capsys.readouterr().out
    assert "CHECKSUM DRIFT" in out and name in out


def test_DRIFT_IN_A_SQL_MIGRATION_IS_CAUGHT(tmp_path, capsys):
    ledger = {**_expected(),
              "1096_formula_draft_retirement": hashlib.sha256(b"edited").hexdigest()}

    assert main(["--ledger-file", str(_ledger_file(tmp_path, ledger))]) == 1
    assert "1096_formula_draft_retirement" in capsys.readouterr().out


def test_a_LEDGER_ROW_THIS_BUILD_DOES_NOT_HAVE_IS_REPORTED(tmp_path, capsys):
    """▲ THE main-at-1089 / DATABASE-at-1096 DIVERGENCE. Without this the verifier would report
    "no checksum drift" against `main` while silently ignoring 1090-1096 — the database ahead of
    the code, which is the more dangerous direction because nothing else mentions it either."""
    ledger = {**_expected(), "1199_from_another_branch": hashlib.sha256(b"x").hexdigest()}

    assert main(["--ledger-file", str(_ledger_file(tmp_path, ledger))]) == 1
    out = capsys.readouterr().out
    assert "UNEXPLAINED LEDGER ROWS" in out and "1199_from_another_branch" in out
    assert "AHEAD of this code" in out


def test_an_ACKNOWLEDGED_ROW_IS_EXPLAINED_not_failed(tmp_path, capsys):
    """A permanently-red verifier trains people to ignore it. A row somebody has explained is
    reported with its reason and passes; anything unexplained still fails."""
    from scripts.verify_migration_ledger import ACKNOWLEDGED_LEDGER_ROWS

    known, (checksum, _replacement, _why) = next(iter(ACKNOWLEDGED_LEDGER_ROWS.items()))
    ledger = {**_expected(), known: checksum}

    assert main(["--ledger-file", str(_ledger_file(tmp_path, ledger))]) == 0
    assert f"known extra ledger row: {known}" in capsys.readouterr().out


def test_an_ACKNOWLEDGED_NAME_WITH_DIFFERENT_BYTES_STILL_FAILS(tmp_path, capsys):
    """▲ The acknowledgement pins the CHECKSUM, not just the name. Accepting the name alone would
    clear any row that happened to share it — an unrelated database carrying different SQL under
    the old name would pass the very check that exists to notice exactly that."""
    from scripts.verify_migration_ledger import ACKNOWLEDGED_LEDGER_ROWS

    known = next(iter(ACKNOWLEDGED_LEDGER_ROWS))
    ledger = {**_expected(), known: hashlib.sha256(b"different bytes entirely").hexdigest()}

    assert main(["--ledger-file", str(_ledger_file(tmp_path, ledger))]) == 1
    assert "UNEXPLAINED LEDGER ROWS" in capsys.readouterr().out


def test_a_MIGRATION_NOT_YET_APPLIED_IS_NOT_A_FAILURE(tmp_path, capsys):
    """A fresh database has applied nothing. That is the normal case, not drift."""
    assert main(["--ledger-file", str(_ledger_file(tmp_path, {}))]) == 0
    assert "not yet applied here" in capsys.readouterr().out
