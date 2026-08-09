"""Task 2 (Phase-2 Slice 1): the egress-redaction boundary is FIELD-AWARE.

Definition fields (business_definition / table_definition) get sample-clause stripping + PII
redaction via `sanitize_definition` (fail closed on an unhandled data marker), with a
`{path, sanitizer_version, state, removed_count}` audit AND their PII spans preserved ([F3]);
prose fields stay PII-only; `synonyms` is a LIST of prose audited at indexed paths ([F6]).
The per-item egress gate is split ([F7]): shape/allowlist BEFORE sanitization, per-value length
AFTER — so a long raw definition whose sample clause strips away still egresses.
"""
import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich_llm
from featuregen.overlay.upload.enrich_batch import EGRESS, VALID, BatchItem
from featuregen.overlay.upload.enrich_llm import (
    _FREE_TEXT_META_KEYS,
    _MAX_LEN_DEFAULT,
    MAX_DEFINITION_LEN,
    _item_egress_ok,
    _item_len_ok,
    _item_shape_ok,
    _redact_free_text_meta,
    audited_batch_call,
)


def test_table_definition_is_a_covered_definition_key():
    assert "table_definition" in _FREE_TEXT_META_KEYS


def test_definition_key_sample_clause_stripped_with_audit():
    meta = {"table": "txn",
            "table_definition": "Txn events. Representative values such as A1, B2.",
            "column_profiles": [{"column": "amt",
                                 "business_definition": "A fee. Values such as 1.23, 4.56."}]}
    out, pii_spans, sample_audits, version = _redact_free_text_meta(meta)
    assert out is not None
    assert "A1" not in out["table_definition"] and "1.23" not in out["column_profiles"][0][
        "business_definition"]
    paths = {a["path"] for a in sample_audits}
    assert "table_definition" in paths and "column_profiles.business_definition" in paths
    assert all({"path", "sanitizer_version", "state", "removed_count"} <= a.keys()
               for a in sample_audits)


def test_prose_key_is_pii_redacted_not_sample_stripped():
    # a term name that merely contains 'values such as'-shaped words must survive (prose, not stripped)
    meta = {"table": "t", "term_name": "Values Such As Flag"}
    out, _pii, sample_audits, _v = _redact_free_text_meta(meta)
    assert out["term_name"] == "Values Such As Flag"
    assert all(a["path"] != "term_name" for a in sample_audits)   # prose keys emit no sample audit


def test_unhandled_marker_fails_closed():
    meta = {"table": "t", "table_definition": "sample values: OPN; CLS; PND"}
    out, _pii, _sa, _v = _redact_free_text_meta(meta)
    assert out is None            # suspected_unhandled -> the caller must not egress the item


def test_definition_pii_span_still_reaches_redacted_spans():
    """[F3]: routing definitions through sanitize_definition must NOT drop their PII spans — the
    span records reach pii_spans (-> input_redaction["redacted_spans"]) annotated with the path,
    at the same {type,start,end} granularity as prose fields, ALONGSIDE the sample audit."""
    meta = {"table": "t", "table_definition": "Fee ledger; escalate to jane.doe@bank.example."}
    out, pii_spans, sample_audits, _v = _redact_free_text_meta(meta)
    assert out is not None
    assert "jane.doe@bank.example" not in out["table_definition"]
    assert any(s["key"] == "table_definition" and s["type"] == "EMAIL"
               and {"start", "end"} <= s.keys() for s in pii_spans)
    assert any(a["path"] == "table_definition" and a["state"] == "none" for a in sample_audits)


def test_synonyms_list_item_pii_redacted_and_audited_at_indexed_path():
    """[F6]: synonyms is prose emitted as list[str] — each item PII-scanned, spans audited at the
    indexed path (synonyms[1]), no sample-strip audit (prose is never sample-stripped)."""
    meta = {"table": "t", "synonyms": ["ledger bal", "contact jane.doe@bank.example"]}
    out, pii_spans, sample_audits, _v = _redact_free_text_meta(meta)
    assert out["synonyms"][0] == "ledger bal"
    assert "jane.doe@bank.example" not in out["synonyms"][1]
    assert "[REDACTED:EMAIL]" in out["synonyms"][1]
    assert any(s["key"] == "synonyms[1]" and s["type"] == "EMAIL" for s in pii_spans)
    assert sample_audits == []


def test_unknown_free_text_kind_is_a_hard_error(monkeypatch):
    """[F6] fail closed: a key added to _FREE_TEXT_META_KEYS without a declared kind must raise —
    never be silently routed down the weaker prose path."""
    monkeypatch.setattr(enrich_llm, "_FREE_TEXT_META_KEYS",
                        enrich_llm._FREE_TEXT_META_KEYS | {"mystery_field"})
    with pytest.raises(ValueError):
        _redact_free_text_meta({"table": "t", "mystery_field": "some free text"})


# ---- [F7]: the shape gate and the length gate are split around the sanitizer -------------------


def test_shape_gate_has_no_length_opinion_but_combined_gate_does():
    long_def = {"table": "t", "table_definition": "x" * (MAX_DEFINITION_LEN + 1)}
    assert _item_shape_ok(long_def) is True       # shape/allowlist only — no length opinion
    assert _item_len_ok(long_def) is False        # the definition length bound lives here
    assert _item_egress_ok(long_def) is False     # the combined contract is unchanged


def test_table_definition_gets_the_WIDER_definition_bound():
    """Read from the constants: the 2026-08-06 zero-truncation raise moved both (600 -> 4000 and
    200 -> 1000). What is pinned is the RELATIONSHIP — a definition gets a wider window than any
    other scalar, and over-window is refused in both cases — not the two numbers."""
    assert _MAX_LEN_DEFAULT < MAX_DEFINITION_LEN
    assert _item_egress_ok({"table": "t", "table_definition": "x" * MAX_DEFINITION_LEN}) is True
    assert _item_egress_ok(
        {"table": "t", "table_definition": "x" * (MAX_DEFINITION_LEN + 1)}) is False
    # every other scalar stays at the tighter default
    assert _item_egress_ok({"table": "t", "term_name": "x" * _MAX_LEN_DEFAULT}) is True
    assert _item_egress_ok({"table": "t", "term_name": "x" * (_MAX_LEN_DEFAULT + 1)}) is False


def test_shape_gate_still_rejects_forbidden_keys_and_wrong_types():
    assert _item_shape_ok({"table": "t", "definition": "leaky free text"}) is False
    assert _item_shape_ok({"table": "t", "column": 42}) is False
    assert _item_shape_ok({"table": "t", "columns": ["a", 1]}) is False


# ---- [F4]: the per-descriptor scan covers EVERY free-text key, not just business_definition -----
#
# A.56 recorded `domain`; the audit it asked for ("audit the rest of `_column_profile_shape_ok`'s
# admitted keys against the two prose lists") finds THREE. `table_synth._descriptor` emits
# `term_type`, `domain` and `process_path` from the uploader's sidecar and its docstring calls them
# "bounded structural tokens (200 cap)" — but `_SCALAR_PROSE_META_KEYS` already classifies
# `term_type`/`process_path` as PROSE at the top level, and A.51 #2 established `domain` as uploader
# text. A 200-char cap is a length bound, not a safety property.


@pytest.mark.parametrize("key", ["domain", "term_type", "process_path"])
def test_every_free_text_profile_key_is_pii_scanned_not_just_the_definition(key: str):
    """The [F4] leak: only `column_profiles[*].business_definition` was routed through a sanitizer,
    so an uploader-authored facet beside it egressed VERBATIM. It compounds with the zero-truncation
    raise — `_MAX_COLUMN_PROFILES` 64 -> 512 means one Pass-B call carries up to 8x as many."""
    meta = {"table": "txn",
            "column_profiles": [{"column": "amt", key: "owner jane.doe@bank.example"}]}
    out, pii_spans, _sa, version = _redact_free_text_meta(meta)
    assert out is not None
    assert "jane.doe@bank.example" not in out["column_profiles"][0][key]
    assert "[REDACTED:EMAIL]" in out["column_profiles"][0][key]
    assert any(s["key"] == f"column_profiles.{key}" and s["type"] == "EMAIL" for s in pii_spans)
    assert version is not None      # a scanned payload must name the redactor that scanned it


def test_a_profile_facet_is_prose_grade_so_an_ordinary_sample_phrase_survives():
    """Prose, NOT definition grade — deliberately. The definition grade's marker gate fails CLOSED
    on phrases like "values such as", and these facets ride EVERY Pass-B item; grading them as
    definitions would hand one uploader sentence a kill switch over the whole table's synthesis,
    which is the exact failure mode `_ADVISORY_CONTEXT_KEYS` was created to avoid."""
    meta = {"table": "txn",
            "column_profiles": [{"column": "amt", "domain": "Values Such As Payments"}]}
    out, _pii, sample_audits, _v = _redact_free_text_meta(meta)
    assert out is not None
    assert out["column_profiles"][0]["domain"] == "Values Such As Payments"
    assert all(not a["path"].startswith("column_profiles.domain") for a in sample_audits)


def test_every_admitted_profile_key_is_CLASSIFIED_so_a_new_one_cannot_ride_unscanned():
    """The structural guard, and the reason this bug was invisible. The top-level loop fails closed
    on an unclassified key (D10); the per-descriptor loop had no such gate, so widening
    `_COLUMN_PROFILE_KEYS` silently opened an unscanned egress path. Adding a key to that set must
    now force a classification decision instead of defaulting to 'rides raw'."""
    unclassified = (enrich_llm._COLUMN_PROFILE_KEYS
                    - enrich_llm._COLUMN_PROFILE_STRUCTURAL_KEYS
                    - enrich_llm._COLUMN_PROFILE_DEFINITION_KEYS
                    - enrich_llm._COLUMN_PROFILE_PROSE_KEYS)
    assert unclassified == set(), (
        f"column-profile key(s) {sorted(unclassified)} egress with no declared classification")


def test_an_unclassified_descriptor_key_fails_the_item_CLOSED():
    """The single-call path has no `_item_shape_ok`, so before [F4] an unknown descriptor key rode
    to the provider unscanned. Mirrors the top-level D10 gate one level down."""
    meta = {"table": "txn", "column_profiles": [{"column": "amt", "mystery": "free text"}]}
    out, _pii, _sa, _v = _redact_free_text_meta(meta)
    assert out is None


def _concept_batch_call(db, items, script_results):
    client = FakeLLM(script={"overlay.enrich.concept":
                             FakeResponse(output={"results": script_results})})
    return audited_batch_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_batch_v1",
        schema_id="overlay_concept_batch", shared_metadata={}, items=items, out_key="concept",
        instruction="Classify each column.", accept=lambda raw: (raw, "valid"))


def test_long_raw_definition_sanitizes_within_bound_and_egresses(db):
    """[F7] crux: the length gate runs AFTER sanitization. A raw business_definition OVER the
    definition bound whose sample clause strips down to a short meaning must still egress — the old
    combined pre-redaction gate excluded it before the sanitizer could shorten it."""
    raw = ("The posted fee amount. Representative values such as "
           + "9" * (MAX_DEFINITION_LEN + 100) + ".")
    assert len(raw) > MAX_DEFINITION_LEN
    items = [BatchItem("h1", {"table": "fees", "column": "amt", "type": "numeric",
                              "business_definition": raw})]
    res = _concept_batch_call(db, items, [{"ref": "h1", "concept": "monetary_amount"}])
    by = {o.ref: o for o in res.outcomes}
    assert by["h1"].status == VALID


def test_item_still_over_bound_after_sanitization_is_excluded_and_audited(db):
    """A sanitized value still over its egress bound is excluded on the same egress path (terminal
    EGRESS outcome + EGRESS_BLOCKED security event), while the sibling item proceeds."""
    items = [BatchItem("h1", {"table": "fees", "column": "amt", "type": "numeric",
                              # sized off the bound so it stays over it after any future raise
                              "business_definition": "x" * (MAX_DEFINITION_LEN + 1)}),
             BatchItem("h2", {"table": "fees", "column": "posted_on", "type": "date"})]
    res = _concept_batch_call(db, items, [{"ref": "h2", "concept": "event_date"}])
    by = {o.ref: o for o in res.outcomes}
    assert by["h1"].status == EGRESS
    assert by["h2"].status == VALID
    n = db.execute("SELECT count(*) FROM security_audit "
                   "WHERE event_type = 'EGRESS_BLOCKED'").fetchone()[0]
    assert n == 1
