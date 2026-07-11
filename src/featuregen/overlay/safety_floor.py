from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn

# Sensitivity is a most-restrictive FLOOR, ordered least → most restrictive. Evidence may only RAISE
# a fact toward `prohibited`; taking it BELOW its floor requires a governed SafetyOverride (§7). Any
# label NOT in this tuple is UNKNOWN and fails closed to `prohibited` (the top rank) — an
# unrecognized value can never rank below the floor and is never persisted/returned verbatim.
SENSITIVITY_ORDER: tuple[str, ...] = (
    "public",
    "internal",
    "confidential",
    "restricted",
    "prohibited",
)

_PROHIBITED = "prohibited"


class GovernanceAuthority(StrEnum):
    """WHO may approve a governed decision. Only PRIVACY and SECURITY may authorize a below-floor
    sensitivity DOWNGRADE (see ``DOWNGRADE_AUTHORITIES``); DATA_OWNER and MODEL_RISK may not — a
    data owner cannot relax the safety floor on their own asset."""

    DATA_OWNER = "data_owner"
    SECURITY = "security"
    PRIVACY = "privacy"
    MODEL_RISK = "model_risk"


# Only these authorities may take a fact BELOW its sensitivity floor. Kept deliberately narrow:
# DATA_OWNER is intentionally EXCLUDED (fail-closed — an owner cannot downgrade their own asset's
# safety floor), as is MODEL_RISK.
DOWNGRADE_AUTHORITIES: frozenset[GovernanceAuthority] = frozenset(
    {GovernanceAuthority.PRIVACY, GovernanceAuthority.SECURITY}
)


@dataclass(frozen=True, slots=True)
class SafetyOverride:
    """A governed authorization to take a fact's ``field`` BELOW ``previous_floor`` to
    ``override_value``. It carries a specific approving authority, a rationale, a policy reference,
    and a bounded effective window (``None`` bounds are open). It is NOT a generic confirmation and
    is distinct from the compliance-gated free-text ``policy_tag`` basis."""

    fact_key: str
    field: str
    previous_floor: str
    override_value: str
    approved_by_authority: GovernanceAuthority
    rationale: str
    policy_reference: str
    effective_from: datetime | None
    effective_until: datetime | None


@dataclass(frozen=True, slots=True)
class SafetyOverrideRecord:
    """The immutable persisted row for a recorded downgrade (mirrors ``read_evidence``'s record
    shape): the minted ``override_id``, the governed ``override``, and who recorded it when."""

    override_id: str
    override: SafetyOverride
    created_by: dict
    created_at: object


def _rank(value: str) -> int:
    """Severity rank of a sensitivity label. Unknown labels FAIL CLOSED to `prohibited` (the top
    rank), so an unrecognized value can never rank below the floor."""
    try:
        return SENSITIVITY_ORDER.index(value)
    except ValueError:
        return SENSITIVITY_ORDER.index(_PROHIBITED)


def _normalize(value: str) -> str:
    """Map any value not in SENSITIVITY_ORDER to `prohibited` (fail-closed). The effective value is
    NEVER an unknown string — we do not persist or return a label we cannot rank."""
    return value if value in SENSITIVITY_ORDER else _PROHIBITED


def _override_is_effective(override: SafetyOverride, now: datetime) -> bool:
    """True iff `now` lies within the override's `[effective_from, effective_until]` window, treating
    each `None` bound as open. A not-yet-started or expired override is NOT effective (fail-closed)."""
    if override.effective_from is not None and now < override.effective_from:
        return False
    if override.effective_until is not None and now > override.effective_until:
        return False
    return True


def _override_permits_downgrade(
    override: SafetyOverride | None, *, floor: str, force_to: str, now: datetime
) -> bool:
    """A below-floor downgrade is permitted ONLY by an override that (a) governs the sensitivity
    field, (b) references exactly this floor and target value, (c) is approved by a
    downgrade-capable authority, and (d) is currently effective. Any mismatch → denied."""
    return (
        override is not None
        and override.field == "sensitivity"
        and override.previous_floor == floor
        and override.override_value == force_to
        and override.approved_by_authority in DOWNGRADE_AUTHORITIES
        and _override_is_effective(override, now)
    )


def apply_sensitivity_floor(
    floor: str,
    proposals: Sequence[str],
    *,
    override: SafetyOverride | None = None,
    force_to: str | None = None,
    now: datetime | None = None,
) -> str:
    """Resolve the effective sensitivity given a governed FLOOR, evidence `proposals`, and an
    optional explicit `force_to`.

    Rules (§7):
      * Evidence can only RAISE: the effective value is `max([floor, *proposals])` by severity rank.
      * Unknown labels fail closed to `prohibited` and are never returned verbatim.
      * A `force_to` at or above the effective floor is a RAISE — always permitted, no override.
      * A below-floor `force_to` (rank < effective) is PERMITTED ONLY with a currently-effective
        SafetyOverride whose `field=="sensitivity"`, `previous_floor==floor`, `override_value==force_to`,
        and `approved_by_authority in DOWNGRADE_AUTHORITIES`. Otherwise raise ``PermissionError``.
    """
    now = now or datetime.now(UTC)
    effective = _normalize(max([floor, *proposals], key=_rank))
    if force_to is None:
        return effective
    force_norm = _normalize(force_to)
    if _rank(force_norm) >= _rank(effective):
        # A raise (or a no-op) — always safe. Return the higher of the two (which is force_norm).
        return force_norm
    # Below the effective floor: a governed downgrade. Fail closed unless a valid override permits it.
    if not _override_permits_downgrade(override, floor=floor, force_to=force_to, now=now):
        raise PermissionError(
            f"below-floor sensitivity downgrade to {force_to!r} (effective floor {effective!r}) "
            f"requires a currently-effective SafetyOverride approved by one of "
            f"{sorted(a.value for a in DOWNGRADE_AUTHORITIES)}"
        )
    return force_norm


def record_safety_override(
    conn: DbConn,
    *,
    fact_key: str,
    override: SafetyOverride,
    created_by: Mapping[str, Any],
) -> str:
    """Persist one governed below-floor downgrade as an immutable `safety_override` row and return
    its minted `sfo_` id. Write-once: each call mints a fresh id and INSERTs — there is no update
    path (the migration installs a no-mutation trigger). `created_by` is a Mapping persisted as
    jsonb — callers pass `identity_to_jsonb(actor)`, never a raw IdentityEnvelope.

    The `fact_key` param must equal `override.fact_key`: persisting the param while ignoring the
    object's field would silently drop it on round-trip, so a divergent pair fails closed."""
    if fact_key != override.fact_key:
        raise ValueError(f"fact_key {fact_key!r} != override.fact_key {override.fact_key!r}")
    override_id = mint_id("sfo")
    conn.execute(
        """
        INSERT INTO safety_override
            (override_id, fact_key, field, previous_floor, override_value,
             approved_by_authority, rationale, policy_reference,
             effective_from, effective_until, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            override_id,
            fact_key,
            override.field,
            override.previous_floor,
            override.override_value,
            override.approved_by_authority.value,
            override.rationale,
            override.policy_reference,
            override.effective_from,
            override.effective_until,
            Jsonb(dict(created_by)),
        ),
    )
    return override_id


def read_safety_override(conn: DbConn, override_id: str) -> SafetyOverrideRecord:
    """Resolve an `override_id` to its immutable record. Raises KeyError if unknown. The persisted
    `fact_key` column is authoritative for the reconstructed override."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM safety_override WHERE override_id = %s", (override_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown override_id {override_id!r}")
    override = SafetyOverride(
        fact_key=row["fact_key"],
        field=row["field"],
        previous_floor=row["previous_floor"],
        override_value=row["override_value"],
        approved_by_authority=GovernanceAuthority(row["approved_by_authority"]),
        rationale=row["rationale"],
        policy_reference=row["policy_reference"],
        effective_from=row["effective_from"],
        effective_until=row["effective_until"],
    )
    return SafetyOverrideRecord(
        override_id=row["override_id"],
        override=override,
        created_by=row["created_by"],
        created_at=row["created_at"],
    )
