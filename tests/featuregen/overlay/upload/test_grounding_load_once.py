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

Section 4 covers the P4 consumer of the same seam: ``ground_all_outcomes(table=...)``, which narrows
the loaded list to one table. It rides on the identical hazard — the list is read-scoped — so the
narrowing is proved to be a pure SUBSET of what the caller could already see, never a way to name a
table into visibility.
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
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.suggestions import suggest_features_for_table
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    ground_all_outcomes,
    ground_template_outcome,
)

CHURN_SOURCE = "churn"
_RESTRICTED = {"public.cards.full_name", "public.cards.beneficiary_name"}


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


# ── 4. The P4 per-table narrowing — it can only NARROW ───────────────────────────────────────────
# `ground_all_outcomes(table=...)` scopes the candidate columns to ONE table, because the pass yields
# at most one candidate per template and catalog-wide the first table to bind a recipe uses it up.
# The hazard is the same one as the hoist: the column list is READ-SCOPED, so the filter must be a
# pure subset of what these roles could already see — never a way to name a table into visibility.
_ONE_SOURCE = "onetable"
_ONE = [
    (CanonicalRow(_ONE_SOURCE, "cards", "customer_id", "integer", is_grain=True, entity="Customer"), "customer_id"),
    (CanonicalRow(_ONE_SOURCE, "cards", "balance", "numeric", additivity="semi_additive", currency="USD"), "monetary_stock"),
    (CanonicalRow(_ONE_SOURCE, "cards", "snapshot_date", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(_ONE_SOURCE, "cards", "amount", "numeric", additivity="additive", currency="USD"), "monetary_flow"),
    (CanonicalRow(_ONE_SOURCE, "cards", "txn_ts", "timestamp"), "event_timestamp"),
    (CanonicalRow(_ONE_SOURCE, "cards", "full_name", "text", sensitivity="pii"), "pii"),
    (CanonicalRow(_ONE_SOURCE, "cards", "beneficiary_name", "text", sensitivity="pii"), "beneficiary_name"),
    (CanonicalRow(_ONE_SOURCE, "cards", "beneficiary_bank", "text"), "beneficiary_bank"),
    (CanonicalRow(_ONE_SOURCE, "other", "amount", "numeric", additivity="additive", currency="USD"), "monetary_flow"),
]


def _one_table_catalog(db):
    """ONE table carrying a whole recipe's worth of concepts, INCLUDING two restricted columns — the
    only shape that can show a per-table pass differing by read scope, because a recipe that reaches
    across tables cannot ground per-table at all."""
    build_graph(db, _ONE_SOURCE, [r for r, _c in _ONE],
                concepts={content_hash(r): c for r, c in _ONE if c})


def _refs(outcomes):
    return {ref for o in outcomes if o.feature is not None for _src, ref in o.feature.derives_pairs}


def test_the_table_filter_never_widens_a_callers_read_scope(db):
    """P4 — ``table=`` narrows the candidate columns to one table. It is applied AFTER the read scope,
    so it can only ever REMOVE candidates: two callers still see correspondingly different results and
    the narrower caller never reaches a restricted column by naming a table."""
    _one_table_catalog(db)
    unscoped = ground_all_outcomes(
        db, ALL_TEMPLATES, catalog_source=_ONE_SOURCE, roles=(), table="cards")
    pii = ground_all_outcomes(
        db, ALL_TEMPLATES, catalog_source=_ONE_SOURCE, roles=("pii_reader",), table="cards")

    # the pii-gated recipe is the observable, per table exactly as catalog-wide
    assert "external_own_transfer_trend_90d" in _names(pii)
    assert "external_own_transfer_trend_90d" not in _names(unscoped)
    assert _names(unscoped) < _names(pii)
    assert not (_refs(unscoped) & _RESTRICTED)
    assert _refs(pii) & _RESTRICTED                  # non-vacuous: the fixture really gates something

    # the filter never reaches BEYOND the named table either
    assert all(ref.startswith("public.cards.") for ref in _refs(unscoped) | _refs(pii))


def test_every_column_a_filtered_pass_binds_is_one_the_caller_could_already_see(db):
    """The general statement of the same guarantee, asserted against the caller's OWN read-scoped
    column list rather than against one gated recipe: a table filter can only ever be a subset."""
    _one_table_catalog(db)
    for roles in ((), ("pii_reader",)):
        visible = {c.object_ref
                   for c in templates_module._load_columns(db, _ONE_SOURCE, roles)}
        for table in ("cards", "other", "no_such_table"):
            outcomes = ground_all_outcomes(
                db, ALL_TEMPLATES, catalog_source=_ONE_SOURCE, roles=roles, table=table)
            assert _refs(outcomes) <= visible, (roles, table)


def test_two_callers_with_different_roles_get_correspondingly_different_suggestions(db):
    """End to end on the read-only endpoint: ``roles`` comes from the authenticated session and is
    handed to the same narrowed grounding pass, so a column the caller may not see is not a candidate
    and cannot be suggested — the per-table pass changes nothing about that.

    REWRITTEN 2026-08-04, when the feature USE gate landed. The observable this test used to read
    was the SUGGESTION ``external_own_transfer_trend_90d``, and the fixture's only role-
    differentiating columns are ``full_name`` and ``beneficiary_name``, both concept-tagged
    personal data. That recipe's own ``eligibility`` field says "consent / purpose / residency
    REQUIRED"; no such policy exists, so ``_use_gate`` refuses it — and refuses it for BOTH
    callers, because whether a feature may be BUILT is a property of the feature, not of who is
    looking. Weakening the gate to keep the old observable would have been weakening it to keep a
    test green.

    So the same guarantee is stated where it now shows. The two callers still get correspondingly
    different results; the difference moved from ``suggestions`` to ``rejections``, which is the
    honest place for it — the pii_reader is told what exists and why it cannot be built, and the
    caller who cannot see the columns is not told the recipe exists at all. That second half is the
    one that actually matters for read scope, and it is the half this test always owned.
    """
    _one_table_catalog(db)
    unscoped = suggest_features_for_table(
        db, catalog_source=_ONE_SOURCE, table="cards", roles=())
    pii = suggest_features_for_table(
        db, catalog_source=_ONE_SOURCE, table="cards", roles=("pii_reader",))

    def _suggested(out):
        return {s["name"] for g in out["groups"] for s in g["suggestions"]}

    def _used(out):
        return {ref for g in out["groups"] for s in g["suggestions"] for ref in s["uses"]}

    def _refused(out):
        return {(r.get("name"), r.get("code")) for r in out["rejections"]}

    # READ SCOPE, which is what this test is for: the pii-only recipe reaches the pii_reader's
    # result and is entirely absent from the unscoped caller's — not as a suggestion, not as a
    # rejection, not as an existence hint.
    assert ("external_own_transfer_trend_90d", "PERSONAL_DATA_POLICY_REQUIRED") in _refused(pii)
    assert not any(name.startswith("external_own_transfer_trend")
                   for name, _code in _refused(unscoped)), _refused(unscoped)
    assert "external_own_transfer_trend_90d" not in _suggested(unscoped)

    # THE USE GATE, which is role-INDEPENDENT: neither caller may build it, so the two suggestion
    # sets are now equal and NEITHER binds a restricted column.
    assert _suggested(unscoped) == _suggested(pii), (
        "a use refusal became a function of the caller's roles — the same feature must not be "
        "safe for one reviewer and unsafe for another")
    assert not (_used(unscoped) & _RESTRICTED)
    assert not (_used(pii) & _RESTRICTED)
    assert _suggested(pii), "both callers got nothing at all; the comparison above is vacuous"
