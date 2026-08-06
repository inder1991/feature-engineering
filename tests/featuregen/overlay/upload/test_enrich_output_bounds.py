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


#: The deployed response ceiling and the driver's escalation of it. Read from the code and the
#: manifest rather than restated, because the whole point is that these three interact.
def _response_ceilings() -> tuple[int, int]:
    import pathlib

    import yaml

    from featuregen.intake.llm import _MAX_TOKENS_CEILING, _TRUNCATION_ESCALATION

    root = pathlib.Path(__file__).resolve().parents[4]
    docs = yaml.safe_load_all((root / "deploy" / "kind" / "k8s" / "20-backend.yaml")
                              .read_text(encoding="utf-8"))
    cfg = next(d for d in docs if d.get("kind") == "ConfigMap")["data"]
    deployed = int(cfg["FEATUREGEN_LLM_MAX_TOKENS"])
    return deployed, min(int(deployed * _TRUNCATION_ESCALATION), _MAX_TOKENS_CEILING)


def test_the_definition_chunk_fits_the_RESPONSE_ceiling_not_just_the_request_budget() -> None:
    """THE reason `_DEFAULT_MAX_ITEMS["definition"]` went 8 -> 4, and the one bound nothing else in
    `enrich_config` models.

    Every other budget in this subsystem is about the REQUEST (`chunk_items` packs on
    `estimate_tokens`, which measures item metadata). This is about the RESPONSE. A definition at
    `MAX_DEFINITION_LEN` is ~8_000 output tokens, so a chunk's output cost is `max_items x 8_000`,
    and it meets `FEATUREGEN_LLM_MAX_TOKENS` — which the driver may escalate exactly ONCE
    (`_TRUNCATION_ESCALATION`, clamped at `_MAX_TOKENS_CEILING`).

    At 8 items that is ~64_000 tokens against a 64_000 escalated ceiling: AT the wall, with the
    retry budget already spent — a chunk of full-length definitions could never complete. At 4 it is
    ~32_000, which the escalation clears with 2x headroom. This asserts the headroom exists, so
    raising `max_items` back without moving `FEATUREGEN_LLM_MAX_TOKENS` fails here rather than in a
    live run.
    """
    from featuregen.overlay.upload import enrich_config

    deployed, escalated = _response_ceilings()
    per_definition = llm.MAX_DEFINITION_LEN // 4          # the estimator's own chars-per-token unit
    chunk_output = enrich_config.max_items("definition") * per_definition

    assert chunk_output <= escalated, (
        f"{enrich_config.max_items('definition')} definitions at the cap need {chunk_output} output "
        f"tokens against an escalated ceiling of {escalated} — the chunk could never complete")
    # Not merely "fits": it must fit with room, because 4-chars-per-token UNDERSTATES real
    # tokenisation and the JSON wrapper (refs, braces, escaping) rides on top.
    assert chunk_output <= escalated // 2, (
        f"{chunk_output} of {escalated} leaves no margin for the response wrapper")
    # The 8-item shape this replaced is recorded as the thing that does NOT fit.
    assert 8 * per_definition > escalated // 2


def test_the_summary_chunk_has_no_response_ceiling_problem() -> None:
    """Checked rather than assumed when `definition` moved: `summary` keeps 8 because its accept
    bound is 1000 chars (~250 output tokens), so a full chunk is ~2_000 tokens — a few percent of
    the deployed ceiling. The two stages are bound by different constraints and must not be
    "made consistent" with each other."""
    from featuregen.overlay.upload import enrich_config

    deployed, _escalated = _response_ceilings()
    chunk_output = enrich_config.max_items("summary") * (enrich._MAX_SUMMARY_LEN // 4)
    assert chunk_output * 10 < deployed, (
        f"summary now costs {chunk_output} output tokens per chunk against {deployed} — it has "
        f"acquired the problem that moved `definition` to 4")


def test_the_REMAINING_ceiling_on_a_long_definition_is_the_SINGLE_LINE_rule() -> None:
    """The bound that still stops a 32_000-char definition short, documented rather than discovered.

    `_bounded` rejects on `"\\n" in val` (M9 — it also catches a list-stringified `['a','b']` dump).
    That rule was written when the cap was 200-600 chars, where "a definition is one line" is a fair
    description. At 32_000 it is the effective ceiling on ACHIEVABLE length: a model writing a long
    definition will paragraph it, and the whole value is then DISCARDED — not shortened, not
    salvaged to its first line. The prompt does say "one-line", so this is consistent; it is
    recorded here because "the cap is 32_000" and "you can get 32_000 chars back" are different
    claims, and only the first is true without qualification.

    NOT changed here: relaxing it is a real decision about what a malformed definition looks like,
    not a mechanical raise, and it would weaken the list-dump guard in the same stroke.
    """
    accept = enrich._accept_bounded(llm.MAX_DEFINITION_LEN)
    one_line = "A single unbroken sentence. " * 100
    assert accept(one_line[:llm.MAX_DEFINITION_LEN])[1] == "valid"
    # The same text, paragraphed, is refused OUTRIGHT — length is not what decides it.
    assert accept("First paragraph.\nSecond paragraph.") == (None, "invalid_value")


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
