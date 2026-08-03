"""Cheap, DB-free, client-free guards so a future gold edit cannot silently break the evaluation.

The load-bearing one is `test_every_discriminator_is_rich_only`. Without it the whole thin-vs-rich
measurement could quietly become tautological (a discriminator that is present in BOTH payloads
would score every case a hit in both modes) or vacuous (one present in NEITHER would score every
case a miss). It fails loudly instead, naming the term and the mode it leaked into.

Deliberately NOT marked `eval`: these are shape guards over checked-in fixtures with no provider
and no database, so they belong in the default run alongside `test_feature_eval.py`.
"""
from __future__ import annotations

import pytest
from tests.eval import gold_catalog_profiles as pg
from tests.eval import gold_semantic_context as sg

from featuregen.overlay.upload.concepts import is_known_concept
from featuregen.overlay.upload.profile_vocab import (
    AuthorityRole,
    DataRole,
    TemporalStorageModel,
)


def _known(concept: str) -> bool:
    return concept == sg.UNCLASSIFIED or is_known_concept(concept)


# ── semantic gold set ────────────────────────────────────────────────────────────────────────────


def test_the_ten_named_defect_families_are_all_present() -> None:
    assert set(sg.families()) == {
        "bic_vs_cif", "internal_vs_external_account", "amount_vs_identifier",
        "branch_id_vs_description", "event_time_vs_ingestion_time", "currency_vs_amount",
        "status_code_vs_description", "sensitive_party_attributes", "target_leakage_labels",
        "honestly_unclassified",
    }


def test_every_family_carries_a_case_and_a_counterexample() -> None:
    for family in sg.families():
        roles = {c.role for c in sg.cases_in(family)}
        assert "case" in roles, f"{family} has no positive case"
        assert "counterexample" in roles, f"{family} has no counterexample"


def test_the_six_named_witnesses_are_fixture_columns() -> None:
    """CIB/FTR witnesses as FIXTURES — the suite must never reach for a live catalog row."""
    columns = {c.column for c in sg.cases()}
    assert set(sg.witnesses()) <= columns
    assert {c.column for c in sg.cases() if c.witness} == set(sg.witnesses())


def test_every_graded_concept_is_real() -> None:
    for case in sg.cases():
        assert _known(case.expected_concept), f"{case.case_id}: {case.expected_concept}"
        assert _known(case.confusable_concept), f"{case.case_id}: {case.confusable_concept}"
        assert case.expected_concept in case.acceptable_concepts, case.case_id
        for alt in case.acceptable_concepts:
            assert _known(alt), f"{case.case_id}: alternative {alt}"
        for bad in case.forbidden_concepts:
            assert _known(bad), f"{case.case_id}: forbidden {bad}"
            assert bad not in case.acceptable_concepts, \
                f"{case.case_id}: {bad} is both forbidden and acceptable"


def test_columns_are_unique() -> None:
    columns = [c.column for c in sg.cases()]
    assert len(columns) == len(set(columns))


@pytest.mark.parametrize("case", sg.cases(), ids=lambda c: c.case_id)
def test_every_discriminator_is_rich_only(case: sg.SemanticGoldCase) -> None:
    """A `requires_any` term must appear in the rich payload and NOT in the thin one.

    Present in both  -> the eval measures nothing (both modes always hit).
    Present in neither -> the eval measures nothing (both modes always miss).
    Either way the number that comes out is a fiction, so this test refuses to let one exist.
    """
    if case.discriminator_mode != sg.MODE_REQUIRES_ANY:
        return
    thin = sg.serialize(sg.thin_payload(case))
    rich = sg.serialize(sg.rich_payload(case))
    for term in case.discriminator_terms:
        assert term in rich, f"{case.case_id}: discriminator {term!r} is absent from the RICH payload"
        assert term not in thin, \
            f"{case.case_id}: discriminator {term!r} LEAKED into the thin payload — the case is tautological"


@pytest.mark.parametrize("case", sg.cases(), ids=lambda c: c.case_id)
def test_the_unclassified_family_declines_in_both_modes(case: sg.SemanticGoldCase) -> None:
    """`requires_absent` cases must find no meaning-bearing key in EITHER payload. If the rich
    adapter ever fabricates one out of the sibling roster or the table context, this trips."""
    if case.discriminator_mode != sg.MODE_REQUIRES_ABSENT:
        return
    for label, payload in (("thin", sg.thin_payload(case)), ("rich", sg.rich_payload(case))):
        present = [k for k in case.discriminator_keys if payload.get(k)]
        assert present == [], f"{case.case_id}: the {label} payload fabricated {present}"


def test_the_cross_namespace_controls_name_real_gold_columns() -> None:
    known = {c.column for c in sg.cases()} | {row.column for row, _c, _r in sg.peer_columns()}
    for left, right, _why in (*sg.must_not_pair(), *sg.may_pair()):
        assert left in known, left
        assert right in known, right
    assert sg.may_pair(), "a must-not-pair list with no positive control proves nothing"


def test_the_retrieval_questions_name_real_gold_columns() -> None:
    known = {c.column for c in sg.cases()}
    for q in sg.retrieval_questions():
        assert q["expected_columns"], q["id"]
        for col in (*q["expected_columns"], *q["forbidden_columns"]):
            assert col in known, f"{q['id']}: {col}"


def test_the_unsafe_feature_gold_has_both_polarities() -> None:
    cases = sg.unsafe_feature_cases()
    assert any(c["unsafe"] for c in cases)
    assert any(not c["unsafe"] for c in cases), "no safe control: a bar that rejects everything passes"
    known = {f"{c.catalog_source}.{c.table}.{c.column}" for c in sg.cases()}
    for c in cases:
        for ref in c["derives_from"]:
            assert ref in known, f"{c['id']}: {ref}"


# ── profile gold set ─────────────────────────────────────────────────────────────────────────────


def test_the_six_named_profile_families_are_all_present() -> None:
    assert set(pg.families()) == {
        "event_fact_vs_snapshot_fact", "customer_dimension_vs_population",
        "system_of_record_vs_replica", "scd2_vs_current_only",
        "real_crosswalk_vs_ordinary_reference", "honestly_unknown_tables",
    }


def test_every_profile_family_carries_a_case_and_a_counterexample() -> None:
    for family in pg.families():
        roles = {d.role for d in pg.datasets_in(family)}
        assert roles == {"case", "counterexample"}, f"{family}: {roles}"


def test_every_expected_classification_is_in_its_closed_vocabulary() -> None:
    data_roles = {m.value for m in DataRole}
    authority = {m.value for m in AuthorityRole}
    temporal = {m.value for m in TemporalStorageModel}
    for d in pg.datasets():
        assert d.expected["data_role"] in data_roles, d.dataset_id
        assert d.expected["authority_role"] in authority, d.dataset_id
        assert d.expected["temporal_storage_model"] in temporal, d.dataset_id


def test_tables_are_unique() -> None:
    tables = [d.table for d in pg.datasets()]
    assert len(tables) == len(set(tables))


def test_load_bearing_is_claimed_only_where_the_evidence_licenses_it() -> None:
    """The operational rule for `authority_role`/`temporal_storage_model` admits source/attested or
    human/confirmed and nothing else. A fixture that claimed load-bearing off an llm_proposed
    record would be asserting the exact defect the release bar exists to catch."""
    licensed = {("source", "attested"), ("human", "confirmed")}
    for d in pg.datasets():
        for field in ("authority_role", "temporal_storage_model"):
            if not d.load_bearing.get(field):
                continue
            ev = d.evidence.get(field)
            assert ev is not None, f"{d.dataset_id}: {field} claims load-bearing with no evidence"
            assert (ev["producer"], ev["strength"]) in licensed, \
                f"{d.dataset_id}: {field} claims load-bearing off {ev['producer']}/{ev['strength']}"


def test_at_least_one_correct_llm_proposal_is_marked_not_load_bearing() -> None:
    """Correctness is not authority. Without a fixture in this shape the release bar could be
    satisfied by a gold set in which the LLM is simply never right."""
    assert any(
        d.evidence.get("authority_role", {}).get("producer") == "llm"
        and d.expected["authority_role"] == d.evidence["authority_role"]["value"]
        and not d.load_bearing["authority_role"]
        for d in pg.datasets()
    )


def test_the_selection_questions_name_real_gold_tables() -> None:
    known = {d.table for d in pg.datasets()}
    for q in pg.selection_questions():
        assert q["prefer"], q["id"]
        for table in (*q["prefer"], *q["avoid"]):
            assert table in known, f"{q['id']}: {table}"


def test_the_honestly_unknown_family_expects_no_decision() -> None:
    unknown = pg.dataset_by_table("stg_raw_landing")
    assert unknown.expected == {"data_role": "unknown", "authority_role": "unknown",
                                "temporal_storage_model": "unknown", "primary_entity": None}
    assert unknown.evidence == {}
    assert not any(unknown.load_bearing.values())
    partial = pg.dataset_by_table("stg_partial_landing")
    assert set(partial.evidence) == {"authority_role"}, \
        "partial knowledge must stay partial — one answerable field licenses no others"
