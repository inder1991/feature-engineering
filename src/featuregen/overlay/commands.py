"""Overlay command facade (SP-1 design §6).

A thin facade that wires the overlay command handlers — each now lives in its own module — into the
SP-0 command registry. It owns the `_OVERLAY_CATALOG` (the ordered `(action, handler)` tuple)
and the idempotent `register_overlay_commands`, and RE-EXPORTS every handler (`propose_fact`,
`confirm_fact`, `reject_fact`, `enter_fact`, `_run_profiler`) plus the back-compat names
(`OverlayCommandError`, `_confirm_approved_join`, `_existing_proposal_fingerprint`,
`get_task_proposal`) so existing `featuregen.overlay.commands` imports keep resolving.

Each handler module imports `append_overlay_event` directly from `featuregen.overlay.store`; tests
that need to observe or perturb an append (occ_spy / inject-concurrent) monkeypatch it on the owning
handler module (e.g. `proposal_commands.append_overlay_event`), not through this facade.
"""
from __future__ import annotations

from featuregen.commands.registry import get_command, register_command
from featuregen.overlay._lifecycle import OverlayCommandError as OverlayCommandError
from featuregen.overlay.confirmation_commands import confirm_fact as confirm_fact
from featuregen.overlay.confirmation_commands import enter_fact as enter_fact
from featuregen.overlay.confirmation_commands import reject_fact as reject_fact
from featuregen.overlay.join_confirmation import (
    _confirm_approved_join as _confirm_approved_join,
)
from featuregen.overlay.profiler_command import (
    _existing_proposal_fingerprint as _existing_proposal_fingerprint,
)
from featuregen.overlay.profiler_command import (
    _run_profiler as _run_profiler,
)
from featuregen.overlay.proposal_commands import propose_fact as propose_fact
from featuregen.overlay.task_read import get_task_proposal as get_task_proposal

# `_OVERLAY_CATALOG` is a TUPLE of (action, handler) pairs (mirrors SP-0's `_CATALOG`),
# NOT a dict; it includes ("run_profiler", ...).
_OVERLAY_CATALOG = (
    ("propose_fact", propose_fact),
    ("confirm_fact", confirm_fact),
    ("reject_fact", reject_fact),
    ("enter_fact", enter_fact),
    ("run_profiler", _run_profiler),
)


def register_overlay_commands() -> None:
    """Idempotent: `register_command` raises on duplicate and the command registry
    persists across tests (the root harness resets only the event registry), so skip any action
    that is already registered instead of re-registering it."""
    for action, handler in _OVERLAY_CATALOG:
        try:
            get_command(action)
        except KeyError:
            register_command(action, handler)
