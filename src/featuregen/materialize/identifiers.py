"""How a feature NAME becomes a Hive column — the one normalizer, and its plan error.

**Extracted because six modules already depend on it and none of them is about V1.**
`group_plan`, `binding`, `identity`, `queue_lane`, `render.nodes_compute`,
`overlay.upload.authoring_work_item_store` and `overlay.upload.group_name_allocator` all import
`hive_identifier` or `FeatureNamePlanError` from `materialize.admission` — a module whose own
`AdmittedFeature` carries a `TypedFormulaV1`. Naming a column has nothing to do with which formula
language produced it.

`hive_identifier`'s own docstring already made the argument: it is PUBLIC because the group plan
names the same columns and must reach the SAME answer, since "a second normalizer would be a second
chance to disagree about which column a feature occupies". Leaving the one normalizer inside the V1
admission module meant every shared consumer had to import from V1 to agree with it.

**Moved verbatim.** This function decides which physical column a published feature occupies, and it
is idempotent by design so that re-applying it to an already-admitted `feature_name` is a
validation. Changing what it folds would move published columns.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ["FeatureNamePlanError", "hive_identifier"]

_HIVE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_NON_HIVE_CHARS = re.compile(r"[^a-z0-9_]")


class FeatureNamePlanError(Exception):
    """A feature NAME the plan cannot express as a distinct Hive column — a PLAN error.

    Deliberately NOT a :class:`~featuregen.materialize.codes.MaterializationRefused`, and
    deliberately not one of the §14 codes: the closed vocabulary has no member for it because it is
    not a governed verdict about an artifact. It is the same class of failure as
    ``canonical.materialize_hash``'s ``TypeError`` on a non-mapping — a defect at the call site,
    where the caller assembled the batch. Spec §1.2 says so in as many words: *"a post-normalization
    collision within a group is a plan error, never a silent overwrite."* Raising it loudly is the
    point; the alternative is two features quietly writing one column.
    """


def hive_identifier(name: str) -> str:
    """``intent.name`` folded to a Hive identifier — the physical column the feature will occupy.

    Deterministic and conservative: NFKC-normalize, strip, lower-case, and map every character Hive
    does not accept in an unquoted identifier to ``_``. Nothing is collapsed or truncated, because
    both would map two distinct names onto one column — the very thing the collision check exists to
    prevent. A name that cannot be expressed at all (empty, not starting with a letter, longer than
    Hive's 128-character bound) is a plan error, not a name to invent a mangling for.

    PUBLIC because the group plan (§9/§10.2) names the same columns and must reach the SAME answer:
    a second normalizer would be a second chance to disagree about which column a feature occupies,
    and the disagreement would surface as a schema gate failing on a name nobody chose. It is
    idempotent, so re-applying it to an already-admitted ``feature_name`` is a validation."""
    folded = _NON_HIVE_CHARS.sub("_", unicodedata.normalize("NFKC", name).strip().lower())
    if not _HIVE_IDENTIFIER.fullmatch(folded):
        raise FeatureNamePlanError(
            f"feature name {name!r} does not normalize to a Hive identifier "
            f"(got {folded!r}: it must start with a letter and be at most 128 characters of "
            "[a-z0-9_])"
        )
    return folded
