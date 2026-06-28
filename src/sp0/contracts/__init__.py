from __future__ import annotations

from sp0.contracts.db import DbConn
from sp0.contracts.documents import (
    BODY_CLASSIFICATIONS,
    BRANCH_ROLES,
    STAGES,
    Stage,
)
from sp0.contracts.envelopes import (
    Command,
    CommandResult,
    Disposition,
    EventEnvelope,
    GateTaskSpec,
    GuardOutcome,
    HandlerContext,
    HandlerResult,
    IdentityEnvelope,
    NewActivation,
    NewDocument,
    NewEvent,
    NewExternalCommand,
    NewTimer,
    ProvenanceEnvelope,
    SignalResult,
)
from sp0.contracts.errors import (
    ConcurrencyError,
    ProjectionApplyError,
    SchemaValidationError,
)
from sp0.contracts.protocols import (
    GuardInputs,
    GuardPredicate,
    Handler,
    PredicateRegistry,
    Projection,
    SchemaRegistry,
    Upcaster,
)

__all__ = [
    "DbConn",
    "Command",
    "CommandResult",
    "Disposition",
    "EventEnvelope",
    "GateTaskSpec",
    "GuardOutcome",
    "HandlerContext",
    "HandlerResult",
    "IdentityEnvelope",
    "NewActivation",
    "NewDocument",
    "Stage",
    "STAGES",
    "BRANCH_ROLES",
    "BODY_CLASSIFICATIONS",
    "NewEvent",
    "NewExternalCommand",
    "NewTimer",
    "ProvenanceEnvelope",
    "SignalResult",
    "ConcurrencyError",
    "ProjectionApplyError",
    "SchemaValidationError",
    "GuardInputs",
    "GuardPredicate",
    "Handler",
    "PredicateRegistry",
    "Projection",
    "SchemaRegistry",
    "Upcaster",
]

# ── Core interface functions (overview "Core interfaces"): re-exported so downstream phases can
# `from sp0.contracts import append_event, ...`. Lazy (PEP 562) to avoid the import cycle with
# sp0.events.* / sp0.projections.*, which import THIS module.
_LAZY_EXPORTS = {
    "append_event": ("sp0.events.store", "append_event"),
    "load_stream": ("sp0.events.store", "load_stream"),
    "run_projection": ("sp0.projections.runner", "run_projection"),
    "rebuild_projection": ("sp0.projections.runner", "rebuild_projection"),
    "projection_lag": ("sp0.projections.runner", "projection_lag"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module, attr = target
    return getattr(importlib.import_module(module), attr)


__all__ += list(_LAZY_EXPORTS)
