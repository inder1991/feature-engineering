# Verbatim from the shared SP-0 contract; Phase 07 authoritative.
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True, slots=True)
class GateTaskSpec:
    gate: str
    required_inputs: tuple[str, ...]
    eligible_assignees: Mapping[str, str]
    allowed_responses: tuple[str, ...]
    run_id: Optional[str] = None
    feature_id: Optional[str] = None
    quorum_required: int = 1
    quorum_of_role: Optional[str] = None
    delegation_allowed: bool = True
    sla: Optional[str] = None


@dataclass(frozen=True, slots=True)
class SignalResult:
    task_id: str
    status: str
    counted: bool
    quorum_met: bool
