"""What the model may WRITE BACK — the INBOUND half of the zero-truncation work.

Task 4b raised `enrich_llm.MAX_DEFINITION_LEN` 600 -> 32_000 and called the job done. It was half
done. That constant governs the OUTBOUND side (`_MAX_LEN_BY_KEY` -> `_item_len_ok`, the egress cap on
what we SEND). What a drafted definition may be coming BACK was capped by two other things entirely:

  * `enrich.draft_definitions`' `accept=_accept_bounded(500)` — **below even the original 600**, and
  * `_SCHEMAS[("overlay_definition_batch", 1)]`'s `definition.maxLength: 500`.

So the model was still being refused a long definition. Worse, the two halves had silently SPLIT on
two other stages: `_MAX_SYNONYMS_LEN` had moved 200 -> 1000 and `_MAX_UNIT_LEN` 32 -> 64 while their
schemas stayed at 200 and 32.

THE TWO HALVES FAIL DIFFERENTLY, which is why the split matters and why they are pinned equal here:

  * the CODE gate (`_accept_bounded`) is PER ITEM — one over-long answer is dropped, its siblings in
    the chunk resolve normally;
  * the SCHEMA `maxLength` is validated against the whole response — one over-long answer fails the
    WHOLE CHUNK, taking every sibling's good answer with it (the note beside `overlay_summary_batch`
    says exactly this).

A schema bound BELOW its code gate therefore converts a bounded per-item drop into a chunk-wide
failure, silently, for the one answer that was too long.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import enrich_llm as llm


def _schema_bound(schema_id: str, field: str) -> int:
    props = llm._SCHEMAS[(schema_id, 1)]["properties"]["results"]["items"]["properties"]
    return props[field]["maxLength"]


#: (schema_id, response field, the code-side accept bound it MUST equal).
_PAIRS = [
    ("overlay_definition_batch", "definition", llm.MAX_DEFINITION_LEN),
    ("overlay_summary_batch", "summary", enrich._MAX_SUMMARY_LEN),
    ("overlay_synonyms_batch", "synonyms", enrich._MAX_SYNONYMS_LEN),
    ("overlay_unit_batch", "unit", enrich._MAX_UNIT_LEN),
]


@pytest.mark.parametrize(("schema_id", "field", "code_bound"), _PAIRS)
def test_the_schema_bound_equals_the_code_accept_bound(schema_id, field, code_bound) -> None:
    """The guard for the whole class. A schema bound under its code gate turns a per-item drop into
    a chunk-wide failure; a schema bound over it is merely dead. Neither may drift."""
    assert _schema_bound(schema_id, field) == code_bound, schema_id


def test_a_definition_may_now_be_written_at_the_full_definition_length() -> None:
    """The actual fix: the accept gate no longer refuses a long drafted definition.

    At the shipped 500 this was the state of the world — the model could be SHOWN a 32_000-char
    definition and refused permission to write one a fifth of the original 600 cap.
    """
    accept = enrich._accept_bounded(llm.MAX_DEFINITION_LEN)
    at_cap = "A drafted business definition. " * 2000
    at_cap = at_cap[:llm.MAX_DEFINITION_LEN]
    value, reason = accept(at_cap)
    assert reason == "valid" and value == at_cap
    # …and it is still a BOUND, not an absence of one.
    over, reason_over = accept("x" * (llm.MAX_DEFINITION_LEN + 1))
    assert over is None and reason_over == "invalid_value"
    # The old bound would have refused a value the new one accepts — the regression this test exists
    # to catch is someone restoring it.
    assert enrich._accept_bounded(500)(at_cap) == (None, "invalid_value")


def test_the_summary_bound_stays_below_the_definition_bound_and_at_its_egress_cap() -> None:
    """`summary` was deliberately NOT given `MAX_DEFINITION_LEN`. It is one sentence by instruction,
    and `ai_summary` is graded under `_MAX_LEN_DEFAULT` on egress — so accepting above that would
    mint a value the egress gate later EXCLUDES, the inversion documented at `_MAX_PROFILE_PROSE`."""
    assert enrich._MAX_SUMMARY_LEN == llm._MAX_LEN_DEFAULT < llm.MAX_DEFINITION_LEN
    assert llm._max_len_for("ai_summary") == enrich._MAX_SUMMARY_LEN


def test_an_accepted_definition_survives_every_consumer_at_the_new_length() -> None:
    """Traced before raising: nothing downstream re-bounds an accepted definition BELOW the gate.

    Storage is `enrichment_definition.definition text` and `field_evidence.proposed_value jsonb`
    (neither length-constrained), and the read path re-bounds at `MAX_DEFINITION_LEN` itself. A
    consumer that clipped lower would make the raise cosmetic.
    """
    from featuregen.overlay.upload.column_view import _bounded
    at_cap = ("Sentence about the column. " * 2000)[:llm.MAX_DEFINITION_LEN]
    assert len(_bounded(at_cap)) == llm.MAX_DEFINITION_LEN     # column_view does not clip it
    assert llm._item_egress_ok({"table": "t", "column": "c", "business_definition": at_cap})
