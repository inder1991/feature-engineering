"""Spec §2 — the COMPILER: governed authoring output becomes an executable IR, contract and plan.

**Why this is a package whose ``__init__`` imports nothing.** A.13 recorded that §7 puts the
compiler's version inside ``sandbox_execution_hash`` and that nothing owned one, so it was a
required parameter a call site could freeze at whatever literal it typed. A.15 handed the fix to
Task 15, with ``render/__init__.py`` as the shape to copy: the constant lives in a module that
imports nothing, :mod:`featuregen.materialize.identity` reads it *through* that module, and the
parameter is removed so there is nowhere left to pass a string.

The import-free rule is load-bearing rather than tidy. §2's chain (admit → ``compile_ir`` →
``authorize_compilation`` → contract → plan) has to build a :class:`CompilationIdentity`, so the
module that runs it must import ``identity`` — while ``identity`` imports *this* one. Declaring the
constant in the same file as the chain would close that loop, and the failure would surface as an
``ImportError`` in whichever module happened to be imported first. So the chain lands in a SIBLING
module inside this package (``compile.chain``), which may import ``identity`` freely, and this file
stays free of imports. A test pins exactly that, as it does for ``render``.

Nothing else lives here yet: §2's orchestrator is not built, and inventing a shell for it would be
a second place the chain is described.
"""
from __future__ import annotations

__all__ = ["COMPILER_VERSION"]

#: The version of the COMPILER — of the governed decisions §2's chain makes, not of the spec it
#: makes them under. It enters ``sandbox_execution_hash`` (§7), which is the value that
#: distinguishes two executions of *the same authored formulas* compiled by two different compilers.
#:
#: **Bump it whenever a change to the compilation chain would decide something differently for
#: unchanged governed inputs** — a new refusal, a changed IR field, a different contract derivation,
#: a moved physical-type resolution. Not for a change that cannot reach a compiled artifact (a
#: docstring, a test, a refactor that decides the same things): a version that moves without the
#: decisions moving makes every past execution hash look stale.
#:
#: A string, and deliberately NOT derived from a git commit, for the two reasons
#: ``RENDERER_VERSION`` gives: the recorded value is an identity rather than a scale, and the same
#: source compiled from two checkouts is the same compiler.
#:
#: It is stated here rather than assembled from the four policy versions the package already owns
#: (``PHYSICAL_TYPE_POLICY_VERSION``, ``CLASSIFICATION_POLICY_VERSION``, ``RETENTION_POLICY_VERSION``,
#: ``CANONICALIZATION_VERSION``): those version individual policies, and a compiler can change what
#: it decides without any of them moving.
#:
#: **"2"** (codegen-review remediation branch): the chain decides differently for unchanged
#: governed inputs — new refusals (``COLUMN_NOT_GOVERNED`` for a read the catalog does not govern,
#: ``PARTITION_MAPPING_NOT_DECLARED`` for an unresolvable partition mapping, ``half_even`` ratios
#: and non-UTC ``DATE`` clocks now refuse), the ``grain_keys`` field folded into the IR, and a
#: changed contract derivation. Formulas that compiled under "1" can refuse under "2", so an
#: execution hashed under "1" describes decisions this compiler no longer makes.
COMPILER_VERSION = "2"
