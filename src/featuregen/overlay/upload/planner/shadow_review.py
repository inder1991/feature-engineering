"""Phase-3B.4 — the Gate-2b review artifact (the human-labelled population review).

Gate 2 splits into a MACHINE half (Layer-A map exhaustiveness — `cause.assert_map_exhaustive`) and a
HUMAN half: an expert labels every DISTINCT observed `(reason, evidence_shape)` with a Layer-B
`ResolutionCause`. The unlabelled durable population cannot prove "zero classifier defects" — only this
signed, versioned, deduplicated artifact can. D8 verifies that every observed shape is labelled here and
that no label is `classifier_defect`/`unknown`/`operationally_unmeasured`, and checks the detached signature.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.upload.planner.cause import CATEGORY_MAP_VERSION, ResolutionCause
from featuregen.overlay.upload.planner.contracts import ReasonCode


def shape_key(reason: ReasonCode, evidence_shape: str) -> str:
    """The dedup unit: a reason + a coarse evidence SHAPE (not a per-row detail), so an expert labels a
    bounded set of distinct shapes rather than every observation."""
    return f"{reason.value}::{evidence_shape}"


@dataclass(frozen=True, slots=True)
class ReviewEntryV1:
    reason: ReasonCode
    evidence_shape: str
    label: ResolutionCause
    note: str = ""

    @property
    def key(self) -> str:
        return shape_key(self.reason, self.evidence_shape)


@dataclass(frozen=True, slots=True)
class ReviewArtifactV1:
    entries: tuple[ReviewEntryV1, ...]
    reviewer: str
    category_map_version: str
    content_hash: str
    signature: str | None = None       # a DETACHED signature, filled by the D8 signer (ed25519)

    @property
    def labelled_keys(self) -> frozenset[str]:
        return frozenset(e.key for e in self.entries)

    @property
    def defect_keys(self) -> tuple[str, ...]:
        return tuple(sorted(e.key for e in self.entries if e.label is ResolutionCause.classifier_defect))

    @property
    def unlabelled_or_unknown(self) -> tuple[str, ...]:
        return tuple(sorted(e.key for e in self.entries
                            if e.label in (ResolutionCause.unknown, ResolutionCause.operationally_unmeasured)))


def _content_hash(entries: Iterable[ReviewEntryV1], reviewer: str, version: str) -> str:
    material = {
        "reviewer": reviewer, "category_map_version": version,
        "entries": sorted([e.reason.value, e.evidence_shape, e.label.value, e.note] for e in entries),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_review_artifact(entries: Iterable[ReviewEntryV1], *, reviewer: str) -> ReviewArtifactV1:
    entries = tuple(entries)
    return ReviewArtifactV1(entries=entries, reviewer=reviewer,
                            category_map_version=CATEGORY_MAP_VERSION,
                            content_hash=_content_hash(entries, reviewer, CATEGORY_MAP_VERSION))


def missing_shape_labels(observed: Iterable[str], artifact: ReviewArtifactV1) -> tuple[str, ...]:
    """The distinct observed shapes with NO review entry — Gate 2b requires this to be empty."""
    labelled = artifact.labelled_keys
    return tuple(sorted(k for k in set(observed) if k not in labelled))


def review_gate_clean(observed_shapes: Iterable[str], artifact: ReviewArtifactV1) -> bool:
    """Gate 2b (human): every observed distinct shape is labelled, and no label is a defect/unknown/
    operationally_unmeasured. (The Layer-A exhaustiveness half is checked separately by the machine.)"""
    return (not missing_shape_labels(observed_shapes, artifact)
            and not artifact.defect_keys
            and not artifact.unlabelled_or_unknown)
