"""Read-scope authorization for catalog reads.

The catalog is a searchable map of where sensitive data lives, so sensitive nodes must not be
world-readable (a concern distinct from the retired fact-approval governance). A node is visible only
to a caller whose roles grant EVERY visibility class the node requires; a node requiring nothing is
always visible. This is a HARD filter applied before ranking — never a rank weight.

**Two independent axes, resolved in one place.** A node's requirements come from BOTH the raw
file-declared ``graph_node.sensitivity`` AND the governed, concept-derived
``graph_node.effective_restriction`` floor. Migration 1032 folds them into the generated column
``visible_requires`` (a ``text[]`` of the classes a reader must hold), so every call site is one
clause::

    AND visible_requires <@ %s          -- %s = allowed_classes(roles)

Before 1032 the predicate read ``sensitivity`` alone, under "untagged is always visible". Because a
business glossary attests no sensitivity, a glossary-ingested catalog had ``sensitivity = NULL``
everywhere while the concept cascade had independently ruled columns restricted or confidential — so
the governed floor never restricted visibility. Measured on the deployed FTR catalog: 28 of 126
columns, including customer names, addresses, phone numbers and a national ID number, were readable
by any caller holding ``catalog:read``.

**``prohibited`` is deliberately absent from the role map.** It can therefore never appear in an
allowed list and no role unlocks it. It is also the rank an unrecognized floor label collapses to
(``safety_floor``), so an unknown value fails closed rather than open.
"""
from __future__ import annotations

from collections.abc import Iterable

#: Which role grants visibility of each raw TAG. **This is also the tag vocabulary migration 0993
#: enforces on ``graph_node.sensitivity`` via a CHECK constraint** — the two are kept identical on
#: purpose, so "a tag with no role" is an impossible row rather than a silently dropped access
#: requirement. Do not add a floor level here: the tag and floor vocabularies overlap on exactly one
#: word (``restricted``) and mean different things, and conflating them is the failure mode
#: ``test_the_two_vocabularies_are_not_the_same_set`` exists to catch.
SENSITIVITY_ROLES: dict[str, str] = {
    "pii": "pii_reader",
    "restricted": "restricted_reader",
}

#: Which role grants visibility of each governed FLOOR level (``safety_floor.SENSITIVITY_ORDER`` =
#: public < internal < confidential < restricted < prohibited).
#:
#: ``public``/``internal`` impose no requirement and so need no role — "internal" means any
#: authenticated catalog reader, which is the population this filter already scopes. ``prohibited``
#: is deliberately unlisted and therefore ungrantable; it is also the rank an unrecognized label
#: collapses to, so an unknown value fails closed.
RESTRICTION_ROLES: dict[str, str] = {
    "confidential": "confidential_reader",
    "restricted": "restricted_reader",
}

#: The SQL predicate every read-scope call site uses. Bind ONE parameter: ``allowed_classes(roles)``.
#: ``<@`` is array containment, backed by ``graph_node_visible_requires_idx`` (GIN). A node requiring
#: nothing has ``{}``, which is contained by every array including ``{}`` — so the ungoverned,
#: untagged common case stays visible with no special case.
VISIBILITY_PREDICATE = "visible_requires <@ %s"


def visibility_predicate(alias: str = "") -> str:
    """The read-scope predicate, optionally qualified by a table alias (``"fn"`` -> ``fn.``).

    A call site joining two ``graph_node`` rows (a from-node and a to-node) applies it once per side,
    binding the same parameter twice — mirroring how those sites already bind the allowed list twice.
    """
    prefix = f"{alias}." if alias else ""
    return f"{prefix}{VISIBILITY_PREDICATE}"


def allowed_classes(roles: Iterable[str]) -> list[str]:
    """The visibility classes these roles may see — the union over BOTH vocabularies.

    ``visible_requires`` holds either a tag value or a floor value (migration 1032 picks one by
    precedence, it never mixes them), so a single allowed list covers both. Values are deduplicated
    and sorted for a stable query parameter: ``restricted`` is legal in both maps and must not appear
    twice.
    """
    role_set = set(roles)
    granted = {cls for cls, required in SENSITIVITY_ROLES.items() if required in role_set}
    granted |= {cls for cls, required in RESTRICTION_ROLES.items() if required in role_set}
    return sorted(granted)


#: Back-compat alias: the name predates the governed floor, when the raw tag was the only axis.
#: The returned value is now the class list above.
allowed_sensitivities = allowed_classes
