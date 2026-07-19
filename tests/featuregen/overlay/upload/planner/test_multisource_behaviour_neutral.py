"""Phase 3C.2b-i-A · Task 13 — behaviour-neutrality golden test (TEST-ONLY, no source change).

Formalizes design §12: the whole multi-source shadow engine (Tasks 1-12) is purely ADDITIVE — it
never edits, monkeypatches, or otherwise perturbs the single-source ``plan_bindings`` frontier.
Three independent proofs:

  1. STATIC — every BEHAVIOURAL reused engine file (``assembly.py``, ``declarations.py``,
     ``plan.py``, ``enumerate.py``, ``candidates.py``, ``order.py``, ``scope.py``) is byte-identical
     to ``origin/main`` at this branch's merge-base (``git diff <merge-base> HEAD -- <file>`` is
     empty). ``contracts.py`` DID change — it carries the new ``MULTISOURCE_*``/``MAX_*``
     constants — so it is checked separately: its branch diff may only ever APPEND lines, never
     remove or change an existing one.
  2. RUNTIME — a representative single-source ``plan_bindings`` run over a small governed
     single-catalog fixture (the ``test_plan.py`` pattern) produces byte-identical identity-bearing
     fields whether captured in a FRESH subprocess interpreter that imports ONLY the single-source
     planner (never any ``multisource_*`` module), or in THIS process (where every ``multisource_*``
     module has been imported) — proving the shadow engine's mere presence never perturbs a
     single-source result.

     The baseline runs in a subprocess rather than being captured "before this process imports any
     multisource_* module", because that in-process ordering is not a reliable precondition: pytest's
     COLLECTION phase imports every sibling ``test_multisource_*.py`` module — which import the
     production ``multisource_*`` modules at module scope — before ANY test body runs. So under
     ``uv run pytest tests/featuregen/overlay/upload/planner/ -q`` (and under a full-tree
     ``uv run pytest -q``), by the time this test's body executes the multisource modules are already
     in ``sys.modules``; only running the file in total isolation ever satisfied the old
     "before any import" snapshot. A fresh subprocess sidesteps collection order entirely — it never
     imports the sibling test modules, so it is immune to which command/directory pytest was invoked
     from.
  3. NO IMPORT-TIME SIDE EFFECT — every ``multisource_*`` module imports cleanly and defines no
     module-level DB/IO (static AST check: no import-time-reachable call whose name looks like a DB
     or network operation, outside a function/method body). With
     ``FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW`` unset, the CLI entrypoint ``run_shadow_cli`` is a
     no-op that opens NO connection (the Task-11 fake-``connect`` pattern) — so there is no possible
     shadow-store write on a normal (flag-off) path.

If proof 1 ever fails (a behavioural engine file WAS modified on this branch), that is a genuine
neutrality violation to ESCALATE — do not weaken this test to make it pass.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import MULTISOURCE_ASSEMBLY_SHADOW_FLAG
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import Need, Template

_NOW = datetime(2026, 7, 19, tzinfo=UTC)

# The eleven Task 1-12 `multisource_*` engine modules (design §12's "new carriers/context/store").
# Used by proof 3 below (imports every one of them, proving the in-process "after" run — which now
# has all of them loaded — still matches the subprocess baseline).
_MULTISOURCE_MODULES = (
    "featuregen.overlay.upload.planner.multisource_contracts",
    "featuregen.overlay.upload.planner.multisource_operation",
    "featuregen.overlay.upload.planner.multisource_endpoints",
    "featuregen.overlay.upload.planner.multisource_reuse",
    "featuregen.overlay.upload.planner.multisource_assembly",
    "featuregen.overlay.upload.planner.multisource_compile",
    "featuregen.overlay.upload.planner.multisource_plan",
    "featuregen.overlay.upload.planner.multisource_shadow_store",
    "featuregen.overlay.upload.planner.multisource_shadow",
    "featuregen.overlay.upload.planner.multisource_gold",
    "featuregen.overlay.upload.planner.multisource_gate",
)

# ── repo/git plumbing ──
_TEST_FILE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(subprocess.run(
    ("git", "rev-parse", "--show-toplevel"), cwd=_TEST_FILE_DIR,
    capture_output=True, text=True, check=True).stdout.strip())


def _git(*args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=_REPO_ROOT, capture_output=True, text=True, check=True)
    return proc.stdout


_MERGE_BASE = _git("merge-base", "HEAD", "origin/main").strip()


def _diff_for(rel_path: str) -> str:
    return _git("diff", _MERGE_BASE, "HEAD", "--", rel_path)


def _removed_lines(diff: str) -> list[str]:
    """Every true removal line in a unified diff (a line starting with ``-`` AFTER the first ``@@``
    hunk header) — i.e. excluding the ``--- a/<path>`` file-header line, which also starts with
    ``-`` but is not a content removal."""
    lines = diff.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            return [ln for ln in lines[i:] if ln.startswith("-")]
    return []   # no hunks at all -> nothing changed


# ── 1. STATIC — behavioural engine files byte-identical; contracts.py additive-only ──
_BEHAVIOURAL_ENGINE_FILES = (
    "src/featuregen/overlay/upload/planner/assembly.py",
    "src/featuregen/overlay/upload/planner/declarations.py",
    "src/featuregen/overlay/upload/planner/plan.py",
    "src/featuregen/overlay/upload/planner/enumerate.py",
    "src/featuregen/overlay/upload/planner/candidates.py",
    "src/featuregen/overlay/upload/planner/order.py",
    "src/featuregen/overlay/upload/planner/scope.py",
)
_CONTRACTS_FILE = "src/featuregen/overlay/upload/planner/contracts.py"


def test_behavioural_engine_files_are_byte_identical_to_origin_main_at_branch_point():
    for rel_path in _BEHAVIOURAL_ENGINE_FILES:
        diff = _diff_for(rel_path)
        assert diff == "", (
            f"NEUTRALITY VIOLATION: {rel_path} was modified on this branch relative to the "
            f"origin/main branch point {_MERGE_BASE} — this file carries single-source planner "
            f"behaviour and design §12 requires it stay byte-identical. Diff:\n{diff}")


def test_contracts_file_branch_diff_is_additive_only():
    diff = _diff_for(_CONTRACTS_FILE)
    removed = _removed_lines(diff)
    assert not removed, (
        f"NEUTRALITY VIOLATION: {_CONTRACTS_FILE} removed or changed an existing line — this "
        "branch may only APPEND new MULTISOURCE_*/MAX_* constants to it (design §12). "
        f"Removed/changed lines:\n" + "\n".join(removed))
    # sanity: the branch DID append the expected constants (a no-op diff would silently pass the
    # "no removals" check above without proving anything was actually appended-and-checked).
    added = [ln for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert any("MULTISOURCE_ASSEMBLY_SHADOW_FLAG" in ln for ln in added), (
        "expected contracts.py's branch diff to append MULTISOURCE_ASSEMBLY_SHADOW_FLAG")


# ── 2. RUNTIME — single-source plan_bindings unaffected by importing the multisource modules ──
def _seed_single_catalog(db, source: str) -> None:
    """Mirrors ``test_plan.py``'s ``_catalog`` helper: one governed single-catalog fixture (a
    customer-grain accounts table with a monetary-stock measure) via the real graph write path,
    plus a fresh drift watermark so the catalog is in-scope."""
    catalog = [
        (CanonicalRow(source, "accounts", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow(source, "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
    ]
    build_graph(db, source, [r for r, _ in catalog], concepts={content_hash(r): c for r, c in catalog})
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES (%s, %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, _NOW, _NOW))


def _tmpl() -> Template:
    return Template(id="t_bal_neutrality", family="f", intent="i",
                    needs=(Need(role="stock_col", concept="monetary_stock"),
                           Need(role="entity", concept="customer_id")),
                    params={}, aggregation="avg", additivity="semi_additive", explain="M",
                    use_cases=(), pit="trailing")


def _identity_snapshot(result) -> dict:
    """The identity-bearing subset of a ``BindingPlanningResultV1`` (design §12's golden fields):
    selected plan, every candidate's physical id, the result/reason vocabulary, and the bounding
    metrics — deliberately NOT the whole dataclass (``catalog_scope_id``/``replay_envelope`` carry
    provenance, not identity). Returned already JSON-safe (str-cast StrEnums, lists not tuples, a
    plain dict for ``bounding``) so it compares equal, field-for-field, to the parsed JSON emitted by
    the subprocess baseline script below — both sides go through the identical normalization."""
    return {
        "selected_plan_id": result.selected_plan_id,
        "candidate_physical_plan_ids": [p.physical_plan_id for p in result.candidate_plans],
        "candidate_resolution_statuses": [str(p.resolution_status) for p in result.candidate_plans],
        "result_status": str(result.result_status),
        "primary_reason_code": (str(result.primary_reason_code)
                                if result.primary_reason_code is not None else None),
        "reason_codes": [str(c) for c in result.reason_codes],
        "bounding": dataclasses.asdict(result.bounding),
    }


# A hermetic, self-contained script for a FRESH `uv run python -c` interpreter: it imports ONLY the
# single-source planner (never any test module, and in particular never any sibling
# `test_multisource_*.py` — the thing that makes an in-process "before" snapshot impossible, since
# pytest's collection phase has already imported all of those, and therefore every `multisource_*`
# production module, before this test's body runs). It duplicates `_seed_single_catalog`/`_tmpl`
# above rather than importing them, so the baseline interpreter's import graph is provably minimal —
# not merely "happens not to import multisource_* today".
#
# Takes the DSN on stdin (not argv, since a libpq keyword/value conninfo string contains spaces) and
# prints one line of JSON — the same identity snapshot shape `_identity_snapshot` builds in-process —
# to stdout. Never commits: the seeded rows live only in this process's own uncommitted transaction
# and vanish when the connection is rolled back and closed, mirroring the repo's `conn` fixture.
_SUBPROCESS_BASELINE_SCRIPT = """
import dataclasses
import json
import sys
from datetime import UTC, datetime

import psycopg

# Sanity check on the baseline interpreter itself: nothing has pulled in a multisource_* module
# merely by starting up (mirrors the intent of the old in-process "before" assertion, but as a
# guarantee about THIS fresh interpreter rather than an assumption about pytest's collection order).
assert not any(name.startswith("featuregen.overlay.upload.planner.multisource_")
              for name in sys.modules), "a multisource module leaked into the baseline interpreter"

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import Need, Template

assert not any(name.startswith("featuregen.overlay.upload.planner.multisource_")
              for name in sys.modules), "importing the single-source planner pulled in a multisource module"

NOW = datetime(2026, 7, 19, tzinfo=UTC)
SOURCE = "core_neutrality"

dsn = sys.stdin.read().strip()
conn = psycopg.connect(dsn)
try:
    catalog = [
        (CanonicalRow(SOURCE, "accounts", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow(SOURCE, "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
    ]
    build_graph(conn, SOURCE, [r for r, _ in catalog], concepts={content_hash(r): c for r, c in catalog})
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES (%s, %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (SOURCE, NOW, NOW))

    template = Template(id="t_bal_neutrality", family="f", intent="i",
                        needs=(Need(role="stock_col", concept="monetary_stock"),
                               Need(role="entity", concept="customer_id")),
                        params={}, aggregation="avg", additivity="semi_additive", explain="M",
                        use_cases=(), pit="trailing")

    scope = resolve_catalog_scope(conn, roles=(), target_entity="customer", now=NOW)
    result = plan_bindings(conn, template=template, target_entity="customer", scope=scope, roles=(), now=NOW)

    snapshot = {
        "selected_plan_id": result.selected_plan_id,
        "candidate_physical_plan_ids": [p.physical_plan_id for p in result.candidate_plans],
        "candidate_resolution_statuses": [str(p.resolution_status) for p in result.candidate_plans],
        "result_status": str(result.result_status),
        "primary_reason_code": (str(result.primary_reason_code)
                                if result.primary_reason_code is not None else None),
        "reason_codes": [str(c) for c in result.reason_codes],
        "bounding": dataclasses.asdict(result.bounding),
    }
finally:
    conn.rollback()
    conn.close()

print(json.dumps(snapshot))
"""


def _run_baseline_subprocess(dsn: str) -> dict:
    proc = subprocess.run(
        ("uv", "run", "python", "-c", _SUBPROCESS_BASELINE_SCRIPT),
        cwd=_REPO_ROOT, input=dsn, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"baseline subprocess exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_single_source_plan_bindings_identical_before_and_after_multisource_import(db, _dsn):
    """The RUNTIME proof (design §12): capture a representative single-source ``plan_bindings`` run
    in a FRESH subprocess interpreter that has imported ONLY the single-source planner, then AGAIN in
    THIS process after importing every ``multisource_*`` module — the identity-bearing fields must be
    byte-for-byte identical. This proves the shadow engine's mere presence never perturbs a
    single-source result, independent of pytest's collection/import order (see the module docstring
    and ``_SUBPROCESS_BASELINE_SCRIPT`` for why an in-process "before any import" snapshot can't be
    relied on once this file sits next to its `test_multisource_*.py` siblings)."""
    baseline_snapshot = _run_baseline_subprocess(_dsn)

    _seed_single_catalog(db, "core_neutrality")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)

    for module_name in _MULTISOURCE_MODULES:
        importlib.import_module(module_name)
    assert all(name in sys.modules for name in _MULTISOURCE_MODULES)   # the import actually happened

    after = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    after_snapshot = _identity_snapshot(after)

    assert baseline_snapshot == after_snapshot
    # and the run resolved at all (a vacuous "both empty" comparison would prove nothing)
    assert after_snapshot["selected_plan_id"] is not None


# ── 3. NO IMPORT-TIME SIDE EFFECT ──
# Substrings of a Call's function name that look like DB/network I/O — deny-listed for any
# top-level (import-time-reachable) call in a `multisource_*` module.
_SUSPICIOUS_CALL_TOKENS = ("connect", "execute", "cursor", "commit", "rollback", "socket",
                          "urlopen", "requests")


def _module_rel_path(module_name: str) -> str:
    tail = module_name.rsplit(".", 1)[-1]
    return f"src/featuregen/overlay/upload/planner/{tail}.py"


def _iter_import_time_calls(node: ast.AST):
    """Yield every ``ast.Call`` reachable from ``node`` WITHOUT recursing into function bodies —
    a function's calls only execute when the function is later CALLED, not at import time. Class
    bodies (dataclass/StrEnum member statements + decorator calls) DO execute at import time, so
    they are walked; methods nested inside a class are skipped for the same reason as top-level
    functions."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Call):
            yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.append(child)


def _assert_no_module_level_io(rel_path: str) -> None:
    source = (_REPO_ROOT / rel_path).read_text()
    tree = ast.parse(source, filename=rel_path)
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue   # imports + top-level function DEFINITIONS don't execute their body now
        for call in _iter_import_time_calls(stmt):
            func = call.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
            assert not any(tok in name.lower() for tok in _SUSPICIOUS_CALL_TOKENS), (
                f"{rel_path}:{getattr(call, 'lineno', '?')}: suspicious import-time-reachable "
                f"call {name!r} — looks like module-level DB/IO, forbidden by design §12")


def test_every_multisource_module_imports_cleanly_with_no_module_level_dbio():
    for module_name in _MULTISOURCE_MODULES:
        importlib.import_module(module_name)   # raises (failing the test) if it doesn't import cleanly
        _assert_no_module_level_io(_module_rel_path(module_name))


def test_multisource_assembly_shadow_flag_is_default_off():
    assert MULTISOURCE_ASSEMBLY_SHADOW_FLAG == "FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW"


def test_flag_off_cli_entrypoint_is_a_noop_opens_no_connection():
    """The Task-11 pattern: with the flag unset (``env={}`` -> no key present), ``run_shadow_cli``
    must be a pure no-op — it opens NO connection (a fake ``connect`` that raises if called proves
    this) and returns ``None``. This is the concrete proof that there is no possible shadow-store
    write on a normal (flag-off) path."""
    from featuregen.overlay.upload.planner.multisource_shadow import run_shadow_cli

    def _connect():
        raise AssertionError("connect() must not be called when the multisource shadow flag is off")

    out = run_shadow_cli(
        intents_provider=lambda _c: {}, run_id="mrun_neutrality_off", roles=("feature_engineer",),
        now=_NOW, connect=_connect, env={})
    assert out is None
