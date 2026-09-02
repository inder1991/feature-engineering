"""Derived target labels — the rule contract.

A training label is normally CONSTRUCTED ("no transaction for 90 days"), not stored. The platform
had two half-representations and no bridge: intake required a `target_ref` resolving to a real
column, and the model contract held `target_definition` as prose "in reviewed words". Neither is
executable. See docs/superpowers/specs/2026-09-01-derived-target-labels-design.md.

The one inverted property that gives this its own lane: a FEATURE must never read forward of the
as-of date; a LABEL must read exactly forward of it. `direction` + `window_days` are that
declaration, and they are the governed object.

Pure Python: no DB, no LLM. Catalog resolution lives in `target_catalog_check`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

LABEL_TYPES = ("binary", "count", "amount")
OPERATORS = ("==", "!=", ">=", "<=", ">", "<")
#: Only forward. A backward rule is a feature — see the module docstring.
DIRECTIONS = ("forward",)

#: `tgt_` is the owner's decision; the tail matches the existing feature-name rule.
_NAME_RE = re.compile(r"^tgt_[a-z0-9_]{1,123}$")


class TargetContractError(ValueError):
    """An invalid rule — refused at construction, exactly like RecipeDefinitionV2."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TargetContractError(message)


@dataclass(frozen=True, slots=True)
class TargetHeaderV1:
    """What every label declares regardless of shape."""

    name: str
    entity: str
    #: Which catalog `grain_ref` and `as_of_ref` live in. A bare ref does not identify a column
    #: (M3) — `graph_node.object_ref` omits the catalog entirely.
    anchor_catalog: str
    grain_ref: str
    as_of_ref: str
    window_days: int
    label_type: str
    direction: str = "forward"
    operator: str | None = None
    threshold: float | None = None

    def __post_init__(self) -> None:
        _require(bool(_NAME_RE.match(self.name)),
                 f"name {self.name!r} must match {_NAME_RE.pattern}")
        _require(bool(self.entity.strip()), "entity is mandatory")
        _require(bool(self.anchor_catalog.strip()),
                 "anchor_catalog is mandatory — a ref without a catalog does not identify a "
                 "column (M3)")
        _require(bool(self.grain_ref.strip()), "grain_ref is mandatory")
        _require(bool(self.as_of_ref.strip()),
                 "as_of_ref is mandatory — a label without an anchor date cannot be computed")
        _require(self.window_days > 0, "window_days must be positive")
        _require(self.direction in DIRECTIONS,
                 f"direction must be forward, got {self.direction!r} — a rule reading backward "
                 "from the as-of date is a FEATURE, not a label")
        _require(self.label_type in LABEL_TYPES,
                 f"label_type {self.label_type!r} not in {LABEL_TYPES}")
        thresholded = self.operator is not None or self.threshold is not None
        if self.label_type == "binary":
            _require(self.operator is not None and self.threshold is not None,
                     "a binary label REQUIRES operator and threshold")
            # Reported separately: "requires an operator" misdirects when one was supplied and is
            # simply not a recognised comparison.
            _require(self.operator in OPERATORS,
                     f"operator {self.operator!r} not in {OPERATORS}")
        else:
            _require(not thresholded,
                     f"a {self.label_type} label FORBIDS operator/threshold — it measures, "
                     "it does not threshold")
