"""The service-internal read interface SP-3+ binds to (§13) — the SP-2 analogue of SP-1's
`resolve_fact`. FAIL-CLOSED: a Confirmed contract is servable only when the FOLDED status is CONFIRMED
and the run is not terminal; every other status (or a rejected/withdrawn run) blocks with a reason.
The fold is authoritative — this reader never consults a projection for a decision (§11, §12)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from featuregen.contracts import DbConn
from featuregen.contracts.documents import Stage
from featuregen.documents.store import get_document
from featuregen.events.store import load_stream
from featuregen.intake.mcv import _latest_body  # R13 authoritative stream body read (mcv.py:158)
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
        """R13 DUAL ACCESS: `view["confirmed"]` / `view["draft"]` return the frozen contract BODY (read
        off the event stream `get_contract` folds — the bodies ride CONTRACT_CONFIRMED /
        DRAFT_CONTRACT_PRODUCED / CONTRACT_REFINED inline), while `view.status` / `view.run_id` / ... stay
        plain attribute access. Any other key → KeyError. SP-3 and the P9 E2E read `view["confirmed"][...]`.

        FAIL-CLOSED: the EXECUTABLE confirmed body is served ONLY when the contract is servable
        (`reason_if_unavailable is None`); a blocked run (non-CONFIRMED fold OR a terminal run) yields
        None even though confirmed_body sits on the stream — "no servable confirmed contract → nothing
        downstream" (§12). The draft body is never the executable artifact, so it is returned regardless."""
        if key not in ("confirmed", "draft"):
            raise KeyError(key)
        if key == "confirmed" and self.reason_if_unavailable is not None:
            return None  # fail-closed: only a servable contract exposes its executable confirmed body
        return self._bodies.get(key)


def _run_terminal_outcome(conn: DbConn, run_id: str) -> str | None:
    for e in load_stream(conn, "run", run_id):
        if e.type in _RUN_TERMINALS:
            return e.type
    return None


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

    # body_ref / content_hash — governance-integrity metadata for the served document (the frozen body
    # itself is READ from the event stream below, not the stubbed object store).
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

    # R13 — resolve the frozen bodies for subscript access from the AUTHORITATIVE event stream (§13): the
    # bodies ride the event payload inline — draft_body on DRAFT_CONTRACT_PRODUCED / CONTRACT_REFINED,
    # confirmed_body on CONTRACT_CONFIRMED (commands.py:708/:1949/:2100) — and `mcv._latest_body` returns
    # the newest payload carrying each key. The EXECUTABLE confirmed body is gated on servability at the
    # subscript seam (`__getitem__`), so a blocked run never hands one back.
    bodies = {
        "draft": _latest_body(stream, "draft_body"),
        "confirmed": _latest_body(stream, "confirmed_body"),
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


def read_contract_status(conn: DbConn, run_id: str) -> FeatureContractStatus | None:
    """OPTIONAL, SECONDARY status query (§11, §12). Fold-backed and fail-closed: returns the folded
    status, or None when no `feature_contract` was opened for the run. NEVER a command decision —
    handlers always fold their own stream inline (a stored OverlayProjection-style table is out of
    scope: SP-2 ships exactly two migrations, both in P1)."""
    stream = load_feature_contract(conn, run_id)
    if not stream:
        return None
    return fold_feature_contract_state(stream).status
