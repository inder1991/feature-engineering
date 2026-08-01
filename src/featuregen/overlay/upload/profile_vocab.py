"""Release-A profile vocabularies (profile plan §6.1, interface doc D5/D8).

Three closed classification vocabularies for the effective dataset semantic profile, plus the
module-local ``UNRESOLVED_REASONS`` vocabulary and the ``FEATUREGEN_DATASET_PROFILES`` flag reader.

ONE normalizer per vocabulary (§6.1): :func:`data_role_from_table_role` ADAPTS the existing
``table_vocab.normalize_table_role`` — it never re-normalizes a raw role itself, so there is no
competing table-role normalization path. Legacy canonical ``table_role='bridge'`` DISPLAYS as
``DataRole.CROSSWALK`` without any evidence rewrite; the *input* alias ``crosswalk`` lives in
``table_vocab._ROLE_ALIASES`` (NOT in ``TABLE_ROLE_ENUM`` — that list is interpolated into the
Pass-B prompt, and extending it would re-version the prompt; review D Release A).

``UNRESOLVED_REASONS`` (D5): a closed enum in which EVERY member maps to exactly one of the three
product families ``{undecided, needs_data_check, structurally_unsuitable}`` — the UI renders the
family, never a failure-shaped free string. "No evidence at all" is ``undecided:no_evidence``,
which is DISTINCT from ``influence_not_operational``: a RECOMMENDATION-ceiling field showing a
display value is in its NORMAL state and carries NO unresolved reason at all.

TODO(semantic-merge): ``UNRESOLVED_REASONS`` is owned by the parallel semantic stream's
``overlay/upload/semantic_context.py`` (D5). When that module lands, replace the enum + family map
below with ``from featuregen.overlay.upload.semantic_context import UNRESOLVED_REASONS`` — this
module is the single adapter point; no other module may define these values.
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


# ---------------------------------------------------------------------------------------------
# UNRESOLVED_REASONS (D5) — module-local closed vocabulary, three families, no fourth.
# ---------------------------------------------------------------------------------------------


class UnresolvedReasonFamily(str, Enum):
    """The three product families (D5; the no-"blocked" rule). The UI renders THESE — "nobody
    decided yet" / "needs a data check" / "structurally unsuitable" — never a raw resolver string,
    and never a fourth family."""

    UNDECIDED = "undecided"
    NEEDS_DATA_CHECK = "needs_data_check"
    STRUCTURALLY_UNSUITABLE = "structurally_unsuitable"


class UnresolvedReason(str, Enum):
    """WHY a profile field has no load-bearing value (D5). Closed; every member maps to exactly
    one :class:`UnresolvedReasonFamily` in :data:`UNRESOLVED_REASONS`.

    ``influence_not_operational`` is deliberately NOT a member: a RECOMMENDATION-ceiling field in
    its display state is NORMAL, not unresolved (§6.3 as amended)."""

    NO_EVIDENCE = "no_evidence"                    # nobody said anything at all
    AUTHORITY_INSUFFICIENT = "authority_insufficient"  # said, but below the operational bar
    CONFLICT = "conflict"                          # active evidence disagrees
    PENDING_REVALIDATION = "pending_revalidation"  # material changed; awaiting re-confirmation
    # Reserved for the deterministic-contradiction path (Task 4: e.g. SCD2 claimed with no
    # candidate boundary columns). No Release-A Task-1/2/3 producer emits it yet; the member
    # exists so the family is representable from day one and the vocabulary stays closed.
    STRUCTURALLY_UNSUITABLE = "structurally_unsuitable"


#: member -> family (the D5 three-family rule). Every member appears exactly once.
UNRESOLVED_REASONS: dict[UnresolvedReason, UnresolvedReasonFamily] = {
    UnresolvedReason.NO_EVIDENCE: UnresolvedReasonFamily.UNDECIDED,
    UnresolvedReason.AUTHORITY_INSUFFICIENT: UnresolvedReasonFamily.UNDECIDED,
    UnresolvedReason.CONFLICT: UnresolvedReasonFamily.NEEDS_DATA_CHECK,
    UnresolvedReason.PENDING_REVALIDATION: UnresolvedReasonFamily.NEEDS_DATA_CHECK,
    UnresolvedReason.STRUCTURALLY_UNSUITABLE: UnresolvedReasonFamily.STRUCTURALLY_UNSUITABLE,
}


def unresolved_family(reason: UnresolvedReason | str) -> UnresolvedReasonFamily:
    """The family for ``reason`` (raising ``KeyError``/``ValueError`` on an unknown member — the
    vocabulary is closed; an unmapped reason is a programming error, not a display case)."""
    return UNRESOLVED_REASONS[UnresolvedReason(reason)]
