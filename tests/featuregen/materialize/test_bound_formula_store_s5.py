"""S5 — resolving the authored intent and persisting the bound formula (1076).

*"The refusal compares **only fields the intent records**; a compiler version bump leaves the
bound-formula hash unchanged."*

The first clause is tested as a DISCRIMINATOR, not a caution: the same disagreement is asserted to
refuse when the author stated the value and to pass when they stated nothing. A test that only
checked the silent case would pass against a resolver that compared nothing at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from featuregen.formula.canonical_v3 import proposal_content_hash_v3
from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.output_intent_v2 import (
    AuthoredOutputIntentV2,
    NumericShapeV2,
    derive_output_intent_v2,
)
from featuregen.formula.parse_v3 import parse_proposal_v3
from featuregen.formula.schema_leaves import AdditivityClass
from featuregen.formula.schema_v3 import FORMULA_SCHEMA_VERSION_V3
from featuregen.materialize.bound_formula_store import (
    BoundFormulaStoreCorrupt,
    compilations_of,
    load_executable_output,
    mismatches_for,
    record_bound_formula,
    record_executable_output,
    record_intent_mismatch,
    same_identity_compilations,
)
from featuregen.materialize.bound_formula_v2 import (
    BoundFormulaRevisionV2,
    ExecutableOutputPolicyV2,
    bound_formula_hash_v2,
    executable_output_hash_v2,
)
from featuregen.materialize.output_resolution_v2 import (
    ADVISORY_FIELDS,
    INTENT_ADDITIVITY_MISMATCH,
    INTENT_CONVERSION_MISMATCH,
    INTENT_CURRENCY_MISMATCH,
    INTENT_DESCRIBES_ANOTHER_FORMULA,
    INTENT_NUMERIC_SHAPE_MISMATCH,
    INTENT_UNIT_MISMATCH,
    STRUCTURAL_FIELDS,
    IntentMismatchV1,
    compared_fields,
    resolve_executable_output_v2,
)
from featuregen.materialize.physical_types_v2 import DecimalTypeV2

_GOLD_V2 = Path(__file__).parent.parent / "formula" / "gold_v2"
MIGRATION = Path("src/featuregen/db/migrations/1076_bound_formula_and_executable_output.sql")

DIR_REF = "direction_sign:foundation-signed-by-indicator"
FX_REF = "currency_conversion:foundation-base-currency"
ENV = "hdfc-local"
FORMULA = "sha256:formula-under-test"
SHAPE = DecimalTypeV2(precision=38, scale=6)


# ══ builders ════════════════════════════════════════════════════════════════════════════════════
def _intent(
    *, unit=None, additivity=None, target_currency=None, conversion_ref: str = "",
    present: bool = False, precision: int = 38, scale: int = 6,
    proposal_hash: str = FORMULA,
) -> AuthoredOutputIntentV2:
    return AuthoredOutputIntentV2(
        unit=unit, additivity=additivity, conversion_required=bool(conversion_ref),
        declared_conversion_ref=conversion_ref, target_currency=target_currency,
        numeric_shape=NumericShapeV2(precision=precision, scale=scale,
                                     rounding="half_even", overflow="error"),
        authored_expectation_present=present, derived_from_proposal_hash=proposal_hash)


def _declared(
    *, unit: str = "monetary", currency: str = "fixed:AED",
    additivity: AdditivityClass = AdditivityClass.ADDITIVE,
) -> FormulaOutputPolicyV2:
    return FormulaOutputPolicyV2(
        output_type="decimal", unit=unit, currency=currency, output_additivity=additivity,
        external_type_required=False)


def _resolve(intent, declared=None, *, physical_type=SHAPE, currency_code="AED",
             formula_content_hash=FORMULA, nullable=True):
    return resolve_executable_output_v2(
        intent, declared if declared is not None else _declared(),
        formula_content_hash=formula_content_hash, physical_type=physical_type,
        currency_code=currency_code, nullable=nullable)


def _policy(**overrides) -> ExecutableOutputPolicyV2:
    kwargs = dict(physical_type="DECIMAL(38,6)", unit="monetary", currency_code="AED",
                  conversion_policy_ref="", output_additivity=AdditivityClass.ADDITIVE,
                  nullable=True)
    kwargs.update(overrides)
    return ExecutableOutputPolicyV2(**kwargs)


def _revision(policy: ExecutableOutputPolicyV2, *, compiler_version: str = "formula-compiler@1",
              inputs: str = "sha256:bound-inputs") -> BoundFormulaRevisionV2:
    return BoundFormulaRevisionV2(
        formula_content_hash=FORMULA, bound_input_set_hash=inputs, environment_id=ENV,
        executable_output_hash=executable_output_hash_v2(policy),
        compiler_version=compiler_version)


def _proposal(*, expected_output=..., decimal=None):
    raw = json.loads((_GOLD_V2 / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    raw["body"]["expr"]["row_selections"] = []
    raw["body"]["expr"]["authority_refs"] = {"direction_policy_ref": DIR_REF}
    if expected_output is not ...:
        raw["expected_output"] = expected_output
    if decimal is not None:
        raw["decimal"] = decimal
    return parse_proposal_v3(raw)


# ══ ACCEPTANCE 1 — the refusal compares ONLY fields the intent RECORDS ══════════════════════════
def test_A_SILENT_INTENT_IS_NOT_JUDGED_ON_A_UNIT_IT_NEVER_STATED():
    """A deterministic run authors no `expected_output` at all. Comparing `unit` there would refuse
    a formula against a value NOBODY AUTHORED — the platform's own computation judged against a
    default."""
    resolved = _resolve(_intent(present=False), _declared(unit="monetary"))
    assert isinstance(resolved, ExecutableOutputPolicyV2)
    assert resolved.unit == "monetary"


def test_THE_SAME_DISAGREEMENT_REFUSES_ONCE_THE_AUTHOR_STATES_IT():
    """The discriminator. Same declared policy, same mismatch — the only thing that moved is whether
    the author said anything, which is exactly what the clause is about."""
    refusal = _resolve(_intent(present=True, unit="count"), _declared(unit="monetary"))
    assert isinstance(refusal, IntentMismatchV1)
    assert refusal.code == INTENT_UNIT_MISMATCH
    assert (refusal.intended, refusal.resolved) == ("count", "monetary")


def test_ADDITIVITY_IS_NEVER_COMPARED_because_the_deriver_NEVER_RECORDS_IT():
    """The sharpest case, and not hypothetical: `derive_output_intent_v2` never populates
    additivity — `ExpectedOutput` carries none and inventing a default "would put a value into the
    intent that nobody authored". So the comparison is vacuous today and would be WRONG the day it
    stopped being vacuous."""
    derived = derive_output_intent_v2(_proposal(expected_output={
        "output_type": "decimal", "unit": "monetary", "currency": "AED"}),
        proposal_hash=FORMULA)
    assert derived.additivity is None
    assert "additivity" not in compared_fields(derived)

    # A declared policy whose additivity differs from anything an author could have meant still
    # resolves, because nothing was stated to disagree with.
    resolved = resolve_executable_output_v2(
        derived, _declared(additivity=AdditivityClass.SEMI_ADDITIVE),
        formula_content_hash=FORMULA, physical_type=SHAPE, currency_code="AED", nullable=True)
    assert isinstance(resolved, ExecutableOutputPolicyV2)
    assert resolved.output_additivity is AdditivityClass.SEMI_ADDITIVE


def test_an_additivity_the_intent_DOES_record_is_compared():
    """The other side, so the skip is a rule rather than an omission."""
    refusal = _resolve(
        _intent(present=True, additivity=AdditivityClass.ADDITIVE),
        _declared(additivity=AdditivityClass.NON_ADDITIVE))
    assert isinstance(refusal, IntentMismatchV1)
    assert refusal.code == INTENT_ADDITIVITY_MISMATCH


def test_a_SILENT_intent_is_not_judged_on_a_TARGET_CURRENCY_either():
    resolved = _resolve(_intent(present=False), currency_code="USD")
    assert isinstance(resolved, ExecutableOutputPolicyV2)
    assert resolved.currency_code == "USD"


def test_a_STATED_target_currency_IS_judged():
    refusal = _resolve(_intent(present=True, target_currency="AED"), currency_code="USD")
    assert isinstance(refusal, IntentMismatchV1)
    assert refusal.code == INTENT_CURRENCY_MISMATCH
    assert (refusal.intended, refusal.resolved) == ("AED", "USD")


@pytest.mark.parametrize("field", STRUCTURAL_FIELDS)
def test_STRUCTURAL_fields_are_compared_EVEN_when_the_author_stated_nothing(field):
    """The complement of the clause: structural fields come from the formula's own shape, are always
    present, and are therefore always in scope — including for a run that authored no expectation."""
    assert field in compared_fields(_intent(present=False))


@pytest.mark.parametrize("name", ADVISORY_FIELDS)
def test_ADVISORY_fields_are_in_scope_ONLY_when_recorded(name):
    silent = _intent(present=False)
    assert name not in compared_fields(silent)

    stated = _intent(present=True, unit="monetary", additivity=AdditivityClass.ADDITIVE,
                     target_currency="AED")
    assert name in compared_fields(stated)


def test_the_two_field_sets_are_DISJOINT_and_cover_the_intent():
    """A field in neither set is one nothing decided about — the shape of a rule with a hole."""
    assert not set(ADVISORY_FIELDS) & set(STRUCTURAL_FIELDS)
    judged = set(ADVISORY_FIELDS) | set(STRUCTURAL_FIELDS)
    # `authored_expectation_present` is what SELECTS the scope and `derived_from_proposal_hash` is
    # checked before any comparison — neither is a value to compare.
    assert judged == {"unit", "additivity", "target_currency",
                      "conversion_required", "declared_conversion_ref", "numeric_shape"}


# ── structural comparisons, which are never skipped ─────────────────────────────────────────────
def test_a_DECLARED_CONVERSION_the_output_did_not_perform_refuses():
    refusal = _resolve(_intent(conversion_ref=FX_REF), _declared(currency="fixed:AED"))
    assert isinstance(refusal, IntentMismatchV1)
    assert refusal.code == INTENT_CONVERSION_MISMATCH
    assert refusal.field == "conversion_required"


def test_a_DIFFERENT_conversion_policy_refuses():
    """Two rate policies are two different numbers, and the difference is not visible in the
    result."""
    refusal = _resolve(_intent(conversion_ref=FX_REF),
                       _declared(currency="converted:currency_conversion:some-other-policy"))
    assert isinstance(refusal, IntentMismatchV1)
    assert refusal.field == "declared_conversion_ref"


def test_a_matching_conversion_RESOLVES_and_keeps_the_ref_out_of_the_currency_code():
    """`FormulaOutputPolicyV2.currency` holds `'converted:<ref>'` — a DECLARATION. Lifting that into
    `currency_code` would make the answer to "what currency is this column in" a policy
    reference."""
    resolved = _resolve(_intent(conversion_ref=FX_REF),
                        _declared(currency=f"converted:{FX_REF}"), currency_code="AED")
    assert isinstance(resolved, ExecutableOutputPolicyV2)
    assert resolved.currency_code == "AED"
    assert resolved.conversion_policy_ref == FX_REF
    assert resolved.was_converted is True


def test_a_NUMERIC_SHAPE_the_arithmetic_does_not_produce_refuses():
    """Silently taking the produced type is how a declared DECIMAL(38,2) starts rounding differently
    in its last places; taking the declared one truncates a value that does not fit."""
    refusal = _resolve(_intent(precision=38, scale=2), physical_type=DecimalTypeV2(38, 6))
    assert isinstance(refusal, IntentMismatchV1)
    assert refusal.code == INTENT_NUMERIC_SHAPE_MISMATCH
    assert (refusal.intended, refusal.resolved) == ("DECIMAL(38,2)", "DECIMAL(38,6)")


def test_AN_INTENT_FROM_ANOTHER_FORMULA_IS_CAUGHT_BEFORE_ANY_COMPARISON():
    """`derived_from_proposal_hash` exists so "an intent that travelled away from its formula can be
    caught instead of trusted" — and nothing checked it until here. Checked FIRST, because every
    later disagreement would otherwise be attributed to an author who never saw this formula."""
    refusal = _resolve(_intent(present=True, unit="count", proposal_hash="sha256:another-formula"),
                       _declared(unit="monetary"))
    assert isinstance(refusal, IntentMismatchV1)
    assert refusal.code == INTENT_DESCRIBES_ANOTHER_FORMULA


# ══ ACCEPTANCE 2 — a COMPILER VERSION BUMP leaves the bound-formula hash unchanged ══════════════
def test_A_COMPILER_BUMP_DOES_NOT_MOVE_THE_HASH():
    """The property, at the type. Recompiling the same formula against the same inputs is one bound
    revision; letting the toolchain into identity would invalidate every downstream pin on a bump
    that changed nothing about the computation."""
    policy = _policy()
    assert bound_formula_hash_v2(_revision(policy, compiler_version="formula-compiler@1")) == (
        bound_formula_hash_v2(_revision(policy, compiler_version="formula-compiler@9")))


def test_THE_DATABASE_AGREES_one_revision_two_compilations(db):
    """The property, at the store — where it stops being a dataclass detail. Both compilations are
    recorded and the record that a second compiler produced it is NOT lost."""
    policy = _policy()
    first = record_bound_formula(db, _revision(policy, compiler_version="formula-compiler@1"),
                                 policy, compilation_id="cmp-1", compiled_at="2026-08-17T00:00:00Z")
    second = record_bound_formula(db, _revision(policy, compiler_version="formula-compiler@9"),
                                  policy, compilation_id="cmp-2",
                                  compiled_at="2026-12-25T09:30:00Z")

    assert first == second
    assert db.execute("SELECT count(*) FROM bound_formula_revision").fetchone()[0] == 1
    assert same_identity_compilations(
        db, _revision(policy)) == ("formula-compiler@1", "formula-compiler@9")


def test_the_revision_table_carries_NO_COMPILER_COLUMN(db):
    """Not "we do not write it" — there is nowhere to write it. A column would be one migration away
    from becoming part of what a downstream pin compares."""
    columns = {row[0] for row in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        ("bound_formula_revision",)).fetchall()}
    assert "compiler_version" not in columns
    assert columns == {"revision_id", "formula_content_hash", "bound_input_set_hash",
                       "environment_id", "executable_output_hash", "recorded_at"}


def test_a_CHANGE_THAT_MATTERS_does_move_the_hash(db):
    """The other side, so the stability above is a rule and not an inability to tell things apart.
    Different bound inputs are a different computation."""
    policy = _policy()
    record_bound_formula(db, _revision(policy), policy, compilation_id="cmp-1",
                         compiled_at="t1")
    record_bound_formula(db, _revision(policy, inputs="sha256:other-inputs"), policy,
                         compilation_id="cmp-2", compiled_at="t2")
    assert db.execute("SELECT count(*) FROM bound_formula_revision").fetchone()[0] == 2


def test_a_different_OUTPUT_POLICY_is_a_different_revision(db):
    policy = _policy()
    other = _policy(nullable=False)
    record_bound_formula(db, _revision(policy), policy, compilation_id="cmp-1", compiled_at="t1")
    record_bound_formula(db, _revision(other), other, compilation_id="cmp-2", compiled_at="t2")
    assert db.execute("SELECT count(*) FROM bound_formula_revision").fetchone()[0] == 2


# ══ the store's own rules ═══════════════════════════════════════════════════════════════════════
def test_a_revision_CANNOT_be_filed_against_a_policy_it_does_not_describe(db):
    policy = _policy()
    with pytest.raises(ValueError, match="does not describe"):
        record_bound_formula(db, _revision(policy), _policy(currency_code="USD"),
                             compilation_id="cmp-1", compiled_at="t1")


def test_the_output_policy_is_written_BEFORE_the_revision_that_names_it(db):
    """The foreign key holds even against a caller that bypasses the writer."""
    policy = _policy()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO bound_formula_revision (revision_id, formula_content_hash, "
            "bound_input_set_hash, environment_id, executable_output_hash) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("rev-x", FORMULA, "sha256:inputs", ENV, executable_output_hash_v2(policy)))


def test_an_output_policy_reads_back_content_verified(db):
    policy = _policy(conversion_policy_ref=FX_REF, currency_code="AED")
    output_hash = record_executable_output(db, policy)
    assert load_executable_output(db, output_hash) == policy


def test_a_CORRUPTED_output_policy_is_surfaced_rather_than_served(db):
    """A row that cannot reproduce its own hash would be served as the description of a column it
    no longer describes."""
    db.execute(
        "INSERT INTO executable_output_policy (output_hash, physical_type, unit, currency_code, "
        "conversion_policy_ref, output_additivity, nullable, physical_type_policy) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        ("sha256:not-its-own-hash", "DECIMAL(38,6)", "monetary", "AED", "", "additive", True,
         "formula-v2/physical-types@1"))
    with pytest.raises(BoundFormulaStoreCorrupt, match="no longer describes"):
        load_executable_output(db, "sha256:not-its-own-hash")


def test_the_DATABASE_refuses_a_declaration_in_the_currency_code(db):
    """The type refuses `'converted:...'` in memory and the schema refuses it too, because one of
    them being absent is how the other gets removed."""
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO executable_output_policy (output_hash, physical_type, unit, "
            "currency_code, conversion_policy_ref, output_additivity, nullable, "
            "physical_type_policy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            ("sha256:x", "DECIMAL(38,6)", "monetary", f"converted:{FX_REF}", "", "additive", True,
             "formula-v2/physical-types@1"))


def test_the_DATABASE_refuses_a_monetary_column_with_no_currency(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO executable_output_policy (output_hash, physical_type, unit, "
            "currency_code, conversion_policy_ref, output_additivity, nullable, "
            "physical_type_policy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            ("sha256:y", "DECIMAL(38,6)", "monetary", "", "", "additive", True,
             "formula-v2/physical-types@1"))


@pytest.mark.parametrize("table,column,key,identifier", [
    ("executable_output_policy", "physical_type", "output_hash", None),
    ("bound_formula_revision", "environment_id", "revision_id", None),
    ("bound_formula_compilation", "compiler_version", "compilation_id", "cmp-1"),
])
def test_everything_here_is_APPEND_ONLY(db, table, column, key, identifier):
    policy = _policy()
    revision_id = record_bound_formula(db, _revision(policy), policy, compilation_id="cmp-1",
                                       compiled_at="t1")
    target = identifier or (executable_output_hash_v2(policy)
                            if key == "output_hash" else revision_id)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(f"UPDATE {table} SET {column} = %s WHERE {key} = %s", ("rewritten", target))


# ══ mismatches are RECORDED with the scope they were judged under ═══════════════════════════════
def test_A_MISMATCH_IS_STORED_WITH_THE_FIELDS_THAT_WERE_IN_SCOPE(db):
    """S5's acceptance is a claim ABOUT the comparison scope, so a stored mismatch that did not
    carry it could not be checked against the rule afterwards — and that rule is exactly the thing
    most likely to be quietly widened later."""
    intent = _intent(present=True, unit="count")
    refusal = _resolve(intent, _declared(unit="monetary"))
    assert isinstance(refusal, IntentMismatchV1)
    record_intent_mismatch(db, refusal, intent, mismatch_id="mis-1",
                           formula_content_hash=FORMULA, environment_id=ENV)

    stored = mismatches_for(db, FORMULA)
    assert len(stored) == 1
    code, field, scope = stored[0]
    assert (code, field) == (INTENT_UNIT_MISMATCH, "unit")
    assert "unit" in scope
    assert "additivity" not in scope        # never recorded, so never in scope
    assert set(STRUCTURAL_FIELDS) <= set(scope)


def test_the_scope_is_DERIVED_from_the_intent_not_supplied(db):
    """A stored mismatch cannot claim a comparison scope the intent did not have."""
    import inspect

    parameters = inspect.signature(record_intent_mismatch).parameters
    assert "compared_fields" not in parameters
    assert "intent" in parameters


def test_a_silent_intents_mismatch_records_the_NARROWER_scope(db):
    """The stored evidence of the clause: the same structural refusal, judged under a scope that
    contains no advisory field at all."""
    intent = _intent(present=False, precision=38, scale=2)
    refusal = _resolve(intent, physical_type=DecimalTypeV2(38, 6))
    assert isinstance(refusal, IntentMismatchV1)
    record_intent_mismatch(db, refusal, intent, mismatch_id="mis-1",
                           formula_content_hash=FORMULA, environment_id=ENV)

    _code, _field, scope = mismatches_for(db, FORMULA)[0]
    assert set(scope) == set(STRUCTURAL_FIELDS)


def test_a_recorded_mismatch_cannot_be_REWRITTEN(db):
    intent = _intent(present=True, unit="count")
    refusal = _resolve(intent, _declared(unit="monetary"))
    assert isinstance(refusal, IntentMismatchV1)
    record_intent_mismatch(db, refusal, intent, mismatch_id="mis-1",
                           formula_content_hash=FORMULA, environment_id=ENV)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE output_intent_mismatch SET code = %s WHERE mismatch_id = %s",
                   ("SOMETHING_ELSE", "mis-1"))


# ══ the real deriver, end to end ════════════════════════════════════════════════════════════════
def test_a_REAL_DERIVED_intent_resolves_against_a_real_declared_policy(db):
    """Not a hand-built intent: derived from a parsed V3 proposal, so the field-by-field skip rule
    is exercised against what the deriver actually produces."""
    proposal = _proposal(expected_output=None)
    intent = derive_output_intent_v2(
        proposal, proposal_hash=proposal_content_hash_v3(proposal))
    assert intent.authored_expectation_present is False

    resolved = resolve_executable_output_v2(
        intent, _declared(unit="monetary", currency="fixed:AED"),
        formula_content_hash=proposal_content_hash_v3(proposal),
        physical_type=DecimalTypeV2(precision=intent.numeric_shape.precision,
                                    scale=intent.numeric_shape.scale),
        currency_code="AED", nullable=True)
    assert isinstance(resolved, ExecutableOutputPolicyV2)

    revision = BoundFormulaRevisionV2(
        formula_content_hash=proposal_content_hash_v3(proposal),
        bound_input_set_hash="sha256:bound-inputs", environment_id=ENV,
        executable_output_hash=executable_output_hash_v2(resolved),
        compiler_version="formula-compiler@1")
    revision_id = record_bound_formula(db, revision, resolved, compilation_id="cmp-1",
                                       compiled_at="2026-08-17T00:00:00Z")
    assert compilations_of(db, revision_id) == (
        ("cmp-1", "formula-compiler@1", "2026-08-17T00:00:00Z"),)


def test_the_migration_states_the_split_that_makes_the_acceptance_true():
    sql = MIGRATION.read_text()
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "CREATE TABLE IF NOT EXISTS bound_formula_compilation" in statements
    assert "compiler_version" in statements.split("bound_formula_compilation", 1)[1]
    assert "compiler_version" not in statements.split("bound_formula_revision", 1)[1].split(
        "bound_formula_compilation", 1)[0]
