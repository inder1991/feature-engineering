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
    _terminal_event,
    _verify_payload_hash,
)
from featuregen.materialize.authoring_trace import authoring_intent_hash as _authoring_intent_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.execution_proof_store import renderer_supported_operators
from featuregen.materialize.identifiers import hive_identifier
from featuregen.materialize.operator_graph_v2 import OperatorKindV2

__all__ = [
    "AdmittedFeatureV2",
    "ResolvedFeatureInputV2",
    "admit_artifacts_v2",
    "implied_operator_signatures",
]

#: The one disposition that needs no explanation. Spelled here rather than imported from V1's
#: `admission` module because what counts as ADMISSIBLE now differs between the two languages,
#: and a shared constant beside two different rules reads as a shared rule.
_RESOLVED = "RESOLVED"
#: V3's terminal state when the only outstanding step is the compiler's output binding.
_READY_FOR_OUTPUT_BINDING = "READY_FOR_OUTPUT_BINDING"
#: The output axis that means "not asked yet", as opposed to `needs_authority`'s
#: "asked, and the governed read failed". Admission distinguishes them; nothing else may
#: collapse them back.
_DEFERRED_OUTPUT = "deferred_to_compiler"

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
                 "authoring_run_id", "operator_kinds", "candidate_output", "output_intent")

    def __init__(self, *, feature_name: str,
                 proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
                 proposal_content_hash: str, intent: AuthoringIntent, authoring_run_id: str,
                 operator_kinds: tuple[str, ...],
                 candidate_output=None, output_intent=None) -> None:
        self.feature_name = feature_name
        self.proposal = proposal
        self.proposal_content_hash = proposal_content_hash
        self.intent = intent
        self.authoring_run_id = authoring_run_id
        #: What the renderer would be asked to emit. Carried because it is what the ADVERTISED-set
        #: check was made against, so a later stage does not re-derive it from a different reading.
        self.operator_kinds = operator_kinds
        #: The GOVERNED output policy the authoring run resolved, and the intent the author
        #: DECLARED. Both were being dropped here, and dropping them is not a tidy: the two exist to
        #: be reconciled against each other by `resolve_executable_output_v2`, and a stage that has
        #: neither cannot do that. It can only re-resolve a policy from whatever facts its caller
        #: happens to hand it — which answers a different question and answers it confidently.
        #:
        #: `None` is legitimate and means the run resolved no output (an `output_status` other than
        #: `resolved`). It is NOT a default standing in for "we did not carry it": admission refuses
        #: a RESOLVED run whose axes disagree, so a resolved run reaches here with its output.
        self.candidate_output = candidate_output
        self.output_intent = output_intent


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
    # ▲ AUTHENTICATE THE PROPOSAL BEFORE JUDGING THE DISPOSITION. The disposition check below
    # grants an exception that is V3's ALONE, so it must be able to ask what language this is — and
    # the only trustworthy answer comes from a proposal whose hash has been proven against the
    # immutable trace. Asking earlier would decide a governed question from an unauthenticated
    # artifact.
    proposal, content_hash = _verify_proposal_hash(event, result, run_id)   # 3
    _verify_language_version(proposal, run_id)                   # 4
    _verify_manifest_declares_the_language(conn, proposal, run_id)          # 4b
    _verify_axes_v2(event, result, run_id)                       # 5
    _require_admissible_disposition(event, result, proposal, run_id)        # 6
    _verify_intent_hash_v2(conn, item.intent, run_id)            # 7
    kinds = _verify_advertised(conn, proposal, run_id, engine_id=engine_id)  # 8 (S13)

    return AdmittedFeatureV2(
        feature_name=hive_identifier(item.intent.name),
        proposal=proposal,
        proposal_content_hash=content_hash,
        intent=item.intent,
        authoring_run_id=run_id,
        operator_kinds=kinds,
        # Carried from the VERIFIED result rather than re-derived. `result` has already survived
        # checks 2, 4, 4b and 5 — the payload hash, the proposal hash, the language version and the
        # axes — so these two fields are as proven as the proposal beside them.
        candidate_output=result.candidate_output,
        output_intent=result.output_intent,
    )


# ── 6. the disposition ADMITS, which for V3 is not the same as RESOLVED ──────────────────────────
def _require_admissible_disposition(
    event: _trace.TerminalEvent,
    result: AuthoringResultV2,
    proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
    run_id: str,
) -> None:
    """``NOT_RESOLVED`` unless the run's verdict permits compilation.

    ▲ **WHY THIS IS NOT V1's `_require_resolved`.** V1 and V2 resolve OUTPUT AUTHORITY during
    authoring, so `RESOLVED` there means every question was answered. **V3 deliberately does not.**
    A V3 run validates the formula, captures the author's intent and records review evidence;
    resolving governed output type, unit, currency and additivity is the COMPILER's (C-A7), and
    emitting `OUTPUT_POLICY_RESOLVED` at authoring time would claim governed metadata had been
    consulted when nothing had consulted it. Such a run terminates `READY_FOR_OUTPUT_BINDING`.

    **THE EXCEPTION IS V3's ALONE, and that is checked rather than assumed.** A V2 run reaches an
    unresolved output too — `replay_authoring_v2` folds `needs_authority` when its governed-facts
    read fails CLOSED — and that is a genuine failure a human must look at, not a deferral. An
    earlier version of this function took only the terminal event, could not ask what language it
    was holding, and therefore admitted exactly that: a V2 run whose authority lookup had failed.
    The two are now different values on the axis AND different dispositions, and this function
    additionally requires the authenticated proposal to be V3 before granting anything.

    Every condition, and none of them redundant:

    1. the proposal is `TypedFormulaProposalV3` — the language whose contract defers;
    2. the disposition is `READY_FOR_OUTPUT_BINDING`;
    3. the output axis is exactly `deferred_to_compiler` — never `needs_authority`,
       `external_requirement` or `invalid_output`, none of which become true later on their own;
    4. an `output_intent` exists — the thing the compiler reconciles against;
    5. that intent was derived from THIS proposal, not carried over from another;
    6. no `candidate_output` — a deferred run carrying an authoritative output has laundered a
       guess into authority;
    7. re-folding the RECORDED axes yields `RESOLVED` once the output axis is resolved, so a
       payload whose disposition disagrees with its own axes is refused rather than believed.
    """
    disposition = _payload_field(event, "authoring_disposition")
    if disposition == _RESOLVED:
        return
    if disposition == _READY_FOR_OUTPUT_BINDING and _is_v3_deferral(event, result, proposal):
        return
    raise MaterializationRefused(
        CompilationRefusalCode.NOT_RESOLVED,
        f"the terminal {event.kind} event of authoring run {run_id} records "
        f"authoring_disposition={disposition!r}, which is neither {_RESOLVED} nor a V3 run whose "
        f"only outstanding step is the governed output binding the compiler performs",
    )


def _is_v3_deferral(
    event: _trace.TerminalEvent,
    result: AuthoringResultV2,
    proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
) -> bool:
    """Conditions 1-7 above. Any one of them false refuses, and none is inferred from another."""
    from featuregen.formula.result_v2 import _content_hash
    from featuregen.formula.schema_v3 import TypedFormulaProposalV3 as _V3

    if not isinstance(proposal, _V3):                                          # 1
        return False
    if _payload_field(event, "output_status") != _DEFERRED_OUTPUT:             # 3
        return False
    intent = result.output_intent                                              # 4
    if intent is None:
        return False
    derived_from = getattr(intent, "derived_from_proposal_hash", None)         # 5
    if not derived_from or derived_from != _content_hash(proposal):
        return False
    if result.candidate_output is not None:                                    # 6
        return False
    return _refolds_to_resolved(event)                                         # 7


def _refolds_to_resolved(event: _trace.TerminalEvent) -> bool:
    """Would this run be ``RESOLVED`` if its output authority were established?

    Asked of :func:`_fold_v2` DIRECTLY, over the axes the terminal event recorded — which check 2
    has already proven authentic against the payload's own digest — rather than by restating the
    fold's precedence as a second list of conditions here. A second list is a second rule, and the
    two would disagree the first time either moved.
    """
    from featuregen.formula.replay_authoring_v2 import _axes, _restore_review
    from featuregen.formula.result_v2 import AuthoringAxesV2, _fold_v2

    shared = dict(
        structural_status=_payload_field(event, "structural_status"),
        capability_status=_payload_field(event, "capability_status"),
        # THE SWAP, and the only one. Everything else is exactly as recorded.
        output_status="resolved",
        expectation_status=_payload_field(event, "expectation_status"),
        technical_status=_payload_field(event, "technical_status"),
    )
    try:
        # WHICH AXES SHAPE, decided the way the RESTORER decides it (`_restore_terminal_result`):
        # a run with a recorded `review` folds through the V2 axes and a run without it through
        # V1's. Choosing wrongly is not a near miss — the v1-shaped builder REJECTS a bypass's
        # `critic_status=None`, so a single shape here would fail closed on exactly one of the two
        # legitimate paths and read as "this run is inadmissible".
        review = _restore_review(_payload_field(event, "review"))
        # ▲ `AuthoringAxesV2` has `review` WHERE V1 HAS `critic_status` — not in addition to it —
        # and `_fold_v2` folds the V1-SHAPED projection, exactly as `derive_disposition_v2` does
        # (`axes = axes.shared_axes()` before folding). A bypass projects to "clean" for folding
        # only, which is what makes the deterministic path foldable at all. Getting either half
        # wrong here does not misjudge a run — it raises, and this function turns a raise into a
        # refusal, so every reviewed-blueprint run would read as inadmissible for a reason that is
        # a typo in the caller rather than anything about the run.
        axes = (AuthoringAxesV2(review=review, **shared).shared_axes()
                if review is not None
                else _axes(critic_status=_payload_field(event, "critic_status"), **shared))
    except (TypeError, ValueError):
        # An axis the vocabulary does not know is not a permissive one. `_validate_axes` fails
        # closed for the same reason and this mirrors it rather than swallowing it.
        return False
    return _fold_v2(axes) == _RESOLVED


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


# ── 4b. the MANIFEST, the PROPOSAL and the RUNTIME TYPE all name one language ────────────────────
def _verify_manifest_declares_the_language(
    conn: DbConn,
    proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
    run_id: str,
) -> None:
    """Three records of one fact must agree: what the run DECLARED, what the proposal SAYS, and
    what the proposal IS.

    ▲ DEFENCE IN DEPTH, and the depth is the point. Authoring now refuses a schema mismatch at the
    moment it happens (`FORMULA_SCHEMA_CONTRACT_MISMATCH`), which is where it belongs — the run
    fails, names the mismatch, and nothing downstream ever sees it. This check exists for the runs
    that did not come through today's authoring path: traces written before it, imported material,
    and whatever a future orchestration mistake produces. Those are exactly the cases the authoring
    check cannot cover, because it did not run.

    The RUNTIME TYPE is compared as well as the declared integer, because they are separately
    forgeable: `parse_versioned` dispatches on the declared field, so an object whose field says 3
    while its class is `TypedFormulaProposalV2` would satisfy an integer comparison and then be
    read by every downstream stage as v2 — which is the shape of the bug this whole family of
    checks exists to catch.

    A run with NO manifest is refused rather than waved through: a run that states nothing about
    what decided it cannot be shown to agree with anything.
    """
    row = conn.execute(
        "SELECT versions FROM formula_authoring_run WHERE authoring_run_id = %s",
        (run_id,)).fetchone()
    declared = (row[0] or {}).get("formula_schema") if row else None
    if declared is None:
        raise MaterializationRefused(
            CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE,
            f"authoring run {run_id} records no formula_schema in its manifest, so what language "
            f"it was opened under is unanswerable — and an unanswerable provenance is not a "
            f"permissive one")
    if declared != proposal.formula_schema_version:
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"authoring run {run_id} declares formula_schema={declared!r} in its manifest but "
            f"carries a version {proposal.formula_schema_version} proposal. The manifest is what "
            f"every later reader is keyed on, so admitting this would file the artifact under a "
            f"language it is not written in")
    expected = TypedFormulaProposalV3 if declared == 3 else TypedFormulaProposalV2
    if not isinstance(proposal, expected):
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"authoring run {run_id} declares formula_schema={declared!r} and its proposal says so, "
            f"but the object is a {type(proposal).__name__}: the declared field and the type are "
            f"separately forgeable, and every stage below this one reads the TYPE")


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
    """The check v1's docstring promised, at the RENDERER-SUPPORT grain.

    **Admission asks whether code can be produced, not whether anyone has proved it correct.** Those
    are two different questions with two different answers and two different remedies, and this
    function now answers only the first:

    * *renderer-supported* gates CODE GENERATION — the user may read what was generated.
    * *execution-qualified* (``advertised_operators``) is reported alongside, not required here.
    * *artifact-verified* gates PUBLICATION, and is a fact about one artifact.

    It used to require dispatch ∩ proof, which made admission unreachable in practice: nothing
    populates proofs until a gold harness exists, so every formula refused, and the shortest route
    to a working pipeline was to write a proof record for a proof nobody ran. Requiring less here
    removes that temptation rather than relying on someone resisting it — and nothing is loosened
    downstream, because publication still demands a current artifact verification.
    """
    implied = implied_operator_signatures(proposal)
    supported = set(renderer_supported_operators(conn, engine_id=engine_id))
    missing = sorted(set(implied) - supported)
    if missing:
        named = ", ".join(f"{kind}:{variant}" for kind, variant in missing)
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"authoring run {run_id} implies operators {named}, which {engine_id!r}'s renderer "
            f"cannot emit in this build. This is the RENDERER-SUPPORT gate: it asks whether code "
            f"can be produced at all, not whether anyone has proved it correct. An operator the "
            f"renderer has no branch for cannot be compiled into anything, so there is nothing to "
            f"show and nothing to verify",
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
