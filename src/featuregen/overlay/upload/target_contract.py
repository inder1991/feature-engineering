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

#: WHICH as-of dates the label is evaluated on. Mandatory and undefaulted: a rule that does not say
#: this does not define a dataset, and two teams using "the same" label at different frequencies
#: get different training sets — which destroys the comparability the registry exists to provide.
#: A different frequency is a different dataset and deserves its own named label, exactly as a
#: different window does.
AS_OF_FREQUENCIES = ("daily", "weekly", "monthly", "quarterly", "single")

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
    as_of_frequency: str
    label_type: str
    #: CENSORING. A customer as-of 15 November with a 90-day window needs history through 13
    #: February to be observable. Where it is not, a rule that emits 0 says "did not happen" when
    #: the truth is "cannot see" — every recent row becomes a false negative and the model learns
    #: that recent customers are safe, which is exactly backwards. Default refuses; a survival
    #: design handling censoring itself may switch it off DELIBERATELY.
    require_full_window: bool = True
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
        _require(self.as_of_frequency in AS_OF_FREQUENCIES,
                 f"as_of_frequency {self.as_of_frequency!r} not in {AS_OF_FREQUENCIES} — a rule "
                 "that does not say which as-of dates it is evaluated on does not define a "
                 "dataset")
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
    #: A NULL at the as-of date means the row's eligibility cannot be determined. Including it
    #: silently invents an answer, so the default drops the row.
    exclude_null_at_as_of: bool = True

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


#: A CLOSED operator set. An open one is an open grammar: free text in a stored, model-authored
#: definition is an injection surface, un-auditable, and — the reason that actually forced this —
#: INVISIBLE TO LINEAGE. A filter written as `"tran_crncy <> 'AED'"` reads a column that
#: `refs_read` cannot see, so `target_derives_from` would answer "no label depends on tran_crncy"
#: while one silently did.
FILTER_OPS = ("==", "!=", ">", ">=", "<", "<=", "in", "not_in")
#: The two ops taking a list. Between them they cover the common OR case ("currency in (USD, EUR)")
#: without admitting OR into the grammar.
_LIST_OPS = ("in", "not_in")


@dataclass(frozen=True, slots=True)
class EventFilterV1:
    """One condition on the event rows. Conditions are ANDed; there is deliberately no OR — that
    is the stated cost of a closed structure, and `in` covers the case that otherwise needs it."""

    column_ref: str
    op: str
    value: str | None = None            # a literal
    values: tuple[str, ...] = ()        # a literal list, for in / not_in
    value_ref: str | None = None        # ANOTHER COLUMN — needs no unverifiable literal at all

    def __post_init__(self) -> None:
        _require(bool(self.column_ref.strip()), "column_ref is mandatory")
        _require(self.op in FILTER_OPS, f"op {self.op!r} not in {FILTER_OPS}")
        supplied = sum([self.value is not None, bool(self.values),
                        self.value_ref is not None])
        _require(supplied == 1,
                 "exactly one of value / values / value_ref must be supplied")
        if self.op in _LIST_OPS:
            _require(bool(self.values),
                     f"{self.op} takes values (a list), not a single value")
        else:
            _require(not self.values,
                     f"{self.op} takes a single value or value_ref, not values (a list)")


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
    event_filters: tuple[EventFilterV1, ...] = ()
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
        # A filter reads columns too. Missing them made `target_derives_from` answer "nothing
        # depends on this column" about a column a label genuinely depends on.
        for condition in rule.event_filters:
            refs.add((rule.event_catalog, condition.column_ref))
            if condition.value_ref is not None:
                refs.add((rule.event_catalog, condition.value_ref))
    return tuple(sorted(refs))


def canonical_target(rule: TargetRuleV1) -> dict:
    """The rule as a plain, ordered dict — the body the content hash is taken over."""
    # `asdict` recurses into the nested header dataclass already — no second pass needed.
    return asdict(rule)


def target_content_hash(rule: TargetRuleV1) -> str:
    """Identity by content, matching `model_feature_revision_hash`."""
    body = json.dumps(canonical_target(rule), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _window_clause(header: TargetHeaderV1) -> str:
    censoring = (f"only where the full {header.window_days} days can be observed"
                 if header.require_full_window
                 else "INCLUDING rows whose window runs past the end of history")
    return (f"over the next {header.window_days} days, sampled {header.as_of_frequency}, "
            f"{censoring}")


def describe_target(rule: TargetRuleV1) -> str:
    """The rule as one plain sentence — what a person actually gives concurrence to.

    A form of a dozen fields gets rubber-stamped; a statement of MEANING gets read. Rendered
    deterministically from the rule itself, with no model call, so it can never drift from what
    was registered — and it states the two things a data scientist checks first, the sampling
    frame and the censoring rule, which the field list would otherwise bury.
    """
    h = rule.header
    if isinstance(rule, StateChangeRuleV1):
        population = ("among those starting in that state"
                      if rule.population_filter == "from_values" else "across everyone")
        nulls = (" Rows whose state cannot be read at the as-of date are excluded."
                 if rule.exclude_null_at_as_of else "")
        return (
            f"{h.name}: one row per {h.entity}, as of {h.as_of_ref}. The label is 1 when "
            f"{rule.column_ref} moves from {list(rule.from_values)} to {list(rule.to_values)} "
            f"{_window_clause(h)}, {population}.{nulls}")

    filters = "".join(
        f" where {f.column_ref} {f.op} "
        f"{f.value_ref or (list(f.values) if f.values else f.value)!r}"
        for f in rule.event_filters)
    if h.label_type == "binary":
        measured = (f"at least {int(h.threshold or 0)} matching rows"
                    if h.operator in (">=", ">") else
                    f"{h.operator} {int(h.threshold or 0)} matching rows")
    else:
        measured = f"the {rule.aggregate} of {rule.measure_ref or 'matching rows'}"
    population = (
        f" among {h.entity}s with none in the prior {rule.population_lookback_days} days"
        if rule.population_having == "none" else "")
    return (
        f"{h.name}: one row per {h.entity}, as of {h.as_of_ref}. The label is {measured} in "
        f"{rule.event_table}{filters}, {_window_clause(h)}{population}.")
