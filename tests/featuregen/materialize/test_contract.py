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
    AvailabilityPromiseKind,
    AvailabilityPromiseV1,
    BackfillBoundary,
    CadenceDecl,
    CadencePeriod,
    CadenceTrigger,
    ContractGroup,
    ContractOverrides,
    MaterializationContractV1,
    PromiseComparison,
    PublicationPolicy,
    compare_availability_promises,
    contract_hash,
    derive_contract,
    derive_group_contract,
    group_by_contract,
    override_availability_promise,
)
from featuregen.materialize.ir import authorize_compilation, ir_hash, physical_read_set
from featuregen.materialize.physical_types import PHYSICAL_TYPE_POLICY_VERSION

_ROLES = ("feature_engineer",)
_SRC = "hdfc"
SUM_30D = "total_debit_amount_30d"
COUNT_90D = "distinct_merchant_count_90d"

CADENCE = CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                      business_date_cutoff="00:00:00", trigger=CadenceTrigger.SCHEDULED)

#: The two cadences that differ from `CADENCE` in exactly ONE element of the comparison basis —
#: the clock, and the hour on it. Each is a DIFFERENT answer to "T+3 by when, exactly?".
ELSEWHERE = dataclasses.replace(CADENCE, timezone="Europe/London")
EVENING = dataclasses.replace(CADENCE, business_date_cutoff="18:00:00")

NEXT_DAY = AvailabilityPromiseV1(calendar_days=1)
T3 = AvailabilityPromiseV1(calendar_days=3)
T3_PLUS_2H = AvailabilityPromiseV1(calendar_days=3, plus_minutes=120)


@pytest.fixture
def catalog(db):
    """THE governed catalog Task 7 seeds, seeded by Task 7's own function (module docstring)."""
    return seed_catalog(db)


def _contract(db, name=SUM_30D, *, ir=None, cadence=CADENCE,
              availability_promise=NEXT_DAY, overrides=None, **compile_kwargs):
    """Derive ONE feature's contract, failing the test HERE if the compile itself refused."""
    compiled = ir if ir is not None else _ok(_compile(db, name, **compile_kwargs))
    return derive_contract(db, compiled, cadence=cadence,
                           availability_promise=availability_promise, overrides=overrides)


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
                                    availability_promise=NEXT_DAY)
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
                              availability_promise=NEXT_DAY)


def test_the_group_contract_is_derived_over_the_authorized_IRS(catalog):
    group = tuple(_ok(_compile(catalog, name)) for name in (SUM_30D, COUNT_90D))
    authorization = authorize_compilation(catalog, group, group[0].spine, roles=_ROLES)
    derived = derive_group_contract(catalog, authorization, cadence=CADENCE,
                                    availability_promise=NEXT_DAY)
    assert isinstance(derived, ContractGroup)
    assert derived.feature_names == (COUNT_90D, SUM_30D)


def test_a_PROHIBITED_input_refuses_the_whole_group(catalog):
    _restrict(catalog, "transactions", "txn_amt", "prohibited")
    group = tuple(_ok(_compile(catalog, name)) for name in (SUM_30D, COUNT_90D))
    authorization = authorize_compilation(catalog, group, group[0].spine, roles=_ROLES)
    refused = derive_group_contract(catalog, authorization, cadence=CADENCE,
                                    availability_promise=NEXT_DAY)
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
        "retention_class", "retention_policy_version", "availability_promise", "cadence",
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


def test_the_availability_promise_is_DECLARED_and_enters_identity(catalog):
    base = contract_hash(_derived(_contract(catalog)))
    other = contract_hash(_derived(_contract(catalog, availability_promise=T3)))
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


# ══ §5.6 — the availability PROMISE is a canonical VALUE, not a label ════════════════════════════
#
# Rev 5 required an `availability_class` to be DECLARED and named no vocabulary, so Task 9 invented
# one — and it entered the contract hash, which made an arbitrary member list load-bearing: anyone
# needing `T+0` or `T+5` would have had to re-key every group to get it. §5.6 replaces the label with
# a structured offset that can express any of them without touching the schema.


def test_the_promise_KIND_vocabulary_is_CLOSED_and_ships_ONE_member():
    """`==`, never `>=`. One member today, and the point of §5.6 is that a second one costs nothing:
    `BUSINESS_DAY_OFFSET` may be added, but only WITH the governed holiday-calendar identifier and
    version §5.6 requires, because calendar and banking days must never be read as each other."""
    assert {k.value for k in AvailabilityPromiseKind} == {"calendar_offset"}


def test_the_KIND_is_in_the_canonical_payload_from_v1(catalog):
    """The whole forward-compatibility property depends on the discriminator shipping NOW.

    If `kind` were omitted while one member exists, the day `BUSINESS_DAY_OFFSET` arrives every
    existing payload would GAIN a field and every group would re-key — which is exactly what §5.6
    exists to avoid. So it is asserted in the promise's own payload AND in the contract's, because
    the contract is what actually gets hashed.
    """
    assert AvailabilityPromiseV1().identity_payload() == {
        "kind": "calendar_offset", "calendar_days": 0, "plus_minutes": 0}
    carried = _derived(_contract(catalog)).identity_payload()["availability_promise"]
    assert carried["kind"] == "calendar_offset"


def test_t3_plus_2h_hashes_differently_from_t3(catalog):
    """The minutes are identity, not decoration — with a control that the same promise is one hash."""
    plain = contract_hash(_derived(_contract(catalog, availability_promise=T3)))
    plus_2h = contract_hash(_derived(_contract(catalog, availability_promise=T3_PLUS_2H)))
    assert plain != plus_2h
    assert contract_hash(_derived(_contract(
        catalog, availability_promise=AvailabilityPromiseV1(calendar_days=3)))) == plain


def test_semantically_equivalent_inputs_have_ONE_canonical_form(catalog):
    """`days=0, plus_minutes=1560` is NOT a value — it is an input that must be normalized FIRST.

    Normalizing silently inside the main constructor would make two spellings of one promise both
    "work", and no call site could then show which was meant. So the plain constructor REFUSES it,
    a separate constructor turns it into `(1, 120)`, and that must hash identically to a directly
    constructed `(1, 120)` — otherwise the group key still depends on the spelling.
    """
    from featuregen.materialize.canonical import materialize_hash

    with pytest.raises(ValueError, match="plus_minutes"):
        AvailabilityPromiseV1(calendar_days=0, plus_minutes=1560)

    normalized = AvailabilityPromiseV1.normalized(calendar_days=0, plus_minutes=1560)
    direct = AvailabilityPromiseV1(calendar_days=1, plus_minutes=120)
    assert (normalized.calendar_days, normalized.plus_minutes) == (1, 120)
    assert normalized == direct
    assert materialize_hash(normalized.identity_payload()) == materialize_hash(
        direct.identity_payload())
    assert contract_hash(_derived(_contract(catalog, availability_promise=normalized))) == \
        contract_hash(_derived(_contract(catalog, availability_promise=direct)))


def test_normalizing_an_ALREADY_canonical_promise_is_the_identity_function():
    """The control for the test above: normalization is a re-spelling, not a second semantics."""
    assert AvailabilityPromiseV1.normalized(calendar_days=3, plus_minutes=120) == T3_PLUS_2H


@pytest.mark.parametrize("kwargs", [
    {"calendar_days": -1},                      # no promise lands before the business date
    {"plus_minutes": -1},
    {"plus_minutes": 1440},                     # a whole day, spelled as minutes
    {"plus_minutes": 1560},                     # §5.6's own example
    {"calendar_days": 1, "plus_minutes": 2880},
])
def test_negative_or_noncanonical_declarations_fail_construction(kwargs):
    """`calendar_days >= 0` and `0 <= plus_minutes < 1440`, enforced at construction (§5.6).

    The bound is what makes `(calendar_days, plus_minutes)` orderable as a plain tuple: once minutes
    can reach a day, `(1, 0)` and `(0, 1440)` are the same instant and the tuple comparison that
    decides a monotonic override starts lying.
    """
    with pytest.raises(ValueError):
        AvailabilityPromiseV1(**kwargs)


@pytest.mark.parametrize("kwargs", [{"calendar_days": "3"}, {"plus_minutes": 1.5},
                                    {"calendar_days": True}, {"kind": "business_day_offset"}])
def test_a_promise_declared_in_the_WRONG_TYPE_is_a_malformed_declaration(kwargs):
    """The payload is hashed: `"3"` and `3` are different canonical JSON and so different groups, and
    a `kind` outside the vocabulary would put an uninterpretable discriminator into identity."""
    with pytest.raises(ValueError):
        AvailabilityPromiseV1(**kwargs)


def test_the_offset_is_in_MINUTES_so_T1_plus_30_needs_no_schema_change():
    """§5.6 chose minutes over hours precisely so a half-hour promise is expressible in v1."""
    assert AvailabilityPromiseV1(calendar_days=1, plus_minutes=30).identity_payload() == {
        "kind": "calendar_offset", "calendar_days": 1, "plus_minutes": 30}


def test_the_normalizing_constructor_still_refuses_a_promise_BEFORE_the_cutoff():
    """Normalization re-spells a total; it does not invent a representable one. `-30` minutes from
    `T+0` is a promise to publish before the business date exists, and v1 cannot express it.

    The message is pinned on the TOTAL, because that is this guard's whole reason to exist: without
    it the borrow still fails downstream, but it fails complaining about a negative `calendar_days`
    the caller never wrote.
    """
    with pytest.raises(ValueError, match="totals -30 minutes"):
        AvailabilityPromiseV1.normalized(calendar_days=0, plus_minutes=-30)


@pytest.mark.parametrize("kwargs", [{"calendar_days": "3"}, {"plus_minutes": "30"},
                                    {"calendar_days": 1.5}, {"kind": "business_day_offset"}])
def test_the_normalizing_constructor_type_checks_BEFORE_it_multiplies(kwargs):
    """`"3" * 1440` is a 1440-character string, not three days: the check has to come first, or a
    declaration mistake surfaces as a `TypeError` from inside the arithmetic."""
    with pytest.raises(ValueError):
        AvailabilityPromiseV1.normalized(**kwargs)


def test_a_negative_MINUTE_component_normalizes_by_BORROWING_a_day():
    """`T+1 minus 30 minutes` is a legal total spelled non-canonically — one form, `(0, 1410)`."""
    assert AvailabilityPromiseV1.normalized(calendar_days=1, plus_minutes=-30) == \
        AvailabilityPromiseV1(calendar_days=0, plus_minutes=1410)


def test_promise_identity_contains_no_live_arrival_observation(catalog):
    """§5.5's exclusions, at the promise. A promise is what was DECLARED; when the data actually
    landed is an observation, and a promise that carried one would change identity every run.

    `==` on both the field set and the payload set, so an `observed_arrival_at` cannot be added
    without failing here — and the moving catalog watermark is exercised as the live observation
    nearest to hand.
    """
    assert {f.name for f in dataclasses.fields(AvailabilityPromiseV1)} == {
        "kind", "calendar_days", "plus_minutes"}
    assert set(AvailabilityPromiseV1().identity_payload()) == {
        "kind", "calendar_days", "plus_minutes"}

    before = contract_hash(_derived(_contract(catalog, availability_promise=T3_PLUS_2H)))
    catalog.execute(
        "UPDATE overlay_drift_watermark SET last_completed_at = now(), head_seq = head_seq + 7 "
        "WHERE catalog_source = %s", (_SRC,))
    assert contract_hash(_derived(_contract(catalog, availability_promise=T3_PLUS_2H))) == before


# ── the monotonic override (§5.4): LATER accepted, EARLIER a caller error, unequal clocks refused ──


def test_a_later_override_succeeds(catalog):
    """Later by DAYS through the derivation path, and later by MINUTES through the comparison."""
    derived = _derived(_contract(
        catalog, availability_promise=T3,
        overrides=ContractOverrides(availability_promise=AvailabilityPromiseV1(calendar_days=5))))
    assert derived.availability_promise == AvailabilityPromiseV1(calendar_days=5)
    assert contract_hash(derived) != contract_hash(
        _derived(_contract(catalog, availability_promise=T3)))

    assert override_availability_promise(T3, T3_PLUS_2H, current_cadence=CADENCE,
                                         proposed_cadence=CADENCE) == T3_PLUS_2H


def test_an_override_to_the_SAME_promise_changes_nothing(catalog):
    same = _derived(_contract(catalog, availability_promise=T3,
                              overrides=ContractOverrides(availability_promise=T3)))
    assert contract_hash(same) == contract_hash(
        _derived(_contract(catalog, availability_promise=T3)))


def test_an_earlier_override_is_a_CALLER_ERROR(catalog):
    """`ValueError`, NOT a governed refusal. §14's codes describe valid requests rejected by the
    catalog or by data state; asking to promise EARLIER than what was derived is a bad request, and
    borrowing a governed code for it would report a caller's mistake as a platform verdict."""
    with pytest.raises(ValueError) as caught:
        _contract(catalog, availability_promise=T3,
                  overrides=ContractOverrides(availability_promise=NEXT_DAY))
    assert "earlier" in str(caught.value)
    assert not isinstance(caught.value, MaterializationRefused)
    assert not any(code.value in str(caught.value) for code in CompilationRefusalCode)

    with pytest.raises(ValueError, match="earlier"):        # earlier by MINUTES alone
        override_availability_promise(T3_PLUS_2H, T3, current_cadence=CADENCE,
                                      proposed_cadence=CADENCE)


@pytest.mark.parametrize("other", [ELSEWHERE, EVENING])
def test_differing_cadence_timezone_or_cutoff_makes_promises_INCOMPARABLE(other):
    """"T+3 at 23:59 Asia/Dubai" and "T+3 at 18:00 UTC" are different clocks.

    Incomparable is a VERDICT of its own, not the absence of "later": forcing an ordering would let a
    monotonic override succeed on a comparison that means nothing. The control below runs the SAME
    two promises under ONE cadence and gets `LATER`, so the refusal is attributable to the clock
    rather than to the promises.
    """
    assert compare_availability_promises(T3, T3_PLUS_2H, left_cadence=CADENCE,
                                         right_cadence=other) is PromiseComparison.INCOMPARABLE
    assert compare_availability_promises(T3, T3_PLUS_2H, left_cadence=CADENCE,
                                         right_cadence=CADENCE) is PromiseComparison.LATER

    with pytest.raises(ValueError, match="incomparable"):
        override_availability_promise(T3, T3_PLUS_2H, current_cadence=CADENCE,
                                      proposed_cadence=other)


@pytest.mark.parametrize("other", [ELSEWHERE, EVENING])
def test_the_SAME_offset_on_a_DIFFERENT_clock_is_not_the_SAME_promise(other):
    """The sharpest case: equal components do NOT make two promises equal, because the offset is
    counted from a cutoff and the cutoff moved."""
    assert compare_availability_promises(T3, T3, left_cadence=CADENCE,
                                         right_cadence=other) is PromiseComparison.INCOMPARABLE
    assert compare_availability_promises(T3, T3, left_cadence=CADENCE,
                                         right_cadence=CADENCE) is PromiseComparison.SAME


def test_the_comparison_verdicts_are_a_CLOSED_four_member_vocabulary():
    assert {v.value for v in PromiseComparison} == {"earlier", "same", "later", "incomparable"}


def test_the_comparison_BASIS_is_the_KIND_the_TIMEZONE_and_the_CUTOFF():
    """The kind's contribution CANNOT be shown behaviourally in v1 — there is one member, so two
    promises of different kinds cannot be constructed — so it is pinned structurally instead. It is
    pinned at all because the day a second kind arrives is the day a calendar-day promise could
    otherwise be declared "later" than a banking-day one, which is not a comparison anyone can make.
    """
    from featuregen.materialize.contract import _comparison_basis

    assert _comparison_basis(T3, CADENCE) == ("calendar_offset", "Asia/Kolkata", "00:00:00")


def test_an_override_that_is_not_a_PROMISE_is_a_malformed_declaration():
    with pytest.raises(ValueError, match="availability_promise"):
        ContractOverrides(availability_promise="next_day")


def test_a_promise_declared_as_a_LABEL_is_refused_by_the_derivation(catalog):
    """The pre-§5.6 spelling. `"next_day"` must not reach the contract hash as a bare string: it
    would be the invented vocabulary again, this time untyped and uncomparable."""
    with pytest.raises(ValueError, match="AvailabilityPromiseV1"):
        _contract(catalog, availability_promise="next_day")


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
                        availability_promise=NEXT_DAY)
