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
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.formula._jcs import dumps as _jcs_dumps
from featuregen.formula.audited import audited_formula_call
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

__all__ = [
    "CRITIC_FINDING_CODES",
    "CRITIC_FINDINGS_V1_SCHEMA",
    "CRITIC_INSTRUCTION",
    "CRITIC_POLICY_VERSION",
    "CRITIC_PROMPT_ID",
    "CRITIC_SCHEMA_ID",
    "CRITIC_SCHEMA_VERSION",
    "CRITIC_TASK",
    "CriticFinding",
    "CriticFindingCode",
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


def proposal_column_refs(proposal: TypedFormulaProposalV1) -> tuple[str, ...]:
    """Every column logical_ref the proposal stands on, sorted + deduplicated."""
    raise NotImplementedError


def build_critic_metadata(conn, intent: AuthoringIntent, proposal: TypedFormulaProposalV1, *,
                          roles: tuple[str, ...]) -> dict:
    """The critic's INDEPENDENTLY-assembled, read-scoped, metadata-only context."""
    raise NotImplementedError


def critique(
    conn,
    intent: AuthoringIntent,
    proposal: TypedFormulaProposalV1,
    client: LLMClient,
    *,
    roles: tuple[str, ...] | list[str] | tuple[()] = (),
    actor: IdentityEnvelope | None = None,
    authoring_run_id: str,
) -> tuple[list[CriticFinding], str, bool]:
    """Run the independent critic once; return ``(findings, critic_findings_hash,
    is_technical_failure)``."""
    raise NotImplementedError
