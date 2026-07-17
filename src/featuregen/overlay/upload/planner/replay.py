"""Phase-3B.4 — replay-time freshness. Split a PURE comparator from an IMPURE current-state adapter.

A stored plan carries per-catalog stamps (the D3 ``compiler_input_fingerprint`` — the classifier's real
read-set — + ``head_seq`` + ``projection_checkpoint``) and a version set. ``ReplayFreshness`` compares
the stored evidence to the CURRENT catalog state:

  * ``current``      — every scoped fingerprint + head_seq matches and the projection is caught up.
  * ``drifted``      — a catalog-state input changed since compile (fingerprint / head_seq moved).
  * ``incompatible`` — a producer/compiler/registry VERSION mismatch (comparison not meaningful; NOT drift).
  * ``unverifiable`` — a stamp is missing/incomplete, the projection is LAGGING (checkpoint < head_seq),
    or a current value can't be read.

``unverifiable``/``incompatible`` are NEVER reported as ``current`` (fail-closed). Never mutates the record.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace

from featuregen.overlay.catalog_changes import drift_head_seq
from featuregen.overlay.upload.catalog_realizations import derive_catalog_realizations
from featuregen.overlay.upload.planner.contracts import CatalogStateStampV1, ReplayFreshness
from featuregen.overlay.upload.planner.fingerprint import _VERSIONS, compiler_input_fingerprint
from featuregen.overlay.upload.templates import _load_columns
from featuregen.projections.runner import _checkpoint_seq


def _current_versions() -> tuple[tuple[str, str], ...]:
    return _VERSIONS


@dataclass(frozen=True, slots=True)
class StoredEvidenceV1:
    """What a stored plan pins for replay: per-catalog fingerprint + head_seq, and the version set."""
    fingerprints: Mapping[str, str]      # catalog -> compiler_input_fingerprint at compile
    head_seqs: Mapping[str, int]         # catalog -> head_seq at compile
    versions: tuple[tuple[str, str], ...]

    @staticmethod
    def from_stamps(stamps: Iterable[CatalogStateStampV1],
                    versions: tuple[tuple[str, str], ...]) -> StoredEvidenceV1:
        fps: dict[str, str] = {}
        heads: dict[str, int] = {}
        for s in stamps:
            fps[s.catalog_source] = s.compiler_input_fingerprint
            heads[s.catalog_source] = s.head_seq
        return StoredEvidenceV1(fingerprints=fps, head_seqs=heads, versions=versions)


@dataclass(frozen=True, slots=True)
class CurrentEvidenceV1:
    fingerprints: Mapping[str, str | None]   # None = unreadable for that catalog
    head_seqs: Mapping[str, int | None]
    checkpoint: int
    versions: tuple[tuple[str, str], ...]


def read_current_evidence(conn, stored: StoredEvidenceV1, roles: Iterable[str] = ()) -> CurrentEvidenceV1:
    """IMPURE: recompute, for each catalog the stored plan pinned, the current classifier-input
    fingerprint + head_seq under a single read of the overlay projection checkpoint."""
    roles = tuple(roles)
    checkpoint = _checkpoint_seq(conn, "overlay")
    fps: dict[str, str | None] = {}
    heads: dict[str, int | None] = {}
    for cat in stored.fingerprints:
        try:
            cols = {c.object_ref: c for c in _load_columns(conn, cat, roles)}
            reals = derive_catalog_realizations(conn, cat).realizations
            mini = SimpleNamespace(columns_by_catalog={cat: cols}, realizations_by_catalog={cat: reals})
            fps[cat] = compiler_input_fingerprint(mini, cat)
            heads[cat] = drift_head_seq(conn, cat)
        except Exception:            # a catalog dropped / unreadable since compile -> unverifiable
            fps[cat] = None
            heads[cat] = None
    return CurrentEvidenceV1(fingerprints=fps, head_seqs=heads, checkpoint=checkpoint,
                             versions=_current_versions())


def compare(stored: StoredEvidenceV1, current: CurrentEvidenceV1) -> ReplayFreshness:
    """PURE. Precedence: incompatible (version) > unverifiable (missing/lagging) > drifted > current."""
    if tuple(stored.versions) != tuple(current.versions):
        return ReplayFreshness.incompatible
    if not stored.fingerprints:
        return ReplayFreshness.unverifiable
    drifted = False
    for cat, stored_fp in stored.fingerprints.items():
        cur_fp = current.fingerprints.get(cat)
        cur_head = current.head_seqs.get(cat)
        if not stored_fp or cur_fp is None or cur_head is None:
            return ReplayFreshness.unverifiable        # incomplete stamp or unreadable current state
        # projection LAG invariant (not equality): the projection must have caught up to this catalog's
        # current head — an unrelated global checkpoint advance is NOT drift.
        if current.checkpoint < cur_head:
            return ReplayFreshness.unverifiable
        if cur_fp != stored_fp or cur_head != stored.head_seqs.get(cat):
            drifted = True
    return ReplayFreshness.drifted if drifted else ReplayFreshness.current


def replay_freshness(conn, stored: StoredEvidenceV1, roles: Iterable[str] = ()) -> ReplayFreshness:
    return compare(stored, read_current_evidence(conn, stored, roles))
