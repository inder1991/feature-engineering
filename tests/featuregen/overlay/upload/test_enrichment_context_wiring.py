"""Joint Task 4 (b) — the Pass-A payloads ARE the semantic-context purpose adapters.

What this pins:

* every Pass-A task's payload is projected from `SemanticContextBundleV1` through its purpose
  adapter, and carries the widened context the plan names (source definition, business term,
  declared type, domain, synonyms/related terms, BIAN/FIBO paths, process path, table role, primary
  entity, bounded neighbour roster);
* D10 — EVERY new key has an explicit egress classification, and the fail-closed gates actually
  admit it (golden egress: each key is either PII-scanned or structural, and an unclassified
  neighbour of it still blocks);
* D12.5 — the roster and the table context enter the PROMPT but NOT the per-column cache key or the
  evidence `input_hash`: a sibling's reclassification must not stale and rewrite every column;
* the re-budgeted batch bounds actually hold the measured payloads, and the roster rides PER-ITEM
  metadata (which `estimate_tokens` measures) rather than `shared_metadata` (which it does not).
"""
from __future__ import annotations

import json

import pytest

from featuregen.overlay.upload import enrich, enrich_config
from featuregen.overlay.upload import enrich_llm as llm
from featuregen.overlay.upload import semantic_context as sc
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich_batch import BatchItem, chunk_items, estimate_tokens
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload

_SOURCE = "ftr"
_TABLE = "comp_fin_tran"
_SCHEMA = "dpl_core"


def _row(column: str, *, definition: str = "", type_: str = "unknown") -> CanonicalRow:
    return CanonicalRow(_SOURCE, _TABLE, column, type_, definition=definition)


def _record(column: str, **over) -> GlossaryRecord:
    base = dict(
        logical_ref=f"{_SOURCE}::{_SCHEMA}.{_TABLE}.{column}",
        term_name="Counterparty BIC",
        definition="The SWIFT BIC of the counterparty bank.",
        domain="Compliance",
        synonyms=("SWIFT Code", "BIC"),
        bian_path="Party Reference Data Directory > Party Routing Profile",
        fibo_path="fibo-fbc:BankIdentifier",
        term_type="Attribute",
        process_path="Payments > Cross Border > Beneficiary Routing",
        related_terms=("Sender BIC", "Beneficiary Bank"),
        schema=_SCHEMA,
        physical_fqn=f"{_SCHEMA}.{_TABLE}.{column}",
        declared_type="varchar(11)",
    )
    base.update(over)
    return GlossaryRecord(**base)


def _upload(columns: list[str]) -> GlossaryUpload:
    return GlossaryUpload(rows=[_row(c) for c in columns],
                          records=[_record(c) for c in columns])


def _table_context() -> tuple[sc.SemanticValueV1, ...]:
    return (
        sc.SemanticValueV1(field_name="primary_entity", value="counterparty", evidence=(),
                           resolution_status="current"),
        sc.SemanticValueV1(field_name="table_role", value="event_fact", evidence=(),
                           resolution_status="current"),
    )


def _bundle(column: str, cohort: list[str]) -> sc.SemanticContextBundleV1:
    rows = [_row(c) for c in cohort]
    anchor = next(r for r in rows if r.column == column)
    return sc.bundle_from_upload(anchor, glossary_record=_record(column), cohort=rows,
                                 roles=enrich._ENRICHMENT_ROLES,
                                 table_context=_table_context())


# ── the payload really is the adapter, and carries everything the plan names ─────────────────────


def test_the_classifier_payload_carries_every_named_context_field() -> None:
    cohort = ["counter_party_bic", "counter_party_cif_id", "tran_crncy"]
    bundle = _bundle("counter_party_bic", cohort)
    payload = enrich._classifier_payload(_row("counter_party_bic"),
                                         _record("counter_party_bic"), bundle)
    assert payload["business_definition"].startswith("The SWIFT BIC")   # source definition
    assert payload["term_name"] == "Counterparty BIC"                   # business term
    assert payload["type"] == "varchar(11)"                             # declared type
    assert payload["data_domain"] == "Compliance"                       # domain
    assert set(payload["synonyms"]) == {"SWIFT Code", "BIC"}            # synonyms
    assert payload["related_terms"] == ["Sender BIC", "Beneficiary Bank"]   # related terms
    assert payload["bian_path"].startswith("Party Reference Data")      # BIAN
    assert payload["fibo_path"] == "fibo-fbc:BankIdentifier"            # FIBO
    assert payload["process_path"].startswith("Payments >")             # process path
    assert payload["table_role"] == "event_fact"                        # table role
    assert payload["primary_entity"] == "counterparty"                  # primary entity
    roster = {e["column"] for e in payload["column_roster"]}            # bounded roster
    assert roster == {"counter_party_cif_id", "tran_crncy"}             # siblings, never itself


def test_the_roster_is_bounded_and_carries_the_sibling_semantics() -> None:
    cohort = [f"col_{i:03d}" for i in range(120)] + ["anchor"]
    bundle = _bundle("anchor", cohort)
    payload = enrich._classifier_payload(_row("anchor"), _record("anchor"), bundle)
    assert len(payload["column_roster"]) == sc.ADAPTER_LIST_LIMIT
    assert all(set(e) <= llm._ROSTER_ENTRY_KEYS for e in payload["column_roster"])


def test_the_drafting_tasks_receive_the_current_stronger_facts() -> None:
    bundle = _bundle("counter_party_bic", ["counter_party_bic", "tran_crncy"])
    payload = enrich._drafting_payload(
        bundle, {"table": _TABLE, "column": "counter_party_bic", "type": "varchar(11)",
                 "concept": "bank_bic"},
        row=_row("counter_party_bic"), rec=_record("counter_party_bic"))
    assert payload["concept"] == "bank_bic"
    assert payload["term_name"] == "Counterparty BIC"
    assert payload["data_domain"] == "Compliance"
    assert payload["process_path"].startswith("Payments >")
    assert payload["table_role"] == "event_fact"
    assert payload["primary_entity"] == "counterparty"
    # A drafting task answers about ONE column: a sibling roster is contamination surface here,
    # not signal, so `for_summary` deliberately omits it.
    assert "column_roster" not in payload


def test_a_source_definition_is_never_paraphrased_into_competing_evidence(db) -> None:
    """The definition drafter only ever fills a BLANK. A column whose source declares a definition
    is not a target, so no `llm/proposed` definition can ever compete with it — the rule is enforced
    by the target set, not by hoping the model behaves. The context it DOES receive still carries
    the curated definition (so a blank sibling's draft is informed by it)."""
    from featuregen.intake.llm import FakeLLM, FakeResponse
    declared = _row("has_definition", definition="Already curated.")
    blank = _row("no_definition")
    rows = [declared, blank]
    client = FakeLLM(script={"overlay.enrich.definition": FakeResponse(output={"results": [
        {"ref": enrich.content_hash(blank), "definition": "drafted"}]})})
    out = enrich.draft_definitions(db, rows, client)
    assert enrich.content_hash(declared) not in out
    assert out[enrich.content_hash(blank)] == "drafted"


# ── D10: every new key is classified, and the fail-closed gates admit it ─────────────────────────

#: The keys joint Task 4 adds to a Pass-A/B payload. Each must be classified EXACTLY once across
#: the three top-level classes, and admitted by the per-item batch gate where it rides items.
_NEW_TOP_LEVEL_KEYS = (
    "semantic_terms", "primary_entity", "declared_type", "operational_type", "concept_path",
    "table_description", "business_context", "authority_role", "temporal_storage_model",
    "evidence_refs",
)


@pytest.mark.parametrize("key", _NEW_TOP_LEVEL_KEYS)
def test_every_new_key_has_exactly_one_egress_classification(key: str) -> None:
    classes = [key in llm._FREE_TEXT_META_KEYS, key in llm._STRUCTURAL_META_KEYS,
               key in llm._ROUNDTRIP_PROSE_KEYS]
    assert sum(classes) == 1, (key, classes)


@pytest.mark.parametrize("key,expected_kind", [
    ("semantic_terms", "definition"),
    ("table_description", "definition"),
    ("business_context", "definition"),
])
def test_new_free_text_keys_are_pii_scanned_as_their_declared_kind(key, expected_kind) -> None:
    assert llm._meta_field_kind(key) == expected_kind
    scrubbed, _spans, _audits, version = llm._redact_free_text_meta(
        {key: "reachable at john.doe@example.com"})
    assert scrubbed is not None
    assert version is not None                             # it WAS scanned, not waved through
    assert "john.doe@example.com" not in scrubbed[key]     # and the scan actually scrubbed


# ── the narrative TABLE fields are the same class of field as a definition ───────────────────────

#: The reviewer's probe text: an FTR-shaped sample clause carrying an account number, an amount and
#: a personal name. The PII backstop catches none of the three by itself.
_SAMPLE_LADEN = ("Posted ledger transactions per counterparty. The sample profile is TEXT, with "
                 "representative values such as ACCT-8891; 1234.56; Jane Roe.")

#: The three keys that carry curated TABLE narrative into a prompt. `table_definition` already had
#: the definition-grade pipeline; the other two are the same class of field and now share it.
_NARRATIVE_KEYS = ("table_definition", "business_context", "table_description")


@pytest.mark.parametrize("key", _NARRATIVE_KEYS)
def test_a_narrative_table_field_is_sample_stripped_not_merely_pii_scanned(key: str) -> None:
    scrubbed, _spans, audits, _v = llm._redact_free_text_meta({"table": "t", key: _SAMPLE_LADEN})
    assert scrubbed is not None
    for value in ("ACCT-8891", "1234.56", "Jane Roe"):
        assert value not in scrubbed[key], (key, value)
    assert any(a["path"] == key and a["state"] == "stripped" for a in audits), audits


def test_the_narrative_table_fields_yield_BYTE_IDENTICAL_output() -> None:
    """The probe the finding names: the SAME text must not egress differently because of which
    narrative key it happened to ride."""
    out = {key: llm._redact_free_text_meta({"table": "t", key: _SAMPLE_LADEN})[0][key]
           for key in _NARRATIVE_KEYS}
    assert len(set(out.values())) == 1, out


@pytest.mark.parametrize("key", _NARRATIVE_KEYS)
def test_a_narrative_table_field_fails_closed_on_an_unhandled_data_marker(key: str) -> None:
    """Fail-closed parity: a sample clause the stripper cannot consume blocks the ITEM, exactly as
    it does for a business/table definition — the prose grade had no such gate at all."""
    blocked, _spans, _audits, _v = llm._redact_free_text_meta(
        {"table": "t", key: "sample values: OPN; CLS; PND"})
    assert blocked is None


def test_the_live_producer_of_semantic_terms_egresses_it_stripped() -> None:
    """The byte-impact check: `for_summary` is the ONE live producer that puts `semantic_terms`
    into item metadata (summary/definition/synonyms/unit). Its payload's bytes change exactly when
    the value carries a sample clause — which is the fix, not a regression."""
    assert "semantic_terms" in sc.SUMMARY_RENDERED_KEYS      # the producer contract, pinned
    payload = {"table": _TABLE, "column": "counter_party_bic", "semantic_terms": _SAMPLE_LADEN}
    scrubbed, _spans, audits, _v = llm._redact_free_text_meta(payload)
    assert scrubbed is not None
    assert scrubbed["semantic_terms"] == "Posted ledger transactions per counterparty."
    assert any(a["path"] == "semantic_terms" and a["state"] == "stripped" for a in audits)


def test_semantic_terms_is_definition_grade_on_BOTH_egress_seams() -> None:
    """It was definition-kind in the feature-menu adapter and prose in the enrichment classifier —
    one field, two grades. The two seams now agree."""
    assert llm._meta_field_kind("semantic_terms") == "definition"
    assert "semantic_terms" in llm._FEATURE_COLUMN_DEFINITION_KEYS
    scrubbed, _spans, _audits, _v = llm._redact_free_text_meta(
        {"table": "t", "semantic_terms": _SAMPLE_LADEN})
    menu, _s, _a, _v2 = llm.sanitize_feature_context(
        {"columns": [{"column": "c", "semantic_terms": _SAMPLE_LADEN}]})
    assert scrubbed is not None and menu is not None
    assert scrubbed["semantic_terms"] == menu["columns"][0]["semantic_terms"]


@pytest.mark.parametrize("key", ["primary_entity", "declared_type", "operational_type",
                                 "authority_role", "temporal_storage_model"])
def test_new_structural_keys_pass_the_fail_closed_top_level_gate(key: str) -> None:
    scrubbed, _spans, _audits, version = llm._redact_free_text_meta({key: "value"})
    assert scrubbed == {key: "value"}       # admitted, untouched
    assert version is None                  # structural: nothing to scan


def test_an_unclassified_neighbour_of_a_new_key_still_fails_closed() -> None:
    """The point of the classification list is that it is CLOSED. Adding keys must not have opened
    the gate for their neighbours."""
    blocked, _s, _a, _v = llm._redact_free_text_meta(
        {"primary_entity": "counterparty", "primary_entity_notes": "free prose"})
    assert blocked is None


def test_the_widened_roster_entry_keys_are_admitted_and_bounded() -> None:
    ok = {"column": "cust_id", "operational_type": "text", "declared_type": "varchar",
          "concept": "customer_id", "party_role": "subject"}
    assert llm._roster_entry_ok(ok)
    assert not llm._roster_entry_ok({**ok, "sensitivity": "pii"})    # unclassified -> blocked
    assert not llm._roster_entry_ok({**ok, "concept": "x" * 201})    # over the per-value bound


def test_a_full_classifier_item_passes_the_per_item_egress_contract() -> None:
    bundle = _bundle("counter_party_bic", ["counter_party_bic", "counter_party_cif_id"])
    payload = enrich._classifier_payload(_row("counter_party_bic"),
                                         _record("counter_party_bic"), bundle)
    assert llm._item_egress_ok(payload), sorted(payload)


# ── D12.5: context enters the PROMPT, never the identity ─────────────────────────────────────────


def test_a_sibling_reclassification_does_not_move_the_concept_cache_key() -> None:
    row, rec = _row("counter_party_bic"), _record("counter_party_bic")
    before = enrich.concept_cache_key(row, rec)
    # The very change D12.5 protects against: a NEIGHBOUR gains a concept, so this column's prompt
    # changes — but its cache key and its evidence input_hash must not move.
    narrow = _bundle("counter_party_bic", ["counter_party_bic"])
    wide = _bundle("counter_party_bic", ["counter_party_bic", "tran_crncy", "pstd_date"])
    assert (enrich._classifier_payload(row, rec, narrow)
            != enrich._classifier_payload(row, rec, wide))     # the PROMPT did change
    assert enrich.concept_cache_key(row, rec) == before         # the IDENTITY did not


def test_the_evidence_material_is_the_identity_payload_not_the_prompt() -> None:
    """`_concept_metadata` remains the evidence `input_hash` material: it carries no roster and no
    table context, so `_write_concept_evidence` cannot rewrite every sibling's evidence when one
    column is reclassified."""
    material = enrich._concept_metadata(_row("counter_party_bic"), _record("counter_party_bic"))
    assert "column_roster" not in material
    assert "table_role" not in material and "primary_entity" not in material


# ── batch bounds: the re-budget is real, and the estimator can see the roster ────────────────────


def test_the_rebudgeted_bounds_hold_the_measured_payloads() -> None:
    """The measured FTR maxima recorded in `enrich_config` must fit their chunks — the whole point
    of re-budgeting. The per-item figures are the RESOLVED ones (every sibling carrying a concept +
    party role), re-measured in the review: 1,144 tok for a classifier item with a full 40-entry
    roster, 3,146 tok for a whole-table domain item. The old 750 here understated the classifier by
    ~50% and made the headroom look like 60% when it is ~5%."""
    assert enrich_config.max_items("concept") * 1144 <= enrich_config.max_input_tokens("concept")
    assert enrich_config.max_items("domain") * 3146 <= enrich_config.max_input_tokens("domain")
    # The isolation boundaries themselves are UNCHANGED: payload size never buys itself a wider
    # contamination surface.
    assert enrich_config.max_items("concept") == 20
    assert enrich_config.max_items("domain") == 8


def test_a_widened_item_still_chunks_by_item_count_not_by_bytes() -> None:
    bundle = _bundle("counter_party_bic", [f"c{i:03d}" for i in range(60)] + ["counter_party_bic"])
    payload = enrich._classifier_payload(_row("counter_party_bic"),
                                         _record("counter_party_bic"), bundle)
    items = [BatchItem(f"h{i}", payload) for i in range(40)]
    chunks = chunk_items(items, max_items=enrich_config.max_items("concept"),
                         max_input_tokens=enrich_config.max_input_tokens("concept"))
    # Item-count bound, exactly as before the widening: same call count, same deadline accounting.
    assert [len(c) for c in chunks] == [20, 20]


def test_the_roster_rides_per_item_metadata_so_the_estimator_measures_it() -> None:
    """`estimate_tokens` measures ITEM metadata only. That is correct for the one shared block that
    exists (the cached classification vocabulary), which is why the roster must never be moved into
    `shared_metadata` — the budget would go blind to it."""
    bundle = _bundle("counter_party_bic", ["counter_party_bic"] + [f"c{i}" for i in range(30)])
    payload = enrich._classifier_payload(_row("counter_party_bic"),
                                         _record("counter_party_bic"), bundle)
    bare = {k: v for k, v in payload.items() if k != "column_roster"}
    assert estimate_tokens(BatchItem("h", payload)) > estimate_tokens(BatchItem("h", bare))
    assert "column_roster" in json.dumps(payload)


def test_no_pass_a_caller_puts_context_into_shared_metadata(db, monkeypatch) -> None:
    from featuregen.intake.llm import FakeLLM, FakeResponse
    seen: list[dict] = []

    class _Capture(FakeLLM):
        def call(self, request):
            from featuregen.intake.redaction import INPUT_KEY_CATALOG
            catalog = request.inputs[INPUT_KEY_CATALOG]
            if request.task == enrich._TASK and "items" in catalog:   # the BATCH classifier call
                seen.append({k: v for k, v in catalog.items() if k != "items"})
            return super().call(request)

    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload = _upload(["counter_party_bic", "tran_crncy"])
    rows = upload.rows
    client = _Capture(script={enrich._TASK: FakeResponse(output={"results": [
        {"ref": enrich.content_hash(r), "concept": "bank_bic"} for r in rows]})})
    enrich.enrich_concepts(db, rows, client, glossary=upload)
    assert seen, "the classifier never dispatched"
    for shared in seen:
        assert "column_roster" not in shared
        assert set(shared) <= {"vocabulary"}
