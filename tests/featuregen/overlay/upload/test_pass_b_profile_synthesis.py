"""Profile Task 4 (e) — Pass B synthesises dataset PROFILE meaning, evidence-bound.

The contract, in the order the plan fixes it:

* a REAL `overlay_table_synth_batch` v3 body (v2 is a byte-alias of v1 with
  `additionalProperties: false` and would REJECT every new field), a prompt version bump, and
  `_require_schema` proving the pair resolves;
* DETERMINISTIC contradictions FIRST, from honest signals, ABSTAINING where the signal is absent;
* every suggestion NAMES evidence refs from the bounded table context — one that cites nothing, or
  cites a ref outside that context, is `dropped_invalid`;
* a SOURCE-authored table definition stays current: the LLM's alternative is `superseded` (kept for
  review in the structured result), never competing `definition` evidence;
* an independent PROPOSAL-BLIND critic vetoes the two operational classifications only; descriptions
  stay one-model;
* per-field dispositions are TOTAL over `DISPOSITION_FIELDS` and drawn from a closed status set;
* the accepted synthesis lands in the existing `structured_result` store (type
  `table_profile_synthesis`) so the Release-A evaluation replays without a live LLM.

No live LLM call anywhere: FakeLLM only.
"""
from __future__ import annotations

import json

import pytest

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import table_synth as ts  # noqa: N813
from featuregen.overlay.upload.enrich_batch import BatchItem
from featuregen.overlay.upload.enrich_llm import _require_schema, register_enrichment_schemas
from featuregen.overlay.upload.structured_results import find_structured_result

_TABLE = "comp_fin_tran"


def _roster(*entries: dict) -> list[dict]:
    return [{"column": e["column"], "operational_type": e.get("operational_type", "unknown"),
             "declared_type": e.get("declared_type", ""),
             **({"concept": e["concept"]} if e.get("concept") else {})} for e in entries]


def _synthesis(**over) -> dict:
    base = {
        "grain_columns": [], "as_of_column": None, "as_of_basis": None,
        "primary_entity": None, "table_role": None, "event_or_snapshot": None,
        "table_description": None, "business_context": None,
        "authority_role": None, "temporal_storage_model": None, "evidence_refs": [],
    }
    base.update(over)
    return base


def _cite(field: str, *refs: str) -> dict:
    return {"field": field, "refs": list(refs)}


def _accept(cols: set[str], *, inventory=None, dispositions=None, source_definition=None,
            catalog_context_available=False, critic=None):
    return ts.make_ref_accept(
        {_TABLE: cols}, dispositions=dispositions,
        inventory_by_table={_TABLE: inventory or []},
        source_definition_by_table=({_TABLE: source_definition} if source_definition else {}),
        source_context_by_table=({_TABLE: True} if source_definition else {}),
        catalog_context_available=catalog_context_available, critic=critic)


def _run(synthesis: dict, cols: set[str], **kw):
    disp: list[dict] = []
    accept = _accept(cols, dispositions=disp, **kw)
    raw, reason = accept(json.dumps(synthesis), _TABLE)
    out = json.loads(raw) if raw else None
    return out, {(r["field"]): r for r in disp}, reason


# ── the contract ─────────────────────────────────────────────────────────────────────────────────


def test_pass_b_v3_is_a_real_registered_body_not_an_alias(db) -> None:
    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)
    v2 = _require_schema(db, reg, "overlay_table_synth_batch", 2)
    v3 = _require_schema(db, reg, "overlay_table_synth_batch", 3)
    assert v2 is not v3 and v2 != v3
    props = v3["properties"]["results"]["items"]["properties"]["synthesis"]["properties"]
    for field in ("table_description", "business_context", "authority_role",
                  "temporal_storage_model", "evidence_refs"):
        assert field in props, field
    # v2 would have REJECTED them — which is exactly why a real body was needed.
    v2_props = v2["properties"]["results"]["items"]["properties"]["synthesis"]
    assert v2_props["additionalProperties"] is False
    assert "authority_role" not in v2_props["properties"]
    # And the companion ids resolve at v3 so one run never stamps two contract generations.
    assert _require_schema(db, reg, "overlay_table_synth", 3) is not None
    assert _require_schema(db, reg, "overlay_table_synth_summary_batch", 3) is not None


def test_the_synthesis_request_stamps_the_v3_contract() -> None:
    assert (ts._SYNTH_PROMPT_ID, ts._SYNTH_PROMPT_VERSION, ts._SYNTH_SCHEMA_VERSION) == (
        "overlay_table_synth_v4", 4, 3)


def test_the_disposition_set_is_total_and_closed() -> None:
    assert set(ts.DISPOSITION_FIELDS) == (
        set(ts._STRUCTURAL_DISPOSITION_FIELDS) | set(ts.PROFILE_DISPOSITION_FIELDS))
    disp: list[dict] = []
    ts.add_not_evaluated(disp, _TABLE)
    assert {r["field"] for r in disp} == set(ts.DISPOSITION_FIELDS)
    assert {r["status"] for r in disp} == {"not_evaluated"}
    assert {"accepted", "abstained", "dropped_invalid", "refuted", "superseded",
            "not_attempted", "not_evaluated"} == ts.DISPOSITION_STATUSES


# ── deterministic contradictions, and their abstentions ──────────────────────────────────────────


def test_scd2_without_candidate_boundaries_is_refuted() -> None:
    inventory = _roster({"column": "cust_id"}, {"column": "bal_amt"})
    out, disp, _ = _run(_synthesis(temporal_storage_model="scd2",
                                   evidence_refs=[_cite("temporal_storage_model", "cust_id")]),
                        {"cust_id", "bal_amt"}, inventory=inventory)
    assert disp["temporal_storage_model"]["status"] == "refuted"
    assert disp["temporal_storage_model"]["reason"] == "scd2_without_candidate_boundaries"
    assert "temporal_storage_model" not in out   # EVICTED, never carried forward


def test_scd2_with_boundary_columns_is_not_refuted() -> None:
    inventory = _roster({"column": "valid_from_dt", "declared_type": "date"},
                        {"column": "valid_to_dt", "declared_type": "date"})
    contradictions = ts.profile_contradictions(
        {"temporal_storage_model": "scd2"}, inventory)
    assert contradictions == {}


def test_scd2_abstains_with_no_column_inventory_at_all() -> None:
    assert ts.profile_contradictions({"temporal_storage_model": "scd2"}, []) == {}


def test_event_fact_without_a_time_column_is_refuted() -> None:
    inventory = _roster({"column": "cust_id"}, {"column": "bal_amt"})
    out, disp, _ = _run(_synthesis(table_role="event_fact", event_or_snapshot="event"),
                        {"cust_id", "bal_amt"}, inventory=inventory)
    assert disp["table_role"]["status"] == "refuted"
    assert disp["table_role"]["reason"] == "event_fact_without_time_column"
    assert out["table_role"] is None      # the value is EVICTED, not merely flagged
    assert out["event_or_snapshot"] == "event"   # its sibling verdict is untouched


@pytest.mark.parametrize("inventory", [
    _roster({"column": "event_ts", "declared_type": "timestamp"}),
    _roster({"column": "pstd_date"}),
])
def test_event_fact_with_a_time_column_is_not_refuted(inventory) -> None:
    assert ts.profile_contradictions(
        {"table_role": "event_fact", "event_or_snapshot": "event"}, inventory) == {}


def test_event_fact_is_satisfied_by_its_own_as_of_proposal() -> None:
    assert ts.profile_contradictions(
        {"table_role": "event_fact", "as_of_column": "some_col"},
        _roster({"column": "cust_id"})) == {}


def test_a_crosswalk_with_fewer_than_two_identifier_sides_is_refuted() -> None:
    inventory = _roster({"column": "cust_id", "concept": "customer_id"},
                        {"column": "bal_amt", "concept": "monetary_stock"})
    assert ts.profile_contradictions({"table_role": "bridge"}, inventory) == {
        "table_role": "crosswalk_with_fewer_than_two_identifier_sides"}


def test_a_crosswalk_with_two_identifier_sides_is_not_refuted() -> None:
    inventory = _roster({"column": "cust_id", "concept": "customer_id"},
                        {"column": "acct_id", "concept": "account_id"})
    assert ts.profile_contradictions({"table_role": "bridge"}, inventory) == {}


def test_a_crosswalk_abstains_on_a_concept_free_roster() -> None:
    """The review's explicit caveat: a glossary catalog's roster can carry no concept at all, and
    refuting every bridge claim there is the misfire the rule exists to avoid."""
    inventory = _roster({"column": "cust_id"}, {"column": "acct_id"})
    assert ts.profile_contradictions({"table_role": "bridge"}, inventory) == {}


def test_an_authority_claim_abstains_when_the_catalog_has_no_source_context() -> None:
    inventory = _roster({"column": "cust_id"})
    assert ts.profile_contradictions(
        {"authority_role": "system_of_record"}, inventory,
        source_context=False, catalog_context_available=False) == {}


def test_an_authority_claim_is_refuted_when_the_catalog_has_context_and_this_table_has_none() -> None:
    inventory = _roster({"column": "cust_id"})
    assert ts.profile_contradictions(
        {"authority_role": "system_of_record"}, inventory,
        source_context=False, catalog_context_available=True) == {
            "authority_role": "authority_claim_without_source_context"}


def test_a_non_authoritative_claim_needs_no_source_context() -> None:
    assert ts.profile_contradictions(
        {"authority_role": "non_authoritative_replica"}, _roster({"column": "c"}),
        source_context=False, catalog_context_available=True) == {}


def test_every_contradiction_code_is_in_the_closed_set() -> None:
    codes = set()
    codes |= set(ts.profile_contradictions({"temporal_storage_model": "scd2"},
                                           _roster({"column": "c"})).values())
    codes |= set(ts.profile_contradictions({"table_role": "event_fact"},
                                           _roster({"column": "c"})).values())
    codes |= set(ts.profile_contradictions(
        {"table_role": "bridge"}, _roster({"column": "c", "concept": "customer_id"})).values())
    codes |= set(ts.profile_contradictions(
        {"authority_role": "system_of_record"}, _roster({"column": "c"}),
        catalog_context_available=True).values())
    assert codes and codes <= ts.PROFILE_CONTRADICTION_CODES


# ── evidence refs ────────────────────────────────────────────────────────────────────────────────


def test_a_suggestion_that_cites_nothing_is_dropped() -> None:
    _out, disp, _ = _run(_synthesis(business_context="It records payments."), {"cust_id"},
                         inventory=_roster({"column": "cust_id"}))
    assert disp["business_context"]["status"] == "dropped_invalid"
    assert disp["business_context"]["reason"] == "no_evidence_ref"


def test_a_suggestion_citing_a_ref_outside_the_bounded_context_is_dropped() -> None:
    _out, disp, _ = _run(
        _synthesis(business_context="It records payments.",
                   evidence_refs=[_cite("business_context", "a_column_of_another_table")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}))
    assert disp["business_context"]["status"] == "dropped_invalid"


def test_an_accepted_suggestion_carries_its_citations() -> None:
    out, disp, _ = _run(
        _synthesis(business_context="It records payments.",
                   evidence_refs=[_cite("business_context", "cust_id", "table_definition")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}))
    assert disp["business_context"]["status"] == "accepted"
    assert out["business_context"] == "It records payments."
    assert out["evidence_refs"]["business_context"] == ["cust_id", "table_definition"]


# ── a source-authored definition stays current ───────────────────────────────────────────────────


def test_a_curated_table_definition_supersedes_the_llm_alternative() -> None:
    out, disp, _ = _run(
        _synthesis(table_description="A different description.",
                   evidence_refs=[_cite("table_description", "table_definition")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}),
        source_definition="One row per posted transaction.")
    assert disp["table_description"]["status"] == "superseded"
    assert disp["table_description"]["reason"] == "source_authored_definition_current"
    assert "table_description" not in out       # never competing evidence


def test_an_absent_table_definition_is_filled_by_the_llm() -> None:
    out, disp, _ = _run(
        _synthesis(table_description="One row per posted transaction.",
                   evidence_refs=[_cite("table_description", "cust_id")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}))
    assert disp["table_description"]["status"] == "accepted"
    assert out["table_description"] == "One row per posted transaction."


def test_the_description_persists_under_the_table_definition_field() -> None:
    assert ts._ADVISORY_FIELD_NAMES["table_description"] == "definition"
    assert "table_description" in ts._ADVISORY_TABLE_FIELDS


# ── the proposal-blind critic ────────────────────────────────────────────────────────────────────


def test_an_operational_classification_is_not_attempted_without_a_critic() -> None:
    """An OPERATIONAL classification never lands on one model's unreviewed say-so."""
    out, disp, _ = _run(
        _synthesis(authority_role="system_of_record",
                   evidence_refs=[_cite("authority_role", "cust_id")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}), critic=None)
    assert disp["authority_role"]["status"] == "not_attempted"
    assert disp["authority_role"]["reason"] == "critic_unavailable"
    assert "authority_role" not in out


def test_an_upheld_classification_is_accepted() -> None:
    out, disp, _ = _run(
        _synthesis(temporal_storage_model="snapshot",
                   evidence_refs=[_cite("temporal_storage_model", "cust_id")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}),
        critic=lambda *_a: True)
    assert disp["temporal_storage_model"]["status"] == "accepted"
    assert out["temporal_storage_model"] == "snapshot"


def test_a_refuted_classification_is_evicted_with_the_critic_reason() -> None:
    out, disp, _ = _run(
        _synthesis(authority_role="system_of_record",
                   evidence_refs=[_cite("authority_role", "cust_id")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}),
        critic=lambda *_a: "independent_disagrees")
    assert disp["authority_role"]["status"] == "refuted"
    assert disp["authority_role"]["reason"] == "independent_disagrees"
    assert "authority_role" not in out


def test_descriptions_stay_on_the_one_model_path() -> None:
    """The critic is never consulted for prose — a second paid pass over a table description buys
    nothing and would double Pass-B spend for display text."""
    seen: list = []

    def _critic(ref, field, value, refs):
        seen.append(field)
        return True

    _out, disp, _ = _run(
        _synthesis(business_context="It records payments.",
                   table_description="One row per transaction.",
                   evidence_refs=[_cite("business_context", "cust_id"),
                                  _cite("table_description", "cust_id")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}), critic=_critic)
    assert seen == []                                    # never asked about prose
    assert disp["business_context"]["status"] == "accepted"
    assert disp["table_description"]["status"] == "accepted"


def test_an_off_vocabulary_classification_drops_that_field_only() -> None:
    out, disp, _ = _run(
        _synthesis(authority_role="the_best_one", business_context="It records payments.",
                   evidence_refs=[_cite("authority_role", "cust_id"),
                                  _cite("business_context", "cust_id")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}), critic=lambda *_a: True)
    assert disp["authority_role"]["status"] == "dropped_invalid"
    assert disp["authority_role"]["reason"] == "authority_role_off_vocab"
    assert disp["business_context"]["status"] == "accepted"     # the sibling survives


def test_a_deterministic_contradiction_short_circuits_the_critic() -> None:
    """Order of authority: a contradiction refutes on its own and the critic is never even asked."""
    seen: list = []
    _out, disp, _ = _run(
        _synthesis(temporal_storage_model="scd2",
                   evidence_refs=[_cite("temporal_storage_model", "cust_id")]),
        {"cust_id"}, inventory=_roster({"column": "cust_id"}),
        critic=lambda *a: seen.append(a) or True)
    assert seen == []
    assert disp["temporal_storage_model"]["status"] == "refuted"


# ── the independent critic itself ────────────────────────────────────────────────────────────────


def _critic_context() -> dict:
    return {"table": _TABLE, "column_roster": _roster({"column": "cust_id"}),
            "table_definition": "One row per posted transaction."}


def test_the_critic_payload_is_proposal_blind(db) -> None:
    from featuregen.overlay.upload.attest import dataset_profile_critic as dpc
    seen: list = []

    class _Capture(FakeLLM):
        def call(self, request):
            seen.append(request)
            return super().call(request)

    client = _Capture(script={dpc.PROFILE_CRITIC_TASK: FakeResponse(
        output={"classification": "system_of_record"})})
    outcome = dpc.critique_profile_claim(
        db, client, dataset_ref="ftr::s.comp_fin_tran", field="authority_role",
        proposed_value="derived", context=_critic_context(), cited_refs=["cust_id"],
        context_revision="rev-1")
    assert seen, "the critic never dispatched"
    # The PROPOSAL never egresses — that is the whole design. Asserted on the CATALOG METADATA
    # (the instruction legitimately enumerates the closed vocabulary the proposal is drawn from,
    # so a whole-request substring check would be vacuous).
    catalog = seen[0].inputs["catalog_metadata"]
    assert "derived" not in json.dumps(catalog)
    assert "proposed_value" not in catalog and "authority_role" not in catalog
    assert outcome.independent_value == "system_of_record"
    assert outcome.disposition is dpc.ProfileCriticDisposition.REFUTED
    assert outcome.reason_codes == ("independent_disagrees",)


def test_the_critic_upholds_on_agreement_and_abstains_on_unknown(db) -> None:
    from featuregen.overlay.upload.attest import dataset_profile_critic as dpc
    agree = FakeLLM(script={dpc.PROFILE_CRITIC_TASK: FakeResponse(
        output={"classification": "derived"})})
    upheld = dpc.critique_profile_claim(
        db, agree, dataset_ref="ftr::s.t", field="authority_role", proposed_value="derived",
        context=_critic_context(), cited_refs=["cust_id"], context_revision="rev-1")
    assert upheld.disposition is dpc.ProfileCriticDisposition.UPHELD
    assert upheld.reason_codes == ("independent_agrees",)

    unknown = FakeLLM(script={dpc.PROFILE_CRITIC_TASK: FakeResponse(
        output={"classification": "unknown"})})
    abstained = dpc.critique_profile_claim(
        db, unknown, dataset_ref="ftr::s.t2", field="authority_role", proposed_value="derived",
        context=_critic_context(), cited_refs=["cust_id"], context_revision="rev-2")
    # Refute-oriented: absence of independent support is not evidence against.
    assert abstained.disposition is dpc.ProfileCriticDisposition.UPHELD
    assert abstained.reason_codes == ("independent_unknown",)


def test_the_critic_fails_soft_with_no_client(db) -> None:
    from featuregen.overlay.upload.attest import dataset_profile_critic as dpc
    outcome = dpc.critique_profile_claim(
        db, None, dataset_ref="ftr::s.t", field="temporal_storage_model",
        proposed_value="scd2", context=_critic_context(), cited_refs=["cust_id"],
        context_revision="rev-1")
    assert outcome.disposition is dpc.ProfileCriticDisposition.UPHELD
    assert outcome.reason_codes == ("critic_unavailable",)


def test_the_critic_replays_from_the_structured_result_store(db) -> None:
    from featuregen.overlay.upload.attest import dataset_profile_critic as dpc
    calls: list = []

    class _Counting(FakeLLM):
        def call(self, request):
            calls.append(request)
            return super().call(request)

    client = _Counting(script={dpc.PROFILE_CRITIC_TASK: FakeResponse(
        output={"classification": "derived"})})
    kwargs = dict(dataset_ref="ftr::s.t", field="authority_role", proposed_value="derived",
                  context=_critic_context(), cited_refs=["cust_id"], context_revision="rev-1")
    first = dpc.critique_profile_claim(db, client, **kwargs)
    second = dpc.critique_profile_claim(db, client, **kwargs)
    assert len(calls) == 1, "a byte-identical re-ask must replay, not re-dispatch"
    assert second.independent_value == first.independent_value
    assert second.structured_result_id == first.structured_result_id


def test_the_critic_never_dispatches_on_an_exhausted_budget(db) -> None:
    """Fix 1: the critic is a PHYSICAL provider call, so it must be spendable only out of the run's
    own call budget. With nothing left, it does NOT dispatch and says so honestly — never a silent
    skip, never a fabricated agreement or refutation."""
    from featuregen.overlay.upload.attest import dataset_profile_critic as dpc
    from featuregen.overlay.upload.enrich_batch import CallLedger
    calls: list = []

    class _Counting(FakeLLM):
        def call(self, request):
            calls.append(request)
            return super().call(request)

    client = _Counting(script={dpc.PROFILE_CRITIC_TASK: FakeResponse(
        output={"classification": "derived"})})
    ledger = CallLedger(max_provider_calls=0)
    outcome = dpc.critique_profile_claim(
        db, client, dataset_ref="ftr::s.t", field="authority_role", proposed_value="derived",
        context=_critic_context(), cited_refs=["cust_id"], context_revision="rev-1",
        call_budget=ledger)
    assert calls == [], "an exhausted budget must not reach the provider"
    assert outcome.disposition is dpc.ProfileCriticDisposition.NOT_ATTEMPTED
    assert outcome.reason_codes == ("critic_budget_exhausted",)
    assert outcome.independent_value is None


def test_a_replayed_critique_consumes_no_budget(db) -> None:
    """A replay is not a provider call, so it must cost nothing — otherwise a re-upload would burn
    the run's ceiling on questions nobody re-asks."""
    from featuregen.overlay.upload.attest import dataset_profile_critic as dpc
    from featuregen.overlay.upload.enrich_batch import CallLedger
    calls: list = []

    class _Counting(FakeLLM):
        def call(self, request):
            calls.append(request)
            return super().call(request)

    client = _Counting(script={dpc.PROFILE_CRITIC_TASK: FakeResponse(
        output={"classification": "derived"})})
    ledger = CallLedger(max_provider_calls=1)
    kwargs = dict(dataset_ref="ftr::s.t", field="authority_role", proposed_value="derived",
                  context=_critic_context(), cited_refs=["cust_id"], context_revision="rev-1",
                  call_budget=ledger)
    first = dpc.critique_profile_claim(db, client, **kwargs)
    assert ledger.calls == 1                       # the dispatch was charged
    second = dpc.critique_profile_claim(db, client, **kwargs)
    assert len(calls) == 1, "the second ask replayed"
    assert ledger.calls == 1, "a replayed critique is FREE — it charged nothing"
    assert second.structured_result_id == first.structured_result_id


def test_the_critic_refuses_a_field_outside_its_scope(db) -> None:
    from featuregen.overlay.upload.attest import dataset_profile_critic as dpc
    with pytest.raises(ValueError, match="outside this critic's scope"):
        dpc.critique_profile_claim(
            db, None, dataset_ref="ftr::s.t", field="table_description",
            proposed_value="x", context=_critic_context(), cited_refs=[],
            context_revision="rev-1")


def test_the_critic_replay_identity_folds_in_the_profile_vocabulary(db, monkeypatch) -> None:
    from featuregen.overlay.upload import profile_vocab
    from featuregen.overlay.upload.attest import dataset_profile_critic as dpc
    payload = dpc._payload("ftr::s.t", "authority_role", _critic_context(), ["cust_id"])
    before = dpc._input_hash(payload, field="authority_role", context_revision="rev-1")
    monkeypatch.setattr(dpc, "profile_vocabulary_fingerprint", lambda: "different")
    assert dpc._input_hash(payload, field="authority_role",
                           context_revision="rev-1") != before
    # And the fingerprint itself is order-insensitive over the closed vocabularies.
    assert profile_vocab.profile_vocabulary_fingerprint() == \
        profile_vocab.profile_vocabulary_fingerprint()


# ── the run's provider-call budget covers the critic (Fix 1) ─────────────────────────────────────


def _budget_items(n: int) -> list[BatchItem]:
    return [BatchItem(ref=f"tbl_{i:02d}", metadata={
        "table": f"tbl_{i:02d}",
        "column_profiles": [{"column": "cust_id", "operational_type": "unknown",
                             "declared_type": "varchar"}]}) for i in range(n)]


#: A synthesis proposing BOTH criticised classifications (each properly cited), so every resolved
#: table costs two critic dispatches on top of its share of the synthesis call.
_CLASSIFIED = _synthesis(authority_role="system_of_record", temporal_storage_model="snapshot",
                         evidence_refs=[_cite("authority_role", "cust_id"),
                                        _cite("temporal_storage_model", "cust_id")])


class _CountingClient(FakeLLM):
    """Counts every PHYSICAL provider call and answers BOTH Pass-B tasks from the request itself —
    a static script cannot, because each synthesis chunk carries different refs."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[str] = []

    def call(self, request):
        from featuregen.intake.llm import PROVIDER_OK, LLMResult
        from featuregen.intake.redaction import INPUT_KEY_CATALOG
        from featuregen.overlay.upload.attest.dataset_profile_critic import PROFILE_CRITIC_TASK
        self.tasks.append(request.task)
        if request.task == PROFILE_CRITIC_TASK:
            return LLMResult(output={"classification": "unknown"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK)
        items = request.inputs[INPUT_KEY_CATALOG]["items"]
        return LLMResult(
            output={"results": [{"ref": it["ref"], "synthesis": _CLASSIFIED} for it in items]},
            self_reported_scores={}, call_ref="", status=PROVIDER_OK)

    @property
    def critic_calls(self) -> list[str]:
        from featuregen.overlay.upload.attest.dataset_profile_critic import PROFILE_CRITIC_TASK
        return [t for t in self.tasks if t == PROFILE_CRITIC_TASK]


# ── replay persistence of the synthesis itself ───────────────────────────────────────────────────


def test_the_accepted_synthesis_lands_in_the_structured_result_store(db) -> None:
    items = [BatchItem(ref=_TABLE, metadata={
        "table": _TABLE,
        "column_profiles": [{"column": "cust_id", "operational_type": "unknown",
                             "declared_type": "varchar"}]})]
    synth = _synthesis(grain_columns=["cust_id"], table_role="dimension",
                       business_context="It records customers.",
                       evidence_refs=[_cite("business_context", "cust_id")])
    client = FakeLLM(script={"table_synth": FakeResponse(output={"results": [
        {"ref": _TABLE, "synthesis": synth}]})})
    resolved = ts.synthesize_tables(db, client, items,
                                    columns_by_table={_TABLE: {"cust_id"}}, actor=None,
                                    catalog_source="ftr")
    assert resolved[_TABLE]["business_context"] == "It records customers."
    rows = db.execute(
        "SELECT input_content_hash FROM structured_result WHERE result_type = %s",
        (ts.PASS_B_RESULT_TYPE,)).fetchall()
    assert len(rows) == 1
    stored = find_structured_result(
        db, result_type=ts.PASS_B_RESULT_TYPE, result_version=ts.PASS_B_RESULT_VERSION,
        input_content_hash=rows[0][0])
    assert stored is not None
    assert stored.output["business_context"] == "It records customers."
    assert stored.output["grain"] == {"columns": ["cust_id"], "is_unique": True}


def test_the_run_wide_call_ceiling_counts_the_critic(db, monkeypatch) -> None:
    """Fix 1 (the finding): a Pass-B run over N tables issues ONE synthesis call per chunk plus TWO
    critic calls per resolved table. Before the fix the critic calls were invisible to
    ``max_provider_calls``, so a 12-table run spent 3 counted + 12 uncounted calls. The ceiling now
    bounds the PHYSICAL total."""
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_PROVIDER_CALLS", "4")
    items = _budget_items(6)
    client = _CountingClient()
    ts.synthesize_tables(db, client, items,
                         columns_by_table={it.ref: {"cust_id"} for it in items},
                         actor=None, catalog_source="ftr")
    assert client.critic_calls, "the critic DID dispatch — the ceiling is not vacuously satisfied"
    assert len(client.tasks) <= 4, client.tasks


def test_an_exhausted_budget_disposes_the_rest_not_attempted_and_the_run_completes(
        db, monkeypatch) -> None:
    """Exhaustion is honest, not silent: the fields whose critique never ran are ``not_attempted``
    (never ``refuted``, never accepted unreviewed), and the synthesis run still returns its
    resolved tables."""
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_PROVIDER_CALLS", "3")
    items = _budget_items(6)
    client = _CountingClient()
    disp: list[dict] = []
    resolved = ts.synthesize_tables(db, client, items,
                                    columns_by_table={it.ref: {"cust_id"} for it in items},
                                    actor=None, dispositions=disp, catalog_source="ftr")
    assert resolved, "the run completed — a spent critic budget never fails Pass B"
    classified = [r for r in disp
                  if r["field"] in ("authority_role", "temporal_storage_model")]
    starved = [r for r in classified if r["status"] == "not_attempted"]
    assert starved, "the budget ran out mid-run — some critiques must be unattempted"
    assert {r["reason"] for r in starved} == {"critic_budget_exhausted"}
    # An unattempted critique NEVER lands its classification, and is never mislabeled a refutation.
    for rec in starved:
        assert resolved.get(rec["table"], {}).get(rec["field"]) in (None, "")
    assert all(r["reason"] != "critic_budget_exhausted"
               for r in classified if r["status"] == "refuted")


def test_replay_is_stable_for_a_byte_identical_re_run(db) -> None:
    items = [BatchItem(ref=_TABLE, metadata={
        "table": _TABLE,
        "column_profiles": [{"column": "cust_id", "operational_type": "unknown",
                             "declared_type": "varchar"}]})]
    synth = _synthesis(grain_columns=["cust_id"])
    client = FakeLLM(script={"table_synth": FakeResponse(output={"results": [
        {"ref": _TABLE, "synthesis": synth}]})})
    kwargs = dict(columns_by_table={_TABLE: {"cust_id"}}, actor=None, catalog_source="ftr")
    ts.synthesize_tables(db, client, items, **kwargs)
    ts.synthesize_tables(db, client, items, **kwargs)
    rows = db.execute(
        "SELECT count(*) FROM structured_result WHERE result_type = %s",
        (ts.PASS_B_RESULT_TYPE,)).fetchone()
    assert rows[0] == 1, "a byte-identical re-run replays onto the SAME content-addressed row"
