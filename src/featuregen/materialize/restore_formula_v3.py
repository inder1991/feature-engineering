"""Rehydrate a READY formula draft into something the V2 compiler can take.

**The bridge that did not exist.** ``materialize/resolve.py`` restores V1 features from the
recipe/shadow lane's work items — the anchor that lane happens to use. The V2 path has a different
anchor: a ``formula_draft``, produced when a person pressed *Draft formula* on a candidate. Nothing
converted one into ``ResolvedFeatureInputV2``, so an admitted V2 formula could not be handed to
anything. This is that conversion, and it is the whole of it.

**Named `restore_`, not `resolve_`, deliberately.** ``resolve_output_v2`` already exists, and the two
verbs are different jobs:

* ``resolve_*`` DECIDES a value that was undetermined — an output policy, a physical type.
* ``restore_*`` REHYDRATES a stored artifact into the object a compiler can use.

A ``resolve_v2`` sitting beside ``resolve_output_v2`` would read as its general case, which it is
not.

**The result is rebuilt from the TRACE, never from the stored JSON.** The draft carries
``formula_json`` for a person to read; this reads the write-once authoring trace instead, through
the same verified checkpoint the V1 restorer uses. A trace that cannot be replayed cannot say what
the run decided — and a stored blob, however convenient, is a copy that no longer proves anything
about the run that produced it. The two are cross-checked: if the trace's proposal hash disagrees
with the draft's, that is a refusal rather than a preference for one of them.

**Group-shaped, and all or nothing.** Like V1's restorer and for the same reason: a caller that got
the survivors of a refused group would be compiling a group whose membership nobody decided.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.formula.control import FormulaControlFlow

from featuregen.formula.replay_authoring_v2 import (  # isort: skip
    _restore_terminal_result as _restore_v2,
)
from featuregen.formula.replay_trace import load_verified_checkpoint as _load_checkpoint
from featuregen.formula.turns import AuthoringIntent
from featuregen.materialize.admission_v2 import ResolvedFeatureInputV2
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.overlay.upload.formula_draft_store import DraftStateV1

__all__ = ["RestoredFormulaV3", "restore_build_set_formulas", "restore_formula"]

_SELECT_MANIFEST_VERSIONS = (
    "SELECT versions FROM formula_authoring_run WHERE authoring_run_id = %s")


@dataclass(frozen=True, slots=True)
class RestoredFormulaV3:
    """One rehydrated formula, and the selection that asked for it.

    ``selection_revision_id`` rides along because every refusal downstream must name the MEMBER a
    person chose, not an internal draft id they have never seen. A group refusal that says
    ``fd_01M0…`` is not actionable.
    """

    selection_revision_id: str
    formula_draft_id: str
    input: ResolvedFeatureInputV2


def restore_formula(
    conn: DbConn,
    *,
    selection_revision_id: str,
) -> RestoredFormulaV3:
    """Rehydrate ONE selection's READY formula, or refuse with a code naming the selection.

    Raises:
        MaterializationRefused: the selection has no draft (``NOT_RESOLVED``), its draft never
            reached READY (``NOT_RESOLVED``), the run has no replayable trace
            (``AUTHORING_RUN_INCOMPLETE``), or the trace and the stored draft disagree about which
            formula was produced (``INTENT_HASH_MISMATCH``).
    """
    row = conn.execute(
        "SELECT s.considered_revision_id, s.option_id, d.formula_draft_id, d.state, "
        "       d.authoring_run_id, d.formula_content_hash "
        "  FROM feature_selection_revision s "
        "  LEFT JOIN formula_draft d "
        "    ON d.considered_revision_id = s.considered_revision_id "
        "   AND d.option_id = s.option_id "
        " WHERE s.revision_id = %s "
        " ORDER BY d.updated_at DESC LIMIT 1", (selection_revision_id,)).fetchone()
    if row is None:
        raise MaterializationRefused(
            CompilationRefusalCode.NOT_RESOLVED,
            f"selection {selection_revision_id} does not exist, so there is no chosen candidate to "
            f"build a formula for")

    considered_revision_id, option_id, draft_id, state, run_id, stored_hash = row
    if draft_id is None:
        raise MaterializationRefused(
            CompilationRefusalCode.NOT_RESOLVED,
            f"selection {selection_revision_id} (option {option_id} of {considered_revision_id}) "
            f"has no formula draft: a feature cannot be built from a candidate nobody has drafted "
            f"a formula for. Draft it first — selecting and drafting are separate acts")

    if DraftStateV1(state) is not DraftStateV1.READY:
        raise MaterializationRefused(
            CompilationRefusalCode.NOT_RESOLVED,
            f"selection {selection_revision_id} names formula draft {draft_id}, which is {state} "
            f"rather than READY. Only a finished formula can be compiled, and a draft that stopped "
            f"short has already recorded why")

    intent = _intent_of(conn, considered_revision_id, option_id, selection_revision_id)
    result = _restore_result(conn, run_id, draft_id, intent=intent)

    # THE TRACE AND THE DRAFT MUST AGREE. The draft's stored hash is what a person was shown; the
    # trace is what the run actually produced. Compiling while those differ would generate code for
    # a formula nobody reviewed — so a disagreement is a refusal, not a choice between them.
    if stored_hash and result.candidate_proposal_hash != stored_hash:
        raise MaterializationRefused(
            CompilationRefusalCode.INTENT_HASH_MISMATCH,
            f"formula draft {draft_id} stores proposal {stored_hash} but authoring run {run_id} "
            f"produced {result.candidate_proposal_hash}. The draft is what a person read and the "
            f"trace is what the run decided; building while they disagree would compile a formula "
            f"nobody reviewed")

    return RestoredFormulaV3(
        selection_revision_id=selection_revision_id,
        formula_draft_id=draft_id,
        input=ResolvedFeatureInputV2(intent, result))


def restore_build_set_formulas(
    conn: DbConn, *, selection_revision_ids: Sequence[str],
) -> tuple[RestoredFormulaV3, ...]:
    """Rehydrate a whole build set IN THE ORDER IT WAS DECLARED, or refuse the whole set.

    Order is preserved rather than sorted, unlike V1's restorer. V1 sorts because its work-item ids
    carry no chosen order; a build set's members DO — the order a person picked features in is
    recorded in ``build_set_member.position`` — and re-sorting here would discard a fact the set
    went to some trouble to keep.

    All or nothing, for the reason ``admit_artifacts_v2`` gives: a caller handed the survivors of a
    refused group would compile a group whose membership nobody decided. The first refusal names its
    selection and stops the set.
    """
    if not selection_revision_ids:
        raise ValueError(
            "a build set with no selections restores nothing: an empty group is a caller defect "
            "rather than a verdict about any feature, so it is not a governed refusal")
    duplicates = sorted({s for s in selection_revision_ids
                         if list(selection_revision_ids).count(s) > 1})
    if duplicates:
        raise ValueError(
            f"the same selection appears twice: {duplicates!r}. Position is meaningful in a build "
            f"set, so a duplicate makes the group's column order unanswerable")

    return tuple(restore_formula(conn, selection_revision_id=selection)
                 for selection in selection_revision_ids)


def _intent_of(
    conn: DbConn, considered_revision_id: str, option_id: str, selection_revision_id: str,
) -> AuthoringIntent:
    """The intent the run was opened for, rebuilt from the FROZEN considered revision.

    Read from the revision rather than stored on the draft, for the reason ``contract.py`` calls
    BLOCKER 1: a copy can describe a candidate the revision no longer holds, and the revision is
    immutable so reading it late reads the same bytes the draft was requested against.
    """
    from featuregen.overlay.upload.contract.gate1 import (
        CONSIDERED_CANONICALIZATION_VERSION,
        Gate1Error,
        UnknownConsideredOption,
        _chosen_option_from_revision,
    )

    row = conn.execute(
        "SELECT r.considered_json, i.hypothesis FROM contract_considered_revision r "
        "JOIN contract_intent i ON i.intent_id = r.intent_id "
        "WHERE r.considered_revision_id = %s", (considered_revision_id,)).fetchone()
    if row is None:
        raise MaterializationRefused(
            CompilationRefusalCode.NOT_RESOLVED,
            f"selection {selection_revision_id} names considered revision "
            f"{considered_revision_id}, which does not exist")

    considered = row[0] if isinstance(row[0], dict) else {}

    # PRE-REGENERATION REVISIONS ARE READABLE, NOT EXECUTABLE. A v2 revision's options were sealed
    # under an identity that did not include their typed computation, so the hash it carries does
    # not describe what the candidate computes. Building from one would be a build of something
    # nobody can name — and the old revisions are kept precisely so the record of what was offered
    # survives. The refusal names the remedy rather than the version.
    version = considered.get("version")
    if version and version != CONSIDERED_CANONICALIZATION_VERSION:
        raise MaterializationRefused(
            CompilationRefusalCode.CANDIDATE_REGENERATION_REQUIRED,
            f"selection {selection_revision_id} names considered revision "
            f"{considered_revision_id}, frozen under {version}. Candidates from that "
            f"canonicalization do not carry their typed computation — operation, measures, grain, "
            f"time column, window and grouping — so their identity does not say what they compute. "
            f"They remain readable for audit and cannot be executed: regenerate the candidate set "
            f"and select again")

    try:
        idea, _source, _identity = _chosen_option_from_revision(considered, option_id)
    except (Gate1Error, UnknownConsideredOption) as exc:
        raise MaterializationRefused(
            CompilationRefusalCode.NOT_RESOLVED,
            f"selection {selection_revision_id} names option {option_id}, which its considered "
            f"revision cannot resolve: {exc}") from exc

    from featuregen.overlay.upload.column_authority import logical_ref_of

    # ── THE GRAIN, AND NO FALLBACK ──────────────────────────────────────────────────────────────
    # This previously read: use the declared grain if there is one, OTHERWISE every operand ref,
    # sorted. That fallback was not a safety net — it was a guess that always fired, because the
    # serializer dropped `grain_ref` and so the declared grain was ALWAYS absent. Every formula was
    # authored against a grain listing each column it derives from: a feature meant to be per
    # customer described to the model as grained on its amount, its date and its customer together.
    #
    # It was invisible because it was CONSISTENT: the intent hash covers the grain keys, the
    # restorer re-derived the same wrong value, and every check agreed with every other. A guess
    # that produces a plausible answer and is confirmed by its own re-derivation is worse than an
    # exception, because nothing ever contradicts it.
    #
    # Missing grain is now a named refusal. A candidate that does not say what it is computed PER
    # cannot be executed, and saying so is the only honest answer.
    if not idea.grain_refs:
        raise MaterializationRefused(
            CompilationRefusalCode.GRAIN_NOT_RESOLVED,
            f"selection {selection_revision_id} names a candidate with no declared grain, so what "
            f"this feature is computed PER is unknown. Candidates frozen before the typed "
            f"computation was carried through carry no grain at all — regenerate the candidate set "
            f"and select again")

    return AuthoringIntent(
        name=idea.name,
        hypothesis=row[1],
        target_entity=idea.grain_table or "",
        # ORDERED, and not sorted: the order of grain keys decides the published column order, so
        # re-sorting here would silently reorder the output of a multi-key feature.
        target_grain_keys=tuple(
            logical_ref_of(conn, str(source), str(ref)) for source, ref in idea.grain_refs),
    )


def _restore_result(conn: DbConn, run_id: str, draft_id: str, *, intent: AuthoringIntent):
    """The run's own ``AuthoringResultV2``, rebuilt from its write-once trace. No provider is called.

    ``load_verified_checkpoint`` walks the events, re-derives every payload hash, enforces the stage
    ordering and reconciles each provider dispatch before handing anything back. A run failing any
    of that is refused as incomplete, because a trace that cannot be replayed cannot say what the
    run decided.

    ``versions`` is read BACK from the manifest rather than reconstructed — V1's restorer explains
    why at length and the reasoning is unchanged: reconstructing it would make every
    previously-authored feature unresolvable the moment any policy version was bumped, for a check
    that proves nothing about the artifact.

    The INTENT half is DERIVED, not read back, and that asymmetry is the point. Reading the
    manifest's own intent hash and handing it straight back would compare a value to itself — a
    guard that always passes. Deriving it from the frozen considered revision makes the checkpoint
    answer a real question: was this formula authored for the candidate this selection names? If a
    revision was superseded or an option resolves differently, that disagreement surfaces here
    rather than as generated code for the wrong feature.
    """
    from featuregen.formula.replay_authoring_v2 import _intent_material
    from featuregen.overlay.field_evidence import canonical_hash

    intent_hash = canonical_hash(_intent_material(intent))
    stored = conn.execute(_SELECT_MANIFEST_VERSIONS, (run_id,)).fetchone()
    if stored is None:
        raise MaterializationRefused(
            CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE,
            f"formula draft {draft_id} names authoring run {run_id}, which has no manifest")

    try:
        checkpoint = _load_checkpoint(
            conn, run_id, intent_hash=intent_hash, versions=stored[0])
        if checkpoint.terminal_result is None:
            raise MaterializationRefused(
                CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE,
                f"formula draft {draft_id} names authoring run {run_id}, which has no terminal "
                f"trace event")
        return _restore_v2(checkpoint, run_id)
    except FormulaControlFlow as exc:
        raise MaterializationRefused(
            CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE,
            f"the trace of authoring run {run_id} (formula draft {draft_id}) cannot be replayed, "
            f"so it cannot say what the run decided: {exc}") from exc
