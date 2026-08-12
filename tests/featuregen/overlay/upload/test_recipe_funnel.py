"""STEP 0 — the catalog-wide recipe funnel: every template's fate, with EVERY unmet need named.

WHAT EXISTED BEFORE THIS (second-review correction to the plan's own premise): the gauntlet's
per-recipe reject codes were ALREADY on the wire — the v2 payload's `collection.rejections` carries
`{template_id, code, explanation}`, measured live (`ftr`: 9 shown + 14 rejections, 11 of them
CURRENCY_POLICY_REQUIRED). What was genuinely invisible is the OTHER side of the funnel: the ~134
templates that never ground at all. `GroundingOutcome` stops at the FIRST unmet need
(`("required_need_missing", role)` — an early return) and names the ROLE, not the concept — so
"which concepts block how many recipes" (the histogram that sized every task in the plan, computed
by hand in kubectl four times this week) had no product surface.

`recipe_funnel` is that surface: one read-scoped pass, the REAL grounding verdict per template
(`ground_all_outcomes` — never a parallel re-implementation that could drift from distinct-binding
or assignment-cap semantics), plus the COMPLETE unmet-need list per unbuildable template, the
blocked-concept histogram, and the grounding stopwatch the Task-2b caching rule demands.
"""
from __future__ import annotations

from tests.featuregen.overlay.upload.test_templates import SOURCE, _churn_catalog  # noqa: F401

from featuregen.overlay.upload.templates import ALL_TEMPLATES, recipe_funnel


def _by_id(funnel):
    return {e.template_id: e for e in funnel.entries}


def test_every_template_appears_exactly_once_with_a_status(db):
    _churn_catalog(db)
    funnel = recipe_funnel(db, catalog_source=SOURCE, roles=("data_owner", "pii_reader"))
    assert funnel.registry_total == len(ALL_TEMPLATES)
    assert len(funnel.entries) == len(ALL_TEMPLATES)
    assert len(_by_id(funnel)) == len(ALL_TEMPLATES), "one entry per template, no duplicates"
    assert all(e.status in ("grounded", "unbuildable", "budget_truncated")
               for e in funnel.entries)
    assert funnel.grounded == sum(1 for e in funnel.entries if e.status == "grounded")


def test_a_grounding_template_is_grounded_and_carries_no_unmet_needs(db):
    """The churn fixture holds event_timestamp + customer_id, so `dormancy_days` grounds."""
    _churn_catalog(db)
    funnel = recipe_funnel(db, catalog_source=SOURCE, roles=("data_owner", "pii_reader"))
    entry = _by_id(funnel)["dormancy_days"]
    assert entry.status == "grounded"
    assert entry.unmet == ()


def test_a_blocked_template_names_EVERY_unmet_need_with_role_AND_concept(db):
    """The whole point: `GroundingOutcome` early-returns at the first unmet need and names only the
    role. The funnel names them ALL, with concepts — the histogram's raw material."""
    _churn_catalog(db)
    funnel = recipe_funnel(db, catalog_source=SOURCE, roles=("data_owner", "pii_reader"))
    blocked = [e for e in funnel.entries if e.status == "unbuildable" and e.unmet]
    assert blocked, "the churn fixture cannot satisfy the whole registry"
    for entry in blocked:
        template = next(t for t in ALL_TEMPLATES if t.id == entry.template_id)
        required = {(n.role, n.concept) for n in template.needs if not n.optional}
        assert entry.unmet, "an unbuildable-for-needs entry must say WHICH needs"
        assert set(entry.unmet) <= required, (
            "an unmet need must be one the template actually declares as required")
    # Facility recipes exist and the churn fixture holds no facility_id: at least one entry must
    # name it — this is the assertion that turns the hand-run histogram into a contract.
    assert any("facility_id" in {c for _r, c in e.unmet} for e in blocked)


def test_the_histogram_is_exactly_the_aggregation_of_the_entries(db):
    _churn_catalog(db)
    funnel = recipe_funnel(db, catalog_source=SOURCE, roles=("data_owner", "pii_reader"))
    recount: dict[str, int] = {}
    for e in funnel.entries:
        for _role, concept in e.unmet:
            recount[concept] = recount.get(concept, 0) + 1
    assert dict(funnel.blocked_concepts) == recount


def test_the_stopwatch_runs(db):
    """The Task-2b caching-rule guard: grounding cost arrives as a NUMBER, per pass."""
    _churn_catalog(db)
    funnel = recipe_funnel(db, catalog_source=SOURCE, roles=("data_owner", "pii_reader"))
    assert funnel.elapsed_ms >= 0.0


def test_read_scope_narrows_the_funnel_like_every_other_surface(db):
    """A caller without pii_reader cannot see pii columns, so a template whose only candidate for a
    need is pii-tagged must report that need unmet FOR THAT CALLER — the funnel must never become a
    side channel that reveals what a hidden column could ground."""
    _churn_catalog(db)
    wide = recipe_funnel(db, catalog_source=SOURCE, roles=("data_owner", "pii_reader"))
    narrow = recipe_funnel(db, catalog_source=SOURCE, roles=("data_owner",))
    assert narrow.grounded <= wide.grounded
    # beneficiary_name is pii in the fixture; recipes requiring it ground only for the wide caller.
    wide_ids = {e.template_id for e in wide.entries if e.status == "grounded"}
    narrow_ids = {e.template_id for e in narrow.entries if e.status == "grounded"}
    assert narrow_ids <= wide_ids
