"""An AI summary for EVERY column — the answer to a bucket-filled description column.

CIB ships 111 columns with 47 distinct descriptions. 80% of rows repeat another row's sentence and
one sentence covers 12 columns, so `cust_curr_ntb_flg` (new to bank NOW) and `cust_prev_9mnth_ntb_flg`
(new to bank NINE MONTHS AGO) are described identically. The existing definition drafter cannot help:
it only targets columns with NO declared definition, and these have one — the boilerplate occupies
the field, so the system reads the question as answered.

So the summary is drafted for EVERY column, unconditionally. That removes the need to judge whether a
given description is "good enough", which is a threshold nobody can set correctly.

The two properties that make it safe: it never touches `definition` (the source keeps its own text at
its own authority), and it is written from the metadata rather than replacing it.
"""
from __future__ import annotations

from featuregen.intake.llm import PROVIDER_OK, LLMResult
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.enrich import _summary_targets, draft_summaries
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload


class _EchoLLM:
    """Answers with a summary naming each item's column, so a test can prove WHICH item an answer
    belongs to — the property a batched prose task most easily gets wrong."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def call(self, request):  # noqa: ANN001, ANN201
        # A full ingest drives every enrichment stage through this one client, so only SUMMARY
        # dispatches are recorded — otherwise the concept/definition stages' payloads land here too
        # and `seen` stops meaning "summary calls".
        if request.task != "overlay.enrich.summary":
            return LLMResult(output={"results": []}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_OK)
        items = request.inputs["catalog_metadata"]["items"]
        self.seen.append(items)
        return LLMResult(
            output={"results": [{"ref": i["ref"], "summary": f"summary of {i['column']}"}
                                for i in items]},
            self_reported_scores={}, call_ref="", status=PROVIDER_OK)


def _rows() -> list[CanonicalRow]:
    return [CanonicalRow(source="cib", table="bo_cib_customer", column=c, type="text")
            for c in ("cust_curr_ntb_flg", "cust_prev_9mnth_ntb_flg")]


def _glossary(definition: str) -> GlossaryUpload:
    records = [
        GlossaryRecord(logical_ref=f"cib::bo_dpl_cib.bo_cib_customer.{c}",
                       term_name=t, definition=definition, domain="Customer",
                       declared_type="varchar(5)",
                       source_attributes=("attribute_category: Customer Lifecycle and Status",))
        for c, t in (("cust_curr_ntb_flg", "Customer Current New to Bank Flag"),
                     ("cust_prev_9mnth_ntb_flg", "Customer Previous 9Mnth New to Bank Flag"))
    ]
    return GlossaryUpload(rows=[], records=records)


from datetime import UTC, datetime
_NOW = datetime(2026, 7, 29, tzinfo=UTC)
_ACTOR = IdentityEnvelope(subject="o", actor_kind="human", authenticated=True,
                          auth_method="oidc", role_claims=("data_owner",))

_BOILERPLATE = ("Status or indicator used to classify customer condition, eligibility, servicing "
                "state, control state, or operational processing state for customer lifecycle.")


# ── every column, not just the blank ones ────────────────────────────────────────────────────────

def test_a_column_that_ALREADY_has_a_description_still_gets_a_summary():
    """THE difference from the definition drafter. These columns have a description — a useless one
    shared with eleven others — and the old rule ("only ever fills a blank") skipped them all."""
    rows = _rows()
    assert len(_summary_targets(rows, _glossary(_BOILERPLATE))) == 2


def test_a_column_with_NO_description_also_gets_one():
    rows = _rows()
    assert len(_summary_targets(rows, _glossary(""))) == 2


def test_every_column_is_targeted_regardless_of_description_quality():
    """No threshold, no judgment about whether a description is 'good enough' — a rule nobody can set
    correctly. Every column, always."""
    rows = _rows()
    for definition in (_BOILERPLATE, "", "A genuinely specific and useful description."):
        assert len(_summary_targets(rows, _glossary(definition))) == len(rows)


# ── it writes from the metadata it is given ──────────────────────────────────────────────────────

def test_the_summary_is_written_from_the_FULL_metadata(db):
    """The old definition task sent only {table, column, type, concept}. A summary written from that
    could not tell the NTB pair apart either — term_name is the field that distinguishes them."""
    llm = _EchoLLM()
    draft_summaries(db, _rows(), llm, glossary=_glossary(_BOILERPLATE))
    sent = [i for batch in llm.seen for i in batch]
    assert sent, "nothing was dispatched"
    assert any("term_name" in m for m in sent), "term_name never reached the model"
    assert any("New to Bank" in str(m.get("term_name", "")) for m in sent)


def test_each_column_gets_its_OWN_summary(db):
    out = draft_summaries(db, _rows(), _EchoLLM(), glossary=_glossary(_BOILERPLATE))
    assert len(set(out.values())) == 2, "two columns must not share one summary"


# ── the source's own text is never touched ───────────────────────────────────────────────────────

def test_drafting_a_summary_does_not_change_the_declared_definition(db):
    """The safety property. A source definition keeps its text AND its authority; the summary is a
    separate, always-llm-proposed field."""
    rows = _rows()
    glossary = _glossary(_BOILERPLATE)
    before = [r.definition for r in glossary.records]
    draft_summaries(db, rows, _EchoLLM(), glossary=glossary)
    assert [r.definition for r in glossary.records] == before


# ── cached, so a re-upload does not re-pay ───────────────────────────────────────────────────────

def test_a_second_run_over_unchanged_columns_calls_no_provider(db):
    rows, glossary = _rows(), _glossary(_BOILERPLATE)
    first = _EchoLLM()
    draft_summaries(db, rows, first, glossary=glossary)
    second = _EchoLLM()
    again = draft_summaries(db, rows, second, glossary=glossary)
    assert second.seen == [], "unchanged columns must be served from cache"
    assert len(again) == 2, "the cached summaries must still be returned"


# ── a technical upload must not pay for what it cannot store ─────────────────────────────────────

def test_a_technical_upload_does_not_call_the_provider_for_summaries(db):
    """`_write_summary_evidence` keys on the glossary record's logical_ref, so a technical CSV has
    nowhere to put a summary. It was still calling the provider, reporting `succeeded`, and storing
    nothing — paying for output no one could ever see."""
    from featuregen.overlay.upload.stage_report import StageRecorder

    llm = _EchoLLM()
    rec = StageRecorder()
    ingest_upload(db, "tech", [CanonicalRow(source="tech", table="t", column="c", type="text")],
                  actor=_ACTOR, now=_NOW, client=llm, stage_recorder=rec)
    assert llm.seen == [], "a technical upload dispatched summary calls it cannot store"
    stage = next(r for r in rec.reports if r.stage == "enrich_summary")
    assert stage.state == "not_applicable", stage


# ── a corrected term must not leave yesterday's summary active ───────────────────────────────────

def test_the_evidence_material_is_the_metadata_the_summary_WAS_DRAFTED_FROM(db):
    """`input_hash` decides whether existing evidence is still current. It was keyed on a
    {table, column, type} stub while the summary was drafted from the full metadata — so correcting
    a term_name left the hash unchanged, and a redraft that failed transiently left the OLD summary
    active to be projected again as though it still described the column.

    Since the Step-6c tail move, the ONE payload builder (`summary_payload`) is shared by the
    drafter AND the evidence writer, so the material is the drafting input BY CONSTRUCTION —
    including the ingest tail's enriched extras."""
    import inspect

    from featuregen.overlay.upload import enrich

    for fn in (enrich.draft_summaries, enrich._write_summary_evidence):
        src = inspect.getsource(fn)
        assert "summary_payload(" in src, f"{fn.__name__} does not use the shared payload builder"
        assert '"type": row.type}' not in src, "material is still the stub"


def test_a_changed_term_changes_the_material(db):
    """The behavioural form: two glossaries differing only in term_name must produce different
    evidence material, or the redraft can never be recognised as needed."""
    from featuregen.overlay.upload.enrich import _concept_metadata
    from featuregen.overlay.upload.glossary_reader import GlossaryRecord

    row = CanonicalRow(source="cib", table="t", column="c", type="text")
    before = GlossaryRecord(logical_ref="cib::s.t.c", term_name="Customer Current NTB Flag",
                            definition="Status or indicator.", declared_type="varchar(5)")
    after = GlossaryRecord(logical_ref="cib::s.t.c", term_name="Customer Previous 9Mnth NTB Flag",
                           definition="Status or indicator.", declared_type="varchar(5)")
    assert _concept_metadata(row, before) != _concept_metadata(row, after)


# ── Step 6c: the TAIL re-draft writes from the FULL enriched dossier, not the file alone ─────────


def test_an_enriched_payload_and_a_file_only_payload_produce_different_cache_keys():
    """The auto-redraft property: `_summary_cache_key` hashes the payload, so the richer tail
    payload re-drafts every column BY CONSTRUCTION — no cache surgery."""
    from featuregen.overlay.upload.enrich import _summary_cache_key, summary_payload

    row = _rows()[0]
    rec = _glossary(_BOILERPLATE).records[0]
    file_only = summary_payload(row, rec)
    enriched = summary_payload(row, rec, extras={"concept": "flag", "party_role": "subject"})
    assert _summary_cache_key("h", file_only) != _summary_cache_key("h", enriched)


def test_summary_payload_carries_the_lost_glossary_fields_and_extras():
    from featuregen.overlay.upload.enrich import summary_payload
    from featuregen.overlay.upload.glossary_reader import GlossaryRecord

    rec = GlossaryRecord(
        logical_ref="cib::s.t.c", term_name="Term", definition="A definition.",
        term_type="dimension", process_path="Onboarding > KYC",
        related_terms=("NTB Segment",), declared_type="varchar(5)")
    payload = summary_payload(
        CanonicalRow(source="cib", table="t", column="c", type="text"), rec,
        extras={"concept": "flag", "party_role": "subject", "grain_role": "grain",
                "table_role": "dimension", "ai_synonyms": "new to bank, ntb"})
    for key, val in (("term_type", "dimension"), ("process_path", "Onboarding > KYC"),
                     ("related_terms", ["NTB Segment"]), ("concept", "flag"),
                     ("party_role", "subject"), ("grain_role", "grain"),
                     ("table_role", "dimension"), ("ai_synonyms", "new to bank, ntb")):
        assert payload[key] == val, key
    # an empty extra is never fabricated into the payload
    assert "data_domain" not in summary_payload(
        CanonicalRow(source="cib", table="t", column="c", type="text"), rec,
        extras={"data_domain": ""})


def test_the_ingest_tail_drafts_from_the_enriched_dossier(db):
    """The full-loop property (the plan's must-die mutation: 'draft the summary from file-only
    metadata again'). A real ingest's summary dispatch carries the enriched dossier fields —
    party_role can ONLY come from the tail axis projection, term_type/process_path only from the
    sidecar's lost fields — and exactly ONE summary draft happens per ingest."""
    llm = _EchoLLM()
    rows = [CanonicalRow(source="cib", table="bo_cib_customer", column="cust_swift_cd",
                         type="text")]
    records = [GlossaryRecord(
        logical_ref="cib::bo_dpl_cib.bo_cib_customer.cust_swift_cd",
        term_name="Customer SWIFT Code", definition="The customer's own bank BIC.",
        domain="Customer", declared_type="varchar(11)", term_type="dimension",
        process_path="Payments > Routing", related_terms=("Sender BIC",))]
    ingest_upload(db, "cib", rows, actor=_ACTOR, now=_NOW, client=llm,
                  glossary=GlossaryUpload(rows=rows, records=records))
    sent = [i for batch in llm.seen for i in batch]
    assert len(sent) == 1, "exactly one summary draft per ingest"
    item = sent[0]
    assert item["party_role"] == "subject"                  # tail-only: the axis projection wrote it
    assert item["term_type"] == "dimension"
    assert item["process_path"] == "Payments > Routing"
    assert item["related_terms"] == ["Sender BIC"]


def test_the_tail_draft_never_overwrites_the_definition(db):
    llm = _EchoLLM()
    rows = _rows()
    glossary = _glossary(_BOILERPLATE)
    glossary = GlossaryUpload(rows=rows, records=glossary.records)
    ingest_upload(db, "cib", rows, actor=_ACTOR, now=_NOW, client=llm, glossary=glossary)
    stored = dict(db.execute(
        "SELECT object_ref, definition FROM graph_node"
        " WHERE catalog_source = 'cib' AND kind = 'column'").fetchall())
    assert set(stored.values()) == {_BOILERPLATE}, "the declared definition must survive the draft"
