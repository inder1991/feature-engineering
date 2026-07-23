"""Child-1 Task 9 — the sequential-turn author's TURN contract (spec §I).

``AuthorTurnV1 = ToolCallV1 | FinalProposalV1``: on every turn the model emits exactly ONE of the
two — a call to one of the 7 governed catalog-authoring tools, or the final raw proposal. The union
rides a DISCRIMINATED ENVELOPE (``turn_type`` + one matching slot) rather than a top-level ``oneOf``
because the provider's structured-output format requires a top-level object; the author loop
enforces (fail-closed) that the discriminator's slot is actually present — a shape the schema alone
cannot force without requiring BOTH slots.

``FinalProposalV1`` is byte-for-byte the Task-2 proposal contract: its slot is the REAL
``proposal_v1.schema.json`` (hoisted into this schema's ``$defs`` so its ``#/$defs/...`` refs
resolve), so the model is held to the exact shape ``parse_proposal_v1`` consumes — but the author
NEVER parses it; the raw dict passes through to the later parse/validate stages (Tasks 2/6/7/10).

Everything here is provider-compatible AFTER ``project_for_anthropic`` (length/size bounds are
wire-stripped; the canonical bounds still govern response validation).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "AUTHOR_TURN_SCHEMA_ID",
    "AUTHOR_TURN_SCHEMA_VERSION",
    "AUTHOR_TURN_V1_SCHEMA",
    "AuthoringIntent",
    "AuthorTurnRecord",
    "FINAL_PROPOSAL_V1_SCHEMA",
    "TOOL_CALL_V1_SCHEMA",
    "TOOL_NAMES",
    "TURN_TYPE_FINAL_PROPOSAL",
    "TURN_TYPE_TOOL_CALL",
    "TurnKind",
]

# The schema-registry identity every author turn is requested (and audited) under.
AUTHOR_TURN_SCHEMA_ID = "formula_author_turn"
AUTHOR_TURN_SCHEMA_VERSION = 1

# §I — the 7 governed catalog-authoring tools (read/validate-only; the registry lives in tools.py).
TOOL_NAMES: tuple[str, ...] = (
    "search_columns",
    "get_column_metadata",
    "get_governed_grain",
    "get_time_anchor",
    "get_verified_lineage",
    "list_supported_operations",
    "validate_draft_formula",
)

TURN_TYPE_TOOL_CALL = "tool_call"
TURN_TYPE_FINAL_PROPOSAL = "final_proposal"

# The Task-2 proposal contract, reused VERBATIM as the FinalProposalV1 slot. Its $defs are hoisted
# to the turn schema's root so the internal "#/$defs/..." refs keep resolving when embedded.
_proposal = json.loads(
    Path(__file__).with_name("proposal_v1.schema.json").read_text(encoding="utf-8"))
_PROPOSAL_DEFS: dict = _proposal["$defs"]
_PROPOSAL_NODE: dict = {
    k: v for k, v in _proposal.items() if k not in ("$schema", "$id", "title", "$defs")}

# FinalProposalV1 — the raw proposal dict, exactly the shape parse_proposal_v1 consumes. A $ref
# into the shared $defs (resolvable within AUTHOR_TURN_V1_SCHEMA, where it is always embedded).
FINAL_PROPOSAL_V1_SCHEMA: dict = {"$ref": "#/$defs/finalProposal"}

# ToolCallV1 — {tool_name ∈ the 7, arguments}. `arguments` is CLOSED over the union of every
# tool's argument keys (a provider-compatible stand-in for per-tool schemas: the per-tool
# input_schema in tools.TOOLS is authoritative, and each tool re-validates its own arguments).
TOOL_CALL_V1_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tool_name": {"type": "string", "enum": list(TOOL_NAMES)},
        "arguments": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                "logical_ref": {"type": "string", "maxLength": 256},
                "catalog_source": {"type": "string", "maxLength": 128},
                "table": {"type": "string", "maxLength": 128},
                "ref": {"type": "string", "maxLength": 256},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3},
                "proposal": {"$ref": "#/$defs/finalProposal"},
            },
        },
    },
    "required": ["tool_name", "arguments"],
}

# AuthorTurnV1 — the discriminated envelope of the union (see module docstring). This is the
# canonical schema registered under AUTHOR_TURN_SCHEMA_ID and requested as every audited authoring
# call's output_schema.
AUTHOR_TURN_V1_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "turn_type": {
            "type": "string",
            "enum": [TURN_TYPE_TOOL_CALL, TURN_TYPE_FINAL_PROPOSAL],
        },
        "tool_call": TOOL_CALL_V1_SCHEMA,
        "final_proposal": FINAL_PROPOSAL_V1_SCHEMA,
    },
    "required": ["turn_type"],
    "$defs": {**_PROPOSAL_DEFS, "finalProposal": _PROPOSAL_NODE},
}


@dataclass(frozen=True, slots=True)
class AuthoringIntent:
    """The MINIMAL authoring request the author works from (shadow-phase; deliberately small).

    ``hypothesis`` is requester-authored free text: it rides ``catalog_metadata`` (metadata about
    what to author, never data values) and passes the same egress guard every payload does."""

    name: str
    hypothesis: str
    target_entity: str
    target_grain_keys: tuple[str, ...] = ()


class TurnKind(StrEnum):
    """What one recorded author turn was."""

    TOOL_CALL = "tool_call"            # the model called a tool; its canonical result is recorded
    FINAL_PROPOSAL = "final_proposal"  # the model emitted the raw proposal (the run's last turn)
    FAILED = "failed"                  # no usable output (egress block / provider failure /
    #                                    discriminator-slot mismatch) — the run ends TECHNICAL


@dataclass(frozen=True, slots=True)
class AuthorTurnRecord:
    """One recorded turn of an authoring run — exactly one audited governed call.

    ``llm_call_ref`` is the immutable audit row for the turn's call; the audited seam records even
    an egress-BLOCKED call, so every turn a run yields carries a real ref (None only if the
    underlying audit itself was impossible). ``usage`` is the provider-reported cost metadata that
    drives the author's token budget."""

    index: int                    # 0-based position in the run
    kind: TurnKind
    llm_call_ref: str | None
    tool_name: str | None         # TOOL_CALL turns only
    tool_result: dict | None      # the canonical tool result (TOOL_CALL turns only)
    output: dict | None           # the validated AuthorTurnV1 the model emitted (None if FAILED
    #                               with no output)
    provider_calls: int
    usage: dict
