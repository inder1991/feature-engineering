from datetime import timedelta

import pytest

from featuregen.overlay.upload.bridge_admission import BridgeAdmissionPolicyV1
from featuregen.overlay.upload.bridge_assessment import PopulationRelation


def test_policy_identity_includes_load_bearing_thresholds() -> None:
    original = BridgeAdmissionPolicyV1()
    stricter = BridgeAdmissionPolicyV1(
        maximum_evidence_age=timedelta(days=7))
    assert original.policy_version != stricter.policy_version
    assert (
        original.threshold_for(PopulationRelation.UNKNOWN)
        == original.conservative_unknown_population
    )


def test_policy_rejects_noncanonical_thresholds() -> None:
    with pytest.raises(ValueError, match="positive"):
        BridgeAdmissionPolicyV1(minimum_observed_rows_per_endpoint=0)
    with pytest.raises(ValueError, match="duplicates"):
        BridgeAdmissionPolicyV1(
            allowed_normalization_ids=("identity_v1", "identity_v1"))
