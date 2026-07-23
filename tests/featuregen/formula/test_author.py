"""Child-1 Task 9 — the sequential-turn (ReAct) author + its 7 governed catalog-authoring tools.

`author_formula` drives ONE governed `audited_formula_call` per turn: the model either calls a
read/validate tool (whose CANONICAL result is threaded into the NEXT turn's `catalog_metadata` —
data, never instructions) or emits a `FinalProposalV1` (the raw proposal dict, returned unparsed).
`max_turns` exhaustion / budget-exceed / a malformed turn all return `(None, turns)` — a TECHNICAL
outcome the caller maps, never a fabricated proposal.

FakeLLM scripting note: FakeLLM's response-sequence cursor is keyed by the request's input HASH, and
every author turn has a DISTINCT hash (the tool trail grows), so multi-turn runs are scripted by
registering one exact-`input_hash` entry per turn. Each turn's hash is computed by replicating the
audited seam's input assembly (`build_llm_inputs` over `build_turn_metadata`) — which doubles as the
strongest possible assertion that the author threads EXACTLY the canonical tool result into the next
turn's metadata: any deviation changes the hash and the FakeLLM raises KeyError mid-run.
"""
import dataclasses
import json

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.author import (
    AUTHOR_INSTRUCTION,
    AUTHOR_PROMPT_ID,
    AUTHOR_TASK,
    author_formula,
    build_turn_metadata,
    tool_trail_entry,
)
from featuregen.formula.turns import (
    AUTHOR_TURN_V1_SCHEMA,
    AuthoringIntent,
    AuthorTurnRecord,
    TOOL_NAMES,
    TurnKind,
)
from featuregen.formula.tools import TOOLS, run_tool
from featuregen.intake.llm import FakeLLM, FakeResponse, compute_input_hash
from featuregen.intake.redaction import RedactionResult, build_llm_inputs
from featuregen.intake.schema_projection import (
    assert_schemas_provider_compatible,
    project_for_anthropic,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

RUN = "formula-authoring-run-t9"
SOURCE = "authored"
# A distinctive stand-in for a raw data value living in catalog free text: if any tool or turn ever
# egresses it, the assertions below catch the exact string. Deliberately NOT PII-shaped so the leak
# is caught by THESE assertions, not masked by the egress guard blocking the call first.
CANARY = "leak-canary-9999"

_ACTOR = IdentityEnvelope(subject="formula-author-t9", actor_kind="service",
                          authenticated=False, auth_method="internal", role_claims=())

_INTENT = AuthoringIntent(
    name="txn_amt_sum_90d",
    hypothesis="Total transaction amount over a trailing 90-day window predicts churn.",
    target_entity="customer",
    target_grain_keys=("authored::public.txns.cif_id",),
)

# Keys that carry DATA VALUES, never metadata — must not appear anywhere in a tool result.
_FORBIDDEN_RESULT_KEYS = frozenset(
    {"rows", "samples", "values", "value_set", "min", "max", "extrema", "profile",
     "data_values", "column_values"})


def _seed_catalog(db):
    rows = [
        CanonicalRow(SOURCE, "txns", "txn_amt", "numeric", additivity="additive",
                     currency="AED", unit="currency_minor"),
        CanonicalRow(SOURCE, "txns", "txn_dt", "date", as_of=True),
        CanonicalRow(SOURCE, "txns", "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(SOURCE, "custs", "cif_id", "text", is_grain=True, entity="customer"),
    ]
    build_graph(db, SOURCE, rows)
    # Governed fact links (the OVERLAY_FACT provenance read_column_facts derives authority from).
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id = 'ovf_evt_grain' "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.cif_id'", (SOURCE,))
    db.execute(
        "UPDATE graph_node SET availability_fact_event_id = 'ovf_evt_asof' "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.txn_dt'", (SOURCE,))
    # Free text the catalog carries but the author tools must NEVER egress.
    db.execute(
        "UPDATE graph_node SET definition = %s "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.txn_amt'",
        (f"average balance e.g. {CANARY}", SOURCE))
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, now(), 'r', 0) ON CONFLICT (catalog_source) "
        "DO UPDATE SET last_completed_at = now()", (SOURCE,))


def _raw_proposal():
    """A shape-valid FinalProposalV1 over the seeded catalog (never parsed by the author)."""
    window = {"event_time_ref": "authored::public.txns.txn_dt", "basis": "trailing",
              "length": 90, "unit": "day", "start_inclusive": "inclusive",
              "end_inclusive": "exclusive", "timezone": "Asia/Dubai",
              "empty_window": "null", "null_input": "ignore"}
    expr = {"aggregation": "sum", "operand": "authored::public.txns.txn_amt",
            "source_relation": {"table_ref": "authored::public.txns"},
            "filter": None, "window": window}
    return {"formula_schema_version": 1, "operation_grammar_version": 1,
            "canonicalization_version": 1,
            "grain": {"entity": "customer", "keys": ["authored::public.txns.cif_id"]},
            "body": {"final_operation": "identity", "expr": expr},
            "parameters": [],
            "decimal": {"precision": 38, "scale": 6, "rounding": "half_even",
                        "overflow": "error"},
            "expected_output": None}


def _tool_turn(name, arguments):
    return {"turn_type": "tool_call", "tool_call": {"tool_name": name, "arguments": arguments}}


def _final_turn(proposal):
    return {"turn_type": "final_proposal", "final_proposal": proposal}


def _hash_for(trail):
    """Replicate the audited seam's input assembly for one turn (see module docstring)."""
    redaction = RedactionResult(text=AUTHOR_INSTRUCTION, redaction_version="metadata-only",
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=build_turn_metadata(_INTENT, trail),
                              raw_input_classification="clean")
    return compute_input_hash(inputs)


def _script(client, trail, response):
    client.script(task=AUTHOR_TASK, prompt_id=AUTHOR_PROMPT_ID, responses=[response],
                  input_hash=_hash_for(trail))


def _script_three_turn_run(db, client):
    """search -> get_metadata -> final proposal. Returns (expected tool results, raw proposal)."""
    search_args = {"query": "txn_amt", "limit": 5}
    meta_args = {"logical_ref": "authored::public.txns.txn_amt"}
    r1 = run_tool(db, "search_columns", search_args, roles=())
    r2 = run_tool(db, "get_column_metadata", meta_args, roles=())
    raw = _raw_proposal()
    trail1 = [tool_trail_entry(1, "search_columns", r1)]
    trail2 = trail1 + [tool_trail_entry(2, "get_column_metadata", r2)]
    _script(client, [], FakeResponse(output=_tool_turn("search_columns", search_args),
                                     cost_metadata={"input_tokens": 100, "output_tokens": 20}))
    _script(client, trail1, FakeResponse(output=_tool_turn("get_column_metadata", meta_args),
                                         cost_metadata={"input_tokens": 120, "output_tokens": 25}))
    _script(client, trail2, FakeResponse(output=_final_turn(raw),
                                         cost_metadata={"input_tokens": 150, "output_tokens": 90}))
    return r1, r2, raw


def _author(db, client, *, max_turns=5, roles=()):
    return author_formula(db, _INTENT, client, roles=roles, max_turns=max_turns,
                          actor=_ACTOR, authoring_run_id=RUN)


def _nested_keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _nested_keys(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _nested_keys(v)


# ---- the 3-turn flow ---------------------------------------------------------------------------


def test_three_turn_flow_returns_final_proposal_and_turn_trail(db):
    _seed_catalog(db)
    client = FakeLLM()
    r1, r2, raw = _script_three_turn_run(db, client)
    proposal, turns = _author(db, client)
    assert proposal == raw                        # the RAW dict, passed through unparsed
    assert len(turns) == 3
    assert [t.kind for t in turns] == [TurnKind.TOOL_CALL, TurnKind.TOOL_CALL,
                                       TurnKind.FINAL_PROPOSAL]
    assert turns[0].tool_name == "search_columns"
    assert turns[0].tool_result == r1             # the canonical result, recorded verbatim
    assert turns[1].tool_name == "get_column_metadata"
    assert turns[1].tool_result == r2
    assert turns[2].output == _final_turn(raw)


def test_each_turn_is_one_audited_call_with_llm_call_ref(db):
    _seed_catalog(db)
    client = FakeLLM()
    _script_three_turn_run(db, client)
    _, turns = _author(db, client)
    refs = [t.llm_call_ref for t in turns]
    assert all(refs)                              # EVERY turn carries an immutable audit ref
    assert len(set(refs)) == 3                    # three DISTINCT audited calls
    assert all(t.provider_calls == 1 for t in turns)
    n = db.execute("SELECT count(*) FROM llm_call WHERE run_id = %s", (RUN,)).fetchone()[0]
    assert n == 3                                 # exactly one llm_call row per turn


def test_tool_results_are_data_in_catalog_metadata_never_instructions(db):
    _seed_catalog(db)
    client = FakeLLM()
    r1, _r2, _raw = _script_three_turn_run(db, client)
    _, turns = _author(db, client)
    for i, turn in enumerate(turns):
        row = db.execute("SELECT redacted_input FROM llm_call WHERE llm_call_ref = %s",
                         (turn.llm_call_ref,)).fetchone()
        redacted_input = row[0]
        # the instruction is the FIXED protocol text on every turn — tool output never rides it
        assert redacted_input["redacted_intent"] == AUTHOR_INSTRUCTION
        trail = redacted_input["catalog_metadata"]["tool_trail"]
        assert len(trail) == i                    # turn N sees exactly the N-1 prior tool results
    row = db.execute("SELECT redacted_input FROM llm_call WHERE llm_call_ref = %s",
                     (turns[1].llm_call_ref,)).fetchone()
    assert row[0]["catalog_metadata"]["tool_trail"][0] == tool_trail_entry(1, "search_columns", r1)


# ---- technical outcomes (never a fabricated proposal) ------------------------------------------


def test_never_final_within_max_turns_returns_none(db):
    _seed_catalog(db)
    client = FakeLLM()
    trail = []
    for turn_no in range(1, 4):
        _script(client, list(trail), FakeResponse(
            output=_tool_turn("list_supported_operations", {})))
        trail.append(tool_trail_entry(turn_no, "list_supported_operations",
                                      run_tool(db, "list_supported_operations", {}, roles=())))
    proposal, turns = _author(db, client, max_turns=3)
    assert proposal is None                       # technical — no proposal was authored
    assert len(turns) == 3
    assert all(t.kind == TurnKind.TOOL_CALL for t in turns)
    assert all(t.llm_call_ref for t in turns)     # the exhausted run is still fully audited


def test_budget_exceeded_returns_none(db):
    from featuregen.formula import author as author_mod
    _seed_catalog(db)
    client = FakeLLM()
    _script(client, [], FakeResponse(
        output=_tool_turn("list_supported_operations", {}),
        cost_metadata={"input_tokens": author_mod.AUTHOR_TOKEN_BUDGET + 1, "output_tokens": 1}))
    proposal, turns = _author(db, client, max_turns=4)
    assert proposal is None                       # budget-exceed surfaces as technical
    assert len(turns) == 1                        # no further provider call was issued
    assert turns[0].llm_call_ref


def test_mismatched_turn_discriminator_is_technical(db):
    _seed_catalog(db)
    client = FakeLLM()
    # shape-valid (turn_type alone satisfies the schema) but the discriminator has no slot
    _script(client, [], FakeResponse(output={"turn_type": "final_proposal"}))
    proposal, turns = _author(db, client)
    assert proposal is None                       # fail closed — never fabricate a proposal
    assert len(turns) == 1
    assert turns[0].kind == TurnKind.FAILED
    assert turns[0].llm_call_ref


# ---- metadata-only, read-scoped tool egress ----------------------------------------------------


def test_tool_egress_is_metadata_only(db):
    _seed_catalog(db)
    r1 = run_tool(db, "search_columns", {"query": "txn_amt"}, roles=())
    assert r1["columns"]                                       # the seeded columns are found
    hit = next(c for c in r1["columns"] if c["column"] == "txn_amt")
    # column/grain metadata IS present...
    assert hit["logical_ref"] == "authored::public.txns.txn_amt"
    assert hit["data_type"] == "numeric"
    assert hit["additivity"] == "additive"
    r2 = run_tool(db, "get_column_metadata",
                  {"logical_ref": "authored::public.txns.txn_amt"}, roles=())
    assert r2["found"] and r2["facts"]["additivity"]["value"] == "additive"
    assert set(r2["facts"]) == {"additivity", "logical_representation", "is_grain", "is_as_of",
                                "unit", "currency", "entity", "declared_type"}
    for result in (r1, r2):
        # ...but NO raw data values: no value-bearing keys, no catalog free text, anywhere
        assert not (_FORBIDDEN_RESULT_KEYS & set(_nested_keys(result)))
        assert "definition" not in set(_nested_keys(result))
        assert CANARY not in json.dumps(result)


def test_no_turn_egresses_catalog_free_text(db):
    _seed_catalog(db)
    client = FakeLLM()
    _script_three_turn_run(db, client)
    _, turns = _author(db, client)
    for turn in turns:
        row = db.execute("SELECT redacted_input, raw_output FROM llm_call "
                         "WHERE llm_call_ref = %s", (turn.llm_call_ref,)).fetchone()
        assert CANARY not in json.dumps(row[0]) + json.dumps(row[1])


def test_tools_honor_read_scope_roles(db):
    _seed_catalog(db)
    db.execute("UPDATE graph_node SET sensitivity = 'pii' "
               "WHERE catalog_source = %s AND object_ref = 'public.txns.cif_id'", (SOURCE,))
    tagged = "authored::public.txns.cif_id"
    visible = run_tool(db, "search_columns", {"query": "cif_id"}, roles=())
    assert tagged not in {c["logical_ref"] for c in visible["columns"]}
    assert "authored::public.custs.cif_id" in {c["logical_ref"] for c in visible["columns"]}
    scoped = run_tool(db, "search_columns", {"query": "cif_id"}, roles=("pii_reader",))
    assert tagged in {c["logical_ref"] for c in scoped["columns"]}
    hidden = run_tool(db, "get_column_metadata",
                      {"logical_ref": "authored::public.txns.cif_id"}, roles=())
    assert hidden == {"found": False}             # hidden is indistinguishable from nonexistent
    granted = run_tool(db, "get_column_metadata",
                       {"logical_ref": "authored::public.txns.cif_id"}, roles=("pii_reader",))
    assert granted["found"]


# ---- the individual tools ----------------------------------------------------------------------


def test_get_governed_grain_and_time_anchor(db):
    _seed_catalog(db)
    grain = run_tool(db, "get_governed_grain",
                     {"catalog_source": SOURCE, "table": "txns"}, roles=())
    assert grain["table_ref"] == "authored::public.txns"
    assert [g["column"] for g in grain["grain_columns"]] == ["cif_id"]
    assert grain["grain_columns"][0]["authority"] == "governed"
    assert grain["grain_columns"][0]["provenance"] == "ovf_evt_grain"
    anchor = run_tool(db, "get_time_anchor",
                      {"catalog_source": SOURCE, "table": "txns"}, roles=())
    assert [a["column"] for a in anchor["time_anchor_columns"]] == ["txn_dt"]
    a = anchor["time_anchor_columns"][0]
    assert a["authority"] == "governed"
    # the anchor's logical_ref is directly usable as WindowPolicy.event_time_ref
    assert a["logical_ref"] == "authored::public.txns.txn_dt"


def test_get_verified_lineage_shows_only_verified_joins(db):
    _seed_catalog(db)
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_status) VALUES "
        "(%s, 'joins', 'public.txns.cif_id', 'public.custs.cif_id', 'N:1', 'operational', "
        "'VERIFIED'), "
        "(%s, 'joins', 'public.txns.txn_amt', 'public.custs.cif_id', 'N:1', 'display_only', "
        "NULL)", (SOURCE, SOURCE))
    out = run_tool(db, "get_verified_lineage",
                   {"catalog_source": SOURCE, "ref": "public.txns"}, roles=())
    assert out["found"]
    joins = [e for e in out["edges"] if e["kind"] == "join"]
    assert len(joins) == 1                        # the unverified join is NOT lineage
    assert joins[0]["approved_join_status"] == "VERIFIED"
    assert {n["id"] for n in out["nodes"]} >= {f"{SOURCE}:public.txns", f"{SOURCE}:public.custs"}
    assert CANARY not in json.dumps(out)          # node metadata carries no catalog free text


def test_list_supported_operations_enumerates_the_b_vocabulary(db):
    out = run_tool(db, "list_supported_operations", {}, roles=())
    by_name = {a["name"]: a for a in out["aggregate_functions"]}
    assert set(by_name) == {"sum", "count_rows", "count_non_null", "count_distinct"}
    assert by_name["sum"]["path_aggregation"] == "sum"
    assert by_name["count_rows"]["path_aggregation"] == "count"
    assert by_name["count_non_null"]["path_aggregation"] == "count"
    assert by_name["count_distinct"]["path_aggregation"] == "count_distinct"
    assert all(a["supported"] for a in out["aggregate_functions"])
    assert out["final_operations"] == ["identity", "ratio", "difference"]


def test_validate_draft_formula_returns_verdicts_not_dispositions(db):
    ok = run_tool(db, "validate_draft_formula", {"proposal": _raw_proposal()}, roles=())
    assert ok["verdict"] == "ok" and ok["detail"] is None
    broken = _raw_proposal()
    del broken["decimal"]
    bad = run_tool(db, "validate_draft_formula", {"proposal": broken}, roles=())
    assert bad["verdict"] == "invalid" and bad["detail"]
    cross = _raw_proposal()
    other_expr = {"aggregation": "count_rows", "operand": None,
                  "source_relation": {"table_ref": "elsewhere::public.events"},
                  "filter": None,
                  "window": dict(cross["body"]["expr"]["window"],
                                 event_time_ref="elsewhere::public.events.event_dt")}
    cross["body"] = {"final_operation": "ratio", "numerator": cross["body"]["expr"],
                     "denominator": other_expr, "zero_denominator": "null"}
    multi = run_tool(db, "validate_draft_formula", {"proposal": cross}, roles=())
    assert multi["verdict"] == "unsupported_capability"


# ---- schemas + shapes --------------------------------------------------------------------------


def test_registry_has_exactly_the_seven_tools():
    assert set(TOOLS) == set(TOOL_NAMES)
    assert len(TOOL_NAMES) == 7
    for name, spec in TOOLS.items():
        assert spec.name == name
        assert spec.input_schema["type"] == "object"
        assert spec.output_schema["type"] == "object"


def test_author_turn_schema_is_provider_compatible():
    projected = project_for_anthropic(AUTHOR_TURN_V1_SCHEMA)
    assert_schemas_provider_compatible([("formula_author_turn", projected)])


def test_turn_records_are_frozen_slotted():
    assert dataclasses.is_dataclass(AuthorTurnRecord)
    rec = AuthorTurnRecord(index=0, kind=TurnKind.FAILED, llm_call_ref=None, tool_name=None,
                           tool_result=None, output=None, provider_calls=0, usage={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.index = 1
    assert not hasattr(rec, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        _INTENT.name = "x"
    assert not hasattr(_INTENT, "__dict__")
