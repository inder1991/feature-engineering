"""Task A3 — the Formula-**v2** authoring ORCHESTRATOR: ``run_authoring``'s sibling, never its edit.

``run_authoring_v2`` drives ONE governed v2 authoring run end to end::

    open_authoring_run  ->  author_formula (under AUTHOR_TURN_CONTRACT_V2)  ->  parse_versioned
    (which runs validate_semantics_v2)  ->  classify_formula_capability_v2(proposal, engine=None)
    ->  resolve_output_v2 over C1 facts  ->  critique (a SEPARATE context on its own client)
    ->  derive_disposition_v2  ->  append_event(COMPLETED|FAILED)

and returns the multi-axis :class:`~featuregen.formula.result_v2.AuthoringResultV2`. OFFLINE: no
execution, no Spark, no durable formula artifact — the only durable records are the append-only
``llm_call`` audit rows and the write-once authoring trace.

**D-3, verbatim: the v1 path is not touched, not deprecated and not deleted.** ``authoring.py``'s
four invariants are RESTATED here over the v2 types, each one still a defect if inverted:

* **The authoritative output comes ONLY from the governed C1 authority stage.** The proposal's
  ``expected_output`` is the model's ADVISORY guess; it is compared (the expectation axis) and never
  consulted for authority. A ``FormulaOutputPolicyV2`` carrying ``external_type_required=True`` is
  NOT a resolved output — :func:`classify_output_policy_v2` maps it to
  ``output_status="external_requirement"``, the same law v1 applies, because §C set that flag
  precisely when a numeric type could not be granted GOVERNED authority.
* **Every result is built by :func:`~featuregen.formula.result_v2.derive_disposition_v2`.** The
  coherence raise-guards live there, so this module never imports ``AuthoringResultV2`` at runtime.
* **Technical outcomes never fabricate.** ``author_formula`` returning ``(None, turns)`` (max_turns
  exhaustion, token budget, an egress-blocked or provider-FAILED call) and a critic reporting
  ``is_technical_failure`` both become ``technical_status="technical_failure"`` — the
  highest-precedence arm, which carries neither an output policy nor a proposal. **A provider
  billing failure is a technical failure and NEVER a capability or schema verdict** (charter D-10;
  the ``3219a209 fix(llm): a billing 400 is never a schema diagnosis`` precedent): the audited seam
  returns ``output=None`` for a failed call, and every ``output=None`` lands here.
* **``unsupported != invalid``.** A body naming an aggregate or a combiner outside the v2 vocabulary
  is ``structural_status="unsupported_operation"`` (→ UNSUPPORTED); only a genuinely malformed or
  semantically-violating proposal is ``invalid_formula`` (→ REJECTED). The RAW dict is inspected
  before parse for exactly this, mirroring v1 — and in v2 the arm is genuinely REACHABLE, because
  ``turns_v2`` relaxes both ``aggregation`` and ``final_operation`` on the wire.

ONE v2-only structural rule: a run driven under the v2 turn contract that comes back declaring
``formula_schema_version: 1`` is ``invalid_formula``, not ``unsupported_operation``. It is not a
grammar the platform lacks — it is the WRONG CONTRACT for the run that was opened, and the v1
generation is fully supported elsewhere. Calling it "unsupported" would report a capability gap that
does not exist.

TRACE + CONNECTION DISCIPLINE are ``authoring``'s, imported rather than re-implemented: seq counts
from 0, STARTED is always 0, the terminal is always last, payloads are canonical redacted metadata
only, and orchestration is SINGLE-THREADED on ``conn`` by contract. Those rules carry no grammar in
them; a second copy could only drift.

FAILURE, DELIBERATELY. An unexpected exception PROPAGATES and the run stays **incomplete** — which
is what ``run_status`` derives from the absence of a terminal event, and exactly the honest answer.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula._jcs import dumps as _jcs_dumps
from featuregen.formula.author import AUTHOR_TURN_CONTRACT_V2, author_formula
from featuregen.formula.authoring import (
    _C1_HARD_FAIL_STATUSES,
    AUTHORING_MAX_TURNS,
    EXTERNAL_TYPE_VALIDATION_REQUIRED,
    _Trace,
    _trace_turns,
)
from featuregen.formula.capability_v2 import classify_formula_capability_v2
from featuregen.formula.critic import CRITIC_POLICY_VERSION, CriticFinding, critique
from featuregen.formula.measure_facts import (
    MeasureFactsUnreadable,
    read_measure_facts,
)
from featuregen.formula.output_authority_v2 import (
    FormulaOutputPolicyV2,
    InvalidOutputV2,
    OperandFactsV2,
    resolve_output_v2,
)
from featuregen.formula.parse_v2 import parse_versioned
from featuregen.formula.result import (
    AuthoringAxes,
    AuthorityFailure,
    CriticStatus,
    ExpectationStatus,
    OutputStatus,
    StructuralStatus,
)
from featuregen.formula.result_v2 import DISPOSITION_POLICY_VERSION_V2, derive_disposition_v2
from featuregen.formula.schema_leaves import SchemaError
from featuregen.formula.schema_v2 import (
    CANONICALIZATION_VERSION_V2,
    FORMULA_SCHEMA_VERSION_V2,
    OPERATION_GRAMMAR_VERSION_V2,
    AggregateFunctionV2,
    FinalOperationV2,
    TypedFormulaProposalV2,
    body_expressions_v2,
)
from featuregen.formula.trace import TraceEventKind, open_authoring_run
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.operational_facts import read_operational_value

if TYPE_CHECKING:
    # Deliberately TYPE_CHECKING-only, for the same reason v1 does it: a name that exists at
    # runtime is a name that can be CONSTRUCTED, bypassing every coherence raise-guard
    # `derive_disposition_v2` owns. The AST test asserts both halves — no runtime import, and no
    # construction call anywhere in this module.
    from featuregen.formula.result_v2 import AuthoringResultV2

__all__ = [
    "AUTHORING_ORCHESTRATOR_VERSION_V2",
    "AUTHORING_VERSIONS_V2",
    "OutputResolutionV2",
    "authoring_intent_hash_v2",
    "classify_output_policy_v2",
    "forbids_authored_artifact_v2",
    "run_authoring_v2",
]

#: Version of THIS orchestrator's wiring (stage order, axis mapping, trace vocabulary).
AUTHORING_ORCHESTRATOR_VERSION_V2 = 2   # C-A6: the stage mapping gained REVIEW_BYPASSED

#: Every rule/registry version a v2 run is decided under, stamped on the manifest BEFORE any
#: provider call, so a later policy bump can never be mistaken for the one that produced a verdict.
AUTHORING_VERSIONS_V2: dict[str, int] = {
    "formula_schema_version": FORMULA_SCHEMA_VERSION_V2,
    "operation_grammar_version": OPERATION_GRAMMAR_VERSION_V2,
    "canonicalization_version": CANONICALIZATION_VERSION_V2,
    "critic_policy_version": CRITIC_POLICY_VERSION,
    "disposition_policy_version": DISPOSITION_POLICY_VERSION_V2,
    "authoring_orchestrator_version": AUTHORING_ORCHESTRATOR_VERSION_V2,
}

#: The ``OperandFactsV2`` slot -> the C1 field it is read from. The ONE place the mapping lives, so
#: the bundle the resolver receives and the attribution a failure carries can never disagree.
_OPERAND_FACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("logical_type", "logical_representation"),
    ("unit", "unit"),
    ("currency", "currency"),
)

#: The one slot ``read_operational_value`` still answers for an operand. ``_OPERAND_FACT_FIELDS``
#: above is deliberately KEPT: ``recipe_authoring``'s frozen reader imports it, and that reader
#: projects a SNAPSHOT (no connection), so it cannot use the verified-decision seam. Its measure
#: facts are still hint-shaped — the frozen half of C-A3c is a separate step, because the snapshot
#: builder would have to record a verified status and that moves ``snapshot_item_hash``.
_LOGICAL_TYPE_FIELD = "logical_representation"

_GRAIN_FIELD = "is_grain"

_AGGREGATION_VALUES: frozenset[str] = frozenset(fn.value for fn in AggregateFunctionV2)
_FINAL_OPERATION_VALUES: frozenset[str] = frozenset(op.value for op in FinalOperationV2)
_EXPRESSION_SLOTS: tuple[str, ...] = (
    "expr", "numerator", "denominator", "minuend", "subtrahend")

_TERMINAL_FOR_DISPOSITION: Mapping[str, TraceEventKind] = {
    "TECHNICAL_FAILURE": TraceEventKind.FAILED,
}


# ── output-axis classification ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class OutputResolutionV2:
    """What ``resolve_output_v2``'s two-arm return means on the ``output_status`` axis.

    ``policy`` is the AUTHORITATIVE :class:`FormulaOutputPolicyV2` — present ONLY when the status is
    ``"resolved"``, so a caller physically cannot claim authority for an unresolved output."""

    status: OutputStatus
    policy: FormulaOutputPolicyV2 | None
    requirements: tuple[str, ...] = ()
    reason: str | None = None


def classify_output_policy_v2(resolved: object) -> OutputResolutionV2:
    """Map ``resolve_output_v2``'s return onto the ``output_status`` axis.

    THE load-bearing case, unchanged from v1: a :class:`FormulaOutputPolicyV2` with
    ``external_type_required=True`` is NOT ``"resolved"``. That flag is set when the operand carried
    no governed logical type, so the output type must still be validated outside the catalog — an
    ``"external_requirement"``. Calling it resolved would hand the fold an unqualified RESOLVED for
    a formula whose output type nothing has certified."""
    if isinstance(resolved, InvalidOutputV2):
        return OutputResolutionV2("invalid_output", None, reason=resolved.reason)
    if isinstance(resolved, FormulaOutputPolicyV2):
        if resolved.external_type_required:
            return OutputResolutionV2(
                "external_requirement", None,
                requirements=(EXTERNAL_TYPE_VALIDATION_REQUIRED,),
                reason=EXTERNAL_TYPE_VALIDATION_REQUIRED)
        return OutputResolutionV2("resolved", resolved)
    # The resolver's return union is closed; an unknown arm fails CLOSED, never toward resolution.
    return OutputResolutionV2(
        "needs_authority", None, reason=f"unknown_output_resolution:{type(resolved).__name__}")


def forbids_authored_artifact_v2(axes: AuthoringAxes) -> bool:
    """``True`` when the fold of ``axes`` lands on a disposition that carries NO authored artifact
    (UNSUPPORTED / REJECTED / TECHNICAL_FAILURE).

    The orchestrator must know this BEFORE calling :func:`derive_disposition_v2`, because the
    artifact it offers has to be coherent with the disposition that call will produce. Pinned
    against the real v2 fold over the complete axis cross-product in the tests."""
    return (
        axes.technical_status == "technical_failure"
        or axes.structural_status in ("invalid_formula", "unsupported_operation")
        or axes.capability_status == "unsupported_capability"
        or axes.output_status == "invalid_output"
    )


def authoring_intent_hash_v2(intent: AuthoringIntent) -> str:
    """The content hash of an authoring intent — sha256 over the RFC 8785 (JCS) bytes of its plain
    form. Identical projection to v1's: the INTENT carries no grammar, only what was asked."""
    return hashlib.sha256(_jcs_dumps({
        "name": intent.name,
        "hypothesis": intent.hypothesis,
        "target_entity": intent.target_entity,
        "target_grain_keys": list(intent.target_grain_keys),
    })).hexdigest()


# ── the orchestrator ─────────────────────────────────────────────────────────────────────────────

def _require_tool_runner_v2(tool_runner) -> None:
    """Refuse a missing runner instead of falling back to V1 tools (C-A8).

    Kept in step with ``replay_authoring_v2``'s guard: both orchestrators reach the same
    ``author_formula`` default, so a seam that failed closed in one and open in the other would
    just move the defect to whichever entry point a caller happened to use.
    """
    if tool_runner is None:
        raise ToolRunnerRequiredV2(
            "run_authoring_v2 requires a tool_runner. Omitting it does not disable tools — "
            "`author_formula` falls back to `run_tool`, the V1 set — so a v2 run would author "
            "against the wrong catalog surface with nothing in the trace to say so")


class ToolRunnerRequiredV2(ValueError):
    """A v2 authoring run was given no tool runner. A caller bug, so it raises."""


def run_authoring_v2(conn, intent: AuthoringIntent, author_client: LLMClient,
                     critic_client: LLMClient, *,
                     roles: tuple[str, ...] | list[str] | tuple[()],
                     actor: IdentityEnvelope,
                     tool_runner: Callable[..., dict] | None = None) -> AuthoringResultV2:
    """Author ONE Formula-v2 proposal for ``intent`` and return the folded result.

    ``author_client`` and ``critic_client`` are DELIBERATELY separate: LLM-2 reviews the proposal
    from an independently-assembled context with no slot for the author's reasoning or tool trail.
    ``roles`` gates the MODEL-VISIBLE catalog reads; it does NOT reach this module's own C1
    AUTHORITY reads (``read_operational_value`` takes no ``roles`` parameter) — the same
    deliberately-open gap v1 documents, and the same non-issue for egress, because nothing
    :func:`_read_c1_facts_v2` reads is ever sent to a provider.

    EVERY stage runs once a proposal parses, even when an earlier axis already decides the fold: an
    out-of-capability proposal is still resolved and still criticized, because each axis is an
    explicit upstream verdict, never an assumed all-clear.

    Single-threaded on ``conn`` by contract. Raises whatever the chain raises — a run whose own SQL
    failed stays honestly INCOMPLETE rather than claiming a terminal state."""
    role_tuple = tuple(roles)
    intent_hash = authoring_intent_hash_v2(intent)
    run_id = open_authoring_run(
        conn, intent_hash=intent_hash, versions=AUTHORING_VERSIONS_V2, actor=actor)
    trace = _Trace(conn, run_id)
    trace.append(TraceEventKind.STARTED, {
        "intent_hash": intent_hash,
        "intent_name": intent.name,
        "target_entity": intent.target_entity,
        "target_grain_key_count": len(intent.target_grain_keys),
        "max_turns": AUTHORING_MAX_TURNS,
        "formula_schema_version": FORMULA_SCHEMA_VERSION_V2,
    })

    # C-A8 — the tool set is STATED. `author_formula` defaults to `run_tool` (the V1 set), so
    # omitting this would author a v2 formula against v1 tools with nothing in the trace saying so.
    _require_tool_runner_v2(tool_runner)
    raw_proposal, turns = author_formula(
        conn, intent, author_client, roles=role_tuple, max_turns=AUTHORING_MAX_TURNS,
        actor=actor, authoring_run_id=run_id, turn_contract=AUTHOR_TURN_CONTRACT_V2,
        tool_runner=tool_runner)
    _trace_turns(trace, turns)

    if raw_proposal is None:
        # (None, turns): max_turns exhaustion / token budget / an egress-blocked or provider-FAILED
        # call — a provider billing refusal lands here and nowhere else. TECHNICAL: a proposal is
        # NEVER fabricated, and the critic is never asked to review something that does not exist.
        return _finish(trace, _axes(technical_status="technical_failure"))

    structural_status, proposal = _parse_v2(raw_proposal)
    if proposal is None:
        return _finish(trace, _axes(structural_status=structural_status))

    capability_status = classify_formula_capability_v2(proposal, engine=None)
    capability_reason = None if capability_status == "ok" else _capability_reason(proposal)

    facts_by_ref, authority_failures = _read_c1_facts_v2(conn, proposal)
    if authority_failures:
        # A governed read that failed CLOSED (fork / hash mismatch / projection unavailable) means
        # no authority was established for this operand at all. Resolving an output over the empty
        # facts it produced would answer with a guess, so the axis is needs_authority and the
        # resolver is not consulted for a verdict it has no evidence for.
        resolution = OutputResolutionV2(
            "needs_authority", None, reason=authority_failures[0].reason)
    else:
        resolution = classify_output_policy_v2(resolve_output_v2(proposal, facts_by_ref))

    review = critique(
        conn, intent, proposal, critic_client, roles=role_tuple, actor=actor,
        authoring_run_id=run_id)
    critic_status = _critic_status(list(review.findings))
    trace.append(TraceEventKind.CRITIC_RECORDED, {
        "critic_findings_hash": review.findings_hash,
        "critic_policy_version": CRITIC_POLICY_VERSION,
        "critic_status": critic_status,
        "technical_failure": review.technical_failure,
        # CODES + targets only: the model-authored ``detail`` is free text and never enters the
        # trace (metadata-only discipline); the hash covers it for tamper-evidence.
        "findings": [{"code": f.code.value, "severity": f.severity, "operand": f.operand}
                     for f in review.findings],
    })

    axes = _axes(
        structural_status=structural_status,
        capability_status=capability_status,
        output_status=resolution.status,
        expectation_status=_expectation_status(proposal, resolution.policy),
        critic_status=critic_status,
        technical_status="technical_failure" if review.technical_failure else "ok",
    )
    return _finish(
        trace, axes,
        proposal=proposal,
        policy=resolution.policy,
        output_requirements=resolution.requirements,
        authority_failures=authority_failures,
        capability_reason=capability_reason,
        critic_findings_hash=review.findings_hash,
    )


def _finish(trace: _Trace, axes: AuthoringAxes, *,
            proposal: TypedFormulaProposalV2 | None = None,
            policy: FormulaOutputPolicyV2 | None = None,
            output_requirements: tuple[str, ...] = (),
            authority_failures: tuple[AuthorityFailure, ...] = (),
            capability_reason: str | None = None,
            critic_findings_hash: str | None = None) -> AuthoringResultV2:
    """Fold the axes through the ONLY constructor of an ``AuthoringResultV2``, append the single
    terminal event, and return the result.

    Artifact coherence is decided HERE and enforced THERE: the v2 artifact is the PAIR (proposal +
    resolved policy) and both halves are offered only when the output actually resolved AND the
    disposition permits an artifact; an unresolved-output review carries the validated proposal
    alone. ``derive_disposition_v2`` raises on any combination that contradicts its fold."""
    carries_artifact = not forbids_authored_artifact_v2(axes)
    resolved_output = axes.output_status == "resolved"
    result = derive_disposition_v2(
        axes,
        authoring_run_id=trace.run_id,
        candidate_proposal=proposal if carries_artifact else None,
        candidate_output=policy if (carries_artifact and resolved_output) else None,
        output_requirements=output_requirements,
        authority_failures=authority_failures,
        capability_reason=capability_reason,
        critic_findings_hash=critic_findings_hash,
    )
    trace.append(
        _TERMINAL_FOR_DISPOSITION.get(result.authoring_disposition, TraceEventKind.COMPLETED),
        {
            "authoring_disposition": result.authoring_disposition,
            "disposition_policy_version": result.disposition_policy_version,
            "structural_status": result.structural_status,
            "capability_status": result.capability_status,
            "output_status": result.output_status,
            "expectation_status": result.expectation_status,
            "critic_status": result.critic_status,
            "technical_status": result.technical_status,
            "candidate_proposal_hash": result.candidate_proposal_hash,
            "critic_findings_hash": result.critic_findings_hash,
            "output_requirements": list(result.output_requirements),
            "authority_failures": [
                {"reason": f.reason, "operand": f.operand, "field": f.field}
                for f in result.authority_failures],
        })
    return result


# ── stage helpers ────────────────────────────────────────────────────────────────────────────────

def _parse_v2(raw: Mapping[str, Any]) -> tuple[StructuralStatus, TypedFormulaProposalV2 | None]:
    """Parse + semantically validate the raw proposal, classifying a failure as UNSUPPORTED or
    INVALID — never conflating them.

    ``parse_versioned`` is the version DISPATCH: it reads the declared ``formula_schema_version``
    and nothing else. A run opened under the v2 turn contract that comes back declaring version 1
    is the wrong CONTRACT, not a missing capability — ``invalid_formula`` (see the module
    docstring). Everything the v2 shape gate or ``validate_semantics_v2`` rejects is
    ``invalid_formula`` too; only a body naming an operation outside the v2 vocabulary is
    ``unsupported_operation``."""
    if _names_an_unsupported_operation_v2(raw):
        return "unsupported_operation", None
    try:
        parsed = parse_versioned(raw)
    except SchemaError:
        return "invalid_formula", None
    if not isinstance(parsed, TypedFormulaProposalV2):
        return "invalid_formula", None
    return "ok", parsed


def _names_an_unsupported_operation_v2(raw: Mapping[str, Any]) -> bool:
    """Does the RAW body name an aggregate or a combiner outside the frozen v2 vocabulary?

    Read from the raw dict deliberately: the shape gate would reject such a value as an enum
    violation, indistinguishable from real malformation once ``SchemaError`` is raised — and
    reporting "invalid" for a well-formed request the grammar simply does not cover yet is the
    exact conflation the fold forbids. ``turns_v2`` relaxes both fields on the wire so such a
    proposal actually REACHES here."""
    body = raw.get("body") if isinstance(raw, Mapping) else None
    if not isinstance(body, Mapping):
        return False
    final_operation = body.get("final_operation")
    if isinstance(final_operation, str) and final_operation not in _FINAL_OPERATION_VALUES:
        return True
    expressions: list[Any] = [body.get(slot) for slot in _EXPRESSION_SLOTS]
    terms = body.get("terms")
    if isinstance(terms, list):
        expressions.extend(
            term.get("expr") for term in terms if isinstance(term, Mapping))
    for expr in expressions:
        if isinstance(expr, Mapping):
            aggregation = expr.get("aggregation")
            if isinstance(aggregation, str) and aggregation not in _AGGREGATION_VALUES:
                return True
    return False


def _read_c1_facts_v2(conn, proposal: TypedFormulaProposalV2
                      ) -> tuple[dict[str, OperandFactsV2], tuple[AuthorityFailure, ...]]:
    """Build ``resolve_output_v2``'s fact bundle from REAL C1 reads, plus WHICH read failed closed.

    Keyed by operand ``logical_ref`` — the key ``resolve_output_v2`` looks its operands up by
    (v1's bundle is keyed by internal body PATH because v1's resolver is; keying either one the
    other's way resolves every operand to empty facts and assembles a policy out of nothing).

    ``read_operational_value`` is the ONLY authority read here: never the author's tool trail (which
    is model-visible), never the flat ``graph_node`` columns (which have no tamper gate). A grain
    key is read too — the grain is part of what the output is FOR, and a grain key whose governance
    failed closed is the same kind of missing authority as an operand's."""
    facts_by_ref: dict[str, OperandFactsV2] = {}
    failures: list[AuthorityFailure] = []
    for expr in body_expressions_v2(proposal.body):
        for ref in (expr.operand, expr.second_operand):
            if ref is None or ref in facts_by_ref:
                continue
            # `logical_representation` is a GOVERNED DECISION field, which `read_operational_value`
            # answers correctly. `unit`/`currency` are not: they are measure ANNOTATIONS, load-
            # bearing under `_SOURCE_OR_HUMAN`, and that reader's only `resolved` arms are the
            # decision-field and specialized-fact sets — neither contains them — so it returned
            # `not_operational` for EVERY unit and currency, `_fact_text` turned that into `""`,
            # and `not_operational` is not a hard-fail status. A per-row-currency monetary operand
            # therefore reached `resolve_output_v2` looking NON-MONETARY with nothing recorded, so
            # BR-6's `CURRENCY_CONVERSION_UNDECLARED` tooth could not fire and a mixed-currency
            # population was summed across currencies in silence. `read_measure_facts` reads them
            # through the verified-decision seam instead, and REFUSES an unreadable one rather
            # than answering with the empty fact that hid the problem (C-A3c).
            logical = read_operational_value(conn, ref, _LOGICAL_TYPE_FIELD)
            measures = read_measure_facts(conn, ref)
            if isinstance(measures, MeasureFactsUnreadable):
                facts_by_ref[ref] = OperandFactsV2(logical_type=_fact_text(logical))
                failures.append(AuthorityFailure(
                    reason=measures.reason or measures.status,
                    operand=ref, field=measures.field))
            else:
                facts_by_ref[ref] = OperandFactsV2(
                    logical_type=_fact_text(logical),
                    unit=measures.unit.value,
                    currency=measures.currency.value)
            failure = _hard_failure(logical, ref, _LOGICAL_TYPE_FIELD)
            if failure is not None:
                failures.append(failure)
    for key in proposal.grain.keys:
        failure = _hard_failure(read_operational_value(conn, key, _GRAIN_FIELD), key, _GRAIN_FIELD)
        if failure is not None:
            failures.append(failure)
    return facts_by_ref, tuple(failures)


class GovernedRead(Protocol):
    """The three axes of a governed read these two projections actually consult.

    Structural on purpose: the LIVE reader hands them a
    :class:`~featuregen.overlay.upload.operational_facts.OperationalValue`, and the frozen
    snapshot reader (``recipe_authoring.FrozenRecipeReadContext.formula_facts_v2``) hands them a
    ``FrozenOperationalValue`` — *"the immutable subset of OperationalValue consumed by formula
    output policy"*, in its own words. One projection, both readers, no second definition of what
    a fact is. Read-only members deliberately: both readers are FROZEN dataclasses, and a mutable
    protocol attribute is invariant and would match neither."""

    @property
    def value(self) -> object | None: ...

    @property
    def status(self) -> str: ...

    @property
    def conflict_status(self) -> str | None: ...


def _fact_text(value: GovernedRead | None) -> str:
    """One governed read as the plain string ``OperandFactsV2`` carries. A read that did not
    RESOLVE contributes ``""`` — the empty fact — rather than a raw value the C1 gates refused to
    serve; the hard-fail attribution beside it is what carries the reason."""
    if value is None or value.status != "resolved" or value.value is None:
        return ""
    return str(value.value)


def _hard_failure(value: GovernedRead | None, ref: str,
                  field: str) -> AuthorityFailure | None:
    """The typed attribution for a C1 read that failed CLOSED — the same three statuses v1 treats
    as fail-closed, imported from ``authoring`` so the two can never drift."""
    if value is None or getattr(value, "status", None) not in _C1_HARD_FAIL_STATUSES:
        return None
    return AuthorityFailure(
        reason=getattr(value, "conflict_status", None) or value.status, operand=ref, field=field)


def _capability_reason(proposal: TypedFormulaProposalV2) -> str:
    """WHY the proposal is out of v2 GRAMMAR capability — the catalog sources it spans, recomputed
    from the same refs ``classify_formula_capability_v2`` reads. (The ENGINE arm is C1's; this
    orchestrator passes ``engine=None`` and asks the grammar question only.)"""
    sources: set[str] = set()
    for expr in body_expressions_v2(proposal.body):
        sources.add(str(expr.source_relation.table_ref).split("::", 1)[0])
        if expr.operand is not None:
            sources.add(str(expr.operand).split("::", 1)[0])
    if len(sources) > 1:
        return "multiple_catalog_sources:" + ",".join(sorted(sources))
    return "aggregation_outside_v2_capability"


def _critic_status(findings: list[CriticFinding]) -> CriticStatus:
    """ANY blocking finding blocks (severity comes from the fixed map, never the model)."""
    if any(finding.severity == "blocking" for finding in findings):
        return "blocking"
    return "advisory" if findings else "clean"


def _expectation_status(proposal: TypedFormulaProposalV2,
                        policy: FormulaOutputPolicyV2 | None) -> ExpectationStatus:
    """Compare the proposal's ADVISORY ``expected_output`` against the AUTHORITATIVE policy.

    Only the fields the advisory actually ASSERTS (non-``None``) are compared — an omitted field is
    not a claim, so it can neither match nor mismatch. With no authoritative policy there is nothing
    to compare against, so the axis is ``not_provided``: the unresolved output already carries the
    review signal and inventing a mismatch there would double-count it."""
    expected = proposal.expected_output
    if expected is None or policy is None:
        return "not_provided"
    claimed = [
        (getattr(expected, "output_type", None), policy.output_type),
        (getattr(expected, "unit", None), policy.unit),
        (getattr(expected, "currency", None), policy.currency),
    ]
    asserted = [(a, b) for a, b in claimed if a is not None]
    if not asserted:
        return "not_provided"
    return "match" if all(_fold_text(a) == _fold_text(b) for a, b in asserted) else "mismatch"


def _fold_text(value: str | None) -> str | None:
    return None if value is None else str(value).strip().casefold()


def _axes(*, structural_status: StructuralStatus = "ok",
          capability_status: str = "ok",
          output_status: OutputStatus = "needs_authority",
          expectation_status: ExpectationStatus = "not_provided",
          critic_status: CriticStatus = "clean",
          technical_status: str = "ok") -> AuthoringAxes:
    """The six axes for a run that ended EARLY.

    The defaults are not an assumed all-clear: an early exit is taken precisely because a
    higher-precedence axis already decides the fold, and the stages that would have set the
    remaining axes never ran. ``output_status`` is the ONE axis whose "nothing was observed" value
    cannot be the clean one — ``"resolved"`` is a POSITIVE claim and an early exit happens before
    the authority stage runs, so ``"needs_authority"`` is the honest reading and it sits below
    technical/invalid/unsupported in the precedence, changing no early-exit disposition."""
    return AuthoringAxes(
        structural_status=structural_status,
        capability_status=capability_status,        # type: ignore[arg-type]
        output_status=output_status,
        expectation_status=expectation_status,
        critic_status=critic_status,
        technical_status=technical_status,          # type: ignore[arg-type]
    )
