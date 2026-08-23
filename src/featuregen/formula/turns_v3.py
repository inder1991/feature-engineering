"""The Formula-**v3** author TURN contract, beside the frozen v1 and v2 ones.

``turns_v2.py`` holds ``AuthorTurnV2``, whose ``final_proposal`` slot is
``proposal_v2.schema.json``. This module is the one and only v3 fork of it: the envelope is
IDENTICAL, the proposal slot is ``proposal_v3.schema.json``, and the tool half is imported verbatim
from v1 because the governed catalog-authoring tools are version-neutral — they read the catalog,
not the grammar.

▲ **WHY THIS HAD TO EXIST, AND WHAT WAS HAPPENING WITHOUT IT.** The production formula-draft worker
declares ``formula_schema: 3`` on every run it opens. It then drove the provider under
``AUTHOR_TURN_CONTRACT_V2``, whose instruction says in as many words *"The proposal MUST declare
formula_schema_version 2."* So the platform asked for a v2 proposal, received one, recorded a
manifest saying v3, and marked the draft READY. Every "v3" run this platform has ever authored was
a v2 run wearing a v3 label — and the orchestrator accepted either type without ever comparing the
result to what it had asked for.

That is not a labelling nit. A v2 proposal carries no ``row_selections``, so the semantic row
selections v3 exists FOR were silently never requested; and an evaluator keyed on the manifest would
have certified this evidence as v3.

**The same schema-id discipline v2 records applies again.** The audited seam records the
``output_schema_id`` and ``output_schema_version`` a call was made under, and
``frozen_configuration`` hashes them. A v3 run requested under the v2 identity is indistinguishable
in the audit from a v2 run, and a frozen provider contract cannot tell them apart. So
``formula_author_turn_v3`` is a distinct registered schema, not a version bump of the second.

**THE SAME WIRE RELAXATION**, for v2's stated reason (§B/§F ``unsupported != invalid``):
``aggregateExpression.aggregation`` and the body's ``final_operation`` are opened to any string on
the WIRE, so a model asked for an unsupported operation emits it, the turn VALIDATES, and the
orchestrator's raw-body inspection classifies it ``unsupported_operation`` rather than the repair
loop exhausting on a schema rejection and reporting "the loop broke". The relaxation is applied to a
COPY: the strict ``proposal_v3.schema.json`` that ``parse_proposal_v3`` loads is never touched and
stays the only gate a proposal is accepted through.
"""
from __future__ import annotations

import json
from pathlib import Path

from featuregen.formula.turns import (
    TOOL_CALL_V1_SCHEMA,
    TURN_TYPE_FINAL_PROPOSAL,
    TURN_TYPE_TOOL_CALL,
)

__all__ = [
    "AUTHOR_TURN_SCHEMA_ID_V3",
    "AUTHOR_TURN_SCHEMA_VERSION_V3",
    "AUTHOR_TURN_V3_SCHEMA",
    "FINAL_PROPOSAL_V3_SCHEMA",
]

#: The schema-registry identity every **v3** author turn is requested (and audited) under. A
#: distinct id, never a version bump of ``formula_author_turn_v2`` — see the module docstring.
AUTHOR_TURN_SCHEMA_ID_V3 = "formula_author_turn_v3"
AUTHOR_TURN_SCHEMA_VERSION_V3 = 1

_proposal_v3 = json.loads(
    Path(__file__).with_name("proposal_v3.schema.json").read_text(encoding="utf-8"))
_PROPOSAL_V3_NODE: dict = {
    k: v for k, v in _proposal_v3.items() if k not in ("$schema", "$id", "title", "$defs")}

_PROPOSAL_V3_DEFS: dict = {
    **_proposal_v3["$defs"],
    "aggregateExpression": {
        **_proposal_v3["$defs"]["aggregateExpression"],
        "properties": {
            **_proposal_v3["$defs"]["aggregateExpression"]["properties"],
            "aggregation": {"type": "string"},
        },
    },
}

# ``formulaBody`` is a oneOf over the four shapes; the discriminating ``final_operation`` const/enum
# lives on each branch, so the relaxation is applied per branch rather than at the union.
for _shape in ("compositeBody", "unaryBody", "ratioBody", "diffBody"):
    _branch = _PROPOSAL_V3_DEFS.get(_shape)
    if isinstance(_branch, dict) and isinstance(_branch.get("properties"), dict):
        _PROPOSAL_V3_DEFS[_shape] = {
            **_branch,
            "properties": {**_branch["properties"], "final_operation": {"type": "string"}},
        }

#: FinalProposalV3 — the raw proposal dict, exactly the shape ``parse_proposal_v3`` consumes.
FINAL_PROPOSAL_V3_SCHEMA: dict = {"$ref": "#/$defs/finalProposal"}

#: AuthorTurnV3 — the same discriminated envelope over the v3 proposal slot.
AUTHOR_TURN_V3_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "turn_type": {
            "type": "string",
            "enum": [TURN_TYPE_TOOL_CALL, TURN_TYPE_FINAL_PROPOSAL],
        },
        "tool_call": TOOL_CALL_V1_SCHEMA,
        "final_proposal": FINAL_PROPOSAL_V3_SCHEMA,
    },
    "required": ["turn_type"],
    "$defs": {**_PROPOSAL_V3_DEFS, "finalProposal": _PROPOSAL_V3_NODE},
}
