import json

from featuregen.intake import redaction as redaction_module
from featuregen.intake.llm import (
    PROVIDER_NON_RETRYABLE,
    PROVIDER_OK,
    FakeLLM,
    FakeResponse,
    LLMResult,
)
from featuregen.intake.redaction import (
    INPUT_KEY_CATALOG,
    INPUT_KEY_CLASSIFICATION,
    INPUT_KEY_INTENT,
    RedactionResult,
)
from featuregen.overlay.upload.enrich_batch import EGRESS, VALID, BatchItem
from featuregen.overlay.upload.enrich_llm import (
    _SCHEMAS,
    audited_batch_call,
    audited_enrich_call,
    register_enrichment_schemas,
)

_META = {"table": "accounts", "column": "balance", "type": "numeric"}


def _forbidden_array_keys(node, path=""):
    """Walk a JSONSchema node (dict/list) and yield the dotted path of every `minItems`/`maxItems`
    encountered anywhere in the structure."""
    if isinstance(node, dict):
        for key, val in node.items():
            here = f"{path}.{key}" if path else key
            if key in ("minItems", "maxItems"):
                yield here
            yield from _forbidden_array_keys(val, here)
    elif isinstance(node, list):
        for i, val in enumerate(node):
            yield from _forbidden_array_keys(val, f"{path}[{i}]")


def test_no_output_schema_carries_array_minitems_or_maxitems():
    """API-compatibility pin: the Anthropic structured-output API rejects `maxItems` (and, defensively,
    `minItems`) on `array` types with HTTP 400, which fails every enrichment call closed. The real
    per-batch count cap is code-enforced (`validate_batch_results`), so NO schema may carry either key.
    Reintroducing one must fail CI, not silently break the live provider (FakeLLM never validates it)."""
    offenders = {
        name: found
        for (name, _ver), schema in _SCHEMAS.items()
        if (found := sorted(_forbidden_array_keys(schema)))
    }
    assert offenders == {}, f"schemas still carry minItems/maxItems (rejected by the API): {offenders}"


def _call(db, client, catalog_metadata=_META):
    register_enrichment_schemas(db)
    return audited_enrich_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_v1",
        schema_id="overlay_concept", catalog_metadata=catalog_metadata, out_key="concept",
        instruction="Classify the concept of this column.")


class _Capture:
    """Client that records the outbound request and answers with a valid concept."""

    def __init__(self):
        self.requests = []

    def call(self, request):
        self.requests.append(request)
        return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                         call_ref="", status=PROVIDER_OK)


def test_audited_call_returns_output_and_records(db):
    out = _call(db, FakeLLM(script={"overlay.enrich.concept":
                                    FakeResponse(output={"concept": "monetary_amount"})}))
    assert out == "monetary_amount"
    # exactly one immutable llm_call record was written under the overlay-enrichment run bucket
    n = db.execute(
        "SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert n == 1


def test_request_carries_schema_and_reserved_keys(db):
    captured = {}

    class _Capture:
        def call(self, request):
            captured["schema"] = request.output_schema
            captured["inputs"] = dict(request.inputs)
            return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK)

    _call(db, _Capture())
    assert captured["schema"] is not None                 # M2: schema attached
    assert INPUT_KEY_INTENT in captured["inputs"]         # reserved keys, not bare
    assert INPUT_KEY_CATALOG in captured["inputs"]
    assert captured["inputs"][INPUT_KEY_CATALOG] == _META
    assert "definition" not in captured["inputs"]         # no free-text egress


def test_provider_that_fails_without_schema_now_succeeds(db):
    """A ClaudeLLM-shaped client fails closed with no output_schema; the audited call attaches one."""
    class _RealShaped:
        def call(self, request):
            if not request.output_schema:
                return LLMResult(output={}, self_reported_scores={}, call_ref="",
                                 status=PROVIDER_NON_RETRYABLE)
            return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK)

    assert _call(db, _RealShaped()) == "monetary_amount"


# ---- finding #19: glossary free-text is uploader-authored content — it MUST be scanned, never
# ---- egressed under a hardcoded "clean" claim. --------------------------------------------------

_PII_SIDECAR = {
    "table": "accounts", "column": "balance", "type": "numeric",
    "term_name": "Account Balance",
    "business_definition": "Posted ledger balance; escalate breaks to jane.doe@bank.example.",
}


def test_glossary_free_text_pii_is_scanned_and_scrubbed_before_egress(db):
    """A detectable PII pattern (email) inside a glossary business definition is redacted BEFORE
    egress — the definition still rides (enrichment keeps its semantics), the pattern does not,
    and the classification is the honest 'contains_pii', not a hardcoded 'clean'."""
    client = _Capture()
    out = _call(db, client, catalog_metadata=_PII_SIDECAR)
    assert out == "monetary_amount"                       # enrichment still works
    sent = client.requests[-1].inputs
    flat = json.dumps(sent[INPUT_KEY_CATALOG])
    assert "jane.doe@bank.example" not in flat            # the email never left the system
    assert "[REDACTED:EMAIL]" in sent[INPUT_KEY_CATALOG]["business_definition"]
    assert sent[INPUT_KEY_CLASSIFICATION] == "contains_pii"


def test_clean_glossary_free_text_still_egresses_and_enriches(db):
    """A clean curated definition passes through verbatim — scanned-clean, enrichment unbroken."""
    meta = {**_META, "term_name": "Account Balance",
            "business_definition": "The posted ledger balance of the account."}
    client = _Capture()
    assert _call(db, client, catalog_metadata=meta) == "monetary_amount"
    sent = client.requests[-1].inputs
    assert sent[INPUT_KEY_CATALOG]["business_definition"] == \
        "The posted ledger balance of the account."
    assert sent[INPUT_KEY_CLASSIFICATION] == "clean"


def test_glossary_pii_scrub_is_recorded_on_the_llm_call_audit(db):
    """The llm_call record carries the redaction honestly: the redactor's version (not the
    'metadata-only' names/types claim) and the span TYPES — never the scrubbed value."""
    _call(db, _Capture(), catalog_metadata=_PII_SIDECAR)
    row = db.execute(
        "SELECT redaction_version, input_redaction::text, redacted_input::text FROM llm_call "
        "WHERE run_id = 'overlay-enrichment'").fetchone()
    assert row is not None
    version, input_redaction, redacted_input = row
    assert version != "metadata-only"                     # free-text WAS scanned
    assert "EMAIL" in input_redaction                     # span type recorded...
    assert "jane.doe@bank.example" not in input_redaction  # ...never the value
    assert "jane.doe@bank.example" not in redacted_input   # stored input is the redacted rendering


def test_registered_ner_redactor_scrubs_names_from_glossary_free_text(db, monkeypatch):
    """The personal-NAMES residual is closeable via the register_intent_redactor seam: when a
    NER-backed redactor IS registered, glossary free-text routes through it."""
    class _NerRedactor:
        def redact(self, raw_intent, raw_input_classification):
            if "Jane Doe" in raw_intent:
                return RedactionResult(raw_intent.replace("Jane Doe", "[REDACTED:NAME]"),
                                       "ner-redactor@test",
                                       ({"type": "NAME", "start": 0, "end": 8},), "ok")
            return RedactionResult(raw_intent, "ner-redactor@test", (), "ok")

    monkeypatch.setattr(redaction_module, "_INTENT_REDACTOR", _NerRedactor())
    meta = {**_META, "business_definition": "Owned by Jane Doe in Finance."}
    client = _Capture()
    assert _call(db, client, catalog_metadata=meta) == "monetary_amount"
    flat = json.dumps(client.requests[-1].inputs[INPUT_KEY_CATALOG])
    assert "Jane Doe" not in flat
    assert "[REDACTED:NAME]" in flat


def test_free_text_redactor_fail_closed_blocks_dispatch(db, monkeypatch):
    """A redactor that fails closed on a glossary value blocks the call: nothing is dispatched
    and the block is audited as a security event."""
    class _FailClosed:
        def redact(self, raw_intent, raw_input_classification):
            return RedactionResult(None, "fail@test", (), "fail_into_clarification")

    monkeypatch.setattr(redaction_module, "_INTENT_REDACTOR", _FailClosed())
    client = _Capture()
    out = _call(db, client, catalog_metadata=dict(_PII_SIDECAR))
    assert out is None
    assert client.requests == []                          # no dispatch — fail closed
    n = db.execute(
        "SELECT count(*) FROM security_audit WHERE event_type = 'EGRESS_BLOCKED'").fetchone()[0]
    assert n == 1


def test_batch_glossary_free_text_pii_scrubbed_per_item(db):
    """Batch seam: a PII pattern (SSN) in one item's business definition is scrubbed pre-egress;
    the item still enriches (redacted, not dropped) and the sibling item is untouched."""
    register_enrichment_schemas(db)
    items = [
        BatchItem("h1", {"table": "accounts", "column": "balance", "type": "numeric",
                         "business_definition": "Balance; sample holder SSN 123-45-6789."}),
        BatchItem("h2", {"table": "accounts", "column": "opened_on", "type": "date"}),
    ]

    class _BatchCapture:
        def __init__(self):
            self.requests = []

        def call(self, request):
            self.requests.append(request)
            return LLMResult(output={"results": [{"ref": "h1", "concept": "monetary_amount"},
                                                 {"ref": "h2", "concept": "event_date"}]},
                             self_reported_scores={}, call_ref="", status=PROVIDER_OK)

    client = _BatchCapture()
    res = audited_batch_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_batch_v1",
        schema_id="overlay_concept_batch", shared_metadata={}, items=items, out_key="concept",
        instruction="Classify each column.", accept=lambda raw: (raw, "valid"))
    by = {o.ref: o for o in res.outcomes}
    assert by["h1"].status == VALID and by["h2"].status == VALID
    sent_items = client.requests[-1].inputs[INPUT_KEY_CATALOG]["items"]
    flat = json.dumps(sent_items)
    assert "123-45-6789" not in flat                      # the SSN never left the system
    assert "[REDACTED:SSN]" in flat
    assert client.requests[-1].inputs[INPUT_KEY_CLASSIFICATION] == "contains_pii"


def test_batch_item_failing_closed_is_excluded_not_batch_fatal(db, monkeypatch):
    """Batch seam: an item whose free-text the redactor fails closed on is EXCLUDED (terminal
    egress outcome, audited) while the rest of the batch proceeds."""
    class _FailOnJane:
        def redact(self, raw_intent, raw_input_classification):
            if "Jane" in raw_intent:
                return RedactionResult(None, "ner@test", (), "fail_into_clarification")
            return RedactionResult(raw_intent, "ner@test", (), "ok")

    monkeypatch.setattr(redaction_module, "_INTENT_REDACTOR", _FailOnJane())
    register_enrichment_schemas(db)
    items = [
        BatchItem("h1", {"table": "accounts", "column": "owner", "type": "text",
                         "business_definition": "Jane's book of accounts."}),
        BatchItem("h2", {"table": "accounts", "column": "balance", "type": "numeric"}),
    ]
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(
        output={"results": [{"ref": "h2", "concept": "monetary_amount"}]})})
    res = audited_batch_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_batch_v1",
        schema_id="overlay_concept_batch", shared_metadata={}, items=items, out_key="concept",
        instruction="Classify each column.", accept=lambda raw: (raw, "valid"))
    by = {o.ref: o for o in res.outcomes}
    assert by["h1"].status == EGRESS
    assert by["h2"].status == VALID


# ---- finding #24: the audit records the REAL generation settings + provider usage --------------


def test_audit_records_real_generation_settings_for_anthropic(db, monkeypatch):
    """#24(a): with a real provider configured, the llm_call generation_settings carry the ACTUAL
    settings the adapter applies — model + max_tokens + thinking + effort, read from the SAME env
    as ClaudeConfig.from_env — not just provider/model."""
    monkeypatch.setenv("FEATUREGEN_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FEATUREGEN_LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("FEATUREGEN_LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("FEATUREGEN_LLM_THINKING", "adaptive")
    monkeypatch.setenv("FEATUREGEN_LLM_EFFORT", "medium")
    _call(db, _Capture())
    gs = db.execute("SELECT generation_settings FROM llm_call "
                    "WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert gs == {"provider": "anthropic", "model": "claude-sonnet-5", "max_tokens": 2048,
                  "thinking": "adaptive", "effort": "medium"}


def test_audit_captures_provider_usage_tokens(db):
    """#24(b): a client that reports provider usage (the real adapter, from resp.usage) has the
    token counts captured on the immutable llm_call record — not discarded."""
    class _WithUsage:
        def call(self, request):
            return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK,
                             cost_metadata={"input_tokens": 321, "output_tokens": 87})

    assert _call(db, _WithUsage()) == "monetary_amount"
    cm = db.execute("SELECT cost_metadata FROM llm_call "
                    "WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert cm["input_tokens"] == 321 and cm["output_tokens"] == 87


def test_audit_without_usage_records_cleanly(db):
    """#24: usage is optional — a FakeLLM call (no usage) still audits cleanly, with empty
    cost_metadata rather than a crash or a fabricated count."""
    out = _call(db, FakeLLM(script={"overlay.enrich.concept":
                                    FakeResponse(output={"concept": "monetary_amount"})}))
    assert out == "monetary_amount"
    cm = db.execute("SELECT cost_metadata FROM llm_call "
                    "WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert cm == {}


# ---- finding #20: the llm_call egress audit must survive the upload transaction ----------------


def test_llm_egress_audit_survives_upload_rollback(db, monkeypatch):
    """By the time the audit row is written, data has ALREADY left the system — so the record must
    commit INDEPENDENTLY of the upload transaction. A later rollback of the request connection
    (graph/DB failure in the same upload) must not erase the evidence. The request conn holds an
    advisory lock here, proving the separate audit conn never re-acquires one (program-audit I-3
    self-deadlock class)."""
    import psycopg

    monkeypatch.setenv("FEATUREGEN_DSN", db.info.dsn)     # production signal: durable audit ON
    task = "test.egress.audit.rollback"
    client = FakeLLM(script={task: FakeResponse(output={"concept": "monetary_amount"})})
    register_enrichment_schemas(db)
    db.execute("SELECT pg_advisory_xact_lock(4242)")      # the upload tx holds an advisory lock
    try:
        out = audited_enrich_call(
            db, client, task=task, prompt_id="overlay_concept_v1", schema_id="overlay_concept",
            catalog_metadata=_META, out_key="concept", instruction="Classify this column.")
        assert out == "monetary_amount"
        db.rollback()                                     # the upload transaction later fails
        with psycopg.connect(db.info.dsn) as fresh:
            n = fresh.execute(
                "SELECT count(*) FROM llm_call WHERE task = %s", (task,)).fetchone()[0]
        assert n == 1                                     # the egress evidence SURVIVES
    finally:
        # llm_call is write-once (trigger-enforced); as table owner, drop the guard just long
        # enough to remove this test's committed row so the shared test DB stays clean.
        with psycopg.connect(db.info.dsn, autocommit=True) as c:
            c.execute("ALTER TABLE llm_call DISABLE TRIGGER llm_call_no_mutation")
            c.execute("DELETE FROM llm_call WHERE task = %s", (task,))
            c.execute("ALTER TABLE llm_call ENABLE TRIGGER llm_call_no_mutation")


def test_llm_egress_audit_stays_transactional_without_production_dsn(db, monkeypatch):
    """Without the production DSN signal (tests / no-DB harness) the audit stays on the request
    conn — same gate as api.deps.audit_access_denied, so a rolled-back test never leaks
    committed rows into the shared database."""
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    _call(db, _Capture())
    n = db.execute(
        "SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert n == 1                                         # visible in-tx; rolled back on teardown


def test_durable_audit_connection_built_from_settings_dsn(db, monkeypatch):
    """#20 follow-up: psycopg3's ConnectionInfo.dsn STRIPS the password, so a durable-audit
    connection built from conn.info.dsn fails auth in any password-auth deployment and silently
    falls back to the request conn — defeating the durability #20 promised. The separate
    connection must be opened from get_settings().dsn (the full configured DSN, password intact),
    the exact api.deps.audit_access_denied pattern."""
    import psycopg

    marker_dsn = "host=settings-dsn-marker dbname=audit user=fg password=kept"
    monkeypatch.setenv("FEATUREGEN_DSN", marker_dsn)      # durable-audit gate ON
    seen: list[str] = []

    def spy(conninfo, *args, **kwargs):
        seen.append(conninfo)
        raise RuntimeError("spy: durable connection intentionally refused")

    monkeypatch.setattr(psycopg, "connect", spy)
    out = _call(db, _Capture())                           # best-effort fallback -> request conn
    assert out == "monetary_amount"
    assert seen == [marker_dsn]                           # settings DSN, NOT conn.info.dsn
    n = db.execute(
        "SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert n == 1                                         # fallback record stays transactional


def test_flag_off_no_client_means_no_llm_egress_paths_at_all(db):
    """FLAG-OFF safety: with no LLM provider wired (client=None — the default), ingest never
    touches the enrichment/egress seams: no llm_call rows, no EGRESS_BLOCKED events, upload
    ingests unchanged. Findings #19/#20 only alter the client-wired path."""
    from datetime import UTC, datetime, timedelta

    from featuregen.contracts.envelopes import IdentityEnvelope
    from featuregen.overlay.config import OverlayConfig, register_overlay_config
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.ingest import ingest_upload

    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))
    actor = IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))
    rows = [CanonicalRow("s", "accounts", "id", "integer", is_grain=True),
            CanonicalRow("s", "accounts", "balance", "numeric")]
    res = ingest_upload(db, "s", rows, actor=actor, now=datetime(2026, 7, 5, tzinfo=UTC))
    assert res.status == "ingested"
    assert db.execute("SELECT count(*) FROM llm_call").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM security_audit "
                      "WHERE event_type = 'EGRESS_BLOCKED'").fetchone()[0] == 0
