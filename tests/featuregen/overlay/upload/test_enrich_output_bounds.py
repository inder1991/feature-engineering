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


def test_a_PARAGRAPHED_definition_is_accepted_whole() -> None:
    """The rule this file used to pin the other way round, now relaxed by human decision.

    `_bounded` rejected any value containing a newline (M9). That was fair when the cap was 200-600
    chars — "a definition is one line" describes a 600-char definition. At `MAX_DEFINITION_LEN` =
    32_000 it had become the binding ceiling on ACHIEVABLE length: a model writing a long definition
    paragraphs it, and the whole value was DISCARDED — not shortened, not salvaged to its first
    line — leaving the column with no AI definition at all, indistinguishable from a provider blip.

    Accepted WHOLE is the assertion that matters: not truncated at the first newline, not
    normalised. What the model wrote is what is cached.
    """
    accept = enrich._accept_bounded(llm.MAX_DEFINITION_LEN)
    paragraphed = ("The posted settlement amount for the transaction.\n\n"
                   "It is denominated in the transaction currency and excludes fees, which are "
                   "carried separately.\n\n"
                   "For reversals the sign is inverted rather than a separate row being written.")
    value, reason = accept(paragraphed)
    assert reason == "valid"
    assert value == paragraphed                    # whole, byte-for-byte

    # A long paragraphed definition at the cap survives too — the point of the raise.
    long_prose = ("A paragraph about the column's meaning and derivation.\n\n" * 2000)
    long_prose = long_prose[:llm.MAX_DEFINITION_LEN]
    assert accept(long_prose) == (long_prose, "valid")


def test_a_LIST_SHAPED_answer_is_still_refused() -> None:
    """M9's real intent, kept by a targeted guard instead of a blanket newline ban.

    The earlier justification for keeping the ban — "it protects the list-dump guard" — was wrong:
    `startswith("[")` catches the stringified `['a','b']` form INDEPENDENTLY. What the newline ban
    uniquely caught is a multi-LINE enumeration, and that is exactly what `_is_enumeration` now
    catches on its own.
    """
    accept = enrich._accept_bounded(llm.MAX_DEFINITION_LEN)
    for listy in ("- the account number\n- the branch code\n- the currency",
                  "1. the account number\n2. the branch code",
                  "1) first thing\n2) second thing",
                  "(a) first thing\n(b) second thing",
                  "* alpha\n* beta\n* gamma"):
        assert accept(listy) == (None, "invalid_value"), listy
    # The stringified-list form is refused by its OWN guard, with no help from the newline rule.
    assert accept("['alpha', 'beta']") == (None, "invalid_value")
    assert enrich._is_enumeration("['alpha', 'beta']") is False   # …and not by this one

    # ONE marker-led line is prose, not a list: a sentence may legitimately open with a dash or a
    # numeral, and refusing that would re-create the over-rejection this change exists to remove.
    assert accept("- a single dashed clause about the column") == (
        "- a single dashed clause about the column", "valid")
    assert accept("The balance as of 1. the posting date, per policy.")[1] == "valid"


def test_the_newline_relaxation_did_NOT_reach_the_comma_parsed_gates() -> None:
    """`_bounded` backs five accept gates; the relaxation was for paragraphed DEFINITIONS only.

    Two gates would have been silently damaged by inheriting it, and both keep their pre-relaxation
    behaviour:

      * `synonyms` — the consumer is `ingest._project_semantic_terms`, which does
        `(ev.proposed_value or "").split(",")`, and `_SYN_INSTRUCTION` asks for "ONE comma-separated
        line per item". A newline-separated answer would be cached and projected as ONE term with
        the whole blob as its dedupe key, instead of three terms. Silently WORSE than the rejection
        it replaced, because a rejected item is retried.
      * `domain` — a grouping-key LABEL flowing into `_accept_domain_result` as both the table
        default and the column overrides; a multi-line label mints a malformed group.
    """
    listy = "account number\nacct num\nacc #"

    syn = enrich._accept_single_line(enrich._MAX_SYNONYMS_LEN)
    assert syn(listy) == (None, "invalid_value")
    assert syn("account number, acct num, acc #")[1] == "valid"     # the shape the prompt asks for

    dom = enrich._accept_domain(64)
    assert dom("Compliance\nRetail Banking") == (None, "invalid_value")
    assert dom("Compliance")[1] == "valid"

    # …while the DEFINITION gate, the one the relaxation was for, still takes it.
    assert enrich._accept_bounded(llm.MAX_DEFINITION_LEN)("First para.\n\nSecond para.")[1] == "valid"

    # The consumer's contract is the reason, so pin it rather than describing it: comma is the only
    # separator it knows.
    import inspect

    from featuregen.overlay.upload import ingest
    assert '.split(",")' in inspect.getsource(ingest._project_semantic_terms)


def test_the_prompt_and_the_gate_AGREE_about_shape() -> None:
    """The instruction used to ask for a "one-line" definition, which at a 32_000-char cap invited
    exactly the shape the caps were widened to stop forcing. A model must never be asked for output
    the gate then discards, in either direction."""
    import inspect

    # CODE only: the comments in this function narrate the "one-line" history on purpose, and
    # matching them would make this test unfixable-by-construction.
    code = "\n".join(ln for ln in inspect.getsource(enrich.draft_definitions).splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "one-line" not in code, "the drafting instruction still asks for one line"
    assert code.count("never a bulleted") + code.count("not a bulleted") == 2, (
        "BOTH instructions (batch and single mode) must name the one shape `_is_enumeration` "
        "still refuses — a model must never be asked for output the gate discards")


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
