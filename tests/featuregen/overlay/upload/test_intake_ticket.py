"""INTAKE BUILD increment 1 — the mandatory read: one hypothesis, one ticket, four consumers.

The #2 spec, engine half. Every new hypothesis gets ONE cached extraction filling the full ticket
{target_column, target_window_days, target_type, business_domain}. Resolution is SELECTION, never
generation: an exactly-typed column PINS (the model cannot override it — a disagreement surfaces as
a contradiction, not a swap); a fuzzy target is chosen FROM the shortlist or abstained. The cache
key covers all four inputs (hypothesis, shortlist content, use-case vocabulary, prompt version) —
the second-review correction to this spec's own first draft. Failure degrades, never blocks.
"""
from __future__ import annotations

from tests.featuregen.overlay.upload.test_templates import SOURCE

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.intake_ticket import (
    INTAKE_TICKET_TASK,
    extract_intake_ticket,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

_STATUS = "public.customers.cust_status_flg"
_SUSP = "public.customers.cust_susp_flg"

_ROWS = [
    (CanonicalRow(SOURCE, "customers", "cust_status_flg", "text",
                  definition="Current lifecycle status of the customer relationship."),
     "customer_relationship_status"),
    (CanonicalRow(SOURCE, "customers", "cust_susp_flg", "text",
                  definition="Whether the customer is currently suspended by compliance action."),
     "restriction_status"),
]


def _catalog(db):
    build_graph(db, SOURCE, [r for r, _ in _ROWS],
                concepts={content_hash(r): c for r, c in _ROWS if c})


def _ticket_client(target: str = _STATUS, window: int = 90,
                   domains=("retail_churn",), runners=()) -> FakeLLM:
    return FakeLLM(script={INTAKE_TICKET_TASK: FakeResponse(output={
        "target_ref": target, "target_window_days": window,
        "target_type": "binary_classification", "business_domain": list(domains),
        "confidence": "high", "runner_up_refs": list(runners)})})


class _MustNotBeCalled:
    def call(self, *a, **k):  # pragma: no cover
        raise AssertionError("a cached ticket must never re-dispatch the model")


def test_a_typed_column_name_PINS_and_the_model_fills_the_rest(db):
    _catalog(db)
    ticket, reason = extract_intake_ticket(
        db, _ticket_client(), catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Predict cust_status_flg. Churn means no activity in 90 days.")
    assert reason == "extracted"
    assert ticket.target_column == _STATUS
    assert ticket.pinned is True, "the user literally typed the column; code matched it, not the model"
    assert ticket.target_window_days == 90
    assert ticket.target_type == "binary_classification"
    assert "retail_churn" in ticket.business_domain


def test_the_model_cannot_override_a_pinned_name_but_the_disagreement_surfaces(db):
    """The confirm screen's contradiction warning: 'you named X; your description reads as Y'."""
    _catalog(db)
    ticket, _reason = extract_intake_ticket(
        db, _ticket_client(target=_SUSP),           # the model reads the prose as suspension
        catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Predict cust_status_flg based on suspension behaviour.")
    assert ticket.target_column == _STATUS, "the pin always wins"
    assert ticket.contradiction is not None
    assert "cust_susp_flg" in ticket.contradiction


def test_a_fuzzy_target_is_selected_from_the_shortlist_and_cached(db):
    _catalog(db)
    ticket, reason = extract_intake_ticket(
        db, _ticket_client(), catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Customers churn when their activity drops off. Churn = 90 days inactive.")
    assert (reason, ticket.pinned) == ("extracted", False)
    assert ticket.target_column == _STATUS
    # the second ask replays — the mandatory read costs ONE call per hypothesis, ever
    again, reason2 = extract_intake_ticket(
        db, _MustNotBeCalled(), catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Customers churn when their activity drops off. Churn = 90 days inactive.")
    assert reason2 == "replayed"
    assert again.target_column == _STATUS


def test_an_off_shortlist_target_is_treated_as_abstain_never_trusted(db):
    _catalog(db)
    ticket, reason = extract_intake_ticket(
        db, _ticket_client(target="public.customers.INVENTED_flg"),
        catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Customers churn when activity drops.")
    assert reason == "extracted"
    assert ticket.target_column is None, "a name not in the catalog never feeds the veto"
    assert ticket.confidence == "abstain"


def test_off_vocabulary_domains_are_dropped_not_trusted(db):
    _catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _ticket_client(domains=("retail_churn", "made_up_domain")),
        catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Customers churn when activity drops.")
    assert "retail_churn" in ticket.business_domain
    assert "made_up_domain" not in ticket.business_domain


def test_no_client_degrades_and_a_pinned_name_still_works(db):
    """Mandatory to ATTEMPT, never load-bearing: the exact-name half is pure code."""
    _catalog(db)
    ticket, reason = extract_intake_ticket(
        db, None, catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Predict cust_status_flg from activity.")
    assert reason == "unavailable"
    assert ticket.target_column == _STATUS and ticket.pinned is True
    assert ticket.target_window_days is None
    assert ticket.confidence == "abstain"


def test_a_window_of_zero_means_not_stated(db):
    _catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _ticket_client(window=0), catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Predict cust_status_flg.")
    assert ticket.target_window_days is None, "0 is the schema's 'not stated', mapped to honest None"


def test_runners_up_are_selection_validated_ranked_and_never_the_target(db):
    """Prompt v2's Change-it menu obeys the same discipline as the target: ⊆ the shortlist, the
    chosen target excluded, an invented ref dropped, order preserved."""
    _catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _ticket_client(runners=(_SUSP, "public.customers.INVENTED", _STATUS)),
        catalog_source=SOURCE, roles=("data_owner",),
        hypothesis="Customers leave when activity drops off.")
    assert ticket.target_column == _STATUS
    assert ticket.runners_up == (_SUSP,),         "invented dropped, the target itself excluded, the real runner-up kept in rank order"
