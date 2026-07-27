"""PERF — the read-scoped column list is loaded ONCE per grounding pass, not once per template.

``ground_template_outcome`` used to call ``_load_columns`` itself, so a single pass over the 157-recipe
registry re-read the WHOLE catalog's columns 157 times and threw 156 identical copies away (the
suggestions endpoint grounds twice -> ~314 full catalog scans per page view). ``ground_all_outcomes``
now loads once and passes the list down.

This is a PURE performance change, so the tests here are about EQUIVALENCE and SCOPE, not speed:

* :func:`test_hoisted_pass_equals_per_template_load_on_the_real_ftr_catalog` — on the REAL FTR ingest
  the hoisted pass is EQUAL, structure for structure, to the same pass computed the old way
  (per-template load): same grounded names / aggregations / derives_pairs / params /
  binding_resolutions AND the same rejected-template set.
* :func:`test_hoist_never_leaks_one_callers_read_scope_into_another` — the ONE real hazard. The list
  is read-scoped (a pii/restricted column is a grounding candidate only for a caller whose roles grant
  it), so a hoisted list must stay pinned to the SAME ``(catalog_source, roles)`` for its own pass and
  must never survive into the next one. Two passes with different roles must still differ.
* :func:`test_columns_are_loaded_exactly_once_per_pass` — the fix's whole point, pinned.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.test_gating_confirm_lift import (  # noqa: F401 — fixture reuse
    SOURCE as FTR_SOURCE,
)
from tests.featuregen.overlay.upload.test_gating_confirm_lift import (  # noqa: F401 — fixture reuse
    ai_proposed_catalog,
)
from tests.featuregen.overlay.upload.test_templates import _CATALOG, _churn_catalog

from featuregen.overlay.upload import templates as templates_module
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    ground_all_outcomes,
    ground_template_outcome,
)

CHURN_SOURCE = "churn"


def _per_template_load(conn, templates, *, catalog_source, roles=()):
    """The OLD way — every template loads the catalog's columns for itself. The control for the
    equivalence proof; deliberately NOT calling the loop function under test."""
    return [
        ground_template_outcome(conn, t, catalog_source=catalog_source, roles=roles)
        for t in templates
    ]


def _shape(outcomes):
    """A readable projection of the whole result: per template its status + reason codes and, when it
    grounded, the fields a consumer actually renders."""
    return [
        (o.template_id, o.status, o.reason_codes,
         None if o.feature is None else (
             o.feature.name, o.feature.aggregation, o.feature.grain_table, o.feature.as_of_column,
             o.feature.derives_pairs, tuple(sorted(o.feature.params.items())),
             o.feature.binding_resolutions, o.feature.role_bindings, o.feature.notes,
             o.feature.eligibility, o.feature.pit, o.feature.additivity, o.feature.near_label))
        for o in outcomes
    ]


def _rejected(outcomes):
    return {o.template_id for o in outcomes if o.feature is None}


def _names(outcomes):
    return {o.feature.name for o in outcomes if o.feature is not None}


# ── 1. Equivalence on the real FTR catalog ───────────────────────────────────────────────────────
def test_hoisted_pass_equals_per_template_load_on_the_real_ftr_catalog(
        overlay_conn, ai_proposed_catalog):  # noqa: F811
    """The load-bearing test: byte-identical output. Whole structure, not a count."""
    hoisted = ground_all_outcomes(
        overlay_conn, ALL_TEMPLATES, catalog_source=FTR_SOURCE, roles=())
    control = _per_template_load(
        overlay_conn, ALL_TEMPLATES, catalog_source=FTR_SOURCE, roles=())

    assert _names(hoisted), "the FTR fixture grounded nothing — the equivalence proof would be vacuous"
    assert _shape(hoisted) == _shape(control)
    assert _rejected(hoisted) == _rejected(control)
    assert hoisted == control                       # full dataclass equality, every field


def test_hoisted_pass_equals_per_template_load_under_a_pii_read_scope(db):
    """Same equivalence with a role that OPENS restricted columns — the hoisted list must carry the
    same widened scope every per-template load would have used."""
    _churn_catalog(db)
    for roles in ((), ("pii_reader",)):
        hoisted = ground_all_outcomes(
            db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=roles)
        control = _per_template_load(
            db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=roles)
        assert _shape(hoisted) == _shape(control), f"diverged for roles={roles}"
        assert hoisted == control


# ── 2. Read-scoping is preserved (the one real hazard) ───────────────────────────────────────────
def test_hoist_never_leaks_one_callers_read_scope_into_another(db):
    """Two passes over the SAME catalog with DIFFERENT roles return correspondingly DIFFERENT results,
    in either order and on repeat — so no pass's column list survived into another's."""
    _churn_catalog(db)
    assert any(r.sensitivity == "pii" for r, _c in _CATALOG), "fixture no longer has a restricted column"

    unscoped_first = _names(ground_all_outcomes(
        db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=()))
    pii = _names(ground_all_outcomes(
        db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",)))
    unscoped_again = _names(ground_all_outcomes(
        db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=()))

    # The pii-gated recipe is the observable: visible ONLY to the caller whose roles grant it.
    assert "external_own_transfer_trend_90d" in pii
    assert "external_own_transfer_trend_90d" not in unscoped_first
    # A leak in either direction would show up here: the unscoped pass must be unchanged by the
    # pii pass that ran between its two runs, and must stay strictly narrower.
    assert unscoped_again == unscoped_first
    assert unscoped_first < pii


def test_hoist_never_leaks_one_catalogs_columns_into_another(db):
    """A second catalog in the same connection grounds on its OWN columns — the hoist is per pass,
    never a process-wide cache."""
    _churn_catalog(db)
    empty = ground_all_outcomes(
        db, ALL_TEMPLATES, catalog_source="no_such_catalog", roles=("pii_reader",))
    assert _names(empty) == set()
    assert _names(ground_all_outcomes(
        db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",)))


# ── 3. The load happens ONCE per pass ────────────────────────────────────────────────────────────
@pytest.fixture
def load_spy(monkeypatch):
    """Counts every ``_load_columns`` call, recording the (catalog_source, roles) scope of each."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    real = templates_module._load_columns

    def _spy(conn, catalog_source, roles):
        calls.append((catalog_source, tuple(roles)))
        return real(conn, catalog_source, roles)

    monkeypatch.setattr(templates_module, "_load_columns", _spy)
    return calls


def test_columns_are_loaded_exactly_once_per_pass(db, load_spy):
    _churn_catalog(db)
    load_spy.clear()
    ground_all_outcomes(db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))
    assert load_spy == [(CHURN_SOURCE, ("pii_reader",))]     # ONE load, at the caller's own scope


def test_the_old_way_loaded_once_per_template(db, load_spy):
    """Pins the win: the control path still pays one full catalog read per template (157 today)."""
    _churn_catalog(db)
    load_spy.clear()
    _per_template_load(db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))
    assert len(load_spy) == len(ALL_TEMPLATES)
