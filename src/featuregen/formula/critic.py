"""Child-1 Task 10 — the INDEPENDENT, fail-closed critic (LLM-2). [fixes review#9]

``critique`` is a CRITIC, not a validator: one governed ``audited_formula_call`` inspects an
authored proposal against the intent and reports STRUCTURED findings from the CLOSED §G code set.
Two load-bearing invariants:

* INDEPENDENCE — the critic's ``catalog_metadata`` is assembled HERE (``build_critic_metadata``)
  from the intent + the proposal + the proposal's columns' governed facts RE-FETCHED from the
  catalog under the caller's read scope. It NEVER receives the author's reasoning or tool trace:
  the signature has no slot for a trail, and the context is a CLOSED three-key payload
  (``authoring_intent`` / ``proposal`` / ``operand_columns``). That is what makes LLM-2 a genuine
  second opinion rather than an echo of LLM-1.
* FAIL-CLOSED — a malformed/unparseable critic response, or an audited call with ``output=None``
  (egress-blocked / provider-failed / repair-exhausted), returns
  ``([], critic_findings_hash([]), is_technical_failure=True)``. A broken critic NEVER reads as
  clean (``([], hash, False)``) — it can never fold toward auto-RESOLVED (Task 12 maps the flag to
  ``technical_status``).

Severity is a FIXED property of each code (``_SEVERITY``), never taken from the model: the wire
schema tolerates an emitted ``severity`` and the parser IGNORES it. An unknown code, or a
duplicate ``(code, target)`` finding, is DROPPED with a recorded note (module logger) — never an
error, never blocking. The critic never mutates the proposal; it only reports.

``critic_findings_hash`` mirrors ``canonical.formula_content_hash``: sha256 over the RFC 8785
(JCS) bytes of the plain findings, deterministically SORTED by ``(code, operand, detail)`` so
emission order can never change the hash. It is computed even on technical failure (over the empty
list) so Task 12 always has a value.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.formula._jcs import dumps as _jcs_dumps
from featuregen.formula.audited import audited_formula_call
from featuregen.formula.control import LeaseFence
from featuregen.formula.schema import (
    DiffBody,
    FilterBool,
    FilterNode,
    FilterPredicate,
    RatioBody,
    SchemaError,
    TypedFormulaProposalV1,
    UnaryBody,
)
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.column_authority import read_column_facts
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities

if TYPE_CHECKING:
    from featuregen.formula.frozen_configuration import FrozenProviderContractV1

__all__ = [
    "CRITIC_FINDING_CODES",
    "CRITIC_FINDINGS_V1_SCHEMA",
    "CRITIC_SCHEMA",
    "CRITIC_INSTRUCTION",
    "CRITIC_POLICY_VERSION",
    "CRITIC_PROMPT_ID",
    "CRITIC_SCHEMA_ID",
    "CRITIC_SCHEMA_VERSION",
    "CRITIC_TASK",
    "CriticFinding",
    "CriticFindingCode",
    "CriticReview",
    "Severity",
    "build_critic_metadata",
    "critic_findings_hash",
    "critique",
    "proposal_column_refs",
]

logger = logging.getLogger(__name__)

CRITIC_POLICY_VERSION = 1

CRITIC_TASK = "formula.critic"
CRITIC_PROMPT_ID = "formula_critic_v1"
# The schema-registry identity the critic's structured findings are requested (and audited) under.
CRITIC_SCHEMA_ID = "formula_critic_findings"
CRITIC_SCHEMA_VERSION = 1
_SCHEMA_OWNER = "featuregen-formula"


class CriticFindingCode(StrEnum):
    """Spec §G — the CLOSED critic finding vocabulary (fixed severity; see ``_SEVERITY``)."""

    MISSING_REQUIRED_OPERAND = "MISSING_REQUIRED_OPERAND"
    WRONG_SLOT_DIRECTION = "WRONG_SLOT_DIRECTION"
    FILTER_INTENT_MISMATCH = "FILTER_INTENT_MISMATCH"
    WINDOW_INTENT_MISMATCH = "WINDOW_INTENT_MISMATCH"
    EXTRA_UNJUSTIFIED_OPERAND = "EXTRA_UNJUSTIFIED_OPERAND"
    WEAK_PROXY = "WEAK_PROXY"


# The spec's name for the closed set (§G): the StrEnum IS the vocabulary.
CRITIC_FINDING_CODES = CriticFindingCode

Severity = Literal["blocking", "advisory"]

# Severity is a FIXED property of the code — assigned from THIS map, never from the model.
_SEVERITY: Mapping[CriticFindingCode, Severity] = {
    CriticFindingCode.MISSING_REQUIRED_OPERAND: "blocking",
    CriticFindingCode.WRONG_SLOT_DIRECTION: "blocking",
    CriticFindingCode.FILTER_INTENT_MISMATCH: "blocking",
    CriticFindingCode.WINDOW_INTENT_MISMATCH: "blocking",
    CriticFindingCode.EXTRA_UNJUSTIFIED_OPERAND: "advisory",
    CriticFindingCode.WEAK_PROXY: "advisory",
}
assert set(_SEVERITY) == set(CriticFindingCode)   # the map is total over the closed set


@dataclass(frozen=True, slots=True)
class CriticFinding:
    """One kept critic finding — metadata-only, never an instruction to change the proposal.

    ``severity`` is ALWAYS ``_SEVERITY[code]``. ``operand`` is the logical_ref the finding refers
    to (None = the proposal as a whole) — the duplicate-detection target. ``detail`` is a short
    model-authored note about catalog METADATA; it never carries raw data values (the wire schema
    bounds it and the egress-guarded context contains none to echo)."""

    code: CriticFindingCode
    severity: Severity
    operand: str | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class CriticReview:
    """Audited critic result with backward-compatible three-value unpacking."""

    findings: Sequence[CriticFinding]
    findings_hash: str
    technical_failure: bool
    llm_call_ref: str | None
    provider_calls: int
    usage: dict

    def __iter__(self) -> Iterator[object]:
        yield self.findings
        yield self.findings_hash
        yield self.technical_failure


# The critic's structured-findings wire contract. ``code`` is DELIBERATELY a plain string (not an
# enum) on the wire: an unknown code must reach the parser to be dropped-with-a-note (§G), not be
# schema-rejected into a repair loop and a technical failure. ``severity`` is tolerated on the wire
# and IGNORED — the fixed ``_SEVERITY`` map is the only severity authority.
CRITIC_FINDINGS_V1_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string", "maxLength": 64},
                    "operand": {"anyOf": [{"type": "string", "maxLength": 256},
                                          {"type": "null"}]},
                    "detail": {"anyOf": [{"type": "string", "maxLength": 500},
                                         {"type": "null"}]},
                    "severity": {"type": "string", "maxLength": 16},
                },
                "required": ["code"],
            },
        },
    },
    "required": ["findings"],
}
# Frozen configuration uses this compatibility name while preserving main's immutable schema ID.
CRITIC_SCHEMA = CRITIC_FINDINGS_V1_SCHEMA

# The FIXED critic protocol instruction — the ONLY instruction text the critic model receives.
# Everything case-specific rides catalog_metadata (data, never instructions); the closed code list
# below is OUR text, enumerated from the StrEnum.
CRITIC_INSTRUCTION = (
    "You are the INDEPENDENT critic of ONE authored TypedFormula proposal. catalog_metadata "
    "carries the authoring intent, the proposal under review, and the governed catalog metadata "
    "of the proposal's columns, re-read from the catalog. You have NO access to the author's "
    "reasoning, and none is provided. Inspect the proposal against the intent and emit findings "
    "ONLY from this closed code set: "
    + ", ".join(code.value for code in CriticFindingCode) + ". "
    "For each finding set code, the operand logical_ref it refers to (or null for the proposal "
    "as a whole), and a short metadata-only detail. Emit an empty findings list when the proposal "
    "faithfully serves the intent. You report findings only — never rewrite the proposal and "
    "never echo data values."
)


def critic_findings_hash(findings: list[CriticFinding]) -> str:
    """sha256 over the JCS bytes of the plain findings, sorted by ``(code, operand, detail)``.

    Deterministic and order-independent (mirrors ``canonical.formula_content_hash``); defined on
    the empty list (the technical-failure value Task 12 still receives). Kept findings are unique
    on ``(code, operand)``; ``detail`` participates in the sort key only so the ordering stays
    total on arbitrary lists."""
    plains = [_finding_plain(finding) for finding in findings]
    plains.sort(key=lambda p: (p["code"], p["operand"] or "", p["detail"] or ""))
    return hashlib.sha256(_jcs_dumps(plains)).hexdigest()


def _finding_plain(finding: CriticFinding) -> dict:
    if not isinstance(finding, CriticFinding):
        raise SchemaError(
            f"critic_findings_hash covers CriticFinding only, got {type(finding).__name__}")
    return {"code": finding.code.value, "severity": finding.severity,
            "operand": finding.operand, "detail": finding.detail}


# ---- the independent context (never the author's trail) ----------------------------------------

# The governable column fields re-fetched for the critic context (the same field set the Task-9
# get_column_metadata tool projects — but read HERE, through the C1 authority adapter, never
# copied from the author's tool trail).
_COLUMN_FACT_FIELDS: tuple[str, ...] = (
    "additivity", "logical_representation", "is_grain", "is_as_of",
    "unit", "currency", "entity", "declared_type",
)


def proposal_column_refs(proposal: TypedFormulaProposalV1) -> tuple[str, ...]:
    """Every column logical_ref the proposal stands on — grain keys plus, per body expression,
    the operand, the window's event-time ref, and every filter-predicate left — sorted and
    deduplicated so the context (and any hash over it) is deterministic."""
    refs: set[str] = set(proposal.grain.keys)
    for expr in _body_expressions(proposal.body):
        if expr.operand is not None:
            refs.add(expr.operand)
        refs.add(expr.window.event_time_ref)
        if expr.filter is not None:
            _collect_filter_lefts(expr.filter, refs)
    return tuple(sorted(refs))


def _body_expressions(body):
    if isinstance(body, UnaryBody):
        return (body.expr,)
    if isinstance(body, RatioBody):
        return (body.numerator, body.denominator)
    if isinstance(body, DiffBody):
        return (body.minuend, body.subtrahend)
    raise SchemaError(f"body must be UnaryBody | RatioBody | DiffBody, got {type(body).__name__}")


def _collect_filter_lefts(node: FilterNode, refs: set[str]) -> None:
    if isinstance(node, FilterPredicate):
        refs.add(node.left)
    elif isinstance(node, FilterBool):
        for child in node.children:
            _collect_filter_lefts(child, refs)


def _column_context(conn, ref: str, roles: tuple[str, ...]) -> dict:
    """One column's governed facts, RE-FETCHED under the caller's read scope — the same
    metadata-only projection the authoring tools expose (value/authority/provenance per field,
    never ``definition`` free text, never data values). A malformed, table-level, unknown, or
    sensitivity-hidden ref reads as ``found: False`` — hidden is indistinguishable from
    nonexistent, and the critic simply sees an ungrounded ref."""
    try:
        source, schema, table, column = parse_ref(ref)
    except ValueError:
        return {"logical_ref": ref, "found": False}
    if column is None:
        return {"logical_ref": ref, "found": False}
    normalized = normalize_ref(source, schema, table, column)
    object_ref = f"public.{table}.{column}"
    row = conn.execute(
        "SELECT data_type, visible_requires FROM graph_node "
        "WHERE catalog_source = %s AND lower(object_ref) = %s AND kind = 'column'",
        (source.strip().lower(), object_ref.lower())).fetchone()
    if row is None or not set(row[1] or ()).issubset(allowed_sensitivities(roles)):
        return {"logical_ref": normalized, "found": False}
    facts = {}
    for field_name in _COLUMN_FACT_FIELDS:
        col = read_column_facts(conn, normalized, field_name)
        facts[field_name] = {
            "value": col.value, "authority": col.authority, "provenance": col.provenance}
    return {"logical_ref": normalized, "found": True, "table": table, "column": column,
            "data_type": row[0], "facts": facts}


def _proposal_plain(proposal: TypedFormulaProposalV1) -> dict:
    """The proposal as plain JSON types (the authored ARTIFACT under review — enums to their
    string values, tuples to lists). Never the author's reasoning: only the proposal itself."""
    if not isinstance(proposal, TypedFormulaProposalV1):
        raise SchemaError(
            f"critique covers TypedFormulaProposalV1 only, got {type(proposal).__name__}")
    # StrEnums are str subclasses and every literal value is already a canonical string, so a
    # JSON round-trip yields exactly the plain document the wire/audit layers expect.
    return json.loads(json.dumps(dataclasses.asdict(proposal)))


def build_critic_metadata(
    conn,
    intent: AuthoringIntent,
    proposal: TypedFormulaProposalV1,
    *,
    roles: tuple[str, ...],
    column_loader: Callable[[str], dict] | None = None,
) -> dict:
    """The critic's INDEPENDENTLY-assembled ``catalog_metadata``: the intent, the proposal, and
    the proposal's columns' governed facts RE-FETCHED here under ``roles``. EXACTLY these three
    keys — there is no slot for the author's reasoning or tool trace, so independence is
    structural, not a convention."""
    return {
        "authoring_intent": {
            "name": intent.name,
            "hypothesis": intent.hypothesis,
            "target_entity": intent.target_entity,
            "target_grain_keys": list(intent.target_grain_keys),
        },
        "proposal": _proposal_plain(proposal),
        "operand_columns": [
            column_loader(ref) if column_loader is not None else _column_context(conn, ref, roles)
            for ref in proposal_column_refs(proposal)
        ],
    }


# ---- the critic call ---------------------------------------------------------------------------


def _register_critic_schema(conn) -> None:
    """Idempotently register the findings output schema so the audited seam can resolve and
    validate it (its self-registration fallback covers only the enrichment schemas)."""
    DocumentSchemaRegistry(conn).register_immutable_schema(
        CRITIC_SCHEMA_ID, CRITIC_SCHEMA_VERSION, CRITIC_FINDINGS_V1_SCHEMA, _SCHEMA_OWNER)


def _parse_findings(output: dict | None) -> tuple[list[CriticFinding], bool, list[str]]:
    """``(kept findings, is_technical_failure, notes)`` from the critic's validated output.

    FAIL-CLOSED: ``output=None`` (egress-blocked / provider-failed / repair-exhausted) or any
    STRUCTURAL malformation — no list-valued ``findings``, a non-object entry, a missing/non-string
    code — is a technical failure with NO findings kept (never a clean critic). Only two things are
    tolerantly dropped, each with a note: a well-formed finding whose code is outside the closed
    set, and a duplicate ``(code, target)``. Severity always comes from ``_SEVERITY`` — an emitted
    severity is ignored, not even read."""
    if output is None:
        return [], True, []                     # already audited by the seam; nothing to parse
    raw = output.get("findings") if isinstance(output, Mapping) else None
    if not isinstance(raw, list):
        return [], True, ["malformed critic response: no list-valued 'findings'"]
    kept: list[CriticFinding] = []
    seen: set[tuple[CriticFindingCode, str | None]] = set()
    notes: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("code"), str):
            return [], True, [f"malformed critic finding at index {index}"]
        try:
            code = CriticFindingCode(entry["code"])
        except ValueError:
            notes.append(f"dropped finding with unknown code {entry['code']!r} "
                         "(outside the closed set)")
            continue
        operand = entry.get("operand")
        operand = operand if isinstance(operand, str) and operand else None
        detail = entry.get("detail")
        detail = detail if isinstance(detail, str) and detail else None
        target = (code, operand)
        if target in seen:
            notes.append(f"dropped duplicate finding {code.value} for target {operand!r}")
            continue
        seen.add(target)
        kept.append(CriticFinding(code=code, severity=_SEVERITY[code],
                                  operand=operand, detail=detail))
    return kept, False, notes


def critique(
    conn,
    intent: AuthoringIntent,
    proposal: TypedFormulaProposalV1,
    client: LLMClient,
    *,
    roles: tuple[str, ...] | list[str] | tuple[()] = (),
    actor: IdentityEnvelope | None = None,
    authoring_run_id: str,
    provider_contract: FrozenProviderContractV1 | None = None,
    metadata_loader: Callable[[str], dict] | None = None,
    progress_callback: Callable[[], None] | None = None,
    lease_fence: LeaseFence | None = None,
) -> CriticReview:
    """Run the independent critic ONCE over ``proposal`` against ``intent``.

    The result still unpacks as ``(findings, hash, is_technical_failure)`` for the original API. The
    single provider call
    goes through the Task-3 audited seam under ``authoring_run_id`` with the independently-built
    context (see ``build_critic_metadata`` — never the author's reasoning or tool trace). A
    malformed/unparseable response or a ``None`` output is ``([], hash-over-[], True)`` — a
    technical failure, NEVER a clean critic. The proposal is never mutated; the hash is computed
    on every path so Task 12 always has a value."""
    _register_critic_schema(conn)
    metadata = build_critic_metadata(
        conn,
        intent,
        proposal,
        roles=tuple(roles),
        column_loader=metadata_loader,
    )
    if progress_callback is not None:
        progress_callback()
    result = audited_formula_call(
        conn, client, authoring_run_id=authoring_run_id, task=CRITIC_TASK,
        prompt_id=(
            provider_contract.prompt_id if provider_contract is not None else CRITIC_PROMPT_ID),
        schema_id=(
            provider_contract.output_schema_id
            if provider_contract is not None
            else CRITIC_SCHEMA_ID),
        instruction=(
            provider_contract.instruction_utf8.decode("utf-8")
            if provider_contract is not None
            else CRITIC_INSTRUCTION),
        catalog_metadata=metadata,
        actor=actor,
        prompt_version=(
            provider_contract.prompt_version if provider_contract is not None else 1),
        schema_version=(
            provider_contract.output_schema_version
            if provider_contract is not None
            else CRITIC_SCHEMA_VERSION),
        generation_settings=(
            provider_contract.generation_settings()
            if provider_contract is not None
            else None),
        turn_index=0,
        provider_contract_hash=(
            provider_contract.contract_hash if provider_contract is not None else None),
        prompt_content_hash=(
            provider_contract.prompt_content_hash if provider_contract is not None else None),
        schema_content_hash=(
            provider_contract.schema_content_hash if provider_contract is not None else None),
        lease_fence=lease_fence,
    )
    if progress_callback is not None:
        progress_callback()
    findings, is_technical_failure, notes = _parse_findings(result.output)
    for note in notes:
        logger.warning("critic (run %s, call %s): %s",
                       authoring_run_id, result.llm_call_ref, note)
    return CriticReview(
        findings=findings,
        findings_hash=critic_findings_hash(findings),
        technical_failure=is_technical_failure,
        llm_call_ref=result.llm_call_ref,
        provider_calls=result.provider_calls,
        usage=result.usage,
    )
