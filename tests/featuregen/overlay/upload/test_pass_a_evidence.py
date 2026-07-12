"""Task 6: Pass A rich glossary context reaching the LLM (required) + item-level concept evidence.

Two guarantees:
  A) a glossary column's concept-enrichment input carries the FULL business sidecar (term, business
     definition, synonyms/aliases, data domain, BIAN + FIBO paths) — not just table/column/type; and
  B) batch-mode concept enrichment writes one `field_evidence` proposal per classified, attachable
     glossary column (producer=llm, strength=proposed), respecting the C3 no-evidence-for-unclassified
     policy, keyed by the schema-preserving `normalize_ref`.

The non-glossary path must stay byte-for-byte unchanged (guarded), so a bare `enrich_concepts` call
sends no glossary keys and writes no evidence.
"""
from __future__ import annotations

import featuregen.overlay.upload.enrich as enrich_mod
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.intake.redaction import INPUT_KEY_CATALOG
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import _vocab_fingerprint, content_hash, enrich_concepts
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.upload_identity import classify_upload

_TASK = "overlay.enrich.concept"

# An FTR-shaped glossary CSV (spec §U): schema-qualified physical FQN + business meaning, no physical
# type. Two COLUMN terms — one carries a rich sidecar (synonyms + BIAN + FIBO), one is simpler.
_GLOSSARY_CSV = (
    "physical_name,business_term,description_business_definition,data_domain,"
    "synonyms,bian_path,fibo_path\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY.CUST_NAME,Customer Name,"
    "The legal name of the customer as registered.,Party,"
    "Client Name;Account Holder,Party/Customer,fibo-be-le-lp:LegalPerson\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY.ACCT_BAL,Account Balance,"
    "The ledger balance of the account.,Deposits,"
    "Balance,Product/CurrentAccount,fibo-fbc:Balance\n"
)

# The schema-preserving refs the sidecar (and thus field_evidence) is keyed by. normalize_ref
# lower-cases every component, so the physical FQN's casing folds away.
_NAME_REF = normalize_ref("ftr", "DPL_EIB_COMPLIANCE", "COMP_REPOS_DLY", "CUST_NAME")
_BAL_REF = normalize_ref("ftr", "DPL_EIB_COMPLIANCE", "COMP_REPOS_DLY", "ACCT_BAL")


class _CapturingFake(FakeLLM):
    """FakeLLM that records the last request so a test can assert the outbound payload."""

    def call(self, request):
        self.last = request
        return super().call(request)


def _rows_by_col(upload):
    return {r.column: r for r in upload.rows}


def test_rich_glossary_context_reaches_the_llm(db, monkeypatch):
    """REQUIRED (must-prove #2): the concept-enrichment input for a glossary column carries the
    business definition, term, synonyms/aliases, data domain, and BIAN/FIBO paths."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload = read_glossary(_GLOSSARY_CSV, source="ftr")
    bindings, _ = classify_upload(upload.rows)
    rows = _rows_by_col(upload)
    h_name = content_hash(rows["CUST_NAME"])
    h_bal = content_hash(rows["ACCT_BAL"])
    client = _CapturingFake(script={_TASK: FakeResponse(output={"results": [
        {"ref": h_name, "concept": "account_identifier"},
        {"ref": h_bal, "concept": "monetary_stock"}]})})

    enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                    source_snapshot_id="snap-1")

    items = {it["ref"]: it for it in client.last.inputs[INPUT_KEY_CATALOG]["items"]}
    name = items[h_name]
    # Structural metadata is still present...
    assert name["table"] == "COMP_REPOS_DLY" and name["column"] == "CUST_NAME"
    # ...AND the full business sidecar rides along (under `business_definition`, not the plain
    # `definition` key which stays egress-forbidden for technical uploads).
    assert name["business_definition"].startswith("The legal name of the customer")
    assert name["term_name"] == "Customer Name"
    assert set(name["synonyms"]) == {"Client Name", "Account Holder"}
    assert name["data_domain"] == "Party"
    assert name["bian_path"] == "Party/Customer"
    assert name["fibo_path"] == "fibo-be-le-lp:LegalPerson"


def test_batch_writes_item_level_concept_evidence(db, monkeypatch):
    """B: each classified glossary column writes a concept field_evidence proposal keyed by the
    schema-preserving normalize_ref, with the producer/strength/refs/config-hash the spec requires."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload = read_glossary(_GLOSSARY_CSV, source="ftr")
    bindings, _ = classify_upload(upload.rows)
    rows = _rows_by_col(upload)
    h_name = content_hash(rows["CUST_NAME"])
    h_bal = content_hash(rows["ACCT_BAL"])
    client = FakeLLM(script={_TASK: FakeResponse(output={"results": [
        {"ref": h_name, "concept": "account_identifier"},
        {"ref": h_bal, "concept": "monetary_stock"}]})})

    out = enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                          source_snapshot_id="snap-1")
    # Return shape unchanged (content_hash -> concept), so build_graph is unaffected.
    assert out == {h_name: "account_identifier", h_bal: "monetary_stock"}

    ev = read_active_field_evidence(db, _NAME_REF, "concept")
    assert len(ev) == 1
    e = ev[0]
    assert e.proposed_value == "account_identifier"
    assert e.producer == "llm" and e.strength == "proposed" and e.lifecycle == "active"
    assert e.producer_ref == "overlay-enrichment"
    assert e.producer_item_ref == h_name                       # the batch item ref (content hash)
    assert e.producer_configuration_hash == _vocab_fingerprint()
    assert e.source_snapshot_id == "snap-1"
    assert e.input_hash                                         # a per-field input hash is recorded
    # both classified columns got a proposal
    assert len(read_active_field_evidence(db, _BAL_REF, "concept")) == 1


def test_no_evidence_for_unclassified(db, monkeypatch):
    """C3: an 'unclassified' column is a valid classification but NOT a proposal — no evidence row."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload = read_glossary(_GLOSSARY_CSV, source="ftr")
    bindings, _ = classify_upload(upload.rows)
    rows = _rows_by_col(upload)
    h_name = content_hash(rows["CUST_NAME"])
    h_bal = content_hash(rows["ACCT_BAL"])
    client = FakeLLM(script={_TASK: FakeResponse(output={"results": [
        {"ref": h_name, "concept": "unclassified"},            # -> no evidence (C3)
        {"ref": h_bal, "concept": "monetary_stock"}]})})

    out = enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                          source_snapshot_id="snap-1")
    assert out[h_name] == "unclassified"                       # still returned + cached
    assert read_active_field_evidence(db, _NAME_REF, "concept") == []     # but no evidence
    assert len(read_active_field_evidence(db, _BAL_REF, "concept")) == 1


def test_non_glossary_upload_is_unchanged(db, monkeypatch):
    """Guard: a bare (non-glossary) batch call sends only structural metadata and writes no evidence."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric",
                         definition="holder SSN 123-45-6789")]   # free text with PII
    h = content_hash(rows[0])
    client = _CapturingFake(script={_TASK: FakeResponse(output={"results": [
        {"ref": h, "concept": "monetary_stock"}]})})

    out = enrich_concepts(db, rows, client)                    # no glossary / bindings / snapshot
    assert out == {h: "monetary_stock"}
    item = client.last.inputs[INPUT_KEY_CATALOG]["items"][0]
    assert set(item) == {"ref", "table", "column", "type"}     # no glossary keys, no free-text definition
    assert "123-45-6789" not in str(client.last.inputs)        # PII never egresses (M4)
    n = db.execute("SELECT count(*) FROM field_evidence WHERE producer = 'llm'").fetchone()[0]
    assert n == 0                                              # no evidence for a non-glossary upload


# ── Whole-branch review CRITICAL (data leak): a glossary business definition EMBEDS raw customer
# sample values in prose; those must NEVER egress to the LLM under `business_definition`. The concept
# payload builder sanitizes the definition (strip_sample_values) before it egresses. These values
# (a decimal, a time, an 8-digit code) bypass the redaction PII backstop entirely — proving the leak
# is real and the sanitizer, not the backstop, is what contains it. ──
_LEAKY_GLOSSARY_CSV = (
    "physical_name,business_term,description_business_definition,data_domain,"
    "synonyms,bian_path,fibo_path\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY.POST_AMT,Posting Amount,"
    "Financial transaction record amount for the customer. The sample profile is NUMERIC with "
    "representative values such as 84848368; 1250.00; 15:07:08, which supports interpretation.,"
    "Deposits,Amount,Product/CurrentAccount,fibo-fbc:MonetaryAmount\n"
)
_AMT_REF = normalize_ref("ftr", "DPL_EIB_COMPLIANCE", "COMP_REPOS_DLY", "POST_AMT")


def test_concept_enrichment_does_not_egress_raw_sample_values(db, monkeypatch):
    """CRITICAL: the raw sample values embedded in the glossary definition do NOT reach the LLM, yet
    the business meaning still rides through under `business_definition`."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload = read_glossary(_LEAKY_GLOSSARY_CSV, source="ftr")
    bindings, _ = classify_upload(upload.rows)
    rows = _rows_by_col(upload)
    h_amt = content_hash(rows["POST_AMT"])
    client = _CapturingFake(script={_TASK: FakeResponse(output={"results": [
        {"ref": h_amt, "concept": "monetary_stock"}]})})

    enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                    source_snapshot_id="snap-1")

    payload = str(client.last.inputs)
    for value in ("84848368", "1250.00", "15:07:08"):
        assert value not in payload            # raw customer sample values never egress
    item = {it["ref"]: it for it in client.last.inputs[INPUT_KEY_CATALOG]["items"]}[h_amt]
    bd = item["business_definition"]
    assert "financial transaction record" in bd.lower()   # business meaning survives...
    assert "customer" in bd.lower()
    assert "84848368" not in bd and "1250.00" not in bd    # ...without the raw values


def test_evidence_write_failure_is_fail_soft(db, monkeypatch):
    """A field_evidence write failure logs and is contained — enrichment still returns its concepts."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload = read_glossary(_GLOSSARY_CSV, source="ftr")
    bindings, _ = classify_upload(upload.rows)
    rows = _rows_by_col(upload)
    h_name = content_hash(rows["CUST_NAME"])
    h_bal = content_hash(rows["ACCT_BAL"])
    client = FakeLLM(script={_TASK: FakeResponse(output={"results": [
        {"ref": h_name, "concept": "account_identifier"},
        {"ref": h_bal, "concept": "monetary_stock"}]})})

    def _boom(*a, **k):
        raise RuntimeError("field_evidence store unavailable")

    monkeypatch.setattr(enrich_mod, "record_field_evidence", _boom)
    out = enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                          source_snapshot_id="snap-1")
    assert out == {h_name: "account_identifier", h_bal: "monetary_stock"}   # enrichment survives
    # the contained failure left no half-written evidence AND did not poison the txn
    assert read_active_field_evidence(db, _NAME_REF, "concept") == []
    assert db.execute("SELECT 1").fetchone()[0] == 1                        # txn still usable
