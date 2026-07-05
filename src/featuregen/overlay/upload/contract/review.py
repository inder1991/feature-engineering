"""Phase 4 — critique → refine loop + MCV (minimum contract validation).

The bounded loop is symmetric with the feature loop: each pass runs the **deterministic MCV** (the gauntlet
re-applied to the draft) *and* the **LLM critique** (advisory, on the definition narrative); their combined
findings drive `refine`; the loop stops clean or at budget. The MCV never lets the LLM gate — a structural
defect the LLM cannot fix stops the loop and is surfaced. The structured facts are immutable; refine only
re-authors the definition narrative (audited).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.enrich_llm import audited_enrich_call, audited_structured_call
from featuregen.overlay.upload.feature_assist import _validate_idea


def _live_columns(conn, object_refs: list[str]) -> set[str]:
    """The columns that ACTUALLY exist in the graph now (not the draft's own claim) — so a column
    dropped/renamed since discovery is caught as ungrounded (B2)."""
    if not object_refs:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT object_ref FROM graph_node WHERE kind = 'column' AND object_ref = ANY(%s)",
        (list(object_refs),)).fetchall()
    return {r[0] for r in rows}


def validate_minimum(conn, draft: ContractDraft, *, target_ref: str | None = None,
                     now: datetime | None = None,
                     fresh_within: timedelta = timedelta(hours=24)) -> tuple[bool, list[str]]:
    """MCV — the deterministic gauntlet re-applied to the draft (defense in depth: a source could have
    gone stale or been dropped since discovery). Reuses the feature loop's checks. No LLM."""
    raw = {"derives_from": draft.derives_from, "aggregation": draft.aggregation}
    known = _live_columns(conn, draft.derives_from)   # LIVE graph, not set(draft.derives_from) (B2)
    src_of: dict[str, set[str]] = {}                  # the draft's carried (catalog, ref) pairs (B3)
    for cs, ref in draft.derives_pairs:
        src_of.setdefault(ref, set()).add(cs)
    idea, reason = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within)
    return (idea is not None, [] if idea is not None else [reason])


def critique_contract(conn, draft: ContractDraft, client: LLMClient, *, actor=None) -> list[str]:
    """Advisory adversarial review of the definition NARRATIVE (accuracy / completeness / undocumented
    assumptions). Routed through the AUDITED seam (egress guard + registered schema + llm_call record) —
    M5. Does NOT gate — the deterministic MCV does."""
    out = audited_structured_call(
        conn, client, task="overlay.contract.critique", prompt_id="overlay_critique_v1",
        schema_id="overlay_critique",
        catalog_metadata={"feature": draft.feature_name, "definition": draft.definition,
                          "aggregation": draft.aggregation or "",
                          "derives_from": list(draft.derives_from)},
        instruction="Adversarially review this feature definition for accuracy, completeness, and "
                    "undocumented assumptions. Return findings; empty if clean. Metadata only.",
        actor=actor)
    return [str(f) for f in (out or {}).get("findings", []) if f]


def refine_contract(conn, draft: ContractDraft, findings: list[str], client: LLMClient, *,
                    actor=None) -> ContractDraft:
    """Re-author the definition to address the findings (audited). Structured facts are immutable."""
    new_def = audited_enrich_call(
        conn, client, task="overlay.contract.refine", prompt_id="overlay_contract_v1",
        schema_id="overlay_contract",
        catalog_metadata={"feature": draft.feature_name, "definition": draft.definition,
                          "findings": list(findings)},
        out_key="definition",
        instruction="Revise the feature definition to address these review findings. Metadata only; "
                    "no data values.",
        actor=actor)
    return replace(draft, definition=new_def) if new_def else draft


def author_contract(conn, draft: ContractDraft, client: LLMClient, *, target_ref: str | None = None,
                    now: datetime | None = None, budget: int = 2,
                    actor=None) -> tuple[ContractDraft, list[str]]:
    """Bounded critique→refine loop; MCV each pass. Returns (draft, unresolved_mcv_reasons) — an empty
    list means the contract passed MCV and the critique is clean (or budget was spent with MCV clean).
    `target_ref` falls back to the draft's own (M3), so the leakage check cannot silently no-op."""
    tref = target_ref if target_ref is not None else draft.target_ref
    for _ in range(budget):
        _, mcv = validate_minimum(conn, draft, target_ref=tref, now=now)
        critique = critique_contract(conn, draft, client, actor=actor)
        if not mcv and not critique:
            return draft, []                       # clean
        if mcv and not critique:
            return draft, mcv                      # structural defect the LLM can't fix → surface
        draft = refine_contract(conn, draft, mcv + critique, client, actor=actor)
    _, mcv = validate_minimum(conn, draft, target_ref=tref, now=now)
    return draft, mcv
