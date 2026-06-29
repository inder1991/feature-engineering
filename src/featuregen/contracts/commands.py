# PHASE 06 IS AUTHORITATIVE for Command / CommandResult — it owns them (per the overview "Key
# Produces interfaces") and they live in featuregen.contracts.envelopes. Phase 07 only CONSUMES the two
# dataclasses. The task brief was authored to TRANSCRIBE them here as an independence bootstrap for
# the case where Phase 06 had not yet landed; in THIS repo Phase 06 is present, so we RE-EXPORT the
# single authoritative definition instead of redefining it (the overview's hard rule: "do not
# redefine shared symbols ... import them"). Re-exporting guarantees byte-identity by construction.
# `test_command_contract_fields_match` (Phase 07, Task 6) still pins the field signature so any
# future drift in the authoritative definition fails loudly here.
from __future__ import annotations

from featuregen.contracts.envelopes import Command, CommandResult

__all__ = ["Command", "CommandResult"]
