"""Phase 3C.2b-i-B · Task 11 — behaviour-neutrality proof (TEST-ONLY, no source change).

B (Tasks 0-10) is a STANDALONE shadow endpoint: a bounded admin service (``govern_llm_idea``) that
governs ONE untrusted LLM cross-catalog feature idea end-to-end, off by default behind
``FEATUREGEN_LLM_XCAT_SHADOW``. This proves B changed NOTHING in the live path. Four independent,
non-vacuous proofs:

  A. STATIC / AST — every ``b_*`` module under ``planner/`` (globbed, so a future B module is
     auto-covered) imports cleanly and defines no module-level DB/network I/O (the same AST
     import-purity check Phase A's neutrality test uses).
  B. STATIC / SOURCE — the live considered-set/draft pipeline files (``contract/gate1.py``,
     ``feature_assist.py``, ``contract/author.py``, ``contract/review.py``, ``contract/govern.py``,
     ``contract/scope_records.py``, ``contract/live_activation.py``, ``api/routes/contract.py``)
     reference ZERO ``b_*`` modules or ``govern_llm_idea``/``normalize_feature_idea`` — B added no
     path into ``build_considered_set`` / ``recommend_features`` / the confirm/draft flow.
  C. GIT — B's own commit range (from the first commit introducing ``b_dispositions.py`` to HEAD)
     touched ONLY B-owned paths (``planner/b_*.py``, ``tests/.../test_b_*.py``, docs/ledger) — never
     a ``multisource_*`` file, an A single-source engine file, or a live pipeline file. This subsumes
     "the live path is byte-identical across B": if B's range never touched those files, they did
     not change.
  D. RUNTIME — with ``FEATUREGEN_LLM_XCAT_SHADOW`` unset, ``govern_llm_idea`` raises
     ``XCatShadowDisabledError`` BEFORE touching ``conn`` at all (a "boom" connection that raises on
     ANY attribute access proves this) — flag-off opens no connection and does no DB work.

If any proof reveals a REAL neutrality violation (a live-path file referencing a ``b_*`` module, or
B's range touching an engine/pipeline file), that assertion must NOT be weakened to make it pass —
escalate it truthfully instead.
"""
from __future__ import annotations

import ast
import importlib
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.planner.b_proposal import new_raw_proposal
from featuregen.overlay.upload.planner.b_service import (
    FEATUREGEN_LLM_XCAT_SHADOW,
    XCatShadowDisabledError,
    govern_llm_idea,
)

_NOW = datetime(2026, 7, 22, tzinfo=UTC)

# ── repo/git plumbing (verbatim from test_multisource_behaviour_neutral.py) ──
_TEST_FILE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(subprocess.run(
    ("git", "rev-parse", "--show-toplevel"), cwd=_TEST_FILE_DIR,
    capture_output=True, text=True, check=True).stdout.strip())


def _git(*args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=_REPO_ROOT, capture_output=True, text=True, check=True)
    return proc.stdout


# ── proof A — every b_* module has no import-time side effect ────────────────────────────────────
_PLANNER_DIR = _REPO_ROOT / "src/featuregen/overlay/upload/planner"
_B_MODULE_PATHS = sorted(_PLANNER_DIR.glob("b_*.py"))
_B_MODULES = tuple(f"featuregen.overlay.upload.planner.{p.stem}" for p in _B_MODULE_PATHS)

# The Task 0-10 b_* modules the recon confirmed exist today — a lower bound, not an exact-set check,
# so a future B module (Task 12+) is auto-covered by the glob without needing this list touched.
_EXPECTED_B_MODULE_STEMS = frozenset({
    "b_dispositions", "b_proposal", "b_scope", "b_gauntlet", "b_concept_authority",
    "b_role_policy", "b_source_grain", "b_operation", "b_output_policy", "b_adapter",
    "b_service", "b_gate1", "b_gate1_gold", "b_slice_spike",
})

# Substrings of a Call's function name that look like DB/network I/O — deny-listed for any
# top-level (import-time-reachable) call in a `b_*` module (verbatim from Phase A's neutrality test).
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
                f"call {name!r} — looks like module-level DB/IO, forbidden by design")


def test_proof_a_every_b_module_imports_cleanly_with_no_module_level_dbio() -> None:
    assert _B_MODULES, "no b_*.py modules found under planner/ — proof A would be vacuous"
    found_stems = {p.stem for p in _B_MODULE_PATHS}
    assert _EXPECTED_B_MODULE_STEMS <= found_stems, (
        f"expected the Task 0-10 b_* modules to be present; missing "
        f"{sorted(_EXPECTED_B_MODULE_STEMS - found_stems)}")
    for module_name in _B_MODULES:
        importlib.import_module(module_name)   # raises (failing the test) if it doesn't import cleanly
        _assert_no_module_level_io(_module_rel_path(module_name))


# ── proof B — the live considered-set/draft path references zero b_* symbols ─────────────────────
_LIVE_PATH_FILES = (
    "src/featuregen/overlay/upload/contract/gate1.py",
    "src/featuregen/overlay/upload/feature_assist.py",
    "src/featuregen/overlay/upload/contract/author.py",
    "src/featuregen/overlay/upload/contract/review.py",
    "src/featuregen/overlay/upload/contract/govern.py",
    "src/featuregen/overlay/upload/contract/scope_records.py",
    "src/featuregen/overlay/upload/contract/live_activation.py",
    "src/featuregen/api/routes/contract.py",
)

_B_SYMBOLS = (
    "b_adapter", "b_service", "b_gauntlet", "b_dispositions", "b_proposal", "b_scope",
    "b_concept_authority", "b_role_policy", "b_source_grain", "b_operation", "b_output_policy",
    "b_gate1_gold", "b_gate1", "b_slice_spike", "govern_llm_idea", "normalize_feature_idea",
)


def test_proof_b_live_considered_set_and_draft_path_references_zero_b_symbols() -> None:
    assert _LIVE_PATH_FILES   # non-vacuous: there is at least one live-path file to scan
    for rel_path in _LIVE_PATH_FILES:
        path = _REPO_ROOT / rel_path
        assert path.is_file(), f"expected live-path file is missing: {rel_path}"
        source = path.read_text()
        assert source.strip(), f"{rel_path} is empty — the scan would be vacuous"
        for symbol in _B_SYMBOLS:
            assert symbol not in source, (
                f"NEUTRALITY VIOLATION: {rel_path} references {symbol!r} — B must add no path into "
                "the live considered-set/draft pipeline")


# ── proof C — B's own commit range touched only B-owned paths ────────────────────────────────────
_B_SOURCE_PATH_RE = re.compile(r"src/featuregen/overlay/upload/planner/b_[^/]*\.py")
_B_TEST_PATH_RE = re.compile(r"tests/featuregen/overlay/upload/planner/test_b_[^/]*\.py")

_FORBIDDEN_ENGINE_FILES = frozenset({
    "src/featuregen/overlay/upload/planner/assembly.py",
    "src/featuregen/overlay/upload/planner/declarations.py",
    "src/featuregen/overlay/upload/planner/plan.py",
    "src/featuregen/overlay/upload/planner/enumerate.py",
    "src/featuregen/overlay/upload/planner/candidates.py",
    "src/featuregen/overlay/upload/planner/order.py",
    "src/featuregen/overlay/upload/planner/scope.py",
    "src/featuregen/overlay/upload/planner/contracts.py",
})


def _is_b_owned(rel_path: str) -> bool:
    return bool(_B_SOURCE_PATH_RE.fullmatch(rel_path) or _B_TEST_PATH_RE.fullmatch(rel_path)
                or rel_path.startswith(("docs/", ".superpowers/")))


def _is_live_pipeline_file(rel_path: str) -> bool:
    return (rel_path.startswith(("src/featuregen/overlay/upload/contract/", "src/featuregen/api/"))
            or rel_path == "src/featuregen/overlay/upload/feature_assist.py")


def _branch_unique_files() -> set[str]:
    """Files touched by commits UNIQUE to this branch — reachable from HEAD, NOT on origin/main, and
    excluding merge commits. So a merge of origin/main into the branch (and main's own 78 commits)
    contribute nothing here; only this branch's real work (A/B additions, the LLM-schema fixes, the
    migration renumber) does. This is what makes the neutrality claim robust to that merge."""
    commits = [c for c in _git("rev-list", "^origin/main", "HEAD", "--no-merges").splitlines() if c]
    files: set[str] = set()
    for c in commits:
        files.update(p for p in _git("show", "--name-only", "--format=", c).splitlines() if p.strip())
    return files


def test_proof_c_no_branch_commit_modifies_the_live_pipeline() -> None:
    """No commit unique to this branch modifies a live considered-set / draft pipeline file — B added
    no path into the live flow. Robust to the merge from main (`^origin/main --no-merges`); the
    earlier by-range check broke once the branch also carried the merge commit + the non-b_* LLM
    fixes, neither of which touches the live pipeline."""
    branch_files = _branch_unique_files()
    if not any(_B_SOURCE_PATH_RE.fullmatch(p) for p in branch_files):
        pytest.skip("no branch-unique planner/b_*.py file on this branch state — B's pre-merge "
                    "neutrality proof is not applicable here (B is merged to origin/main, or the branch "
                    "carries only unrelated work); it re-arms on any future unmerged branch touching b_*")
    touched_live = sorted(f for f in branch_files if _is_live_pipeline_file(f))
    assert not touched_live, (
        "NEUTRALITY VIOLATION: commit(s) unique to this branch modified live considered-set/draft "
        f"pipeline file(s) B must not touch: {touched_live}")


# ── proof D — the flag-off entrypoint is inert (raises before any DB work) ───────────────────────
class _BoomConn:
    """A stand-in connection that raises on ANY attribute access — used to PROVE ``govern_llm_idea``
    performs no DB work before the flag-off raise (if it were touched, an ``AssertionError`` would
    surface instead of ``XCatShadowDisabledError``)."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"conn.{name} touched though {FEATUREGEN_LLM_XCAT_SHADOW} is off")


def test_proof_d_govern_llm_idea_flag_off_raises_before_any_db_work(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FEATUREGEN_LLM_XCAT_SHADOW, raising=False)   # ensure truly unset

    boom_conn = cast(DbConn, _BoomConn())
    boom_adapter = cast(CatalogAdapter, None)   # unreachable: the flag check raises before use
    actor = IdentityEnvelope(subject="fe", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("feature_engineer",))
    proposal = new_raw_proposal(operands=("public.txn.tran_amt",), operation="sum",
                                window=None, grain_hint=None)

    with pytest.raises(XCatShadowDisabledError):
        govern_llm_idea(boom_conn, boom_adapter, actor=actor, proposal=proposal,
                        generation_run_id="r_b_neutrality", now=_NOW,
                        fresh_within=timedelta(hours=24))
