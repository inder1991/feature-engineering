"""Control-flow signals and lease identity shared by formula orchestration."""
from __future__ import annotations

from dataclasses import dataclass


class FormulaControlFlow(RuntimeError):
    """A formula control signal that must never be folded into a semantic result."""


class LeaseFenceLost(FormulaControlFlow):
    """The worker no longer owns the live queue lease for this authoring run."""


class RecoveryRequiresReconciliation(FormulaControlFlow):
    """Durable replay state is incomplete, forked, or cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class LeaseFence:
    queue_id: int
    lease_owner: str
    lease_fence: int
