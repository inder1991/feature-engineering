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

import hashlib
import json
import re
from dataclasses import asdict, dataclass

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


#: How the population is bounded for a state-change rule. `from_values` excludes rows that
#: ALREADY have the outcome at the as-of date — a customer already non-performing on 1 January is
#: not a candidate. Omitting this is the most common way to build a silently broken label.
STATE_POPULATIONS = ("from_values", "all")


@dataclass(frozen=True, slots=True)
class StateChangeRuleV1:
    """A column's value at the as-of date, compared against its value inside the window.

    Requires an append-only snapshot source: if the source rewrites a row in place rather than
    appending a new as-of, "the value in January" silently returns a later one. `header.as_of_ref`
    records which column carries that assumption.
    """

    header: TargetHeaderV1
    column_ref: str
    from_values: tuple[str, ...]
    to_values: tuple[str, ...]
    at_least_once: bool = True
    population_filter: str = "from_values"

    shape: str = "state_change"

    def __post_init__(self) -> None:
        _require(bool(self.column_ref.strip()), "column_ref is mandatory")
        _require(bool(self.from_values), "from_values is mandatory")
        _require(bool(self.to_values), "to_values is mandatory")
        overlap = set(self.from_values) & set(self.to_values)
        _require(not overlap,
                 f"{sorted(overlap)!r} appear in both from_values and to_values — a change from "
                 "a state to itself is not an outcome")
        _require(self.population_filter in STATE_POPULATIONS,
                 f"population_filter {self.population_filter!r} not in {STATE_POPULATIONS}")
        _require(self.header.label_type == "binary",
                 "a state_change label must be binary — a state changed or it did not")
        _require(self.column_ref != self.header.as_of_ref,
                 "column_ref and as_of_ref are the same column — the rule would compare a date "
                 "against itself and observe nothing")


AGGREGATES = ("count", "sum")
#: `none` restricts the population to rows with NO matching event in the lookback before the as-of
#: date ("who will START"); `any` is the whole population ("who will do it at all"). The difference
#: is which question is being asked, and `any` silently yields the degenerate label.
EVENT_POPULATIONS = ("any", "none")


@dataclass(frozen=True, slots=True)
class EventWindowRuleV1:
    """Rows in a second table, inside the window, joined to the grain.

    Cross-catalog by construction — anchored in one catalog, counting events in another. The join
    is DECLARED in a reviewed definition rather than decided by a planner at request time, so this
    needs none of the live cross-catalog machinery.
    """

    header: TargetHeaderV1
    #: The catalog the EVENT side lives in — routinely different from `header.anchor_catalog`,
    #: which is what makes this shape cross-catalog and why it must be stated.
    event_catalog: str
    event_table: str
    event_date_ref: str
    join_left: str
    join_right: str
    aggregate: str
    event_filter: str | None = None
    measure_ref: str | None = None
    population_lookback_days: int = 0
    population_having: str = "any"

    shape: str = "event_window"

    def __post_init__(self) -> None:
        for field_name in ("event_catalog", "event_table", "event_date_ref",
                           "join_left", "join_right"):
            _require(bool(getattr(self, field_name).strip()), f"{field_name} is mandatory")
        _require(self.aggregate in AGGREGATES,
                 f"aggregate {self.aggregate!r} not in {AGGREGATES}")
        if self.aggregate == "sum":
            _require(self.measure_ref is not None and bool(self.measure_ref.strip()),
                     "a sum aggregate REQUIRES measure_ref — there is nothing to add up")
        else:
            _require(self.measure_ref is None,
                     "a count aggregate FORBIDS measure_ref — it counts rows, not values")
        _require(self.population_having in EVENT_POPULATIONS,
                 f"population_having {self.population_having!r} not in {EVENT_POPULATIONS}")
        if self.population_having == "none":
            _require(self.population_lookback_days > 0,
                     "excluding prior activity REQUIRES a positive population lookback — "
                     "'not currently doing this' is meaningless without a period")
        _require(self.population_lookback_days >= 0,
                 "population_lookback_days cannot be negative")


TargetRuleV1 = StateChangeRuleV1 | EventWindowRuleV1


def refs_read(rule: TargetRuleV1) -> tuple[tuple[str, str], ...]:
    """Every (catalog_source, object_ref) pair the rule reads, sorted and deduped.

    For LINEAGE and IMPACT — "this column is being retired, which labels break?" — and NOT a
    leakage blocklist. A feature reading the same columns BACKWARD from the as-of date is the
    method (past FX predicting future FX); blocking on column overlap would delete the use case.
    The leakage control is temporal and already exists. Spec §8, §9.

    Pairs, never bare refs: `object_ref` is only `public.{table}.{column}` (M3).
    """
    anchor = rule.header.anchor_catalog
    refs = {(anchor, rule.header.grain_ref), (anchor, rule.header.as_of_ref)}
    if isinstance(rule, StateChangeRuleV1):
        refs.add((anchor, rule.column_ref))
    else:
        # join_left is on the ANCHOR side, join_right on the EVENT side — the whole point of the
        # pair. Collapsing them to bare refs is the M3 defect.
        refs.add((anchor, rule.join_left))
        refs.update({(rule.event_catalog, rule.event_date_ref),
                     (rule.event_catalog, rule.join_right)})
        if rule.measure_ref is not None:
            refs.add((rule.event_catalog, rule.measure_ref))
    return tuple(sorted(refs))


def canonical_target(rule: TargetRuleV1) -> dict:
    """The rule as a plain, ordered dict — the body the content hash is taken over."""
    # `asdict` recurses into the nested header dataclass already — no second pass needed.
    return asdict(rule)


def target_content_hash(rule: TargetRuleV1) -> str:
    """Identity by content, matching `model_feature_revision_hash`."""
    body = json.dumps(canonical_target(rule), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()
