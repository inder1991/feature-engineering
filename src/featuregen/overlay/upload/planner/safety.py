"""The stable planner-facing safety boundary. Delegates to templates._safe_to_bind today; parity-tested.
Invariant: the planner may add STRICTER eligibility, but NEVER accepts a binding _safe_to_bind rejects."""
from __future__ import annotations

from featuregen.overlay.upload.planner.contracts import BindingSafety
from featuregen.overlay.upload.templates import _Col, _safe_to_bind


def evaluate_binding_safety(col: _Col) -> BindingSafety:
    return BindingSafety.safe if _safe_to_bind(col) else BindingSafety.unsafe
