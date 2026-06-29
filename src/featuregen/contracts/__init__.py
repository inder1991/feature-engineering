from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.contracts.documents import (
    BODY_CLASSIFICATIONS,
    BRANCH_ROLES,
    STAGES,
    Stage,
)
from featuregen.contracts.envelopes import (
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
    SignalResult,
)
from featuregen.contracts.errors import (
    ConcurrencyError,
    ProjectionApplyError,
    SchemaValidationError,
)
from featuregen.contracts.protocols import (
    GuardInputs,
    GuardPredicate,
    Handler,
    PredicateRegistry,
    Projection,
    SchemaRegistry,
    Upcaster,
)
from featuregen.contracts.provenance import ProvenanceEnvelope

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
# `from featuregen.contracts import append_event, ...`. Lazy (PEP 562) to avoid the import cycle with
# featuregen.events.* / featuregen.projections.*, which import THIS module.
_LAZY_EXPORTS = {
    "append_event": ("featuregen.events.store", "append_event"),
    "load_stream": ("featuregen.events.store", "load_stream"),
    "run_projection": ("featuregen.projections.runner", "run_projection"),
    "rebuild_projection": ("featuregen.projections.runner", "rebuild_projection"),
    "projection_lag": ("featuregen.projections.runner", "projection_lag"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module, attr = target
    return getattr(importlib.import_module(module), attr)


__all__ += list(_LAZY_EXPORTS)
