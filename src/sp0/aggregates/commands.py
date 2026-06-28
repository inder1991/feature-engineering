from __future__ import annotations

from sp0.commands.registry import register_command
from sp0.aggregates.request_aggregate import (
    create_request_command, create_run_command, duplicate_of_command, select_candidate_command,
)
from sp0.aggregates.run_lifecycle import (
    reject_command, cancel_command, withdraw_command, park_command, unpark_command,
    reopen_as_new_run_command, resolve_degraded_command,
    fact_confirmed_resume_command, source_changed_revalidate_command,
)
from sp0.aggregates.activation import (
    activate_command, deactivate_expired_version_command,
)
from sp0.aggregates.consumers import (
    register_consumer_command, deregister_consumer_command,
    supersede_command, deprecate_command, finalize_deprecate_command, retier_command,
)
from sp0.aggregates.feature_lifecycle import (
    raise_monitoring_alert_command, require_revalidation_command,
    record_revalidation_outcome_command,
)

_CATALOG = {
    "create_request": create_request_command,
    "create_run": create_run_command,
    "duplicate_of": duplicate_of_command,
    "select_candidate": select_candidate_command,
    "cancel": cancel_command,
    "withdraw": withdraw_command,
    "reject": reject_command,
    "park": park_command,
    "unpark": unpark_command,
    "reopen_as_new_run": reopen_as_new_run_command,
    "resolve_degraded": resolve_degraded_command,
    "fact_confirmed_resume": fact_confirmed_resume_command,
    "source_changed_revalidate": source_changed_revalidate_command,
    "activate": activate_command,
    "supersede": supersede_command,
    "deprecate": deprecate_command,
    "finalize_deprecate": finalize_deprecate_command,
    "retier": retier_command,
    "register_consumer": register_consumer_command,
    "deregister_consumer": deregister_consumer_command,
    "raise_monitoring_alert": raise_monitoring_alert_command,
    "require_revalidation": require_revalidation_command,
    "record_revalidation_outcome": record_revalidation_outcome_command,
    "deactivate_expired_version": deactivate_expired_version_command,
}


def register_phase06_commands() -> None:
    for action, handler in _CATALOG.items():
        register_command(action, handler)
