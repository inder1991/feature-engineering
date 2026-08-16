"""C-B6/C-B7/C-B8 — bound input set, generation inventory, verification compatibility.

C-B7's two gates are the interesting ones and they pull in opposite directions: *"an identical
re-capture with a new observation id and capture time leaves identity unchanged"* and *"an unrelated
table added to the inventory leaves it unchanged"*. Together they say the identity must be narrower
than the observation, which is why the record stores everything and hashes a subset.

C-B8's gate is *"a comparison rule and a test per dimension, eight of eight"* — parametrised over
`EngineVersions`' own fields, so a ninth runtime cannot appear without a rule.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    EngineVersions,
    TableLayout,
)
from featuregen.overlay.upload.inventory_revisions import (
    RUNTIME_DIMENSIONS,
    BoundInputSetRevisionV2,
    BoundInputV2,
    CompatibilityRule,
    GenerationInventoryObservationV1,
    VerificationInventoryObservationV1,
    compare_inventories,
)

TXN = "hdfc::public.transactions.txn_amt"
ACCT = "hdfc::public.transactions.acct_id"


def _versions(**overrides) -> EngineVersions:
    kwargs = dict(hive="3.1.2", spark="3.3.0", metastore="3.1.2", python="3.11.14",
                  java="11.0.20", pyspark="3.3.0", kedro="0.19.3", kedro_datasets="2.1.0")
    kwargs.update(overrides)
    return EngineVersions(**kwargs)


def _layout(table: str, columns=("txn_amt", "acct_id")) -> TableLayout:
    return TableLayout(
        schema="public", table=table, partition_columns=(("load_dt", "string"),),
        partition_mapping=None, columns=tuple((c, "decimal(18,2)") for c in columns),
        location=f"hdfs://nn/warehouse/public.db/{table}", rewritten_in_place=False)


def _inventory(*, tables=None, versions=None, captured_at="2026-08-16T00:00:00Z",
               environment_id="hdfc-local", schema_map=None) -> ClusterInventoryV1:
    return ClusterInventoryV1(
        environment_id=environment_id,
        tables=tables if tables is not None else {"public.transactions": _layout("transactions")},
        logical_schema_map=schema_map if schema_map is not None else {
            "hdfc::public.transactions": "public"},
        engine_versions=versions or _versions(), captured_at=captured_at)


def _observation(**overrides) -> GenerationInventoryObservationV1:
    kwargs = dict(observation_id="obs-1", inventory=_inventory(),
                  used_logical_schema_refs=("hdfc::public.transactions",),
                  read_set=(TXN, ACCT))
    kwargs.update(overrides)
    return GenerationInventoryObservationV1(**kwargs)


# ══ C-B6 — the bound input set is POLICY-FREE ════════════════════════════════════════════════════
def test_a_bound_input_set_is_CONSTRUCTIBLE_WITHOUT_ANY_POLICY():
    """C-B6's gate. "Where does this ref live" has an answer before anyone asks which policies apply,
    and C-C7 consumes this rather than the reverse."""
    names = " ".join(f.name for f in dataclasses.fields(BoundInputSetRevisionV2))
    assert "polic" not in names
    bound = BoundInputSetRevisionV2(
        revision_id="bis-1", environment_id="hdfc-local",
        inputs=(BoundInputV2(TXN, "public.transactions", "txn_amt"),))
    assert bound.content_hash
    assert bound.datasets == ("public.transactions",)


def test_the_bound_set_names_its_ENVIRONMENT():
    """The same logical ref resolves to different physical datasets in different environments."""
    with pytest.raises(ValueError, match="different physical datasets in different environments"):
        BoundInputSetRevisionV2(revision_id="bis-1", environment_id=" ",
                                inputs=(BoundInputV2(TXN, "public.transactions", "txn_amt"),))


def test_binding_one_ref_twice_is_refused():
    with pytest.raises(ValueError, match="tuple-order accident"):
        BoundInputSetRevisionV2(
            revision_id="bis-1", environment_id="hdfc-local",
            inputs=(BoundInputV2(TXN, "public.transactions", "txn_amt"),
                    BoundInputV2(TXN, "public.other", "txn_amt")))


def test_an_empty_bound_set_is_refused():
    with pytest.raises(ValueError, match="binds nothing"):
        BoundInputSetRevisionV2(revision_id="b", environment_id="e", inputs=())


# ══ C-B7 — the identity is NARROWER than the observation ═════════════════════════════════════════
def test_AN_IDENTICAL_RECAPTURE_LEAVES_IDENTITY_UNCHANGED():
    """New observation id, new capture time, same environment — the same compilation identity."""
    first = _observation(observation_id="obs-1")
    again = _observation(observation_id="obs-2",
                         inventory=_inventory(captured_at="2026-12-25T09:30:00Z"))
    assert first.content_hash == again.content_hash


def test_AN_UNRELATED_TABLE_LEAVES_IDENTITY_UNCHANGED():
    """Otherwise adding a table to the cluster would invalidate every compiled feature."""
    wider = _inventory(tables={
        "public.transactions": _layout("transactions"),
        "public.marketing_campaigns": _layout("marketing_campaigns", ("campaign_id",)),
    })
    assert _observation().content_hash == _observation(inventory=wider).content_hash


def test_a_layout_change_IN_THE_READ_SET_does_change_identity():
    """The other side of the same rule — otherwise the narrowing would make identity meaningless."""
    changed = _inventory(tables={
        "public.transactions": _layout("transactions", ("txn_amt", "acct_id", "new_col"))})
    assert _observation().content_hash != _observation(inventory=changed).content_hash


@pytest.mark.parametrize("dimension", sorted(RUNTIME_DIMENSIONS))
def test_every_engine_version_enters_generation_identity(dimension):
    """An artifact rendered against different runtimes is a different artifact."""
    moved = _inventory(versions=_versions(**{dimension: "99.99.99"}))
    assert _observation().content_hash != _observation(inventory=moved).content_hash


def test_the_environment_id_enters_identity():
    assert _observation().content_hash != _observation(
        inventory=_inventory(environment_id="hdfc-prod")).content_hash


def test_only_the_USED_schema_mappings_enter_identity():
    """A mapping the compilation never consulted is not a fact it depended on."""
    wider = _inventory(schema_map={"hdfc::public.transactions": "public",
                                   "hdfc::public.unrelated": "other_schema"})
    assert _observation().content_hash == _observation(inventory=wider).content_hash


def test_using_a_mapping_the_inventory_DOES_NOT_DECLARE_is_refused():
    """The identity would cover a mapping nobody captured."""
    with pytest.raises(ValueError, match="does not declare"):
        _observation(used_logical_schema_refs=("hdfc::public.transactions", "hdfc::public.ghost"))


def test_the_complete_observation_is_STORED_even_though_it_is_not_hashed():
    """Audit needs the whole snapshot; identity needs a slice of it."""
    wider = _inventory(tables={
        "public.transactions": _layout("transactions"),
        "public.marketing_campaigns": _layout("marketing_campaigns", ("campaign_id",))})
    observation = _observation(inventory=wider)
    assert "public.marketing_campaigns" in observation.inventory.tables
    assert "public.marketing_campaigns" not in observation.identity_payload()["used_layouts"]


# ══ C-B8 — eight of eight, plus every layout ═════════════════════════════════════════════════════
def test_THE_EIGHT_RUNTIME_DIMENSIONS_ARE_EXHAUSTIVE_OVER_EngineVersions():
    """A ninth runtime cannot be added without deciding how it compares."""
    assert set(RUNTIME_DIMENSIONS) == {f.name for f in dataclasses.fields(EngineVersions)}
    assert len(RUNTIME_DIMENSIONS) == 8


@pytest.mark.parametrize("dimension", sorted(RUNTIME_DIMENSIONS))
def test_every_dimension_has_a_rule_AND_a_reason(dimension):
    rule, reason = RUNTIME_DIMENSIONS[dimension]
    assert isinstance(rule, CompatibilityRule)
    assert len(reason) > 30, "a rule without a reason cannot be argued with"


@pytest.mark.parametrize("dimension", sorted(RUNTIME_DIMENSIONS))
def test_A_DIFFERENCE_ON_EVERY_DIMENSION_IS_DETECTED(dimension):
    """Eight of eight, driven rather than asserted."""
    verification = VerificationInventoryObservationV1(
        observation_id="vobs-1", inventory=_inventory(versions=_versions(**{dimension: "99.0.0"})))
    findings = compare_inventories(_observation(), verification)
    assert [f.dimension for f in findings] == [dimension]
    assert findings[0].verifying_with == "99.0.0"


@pytest.mark.parametrize("dimension", sorted(
    d for d, (rule, _) in RUNTIME_DIMENSIONS.items() if rule is CompatibilityRule.MAJOR_COMPATIBLE))
def test_a_PATCH_bump_is_tolerated_where_the_rule_says_so(dimension):
    """The distinction a single "are these the same" boolean cannot express."""
    current = getattr(_versions(), dimension)
    bumped = ".".join([current.split(".")[0], "99", "99"])
    verification = VerificationInventoryObservationV1(
        observation_id="vobs-1", inventory=_inventory(versions=_versions(**{dimension: bumped})))
    assert compare_inventories(_observation(), verification) == ()


@pytest.mark.parametrize("dimension", sorted(
    d for d, (rule, _) in RUNTIME_DIMENSIONS.items() if rule is CompatibilityRule.EXACT))
def test_an_EXACT_dimension_tolerates_no_drift_at_all(dimension):
    current = getattr(_versions(), dimension)
    bumped = ".".join([current.split(".")[0], "99", "99"])
    verification = VerificationInventoryObservationV1(
        observation_id="vobs-1", inventory=_inventory(versions=_versions(**{dimension: bumped})))
    assert [f.dimension for f in compare_inventories(_observation(), verification)] == [dimension]


def test_A_CHANGED_PHYSICAL_LAYOUT_IS_DETECTED():
    """Partitioning and types decide what a scan returns."""
    verification = VerificationInventoryObservationV1(
        observation_id="vobs-1", inventory=_inventory(tables={
            "public.transactions": _layout("transactions", ("txn_amt",))}))
    findings = compare_inventories(_observation(), verification)
    assert [f.dimension for f in findings] == ["layout:public.transactions"]


def test_a_MISSING_layout_is_detected_as_absent():
    verification = VerificationInventoryObservationV1(
        observation_id="vobs-1", inventory=_inventory(tables={}))
    (finding,) = compare_inventories(_observation(), verification)
    assert finding.verifying_with == "ABSENT"


def test_a_DIFFERENT_ENVIRONMENT_is_not_a_verification_of_this_artifact():
    verification = VerificationInventoryObservationV1(
        observation_id="vobs-1", inventory=_inventory(environment_id="hdfc-prod"))
    findings = compare_inventories(_observation(), verification)
    assert findings[0].dimension == "environment_id"
    assert "reads other data" in findings[0].reason


def test_an_identical_environment_produces_NO_findings():
    verification = VerificationInventoryObservationV1(
        observation_id="vobs-1", inventory=_inventory(captured_at="2027-01-01T00:00:00Z"))
    assert compare_inventories(_observation(), verification) == ()


def test_findings_are_returned_not_a_boolean():
    """An operator deciding whether to re-run needs to know WHICH dimension moved; "incompatible"
    alone sends them to diff two whole inventories by hand."""
    verification = VerificationInventoryObservationV1(
        observation_id="vobs-1",
        inventory=_inventory(versions=_versions(spark="9.9.9", hive="9.9.9")))
    findings = compare_inventories(_observation(), verification)
    assert {f.dimension for f in findings} == {"spark", "hive"}
    assert all(f.reason for f in findings)
