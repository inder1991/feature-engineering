from featuregen.overlay.upload.contract.scope_mode import (
    ScopeExecutionMode,
    confirmation_required,
    scope_mode_status,
)


def test_scope_mode_defaults_to_confirmation_required(monkeypatch):
    monkeypatch.delenv("FEATUREGEN_SCOPE_EXECUTION_MODE", raising=False)

    status = scope_mode_status()

    assert status.mode is ScopeExecutionMode.CONFIRMATION_REQUIRED
    assert status.configuration_valid is True
    assert confirmation_required() is True


def test_unknown_scope_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "typo")

    status = scope_mode_status()

    assert status.mode is ScopeExecutionMode.CONFIRMATION_REQUIRED
    assert status.configuration_valid is False
    assert confirmation_required() is True


def test_legacy_scope_mode_must_be_explicit(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "legacy_unscoped")

    assert scope_mode_status().mode is ScopeExecutionMode.LEGACY_UNSCOPED
    assert confirmation_required() is False
