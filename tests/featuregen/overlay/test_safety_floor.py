from datetime import UTC, datetime

import pytest

from featuregen.overlay.safety_floor import (
    DOWNGRADE_AUTHORITIES,
    GovernanceAuthority,
    SafetyOverride,
    apply_sensitivity_floor,
    read_safety_override,
    record_safety_override,
)


def _ovr(val, auth=GovernanceAuthority.PRIVACY, until=None):
    return SafetyOverride(
        fact_key="fk",
        field="sensitivity",
        previous_floor="restricted",
        override_value=val,
        approved_by_authority=auth,
        rationale="tokenized",
        policy_reference="POL-1",
        effective_from=None,
        effective_until=until,
    )


def test_floor_holds_and_evidence_can_only_raise():
    assert apply_sensitivity_floor("restricted", ["public", "internal"]) == "restricted"
    assert apply_sensitivity_floor("internal", ["restricted"]) == "restricted"


def test_unknown_sensitivity_is_prohibited_not_persisted_verbatim():
    assert apply_sensitivity_floor("internal", ["top_secret"]) == "prohibited"


def test_unknown_floor_is_also_fail_closed_to_prohibited():
    # An unrecognized FLOOR must never persist verbatim either — it fails closed to the top rank.
    assert apply_sensitivity_floor("mystery", ["public"]) == "prohibited"


def test_below_floor_downgrade_requires_permitted_authority():
    with pytest.raises(PermissionError):
        apply_sensitivity_floor("restricted", ["public"], override=None, force_to="public")
    with pytest.raises(PermissionError):  # DATA_OWNER not permitted to downgrade
        apply_sensitivity_floor(
            "restricted",
            ["public"],
            override=_ovr("internal", GovernanceAuthority.DATA_OWNER),
            force_to="internal",
        )
    assert (
        apply_sensitivity_floor(
            "restricted", ["public"], override=_ovr("internal"), force_to="internal"
        )
        == "internal"
    )


def test_security_authority_may_also_downgrade():
    assert (
        apply_sensitivity_floor(
            "restricted",
            ["public"],
            override=_ovr("internal", GovernanceAuthority.SECURITY),
            force_to="internal",
        )
        == "internal"
    )


def test_override_must_match_field_floor_and_value():
    # override_value must equal force_to
    with pytest.raises(PermissionError):
        apply_sensitivity_floor(
            "restricted", ["public"], override=_ovr("public"), force_to="internal"
        )
    # previous_floor must equal the floor argument
    mismatched = SafetyOverride(
        fact_key="fk",
        field="sensitivity",
        previous_floor="confidential",
        override_value="internal",
        approved_by_authority=GovernanceAuthority.PRIVACY,
        rationale="x",
        policy_reference="P",
        effective_from=None,
        effective_until=None,
    )
    with pytest.raises(PermissionError):
        apply_sensitivity_floor(
            "restricted", ["public"], override=mismatched, force_to="internal"
        )
    # field must be "sensitivity"
    wrong_field = SafetyOverride(
        fact_key="fk",
        field="policy_tag",
        previous_floor="restricted",
        override_value="internal",
        approved_by_authority=GovernanceAuthority.PRIVACY,
        rationale="x",
        policy_reference="P",
        effective_from=None,
        effective_until=None,
    )
    with pytest.raises(PermissionError):
        apply_sensitivity_floor(
            "restricted", ["public"], override=wrong_field, force_to="internal"
        )


def test_expired_override_is_rejected():
    past = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(PermissionError):
        apply_sensitivity_floor(
            "restricted",
            ["public"],
            override=_ovr("internal", until=past),
            force_to="internal",
            now=datetime(2026, 7, 11, tzinfo=UTC),
        )


def test_not_yet_effective_override_is_rejected():
    future = datetime(2030, 1, 1, tzinfo=UTC)
    ovr = SafetyOverride(
        fact_key="fk",
        field="sensitivity",
        previous_floor="restricted",
        override_value="internal",
        approved_by_authority=GovernanceAuthority.PRIVACY,
        rationale="x",
        policy_reference="P",
        effective_from=future,
        effective_until=None,
    )
    with pytest.raises(PermissionError):
        apply_sensitivity_floor(
            "restricted",
            ["public"],
            override=ovr,
            force_to="internal",
            now=datetime(2026, 7, 11, tzinfo=UTC),
        )


def test_force_to_that_raises_is_permitted_without_override():
    # force_to at or above the effective floor is a RAISE, always safe, no override required.
    assert apply_sensitivity_floor("internal", ["public"], force_to="restricted") == "restricted"


def test_unknown_force_to_never_downgrades_and_is_not_verbatim():
    # An unknown force_to normalizes to prohibited (top rank), so it can never be a downgrade.
    assert apply_sensitivity_floor("internal", ["public"], force_to="top_secret") == "prohibited"


def test_downgrade_authorities_are_privacy_and_security_only():
    assert DOWNGRADE_AUTHORITIES == frozenset(
        {GovernanceAuthority.PRIVACY, GovernanceAuthority.SECURITY}
    )
    assert GovernanceAuthority.DATA_OWNER not in DOWNGRADE_AUTHORITIES
    assert GovernanceAuthority.MODEL_RISK not in DOWNGRADE_AUTHORITIES


def test_record_and_read_safety_override_round_trips(db):
    ovr = SafetyOverride(
        fact_key="fk-9",
        field="sensitivity",
        previous_floor="restricted",
        override_value="internal",
        approved_by_authority=GovernanceAuthority.PRIVACY,
        rationale="tokenized at rest",
        policy_reference="POL-42",
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_until=datetime(2027, 1, 1, tzinfo=UTC),
    )
    oid = record_safety_override(
        db, fact_key="fk-9", override=ovr, created_by={"subject": "privacy-officer"}
    )
    assert oid.startswith("sfo_")
    rec = read_safety_override(db, oid)
    assert rec.override_id == oid
    assert rec.override == ovr
    assert rec.created_by == {"subject": "privacy-officer"}


def test_read_unknown_override_raises_keyerror(db):
    with pytest.raises(KeyError):
        read_safety_override(db, "sfo_does_not_exist")


def test_record_safety_override_rejects_divergent_fact_key(db):
    # WBR fix 2: the passed fact_key must equal override.fact_key, else the object's field is
    # silently dropped on round-trip. A divergent pair must fail closed.
    ovr = _ovr("internal")  # override.fact_key == "fk"
    with pytest.raises(ValueError):
        record_safety_override(
            db, fact_key="different-key", override=ovr, created_by={"subject": "x"}
        )


def test_record_safety_override_matching_fact_key_round_trips(db):
    # WBR fix 2: a matching pair still round-trips cleanly (guard is equality, not a new constraint).
    ovr = _ovr("internal")  # override.fact_key == "fk"
    oid = record_safety_override(db, fact_key="fk", override=ovr, created_by={"subject": "x"})
    rec = read_safety_override(db, oid)
    assert rec.override.fact_key == "fk"
    assert rec.override == ovr
