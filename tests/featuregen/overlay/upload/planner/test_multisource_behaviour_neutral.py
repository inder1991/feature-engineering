"""Phase 3C.2b-i-A · Task 13 — behaviour-neutrality golden test (TEST-ONLY, no source change).

Formalizes design §12: the whole multi-source shadow engine (Tasks 1-12) is purely ADDITIVE — it
never edits, monkeypatches, or otherwise perturbs the single-source ``plan_bindings`` frontier.
Three independent proofs:

  1. STATIC — every BEHAVIOURAL reused engine file (``assembly.py``, ``declarations.py``,
     ``plan.py``, ``enumerate.py``, ``candidates.py``, ``order.py``, ``scope.py``) is
     BEHAVIOURALLY identical to ``origin/main`` at this branch's merge-base. Not byte-identical:
     the comparison is over the module's AST with docstrings stripped, so prose may be corrected
     (see the escalation note below for why that had to be allowed) while any change to a
     statement, expression, constant, default, decorator or import still fails. A file whose
     EXECUTABLE code did change is reported per TOP-LEVEL DEFINITION and must name every changed
     symbol in ``_ALLOWED_BEHAVIOURAL_CHANGES`` — an explicit, reviewable exception per symbol, not
     a blanket exemption for the file. ``contracts.py`` is checked differently again: its branch
     diff may only ever APPEND lines, never remove or change an existing one.

     ``_executable_ast`` is itself under test: ``test_ast_identity_survives_prose_and_catches_code``
     applies six MUTATION CONTROLS — a changed comment and a rewritten docstring must SURVIVE; a
     changed constant, expression, import or function body must FAIL. Without them this proof is
     only as strong as an unverified helper, and the git-diff half is vacuous whenever the branch
     equals ``origin/main`` (every file's diff is empty, so the loop compares nothing).
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

import pytest

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

#: The pseudo-symbol `_definition_asts` files every name-binding-free top-level statement under —
#: imports included. Defined here rather than beside that function so the allow-list below can name
#: it symbolically: a branch that changes only an import must still say so, by name.
_MODULE_BODY = "<module-level statements and imports>"

# Top-level symbols whose EXECUTABLE code a branch may deliberately change in a behavioural engine
# file, each with the reason. Per SYMBOL, never per file: everything else in the same file is still
# held to behavioural identity, so this exception cannot grow silently into a blanket exemption.
#
# THE CARDINALITY CORRECTION, in two owner-directed steps. Step 1 (fail-close) removed the
# `Cardinality.MANY_TO_ONE` a bridge-rollup hop returned "BY CONSTRUCTION" — an assumption about the
# far side's grain presented as evidence, which silently inflates every SUM taken across a bridge
# whose far side is not in fact at that grain. Step 2 (tier 2, these entries) restores the capability
# by DERIVING the fan-in instead: a hop whose far endpoint IS the far table's complete, current,
# VERIFIED, unique grain really is many_to_one, and anything short of that stays unknown.
#
#   `_hop_evidence`                        — the derivation itself, and the fail-closed default.
#   `CARDINALITY_SOURCE_BRIDGE_FAR_GRAIN`  — the new `source` vocabulary entry naming that evidence.
#   `CompilerContext`                      — carries the batch-read `governed_grain_by_table` the
#                                            derivation reads (the context stays conn-free and pure).
#   `build_compiler_context`               — batch-reads it once per run for the tables the active
#                                            bridges name.
#
# Both steps are CORRECTNESS changes, not shadow-engine perturbations: the branch they touch is
# reachable only from a cross-catalog plan, which single-source `plan_bindings` never produces, and
# proof 2 (RUNTIME) measures that directly and still passes.
#
# Step 1's own entries (`CARDINALITY_SOURCE_BRIDGE`, `CARDINALITY_SOURCE_BRIDGE_UNATTESTED`) are GONE
# from this list, not because they stopped mattering but because they are now IN the baseline: the
# correction landed on origin/main, so the merge-base this proof compares against already contains it.
# That is the intended end state — the allow-list shrinks as each correction becomes the baseline
# rather than accumulating forever. Escalate anything that appears here without a matching,
# owner-directed reason.
#
# S1A-2, THE IDENTITY-NEUTRAL REGISTRY BYPASS (cross-catalog Stage 1). `RESOLVED_NEED_METADATA` is
# keyed on `template.id`, and 106 ids in the legacy template corpus COLLIDE with V2 recipe ids — so
# a probe projected from a V2 recipe silently inherits the same-named legacy template's resolved
# needs, overriding its own declared grains/roles (measured: 37 of the 317 V2 recipes). The fix adds
# a keyword-only `metadata_resolution_mode` to the two functions that read that registry:
#
#   `METADATA_RESOLUTION_MODES`      — the new closed pair ("legacy_registry", "request_contract").
#   `plan_bindings`                  — validates the mode, then threads it to discovery.
#   `discover_ingredient_candidates` — "request_contract" skips the id-keyed registry read.
#
# This is NOT a shadow-engine perturbation, and specifically not the single-source perturbation
# design §12 forbids: the parameter DEFAULTS to "legacy_registry", under which `resolved` is the
# identical comprehension over the identical mapping, so every pre-existing caller
# (contract/gate1, contract/governed_plan, planner/shadow) is byte-for-byte unchanged — a property
# proof 2 (RUNTIME) below measures directly and still passes. Only `plan_planning_request`, the
# origin-neutral request seam, asks for the new mode. The discriminator had to be an ARGUMENT
# rather than a `Template` field because `recipe_grounding_context` enumerates Template's fields
# dynamically, so a new field would move every legacy template's canonical hash.
#
# S1A-4a, THE PLAN FACTS A GOVERNED OPTION BUILDER CONSUMES (cross-catalog Stage 1). Two facts a
# plan did not carry, both APPENDED as defaulted fields and neither hashed into any identity:
#
#   `assemble_paths`   — sets `output_grain_ref=(landing catalog, landing grain key)` at the ONE
#                        point that knows it: the completing mint. The output grain is NOT
#                        recoverable downstream — a transaction -> account roll-up binds no
#                        account-key ingredient, and the landing catalog is not the plan's
#                        `catalog_source`. Rejected/dead-end mints keep None.
#   `_grain_key_ref`   — the new helper that resolves it, by the identical rule
#                        `reposition_bridges` already uses (`is_grain` AND `key_entity` ==
#                        position entity), through the planner's own governed helpers.
#   `compile_temporal` — qualifies `anchor_binding` with the `bound_catalog_source` of the SAME
#                        binding the ref was chosen from. The ambiguity decision is still taken
#                        over the distinct REFS, so no declaration outcome moves.
#
# Neither is a shadow-engine perturbation. State the basis precisely, because the obvious argument
# is FALSE: it is NOT true that these fields only appear on cross-catalog plans. A single-catalog
# run whose recipe rolls transaction -> account INSIDE one catalog produces a
# source_to_target_resolved plan with a populated `output_grain_ref`
# (`test_plan.py::test_zero_bridge_rollup_output_grain_is_its_own_catalog` is exactly that, with one
# seeded catalog), and that same plan compiles, so `anchor_catalog_source` is reachable
# single-source too. The real basis is narrower and checkable:
#
#   * NEITHER FIELD IS IDENTITY MATERIAL. `output_grain_ref` enters no hash and no persisted payload
#     anywhere in the tree (audited: `fingerprint.contract_input_hash` / `planner_input_hash` /
#     `declarations_output_hash`, `shadow_capture`'s row + declarations payload, `PlanEnvelopeV1`,
#     `plan_dependency_pins`, gate1's governed trace — every one is an EXPLICIT projection).
#     `make_binding_plan`'s material excludes both, pinned by pre-change literals in
#     `test_plan.py::test_new_plan_facts_move_no_identity`.
#   * `anchor_catalog_source` is the ONE exception and is disclosed, not hidden: it rides inside
#     `dataclasses.asdict(plan.temporal_declaration)`, so it does enter `declarations_output_hash`
#     and the `planner_shadow_plan_observation.declarations` payload — a shadow-lane OUTPUT
#     stability signal, keyed on by nothing, and a one-time shift.
#   * Neither field appears in any value proof 2 (RUNTIME) compares, and proof 2 still passes.
#   * No production caller reads either field yet.
#
# S1B-2, THE TYPED UNMET HOP A REJECTED PLAN CARRIES OUT (cross-catalog Stage 1). At the frontier's
# dead end the assembler already held the failing relationship, the exact `_Position`, and — inside
# the bool-returning taxonomy probe — the realizing catalogs with their endpoint key columns. All of
# it was discarded; only a reason-code string escaped, so the bridge-demand ledger (S1B-1, live) had
# no evidence source. The changed symbols:
#
#   `_hop_realizable_elsewhere` / `_hop_realizers`
#                        — the SAME probe over the SAME cached rows, returning the facts it was
#                          already computing instead of a bool. The RENAME is why two names appear:
#                          the old one is gone from the head file, so the per-symbol differ reports
#                          both halves of one change.
#   `_near_side_key_refs`, `MAX_NEAR_SIDE_COLUMNS_WALKED`, `NEAR_SIDE_WALKED` /
#   `NEAR_SIDE_CAPPED` / `NEAR_SIDE_DEADLINE_SKIPPED` / `NEAR_SIDE_NOT_COLLECTED`
#                        — new: the near-side key columns a bridge out of this dead end would
#                          anchor on, resolved AT the refusal site through the same governed
#                          key-entity reading the transitions use, in ONE batched
#                          `key_entities_for` query (never `key_entity` per column), cached per
#                          (catalog, table, entity), capped, and reporting WHICH of the four ways
#                          it reached its answer so `()` never means three different things.
#   `assemble_paths`     — already listed: mints the dead-end reject with the typed hop attached,
#                          and threads the two caches + the (consulted, never spent) compile budget.
#   `<module-level ...>` — three imports: `RealizerFactV1`/`UnmetHopV1` from contracts,
#                          `key_entities_for` from the same `catalog_realizations` module
#                          `key_entity` already came from, and a TYPE_CHECKING-only `CompileBudget`
#                          (no runtime import graph change).
#   `_assemble_rollups`  — plan.py: passes the run-owned budget the assembler now consults.
#
# The basis, stated so it is checkable rather than asserted:
#
#   * VERDICTS ARE UNMOVED. The routing is the identical three-way decision on the identical inputs
#     (`budget_blocked` -> bounded_out_max_bridges; a realizer exists -> unsanctioned_bridge; else
#     missing_realization) — `realizers` non-empty is exactly the old probe's True. Every
#     pre-existing reject-verdict test still passes untouched.
#   * `unmet_hop` IS NOT IDENTITY MATERIAL. It is a defaulted, appended `BindingPlanV1` field that
#     enters neither `_physical_plan_material` nor `make_contract_id`, pinned by the S1A-4a literals
#     in `test_plan.py::test_the_unmet_hop_moves_no_identity` and, non-vacuously, by
#     `test_the_same_reject_keeps_its_id_whether_or_not_the_hop_is_carried`.
#   * THE BUDGET GATES EVIDENCE, NEVER A PLAN. It is consulted (clock vs deadline) and never spent:
#     `remaining`/`stopped_by_time` are untouched, so a walk skipped past the deadline cannot
#     masquerade as the compile pass's truncation. Which plans exist does not depend on it — and
#     neither does WHY they were refused. `budget is None` (which is exactly the two multi-source
#     callers, both of which read only `assembly.complete`) turns evidence collection OFF: the
#     realizer probe reverts to its pre-S1B-2 first-hit early exit, and the near-side walk does not
#     run at all. "A realizer exists" is what the verdict has always asked, and both modes answer
#     it identically — pinned end-to-end by
#     `test_plan.py::test_a_caller_with_no_budget_gets_the_same_verdicts_with_leaner_evidence`
#     (same plans, same ids, same primary reason codes; only the hop's evidence differs).
#   * NO COST DELTA ON THE BUDGET-BLOCKED BRANCH: the realizer probe stays skipped there, exactly
#     as the pre-S1B-2 code short-circuited past it — the verdict is bounded_out_max_bridges
#     regardless of what the probe would find (the demand is the capacity, not a specific
#     crossing), so its typed hop honestly carries no realizers. Nothing here adds a per-column
#     read either: the near-side walk is ONE batched `key_entities_for` query per (catalog, table,
#     entity), pinned by `test_the_near_side_walk_is_one_batched_read_not_one_per_column`.
#   * Proof 2 (RUNTIME) compares no value any of this touches, and still passes.
_ALLOWED_BEHAVIOURAL_CHANGES: dict[str, frozenset[str]] = {
    "src/featuregen/overlay/upload/planner/assembly.py": frozenset({
        "_grain_key_ref", "assemble_paths",
        "_hop_realizable_elsewhere", "_hop_realizers", "_near_side_key_refs",
        "MAX_NEAR_SIDE_COLUMNS_WALKED", "NEAR_SIDE_WALKED", "NEAR_SIDE_CAPPED",
        "NEAR_SIDE_DEADLINE_SKIPPED", "NEAR_SIDE_NOT_COLLECTED",
    }),
    "src/featuregen/overlay/upload/planner/declarations.py": frozenset({
        "compile_temporal",
    }),
    "src/featuregen/overlay/upload/planner/plan.py": frozenset({
        "_assemble_rollups",
    }),
}


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) \
                and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.fix_missing_locations(tree)


def _executable_ast(source: str) -> str:
    """The module's AST with every DOCSTRING removed. Comments never reach the AST at all, so two
    modules whose ``_executable_ast`` are equal differ only in prose — no statement, expression,
    constant, default, decorator or import can differ without changing this string. The mutation
    controls in ``test_ast_identity_survives_prose_and_catches_code`` prove both halves of that."""
    return ast.dump(_strip_docstrings(ast.parse(source)))


def _definition_asts(source: str) -> dict[str, str]:
    """``{top-level symbol -> its docstring-stripped AST dump}``, so a changed file can be reported
    by the SYMBOL that changed rather than as one opaque blob. Statements that bind no name — imports
    included, deliberately, so a changed import cannot slip through unnamed — are compared together
    under ``_MODULE_BODY``."""
    tree = _strip_docstrings(ast.parse(source))
    out: dict[str, str] = {}
    loose: list[str] = []
    for stmt in tree.body:                       # type: ignore[attr-defined]
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out[stmt.name] = ast.dump(stmt)
            continue
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if names and len(names) == len(targets):
            for name in names:
                out[name] = ast.dump(stmt)
        else:
            loose.append(ast.dump(stmt))
    out[_MODULE_BODY] = "\n".join(loose)
    return out


def _changed_definitions(base: str, head: str) -> set[str]:
    base_defs, head_defs = _definition_asts(base), _definition_asts(head)
    return {name for name in base_defs.keys() | head_defs.keys()
            if base_defs.get(name) != head_defs.get(name)}


def test_behavioural_engine_files_are_behaviourally_identical_to_origin_main_at_branch_point():
    """Design §12: the multi-source shadow engine never perturbs the single-source frontier.

    Byte-identity where the bytes are identical; AST-identity (docstrings stripped, comments
    invisible to the parser) where they are not. That distinction was forced by the bridge lifecycle
    correction, which had to fix a comment in ``assembly.py`` asserting a dead invariant — "Crossings
    are governed-bridge-only (active_bridges = VERIFIED)", false since ``active_bridges`` began
    consuming confirmed and proposed alike, and load-bearing enough that it invalidated a plan
    premise built on it. A proof that a false comment may never be corrected makes it permanent.

    A file whose EXECUTABLE code did change is then diffed per TOP-LEVEL SYMBOL and every changed
    symbol must be named in ``_ALLOWED_BEHAVIOURAL_CHANGES`` with its reason. That is strictly
    stronger than the whole-file check it replaces for every symbol NOT listed — an unexpected change
    is now reported by name — and it keeps a deliberate, owner-directed correctness fix from either
    being blocked by a proof about a different concern or quietly disabling that proof for the whole
    file.

    This is NOT the weakening the module docstring warns against. Any unnamed change to a statement,
    expression, constant, default, decorator or import still fails — escalate that, do not add it to
    the allow-list without a reason that survives review.
    """
    for rel_path in _BEHAVIOURAL_ENGINE_FILES:
        diff = _diff_for(rel_path)
        if not diff:
            continue
        base = _git("show", f"{_MERGE_BASE}:{rel_path}")
        head = (_REPO_ROOT / rel_path).read_text()
        if _executable_ast(base) == _executable_ast(head):
            continue                                        # prose only — behaviourally identical
        allowed = _ALLOWED_BEHAVIOURAL_CHANGES.get(rel_path, frozenset())
        unexpected = sorted(_changed_definitions(base, head) - allowed)
        assert not unexpected, (
            f"NEUTRALITY VIOLATION: {rel_path} changed EXECUTABLE code on this branch relative to "
            f"the origin/main branch point {_MERGE_BASE} in symbol(s) {unexpected} — this file "
            "carries single-source planner behaviour and design §12 requires it stay behaviourally "
            f"identical. Diff:\n{diff}")


# ── 1b. the self-check: is `_executable_ast` actually strong enough to carry proof 1? ──
#
# The git-diff half of proof 1 is VACUOUS whenever this branch equals origin/main — every file's diff
# is empty, so the loop above compares nothing at all. Whatever confidence proof 1 carries therefore
# has to come from `_executable_ast` itself, and an unexercised helper carries none. These controls
# live in the repo rather than in a one-off verification run so the guarantee re-arms on every run.
_MUTATION_SUBJECT = '''\
"""Subject module docstring."""
from __future__ import annotations

import os

THRESHOLD = 10


def widget(x):
    """Subject function docstring."""
    # a comment about the arithmetic
    return x + THRESHOLD
'''

#: Prose-only mutations. Each MUST leave `_executable_ast` unchanged — this is exactly the freedom
#: the bridge lifecycle correction needed in order to delete a false comment from `assembly.py`.
_PROSE_MUTATIONS = {
    "comment rewritten": ("# a comment about the arithmetic", "# an entirely different remark"),
    "module docstring rewritten": ('"""Subject module docstring."""', '"""Rewritten."""'),
    "function docstring rewritten": ('"""Subject function docstring."""', '"""Rewritten too."""'),
}

#: Executable mutations. Each MUST change `_executable_ast`, or proof 1 is decorative.
_CODE_MUTATIONS = {
    "constant changed": ("THRESHOLD = 10", "THRESHOLD = 11"),
    "expression changed": ("return x + THRESHOLD", "return x - THRESHOLD"),
    "import changed": ("import os", "import sys"),
    "function body changed": ("    return x + THRESHOLD", "    raise ValueError('no')"),
}


@pytest.mark.parametrize("label", sorted(_PROSE_MUTATIONS))
def test_ast_identity_survives_prose(label):
    """MUST-SURVIVE controls. A helper that failed these would make a false comment permanent."""
    old, new = _PROSE_MUTATIONS[label]
    mutated = _MUTATION_SUBJECT.replace(old, new)
    assert mutated != _MUTATION_SUBJECT, f"{label}: the mutation did not apply — vacuous control"
    assert _executable_ast(mutated) == _executable_ast(_MUTATION_SUBJECT), label


@pytest.mark.parametrize("label", sorted(_CODE_MUTATIONS))
def test_ast_identity_catches_code(label):
    """MUST-FAIL controls, one per kind of change proof 1 claims to catch."""
    old, new = _CODE_MUTATIONS[label]
    mutated = _MUTATION_SUBJECT.replace(old, new)
    assert mutated != _MUTATION_SUBJECT, f"{label}: the mutation did not apply — vacuous control"
    assert _executable_ast(mutated) != _executable_ast(_MUTATION_SUBJECT), label
    # and the per-symbol report names the right symbol rather than shrugging at the whole file
    assert _changed_definitions(_MUTATION_SUBJECT, mutated), label


def test_ast_identity_controls_hold_on_a_real_engine_file():
    """The synthetic subject above is small enough to be unrepresentative, so run both directions
    against a real behavioural engine file: appending a comment survives, appending one statement
    does not."""
    source = (_REPO_ROOT / "src/featuregen/overlay/upload/planner/assembly.py").read_text()
    assert _executable_ast(source + "\n# a trailing remark\n") == _executable_ast(source)
    assert _executable_ast(source + "\n_NEUTRALITY_PROBE = 1\n") != _executable_ast(source)
    assert _changed_definitions(source, source + "\n_NEUTRALITY_PROBE = 1\n") == {
        "_NEUTRALITY_PROBE"}


def test_contracts_file_branch_diff_is_additive_only():
    diff = _diff_for(_CONTRACTS_FILE)
    if not diff:
        pytest.skip("branch is merged to origin/main — this pre-merge additive-only branch-diff proof "
                    "has no delta here (it re-arms automatically on any future unmerged branch)")
    # The four-objective integration branch also centralizes these three released input versions
    # in taxonomy.versions. That exact source move is behavior-bearing and covered by
    # test_version_consistency; no other existing planner contract line may change here.
    allowed_version_source_move = {
        '-RECIPE_REGISTRY_VERSION = "1.0.0"',
        '-APPLICABILITY_MAPPING_VERSION = "1.0.0"',
        '-CONCEPT_REGISTRY_VERSION = "concepts@1"',
    }
    # S1A-4a, THE SHARED PHYSICAL-MATERIAL EXTRACTION (cross-catalog Stage 1). A governed option
    # builder needs the UNTRUNCATED digest of a plan's physical identity, so contracts.py gained
    # `full_physical_plan_hash`. The one thing that must never happen is giving it its OWN copy of
    # the id material — two constructions of one identity drift apart — so the material moved into
    # `_physical_plan_material`, read by BOTH it and `make_binding_plan`. These EXACT eight lines
    # are everything that move removes, together with `make_binding_plan` gaining a defaulted,
    # never-hashed `output_grain_ref` argument. The mint's OUTPUT is byte-for-byte unchanged, pinned
    # by literals captured on the pre-change checkout
    # (`test_plan.py::test_new_plan_facts_move_no_identity`). Listed line-for-line rather than by
    # symbol so this exception cannot silently cover any other edit to the same function.
    allowed_shared_material_extraction = set('''\
-                      candidate_role: CandidateRole) -> BindingPlanV1:
-    refs = tuple(sorted(b.bound_object_ref for b in ingredient_bindings))
-    segments_material = ">".join(
-        f"{s.segment_kind}:{s.catalog_source}:{_segment_physical_identity(s)}"
-        for s in path_segments)
-    material = (f"{recipe_id}|{catalog_source}|{'|'.join(refs)}|{tier}|{segments_material}"
-                f"|{path_resolution_status}|{PLANNER_VERSION}|{PHYSICAL_PLAN_VERSION}")
-        path_resolution_status=path_resolution_status, candidate_role=candidate_role)
'''.splitlines())
    removed = [
        line for line in _removed_lines(diff)
        if line not in allowed_version_source_move | allowed_shared_material_extraction
    ]
    assert not removed, (
        f"NEUTRALITY VIOLATION: {_CONTRACTS_FILE} removed or changed an existing line that no "
        "exception covers. This branch may only APPEND to it (design §12), plus exactly two "
        "reviewed exceptions, each an EXACT-LINE allow-set defined in this test: "
        "`allowed_version_source_move` (three released input versions centralized into "
        "taxonomy.versions) and `allowed_shared_material_extraction` (S1A-4a: the physical-id "
        "material moved into `_physical_plan_material` so `make_binding_plan` and "
        "`full_physical_plan_hash` cannot drift apart). Anything else is an escalation — add a "
        "line to an allow-set only with an owner-directed reason, never to make this pass. "
        f"Removed/changed lines:\n" + "\n".join(removed))
    # Sanity when this test is run on the original A branch: the branch appended the capability
    # constant. On a later integration branch the merge base may already contain A, in which case
    # requiring the same line to be re-added is impossible and would make the guard branch-shape
    # dependent.
    added = [ln for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    base_contracts = _git("show", f"{_MERGE_BASE}:{_CONTRACTS_FILE}")
    if "MULTISOURCE_ASSEMBLY_SHADOW_FLAG" not in base_contracts:
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
