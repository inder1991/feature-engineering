"""Task 7b — the CURATED HUMAN context reaches the model.

Three values a human typed, or a glossary curated, that the platform stored and then never showed
to any model. Each is tested at the seam it was missing from, and each asserts the VALUE arrives,
never that a key exists — a key that is always null carries nothing while passing a key-set check.

* **The catalog narrative** (`catalog_profile_revision`, migration 1047). The upload form promises
  its description is *"used by search and by the AI when it interprets tables"*. It reached no LLM
  payload at all: the bundle carried `catalog_profile_revision_id` — the ID, not the prose — and the
  ingest path already read that id, but only as a BOOLEAN ("does a narrative exist") for the
  `authority_claim_without_source_context` rule. It now rides the two TABLE-grain payloads: Pass B's
  per-table synthesis item (where grain / table_role / primary_entity are decided) and the
  feature-generation table-context block.
* **`business_term`** — the glossary's curated business NAME for a column. Sent by
  `for_concept_enrichment` and `for_summary`; absent from the feature seam and from the objective's
  own token intersection.
* **`related_terms`** — the glossary's curated related vocabulary. Parsed, persisted,
  egress-classified and sent to Pass A; absent from the feature seam, while Task 6d pays a provider
  to expand the objective with the same kind of vocabulary.

THE LANDMINE EVERY NEW KEY WALKS INTO. `enrich_llm.sanitize_feature_context` and
`_item_shape_ok`/`_redact_free_text_meta` both fail CLOSED: an unclassified key refuses the WHOLE
payload (or excludes the whole item), not the field. So every key added here is pinned by a
removal-from-its-own-list refusal test — remove the classification, the payload dies.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_feature_context_coverage import (
    fully_populated_bundle,
    sentinel,
)

from featuregen.overlay.upload import enrich_llm
from featuregen.overlay.upload import feature_assist as fa
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_profiles import (
    CATALOG_NARRATIVE_AUTHORITY_KEY,
    CATALOG_NARRATIVE_KEYS,
    build_catalog_profile_revision,
    catalog_narrative_block,
)
from featuregen.overlay.upload.enrich_llm import sanitize_feature_context
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.profile_store import (
    current_catalog_narrative_block,
    record_catalog_profile_revision,
    upsert_current_catalog_profile,
)
from featuregen.overlay.upload.semantic_context import for_feature_generation

ACTOR = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
_SRC = "cibpay"

_DISPLAY = "CIB Payments Catalog"
_DESCRIPTION = ("Funds-transfer records for the corporate and investment bank: one row per "
                "outbound payment instruction.")
_CONTEXT = ("Compliance owns this catalog; it is the book of record for outbound SWIFT and RTGS "
            "payments and is reconciled nightly against the settlement ledger.")
_DOMAINS = ("payments", "financial crime")


# ── fixtures ────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def profiles_on(monkeypatch):
    """The Release-A gate. A narrative CANNOT be authored with it off (`uploads.py` ignores the
    part; the PUT route 404s behind `require_dataset_profiles`), so the read side honours the same
    gate — the flag's whole contract is that every payload is byte-identical without it."""
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    return monkeypatch


def _author_narrative(db, source: str = _SRC) -> None:
    revision = build_catalog_profile_revision(
        catalog_source=source, display_name=_DISPLAY, description=_DESCRIPTION,
        business_context=_CONTEXT, business_domains=_DOMAINS, producer_ref="user:owner")
    record_catalog_profile_revision(db, revision)
    upsert_current_catalog_profile(db, catalog_source=source, revision_id=revision.revision_id)


@pytest.fixture
def catalog(db):
    rows = [
        CanonicalRow(_SRC, "payments", "pmt_id", "text", is_grain=True),
        CanonicalRow(_SRC, "payments", "cpty_expsr_amt", "numeric"),
    ]
    assert ingest_upload(db, _SRC, rows, actor=ACTOR).status == "ingested"
    return db


# ── gap 1: the catalog narrative ────────────────────────────────────────────────────────────────

def test_the_narrative_block_carries_the_prose_and_labels_its_authority():
    """The one builder both seams read, so the two can never render the same narrative differently.

    Authority is EXPLICIT and it is `human/proposed`: this is prose a person typed, it informs the
    model and it overrides no evidence. `put_catalog_profile`'s docstring states that for the write
    side ("it never defaults any dataset's role, authority or temporal model"); the block preserves
    it on the read side by publishing the D2 axes the revision actually stored, never a stronger
    label invented here."""
    revision = build_catalog_profile_revision(
        catalog_source=_SRC, display_name=_DISPLAY, description=_DESCRIPTION,
        business_context=_CONTEXT, business_domains=_DOMAINS, producer_ref="user:owner")
    block = catalog_narrative_block(revision)
    assert block["catalog_display_name"] == _DISPLAY
    assert block["catalog_description"] == _DESCRIPTION
    assert block["catalog_business_context"] == _CONTEXT
    assert block["catalog_business_domains"] == list(_DOMAINS)
    assert block[CATALOG_NARRATIVE_AUTHORITY_KEY] == "human/proposed"
    assert set(block) <= set(CATALOG_NARRATIVE_KEYS)


def test_an_unauthored_field_is_absent_from_the_block_never_blank():
    """A narrative with only a description carries only a description. An empty string in a prompt
    is a fabricated blank, and on the feature seam it is also an egress cost for no content."""
    revision = build_catalog_profile_revision(
        catalog_source=_SRC, description=_DESCRIPTION, producer_ref="user:owner")
    block = catalog_narrative_block(revision)
    assert block["catalog_description"] == _DESCRIPTION
    assert "catalog_display_name" not in block
    assert "catalog_business_context" not in block
    assert "catalog_business_domains" not in block


def test_the_narrative_reaches_the_feature_generation_table_block(catalog, profiles_on):
    """Gap 1 at the feature seam. The generator saw a column menu with no idea what the catalog IS."""
    _author_narrative(catalog)
    cols = fa._candidate_columns(catalog, _SRC, roles=())
    blocks = fa._table_context(cols, narratives={_SRC: current_catalog_narrative_block(catalog, _SRC)})
    block = blocks[0]
    assert block["catalog_description"] == _DESCRIPTION
    assert block["catalog_business_context"] == _CONTEXT
    assert block["catalog_display_name"] == _DISPLAY
    assert block["catalog_business_domains"] == list(_DOMAINS)
    assert block[CATALOG_NARRATIVE_AUTHORITY_KEY] == "human/proposed"


def test_the_real_assembly_resolves_the_narrative_and_does_not_merely_accept_one(
        catalog, profiles_on, monkeypatch):
    """WHAT CONSUMES THIS. `_table_context` taking a narrative proves nothing about production:
    `select_relevant_context` is the ONE caller that builds the assembled batch, and a version of
    this change that added the parameter without threading it would pass every test above while the
    narrative silently never populated. That is the exact defect class this plan exists for."""
    monkeypatch.setenv(fa.FEATURE_CONTEXT_FLAG, "1")
    _author_narrative(catalog)
    cols = fa._candidate_columns(catalog, _SRC, roles=())
    _columns, blocks, _dropped = fa.select_relevant_context(
        catalog, cols, objective="payment exposure", entity=None)
    assert blocks and blocks[0]["catalog_description"] == _DESCRIPTION


def test_a_catalog_with_no_narrative_degrades_to_todays_block_exactly(catalog, profiles_on):
    """`catalog_profile_absent` is a real missing-context code; keep it honest. No narrative means
    NO key — not an empty one, and not a key whose value says "unknown"."""
    cols = fa._candidate_columns(catalog, _SRC, roles=())
    assert current_catalog_narrative_block(catalog, _SRC) == {}
    block = fa._table_context(cols, narratives={_SRC: current_catalog_narrative_block(catalog, _SRC)})[0]
    assert not set(block) & set(CATALOG_NARRATIVE_KEYS)
    assert block == fa._table_context(cols)[0]


def test_the_narrative_is_gated_by_the_release_a_flag(catalog):
    """Flag OFF ⟹ byte-identical to the pre-profile shape, which is the flag's whole contract. A
    narrative cannot even be AUTHORED with it off, so reading one would surface state no supported
    write path could have created."""
    _author_narrative(catalog)
    assert current_catalog_narrative_block(catalog, _SRC) == {}


def test_the_narrative_survives_the_feature_egress_adapter(catalog, profiles_on):
    """The landmine, live: one unclassified key returns `(None, ...)` and the caller blocks the
    WHOLE feature-generation dispatch. Assert the prose comes out the other side, not merely that
    the call did not crash."""
    _author_narrative(catalog)
    cols = fa._candidate_columns(catalog, _SRC, roles=())
    blocks = fa._table_context(cols, narratives={_SRC: current_catalog_narrative_block(catalog, _SRC)})
    safe, _spans, _audits, _version = sanitize_feature_context(
        {"columns": [{"object_ref": "x", "table": "payments", "column": "pmt_id"}],
         "table_context": blocks})
    assert safe is not None, "the narrative refused the whole payload"
    assert safe["table_context"][0]["catalog_description"] == _DESCRIPTION
    assert safe["table_context"][0]["catalog_business_domains"] == list(_DOMAINS)


@pytest.mark.parametrize("key,listname", [
    ("catalog_description", "_TABLE_CONTEXT_DEFINITION_KEYS"),
    ("catalog_business_context", "_TABLE_CONTEXT_DEFINITION_KEYS"),
    ("catalog_display_name", "_TABLE_CONTEXT_PROSE_KEYS"),
    ("catalog_business_domains", "_TABLE_CONTEXT_PROSE_LIST_KEYS"),
    (CATALOG_NARRATIVE_AUTHORITY_KEY, "_TABLE_CONTEXT_IDENTITY_KEYS"),
])
def test_removing_a_narrative_keys_classification_refuses_the_whole_payload(
        monkeypatch, key, listname):
    """Each key pinned against the list it is ACTUALLY classified in — remove it there and the
    terminal `else: return None` fires. A key classified in some OTHER list would keep this green
    while its own list did nothing."""
    block = {"table": "payments", "catalog_display_name": _DISPLAY,
             "catalog_description": _DESCRIPTION, "catalog_business_context": _CONTEXT,
             "catalog_business_domains": list(_DOMAINS),
             CATALOG_NARRATIVE_AUTHORITY_KEY: "human/proposed"}
    payload = {"columns": [{"object_ref": "x"}], "table_context": [block]}
    assert sanitize_feature_context(payload)[0] is not None
    current = getattr(enrich_llm, listname)
    monkeypatch.setattr(enrich_llm, listname, frozenset(current) - {key})
    assert sanitize_feature_context(payload)[0] is None, f"{key} is not graded by {listname}"


def test_uploader_narrative_prose_is_pii_redacted_and_never_identity_graded():
    """Grading uploader text as IDENTITY was a real exposure two tasks ago (`domain`, fixed in
    `c62ab49d`): identity grade applies a type-and-length check ONLY — no redaction, no audit, and a
    detectable span then trips `assert_llm_safe` and kills the whole call instead of being scrubbed.
    Every uploader-typed narrative field routes through a redactor here, INCLUDING the list."""
    block = {"table": "payments",
             "catalog_display_name": "Owned by jane.roe@bank.example",
             "catalog_description": f"{_DESCRIPTION} Contact jane.roe@bank.example.",
             "catalog_business_domains": ["payments", "escalate to jane.roe@bank.example"]}
    safe, spans, _audits, version = sanitize_feature_context(
        {"columns": [{"object_ref": "x"}], "table_context": [block]})
    assert safe is not None
    out = safe["table_context"][0]
    assert "jane.roe@bank.example" not in out["catalog_display_name"]
    assert "jane.roe@bank.example" not in out["catalog_description"]
    assert not any("jane.roe@bank.example" in d for d in out["catalog_business_domains"])
    assert version is not None
    assert {s["key"] for s in spans} >= {
        "table_context[0].catalog_display_name", "table_context[0].catalog_business_domains[1]"}


def test_a_maximum_length_authored_narrative_is_admitted_not_refused():
    """The bound that decides whether a legitimately-authored narrative reaches the model at all.
    `catalog_profiles` accepts 4000 characters of description; the definition grade applies no
    length ceiling, so the longest thing a human can author cannot be silently refused whole."""
    from featuregen.overlay.upload.catalog_profiles import MAX_DESCRIPTION

    longest = "a" * MAX_DESCRIPTION
    safe, _spans, _audits, _v = sanitize_feature_context(
        {"columns": [{"object_ref": "x"}],
         "table_context": [{"table": "t", "catalog_description": longest}]})
    assert safe is not None and safe["table_context"][0]["catalog_description"] == longest


# ── gap 1: an ordinary business phrase must not become a catalog-wide kill switch ───────────────
#
# `sanitize_definition` fails closed on five literal phrases (sanitize.py:55-62). The narrative
# rides EVERY table block and EVERY Pass-B item, so under the ordinary blanked-means-block rule one
# sentence in the upload form would refuse every feature-generation call for the catalog AND exclude
# every table from Pass B. `catalog_profiles.parse_narrative_payload` validates length and type
# only — nothing warns the business user at author time.

#: A realistic sentence, not a probe. A data owner describing a catalog writes this.
_MARKER_SENTENCE = ("Funds-transfer records for the corporate and investment bank. The description "
                    "column gives example values for each payment type.")


def test_an_ordinary_business_phrase_does_not_refuse_the_whole_feature_payload():
    """The blast-radius fix. The marker-bearing narrative field is DROPPED — it never egresses —
    while the rest of the payload, including the other narrative fields, still does."""
    from featuregen.overlay.upload.sanitize import sanitize_definition

    assert sanitize_definition(_MARKER_SENTENCE).reason == "unhandled_marker", (
        "the sentence no longer trips the data-marker gate — this test asserts nothing")
    block = {"table": "payments", "catalog_description": _MARKER_SENTENCE,
             "catalog_business_context": _CONTEXT, "catalog_display_name": _DISPLAY}
    safe, _spans, audits, _v = sanitize_feature_context(
        {"columns": [{"object_ref": "x"}], "table_context": [block]})
    assert safe is not None, "one ordinary sentence refused the whole catalog's feature generation"
    out = safe["table_context"][0]
    assert "catalog_description" not in out, "the marker-bearing value egressed"
    assert out["catalog_business_context"] == _CONTEXT, "the drop took the rest of the block with it"
    assert out["catalog_display_name"] == _DISPLAY
    # NEVER SILENT: the sanitizer's own audit still says what was removed and why.
    assert any(a["state"] == "suspected_unhandled"
               and a["path"] == "table_context[0].catalog_description" for a in audits)


def test_the_same_phrase_in_a_table_definition_still_fails_closed():
    """The drop is scoped to ADVISORY context and nothing else. A `table_definition` the sanitizer
    blanks still refuses: that field IS the subject the model was asked to reason about, and
    egressing the block without it produces a confident answer to a question nobody could answer."""
    payload = {"columns": [{"object_ref": "x"}],
               "table_context": [{"table": "payments", "table_definition": _MARKER_SENTENCE}]}
    assert sanitize_feature_context(payload)[0] is None


def test_an_ordinary_business_phrase_does_not_exclude_every_pass_b_item():
    """The same fix at the enrichment seam. Excluding the item costs that table its ENTIRE
    synthesis — grain, table_role, primary_entity, event_or_snapshot — audited only as an egress
    block, and the narrative is on every item, so it would have cost the whole catalog."""
    meta, _spans, audits, _v = enrich_llm._redact_free_text_meta(
        {"table": "payments", "catalog_description": _MARKER_SENTENCE,
         "catalog_business_context": _CONTEXT})
    assert meta is not None, "one ordinary sentence excluded every table from Pass B"
    assert "catalog_description" not in meta
    assert meta["catalog_business_context"] == _CONTEXT
    assert meta["table"] == "payments"
    assert any(a["state"] == "suspected_unhandled" for a in audits)


def test_a_column_definition_that_blanks_still_excludes_its_own_item():
    """The other side of the same boundary, at the Pass-B seam."""
    assert enrich_llm._redact_free_text_meta(
        {"table": "t", "business_definition": _MARKER_SENTENCE})[0] is None


def test_one_bad_domain_drops_that_term_not_the_list_and_not_the_payload():
    """Per-item, like every other prose list on this seam. A redactor failure on one authored
    domain must not take the other 31 with it, nor the catalog."""
    long_domain = "z" * (enrich_llm._FEATURE_STRUCTURAL_MAX_LEN + 1)
    block = {"table": "t", "catalog_business_domains": ["payments", long_domain, "treasury"]}
    safe, _spans, _audits, _v = sanitize_feature_context(
        {"columns": [{"object_ref": "x"}], "table_context": [block]})
    assert safe is not None
    assert safe["table_context"][0]["catalog_business_domains"] == ["payments", "treasury"]


def test_a_narrative_scrubbed_empty_leaves_no_key_behind():
    """A fabricated `[]` or `""` is not context — it is a value the model can reason about. Absence
    is the honest signal, and it is the same signal an unauthored field gives."""
    long_domain = "z" * (enrich_llm._FEATURE_STRUCTURAL_MAX_LEN + 1)
    safe, _spans, _audits, _v = sanitize_feature_context(
        {"columns": [{"object_ref": "x"}],
         "table_context": [{"table": "t", "catalog_business_domains": [long_domain]}]})
    assert safe is not None
    assert "catalog_business_domains" not in safe["table_context"][0]


def test_a_dropped_value_is_never_handed_back_by_the_structural_passthrough():
    """The hole under the drop mechanism. Both seams short-circuit to the ORIGINAL payload when no
    free text was scanned (`version is None`) — and a value the scrubber refuses WITHOUT running a
    scrubber body (a non-`str` under a free-text key) leaves `version` None while the key has
    already been dropped. The passthrough then returns the untouched original, egressing the exact
    value the warning line said was removed.

    Not reachable from `catalog_narrative_block`, which emits validated `str`/`list[str]` — but
    "never silent" is the contract this whole mechanism rests on, and a log line that asserts
    something false is worse than no log line."""
    safe, _spans, _audits, _v = sanitize_feature_context(
        {"columns": [{"object_ref": "x"}],
         "table_context": [{"table": "t", "catalog_display_name": 12345}]})
    assert safe is not None
    assert "catalog_display_name" not in safe["table_context"][0], "the drop was undone"

    meta, _s, _a, _v2 = enrich_llm._redact_free_text_meta(
        {"table": "t", "catalog_display_name": 12345})
    assert meta is not None
    assert "catalog_display_name" not in meta, "the drop was undone"
    assert meta["table"] == "t"


def test_the_pass_b_item_carries_the_narrative_past_its_own_egress_gate():
    """Pass B decides grain, table_role, primary_entity and event_or_snapshot from columns and
    profiles alone — and grain is the most expensive thing in this pipeline to get wrong. The
    narrative answers three of those four outright, so the highest-leverage place to put it is the
    per-TABLE synthesis item.

    `_item_egress_ok` is the gate that item must clear. An unallowlisted key fails the SHAPE half;
    a 4000-character description over the default 1000-char cap fails the LENGTH half — either way
    the whole item is EXCLUDED and that table gets no synthesis at all."""
    item = {"table": "payments", "column_profiles": [{"column": "pmt_id", "type": "text"}],
            "catalog_display_name": _DISPLAY, "catalog_description": "a" * 4000,
            "catalog_business_context": _CONTEXT,
            "catalog_business_domains": list(_DOMAINS),
            CATALOG_NARRATIVE_AUTHORITY_KEY: "human/proposed"}
    assert enrich_llm._item_egress_ok(item), "the narrative excludes its own Pass-B item"


@pytest.mark.parametrize("key,kind", [
    ("catalog_description", "definition"),
    ("catalog_business_context", "definition"),
    ("catalog_display_name", "prose"),
    ("catalog_business_domains", "list_of_prose"),
])
def test_every_free_text_narrative_key_declares_its_enrichment_egress_kind(key, kind):
    """`_meta_field_kind` raises on a free-text key with no declared kind ([F6] fail closed). The
    two 4000-character narrative fields take the DEFINITION grade — sample-clause strip + the
    fail-closed data-marker scan on top of PII redaction — because they are the same class of value
    as `table_description`: curated narrative prose authored against real data."""
    assert enrich_llm._meta_field_kind(key) == kind


def test_pass_b_narrative_keys_are_allowlisted_on_the_item_seam():
    assert set(CATALOG_NARRATIVE_KEYS) <= enrich_llm._ITEM_META_ALLOWED


def test_removing_a_narrative_key_from_the_item_allowlist_excludes_the_whole_item(monkeypatch):
    item = {"table": "payments", "catalog_display_name": _DISPLAY}
    assert enrich_llm._item_egress_ok(item)
    monkeypatch.setattr(enrich_llm, "_ITEM_META_ALLOWED",
                        enrich_llm._ITEM_META_ALLOWED - {"catalog_display_name"})
    assert not enrich_llm._item_egress_ok(item)


def test_the_narrative_is_joined_onto_every_pass_b_synthesis_item(monkeypatch):
    """WHAT CONSUMES THIS, for the enrichment half. Classifying the keys proves only that they COULD
    egress; this pins that `synthesize_tables` actually puts them on the items it dispatches — the
    difference between the field being wired and merely being permitted.

    Captured at `_run_synthesis`, the ONE funnel both the narrow fast path and the wide two-phase
    path pass through."""
    from featuregen.overlay.upload import table_synth
    from featuregen.overlay.upload.enrich_batch import BatchItem

    seen: list[dict] = []
    monkeypatch.setattr(table_synth, "_run_synthesis",
                        lambda *a, **kw: seen.extend(i.metadata for i in a[2]) or {})
    monkeypatch.setattr(table_synth, "_profile_critic", lambda *a, **kw: None)
    monkeypatch.setattr(table_synth, "_record_synthesis_results", lambda *a, **kw: None)
    items = [BatchItem(ref="payments", metadata={"table": "payments", "column_profiles": []})]
    table_synth.synthesize_tables(None, None, items, columns_by_table={}, actor=None,
                                  catalog_narrative={"catalog_description": _DESCRIPTION,
                                                     "catalog_display_name": _DISPLAY})
    assert seen and seen[0]["catalog_description"] == _DESCRIPTION
    assert seen[0]["table"] == "payments", "the narrative displaced the item's own identity"


def test_a_narrative_edit_re_keys_the_pass_b_replay_identity(monkeypatch):
    """`context_revision` is what lets a byte-identical re-upload replay every stored synthesis for
    free. The narrative is now part of the question Pass B is asked, so an EDIT must re-ask it —
    joining the prose on AFTER the identity was computed would replay yesterday's answer to a
    different question, which is precisely the trap `CONCEPT_CRITIC_VERSION` documents."""
    from featuregen.overlay.upload import table_synth
    from featuregen.overlay.upload.enrich_batch import BatchItem

    revisions: list[str] = []
    monkeypatch.setattr(table_synth, "_run_synthesis", lambda *a, **kw: {})
    monkeypatch.setattr(table_synth, "_record_synthesis_results", lambda *a, **kw: None)
    monkeypatch.setattr(table_synth, "_profile_critic",
                        lambda *a, **kw: revisions.append(kw["context_revision"]))

    def _run(narrative):
        table_synth.synthesize_tables(
            None, None, [BatchItem(ref="payments", metadata={"table": "payments"})],
            columns_by_table={}, actor=None, catalog_narrative=narrative)

    _run({"catalog_description": "Outbound payment instructions."})
    _run({"catalog_description": "Inbound collections."})
    _run(None)
    assert len(set(revisions)) == 3, "the narrative is outside the Pass-B replay identity"


def test_a_pass_b_suggestion_can_rest_on_the_narrative_and_say_so():
    """The blocker: `_cited_refs` filtered `evidence_refs` to columns + `table_definition`, and
    `_accept_profile_fields` drops a suggestion with empty refs as `no_evidence_ref`. So
    `table_description` / `business_context` / `authority_role` / `temporal_storage_model` were
    MECHANICALLY incapable of resting on the narrative — an honest citation was filtered out and the
    field discarded, and the only route to acceptance was attaching a column citation the suggestion
    does not actually rest on. Delivering prose the model may not cite would have taught it to cite
    dishonestly."""
    from featuregen.overlay.upload.table_synth import _cited_refs, make_ref_accept

    carried = frozenset({"catalog_business_context"})
    synthesis = {
        "business_context": "The book of record for outbound SWIFT and RTGS payments.",
        "evidence_refs": [{"field": "business_context", "refs": ["catalog_business_context"]}]}
    assert _cited_refs(synthesis, "business_context", cols={"pmt_id"},
                       narrative_refs=carried) == ["catalog_business_context"]

    dispositions: list[dict] = []
    accept = make_ref_accept({"payments": {"pmt_id"}}, dispositions=dispositions,
                             inventory_by_table={"payments": []},
                             narrative_refs_by_table={"payments": carried})
    accept(json.dumps(synthesis), "payments")     # (raw, ref) — raw first
    got = {d["field"]: (d["status"], d["reason"]) for d in dispositions}
    assert got["business_context"] == ("accepted", None), got


def test_a_narrative_ref_cannot_be_cited_when_no_item_carries_one():
    """THE FORGERY SURFACE, and it is the DEFAULT deployment. `FEATUREGEN_DATASET_PROFILES` is off
    by default, so no Pass-B item carries any narrative key — and an unconditional allowlist would
    let a model clear the `no_evidence_ref` gate by naming prose that does not exist. That is the
    "cite anything to get accepted" behaviour widening these refs was supposed to make unnecessary,
    and it would have widened the forgeable set from one ref to six."""
    from featuregen.overlay.upload.table_synth import _cited_refs, make_ref_accept

    synthesis = {
        "business_context": "Invented.",
        "evidence_refs": [{"field": "business_context", "refs": ["catalog_description"]}]}
    assert _cited_refs(synthesis, "business_context", cols={"pmt_id"}) == []

    dispositions: list[dict] = []
    accept = make_ref_accept({"payments": {"pmt_id"}}, dispositions=dispositions,
                             inventory_by_table={"payments": []})
    accept(json.dumps(synthesis), "payments")
    got = {d["field"]: (d["status"], d["reason"]) for d in dispositions}
    assert got["business_context"] == ("dropped_invalid", "no_evidence_ref"), got


def test_only_the_narrative_keys_the_item_actually_carries_are_citable():
    """Per ITEM, not per deployment. A catalog that authored a description but no business context
    makes the first citable and the second not — the allowlist is the item's own contents."""
    from featuregen.overlay.upload.table_synth import _cited_refs, narrative_refs_of

    carried = narrative_refs_of({"table": "payments", "column_profiles": [],
                                 "catalog_description": _DESCRIPTION})
    assert carried == frozenset({"catalog_description"})
    synthesis = {"evidence_refs": [{"field": "business_context",
                                    "refs": ["catalog_description", "catalog_business_context"]}]}
    assert _cited_refs(synthesis, "business_context", cols=set(),
                       narrative_refs=carried) == ["catalog_description"]


def test_the_synthesis_run_derives_the_citable_set_from_the_items_it_dispatched():
    """WHAT CONSUMES THIS. The gate is only real if `_run_synthesis` builds the map from the SAME
    items it sends — a version that took the parameter but never threaded it would leave the
    forgery surface open while every test above passed."""
    from featuregen.overlay.upload import table_synth
    from featuregen.overlay.upload.enrich_batch import BatchItem

    seen: dict = {}
    monkeypatch_target = table_synth.make_ref_accept

    def _capture(*args, **kwargs):
        seen.update(kwargs.get("narrative_refs_by_table") or {})
        return monkeypatch_target(*args, **kwargs)

    table_synth.make_ref_accept = _capture
    try:
        table_synth._run_synthesis(
            None, None,
            [BatchItem(ref="payments", metadata={"table": "payments",
                                                 "catalog_description": _DESCRIPTION}),
             BatchItem(ref="other", metadata={"table": "other"})],
            columns_by_table={}, actor=None, instruction="x")
    except Exception:  # noqa: BLE001 — the dispatch fails on a None conn; the capture already ran
        pass
    table_synth.make_ref_accept = monkeypatch_target
    assert seen == {"payments": frozenset({"catalog_description"}), "other": frozenset()}


def test_a_hallucinated_narrative_key_is_still_dropped():
    """Widening the allowlist is not un-gating it. A ref that is neither a real column nor a real
    context key is still discarded, so "names existing evidence refs" stays enforced."""
    from featuregen.overlay.upload.table_synth import _cited_refs

    synthesis = {"evidence_refs": [
        {"field": "business_context", "refs": ["catalog_mission_statement", "catalog_description"]}]}
    assert _cited_refs(synthesis, "business_context", cols=set(),
                       narrative_refs=frozenset({"catalog_description"})) == ["catalog_description"]


def test_the_pass_b_prompt_says_what_the_narrative_is_and_that_it_is_citable():
    """Both halves of prompt v5, pinned together because either alone is a defect: widening
    `_cited_refs` without telling the model leaves the capability unreachable, and telling the model
    without widening `_cited_refs` invites a citation the code silently discards."""
    from featuregen.overlay.upload import table_synth

    assert "catalog_description" in table_synth._TYPE_FIELDS_NOTE
    assert "WHOLE CATALOG" in table_synth._TYPE_FIELDS_NOTE
    assert "catalog_business_context" in table_synth._PROFILE_NOTE
    # The version MUST move with the text — a changed question replayed under the old identity is
    # the trap this contract's own comments document. A LITERAL on purpose: reading the constant
    # would make this tautological, and the point is that a human re-versions deliberately.
    # 5 -> 6 (2026-08-09): `_PROFILE_NOTE` now shows a correct `evidence_refs` entry and forbids a
    # placeholder, after the first live run lost its `business_context` and `table_description`
    # to `no_evidence_ref`.
    assert table_synth._SYNTH_PROMPT_VERSION == 6
    # The id and the version are ONE contract generation; they must never disagree.
    assert table_synth._SYNTH_PROMPT_ID.endswith(f"v{table_synth._SYNTH_PROMPT_VERSION}")
    assert table_synth._SUMMARY_PROMPT_ID.endswith(f"v{table_synth._SYNTH_PROMPT_VERSION}")


def test_the_long_narrative_fields_have_their_own_egress_length_cap():
    """Left to inherit `_MAX_LEN_DEFAULT` (1000) a legitimately-authored 4000-character description
    would EXCLUDE its item — the table would lose its whole synthesis, silently and audited as an
    egress block. The cap sits ABOVE the authored bound with room for redaction, which LENGTHENS
    (a 6-char email becomes a 16-char placeholder)."""
    from featuregen.overlay.upload.catalog_profiles import MAX_BUSINESS_CONTEXT, MAX_DESCRIPTION

    assert enrich_llm._max_len_for("catalog_description") > MAX_DESCRIPTION
    assert enrich_llm._max_len_for("catalog_business_context") > MAX_BUSINESS_CONTEXT


# ── gaps 2 and 3: business_term and related_terms at the feature seam ────────────────────────────

def test_business_term_and_related_terms_reach_the_feature_payload():
    """The two `UNCARRIED_GAPS` entries this task closes, asserted on VALUES through the same
    sentinel bundle the coverage guard derives its own answer from."""
    payload = for_feature_generation(fully_populated_bundle())
    assert payload["business_term"] == sentinel("business_term")
    assert payload["related_terms"] == [sentinel("related_terms")]


def test_related_terms_is_always_a_list_even_when_the_glossary_curated_none():
    """The landmine in its quietest form. `_token_list_ok(None)` is False, so emitting `None` for a
    column with no curated related terms would refuse that column's WHOLE payload — every
    non-glossary column in the catalog, silently, with the flag on."""
    bundle = fully_populated_bundle()
    bare = type(bundle)(**{**{f.name: getattr(bundle, f.name)
                             for f in bundle.__dataclass_fields__.values()},
                           "source_semantics": (), "resolved_semantics": ()})
    payload = for_feature_generation(bare)
    assert payload["related_terms"] == []
    assert enrich_llm._token_list_ok(payload["related_terms"])


@pytest.mark.parametrize("key,listname", [
    ("business_term", "_FEATURE_COLUMN_PROSE_KEYS"),
    ("related_terms", "_FEATURE_COLUMN_PROSE_LIST_KEYS"),
])
def test_removing_a_glossary_keys_classification_refuses_the_whole_column(
        monkeypatch, key, listname):
    payload = {"columns": [{"object_ref": "x", "business_term": "Counterparty Exposure Amount",
                            "related_terms": ["obligor exposure", "credit exposure"]}]}
    assert sanitize_feature_context(payload)[0] is not None
    current = getattr(enrich_llm, listname)
    monkeypatch.setattr(enrich_llm, listname, frozenset(current) - {key})
    assert sanitize_feature_context(payload)[0] is None, f"{key} is not graded by {listname}"


def test_the_glossary_term_list_is_pii_redacted_per_term_not_length_bounded_as_one_blob():
    """`related_terms` is uploader-authored, so it takes the PROSE grade — not the token-list grade
    its shape would suggest, which applies no redaction at all. Each term is scanned at its own
    indexed path, exactly as `_LIST_PROSE_META_KEYS` does on the enrichment seam."""
    payload = {"columns": [{"object_ref": "x",
                            "related_terms": ["obligor", "ask jane.roe@bank.example"]}]}
    safe, spans, _audits, version = sanitize_feature_context(payload)
    assert safe is not None
    assert "jane.roe@bank.example" not in safe["columns"][0]["related_terms"][1]
    assert version is not None
    assert {s["key"] for s in spans} >= {"columns[0].related_terms[1]"}


def test_the_curated_terms_authority_rides_the_payload_beside_them(catalog, monkeypatch):
    """Every unconfirmed value in this plan says who vouched for it. `business_term` and
    `related_terms` reach no `graph_node` display column, so they ride `source_semantics` alone and
    the `resolved_semantics` loop that builds `semantic_authority` never saw them — the value would
    have egressed with nothing beside it saying it is the SOURCE's declaration and not the
    platform's finding."""
    monkeypatch.setenv(fa.FEATURE_CONTEXT_FLAG, "1")
    _term_evidence(catalog, "payments", "cpty_expsr_amt", "Counterparty Exposure Amount")
    cols = [c for c in fa._candidate_columns(catalog, _SRC, roles=())
            if c["column"] == "cpty_expsr_amt"]
    payload = fa._context_v4_column(catalog, cols[0], roles=())
    assert payload["business_term"] == "Counterparty Exposure Amount"
    assert payload["semantic_authority"]["business_term"] == "source/attested"


def test_the_objective_can_match_the_banks_own_curated_term(catalog):
    """Gap 2's second half, and the reason it matters. An analyst asking about "counterparty
    exposure" must reach `CPTY_EXPSR_AMT`, whose only readable English is the glossary's curated
    business NAME. `_column_tokens` reads the CANDIDATE row, so the term has to arrive there — a
    payload-only fix would leave the ranking exactly as blind as it was."""
    _term_evidence(catalog, "payments", "cpty_expsr_amt", "Counterparty Exposure Amount")
    cols = {c["column"]: c for c in fa._candidate_columns(catalog, _SRC, roles=())}
    assert "counterparty" in fa._column_tokens(cols["cpty_expsr_amt"])
    assert "counterparty" not in fa._column_tokens(cols["pmt_id"])


def _term_evidence(db, table: str, column: str, value: str) -> None:
    from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.object_ref import normalize_ref

    ref = normalize_ref(_SRC, None, table, column)
    record_field_evidence(
        db, logical_ref=ref, field_name="business_term", proposed_value=value,
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="test", source_snapshot_id="snap",
        input_hash=field_input_hash(logical_ref=ref, field_name="business_term",
                                    material=f"{value}:source:attested"))


# ── the LAST gap: fibo_path at the feature seam (readiness wave, migration 1058) ──────────────────


def test_fibo_path_reaches_the_feature_payload_like_its_two_siblings():
    """The final `UNCARRIED_GAPS` entry, asserted on a VALUE rather than on the field's name.

    The handover called closing this "one line to close" — the `_FEATURE_COLUMN_PROSE_KEYS` grade.
    It was six: `fibo_path` had NO `graph_node` column (1051 added `bian_path`/`process_path`/
    `sub_domain` and not it), no field policy and no projection, so there was nothing for the grade
    to carry. It was invisible precisely BECAUSE its two siblings from the same sidecar worked —
    all three are captured identically as SOURCE evidence by the glossary reader, and only these
    two had somewhere to land."""
    payload = for_feature_generation(fully_populated_bundle())
    assert payload["fibo_path"] == sentinel("fibo_path")
    # …and it travels beside the siblings it was always meant to, not on some private path.
    assert payload["bian_path"] == sentinel("bian_path")
    assert payload["process_path"] == sentinel("process_path")


def test_fibo_path_is_prose_graded_so_an_unredacted_source_value_is_scanned():
    """Its origin (`glossary_reader`) is uploader text, so the grade is the point of the whole
    exercise: without a `_FEATURE_COLUMN_PROSE_KEYS` entry the key is UNCLASSIFIED and fails the
    column closed; with the wrong (structural) grade it would egress unscanned."""
    payload = {"columns": [{"object_ref": "x",
                            "fibo_path": "FIBO > Contracts > owner jane.doe@bank.example"}]}
    out, pii_spans, _sa, _v = sanitize_feature_context(payload)
    assert out is not None, "fibo_path is not classified — it fails the whole column closed"
    assert "jane.doe@bank.example" not in out["columns"][0]["fibo_path"]
    assert any(s["key"] == "columns[0].fibo_path" for s in pii_spans)
