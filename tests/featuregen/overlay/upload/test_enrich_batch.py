from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import enrich_batch as eb
from featuregen.overlay.upload import enrich_config as cfg
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash, enrich_concepts
from featuregen.overlay.upload.enrich_llm import audited_batch_call


def test_mode_defaults_single_and_reads_env(monkeypatch):
    assert cfg.mode("concept") == "single"
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    assert cfg.mode("concept") == "batch"


def test_max_items_default_and_override(monkeypatch):
    assert cfg.max_items("concept") == 40
    assert cfg.max_items("definition") == 12
    assert cfg.max_items("domain") == 20
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "16")
    assert cfg.max_items("concept") == 16


def test_budget_defaults(monkeypatch):
    b = cfg.budget("definition")
    assert b.max_batch_attempts == 2 and b.max_single_fallback == 8 and b.min_split == 4
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "3")
    assert cfg.budget("definition").max_single_fallback == 3


def test_cache_is_version_scoped(db):
    row = CanonicalRow("deposits", "accounts", "balance", "numeric")
    h = content_hash(row)
    enrich._cache_put(db, "enrichment_concept", h, "monetary_stock", "vA")
    assert enrich._cache_get(db, "enrichment_concept", [h], "vA") == {h: "monetary_stock"}
    # A different cache_version does NOT see the vA entry -> forces recompute (spec C6).
    assert enrich._cache_get(db, "enrichment_concept", [h], "vB") == {}


def test_vocab_fingerprint_is_stable_and_short():
    fp = enrich._vocab_fingerprint()
    assert len(fp) == 12 and fp == enrich._vocab_fingerprint()


def _accept_known(raw):
    known = {"monetary_stock", "unclassified"}
    if raw == "unclassified":
        return "unclassified", "valid"
    return (raw, "valid") if raw in known else (None, "invalid_value")


def test_validate_classifies_every_return():
    items = [eb.BatchItem("r1", {}), eb.BatchItem("r2", {}), eb.BatchItem("r3", {})]
    results = [
        {"ref": "r1", "concept": "monetary_stock"},   # valid
        {"ref": "r2", "concept": "made_up"},           # invalid_value -> not cacheable
        {"ref": "r2", "concept": "monetary_stock"},    # duplicate ref
        {"ref": "rX", "concept": "monetary_stock"},    # extra (not requested)
        {"ref": "r4", "concept": ""},                  # extra (not requested) + blank value
    ]
    # A ref may yield multiple outcomes (primary + duplicate); collapse to each ref's PRIMARY
    # classification (duplicates are asserted distinctly below). Plain last-wins would let the
    # trailing DUPLICATE(r2) shadow its INVALID(r2) primary.
    out = {o.ref: o for o in eb.validate_batch_results(items, results, "concept", _accept_known)
           if o.status != eb.DUPLICATE}
    assert out["r1"].status == eb.VALID and out["r1"].value == "monetary_stock"
    assert out["r2"].status == eb.INVALID and out["r2"].value is None
    assert out["rX"].status == eb.EXTRA
    assert out["r3"].status == eb.MISSING   # never returned
    # the second r2 entry is a duplicate; recorded distinctly
    dups = [o for o in eb.validate_batch_results(items, results, "concept", _accept_known)
            if o.status == eb.DUPLICATE]
    assert len(dups) == 1 and dups[0].ref == "r2"


_CTASK = "overlay.enrich.concept"


def test_audited_batch_call_returns_per_item_outcomes(db):
    items = [eb.BatchItem("h1", {"table": "accounts", "column": "balance", "type": "numeric"}),
             eb.BatchItem("h2", {"table": "accounts", "column": "mystery", "type": "text"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"},
        {"ref": "h2", "concept": "made_up"}]})})
    res = audited_batch_call(db, client, task=_CTASK, prompt_id="overlay_concept_batch_v1",
                             schema_id="overlay_concept_batch",
                             shared_metadata={"vocabulary": [{"name": "monetary_stock"}]},
                             items=items, out_key="concept", instruction="Classify each column.",
                             accept=_accept_known)
    by = {o.ref: o for o in res.outcomes}
    assert by["h1"].status == eb.VALID and by["h1"].value == "monetary_stock"
    assert by["h2"].status == eb.INVALID
    assert res.provider_calls == 1
    # one immutable llm_call row was written for the batch (item summary in cost_metadata)
    n = db.execute("SELECT count(*) FROM llm_call WHERE task = %s", (_CTASK,)).fetchone()[0]
    assert n == 1


def test_audited_batch_call_excludes_unsafe_item_before_egress(db):
    # An item whose metadata carries a disallowed key (free-text definition) is excluded, audited,
    # and the remainder still batched (spec C9 exclude-and-proceed).
    items = [eb.BatchItem("h1", {"table": "accounts", "column": "balance", "type": "numeric"}),
             eb.BatchItem("h2", {"table": "accounts", "column": "ssn", "type": "text",
                                 "definition": "customer social security number"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"}]})})
    res = audited_batch_call(db, client, task=_CTASK, prompt_id="overlay_concept_batch_v1",
                             schema_id="overlay_concept_batch", shared_metadata={},
                             items=items, out_key="concept", instruction="Classify each column.",
                             accept=_accept_known)
    by = {o.ref: o for o in res.outcomes}
    assert by["h2"].status == eb.EGRESS
    assert by["h1"].status == eb.VALID


def test_chunk_respects_item_count():
    items = [eb.BatchItem(f"r{i}", {"column": "c"}) for i in range(25)]
    chunks = eb.chunk_items(items, max_items=10, max_input_tokens=10_000)
    assert [len(c) for c in chunks] == [10, 10, 5]


def test_chunk_respects_token_budget():
    big = "x" * 400   # ~100 tokens each
    items = [eb.BatchItem(f"r{i}", {"column": big}) for i in range(10)]
    chunks = eb.chunk_items(items, max_items=100, max_input_tokens=250)
    assert all(len(c) <= 3 for c in chunks) and sum(len(c) for c in chunks) == 10


def test_chunk_never_drops_an_oversized_singleton():
    items = [eb.BatchItem("r0", {"column": "x" * 10_000})]
    chunks = eb.chunk_items(items, max_items=10, max_input_tokens=10)
    assert chunks == [[items[0]]]   # one item always survives as its own chunk


def test_run_batched_salvages_valid_and_leaves_invalid_uncached(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    items = [eb.BatchItem("h1", {"table": "t", "column": "balance", "type": "numeric"}),
             eb.BatchItem("h2", {"table": "t", "column": "mystery", "type": "text"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"},
        {"ref": "h2", "concept": "made_up"}]})})
    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None)
    assert got == {"h1": "monetary_stock"}     # invalid h2 not returned, not cached (spec C3/C4)


def test_run_batched_falls_back_to_single_for_missing(db, monkeypatch):
    # Batch omits h2 entirely (missing); bounded single fallback recovers it.
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    items = [eb.BatchItem("h1", {"table": "t", "column": "balance", "type": "numeric"}),
             eb.BatchItem("h2", {"table": "t", "column": "bal2", "type": "numeric"})]
    client = FakeLLM(script={_CTASK: [
        FakeResponse(output={"results": [{"ref": "h1", "concept": "monetary_stock"}]}),  # batch: h2 missing
        FakeResponse(output={"concept": "monetary_stock"})]})                            # single fallback for h2
    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None)
    assert got == {"h1": "monetary_stock", "h2": "monetary_stock"}


def test_run_batched_respects_single_fallback_cap(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")   # no fallback allowed
    items = [eb.BatchItem("h1", {"table": "t", "column": "c", "type": "text"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": []})})   # batch returns nothing
    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None)
    assert got == {}   # unresolved, left uncached (retried next ingest)


def test_enrich_concepts_batch_mode_caches_valid_only(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric"),
            CanonicalRow("deposits", "accounts", "mystery", "text")]
    h0, h1 = content_hash(rows[0]), content_hash(rows[1])
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(output={"results": [
        {"ref": h0, "concept": "monetary_stock"},
        {"ref": h1, "concept": "totally_made_up"}]})})
    out = enrich_concepts(db, rows, client)
    assert out == {h0: "monetary_stock"}       # invalid concept NOT cached as UNCLASSIFIED (C3)
    # a second batch run for the same rows hits the cache for h0 (no call needed for it)
    cached = enrich_concepts(db, rows, FakeLLM(script={"overlay.enrich.concept": FakeResponse(
        output={"results": [{"ref": h1, "concept": "unclassified"}]})}))
    assert cached[h0] == "monetary_stock" and cached[h1] == "unclassified"


def test_enrich_concepts_single_mode_unchanged(db, monkeypatch):
    monkeypatch.delenv("OVERLAY_ENRICH_CONCEPT_MODE", raising=False)   # default single
    rows = [CanonicalRow("deposits", "accounts", "weird", "text")]
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(output={"concept": "totally_made_up"})})
    out = enrich_concepts(db, rows, client)
    assert out[content_hash(rows[0])] == "unclassified"   # single keeps today's coerce behaviour


from featuregen.overlay.upload.enrich import classify_domains


def test_classify_domains_batch_mode(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_DOMAIN_MODE", "batch")
    rows = [CanonicalRow("deposits", "accounts", "id", "integer"),
            CanonicalRow("deposits", "loans", "principal", "numeric")]
    client = FakeLLM(script={"overlay.enrich.domain": FakeResponse(output={"results": [
        {"ref": "accounts", "domain": "Deposits"},
        {"ref": "loans", "domain": "Lending"}]})})
    out = classify_domains(db, rows, client)
    assert out == {"accounts": "Deposits", "loans": "Lending"}


from featuregen.overlay.upload.enrich import draft_definitions


def test_draft_definitions_batch_grouped_by_table(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_DEFINITION_MODE", "batch")
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric"),                    # blank -> drafted
            CanonicalRow("deposits", "accounts", "id", "integer", definition="acct id")]  # declared -> skipped
    h0 = content_hash(rows[0])
    client = FakeLLM(script={"overlay.enrich.definition": FakeResponse(output={"results": [
        {"ref": h0, "definition": "the account ledger balance"}]})})
    out = draft_definitions(db, rows, client, concepts={h0: "monetary_stock"})
    assert out == {h0: "the account ledger balance"}
    assert content_hash(rows[1]) not in out          # declared definition never overwritten (R3)


def test_definition_cache_key_includes_concept(db):
    from featuregen.overlay.upload.enrich import _def_cache_key
    row = CanonicalRow("deposits", "accounts", "bal", "numeric")
    assert _def_cache_key(content_hash(row), "monetary_stock") != _def_cache_key(content_hash(row), "")
