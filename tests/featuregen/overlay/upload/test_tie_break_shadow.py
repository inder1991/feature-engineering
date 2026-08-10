"""TASK 2 — the tie-break consult in grounding: SHADOW first, flag-gated binding second.

Phase A (shadow, always on): when a need's top score is a genuine tie, grounding LOOKS UP the
warmed verdict and COUNTS agreement vs disagreement with the alphabetical choice — changing
nothing. The disagreement counter is the SME's review feed and the flag's evidence base.

Phase B (the flag, default OFF): `FEATUREGEN_TIE_BREAK_BINDING=1` makes the verdict's first ref the
binding. Flag-off is byte-identical to today — the platform's own rollout pattern — and a missing
verdict keeps the deterministic order under either flag state: the model was consulted at ingest or
not at all; requests never dispatch.

The fixture makes spelling and meaning DISAGREE (the plan's own test discipline): the semantically
right event clock sorts LAST, so an assertion that it wins can only pass because the verdict —
never the alphabet — chose it.
"""
from __future__ import annotations

from tests.featuregen.overlay.upload.test_templates import SOURCE

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.templates import ALL_TEMPLATES, ground_template_outcome
from featuregen.overlay.upload.tie_break import (
    TieBreakCandidate,
    store_tie_break_verdict,
    tie_break_input_hash,
)
from featuregen.runtime.observability import counters

_TS_A = "public.transactions.aaa_load_ts"      # alphabetical winner, semantically wrong
_TS_B = "public.transactions.zzz_event_ts"     # the business event clock

_ROWS = [
    (CanonicalRow(SOURCE, "transactions", "cust_ref", "integer", is_grain=True,
                  entity="Customer", definition="The customer this transaction belongs to."),
     "customer_id"),
    (CanonicalRow(SOURCE, "transactions", "aaa_load_ts", "timestamp",
                  definition="The warehouse batch load timestamp — a pipeline artifact."),
     "event_timestamp"),
    (CanonicalRow(SOURCE, "transactions", "zzz_event_ts", "timestamp",
                  definition="When the customer's transaction actually occurred."),
     "event_timestamp"),
]

_DORMANCY = next(t for t in ALL_TEMPLATES if t.id == "dormancy_days")


def _tied_catalog(db):
    build_graph(db, SOURCE, [r for r, _ in _ROWS],
                concepts={content_hash(r): c for r, c in _ROWS if c})


def _store_verdict(db, ranking) -> None:
    """Store the verdict EXACTLY as warming would — same key material, read from the graph."""
    need = next(n for n in _DORMANCY.needs if n.concept == "event_timestamp")
    rows = {r[0]: r for r in db.execute(
        "SELECT object_ref, definition, ai_summary, semantic_terms FROM graph_node "
        "WHERE catalog_source=%s AND object_ref = ANY(%s)",
        (SOURCE, [_TS_A, _TS_B])).fetchall()}
    tied = tuple(TieBreakCandidate(ref=ref, definition=row[1] or "", ai_summary=row[2] or "",
                                   semantic_terms=row[3] or "")
                 for ref, row in sorted(rows.items()))
    key = tie_break_input_hash(template_id=_DORMANCY.id, need_role=need.role,
                               need_concept=need.concept, intent=_DORMANCY.intent, tied=tied)
    store_tie_break_verdict(db, input_hash=key, ranking=ranking,
                            rationale="the recipe wants the business event time",
                            producer_ref="llm_call:shadow-test")


def _event_ts_binding(outcome):
    assert outcome.feature is not None
    return next(r for r in outcome.feature.binding_resolutions
                if r.tied_candidate_refs)


def _count(name: str) -> int:
    return counters.snapshot()["counters"].get(name, 0)


def test_flag_off_is_byte_identical_and_counts_the_disagreement(db):
    _tied_catalog(db)
    _store_verdict(db, [_TS_B, _TS_A])          # the verdict DISAGREES with the alphabet
    before = _count("overlay.tie_break.shadow_disagree")
    outcome = ground_template_outcome(db, _DORMANCY, catalog_source=SOURCE,
                                      roles=("data_owner",))
    binding = _event_ts_binding(outcome)
    assert binding.selected_object_ref == _TS_A, "flag off: today's alphabetical binding stands"
    assert _count("overlay.tie_break.shadow_disagree") == before + 1


def test_agreement_is_counted_too(db):
    _tied_catalog(db)
    _store_verdict(db, [_TS_A, _TS_B])          # the verdict AGREES with the alphabet
    before = _count("overlay.tie_break.shadow_agree")
    ground_template_outcome(db, _DORMANCY, catalog_source=SOURCE, roles=("data_owner",))
    assert _count("overlay.tie_break.shadow_agree") == before + 1


def test_no_verdict_counts_unadjudicated_and_changes_nothing(db):
    _tied_catalog(db)
    before = _count("overlay.tie_break.shadow_unadjudicated")
    outcome = ground_template_outcome(db, _DORMANCY, catalog_source=SOURCE,
                                      roles=("data_owner",))
    assert _event_ts_binding(outcome).selected_object_ref == _TS_A
    assert _count("overlay.tie_break.shadow_unadjudicated") == before + 1


def test_flag_on_binds_the_verdicts_choice(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_TIE_BREAK_BINDING", "1")
    _tied_catalog(db)
    _store_verdict(db, [_TS_B, _TS_A])
    outcome = ground_template_outcome(db, _DORMANCY, catalog_source=SOURCE,
                                      roles=("data_owner",))
    binding = _event_ts_binding(outcome)
    assert binding.selected_object_ref == _TS_B, "meaning beat spelling, by recorded verdict"
    # the bound OPERAND moved too — not just the resolution record
    assert (SOURCE, _TS_B) in outcome.feature.derives_pairs
    assert (SOURCE, _TS_A) not in outcome.feature.derives_pairs
    # the tie is still HONESTLY recorded as a tie
    assert set(binding.tied_candidate_refs) == {_TS_A, _TS_B}


def test_flag_on_without_a_verdict_keeps_the_deterministic_order(db, monkeypatch):
    """Requests NEVER dispatch: no warmed verdict means today's order, under either flag."""
    monkeypatch.setenv("FEATUREGEN_TIE_BREAK_BINDING", "1")
    _tied_catalog(db)
    outcome = ground_template_outcome(db, _DORMANCY, catalog_source=SOURCE,
                                      roles=("data_owner",))
    assert _event_ts_binding(outcome).selected_object_ref == _TS_A
