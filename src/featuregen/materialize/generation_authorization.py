"""S11 — the generation authorization: what a generation is authorized FOR (invariant 17).

**This closes a gap rather than tidying one.** ``verification_attempt`` is keyed on
``generation_authorization_revision_id``, ``evaluate_generate`` takes one and refuses a blank, and
invariant 17 says a generation is authorized FOR a target. Every one of those treated the id as
given, and until now nothing anywhere produced one — so a verification could name an authorization
that never existed and nothing could tell.

**The target travels with the selection, and its MODE decides whether there is one.** A generation
authorized for a ``PREDICTION`` target and one authorized for an ``EXPLORATION`` build are different
authorizations over the same group: the first has a column that must not leak into its own features,
the second has none. ``target_ref`` is therefore ``None`` exactly when the mode is ``EXPLORATION`` —
enforced here and by a database CHECK, because two fields that can disagree eventually do.

**Content-addressed, so a double-click is one authorization.** Re-authorizing the same generation
over the same target in the same environment produces the same revision id; who authorized it and
when are provenance and sit outside the hash, the same split 1074 made for inventory observations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.contracts.db import DbConn
from featuregen.overlay.upload.selection_revisions import TargetModeV1

__all__ = [
    "GenerationAuthorizationV1",
    "load_generation_authorization",
    "record_generation_authorization",
]


@dataclass(frozen=True, slots=True)
class GenerationAuthorizationV1:
    """One authorization: which build set, in which environment, for which target."""

    environment_id: str
    logical_group_name: str
    build_set_revision_id: str
    target_mode: TargetModeV1
    target_ref: str | None

    def __post_init__(self) -> None:
        for value, what in (
            (self.environment_id, "environment_id"),
            (self.logical_group_name, "logical_group_name"),
            (self.build_set_revision_id, "build_set_revision_id"),
        ):
            if not value.strip():
                raise ValueError(
                    f"a generation authorization with a blank {what} authorizes a generation "
                    f"nobody can locate")
        has_target = self.target_ref is not None and self.target_ref.strip() != ""
        if (self.target_mode is TargetModeV1.PREDICTION) != has_target:
            raise ValueError(
                f"target_mode={self.target_mode.value} and target_ref={self.target_ref!r} disagree: "
                f"an exploration build HAS no target — that is what the mode means — and a "
                f"prediction without one would be authorized to predict something nobody named")

    def identity_payload(self) -> dict[str, Any]:
        """What this authorization IS. No actor, no timestamp — those are provenance."""
        return {
            "environment_id": self.environment_id,
            "logical_group_name": self.logical_group_name,
            "build_set_revision_id": self.build_set_revision_id,
            "target_mode": self.target_mode.value,
            "target_ref": self.target_ref,
        }

    @property
    def revision_id(self) -> str:
        return jcs_sha256(self.identity_payload())


def record_generation_authorization(
    conn: DbConn, authorization: GenerationAuthorizationV1, *, authorized_by: str,
    authorized_at: str,
) -> str:
    """Append an authorization and return its revision id. Idempotent on content.

    Raises:
        ValueError: nobody is recorded as authorizing it. A generation is authorized BY someone —
            that is what makes it an authorization rather than an observation.
    """
    if not authorized_by.strip():
        raise ValueError(
            "a generation authorization must record who authorized it: it is the act of a person "
            "taking responsibility for a target, and one with no actor is an observation")

    conn.execute(
        "INSERT INTO generation_authorization (revision_id, environment_id, logical_group_name, "
        "build_set_revision_id, target_mode, target_ref, authorized_by, authorized_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (revision_id) DO NOTHING",
        (authorization.revision_id, authorization.environment_id,
         authorization.logical_group_name, authorization.build_set_revision_id,
         authorization.target_mode.value, authorization.target_ref, authorized_by.strip(),
         authorized_at))
    return authorization.revision_id


def load_generation_authorization(
    conn: DbConn, revision_id: str,
) -> GenerationAuthorizationV1 | None:
    """One authorization, reconstructed and identity-verified.

    A row that cannot reproduce the id it is filed under is store corruption: it would authorize a
    generation for a target it no longer names.
    """
    row = conn.execute(
        "SELECT environment_id, logical_group_name, build_set_revision_id, target_mode, "
        "target_ref FROM generation_authorization WHERE revision_id = %s",
        (revision_id,)).fetchone()
    if row is None:
        return None
    authorization = GenerationAuthorizationV1(
        environment_id=row[0], logical_group_name=row[1], build_set_revision_id=row[2],
        target_mode=TargetModeV1(row[3]), target_ref=row[4])
    if authorization.revision_id != revision_id:
        raise ValueError(
            f"generation authorization {revision_id} does not reproduce its own id: it would "
            f"authorize a generation for a target it no longer names")
    return authorization
