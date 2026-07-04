from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.events.registry import event_registry
from featuregen.overlay.commands import register_overlay_commands
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.facts import register_overlay_event_types

# §6.5 overlay command authz rows (coarse capability only; fine authority/SoD lives in the
# handlers + authority.py). Same shape as authz.policy._POLICY_ROWS.
_OVERLAY_POLICY_ROWS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("propose_fact", "", "data_owner", "human", None),
    ("propose_fact", "", "overlay", "service", None),
    ("run_profiler", "", "overlay", "service", None),
    ("confirm_fact", "", "data_owner", "human", None),
    ("confirm_fact", "", "compliance", "human", None),
    ("reject_fact", "", "data_owner", "human", None),
    ("reject_fact", "", "compliance", "human", None),
    # Governance-queue (unknown-owner) confirmations — a platform-admin clears the fallback task
    # via the PUBLIC execute_command path; the handler still enforces fine-grained authority.
    ("confirm_fact", "", "platform-admin", "human", None),
    ("reject_fact", "", "platform-admin", "human", None),
    ("enter_fact", "", "data_owner", "human", None),
    ("enter_fact", "", "compliance", "human", None),
)


def register_overlay(handler_registry, *, config: OverlayConfig | None = None) -> None:
    """Production wiring for the overlay write side: event schemas (so `append_event` validation
    passes) + the (idempotent) overlay command catalog. Expiry is NOT a HandlerRegistry handler —
    it is the explicit `freshness.fire_due_overlay_expiries` poller, so nothing is
    registered into `handler_registry` here; it is accepted only for signature symmetry with the
    SP-0 bootstrap. The catalog adapter is injected separately via `register_catalog_adapter(...)`
    from `overlay/catalog.py`.

    `config` seals the server-side OverlayConfig (SP-1.5 §3.1/§10) — deployment-injected, resolved
    by the renewal/drift/profiler stages via `current_overlay_config()`. When omitted, no config is
    sealed and those stages fail closed / skip-loud (never silently defaulted)."""
    del handler_registry
    register_overlay_event_types(event_registry())
    register_overlay_commands()
    if config is not None:
        register_overlay_config(config)


def seed_overlay_authz(conn: DbConn) -> None:
    """Idempotently seed the overlay authz rows and the overlay projection checkpoint."""
    for action, gate, role, kind, scope in _OVERLAY_POLICY_ROWS:
        conn.execute(
            """
            INSERT INTO authz_policy (action, gate, permitted_role, actor_kind, scope)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (action, gate, permitted_role, actor_kind) DO NOTHING
            """,
            (action, gate, role, kind, scope),
        )
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name) VALUES ('overlay') "
        "ON CONFLICT DO NOTHING"
    )
