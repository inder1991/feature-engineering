"""C-A3c — the measure-fact reader refuses an unreadable unit/currency instead of reading it as
absent, which is what lets BR-6's ``CURRENCY_CONVERSION_UNDECLARED`` tooth fire at all.

The regression these guard is specific: today ``read_operational_value`` answers
``not_operational`` for unit/currency even when the decision is source-attested or human-confirmed,
``_fact_text`` turns that into ``""``, and ``not_operational`` is not a C1 hard-fail status — so a
per-row-currency monetary operand reaches ``resolve_output_v2`` looking non-monetary, with nothing
recorded.
"""
from __future__ import annotations

import pytest

from featuregen.formula.measure_facts import (
    MEASURE_FIELDS,
    MeasureFacts,
    MeasureFactsUnreadable,
    MeasureReadDisposition,
    disposition_of,
    read_measure_facts,
)

#: The full C1 status vocabulary, taken from ``operational_facts`` rather than invented here.
_C1_STATUSES = ("conflict", "fork", "hash_mismatch", "no_decision", "no_value",
                "not_operational", "projection_unavailable", "resolved", "retired")


class _Value:
    """The subset of ``OperationalValue`` this reader touches."""

    def __init__(self, status: str, value: object | None = None, *,
                 conflict_status: str | None = None):
        self.status = status
        self.value = value
        self.producer = None
        self.strength = None
        self.conflict_status = conflict_status
        self.selected_evidence_ids: tuple[str, ...] = ()
        self.decision_event_id = None
        self.fact_key = None
        self.fact_event_id = None
        self.policy_version = "test-policy-v1"
        self.resolver_version = None


def _reader(monkeypatch, by_field: dict[str, _Value]):
    monkeypatch.setattr(
        "featuregen.formula.measure_facts.read_verified_decision_value",
        lambda conn, ref, field: by_field[field])


def test_the_disposition_is_total_over_the_c1_vocabulary():
    """Every status the platform can produce maps to exactly one judgement — no status falls
    through to a default that would re-create the silent path."""
    for status in _C1_STATUSES:
        assert isinstance(disposition_of(status), MeasureReadDisposition), status


def test_only_no_decision_and_no_value_are_absent():
    """The narrow cut: 'nobody decided' is legitimate, everything else is a refusal.

    ``retired`` is deliberately a refusal — a withdrawn decision is a column whose meaning USED to
    be declared, and reading that as 'never a measure' is the same silent downgrade as ``""``.
    """
    absent = {s for s in _C1_STATUSES
              if disposition_of(s) is MeasureReadDisposition.ABSENT}
    assert absent == {"no_decision", "no_value"}
    assert disposition_of("retired") is MeasureReadDisposition.UNREADABLE


def test_an_unrecognised_status_is_unreadable_not_absent():
    """A status added later must fail closed: guessing is least defensible where we have never
    seen the value."""
    assert disposition_of("some_future_status") is MeasureReadDisposition.UNREADABLE


def test_a_verified_monetary_per_row_operand_reads_as_monetary(monkeypatch):
    """The read the FX tooth needs, and cannot get through ``read_operational_value`` today."""
    _reader(monkeypatch, {"unit": _Value("resolved", "monetary"),
                          "currency": _Value("resolved", "per_row")})
    facts = read_measure_facts(object(), "ftr::public.txns.txn_amt")
    assert isinstance(facts, MeasureFacts)
    assert facts.is_monetary_per_row
    assert facts.unit.disposition is MeasureReadDisposition.RESOLVED
    assert facts.unit.policy_version == "test-policy-v1"


def test_an_undecided_operand_is_absent_not_a_refusal(monkeypatch):
    """Most columns carry no unit. Refusing here would break every existing feature."""
    _reader(monkeypatch, {"unit": _Value("no_decision"),
                          "currency": _Value("no_decision")})
    facts = read_measure_facts(object(), "ftr::public.txns.merchant_id")
    assert isinstance(facts, MeasureFacts)
    assert not facts.is_monetary_per_row
    assert facts.unit.value == ""
    assert facts.unit.disposition is MeasureReadDisposition.ABSENT


@pytest.mark.parametrize("status", ["conflict", "fork", "hash_mismatch",
                                    "not_operational", "projection_unavailable", "retired"])
def test_an_unreadable_currency_refuses_by_name(monkeypatch, status):
    """THE regression. Each of these once produced ``""`` and a silent cross-currency sum."""
    _reader(monkeypatch, {"unit": _Value("resolved", "monetary"),
                          "currency": _Value(status, conflict_status="two live decisions")})
    out = read_measure_facts(object(), "ftr::public.txns.txn_amt")
    assert isinstance(out, MeasureFactsUnreadable), status
    assert out.field == "currency"
    assert out.status == status
    assert "currency" in out.detail() and status in out.detail()


def test_an_unreadable_unit_refuses_before_currency_is_consulted(monkeypatch):
    """Deterministic refusal order: the same catalog state always names the same field."""
    _reader(monkeypatch, {"unit": _Value("hash_mismatch"),
                          "currency": _Value("fork")})
    out = read_measure_facts(object(), "ftr::public.txns.txn_amt")
    assert isinstance(out, MeasureFactsUnreadable)
    assert out.field == "unit" == MEASURE_FIELDS[0]


def test_the_refusal_detail_names_the_consequence(monkeypatch):
    """A refusal an operator can act on: which column, which field, which status, and why it
    matters — not 'currency is empty'."""
    _reader(monkeypatch, {"unit": _Value("resolved", "monetary"),
                          "currency": _Value("conflict", conflict_status="two live decisions")})
    out = read_measure_facts(object(), "ftr::public.txns.txn_amt")
    assert isinstance(out, MeasureFactsUnreadable)
    detail = out.detail()
    assert "ftr::public.txns.txn_amt" in detail
    assert "two live decisions" in detail
    assert "silence" in detail


# ── the DISCRIMINATING half: the real C1 path, not a stub ────────────────────────────────────────
# `seed_not_operational` seeds source-ATTESTED `unit` evidence through the REAL
# evidence -> decision -> projection machinery. Its own docstring records the situation this module
# exists for: *"the decision is live and load-bearing, but ``read_column_facts`` governs ``unit``
# only as a HINT ... so C1 refuses governed authority while carrying the decision"*. That is the
# column whose unit the old path answered `""` for.

def test_a_live_source_attested_unit_is_never_answered_as_absent(db):
    """THE invariant. For a live, load-bearing, source-attested unit decision the reader may READ
    the value or REFUSE — both are honest. What it must never do is answer ABSENT, because absent
    is indistinguishable from "this column has no unit" and is exactly how a monetary operand came
    to look non-monetary.
    """
    from tests.featuregen.formula.c1_fixtures import seed_not_operational

    from featuregen.overlay.upload.operational_facts import read_operational_value

    col = seed_not_operational(db)

    # the old path, pinned here so this test fails loudly if the premise ever stops being true
    assert read_operational_value(db, col.logical_ref, "unit").status == "not_operational"

    out = read_measure_facts(db, col.logical_ref)
    if isinstance(out, MeasureFactsUnreadable):
        assert out.field == "unit"          # refused — honest
    else:
        assert out.unit.disposition is not MeasureReadDisposition.ABSENT, (
            "a live source-attested unit decision was reported ABSENT — the silent downgrade "
            "C-A3c exists to end")
