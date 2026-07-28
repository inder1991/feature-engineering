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
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import _summary_targets, draft_summaries
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload


class _EchoLLM:
    """Answers with a summary naming each item's column, so a test can prove WHICH item an answer
    belongs to — the property a batched prose task most easily gets wrong."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def call(self, request):  # noqa: ANN001, ANN201
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
