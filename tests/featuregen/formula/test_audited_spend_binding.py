"""§11.2's last inch: the job's ceiling reaches the CONTEXT every physical call dispatches under.

`record_dispatch` reserves exactly when the context names an authorization — proved by the spend
seam tests. What THESE tests pin is the fold above it: `audited_formula_call` puts the binding
into whichever context is in effect, so neither a default-built context nor a caller-supplied
richer one can shed the money guard.
"""
from __future__ import annotations

from decimal import Decimal

from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext, SpendBindingV1

_BINDING = SpendBindingV1(
    spend_authorization_id="spend-1", call_tokens=20_000, call_cost=Decimal("2.00"))


def _call(monkeypatch, *, dispatch_audit=None, spend=None):
    from featuregen.formula import audited

    captured = {}

    class _Res:
        output = {"ok": True}
        llm_call_ref = "lc-x"
        provider_calls = 1
        usage = {}
        repair_attempts = ()

    def fake_drive(conn, client, **kwargs):
        captured["ctx"] = kwargs["dispatch_audit"]
        return _Res()

    monkeypatch.setattr(audited, "drive_audited_structured_call", fake_drive)
    audited.audited_formula_call(
        None, object(), authoring_run_id="run-1", task="author", prompt_id="p",
        schema_id="s", instruction="i", catalog_metadata={},
        dispatch_audit=dispatch_audit, spend=spend)
    return captured["ctx"]


def test_the_binding_lands_on_a_DEFAULT_BUILT_context(monkeypatch):
    ctx = _call(monkeypatch, spend=_BINDING)
    assert ctx.spend_authorization_id == "spend-1"
    assert ctx.spend_call_tokens == 20_000
    assert ctx.spend_call_cost == Decimal("2.00")


def test_the_binding_lands_on_a_CALLER_SUPPLIED_context_too(monkeypatch):
    """A richer context must not shed the money guard."""
    supplied = DispatchAuditContext(
        ingestion_run_id=None, stage="formula:author", subjects=(),
        authoring_run_id="run-1", call_role="author")
    ctx = _call(monkeypatch, dispatch_audit=supplied, spend=_BINDING)
    assert ctx.spend_authorization_id == "spend-1"
    assert ctx.stage == "formula:author", "the supplied context's own fields survive the fold"


def test_no_binding_means_the_context_is_UNTOUCHED(monkeypatch):
    ctx = _call(monkeypatch, spend=None)
    assert ctx.spend_authorization_id is None
