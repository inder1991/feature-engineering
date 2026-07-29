"""Relationship evidence — what the DATA says about a candidate join.

Every fact about a relationship in this system has so far been declared: a file said `joins_to`, or
a concept registry said two columns denote the same entity, or an ingest path fabricated `N:1` for a
blank cell. This is the first evidence that comes from the values.

It produces five things a declaration cannot:

* **uniqueness per side** — whether either column is actually a key;
* **null rate** on the referencing side;
* **referential coverage** — how many distinct left values have a matching right value. An
  unmatched key is the strongest cheap signal that two identifier columns do NOT share a namespace;
* **join multiplier** — how many left rows one right key attracts, measured rather than assumed;
* **observed cardinality**, which is what `graph.py`'s propose-time `or "N:1"` currently guesses.

**It promotes nothing.** Release 1 produces evidence; Release 2 decides what it may support. The
rule governing that decision lives here as :meth:`RelationshipEvidenceV1.uniqueness_verdict`, and it
is deliberately asymmetric: a duplicate DISPROVES uniqueness however it was found, while finding no
duplicate proves nothing unless the probe was exact.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.data_agent.observation import require_identifier
from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1


@dataclass(frozen=True, slots=True)
class RelationshipProbeV1:
    """One candidate relationship: a referencing column and the column it may reference."""

    left_binding: PhysicalDatasetBindingV1
    left_column: str
    right_binding: PhysicalDatasetBindingV1
    right_column: str
    policy: ProfilePolicyV1

    def __post_init__(self) -> None:
        require_identifier(self.left_column, what="left column")
        require_identifier(self.right_column, what="right column")


@dataclass(frozen=True, slots=True)
class RelationshipEvidenceV1:
    """Observed facts about one candidate relationship. Counts and ratios only — never a key value:
    an unmatched-key COUNT is a statistic, while the unmatched keys themselves are identifiers."""

    left_physical_id: str
    left_column: str
    right_physical_id: str
    right_column: str
    left_rows: int
    left_distinct: int
    left_nulls: int
    right_rows: int
    right_distinct: int
    matched_distinct: int
    unmatched_distinct: int
    max_left_rows_per_right_key: int
    method: str

    @property
    def left_is_unique(self) -> bool:
        """Observed, not asserted — see :meth:`uniqueness_verdict` for what it may SUPPORT."""
        return self.left_rows > 0 and self.left_distinct == self.left_rows

    @property
    def right_is_unique(self) -> bool:
        return self.right_rows > 0 and self.right_distinct == self.right_rows

    @property
    def coverage_ratio(self) -> float:
        """Distinct left values with a match, over distinct left values. 1.0 means every referencing
        value exists on the other side."""
        total = self.matched_distinct + self.unmatched_distinct
        return (self.matched_distinct / total) if total else 0.0

    @property
    def observed_cardinality(self) -> str:
        """What the data shows, in the vocabulary the governed fact uses."""
        if self.right_is_unique and self.left_is_unique:
            return "one_to_one"
        if self.right_is_unique:
            return "many_to_one"
        if self.left_is_unique:
            return "one_to_many"
        return "many_to_many"

    def uniqueness_verdict(self, side: str) -> str:
        """`unique` | `not_unique` | `unknown` — deliberately ASYMMETRIC.

        A duplicate disproves uniqueness however it was found, so `not_unique` is safe from an
        approximate probe. The reverse is not: an approximate distinct count that happens to equal
        the row count proves nothing, and returning `unique` there is exactly how a cheap profile
        would silently promote a bad key. Only an EXACT probe may assert uniqueness.
        """
        is_unique = self.left_is_unique if side == "left" else self.right_is_unique
        if not is_unique:
            return "not_unique"
        return "unique" if self.method == "exact" else "unknown"


#: Why a piece of evidence does not support a join. Named here rather than in `analysis.py` because
#: the rule is about relationships, not about one query shape — a second consumer would otherwise
#: re-derive it and drift.
JOIN_EVIDENCE_MISSING = "JOIN_EVIDENCE_MISSING"
JOIN_EVIDENCE_MISMATCHED = "JOIN_EVIDENCE_MISMATCHED"
JOIN_KEY_NOT_UNIQUE = "JOIN_KEY_NOT_UNIQUE"
JOIN_UNIQUENESS_UNKNOWN = "JOIN_UNIQUENESS_UNKNOWN"


def join_refusal(evidence: RelationshipEvidenceV1 | None, *,
                 referencing_table_id: str, referencing_column: str,
                 referenced_table_id: str, referenced_column: str) -> tuple[str, str] | None:
    """`(code, message)` if this evidence does not support joining these two columns, else `None`.

    This is the Release 2 half of the sentence at the top of this module: Release 1 produces the
    evidence, and this decides what it may support — for exactly one use, an analysis join.

    Three refusals, in order of how badly they mislead:

    * **no evidence** — the relationship has never been looked at. Refusal is the default, because
      an analysis that joins on an unexamined relationship is indistinguishable from one that joins
      on a verified relationship once the result is in a spreadsheet.
    * **mismatched evidence** — worse than none. It reads as verification in the audit trail while
      describing a relationship the query does not perform.
    * **the referenced key is not unique, or cannot be shown to be** — the correctness property. In
      a population-spine analysis the referenced side is the LEFT side of the query, so a duplicate
      key multiplies the whole population and overstates every total that includes it.

    The uniqueness test goes through :meth:`RelationshipEvidenceV1.uniqueness_verdict` rather than
    reading `right_is_unique`, because only that method knows an approximate probe may not assert
    uniqueness. Reading the raw property is how a cheap profile silently promotes a bad key.

    The referencing side is deliberately NOT required to be unique: many transactions per customer
    is the expected shape, and the analysis aggregates that side before joining anyway.
    """
    if evidence is None:
        return (JOIN_EVIDENCE_MISSING,
                f"no relationship evidence for {referencing_table_id}.{referencing_column} -> "
                f"{referenced_table_id}.{referenced_column}: the join has never been observed")

    actual = (evidence.left_physical_id, evidence.left_column,
              evidence.right_physical_id, evidence.right_column)
    expected = (referencing_table_id, referencing_column, referenced_table_id, referenced_column)
    if actual != expected:
        return (JOIN_EVIDENCE_MISMATCHED,
                f"evidence describes {actual[0]}.{actual[1]} -> {actual[2]}.{actual[3]}, but the "
                f"join is {expected[0]}.{expected[1]} -> {expected[2]}.{expected[3]}")

    verdict = evidence.uniqueness_verdict("right")
    if verdict == "not_unique":
        return (JOIN_KEY_NOT_UNIQUE,
                f"{referenced_table_id}.{referenced_column} is not unique "
                f"({evidence.right_rows} rows, {evidence.right_distinct} distinct); joining on it "
                "would duplicate the population and overstate every total")
    if verdict == "unknown":
        return (JOIN_UNIQUENESS_UNKNOWN,
                f"uniqueness of {referenced_table_id}.{referenced_column} was probed with method "
                f"{evidence.method!r}; only an exact probe may assert a key")
    return None


def _qualified(dialect, binding: PhysicalDatasetBindingV1) -> str:
    """Reuse the dialect's own table-reference rules rather than re-deriving quoting here."""
    class _Shim:                                     # minimal duck-type for `table_ref`
        def __init__(self, b): self.binding = b
    return dialect.table_ref(_Shim(binding))


def observe_relationship(conn, probe: RelationshipProbeV1, *, dialect) -> RelationshipEvidenceV1:
    """Run the probe. One statement per side plus one join probe — three bounded aggregates.

    Kept as separate statements rather than one clever query because each is independently
    meaningful, independently cheap, and independently readable in a plan preview. A single fused
    statement would be harder to review before it runs against a bank cluster.
    """
    left_ref = _qualified(dialect, probe.left_binding)
    right_ref = _qualified(dialect, probe.right_binding)
    left_col = f'"{probe.left_column}"'
    right_col = f'"{probe.right_column}"'

    left_rows, left_distinct, left_nulls = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT {left_col}), COUNT(*) - COUNT({left_col}) "
        f"FROM {left_ref}").fetchone()
    right_rows, right_distinct = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT {right_col}) FROM {right_ref}").fetchone()

    # Referential coverage over DISTINCT left values, so a popular key does not dominate the ratio.
    matched, unmatched = conn.execute(
        f"SELECT COUNT(*) FILTER (WHERE matched), COUNT(*) FILTER (WHERE NOT matched) FROM ("
        f"  SELECT EXISTS (SELECT 1 FROM {right_ref} r WHERE r.{right_col} = l.{left_col}) "
        f"         AS matched "
        f"  FROM (SELECT DISTINCT {left_col} FROM {left_ref} "
        f"        WHERE {left_col} IS NOT NULL) l) probe").fetchone()

    # Fan-out: the most left rows a single right key attracts.
    multiplier = conn.execute(
        f"SELECT COALESCE(MAX(n), 0) FROM ("
        f"  SELECT COUNT(*) AS n FROM {left_ref} "
        f"  WHERE {left_col} IS NOT NULL GROUP BY {left_col}) counts").fetchone()[0]

    return RelationshipEvidenceV1(
        left_physical_id=probe.left_binding.identity.table_id, left_column=probe.left_column,
        right_physical_id=probe.right_binding.identity.table_id, right_column=probe.right_column,
        left_rows=int(left_rows), left_distinct=int(left_distinct), left_nulls=int(left_nulls),
        right_rows=int(right_rows), right_distinct=int(right_distinct),
        matched_distinct=int(matched), unmatched_distinct=int(unmatched),
        max_left_rows_per_right_key=int(multiplier),
        method="exact" if probe.policy.exact_distinct else "approximate")
