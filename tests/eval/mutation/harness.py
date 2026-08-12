"""The mutation harness: run a named victim set in a subprocess, with and without a mutation.

ONE COMMAND runs the whole thing:

    uv run --extra dev pytest -q -m eval tests/eval/mutation/

DESIGN, and why each choice is the way it is.

* **Subprocess, not monkeypatch-in-process.** A mutation has to be in place before the victim
  module is imported, and it has to be gone afterwards. A subprocess gives both for free and
  cannot leak a patched module into a later test in this session.
* **The parent's DSN is handed down.** `tests/conftest.py` boots an EPHEMERAL PostgreSQL cluster
  per session and applies migrations; letting every subprocess do that again would cost minutes.
  Passing `FEATUREGEN_TEST_DSN` makes the child reuse the parent's already-migrated database. Each
  victim still rolls its own writes back, so the child cannot corrupt the parent's fixtures.
* **The collected count is asserted, always.** `addopts` carries `-m 'not eval'`, so a victim that
  was eval-marked, renamed, or moved would be DESELECTED and the run would exit 0 — a mutation
  would then look like it "survived nothing" and the harness would report a pass. `RunResult`
  therefore parses the counts and `assert_ran` refuses a run that collected nothing.
* **The baseline is measured once per victim set**, not per mutation, because it is the same
  command; `must_survive` reuses it as its control.
* **A kill is the EXPECTED failure, not any failure.** `RunResult` keeps the child's full output so
  `test_the_mutation_is_caught` can require the registry's `expect_failure_contains` in it and
  require that a NAMED victim is among the failures. Counting any red as a kill scored a broken
  import or a malformed query as a caught invariant — which is exactly what
  `retrieval_drops_profile_context` was doing until this check went in.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tests.eval.mutation.mutations import Mutation
from tests.eval.mutation.plugin import ENV_VAR

REPO_ROOT = Path(__file__).resolve().parents[3]

_COUNT = re.compile(r"(\d+) (passed|failed|error|errors|xfailed|xpassed|skipped|deselected)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    counts: dict[str, int]
    tail: str
    #: The child's FULL stdout+stderr. Kept because the kill criterion reads the failure DETAIL, not
    #: only the counts: `tail` is the last 25 lines and the assertion that names the broken
    #: invariant is usually further up, above pytest's summary.
    output: str = ""

    @property
    def failed_node_ids(self) -> tuple[str, ...]:
        """The node ids pytest reported as FAILED/ERROR, from its own summary lines.

        ANSI-stripped first: under a color-forcing terminal env the child colorizes its summary
        even when captured, so the lines arrive as ``\x1b[31mFAILED …`` and a color-naive
        prefix match silently reports NO failures — which this harness then misreads as "the
        mutation killed nothing named". That exact defect made 26 mutations report as
        uncaught in one environment while passing in another (A0 triage, 2026-08-13)."""
        plain = _ANSI.sub("", self.output)
        return tuple(line.split(" ", 1)[1].split(" - ", 1)[0].strip()
                     for line in plain.splitlines()
                     if line.startswith(("FAILED ", "ERROR ")))

    @property
    def collected(self) -> int:
        return sum(self.counts.get(k, 0)
                   for k in ("passed", "failed", "error", "errors", "xfailed", "xpassed"))

    @property
    def all_passed(self) -> bool:
        return (self.exit_code == 0
                and self.counts.get("failed", 0) == 0
                and self.counts.get("error", 0) == 0
                and self.counts.get("errors", 0) == 0)

    @property
    def any_failed(self) -> bool:
        return (self.counts.get("failed", 0) > 0
                or self.counts.get("error", 0) > 0
                or self.counts.get("errors", 0) > 0)


def _python() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def run_victims(victims: tuple[str, ...], *, dsn: str, mutation_id: str | None = None,
                timeout_s: int = 300) -> RunResult:
    """Run exactly these node ids in a child pytest, optionally under a mutation."""
    env = dict(os.environ)
    env["FEATUREGEN_TEST_DSN"] = dsn
    env.pop("ANTHROPIC_API_KEY", None)
    env.setdefault("FEATUREGEN_AUDIT_HMAC_KEY", "test-audit-hmac-key-deterministic")
    if mutation_id:
        env[ENV_VAR] = mutation_id
        plugin_args = ["-p", "tests.eval.mutation.plugin"]
    else:
        env.pop(ENV_VAR, None)
        plugin_args = []

    # Color-proof the child BOTH ways: force no markup (a color-forcing parent terminal env
    # otherwise colorizes the captured summary) and the parser above strips ANSI regardless.
    env["NO_COLOR"] = "1"
    for var in ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE"):
        env.pop(var, None)
    proc = subprocess.run(
        # `-m ""` clears the `-m 'not eval'` in addopts: some victims (the release bars) ARE
        # eval-marked, and a deselected victim exits 0, which the harness would have to read as
        # "the mutation survived nothing".
        [_python(), "-m", "pytest", "-q", "-p", "no:randomly", "--timeout=120", "-m", "",
         "--color=no", *plugin_args, *victims],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout_s, check=False)
    out = proc.stdout + proc.stderr
    counts = {kind: int(n) for n, kind in _COUNT.findall(out)}
    return RunResult(exit_code=proc.returncode, counts=counts, output=out,
                     tail="\n".join(out.splitlines()[-25:]))


@cache
def _baseline_cached(victims: tuple[str, ...], dsn: str) -> RunResult:
    return run_victims(victims, dsn=dsn)


def baseline(mutation: Mutation, *, dsn: str) -> RunResult:
    """The unmutated control for this mutation's victims. Cached: it is the same command."""
    return _baseline_cached(mutation.victims, dsn)


def assert_ran(result: RunResult, *, what: str) -> None:
    """A run that collected nothing proves nothing — and `-m 'not eval'` makes that easy to do
    accidentally, since a deselected victim still exits 0."""
    assert result.collected > 0, (
        f"{what} collected no tests at all (counts={result.counts}). A renamed, moved or "
        f"eval-marked victim is DESELECTED and exits 0, which would read as a pass.\n{result.tail}")
