"""Child-1 Task 10 — the INDEPENDENT, fail-closed critic (LLM-2).

Two load-bearing invariants under test:

* INDEPENDENCE — the critic's one audited call carries an independently-assembled, read-scoped
  metadata context (the intent + the proposal + the proposal's columns' governed facts RE-FETCHED
  from the catalog under the caller's roles). The author's reasoning/tool trace has no slot in the
  signature and never appears in the audited call inputs.
* FAIL-CLOSED — a malformed/unparseable critic response (or an egress-blocked/provider-failed
  call) is ``is_technical_failure=True`` with ``findings == []``. It is NEVER the clean-critic
  shape ``([], hash, False)`` — a broken critic can never clear a formula toward auto-RESOLVED.

Severity is a FIXED property of each closed finding code — the model's emitted severity is
tolerated on the wire and IGNORED. Unknown codes and duplicate (code, target) findings are DROPPED
with a recorded note, never raised, never blocking.
"""
import dataclasses
import hashlib
import inspect
import json
import logging

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.critic import (
    _SEVERITY,
    CRITIC_FINDING_CODES,
    CRITIC_FINDINGS_V1_SCHEMA,
    CRITIC_INSTRUCTION,
    CRITIC_POLICY_VERSION,
    CRITIC_PROMPT_ID,
    CRITIC_TASK,
    CriticFinding,
    CriticFindingCode,
    build_critic_metadata,
    critic_findings_hash,
    critique,
    proposal_column_refs,
)
from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import PROVIDER_NON_RETRYABLE, FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import read_column_facts
from featuregen.overlay.upload.graph import build_graph

RUN = "formula-authoring-run-t10"
SOURCE = "authored"
REF_AMT = "authored::public.txns.txn_amt"
REF_DT = "authored::public.txns.txn_dt"
REF_CIF = "authored::public.txns.cif_id"
# A distinctive stand-in for catalog free text (a raw data value): if the critic context ever
# egresses it, the assertions below catch the exact string (same discipline as test_author).
CANARY = "leak-canary-1010"

_ACTOR = IdentityEnvelope(subject="formula-critic-t10", actor_kind="service",
                          authenticated=False, auth_method="internal", role_claims=())

_INTENT = AuthoringIntent(
    name="txn_amt_sum_90d",
    hypothesis="Total transaction amount over a trailing 90-day window predicts churn.",
    target_entity="customer",
    target_grain_keys=(REF_CIF,),
)


def _finding(code, operand=None, detail=None):
    return CriticFinding(code=code, severity=_SEVERITY[code], operand=operand, detail=detail)


# ---- the closed §G finding set + fixed severity ------------------------------------------------


def test_finding_codes_are_the_closed_g_set_with_fixed_severity():
    assert CRITIC_POLICY_VERSION == 1
    assert CRITIC_FINDING_CODES is CriticFindingCode      # the spec name binds the closed StrEnum
    expected = {
        "MISSING_REQUIRED_OPERAND": "blocking",
        "WRONG_SLOT_DIRECTION": "blocking",
        "FILTER_INTENT_MISMATCH": "blocking",
        "WINDOW_INTENT_MISMATCH": "blocking",
        "EXTRA_UNJUSTIFIED_OPERAND": "advisory",
        "WEAK_PROXY": "advisory",
    }
    assert {code.value for code in CriticFindingCode} == set(expected)
    # severity is a FIXED property of the code — the map is total over the closed set
    assert {code.value: _SEVERITY[code] for code in CriticFindingCode} == expected


def test_finding_is_a_frozen_slotted_dataclass():
    finding = _finding(CriticFindingCode.WEAK_PROXY, operand=REF_AMT, detail="proxy only")
    assert dataclasses.is_dataclass(CriticFinding)
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.severity = "blocking"
    assert not hasattr(finding, "__dict__")               # slots=True


# ---- the deterministic findings hash -----------------------------------------------------------


def test_findings_hash_is_order_independent_and_content_sensitive():
    a = _finding(CriticFindingCode.MISSING_REQUIRED_OPERAND, operand=REF_AMT, detail="a")
    b = _finding(CriticFindingCode.WEAK_PROXY, operand=REF_DT, detail="b")
    assert critic_findings_hash([a, b]) == critic_findings_hash([b, a])   # ordering can't matter
    assert critic_findings_hash([a]) != critic_findings_hash([b])         # content does
    assert len(critic_findings_hash([a, b])) == 64
    # well-defined over NO findings (the technical-failure hash Task 12 always receives)
    assert critic_findings_hash([]) == hashlib.sha256(b"[]").hexdigest()


# ---- the independently-assembled, read-scoped critic context -----------------------------------


def _seed_catalog(db):
    rows = [
        CanonicalRow(SOURCE, "txns", "txn_amt", "numeric", additivity="additive",
                     currency="AED", unit="currency_minor"),
        CanonicalRow(SOURCE, "txns", "txn_dt", "date", as_of=True),
        CanonicalRow(SOURCE, "txns", "cif_id", "text", is_grain=True, entity="customer"),
    ]
    build_graph(db, SOURCE, rows)
    # governed fact links (the OVERLAY_FACT provenance read_column_facts derives authority from)
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id = 'ovf_evt_grain' "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.cif_id'", (SOURCE,))
    db.execute(
        "UPDATE graph_node SET availability_fact_event_id = 'ovf_evt_asof' "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.txn_dt'", (SOURCE,))
    # catalog free text (a raw data value) the critic context must NEVER egress
    db.execute(
        "UPDATE graph_node SET definition = %s "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.txn_amt'",
        (f"average balance e.g. {CANARY}", SOURCE))


def _raw_proposal():
    window = {"event_time_ref": REF_DT, "basis": "trailing", "length": 90, "unit": "day",
              "start_inclusive": "inclusive", "end_inclusive": "exclusive",
              "timezone": "Asia/Dubai", "empty_window": "null", "null_input": "ignore"}
    expr = {"aggregation": "sum", "operand": REF_AMT,
            "source_relation": {"table_ref": "authored::public.txns"},
            "filter": {"kind": "predicate", "op": "is_not_null", "left": REF_CIF},
            "window": window}
    return {"formula_schema_version": 1, "operation_grammar_version": 1,
            "canonicalization_version": 1,
            "grain": {"entity": "customer", "keys": [REF_CIF]},
            "body": {"final_operation": "identity", "expr": expr},
            "parameters": [],
            "decimal": {"precision": 38, "scale": 6, "rounding": "half_even",
                        "overflow": "error"},
            "expected_output": None}


def _proposal():
    return parse_proposal_v1(_raw_proposal())


def _nested_keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _nested_keys(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _nested_keys(v)


def test_proposal_column_refs_walks_every_body_slot_sorted_deduped():
    refs = proposal_column_refs(_proposal())
    # grain key + operand + window event-time + filter left, deduplicated, sorted
    assert refs == (REF_CIF, REF_AMT, REF_DT)


def test_critic_metadata_is_exactly_intent_proposal_and_refetched_columns(db):
    _seed_catalog(db)
    meta = build_critic_metadata(db, _INTENT, _proposal(), roles=())
    # the CLOSED three-key context — no slot exists for the author's reasoning or tool trail
    assert set(meta) == {"authoring_intent", "proposal", "operand_columns"}
    assert meta["authoring_intent"] == {
        "name": _INTENT.name, "hypothesis": _INTENT.hypothesis,
        "target_entity": _INTENT.target_entity, "target_grain_keys": [REF_CIF]}
    assert meta["proposal"] == json.loads(json.dumps(meta["proposal"]))   # plain JSON types only
    assert meta["proposal"]["body"]["expr"]["operand"] == REF_AMT
    # the columns are RE-FETCHED governed facts, matching a direct authority read
    by_ref = {c["logical_ref"]: c for c in meta["operand_columns"]}
    assert list(by_ref) == [REF_CIF, REF_AMT, REF_DT]                     # sorted, deduplicated
    amt = by_ref[REF_AMT]
    assert amt["found"] and amt["data_type"] == "numeric"
    direct = read_column_facts(db, REF_AMT, "additivity")
    assert amt["facts"]["additivity"] == {"value": direct.value, "authority": direct.authority,
                                          "provenance": direct.provenance}
    assert by_ref[REF_CIF]["facts"]["is_grain"]["authority"] == "governed"
    assert by_ref[REF_DT]["facts"]["is_as_of"]["provenance"] == "ovf_evt_asof"
    # metadata-only egress: no catalog free text, no data values, no author-trail keys
    assert CANARY not in json.dumps(meta)
    assert not ({"tool_trail", "definition", "rows", "samples", "values"}
                & set(_nested_keys(meta)))


def test_critic_metadata_is_read_scoped_hidden_reads_as_not_found(db):
    _seed_catalog(db)
    db.execute("UPDATE graph_node SET sensitivity = 'pii' "
               "WHERE catalog_source = %s AND object_ref = 'public.txns.txn_amt'", (SOURCE,))
    hidden = build_critic_metadata(db, _INTENT, _proposal(), roles=())
    amt = next(c for c in hidden["operand_columns"] if c["logical_ref"] == REF_AMT)
    assert amt == {"logical_ref": REF_AMT, "found": False}    # hidden == nonexistent, no facts
    granted = build_critic_metadata(db, _INTENT, _proposal(), roles=("pii_reader",))
    amt = next(c for c in granted["operand_columns"] if c["logical_ref"] == REF_AMT)
    assert amt["found"] and amt["facts"]["additivity"]["value"] == "additive"


# ---- critique: the one audited critic call -----------------------------------------------------


def _script(client, output, **kwargs):
    client.script(task=CRITIC_TASK, prompt_id=CRITIC_PROMPT_ID,
                  responses=[FakeResponse(output=output, **kwargs)])


def _critique(db, client, *, roles=()):
    return critique(db, _INTENT, _proposal(), client, roles=roles, actor=_ACTOR,
                    authoring_run_id=RUN)


def test_blocking_finding_surfaced_with_severity_from_the_fixed_map(db):
    _seed_catalog(db)
    client = FakeLLM()
    _script(client, {"findings": [{"code": "MISSING_REQUIRED_OPERAND", "operand": REF_AMT,
                                   "detail": "the intent's amount concept is not aggregated",
                                   "severity": "advisory"}]})   # the model LIES about severity
    findings, digest, technical = _critique(db, client)
    assert technical is False
    assert len(findings) == 1
    assert findings[0].code is CriticFindingCode.MISSING_REQUIRED_OPERAND
    assert findings[0].severity == "blocking"       # the FIXED map wins — never the model
    assert findings[0].operand == REF_AMT
    assert digest == critic_findings_hash(findings)


def test_clean_critic_is_empty_findings_not_technical(db):
    _seed_catalog(db)
    client = FakeLLM()
    _script(client, {"findings": []})
    findings, digest, technical = _critique(db, client)
    assert (findings, technical) == ([], False)     # THE clean-critic shape...
    assert digest == critic_findings_hash([])


def test_malformed_critic_response_is_technical_failure_never_clean(db):
    _seed_catalog(db)
    client = FakeLLM()
    # schema-invalid on every (bounded) repair attempt -> the audited seam fails closed
    _script(client, {"findings": "totally-not-a-list"})
    findings, digest, technical = _critique(db, client)
    assert findings == []
    assert technical is True                        # ...and the malformed one is NOT it:
    assert (findings, technical) != ([], False)     # a broken critic never reads as clean
    assert digest == critic_findings_hash([])       # the hash is still computed for Task 12


def test_provider_failure_is_technical_failure(db):
    _seed_catalog(db)
    client = FakeLLM()
    _script(client, {}, provider_status=PROVIDER_NON_RETRYABLE)
    findings, digest, technical = _critique(db, client)
    assert (findings, technical) == ([], True)
    assert digest == critic_findings_hash([])


def test_unknown_and_duplicate_findings_dropped_with_recorded_notes(db, caplog):
    _seed_catalog(db)
    client = FakeLLM()
    _script(client, {"findings": [
        {"code": "WEAK_PROXY", "operand": REF_AMT, "detail": "amount proxies balance"},
        {"code": "TOTALLY_MADE_UP", "operand": REF_AMT, "detail": "outside the closed set"},
        {"code": "WEAK_PROXY", "operand": REF_AMT, "detail": "same (code, target) again"},
        {"code": "EXTRA_UNJUSTIFIED_OPERAND", "operand": REF_DT},
    ]})
    with caplog.at_level(logging.WARNING, logger="featuregen.formula.critic"):
        findings, digest, technical = _critique(db, client)
    assert technical is False                       # dropped, never raised, never blocking
    assert [(f.code.value, f.operand) for f in findings] == [
        ("WEAK_PROXY", REF_AMT), ("EXTRA_UNJUSTIFIED_OPERAND", REF_DT)]
    notes = " | ".join(r.getMessage() for r in caplog.records)
    assert "TOTALLY_MADE_UP" in notes               # the unknown code's note
    assert "duplicate" in notes                     # the duplicate's note
    assert digest == critic_findings_hash(findings)


def test_author_reasoning_and_tool_trace_never_reach_the_critic(db):
    _seed_catalog(db)
    client = FakeLLM()
    _script(client, {"findings": []})
    _critique(db, client)
    rows = db.execute("SELECT task, redacted_input, raw_output FROM llm_call "
                      "WHERE run_id = %s", (RUN,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == CRITIC_TASK   # exactly ONE audited critic call
    redacted_input = rows[0][1]
    # the instruction is the FIXED critic protocol text — no author reasoning can ride it
    assert redacted_input["redacted_intent"] == CRITIC_INSTRUCTION
    meta = redacted_input["catalog_metadata"]
    assert set(meta) == {"authoring_intent", "proposal", "operand_columns"}   # closed key set
    assert not ({"tool_trail", "reasoning", "turns", "tool_result", "author_output"}
                & set(_nested_keys(meta)))
    # the operand metadata on the wire is the INDEPENDENT re-fetch, not an author artifact
    amt = next(c for c in meta["operand_columns"] if c["logical_ref"] == REF_AMT)
    direct = read_column_facts(db, REF_AMT, "additivity")
    assert amt["facts"]["additivity"] == {"value": direct.value, "authority": direct.authority,
                                          "provenance": direct.provenance}
    assert CANARY not in json.dumps(rows[0][1]) + json.dumps(rows[0][2])


def test_critique_signature_has_no_slot_for_an_author_trace():
    assert set(inspect.signature(critique).parameters) == {
        "conn", "intent", "proposal", "client", "roles", "actor", "authoring_run_id"}


def test_findings_hash_is_order_independent_end_to_end(db):
    _seed_catalog(db)
    first = {"code": "WEAK_PROXY", "operand": REF_AMT, "detail": "d1"}
    second = {"code": "WINDOW_INTENT_MISMATCH", "operand": REF_DT, "detail": "d2"}
    one, other = FakeLLM(), FakeLLM()
    _script(one, {"findings": [first, second]})
    _script(other, {"findings": [second, first]})   # same findings, reversed emission order
    _, digest_one, _ = _critique(db, one)
    _, digest_other, _ = _critique(db, other)
    assert digest_one == digest_other


def test_findings_schema_is_provider_compatible():
    from featuregen.intake.schema_projection import (
        assert_schemas_provider_compatible,
        project_for_anthropic,
    )
    projected = project_for_anthropic(CRITIC_FINDINGS_V1_SCHEMA)
    assert_schemas_provider_compatible([("formula_critic_findings", projected)])
