"""Choose a chart from a closed vocabulary, and never plot a person.

"Safe" here is a disclosure property, not an aesthetic one. A result of one row per customer is
individual-level data; drawing it is publishing it. And a grouped chart is worse than a table for
this, because a bar of height one is instantly legible as "there is exactly one person in this
segment, and here is their activity" — which is why `SMALL_CELL_RISK` is already in the finding
vocabulary.

Three rules, in order of how badly breaking them fails:

**A chart plots AGGREGATES, never entities.** Every point is a count of entities in a group, never an
entity's own number. A per-customer bar chart of the pilot result would name six customers and their
transaction counts on one screen.

**A group smaller than the threshold is SUPPRESSED**, because a group of one is that person. The
threshold is a parameter with a conservative default rather than a constant, since the right value is
a policy decision — but there is no way to ask for zero.

**Suppression is REPORTED.** A chart that quietly drops three segments looks like a complete picture
of the whole population, which is a worse lie than showing nothing. The spec carries how many groups
were withheld and how many entities they covered, so a caller can say so.

The chart KIND is chosen deterministically from the result's shape, from a closed enum. No model
picks it and no code is generated: a chart type is a rendering decision with a right answer given the
data, and the failure mode of getting it wrong — a pie of forty categories, a line over one period —
is a misleading picture rather than an error.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from featuregen.analysis.plan import AnalysisPlanV1
from featuregen.data_agent.analysis import AnalysisRow

#: Below this many entities a group is withheld. Conservative by default and overridable, because the
#: right number is a policy decision — five is the common floor for published statistics.
DEFAULT_MIN_CELL_SIZE = 5


class ChartKind(StrEnum):
    """Closed. A kind absent here cannot be produced, so a new one is a decision rather than a
    plausible-looking string arriving from somewhere."""

    #: Counts per group — the shape of "how many customers declined, by segment".
    BAR = "bar"
    #: Two counts, no grouping: how many met the comparison and how many did not.
    SUMMARY = "summary"
    #: Nothing may be shown — every group was too small, or there is nothing to compare.
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ChartPoint:
    label: str
    value: int


@dataclass(frozen=True, slots=True)
class ChartSpec:
    kind: ChartKind
    points: tuple[ChartPoint, ...] = ()
    x_label: str = ""
    y_label: str = ""
    #: Groups withheld for being too small, and how many entities they covered. Reported so a caller
    #: can say the picture is partial; a silent drop reads as a complete population.
    suppressed_groups: int = 0
    suppressed_entities: int = 0
    min_cell_size: int = DEFAULT_MIN_CELL_SIZE
    #: Why nothing can be drawn, when `kind` is NONE.
    reason: str = ""
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_partial(self) -> bool:
        return self.suppressed_groups > 0


def _matches(row: AnalysisRow, comparison: str) -> bool:
    if comparison == "decrease":
        return row.decreased
    if comparison == "increase":
        return row.increased
    if comparison == "change":
        return row.decreased or row.increased
    return True


def choose_chart(rows: Sequence[AnalysisRow], plan: AnalysisPlanV1, *,
                 min_cell_size: int = DEFAULT_MIN_CELL_SIZE) -> ChartSpec:
    """The chart for this result, or an explicit refusal to draw one.

    `min_cell_size` is clamped to at least 1: a threshold of zero would suppress nothing and turn
    this into a plotting helper, which is the one thing it must not silently become.
    """
    threshold = max(1, int(min_cell_size))
    comparison = (plan.comparison or "").strip().lower()
    if not rows:
        return ChartSpec(kind=ChartKind.NONE, reason="the result has no rows",
                         min_cell_size=threshold)

    matching = [r for r in rows if _matches(r, comparison)]

    if not plan.dimensions:
        # No grouping: the only aggregate available is how many entities met the comparison. Even
        # here the counts are of ENTITIES, never a list of them.
        if not comparison:
            return ChartSpec(
                kind=ChartKind.NONE, min_cell_size=threshold,
                reason="a single-population result with no grouping and no comparison has nothing "
                       "to plot that is not the rows themselves")
        return ChartSpec(
            kind=ChartKind.SUMMARY, min_cell_size=threshold,
            x_label=comparison, y_label="customers",
            points=(ChartPoint(label=comparison, value=len(matching)),
                    ChartPoint(label=f"no {comparison}", value=len(rows) - len(matching))))

    # Grouped: one point per dimension VALUE, valued by how many entities fall in it. The dimension
    # is the first one — a chart with two group-bys is a table, and pretending otherwise produces a
    # picture nobody can read.
    dimension = plan.dimensions[0].logical_ref
    column = dimension.rsplit(".", 1)[-1]
    counts: dict[str, int] = {}
    for row in matching:
        counts[str(row.dimensions.get(column, "Unknown"))] = counts.get(
            str(row.dimensions.get(column, "Unknown")), 0) + 1

    kept = {label: n for label, n in counts.items() if n >= threshold}
    suppressed = {label: n for label, n in counts.items() if n < threshold}
    if not kept:
        return ChartSpec(
            kind=ChartKind.NONE, min_cell_size=threshold,
            suppressed_groups=len(suppressed), suppressed_entities=sum(suppressed.values()),
            reason=f"every group is smaller than {threshold}; drawing any of them would identify "
                   "the individuals in it",
            findings=("SMALL_CELL_RISK",))

    return ChartSpec(
        kind=ChartKind.BAR, min_cell_size=threshold,
        x_label=column, y_label=f"customers ({comparison})" if comparison else "customers",
        points=tuple(ChartPoint(label=label, value=counts[label])
                     for label in sorted(kept, key=lambda k: (-counts[k], k))),
        suppressed_groups=len(suppressed), suppressed_entities=sum(suppressed.values()),
        findings=("SMALL_CELL_RISK",) if suppressed else ())
