"""S13 — admission for the Formula-V2 LANGUAGE: the door a free-form V2 run had no way through.

**This is the gate whose absence stopped the whole thing.** ``admit_artifacts``'s check 4b refuses
any formula whose schema version is not 1, and says why in its own docstring: *"The v2 path arrives
with an engine that ADVERTISES it."* Until S13 that engine advertisement did not exist, so the
refusal was correct and there was nowhere for a v2 run to go. S13's second clause built the
advertised set (``renderer-dispatchable ∩ execution-proved``); this module is the door it unlocks,
and the advertisement is a CHECK here rather than a comment.

**The V1 checks are not copied, they are REUSED.** Checks 1, 2 and 3 — the terminal event exists,
its payload authenticates against its recorded digest, and its disposition is ``RESOLVED`` — read
the shared ``formula_authoring_trace_event`` row and carry no grammar at all. Re-implementing them
for v2 would be a second reading of one record, and the two would eventually disagree about what a
tampered payload looks like. They are imported.

**What genuinely differs is four things**, and each is a fact about the v2 language rather than a
preference:

* the artifact is a PROPOSAL, not a ``TypedFormulaV1``, and its hash dispatches on wire version —
  hashing a v3 proposal under v2's canonicalizer would mint an identity from a projection that never
  saw ``row_selections``;
* the version gate is the mirror image: 2 or 3 admitted, 1 REFUSED, because a v1 formula reaching
  the v2 chain would be read under operations v1 never defined;
* the axes are SEVEN, not six — v2 adds ``review``, which records HOW review was obtained, and a
  bypass-authored run carries ``critic_status=None`` that the six-axis check would read as missing;
* the operators the proposal implies must be in the engine's ADVERTISED set.

**``review`` is checked as a PRESENCE, not a value.** C-A5's whole point is that "the critic ran and
found nothing" and "no critic ran" are different facts, so a terminal payload that recorded neither
cannot be admitted: it would be a run whose review provenance nobody can recover, and the deterministic
recipe path — the bypass's entire use case — is exactly the one that produces ``critic_status=None``.

**No plan envelope check.** B3's envelope is the recipe path's frozen plan, and a FREE-FORM run has
none by construction. Requiring one would refuse every free-form artifact; inventing an empty one
would claim a governed plan that nobody wrote. Its absence here is the difference between the two
paths, stated rather than defaulted.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from featuregen.contracts.db import DbConn
from featuregen.formula.result_v2 import AuthoringResultV2
from featuregen.formula.schema_v2 import TypedFormulaProposalV2
from featuregen.formula.schema_v3 import TypedFormulaProposalV3
from featuregen.formula.turns import AuthoringIntent
from featuregen.materialize import authoring_trace as _trace
from featuregen.materialize.admission import (
    _payload_field,
    _require_resolved,
    _terminal_event,
    _verify_payload_hash,
    hive_identifier,
)
from featuregen.materialize.authoring_trace import authoring_intent_hash as _authoring_intent_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.execution_proof_store import advertised_operators
from featuregen.materialize.operator_graph_v2 import OperatorKindV2

__all__ = [
    "AdmittedFeatureV2",
    "ResolvedFeatureInputV2",
    "admit_artifacts_v2",
    "implied_operator_signatures",
]

#: The wire versions of the Formula-V2 LANGUAGE. Two members, not a floor: a version 4 nobody has
#: defined must be refused rather than admitted by an inequality that happened to be open-ended.
_V2_LANGUAGE_VERSIONS = frozenset({2, 3})

#: The SEVEN axes a v2 terminal payload records. ``review`` is v2's addition and the reason this
#: list is not `admission._AXIS_FIELDS`.
_AXIS_FIELDS_V2: tuple[str, ...] = (
    "structural_status",
    "capability_status",
    "output_status",
    "expectation_status",
    "critic_status",
    "review",
    "technical_status",
)


class ResolvedFeatureInputV2:
    """The v2 chain's input: the intent a run was opened for, and the result it folded to.

    A plain class rather than a dataclass with a ``plan_envelope`` field, because a free-form run has
    no frozen plan and a field for one would be filled in with ``None`` everywhere until somebody
    read that as "checked, and fine".
    """

    __slots__ = ("intent", "result")

    def __init__(self, intent: AuthoringIntent, result: AuthoringResultV2) -> None:
        self.intent = intent
        self.result = result


class AdmittedFeatureV2:
    """One free-form v2 artifact whose provenance has been PROVEN against the immutable trace.

    Carries the verified proposal hash alongside the proposal for V1's reason: any later stage can
    re-derive the same proof instead of trusting that someone upstream performed it.
    """

    __slots__ = ("feature_name", "proposal", "proposal_content_hash", "intent",
                 "authoring_run_id", "operator_kinds")

    def __init__(self, *, feature_name: str,
                 proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
                 proposal_content_hash: str, intent: AuthoringIntent, authoring_run_id: str,
                 operator_kinds: tuple[str, ...]) -> None:
        self.feature_name = feature_name
        self.proposal = proposal
        self.proposal_content_hash = proposal_content_hash
        self.intent = intent
        self.authoring_run_id = authoring_run_id
        #: What the renderer would be asked to emit. Carried because it is what the ADVERTISED-set
        #: check was made against, so a later stage does not re-derive it from a different reading.
        self.operator_kinds = operator_kinds


def implied_operator_signatures(
    proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
) -> tuple[tuple[str, str], ...]:
    """The operator SIGNATURES this proposal's execution implies, sorted.

    A signature is ``(kind, variant)``. Kinds alone cannot express the truth this check exists to
    enforce: `sum` is renderable and `avg` is not, and both are ``AGGREGATE``. Checking kinds would
    admit a median against an engine that can only add up.

    DERIVED from what the proposal declares, never asserted beside it — the same rule C-C10 applies
    to subgraph requirements: an operator list a caller supplied is one that gets forgotten on the
    feature it mattered for.

    Deliberately NARROW: it names the operators whose presence follows from the proposal's own text
    (a governed scan and an aggregate always; an as-of FX join and its two gates when a currency
    conversion is declared; a semantic selection when one is declared). It does NOT attempt the full
    graph — that is the compiler's, over bound inputs — because a second, laxer topology here would
    be checked against the advertised set instead of the real one.
    """
    from featuregen.formula.schema_v2 import body_expressions_v2
    from featuregen.materialize.execution_proof_store import SOLE_VARIANT

    signatures: set[tuple[str, str]] = {
        (OperatorKindV2.GOVERNED_SCAN.value, SOLE_VARIANT),
        (OperatorKindV2.GROUP_ASSEMBLY.value, SOLE_VARIANT),
    }
    # The FINAL COMBINATION is a variant too, and naming it here is what stops a `signed_sum` being
    # admitted against a renderer that can only divide. The graph has no node for it yet (step 7),
    # but the capability question is answerable now and the answer is load-bearing now.
    signatures.add(("final_combine", str(getattr(proposal.body, "final_operation", "identity"))))

    for expression in body_expressions_v2(proposal.body):
        # THE AGGREGATE'S OWN FUNCTION, not the bare kind. This is the whole point of the typed
        # signature: `sum` and `avg` are both AGGREGATE and only one of them renders.
        aggregation = getattr(expression, "aggregation", None)
        signatures.add((OperatorKindV2.AGGREGATE.value,
                        str(getattr(aggregation, "value", aggregation or "unknown"))))

        refs = getattr(expression, "authority_refs", None)
        if refs is not None and getattr(refs, "currency_conversion_ref", "").strip():
            signatures |= {
                (OperatorKindV2.AS_OF_FX_JOIN.value, SOLE_VARIANT),
                (OperatorKindV2.DUPLICATE_RATE_GATE.value, SOLE_VARIANT),
                (OperatorKindV2.MISSING_RATE_GATE.value, SOLE_VARIANT),
                (OperatorKindV2.DECIMAL_MULTIPLICATION.value, SOLE_VARIANT),
            }
        for selection in getattr(expression, "row_selections", ()) or ():
            # Each selection names WHICH semantic rule, so each is its own capability question.
            signatures.add((OperatorKindV2.SEMANTIC_SELECTION.value,
                            str(getattr(selection, "kind", None)
                                or getattr(selection, "selection_kind", SOLE_VARIANT))))
    return tuple(sorted(signatures))


def admit_artifacts_v2(
    conn: DbConn, inputs: Iterable[ResolvedFeatureInputV2], *, engine_id: str,
) -> tuple[AdmittedFeatureV2, ...]:
    """Admit every v2 input, in order, or refuse the WHOLE batch.

    No partial admission, for V1's reason: a caller that admitted the survivors of a refused batch
    would be compiling a group whose membership nobody decided.

    Args:
        engine_id: whose advertised set the operators are checked against. Required and never
            defaulted — an engine this build has never heard of must be unsupported, and a default
            would quietly pick one.

    Raises:
        MaterializationRefused: one of the checks failed, carrying the matching
            ``CompilationRefusalCode``.
    """
    if not engine_id.strip():
        raise ValueError(
            "admit_artifacts_v2 requires an engine_id: the advertised set is per engine, and "
            "admitting against an unnamed one would check the operators against nothing")
    return tuple(_admit_one_v2(conn, item, engine_id=engine_id) for item in inputs)


def _admit_one_v2(
    conn: DbConn, item: ResolvedFeatureInputV2, *, engine_id: str,
) -> AdmittedFeatureV2:
    result = item.result
    run_id = result.authoring_run_id

    event = _terminal_event(conn, run_id)                        # 1 — shared with v1
    _verify_payload_hash(event, run_id)                          # 2 — shared with v1
    _require_resolved(event, run_id)                             # 3 — shared with v1
    proposal, content_hash = _verify_proposal_hash(event, result, run_id)   # 4
    _verify_language_version(proposal, run_id)                   # 4b
    _verify_axes_v2(event, result, run_id)                       # 5
    _verify_intent_hash_v2(conn, item.intent, run_id)            # 6
    kinds = _verify_advertised(conn, proposal, run_id, engine_id=engine_id)  # 7 (S13)

    return AdmittedFeatureV2(
        feature_name=hive_identifier(item.intent.name),
        proposal=proposal,
        proposal_content_hash=content_hash,
        intent=item.intent,
        authoring_run_id=run_id,
        operator_kinds=kinds,
    )


# ── 4. the recorded candidate hash equals the supplied proposal's ────────────────────────────────
def _verify_proposal_hash(
    event: _trace.TerminalEvent, result: AuthoringResultV2, run_id: str,
) -> tuple[TypedFormulaProposalV2 | TypedFormulaProposalV3, str]:
    """THE forgery check, in v2's terms.

    The digest is RECOMPUTED from ``result.candidate_proposal`` through the result type's own
    version-dispatching hasher; ``result.candidate_proposal_hash`` is never trusted, because a
    forger sets it to whatever makes the pair look consistent. The comparison is against the
    PAYLOAD's recorded hash, which migration 1022 makes physically immutable.
    """
    proposal = result.candidate_proposal
    if proposal is None:
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_HASH_MISMATCH,
            f"the result supplied for authoring run {run_id} carries no candidate proposal, so "
            f"there is nothing whose hash could match the terminal event's",
        )

    from featuregen.formula.result_v2 import _content_hash

    recomputed = _content_hash(proposal)
    recorded = _payload_field(event, "candidate_proposal_hash")
    if recorded != recomputed:
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_HASH_MISMATCH,
            f"the proposal supplied for authoring run {run_id} hashes to {recomputed}, but its "
            f"terminal event records candidate_proposal_hash={recorded!r}",
        )
    return proposal, recomputed


# ── 4b. the LANGUAGE is Formula-V2 ───────────────────────────────────────────────────────────────
def _verify_language_version(
    proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3, run_id: str,
) -> None:
    """The mirror image of v1's check 4b, and a MEMBERSHIP test rather than a floor.

    A version 4 nobody has defined must be refused rather than admitted by an inequality that
    happened to be open-ended, and a v1 formula arriving here would be read under operations v1
    never defined — which is exactly the failure v1's own check exists to prevent, in the other
    direction.
    """
    version = proposal.formula_schema_version
    if version not in _V2_LANGUAGE_VERSIONS:
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"authoring run {run_id}: formula schema version {version} is not the Formula-V2 "
            f"language ({sorted(_V2_LANGUAGE_VERSIONS)}). A v1 formula reaching this chain would "
            f"be read under operations v1 never defined, and a version nobody has defined would "
            f"be read under a grammar that does not exist",
        )


# ── 5. the SEVEN axes match the trace ────────────────────────────────────────────────────────────
def _verify_axes_v2(
    event: _trace.TerminalEvent, result: AuthoringResultV2, run_id: str,
) -> None:
    """Every axis on the supplied result equals the one the trace recorded.

    ``review`` is checked as a PRESENCE rather than by value: C-A5 records HOW review was obtained,
    and a payload that recorded neither a critic status nor a review is a run whose review
    provenance nobody can recover. The deterministic recipe path is precisely the one that folds
    ``critic_status=None``, so a six-axis check would refuse the bypass's whole use case while
    passing a run that lost the axis silently.
    """
    for field in _AXIS_FIELDS_V2:
        if field == "review":
            continue
        recorded = _payload_field(event, field)
        supplied = getattr(result, field, None)
        if recorded != supplied:
            raise MaterializationRefused(
                CompilationRefusalCode.AXES_MISMATCH,
                f"the result supplied for authoring run {run_id} records {field}={supplied!r}, "
                f"but its terminal event records {recorded!r}",
            )

    if _payload_field(event, "critic_status") is None and _payload_field(event, "review") is None:
        raise MaterializationRefused(
            CompilationRefusalCode.AXES_MISMATCH,
            f"the terminal event of authoring run {run_id} records neither a critic_status nor a "
            f"review: 'the critic ran and found nothing' and 'no critic ran' are different facts, "
            f"and a run recording neither has review provenance nobody can recover",
        )


# ── 6. the intent is the one the run was opened for ──────────────────────────────────────────────
def _verify_intent_hash_v2(conn: DbConn, intent: AuthoringIntent, run_id: str) -> None:
    """``INTENT_HASH_MISMATCH`` unless the intent re-hashes to the manifest's ``intent_hash``.

    **The hasher is V1's, and that is not an oversight — it is the only correct choice.** The INTENT
    carries no grammar, so there is no "v2 projection" of it; what decides the digest is the recipe
    the 1022 MANIFEST was stamped with, and the replay lane stamps all FIVE fields of
    ``AuthoringIntent`` including ``recipe_authoring_context``.
    ``authoring_v2.authoring_intent_hash_v2`` covers four, which is right for the 1020 lane it
    belongs to and wrong here: using it would refuse every genuine replay-lane run, because the
    fifth field is in the recorded hash and not in the recomputed one.

    Found by driving a real run rather than by reading: the first version of this check used the v2
    hasher and refused five otherwise-perfect end-to-end tests with ``INTENT_HASH_MISMATCH``.
    """
    recorded = _trace.read_run_intent_hash(conn, run_id)
    supplied = _authoring_intent_hash(intent)
    if recorded != supplied:
        raise MaterializationRefused(
            CompilationRefusalCode.INTENT_HASH_MISMATCH,
            f"the intent supplied for authoring run {run_id} hashes to {supplied}, but the run's "
            f"manifest records intent_hash={recorded!r}",
        )


# ── 7. every implied operator is ADVERTISED (S13) ────────────────────────────────────────────────
def _verify_advertised(
    conn: DbConn, proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3, run_id: str, *,
    engine_id: str,
) -> tuple[str, ...]:
    """The check v1's docstring promised: the v2 path arrives with an engine that ADVERTISES it.

    Advertised means ``renderer-dispatchable ∩ execution-proved`` — a renderer branch with no proof
    is a branch nobody ran against reviewed gold, and a proof for an operator this build cannot emit
    is a proof about a different build. Admitting on either half alone would let a feature into the
    chain on half the evidence.
    """
    implied = implied_operator_signatures(proposal)
    advertised = set(advertised_operators(conn, engine_id=engine_id))
    missing = sorted(set(implied) - advertised)
    if missing:
        named = ", ".join(f"{kind}:{variant}" for kind, variant in missing)
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"authoring run {run_id} implies operators {named}, which {engine_id!r} does not "
            f"advertise for this build. Advertised means renderer-dispatchable AND execution-proved: "
            f"a renderer branch with no gold proof is a branch nobody ran, and a proof for an "
            f"operator this build cannot emit is a proof about a different build",
        )
    return implied


def admitted_payload(admitted: AdmittedFeatureV2) -> Mapping[str, object]:
    """A plain view of an admitted artifact, for a caller that logs or reports one."""
    return {
        "feature_name": admitted.feature_name,
        "proposal_content_hash": admitted.proposal_content_hash,
        "authoring_run_id": admitted.authoring_run_id,
        "operator_kinds": list(admitted.operator_kinds),
    }
