"""Task 3 — the independent BLIND re-classification signal. Mirrors ``test_enrich.py``'s FakeLLM
convention: no real provider, a task-keyed script drives a fixed structured response."""
from __future__ import annotations

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.attest.reclassify import (
    _TASK,
    ColumnContext,
    reclassify_concept,
)


def test_in_vocab_concept_is_returned(db) -> None:
    """A FAKE client returning a fixed in-vocabulary concept -> reclassify_concept returns it."""
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_amount"})})
    ctx = ColumnContext(name="balance", definition="the account ledger balance")

    result = reclassify_concept(db, client, "src::public.accounts.balance", column_ctx=ctx)

    assert result.value == "monetary_amount"


def test_out_of_vocab_concept_is_rejected_to_none(db) -> None:
    """A FAKE client returning an off-vocabulary string -> rejected to None via the shared
    `_accept_concept` gate (the identical vocabulary contract the proposer's call uses)."""
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "totally_made_up"})})
    ctx = ColumnContext(name="weird", definition=None)

    result = reclassify_concept(db, client, "src::public.accounts.weird", column_ctx=ctx)

    assert result.value is None


def test_genuine_unclassified_is_returned_not_coerced_to_none(db) -> None:
    """The literal 'unclassified' is a real classification (matches enrich.py's contract) — an
    independent reclassifier saying "none fits" must not be flattened into a rejection."""
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "unclassified"})})
    ctx = ColumnContext(name="notes")

    result = reclassify_concept(db, client, "src::public.accounts.notes", column_ctx=ctx)

    assert result.value == "unclassified"


def test_measure_only_writes_no_authority_state(db) -> None:
    """MEASURE-ONLY: the call must write nothing to field_evidence / graph_node — only the audited
    seam's own telemetry (document_schema / llm_call), never an authority-tier row."""
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_amount"})})
    ctx = ColumnContext(name="balance", definition="the account ledger balance",
                        sample_values=("1250.00", "99.50"))

    reclassify_concept(db, client, "src::public.accounts.balance", column_ctx=ctx)

    assert db.execute("SELECT count(*) FROM field_evidence").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM graph_node").fetchone()[0] == 0
