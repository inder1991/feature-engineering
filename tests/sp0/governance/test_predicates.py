from datetime import datetime, timezone

import pytest

from sp0.governance.predicates import (
    GOVERNANCE_PREDICATES,
    approval_not_expired,
    approval_type_is,
    max_uses_not_exceeded,
    register_governance_predicates,
    required_artifact_present,
    risk_tier_within_ceiling,
    use_case_not_blocked,
    verification_stamp_satisfies,
)


def test_verification_stamp_ordering_uses_sp0_normative_rank():
    assert verification_stamp_satisfies(
        {"verification_stamp": "USEFULNESS-CHECKED", "required_stamp": "USEFULNESS-CHECKED"}) is True
    assert verification_stamp_satisfies(
        {"verification_stamp": "DATA-CHECKED", "required_stamp": "USEFULNESS-CHECKED"}) is False
    assert verification_stamp_satisfies(
        {"verification_stamp": "USEFULNESS-CHECKED", "required_stamp": "DATA-CHECKED"}) is True


def test_use_case_block_and_artifact_presence_and_type_and_tier():
    assert use_case_not_blocked({"target_use_case": "fraud", "blocked_use_cases": ("credit_decisioning",)}) is True
    assert use_case_not_blocked({"target_use_case": "credit_decisioning", "blocked_use_cases": ("credit_decisioning",)}) is False
    assert required_artifact_present({"required_artifact_refs": {"monitoring_spec": "doc_m"}, "artifact_name": "monitoring_spec"}) is True
    assert required_artifact_present({"required_artifact_refs": {"monitoring_spec": None}, "artifact_name": "monitoring_spec"}) is False
    assert required_artifact_present({"required_artifact_refs": {}, "artifact_name": "monitoring_spec"}) is False
    assert approval_type_is({"approval_type": "PRODUCTION", "expected_approval_type": "PRODUCTION"}) is True
    assert risk_tier_within_ceiling({"risk_tier_rank": 2, "ceiling_rank": 2}) is True
    assert risk_tier_within_ceiling({"risk_tier_rank": 3, "ceiling_rank": 2}) is False


def test_expiry_and_max_uses_are_deterministic_in_supplied_inputs():
    now = datetime(2026, 6, 27, tzinfo=timezone.utc)
    future = datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert approval_not_expired({"expires_at": None, "as_of": now}) is True
    assert approval_not_expired({"expires_at": future, "as_of": now}) is True
    assert approval_not_expired({"expires_at": now, "as_of": future}) is False
    assert max_uses_not_exceeded({"max_uses": None, "uses_count": 99}) is True
    assert max_uses_not_exceeded({"max_uses": 3, "uses_count": 2}) is True
    assert max_uses_not_exceeded({"max_uses": 3, "uses_count": 3}) is False


def test_predicates_declare_only_the_inputs_they_read():
    assert verification_stamp_satisfies.declared_inputs == ("verification_stamp", "required_stamp")
    for predicate in GOVERNANCE_PREDICATES:
        assert isinstance(predicate.name, str) and predicate.name
        assert isinstance(predicate.declared_inputs, tuple)


def test_register_governance_predicates_registers_all_seven():
    class FakeRegistry:
        def __init__(self):
            self.registered = {}

        def register(self, predicate):
            if predicate.name in self.registered:
                raise AssertionError("re-registration is a load-time error")
            self.registered[predicate.name] = predicate

        def get(self, name):
            return self.registered[name]

        def evaluate(self, guard_expr, inputs):  # pragma: no cover - unused
            raise NotImplementedError

    reg = FakeRegistry()
    register_governance_predicates(reg)
    assert set(reg.registered) == {
        "verification_stamp_satisfies", "approval_type_is", "use_case_not_blocked",
        "required_artifact_present", "risk_tier_within_ceiling", "approval_not_expired",
        "max_uses_not_exceeded",
    }
