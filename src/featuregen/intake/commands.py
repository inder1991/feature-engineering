"""SP-2 intake command hub (mirrors SP-1's `overlay/commands.py`): the collaborator-seam accessors
the handlers read, the R1 feature_contract append helper, and the idempotent command registrar.

R10: the LLM / redactor / catalog collaborator seams are the CANONICAL module-globals owned by P3
(`current_llm_client`, `current_intent_redactor`) and P2 (`current_intake_catalog`) — imported and
re-exported here, NEVER redefined. R1: `append_fc_event` is `intake.store.append_feature_contract_event`
imported verbatim (aliased), NOT a local redefinition. Phase 4 owns ONLY a Phase-4-local override of
P2's pure `classify_intent` (`register_intake_classifier`/`_current_classifier`/`reset_intake_seams`)
so a test can pin the banking outcome deterministically."""

from __future__ import annotations

from featuregen.aggregates._append import append
from featuregen.commands.registry import get_command, register_command
from featuregen.contracts import DbConn, EventEnvelope, IdentityEnvelope
from featuregen.intake.banking_catalog import IntakeClassification, classify_intent
from featuregen.intake.catalog import current_intake_catalog  # R8/R10 seam (P2, catalog.py)
from featuregen.intake.llm import current_llm_client  # R10 seam (P3, llm.py)
from featuregen.intake.redaction import current_intent_redactor  # R10 seam (P3, redaction.py)
from featuregen.intake.store import (  # R1 seam (P1, store.py)
    append_feature_contract_event as append_fc_event,
)
from featuregen.intake.store import (
    load_feature_contract,
)

__all__ = [
    "IntakeError",
    "register_intake_classifier",
    "reset_intake_seams",
    "register_sp2_commands",
    # re-exported collaborator seams (R10) + R1 append/load — the handlers added by later Phase-4
    # tasks read these off this module; consumers import them from here.
    "append_fc_event",
    "load_feature_contract",
    "current_llm_client",
    "current_intent_redactor",
    "current_intake_catalog",
    "classify_intent",
    "IntakeClassification",
    "append",
    "DbConn",
    "EventEnvelope",
    "IdentityEnvelope",
]


class IntakeError(Exception):
    """Raised on intake command misconfiguration."""


# ── Phase-4-local classifier override (NOT a shared seam) ─────────────────────────────────────
# R10: the LLM / redactor / catalog collaborator seams are the canonical module-globals owned by
# P3 (`current_llm_client`, `current_intent_redactor`) and P2 (`current_intake_catalog`) — imported
# above, NEVER redefined here (Phase 9's `register_sp2` wires all four in production; tests wire
# stubs via the same `register_*` functions). Phase 4 keeps ONLY a local override of P2's pure
# `classify_intent` so a test can pin the banking outcome deterministically.
_CLASSIFIER = None  # None ⟹ production default `classify_intent`


def register_intake_classifier(fn) -> None:
    global _CLASSIFIER
    _CLASSIFIER = fn


def _current_classifier():
    return _CLASSIFIER if _CLASSIFIER is not None else classify_intent


def reset_intake_seams() -> None:
    global _CLASSIFIER
    _CLASSIFIER = None


# ── feature_contract append path (R1) ─────────────────────────────────────────────────────────
# `append_fc_event` is the R1 seam imported (aliased) from `intake.store`; it sets
# aggregate="feature_contract", aggregate_id=run_id, feature_contract_id=run_id. Phase 4 does NOT
# define its own append helper and threads only `run_id` (feature_contract_id == run_id).
# X4 (CAS on the folded head): `INTENT_SUBMITTED` opens the brand-new stream at expected_version=0;
# every later fc append passes expected_version=_fc_head(conn, run_id) (the folded head re-loaded
# right before the append, which includes `call_llm`'s interleaved `LLM_CALL_RECORDED`) and treats a
# raised ConcurrencyError as a `stale` denial — never expected_version=None (`aggregates/_append.py:76`
# treats None as "current head at append time", the lost-update hazard X4 removes).


# `_SP2_CATALOG` is a TUPLE of (action, handler) pairs (mirrors SP-0's `_CATALOG`); Phase 4 appends only
# `submit_intent` (Task 4.4). Later phases extend commands.py with their own handlers — P5/P6/P7 add
# answer_clarification / select_candidate_doc / confirm_contract / request_edit, and P8 adds the
# standalone `reject_intent` (X5 — NOT Phase 4).
_SP2_CATALOG: tuple[tuple[str, object], ...] = ()


def register_sp2_commands() -> None:
    """Idempotent (mirrors SP-1's `register_overlay_commands`): `register_command` raises on a
    duplicate and the command registry persists across tests, so skip already-registered actions."""
    for action, handler in _SP2_CATALOG:
        try:
            get_command(action)
        except KeyError:
            register_command(action, handler)
