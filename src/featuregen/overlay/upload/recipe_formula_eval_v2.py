"""The Formula-v2/v3 evaluation lane — a REPLACEMENT for `recipe_formula_eval`, not a mode of it.

▲ **WHY A SEPARATE MODULE AND NOT A VERSION SWITCH.** `recipe_formula_eval` is 712 lines that bind
to V1 on three of its four axes: the V1 expectation registry, the V1 gold corpus, and V1's
`OPERATION_GRAMMAR_VERSION` / `OUTPUT_POLICY_VERSION`. Threading a version conditional through it
would produce one module that is honestly neither generation — and, worse, would tempt exactly the
import swap §0.5 forbids, since V1's and V2's version integers both equal 1. The V1 evaluator stays
truthfully V1 and is deleted whole when this lane is the only accepted one.

**WHAT THIS LANE MEASURES THAT V1's CANNOT.**

* A clean case is correct when it reaches `READY_FOR_OUTPUT_BINDING`, NOT `RESOLVED`. A V3 run
  captures the author's intent and stops; the compiler resolves output authority (C-A7). Scoring
  against `RESOLVED` would mark every correct V3 run a failure and every run that wrongly resolved
  its own output a success.
* An attempt is only evidence if the run actually happened under the v3 contract with a trace that
  replays — `qualifies_as_v3_evidence_for_run`. A manifest claiming v3 is not enough: between two
  known commits a run carried a fully current manifest while being physically driven under
  `formula_author_turn_v2`. This is that qualifier's production caller.
* The corpus can be short of what certification needs, and a lane that could not say so would issue
  a green verdict on one reviewed case. `evaluate_persisted_run_v2` refuses, by name.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from featuregen.formula.authoring_versions import v3_run_conformance
from featuregen.idgen import mint_id
from featuregen.overlay.upload.recipe_formula_evaluation_contract import (
    EvaluationContractV2,
    current_evaluation_contract,
    record_evaluation_contract,
    verify_recorded_contract,
)
from featuregen.overlay.upload.recipe_formula_gold_v2 import (
    V2_CORPUS_VERSION,
    FormulaGoldCaseV2,
    corpus_adequacy,
    formula_gold_v2_cases,
    v2_corpus_content_hash,
    validate_formula_gold_v2_corpus,
)
from featuregen.overlay.upload.recipe_formula_shadow import content_hash

#: What an attempt can be true about. Fixed, because an outcome dictionary whose keys varied by case
#: would make "how often did this pass" unanswerable across a run.
OUTCOME_KEYS_V2 = frozenset({
    "technical_failure",
    #: Both halves of the v3 question — the whole-run answer the release gate asserts on.
    "v3_evidence",
    #: The conduct half alone. Recorded separately because an adversarial case legitimately has
    #: `v3_evidence` False (its artifact is invalid on purpose) while still requiring this to be
    #: True — without it, "the platform refused" and "the lane was never v3" look the same.
    "conducted_under_v3",
    "reproduced_reviewed_formula",
    "exact_match",
    "false_ready",
    "accepted",
    "preservation_ok",
    "refusal_detected",
})

#: The disposition a CORRECT V3 authoring run ends on. Named once here so the inversion described in
#: the module docstring cannot be reintroduced by a stray literal.
CLEAN_TERMINAL_DISPOSITION = "READY_FOR_OUTPUT_BINDING"

#: Dispositions that mean the platform ACCEPTED the proposal. `RESOLVED` is included because a V3
#: run reaching it is a real failure — output authority resolved a stage early — and an evaluator
#: that ignored it would score that failure as a refusal.
_ACCEPTING_DISPOSITIONS = frozenset({CLEAN_TERMINAL_DISPOSITION, "RESOLVED"})


class FormulaEvaluationIntegrityErrorV2(RuntimeError):
    """Persisted evaluation material no longer verifies against what it claims to be."""


@dataclass(frozen=True, slots=True)
class EvaluationRunConfigurationV2:
    """The part of a run that is a CHOICE rather than an identity.

    Everything version-bearing lives on the evaluation contract (migration 1097). What is here is
    who ran it, against which provider, under what budget and over which window — legitimately
    different between two runs that measured exactly the same thing.
    """

    provider: str
    model: str
    generation_controls: dict[str, Any]
    author_provider_contract_hash: str
    critic_provider_contract_hash: str
    shadow_window_start: datetime
    shadow_window_end: datetime
    shadow_generation_run_ids: tuple[str, ...]
    token_budget: int
    cost_budget: Decimal
    created_by: dict[str, Any]
    runner_kind: str = "REAL_PROVIDER"
    code_commit: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationGateResultV2:
    """What a completed V2/V3 evaluation says — including that it may say nothing yet.

    `certifiable` is deliberately separate from `passed`. A run can have every attempt come out
    right and still not certify the lane, because one reviewed clean case does not demonstrate
    reliability. Collapsing the two would turn a governance gap into a green light.
    """

    eval_run_id: str
    contract: EvaluationContractV2
    attempts: int
    clean_attempts: int
    adversarial_attempts: int
    exact_matches: int
    false_ready: int
    technical_failures: int
    attempts_without_v3_evidence: int
    passed: bool
    certifiable: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _code_commit(configured: str | None) -> str:
    if configured:
        return configured
    from featuregen.overlay.upload.recipe_formula_eval import _code_commit as v1_code_commit

    return v1_code_commit()


def create_evaluation_run_v2(
    conn,
    configuration: EvaluationRunConfigurationV2,
    *,
    eval_run_id: str | None = None,
) -> str:
    """Freeze the identity and the reviewed corpus into a write-once run. Returns the run id.

    ▲ **THIS DOES NOT REFUSE ON A SHORT CORPUS**, and that is deliberate. A run over one reviewed
    case still produces real per-case evidence, and refusing to gather it would leave the lane
    unexercised until governance caught up — the transition §0.5 describes needs it RUN before it
    can be made the only lane. What must not happen is that such a run reads as certification, and
    `evaluate_persisted_run_v2` is where that is refused.
    """
    validate_formula_gold_v2_corpus()
    if configuration.runner_kind not in {"REAL_PROVIDER", "FAKE_TEST"}:
        raise ValueError("runner_kind must be REAL_PROVIDER or FAKE_TEST")
    if configuration.shadow_window_end <= configuration.shadow_window_start:
        raise ValueError("shadow evaluation window must be non-empty")
    if configuration.token_budget <= 0 or configuration.cost_budget < 0:
        raise ValueError("evaluation budgets must be non-negative and non-vacuous")
    if len(configuration.shadow_generation_run_ids) != len(
            set(configuration.shadow_generation_run_ids)):
        raise ValueError("shadow generation run ids must be unique")

    contract = current_evaluation_contract(
        corpus_version=V2_CORPUS_VERSION,
        corpus_content_hash=v2_corpus_content_hash(),
        author_provider_contract_hash=configuration.author_provider_contract_hash,
        critic_provider_contract_hash=configuration.critic_provider_contract_hash)

    run_id = eval_run_id or mint_id("rfe2")
    with conn.transaction():
        record_evaluation_contract(conn, contract)
        conn.execute(
            "INSERT INTO recipe_formula_eval_run "
            "(eval_run_id,corpus_version,corpus_content_hash,expectation_registry_hash,"
            "operation_grammar_version,output_policy_version,author_provider_contract_hash,"
            "critic_provider_contract_hash,provider,model,generation_controls,code_commit,"
            "shadow_window_start,shadow_window_end,shadow_generation_run_ids,token_budget,"
            "cost_budget,runner_kind,created_by,evaluation_contract_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, contract.corpus_version, contract.corpus_content_hash,
             contract.expectation_registry_hash, contract.operation_grammar_version,
             contract.output_policy_version, contract.author_provider_contract_hash,
             contract.critic_provider_contract_hash, configuration.provider, configuration.model,
             _json(configuration.generation_controls), _code_commit(configuration.code_commit),
             configuration.shadow_window_start, configuration.shadow_window_end,
             _json(list(configuration.shadow_generation_run_ids)), configuration.token_budget,
             configuration.cost_budget, configuration.runner_kind,
             _json(configuration.created_by), contract.contract_hash))

        for case in formula_gold_v2_cases():
            conn.execute(
                "INSERT INTO recipe_formula_eval_case_v2 "
                "(eval_run_id,case_id,case_kind,subject_kind,subject_ref,fixture_name,fixture_pin,"
                "expected_json,expected_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, case.case_id, case.case_kind,
                 "expectation_ref" if case.case_kind == "clean" else "gold_fixture",
                 case.expectation_ref if case.case_kind == "clean" else case.fixture_name,
                 case.fixture_name, case.fixture_pin,
                 _json(case.expected), content_hash(case.expected)))
    return run_id


def derive_outcome_v2(
    *,
    case: FormulaGoldCaseV2,
    result: dict[str, Any],
    candidate_proposal_hash: str | None,
    conducted_under_v3: bool,
    artifact_is_v3: bool,
) -> dict[str, bool]:
    """Score ONE attempt against ONE reviewed case. Pure — the caller supplies the facts.

    Pure on purpose: every interesting scoring question (does a V3 run ending RESOLVED count as a
    pass? does an unsupported-capability refusal satisfy a schema-error case?) is answerable here
    without a database, an authoring run or a provider.
    """
    disposition = str(result.get("authoring_disposition"))
    accepted = disposition in _ACCEPTING_DISPOSITIONS

    # ── the clean half ──────────────────────────────────────────────────────────────────────────
    # `output_status` is checked as well as the disposition because the two can disagree in the one
    # direction that matters: a run that resolved output authority itself has taken a decision the
    # compiler owns, and it must not be scored as a correct capture.
    preservation_ok = (
        case.case_kind == "clean"
        and disposition == CLEAN_TERMINAL_DISPOSITION
        and result.get("output_status") == case.expected.get("output_status")
        and result.get("structural_status") == "ok"
        and result.get("expectation_status") in {"match", "not_provided"}
        and candidate_proposal_hash is not None)

    reproduced = bool(
        case.case_kind == "clean"
        and candidate_proposal_hash is not None
        and candidate_proposal_hash == case.expected.get("proposal_hash"))

    # ── the adversarial half ────────────────────────────────────────────────────────────────────
    # By CLASS, never by "something went wrong". A malformed proposal caught by the capability gate
    # would mean the structural check missed it, which is a different defect from the one the case
    # was written to detect — so each class is satisfied only by its own axis.
    refusal_class = case.expected.get("refusal_class")
    if case.case_kind != "adversarial":
        refusal_detected = False
    elif refusal_class == "schema_error":
        refusal_detected = result.get("structural_status") not in {"ok", None}
    elif refusal_class == "unsupported_capability":
        refusal_detected = result.get("capability_status") not in {"ok", None}
    else:
        refusal_detected = False

    false_ready = accepted and (
        case.case_kind == "adversarial" or not preservation_ok)

    exact_match = (
        (preservation_ok and reproduced) if case.case_kind == "clean"
        else (refusal_detected and not accepted))

    # ▲ **THE TWO HALVES NEED DIFFERENT HALVES OF THE V3 QUESTION, and collapsing them into one
    # flag made every adversarial case unpassable by construction.** An adversarial case shows the
    # provider a malformed proposal ON PURPOSE, so its artifact cannot parse as V3 — under a single
    # "is this v3 evidence" flag a correct refusal and a broken lane scored identically.
    #
    # What each half actually requires:
    #   * CLEAN — conducted under v3 AND produced a genuine V3 artifact. Both, or the run cannot
    #     demonstrate v3 authoring quality however good the formula looks.
    #   * ADVERSARIAL — conducted under v3, and the artifact NOT being v3 is the refusal itself.
    #     Still requires the conduct half, so a refusal from a lane that was never v3 is not a pass.
    v3_admissible = conducted_under_v3 and (
        artifact_is_v3 if case.case_kind == "clean" else True)

    return {
        "technical_failure": result.get("technical_status") == "technical_failure",
        "v3_evidence": bool(conducted_under_v3 and artifact_is_v3),
        "conducted_under_v3": conducted_under_v3,
        "reproduced_reviewed_formula": reproduced,
        "exact_match": bool(exact_match and v3_admissible),
        "false_ready": bool(false_ready),
        "accepted": accepted,
        "preservation_ok": bool(preservation_ok),
        "refusal_detected": bool(refusal_detected),
    }


def record_evaluation_attempt_v2(
    conn,
    *,
    eval_run_id: str,
    case: FormulaGoldCaseV2,
    repeat_index: int,
    authoring_run_id: str,
    result: dict[str, Any],
    candidate_proposal_hash: str | None,
) -> str:
    """Score and persist one attempt, DERIVING every fact about it from the audited run.

    ▲ **NOTHING HERE IS CALLER-SUPPLIED EXCEPT WHAT THE CALLER ALONE KNOWS.** An earlier version of
    this function accepted the dispatch refs and the token/cost figures as parameters. That is not
    evidence — a caller could have passed anything, and an evaluation assembled from numbers it was
    handed measures the caller rather than the provider. They are read from the audit instead, the
    way `recipe_formula_eval` already reads them.

    Three refusals, before anything is written:

    * the case must belong to THIS run's frozen corpus, or the attempt is evidence about a case the
      run never froze;
    * the authoring dispatch must be strictly reconciled, or the audit cannot say what was sent;
    * there must be audited author AND critic calls, because an attempt with neither did not
      exercise the lane it claims to have measured.

    `authoring_run_id` is REQUIRED, deliberately. An attempt with no authoring run is not an
    attempt, and scoring one as merely "unevidenced" would let a run of twelve no-ops record twelve
    rows that say nothing.
    """
    from featuregen.overlay.upload.dispatch_audit import formula_dispatches_reconciled
    from featuregen.overlay.upload.recipe_formula_eval import (
        _audited_usage,
        _dispatch_identity,
    )

    if repeat_index < 0:
        raise ValueError("evaluation repeat index cannot be negative")
    frozen = conn.execute(
        "SELECT 1 FROM recipe_formula_eval_case_v2 WHERE eval_run_id=%s AND case_id=%s",
        (eval_run_id, case.case_id)).fetchone()
    if frozen is None:
        raise FormulaEvaluationIntegrityErrorV2(
            f"case {case.case_id!r} does not belong to the corpus frozen into run "
            f"{eval_run_id!r}")
    if not formula_dispatches_reconciled(conn, authoring_run_id):
        raise FormulaEvaluationIntegrityErrorV2(
            "evaluation attempt authoring dispatch is not strictly reconciled, so the audit "
            "cannot say what was sent to the provider")

    author_refs, critic_refs, llm_refs = _dispatch_identity(conn, authoring_run_id)
    if not author_refs or not critic_refs or not llm_refs:
        raise FormulaEvaluationIntegrityErrorV2(
            "evaluation attempt requires audited author and critic provider calls")
    input_tokens, output_tokens, cost_amount = _audited_usage(conn, llm_refs)

    conformance = v3_run_conformance(conn, authoring_run_id)
    qualifies, problems = conformance.as_evidence()

    outcome = derive_outcome_v2(
        case=case, result=result, candidate_proposal_hash=candidate_proposal_hash,
        conducted_under_v3=conformance.conducted_under_v3,
        artifact_is_v3=conformance.artifact_is_v3)
    if set(outcome) != OUTCOME_KEYS_V2:
        raise FormulaEvaluationIntegrityErrorV2(
            "evaluation outcome keys do not match the fixed V2 vocabulary")

    attempt_id = mint_id("rfa2")
    conn.execute(
        "INSERT INTO recipe_formula_eval_attempt_v2 "
        "(attempt_id,eval_run_id,case_id,repeat_index,authoring_run_id,author_dispatch_refs,"
        "critic_dispatch_refs,llm_call_refs,disposition,v3_evidence,v3_evidence_problems,"
        "outcome_json,outcome_hash,input_tokens,output_tokens,cost_amount) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (attempt_id, eval_run_id, case.case_id, repeat_index, authoring_run_id,
         _json(list(author_refs)), _json(list(critic_refs)), _json(list(llm_refs)),
         str(result.get("authoring_disposition")), qualifies, _json(list(problems)),
         _json(outcome), content_hash(outcome), input_tokens, output_tokens, cost_amount))
    return attempt_id


def evaluate_persisted_run_v2(conn, eval_run_id: str) -> EvaluationGateResultV2:
    """Read a completed run back and say what it establishes — and what it does not.

    ▲ **CERTIFIABILITY IS ANSWERED FIRST AND SEPARATELY.** Whether every attempt came out right and
    whether the corpus is adequate to certify anything are different questions, and a caller that
    saw only `passed` would read one as the other. A run over one reviewed clean case can pass
    perfectly and certify nothing.
    """
    run = conn.execute(
        "SELECT evaluation_contract_hash, corpus_content_hash FROM recipe_formula_eval_run "
        "WHERE eval_run_id=%s", (eval_run_id,)).fetchone()
    if run is None:
        raise FormulaEvaluationIntegrityErrorV2(f"no evaluation run {eval_run_id!r}")
    if run[0] is None:
        raise FormulaEvaluationIntegrityErrorV2(
            f"evaluation run {eval_run_id!r} cites no evaluation contract; a run that cannot say "
            f"what it was conducted under is not V2/V3 evidence")

    contract = verify_recorded_contract(conn, run[0])

    reasons: list[str] = []
    # The corpus is re-derived and compared, never trusted from the row: a run recorded under a
    # corpus that has since changed is evidence for the OLD corpus, and must say so.
    if contract.corpus_content_hash != v2_corpus_content_hash():
        reasons.append(
            "CORPUS_MOVED: this run was conducted against a corpus that is no longer the current "
            "one, so it is evidence for what it measured and not for what is reviewed now")
    if run[1] != contract.corpus_content_hash:
        raise FormulaEvaluationIntegrityErrorV2(
            f"evaluation run {eval_run_id!r} disagrees with its own contract about the corpus")

    rows = tuple(
        (kind, outcome, evidence) for kind, outcome, evidence in conn.execute(
            "SELECT c.case_kind, a.outcome_json, a.v3_evidence "
            "  FROM recipe_formula_eval_attempt_v2 a "
            "  JOIN recipe_formula_eval_case_v2 c "
            "    ON c.eval_run_id = a.eval_run_id AND c.case_id = a.case_id "
            " WHERE a.eval_run_id = %s", (eval_run_id,)).fetchall())

    return summarise_attempts_v2(
        eval_run_id=eval_run_id, contract=contract, rows=rows,
        shortfalls=corpus_adequacy(), prior_reasons=tuple(reasons))


def summarise_attempts_v2(
    *,
    eval_run_id: str,
    contract: EvaluationContractV2,
    rows: tuple[tuple[str, dict[str, Any], bool], ...],
    shortfalls: tuple[str, ...],
    prior_reasons: tuple[str, ...] = (),
) -> EvaluationGateResultV2:
    """Turn scored attempts into a verdict. Pure, for the same reason `derive_outcome_v2` is.

    Every question worth arguing about — does a perfect run over a short corpus certify? does one
    false-ready attempt sink a run? — is decided here and answerable without a database, an
    authoring run or a provider. The database half above only fetches and verifies.

    ``rows`` is ``(case_kind, outcome_json, v3_evidence)`` per attempt.
    """
    reasons = list(prior_reasons)

    attempts = len(rows)
    clean = sum(kind == "clean" for kind, _outcome, _ev in rows)
    exact = sum(bool(outcome.get("exact_match")) for _kind, outcome, _ev in rows)
    false_ready = sum(bool(outcome.get("false_ready")) for _kind, outcome, _ev in rows)
    technical = sum(bool(outcome.get("technical_failure")) for _kind, outcome, _ev in rows)
    # ▲ PER CASE KIND. A clean attempt needs whole-run evidence; an adversarial one needs only the
    # conduct half, because its artifact is invalid on purpose. Counting `v3_evidence` for both
    # would report every correct refusal as unevidenced.
    unevidenced = sum(
        not (evidence if kind == "clean" else outcome.get("conducted_under_v3"))
        for kind, outcome, evidence in rows)

    if attempts == 0:
        reasons.append("NO_ATTEMPTS: a run with no attempts establishes nothing")
    if false_ready:
        reasons.append(
            f"FALSE_READY: {false_ready} attempt(s) were accepted that should not have been")
    if technical:
        reasons.append(f"TECHNICAL_FAILURES: {technical} attempt(s) never reached a verdict")
    if unevidenced:
        reasons.append(
            f"NOT_V3_EVIDENCE: {unevidenced} attempt(s) did not qualify — the trace did not replay, "
            f"or the author calls were not requested under the v3 contract")
    if exact != attempts:
        reasons.append(f"INEXACT: {exact} of {attempts} attempt(s) matched their reviewed case")

    passed = attempts > 0 and not reasons
    reasons.extend(shortfalls)

    return EvaluationGateResultV2(
        eval_run_id=eval_run_id,
        contract=contract,
        attempts=attempts,
        clean_attempts=clean,
        adversarial_attempts=attempts - clean,
        exact_matches=exact,
        false_ready=false_ready,
        technical_failures=technical,
        attempts_without_v3_evidence=unevidenced,
        passed=passed,
        # ▲ BOTH, and in this order. A short corpus cannot certify however well the attempts went;
        # attempts that went badly cannot certify however complete the corpus is.
        certifiable=passed and not shortfalls,
        reasons=tuple(reasons))


def _json(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(asdict(value) if hasattr(value, "__dataclass_fields__") else value)
