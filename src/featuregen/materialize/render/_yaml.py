"""The ONE decision about how a value becomes a YAML scalar in a rendered catalog.

Extracted from ``render.project`` when ``render.publish`` began emitting a catalog entry of its own
(§10.3). The alternative — a second copy of the same four-line function — is a second chance to
quote a storage location differently, and a catalog whose entries disagree about escaping is one
whose datasets resolve to different paths for reasons nobody wrote down.

Import-free by construction, so ``project`` and ``publish`` can both depend on it without either
depending on the other.
"""
from __future__ import annotations

__all__ = ["yaml_scalar"]


def yaml_scalar(value: str) -> str:
    """One YAML scalar. Always double-quoted, so a value that looks like a number, a date or a
    boolean stays the string the catalog meant."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
