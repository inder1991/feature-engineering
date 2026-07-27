"""Spec A — executable materialization: compile governed formulas into a runnable project.

This package is **render-only**. It never imports ``pyspark``; it emits the
Kedro/PySpark project that does the computing, plus the manifests, gates and
identity records that make the result auditable.

Two package-wide invariants, both established here in Task 1:

- **One hasher.** ``canonical.materialize_hash`` (RFC 8785 JCS + sha256) is the
  ONLY canonicalization scheme in this package. A second one would silently
  fork identity, so no other may ever be introduced.
- **Closed failure vocabularies.** Every governed refusal, blocking gate and
  validation finding names a member of one of the four ``StrEnum``s in
  ``codes`` (spec §14). A governed refusal never surfaces as a bare
  ``TypeError``/``ValueError``, and never as a raw string.
"""
