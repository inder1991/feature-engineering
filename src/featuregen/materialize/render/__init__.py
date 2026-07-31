"""Spec §7 — the RENDERER: governed compilation output becomes a complete, runnable Kedro project.

**This package emits text.** It never imports ``pyspark`` and never imports ``kedro``; PySpark and
Kedro appear only *inside* the strings it produces. That is the package-wide render-only rule, and
it is what lets the compiler run anywhere while the artifact it emits runs on the cluster.

**Why ``RENDERER_VERSION`` lives in this ``__init__`` rather than in ``project.py``.**
:func:`~featuregen.materialize.identity.sandbox_execution_hash` must cover the renderer's version
(§7), and ``identity`` is *below* the renderer in the import graph — ``render.project`` imports
``identity`` to seal a project, so ``identity`` cannot import ``render.project`` back. It can import
THIS module, because this module imports nothing at all. So the constant sits at the package root,
``identity`` reads it *through the module* (the same shape ``derive_namespace`` uses for
``binding.SANDBOX_NAMESPACE``), and the parameter that used to accept it is gone: nothing can pass a
string, because there is nowhere left to pass one.

That arrangement has one live edge, and a test pins it: **this file must stay import-free.** An
``from .project import …`` added here would make ``import featuregen.materialize.identity`` execute
``render.project``, which imports ``identity`` — a cycle, and one that would only surface as an
``ImportError`` in whichever module happened to be imported first.
"""
from __future__ import annotations

__all__ = ["RENDERER_VERSION"]

#: The version of the RENDERER — of the bytes ``render.project`` emits, not of the spec it emits
#: them from. It enters ``sandbox_execution_hash`` (§7), which is the value that distinguishes two
#: executions of *the same compilation* built by two different renderers.
#:
#: **Bump it whenever a change to this package would produce different rendered bytes for an
#: unchanged compilation** — a new file, a changed catalog entry, a different node wiring, a
#: reworded README. Not for a change that cannot reach the output (a docstring in this package, a
#: test, a refactor with identical bytes): a version that moves without the artifact moving makes
#: every past execution hash look stale.
#:
#: A string rather than an int because ``sandbox_execution_hash`` records it verbatim and because
#: the renderer's versions are not a policy scale — there is no ordering question to answer, only an
#: identity one. It is deliberately NOT derived from a git commit: the same source rendered from two
#: checkouts is the same renderer, and a commit-derived version would fork identity on a rebase.
RENDERER_VERSION = "2"
