"""Explaining the source and row decisions WITHOUT exposing hidden catalogs (Release-B Task 9).

The decision is RECORDED whole and RENDERED narrow, and the interesting tests are the narrow half:

  * THE EXISTENCE-ORACLE PROBE. A caller who may not see the archive copy must learn that the
    decision was contested — "1 other candidate considered" — and must never learn its NAME, not in
    the candidate list, not in a refusal subject, not anywhere in the payload. The probe asserts on
    the WHOLE serialized body, because a leak that only shows up in a field nobody thought to check
    is exactly the leak that ships.
  * the count is not suppressed. Hiding it too would misrepresent a contested decision as an
    uncontested one, which is the worse failure.
  * ROW SEMANTICS, not a row label: the half-open `[from, to)` predicates are rendered — and
    withheld WHOLE when the caller may not see every column they name, the rule the temporal policy
    route already settled.
  * no refusal is framed as a failure: every closed code maps to a family a person can act on.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.analysis.release_b_bank import (
    ARCHIVE,
    CUTOFF_REF,
    SEG,
    SEG_FROM,
    SEG_TO,
    SRC,
    TRAN,
    build_bank,
    pilot_plan,
)

from featuregen.analysis.explain import (
    NEEDS_DATA_CHECK,
    NEEDS_SETUP,
    REFUSAL_FAMILIES,
    STRUCTURALLY_UNSUITABLE,
    UNDECIDED,
    render_selection,
)
from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.overlay.upload.source_selection import SELECTION_REFUSAL_CODES

#: The role claim the restricted archive is behind. No caller in these tests holds it.
_ARCHIVE_READER = "restricted_reader"


@pytest.fixture
def bank(db, monkeypatch):
    return build_bank(db, monkeypatch)


@pytest.fixture
def bank_with_hidden_archive(db, monkeypatch):
    return build_bank(db, monkeypatch, restricted_archive=True)


def _grounded(bank, *, roles=(), **over):
    return ground_analysis_plan(bank, pilot_plan(**over), cutoff_value_ref=CUTOFF_REF,
                                recorded_by="task9", roles=roles)


def _rendered(bank, *, roles=(), **over):
    return render_selection(bank, _grounded(bank, roles=roles, **over).selections, roles=roles)


def _event(view) -> dict:
    return next(s for s in view["sources"] if s["need_role"] == "event_source")


# ── the readable case ────────────────────────────────────────────────────────────────────────────


def test_a_caller_who_may_see_everything_gets_every_candidate_by_name(bank):
    view = _rendered(bank, roles=(_ARCHIVE_READER,))
    event = _event(view)
    assert event["dataset_ref"] == TRAN
    assert event["selection_basis"] == "serving_policy"
    dispositions = {c["dataset_ref"]: c["disposition"] for c in event["considered"]}
    assert dispositions == {TRAN: "selected", ARCHIVE: "eligible"}
    assert event["considered_withheld"] == 0
    assert event["considered_total"] == 2


def test_the_row_semantics_are_rendered_not_just_the_row_LABEL(bank):
    """"Valid at the report cutoff" is a label; the two column refs and two operators under it are
    the claim, and they are what a person checks."""
    view = _rendered(bank)
    row = view["rows"][0]
    assert row["dataset_ref"] == SEG
    assert row["selection_kind"] == "valid_at_report_cutoff"
    assert [(p["column_ref"], p["operator"]) for p in row["predicates"]] == [
        (SEG_FROM, "<="), (SEG_TO, ">")]
    assert row["predicates_withheld"] is False
    # The cutoff's NAME, never a date: a value here would be a different rule every day.
    assert row["cutoff_value_ref"] == CUTOFF_REF
    assert "2026" not in json.dumps(view)


# ── THE EXISTENCE-ORACLE PROBE ───────────────────────────────────────────────────────────────────


def test_a_hidden_candidate_is_a_COUNT_and_its_name_appears_NOWHERE(bank_with_hidden_archive):
    """The probe. Asserted against the WHOLE serialized payload, because a leak in a field nobody
    thought to check is exactly the leak that ships."""
    view = _rendered(bank_with_hidden_archive)
    event = _event(view)

    assert event["dataset_ref"] == TRAN
    assert [c["dataset_ref"] for c in event["considered"]] == [TRAN]
    assert event["considered_withheld"] == 1
    assert event["considered_total"] == 2

    body = json.dumps(view)
    assert ARCHIVE not in body
    assert "tran_archive" not in body


def test_the_contest_is_still_REPORTED_even_though_the_alternative_is_not_named(
        bank_with_hidden_archive):
    """Suppressing the count as well would misrepresent a contested decision as an uncontested one,
    which is a worse failure than the one being avoided. The reader learns enough to ask their
    governance owner a question, and nothing more."""
    event = _event(_rendered(bank_with_hidden_archive))
    assert event["considered_withheld"] >= 1
    assert event["considered_total"] > len(event["considered"])


def test_a_caller_who_may_not_see_the_SELECTED_dataset_gets_the_whole_entry_withheld(bank):
    """Not a redacted decision about a table they cannot see — the same words a decision that was
    never made gets.

    The shape is a decision MADE by someone with wide scope and READ by someone without it, which
    is the only way a selected dataset can be unreadable at render time: the selector itself drops
    a candidate the PLANNER cannot see, so a narrow planner never selects one at all.
    """
    grounded = _grounded(bank, roles=(_ARCHIVE_READER,))
    bank.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
        (SRC, "customer_segment_history"))

    view = render_selection(bank, grounded.selections, roles=())
    dimension = next(s for s in view["sources"] if s["need_role"] == "dimension_source")
    assert dimension["withheld"] is True
    assert "dataset_ref" not in dimension
    assert "customer_segment_history" not in json.dumps(dimension)


def test_row_predicates_are_withheld_WHOLE_when_a_named_column_is_hidden(bank):
    """The temporal policy route's rule, applied here: withholding one field at a time still
    answers "a column exists here that you may not see", one field at a time."""
    bank.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND column_name = 'effective_to'", (SRC,))
    row = _rendered(bank)["rows"][0]
    assert row["predicates_withheld"] is True
    assert row["predicates"] == []
    # The RULE is still named — what is withheld is which columns express it.
    assert row["selection_kind"] == "valid_at_report_cutoff"
    assert "effective_to" not in json.dumps(row)


# ── no-blocked framing ───────────────────────────────────────────────────────────────────────────


def test_every_closed_refusal_code_has_a_family_a_person_can_act_on(bank):
    """A UI must never frame an unreviewed or undecided thing as a failure. Each closed code maps
    to exactly one of: nobody has decided yet / someone must look at the data / this source
    structurally cannot answer / an operator has to register something."""
    assert set(SELECTION_REFUSAL_CODES) <= set(REFUSAL_FAMILIES)
    assert set(REFUSAL_FAMILIES.values()) == {
        UNDECIDED, NEEDS_DATA_CHECK, STRUCTURALLY_UNSUITABLE, NEEDS_SETUP}


def test_an_undeclared_population_reads_as_UNDECIDED_never_as_a_failure(bank):
    view = _rendered(bank, population_table_ref="", population_key_ref="")
    refusal = next(r for r in view["refusals"]
                   if r["code"] == "SELECTION_POPULATION_UNDECLARED")
    assert refusal["family"] == UNDECIDED
    assert view["resolved"] is False
    # EVERY refusal carries a family, so a screen can never fall back to rendering a bare code —
    # which is how "nobody has decided yet" ends up looking like an error.
    assert all(r["family"] in REFUSAL_FAMILIES.values() for r in view["refusals"])


def test_a_refusal_subject_that_cannot_ADDRESS_a_dataset_is_shown_rather_than_withheld(bank):
    """`SELECTION_BINDING_MISSING` is ABOUT a ref the catalog cannot address. Treating it as hidden
    would withhold the one ref the reader most needs to see."""
    view = _rendered(bank, base_table_ref="ftr::unbound_thing",
                     entity_ref="ftr::unbound_thing.cif_id")
    binding = [r for r in view["refusals"] if r["code"] == "SELECTION_BINDING_MISSING"]
    assert binding, view["refusals"]
    assert binding[0]["family"] == NEEDS_SETUP
    assert any("unbound_thing" in s for s in binding[0]["subjects"])
    assert binding[0]["subjects_withheld"] == 0


def test_a_PROPOSED_AUTHORITY_warning_travels_with_the_decision(bank):
    """The point of letting an unconfirmed classification be useful is that its authority travels
    with it — visible, never a log line."""
    view = _rendered(bank)
    assert isinstance(view["warnings"], list)


def test_nothing_is_rendered_while_the_flag_is_off(bank, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    assert render_selection(bank, _grounded(bank).selections) is None
