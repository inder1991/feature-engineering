"""Release-A profile vocabularies (profile plan §6.1, interface doc D5/D8).

Three closed classification vocabularies for the effective dataset semantic profile, plus the
``FEATUREGEN_DATASET_PROFILES`` flag reader.

ONE normalizer per vocabulary (§6.1): :func:`data_role_from_table_role` ADAPTS the existing
``table_vocab.normalize_table_role`` — it never re-normalizes a raw role itself, so there is no
competing table-role normalization path. Legacy canonical ``table_role='bridge'`` DISPLAYS as
``DataRole.CROSSWALK`` without any evidence rewrite; the *input* alias ``crosswalk`` lives in
``table_vocab._ROLE_ALIASES`` (NOT in ``TABLE_ROLE_ENUM`` — that list is interpolated into the
Pass-B prompt, and extending it would re-version the prompt; review D Release A).

``UNRESOLVED_REASONS`` is NOT here. It is owned by ``overlay/upload/semantic_context.py`` (D5 —
the canonical home), and consumers import it from there directly: one closed vocabulary, one
spelling per member, no re-export layer to drift against. Every member maps to exactly one of the
three product families ``{undecided, needs_data_check, structurally_unsuitable}`` — the UI renders
the family, never a failure-shaped free string. "No evidence at all" is ``undecided:no_evidence``,
DISTINCT from ``influence_not_operational``: a RECOMMENDATION-ceiling field showing a display value
is in its NORMAL state and carries NO unresolved reason at all.
"""
from __future__ import annotations

import os
from enum import Enum

from featuregen.overlay.upload.table_vocab import normalize_table_role

# ---------------------------------------------------------------------------------------------
# Flag (interface doc D8): default OFF; the widened truthy set (feature_assist.py pattern).
# ---------------------------------------------------------------------------------------------

DATASET_PROFILES_FLAG = "FEATUREGEN_DATASET_PROFILES"


def dataset_profiles_enabled() -> bool:
    """The single env gate for every Release-A profile surface (routes, upload part, listing
    extension). Default OFF ⟹ all new API surfaces 404/hidden, the upload part is ignored with a
    warning, and every existing payload is byte-identical. Reads the widened truthy set
    ``{"1","true","yes","on"}`` (D8; the ``feature_context_enabled`` pattern)."""
    return os.environ.get(DATASET_PROFILES_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------------------------
# §6.1 vocabularies
# ---------------------------------------------------------------------------------------------


class DataRole(str, Enum):
    """What KIND of dataset a table is (§6.1) — a DERIVED display classification over the existing
    canonical ``table_role`` evidence; never a second evidence field."""

    EVENT_FACT = "event_fact"
    SNAPSHOT_FACT = "snapshot_fact"
    FACT = "fact"                     # legacy canonical: a fact with no event/snapshot signal
    DIMENSION = "dimension"
    REFERENCE = "reference"
    CROSSWALK = "crosswalk"           # derived from existing canonical table_role="bridge"
    UNKNOWN = "unknown"


class AuthorityRole(str, Enum):
    """How authoritative a dataset COPY is (§6.1). Load-bearing only from source-attested /
    human-confirmed / deterministic-profiler evidence — see ``field_policies``."""

    SYSTEM_OF_RECORD = "system_of_record"
    MASTERED_VIEW = "mastered_view"
    AUTHORITATIVE_REPLICA = "authoritative_replica"
    NON_AUTHORITATIVE_REPLICA = "non_authoritative_replica"
    DERIVED = "derived"
    EXTERNAL_REFERENCE = "external_reference"
    UNKNOWN = "unknown"


class TemporalStorageModel(str, Enum):
    """How a dataset stores history (§6.1). Same load-bearing bar as :class:`AuthorityRole`."""

    CURRENT_ONLY = "current_only"
    SCD1 = "scd1"
    SCD2 = "scd2"
    SNAPSHOT = "snapshot"
    EVENT_LOG = "event_log"
    UNKNOWN = "unknown"


# Canonical table_role -> display DataRole. ``bridge`` -> CROSSWALK is the ONLY remap (correction 4:
# derive, don't duplicate; evidence keeps saying ``bridge``).
_TABLE_ROLE_TO_DATA_ROLE: dict[str, DataRole] = {
    "event_fact": DataRole.EVENT_FACT,
    "snapshot_fact": DataRole.SNAPSHOT_FACT,
    "fact": DataRole.FACT,
    "dimension": DataRole.DIMENSION,
    "reference": DataRole.REFERENCE,
    "bridge": DataRole.CROSSWALK,
}


def data_role_from_table_role(raw: str | None, *, event_or_snapshot: str | None = None) -> DataRole:
    """The display :class:`DataRole` for a (possibly raw) table role — ADAPTING
    ``table_vocab.normalize_table_role`` (the one normalizer, §6.1). Accepts either an
    already-canonical role (``bridge``) or a raw spelling (``dim`` / the ``crosswalk`` input
    alias); anything off-vocabulary resolves to :attr:`DataRole.UNKNOWN`, never an error."""
    canonical = normalize_table_role(raw, event_or_snapshot=event_or_snapshot)
    if canonical is None:
        return DataRole.UNKNOWN
    return _TABLE_ROLE_TO_DATA_ROLE[canonical]


def _normalize_member(raw: object, vocab: type[Enum]) -> str | None:
    """``strip().lower()``-normalize into ``vocab``; off-vocab (or non-string) -> ``None`` (the
    caller records the disposition / refuses) — the ``table_vocab`` normalizer convention."""
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    return v if v in {m.value for m in vocab} else None


def normalize_authority_role(raw: object) -> str | None:
    """Normalize a raw authority-role into the closed :class:`AuthorityRole` vocabulary."""
    return _normalize_member(raw, AuthorityRole)


def normalize_temporal_storage_model(raw: object) -> str | None:
    """Normalize a raw temporal-storage-model into :class:`TemporalStorageModel`."""
    return _normalize_member(raw, TemporalStorageModel)


def profile_vocabulary_fingerprint() -> str:
    """Canonical, ORDER-INSENSITIVE fingerprint of the three closed profile vocabularies.

    Folded into the Pass-B profile-synthesis replay identity (joint Task 4 item e) for the same
    reason the concept vocabulary is folded into the classifier's: a verdict reached against a
    DIFFERENT set of admissible answers must not replay as if nothing changed. Sorted member lists
    through the one ``canonical_hash``, so neither declaration order nor dict key order can churn
    identity."""
    from featuregen.overlay.field_evidence import canonical_hash
    return canonical_hash({
        "data_role": sorted(m.value for m in DataRole),
        "authority_role": sorted(m.value for m in AuthorityRole),
        "temporal_storage_model": sorted(m.value for m in TemporalStorageModel),
    })[:12]
