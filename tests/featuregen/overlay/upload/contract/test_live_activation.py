from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.contract.live_activation import (
    LiveActivationNotReady,
    current_version_vector,
    is_live_cross_catalog_enabled,
    record_decision,
    record_evaluation,
    require_live_ready,
)

_NOW = datetime(2026, 7, 18, tzinfo=UTC)


def _approved(db, *, result="PASS", vv=None):
    eid = record_evaluation(db, telemetry_window={"cohort": "c"}, population_report={},
                            gold_set_result={}, stability_result={}, result=result, evaluated_at=_NOW)
    if vv is not None:   # force a stored vector that differs from current, for the mismatch test
        db.execute("UPDATE enablement_evaluation SET version_vector = %s WHERE evaluation_id = %s",
                   (__import__("json").dumps(vv), eid))
    return record_decision(db, evaluation_id=eid, decision="APPROVE", decided_by="admin",
                           reason="go", decided_at=_NOW)


def test_flag_off_is_disabled(db, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", raising=False)
    _approved(db)
    assert is_live_cross_catalog_enabled(db) is False
    with pytest.raises(LiveActivationNotReady):
        require_live_ready(db)


def test_flag_on_without_approval_is_disabled(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    assert is_live_cross_catalog_enabled(db) is False   # no decision at all


def test_flag_on_with_matching_pass_approval_is_enabled(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    _approved(db)
    assert is_live_cross_catalog_enabled(db) is True
    require_live_ready(db)   # no raise


def test_approval_over_a_fail_is_rejected(db, monkeypatch):
    with pytest.raises(ValueError):
        _approved(db, result="FAIL")   # record_decision APPROVE over a FAIL evaluation → refused


def test_version_vector_mismatch_disables(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    _approved(db, vv={"planner": "0.0.0-stale"})   # stored vector != current
    assert is_live_cross_catalog_enabled(db) is False


def test_revoke_supersedes_approval(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    eid = record_evaluation(db, telemetry_window={}, population_report={}, gold_set_result={},
                            stability_result={}, result="PASS", evaluated_at=_NOW)
    dec = record_decision(db, evaluation_id=eid, decision="APPROVE", decided_by="a", reason="", decided_at=_NOW)
    assert is_live_cross_catalog_enabled(db) is True
    record_decision(db, evaluation_id=eid, decision="REVOKE", decided_by="a", reason="stop",
                    decided_at=_NOW, supersedes_decision_id=dec)
    assert is_live_cross_catalog_enabled(db) is False


def test_wrong_deployment_does_not_inherit_approval(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    _approved(db)                                 # decision recorded for d1
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d2")
    assert is_live_cross_catalog_enabled(db) is False


def test_version_vector_is_code_only_no_graph_fingerprint():
    vv = current_version_vector()
    assert "planner" in vv and "gate_evaluator" in vv and "gold_set" in vv
    assert not any("fingerprint" in k or "graph" in k for k in vv)   # graph/catalog state excluded
