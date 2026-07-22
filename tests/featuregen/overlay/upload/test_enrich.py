import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash, enrich_concepts

_TASK = "overlay.enrich.concept"


class _NeverCalledLLM:
    def call(self, request):
        raise AssertionError("LLM must not be called on a cache hit")


def test_classifies_and_caches(db):
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance")]
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_amount"})})
    out = enrich_concepts(db, rows, client)
    assert out[content_hash(rows[0])] == "monetary_amount"
    # Cached: a second run with a client that would raise is never called.
    cached = enrich_concepts(db, rows, _NeverCalledLLM())
    assert cached[content_hash(rows[0])] == "monetary_amount"


def test_off_vocab_concept_is_rejected_not_resolved_single_mode(db, monkeypatch):
    """#5: single mode must enforce the SAME response contract as batch — an off-vocabulary/invalid
    concept response is NOT accepted, NOT coerced to 'unclassified', and NOT counted resolved (it is
    simply absent from the returned dict), exactly like batch's `_accept_concept`/`validate_batch_results`
    treats an INVALID batch entry. A stage report computed from `len(out)` must not be able to see
    "all N items resolved" when every provider response was garbage."""
    # Pin single mode: Pass A defaults to BATCH (#4), so without this the #5 single-mode reject path
    # would not be exercised.
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "single")
    rows = [CanonicalRow("deposits", "accounts", "weird", "text")]
    h = content_hash(rows[0])
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "totally_made_up"})})
    out = enrich_concepts(db, rows, client)
    assert h not in out                                          # NOT resolved — mirrors batch INVALID
    assert db.execute("SELECT count(*) FROM enrichment_concept").fetchone()[0] == 0  # NOT cached


def test_off_vocab_concept_is_not_cached_and_retries_next_run(db):
    """#5/#22: a transient off-vocabulary response must not poison the cache — a later run re-attempts
    and a now-valid response resolves and caches normally."""
    rows = [CanonicalRow("deposits", "accounts", "weird", "text")]
    h = content_hash(rows[0])
    bad = FakeLLM(script={_TASK: FakeResponse(output={"concept": "totally_made_up"})})
    assert h not in enrich_concepts(db, rows, bad)
    assert db.execute("SELECT count(*) FROM enrichment_concept").fetchone()[0] == 0  # NOT cached
    # Next run re-attempts (no poisoned cache hit) and the valid classification sticks.
    ok = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_stock"})})
    assert enrich_concepts(db, rows, ok)[h] == "monetary_stock"
    cached = enrich_concepts(db, rows, _NeverCalledLLM())
    assert cached[h] == "monetary_stock"


def test_valid_concept_still_resolves_single_mode(db, monkeypatch):
    """#5 counterpart: a valid (in-vocabulary) single-mode response is still accepted, cached, and
    counted resolved exactly as before — only the off-vocab path changed."""
    # Pin single mode: Pass A defaults to BATCH (#4), so without this the single-mode accept path
    # would not be exercised.
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "single")
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    h = content_hash(rows[0])
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_stock"})})
    out = enrich_concepts(db, rows, client)
    assert out[h] == "monetary_stock"
    cached = enrich_concepts(db, rows, _NeverCalledLLM())
    assert cached[h] == "monetary_stock"


def test_genuine_unclassified_is_a_real_classification_and_caches(db):
    """#22: the literal 'unclassified' is a legitimate vocabulary value ("none of the concepts
    fits") — it stays cacheable, unlike the unknown/error coercion."""
    rows = [CanonicalRow("deposits", "accounts", "weird", "text")]
    h = content_hash(rows[0])
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "unclassified"})})
    assert enrich_concepts(db, rows, client)[h] == "unclassified"
    assert db.execute("SELECT count(*) FROM enrichment_concept").fetchone()[0] == 1  # cached
    cached = enrich_concepts(db, rows, _NeverCalledLLM())   # cache hit — client never called
    assert cached[h] == "unclassified"


class _CapturingFake(FakeLLM):
    def call(self, request):
        self.last = request
        return super().call(request)


def test_b1b_hands_the_full_vocabulary_to_the_classifier_and_accepts_a_rich_concept(db):
    # B1b: the classifier is handed the full structured vocabulary (so it can classify into the ~116
    # concepts, not a hardcoded subset), and a rich concept it returns is accepted end-to-end.
    from featuregen.intake.redaction import INPUT_KEY_CATALOG
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    client = _CapturingFake(script={_TASK: FakeResponse(output={"concept": "monetary_stock"})})
    out = enrich_concepts(db, rows, client)
    vocab = client.last.inputs[INPUT_KEY_CATALOG]["vocabulary"]
    names = {v["name"] for v in vocab}
    assert "monetary_stock" in names and "outcome_label" in names   # rich §3 concepts offered
    assert "monetary_amount" not in names                           # legacy alias excluded as a target
    assert out[content_hash(rows[0])] == "monetary_stock"           # accepted, not coerced to unclassified


def test_drafts_definition_only_when_blank(db):
    from featuregen.overlay.upload.enrich import draft_definitions
    rows = [
        CanonicalRow("deposits", "accounts", "bal", "numeric"),                        # blank -> drafted
        CanonicalRow("deposits", "accounts", "id", "integer", definition="account id"),  # declared -> skipped
    ]
    client = FakeLLM(script={"overlay.enrich.definition":
                             FakeResponse(output={"definition": "the account ledger balance"})})
    out = draft_definitions(db, rows, client)
    assert out[content_hash(rows[0])] == "the account ledger balance"
    assert content_hash(rows[1]) not in out   # declared definition is never overwritten (R3)


def test_classifies_domain_per_table(db):
    from featuregen.overlay.upload.enrich import classify_domains
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer"),
        CanonicalRow("deposits", "accounts", "balance", "numeric"),
    ]
    client = FakeLLM(script={"overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"})})
    out = classify_domains(db, rows, client)
    assert out["accounts"] == "Deposits"


def test_provider_failure_is_not_cached(db):
    """M3: a non-OK provider outcome must not poison the cache — retried next time."""
    from featuregen.intake.llm import PROVIDER_REFUSAL
    rows = [CanonicalRow("deposits", "accounts", "x", "text")]
    fail = FakeLLM(script={_TASK: FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)})
    out = enrich_concepts(db, rows, fail)
    assert out == {}                                      # nothing cached
    assert db.execute("SELECT count(*) FROM enrichment_concept").fetchone()[0] == 0
    # A later OK call succeeds (the failure did not stick).
    ok = FakeLLM(script={_TASK: FakeResponse(output={"concept": "account_identifier"})})
    out2 = enrich_concepts(db, rows, ok)
    assert out2[content_hash(rows[0])] == "account_identifier"


def test_garbage_domain_and_definition_are_rejected(db):
    from featuregen.overlay.upload.enrich import classify_domains, draft_definitions
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]
    listish = FakeLLM(script={
        "overlay.enrich.definition": FakeResponse(output={"definition": "['a', 'b']"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "['Deposits','Payments']"}),
    })
    assert draft_definitions(db, rows, listish) == {}     # list-stringified -> rejected
    assert classify_domains(db, rows, listish) == {}


@pytest.mark.parametrize("echo", [
    "overlay.enrich.domain",       # the exact 07-17 prompt-echo garbage that got durably cached
    "overlay.enrich.concept",      # any internal task id
    "featuregen.overlay.upload",   # a bare dotted-lowercase token shaped like a task id
    "overlay_domain",              # the reserved overlay_ namespace
])
def test_prompt_echo_domain_is_rejected_and_not_cached(db, echo):
    """07-17 bug: the model echoed its own task name and 'overlay.enrich.domain' was durably cached
    as a business domain (stage 'succeeded'). The domain acceptor must reject a prompt/task echo or
    an internal dotted identifier — treated as failure, so NOT cached (M3) — while domains stay
    open-vocabulary (no controlled list)."""
    from featuregen.overlay.upload.enrich import classify_domains
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]
    client = FakeLLM(script={"overlay.enrich.domain": FakeResponse(output={"domain": echo})})
    assert classify_domains(db, rows, client) == {}                                     # rejected
    assert db.execute("SELECT count(*) FROM enrichment_domain").fetchone()[0] == 0      # NOT cached


def test_prompt_echo_domain_is_rejected_in_single_mode(db, monkeypatch):
    """The single-fallback path must apply the SAME plausibility gate as batch (domain defaults to
    batch, so pin single to exercise the fallback acceptor)."""
    monkeypatch.setenv("OVERLAY_ENRICH_DOMAIN_MODE", "single")
    from featuregen.overlay.upload.enrich import classify_domains
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]
    client = FakeLLM(script={"overlay.enrich.domain":
                             FakeResponse(output={"domain": "overlay.enrich.domain"})})
    assert classify_domains(db, rows, client) == {}
    assert db.execute("SELECT count(*) FROM enrichment_domain").fetchone()[0] == 0


@pytest.mark.parametrize("domain", ["banking_payments_transactions", "Compliance"])
def test_legitimate_open_vocab_domain_is_accepted_and_cached(db, domain):
    """Domains are open-vocabulary: a real business domain (snake_case or a plain word) is still
    accepted and cached — only prompt/task echoes and internal identifiers are filtered out."""
    from featuregen.overlay.upload.enrich import classify_domains
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]
    client = FakeLLM(script={"overlay.enrich.domain": FakeResponse(output={"domain": domain})})
    assert classify_domains(db, rows, client)["accounts"] == domain                     # accepted
    assert db.execute("SELECT count(*) FROM enrichment_domain").fetchone()[0] == 1      # cached


def test_concept_inputs_exclude_free_text_definition(db):
    """M4: the uploader's free-text definition must not be sent to the LLM."""
    captured = {}

    class _Capture:
        def call(self, request):
            captured["inputs"] = dict(request.inputs)
            from featuregen.intake.llm import LLMResult
            return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                             call_ref="", status="ok")

    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric",
                         definition="holder SSN 123-45-6789")]   # PII in free text
    enrich_concepts(db, rows, _Capture())
    from featuregen.intake.redaction import INPUT_KEY_CATALOG
    # Inputs are reserved-keyed; the LLM-visible catalog metadata is names/types + the static
    # classification vocabulary (B1b) only — the uploader's free-text definition (and its PII) is
    # nowhere in the outbound payload.
    catalog = captured["inputs"][INPUT_KEY_CATALOG]
    assert catalog["table"] == "accounts" and catalog["column"] == "bal" and catalog["type"] == "numeric"
    assert "definition" not in catalog                        # the free-text definition is excluded (M4)
    assert set(catalog) == {"table", "column", "type", "vocabulary"}   # nothing else is sent
    assert "123-45-6789" not in str(captured["inputs"])       # the PII never reaches the payload


# ── R5-5: the FTR-declared SQL type reaches the concept classifier ───────────────────────────────

def test_concept_metadata_carries_declared_type_instead_of_unknown():
    """R5-5: the FTR adapter keeps the OPERATIONAL type UNKNOWN_TYPE, but the file's real declared
    SQL type (VARCHAR/DOUBLE/TIMESTAMP) is classifier signal — it must ride the allowlisted `type`
    key, and the useless operational `unknown` token must not be sent in its place."""
    from featuregen.overlay.upload.canonical import UNKNOWN_TYPE
    from featuregen.overlay.upload.enrich import _concept_metadata
    from featuregen.overlay.upload.enrich_llm import _ITEM_META_ALLOWED, _item_egress_ok
    from featuregen.overlay.upload.glossary_reader import GlossaryRecord

    row = CanonicalRow("ftr", "comp_fin_tran", "cust_name", UNKNOWN_TYPE)
    rec = GlossaryRecord(logical_ref="ftr::dpl_eib_compliance.comp_fin_tran.cust_name",
                         term_name="Customer Name", definition="Registered legal name.",
                         declared_type="varchar")
    meta = _concept_metadata(row, rec)
    assert meta["type"] == "varchar"            # the declared type reaches the classifier ...
    assert UNKNOWN_TYPE not in meta.values()    # ... never the operational unknown token
    assert "type" in _ITEM_META_ALLOWED        # rides an allowlisted key ...
    assert _item_egress_ok(meta)               # ... and the whole dict passes the egress filter


def test_concept_metadata_keeps_operational_type_when_no_declared_type():
    """Without a declared type (technical rows; generic glossary records) the operational
    CanonicalRow.type is sent unchanged — byte-for-byte today's behaviour."""
    from featuregen.overlay.upload.enrich import _concept_metadata
    from featuregen.overlay.upload.glossary_reader import GlossaryRecord

    row = CanonicalRow("upl", "accounts", "bal", "numeric")
    rec = GlossaryRecord(logical_ref="upl::public.accounts.bal", term_name="Balance",
                         definition="ledger balance")
    assert _concept_metadata(row, rec)["type"] == "numeric"
    assert _concept_metadata(row, None)["type"] == "numeric"


# ── #3: the concept CACHE key hashes the FULL classifier input, not just the raw row ─────────────

def _rec(**overrides):
    from featuregen.overlay.upload.glossary_reader import GlossaryRecord
    base = dict(logical_ref="ftr::s.comp_fin_tran.txn_ts", term_name="Transaction Time",
                definition="Business timestamp of the transaction.", declared_type="varchar",
                domain="Payments")
    base.update(overrides)
    return GlossaryRecord(**base)


def test_concept_cache_key_changes_with_declared_type():
    """#3: correcting the glossary's declared SQL type changes the classifier's real input, so the
    CACHE key must change (re-classify) while content_hash — the downstream dict key consumed by
    graph/ingest — stays byte-for-byte stable (it never sees the sidecar)."""
    from featuregen.overlay.upload.enrich import concept_cache_key
    row = CanonicalRow("ftr", "comp_fin_tran", "txn_ts", "unknown")
    a, b = _rec(declared_type="varchar"), _rec(declared_type="timestamp")
    assert concept_cache_key(row, a) != concept_cache_key(row, b)   # cache key discriminates
    assert concept_cache_key(row, a) == concept_cache_key(row, a)   # ... and is deterministic
    assert content_hash(row) == content_hash(row)                   # downstream key: sidecar-blind


def test_concept_cache_key_changes_with_term_domain_and_taxonomy():
    from featuregen.overlay.upload.enrich import concept_cache_key
    row = CanonicalRow("ftr", "comp_fin_tran", "txn_ts", "unknown")
    base = concept_cache_key(row, _rec())
    assert concept_cache_key(row, _rec(term_name="Settlement Time")) != base
    assert concept_cache_key(row, _rec(domain="Treasury")) != base
    assert concept_cache_key(row, _rec(bian_path="BIAN/Payments/Execution")) != base
    assert concept_cache_key(row, _rec(fibo_path="FIBO/FND/DateTime")) != base
    assert concept_cache_key(row, _rec(synonyms=("posting time",))) != base


def test_concept_cache_key_handles_technical_rows_without_glossary():
    """A technical CSV has no sidecar (rec=None): the key must still work, be deterministic, and
    differ from a glossary-enriched key for the same physical column."""
    from featuregen.overlay.upload.enrich import concept_cache_key
    row = CanonicalRow("ftr", "comp_fin_tran", "txn_ts", "unknown")
    assert concept_cache_key(row, None) == concept_cache_key(row, None)
    assert concept_cache_key(row, None) != concept_cache_key(row, _rec())


def test_corrected_glossary_metadata_misses_the_stale_concept_cache(db):
    """#3 end-to-end: a re-upload that CORRECTS declared_type must MISS the cache and re-classify
    (the second client's answer wins), while an UNCHANGED re-upload still HITS (client not called)."""
    from featuregen.overlay.upload.glossary_reader import GlossaryUpload
    row = CanonicalRow("ftr", "comp_fin_tran", "txn_ts", "unknown")
    h = content_hash(row)

    def _glossary(declared_type):
        return GlossaryUpload(rows=[row], records=[_rec(declared_type=declared_type)])

    first = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_stock"})})
    assert enrich_concepts(db, [row], first, glossary=_glossary("varchar"))[h] == "monetary_stock"
    # Unchanged re-upload: still a cache HIT — the raising client is never called.
    out = enrich_concepts(db, [row], _NeverCalledLLM(), glossary=_glossary("varchar"))
    assert out[h] == "monetary_stock"
    # Corrected declared_type: the classifier input changed -> MISS -> re-classified, not stale.
    second = FakeLLM(script={_TASK: FakeResponse(output={"concept": "account_identifier"})})
    out2 = enrich_concepts(db, [row], second, glossary=_glossary("timestamp"))
    assert out2[h] == "account_identifier"                 # the fresh classification, keyed for downstream


# ── #6: a cache HIT must (idempotently) repair concept evidence a prior write failed to create ──

def test_cache_hit_repairs_missing_concept_evidence(db, monkeypatch):
    """#6: if the FIRST run's concept field_evidence write failed (contained, fail-soft — see
    `test_evidence_write_failure_is_fail_soft` in test_pass_a_evidence.py), the classification is
    still cached and `graph_node.concept` still gets populated downstream, but with NO supporting
    field_evidence. A prior version of `enrich_concepts` never revisited a cache HIT, so that gap
    was permanent. The SECOND run — a genuine cache HIT (the LLM is never called) — must WRITE the
    missing evidence."""
    import featuregen.overlay.upload.enrich as enrich_mod
    from featuregen.overlay.field_evidence import read_active_field_evidence
    from featuregen.overlay.upload.glossary_reader import GlossaryUpload

    row = CanonicalRow("ftr", "comp_fin_tran", "txn_ts", "unknown")
    rec = _rec()
    glossary = GlossaryUpload(rows=[row], records=[rec])

    def _boom(*a, **k):
        raise RuntimeError("field_evidence store unavailable")

    monkeypatch.setattr(enrich_mod, "record_field_evidence", _boom)
    first = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_stock"})})
    out = enrich_concepts(db, [row], first, glossary=glossary, source_snapshot_id="snap-1")
    assert out[content_hash(row)] == "monetary_stock"                    # enrichment survives (fail-soft)
    assert read_active_field_evidence(db, rec.logical_ref, "concept") == []  # evidence write FAILED

    monkeypatch.undo()   # restore the real record_field_evidence for the repair run
    hit = enrich_concepts(db, [row], _NeverCalledLLM(), glossary=glossary, source_snapshot_id="snap-1")
    assert hit[content_hash(row)] == "monetary_stock"                    # a genuine cache HIT (no LLM call)
    ev = read_active_field_evidence(db, rec.logical_ref, "concept")
    assert len(ev) == 1 and ev[0].proposed_value == "monetary_stock"     # the HIT repaired the evidence


def test_cache_hit_evidence_write_is_idempotent(db):
    """#6 idempotency: repeated cache-hit runs never duplicate the concept evidence row — the
    input_hash reuse check makes re-writing an already-present, unchanged proposal a safe no-op."""
    from featuregen.overlay.field_evidence import read_active_field_evidence
    from featuregen.overlay.upload.glossary_reader import GlossaryUpload

    row = CanonicalRow("ftr", "comp_fin_tran", "txn_ts", "unknown")
    rec = _rec()
    glossary = GlossaryUpload(rows=[row], records=[rec])

    first = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_stock"})})
    enrich_concepts(db, [row], first, glossary=glossary, source_snapshot_id="snap-1")
    assert len(read_active_field_evidence(db, rec.logical_ref, "concept")) == 1

    # Two more cache-hit runs (LLM never called) must not add a second active row.
    enrich_concepts(db, [row], _NeverCalledLLM(), glossary=glossary, source_snapshot_id="snap-1")
    enrich_concepts(db, [row], _NeverCalledLLM(), glossary=glossary, source_snapshot_id="snap-1")
    ev = read_active_field_evidence(db, rec.logical_ref, "concept")
    assert len(ev) == 1 and ev[0].proposed_value == "monetary_stock"


# ── R5-3: a sanitizer-SUPPRESSED definition is never silently LLM-drafted ────────────────────────

def test_suppressed_definition_is_not_llm_drafted(db):
    """R5-3: a definition the sanitizer blanked FAIL-CLOSED (un-strippable sample data survived) is
    NOT 'missing' — silently LLM-drafting it would land generated text in the graph with no
    governance decision. It stays empty; a naturally-missing definition on the same upload still
    drafts."""
    from featuregen.overlay.upload.enrich import draft_definitions
    from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload

    suppressed = CanonicalRow("ftr", "comp_fin_tran", "cust_name", "unknown")
    missing = CanonicalRow("ftr", "comp_fin_tran", "txn_amt", "unknown")
    glossary = GlossaryUpload(rows=[suppressed, missing], records=[
        GlossaryRecord(logical_ref="ftr::dpl_eib_compliance.comp_fin_tran.cust_name",
                       term_name="Customer Name", definition="", definition_suppressed=True),
        GlossaryRecord(logical_ref="ftr::dpl_eib_compliance.comp_fin_tran.txn_amt",
                       term_name="Transaction Amount", definition=""),
    ])
    client = FakeLLM(script={"overlay.enrich.definition":
                             FakeResponse(output={"definition": "an llm draft"})})
    out = draft_definitions(db, [suppressed, missing], client, glossary=glossary)
    assert out[content_hash(missing)] == "an llm draft"   # naturally-missing still drafts
    assert content_hash(suppressed) not in out            # suppressed stays empty — no silent draft


def test_suppressed_definition_hashes_only_flags_suppressed_blank_rows():
    """The shared helper (draft skip + ingest's honest expected count) flags ONLY blank rows whose
    sidecar marks the definition suppressed — declared and naturally-missing rows are untouched."""
    from featuregen.overlay.upload.enrich import suppressed_definition_hashes
    from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload

    suppressed = CanonicalRow("ftr", "t", "a", "unknown")
    missing = CanonicalRow("ftr", "t", "b", "unknown")
    declared = CanonicalRow("ftr", "t", "c", "unknown", definition="kept")
    glossary = GlossaryUpload(rows=[suppressed, missing, declared], records=[
        GlossaryRecord(logical_ref="ftr::s.t.a", term_name="A", definition="",
                       definition_suppressed=True),
        GlossaryRecord(logical_ref="ftr::s.t.b", term_name="B", definition=""),
        GlossaryRecord(logical_ref="ftr::s.t.c", term_name="C", definition="kept"),
    ])
    rows = [suppressed, missing, declared]
    assert suppressed_definition_hashes(rows, glossary) == {content_hash(suppressed)}
    assert suppressed_definition_hashes(rows, None) == set()
