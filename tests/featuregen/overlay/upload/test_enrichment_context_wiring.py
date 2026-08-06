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
    # The cohort is sized OFF the limit, not off a literal: the 2026-08-06 raise took
    # ADAPTER_LIST_LIMIT 40 -> 256, and a fixed 120-column cohort would have quietly stopped
    # testing the bound (120 < 256 returns 120 and the assertion becomes an identity).
    cohort = [f"col_{i:03d}" for i in range(sc.ADAPTER_LIST_LIMIT + 20)] + ["anchor"]
    bundle = _bundle("anchor", cohort)
    payload = enrich._classifier_payload(_row("anchor"), _record("anchor"), bundle)
    assert len(payload["column_roster"]) == sc.ADAPTER_LIST_LIMIT
    assert all(set(e) <= llm._ROSTER_ENTRY_KEYS for e in payload["column_roster"])


def test_a_real_table_now_fits_the_roster_whole_so_nothing_is_sliced_off() -> None:
    """The point of the raise. `canonical.MAX_COLUMNS_PER_TABLE` caps ingestion at 200 columns per
    table, and ADAPTER_LIST_LIMIT is now 256 — so for EVERY table this platform can ingest the
    sibling roster is COMPLETE, and which siblings a classifier sees is no longer an ordering
    accident. At the old 40 a 126-column FTR table showed the classifier under a third of them."""
    from featuregen.overlay.upload.canonical import MAX_COLUMNS_PER_TABLE

    assert sc.ADAPTER_LIST_LIMIT >= MAX_COLUMNS_PER_TABLE
    assert sc.NEIGHBOUR_LIMIT >= MAX_COLUMNS_PER_TABLE
    widest = [f"col_{i:03d}" for i in range(MAX_COLUMNS_PER_TABLE - 1)] + ["anchor"]
    payload = enrich._classifier_payload(_row("anchor"), _record("anchor"),
                                         _bundle("anchor", widest))
    assert len(payload["column_roster"]) == MAX_COLUMNS_PER_TABLE - 1   # every sibling, none cut


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
    # The bound is read from the constant, not restated: the 2026-08-06 raise moved it 200 -> 1000
    # and a literal here would have silently become an assertion that 201 chars is admitted.
    assert llm._roster_entry_ok({**ok, "concept": "x" * llm._MAX_LEN_DEFAULT})
    assert not llm._roster_entry_ok({**ok, "concept": "x" * (llm._MAX_LEN_DEFAULT + 1)})


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


def _resolved_roster(payload: dict) -> dict:
    """The payload with every roster entry at its RESOLVED fill — column + concept + party role.

    This is the shape a RE-upload produces (every sibling already classified) and it is ~3.5x the
    bytes of the bare first-upload roster, so it, not the bare one, is the size the budgets must
    hold. Modelled here rather than round-tripped through the store because the size question is
    about the JSON the estimator sees, and `_classifier_payload` builds that from the bundle.
    """
    return {**payload,
            "column_roster": [{**e, "concept": "monetary_stock", "party_role": "counterparty"}
                              for e in payload["column_roster"]]}


#: A definition EXACTLY at `MAX_DEFINITION_LEN`. The whole point of the 32_000 raise is that this
#: length now egresses, so it is the length the packing budgets must be proved against — an item
#: built at any smaller definition makes every assertion below vacuous.
_DEF_AT_CAP = ("The settlement amount of the posted financial transaction, expressed in the "
               "transaction currency. " * 2000)[:llm.MAX_DEFINITION_LEN]

#: The REALISTIC source-attribute fill, not the cap. `ftr_adapter._MAX_SOURCE_ATTRIBUTES` was raised
#: 40 -> 256 and `_MAX_SOURCE_ATTRIBUTE_LEN` 240 -> 1000, but those are ceilings set by the egress
#: gates downstream, not by any observed file: the real FTR export has 17 headers IN TOTAL. Modelled
#: at 17 entries of the full 1000 chars — every real attribute at its new maximum length — because
#: that is the shape the budgets have to hold. The degenerate 256 x 1000 fill is a different
#: question and is pinned separately, below.
_REALISTIC_SOURCE_ATTRS = tuple(
    (f"governance_header_{i}: " + "v" * 1000)[:1000] for i in range(17))


def _record_at_the_definition_cap(column: str) -> GlossaryRecord:
    """A fully-populated FTR-shaped sidecar whose definition sits AT the cap, with
    `source_attributes` at their realistic count and their new maximum LENGTH, so the
    non-definition metadata is modelled at its realistic maximum too, not at zero."""
    return _record(column, definition=_DEF_AT_CAP, source_attributes=_REALISTIC_SOURCE_ATTRS)


def _classifier_item_at_the_new_caps(*, siblings: int, name_len: int = 30) -> dict:
    """A concept item at the zero-truncation caps on BOTH axes: a SATURATED roster of long
    bank-style names at RESOLVED fill, AND the anchor's own prose at the definition cap with a
    realistic source-attribute sidecar.

    The prose half was missing at first — the item saturated the roster but carried a 40-character
    fixture definition against a 32_000 cap, so it proved packing on the axis this task did not
    widen and left the axis it did widen untested. Anything smaller on either axis makes the
    packing assertions below vacuous.
    """
    pad = "x" * max(0, name_len - 7)
    cohort = [f"col{pad}{i:04d}" for i in range(siblings)] + ["anchor"]
    bundle = _bundle("anchor", cohort)
    payload = enrich._classifier_payload(
        _row("anchor"), _record_at_the_definition_cap("anchor"), bundle)
    assert len(payload["business_definition"]) == llm.MAX_DEFINITION_LEN   # genuinely at the cap
    return _resolved_roster(payload)


def _drafting_item_at_the_definition_cap() -> dict:
    """The item shape the `definition` / `synonyms` / `unit` stages dispatch, at the cap."""
    rec = _record_at_the_definition_cap("anchor")
    payload = enrich._drafting_payload(
        _bundle("anchor", ["anchor"]),
        {"table": _TABLE, "column": "anchor", "type": "varchar(11)", "concept": "monetary_stock"},
        row=_row("anchor"), rec=rec)
    assert len(payload["business_definition"]) == llm.MAX_DEFINITION_LEN   # genuinely at the cap
    return payload


def test_the_rebudgeted_bounds_hold_the_measured_payloads() -> None:
    """The measured maxima must fit their chunks — the whole point of re-budgeting.

    RE-MEASURED 2026-08-06 at the zero-truncation caps, resolved roster, long bank-style names:

        roster fill                              tok/item   chunk cost (x20)   % of budget
        40 entries  (the OLD ADAPTER_LIST_LIMIT)      982             19_640         9.8% of 200_000
        199 entries (the widest INGESTIBLE table)   5_365            107_300        53.6%
        256 entries (a saturated ADAPTER_LIST)      6_862            137_240        68.6%

    The 982 reproduces the 971-1,144 the previous review recorded, which is why the rest is
    trustworthy. Note what the middle row would have cost at the OLD 24_000 concept budget: 107_300
    is 4.5x it, so the concept stage would have packed ~4 items per chunk instead of 20 — a 5x
    call-count increase on every re-upload. The Step-5 token raise is load-bearing, not decorative.

    RE-DERIVED (2026-08-06, Task 4b re-review): the concept assertion was `max_items * 6862 <=
    max_input_tokens` — 137_240 of 200_000, 69%, a real bound when written. A concurrent commit then
    doubled that budget to 400_000 and the same line silently became a 34% no-op: the constant moved
    beneath the test. It now MEASURES the item instead of restating a stale literal, and asserts the
    budget is sized to that item rather than merely larger than it — so the next budget change has
    to come back here, exactly as the `<6_000` allowance guard does.
    """
    from featuregen.overlay.upload.canonical import MAX_COLUMNS_PER_TABLE

    measured = estimate_tokens(BatchItem(
        "h", _classifier_item_at_the_new_caps(siblings=MAX_COLUMNS_PER_TABLE - 1)))
    chunk_cost = enrich_config.max_items("concept") * measured
    assert chunk_cost <= enrich_config.max_input_tokens("concept"), (
        f"the widest real concept item ({measured} tok) no longer packs its full chunk")
    # …and the budget is SIZED to it, not arbitrarily above it. Halving the budget must not still
    # fit, or the number has stopped being a derivation and become a round number.
    assert chunk_cost > enrich_config.max_input_tokens("concept") // 2, (
        f"{chunk_cost} uses under half of {enrich_config.max_input_tokens('concept')} — the budget "
        f"is no longer derived from the item; re-derive it or lower it")
    # The domain item is a whole TABLE and carries no ADAPTER_LIST_LIMIT-bounded list, so its
    # measured 3,146 tok/item is untouched by the cap raise.
    assert enrich_config.max_items("domain") * 3146 <= enrich_config.max_input_tokens("domain")
    # The isolation boundaries themselves are UNCHANGED: payload size never buys itself a wider
    # contamination surface.
    assert enrich_config.max_items("concept") == 20
    assert enrich_config.max_items("domain") == 8


# ── MAX_DEFINITION_LEN 4000 -> 32_000: every stage carrying a definition must still pack ──────────


def test_a_definition_at_the_cap_costs_the_tokens_the_budget_was_derived_from() -> None:
    """The arithmetic the `enrich_config` budget block states, asserted rather than trusted.

    `estimate_tokens` is exactly `len(json.dumps(metadata)) // 4`, so a definition at the 32_000 cap
    is 8_000 estimated tokens BY CONSTRUCTION — that is the number every budget below was derived
    from, and if the cap moves without the budgets this is the assertion that says so.
    """
    payload = _drafting_item_at_the_definition_cap()
    definition_tokens = llm.MAX_DEFINITION_LEN // 4
    assert definition_tokens == 8_000

    total = estimate_tokens(BatchItem("h", payload))
    # The definition dominates the item, and the REST is the "other metadata" allowance the
    # derivation assumed. Pinning it bounds the assumption instead of leaving it prose.
    assert total >= definition_tokens
    # RE-DERIVED (Task 4b review, Important #1): this guard was written at <2_000 and it FIRED when
    # `ftr_adapter._MAX_SOURCE_ATTRIBUTE_LEN` went 240 -> 1000 — doing exactly its job. 17 realistic
    # source attributes at the new 1000-char maximum are ~4_250 tokens on their own, so the
    # non-definition allowance is now ~4_400. The bound is re-set with margin rather than relaxed to
    # fit: it must still fail on the NEXT few-thousand-token addition, and the packing consequence
    # is asserted directly below so the number is never the only thing holding the budget up.
    other = total - definition_tokens
    assert other < 6_000, f"non-definition metadata outgrew the derivation: {other}"
    # The derivation exists to protect PACKING, so assert that, not just the arithmetic.
    assert (enrich_config.max_items("definition") * total
            <= enrich_config.max_input_tokens("definition"))


@pytest.mark.parametrize("short", ["definition", "synonyms", "unit"])
def test_the_drafting_stages_still_pack_a_full_chunk_at_the_definition_cap(short: str) -> None:
    """`definition`/`synonyms`/`unit` dispatch the SAME `_drafting_payload` item at the SAME
    `max_items`, so all three are proved together — raising one budget and not its twins would have
    left two stages shattered for the identical reason.

    At the 4000 cap these packed 8 per chunk; a 32_000-char definition is 8_000 tokens, so 8 of them
    (66_464) crossed the old 60_000 budget and packing fell to 7 — ~14% more provider calls on
    stages that fan out PER COLUMN, against an unchanged `max_provider_calls`. The budget move is
    what puts it back, and this is the assertion that fails if it is reverted.
    """
    payload = _drafting_item_at_the_definition_cap()
    items = [BatchItem(f"h{i}", payload) for i in range(40)]
    chunks = chunk_items(items, max_items=enrich_config.max_items(short),
                         max_input_tokens=enrich_config.max_input_tokens(short))
    assert max(len(c) for c in chunks) == enrich_config.max_items(short) == 8
    assert len(chunks) == 5           # 40 items / 8 — bound by ITEM COUNT, not by tokens


def test_the_summary_stage_still_packs_a_full_chunk_at_the_definition_cap() -> None:
    """`summary_payload` is `_drafting_payload`'s superset (it also carries the ingest tail's
    dossier extras), so it is the LARGEST of the 8-item stages and must be checked, not assumed.

    It packed 8 per chunk at its old 100_000 too — the measured margin was real. The raise to
    200_000 is about the ORDERING, asserted below: the bigger item must never hold the smaller
    budget, or a pathological fill shatters summary while its own subset survives.
    """
    assert (enrich_config.max_input_tokens("summary")
            >= enrich_config.max_input_tokens("definition")), \
        "summary's item is definition's SUPERSET; it cannot have the tighter budget"
    payload = enrich.summary_payload(
        _row("anchor"), _record_at_the_definition_cap("anchor"),
        {"concept": "monetary_stock", "party_role": "counterparty", "ai_synonyms": ["a", "b"]},
        _bundle("anchor", ["anchor"]))
    assert len(payload["business_definition"]) == llm.MAX_DEFINITION_LEN
    chunks = chunk_items([BatchItem(f"h{i}", payload) for i in range(40)],
                         max_items=enrich_config.max_items("summary"),
                         max_input_tokens=enrich_config.max_input_tokens("summary"))
    assert max(len(c) for c in chunks) == enrich_config.max_items("summary") == 8


def test_the_concept_stage_still_packs_at_a_capped_definition_AND_a_saturated_roster() -> None:
    """The concept item carries BOTH a `business_definition` and the sibling roster, so it is the
    stage the 32_000 cap hurt most: 20 x (8_000 + ~5_200) = ~264_600 against the old 200_000 budget
    dropped it from 20 items per chunk to 15 — a third more calls on the widest-fanning stage.

    Built at the widest table ingestion admits (`MAX_COLUMNS_PER_TABLE`), every sibling resolved,
    every column's definition at the cap.
    """
    from featuregen.overlay.upload.canonical import MAX_COLUMNS_PER_TABLE

    cohort = ([f"col_bank_style_name_{i:04d}" for i in range(MAX_COLUMNS_PER_TABLE - 1)]
              + ["anchor"])
    rec = _record_at_the_definition_cap("anchor")
    payload = _resolved_roster(
        enrich._classifier_payload(_row("anchor"), rec, _bundle("anchor", cohort)))
    assert len(payload["business_definition"]) == llm.MAX_DEFINITION_LEN
    assert len(payload["column_roster"]) == MAX_COLUMNS_PER_TABLE - 1

    chunks = chunk_items([BatchItem(f"h{i}", payload) for i in range(60)],
                         max_items=enrich_config.max_items("concept"),
                         max_input_tokens=enrich_config.max_input_tokens("concept"))
    assert max(len(c) for c in chunks) == enrich_config.max_items("concept") == 20


def test_pass_b_prose_stays_admissible_at_the_egress_gate_after_the_raise() -> None:
    """The invariant the previous raise established and this one must not break.

    `table_synth._MAX_PROFILE_PROSE` bounds Pass B's OWN output, which can be re-threaded into a
    later item as `business_context`/`table_description`, where it meets `_MAX_LEN_DEFAULT` — NOT
    `MAX_DEFINITION_LEN`. It was deliberately not pinned to the definition cap, so the 32_000 raise
    must leave it admissible; a future "make these consistent" edit is what this catches.
    """
    from featuregen.overlay.upload.table_synth import _MAX_PROFILE_PROSE

    assert _MAX_PROFILE_PROSE < llm._MAX_LEN_DEFAULT < llm.MAX_DEFINITION_LEN
    at_bound = "x" * _MAX_PROFILE_PROSE
    assert llm._item_egress_ok({"table": "t", "business_context": at_bound,
                                "table_description": at_bound}) is True


def test_the_concept_stage_still_packs_multiple_items_per_chunk_at_the_new_caps() -> None:
    """The cap raise must degrade packing PROPORTIONALLY, not shatter it.

    `NEIGHBOUR_LIMIT` 64 -> 512 and `ADAPTER_LIST_LIMIT` 40 -> 256 multiply EVERY concept item, and
    `chunk_items` packs by both item count and estimated tokens. One item per chunk would make the
    concept stage's call count equal its COLUMN count — the shape that makes a call ceiling bind and
    turns "more expensive" into "stopped enriching columns".

    Run against the real `chunk_items` and the real budgets, at a saturated resolved roster. This is
    the offline stand-in for the live before/after call count, which is DEFERRED by human decision
    (docs/DEFERRED-WORK.md) — it bounds the failure mode without observing the real number.
    """
    payload = _classifier_item_at_the_new_caps(siblings=sc.ADAPTER_LIST_LIMIT + 20)
    assert len(payload["column_roster"]) == sc.ADAPTER_LIST_LIMIT   # genuinely saturated

    items = [BatchItem(f"h{i}", payload) for i in range(60)]
    chunks = chunk_items(items, max_items=enrich_config.max_items("concept"),
                         max_input_tokens=enrich_config.max_input_tokens("concept"))
    assert len(chunks) < len(items), "every item formed its own chunk — packing collapsed"
    assert max(len(c) for c in chunks) > 1
    # Stronger than "did not collapse": packing is still bound by the ITEM COUNT, not by tokens, so
    # the call count is what it was before the raise. If this ever drops below max_items the stage
    # has started costing more calls and the ceiling arithmetic in the deployment must be revisited.
    assert max(len(c) for c in chunks) == enrich_config.max_items("concept")


def test_the_widest_ingestible_table_also_packs_at_the_item_bound() -> None:
    """The production case, as opposed to the saturated one above: `MAX_COLUMNS_PER_TABLE` caps
    ingestion at 200 columns, so a real concept item's roster tops out at 199 siblings and can never
    reach `ADAPTER_LIST_LIMIT` at all."""
    from featuregen.overlay.upload.canonical import MAX_COLUMNS_PER_TABLE

    payload = _classifier_item_at_the_new_caps(siblings=MAX_COLUMNS_PER_TABLE - 1)
    assert len(payload["column_roster"]) == MAX_COLUMNS_PER_TABLE - 1    # never sliced
    chunks = chunk_items([BatchItem(f"h{i}", payload) for i in range(60)],
                         max_items=enrich_config.max_items("concept"),
                         max_input_tokens=enrich_config.max_input_tokens("concept"))
    assert max(len(c) for c in chunks) == enrich_config.max_items("concept")


# ── the ftr_adapter source-attribute caps (Task 4b review, Important #1) ─────────────────────────


def test_the_producer_caps_no_longer_bind_before_the_egress_caps_they_feed() -> None:
    """`ftr_adapter` is the PRODUCER of `source_attributes`, so its two caps bind BEFORE every
    downstream one. While they sat at 40 / 240 chars, raising `enrich_llm._MAX_SOURCE_ATTRIBUTES` to
    256 and `enrich._MAX_META_LEN` to 1000 achieved literally nothing on this field and a 240+ char
    governance value was still cut on the way to the model.

    Both are now set EXACTLY to the gates they feed, and neither may exceed them: a longer list is
    egress-REJECTED on count (`_item_shape_ok`) and a longer value has the column's whole item
    EXCLUDED + audited on length (`_item_len_ok`, via `_max_len_for("source_attributes")`). Going
    past either would trade a silent trim for a dropped column, which is strictly worse.
    """
    from featuregen.overlay.upload import ftr_adapter

    assert ftr_adapter._MAX_SOURCE_ATTRIBUTES == llm._MAX_SOURCE_ATTRIBUTES
    assert ftr_adapter._MAX_SOURCE_ATTRIBUTE_LEN == llm._MAX_LEN_DEFAULT == enrich._MAX_META_LEN
    # …and a value at the producer's new bound really does survive the whole path to egress.
    at_bound = "governance_header: " + "v" * (ftr_adapter._MAX_SOURCE_ATTRIBUTE_LEN - 19)
    assert len(at_bound) == ftr_adapter._MAX_SOURCE_ATTRIBUTE_LEN
    meta = enrich._concept_metadata(_row("anchor"),
                                    _record("anchor", source_attributes=(at_bound,)))
    assert meta["source_attributes"] == [at_bound], "truncated between the reader and the request"
    assert llm._item_egress_ok(meta) is True


def test_a_realistic_source_attribute_fill_still_packs_a_full_chunk() -> None:
    """The raise costs tokens, and the budgets must hold the REALISTIC fill: 17 entries (the real
    FTR export's entire header count) each at the new 1000-char maximum. Every per-column stage must
    still pack its full `max_items`, or the raise has bought richer context with more provider calls.
    """
    from featuregen.overlay.upload.canonical import MAX_COLUMNS_PER_TABLE

    summary_item = enrich.summary_payload(
        _row("anchor"), _record_at_the_definition_cap("anchor"),
        {"concept": "monetary_stock", "party_role": "counterparty", "ai_synonyms": ["a", "b"]},
        _bundle("anchor", ["anchor"]))
    for short, payload in (("definition", _drafting_item_at_the_definition_cap()),
                           ("summary", summary_item),
                           ("concept", _classifier_item_at_the_new_caps(
                               siblings=MAX_COLUMNS_PER_TABLE - 1))):
        assert len(payload["source_attributes"]) == len(_REALISTIC_SOURCE_ATTRS)
        chunks = chunk_items([BatchItem(f"h{i}", payload) for i in range(40)],
                             max_items=enrich_config.max_items(short),
                             max_input_tokens=enrich_config.max_input_tokens(short))
        assert max(len(c) for c in chunks) == enrich_config.max_items(short), short


def test_a_DEGENERATE_source_attribute_fill_degrades_proportionally_and_never_truncates() -> None:
    """The honest cost of raising the COUNT cap 40 -> 256, recorded rather than discovered later.

    No observed file comes near this — FTR has 17 headers in total — but a mapping export with 256
    unmapped governance headers each at 1000 chars is now admissible, and it costs ~72_000 estimated
    tokens per item. Packing falls to 2 of 8. That is the documented, ACCEPTABLE failure mode:
    `chunk_items` never drops an item for size (an over-budget item forms its own chunk), so the
    stage degrades PROPORTIONALLY into more calls, and the deployed per-stage ceiling still clears
    the result. What must never happen is a lost column.
    """
    rec = _record("anchor", definition=_DEF_AT_CAP, source_attributes=tuple(
        (f"governance_header_{i}: " + "v" * 1000)[:1000] for i in range(256)))
    payload = enrich._drafting_payload(
        _bundle("anchor", ["anchor"]),
        {"table": _TABLE, "column": "anchor", "type": "varchar(11)", "concept": "monetary_stock"},
        row=_row("anchor"), rec=rec)
    chunks = chunk_items([BatchItem(f"h{i}", payload) for i in range(40)],
                         max_items=enrich_config.max_items("definition"),
                         max_input_tokens=enrich_config.max_input_tokens("definition"))
    packed = max(len(c) for c in chunks)
    assert 1 < packed < enrich_config.max_items("definition"), (
        f"degenerate fill packed {packed}/{enrich_config.max_items('definition')}")
    # Nothing is dropped: every item still rides some chunk.
    assert sum(len(c) for c in chunks) == 40
    # And the degraded chunk count still clears the deployed per-stage ceiling for a 237-column
    # catalog — the bound that turns "more calls" into "stopped enriching columns".
    worst_case_calls = -(-237 // packed) * 2 + 8
    assert worst_case_calls < 512, worst_case_calls


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
