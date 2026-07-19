"""Task 4 (Phase-2 Slice 3A-iii) — nested field-aware egress adapter for the feature menu.

`sanitize_feature_context` runs inside `audited_structured_call` on EVERY overlay LLM call, so it
must (a) sample-strip + PII-redact the definition-kind fields nested in `columns[*]` and
`table_context[*]`, (b) allowlist+bound the structural fields, (c) FAIL CLOSED on any unclassified
key or a blanked definition, and (d) stay INERT (byte-identical passthrough) on every payload that
carries no feature menu — including the enrichment roster (`columns` as list of strings) and the
flag-off thin menu. RF-I6 (BINDING): the contract-draft `_column_defs` shape — a DIFFERENT but
SAFE dict shape, with NULLABLE graph `definition`/`concept` — must pass the gate, never block.
"""
import json

from featuregen.intake.llm import PROVIDER_OK, FakeLLM, FakeResponse, LLMResult
from featuregen.intake.redaction import INPUT_KEY_CATALOG, INPUT_KEY_CLASSIFICATION
from featuregen.overlay.upload.enrich_llm import sanitize_feature_context

_SAMPLE = ("Posting amount is the monetary value of the ledger entry, with representative values "
           "such as 3708484836801; 3708446902413; 3708454004701, which supports interpretation.")

# Plain-prose definition carrying a REAL PII token (EMAIL — the Slice-1 deterministic detector's
# canonical class): no sample-clause marker anywhere, so `sanitize_definition` neither strips nor
# blanks (state="none", reason="") and the ONLY protective action is the PII redaction — exercising
# the adapter's `pii_spans.extend(...)` span-emission branch, which no clause-strip/blank test can.
_PII_TOKEN = "jane.doe@bank.example"
_PII_DEFN = (f"Escalation contact for this ledger column; route alerts to {_PII_TOKEN} "
             "before month-end close.")


def test_non_feature_payload_untouched():
    meta = {"table": "t", "columns": ["a:int", "b:int"]}  # enrichment roster (list of strings)
    safe, spans, audits, ver = sanitize_feature_context(meta)
    assert safe is meta          # same object -> byte-identical
    assert (spans, audits, ver) == ([], [], None)


def test_thin_menu_with_null_identity_untouched():
    meta = {"columns": [{"object_ref": "public.t.c", "table": "t", "column": "c",
                         "concept": None, "domain": None}], "avoid": []}
    safe, spans, audits, ver = sanitize_feature_context(meta)
    assert safe is meta          # no definition-kind field -> untouched (flag-off byte-identity)
    assert (spans, audits, ver) == ([], [], None)


def test_definition_sample_clause_stripped_and_audited():
    meta = {"columns": [{"object_ref": "public.t.amount", "table": "t", "column": "amount",
                        "definition": _SAMPLE,
                         "additivity": {"value": "additive", "authority": "governed"}}]}
    safe, spans, audits, ver = sanitize_feature_context(meta)
    assert safe is not None
    clean = safe["columns"][0]["definition"]
    assert "3708484836801" not in clean
    assert "representative values" not in clean
    assert ver  # a sanitizer/redaction version was stamped
    assert any(a["path"] == "columns[0].definition" and a["removed_count"] >= 1 for a in audits)
    # The {value, authority} fact wrapper passes through structurally unchanged.
    assert safe["columns"][0]["additivity"] == {"value": "additive", "authority": "governed"}


def test_unclassified_key_fails_closed():
    meta = {"columns": [{"object_ref": "public.t.c", "evil": "surprise"}]}
    safe, _spans, _audits, _ver = sanitize_feature_context(meta)
    assert safe is None          # any unclassified key blocks the payload


def test_blanked_definition_fails_closed():
    # A data marker the stripper cannot consume -> sanitize_definition blanks -> block dispatch.
    meta = {"columns": [{"object_ref": "public.t.c", "definition": "sample values: 4111 2222"}]}
    safe, _spans, audits, _ver = sanitize_feature_context(meta)
    assert safe is None
    assert any(a["state"] == "suspected_unhandled" for a in audits)


def test_bad_fact_wrapper_and_table_context():
    ok = {"columns": [{"object_ref": "x", "unit": {"value": "dollars", "authority": "hint"}}],
          "table_context": [{"table": "t", "grain_columns": ["id"], "table_definition": _SAMPLE}]}
    safe, _spans, audits, _ver = sanitize_feature_context(ok)
    assert safe is not None
    assert "3708484836801" not in safe["table_context"][0]["table_definition"]
    assert any(a["path"] == "table_context[0].table_definition" for a in audits)
    bad = {"columns": [{"object_ref": "x", "additivity": {"value": "additive"}}]}  # missing authority
    assert sanitize_feature_context(bad)[0] is None


def test_contract_draft_column_defs_shape_passes():
    """RF-I6 (BINDING): the `overlay.contract.draft` payload's `_column_defs` shape —
    {object_ref, column, concept, definition}, with `definition`/`concept` NULLABLE straight from
    graph_node — must PASS the gate (definition-kind sanitized, NULLs tolerated), never fail-close
    legitimate contract authoring."""
    meta = {"feature": "avg_posting_amount", "aggregation": "avg",
            "columns": [
                {"object_ref": "public.ledger.entry_id", "column": "entry_id",
                 "concept": None, "definition": None},                    # NULL graph definition
                {"object_ref": "public.ledger.amount", "column": "amount",
                 "concept": "monetary_amount", "definition": _SAMPLE}]}
    safe, _spans, audits, ver = sanitize_feature_context(meta)
    assert safe is not None                                # NOT blocked — the draft path stays open
    assert safe["columns"][0]["definition"] is None        # NULL tolerated, passed through
    assert "3708484836801" not in safe["columns"][1]["definition"]
    assert any(a["path"] == "columns[1].definition" and a["removed_count"] >= 1 for a in audits)
    assert ver
    assert safe["feature"] == "avg_posting_amount"         # non-menu top-level keys untouched


def test_enriched_menu_shape_with_flag_facts_passes():
    """RF-I7: grain/as-of facts arrive as {"value": "true"/"false", "authority": ...} wrappers and
    identity keys may be absent (`catalog_source` is never emitted) or None — tolerated, not
    unknown-key-blocked."""
    meta = {"columns": [{
        "object_ref": "public.txn.posted_at", "table": "txn", "column": "posted_at",
        "data_type": {"value": "timestamp", "authority": "hint"},
        "declared_type": {"value": None, "authority": "hint"},
        "entity": {"value": None, "authority": "hint"},
        "additivity": {"value": None, "authority": "hint"},
        "unit": {"value": None, "authority": "hint"},
        "currency": {"value": None, "authority": "hint"},
        "is_grain": {"value": "false", "authority": "hint"},
        "is_as_of": {"value": "true", "authority": "governed"}}]}
    safe, spans, audits, ver = sanitize_feature_context(meta)
    assert safe is meta                       # structural-only -> untouched (byte-identity)
    assert (spans, audits, ver) == ([], [], None)


def test_definition_pii_redaction_emits_keyed_spans():
    """[F3] span-emission branch: a REAL PII token in a nested menu definition (no sample clause,
    so nothing strips or blanks) must surface as a pii_spans record keyed to its nested path —
    delete `pii_spans.extend(...)` in `_defn` and this fails."""
    meta = {"columns": [{"object_ref": "public.ledger.contact_notes", "table": "ledger",
                         "column": "contact_notes", "definition": _PII_DEFN}]}
    safe, spans, audits, ver = sanitize_feature_context(meta)
    assert safe is not None                                  # redacted, NOT fail-closed
    clean = safe["columns"][0]["definition"]
    assert _PII_TOKEN not in clean                           # the raw token never survives
    assert "[REDACTED:EMAIL]" in clean
    # The span record keeps nested-path granularity: key + type + offsets, never the value.
    email_spans = [s for s in spans if s.get("key") == "columns[0].definition"]
    assert email_spans, f"no span keyed columns[0].definition in {spans!r}"
    assert all(s["type"] == "EMAIL" and "start" in s and "end" in s for s in email_spans)
    assert all("value" not in s for s in email_spans)
    # Plain prose: sanitized (state none), not clause-stripped, and versioned by the redactor.
    assert any(a["path"] == "columns[0].definition" and a["state"] == "none" for a in audits)
    assert ver


class _Capture:
    """Client that records the outbound request and answers with a valid (empty) idea list."""

    def __init__(self):
        self.requests = []

    def call(self, request):
        self.requests.append(request)
        return LLMResult(output={"features": []}, self_reported_scores={},
                         call_ref="", status=PROVIDER_OK)


def _seam_call(db, client, catalog_metadata):
    from featuregen.overlay.upload.enrich_llm import (
        audited_structured_call,
        register_enrichment_schemas,
    )
    register_enrichment_schemas(db)
    return audited_structured_call(
        db, client, task="overlay.feature.recommend", prompt_id="feature_recommend_v1",
        schema_id="feature_ideas", catalog_metadata=catalog_metadata,
        instruction="predict churn")


def test_audited_structured_call_blocks_unclassified_menu_key(db):
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": []})})
    before = db.execute("SELECT count(*) FROM llm_call").fetchone()[0]
    out = _seam_call(db, client,
                     {"columns": [{"object_ref": "public.t.c", "evil": "surprise"}]})
    assert out is None                              # blocked, no dispatch
    after = db.execute("SELECT count(*) FROM llm_call").fetchone()[0]
    assert after == before                          # no llm_call recorded
    n = db.execute(
        "SELECT count(*) FROM security_audit WHERE event_type = 'EGRESS_BLOCKED'").fetchone()[0]
    assert n == 1                                   # the block itself is audited


def test_audited_structured_call_strips_planted_sample_before_dispatch(db):
    """End-to-end at the seam: a planted sample clause in a menu column's definition NEVER reaches
    the provider — the outbound request is the sanitized rendering, and the strip is recorded on
    the immutable llm_call's input_redaction."""
    client = _Capture()
    out = _seam_call(db, client, {"columns": [
        {"object_ref": "public.ledger.amount", "table": "ledger", "column": "amount",
         "definition": _SAMPLE,
         "additivity": {"value": "additive", "authority": "governed"}}]})
    assert out == {"features": []}                  # the call still works
    assert len(client.requests) == 1
    flat = json.dumps(client.requests[-1].inputs[INPUT_KEY_CATALOG])
    assert "3708484836801" not in flat              # the planted sample never left the system
    assert "3708446902413" not in flat
    assert "representative values" not in flat
    row = db.execute(
        "SELECT input_redaction::text FROM llm_call WHERE task = 'overlay.feature.recommend'"
    ).fetchone()
    assert row is not None
    assert "columns[0].definition" in row[0]        # sample_strip audit reached the record
    assert "3708484836801" not in row[0]            # ...and never the value


def test_audited_structured_call_persists_pii_span_and_classification(db):
    """End-to-end at the seam: a REAL PII token in a menu column's definition is redacted (not
    blanked) before dispatch, its span record reaches the immutable llm_call's input_redaction
    keyed to the nested path, and the persisted classification honestly reads `contains_pii` —
    dropping the adapter's span branch would silently flip it to `clean`."""
    client = _Capture()
    out = _seam_call(db, client, {"columns": [
        {"object_ref": "public.ledger.contact_notes", "table": "ledger",
         "column": "contact_notes", "definition": _PII_DEFN}]})
    assert out == {"features": []}                  # redacted, dispatched — NOT fail-closed
    assert len(client.requests) == 1
    req_inputs = client.requests[-1].inputs
    assert _PII_TOKEN not in json.dumps(req_inputs[INPUT_KEY_CATALOG])  # never egressed
    assert req_inputs[INPUT_KEY_CLASSIFICATION] == "contains_pii"       # honest outbound verdict
    row = db.execute(
        "SELECT input_redaction, redacted_input FROM llm_call"
        " WHERE task = 'overlay.feature.recommend'").fetchone()
    assert row is not None
    input_redaction, redacted_input = row
    # (a) the span record persisted at nested-path granularity: key + type + offsets, no value.
    spans = [s for s in input_redaction["redacted_spans"]
             if s.get("key") == "columns[0].definition"]
    assert spans, f"no persisted span keyed columns[0].definition in {input_redaction!r}"
    assert all(s["type"] == "EMAIL" and "start" in s and "end" in s for s in spans)
    # (b) the raw token is in neither the redaction record nor the persisted outbound payload.
    assert _PII_TOKEN not in json.dumps(input_redaction)
    assert _PII_TOKEN not in json.dumps(redacted_input)
    # (c) the persisted request's classification field reads the scan's honest verdict.
    assert redacted_input[INPUT_KEY_CLASSIFICATION] == "contains_pii"


def test_audited_structured_call_blocks_blanked_definition_no_dispatch(db):
    """A definition the sanitizer BLANKS (unconsumable data marker) blocks the WHOLE call at the
    seam: no dispatch, EGRESS_BLOCKED audited."""
    client = _Capture()
    out = _seam_call(db, client, {"columns": [
        {"object_ref": "public.t.c", "definition": "sample values: 4111 2222"}]})
    assert out is None
    assert client.requests == []                    # nothing egressed
    n = db.execute(
        "SELECT count(*) FROM security_audit WHERE event_type = 'EGRESS_BLOCKED'").fetchone()[0]
    assert n == 1
