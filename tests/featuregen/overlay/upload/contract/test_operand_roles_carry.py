"""Operand roles Task 1 — the TEMPLATE AUTHOR's declared role for each bound operand reaches the idea.

The role of every bound operand is already declared by the template (``Need.role``) and already
resolved during grounding (``GroundedFeature.binding_resolutions`` carries ``role`` +
``selected_object_ref``) — it was simply DROPPED at ``derives_pairs``, which is bare
``(catalog_source, object_ref)``. These tests pin the carry-through and, just as importantly, pin
that carrying it changes NOTHING: the same fixture's ``validation_status`` and ``requirements`` are
byte-identical to what the validator returns on its own.

The source is the template author's hand-written declaration — never a concept, a column name or a
data type. An LLM-proposed candidate has no template, so it carries an EMPTY tuple and every
downstream consumer must fall back to today's behaviour on it.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.overlay.upload.test_templates import (
    SOURCE as CHURN_SOURCE,
)
from tests.featuregen.overlay.upload.test_templates import (
    _churn_catalog,
)

import featuregen.overlay.upload.contract.gate1 as gate1
from featuregen.overlay.upload.contract.gate1 import _idea_from_grounded, _template_candidates
from featuregen.overlay.upload.feature_assist import _validate_idea
from featuregen.overlay.upload.templates import (
    RETAIL_CHURN_TEMPLATES,
    BindingResolution,
    GroundedFeature,
    GroundedNeedResolution,
    GroundingOutcome,
    GroundingStatus,
    Need,
    Template,
    ground_template,
)

NOW = datetime(2026, 7, 27, tzinfo=UTC)

_TMPL = Template(
    id="sum_balance", family="balance_stock", intent="total balance per loan",
    needs=(Need(role="stock_col", concept="monetary_stock"),
           Need(role="entity", concept="customer_id")),
    params={}, aggregation="sum", additivity="additive", explain="M", use_cases=(), pit="")

_GF = GroundedFeature(
    template_id="sum_balance", name="sum_balance", aggregation="sum",
    grain_table=None, as_of_column=None,
    derives_pairs=(("ftr", "public.loans.balance"),), params={},
    binding_resolutions=(
        GroundedNeedResolution(role="stock_col", resolution=BindingResolution.UNIQUE,
                               selected_object_ref="public.loans.balance"),
        GroundedNeedResolution(role="entity", resolution=BindingResolution.UNIQUE,
                               selected_object_ref="public.loans.cust_id"),
    ))


def _ftr_numeric_graph(db):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, declared_type) VALUES ('ftr', 'public.loans.balance', 'column', 'loans', "
        "'balance', 'unknown', 'numeric')")
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('ftr', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = %s", (NOW, NOW))


# ── the carry itself ─────────────────────────────────────────────────────────────────────────────
def test_idea_from_grounded_carries_a_role_for_every_bound_operand():
    idea = _idea_from_grounded(_GF, _TMPL)
    assert idea.operand_roles == (
        ("public.loans.balance", "stock_col"),
        ("public.loans.cust_id", "entity"),
    )
    # every carried role is one the TEMPLATE itself declared — nothing inferred
    declared = {need.role for need in _TMPL.needs}
    assert {role for _ref, role in idea.operand_roles} <= declared


def test_operand_roles_are_sorted_so_the_frozen_idea_stays_deterministic():
    gf = GroundedFeature(
        template_id="t", name="t", aggregation="sum", grain_table=None, as_of_column=None,
        derives_pairs=(), params={},
        binding_resolutions=(
            GroundedNeedResolution(role="z_role", resolution=BindingResolution.UNIQUE,
                                   selected_object_ref="public.t.z"),
            GroundedNeedResolution(role="a_role", resolution=BindingResolution.UNIQUE,
                                   selected_object_ref="public.t.a"),
        ))
    assert _idea_from_grounded(gf, _TMPL).operand_roles == (
        ("public.t.a", "a_role"), ("public.t.z", "z_role"))


def test_unresolved_needs_carry_no_role():
    """A need with no selected column has no operand to label — it is skipped, never guessed."""
    gf = GroundedFeature(
        template_id="t", name="t", aggregation="sum", grain_table=None, as_of_column=None,
        derives_pairs=(), params={},
        binding_resolutions=(
            GroundedNeedResolution(role="stock_col", resolution=BindingResolution.UNIQUE,
                                   selected_object_ref="public.t.bal"),
            GroundedNeedResolution(role="asof", resolution=BindingResolution.MISSING,
                                   selected_object_ref=None),
        ))
    assert _idea_from_grounded(gf, _TMPL).operand_roles == (("public.t.bal", "stock_col"),)


# ── against a REAL template ground on a REAL catalog ─────────────────────────────────────────────
def test_real_grounding_roles_match_the_templates_own_need_roles(db):
    """The roles carried are exactly the ones the recipe library declares, bound to the columns
    grounding actually selected — read back off the grounded feature, not re-derived here."""
    _churn_catalog(db)
    template = next(t for t in RETAIL_CHURN_TEMPLATES if t.id == "balance_trend")
    gf = ground_template(db, template, catalog_source=CHURN_SOURCE)
    assert gf is not None
    idea = _idea_from_grounded(gf, template)
    assert idea.operand_roles, "a grounded recipe must carry its declared operand roles"
    declared = {need.role for need in template.needs}
    assert {role for _ref, role in idea.operand_roles} <= declared
    expected = tuple(sorted({(r.selected_object_ref, r.role) for r in gf.binding_resolutions
                             if r.selected_object_ref is not None}))
    assert idea.operand_roles == expected


# ── the role survives the gauntlet, and the gauntlet's verdict is untouched ──────────────────────
def test_surviving_template_candidate_keeps_its_operand_roles(db, monkeypatch):
    _ftr_numeric_graph(db)
    monkeypatch.setattr(
        gate1, "ground_all_outcomes",
        lambda *a, **k: [GroundingOutcome("sum_balance", GroundingStatus.GROUNDED, _GF)])
    ideas, *_rest = _template_candidates(
        db, catalog_source="ftr", roles=(), target_ref=None, now=NOW, templates=(_TMPL,))
    assert ideas
    assert ideas[0].operand_roles == (
        ("public.loans.balance", "stock_col"),
        ("public.loans.cust_id", "entity"),
    )


def test_carrying_the_role_changes_no_disposition(db, monkeypatch):
    """BEHAVIOUR-NEUTRALITY: the appended idea's status + requirements are EXACTLY the ones the
    validator returns for the same raw input — the carry adds a field and decides nothing."""
    _ftr_numeric_graph(db)
    monkeypatch.setattr(
        gate1, "ground_all_outcomes",
        lambda *a, **k: [GroundingOutcome("sum_balance", GroundingStatus.GROUNDED, _GF)])
    ideas, rejections, *_rest = _template_candidates(
        db, catalog_source="ftr", roles=(), target_ref=None, now=NOW, templates=(_TMPL,))
    raw = {"name": "sum_balance", "description": _TMPL.intent,
           "derives_from": ["public.loans.balance"], "aggregation": "sum",
           "grain_table": None, "rationale": f"template sum_balance: {_TMPL.intent}"}
    baseline, rej = _validate_idea(
        db, raw, {"public.loans.balance"}, {"public.loans.balance": {"ftr"}}, None, NOW,
        gate1.timedelta(hours=24))
    assert rej is None and rejections == []
    assert ideas[0].validation_status == baseline.validation_status
    assert ideas[0].requirements == baseline.requirements
    assert baseline.operand_roles == ()   # the validator itself never mints a role


# ── the LLM path ─────────────────────────────────────────────────────────────────────────────────
def test_llm_proposed_candidate_carries_no_operand_roles(db):
    """An LLM candidate has no template, so there is NO declaration to carry: the tuple is empty and
    every consumer must fall back to today's behaviour on it."""
    _ftr_numeric_graph(db)
    raw = {"name": "llm_idea", "description": "an LLM proposal",
           "derives_from": ["public.loans.balance"], "aggregation": "sum", "grain_table": None}
    idea, rej = _validate_idea(
        db, raw, {"public.loans.balance"}, {"public.loans.balance": {"ftr"}}, None, NOW,
        gate1.timedelta(hours=24))
    assert rej is None
    assert idea.operand_roles == ()


# ── the WIRE decision, pinned deliberately (three known copies of the idea shape) ────────────────
def test_operand_roles_are_deliberately_off_every_wire_shape():
    """`operand_roles` is a SERVER-SIDE carry only, and this pins that as a DECISION, not an omission.

    Three copies of the idea shape exist — ``contract/gate1._idea_json`` (the considered-set
    snapshot), ``api/feature_serialize.serialize_feature_idea_v1/v2`` (the assist route), and
    ``suggestions._suggestion`` (the card). All three stay byte-identical to an idea WITHOUT roles:

      * ``_idea_json`` is hashed into ``option_id`` (``_candidate_identity`` -> ``canonical_hash``)
        and into ``considered_content_hash``. Emitting a new NON-EMPTY field for every recipe
        candidate would change those ids — a behaviour change this additive task must not make.
      * ``serialize_feature_idea_v1`` is the flag-OFF byte-identity contract; no client reads roles.
      * Both consumers of the role run IN PROCESS at generation time (``_template_candidates`` ->
        ``_validate_idea``), where the grounded feature is in hand — no serializer sits between them.

    Putting it on the wire is therefore a separate, deliberately-hashed change if a confirm-time
    consumer ever needs it (see the task report).
    """
    from featuregen.api.feature_serialize import (
        serialize_feature_idea_v1,
        serialize_feature_idea_v2,
    )
    from featuregen.overlay.upload.contract.gate1 import _idea_json
    from featuregen.overlay.upload.suggestions import _suggestion

    with_roles = _idea_from_grounded(_GF, _TMPL)
    assert with_roles.operand_roles, "fixture must actually carry roles for this to prove anything"
    without = gate1.replace(with_roles, operand_roles=())

    for shape in (_idea_json, serialize_feature_idea_v1, serialize_feature_idea_v2,
                  lambda i: _suggestion(i, {}, "")):
        emitted = shape(with_roles)
        assert "operand_roles" not in emitted
        assert emitted == shape(without)
