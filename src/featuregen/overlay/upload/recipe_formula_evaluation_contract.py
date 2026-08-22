"""The IDENTITY a Formula-v2/v3 evaluation is conducted under — minted here, recorded once.

▲ **WHY THIS EXISTS, and it is not bookkeeping.** `recipe_formula_eval` stamps V1's
``OPERATION_GRAMMAR_VERSION`` and ``OUTPUT_POLICY_VERSION``. Both equal ``1``. So do V2's. That is
ACCIDENTAL NUMERIC EQUALITY, not compatibility — and it means the two version numbers a run row
records cannot, on their own, tell V1 evidence from V2 evidence. Swapping an import would have
relabelled one as the other without changing a single thing about what was actually evaluated.

So the identity that distinguishes them has to be made of fields that CANNOT collide by accident:
which evaluator ran, what an expectation means under it, which wire the provider spoke, and which
reviewed material it was measured against. Those are recorded together, addressed by the hash of
their own content, in a table nothing can edit (migration 1097).

**Addressed by content, so the same identity is the same row.** Two runs under identical versions
cite one contract; one changed version mints a different hash and therefore a different row. "Did
these two runs measure the same thing" becomes a key comparison instead of a nine-column diff.

**This module does NOT decide whether a run may proceed.** It says what a run would be conducted
under. Whether the reviewed corpus is adequate to certify anything is a separate question with a
separate, governance-shaped answer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from featuregen.formula.output_authority_v2 import OUTPUT_POLICY_VERSION_V2
from featuregen.formula.schema_v3 import (
    CANONICALIZATION_VERSION_V3,
    OPERATION_GRAMMAR_VERSION_V3,
)
from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
    RECIPE_FORMULA_V2_EXPECTATIONS,
    validate_v2_expectation_registry,
)
from featuregen.overlay.upload.recipe_formula_shadow import content_hash

#: The evaluator lane itself. The ONE field no version-number coincidence can forge, which is why
#: it is a name rather than an integer.
EVALUATOR_CONTRACT_VERSION_V2 = "recipe-formula-evaluator-v2"

#: What an expectation IS under this contract: a reviewed ``gold_v2`` fixture pinned by its
#: canonical proposal sha256 — not V1's unary count-distinct blueprint.
EXPECTATION_SCHEMA_V2 = "formula-v2"

#: The wire the provider is asked to speak. SEPARATE from the language the formula is written in: a
#: V3 wire carries a V2 formula plus optional ``row_selections``, so "formula v2" and "wire v3" are
#: both true at once and neither implies the other.
FORMULA_WIRE_SCHEMA_VERSION_V3 = 3


class EvaluationContractDisagreement(RuntimeError):
    """A stored contract does not match the one being recorded under its own hash.

    Nothing legitimate produces this. The hash is derived from every identity field, so two rows
    sharing a hash and differing in a field means either the stored row was tampered with or the
    hashing changed without its version moving — and both are answered by refusing, never by
    overwriting.
    """


@dataclass(frozen=True, slots=True)
class EvaluationContractV2:
    """Everything a V2/V3 evaluation run is conducted under, and nothing else.

    Configuration — budgets, windows, who asked — deliberately stays on the run row. This is the
    part two runs can legitimately SHARE, which is exactly what makes it worth addressing by hash.
    """

    evaluator_contract_version: str
    expectation_schema: str
    formula_wire_schema_version: int
    operation_grammar_version: int
    output_policy_version: int
    canonicalization_version: int
    corpus_version: str
    corpus_content_hash: str
    expectation_registry_hash: str
    author_provider_contract_hash: str
    critic_provider_contract_hash: str

    @property
    def contract_hash(self) -> str:
        """The canonical hash of every field above — recomputable, therefore checkable.

        Not a surrogate id. A reader that recomputes this from the stored columns learns whether the
        row still says what it said when it was written, which a generated id could never tell them.
        """
        return content_hash(asdict(self))


#: Column order for the insert and the readback, written once so the two cannot drift apart.
_IDENTITY_COLUMNS = (
    "evaluator_contract_version",
    "expectation_schema",
    "formula_wire_schema_version",
    "operation_grammar_version",
    "output_policy_version",
    "canonicalization_version",
    "corpus_version",
    "corpus_content_hash",
    "expectation_registry_hash",
    "author_provider_contract_hash",
    "critic_provider_contract_hash",
)


def v2_expectation_registry_hash() -> str:
    """Hash the VALIDATED v2 expectation registry — the fixture names and their pinned hashes.

    Validated first, deliberately: hashing a registry that has not been checked would freeze a
    malformed entry into a run's identity, and the identity is the thing later evidence is compared
    against.
    """
    validate_v2_expectation_registry()
    return content_hash(
        {ref: list(entry) for ref, entry in sorted(RECIPE_FORMULA_V2_EXPECTATIONS.items())})


def current_evaluation_contract(
    *,
    corpus_version: str,
    corpus_content_hash: str,
    author_provider_contract_hash: str,
    critic_provider_contract_hash: str,
) -> EvaluationContractV2:
    """The identity THIS BUILD would evaluate under, read from the live constants.

    The version fields are imported rather than passed, so a contract cannot be minted claiming a
    grammar or canonicalization the running code does not actually implement. The corpus and the
    provider contracts are passed, because they are the caller's material and not this build's.
    """
    return EvaluationContractV2(
        evaluator_contract_version=EVALUATOR_CONTRACT_VERSION_V2,
        expectation_schema=EXPECTATION_SCHEMA_V2,
        formula_wire_schema_version=FORMULA_WIRE_SCHEMA_VERSION_V3,
        operation_grammar_version=OPERATION_GRAMMAR_VERSION_V3,
        output_policy_version=OUTPUT_POLICY_VERSION_V2,
        canonicalization_version=CANONICALIZATION_VERSION_V3,
        corpus_version=corpus_version,
        corpus_content_hash=corpus_content_hash,
        expectation_registry_hash=v2_expectation_registry_hash(),
        author_provider_contract_hash=author_provider_contract_hash,
        critic_provider_contract_hash=critic_provider_contract_hash)


def record_evaluation_contract(conn, contract: EvaluationContractV2) -> str:
    """Record this identity if it is new, and CHECK it if it is not. Returns the contract hash.

    Idempotent because two runs under the same versions legitimately share one identity — that is
    the design, not a collision. But "already present" is verified rather than assumed: the stored
    row is read back and compared field by field, so a row that drifted from its own hash is a
    refusal instead of a silently reused identity.
    """
    stored = read_evaluation_contract(conn, contract.contract_hash)
    if stored is not None:
        if stored != contract:
            raise EvaluationContractDisagreement(
                f"stored evaluation contract {contract.contract_hash} disagrees with the one being "
                f"recorded under the same hash; the identity is derived from these very fields, so "
                f"this cannot happen without the row or the hashing having changed")
        return contract.contract_hash

    columns = ("contract_hash", *_IDENTITY_COLUMNS)
    values = [contract.contract_hash] + [getattr(contract, name) for name in _IDENTITY_COLUMNS]
    conn.execute(
        f"INSERT INTO recipe_formula_evaluation_contract ({','.join(columns)}) "
        f"VALUES ({','.join(['%s'] * len(columns))}) ON CONFLICT (contract_hash) DO NOTHING",
        values)
    return contract.contract_hash


def read_evaluation_contract(conn, contract_hash: str) -> EvaluationContractV2 | None:
    """The identity a run cites, or None. Never a partially-populated contract."""
    row = conn.execute(
        f"SELECT {','.join(_IDENTITY_COLUMNS)} FROM recipe_formula_evaluation_contract "
        f"WHERE contract_hash = %s", (contract_hash,)).fetchone()
    if row is None:
        return None
    return EvaluationContractV2(**dict(zip(_IDENTITY_COLUMNS, row, strict=True)))


def verify_recorded_contract(conn, contract_hash: str) -> EvaluationContractV2:
    """Read a contract AND re-derive its hash from what was stored.

    The stronger read, for anyone about to rely on an identity rather than merely display it. A
    stored row whose fields no longer hash to the key it is filed under is not a contract to be
    used with a warning — it is evidence that something edited what it should not have.
    """
    contract = read_evaluation_contract(conn, contract_hash)
    if contract is None:
        raise EvaluationContractDisagreement(
            f"no evaluation contract {contract_hash}: a run citing an identity that is not "
            f"recorded cannot say what it measured")
    if contract.contract_hash != contract_hash:
        raise EvaluationContractDisagreement(
            f"evaluation contract {contract_hash} does not hash to its own key "
            f"(recomputed {contract.contract_hash}); its stored fields have changed")
    return contract


def contract_disagreements(
    left: EvaluationContractV2, right: EvaluationContractV2) -> tuple[str, ...]:
    """WHICH fields two identities differ on, named — for comparing runs, and for explaining why
    one body of evidence does not speak to another.

    A bare "these differ" would send a reader back to diff eleven fields by hand, which is the
    ergonomics this table was built to replace.
    """
    return tuple(
        name for name in _IDENTITY_COLUMNS if getattr(left, name) != getattr(right, name))


def with_corpus(
    contract: EvaluationContractV2, *, corpus_version: str, corpus_content_hash: str,
) -> EvaluationContractV2:
    """The same identity against different reviewed material — a NEW contract, necessarily.

    Offered as a named operation because growing the corpus is the expected way this identity moves
    (§0.5 step 5B), and it should be obvious at the call site that doing so mints a different
    contract rather than updating one.
    """
    return replace(
        contract, corpus_version=corpus_version, corpus_content_hash=corpus_content_hash)
