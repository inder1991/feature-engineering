"""TASK 1 — needs must be producible TODAY, plus ordered alternate concepts (hygiene, not recovery).

The silent-death class this closes: ``Need.concept`` validated against ``CONCEPT_REGISTRY``, which
keeps retired legacy aliases for fact-key stability — so a need could target ``counterparty_id``,
pass validation, and never ground again (the classifier stopped producing it; matching is string
equality). Measured before building: fixing it recovers ZERO templates today — this is correctness,
not feature gain, and it bites the moment a catalog arrives that WOULD satisfy the rest.

The companion capability (SME triage F1): ``Need(alternates=(...))`` — ordered fallbacks, FIRST
MATCH WINS, consulted only when the concept before them matched NOTHING. ``tenure_days``' author
wanted exactly this and could only write it as a comment.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import canonical_concept_name, is_classifier_producible
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    Need,
    Template,
    ground_template_outcome,
)

SOURCE = "altbank"


def test_every_authored_need_is_producible_by_the_classifier_today():
    """The plan's named test — it would have FAILED the moment `counterparty_id` was retired."""
    for template in ALL_TEMPLATES:
        for need in template.needs:
            for concept_name in (need.concept, *need.alternates):
                assert is_classifier_producible(canonical_concept_name(concept_name)), (
                    f"template {template.id!r} need {need.role!r} targets {concept_name!r}, "
                    "which no fresh classification can produce")


def test_an_authored_alias_matches_both_spellings_without_merging_them(db):
    """The verified-before-choosing design: the authored spelling is PRESERVED and matching is
    two-tier, so `fan_in_fan_out`'s counterparty leg (authored `counterparty_id`) still prefers a
    stored-alias column over the true `customer_id` column — the two party legs never merge — while
    a fresh catalog (which can only store the successor) still grounds through the weaker tier."""
    rows = [
        (CanonicalRow(SOURCE, "wires", "cpty_ref", "integer",
                      definition="the counterparty of the wire"), "counterparty_id"),
        (CanonicalRow(SOURCE, "wires", "cust_ref", "integer", is_grain=True, entity="Customer",
                      definition="the customer this wire belongs to"), "customer_id"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    probe = Template(
        id="party_probe", family="probe", intent="distinct counterparties per customer",
        needs=(Need("counterparty", "counterparty_id"), Need("entity", "customer_id")),
        params={}, aggregation="probe", additivity="n/a", explain="H",
        use_cases=("retail_churn",), pit="",
        source_entity_need_role="entity")   # two entity-linked needs -> name the source grain
    outcome = ground_template_outcome(db, probe, catalog_source=SOURCE, roles=("data_owner",))
    assert outcome.feature is not None
    bound = {r.role: r.selected_object_ref for r in outcome.feature.binding_resolutions}
    assert bound["counterparty"] == "public.wires.cpty_ref", "exact authored match outranks"
    assert bound["entity"] == "public.wires.cust_ref", "…and the legs bind DISTINCT columns"


def test_a_successorless_retired_alias_is_refused_at_validation():
    from featuregen.overlay.upload.templates import _validate_family

    probe = Template(
        id="alias_probe", family="probe", intent="probe",
        needs=(Need("amt", "monetary_amount"),),   # retired, no unambiguous successor
        params={}, aggregation="probe", additivity="n/a", explain="H",
        use_cases=("retail_churn",), pit="")
    with pytest.raises(ValueError, match="no fresh classification can produce"):
        _validate_family((probe,), "probe", set())


# ── alternates: first match wins, tried only on a total primary miss ─────────────────────────────

def _catalog(db, *, with_primary: bool):
    rows = [
        (CanonicalRow(SOURCE, "loans", "cust_ref", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow(SOURCE, "loans", "eff_dt", "date",
                      definition="when the facility became effective"), "effective_date"),
    ]
    if with_primary:
        rows.append(
            (CanonicalRow(SOURCE, "loans", "orig_dt", "date",
                          definition="when the facility was originated"), "origination_date"))
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


_PROBE = Template(
    id="alt_probe", family="probe", intent="tenure from origination, else effective",
    needs=(Need("entity", "customer_id"),
           Need("start", "origination_date", alternates=("effective_date",))),
    params={}, aggregation="probe", additivity="n/a", explain="H",
    use_cases=("retail_churn",), pit="")


def test_the_primary_wins_when_it_matches_and_the_alternate_is_never_consulted(db):
    _catalog(db, with_primary=True)
    outcome = ground_template_outcome(db, _PROBE, catalog_source=SOURCE, roles=("data_owner",))
    assert outcome.feature is not None
    refs = {ref for _src, ref in outcome.feature.derives_pairs}
    assert "public.loans.orig_dt" in refs
    assert "public.loans.eff_dt" not in refs


def test_the_alternate_binds_only_on_a_total_primary_miss(db):
    """The F1 shape: no origination column exists — the authored fallback grounds the template
    instead of it silently dying (or binding a semantically wrong date)."""
    _catalog(db, with_primary=False)
    outcome = ground_template_outcome(db, _PROBE, catalog_source=SOURCE, roles=("data_owner",))
    assert outcome.feature is not None, "the alternate rescued a template the primary lost"
    assert "public.loans.eff_dt" in {ref for _src, ref in outcome.feature.derives_pairs}


def test_no_alternates_is_byte_identical_todays_single_pass(db):
    _catalog(db, with_primary=False)
    bare = Template(
        id="bare_probe", family="probe", intent="probe",
        needs=(Need("entity", "customer_id"), Need("start", "origination_date")),
        params={}, aggregation="probe", additivity="n/a", explain="H",
        use_cases=("retail_churn",), pit="")
    outcome = ground_template_outcome(db, bare, catalog_source=SOURCE, roles=("data_owner",))
    assert outcome.feature is None, "without alternates the primary miss ungrounds — exactly today"
