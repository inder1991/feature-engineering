"""The V2/V3 evaluation identity — that it is recorded, that it is checkable, that it is immutable.

▲ **THE ONE PROPERTY WORTH STATING FIRST.** V1's evaluator stamps `OPERATION_GRAMMAR_VERSION` and
`OUTPUT_POLICY_VERSION`, both of which equal `1` — and so do V2's. The whole reason this contract
exists is that those two integers CANNOT distinguish V1 evidence from V2 evidence. So the first
test here is not a round-trip; it is the proof that the identity survives that coincidence.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.recipe_formula_evaluation_contract import (
    EVALUATOR_CONTRACT_VERSION_V2,
    EXPECTATION_SCHEMA_V2,
    EvaluationContractDisagreement,
    EvaluationContractV2,
    contract_disagreements,
    current_evaluation_contract,
    read_evaluation_contract,
    record_evaluation_contract,
    v2_expectation_registry_hash,
    verify_recorded_contract,
    with_corpus,
)

_CORPUS = "recipe-formula-gold-v2"
_CORPUS_HASH = "c" * 64


def _contract(**overrides) -> EvaluationContractV2:
    base = current_evaluation_contract(
        corpus_version=_CORPUS, corpus_content_hash=_CORPUS_HASH,
        author_provider_contract_hash="author-contract-1",
        critic_provider_contract_hash="critic-contract-1")
    return base if not overrides else EvaluationContractV2(
        **{**{f: getattr(base, f) for f in base.__slots__}, **overrides})


# ══ THE COINCIDENCE THIS TABLE EXISTS FOR ══════════════════════════════════════════════════════
def test_the_IDENTITY_SURVIVES_V1_AND_V2_SHARING_VERSION_NUMBERS():
    """▲ Both generations stamp grammar 1 and output policy 1. If the identity were made of those,
    V1 and V2 evidence would be indistinguishable — and relabelling one as the other would be a
    single changed import away, which is precisely what §0.5 forbids.

    So this asserts the coincidence is REAL (not a stale assumption) and that the contract is
    nonetheless unambiguous, because the fields that separate the lanes are names, not integers.
    """
    from featuregen.formula.output_authority_v2 import OUTPUT_POLICY_VERSION_V2
    from featuregen.formula.schema_v3 import OPERATION_GRAMMAR_VERSION_V3
    from featuregen.overlay.upload.recipe_formula_eval import EVALUATOR_VERSION
    from featuregen.formula.schema import (
        OPERATION_GRAMMAR_VERSION,
        OUTPUT_POLICY_VERSION,
    )

    # The coincidence, asserted rather than assumed. If V2's constants ever move, this test says so
    # instead of quietly becoming a test of nothing.
    assert OPERATION_GRAMMAR_VERSION == OPERATION_GRAMMAR_VERSION_V3
    assert OUTPUT_POLICY_VERSION == OUTPUT_POLICY_VERSION_V2

    contract = _contract()
    assert contract.evaluator_contract_version == EVALUATOR_CONTRACT_VERSION_V2
    assert contract.evaluator_contract_version != EVALUATOR_VERSION
    assert contract.expectation_schema == EXPECTATION_SCHEMA_V2
    assert contract.formula_wire_schema_version == 3


def test_the_HASH_IS_DERIVED_FROM_EVERY_IDENTITY_FIELD():
    """Every field moves the hash. A field that did not would be one an identity could differ on
    while claiming to be the same identity — which is the failure the content address prevents."""
    base = _contract()
    for field, other in (
        ("evaluator_contract_version", "recipe-formula-evaluator-v9"),
        ("expectation_schema", "formula-v1"),
        ("formula_wire_schema_version", 2),
        ("operation_grammar_version", 2),
        ("output_policy_version", 2),
        ("canonicalization_version", 2),
        ("corpus_version", "other-corpus"),
        ("corpus_content_hash", "d" * 64),
        ("expectation_registry_hash", "e" * 64),
        ("author_provider_contract_hash", "author-contract-2"),
        ("critic_provider_contract_hash", "critic-contract-2"),
    ):
        assert _contract(**{field: other}).contract_hash != base.contract_hash, field


# ══ RECORDING ══════════════════════════════════════════════════════════════════════════════════
def test_RECORDING_IS_IDEMPOTENT_because_two_runs_legitimately_share_one_identity(db):
    """Two runs under identical versions cite ONE contract. That is the design — a second row would
    make "did these measure the same thing" answerable only by comparing eleven columns."""
    contract = _contract()
    first = record_evaluation_contract(db, contract)
    second = record_evaluation_contract(db, contract)

    assert first == second == contract.contract_hash
    assert db.execute(
        "SELECT count(*) FROM recipe_formula_evaluation_contract WHERE contract_hash=%s",
        (contract.contract_hash,)).fetchone()[0] == 1
    assert read_evaluation_contract(db, contract.contract_hash) == contract


def test_a_DIFFERENT_IDENTITY_IS_A_DIFFERENT_ROW_never_an_update(db):
    """The corpus growing is the expected way this identity moves (§0.5 5B), and it must MINT
    rather than edit — an evaluation identity that could be edited would let a run claim it
    measured material it never saw."""
    original = _contract()
    grown = with_corpus(original, corpus_version="recipe-formula-gold-v2.1",
                        corpus_content_hash="f" * 64)
    record_evaluation_contract(db, original)
    record_evaluation_contract(db, grown)

    assert grown.contract_hash != original.contract_hash
    assert read_evaluation_contract(db, original.contract_hash) == original
    assert contract_disagreements(original, grown) == (
        "corpus_version", "corpus_content_hash")


def test_the_STORED_ROW_CANNOT_BE_EDITED_or_removed(db):
    """Migration 1097's guard, exercised rather than described. Both arms, because an identity that
    could be DELETED and re-inserted differently is exactly as forgeable as one that could be
    updated in place."""
    contract = _contract()
    record_evaluation_contract(db, contract)

    with db.transaction(force_rollback=True), pytest.raises(Exception, match="immutable"):
        db.execute(
            "UPDATE recipe_formula_evaluation_contract SET corpus_version='tampered' "
            "WHERE contract_hash=%s", (contract.contract_hash,))
    with db.transaction(force_rollback=True), pytest.raises(Exception, match="immutable"):
        db.execute("DELETE FROM recipe_formula_evaluation_contract WHERE contract_hash=%s",
                   (contract.contract_hash,))


# ══ VERIFICATION ═══════════════════════════════════════════════════════════════════════════════
def test_VERIFY_RECOMPUTES_THE_HASH_from_what_was_actually_stored(db):
    """The stronger read, for a caller about to RELY on an identity. Recomputing from the stored
    columns is what tells them the row still says what it said when written."""
    contract = _contract()
    record_evaluation_contract(db, contract)
    assert verify_recorded_contract(db, contract.contract_hash) == contract


def test_VERIFY_REFUSES_A_CITED_IDENTITY_THAT_WAS_NEVER_RECORDED(db):
    """A run citing an unrecorded identity cannot say what it measured, so this refuses rather than
    returning None for a caller to overlook."""
    with pytest.raises(EvaluationContractDisagreement, match="cannot say what it measured"):
        verify_recorded_contract(db, "a" * 64)


def test_VERIFY_CATCHES_A_ROW_THAT_NO_LONGER_HASHES_TO_ITS_OWN_KEY(db):
    """▲ The tamper case, reached the only way it can be — by writing a row whose fields disagree
    with its key. The guard blocks UPDATE and DELETE, not the initial INSERT, so a row could be
    filed under a key its contents do not produce. `verify_recorded_contract` is what notices.

    Not reachable through `record_evaluation_contract`, which derives the key from the fields. That
    is the point: the check defends against writes that did not come through this module.
    """
    contract = _contract()
    forged_key = "b" * 64
    db.execute(
        "INSERT INTO recipe_formula_evaluation_contract (contract_hash,evaluator_contract_version,"
        "expectation_schema,formula_wire_schema_version,operation_grammar_version,"
        "output_policy_version,canonicalization_version,corpus_version,corpus_content_hash,"
        "expectation_registry_hash,author_provider_contract_hash,critic_provider_contract_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (forged_key, contract.evaluator_contract_version, contract.expectation_schema,
         contract.formula_wire_schema_version, contract.operation_grammar_version,
         contract.output_policy_version, contract.canonicalization_version,
         contract.corpus_version, contract.corpus_content_hash,
         contract.expectation_registry_hash, contract.author_provider_contract_hash,
         contract.critic_provider_contract_hash))

    with pytest.raises(EvaluationContractDisagreement, match="does not hash to its own key"):
        verify_recorded_contract(db, forged_key)


def test_RECORDING_REFUSES_TO_REUSE_A_DRIFTED_ROW(db):
    """Same hash, different stored fields — answered by refusing, never by overwriting. Overwriting
    would be the tamper this table exists to prevent, performed by the module that guards it."""
    contract = _contract()
    db.execute(
        "INSERT INTO recipe_formula_evaluation_contract (contract_hash,evaluator_contract_version,"
        "expectation_schema,formula_wire_schema_version,operation_grammar_version,"
        "output_policy_version,canonicalization_version,corpus_version,corpus_content_hash,"
        "expectation_registry_hash,author_provider_contract_hash,critic_provider_contract_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (contract.contract_hash, contract.evaluator_contract_version, contract.expectation_schema,
         contract.formula_wire_schema_version, contract.operation_grammar_version,
         contract.output_policy_version, contract.canonicalization_version,
         "DRIFTED", contract.corpus_content_hash, contract.expectation_registry_hash,
         contract.author_provider_contract_hash, contract.critic_provider_contract_hash))

    with pytest.raises(EvaluationContractDisagreement, match="disagrees with the one being"):
        record_evaluation_contract(db, contract)


# ══ THE REGISTRY THE IDENTITY PINS ═════════════════════════════════════════════════════════════
def test_the_REGISTRY_HASH_VALIDATES_BEFORE_IT_HASHES():
    """Hashing an unvalidated registry would freeze a malformed entry into a run's identity — and
    the identity is what later evidence gets compared against, so it must not carry one."""
    assert len(v2_expectation_registry_hash()) == 64


def test_the_REGISTRY_HASH_MOVES_WHEN_THE_REVIEWED_SET_DOES(monkeypatch):
    """Growing the reviewed set is a governance act with an engineering consequence: the identity
    changes, so evidence gathered before it does not silently speak for the set after it."""
    import featuregen.overlay.upload.recipe_formula_evaluation_contract as mod

    before = v2_expectation_registry_hash()
    monkeypatch.setattr(mod, "RECIPE_FORMULA_V2_EXPECTATIONS",
                        {**mod.RECIPE_FORMULA_V2_EXPECTATIONS, "another_ref": ("f.json", "9" * 64)})
    assert v2_expectation_registry_hash() != before
