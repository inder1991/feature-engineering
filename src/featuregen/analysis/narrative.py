"""Every number in the prose must come from a cell in the result.

The last place a grounded system can quietly stop being grounded. The plan is checked, the SQL is
the SQL that runs, the answer reconciles — and then a sentence says "transaction volumes fell by
roughly a third across the retail segment" and none of that was measured. Prose is where a careful
pipeline launders a guess into a finding, because a number in a sentence carries the authority of
everything upstream of it.

So the rule here is mechanical and absolute: **a claim may contain no number that is not in a cell it
cites.** Not "should be consistent with" — the digits must be present in a cited cell. A model can
still write a wrong SENTENCE around a right number, and no checker fixes that; what this removes is
the whole class where the number itself was never computed.

**Derived figures are cells too.** "A 67% drop" is a legitimate thing to say and 67 appears nowhere
in a result of counts, so a caller may add computed cells — a delta, a percentage — to the citable
universe. That keeps the invariant exact rather than carving out an exception for arithmetic: if you
want to say it, compute it and cite it.

**Period labels are citable**, because "in 2026-05" is a claim about which slice was read, and the
digits in a partition label would otherwise read as uncited numbers.

**No LLM here.** Generation belongs elsewhere; this is the gate every generated sentence passes
through, and a gate that shares a model with the thing it checks is not a gate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from featuregen.data_agent.analysis import AnalysisRow


class NarrativeError(ValueError):
    """A claim that cannot stand. Carries the code and the offending fragment."""

    def __init__(self, code: str, message: str, *, subject: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.subject = subject


#: A FIGURE: digits with optional thousands separators, decimals, and hyphenated groups so a period
#: label like `2026-05` stays one token instead of becoming the numbers 2026 and 5.
#:
#: The lookarounds are what stop a figure being a fragment of an identifier — without them the `1` in
#: entity key `C1` reads as an uncited number and every honest sentence about a customer is rejected.
#: Being greedy about separators is deliberate in the other direction: a checker that overlooks
#: `1,240` because of the comma is worse than useless, since the figures most worth inventing are the
#: large ones.
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])-?\d[\d,]*(?:\.\d+)?(?:-\d+)*(?![A-Za-z0-9_])")


def _normalise(token: str) -> str:
    """Compare by VALUE, not spelling: `1,240`, `1240` and `1240.0` are one number.

    Without this a narrative citing a real cell would be rejected for formatting its number the way
    a person writes it, and the pressure would be to loosen the check rather than fix the comparison.
    """
    cleaned = token.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return cleaned
    return str(int(value)) if value == int(value) else str(value)


@dataclass(frozen=True, slots=True)
class Cell:
    """One citable value from the result."""

    row_key: str          # the entity, or "" for a result-level figure such as a period label
    column: str
    value: str

    @property
    def ref(self) -> str:
        return f"{self.row_key}.{self.column}" if self.row_key else self.column


@dataclass(frozen=True, slots=True)
class Claim:
    """One sentence and the cells it rests on."""

    text: str
    cites: tuple[str, ...] = ()      # Cell.ref values


@dataclass(frozen=True, slots=True)
class CitableResult:
    """Everything a narrative is allowed to refer to."""

    cells: dict[str, Cell] = field(default_factory=dict)

    def values(self, refs: Iterable[str]) -> list[str]:
        return [self.cells[ref].value for ref in refs if ref in self.cells]


def citable_result(rows: Sequence[AnalysisRow], *,
                   periods: Iterable[str] = (),
                   derived: Iterable[Cell] = ()) -> CitableResult:
    """The citable universe: every count, every dimension value, the periods, plus any derived cells.

    `derived` is how a caller makes an arithmetic claim sayable — compute the percentage, add it as a
    cell, and the narrative may cite it. The alternative, allowing "obviously derivable" numbers
    through unchecked, is precisely the gap a model fills with plausible ones.
    """
    cells: dict[str, Cell] = {}

    def _put(cell: Cell) -> None:
        cells[cell.ref] = cell

    for row in rows:
        _put(Cell(row_key=row.key, column="previous_count", value=str(row.previous_count)))
        _put(Cell(row_key=row.key, column="current_count", value=str(row.current_count)))
        for name, value in row.dimensions.items():
            _put(Cell(row_key=row.key, column=name, value=str(value)))
    for label in periods:
        _put(Cell(row_key="", column=f"period:{label}", value=str(label)))
    for cell in derived:
        _put(cell)
    return CitableResult(cells=cells)


def validate_claim(claim: Claim, result: CitableResult) -> None:
    """Raise unless every number in the claim appears in a cell the claim cites."""
    if not claim.cites:
        # A sentence with no numbers still needs a citation: "retail customers declined the most" is
        # a claim about the data, and an uncited one cannot be checked at all.
        raise NarrativeError(
            "CLAIM_UNCITED", f"claim cites nothing: {claim.text!r}", subject=claim.text)

    unknown = [ref for ref in claim.cites if ref not in result.cells]
    if unknown:
        raise NarrativeError(
            "CITATION_UNKNOWN",
            f"cited cell {unknown[0]!r} is not in the result", subject=unknown[0])

    cited = {_normalise(v) for v in result.values(claim.cites)}
    for token in _NUMBER.findall(claim.text):
        if _normalise(token) not in cited:
            raise NarrativeError(
                "NUMBER_NOT_IN_CITED_CELLS",
                f"{token!r} appears in the claim but in none of the cells it cites "
                f"({', '.join(claim.cites)}) — a figure that was never computed",
                subject=token)


def validate_narrative(claims: Sequence[Claim], result: CitableResult) -> None:
    """Every claim, in order. The first failure names itself rather than reporting "the narrative is
    ungrounded", so a caller can regenerate one sentence instead of discarding the answer."""
    for claim in claims:
        validate_claim(claim, result)
