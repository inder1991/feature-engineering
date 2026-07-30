"""Prose is where a grounded pipeline quietly stops being grounded.

The plan is checked, the SQL is the SQL that runs, the answer reconciles — and then a sentence says
"volumes fell by roughly a third" and none of that was measured. A number in a sentence carries the
authority of everything upstream of it, which is exactly what makes an invented one dangerous.

The rule is mechanical: a claim may contain no number that is not in a cell it cites. Not "consistent
with" — present. This cannot stop a model writing a wrong sentence around a right number; it removes
the class where the number itself was never computed.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.narrative import (
    Cell,
    Claim,
    NarrativeError,
    citable_result,
    validate_claim,
    validate_narrative,
)
from featuregen.data_agent.analysis import AnalysisRow

_ROWS = (
    AnalysisRow(key="C1", previous_count=3, current_count=1,
                dimensions={"segment": "SME", "sector": "TRADING"}),
    AnalysisRow(key="C4", previous_count=2, current_count=0,
                dimensions={"segment": "CORPORATE", "sector": "REAL_ESTATE"}),
)


def _result(**over):
    kw = dict(rows=_ROWS, periods=("2026-05", "2026-06"))
    kw.update(over)
    return citable_result(**kw)


# ── what may be said ─────────────────────────────────────────────────────────────────────────────

def test_a_claim_whose_numbers_all_come_from_cited_cells_is_accepted():
    validate_claim(Claim(text="C1 fell from 3 to 1",
                         cites=("C1.previous_count", "C1.current_count")), _result())


def test_a_claim_with_no_numbers_still_needs_a_citation():
    """"Retail declined the most" is a claim ABOUT the data. Uncited, it cannot be checked at all —
    which is worse than a wrong number, because nothing marks it as unverifiable."""
    with pytest.raises(NarrativeError) as exc:
        validate_claim(Claim(text="corporate customers declined the most"), _result())
    assert exc.value.code == "CLAIM_UNCITED"


def test_a_dimension_value_is_citable():
    validate_claim(Claim(text="C1 is in the SME segment", cites=("C1.segment",)), _result())


def test_a_period_label_is_citable():
    """"in 2026-05" is a claim about which slice was read, and its digits would otherwise read as an
    uncited number."""
    validate_claim(Claim(text="the comparison period is 2026-05", cites=("period:2026-05",)),
                   _result())


# ── THE property ─────────────────────────────────────────────────────────────────────────────────

def test_an_invented_number_is_rejected_even_when_the_citation_is_REAL():
    """The whole point. Citing a genuine cell and then stating a figure that is not in it is exactly
    how a plausible narrative launders a guess — the citation makes it look checked."""
    with pytest.raises(NarrativeError) as exc:
        validate_claim(Claim(text="C1 fell by 67%", cites=("C1.previous_count",)), _result())
    assert exc.value.code == "NUMBER_NOT_IN_CITED_CELLS"
    assert exc.value.subject == "67"


def test_a_number_from_a_cell_the_claim_did_NOT_cite_is_still_rejected():
    """The cell exists, so the figure is real — but the claim did not rest on it, and accepting that
    would make citations decorative."""
    with pytest.raises(NarrativeError, match="NUMBER_NOT_IN_CITED_CELLS"):
        validate_claim(Claim(text="C1 fell from 3 to 1", cites=("C1.previous_count",)), _result())


def test_a_citation_to_a_cell_that_does_not_exist_is_rejected():
    with pytest.raises(NarrativeError) as exc:
        validate_claim(Claim(text="C9 had 5", cites=("C9.current_count",)), _result())
    assert exc.value.code == "CITATION_UNKNOWN"


def test_a_digit_inside_an_ENTITY_KEY_is_not_treated_as_a_figure():
    """`C1` contains a 1, and `2026-05` contains 2026 and 5. Reading those as uncited numbers rejects
    every honest sentence about a customer or a period — which is the pressure that would get this
    check disabled rather than fixed."""
    validate_claim(Claim(text="C1 and C4 both declined", cites=("C1.segment", "C4.segment")),
                   _result())


def test_but_a_real_figure_beside_an_entity_key_is_still_checked():
    """The tolerance must not swallow the number next to it."""
    with pytest.raises(NarrativeError, match="NUMBER_NOT_IN_CITED_CELLS"):
        validate_claim(Claim(text="C1 declined by 99", cites=("C1.segment",)), _result())


# ── the comparison is by VALUE, not by spelling ──────────────────────────────────────────────────

@pytest.mark.parametrize("written", ["1240", "1,240", "1240.0"])
def test_a_number_is_matched_by_value_however_it_is_written(written):
    """Rejecting `1,240` because a cell says `1240` would put pressure on loosening the check rather
    than fixing the comparison — and the figures most worth inventing are the large ones."""
    result = citable_result(rows=(), derived=(Cell(row_key="", column="total", value="1240"),))
    validate_claim(Claim(text=f"the total was {written}", cites=("total",)), result)


def test_a_thousands_separated_INVENTION_is_still_caught():
    """The formatting tolerance must not become a hole."""
    result = citable_result(rows=(), derived=(Cell(row_key="", column="total", value="1240"),))
    with pytest.raises(NarrativeError, match="NUMBER_NOT_IN_CITED_CELLS"):
        validate_claim(Claim(text="the total was 1,241", cites=("total",)), result)


# ── derived figures are cells, not exceptions ────────────────────────────────────────────────────

def test_a_percentage_becomes_sayable_by_being_COMPUTED_and_cited():
    """"A 67% drop" is legitimate; 67 appears nowhere in a result of counts. The answer is to compute
    it and cite it — not to let "obviously derivable" numbers through unchecked, which is precisely
    the gap a model fills with plausible ones."""
    result = _result(derived=(Cell(row_key="C1", column="pct_change", value="67"),))
    validate_claim(Claim(text="C1 fell by 67%", cites=("C1.pct_change",)), result)


# ── whole narratives ─────────────────────────────────────────────────────────────────────────────

def test_the_first_bad_claim_names_itself_rather_than_condemning_the_narrative():
    """So a caller can regenerate one sentence instead of discarding the answer."""
    claims = [
        Claim(text="C1 fell from 3 to 1", cites=("C1.previous_count", "C1.current_count")),
        Claim(text="C4 fell by 90%", cites=("C4.previous_count",)),
    ]
    with pytest.raises(NarrativeError) as exc:
        validate_narrative(claims, _result())
    assert exc.value.subject == "90"


def test_a_fully_grounded_narrative_passes():
    validate_narrative([
        Claim(text="C1 fell from 3 to 1", cites=("C1.previous_count", "C1.current_count")),
        Claim(text="C4 fell from 2 to 0", cites=("C4.previous_count", "C4.current_count")),
        Claim(text="both are in the 2026-06 period", cites=("period:2026-06",)),
    ], _result())


# ── the universe is built from the REAL result ───────────────────────────────────────────────────

def test_the_citable_universe_comes_from_the_rows_that_were_returned(db):
    """Built from an actual run, so a narrative can only cite what the query produced — not what the
    plan said it would produce."""
    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.data_agent.analysis import run_analysis
    from featuregen.data_agent.sql_postgres import PostgresDialect
    from tests.featuregen.analysis.test_plan_to_execution import _grounded, _inputs
    from tests.featuregen.data_agent.pilot_fixture import create_pilot_tables

    create_pilot_tables(db)
    rows = run_analysis(db, plan_to_execution_ir(_grounded(), _inputs()),
                        dialect=PostgresDialect())
    result = citable_result(rows=rows, periods=("2026-05", "2026-06"))

    # C4 is the customer the question is about: 2 -> 0, and both figures are citable.
    validate_claim(Claim(text="C4 fell from 2 to 0",
                         cites=("C4.previous_count", "C4.current_count")), result)
    # C9 never reached the result, so nothing about it can be said.
    with pytest.raises(NarrativeError, match="CITATION_UNKNOWN"):
        validate_claim(Claim(text="C9 was flat", cites=("C9.current_count",)), result)
