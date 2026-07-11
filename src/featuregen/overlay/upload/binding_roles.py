"""Phase-3B.1 — the governed vocabularies for a recipe need's cross-catalog binding role.

Leaf module (no deps) so ``templates.Need`` can type its fields on these without an import cycle. The
role of a need is GOVERNED metadata, never inferred from a column name or a need's tuple position."""
from __future__ import annotations

from enum import StrEnum


class JoinRole(StrEnum):
    """What a need contributes to a cross-catalog join. ``SOURCE_ENTITY_KEY`` fixes the recipe's source
    grain; ``TARGET_ENTITY_KEY`` is the grain the plan rolls up to (the confirmed scope's target — not a
    need at authoring time, reserved); ``INTERMEDIATE_ENTITY_KEY`` is a hop key from an intermediate
    catalog; ``MEASURE`` is a value carried/aggregated to the target grain; ``TIME`` is a timestamp for
    the window / as-of."""

    SOURCE_ENTITY_KEY = "source_entity_key"
    TARGET_ENTITY_KEY = "target_entity_key"
    INTERMEDIATE_ENTITY_KEY = "intermediate_entity_key"
    MEASURE = "measure"
    TIME = "time"


class TemporalRole(StrEnum):
    """A need's temporal semantics, derived from the concept's governed ``pit_role``. ``VALID_TO`` has no
    current ``pit_role`` source (reserved for a future bitemporal concept)."""

    NONE = "none"
    EVENT_TIME = "event_time"
    AS_OF_TIME = "as_of_time"
    INGESTION_TIME = "ingestion_time"
    VALID_FROM = "valid_from"
    VALID_TO = "valid_to"
