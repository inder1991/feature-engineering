from __future__ import annotations

from featuregen.aggregates.events import register_phase06_event_types
from featuregen.events.registry import event_registry

# Identity of the live event-registry singleton we last registered into. Using the
# registry object (rather than a bare bool) makes the guard reset-aware: the Phase-01
# `event_registry()` accessor returns the SAME singleton across calls, but
# `reset_event_registry()` (used by the autouse test fixture and on re-bootstrap)
# swaps in a fresh, empty instance — at which point we must register again.
_REGISTERED_INTO: object | None = None


def register_phase06_event_schemas() -> None:
    """Idempotently register Phase-06 event schemas into the process-wide event registry
    (the singleton returned by `featuregen.events.registry.event_registry()`) so runtime
    `append_event` validation passes outside pytest. Called at process startup
    (the §"Production wiring" path) and by tests. A no-op when the current live singleton
    already holds these schemas; re-registers transparently after a registry reset.
    `register_schema` overwrites in place, so this never raises a duplicate-registration error."""
    global _REGISTERED_INTO
    registry = event_registry()
    if _REGISTERED_INTO is registry:
        return
    register_phase06_event_types(registry)
    _REGISTERED_INTO = registry


def bootstrap_phase06(handler_registry) -> None:
    """Single production wiring call: event schemas (so runtime `append_event` validation
    passes) + the §4.4 command catalog + the §5.8 saga handler into Phase-04's HandlerRegistry."""
    from featuregen.aggregates.commands import register_phase06_commands
    from featuregen.aggregates.activation import register_phase06_handlers

    register_phase06_event_schemas()      # idempotent (Task 3)
    register_phase06_commands()           # §4.4 catalog
    register_phase06_handlers(handler_registry)  # §5.8 activate_version handler


def bootstrap_phase07(conn) -> None:
    """Production wiring for §6.2 command authorization. Seeds the canonical authz-policy rows and
    registers the concrete `PolicyAuthorizer` so `execute_command` enforces command-level authz and
    routes denials to the tamper-evident security stream — replacing the allow-all Phase-06 default.
    Idempotent: `seed_authz_policy` upserts, and re-registering the authorizer is a plain swap. Must
    run AFTER `bootstrap_phase06` so the §4.4 catalog (incl. `submit_human_signal`) is registered."""
    from featuregen.authz.policy import seed_authz_policy
    from featuregen.authz.authorizer import PolicyAuthorizer
    from featuregen.commands.authz_seam import register_command_authorizer

    seed_authz_policy(conn)
    register_command_authorizer(PolicyAuthorizer())
