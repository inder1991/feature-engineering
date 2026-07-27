"""Release policy for recognition and human-confirmed generation scope.

The legacy workflow generated against the complete recipe registry when the client omitted
``confirmed_scope``. That made a frontend flag an authority boundary. The release mode moves the
boundary to the server: missing scope is rejected unless an operator explicitly selects the
auditable emergency compatibility mode.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class ScopeExecutionMode(StrEnum):
    CONFIRMATION_REQUIRED = "confirmation_required"
    LEGACY_UNSCOPED = "legacy_unscoped"


_ENV = "FEATUREGEN_SCOPE_EXECUTION_MODE"


@dataclass(frozen=True, slots=True)
class ScopeModeStatus:
    mode: ScopeExecutionMode
    configured_value: str | None
    configuration_valid: bool

    @property
    def confirmation_required(self) -> bool:
        return self.mode is ScopeExecutionMode.CONFIRMATION_REQUIRED


def scope_mode_status() -> ScopeModeStatus:
    """Return the active mode, failing closed on an unknown configured value."""
    configured = os.environ.get(_ENV)
    if configured is None or configured == "":
        return ScopeModeStatus(
            mode=ScopeExecutionMode.CONFIRMATION_REQUIRED,
            configured_value=None,
            configuration_valid=True,
        )
    try:
        mode = ScopeExecutionMode(configured)
    except ValueError:
        return ScopeModeStatus(
            mode=ScopeExecutionMode.CONFIRMATION_REQUIRED,
            configured_value=configured,
            configuration_valid=False,
        )
    return ScopeModeStatus(mode=mode, configured_value=configured, configuration_valid=True)


def confirmation_required() -> bool:
    return scope_mode_status().confirmation_required
