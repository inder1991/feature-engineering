"""Release-B Task 9 — ``AnalysisExecutionIRV2``: the analysis plan with its decisions SEALED.

**The V1 → V2 delta, stated explicitly** (the plan review asked for exactly this, because
``AnalysisExecutionIRV1`` carries no version discriminator at all and its ``plan_hash`` is a raw
``"|"``-join of stringified fields):

======================  =========================================================================
V1                      V2
======================  =========================================================================
the computation         **unchanged** — V2 CONTAINS a validated ``AnalysisExecutionIRV1`` rather
                        than restating it, so there is one executor, one compiler and one place
                        the computation is defined. ``run_analysis`` still takes the V1.
(no version field)      ``contract_version = 2``, and the token is INSIDE the hashed content, so a
                        future V3 cannot collide with a V2 that happens to carry the same fields.
(no decision refs)      the SIX PINS (D6), per need and per row decision:
                        ``dataset_profile_hash``, ``serving_policy_revision_id``,
                        ``source_selection_hash``, ``binding_revision_id``,
                        ``temporal_policy_revision_id``, ``row_selection_hash``.
``plan_hash`` = sha256  ``plan_hash`` = ``materialize_hash`` (RFC 8785 JCS, D1) over canonical
of a ``|``-joined       content that includes the version token AND the decision refs. V1's join
string, truncated       is NOT retained: it cannot carry a version, it does not escape (a column
                        named ``a|b`` and the pair ``a``,``b`` are the same bytes), and it
                        truncates to 32 hex.
======================  =========================================================================

**Nothing re-keys.** ``AnalysisExecutionIRV1.plan_hash`` is byte-identical to what it always was —
V2 hashes :meth:`AnalysisExecutionIRV1.identity_payload`, a structural restatement added beside it,
never the join. And no stored analysis plan exists anywhere on this tree today: the store this
release seals into (``structured_result``, result type ``analysis_execution_plan``) has never had a
row of that type written by any code path, so switching the identity function rewrites nothing.
Turning the flag on cannot re-key an existing plan because there are no existing plans.

**V1 remains constructible.** Every current caller — the executor's own tests, the plan-to-execution
bridge, the batch paths — builds a V1 and is untouched. V2 is built ONLY when
``FEATUREGEN_SOURCE_TEMPORAL_SELECTION`` is on AND the selections resolved; a refused or
half-resolved plan produces no V2, and therefore seals nothing.

**Snapshot item kinds are deliberately NOT wired here** — see :data:`SNAPSHOT_KINDS_DEFERRED`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.data_agent.analysis import AnalysisExecutionIRV1
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.source_selection import (
    CandidateDecisionV1,
    DatasetSourceSelectionV1,
    reject_freshness_keys,
)
from featuregen.overlay.upload.temporal_policy import DatasetRowSelectionV1

#: The contract name and version that together identify an analysis-plan record. Both enter the
#: hashed content, and both are the ``structured_result`` store's ``(result_type, result_version)``
#: key — one spelling, so a stored row and a computed identity can never disagree about which
#: contract produced it.
ANALYSIS_PLAN_CONTRACT = "analysis_execution_plan"
ANALYSIS_PLAN_CONTRACT_VERSION = 2

#: WHY the six snapshot item kinds (`feature_metadata_snapshot.ITEM_KINDS`) get no builder in this
#: task. Profile Task 9's own rule is "use the six shared snapshot kinds WHEN feature/materialization
#: consumes the same decision". Analysis consumes them here; the FEATURE side does not consume them
#: until the Phase-G execution-wiring merge, and a snapshot pin with exactly one producer and no
#: consumer is the inert-mechanism failure this programme has now found seven times. The kinds stay
#: REGISTERED vocabulary with the compat-safe hash rule already in place (D6), so the builders land
#: additively with no migration and no re-hash of a stored snapshot.
SNAPSHOT_KINDS_DEFERRED = (
    "dataset_profile", "serving_policy", "source_selection",
    "physical_binding", "temporal_policy", "row_selection",
)


class SealedPlanError(ValueError):
    """A sealed plan could not be built or read back. Never a decision outcome — a decision outcome
    is a typed refusal that rides the payload, which is what `SEALED_PLAN_STALE_CODES` are for."""


# ── the stale vocabulary: one code per pin, plus absence ─────────────────────────────────────────
#
# CLOSED, and deliberately NOT added to `intent.UNRESOLVED_CODES`, `learning.GAP_CODES` or
# `REFUSAL_TO_GAP`. A stale pin is not a decision anyone is waiting to make: the catalog moved and
# the honest action is to re-plan, exactly as `learning.py` keeps capability and data-quality
# failures out of the ontology-gap queue. Recording "someone published a newer serving policy" as a
# gap would put an event in a human's queue whose only resolution is "yes, that happened".

SEALED_PLAN_ABSENT = "SEALED_PLAN_ABSENT"
SEALED_PLAN_STALE_DATASET_PROFILE = "SEALED_PLAN_STALE_DATASET_PROFILE"
SEALED_PLAN_STALE_SERVING_POLICY = "SEALED_PLAN_STALE_SERVING_POLICY"
SEALED_PLAN_STALE_SOURCE_SELECTION = "SEALED_PLAN_STALE_SOURCE_SELECTION"
SEALED_PLAN_STALE_PHYSICAL_BINDING = "SEALED_PLAN_STALE_PHYSICAL_BINDING"
SEALED_PLAN_STALE_TEMPORAL_POLICY = "SEALED_PLAN_STALE_TEMPORAL_POLICY"
SEALED_PLAN_STALE_ROW_SELECTION = "SEALED_PLAN_STALE_ROW_SELECTION"
#: The caller executing may not read a dataset the plan was sealed against. Same words as "missing"
#: at the surface (D11), a distinct code here so the audit can tell the two apart.
SEALED_PLAN_SOURCE_UNREADABLE = "SEALED_PLAN_SOURCE_UNREADABLE"

#: One code per pin, in the D6 order, plus absence and read scope.
SEALED_PLAN_REFUSAL_CODES: frozenset[str] = frozenset({
    SEALED_PLAN_ABSENT,
    SEALED_PLAN_STALE_DATASET_PROFILE,
    SEALED_PLAN_STALE_SERVING_POLICY,
    SEALED_PLAN_STALE_SOURCE_SELECTION,
    SEALED_PLAN_STALE_PHYSICAL_BINDING,
    SEALED_PLAN_STALE_TEMPORAL_POLICY,
    SEALED_PLAN_STALE_ROW_SELECTION,
    SEALED_PLAN_SOURCE_UNREADABLE,
})


def _text(raw: object, *, what: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise SealedPlanError(f"{what} must not be blank on a sealed plan")
    return value


@dataclass(frozen=True, slots=True)
class SealedSourceDecisionV1:
    """FOUR of the six pins, for ONE dataset need, plus what it takes to revalidate them.

    ``entity_id``/``serving_purpose``/``need_role`` are carried because the serving-policy pointer
    is keyed by exactly that triple (§6.4): without them a revalidation could compare the sealed
    policy id against nothing, which reads as "unchanged" and is the silent re-derivation this task
    exists to prevent.
    """

    need_role: str
    entity_id: str
    serving_purpose: str
    execution_tier: str
    #: The remaining two ``DatasetNeedV1`` fields. They are here for ONE reason: without them the
    #: immutable ``DatasetSourceSelectionV1`` cannot be rebuilt from the seal, and a sealed record
    #: that cannot reproduce the hash it is filed under is a SUMMARY of the decision rather than
    #: the decision. Both enter that contract's content hash.
    required_concepts: tuple[str, ...]
    explicit_dataset_ref: str | None
    dataset_ref: str
    #: PIN 1 (``dataset_profile``).
    dataset_profile_hash: str
    #: PIN 2 (``serving_policy``) — ``None`` when no policy governed this need, and a policy
    #: APPEARING later is drift exactly as a policy changing is.
    serving_policy_revision_id: str | None
    #: PIN 3 (``source_selection``) — the composite decision identity.
    source_selection_hash: str
    #: PIN 4 (``physical_binding``).
    binding_revision_id: str
    selection_basis: str
    authority_basis: str
    authority_role: str
    #: Rule 9's alternatives-considered, sealed WITH the decision. Read-scoped at RENDER time, never
    #: at seal time: the record is what the planner saw, and narrowing the record to each reader
    #: would make the audit trail a function of who is looking at it.
    considered_candidates: tuple[CandidateDecisionV1, ...] = ()

    @classmethod
    def from_selection(cls, selection: DatasetSourceSelectionV1) -> SealedSourceDecisionV1:
        if not isinstance(selection, DatasetSourceSelectionV1):
            raise SealedPlanError("a sealed source decision is built from a "
                                  "DatasetSourceSelectionV1")
        need = selection.need
        return cls(
            need_role=need.need_role.value,
            entity_id=need.entity_id,
            serving_purpose=need.serving_purpose.value,
            execution_tier=need.execution_tier.value,
            required_concepts=tuple(need.required_concepts),
            explicit_dataset_ref=need.explicit_dataset_ref,
            dataset_ref=selection.selected_dataset_ref,
            dataset_profile_hash=selection.selected_dataset_profile_hash,
            serving_policy_revision_id=selection.serving_policy_revision_id,
            source_selection_hash=selection.content_hash,
            binding_revision_id=selection.selected_binding_revision_id,
            selection_basis=selection.selection_basis.value,
            authority_basis=selection.authority_basis.value,
            authority_role=selection.authority_role,
            considered_candidates=tuple(selection.considered_candidates))

    def need(self) -> Any:
        """The immutable ``DatasetNeedV1`` this decision answered, rebuilt from the seal."""
        from featuregen.overlay.upload.source_selection import DatasetNeedV1

        return DatasetNeedV1(
            entity_id=self.entity_id, need_role=self.need_role,
            serving_purpose=self.serving_purpose, execution_tier=self.execution_tier,
            required_concepts=self.required_concepts,
            explicit_dataset_ref=self.explicit_dataset_ref)

    def rebuild(self) -> DatasetSourceSelectionV1:
        """The immutable ``DatasetSourceSelectionV1``, reconstructed from the sealed payload.

        Its ``content_hash`` MUST equal :attr:`source_selection_hash`; revalidation checks exactly
        that before it looks at the catalog, so a tampered or truncated seal is caught as
        corruption rather than silently re-derived into a decision nobody made.
        """
        return DatasetSourceSelectionV1(
            need=self.need(),
            selected_dataset_ref=self.dataset_ref,
            selected_dataset_profile_hash=self.dataset_profile_hash,
            selected_binding_revision_id=self.binding_revision_id,
            serving_policy_revision_id=self.serving_policy_revision_id,
            authority_role=self.authority_role,
            authority_basis=self.authority_basis,
            selection_basis=self.selection_basis,
            considered_candidates=self.considered_candidates)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SealedSourceDecisionV1:
        policy_id = payload.get("serving_policy_revision_id")
        explicit = payload.get("explicit_dataset_ref")
        return cls(
            need_role=_text(payload.get("need_role"), what="need_role"),
            entity_id=_text(payload.get("entity_id"), what="entity_id"),
            serving_purpose=_text(payload.get("serving_purpose"), what="serving_purpose"),
            execution_tier=_text(payload.get("execution_tier"), what="execution_tier"),
            required_concepts=tuple(str(c) for c in payload.get("required_concepts") or ()),
            explicit_dataset_ref=(None if explicit is None else str(explicit)),
            dataset_ref=_text(payload.get("dataset_ref"), what="dataset_ref"),
            dataset_profile_hash=_text(payload.get("dataset_profile_hash"),
                                       what="dataset_profile_hash"),
            serving_policy_revision_id=(None if policy_id is None else str(policy_id)),
            source_selection_hash=_text(payload.get("source_selection_hash"),
                                        what="source_selection_hash"),
            binding_revision_id=_text(payload.get("binding_revision_id"),
                                      what="binding_revision_id"),
            selection_basis=_text(payload.get("selection_basis"), what="selection_basis"),
            authority_basis=_text(payload.get("authority_basis"), what="authority_basis"),
            authority_role=_text(payload.get("authority_role"), what="authority_role"),
            considered_candidates=tuple(
                CandidateDecisionV1(
                    dataset_ref=c["dataset_ref"],
                    dataset_profile_hash=c["dataset_profile_hash"],
                    binding_revision_id=c.get("binding_revision_id"),
                    disposition=c["disposition"],
                    reason_codes=tuple(c.get("reason_codes") or ()))
                for c in payload.get("considered_candidates") or ()))

    def payload(self) -> dict[str, Any]:
        return {
            "need_role": self.need_role,
            "entity_id": self.entity_id,
            "serving_purpose": self.serving_purpose,
            "execution_tier": self.execution_tier,
            "required_concepts": list(self.required_concepts),
            "explicit_dataset_ref": self.explicit_dataset_ref,
            "dataset_ref": self.dataset_ref,
            "dataset_profile_hash": self.dataset_profile_hash,
            "serving_policy_revision_id": self.serving_policy_revision_id,
            "source_selection_hash": self.source_selection_hash,
            "binding_revision_id": self.binding_revision_id,
            "selection_basis": self.selection_basis,
            "authority_basis": self.authority_basis,
            "authority_role": self.authority_role,
            "considered_candidates": [c.payload() for c in self.considered_candidates],
        }


@dataclass(frozen=True, slots=True)
class SealedRowDecisionV1:
    """The remaining TWO pins, for ONE dataset's row rule."""

    dataset_ref: str
    dataset_profile_hash: str
    #: PIN 5 (``temporal_policy``).
    temporal_policy_revision_id: str
    #: PIN 6 (``row_selection``).
    row_selection_hash: str
    selection_kind: str
    #: The cutoff's REF, never its value (§6.5). A literal here would re-key the same governed
    #: decision on every day it was replayed.
    cutoff_value_ref: str | None = None
    #: WHICH ROWS, in the row rule's own words — the half-open ``[from, to)`` pair for an SCD2
    #: cutoff selection, the snapshot comparison for a latest-snapshot one. Sealed because "the row
    #: semantics" is half of what plan preview has to render (§5.7): "valid at the report cutoff"
    #: is a label, and the two column refs and two operators under it are the claim.
    predicate_payloads: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_selection(cls, row: DatasetRowSelectionV1) -> SealedRowDecisionV1:
        if not isinstance(row, DatasetRowSelectionV1):
            raise SealedPlanError("a sealed row decision is built from a DatasetRowSelectionV1")
        return cls(
            dataset_ref=row.dataset_logical_ref,
            dataset_profile_hash=row.dataset_profile_hash,
            temporal_policy_revision_id=row.temporal_policy_revision_id,
            row_selection_hash=row.content_hash,
            selection_kind=row.selection_kind.value,
            cutoff_value_ref=row.cutoff_value_ref,
            predicate_payloads=tuple(dict(p) for p in row.predicate_payloads))

    def rebuild(self) -> DatasetRowSelectionV1:
        """The immutable ``DatasetRowSelectionV1``, reconstructed from the sealed payload."""
        return DatasetRowSelectionV1(
            dataset_logical_ref=self.dataset_ref,
            dataset_profile_hash=self.dataset_profile_hash,
            temporal_policy_revision_id=self.temporal_policy_revision_id,
            selection_kind=self.selection_kind,
            cutoff_value_ref=self.cutoff_value_ref,
            predicate_payloads=tuple(dict(p) for p in self.predicate_payloads))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SealedRowDecisionV1:
        cutoff = payload.get("cutoff_value_ref")
        return cls(
            dataset_ref=_text(payload.get("dataset_ref"), what="dataset_ref"),
            dataset_profile_hash=_text(payload.get("dataset_profile_hash"),
                                       what="dataset_profile_hash"),
            temporal_policy_revision_id=_text(payload.get("temporal_policy_revision_id"),
                                              what="temporal_policy_revision_id"),
            row_selection_hash=_text(payload.get("row_selection_hash"),
                                     what="row_selection_hash"),
            selection_kind=_text(payload.get("selection_kind"), what="selection_kind"),
            cutoff_value_ref=(None if cutoff is None else str(cutoff)),
            predicate_payloads=tuple(dict(p) for p in payload.get("predicate_payloads") or ()))

    def payload(self) -> dict[str, Any]:
        return {
            "dataset_ref": self.dataset_ref,
            "dataset_profile_hash": self.dataset_profile_hash,
            "temporal_policy_revision_id": self.temporal_policy_revision_id,
            "row_selection_hash": self.row_selection_hash,
            "selection_kind": self.selection_kind,
            "cutoff_value_ref": self.cutoff_value_ref,
            "predicate_payloads": [dict(p) for p in self.predicate_payloads],
        }


@dataclass(frozen=True, slots=True)
class SealedDecisionRefsV1:
    """Every decision ref one analysis plan rests on — the six pins, grouped by what they pin."""

    sources: tuple[SealedSourceDecisionV1, ...] = ()
    rows: tuple[SealedRowDecisionV1, ...] = ()
    #: Warnings that rode a RESOLVED decision (e.g. ``PROPOSED_AUTHORITY_USED``). Sealed with the
    #: plan because the authority a decision was made under is part of what the decision WAS — a
    #: warning dropped at seal time would make an unconfirmed classification look confirmed the
    #: moment the plan was recorded.
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Sorted so an enumeration order cannot re-key an identical set of decisions. The row
        # decisions sort by ref for the same reason; a plan is its decisions, not the order the
        # resolver happened to walk its needs in.
        object.__setattr__(self, "sources", tuple(sorted(
            self.sources, key=lambda s: (s.need_role, s.dataset_ref))))
        object.__setattr__(self, "rows", tuple(sorted(self.rows, key=lambda r: r.dataset_ref)))
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))

    def payload(self) -> dict[str, Any]:
        return {
            "sources": [s.payload() for s in self.sources],
            "rows": [r.payload() for r in self.rows],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SealedDecisionRefsV1:
        return cls(
            sources=tuple(SealedSourceDecisionV1.from_payload(s)
                          for s in payload.get("sources") or ()),
            rows=tuple(SealedRowDecisionV1.from_payload(r) for r in payload.get("rows") or ()),
            warnings=tuple(str(w) for w in payload.get("warnings") or ()))


@dataclass(frozen=True, slots=True)
class AnalysisExecutionIRV2:
    """A validated analysis plan WITH the decisions it rests on, pinned.

    Composition, not restatement: ``execution_ir`` IS the V1 the executor runs, so there is exactly
    one definition of the computation and ``run_analysis`` needs no V2 awareness. What V2 adds is
    the governance half — which copy of each dataset, under which policy, at which binding
    revision, with which rows — and an identity that covers BOTH halves, so a plan whose
    computation is unchanged but whose source moved is a DIFFERENT plan.
    """

    execution_ir: AnalysisExecutionIRV1
    decisions: SealedDecisionRefsV1
    contract_version: int = ANALYSIS_PLAN_CONTRACT_VERSION
    #: Provenance, never identity — see :attr:`plan_hash`.
    question: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution_ir, AnalysisExecutionIRV1):
            raise SealedPlanError("execution_ir must be a validated AnalysisExecutionIRV1")
        if not isinstance(self.decisions, SealedDecisionRefsV1):
            raise SealedPlanError("decisions must be a SealedDecisionRefsV1")
        if self.contract_version != ANALYSIS_PLAN_CONTRACT_VERSION:
            raise SealedPlanError(
                f"contract_version {self.contract_version!r} is not "
                f"{ANALYSIS_PLAN_CONTRACT_VERSION}; a version token that can be set to anything "
                "discriminates nothing")
        if not self.decisions.sources:
            raise SealedPlanError(
                "an AnalysisExecutionIRV2 with no source decisions pins nothing and is a V1 "
                "wearing a version number; build it only from a RESOLVED selection")

    def content_payload(self) -> dict[str, Any]:
        """The canonical JCS content the plan identity is taken over.

        ``question`` is EXCLUDED, exactly as it is from V1: two differently-worded questions that
        resolve to the same computation over the same decisions are the same plan. This matters
        more here than it did in V1, because this payload is also the SEALED BYTES — a question
        inside them would make the second wording of one plan a content-address collision with
        different bytes, which the structured-result store correctly reports as corruption.
        """
        payload = {
            "contract": ANALYSIS_PLAN_CONTRACT,
            "contract_version": self.contract_version,
            "computation": self.execution_ir.identity_payload(),
            "decisions": self.decisions.payload(),
        }
        reject_freshness_keys(payload, where="a sealed analysis plan")
        return payload

    @property
    def plan_hash(self) -> str:
        """Versioned JCS identity over computation AND decisions (D1)."""
        return materialize_hash(self.content_payload())

    @property
    def v1_plan_hash(self) -> str:
        """The V1 identity of the computation half, unchanged and still reachable.

        NOT part of V2's identity — it is the flag-off plan hash the preview has always shown, kept
        addressable so the flag matrix can prove it did not move.
        """
        return self.execution_ir.plan_hash


def seal_refs_from_selections(selections: object) -> SealedDecisionRefsV1:
    """The six pins, lifted from a RESOLVED :class:`~featuregen.analysis.plan.SelectionPreviewV1`.

    Refuses an unresolved preview rather than sealing a partial one: ``resolved`` already means
    "every need that was asked about got its decision and nothing refused", and a plan sealed
    without one of its decisions would be replayed later as though the missing decision had been
    made and simply not recorded.
    """
    if selections is None:
        raise SealedPlanError(
            "there are no selections to seal; the source/temporal flag is off, and a flag-off plan "
            "is the V1 path by design")
    if not getattr(selections, "resolved", False):
        raise SealedPlanError(
            "a plan whose selections did not resolve seals nothing: a refusal is a question for a "
            "person, and recording it as a plan would replay as a decision that was never made")
    return SealedDecisionRefsV1(
        sources=tuple(SealedSourceDecisionV1.from_selection(s)
                      for s in selections.source_selections),
        rows=tuple(SealedRowDecisionV1.from_selection(r) for r in selections.row_selections),
        warnings=tuple(selections.warnings))


def build_execution_ir_v2(execution_ir: AnalysisExecutionIRV1, selections: object, *,
                          question: str = "") -> AnalysisExecutionIRV2:
    """V1 + the sealed decision refs. The ONLY constructor callers should use."""
    return AnalysisExecutionIRV2(
        execution_ir=execution_ir,
        decisions=seal_refs_from_selections(selections),
        question=question or execution_ir.question)


__all__ = [
    "ANALYSIS_PLAN_CONTRACT",
    "ANALYSIS_PLAN_CONTRACT_VERSION",
    "SEALED_PLAN_ABSENT",
    "SEALED_PLAN_REFUSAL_CODES",
    "SEALED_PLAN_SOURCE_UNREADABLE",
    "SEALED_PLAN_STALE_DATASET_PROFILE",
    "SEALED_PLAN_STALE_PHYSICAL_BINDING",
    "SEALED_PLAN_STALE_ROW_SELECTION",
    "SEALED_PLAN_STALE_SERVING_POLICY",
    "SEALED_PLAN_STALE_SOURCE_SELECTION",
    "SEALED_PLAN_STALE_TEMPORAL_POLICY",
    "SNAPSHOT_KINDS_DEFERRED",
    "AnalysisExecutionIRV2",
    "SealedDecisionRefsV1",
    "SealedPlanError",
    "SealedRowDecisionV1",
    "SealedSourceDecisionV1",
    "build_execution_ir_v2",
    "seal_refs_from_selections",
]
