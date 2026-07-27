"""Spec A Task 9 — §5's `MaterializationContractV1`: derived PER FEATURE, then grouped by hash.

**Why per feature is the whole design.** Deriving ONE contract from the union of the supplied IRs
would let a caller force a public feature into a restricted group merely by passing them together —
the group would inherit the maximum of everything and nobody would be told. So each feature is
classified over ITS OWN §1.3 read set, and features are grouped afterwards by equal contract hash.
More than one distinct hash returns `MULTIPLE_MATERIALIZATION_CONTRACTS` listing the groups; nothing
is silently promoted, and the public feature's own contract still says what it always said.

**Two hash mistakes these tests exist to catch.**

1. *The calculation window is not contract identity (§5.3).* A 30-day and a 90-day trailing feature
   must share a contract — the contract describes what the landing key MEANS, not how a column was
   computed. `test_the_calculation_window_is_not_contract_identity` carries its own control: it
   asserts the two IR hashes DIFFER first, so "the contract hashes are equal" is a statement about
   the contract rather than about two fixtures that were never different.
2. *The spine contributes `identity_payload()` and never its provenance (§4).* Two people making
   the identical semantic declaration must produce the same contract; letting `declared_by` in would
   partition groups by who happened to declare the population.

**And a third that would be invisible.** The resolved physical TYPE is not in the contract — only
`PHYSICAL_TYPE_POLICY_VERSION` is. A contract carrying `DECIMAL(38,6)` would make every differently
typed feature its own contract, so a `BIGINT` count and a `DECIMAL` sum could never share a table
and the group would refuse itself. `test_a_count_and_a_sum_share_a_contract` is that check.

The catalog, the inventory and the compile helpers are imported from `test_ir` rather than copied: a
second definition of the same governed catalog is a second chance to disagree about what the catalog
is, and every assertion here about a read set is only as good as the read set being the real one.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_ir import (
    ACCOUNTS_ID,
    CUSTOMERS_CIF,
    INVENTORY,
    PUBLIC_FEATURE,
    RATIO_FEATURE,
    TXN_AMT,
    _admitted,
    _compile,
    _declaration,
    _govern_availability,
    _grain_on_customers,
    _ok,
    seed_catalog,
)

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize import classify as classify_module
from featuregen.materialize import contract as contract_module
from featuregen.materialize.classify import CLASSIFICATION_POLICY_VERSION
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.contract import (
    DEFAULT_RETENTION_CLASS,
    RETENTION_POLICY_VERSION,
    AvailabilityClass,
    BackfillBoundary,
    CadenceDecl,
    CadencePeriod,
    CadenceTrigger,
    ContractGroup,
    ContractOverrides,
    MaterializationContractV1,
    PublicationPolicy,
    contract_hash,
    derive_contract,
    derive_group_contract,
    group_by_contract,
)
from featuregen.materialize.ir import authorize_compilation, ir_hash, physical_read_set
from featuregen.materialize.physical_types import PHYSICAL_TYPE_POLICY_VERSION

_ROLES = ("feature_engineer",)
_SRC = "hdfc"
SUM_30D = "total_debit_amount_30d"
COUNT_90D = "distinct_merchant_count_90d"

CADENCE = CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                      business_date_cutoff="00:00:00", trigger=CadenceTrigger.SCHEDULED)


@pytest.fixture
def catalog(db):
    """THE governed catalog Task 7 seeds, seeded by Task 7's own function (module docstring)."""
    return seed_catalog(db)


def _contract(db, name=SUM_30D, *, ir=None, cadence=CADENCE,
              availability_class=AvailabilityClass.NEXT_DAY, overrides=None, **compile_kwargs):
    """Derive ONE feature's contract, failing the test HERE if the compile itself refused."""
    compiled = ir if ir is not None else _ok(_compile(db, name, **compile_kwargs))
    return derive_contract(db, compiled, cadence=cadence, availability_class=availability_class,
                           overrides=overrides)


def _derived(value) -> MaterializationContractV1:
    assert isinstance(value, MaterializationContractV1), value
    return value


def _restrict(db, table, column, restriction) -> None:
    """Set the ORDERED restriction level (`graph_node.effective_restriction`) — axis two of §5.2,
    which is NOT the read-scope tag Gate 2 reads."""
    db.execute(
        "UPDATE graph_node SET effective_restriction = %s WHERE catalog_source = %s "
        "AND object_ref = %s", (restriction, _SRC, f"public.{table}.{column}"))


def _rewindowed(name, length):
    """The same worked feature with a different trailing window — everything else byte-identical."""
    formula = fixtures.authored_formula(name)
    expr = formula.body.expr
    return dataclasses.replace(formula, body=dataclasses.replace(
        formula.body,
        expr=dataclasses.replace(expr, window=dataclasses.replace(expr.window, length=length))))


# ══ §5.1 — derived PER FEATURE, then grouped ═════════════════════════════════════════════════════


def test_contracts_are_derived_PER_FEATURE_never_from_the_union(catalog):
    """The governance claim of §5.1, tested where it can actually fail.

    `total_debit_amount_30d` reads `txn_amt`, which is made `confidential`; the count feature does
    not read it. If contracts came from the union, BOTH would come out `confidential` and the public
    feature would have been promoted with nobody told.
    """
    _restrict(catalog, "transactions", "txn_amt", "confidential")
    _restrict(catalog, "transactions", "merchant_id", "public")

    restricted = _derived(_contract(catalog, SUM_30D))
    public = _derived(_contract(catalog, COUNT_90D))
    assert restricted.sensitivity_class == "confidential"
    assert public.sensitivity_class == "internal"        # its own read set, its own answer
    assert contract_hash(restricted) != contract_hash(public)


def test_passing_two_features_together_cannot_promote_either(catalog):
    """The attack §5.1 names: a caller hands both features to one call and hopes for one contract."""
    _restrict(catalog, "transactions", "txn_amt", "restricted")
    group = tuple(_ok(_compile(catalog, name)) for name in (COUNT_90D, SUM_30D))
    authorization = authorize_compilation(catalog, group, group[0].spine, roles=_ROLES)

    refused = derive_group_contract(catalog, authorization, cadence=CADENCE,
                                    availability_class=AvailabilityClass.NEXT_DAY)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.MULTIPLE_MATERIALIZATION_CONTRACTS
    # Neither feature was promoted: each still derives its OWN class.
    assert _derived(_contract(catalog, COUNT_90D)).sensitivity_class == "internal"
    assert _derived(_contract(catalog, SUM_30D)).sensitivity_class == "restricted"


def test_more_than_one_contract_LISTS_THE_GROUPS_rather_than_unioning_them(catalog):
    _restrict(catalog, "transactions", "txn_amt", "confidential")
    contracts = {name: _derived(_contract(catalog, name)) for name in (SUM_30D, COUNT_90D)}
    refused = group_by_contract(contracts)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.MULTIPLE_MATERIALIZATION_CONTRACTS
    assert "2 " in refused.detail
    for name, derived in contracts.items():
        assert name in refused.detail
        assert contract_hash(derived) in refused.detail


def test_one_contract_groups_every_feature_that_shares_it(catalog):
    contracts = {name: _derived(_contract(catalog, name)) for name in (SUM_30D, COUNT_90D)}
    group = group_by_contract(contracts)
    assert isinstance(group, ContractGroup)
    assert group.feature_names == (COUNT_90D, SUM_30D)          # sorted, deterministic
    assert group.contract_hash == contract_hash(contracts[SUM_30D])


def test_grouping_nothing_is_a_CALLER_error(catalog):
    with pytest.raises(ValueError, match="no features"):
        group_by_contract({})


def test_the_group_entry_point_requires_an_AUTHORIZATION_TOKEN(catalog):
    """Gate 2 cannot be skipped on the way to a group: §1.3 says a refused group produces no
    contract, and the only way to keep that true is to require the token in the signature."""
    group = (_ok(_compile(catalog, SUM_30D)),)
    with pytest.raises(TypeError, match="AuthorizedCompilation"):
        derive_group_contract(catalog, group, cadence=CADENCE,
                              availability_class=AvailabilityClass.NEXT_DAY)


def test_the_group_contract_is_derived_over_the_authorized_IRS(catalog):
    group = tuple(_ok(_compile(catalog, name)) for name in (SUM_30D, COUNT_90D))
    authorization = authorize_compilation(catalog, group, group[0].spine, roles=_ROLES)
    derived = derive_group_contract(catalog, authorization, cadence=CADENCE,
                                    availability_class=AvailabilityClass.NEXT_DAY)
    assert isinstance(derived, ContractGroup)
    assert derived.feature_names == (COUNT_90D, SUM_30D)


def test_a_PROHIBITED_input_refuses_the_whole_group(catalog):
    _restrict(catalog, "transactions", "txn_amt", "prohibited")
    group = tuple(_ok(_compile(catalog, name)) for name in (SUM_30D, COUNT_90D))
    authorization = authorize_compilation(catalog, group, group[0].spine, roles=_ROLES)
    refused = derive_group_contract(catalog, authorization, cadence=CADENCE,
                                    availability_class=AvailabilityClass.NEXT_DAY)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.PROHIBITED_INPUT


# ══ §5.3 — the calculation window is NOT contract identity ═══════════════════════════════════════


def test_the_calculation_window_is_not_contract_identity(catalog):
    """A 30-day and a 90-day trailing feature share a contract — the intended behaviour (§5.3).

    The CONTROL comes first: the two IR hashes must differ, otherwise "the contracts are equal" is a
    statement about two fixtures that were never different.
    """
    thirty = _ok(_compile(catalog, SUM_30D))
    ninety = _ok(_compile(catalog, SUM_30D, formula=_rewindowed(SUM_30D, 90)))
    assert ir_hash(thirty) != ir_hash(ninety)
    assert {e.pit.window_length for e in thirty.expressions} == {30}
    assert {e.pit.window_length for e in ninety.expressions} == {90}

    assert contract_hash(_derived(_contract(catalog, ir=thirty))) == contract_hash(
        _derived(_contract(catalog, ir=ninety)))


def test_30d_and_90d_features_land_in_ONE_group(catalog):
    contracts = {name: _derived(_contract(catalog, name)) for name in (SUM_30D, COUNT_90D)}
    assert contract_hash(contracts[SUM_30D]) == contract_hash(contracts[COUNT_90D])


def test_a_count_and_a_sum_share_a_contract(catalog):
    """The resolved physical type is NOT contract identity — only the POLICY VERSION is (§5.5).

    A `COUNT_DISTINCT` publishes `BIGINT` and a `SUM` publishes `DECIMAL(38,6)`. A contract carrying
    the type would give them different hashes, so a group could never hold both — which is every
    real feature group.
    """
    payload = _derived(_contract(catalog, SUM_30D)).identity_payload()
    assert "BIGINT" not in str(payload) and "DECIMAL" not in str(payload)
    assert contract_hash(_derived(_contract(catalog, SUM_30D))) == contract_hash(
        _derived(_contract(catalog, COUNT_90D)))


def test_a_ratios_two_expressions_do_not_split_its_contract(catalog):
    """One feature, two windows and two expressions — still one contract."""
    assert contract_hash(_derived(_contract(catalog, RATIO_FEATURE))) == contract_hash(
        _derived(_contract(catalog, SUM_30D)))


# ══ §5.5 — what the hash contains, and what it must not ══════════════════════════════════════════


def test_the_identity_payload_is_EXACTLY_the_specified_field_set(catalog):
    """`==`, never `<=`: a superset assertion permits anything extra to creep in, which is how a
    run id or a wall-clock reading enters an identity nobody notices changing."""
    assert set(_derived(_contract(catalog)).identity_payload()) == {
        "entity", "ordered_keys", "pit_semantics", "sensitivity_class", "access_requirements",
        "retention_class", "retention_policy_version", "availability_class", "cadence",
        "publication_policy", "backfill_boundary", "spine", "classification_policy_version",
        "physical_type_policy_version"}


def test_the_landing_pit_semantics_are_the_LANDING_KEYS_meaning(catalog):
    """§5.3: entity keys, `business_dt`, the cutoff timezone and time, and the availability basis
    class — and NOT the calculation window."""
    payload = _derived(_contract(catalog)).identity_payload()["pit_semantics"]
    assert set(payload) == {"entity_keys", "business_dt_column", "cutoff_timezone", "cutoff_time",
                            "availability_basis_class"}
    assert payload["entity_keys"] == [CUSTOMERS_CIF]
    assert payload["business_dt_column"] == "business_dt"
    assert payload["cutoff_timezone"] == "Asia/Kolkata"
    assert payload["cutoff_time"] == "00:00:00"
    assert payload["availability_basis_class"] == ["posted_at"]


def test_the_availability_BASIS_is_part_of_the_landing_semantics(catalog):
    """Two rows keyed `(cif_id, 2026-07-27)` whose columns are gated on `posted_at` in one case and
    `ingested_at` in the other do not mean the same thing, so they are not one contract."""
    posted = _derived(_contract(catalog, SUM_30D))
    _govern_availability(catalog, "transactions", "posted_ts", basis="ingested_at")
    ingested = _derived(_contract(catalog, SUM_30D))
    assert ingested.pit_semantics.availability_basis_class == ("ingested_at",)
    assert contract_hash(posted) != contract_hash(ingested)


def test_the_contract_carries_NO_feature_name(catalog):
    """A feature name in the contract would give every feature its own contract, and a group could
    never contain two features."""
    payload = str(_derived(_contract(catalog, SUM_30D)).identity_payload())
    assert SUM_30D not in payload
    assert not any(field.name == "feature_name"
                   for field in dataclasses.fields(MaterializationContractV1))


def test_the_hash_excludes_the_IRs_own_provenance(catalog):
    """`authoring_run_id`, the formula hash and the expression hashes are per-feature facts; a
    contract that carried any of them could not be shared."""
    payload = str(_derived(_contract(catalog, SUM_30D, run_id="run-9999")).identity_payload())
    assert "run-9999" not in payload
    assert contract_hash(_derived(_contract(catalog, SUM_30D, run_id="run-0001"))) == contract_hash(
        _derived(_contract(catalog, SUM_30D, run_id="run-9999")))


def test_the_hash_excludes_LIVE_OBSERVATIONS(catalog):
    """§5.5's exclusions: the catalog's watermark moves on every projection run, and a contract that
    followed it would change identity without a single governed fact changing."""
    before = contract_hash(_derived(_contract(catalog, SUM_30D)))
    catalog.execute(
        "UPDATE overlay_drift_watermark SET last_completed_at = now(), head_seq = head_seq + 41 "
        "WHERE catalog_source = %s", (_SRC,))
    assert contract_hash(_derived(_contract(catalog, SUM_30D))) == before


def test_the_spine_contributes_its_IDENTITY_never_its_provenance(catalog):
    """Two people making the identical semantic declaration produce the SAME contract (§4)."""
    other_declarer = IdentityEnvelope(
        subject="user:ravi", actor_kind="human", authenticated=False, auth_method="test",
        role_claims=("feature_engineer",))
    twin = _declaration(declared_by=other_declarer, declaration_reason="the same table, said twice",
                        recorded_at="2027-01-01T00:00:00+00:00",
                        declaration_record_id="spine-decl-9999")

    mine = _derived(_contract(catalog, SUM_30D))
    theirs = _derived(_contract(catalog, SUM_30D, spine_decl=twin))
    assert contract_hash(mine) == contract_hash(theirs)
    payload = str(mine.identity_payload())
    assert "user:asha" not in payload and "spine-decl-0001" not in payload


def test_a_SEMANTIC_change_to_the_declaration_does_change_the_contract(catalog):
    """The control for the test above — otherwise "the spine barely matters" would pass it too."""
    from featuregen.materialize.spine import CurrentSnapshot

    other = _declaration(snapshot_policy=CurrentSnapshot(observed_snapshot_ref="2020-01-01"))
    assert contract_hash(_derived(_contract(catalog, SUM_30D))) != contract_hash(
        _derived(_contract(catalog, SUM_30D, spine_decl=other)))


@pytest.mark.parametrize("module_and_name", [
    (contract_module, "RETENTION_POLICY_VERSION"),
    (contract_module, "PHYSICAL_TYPE_POLICY_VERSION"),
    (classify_module, "CLASSIFICATION_POLICY_VERSION"),
])
def test_all_three_policy_versions_enter_identity(catalog, monkeypatch, module_and_name):
    """§5.5 names all three. A column typed, classified or retained under different rules is a
    different artifact even when every word of the contract reads the same."""
    module, name = module_and_name
    before = contract_hash(_derived(_contract(catalog)))
    monkeypatch.setattr(module, name, getattr(module, name) + 1)
    assert contract_hash(_derived(_contract(catalog))) != before


def test_the_declared_retention_constants_are_PINNED():
    """Both enter identity, and neither is derived from anything — so the only thing that can catch
    an accidental edit is a test that names the values. §5.2 requires them to be DECLARED rather than
    invented, and a declaration nobody pinned is a value that can drift silently."""
    assert (DEFAULT_RETENTION_CLASS, RETENTION_POLICY_VERSION) == ("platform_default", 1)


def test_the_declared_constants_are_the_ones_the_contract_records(catalog):
    derived = _derived(_contract(catalog))
    assert derived.retention_class == DEFAULT_RETENTION_CLASS
    assert derived.retention_policy_version == RETENTION_POLICY_VERSION
    assert derived.classification_policy_version == CLASSIFICATION_POLICY_VERSION
    assert derived.physical_type_policy_version == PHYSICAL_TYPE_POLICY_VERSION


def test_the_defaults_are_the_ones_5_4_declares(catalog):
    derived = _derived(_contract(catalog))
    assert derived.publication_policy is PublicationPolicy.ATOMIC_GROUP
    assert derived.backfill_boundary is BackfillBoundary.GROUP_LEVEL


def test_the_hash_is_the_packages_ONE_hasher(catalog):
    from featuregen.materialize.canonical import materialize_hash

    derived = _derived(_contract(catalog))
    assert contract_hash(derived) == materialize_hash(derived.identity_payload())


def test_the_hash_is_stable_across_derivations(catalog):
    assert contract_hash(_derived(_contract(catalog))) == contract_hash(_derived(_contract(catalog)))


# ══ §5.2 — the classification the contract records ═══════════════════════════════════════════════


def test_the_contract_takes_BOTH_axes_from_the_classification(catalog):
    catalog.execute(
        "UPDATE graph_node SET sensitivity = 'pii' WHERE catalog_source = %s AND object_ref = %s",
        (_SRC, "public.transactions.txn_amt"))
    _restrict(catalog, "transactions", "txn_amt", "confidential")
    derived = _derived(_contract(catalog, SUM_30D, roles=(*_ROLES, "pii_reader")))
    assert derived.sensitivity_class == "confidential"
    assert derived.access_requirements == ("pii_reader",)


def test_the_SPINE_can_decide_the_class(catalog):
    """The read set the contract classifies is §1.3's union, so the population's own columns count
    even though no expression mentions them."""
    _restrict(catalog, "customers", "cif_id", "restricted")
    assert _derived(_contract(catalog, SUM_30D)).sensitivity_class == "restricted"


def test_a_JOIN_ENDPOINT_can_decide_the_class(catalog):
    """A column nothing reads as an operand, only as a hop of the governed traversal."""
    ir = _ok(_compile(catalog, PUBLIC_FEATURE, formula=_grain_on_customers(PUBLIC_FEATURE)))
    assert ACCOUNTS_ID in physical_read_set((ir,), ir.spine)
    _restrict(catalog, "accounts", "account_id", "confidential")
    assert _derived(_contract(catalog, ir=ir)).sensitivity_class == "confidential"


def test_the_contract_classifies_THIS_features_read_set_only(catalog):
    """The control for the two above: a restriction on a column this feature does not read does not
    reach its contract."""
    _restrict(catalog, "transactions", "cross_border_flag", "restricted")
    assert _derived(_contract(catalog, SUM_30D)).sensitivity_class == "internal"
    assert _derived(_contract(catalog, RATIO_FEATURE)).sensitivity_class == "restricted"


# ══ §5.4 — declared cadence, and the overrides that may only TIGHTEN ═════════════════════════════


def test_the_trigger_vocabulary_is_CLOSED_and_dependencies_ready_is_not_in_it():
    """`==`, never `>=`. `dependencies_ready` is a DEFERRED NFR: accepting it would schedule runs on
    a dependency graph this slice does not have."""
    assert {t.value for t in CadenceTrigger} == {"scheduled", "manual"}


def test_the_cadence_identity_is_EXACTLY_its_four_declared_fields():
    """Pinned as a key set, because the cutoff and the zone appear TWICE in a contract — once as the
    schedule and once as the landing key's meaning. Without this, dropping either from the cadence
    payload would leave the contract hash unchanged (the pit semantics still carry them) and the
    schedule would quietly stop being identity-bearing. Both copies come from ONE declaration, so
    they cannot disagree; they are pinned separately so neither can go missing."""
    assert CADENCE.identity_payload() == {
        "period": "daily", "timezone": "Asia/Kolkata",
        "business_date_cutoff": "00:00:00", "trigger": "scheduled"}


def test_an_unknown_cadence_PERIOD_is_REFUSED():
    """The period is a closed vocabulary too: an hourly cadence declared against machinery with one
    `business_dt` per run would schedule runs that overwrite each other."""
    with pytest.raises(ValueError, match="period"):
        CadenceDecl(period="hourly", timezone="Asia/Kolkata",
                    business_date_cutoff="00:00:00", trigger=CadenceTrigger.SCHEDULED)


def test_a_dependencies_ready_trigger_is_REFUSED():
    with pytest.raises(ValueError, match="dependencies_ready"):
        CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                    business_date_cutoff="00:00:00", trigger="dependencies_ready")


def test_an_invalid_timezone_is_REFUSED():
    """`ZoneInfo`-validated (§5.4): the cutoff is a wall-clock time in a named zone, and an unknown
    zone makes `business_dt` mean nothing."""
    with pytest.raises(ValueError, match="timezone"):
        CadenceDecl(period=CadencePeriod.DAILY, timezone="Mars/Olympus_Mons",
                    business_date_cutoff="00:00:00", trigger=CadenceTrigger.SCHEDULED)


def test_an_invalid_cutoff_TIME_is_REFUSED():
    with pytest.raises(ValueError, match="cutoff"):
        CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                    business_date_cutoff="25:00:00", trigger=CadenceTrigger.SCHEDULED)


def test_a_cutoff_carrying_its_OWN_offset_is_REFUSED():
    """The zone is declared once, in `timezone`. A cutoff with an offset would be a second, silently
    disagreeing answer to "which clock?"."""
    with pytest.raises(ValueError, match="offset"):
        CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                    business_date_cutoff="00:00:00+05:30", trigger=CadenceTrigger.SCHEDULED)


def test_the_cutoff_is_CANONICALIZED_so_one_time_is_one_contract(catalog):
    """`00:00` and `00:00:00` are the same instant; two spellings must not fork the group."""
    terse = CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                        business_date_cutoff="00:00", trigger=CadenceTrigger.SCHEDULED)
    assert terse.business_date_cutoff == "00:00:00"
    assert contract_hash(_derived(_contract(catalog, cadence=terse))) == contract_hash(
        _derived(_contract(catalog)))


def test_the_cadence_enters_identity(catalog):
    later = dataclasses.replace(CADENCE, business_date_cutoff="18:30:00")
    elsewhere = dataclasses.replace(CADENCE, timezone="Europe/London")
    manual = dataclasses.replace(CADENCE, trigger=CadenceTrigger.MANUAL)
    base = contract_hash(_derived(_contract(catalog)))
    for cadence in (later, elsewhere, manual):
        assert contract_hash(_derived(_contract(catalog, cadence=cadence))) != base


def test_the_availability_class_is_DECLARED_and_enters_identity(catalog):
    base = contract_hash(_derived(_contract(catalog)))
    other = contract_hash(_derived(
        _contract(catalog, availability_class=AvailabilityClass.BEST_EFFORT)))
    assert base != other


def test_an_override_may_TIGHTEN_the_class(catalog):
    tightened = _derived(_contract(
        catalog, overrides=ContractOverrides(sensitivity_class="restricted")))
    assert tightened.sensitivity_class == "restricted"
    assert contract_hash(tightened) != contract_hash(_derived(_contract(catalog)))


def test_an_override_may_ADD_an_access_requirement(catalog):
    tightened = _derived(_contract(
        catalog, overrides=ContractOverrides(access_requirements=("pii_reader",))))
    assert tightened.access_requirements == ("pii_reader",)


def test_an_override_may_NOT_LOOSEN_the_class(catalog):
    """Monotonic (§5.4): stricter accepted, looser refused. The derived class here is `internal`
    (the stated missing-classification policy), so `public` is a LOOSENING."""
    with pytest.raises(ValueError, match="looser"):
        _contract(catalog, overrides=ContractOverrides(sensitivity_class="public"))


def test_an_override_may_NOT_DROP_an_access_requirement(catalog):
    catalog.execute(
        "UPDATE graph_node SET sensitivity = 'pii' WHERE catalog_source = %s AND object_ref = %s",
        (_SRC, "public.transactions.txn_amt"))
    with pytest.raises(ValueError, match="looser"):
        _contract(catalog, SUM_30D, roles=(*_ROLES, "pii_reader"),
                  overrides=ContractOverrides(access_requirements=("restricted_reader",)))


def test_an_override_to_an_unrankable_class_is_a_MALFORMED_declaration():
    """A catalog value that cannot be ranked is normalized then refused (§5.2); a DECLARED override
    that cannot be ranked is a typo, and normalizing it to the top of the scale would silently turn
    a misspelling into a prohibition."""
    with pytest.raises(ValueError, match="not a rank"):
        ContractOverrides(sensitivity_class="konfidential")


def test_an_override_INTO_the_refusing_class_refuses_the_contract(catalog):
    """Tightening all the way to the top is a legal declaration and an illegal artifact."""
    refused = _contract(catalog, overrides=ContractOverrides(sensitivity_class="prohibited"))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.PROHIBITED_INPUT


def test_a_no_op_override_changes_nothing(catalog):
    base = _derived(_contract(catalog))
    same = _derived(_contract(
        catalog, overrides=ContractOverrides(sensitivity_class=base.sensitivity_class)))
    assert contract_hash(same) == contract_hash(base)


# ══ shape ════════════════════════════════════════════════════════════════════════════════════════


def test_the_contract_is_frozen_and_slotted(catalog):
    derived = _derived(_contract(catalog))
    with pytest.raises(Exception):
        derived.sensitivity_class = "public"                     # type: ignore[misc]
    assert not hasattr(derived, "__dict__")


def test_the_entity_and_keys_are_the_LANDING_populations(catalog):
    """The published row is one per spine key per `business_dt`, so the contract's keys are the
    population's — not the aggregate's own grain columns, which live in the IR."""
    derived = _derived(_contract(catalog, SUM_30D))
    assert derived.entity == "customer"
    assert derived.ordered_keys == (CUSTOMERS_CIF,)
    assert TXN_AMT not in str(derived.identity_payload())


def test_the_declared_inventory_is_not_part_of_the_contract(catalog):
    """The contract is a statement about MEANING; the cluster it happens to be compiled against is
    a statement about placement (§7's identity carries that)."""
    assert INVENTORY.environment_id not in str(_derived(_contract(catalog)).identity_payload())


def test_an_admitted_feature_alone_cannot_produce_a_contract(catalog):
    """`derive_contract` takes a compiled IR: the read set it classifies does not exist before
    compilation, so there is no path from an admitted artifact straight to a contract."""
    with pytest.raises(AttributeError):
        derive_contract(catalog, _admitted(SUM_30D), cadence=CADENCE,
                        availability_class=AvailabilityClass.NEXT_DAY)
