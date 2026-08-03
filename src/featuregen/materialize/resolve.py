"""Phase G §3.1 — the RESOLUTION SEAM: a durable identity becomes admission's input.

THE HOLE THIS CLOSES. ``admit_artifacts`` takes ``ResolvedFeatureInput(intent, result)``, and until
this module existed that type had **zero constructors in ``src/``**: only tests built one.
``AuthoringResult`` is by design "pure in-memory: no DB, no execution, no durable artifact"
(``formula/result.py:22``), so nothing a user or a queue job could NAME — a feature, a group —
reached the gate. Every stage downstream of admission was therefore unreachable from any durable
state, which is exactly what "the chain is unwired" meant.

WHAT IS AND IS NOT PROVEN HERE. This module RECONSTRUCTS; it does not vouch. Both halves of a
``ResolvedFeatureInput`` are rebuilt from durable state, and neither is trusted on this side of the
seam:

* the **result** is restored by the replay lane's own restorer
  (``replay_authoring._restore_terminal_result`` over ``replay_trace.load_verified_checkpoint``) —
  the only code in the tree that can turn a 1022 trace back into an ``AuthoringResult``. It is a
  PURE function of the checkpoint: no provider is called, nothing is re-authored, and a run whose
  terminal event does not exist simply has no result to restore.
* the **intent** is rebuilt from the work item the authoring run was opened for
  (:func:`_read_intent`), by the same projection ``recipe_formula_worker.py:343-349`` used to build
  it in the first place.

``admission.py:115-117`` forbids handing the gate "a look-alike rebuilt from its parts" — and a
reconstruction is exactly that until something proves otherwise. What makes it THE intent is the
HASH: admission's check 6 re-derives ``authoring_intent_hash`` from whatever this module produced
and compares it against the write-once manifest, which was stamped before any provider call. A
reconstruction that differs in any of the five hashed fields is refused there. So the proof is not
weakened by reconstructing — it is *relocated* to the one place that reads immutable evidence.

This module's own manifest check (:func:`_verify_manifest_intent`) is therefore NOT that proof and
must never be mistaken for it. It exists for ATTRIBUTION: admission refuses a batch, but an
operator needs to know WHICH member of a group was wrong, and admission has no work-item id to
name. Deleting it would lose the diagnosis, not the guarantee.

ALL-OR-NOTHING, mirroring ``admit_artifacts`` (``admission.py:143-147``). The trigger is per-group
because the code downstream demands it: ``authorize_compilation`` raises on an empty group and
authorizes a group-wide read set (``ir.py:565``), and ``build_group_plan`` requires exactly the
group's members. A caller that resolved the survivors of a failed group would be compiling a
membership nobody decided, so the first unresolvable member refuses the whole set.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.formula.control import FormulaControlFlow
from featuregen.formula.result import AuthoringResult
from featuregen.formula.turns import AuthoringIntent
from featuregen.materialize.admission import ResolvedFeatureInput
from featuregen.materialize.authoring_trace import authoring_intent_hash, read_run_intent_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

# Bound PRIVATELY: the replay lane's restorer is `formula/`'s implementation, not an API Phase G
# owns or may re-export. `_restore_terminal_result` is the ONLY function in the tree that rebuilds
# an `AuthoringResult` from a durable trace; re-implementing it here would be a second, drifting
# copy of the §F artifact-coherence rules that `derive_disposition` enforces.
from featuregen.formula.replay_authoring import (  # isort: skip
    _restore_terminal_result as _restore,
)
from featuregen.formula.replay_trace import load_verified_checkpoint as _load_checkpoint

__all__ = ["ResolvedFeature", "resolve_feature_inputs"]

#: The columns of ONE ``recipe_formula_shadow_work_item`` that carry an authoring intent. Read-only
#: use of a store `overlay/upload/` owns: this module never writes it.
_SELECT_WORK_ITEM = (
    "SELECT recipe_id, provider_input_json FROM recipe_formula_shadow_work_item "
    "WHERE work_item_id = %s"
)
_SELECT_MANIFEST_VERSIONS = (
    "SELECT versions FROM formula_authoring_run WHERE authoring_run_id = %s"
)

#: The ONE disposition that carries an admissible artifact. Checked here so a group refuses with the
#: MEMBER named; admission checks it again against the trace, which is where it is proven.
_RESOLVED = "RESOLVED"


@dataclass(frozen=True, slots=True)
class ResolvedFeature:
    """One feature resolved from durable state, WITH the provenance that identifies it.

    ``input`` is the object ``admit_artifacts`` consumes. The other three fields are what a later
    stage needs in order to re-derive the same proof rather than trust that someone upstream
    performed it — ``work_item_id`` is the durable name the request used, ``authoring_run_id`` the
    run whose verdict is being claimed, and ``intent_hash`` the manifest digest the reconstruction
    matched. ``authoring_run_id`` is deliberately duplicated from ``input.result`` so a caller
    reading provenance never has to reach through the forgeable object to get it."""

    work_item_id: str
    authoring_run_id: str
    intent_hash: str
    input: ResolvedFeatureInput


def resolve_feature_inputs(
    conn: DbConn, *, work_item_ids: Sequence[str]
) -> tuple[ResolvedFeature, ...]:
    """Resolve a GROUP of durable feature identities into admission's inputs, or refuse all of them.

    ``work_item_ids`` names ``recipe_formula_shadow_work_item`` rows — the durable anchor from which
    BOTH the authoring run id and the authoring intent are recoverable (:func:`_authoring_run_id`,
    :func:`_read_intent`). It is the only such anchor that exists: the authoring manifest stores the
    intent's HASH, never its material, so the material has to come from wherever the request was
    assembled.

    Returned in ascending ``work_item_id`` order regardless of how the caller listed them, so two
    callers naming the same group reach the same plan — the group contract and the physical column
    order downstream are derived from this sequence, and an order that depended on call site would
    make them caller-dependent too.

    Raises:
        MaterializationRefused: a governed verdict about a member — the run has no terminal event
            (``AUTHORING_RUN_INCOMPLETE``), its trace cannot be replayed
            (``AUTHORING_RUN_INCOMPLETE``), it did not resolve (``NOT_RESOLVED``), or the durable
            intent is not the one the run was opened for (``INTENT_HASH_MISMATCH``). The detail
            names the member, which admission cannot do.
        ValueError: the REQUEST is malformed — empty, or naming one member twice. Not a §14 code:
            the closed vocabulary has no member for "the caller assembled the batch wrongly", and
            inventing one would type a caller defect as a verdict about an artifact.
    """
    ordered = _require_a_well_formed_group(work_item_ids)
    return tuple(_resolve_one(conn, work_item_id) for work_item_id in ordered)


def _require_a_well_formed_group(work_item_ids: Sequence[str]) -> tuple[str, ...]:
    """The group as a deterministically ordered tuple, or ``ValueError``.

    An empty group is rejected HERE rather than passed on: ``authorize_compilation`` raises on one
    anyway (``ir.py:565``), and failing at the seam names the actual problem instead of surfacing it
    three stages later. A duplicate is rejected rather than de-duplicated because the two are
    indistinguishable to a caller who made a mistake, and admission would report the same defect
    much less clearly — as a Hive column collision between a feature and itself."""
    if not work_item_ids:
        raise ValueError(
            "a materialization group needs at least one work item: compilation is authorized "
            "group-wide and there is nothing to authorize for an empty group"
        )
    seen: set[str] = set()
    for work_item_id in work_item_ids:
        if work_item_id in seen:
            raise ValueError(
                f"work item {work_item_id!r} appears twice in the group; a duplicate member would "
                "resolve to two features occupying one Hive column"
            )
        seen.add(work_item_id)
    return tuple(sorted(work_item_ids))


def _resolve_one(conn: DbConn, work_item_id: str) -> ResolvedFeature:
    """Reconstruct ONE feature's admission input from its durable identity."""
    run_id = _authoring_run_id(work_item_id)
    intent = _read_intent(conn, work_item_id)
    intent_hash = _verify_manifest_intent(conn, run_id, intent, work_item_id)
    result = _restore_result(conn, run_id, intent_hash, work_item_id)
    _require_resolved(result, run_id, work_item_id)
    return ResolvedFeature(
        work_item_id=work_item_id,
        authoring_run_id=run_id,
        intent_hash=intent_hash,
        input=ResolvedFeatureInput(intent=intent, result=result),
    )


def _authoring_run_id(work_item_id: str) -> str:
    """The authoring run a work item names.

    DERIVED, not stored: the worker mints it as ``"far_" + sha256(work_item_id)[:24]``
    (``recipe_formula_worker.py:338-339``) precisely so a retry of the same work item resumes the
    same run instead of opening a second one. Re-deriving it here is reading that decision, not
    re-making it — and it is why a work item that was never worked resolves to a run id that names
    nothing, which is refused as incomplete."""
    return "far_" + hashlib.sha256(work_item_id.encode()).hexdigest()[:24]


def _read_intent(conn: DbConn, work_item_id: str) -> AuthoringIntent:
    """The authoring intent a work item was worked under.

    THE ONE PLACE that knows where an intent's material is durably kept. The projection is
    ``recipe_formula_worker.py:343-349`` verbatim — the same five fields, from the same immutable
    columns — because an intent assembled even slightly differently is a different intent to the
    manifest hash, and would be refused by admission's check 6 for a reason nobody could diagnose.

    A missing row is NOT an error here: it means the durable identity names nothing, which is the
    same state as a run that never happened, so it is left to :func:`_verify_manifest_intent` and
    the restore to refuse as ``AUTHORING_RUN_INCOMPLETE`` rather than raising a different shape of
    failure for what is the same fact."""
    row = conn.execute(_SELECT_WORK_ITEM, (work_item_id,)).fetchone()
    if row is None:
        return AuthoringIntent(name="", hypothesis="", target_entity="")
    recipe_id, provider_input = row
    material = provider_input if isinstance(provider_input, Mapping) else {}
    expectation = material.get("formula_expectation")
    grain_keys = (
        expectation.get("grain_key_refs", ()) if isinstance(expectation, Mapping) else ())
    return AuthoringIntent(
        name=str(recipe_id),
        hypothesis=str(material.get("hypothesis", "")),
        target_entity=str(material.get("target_entity", "")),
        target_grain_keys=tuple(grain_keys),
        recipe_authoring_context=dict(material) if material else None,
    )


def _verify_manifest_intent(
    conn: DbConn, run_id: str, intent: AuthoringIntent, work_item_id: str
) -> str:
    """Return the intent hash, having checked it against the run's manifest — FOR ATTRIBUTION.

    NOT the governed proof. Admission's check 6 re-derives this same digest from the same intent and
    compares it against the same write-once manifest, and THAT is what makes the reconstruction
    THE intent; this module could be removed entirely without weakening the gate. What would be lost
    is the diagnosis: admission refuses a whole batch and has no work-item id to name, so an
    operator staring at ``INTENT_HASH_MISMATCH`` for a group of twelve would have nothing to go on.

    An absent manifest fails CLOSED as ``AUTHORING_RUN_INCOMPLETE`` rather than as a mismatch: a run
    nothing recorded has no intent to disagree with, and reporting it as a mismatch would send an
    operator hunting a drifted projection instead of a run that never happened."""
    recorded = read_run_intent_hash(conn, run_id)
    supplied = authoring_intent_hash(intent)
    if recorded is None:
        raise MaterializationRefused(
            CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE,
            f"work item {work_item_id} names authoring run {run_id}, which has no manifest",
        )
    if recorded != supplied:
        raise MaterializationRefused(
            CompilationRefusalCode.INTENT_HASH_MISMATCH,
            f"the intent rebuilt for work item {work_item_id} hashes to {supplied}, but authoring "
            f"run {run_id}'s manifest records intent_hash={recorded!r}",
        )
    return supplied


def _restore_result(
    conn: DbConn, run_id: str, intent_hash: str, work_item_id: str
) -> AuthoringResult:
    """The run's own ``AuthoringResult``, rebuilt from its write-once trace. No provider is called.

    ``load_verified_checkpoint`` walks the run's events, re-derives each payload hash, enforces the
    stage ordering and reconciles every provider dispatch before it hands anything back; a run that
    fails any of that raises ``RecoveryRequiresReconciliation`` and is refused here as incomplete,
    because a trace that cannot be replayed is a trace that cannot say what the run decided.

    ``versions`` is READ BACK from the manifest rather than reconstructed, and that is deliberate.
    The checkpoint compares ``(intent_hash, versions)`` against the manifest as a REPLAY-SAFETY
    guard — "do not resume this run under different policy" — which is the orchestrator's concern,
    not admission's. Reconstructing it would make every previously-authored feature unresolvable the
    moment any policy version was bumped, for a check that proves nothing about the artifact. The
    INTENT half is not read back: it is supplied from the work item, so the checkpoint's identity
    guard remains a real comparison on the field that matters.

    ``FormulaControlFlow`` covers the replay lane's whole control-flow family
    (``RecoveryRequiresReconciliation``, ``LeaseFenceLost``): none of them can be a bare exception
    escaping a governed boundary (§14), and all of them mean the same thing to a caller that only
    wants to know whether a verdict is readable."""
    versions_row = conn.execute(_SELECT_MANIFEST_VERSIONS, (run_id,)).fetchone()
    versions = versions_row[0] if versions_row is not None else {}
    try:
        checkpoint = _load_checkpoint(
            conn, run_id, intent_hash=intent_hash, versions=versions)
        if checkpoint.terminal_result is None:
            raise MaterializationRefused(
                CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE,
                f"work item {work_item_id} names authoring run {run_id}, which has no terminal "
                "trace event",
            )
        return _restore(checkpoint, run_id)
    except FormulaControlFlow as exc:
        raise MaterializationRefused(
            CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE,
            f"the trace of authoring run {run_id} (work item {work_item_id}) cannot be replayed, "
            f"so it cannot say what the run decided: {exc}",
        ) from exc


def _require_resolved(result: AuthoringResult, run_id: str, work_item_id: str) -> None:
    """Refuse a member whose run did not resolve, naming the MEMBER (``NOT_RESOLVED``).

    Admission checks this too, against the trace, and that is where it is proven — the restored
    object is as forgeable as any other ``AuthoringResult``, so this is not evidence. It is the same
    attribution argument as :func:`_verify_manifest_intent`: without it a group of twelve refuses
    with no indication of which member is awaiting a human verdict."""
    if result.authoring_disposition != _RESOLVED:
        raise MaterializationRefused(
            CompilationRefusalCode.NOT_RESOLVED,
            f"work item {work_item_id} names authoring run {run_id}, which closed "
            f"{result.authoring_disposition}, not {_RESOLVED}",
        )
