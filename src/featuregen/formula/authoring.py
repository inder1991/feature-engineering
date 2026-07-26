"""Child-1 Task 12 — the authoring ORCHESTRATOR: the one place the eleven components compose.

``run_authoring`` drives ONE governed authoring run end to end::

    open_authoring_run (T11)  ->  author_formula (T9)   ->  parse_proposal_v1 (T2, which runs
    validate_semantics, T1)   ->  classify_formula_capability (T7)  ->
    resolve_formula_output_policy over C1 (T6, fed by read_operational_value, T5)  ->
    critique (T10, a SEPARATE context on its own client)  ->  derive_disposition (T8)  ->
    append_event(COMPLETED|FAILED) (T11)

and returns the multi-axis :class:`~featuregen.formula.result.AuthoringResult`. OFFLINE: no
execution, no Spark, no durable formula/version artifact — the only durable records are the
append-only ``llm_call`` audit rows and the write-once authoring trace.

FOUR invariants this module exists to hold, each one a defect if inverted:

* **The authoritative output comes ONLY from Task 6 over C1.** The proposal's ``expected_output``
  is the LLM's ADVISORY guess; it is compared (the §F expectation axis) and never consulted for
  authority. A resolved :class:`~featuregen.formula.schema.FormulaOutputPolicyV1` carrying
  ``external_type_required=True`` is NOT a resolved output — :func:`classify_output_policy` maps it
  to ``output_status="external_requirement"``. The §F fold sees only the six axes, never the policy,
  so this mapping is the single place an externally-unvalidated type could be laundered into
  RESOLVED.
* **Every :class:`AuthoringResult` is built by :func:`~featuregen.formula.result.derive_disposition`.**
  The coherence raise-guards (RESOLVED requires a ``TypedFormulaV1``; an unresolved-output
  NEEDS_REVIEW forbids one and carries the validated proposal; the hash is always derived) live
  there, so this module never imports the dataclass, let alone constructs it. Which artifact to
  offer is decided by :func:`forbids_authored_artifact`, pinned against the real fold over the whole
  axis cross-product in ``test_authoring.py``.
* **Technical outcomes never fabricate.** ``author_formula`` returning ``(None, turns)`` (max_turns
  exhaustion, token budget, an egress-blocked call, a discriminator without its slot) and a critic
  reporting ``is_technical_failure`` both become ``technical_status="technical_failure"`` — the
  highest-precedence §F arm, which carries no formula and no proposal.
* **``unsupported != invalid``.** An operation outside the §B vocabulary is
  ``structural_status="unsupported_operation"`` (→ UNSUPPORTED); only a genuinely malformed or
  semantically-violating proposal is ``invalid_formula`` (→ REJECTED).

TRACE DISCIPLINE (§H). The DB enforces "nothing after a terminal" and at most one terminal, but NOT
"STARTED first" nor ``seq`` monotonicity — those are THIS module's convention: seq counts from 0,
STARTED is always 0, and the terminal is always last. Payloads are CANONICAL REDACTED METADATA ONLY
(tool-result identities/verdicts/hashes, dispositions, reason codes, finding CODES) — never raw
catalog data values and never the model's free text, matching ``formula.tools``' egress discipline.
:func:`_metadata_payload` additionally coerces anything RFC 8785 cannot serialize (notably integers
outside ±2^53) so a trace write can never fail on a payload shape.

CONNECTION DISCIPLINE. Orchestration is SINGLE-THREADED on ``conn`` by contract. ``formula.trace``'s
degraded replay reasons about the caller's transaction status, and sharing one connection across
threads would let a concurrent statement move that state underneath it (the codebase precedent for
the right shape is ``runtime/dispatch.py``, which gives each handler thread its own connection).

FAILURE, DELIBERATELY. ``append_event`` RAISES on an already-aborted caller transaction, so a run
whose OWN SQL has failed cannot write its terminal event. This module therefore does NOT wrap the
chain in a rescue that would try: an unexpected exception PROPAGATES and the run stays
**incomplete** — which is exactly what ``run_status`` derives from the absence of a terminal event,
and exactly the honest answer. Nothing is ever swallowed into a false "completed".
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula._jcs import dumps as _jcs_dumps
from featuregen.formula.author import author_formula
from featuregen.formula.capability import CAPABILITY_POLICY_VERSION, classify_formula_capability
from featuregen.formula.critic import CRITIC_POLICY_VERSION, CriticFinding, critique
from featuregen.formula.operations import to_path_aggregation
from featuregen.formula.output_authority import (
    ExprFacts,
    ExternalRequirement,
    InvalidOutput,
    NeedsAuthority,
    resolve_formula_output_policy,
)
from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.result import (
    DISPOSITION_POLICY_VERSION,
    AuthoringAxes,
    AuthorityFailure,
    CriticStatus,
    ExpectationStatus,
    OutputStatus,
    StructuralStatus,
    derive_disposition,
)
from featuregen.formula.schema import (
    CANONICALIZATION_VERSION,
    FORMULA_SCHEMA_VERSION,
    OPERATION_GRAMMAR_VERSION,
    OUTPUT_POLICY_VERSION,
    AggregateFunction,
    DiffBody,
    FinalOperation,
    FormulaOutputPolicyV1,
    RatioBody,
    SchemaError,
    TypedFormulaProposalV1,
    TypedFormulaV1,
    UnaryBody,
)
from featuregen.formula.trace import TraceEventKind, append_event, open_authoring_run
from featuregen.formula.turns import AuthoringIntent, AuthorTurnRecord, TurnKind
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.operational_facts import (
    OperationalValue,
    read_operational_value,
)

if TYPE_CHECKING:
    # Deliberately TYPE_CHECKING-only: `AuthoringResult` must not exist as a runtime name in this
    # module, because a name that exists is a name that can be CONSTRUCTED — bypassing every
    # coherence raise-guard `derive_disposition` owns. `test_authoring.py` asserts both halves over
    # the AST: no runtime import, and no construction call anywhere.
    from featuregen.formula.result import AuthoringResult

__all__ = [
    "AUTHORING_MAX_TURNS",
    "AUTHORING_ORCHESTRATOR_VERSION",
    "AUTHORING_VERSIONS",
    "EXTERNAL_TYPE_VALIDATION_REQUIRED",
    "OutputResolution",
    "authoring_intent_hash",
    "classify_output_policy",
    "forbids_authored_artifact",
    "run_authoring",
]

#: Version of THIS orchestrator's wiring (stage order, axis mapping, trace vocabulary), stamped on
#: every run manifest alongside the rule/registry versions it composes.
AUTHORING_ORCHESTRATOR_VERSION = 1

#: The bounded ReAct budget every run gives the author. A module constant, not a per-call knob: the
#: public signature is the brief's verbatim one, and a caller-tunable bound on a governed authoring
#: run is a policy change, not a request parameter.
AUTHORING_MAX_TURNS = 8

#: Every rule/registry version a run is decided under, stamped on the §H manifest BEFORE any
#: provider call, so a later policy bump can never be mistaken for the one that produced a verdict.
AUTHORING_VERSIONS: dict[str, int] = {
    "formula_schema_version": FORMULA_SCHEMA_VERSION,
    "operation_grammar_version": OPERATION_GRAMMAR_VERSION,
    "output_policy_version": OUTPUT_POLICY_VERSION,
    "canonicalization_version": CANONICALIZATION_VERSION,
    "capability_policy_version": CAPABILITY_POLICY_VERSION,
    "critic_policy_version": CRITIC_POLICY_VERSION,
    "disposition_policy_version": DISPOSITION_POLICY_VERSION,
    "authoring_orchestrator_version": AUTHORING_ORCHESTRATOR_VERSION,
}

#: The typed requirement a numeric type that could not be GOVERNED raises. §C's
#: ``external_type_required`` says "this type must be validated outside the catalog before it can be
#: trusted" — a requirement, not a resolution.
EXTERNAL_TYPE_VALIDATION_REQUIRED = "EXTERNAL_TYPE_VALIDATION_REQUIRED"

#: The C1 statuses §C treats as FAIL-CLOSED on a required field. Mirrors
#: ``output_authority._HARD_FAIL_STATUSES`` — used ONLY to attribute WHICH read failed (Task 6 owns
#: the verdict; this is evidence reporting). Pinned against that module in ``test_authoring.py`` so
#: the two can never drift.
_C1_HARD_FAIL_STATUSES: frozenset[str] = frozenset(
    {"fork", "hash_mismatch", "projection_unavailable"})

#: The ``ExprFacts`` slot -> the C1 field name it is read from. The ONE place the mapping lives, so
#: the bundle the resolver receives and the attribution a failure carries can never disagree.
_EXPR_FACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("output_type", "logical_representation"),
    ("additivity", "additivity"),
    ("unit", "unit"),
    ("currency", "currency"),
)

#: The C1 field a grain key is read under.
_GRAIN_FIELD = "is_grain"

#: The §A INTERNAL EXPRESSION PATHS, per body shape. These — NOT operand refs, NOT slot indices —
#: are the keys Task 6 resolves ``per_expr_facts`` against; a bundle keyed by anything else silently
#: reads as ``ExprFacts()`` (every governed fact missing) and the resolver's output becomes a guess.
_BODY_PATHS: tuple[tuple[type, tuple[tuple[str, str], ...]], ...] = (
    (UnaryBody, (("body.expr", "expr"),)),
    (RatioBody, (("body.numerator", "numerator"), ("body.denominator", "denominator"))),
    (DiffBody, (("body.minuend", "minuend"), ("body.subtrahend", "subtrahend"))),
)

_AGGREGATION_VALUES: frozenset[str] = frozenset(fn.value for fn in AggregateFunction)
_FINAL_OPERATION_VALUES: frozenset[str] = frozenset(op.value for op in FinalOperation)
_TERMINAL_FOR_DISPOSITION: Mapping[str, TraceEventKind] = {
    "TECHNICAL_FAILURE": TraceEventKind.FAILED,
}


# ── output-axis classification (carry-forward #2 lives here) ─────────────────────────────────────

@dataclass(frozen=True, slots=True)
class OutputResolution:
    """What Task 6's return value means on the §F ``output_status`` axis.

    ``policy`` is the AUTHORITATIVE :class:`FormulaOutputPolicyV1` — present ONLY when the status is
    ``"resolved"``, so a caller physically cannot build a ``TypedFormulaV1`` out of an unresolved
    output. ``requirements`` carries the typed external requirements; ``reason`` the machine reason
    of a fail-closed or invalid arm."""

    status: OutputStatus
    policy: FormulaOutputPolicyV1 | None
    requirements: tuple[ExternalRequirement, ...] = ()
    reason: str | None = None


def classify_output_policy(resolved: object) -> OutputResolution:
    """Map Task 6's four-arm return onto the §F ``output_status`` axis.

    THE load-bearing case: a :class:`FormulaOutputPolicyV1` with ``external_type_required=True`` is
    NOT ``"resolved"``. §C sets that flag when a numeric type could not be granted GOVERNED
    authority (an ungoverned-but-numeric value, or no type at all) — the type must still be
    validated externally, so the output is an ``"external_requirement"``. Calling it resolved would
    hand the fold — which never sees the policy — an unqualified RESOLVED for a formula whose output
    type nothing has certified."""
    if isinstance(resolved, NeedsAuthority):
        return OutputResolution("needs_authority", None, reason=resolved.reason)
    if isinstance(resolved, ExternalRequirement):
        return OutputResolution("external_requirement", None, requirements=(resolved,),
                                reason=resolved.requirement)
    if isinstance(resolved, InvalidOutput):
        return OutputResolution("invalid_output", None, reason=resolved.reason)
    if isinstance(resolved, FormulaOutputPolicyV1):
        if resolved.external_type_required:
            requirement = ExternalRequirement(EXTERNAL_TYPE_VALIDATION_REQUIRED)
            return OutputResolution("external_requirement", None, requirements=(requirement,),
                                    reason=EXTERNAL_TYPE_VALIDATION_REQUIRED)
        return OutputResolution("resolved", resolved)
    # Task 6's return union is closed; an unknown arm fails CLOSED rather than toward resolution.
    return OutputResolution("needs_authority", None,
                            reason=f"unknown_output_resolution:{type(resolved).__name__}")


def forbids_authored_artifact(axes: AuthoringAxes) -> bool:
    """``True`` when the §F fold of ``axes`` will land on a disposition that carries NO authored
    artifact (UNSUPPORTED / REJECTED / TECHNICAL_FAILURE).

    The orchestrator must know this BEFORE calling :func:`derive_disposition`, because the artifact
    it offers has to be coherent with the disposition that call will produce. This mirrors the first
    three arms of the §F precedence; ``test_authoring.py`` pins it against the real fold over the
    complete axis cross-product (432 combinations), so the mirror can never drift silently."""
    return (
        axes.technical_status == "technical_failure"
        or axes.structural_status in ("invalid_formula", "unsupported_operation")
        or axes.capability_status == "unsupported_capability"
        or axes.output_status == "invalid_output"
    )


def authoring_intent_hash(intent: AuthoringIntent) -> str:
    """The content hash of an authoring intent — sha256 over the RFC 8785 (JCS) bytes of its plain
    form. Stamped on the §H manifest so a run is addressable by WHAT was asked, not just when."""
    return hashlib.sha256(_jcs_dumps({
        "name": intent.name,
        "hypothesis": intent.hypothesis,
        "target_entity": intent.target_entity,
        "target_grain_keys": list(intent.target_grain_keys),
    })).hexdigest()


# ── the orchestrator ─────────────────────────────────────────────────────────────────────────────

def run_authoring(conn, intent: AuthoringIntent, author_client: LLMClient,
                  critic_client: LLMClient, *, roles: tuple[str, ...] | list[str] | tuple[()],
                  actor: IdentityEnvelope) -> AuthoringResult:
    """Author ONE TypedFormula for ``intent`` and return the folded §F result.

    ``author_client`` and ``critic_client`` are DELIBERATELY separate: LLM-2 reviews the proposal
    from an independently-assembled context (``critic.build_critic_metadata``) with no slot for the
    author's reasoning or tool trail, so it is a second opinion rather than an echo. ``roles`` gates
    the MODEL-VISIBLE catalog reads — the author's tools (Task 9) and the critic's re-fetch (which
    gates on ``allowed_sensitivities(roles)`` itself). It does NOT reach this module's own C1
    AUTHORITY reads: ``read_operational_value`` (Task 5) and the ``read_column_facts`` beneath it take
    no ``roles`` parameter at all, so :func:`_read_c1_facts` is UN-SCOPED and the output policy is
    resolved over facts a read-scoped caller might not itself be allowed to see. A real gap,
    deliberately not closed here — adding the seam is a cross-task change to Tasks 5/6. What it does
    NOT do is widen model egress: nothing :func:`_read_c1_facts` reads is ever sent to a provider.
    ``actor`` is stamped on the manifest and threaded through both audited seams.

    EVERY stage runs once a proposal parses, even when an earlier axis already decides the fold (an
    out-of-capability proposal is still resolved and still criticized). That is deliberate: §F says
    each axis is an explicit upstream verdict, never an assumed all-clear, and short-circuiting would
    record ``"ok"``/``"clean"`` for stages that never ran. The cost is one critic call on a proposal
    that cannot be built; the §F precedence guarantees it can never change the disposition.

    Single-threaded on ``conn`` by contract (module docstring). Raises whatever the chain raises —
    a run whose own SQL failed stays honestly INCOMPLETE rather than claiming a terminal state."""
    role_tuple = tuple(roles)
    run_id = open_authoring_run(
        conn, intent_hash=authoring_intent_hash(intent), versions=AUTHORING_VERSIONS, actor=actor)
    trace = _Trace(conn, run_id)
    trace.append(TraceEventKind.STARTED, {
        "intent_hash": authoring_intent_hash(intent),
        "intent_name": intent.name,
        "target_entity": intent.target_entity,
        "target_grain_key_count": len(intent.target_grain_keys),
        "max_turns": AUTHORING_MAX_TURNS,
    })

    raw_proposal, turns = author_formula(
        conn, intent, author_client, roles=role_tuple, max_turns=AUTHORING_MAX_TURNS,
        actor=actor, authoring_run_id=run_id)
    _trace_turns(trace, turns)

    if raw_proposal is None:
        # (None, turns): max_turns exhaustion / token budget / egress block / a discriminator
        # without its slot. TECHNICAL — a proposal is NEVER fabricated, and the critic is never
        # asked to review something that does not exist.
        return _finish(trace, _axes(technical_status="technical_failure"))

    structural_status, proposal = _parse(raw_proposal)
    if proposal is None:
        return _finish(trace, _axes(structural_status=structural_status))

    capability_status = classify_formula_capability(proposal)
    capability_reason = (
        None if capability_status == "ok" else _capability_reason(proposal))

    per_expr_facts, grain_facts = _read_c1_facts(conn, proposal)
    resolution = classify_output_policy(resolve_formula_output_policy(
        proposal, per_expr_facts=per_expr_facts, grain_facts=grain_facts, now=datetime.now(UTC)))
    authority_failures = (
        _attribute_authority_failures(per_expr_facts, grain_facts, resolution.reason)
        if resolution.status == "needs_authority" else ())

    findings, critic_findings_hash, critic_technical = critique(
        conn, intent, proposal, critic_client, roles=role_tuple, actor=actor,
        authoring_run_id=run_id)
    critic_status = _critic_status(findings)
    trace.append(TraceEventKind.CRITIC_RECORDED, {
        "critic_findings_hash": critic_findings_hash,
        "critic_policy_version": CRITIC_POLICY_VERSION,
        "critic_status": critic_status,
        "technical_failure": critic_technical,
        # CODES + targets only: the model-authored ``detail`` is free text and never enters the
        # trace (metadata-only discipline); the hash covers it for tamper-evidence.
        "findings": [{"code": f.code.value, "severity": f.severity, "operand": f.operand}
                     for f in findings],
    })

    axes = _axes(
        structural_status=structural_status,
        capability_status=capability_status,
        output_status=resolution.status,
        expectation_status=_expectation_status(proposal, resolution.policy),
        critic_status=critic_status,
        # A technical critic is ``([], hash, True)`` — NEVER the clean shape ``([], hash, False)``.
        # It surfaces on the TECHNICAL axis (which dominates the fold), so ``critic_status`` is only
        # ever read together with it; the critic produced no verdict at all.
        technical_status="technical_failure" if critic_technical else "ok",
    )
    return _finish(
        trace, axes,
        formula=_typed_formula(proposal, resolution.policy),
        proposal=proposal,
        output_requirements=resolution.requirements,
        authority_failures=authority_failures,
        capability_reason=capability_reason,
        critic_findings_hash=critic_findings_hash,
    )


def _finish(trace: _Trace, axes: AuthoringAxes, *, formula: TypedFormulaV1 | None = None,
            proposal: TypedFormulaProposalV1 | None = None,
            output_requirements: tuple[ExternalRequirement, ...] = (),
            authority_failures: tuple[AuthorityFailure, ...] = (),
            capability_reason: str | None = None,
            critic_findings_hash: str | None = None) -> AuthoringResult:
    """Fold the axes through Task 8 (the ONLY constructor of an ``AuthoringResult``), append the
    single terminal event, and return the result.

    Artifact coherence is decided HERE and enforced THERE: an authored ``TypedFormulaV1`` is offered
    only when one actually exists (the output resolved) AND the disposition permits it; an
    unresolved-output review carries the validated proposal instead. ``derive_disposition`` raises
    on any combination that contradicts its fold, so a mistake here fails loudly rather than
    shipping an incoherent result."""
    carries_artifact = not forbids_authored_artifact(axes)
    resolved_output = axes.output_status == "resolved"
    result = derive_disposition(
        axes,
        authoring_run_id=trace.run_id,
        candidate_formula=formula if (carries_artifact and resolved_output) else None,
        candidate_proposal=proposal if (carries_artifact and not resolved_output) else None,
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
            "candidate_formula_hash": result.candidate_formula_hash,
            "critic_findings_hash": result.critic_findings_hash,
            "output_requirements": [r.requirement for r in result.output_requirements],
            "authority_failures": [
                {"reason": f.reason, "operand": f.operand, "field": f.field}
                for f in result.authority_failures],
        })
    return result


# ── stage helpers ────────────────────────────────────────────────────────────────────────────────

def _parse(raw: Mapping[str, Any]) -> tuple[StructuralStatus, TypedFormulaProposalV1 | None]:
    """Parse + semantically validate the raw proposal, classifying a failure as UNSUPPORTED or
    INVALID — never conflating them.

    ``unsupported != invalid`` (§B/§F): a body naming an operation outside the frozen vocabulary
    (``avg``, ``min``, ``stddev``, an unknown ``final_operation``) is out of the v1 GRAMMAR, not
    malformed, so it is ``unsupported_operation`` and folds to UNSUPPORTED. Everything else the
    shape gate or ``validate_semantics`` rejects is ``invalid_formula`` → REJECTED. A parsed
    proposal whose aggregate function has no §B path aggregation is unsupported too (belt and
    braces: the map is total over v1, so this arm is reachable only if the vocabulary widens)."""
    if _names_an_unsupported_operation(raw):
        return "unsupported_operation", None
    try:
        proposal = parse_proposal_v1(raw)
    except SchemaError:
        return "invalid_formula", None
    for _path, expr in _expressions(proposal):
        if to_path_aggregation(expr.aggregation) is None:   # pragma: no cover - total in v1
            return "unsupported_operation", None
    return "ok", proposal


def _names_an_unsupported_operation(raw: Mapping[str, Any]) -> bool:
    """Does the RAW body name an operation outside the frozen §A/§B vocabulary?

    Read from the raw dict deliberately: the shape gate would reject such a value as an enum
    violation, which is indistinguishable from real malformation once ``SchemaError`` is raised —
    and reporting "invalid" for a perfectly well-formed request the grammar simply does not cover
    yet is the exact conflation §F forbids."""
    body = raw.get("body") if isinstance(raw, Mapping) else None
    if not isinstance(body, Mapping):
        return False
    final_operation = body.get("final_operation")
    if isinstance(final_operation, str) and final_operation not in _FINAL_OPERATION_VALUES:
        return True
    for slot in ("expr", "numerator", "denominator", "minuend", "subtrahend"):
        expr = body.get(slot)
        if isinstance(expr, Mapping):
            aggregation = expr.get("aggregation")
            if isinstance(aggregation, str) and aggregation not in _AGGREGATION_VALUES:
                return True
    return False


def _expressions(proposal: TypedFormulaProposalV1):
    """``(internal path, expression)`` for every aggregate expression in the body — the §A paths."""
    for body_type, slots in _BODY_PATHS:
        if isinstance(proposal.body, body_type):
            return tuple((path, getattr(proposal.body, attr)) for path, attr in slots)
    raise SchemaError(   # pragma: no cover - the body union is closed after Task 1/2
        f"body must be UnaryBody | RatioBody | DiffBody, got {type(proposal.body).__name__}")


def _read_c1_facts(conn, proposal: TypedFormulaProposalV1
                   ) -> tuple[dict[str, ExprFacts], dict[str, OperationalValue]]:
    """Build Task 6's two governed-fact bundles from REAL C1 reads.

    ``per_expr_facts`` is keyed by the §A INTERNAL BODY PATH of each expression (``body.expr`` /
    ``body.numerator`` / ``body.denominator`` / ``body.minuend`` / ``body.subtrahend``) —
    ``resolve_formula_output_policy`` looks its operands up by exactly those strings, and a bundle
    keyed by anything else (an operand ref, a slot index) resolves to an empty ``ExprFacts`` for
    every operand: no governed type, no governed additivity, and a policy assembled from nothing.
    ``grain_facts`` is keyed by grain-key ``logical_ref`` (what §C's COUNT_ROWS grain check reads).

    ``read_operational_value`` is the ONLY authority read here: never the author's tool trail (which
    is model-visible), never the flat ``graph_node`` columns (which have no tamper gate)."""
    per_expr_facts: dict[str, ExprFacts] = {}
    for path, expr in _expressions(proposal):
        if expr.operand is None:
            per_expr_facts[path] = ExprFacts()      # COUNT_ROWS has no operand to read (§A)
            continue
        per_expr_facts[path] = ExprFacts(**{
            slot: read_operational_value(conn, expr.operand, field)
            for slot, field in _EXPR_FACT_FIELDS})
    grain_facts = {
        key: read_operational_value(conn, key, _GRAIN_FIELD) for key in proposal.grain.keys}
    return per_expr_facts, grain_facts


def _attribute_authority_failures(per_expr_facts: Mapping[str, ExprFacts],
                                  grain_facts: Mapping[str, OperationalValue],
                                  reason: str | None) -> tuple[AuthorityFailure, ...]:
    """WHICH operand/field failed closed, for a NEEDS_AUTHORITY output.

    Task 6 owns the VERDICT and returns only a machine reason; this walks the same reads to report
    the evidence behind it. Deterministic order (body path, then the §C field order). If nothing
    matches — the resolver refused for a reason no single read carries — the reason is still
    reported, locator-less, rather than silently dropped."""
    failures: list[AuthorityFailure] = []
    for path, facts in per_expr_facts.items():
        for slot, field in _EXPR_FACT_FIELDS:
            ov = getattr(facts, slot)
            if ov is not None and getattr(ov, "status", None) in _C1_HARD_FAIL_STATUSES:
                failures.append(AuthorityFailure(
                    reason=getattr(ov, "conflict_status", None) or ov.status,
                    operand=path, field=field))
    for key, ov in grain_facts.items():
        if ov is not None and getattr(ov, "status", None) in _C1_HARD_FAIL_STATUSES:
            failures.append(AuthorityFailure(
                reason=getattr(ov, "conflict_status", None) or ov.status,
                operand=key, field=_GRAIN_FIELD))
    if not failures and reason is not None:
        failures.append(AuthorityFailure(reason=reason))
    return tuple(failures)


def _capability_reason(proposal: TypedFormulaProposalV1) -> str:
    """WHY the proposal is out of v1 capability — the catalog sources it spans (the single guard v1
    enforces), recomputed from the same refs ``classify_formula_capability`` reads."""
    sources: set[str] = set()
    for _path, expr in _expressions(proposal):
        sources.add(expr.source_relation.table_ref.split("::", 1)[0])
        if expr.operand is not None:
            sources.add(expr.operand.split("::", 1)[0])
    if len(sources) > 1:
        return "multiple_catalog_sources:" + ",".join(sorted(sources))
    return "body_shape_outside_v1_capability"   # pragma: no cover - closed union after Task 1/2


def _critic_status(findings: list[CriticFinding]) -> CriticStatus:
    """§G: ANY blocking finding blocks (severity comes from the fixed map, never the model)."""
    if any(finding.severity == "blocking" for finding in findings):
        return "blocking"
    return "advisory" if findings else "clean"


def _expectation_status(proposal: TypedFormulaProposalV1,
                        policy: FormulaOutputPolicyV1 | None) -> ExpectationStatus:
    """Compare the proposal's ADVISORY ``expected_output`` against the AUTHORITATIVE policy.

    Only the fields the advisory actually ASSERTS (non-``None``) are compared — an omitted field is
    not a claim, so it can neither match nor mismatch. With no authoritative policy there is nothing
    to compare against, so the axis is ``not_provided``: the unresolved output is already carrying
    the review signal, and inventing a mismatch there would double-count it. Comparison is on
    stripped, case-folded text: the advisory is prose, the policy is a governed identifier, and a
    casing difference is not a disagreement about the value."""
    expected = proposal.expected_output
    if expected is None or policy is None:
        return "not_provided"
    claimed = [
        (expected.output_type, policy.output_type),
        (expected.unit, policy.unit),
        (expected.currency, policy.currency),
    ]
    asserted = [(a, b) for a, b in claimed if a is not None]
    if not asserted:
        return "not_provided"
    return "match" if all(_fold_text(a) == _fold_text(b) for a, b in asserted) else "mismatch"


def _fold_text(value: str | None) -> str | None:
    return None if value is None else str(value).strip().casefold()


def _typed_formula(proposal: TypedFormulaProposalV1,
                   policy: FormulaOutputPolicyV1 | None) -> TypedFormulaV1 | None:
    """The AUTHORITATIVE identity object — proposal structure + the C1-resolved output policy.

    ``None`` when the output did not resolve: there is then no authoritative policy, and a
    ``TypedFormulaV1`` cannot be assembled without one (which is precisely why an unresolved output
    can carry only the proposal). ``output_policy_version`` is the module's pin, never the
    proposal's — the proposal has no such field, because the output is not the author's to declare.
    The advisory ``expected_output`` is dropped here: it is not identity-bearing (§E)."""
    if policy is None:
        return None
    return TypedFormulaV1(
        formula_schema_version=proposal.formula_schema_version,
        operation_grammar_version=proposal.operation_grammar_version,
        output_policy_version=OUTPUT_POLICY_VERSION,
        canonicalization_version=proposal.canonicalization_version,
        grain=proposal.grain,
        body=proposal.body,
        parameters=proposal.parameters,
        decimal=proposal.decimal,
        output=policy,
    )


def _axes(*, structural_status: StructuralStatus = "ok",
          capability_status: str = "ok",
          output_status: OutputStatus = "needs_authority",
          expectation_status: ExpectationStatus = "not_provided",
          critic_status: CriticStatus = "clean",
          technical_status: str = "ok") -> AuthoringAxes:
    """The six axes for a run that ended EARLY.

    The defaults are not an assumed all-clear: an early exit is taken precisely because a
    higher-precedence axis already decides the fold (technical, or a structural verdict), and the
    stages that would have set the remaining axes never ran — so ``"ok"``/``"clean"`` here means
    "nothing was observed", and the §F precedence guarantees it is never what the disposition turns
    on. Every full run passes all six explicitly.

    ``output_status`` is the ONE axis whose "nothing was observed" value cannot be the clean one:
    ``"resolved"`` is a POSITIVE claim — the C1 authority stage resolved this run's output — and an
    early exit is taken before that stage ever runs. Writing it onto the returned result and into
    the write-once §H terminal payload would be a dishonest durable record of exactly the thing §J's
    ``false_resolve_rate`` exists to prevent. ``"needs_authority"`` is the honest reading (no
    authority was established) and sits BELOW technical/invalid/unsupported in the §F precedence, so
    it changes no disposition on any early-exit path."""
    return AuthoringAxes(
        structural_status=structural_status,
        capability_status=capability_status,        # type: ignore[arg-type]
        output_status=output_status,
        expectation_status=expectation_status,
        critic_status=critic_status,
        technical_status=technical_status,          # type: ignore[arg-type]
    )


# ── the §H trace ─────────────────────────────────────────────────────────────────────────────────

class _Trace:
    """The run's append-only event writer: monotone ``seq`` from 0, STARTED first, one terminal.

    The database enforces at most one terminal and nothing after it; ORDERING and MONOTONICITY are
    this class's contract (``authoring_trace_event`` has a ``UNIQUE (run, seq)`` but no ordering
    rule). Single-threaded by contract — see the module docstring on connection discipline."""

    __slots__ = ("conn", "run_id", "_seq")

    def __init__(self, conn, run_id: str) -> None:
        self.conn = conn
        self.run_id = run_id
        self._seq = 0

    def append(self, kind: TraceEventKind, payload: Mapping[str, Any],
               llm_call_ref: str | None = None) -> None:
        seq = self._seq
        self._seq += 1
        append_event(self.conn, self.run_id, kind, seq=seq,
                     idempotency_key=f"{self.run_id}:{seq}", llm_call_ref=llm_call_ref,
                     payload=_metadata_payload(payload))


#: The tool-result fields that echo the MODEL's own words (see :func:`_trace_turns`): a jsonschema
#: message quoting the instance value the model wrote, or a bad-argument error quoting it. Omitted
#: from the write-once trace exactly as the critic's model-authored ``detail`` is.
_MODEL_FREE_TEXT_KEYS: frozenset[str] = frozenset({"detail", "error"})


def _trace_turns(trace: _Trace, turns: list[AuthorTurnRecord]) -> None:
    """One LLM_CALL_RECORDED per author turn (linked to its immutable audit row), plus
    TOOL_CALLED + TOOL_RESULT_RECORDED for a tool turn — the canonical tool result MINUS its
    model-echoing free text, with the sha256 of the FULL result's RFC 8785 bytes.

    Tool results are metadata-only as to the CATALOG by construction (``formula.tools`` returns
    column/grain/operation METADATA and never data values or ``graph_node.definition``), which is
    what makes them storable here at all. Two fields nevertheless echo the MODEL's own words back:
    ``validate_draft_formula``'s ``detail`` is a ``str(SchemaError)``, and jsonschema messages quote
    the offending instance value; ``_error``'s ``error`` quotes a bad argument (a ``logical_ref`` the
    model wrote). Both are dropped — the SAME omission the CRITIC_RECORDED payload makes for the
    model-authored finding ``detail``, and for the same reason: the §H payload is metadata only, and
    migration 1020 makes the row physically immutable, so nothing written here can ever be
    remediated. ``result_hash`` covers the FULL canonical result (again mirroring the critic, whose
    ``critic_findings_hash`` covers the ``detail`` its payload omits), so the bytes the model actually
    saw stay tamper-evident without being stored."""
    for turn in turns:
        trace.append(TraceEventKind.LLM_CALL_RECORDED, {
            "turn": turn.index,
            "turn_kind": turn.kind.value,
            "provider_calls": turn.provider_calls,
            "input_tokens": turn.usage.get("input_tokens"),
            "output_tokens": turn.usage.get("output_tokens"),
        }, llm_call_ref=turn.llm_call_ref)
        if turn.kind is not TurnKind.TOOL_CALL:
            continue
        trace.append(TraceEventKind.TOOL_CALLED,
                     {"turn": turn.index, "tool_name": turn.tool_name})
        result = _metadata_payload(turn.tool_result or {})
        trace.append(TraceEventKind.TOOL_RESULT_RECORDED, {
            "turn": turn.index,
            "tool_name": turn.tool_name,
            "result": {k: v for k, v in result.items() if k not in _MODEL_FREE_TEXT_KEYS},
            "result_hash": hashlib.sha256(_jcs_dumps(result)).hexdigest(),
        })


#: RFC 8785 rejects integers outside IEEE-754's exact range (``_jcs.IntegerDomainError``), and jsonb
#: takes only JSON types. A trace write must never fail on a payload SHAPE, so anything outside the
#: representable set is carried as its text form rather than dropped or raised on.
_JCS_INT_MAX = 2**53 - 1
_JCS_INT_MIN = -(2**53) + 1


def _metadata_payload(value: Any) -> Any:
    """Coerce a payload into canonical, JCS-serializable JSON, structure preserved.

    This is a SHAPE guard, not a redaction: what may be stored is decided at the call site (codes,
    hashes, verdicts, canonical tool results — never raw catalog data values or model free text).
    Non-string mapping keys become text, out-of-range integers become text, and anything that is not
    a JSON primitive is stringified — so a payload can never crash the trace write it evidences."""
    if isinstance(value, Mapping):
        return {str(key): _metadata_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_metadata_payload(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value if _JCS_INT_MIN <= value <= _JCS_INT_MAX else str(value)
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    return str(value)
