"""Task A3 — the Formula-**v2** author TURN contract, beside the frozen v1 one.

``turns.py`` holds ``AuthorTurnV1``: the same discriminated envelope (``turn_type`` + exactly one
matching slot) whose ``final_proposal`` slot is ``proposal_v1.schema.json``. This module holds the
one and only v2 fork of it — the envelope is IDENTICAL, the proposal slot is
``proposal_v2.schema.json``, and the tool half is imported verbatim from v1 because the seven
governed catalog-authoring tools are version-neutral (they read the catalog, not the grammar).

Why a second schema id at all: the audited seam records the ``output_schema_id`` +
``output_schema_version`` a call was made under, and ``frozen_configuration`` HASHES them. A v2 run
requested under the v1 identity would be indistinguishable in the audit from a v1 run, and a frozen
provider contract could not tell the two apart. So ``formula_author_turn_v2`` is a distinct
registered schema, not a version bump of the first.

THE SAME WIRE RELAXATION v1 makes, for the same reason (§B/§F ``unsupported != invalid``):
``aggregateExpression.aggregation`` and ``formulaBody.final_operation`` are opened to any string on
the WIRE, so a model asked for an average emits ``"avg"``, the turn VALIDATES, and the orchestrator's
raw-body inspection classifies it ``unsupported_operation`` -> UNSUPPORTED. With the enum pinned on
the wire the response-schema check would reject the turn, the repair loop would exhaust, and the
requester would be told "the loop broke" instead of "the v2 grammar does not carry averages".
The relaxation is applied to a COPY: the strict ``proposal_v2.schema.json`` that
``parse_proposal_v2`` loads is never touched and stays the only gate a proposal is accepted through.
"""
from __future__ import annotations

import json
from pathlib import Path

from featuregen.formula.turns import (
    TOOL_CALL_V1_SCHEMA,
    TURN_TYPE_FINAL_PROPOSAL,
    TURN_TYPE_TOOL_CALL,
)
from featuregen.intake.schema_projection import declare_enum_types

__all__ = [
    "AUTHOR_TURN_SCHEMA_ID_V2",
    "AUTHOR_TURN_SCHEMA_VERSION_V2",
    "AUTHOR_TURN_V2_SCHEMA",
    "FINAL_PROPOSAL_V2_SCHEMA",
]

#: The schema-registry identity every **v2** author turn is requested (and audited) under. A
#: distinct id, never a version bump of ``formula_author_turn`` — see the module docstring.
AUTHOR_TURN_SCHEMA_ID_V2 = "formula_author_turn_v2"
AUTHOR_TURN_SCHEMA_VERSION_V2 = 1

_proposal_v2 = json.loads(
    Path(__file__).with_name("proposal_v2.schema.json").read_text(encoding="utf-8"))
_PROPOSAL_V2_NODE: dict = {
    k: v for k, v in _proposal_v2.items() if k not in ("$schema", "$id", "title", "$defs")}

#: ▲ **AND EVERY ENUM DECLARES ITS TYPE** (2026-08-24) — v2 carried the identical defect v3 was
#: diagnosed with, because both inherit the same bare-enum idiom from their proposal ``$defs``
#: (``"type": {"enum": [...]}`` with no ``"type"`` keyword). Legal JSON Schema; refused by the
#: provider's structured-output subset, which requires every subschema to declare what it is.
#: Fifteen of them here. Purely additive — the members already state the type — and the canonical
#: ``proposal_v2.schema.json`` is still never touched. See ``turns_v3.py`` for the live diagnostic
#: this came out of, and for why the transform is applied per DEFINITION rather than to ``$defs``.
_PROPOSAL_V2_DEFS: dict = {
    _name: declare_enum_types(_node) for _name, _node in {
        **_proposal_v2["$defs"],
        "aggregateExpression": {
            **_proposal_v2["$defs"]["aggregateExpression"],
            "properties": {
                **_proposal_v2["$defs"]["aggregateExpression"]["properties"],
                "aggregation": {"type": "string"},
            },
        },
    }.items()
}

# ``formulaBody`` is a oneOf over the four shapes; the discriminating ``final_operation`` const/enum
# lives on each branch, so the relaxation is applied per branch rather than at the union.
for _shape in ("compositeBody", "unaryBody", "ratioBody", "diffBody"):
    _branch = _PROPOSAL_V2_DEFS.get(_shape)
    if isinstance(_branch, dict) and isinstance(_branch.get("properties"), dict):
        _PROPOSAL_V2_DEFS[_shape] = {
            **_branch,
            "properties": {**_branch["properties"], "final_operation": {"type": "string"}},
        }

#: FinalProposalV2 — the raw proposal dict, exactly the shape ``parse_proposal_v2`` consumes.
FINAL_PROPOSAL_V2_SCHEMA: dict = {"$ref": "#/$defs/finalProposal"}

#: AuthorTurnV2 — v1's discriminated envelope over the v2 proposal slot. The tool half is v1's
#: object verbatim: its ``arguments.proposal`` ``$ref`` resolves to THIS schema's ``finalProposal``,
#: so ``validate_draft_formula`` on a v2 run receives a v2-shaped draft.
AUTHOR_TURN_V2_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "turn_type": {
            "type": "string",
            "enum": [TURN_TYPE_TOOL_CALL, TURN_TYPE_FINAL_PROPOSAL],
        },
        "tool_call": TOOL_CALL_V1_SCHEMA,
        "final_proposal": FINAL_PROPOSAL_V2_SCHEMA,
    },
    "required": ["turn_type"],
    "$defs": {**_PROPOSAL_V2_DEFS, "finalProposal": _PROPOSAL_V2_NODE},
}
