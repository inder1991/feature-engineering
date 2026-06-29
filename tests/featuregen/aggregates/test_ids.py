from featuregen.aggregates.ids import (
    mint_id,
    new_command_id,
    new_consumer_id,
    new_feature_id,
    new_feature_version_id,
    new_request_id,
    new_run_id,
    normalize_concept_key,
)


def test_prefixes():
    assert new_request_id().startswith("req_")
    assert new_feature_id().startswith("feat_")
    assert new_run_id().startswith("run_")
    assert new_feature_version_id().startswith("fv_")
    assert new_consumer_id().startswith("con_")
    assert new_command_id().startswith("cmd_")


def test_ulid_shape_and_uniqueness():
    ids = {mint_id("x") for _ in range(5000)}
    assert len(ids) == 5000
    body = mint_id("x").split("_", 1)[1]
    assert len(body) == 26
    assert set(body) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ids_are_lexicographically_time_sortable():
    import time

    a = mint_id("x")
    time.sleep(0.002)
    b = mint_id("x")
    assert a < b


def test_normalize_concept_key():
    assert normalize_concept_key("  Salary  Irregularity! ") == "salary-irregularity"
    assert normalize_concept_key("Salary irregularity") == normalize_concept_key(
        "salary IRREGULARITY"
    )
    assert normalize_concept_key("churn_risk (v2)") == "churn-risk-v2"
