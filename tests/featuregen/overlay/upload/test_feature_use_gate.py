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
    _mixes_currency_values,
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
    disguised = _col(db, "txn", "fld_017", concept="code_label", data_type="text")

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
    """`relationship_manager_name` is BOTH personal data and the label beside an id. The reviewer
    must be told the thing no policy can fix, not sent to a governance owner who could never help."""
    concept_name = "relationship_manager_name"
    assert is_personal_data(concept_name) and is_descriptive(concept_name)
    name = _col(db, "cust", "rm_nm", concept=concept_name, data_type="text")
    _idea, rej = _validate(db, [name])
    assert rej.code == RejectCode.DESCRIPTIVE_OPERAND


def test_a_payment_narration_is_a_POLICY_refusal_and_is_never_sent_to_a_code_column(db):
    """The `text` GROUP SWEEP, undone — the review finding this test exists for.

    `DESCRIPTIVE_GROUPS = {"text"}` pulled six concepts into structurally_unsuitable without ever
    asking them the question `descriptive` asks. `payment_narrative` is the sharpest case: the
    registry's OWN description calls it "the single richest signal in transaction data — it drives
    categorisation, merchant identification and AML screening", and the sweep answered a reviewer
    with "no approval can ever help; use the CODE column beside it" — about a narration, which has
    no code column beside it and which an AML policy is exactly what would unblock.
    """
    assert "AML screening" in CONCEPT_REGISTRY["payment_narrative"].description, (
        "this test is anchored on the registry's own words; they changed")

    narration = _col(db, "txn", "tran_rmks", concept="payment_narrative", data_type="text")
    idea, rej = _validate(db, [narration])

    assert idea is None
    assert rej.code == RejectCode.PERSONAL_DATA_POLICY_REQUIRED
    assert refusal_family(rej.code) == NEEDS_SETUP
    assert "personal-data use policy" in rej.message
    assert "governance owner must declare one" in rej.message
    assert "CODE column" not in rej.message, (
        "a narration has no code column beside it — the refusal named a fix that cannot exist")


@pytest.mark.parametrize("concept_name", [
    "payment_narrative", "free_text", "kyc_narrative", "unstructured_doc", "record_author"])
def test_every_PII_text_concept_is_a_policy_question_not_a_structural_one(db, concept_name):
    """The other four members of the old sweep, adjudicated the same way and for the same reason:
    each is computable text that carries personal data, so the answer is a policy with an owner."""
    assert CONCEPT_REGISTRY[concept_name].group == "text"
    assert not is_descriptive(concept_name), (
        f"{concept_name} is prose, not the label beside a code — it must not claim to be")

    ref = _col(db, "cust", f"c_{concept_name}", concept=concept_name, data_type="text")
    _idea, rej = _validate(db, [ref])
    assert rej.code == RejectCode.PERSONAL_DATA_POLICY_REQUIRED, concept_name


def test_the_one_text_concept_with_neither_property_is_left_alone(db):
    """`document_reference` — "Reference/pointer to a stored document". No declared sensitivity, no
    label-beside-a-code semantics, and counting documents per customer is an ordinary feature. The
    sweep refused it structurally; the criterion says the gate has nothing to say about it."""
    record = CONCEPT_REGISTRY["document_reference"]
    assert record.group == "text" and record.sensitivity == "public" and not record.descriptive

    ref = _col(db, "cust", "kyc_doc_ref", concept="document_reference", data_type="text")
    idea, rej = _validate(db, [ref], aggregation="count")
    assert rej is None and idea is not None, f"refused: {rej and rej.message}"


def test_no_text_concept_claims_to_be_the_label_beside_a_code(db):
    """The group sweep is gone, stated as a property rather than as six cases: `descriptive` is a
    per-concept self-declaration, nothing in the `text` group makes it, and — the docstring's own
    claim — every concept that DOES make it says so in its description."""
    assert [c.name for c in CONCEPT_REGISTRY.values()
            if c.group == "text" and c.descriptive] == []
    assert {c.name for c in CONCEPT_REGISTRY.values() if c.descriptive} == {
        "branch_name", "relationship_manager_name", "merchant_name", "account_name",
        "instrument_name", "counterparty_name", "code_label"}
    for c in CONCEPT_REGISTRY.values():
        if c.descriptive:
            assert "label beside" in c.description or "description of a coded value" in \
                c.description, f"{c.name} sets `descriptive` without saying so in its description"


def test_a_name_with_a_documented_computable_use_is_a_POLICY_refusal_not_a_structural_one(db):
    """The adjudication that sharpened `descriptive`, kept as a test.

    `beneficiary_name` is a name, but the registry documents it as the MATCH INPUT of the §A9
    own-transfer recipe, and `postal_address` as generalising to a region or distance feature. A
    structural refusal would tell a reviewer "no approval can ever help" about features an approval
    is exactly what unblocks. They are personal data — a policy question with a policy answer.
    """
    for concept_name in ("beneficiary_name", "postal_address", "party_name"):
        assert not is_descriptive(concept_name), concept_name
        ref = _col(db, "cust", f"c_{concept_name}", concept=concept_name, data_type="text")
        _idea, rej = _validate(db, [ref])
        assert rej.code == RejectCode.PERSONAL_DATA_POLICY_REQUIRED, concept_name


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


@pytest.mark.parametrize("aggregation", ["count", "count_distinct", "distinct_count"])
def test_COUNTING_an_amount_is_currency_agnostic_and_is_not_refused(db, aggregation):
    """How many is the same number in dollars and in fils. The refusal says "the result would
    silently mix currencies", which is simply false about a count, and it hands the reviewer a
    setup task that could not change their answer."""
    amount = _col(db, "txn", "amt", concept="monetary_flow")
    _currency = _col(db, "txn", "crncy", concept="currency_code", data_type="text")

    idea, rej = _validate(db, [amount], aggregation=aggregation)
    assert rej is None and idea is not None, f"{aggregation}: {rej and rej.message}"


@pytest.mark.parametrize("aggregation", ["sum", "avg", "max", "min", "latest", "sum_90d",
                                         "mean", "median", "not_a_known_operation"])
def test_every_VALUE_returning_aggregation_stays_gated(db, aggregation):
    """The other polarity, including the two adjudications that are not obvious.

    `max` / `min` / `latest` PICK rather than combine — and they still hand back a number
    denominated in something the caller was never told, so the wrong-number problem moves one row
    along rather than disappearing. And an UNRECOGNISED aggregation is gated: it is free text from a
    model, so the exemption has to be EARNED by matching a counting word, never granted by failing
    to match a value-mixing one.
    """
    amount = _col(db, "txn", "amt", concept="monetary_flow")
    currency = _col(db, "txn", "crncy", concept="currency_code", data_type="text")

    _idea, rej = _validate(db, [amount], aggregation=aggregation)
    assert rej is not None and rej.code == RejectCode.CURRENCY_POLICY_REQUIRED, aggregation
    assert currency in rej.message


@pytest.mark.parametrize("aggregation", ["count_90d", "COUNT_DISTINCT_30d", "count 12 months"])
def test_a_WINDOWED_count_is_still_a_count_for_this_class(db, aggregation):
    """A window narrows WHICH rows are counted and never makes counting currency-sensitive, so a
    trailing window is stripped before the operation is recognised.

    Asserted as "not THIS refusal" rather than "accepted", because a windowed aggregation trips the
    pre-existing point-in-time gate on a table with no as-of column — a different gate, correctly
    firing, that this slice did not touch and must not be credited with.
    """
    amount = _col(db, "txn", "amt", concept="monetary_flow")
    _currency = _col(db, "txn", "crncy", concept="currency_code", data_type="text")

    _idea, rej = _validate(db, [amount], aggregation=aggregation)
    assert rej is None or rej.code != RejectCode.CURRENCY_POLICY_REQUIRED, aggregation


def test_the_counting_predicate_reads_the_operation_and_fails_closed_on_the_unknown(db):
    """The exemption as a unit, so the window forms and the fail-closed default are stated where
    the point-in-time gate cannot obscure them."""
    for counting in ("count", "count_distinct", "COUNT", "count_90d", "count_distinct_30d",
                     "distinct_count", "count 12 months"):
        assert not _mixes_currency_values(counting), counting
    for mixing in ("sum", "avg", "max", "min", "latest", "sum_90d", "median", "amount",
                   "count_ratio", "discount_avg", "", None, "who_knows"):
        assert _mixes_currency_values(mixing), mixing


def test_the_counting_exemption_does_not_leak_into_the_other_three_classes(db):
    """A count is currency-agnostic; it is not PII-agnostic, protected-characteristic-agnostic or
    prose-agnostic. Counting distinct citizenships is exactly as unusable as averaging them."""
    protected = _col(db, "cust", "ctzn_ctry_cd", concept="protected_attribute", data_type="text")
    dob = _col(db, "cust", "dob", concept="pii", data_type="date")
    label = _col(db, "txn", "sol_desc", concept="branch_name", data_type="text")

    for ref, code in ((protected, RejectCode.PROTECTED_CHARACTERISTIC),
                      (dob, RejectCode.PERSONAL_DATA_POLICY_REQUIRED),
                      (label, RejectCode.DESCRIPTIVE_OPERAND)):
        _idea, rej = _validate(db, [ref], aggregation="count_distinct")
        assert rej is not None and rej.code == code, ref


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
    assert is_descriptive("branch_name") and is_descriptive("code_label")
    assert not is_descriptive("branch_id")
    assert not is_descriptive("free_text"), (
        "`descriptive` means the label beside a CODE; a memo is not that")
    assert not is_descriptive(None) and not is_descriptive("not_a_concept")


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
