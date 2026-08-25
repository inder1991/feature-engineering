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


# ── T7's arrangement: the 2026-08-24 AML run, reproduced ─────────────────────────────────────────
# The live catalog's own shape — a `bo_cib_customer` table whose only outcome-ISH columns are
# compliance CONSEQUENCES (`restriction_status`, `restriction_reason`), and NOT ONE column carrying
# a `leakage_anchor` concept. The run committed at confidence=medium to `cust_susp_flg` anyway.
_CIB = "cib"
_CIB_SUSP = "public.bo_cib_customer.cust_susp_flg"
_CIB_STATUS = "public.bo_cib_customer.cust_status_flg"
_CIB_REASON = "public.bo_cib_customer.cust_susp_reason_cd"

_CIB_ROWS = [
    (CanonicalRow(_CIB, "bo_cib_customer", "cust_susp_flg", "text",
                  definition="Whether the customer is suspended, negated or blacklisted — set by "
                             "compliance action, and AML- and KYC-relevant."),
     "restriction_status"),
    (CanonicalRow(_CIB, "bo_cib_customer", "cust_status_flg", "text",
                  definition="Current lifecycle status of the customer relationship."),
     "customer_relationship_status"),
    (CanonicalRow(_CIB, "bo_cib_customer", "cust_susp_reason_cd", "text",
                  definition="Why the servicing restriction was applied."),
     "restriction_reason"),
]

#: The owner's brief, verbatim in the two parts this task turns on — the AML framing (which has no
#: outcome-family column anywhere in this catalog) and the stated horizon the ticket returned 0 for.
_AML_GOAL = ("Identify commercial banking customers likely to be flagged for AML review in the "
             "next 90 days.")


#: The reviewer's probe fixture: the SAME near-label catalog, plus one real outcome-family column.
#: `fraud_flag` declares `leakage_anchor=True`, so this is the label itself standing right beside
#: the proxy the run picked.
_CIB_LABEL = "public.bo_cib_customer.aml_sar_filed_flg"
_CIB_LABEL_ROW = (CanonicalRow(_CIB, "bo_cib_customer", "aml_sar_filed_flg", "text",
                               definition="Whether a SAR was filed for this customer."),
                  "fraud_flag")


def _cib_catalog(db, *, with_label: bool = False):
    rows = [*_CIB_ROWS, _CIB_LABEL_ROW] if with_label else list(_CIB_ROWS)
    build_graph(db, _CIB, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows if c})


def _cib_client(target: str = _CIB_SUSP, window: int = 0, confidence: str = "medium",
                runners=(_CIB_STATUS,)) -> FakeLLM:
    """The run's own answer: `cust_susp_flg` at medium confidence, window 0 against "90 days"."""
    return FakeLLM(script={INTAKE_TICKET_TASK: FakeResponse(output={
        "target_ref": target, "target_window_days": window,
        "target_type": "binary_classification", "business_domain": [],
        "confidence": confidence, "runner_up_refs": list(runners)})})


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


# ══ T7 (a) — ABSTAIN-BY-DEFAULT: no outcome-family concept, no commit ════════════════════════════

def test_no_outcome_family_concept_in_the_catalog_ABSTAINS_and_names_the_proxies(db):
    """The 2026-08-24 AML run's own arrangement. `cust_susp_flg` carries `restriction_status`, whose
    registry record says `near_label=True` — a compliance CONSEQUENCE, not the AML outcome. Nothing
    in this catalog carries a `leakage_anchor` concept, so the honest answer is the morning run's:
    abstain, and hand back the nearest proxies with the concept each one actually carries."""
    _cib_catalog(db)
    ticket, reason = extract_intake_ticket(
        db, _cib_client(), catalog_source=_CIB, roles=("data_owner",), hypothesis=_AML_GOAL)
    assert reason == "extracted"
    assert ticket.confidence == "abstain", \
        "the run committed at medium to a near-label proxy; only an outcome-family concept commits"
    assert ticket.target_is_proxy is True
    assert ticket.target_leakage_class == "near_label"
    assert ticket.target_concept == "restriction_status"
    # the abstention answer is DATA: ranked proxies, each labelled with its REAL concept
    assert [(c.ref, c.concept, c.leakage_class) for c in ticket.proxy_candidates] == [
        (_CIB_SUSP, "restriction_status", "near_label"),
        (_CIB_REASON, "restriction_reason", "near_label"),
        (_CIB_STATUS, "customer_relationship_status", "standard"),
    ], "near-label proxies rank above ordinary columns; the model's own order breaks ties"
    assert not any(c.leakage_class == "outcome" for c in ticket.proxy_candidates)
    assert ticket.outcome_candidates == (), "this catalog genuinely holds no label — say nothing"


def test_an_outcome_family_concept_is_the_ONE_thing_that_commits(db):
    """The other side of the same rule — `fraud_flag` declares `leakage_anchor=True`, so a target
    landing on it is the label itself and the model's confidence stands."""
    rows = [(CanonicalRow(_CIB, "bo_cib_customer", "aml_sar_filed_flg", "text",
                          definition="Whether a SAR was filed for this customer."), "fraud_flag")]
    build_graph(db, _CIB, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    ref = "public.bo_cib_customer.aml_sar_filed_flg"
    ticket, _ = extract_intake_ticket(
        db, _cib_client(target=ref, confidence="high", runners=()),
        catalog_source=_CIB, roles=("data_owner",), hypothesis=_AML_GOAL)
    assert ticket.target_column == ref
    assert (ticket.confidence, ticket.target_is_proxy) == ("high", False)
    assert ticket.target_leakage_class == "outcome"
    assert ticket.proxy_candidates == (), "there is nothing to fall back to — this IS the label"


def test_a_target_with_no_registered_concept_is_uncommittable_but_is_NOT_called_a_proxy(db):
    """Absence is not an assertion (the `is_descriptive` precedent). An unclassified column gives no
    warrant that it is the label, so it cannot commit — but calling it a PROXY would be a claim the
    registry never made."""
    rows = [(CanonicalRow(_CIB, "bo_cib_customer", "mystery_cd", "text",
                          definition="Unknown."), None)]
    build_graph(db, _CIB, [r for r, _ in rows], concepts={})
    ref = "public.bo_cib_customer.mystery_cd"
    ticket, _ = extract_intake_ticket(
        db, _cib_client(target=ref, confidence="high", runners=()),
        catalog_source=_CIB, roles=("data_owner",), hypothesis=_AML_GOAL)
    assert ticket.confidence == "abstain"
    assert ticket.target_leakage_class is None
    assert ticket.target_is_proxy is False
    assert ticket.target_concept == ""


def test_a_model_abstention_still_hands_back_the_ranked_proxies(db):
    """The morning run, institutionalised: the model chose nothing, and the answer is still data."""
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(target="", runners=()), catalog_source=_CIB, roles=("data_owner",),
        hypothesis=_AML_GOAL)
    assert ticket.target_column is None and ticket.confidence == "abstain"
    assert [c.ref for c in ticket.proxy_candidates] == [_CIB_SUSP, _CIB_REASON]


# ══ T7 (b) — the window contradiction ════════════════════════════════════════════════════════════

def test_a_stated_horizon_against_a_zero_window_is_a_TYPED_refusal_naming_both_numbers(db):
    """"in the next 90 days" + `target_window_days: 0`. Nothing cross-checked this on the live run,
    and the near-label critic downstream then abstained without saying why."""
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(window=0), catalog_source=_CIB, roles=("data_owner",),
        hypothesis=_AML_GOAL)
    assert ticket.window_refusal is not None
    assert ticket.window_refusal.code == "WINDOW_CONTRADICTS_GOAL"
    assert ticket.window_refusal.stated_days == 90
    assert ticket.window_refusal.ticket_days == 0
    assert "90" in ticket.window_refusal.detail and "0" in ticket.window_refusal.detail
    assert ticket.target_window_days is None, "a contradicted window is never accepted"
    assert ticket.window_source == "contradicted"


def test_a_mismatched_horizon_is_the_same_typed_refusal(db):
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(window=30), catalog_source=_CIB, roles=("data_owner",),
        hypothesis=_AML_GOAL)
    assert ticket.window_refusal is not None
    assert (ticket.window_refusal.stated_days, ticket.window_refusal.ticket_days) == (90, 30)
    assert ticket.target_window_days is None


def test_a_horizon_the_reading_AGREES_with_is_recorded_as_stated(db):
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(window=90), catalog_source=_CIB, roles=("data_owner",),
        hypothesis=_AML_GOAL)
    assert (ticket.window_refusal, ticket.target_window_days) == (None, 90)
    assert ticket.window_source == "stated"


def test_no_stated_horizon_and_no_window_is_HONEST_ABSENCE_not_a_refusal(db):
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(window=0), catalog_source=_CIB, roles=("data_owner",),
        hypothesis="Identify commercial banking customers likely to be flagged for AML review.")
    assert ticket.window_refusal is None
    assert ticket.target_window_days is None
    assert ticket.window_source == "unstated"


def test_a_window_the_goal_never_stated_passes_through_as_the_model_s_alone(db):
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(window=90), catalog_source=_CIB, roles=("data_owner",),
        hypothesis="Predict AML review. Churn = 90 days of inactivity.")
    assert (ticket.window_refusal, ticket.target_window_days) == (None, 90)
    assert ticket.window_source == "model_only", \
        "'90 days of inactivity' is a DEFINITION, not a stated horizon — no claim is made"


def test_horizon_extraction_is_conservative_and_unit_exact(db):
    """The patterns, pinned one at a time. Weeks convert exactly; a MONTH has no exact day count,
    so it states a horizon without a comparable number — and two horizons is no claim at all."""
    from featuregen.overlay.upload.contract.intake_ticket import stated_horizon

    assert stated_horizon("flagged for AML review in the next 90 days").days == 90
    assert stated_horizon("flagged within 30 days of onboarding").days == 30
    assert stated_horizon("a 14-day window after the alert").days == 14
    assert stated_horizon("reviewed in the next 6 weeks").days == 42
    months = stated_horizon("reviewed in the next 3 months")
    assert months is not None and months.days is None and months.text == "3 months"
    assert stated_horizon("no activity in 90 days") is None, "a definition is not a horizon"
    assert stated_horizon("within 30 days, or in the next 90 days") is None, "two horizons: no claim"
    assert stated_horizon("flagged within ninety days") is None, "words are not extracted"


def test_a_month_horizon_still_catches_a_missing_window_without_inventing_a_day_count(db):
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(window=0), catalog_source=_CIB, roles=("data_owner",),
        hypothesis="Identify customers flagged for AML review in the next 3 months.")
    assert ticket.window_refusal is not None
    assert ticket.window_refusal.stated_days is None
    assert "3 months" in ticket.window_refusal.detail
    # ...but a number against a month horizon is never called a contradiction. A DIFFERENT
    # objective, because the same one would replay the ticket above rather than re-ask.
    other, _ = extract_intake_ticket(
        db, _cib_client(window=92), catalog_source=_CIB, roles=("data_owner",),
        hypothesis="Identify corporate customers flagged for AML review in the next 3 months.")
    assert (other.window_refusal, other.target_window_days) == (None, 92)
    assert other.window_source == "model_only"


def test_the_outcome_family_is_EXACTLY_what_the_registry_already_declares(db):
    """The derivation itself, pinned by MEMBERSHIP — no new taxonomy was invented here.

    OUTCOME is ``Concept.leakage_anchor``, the field `templates._safe_to_bind` already refuses to
    build features from. PROXY is ``Concept.near_label``, "funnel-tail signals that BORDER the
    label". The class NAMES come from `recipe_contract_v2.LEAKAGE_CLASSES`. Growing either set
    changes what the platform will commit a target to, so it must be a conscious act — and the two
    counts in `intake_ticket`'s own comment must move with it.
    """
    from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY
    from featuregen.overlay.upload.contract.intake_ticket import target_leakage_class
    from featuregen.overlay.upload.recipe_contract_v2 import LEAKAGE_CLASSES

    outcome = {n for n in CONCEPT_REGISTRY if target_leakage_class(n) == "outcome"}
    assert outcome == {"outcome_label", "lapsed", "surrendered", "settlement_fail", "redeemed",
                       "delinquency_flag", "default_flag", "fraud_flag"}, \
        "eight concepts, as the module comment says"
    near = {n for n in CONCEPT_REGISTRY if target_leakage_class(n) == "near_label"}
    assert len(near) == 13 and "restriction_status" in near
    assert target_leakage_class("no_such_concept") is None, "absence is not an assertion"
    assert target_leakage_class(None) is None
    assert set(LEAKAGE_CLASSES) == {"standard", "near_label", "outcome"}, \
        "the class names are the recipe contract's, looked up rather than re-spelled"


# ══ FIX ROUND — NB-3/NB-5: when the catalog HOLDS a label, say so ════════════════════════════════

def test_an_outcome_column_in_the_catalog_is_NAMED_and_is_never_listed_as_a_proxy(db):
    """The reviewer's probe: `cust_susp_flg` (the proxy the run picked) standing beside
    `aml_sar_filed_flg` (`fraud_flag`, `leakage_anchor=True` — the label itself).

    Abstaining while the answer to the question sits in the same table and goes unmentioned is its
    own kind of silence. The outcome columns are CATALOG-DERIVED data on their own field: this
    reports what exists, it never substitutes a target the model did not pick (SELECTION
    discipline). And a label is never a proxy for itself, so it must appear in exactly one list.

    The model RANKS the real label second here, which is the shape that actually exercises the
    exclusion: a label reaches the proxy list only by being picked or ranked, so a fixture where
    nobody ranked it cannot tell the exclusion from its absence.
    """
    _cib_catalog(db, with_label=True)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(runners=(_CIB_LABEL, _CIB_STATUS)), catalog_source=_CIB,
        roles=("data_owner",), hypothesis=_AML_GOAL)
    assert ticket.target_column == _CIB_SUSP and ticket.confidence == "abstain"
    assert _CIB_LABEL in ticket.runners_up, "the model did rank it — this is not a no-op fixture"
    assert [(c.ref, c.concept, c.leakage_class) for c in ticket.outcome_candidates] == [
        (_CIB_LABEL, "fraud_flag", "outcome")]
    assert _CIB_LABEL not in [c.ref for c in ticket.proxy_candidates], \
        "a label is not a proxy for itself — even when the model ranked it among the runners-up"
    assert all(c.leakage_class != "outcome" for c in ticket.proxy_candidates)


def test_a_committed_outcome_target_hands_back_neither_list(db):
    _cib_catalog(db, with_label=True)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(target=_CIB_LABEL, confidence="high", runners=()),
        catalog_source=_CIB, roles=("data_owner",), hypothesis=_AML_GOAL)
    assert (ticket.confidence, ticket.target_leakage_class) == ("high", "outcome")
    assert (ticket.proxy_candidates, ticket.outcome_candidates) == ((), ()), \
        "the target IS the label — there is nothing to fall back to and nothing to point at"


# ══ FIX ROUND — NB-1(a): `target_is_proxy` asserts label-adjacency, and only that ═════════════════

def test_a_STANDARD_class_target_is_uncommittable_but_is_NOT_a_proxy(db):
    """`customer_relationship_status` is `standard`: the registry positively DEclassified it —
    neither the label nor label-adjacent. Refusing to commit to it is warranted; calling it "a
    proxy for the AML outcome" is a correlation claim the registry never made."""
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, _cib_client(target=_CIB_STATUS, runners=()), catalog_source=_CIB,
        roles=("data_owner",), hypothesis=_AML_GOAL)
    assert ticket.target_leakage_class == "standard"
    assert ticket.confidence == "abstain", "still uncommittable — nothing certifies it as a label"
    assert ticket.target_is_proxy is False, \
        "`is_proxy` is true only where the registry ASSERTS label-adjacency (near_label)"


# ══ FIX ROUND — NB-4: a degraded ticket still reads the goal text ════════════════════════════════

def test_a_degraded_ticket_still_takes_the_horizon_the_GOAL_states(db):
    """No client, no model reading — but `stated_horizon` is pure code and never needed one. The
    old answer said `window_source: "unstated"` against an objective that plainly says 90 days."""
    _cib_catalog(db)
    ticket, reason = extract_intake_ticket(
        db, None, catalog_source=_CIB, roles=("data_owner",), hypothesis=_AML_GOAL)
    assert reason == "unavailable"
    assert ticket.target_window_days == 90
    assert ticket.window_source == "stated"
    assert ticket.window_refusal is None, \
        "never a contradiction about a model reading that never happened"


def test_a_degraded_ticket_with_an_uncountable_horizon_claims_no_number(db):
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, None, catalog_source=_CIB, roles=("data_owner",),
        hypothesis="Identify customers flagged for AML review in the next 3 months.")
    assert (ticket.target_window_days, ticket.window_source) == (None, "stated")
    assert ticket.window_refusal is None


def test_a_degraded_ticket_against_a_horizonless_goal_is_still_honest_absence(db):
    _cib_catalog(db)
    ticket, _ = extract_intake_ticket(
        db, None, catalog_source=_CIB, roles=("data_owner",),
        hypothesis="Identify customers flagged for AML review.")
    assert (ticket.target_window_days, ticket.window_source) == (None, "unstated")


# ══ FIX ROUND — rider: WINDOW_SOURCES was validated by nothing ════════════════════════════════════

def test_every_ticket_window_source_is_a_member_of_the_closed_vocabulary(db):
    from featuregen.overlay.upload.contract.intake_ticket import WINDOW_SOURCES

    _cib_catalog(db)
    for hypothesis, window in ((_AML_GOAL, 0), (_AML_GOAL, 90), ("AML review, no horizon", 0),
                               ("AML review with no horizon at all", 45)):
        ticket, _ = extract_intake_ticket(
            db, _cib_client(window=window), catalog_source=_CIB, roles=("data_owner",),
            hypothesis=hypothesis)
        assert ticket.window_source in WINDOW_SOURCES
    degraded, _ = extract_intake_ticket(
        db, None, catalog_source=_CIB, roles=("data_owner",), hypothesis="a different question")
    assert degraded.window_source in WINDOW_SOURCES
