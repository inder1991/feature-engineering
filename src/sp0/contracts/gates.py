# PHASE 06 IS AUTHORITATIVE for GateTaskSpec / SignalResult — they live in sp0.contracts.envelopes.
# Phase 07 only CONSUMES these two dataclasses. The task brief was authored to TRANSCRIBE them here as
# an independence bootstrap for the case where Phase 06 had not yet landed; in THIS repo Phase 06 is
# present, so we RE-EXPORT the single authoritative definition instead of redefining it (the overview's
# hard rule: "do not redefine shared symbols ... import them"). Re-exporting guarantees byte-identity
# (and isinstance/identity across phases) by construction.
from __future__ import annotations

from sp0.contracts.envelopes import GateTaskSpec, SignalResult

__all__ = ["GateTaskSpec", "SignalResult"]
