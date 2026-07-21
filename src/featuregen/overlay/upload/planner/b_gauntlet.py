"""Phase 3C.2b-i-B · Task 4 — the SAFETY BRAKE: gauntlet + preservation + tri-state + categorization.

B governs ONE untrusted LLM cross-catalog proposal at a time. This module is the brake between the
lossless capture (T2 :class:`RawFeatureProposalV1`) + the server-side trust derivation (T3
:class:`IdentityMapV1`) and A's assembly planner: it runs the PRODUCTION deterministic gauntlet on the
raw proposal, proves NO operand was silently dropped or rewritten, retains the Slice-3 tri-state, and
splits the vetted refs into computation operands vs structural refs.

WHY ``_validate_idea`` DIRECTLY, NOT ``_vet`` (closes review minor BM1)
----------------------------------------------------------------------
B runs the FULL safety + tri-state gauntlet — :func:`feature_assist._validate_idea` — which is the
whole deterministic floor: grounding, leakage, drift-freshness (STALE), numeric type, additivity,
units/currency, temporal point-in-time, grain, and cross-table read-scope/join authority, returning
the Slice-3 ``validation_status`` (``DESIGN_CHECKED`` / ``NEEDS_EXTERNAL_VALIDATION``) with its typed
requirements, or a typed :class:`feature_assist.Rejection`.

``feature_assist._vet`` wraps that SAME ``_validate_idea`` and adds THREE *loop-level dedup* gates on
top: name-already-``seen``, ``_redundant_of`` an already-accepted candidate, and signature-already-
``registered``. Those three are generation-MENU-assembly concerns (they need the generation loop's
mutable ``registered`` / ``accepted`` / ``seen`` state), NOT safety or governance. B governs a SINGLE
proposal, not a menu, so ``_vet``'s loop-level dedup is intentionally NOT applied here — and calling
``_validate_idea`` directly also returns the typed ``Rejection`` (with its ``RejectCode``) rather than
burying it in ``_vet``'s ``avoid`` side-list. The gauntlet itself is REUSED AS-IS and never forked.

FRESHNESS (BM2). The STALE gate's freshness RULE lives inside ``_validate_idea`` (a ``drift_watermark``
read) and is already real; the production writer that advances that watermark is
``catalog_changes.detect_catalog_changes``. This module consumes the rule unchanged.

RETURN-TYPE RECONCILIATION. The plan sketched ``-> VettedProposal | BDisposition`` but ALSO required
the ``gauntlet_rejected`` outcome to CARRY the ``RejectCode`` — a bare :class:`BDisposition` enum
member cannot. So the reject arm returns :class:`GauntletRejectionV1` (whose ``.disposition`` IS the
``BDisposition``), satisfying both. This is the one faithful deviation.

Shadow-only; no data plane. Frozen slotted dataclasses; no pydantic. Read-only over A / the gauntlet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from featuregen.overlay.upload.feature_assist import FeatureIdea, _validate_idea
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_proposal import RawFeatureProposalV1
from featuregen.overlay.upload.planner.b_scope import IdentityMapV1


class OperandCategorizationError(Exception):
    """An INVARIANT breach in operand categorization — a vetted ref landed in NEITHER or BOTH of
    ``computation_operands`` / ``structural_refs``. This is a programming/categorization bug on the
    safe (already-vetted) path, never a user-reachable rejection, so it raises rather than returning a
    disposition. ``message`` names object_refs / codes only (audit-safe)."""


@dataclass(frozen=True, slots=True)
class VettedProposal:
    """A raw proposal that CLEARED the gauntlet AND survived the raw≡vetted preservation check. The
    Slice-3 tri-state rides on ``idea.validation_status`` + ``idea.requirements`` (retained verbatim,
    never recomputed here)."""
    idea: FeatureIdea
    # (catalog_source, object_ref) columns the feature aggregates — A's OperandSlotV1s.
    computation_operands: tuple[tuple[str, str], ...]
    # (catalog_source, object_ref) grain / point-in-time / grouping refs — A's source_binding / grain.
    structural_refs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class GauntletRejectionV1:
    """A non-pass outcome. ``disposition`` is the B disposition (``gauntlet_rejected`` for a hard
    deterministic reject, ``proposal_lossy`` when the gauntlet silently dropped/rewrote an operand).
    ``reject_code`` is the ``_validate_idea`` ``RejectCode`` on ``gauntlet_rejected`` and ``None`` on
    ``proposal_lossy``. ``message`` is audit-safe: object_refs / codes only, NEVER PII or sample
    values."""
    disposition: BDisposition
    reject_code: str | None
    message: str


def _synthesized_name(proposal: RawFeatureProposalV1) -> str:
    """A DETERMINISTIC, non-load-bearing name for the raw dict ``_validate_idea`` consumes.
    ``RawFeatureProposalV1`` captures only the computational essence, not a display label; the gauntlet
    uses ``name`` only for its (skipped-here — see BM1) name-based dedup, so any deterministic string is
    safe. Never derived from data values (audit-safe)."""
    return f"b:{proposal.operation or ''}:{','.join(proposal.operands)}"


def run_gauntlet_and_preserve(
    conn, *, proposal: RawFeatureProposalV1, identity_map: IdentityMapV1,
    target_ref: str | None, roles: tuple[str, ...], now: datetime,
    fresh_within: timedelta,
) -> VettedProposal | GauntletRejectionV1:
    """Run the deterministic gauntlet on ``proposal`` and prove no operand was silently lost.

    1. Derive the gauntlet inputs from the SERVER trust inputs (never the caller): ``known`` and
       ``src_of`` come only from ``identity_map`` (T3's authorized ``object_ref -> catalog source(s)``).
    2. Build the raw dict ``_validate_idea`` consumes; ``grain_hint`` fills the ``grain_table`` key.
    3. Run the gauntlet (``_validate_idea`` — see BM1). A hard reject -> ``gauntlet_rejected`` carrying
       the ``RejectCode``.
    4. Preservation: every raw operand ``object_ref`` MUST survive into the vetted ref set (measures +
       grain + time + grouping). A dropped/rewritten operand -> ``proposal_lossy`` (the exact gap this
       task closes — the gauntlet drops ungrounded operands at ``feature_assist.py:474`` with no
       record).
    5. Categorize into computation operands vs structural refs and assert the split is disjoint AND
       total over the vetted ref set (an invariant on the safe path — raises, not a user disposition).
    """
    # 1. Gauntlet inputs come ONLY from the server-derived identity map (a caller catalog is
    #    un-injectable — there is nowhere here to inject one).
    known = set(identity_map.known)
    src_of = {ref: set(identity_map.sources_for(ref)) for ref in identity_map.known}

    # 2. The raw dict `_validate_idea` consumes. `grain_hint` fills `grain_table` (the key the gauntlet
    #    reads); `window` is captured-not-consumed by the gauntlet (it derives the window from the
    #    aggregation string) but passed through for faithfulness. `name` is deterministic + inert here.
    raw: dict = {
        "name": _synthesized_name(proposal),
        "derives_from": list(proposal.operands),
        "aggregation": proposal.operation,
        "window": proposal.window,
    }
    if proposal.grain_hint is not None:
        raw["grain_table"] = proposal.grain_hint

    # 3. The full safety + tri-state gauntlet, REUSED AS-IS (not `_vet`; see BM1 / module docstring).
    idea, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within, roles=roles)
    if rej is not None:
        return GauntletRejectionV1(BDisposition.gauntlet_rejected, rej.code, rej.message)
    assert idea is not None  # _validate_idea contract: rej is None => a FeatureIdea is returned.

    # 4. Preservation (raw ≡ vetted): the vetted ref set is every object_ref the gauntlet resolved.
    vetted_refs = {ref for _cat, ref in idea.measure_refs}
    if idea.grain_ref is not None:
        vetted_refs.add(idea.grain_ref[1])
    if idea.time_ref is not None:
        vetted_refs.add(idea.time_ref[1])
    vetted_refs |= {ref for _cat, ref in idea.grouping_refs}
    if not (set(proposal.operands) <= vetted_refs):
        dropped = sorted(set(proposal.operands) - vetted_refs)
        return GauntletRejectionV1(
            BDisposition.proposal_lossy, None, f"dropped/rewritten operands: {dropped}")

    # 5. Categorize + prove full, disjoint coverage of the resolved refs.
    computation_operands = idea.measure_refs
    structural_refs = tuple(
        r for r in (idea.grain_ref, idea.time_ref, *idea.grouping_refs) if r is not None)
    comp_set = set(computation_operands)
    struct_set = set(structural_refs)
    overlap = comp_set & struct_set
    if overlap:
        raise OperandCategorizationError(
            f"ref(s) categorized as BOTH computation and structural: {sorted(overlap)}")
    covered = {ref for _c, ref in comp_set} | {ref for _c, ref in struct_set}
    if covered != vetted_refs:
        raise OperandCategorizationError(
            f"categorization does not cover the resolved refs exactly: "
            f"missing={sorted(vetted_refs - covered)} extra={sorted(covered - vetted_refs)}")

    return VettedProposal(
        idea=idea, computation_operands=computation_operands, structural_refs=structural_refs)
