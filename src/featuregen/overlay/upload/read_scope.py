"""Read-scope authorization for catalog search.

The catalog is a searchable map of where sensitive data lives, so sensitivity-tagged nodes must not
be world-readable (a concern distinct from the retired fact-approval governance). A node with a
sensitivity is visible only to a caller whose roles grant that sensitivity; untagged nodes are
always visible. This is a HARD filter applied before ranking — never a rank weight.
"""
from __future__ import annotations

from collections.abc import Iterable

# Which role grants visibility of each sensitivity class.
SENSITIVITY_ROLES: dict[str, str] = {
    "pii": "pii_reader",
    "restricted": "restricted_reader",
}


def allowed_sensitivities(roles: Iterable[str]) -> list[str]:
    """The sensitivity classes these roles may see (untagged nodes are always visible, handled in SQL)."""
    role_set = set(roles)
    return [s for s, required in SENSITIVITY_ROLES.items() if required in role_set]
