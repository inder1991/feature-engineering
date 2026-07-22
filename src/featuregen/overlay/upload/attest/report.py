"""P0 shadow-measurement harness — Task 6: the REPORT / METRIC (design §Components/6).

``shadow_report`` is the go/no-go ARTIFACT: it JOINs ``attestation_shadow_observation`` (Task 1) to
``attestation_gold_label`` (Task 1) AT READ TIME. The observation never stores a gold value (see the
migration 1018 / ``shadow_store.py`` docstrings), so correcting a mislabelled gold row and re-running
this report yields an updated number WITHOUT re-running any signal — a test proves this property.

For a threshold sweep (0.50 -> 0.95 step 0.05) and split by (``all`` / ``grounding_covered`` /
``grounding_thin``) x ``field_name``, computes per cell:

* ``false_attest_rate`` — among AUTO-ATTESTED observations (``confidence >= T`` AND
  ``risk_tier == 'low'``), the fraction whose ``proposer_value`` disagrees with the joined
  ``gold_value`` (case/whitespace-insensitive, mirroring ``fusion._values_agree``'s convention), with
  a Wilson score 95% CI (:func:`wilson_ci` — implemented directly, no scipy).
* ``auto_attestable_fraction`` — share of ALL gold-joined observations in the cell that clear the gate.
* ``grounding_coverage_distribution`` — a histogram of the cell's observed ``grounding_coverage``
  values (rounded to 4dp to absorb ``numeric``-to-``float`` round-trip noise).
* ``n`` — the cell's total gold-joined population.
* ``triaged_low_n`` / ``defaulted_low_n`` — among the auto-attested set, how many ``'low'``
  ``risk_tier`` observations had TAXONOMY evidence informing that tier (TRIAGED) vs none at all
  (DEFAULTED — an untriaged column that reads ``'low'`` only because ``runner._risk_tier`` treats "no
  signal" as "no risk", per that function's own docstring: "No signal at all ... -> 'low', mirroring
  grounding's own 'absent is not a conflict' convention"). Surfacing this split keeps an untriaged
  low-risk bulk from silently hiding inside the headline number (the T5 note).

READ-ONLY over the DB: only SELECTs (the join, plus a per-column taxonomy-evidence lookup for the
triage segmentation, via the same :func:`read_active_field_evidence` reader every other Task in this
harness uses). This module writes nothing.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.overlay.evidence import EvidenceProducer
from featuregen.overlay.field_evidence import read_active_field_evidence

# The threshold sweep: 0.50, 0.55, ..., 0.95 (10 points). round() avoids float-step drift
# (0.5 + 0.05*i accumulates binary rounding error past a handful of steps).
THRESHOLDS: tuple[float, ...] = tuple(round(0.50 + 0.05 * i, 2) for i in range(10))

SPLIT_ALL = "all"
SPLIT_GROUNDING_COVERED = "grounding_covered"
SPLIT_GROUNDING_THIN = "grounding_thin"
_SPLITS: tuple[str, ...] = (SPLIT_ALL, SPLIT_GROUNDING_COVERED, SPLIT_GROUNDING_THIN)

_LOW_RISK = "low"
_Z_95 = 1.96   # standard normal 97.5th-percentile critical value for a two-sided 95% CI

# The taxonomy fields runner._risk_tier reads to derive a 'low'/'high' risk tier (mirrored here, not
# imported, because the report needs the RAW presence/absence signal — not the tier the runner already
# wrote — and must stay independent of the runner's write-path internals; it is read-only over the
# same field_evidence reader every Task in this harness already uses).
_TRIAGE_FIELDS: tuple[str, ...] = ("sensitivity_floor", "leakage_anchor")


@dataclass(frozen=True, slots=True)
class WilsonCIV1:
    """A Wilson score confidence interval, ``[lower, upper]`` in ``[0, 1]``."""

    lower: float
    upper: float


def wilson_ci(k: int, n: int, *, z: float = _Z_95) -> WilsonCIV1:
    """The Wilson score CI for ``k`` successes out of ``n`` trials — implemented directly (no scipy).

    ``n <= 0`` returns the maximally-uncertain ``(0.0, 1.0)`` interval: there is no evidence to narrow
    it. Bounds are clamped to ``[0, 1]`` (the closed-form can spill a fraction of a ULP past either
    edge at the extremes, e.g. ``k == n``)."""
    if n <= 0:
        return WilsonCIV1(0.0, 1.0)
    p_hat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n)) / denom
    return WilsonCIV1(lower=max(0.0, center - margin), upper=min(1.0, center + margin))


@dataclass(frozen=True, slots=True)
class ReportCellV1:
    """One (threshold, split, field_name) cell of the sweep."""

    threshold: float
    split: str
    field_name: str
    n: int                                  # total gold-joined observations in this cell
    auto_attested_n: int                    # confidence >= threshold AND risk_tier == 'low'
    false_attest_n: int                     # of the above, fused value != gold value
    false_attest_rate: float
    false_attest_ci: WilsonCIV1
    auto_attestable_fraction: float         # auto_attested_n / n (0.0 when n == 0)
    grounding_coverage_distribution: Mapping[float, int]
    triaged_low_n: int                      # auto-attested 'low' with TAXONOMY evidence present
    defaulted_low_n: int                    # auto-attested 'low' with NO taxonomy evidence at all


@dataclass(frozen=True, slots=True)
class ReportV1:
    """The full sweep for one shadow run — the go/no-go artifact."""

    shadow_run_id: str
    thresholds: tuple[float, ...]
    field_names: tuple[str, ...]
    cells: tuple[ReportCellV1, ...]

    def cell(self, *, threshold: float, split: str, field_name: str) -> ReportCellV1 | None:
        for c in self.cells:
            if c.threshold == threshold and c.split == split and c.field_name == field_name:
                return c
        return None


@dataclass(frozen=True, slots=True)
class _JoinedRow:
    logical_ref: str
    field_name: str
    proposer_value: str | None
    confidence: float
    risk_tier: str
    grounding_coverage: float
    gold_value: str


def _values_match(a: str | None, b: str | None) -> bool:
    """Case/whitespace-insensitive match; a missing value on either side never matches — mirrors
    ``fusion._values_agree``'s convention (a missing value is scored the same as a disagreement)."""
    if a is None or b is None:
        return False
    return a.strip().lower() == b.strip().lower()


def _joined_rows(conn: DbConn, shadow_run_id: str) -> list[_JoinedRow]:
    """The READ-TIME JOIN: observation x gold label on (logical_ref, field_name), scoped to one run.
    An observation with no gold label at all (never labelled, or not yet ingested) simply does not
    appear here — it is outside the scored population, not a mismatch."""
    rows = conn.execute(
        "SELECT o.logical_ref, o.field_name, o.proposer_value, o.confidence, o.risk_tier, "
        "       o.grounding_coverage, g.gold_value "
        "FROM attestation_shadow_observation o "
        "JOIN attestation_gold_label g "
        "  ON g.logical_ref = o.logical_ref AND g.field_name = o.field_name "
        "WHERE o.shadow_run_id = %s",
        (shadow_run_id,)).fetchall()
    return [
        _JoinedRow(logical_ref=r[0], field_name=r[1], proposer_value=r[2], confidence=float(r[3]),
                  risk_tier=r[4], grounding_coverage=float(r[5]), gold_value=r[6])
        for r in rows
    ]


def _low_risk_triaged(conn: DbConn, logical_ref: str) -> bool:
    """True iff the column has ANY active TAXONOMY evidence on the risk-driving fields — i.e. its
    ``'low'`` risk_tier was TRIAGED (a taxonomy signal existed and was considered), not DEFAULTED (no
    signal at all, silently read as low risk per ``runner._risk_tier``'s own "absent is not a
    conflict" convention)."""
    for field_name in _TRIAGE_FIELDS:
        for ev in read_active_field_evidence(conn, logical_ref, field_name):
            if ev.producer == EvidenceProducer.TAXONOMY.value:
                return True
    return False


def _in_split(row: _JoinedRow, split: str) -> bool:
    if split == SPLIT_ALL:
        return True
    if split == SPLIT_GROUNDING_COVERED:
        return row.grounding_coverage > 0.0
    if split == SPLIT_GROUNDING_THIN:
        return row.grounding_coverage == 0.0
    raise ValueError(f"unknown split: {split!r}")


def _coverage_distribution(rows: list[_JoinedRow]) -> dict[float, int]:
    dist: dict[float, int] = {}
    for row in rows:
        key = round(row.grounding_coverage, 4)
        dist[key] = dist.get(key, 0) + 1
    return dist


def _build_cell(rows: list[_JoinedRow], *, threshold: float, split: str, field_name: str,
                triaged: dict[str, bool]) -> ReportCellV1:
    n = len(rows)
    auto = [r for r in rows if r.confidence >= threshold and r.risk_tier == _LOW_RISK]
    auto_n = len(auto)
    false_n = sum(1 for r in auto if not _values_match(r.proposer_value, r.gold_value))
    rate = false_n / auto_n if auto_n else 0.0
    triaged_n = sum(1 for r in auto if triaged[r.logical_ref])
    return ReportCellV1(
        threshold=threshold, split=split, field_name=field_name, n=n, auto_attested_n=auto_n,
        false_attest_n=false_n, false_attest_rate=rate, false_attest_ci=wilson_ci(false_n, auto_n),
        auto_attestable_fraction=(auto_n / n if n else 0.0),
        grounding_coverage_distribution=_coverage_distribution(rows),
        triaged_low_n=triaged_n, defaulted_low_n=auto_n - triaged_n)


def shadow_report(conn: DbConn, shadow_run_id: str) -> ReportV1:
    """Build the full threshold-sweep x split x field report for one shadow run. READ-ONLY: SELECTs
    only, no write. Re-running this after ``attestation_gold_label`` changes (a corrected/late-arrived
    label) reflects the new state immediately — the observation carries no cached gold value."""
    rows = _joined_rows(conn, shadow_run_id)
    field_names = tuple(sorted({r.field_name for r in rows}))

    # One taxonomy-evidence lookup per distinct logical_ref (not per row) — a column can appear under
    # multiple field_names but its triage status is a property of the COLUMN, not the field.
    triaged: dict[str, bool] = {
        logical_ref: _low_risk_triaged(conn, logical_ref)
        for logical_ref in {r.logical_ref for r in rows}
    }

    cells: list[ReportCellV1] = []
    for threshold in THRESHOLDS:
        for split in _SPLITS:
            split_rows = [r for r in rows if _in_split(r, split)]
            for field_name in field_names:
                cell_rows = [r for r in split_rows if r.field_name == field_name]
                cells.append(_build_cell(cell_rows, threshold=threshold, split=split,
                                         field_name=field_name, triaged=triaged))
    return ReportV1(shadow_run_id=shadow_run_id, thresholds=THRESHOLDS, field_names=field_names,
                    cells=tuple(cells))


__all__ = [
    "SPLIT_ALL", "SPLIT_GROUNDING_COVERED", "SPLIT_GROUNDING_THIN", "THRESHOLDS", "ReportCellV1",
    "ReportV1", "WilsonCIV1", "shadow_report", "wilson_ci",
]
