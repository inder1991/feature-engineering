"""The registered shared domain/entity resolver seam (Task 0S; freeze 0F-6 / deviation D9).

The controlled business-domain registry and the entity controlled-ID upgrade are owned by
the 2026-08-01 semantic plan, which has NOT landed: no controlled resolver exists at this
baseline. This seam freezes what every consumer does about that:

- an axis with **no registered resolver resolves nothing** — callers fall back to
  :class:`~featuregen.contracts.evidence_axes.AttributedTextV1` (displayable, text-
  searchable) and emit **no controlled facet**;
- an ID is **never invented** — not by lowercasing, not by slugging, not by a resolver
  returning a bare string (that fails loudly);
- when the semantic plan lands its registry, it registers the real resolver here at
  import and every consumer picks it up with zero suggestion-side change.

The two frozen axes are exactly the 0F-6 rows whose controlled owner is the semantic
plan: ``business_domain`` and ``entity``. (``feature_category`` / ``recipe_family`` /
``use_case`` have in-repo controlled registries and never pass through this seam;
Release-A entity FACETS come from the grounding engine's ``Concept.entity_link``
binding, not from free-text resolution.)
"""
from __future__ import annotations

from typing import Protocol

from featuregen.contracts.evidence_axes import (
    AttributedLabelV1,
    AttributedTextV1,
    EvidenceAuthorityV1,
)

__all__ = [
    "RESOLVER_AXES",
    "ControlledIdResolver",
    "ResolverSeamError",
    "register_resolver",
    "registered_resolver",
    "reset_resolver",
    "resolve_controlled",
    "resolve_or_text",
]


class ResolverSeamError(RuntimeError):
    """Unknown axis, double registration, or a resolver emitting a non-label."""


#: The axes whose controlled registry is owned by the semantic plan (0F-6).
RESOLVER_AXES: tuple[str, ...] = ("business_domain", "entity")


class ControlledIdResolver(Protocol):
    """Maps free wording to a controlled label WITH provenance, or ``None`` if it cannot."""

    def resolve(self, text: str) -> AttributedLabelV1 | None: ...


_RESOLVERS: dict[str, ControlledIdResolver] = {}


def _require_axis(axis: str) -> None:
    if axis not in RESOLVER_AXES:
        raise ResolverSeamError(
            f"unknown resolver axis {axis!r}; the frozen axes are {RESOLVER_AXES}")


def register_resolver(axis: str, resolver: ControlledIdResolver) -> None:
    """Register THE resolver for an axis (one per axis; the owner plan registers at import)."""
    _require_axis(axis)
    if axis in _RESOLVERS:
        raise ResolverSeamError(
            f"a resolver for axis {axis!r} is already registered; the seam holds exactly "
            f"one resolver per axis (reset_resolver is the bootstrap/test escape hatch)")
    _RESOLVERS[axis] = resolver


def registered_resolver(axis: str) -> ControlledIdResolver | None:
    """The registered resolver, or ``None`` — the truthful baseline state (D9)."""
    _require_axis(axis)
    return _RESOLVERS.get(axis)


def reset_resolver(axis: str) -> None:
    """Remove an axis's resolver (bootstrap/test seam; absence is a valid state)."""
    _require_axis(axis)
    _RESOLVERS.pop(axis, None)


def resolve_controlled(axis: str, text: str) -> AttributedLabelV1 | None:
    """Resolve wording to a controlled label, or ``None`` when the axis has no resolver or
    the resolver cannot map it. ``None`` means: expose attributed text, no facet.

    A resolver returning anything but ``AttributedLabelV1``/``None`` raises — a bare
    string here would be exactly the invented ID the brief forbids.
    """
    resolver = registered_resolver(axis)
    if resolver is None:
        return None
    label = resolver.resolve(text)
    if label is not None and not isinstance(label, AttributedLabelV1):
        raise ResolverSeamError(
            f"resolver for axis {axis!r} returned {type(label).__name__}; a controlled "
            f"facet must be an AttributedLabelV1 with provenance, never an invented ID")
    return label


def resolve_or_text(
    axis: str,
    text: str,
    *,
    basis: str,
    evidence: tuple[EvidenceAuthorityV1, ...] = (),
    source_refs: tuple[str, ...] = (),
) -> AttributedLabelV1 | AttributedTextV1:
    """The frozen consumer behavior: a controlled label when a registered resolver maps the
    wording; otherwise the wording VERBATIM as attributed text with the caller's
    provenance, ``operational_influence=None`` and no facet."""
    label = resolve_controlled(axis, text)
    if label is not None:
        return label
    return AttributedTextV1(value=text, basis=basis, evidence=tuple(evidence),
                            operational_influence=None, source_refs=tuple(source_refs))
