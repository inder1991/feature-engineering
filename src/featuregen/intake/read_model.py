"""The service-internal read interface SP-3+ binds to (§13) — the SP-2 analogue of SP-1's
`resolve_fact`. FAIL-CLOSED: a Confirmed contract is servable only when the FOLDED status is CONFIRMED
and the run is not terminal; every other status (or a rejected/withdrawn run) blocks with a reason.
The fold is authoritative — this reader never consults a projection for a decision (§11, §12)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from featuregen.contracts import DbConn
from featuregen.contracts.documents import Stage
from featuregen.documents.store import get_document
from featuregen.events.store import load_stream
from featuregen.intake.state import FeatureContractStatus, fold_feature_contract_state
from featuregen.intake.store import load_feature_contract  # R1 — append/load seam owned by P1

_RUN_TERMINALS = ("RUN_REJECTED", "RUN_WITHDRAWN", "RUN_CANCELLED")


@dataclass(frozen=True, slots=True)
class ContractView:
    run_id: str
    stage: str
    status: str
    intake_mode: str | None
    feature_name: str | None
    draft_doc_id: str | None
    confirmed_doc_id: str | None
    assumption_ledger_ref: str | None
    field_scores: Mapping[str, Any] | None
    open_questions: tuple[Any, ...]
    open_fields: tuple[str, ...]
    requires_independent_validation: bool | None
    catalog_version: str | None
    selected_candidate: str | None
    terminal_outcome: str | None
    body_ref: str | None
    content_hash: str | None
    reason_if_unavailable: str | None
    # R13 — the resolved frozen bodies for subscript access ({"draft"/"confirmed": parsed body | None}).
    _bodies: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __getitem__(self, key: str) -> Mapping[str, Any] | None:
        """R13 DUAL ACCESS: `view["confirmed"]` / `view["draft"]` return the frozen contract BODY
        (parsed from the governance-retained blob at the document's `body_ref` via the document store),
        while `view.status` / `view.run_id` / ... stay plain attribute access. Any other key → KeyError.
        SP-3 and the P9 E2E read `view["confirmed"][...]`."""
        if key not in ("confirmed", "draft"):
            raise KeyError(key)
        return self._bodies.get(key)


def _run_terminal_outcome(conn: DbConn, run_id: str) -> str | None:
    for e in load_stream(conn, "run", run_id):
        if e.type in _RUN_TERMINALS:
            return e.type
    return None


def _read_frozen_body(conn: DbConn, body_ref: str | None) -> bytes | None:
    """Read the governance-retained object SP-2 wrote when it froze a contract document (`body_ref`),
    or None if unresolved. The object-store binding is wired in P9 (the E2E freezes real bodies); the
    fold + attribute view never depend on it (fail-soft), so unit tests need no object store."""
    if body_ref is None:
        return None
    # SP-2 governance-retained object-store read (bound in P9's `_wire`); fail-soft until then.
    return None


def _load_document_body(conn: DbConn, doc_id: str) -> Mapping[str, Any] | None:
    """Resolve a frozen document's parsed body for R13 subscript access: `get_document` → `body_ref` →
    the governance-retained blob → JSON. None when the document or its body is not resolvable."""
    doc = get_document(conn, doc_id)
    if doc is None:
        return None
    raw = _read_frozen_body(conn, doc.get("body_ref"))
    return json.loads(raw) if raw is not None else None


def get_contract(conn: DbConn, run_id: str) -> ContractView | None:
    """Fetch the folded Feature Contract view for a run, or None if no `feature_contract` was opened.
    Servable (reason_if_unavailable is None) ONLY for a CONFIRMED, non-terminal-run contract."""
    stream = load_feature_contract(conn, run_id)
    if not stream:
        return None
    state = fold_feature_contract_state(stream)
    terminal_outcome = _run_terminal_outcome(conn, run_id)
    is_confirmed = (
        state.status is FeatureContractStatus.CONFIRMED and terminal_outcome is None
    )
    stage = Stage.CONFIRMED_CONTRACT.value if is_confirmed else Stage.DRAFT_CONTRACT.value
    doc_id = state.confirmed_doc_id if is_confirmed else state.draft_doc_id

    body_ref = content_hash = None
    if doc_id is not None:
        doc = get_document(conn, doc_id)
        if doc is not None:
            body_ref = doc.get("body_ref")
            content_hash = doc.get("content_hash")

    if terminal_outcome is not None:
        reason: str | None = f"run terminal: {terminal_outcome}"
    elif state.status is not FeatureContractStatus.CONFIRMED:
        status_label = state.status.value if state.status is not None else "MISSING"
        reason = f"not confirmed (status={status_label})"
    else:
        reason = None

    # R13 — resolve the frozen bodies for subscript access (fail-soft when the object store is unbound).
    bodies = {
        "draft": _load_document_body(conn, state.draft_doc_id) if state.draft_doc_id else None,
        "confirmed": _load_document_body(conn, state.confirmed_doc_id) if state.confirmed_doc_id else None,
    }

    return ContractView(
        run_id=run_id,
        stage=stage,
        status=state.status.value if state.status is not None else "MISSING",
        intake_mode=state.intake_mode,
        feature_name=state.feature_name or state.proposed_feature_name,
        draft_doc_id=state.draft_doc_id,
        confirmed_doc_id=state.confirmed_doc_id,
        assumption_ledger_ref=state.assumption_ledger_ref,
        field_scores=state.field_scores,
        open_questions=state.open_questions,
        open_fields=state.open_fields,
        requires_independent_validation=state.requires_independent_validation,
        catalog_version=state.catalog_version,
        selected_candidate=state.selected_candidate,
        terminal_outcome=terminal_outcome,
        body_ref=body_ref,
        content_hash=content_hash,
        reason_if_unavailable=reason,
        _bodies=bodies,
    )
