"""The feature USE gate — sensitivity says who may SEE a column, this says who may BUILD from it.

THE FINDING (Release-A evaluation, 2026-08-03). Of five unsafe gold classes the platform refused
exactly ONE, target leakage. A PII column, a protected characteristic, a currency-blind amount and a
free-text label were all accepted as DESIGN_CHECKED with zero requirements. Read scope had worked
correctly — those columns were visible precisely because the caller held `pii_reader` /
`restricted_reader` — and visibility was the only question anything asked.

WHAT IS ASSERTED HERE, and why in this shape:

* one test per class, each proving BOTH polarities — the unsafe operand is refused with its own
  code AND a comparable safe operand on the same table is accepted. A gate that refused everything
  would pass a one-sided test and would be useless;
* the gate is REGISTRY-driven: a column whose NAME screams "description" but whose CONCEPT is an
  identifier passes, and a blandly-named column whose concept is descriptive does not. Name-pattern
  matching is exactly what this must not be;
* BOTH consumers of a proposed feature hit the same wall. `_validate_idea` is the one choke point
  (`_vet` -> menu, `contract.review.validate_minimum` -> confirm-time MCV, `contract.gate1` ->
  considered set, `planner.b_gauntlet` -> cross-catalog proposals), and this file proves the wall
  is really there on the contract and planner paths rather than asserting the call graph;
* the refusal VOCABULARY is closed and every member maps to a product family, so no refusal can
  reach a person as a bare red badge.
"""
from datetime import UTC, datetime, timedelta

import pytest

from featuregen.analysis.explain import (
    NEEDS_DATA_CHECK,
    NEEDS_SETUP,
    STRUCTURALLY_UNSUITABLE,
    UNDECIDED,
    UNMAPPED,
)
from featuregen.overlay.upload.concepts import (
    CONCEPT_REGISTRY,
    denomination_concepts,
    is_currency_denomination,
    is_descriptive,
    is_personal_data,
    is_protected_characteristic,
)
from featuregen.overlay.upload.feature_assist import (
    FEATURE_REFUSAL_FAMILIES,
    RejectCode,
    _validate_idea,
    feature_use_gate_enabled,
    refusal_family,
)

NOW = datetime(2026, 8, 4, tzinfo=UTC)
FRESH = timedelta(hours=24)
SOURCE = "bank"


def _col(db, table, column, *, concept=None, currency=None, data_type="numeric"):
    ref = f"public.{table}.{column}"
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, concept, currency) VALUES (%s, %s, 'column', %s, %s, %s, %s, %s)",
        (SOURCE, ref, table, column, data_type, concept, currency))
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = %s", (SOURCE, NOW, NOW))
    return ref


def _validate(db, refs, *, aggregation="latest", name="f"):
    known = set(refs)
    src_of = {r: {SOURCE} for r in refs}
    raw = {"name": name, "derives_from": list(refs), "aggregation": aggregation}
    return _validate_idea(db, raw, known, src_of, None, NOW, FRESH)


# ── class 2 · a protected characteristic ─────────────────────────────────────────────────────────


def test_a_protected_characteristic_is_refused_and_a_business_driver_beside_it_is_not(db):
    """ECOA / GDPR Article 9 as a model input. `structurally_unsuitable`: no approval lifts it."""
    protected = _col(db, "cust", "ctzn_ctry_cd", concept="protected_attribute", data_type="text")
    segment = _col(db, "cust", "seg_cd", concept="segment", data_type="text")

    idea, rej = _validate(db, [protected])
    assert idea is None
    assert rej.code == RejectCode.PROTECTED_CHARACTERISTIC
    assert refusal_family(rej.code) == STRUCTURALLY_UNSUITABLE

    idea, rej = _validate(db, [segment])
    assert rej is None and idea.validation_status == "DESIGN_CHECKED", (
        "a plain business driver on the same table was refused — the gate is not discriminating")


def test_a_protected_characteristic_is_refused_as_a_GROUPING_operand_too(db):
    """The finding names "operand OR grouping". A grouping key rides in `derives_from` like any
    other operand, so the refusal must not depend on where in the list it sits."""
    amount = _col(db, "txn", "amt", concept="monetary_flow", currency="USD")
    protected = _col(db, "txn", "gender_cd", concept="protected_attribute", data_type="text")

    _idea, rej = _validate(db, [amount, protected], aggregation="avg")
    assert rej is not None and rej.code == RejectCode.PROTECTED_CHARACTERISTIC


def test_a_special_category_concept_is_refused_by_the_same_class(db):
    """`special_category` (GDPR Art. 9 health/biometric) shares the class with protected
    characteristics — the registry says so through `sensitivity`, not through a second list."""
    vulnerable = _col(db, "cust", "vuln_flag", concept="vulnerability_flag", data_type="text")
    _idea, rej = _validate(db, [vulnerable])
    assert rej is not None and rej.code == RejectCode.PROTECTED_CHARACTERISTIC


# ── class 4 · a descriptive label ────────────────────────────────────────────────────────────────


def test_a_descriptive_label_is_refused_and_the_code_beside_it_is_accepted(db):
    """The registry has always said "the id joins, the name does not". This is that, enforced."""
    label = _col(db, "txn", "sol_desc", concept="branch_name", data_type="text")
    code = _col(db, "txn", "sol_id", concept="branch_id", data_type="text")

    idea, rej = _validate(db, [label])
    assert idea is None
    assert rej.code == RejectCode.DESCRIPTIVE_OPERAND
    assert refusal_family(rej.code) == STRUCTURALLY_UNSUITABLE
    assert "Use the CODE column beside it" in rej.message, (
        "a structurally_unsuitable refusal still has to tell the reviewer what to do instead")

    idea, rej = _validate(db, [code], aggregation="count")
    assert rej is None and idea.validation_status == "DESIGN_CHECKED"


def test_the_gate_reads_the_CONCEPT_not_the_column_name(db):
    """Both halves of "registry-driven, not name-driven", in one test.

    A column NAMED like a description whose concept is an identifier must pass; a blandly-named
    column whose concept is descriptive must not. A name-pattern implementation fails both ways.
    """
    innocent = _col(db, "txn", "cust_description_id", concept="customer_id", data_type="text")
    disguised = _col(db, "txn", "fld_017", concept="free_text", data_type="text")

    idea, rej = _validate(db, [innocent], aggregation="count")
    assert rej is None and idea is not None, (
        f"a column refused for its NAME: {rej and rej.message}")

    _idea, rej = _validate(db, [disguised])
    assert rej is not None and rej.code == RejectCode.DESCRIPTIVE_OPERAND


def test_a_column_with_no_concept_at_all_is_not_refused_by_this_gate(db):
    """Absence is not an assertion. An ungoverned catalog has no concepts and must keep working —
    a gate that refused the unknown would refuse an entire catalog on its first upload."""
    unknown = _col(db, "txn", "mystery_amt", data_type="numeric")
    idea, rej = _validate(db, [unknown], aggregation="avg")
    assert rej is None and idea is not None


# ── class 1 · personal data ──────────────────────────────────────────────────────────────────────


def test_personal_data_is_refused_naming_the_MISSING_POLICY_not_the_column(db):
    """`needs_setup`: a lawful-basis policy could license this and none exists, so the wording has
    to send the reviewer to a governance owner rather than reading as a permanent verdict."""
    dob = _col(db, "cust", "dob", concept="pii", data_type="date")
    idea, rej = _validate(db, [dob])

    assert idea is None
    assert rej.code == RejectCode.PERSONAL_DATA_POLICY_REQUIRED
    assert refusal_family(rej.code) == NEEDS_SETUP
    assert "personal-data use policy" in rej.message
    assert "governance owner must declare one" in rej.message


def test_the_personal_data_class_comes_from_the_registry_sensitivity_not_a_hand_list(db):
    """`pep_flag` is a flag, not a "sensitive"-group concept, and the registry still tags it pii.
    A gate keyed on the GROUP would miss it; this one reads `sensitivity`."""
    assert CONCEPT_REGISTRY["pep_flag"].group == "flag"
    pep = _col(db, "cust", "pep_ind", concept="pep_flag", data_type="text")
    _idea, rej = _validate(db, [pep])
    assert rej is not None and rej.code == RejectCode.PERSONAL_DATA_POLICY_REQUIRED


def test_a_structurally_unsuitable_operand_outranks_the_policy_refusal(db):
    """`party_name` is BOTH personal data and a descriptive label. The reviewer must be told the
    thing no policy can fix, not sent to a governance owner who could never help."""
    assert is_personal_data("party_name") and is_descriptive("party_name")
    name = _col(db, "cust", "full_nm", concept="party_name", data_type="text")
    _idea, rej = _validate(db, [name])
    assert rej.code == RejectCode.DESCRIPTIVE_OPERAND


# ── class 3 · a currency-carrying amount with the dimension in plain sight ────────────────────────


def test_an_amount_is_refused_when_the_currency_column_sits_unbound_on_its_own_table(db):
    amount = _col(db, "txn", "amt", concept="monetary_flow")
    currency = _col(db, "txn", "crncy", concept="currency_code", data_type="text")

    idea, rej = _validate(db, [amount], aggregation="sum")
    assert idea is None
    assert rej.code == RejectCode.CURRENCY_POLICY_REQUIRED
    assert refusal_family(rej.code) == NEEDS_SETUP
    assert currency in rej.message, "the refusal must name the column that fixes it"


def test_binding_the_currency_dimension_clears_it(db):
    """The gold set's safe control, as a unit test: the same amount WITH its currency."""
    amount = _col(db, "txn", "amt", concept="monetary_flow")
    currency = _col(db, "txn", "crncy", concept="currency_code", data_type="text")

    idea, rej = _validate(db, [amount, currency], aggregation="sum")
    assert rej is None and idea is not None


def test_a_declared_currency_on_the_column_clears_it(db):
    """The other way through: the column itself says what it is denominated in."""
    _currency = _col(db, "txn", "crncy", concept="currency_code", data_type="text")
    amount = _col(db, "txn", "amt", concept="monetary_flow", currency="AED")

    idea, rej = _validate(db, [amount], aggregation="sum")
    assert rej is None and idea is not None


def test_an_amount_on_a_table_with_NO_currency_column_is_left_to_the_existing_machinery(db):
    """Deliberate narrowing, stated as a test.

    With no visible currency dimension there is no fix to name, and refusing would refuse every
    amount feature in every ungoverned catalog. That case already belongs to MIXED_CURRENCY /
    CURRENCY_CONSISTENT, which this slice did not touch.
    """
    amount = _col(db, "ledger", "bal", concept="monetary_stock")
    idea, rej = _validate(db, [amount], aggregation="latest")
    assert rej is None and idea is not None


def test_an_FX_RATE_beside_the_amount_does_not_pass_for_the_currency_dimension(db):
    """`fx_conversion_rate` shares the `currency` group with `currency_code` and answers a
    different question. Binding a rate must not clear "in what currency is this number"."""
    amount = _col(db, "txn", "amt", concept="monetary_flow")
    _rate = _col(db, "txn", "fx_rt", concept="fx_conversion_rate")
    code = _col(db, "txn", "crncy", concept="currency_code", data_type="text")

    _idea, rej = _validate(db, [amount, "public.txn.fx_rt"], aggregation="sum")
    assert rej is not None and rej.code == RejectCode.CURRENCY_POLICY_REQUIRED
    assert code in rej.message


def test_the_mixed_currency_hard_reject_is_untouched(db):
    """The pre-existing refusal still fires and still reports as itself — the new gate extends the
    currency story, it does not replace or shadow it."""
    a = _col(db, "txn", "amt_a", concept="monetary_flow", currency="USD")
    b = _col(db, "txn", "amt_b", concept="monetary_flow", currency="EUR")
    _idea, rej = _validate(db, [a, b], aggregation="sum")
    assert rej.code == RejectCode.MIXED_CURRENCY
    assert refusal_family(rej.code) == NEEDS_DATA_CHECK


# ── the flag ─────────────────────────────────────────────────────────────────────────────────────


def test_the_gate_is_ON_by_default_and_the_flag_only_turns_it_OFF(db, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_FEATURE_USE_GATE", raising=False)
    assert feature_use_gate_enabled(), "the default must be ON; off would ship the hole"
    monkeypatch.setenv("FEATUREGEN_FEATURE_USE_GATE", "0")
    assert not feature_use_gate_enabled()
    monkeypatch.setenv("FEATUREGEN_FEATURE_USE_GATE", "1")
    assert feature_use_gate_enabled()


def test_with_the_gate_disabled_the_finding_comes_straight_back(db, monkeypatch):
    """The reachability control, the same standard the release bars hold themselves to: every one
    of the four classes is accepted as DESIGN_CHECKED with zero requirements again. If this stops
    reproducing, the tests above are passing for some other reason."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_USE_GATE", "0")
    _currency = _col(db, "txn", "crncy", concept="currency_code", data_type="text")
    unsafe = [
        _col(db, "txn", "amt", concept="monetary_flow"),
        _col(db, "cust", "dob", concept="pii", data_type="date"),
        _col(db, "cust", "ctzn_ctry_cd", concept="protected_attribute", data_type="text"),
        _col(db, "txn", "sol_desc", concept="branch_name", data_type="text"),
    ]
    for ref in unsafe:
        idea, rej = _validate(db, [ref])
        assert rej is None, f"{ref} was still refused with the gate off: {rej.message}"
        assert idea.validation_status == "DESIGN_CHECKED" and idea.requirements == ()


# ── the refusal vocabulary is closed and never a bare red badge ──────────────────────────────────


def test_every_refusal_code_maps_to_a_product_family(db):
    """The no-blocked rule, mechanically. A code with no family would render as a failure-shaped
    string; the import-time validator makes that impossible, and this states the contract."""
    codes = {v for k, v in vars(RejectCode).items()
             if not k.startswith("_") and isinstance(v, str)}
    assert set(FEATURE_REFUSAL_FAMILIES) == codes
    assert set(FEATURE_REFUSAL_FAMILIES.values()) <= {
        UNDECIDED, NEEDS_DATA_CHECK, STRUCTURALLY_UNSUITABLE, NEEDS_SETUP}
    assert UNMAPPED not in set(FEATURE_REFUSAL_FAMILIES.values())


def test_an_unknown_code_is_LOUD_rather_than_reported_as_undecided(db):
    """Defaulting to `undecided` would tell a reader "someone is still deciding" about a refusal
    this build cannot classify — a claim that may be false."""
    assert refusal_family("SOME_FUTURE_CODE") == UNMAPPED


def test_the_four_new_codes_land_in_the_families_the_product_rule_assigns(db):
    assert refusal_family(RejectCode.PROTECTED_CHARACTERISTIC) == STRUCTURALLY_UNSUITABLE
    assert refusal_family(RejectCode.DESCRIPTIVE_OPERAND) == STRUCTURALLY_UNSUITABLE
    assert refusal_family(RejectCode.PERSONAL_DATA_POLICY_REQUIRED) == NEEDS_SETUP
    assert refusal_family(RejectCode.CURRENCY_POLICY_REQUIRED) == NEEDS_SETUP


# ── the registry predicates ──────────────────────────────────────────────────────────────────────


def test_the_registry_predicates_answer_over_concepts_and_decline_the_unknown(db):
    assert is_protected_characteristic("protected_attribute")
    assert not is_protected_characteristic("customer_id")
    assert not is_protected_characteristic(None)
    assert not is_protected_characteristic("not_a_concept")
    assert is_descriptive("free_text") and is_descriptive("branch_name")
    assert not is_descriptive("branch_id")


def test_a_denomination_is_distinguished_from_a_conversion_rate(db):
    """Both live in the `currency` group; only one answers "denominated in what"."""
    assert is_currency_denomination("currency_code")
    assert not is_currency_denomination("fx_conversion_rate")
    assert not is_currency_denomination("cross_rate")
    assert denomination_concepts() == frozenset(
        {"currency_code", "base_currency", "local_currency"})


def test_no_identifier_concept_is_marked_descriptive(db):
    """A join key that reports as prose would be refused by class 4. The registry validator makes
    this impossible at import; this states why it matters."""
    assert [c.name for c in CONCEPT_REGISTRY.values()
            if c.descriptive and c.group == "identifier"] == []


# ── BOTH consumers of a proposed feature hit the same wall ───────────────────────────────────────


def test_the_confirm_time_contract_validation_refuses_the_same_operand(db):
    """The contract path. A feature that never went through the menu — a draft assembled and put
    straight to `validate_minimum` — must hit the identical wall at confirm time, or the gate would
    be a UI filter with a bypass beside it."""
    from featuregen.overlay.upload.contract.author import ContractDraft
    from featuregen.overlay.upload.contract.review import validate_minimum

    dob = _col(db, "cust", "dob", concept="pii", data_type="date")
    draft = ContractDraft(
        feature_name="customer_dob_bucket", definition="d", grain_table=None,
        aggregation="latest", as_of_column=None, derives_from=[dob],
        derives_pairs=((SOURCE, dob),))

    check = validate_minimum(db, draft, now=NOW)
    assert check.ok is False
    assert check.validation_status == "REJECTED"
    assert any("personal data" in r for r in check.reasons), check.reasons


def test_a_clean_draft_still_passes_confirm_time_validation(db):
    """The other polarity on the contract path, so the test above is not passing because
    `validate_minimum` rejects everything."""
    from featuregen.overlay.upload.contract.author import ContractDraft
    from featuregen.overlay.upload.contract.review import validate_minimum

    amount = _col(db, "txn", "amt", concept="monetary_flow", currency="AED")
    draft = ContractDraft(
        feature_name="avg_amount", definition="d", grain_table=None, aggregation="avg",
        as_of_column=None, derives_from=[amount], derives_pairs=((SOURCE, amount),))

    check = validate_minimum(db, draft, now=NOW)
    assert check.ok is True and check.validation_status == "DESIGN_CHECKED"


def test_the_cross_catalog_proposal_brake_refuses_the_same_operand(db):
    """The planner path (`b_gauntlet`), which calls `_validate_idea` directly rather than through
    `_vet`. It must report the USE refusal as a gauntlet rejection carrying the code, not lose it."""
    from featuregen.overlay.upload.planner.b_gauntlet import (
        GauntletRejectionV1,
        run_gauntlet_and_preserve,
    )
    from featuregen.overlay.upload.planner.b_proposal import new_raw_proposal
    from featuregen.overlay.upload.planner.b_scope import IdentityEntryV1, IdentityMapV1

    label = _col(db, "txn", "sol_desc", concept="branch_name", data_type="text")
    outcome = run_gauntlet_and_preserve(
        db,
        proposal=new_raw_proposal(operands=(label,), operation="latest", window=None,
                                  grain_hint=None),
        identity_map=IdentityMapV1(entries=(IdentityEntryV1(label, (SOURCE,)),)),
        target_ref=None, roles=(), now=NOW, fresh_within=FRESH)

    assert isinstance(outcome, GauntletRejectionV1)
    assert outcome.reject_code == RejectCode.DESCRIPTIVE_OPERAND


@pytest.mark.parametrize("concept_name", ["pii", "protected_attribute", "branch_name"])
def test_the_menu_path_and_the_contract_path_agree_on_every_class(db, concept_name):
    """The both-consumers proof generalized: whatever `_validate_idea` refuses for the menu, the
    confirm-time re-validation refuses too. They share one implementation; this pins that they do."""
    from featuregen.overlay.upload.contract.author import ContractDraft
    from featuregen.overlay.upload.contract.review import validate_minimum

    ref = _col(db, "cust", "c", concept=concept_name, data_type="text")
    _idea, rej = _validate(db, [ref])
    assert rej is not None

    draft = ContractDraft(
        feature_name="f", definition="d", grain_table=None, aggregation="latest",
        as_of_column=None, derives_from=[ref], derives_pairs=((SOURCE, ref),))
    check = validate_minimum(db, draft, now=NOW)
    assert check.ok is False
    assert check.reasons == [rej.message], (
        "the two consumers disagree — one of them is running a different gauntlet")
