"""Task 5 — scorer + direction/cardinality inference.

The load-bearing rules (baked in by a two-round review):
- ONLY `INFERRED_FROM_CONFIRMED_GRAIN` is strong-eligible; both-grain (would-be 1:1) and
  neither-grain (M:N risk) are FORCED weak regardless of score, with both grains listed in
  `missing_requirements` (two unique columns are not necessarily 1:1: account_id != card_id).
- A POSSIBLE namespace caps at weak unless a `related_terms_key_link` signal fired.
- A shared-EMPTY canonical column name never fires `same_column_name` (Task-3 review guard).
"""
from featuregen.overlay.upload.passc.candidates import CandidatePair, block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.types import (
    ALGORITHM_VERSION, CONFIG_VERSION, CardinalityInferenceStatus as S,
    NamespaceCompatibility as N)


def _c(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


_CIF_TERM = "Customer Information File Identifier"


def _pair(a, b):
    pairs = block_candidates([a, b])
    assert len(pairs) == 1, "test setup must yield exactly one blocked pair"
    return pairs[0]


def _score(a, b):
    return score(_pair(a, b), source_snapshot_id="snap-1")


def _names(ev):
    return [s.signal_name for s in ev.positive_signals]


def test_same_concept_same_name_one_side_grain_is_strong_compatible():
    a = _c("transactions", "cif_id", term_name=_CIF_TERM)
    b = _c("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)
    ev = _score(a, b)
    assert ev.bucket == "strong"
    assert ev.namespace_compatibility is N.COMPATIBLE
    assert ev.cardinality_status is S.INFERRED_FROM_CONFIRMED_GRAIN
    names = _names(ev)
    assert "same_identifier_concept" in names
    assert "same_column_name" in names
    assert "one_side_confirmed_grain" in names
    assert ev.score == sum(s.score_delta for s in ev.positive_signals) >= 80
    assert ev.missing_requirements == ()


def test_right_grain_orients_a_to_b_n_to_1():
    a = _c("transactions", "cif_id", term_name=_CIF_TERM)
    b = _c("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)
    ev = _score(a, b)
    assert (ev.from_ref, ev.to_ref) == (a.object_ref, b.object_ref)
    assert ev.proposed_cardinality == "N:1"
    assert ev.column_pairs == (("cif_id", "cif_id"),)
    assert ev.proposed_direction and a.object_ref in ev.proposed_direction
    assert b.object_ref in ev.grain_evidence


def test_left_grain_flips_direction_to_b_to_a():
    a = _c("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)   # sorts first → pair.a
    b = _c("transactions", "cust_ref", term_name=_CIF_TERM)
    ev = _score(a, b)
    assert ev.cardinality_status is S.INFERRED_FROM_CONFIRMED_GRAIN
    assert (ev.from_ref, ev.to_ref) == (b.object_ref, a.object_ref)     # oriented b -> a
    assert ev.proposed_cardinality == "N:1"
    assert ev.column_pairs == (("cust_ref", "cif_id"),)                 # (from_col, to_col)


def test_possible_namespace_caps_at_weak_despite_strong_score():
    # Same concept, different column name, no synonyms → POSSIBLE; score alone clears 80.
    a = _c("accounts", "cif_id", term_name=_CIF_TERM, data_domain="party")
    b = _c("loans", "cust_file_ref", term_name=_CIF_TERM, data_domain="party", is_grain=True)
    ev = _score(a, b)
    assert ev.namespace_compatibility is N.POSSIBLE
    assert ev.cardinality_status is S.INFERRED_FROM_CONFIRMED_GRAIN     # not a grain problem
    assert ev.score >= 80                                               # strong on score alone
    assert ev.bucket == "weak"                                          # …but capped


def test_related_terms_key_link_lifts_the_possible_cap():
    a = _c("accounts", "cif_id", term_name=_CIF_TERM, data_domain="party")
    b = _c("loans", "cust_file_ref", term_name=_CIF_TERM, data_domain="party", is_grain=True)
    pair = CandidatePair(a=a, b=b, namespace=N.POSSIBLE,
                         namespace_reasons=("same_identifier_concept", "related_terms_link"))
    ev = score(pair, source_snapshot_id="snap-1")
    assert "related_terms_key_link" in _names(ev)
    assert ev.bucket == "strong"


def test_both_grain_forced_weak_1_to_1_never_auto_proposed():
    a = _c("accounts", "cif_id", term_name=_CIF_TERM, is_grain=True)
    b = _c("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)
    ev = _score(a, b)
    assert ev.score >= 80                                   # a high score cannot rescue it
    assert ev.bucket == "weak"
    assert ev.cardinality_status is S.AMBIGUOUS_BOTH_GRAINS
    assert ev.proposed_cardinality == "1:1"
    assert ev.proposed_direction is None
    assert "one_side_confirmed_grain" not in _names(ev)     # XOR signal: both-grain is not one-side
    assert any(a.object_ref in m for m in ev.missing_requirements)
    assert any(b.object_ref in m for m in ev.missing_requirements)


def test_neither_grain_high_score_forced_weak_with_both_grains_missing():
    a = _c("accounts", "cif_id", term_name=_CIF_TERM)
    b = _c("customers", "cif_id", term_name=_CIF_TERM)
    ev = _score(a, b)
    assert ev.score >= 80
    assert ev.bucket == "weak"
    assert ev.cardinality_status is S.MANY_TO_MANY_RISK
    assert ev.proposed_cardinality is None                  # NEVER a 1:1 default
    assert ev.proposed_direction is None
    assert any(a.object_ref in m for m in ev.missing_requirements)
    assert any(b.object_ref in m for m in ev.missing_requirements)
    assert ev.grain_evidence == ()


def test_low_score_inferred_grain_pair_is_suppressed():
    # COMPATIBLE via column_entity alone; no concept/name/term signals → 25+10=35 < 50.
    a = _c("transactions", "cust_id", column_entity="customer")
    b = _c("customers", "customer_key", column_entity="customer", is_grain=True)
    ev = _score(a, b)
    assert ev.cardinality_status is S.INFERRED_FROM_CONFIRMED_GRAIN
    assert ev.score < 50
    assert ev.bucket == "suppressed"


def test_shared_empty_canonical_column_name_does_not_fire_same_name():
    # Both "id" and "key" canonicalize to "" — a shared-empty canon is NOT a name match.
    a = _c("accounts", "id", term_name=_CIF_TERM)
    b = _c("customers", "key", term_name=_CIF_TERM)
    pair = CandidatePair(a=a, b=b, namespace=N.POSSIBLE,
                         namespace_reasons=("same_identifier_concept",))
    ev = score(pair, source_snapshot_id="snap-1")
    assert "same_column_name" not in _names(ev)


def test_every_result_self_explains_and_carries_provenance():
    scenarios = [
        (_c("transactions", "cif_id", term_name=_CIF_TERM),
         _c("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)),
        (_c("accounts", "cif_id", term_name=_CIF_TERM, is_grain=True),
         _c("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)),
        (_c("accounts", "cif_id", term_name=_CIF_TERM),
         _c("customers", "cif_id", term_name=_CIF_TERM)),
        (_c("transactions", "cust_id", column_entity="customer"),
         _c("customers", "customer_key", column_entity="customer", is_grain=True)),
    ]
    for a, b in scenarios:
        ev = _score(a, b)
        assert ev.explanation.strip()                       # a human sentence, never empty
        assert ev.producer == "deterministic_pass_c"
        assert ev.config_version == CONFIG_VERSION
        assert ev.candidate_algorithm_version == ALGORITHM_VERSION
        assert ev.source_snapshot_id == "snap-1"
        assert len(ev.candidate_id) == 16
        assert ev.namespace_reason_codes                    # carried from the blocker
        assert ev.negative_signals == () and ev.llm_annotations == ()


def test_deterministic_same_input_same_evidence():
    a = _c("transactions", "cif_id", term_name=_CIF_TERM)
    b = _c("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)
    assert _score(a, b) == _score(a, b)


def test_empty_canon_names_stay_possible_and_capped_at_weak():
    # Audit fix I-4 (scoring consequence): "id"/"key" both canonicalize to "" — the vacuous name
    # match must not flip the namespace to COMPATIBLE, so this high-scoring pair (85 without any
    # name signal) stays POSSIBLE and rule 2 caps it at weak instead of proposing it as strong.
    a = _c("transactions", "id", term_name=_CIF_TERM, data_domain="retail")
    b = _c("customers", "key", term_name=_CIF_TERM, data_domain="retail", is_grain=True)
    ev = _score(a, b)
    assert ev.namespace_compatibility is N.POSSIBLE
    assert "same_column_name" not in ev.namespace_reason_codes
    assert "same_column_name" not in _names(ev)              # the scorer guard already held
    assert ev.score >= 80                                    # strong-threshold score...
    assert ev.bucket == "weak"                               # ...but POSSIBLE caps at weak
    assert "Capped at weak" in ev.explanation
