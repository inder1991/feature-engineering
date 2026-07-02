"""Task 5.4 — Minimum Contract Validation (§6.7) + the SP-2 lifecycle-guard predicates (§11).

R5 two-symbol split:
  * PURE  — `minimum_contract_validated(draft_body, ledger, classification, ...) -> MCVResult`: the
    deterministic 6-check pre-gate checklist that MUST pass before Gate #1 can open. DB-free / LLM-free.
  * DB-BACKED — `run_minimum_contract_validation(conn, run_id, *, actor) -> CommandResult`: folds the
    feature_contract status (R3), reads the current draft/ledger/classification off the stream, runs
    the pure checklist, and on a pass appends MINIMUM_CONTRACT_VALIDATED with X4 CAS on the folded head
    (a ConcurrencyError denies `stale`). P7's Task 7.6 open_gate1_task reads `.accepted`.

Plus the guard predicates later handlers evaluate inline (§11): `open_fields_empty`,
`not_prohibited_intent`, `calculation_method_available`, `confirmer_is_requester_human` (built on the
ONE state-based owner predicate P2 owns — R4 `intake.state.actor_is_request_owner`, imported not
redefined).

IMPLEMENTATION NOTE (deviation from the brief's illustrative Step-3 code, kept inside this task's two
files): the brief read the bodies via `commands.read_contract_body(conn, doc_id)` and the classification
off `state.classification`. Neither works against the shipped seams — the P4 body-read helpers
(`read_contract_body`/`freeze_draft`/`_candidate_count`) were never built, document bodies ride the
DRAFT_CONTRACT_PRODUCED event payload inline (`draft_body`/`assumption_ledger_body`, commands.py:474-475,
Phase-8 replay), and the Task-2.5 fold surfaces `state.classification` only as the rejection STRING —
the classification MAPPING (R9 `as_mapping()`) rides the INTENT_SUBMITTED payload. So the DB-backed
wrapper reads all three off the folded stream directly. The public signatures and the pure checklist are
exactly as R5 specifies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.contracts import CommandResult, ConcurrencyError, DbConn, IdentityEnvelope
from featuregen.documents.draft import UNKNOWN
from featuregen.intake.events import (
    CLARIFICATION_ANSWERED,
    INTENT_SUBMITTED,
    MINIMUM_CONTRACT_VALIDATED,
)

# R4: the ONE owner predicate is owned by P2 (intake.state) — mcv IMPORTS it, never redefines it.
# R3: the DB-backed wrapper folds the feature_contract status through the P2 fold.
from featuregen.intake.state import (
    FeatureContractStatus,
    actor_is_request_owner,
    fold_feature_contract_state,
)
from featuregen.intake.store import append_feature_contract_event, load_feature_contract

# Classification outcomes that terminally block a contract (string values of banking_catalog.IntakeOutcome).
_BLOCKING_OUTCOMES = ("OUT_OF_SCOPE", "PROHIBITED_DATA_CLASS")
# Sources whose value the PLATFORM supplied (not the intent/human) → must carry a ledger account (§5.3, check 6).
_PLATFORM_SOURCES = ("default", "catalog")
_HIGH_AMBIGUITY = 0.30


@dataclass(frozen=True, slots=True)
class MCVResult:
    passed: bool
    failures: tuple[str, ...]


def _ledger_fields(ledger_body: Mapping[str, Any]) -> set[str]:
    return {str(a.get("field")) for a in ledger_body.get("assumptions", [])}


def _is_unknown(value: Any) -> bool:
    if value == UNKNOWN:
        return True
    if isinstance(value, list):
        return not value or any(v == UNKNOWN for v in value)
    return value in (None, "")


# ── lifecycle-guard predicates (evaluated inline by later handlers, §11) ───────────────────────
def open_fields_empty(draft_body: Mapping[str, Any]) -> bool:
    """Guard `open_fields_empty` (§11): a Draft with any open field can never pass Gate #1 (§3.5)."""
    return not draft_body.get("open_fields")


def not_prohibited_intent(classification: Mapping[str, Any] | None) -> bool:
    """Guard `not_prohibited_intent` (§11): fail-closed if the classification is absent."""
    return classification is not None and classification.get("outcome") not in _BLOCKING_OUTCOMES


def calculation_method_available(
    draft_body: Mapping[str, Any], *, mode: str, candidate_count: int
) -> bool:
    """MCV #2 / guard `calculation_method_available` (§6.7): in definition mode the single faithful
    method is present and non-UNKNOWN; in hypothesis mode a NON-EMPTY scored candidate set exists
    pre-gate (the human selects one AT Gate #1 — this does NOT assert `chosen` is already set)."""
    if mode == "hypothesis":
        return candidate_count >= 1
    method = draft_body.get("feature_semantics", {}).get("calculation_method")
    return bool(method) and method != UNKNOWN


def confirmer_is_requester_human(state, actor: IdentityEnvelope) -> bool:
    """Guard `confirmer_is_requester_human` = actor_is_request_owner ∧ actor_kind=="human" (§8.2),
    built on the ONE state-based owner predicate P2 owns (R4 — `actor_is_request_owner(state, actor)`,
    where `state.requester` is the INTENT_SUBMITTED actor.subject). A service or the LLM can never
    confirm; a different data scientist can never confirm."""
    return actor.actor_kind == "human" and actor_is_request_owner(state, actor)


# ── the pure 6-check pre-gate checklist (R5, §6.7) ─────────────────────────────────────────────
def minimum_contract_validated(
    draft_body: Mapping[str, Any],
    ledger_body: Mapping[str, Any],
    classification: Mapping[str, Any] | None,
    *,
    mode: str = "definition",
    candidate_count: int = 0,
    confirmed_fields: Iterable[str] = (),
) -> MCVResult:
    """The deterministic 6-check pre-gate checklist (spec §6.7). **R5** pure form — the canonical
    `minimum_contract_validated(draft_body, ledger, classification)` 3-arg call is valid (the extras are
    optional keyword-only; the DB-backed `run_minimum_contract_validation` supplies them). Pure and
    machine-checkable — evaluated INLINE by `open_gate1_task` (P7) against the folded status, NOT the
    state-machine engine. A failure keeps the run in the Refinement Loop; success emits
    MINIMUM_CONTRACT_VALIDATED.

    Accountable = has a ledger entry OR was human-confirmed (`confirmed_fields`). §5.3's no-silent-
    assumption rule."""
    failures: list[str] = []
    sem = draft_body.get("feature_semantics", {})
    ledger = _ledger_fields(ledger_body)
    accountable = ledger | set(confirmed_fields)
    field_scores = draft_body.get("field_scores", {})

    # 1) Grain resolved — entity + the grain the DRAFT carries (entity_grain), non-UNKNOWN.
    if _is_unknown(sem.get("entity")) or _is_unknown(sem.get("entity_grain")):
        failures.append("grain_unresolved")

    # 2) A calculation method is available for selection (mode-specific, §6.7 #2).
    if not calculation_method_available(draft_body, mode=mode, candidate_count=candidate_count):
        failures.append("calculation_method_unavailable")

    # 3) No unresolved high-ambiguity field: open_fields empty AND no ambiguity > 0.30 left unaccounted.
    if draft_body.get("open_fields"):
        failures.append("open_fields_nonempty")
    else:
        for field, sc in field_scores.items():
            if float(sc.get("ambiguity", 0.0)) > _HIGH_AMBIGUITY and field not in accountable:
                failures.append(f"high_ambiguity_unaccounted:{field}")

    # 4) Observation intent present (so SP-3 can bind point-in-time).
    oi = sem.get("observation_intent") or {}
    if _is_unknown(oi.get("kind")):
        failures.append("observation_intent_missing")

    # 5) In banking scope — fail-closed on absent/unversioned classification (§4.5(b)); else not blocked.
    if classification is None or classification.get("catalog_version") in (None, ""):
        failures.append("classification_unavailable")
    elif classification.get("outcome") in _BLOCKING_OUTCOMES:
        failures.append(f"blocked:{classification.get('outcome')}")

    # 6) Every PLATFORM-supplied field is accountable (§5.3): a default/catalog value MUST be in the
    #    ledger or human-confirmed. Verbatim (source=llm) fields are accounted by the intent itself.
    for field, sc in field_scores.items():
        if sc.get("source") in _PLATFORM_SOURCES and field not in accountable:
            failures.append(f"unaccounted:{field}")

    return MCVResult(passed=not failures, failures=tuple(failures))


# ── the DB-backed wrapper (R5) ─────────────────────────────────────────────────────────────────
def _latest_body(stream, key: str) -> Mapping[str, Any] | None:
    """The last event payload carrying `key` — the current frozen DRAFT/LEDGER body rides the
    DRAFT_CONTRACT_PRODUCED (and, later, CONTRACT_REFINED) event for Phase-8 replay (commands.py:474)."""
    body: Mapping[str, Any] | None = None
    for e in stream:
        candidate = e.payload.get(key)
        if isinstance(candidate, Mapping):
            body = candidate
    return body


def _classification_mapping(stream) -> Mapping[str, Any] | None:
    """The classification MAPPING (R9 `as_mapping()`) persisted on INTENT_SUBMITTED — the fold surfaces
    `state.classification` only as the rejection STRING, so the mapping is read off the opening event."""
    for e in stream:
        if e.type == INTENT_SUBMITTED:
            got = e.payload.get("classification")
            return got if isinstance(got, Mapping) else None
    return None


def run_minimum_contract_validation(conn: DbConn, run_id: str, *, actor) -> CommandResult:
    """**R5** DB-backed MCV — the boundary guard P7's `open_gate1_task` reads via `.accepted`. Folds the
    `feature_contract` status (**R3** `fold_feature_contract_state`), reads the current draft/ledger/
    classification off the stream, runs the pure 6-check checklist, and appends
    `MINIMUM_CONTRACT_VALIDATED` on a pass. All appends go through the **R1** `intake.store` seam. **X4**
    — the append is CAS-pinned to the folded head's `stream_version`; a `ConcurrencyError` (a concurrent
    transition raced the fold) denies `stale`."""
    stream = load_feature_contract(conn, run_id)
    # X4 (CAS on the folded head, Global Constraints): capture the folded head's stream_version and CAS
    # the MCV transition on it. SP-0's append treats expected_version=None as "current head at append
    # time", so a stale fold + a None-append could commit MINIMUM_CONTRACT_VALIDATED on top of a
    # concurrent transition (SP-1 capstone C2). Pinning the head makes that race fail closed as `stale`.
    head_version = stream[-1].stream_version if stream else 0
    state = fold_feature_contract_state(stream)
    # No-regression guard (mirrors the fold's own lock): a status already at/past MCV or CONFIRMED does
    # not re-append — idempotent accept.
    if state.status in (
        FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED,
        FeatureContractStatus.CONFIRMED,
    ):
        return CommandResult(accepted=True, aggregate_id=run_id)

    draft_body = _latest_body(stream, "draft_body") or {}
    ledger_body = _latest_body(stream, "assumption_ledger_body") or {
        "request_id": state.request_id,
        "assumptions": [],
    }
    classification = _classification_mapping(stream)
    # Human-answered fields are accountable (§5.3) — the fields the requester confirmed via clarification.
    confirmed_fields = {
        e.payload.get("field") for e in stream if e.type == CLARIFICATION_ANSWERED
    }
    confirmed_fields.discard(None)
    candidate_count = len(state.candidate_doc_ids) if state.intake_mode == "hypothesis" else 0

    res = minimum_contract_validated(
        draft_body, ledger_body, classification, mode=state.intake_mode or "definition",
        candidate_count=candidate_count, confirmed_fields=confirmed_fields,
    )
    if not res.passed:
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason="mcv_failed: " + ",".join(res.failures),
        )
    try:
        append_feature_contract_event(
            conn, run_id=run_id, type=MINIMUM_CONTRACT_VALIDATED,
            payload={"draft_doc_id": state.draft_doc_id, "checks": {"failures": []}}, actor=actor,
            expected_version=head_version,   # X4 — CAS on the folded head
        )
    except ConcurrencyError:
        # A concurrent feature_contract transition advanced the head since the fold → fail closed.
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason="stale")
    return CommandResult(accepted=True, aggregate_id=run_id)
