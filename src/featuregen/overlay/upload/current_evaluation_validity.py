"""Is there a PASSING evaluation for this expectation, produced under the world that is current now?

▲ **THE READER `_gold_evaluation_recorded` NAMED AND DID NOT HAVE.** That function returned a
hardcoded `False` with a docstring saying why: not because nothing records evaluation outcomes —
migration 1029 has done so for a long time — but because *"a passing artifact only counts if it was
produced under the world that is current now"*, and no reader checked that. **A stale pass is not a
pass**, and returning `True` for one would launder an old verdict into a present authority.

**WHAT "THE WORLD THAT IS CURRENT NOW" MEANS, exactly.** It is the evaluation contract (migration
1097) — the grammar, output-policy and canonicalization versions, the reviewed corpus and its
content hash, the expectation registry, and the byte-frozen author and critic provider contracts.
Every one of those is derivable from this build, so the comparison is a single hash rather than a
judgement: an evaluation whose contract hash equals the one this build would mint measured the
current world, and one whose hash differs measured a different one.

▲ **AND CODE REVISION IS DELIBERATELY NOT A VALIDITY CONDITION.** Requiring `code_commit` equality
would invalidate every evaluation on every commit, making validity unmaintainable in practice — so
it is REPORTED beside the verdict rather than folded into it. That places a real obligation
elsewhere: a behaviour change must move a version constant. If behaviour can change while every
version stays put, this reader will call a stale evaluation current, and the defect is in the
constant that failed to move, not here. Stated rather than left implicit, because an unstated
assumption of that size is how laundered verdicts happen.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.recipe_formula_evaluation_contract import EvaluationContractV2


@dataclass(frozen=True, slots=True)
class EvaluationValidityV1:
    """What is known about this expectation's evaluation — including that there is none.

    `is_current` is the answer; `reasons` is why, always populated when the answer is False. A bare
    boolean would send the caller back to re-derive what this function already knows.
    """

    expectation_ref: str
    eval_run_id: str | None
    is_current: bool
    reasons: tuple[str, ...]
    #: The build that produced the evaluation, when there is one. Informational — see the module
    #: docstring for why it is not a validity condition.
    code_commit: str | None = None


def current_evaluation_contract_now() -> EvaluationContractV2:
    """The evaluation identity THIS BUILD would mint, provider contracts included.

    The provider contracts are frozen from the running code rather than passed in, so an evaluation
    conducted against a different author instruction or output schema cannot be mistaken for a
    current one — which is the failure the whole byte-freeze exists to prevent.
    """
    from featuregen.formula.audited import current_formula_generation_settings
    from featuregen.formula.frozen_configuration import freeze_current_configuration_v2
    from featuregen.overlay.upload.recipe_formula_evaluation_contract import (
        FORMULA_WIRE_SCHEMA_VERSION_V3,
        current_evaluation_contract,
    )
    from featuregen.overlay.upload.recipe_formula_gold_v2 import (
        V2_CORPUS_VERSION,
        v2_corpus_content_hash,
    )

    frozen = freeze_current_configuration_v2(
        generation_settings=current_formula_generation_settings(),
        formula_schema_version=FORMULA_WIRE_SCHEMA_VERSION_V3)
    return current_evaluation_contract(
        corpus_version=V2_CORPUS_VERSION,
        corpus_content_hash=v2_corpus_content_hash(),
        author_provider_contract_hash=frozen.author.contract_hash,
        critic_provider_contract_hash=frozen.critic.contract_hash)


def current_evaluation_validity(conn, expectation_ref: str) -> EvaluationValidityV1:
    """Does a CERTIFIABLE evaluation of this expectation exist under the current contract?

    Certifiable, not merely passing. A run can score every attempt correctly and still certify
    nothing — one reviewed clean case does not demonstrate reliability — and promoting such a run
    to an activation authority is precisely the laundering this reader exists to prevent.
    """
    from featuregen.overlay.upload.recipe_formula_eval_v2 import evaluate_persisted_run_v2

    contract = current_evaluation_contract_now()

    # Newest first: if several runs measured the current world, the most recent one is the answer,
    # and an older passing run cannot outvote a newer failing one.
    candidates = conn.execute(
        "SELECT r.eval_run_id, r.code_commit "
        "  FROM recipe_formula_eval_run r "
        "  JOIN recipe_formula_eval_case_v2 c ON c.eval_run_id = r.eval_run_id "
        " WHERE r.evaluation_contract_hash = %s "
        "   AND c.case_kind = 'clean' AND c.subject_kind = 'expectation_ref' "
        "   AND c.subject_ref = %s "
        " ORDER BY r.created_at DESC", (contract.contract_hash, expectation_ref)).fetchall()

    if not candidates:
        return EvaluationValidityV1(
            expectation_ref=expectation_ref, eval_run_id=None, is_current=False,
            reasons=(
                "NO_CURRENT_EVALUATION: no evaluation run covering this expectation was conducted "
                "under the contract this build would mint. An evaluation under a different "
                "contract measured a different world",))

    eval_run_id, code_commit = candidates[0]
    gate = evaluate_persisted_run_v2(conn, eval_run_id)
    return EvaluationValidityV1(
        expectation_ref=expectation_ref,
        eval_run_id=eval_run_id,
        is_current=gate.certifiable,
        reasons=() if gate.certifiable else gate.reasons,
        code_commit=code_commit)


def expectations_with_current_evaluation(conn) -> frozenset[str]:
    """Every expectation ref that HAS a certifiable current evaluation — one query, one gate pass.

    ▲ The set form exists because the serving path asks this question once per candidate. Asking
    per candidate would mean a gate evaluation per candidate; asking once and consulting a set does
    not. Callers that need the REASON for a single expectation use
    :func:`current_evaluation_validity` instead — the set deliberately cannot say why something is
    absent, and a caller that needs to explain an absence should not be reading a set.
    """
    from featuregen.overlay.upload.recipe_formula_eval_v2 import evaluate_persisted_run_v2

    contract = current_evaluation_contract_now()
    rows = conn.execute(
        "SELECT DISTINCT r.eval_run_id, c.subject_ref "
        "  FROM recipe_formula_eval_run r "
        "  JOIN recipe_formula_eval_case_v2 c ON c.eval_run_id = r.eval_run_id "
        " WHERE r.evaluation_contract_hash = %s "
        "   AND c.case_kind = 'clean' AND c.subject_kind = 'expectation_ref'",
        (contract.contract_hash,)).fetchall()

    certifiable: dict[str, bool] = {}
    covered: set[str] = set()
    for eval_run_id, subject_ref in rows:
        if eval_run_id not in certifiable:
            certifiable[eval_run_id] = evaluate_persisted_run_v2(conn, eval_run_id).certifiable
        if certifiable[eval_run_id]:
            covered.add(subject_ref)
    return frozenset(covered)
