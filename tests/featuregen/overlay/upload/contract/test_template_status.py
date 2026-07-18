"""Slice 3a-i [F9] — `_template_candidates` keeps the VALIDATOR's returned honest idea.

The template half of the considered set must carry the tri-state `validation_status` +
`requirements` the gauntlet resolved — not the pre-validation DESIGN_CHECKED default of the
converted `_idea_from_grounded` object. `ground_all` is monkeypatched (the symbol imported INTO
gate1) with a hand-built GroundedFeature so the REAL convert -> `_validate_idea` -> append path
runs against a fragile-free grounding contract.
"""
from datetime import UTC, datetime

import featuregen.overlay.upload.contract.gate1 as gate1
from featuregen.overlay.upload.contract.gate1 import _template_candidates
from featuregen.overlay.upload.templates import GroundedFeature, Template

NOW = datetime(2026, 7, 18, tzinfo=UTC)

# A fully-valid minimal Template whose id matches the grounded feature below. needs=()/params={} are
# never exercised because ground_all is monkeypatched — this object only serves the by_id lookup +
# _idea_from_grounded(template.intent).
_TMPL = Template(id="sum_balance", family="balance_stock", intent="total balance per loan",
                 needs=(), params={}, aggregation="sum", additivity="additive", explain="M",
                 use_cases=(), pit="")

_GF = GroundedFeature(template_id="sum_balance", name="sum_balance", aggregation="sum",
                      grain_table=None, as_of_column=None,
                      derives_pairs=(("ftr", "public.loans.balance"),), params={})


def _ftr_numeric_graph(db):
    # An FTR-shaped column: operational data_type 'unknown' + a numeric declared_type hint. A numeric
    # aggregation over it must resolve to NEEDS_EXTERNAL_VALIDATION (TYPE_IS_NUMERIC).
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, declared_type) VALUES ('ftr', 'public.loans.balance', 'column', 'loans', "
        "'balance', 'unknown', 'numeric')")
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('ftr', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = %s", (NOW, NOW))


def test_template_candidate_carries_needs_external_validation_status(db, monkeypatch):
    _ftr_numeric_graph(db)
    monkeypatch.setattr(gate1, "ground_all", lambda *a, **k: [_GF])
    ideas, rejections, grounded_ids, rejected_ids, binding = _template_candidates(
        db, catalog_source="ftr", roles=(), target_ref=None, now=NOW, templates=(_TMPL,))
    assert ideas, "the grounded numeric template should survive as a needs-check candidate"
    idea = ideas[0]
    # [F9]: the APPENDED idea is the validator's RETURNED idea (status + requirements), not the
    # pre-validation DESIGN-CHECKED _idea_from_grounded object.
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "TYPE_IS_NUMERIC" for r in idea.requirements)
    assert "sum_balance" in grounded_ids
