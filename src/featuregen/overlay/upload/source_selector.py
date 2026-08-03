"""The source SELECTOR — a :class:`DatasetNeedV1` in, a :class:`DatasetSourceSelectionV1` out.

Task 7 froze the §6.4 contracts and left them without a producer. This is the producer, and it is
the module that decides WHICH COPY of a dataset answers a need. Everything it does is a rule the
plan states in words; nothing here is a heuristic.

**The population is never inferred.** ``POPULATION`` resolves from an explicit request or a current
serving policy, and from nothing else (§5.4). It deliberately does not look at ``primary_entity``,
at ``authority_role``, or at the table's NAME — the three things that make ``kyc_customers``,
``card_customers`` and ``customers`` look interchangeable to a catalog. A wrong population does not
fail; it silently shrinks every number built on it, and the audit trail says the system chose. So
the absence of a declaration is :data:`~...source_selection.SELECTION_POPULATION_UNDECLARED`, and
the caller's candidate list is not even READ for that need role. ``test_source_selector`` pins that
by handing the population need a candidate set that would obviously "win" any ranking.

**And a population is ONE ROW PER MEMBER.** A declared dataset whose own temporal model says it
keeps history (scd2 / snapshot / event_log) is refused for that role — see
:func:`_population_history_refusal`, which also records why that refusal is spelled with the
population code rather than a temporal one. The spine carries no row rule at execution, so a
history-keeping population does not fail; it multiplies every count while looking entirely
reasonable.

**Precedence for every other need (§8 Task 8):** explicit request -> current serving policy -> a
single unambiguous eligible candidate. Two candidates that a rule cannot separate are
:data:`SELECTION_SOURCE_AMBIGUOUS`, never broken by upload time, lexical order or catalog recency
— this module reads no clock, no row ordering and no recency column at all. Two mutation tests pin
that: a reordered candidate list gives the byte-identical refusal, and the module TEXT is asserted
to contain none of the tokens by which that rule gets broken by accident.

**Authority ranks; it does not admit.** Only a LOAD-BEARING ``authority_role`` may rank candidates
(§5.3). A merely displayed (LLM-proposed) authority value may rank in the SANDBOX tier only, and
when it does the selection rides a visible :data:`PROPOSED_AUTHORITY_USED` warning; in PRODUCTION
the same situation is :data:`SELECTION_AUTHORITY_INSUFFICIENT`. That is why ``execution_tier`` is
part of the NEED rather than a call-site argument: the same need answered for sandbox and for
production is genuinely two decisions.

**Only the WINNER is pinned.** Candidates are assessed through ``binding_store.resolve_table``, a
pure read; the selected dataset alone goes through ``binding_store.select_table_binding``, which is
the seam that PERSISTS the ``pbr_`` revision the selection then names. Assessing through the
persisting seam would record bindings for datasets the selector is about to reject, and a losing
candidate therefore carries ``binding_revision_id=None`` rather than a deterministic id naming a
row that was never written.

**Refusals are RETURNED, not raised** — the precedent named in ``analysis/plan.py`` and reused by
``temporal_policy_agreement``: a refusal is a decision outcome that the caller renders as a
clarification or records as a gap, and an exception forces every call site to re-classify it.

**Import weight.** ``dataset_profiles`` pulls the concept registry (~1s). Every import of it here is
made lazily inside the function that needs it, the precedent ``source_selection`` records for
itself, so ``analysis`` importing this module costs the four dataclasses and nothing else.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.profile_vocab import AuthorityRole
from featuregen.overlay.upload.source_selection import (
    SELECTION_AUTHORITY_INSUFFICIENT,
    SELECTION_BINDING_MISSING,
    SELECTION_POPULATION_UNDECLARED,
    SELECTION_SOURCE_AMBIGUOUS,
    AuthorityBasis,
    CandidateDecisionV1,
    CandidateDisposition,
    DatasetNeedRole,
    DatasetNeedV1,
    DatasetSourceSelectionV1,
    SelectionBasis,
    SelectionError,
    normalize_dataset_ref,
)

if TYPE_CHECKING:                       # import weight — see the module docstring
    from featuregen.contracts import DbConn
    from featuregen.overlay.upload.source_selection import DatasetServingPolicyRevisionV1

#: Rides a SELECTION that was ranked on a merely PROPOSED authority value. Sandbox only, and always
#: visible: the whole point of letting an unconfirmed classification be USEFUL is that its authority
#: travels with it (§5.2), so the warning is part of the decision rather than a log line.
PROPOSED_AUTHORITY_USED = "PROPOSED_AUTHORITY_USED"

#: Which ``AuthorityRole`` members are authority to RANK on, and how strongly.
#:
#: Deliberately PARTIAL. ``derived``, ``non_authoritative_replica``, ``external_reference`` and
#: ``unknown`` are absent because none of them is a claim that this copy is the one to believe — and
#: inventing an order among them ("is a derived table better than an unvouched replica?") would be
#: this module making a business decision it has no standing to make. An absent role ranks 0, which
#: means it never WINS a contest; it can still be selected as the single eligible candidate, because
#: "the only dataset that can serve this" is not a claim about authority at all.
_AUTHORITY_RANK: dict[str, int] = {
    AuthorityRole.SYSTEM_OF_RECORD.value: 3,
    AuthorityRole.MASTERED_VIEW.value: 2,
    AuthorityRole.AUTHORITATIVE_REPLICA.value: 1,
}

#: The canonical content of "this dataset has no assembled profile". A CandidateDecisionV1 requires
#: a non-empty ``dataset_profile_hash`` (rule 9: every candidate is recorded, including the rejected
#: ones), and a blank or a literal ``"absent"`` would either be refused by the contract or collide
#: across datasets. A JCS hash over a DIFFERENT contract token is honest and cannot be mistaken for
#: a real profile hash.
_ABSENT_PROFILE_CONTRACT = "dataset_semantic_profile_absent_v1"


def _absent_profile_hash(dataset_ref: str) -> str:
    return materialize_hash({"contract": _ABSENT_PROFILE_CONTRACT,
                             "dataset_logical_ref": dataset_ref})


@dataclass(frozen=True, slots=True)
class SelectionRefusalV1:
    """A typed source/row refusal: the closed code, what it is about, and everything considered.

    ``considered_candidates`` is populated even here — especially here. Functional rule 9 says every
    decision records the candidates and why each was admitted, rejected or tied, and a REFUSAL is
    the decision a person most needs to see the workings of."""

    code: str
    subject_refs: tuple[str, ...]
    detail: str
    considered_candidates: tuple[CandidateDecisionV1, ...] = ()
    #: WHAT is actually absent, for the codes that cover more than one situation (the closed eight
    #: cannot grow, so the payload discriminates — the same move `considered_candidates` makes for
    #: SELECTION_SOURCE_AMBIGUOUS). Closed micro-vocabulary, temporal refusals only:
    #: "temporal_policy" (nothing declared), "report_cutoff" (rule reads as-of, request names no
    #: instant), "current_row_rule" (policy declares no rule for a current question). None for
    #: source-side refusals. Ephemeral decision payload — never content-hashed.
    missing: str | None = None

    def __post_init__(self) -> None:
        if not self.subject_refs:
            raise SelectionError(
                "a selection refusal with no subject cannot become a clarification or a gap")


@dataclass(frozen=True, slots=True)
class SourceSelectionOutcomeV1:
    """Exactly one of ``selection`` / ``refusal``, plus any warnings riding the decision."""

    need: DatasetNeedV1
    selection: DatasetSourceSelectionV1 | None = None
    refusal: SelectionRefusalV1 | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.selection is None) == (self.refusal is None):
            raise SelectionError(
                "a source-selection outcome is exactly one of a selection or a refusal")

    @property
    def resolved(self) -> bool:
        return self.selection is not None


@dataclass(slots=True)
class _Assessed:
    """One candidate, as the catalog answers for it. Mutable only while being assembled."""

    dataset_ref: str
    profile_hash: str
    has_profile: bool
    has_binding: bool
    load_bearing_authority: str = ""
    proposed_authority: str = ""
    reasons: set[str] = field(default_factory=set)

    def decision(self, disposition: CandidateDisposition, *,
                 binding_revision_id: str | None = None) -> CandidateDecisionV1:
        return CandidateDecisionV1(
            dataset_ref=self.dataset_ref, dataset_profile_hash=self.profile_hash,
            binding_revision_id=binding_revision_id, disposition=disposition,
            reason_codes=tuple(sorted(self.reasons)))

    def rank(self, *, allow_proposed: bool) -> int:
        """The candidate's ranking strength. ``allow_proposed`` is the SANDBOX-only widening."""
        role = self.load_bearing_authority or (self.proposed_authority if allow_proposed else "")
        return _AUTHORITY_RANK.get(role, 0)


def _split(dataset_ref: str) -> tuple[str, str]:
    """(catalog_source, bare table name) for a normalized schema-preserving table ref."""
    source, _schema, table, _column = parse_ref(dataset_ref)
    return source, table


def _readable(conn: DbConn, dataset_ref: str, roles: Sequence[str]) -> bool:
    """Can this caller see the table anchor? HIDDEN AND MISSING ARE THE SAME ANSWER (D11) — a
    candidate the caller may not see is dropped exactly as one that does not exist, because
    reporting "there is a better copy you may not see" is an existence oracle over a catalog they
    were never granted."""
    from featuregen.overlay.upload.source_selection import assert_dataset_refs_readable

    try:
        assert_dataset_refs_readable(conn, (dataset_ref,), roles=roles)
    except SelectionError:
        return False
    return True


def _assess(conn: DbConn, dataset_ref: str, *, roles: Sequence[str]) -> _Assessed:
    """Read one candidate's profile and its physical addressability. NO WRITES: the persisting
    binding seam is reserved for the winner (module docstring)."""
    from featuregen.data_agent.binding_store import resolve_table
    from featuregen.data_agent.connection import ConnectionError_
    from featuregen.overlay.upload.dataset_profiles import build_dataset_profile
    from featuregen.overlay.upload.profile_store import current_catalog_profile_revision_id

    source, table = _split(dataset_ref)
    profile = build_dataset_profile(
        conn, source=source, dataset_logical_ref=dataset_ref,
        catalog_profile_revision_id=current_catalog_profile_revision_id(conn, source))
    assessed = _Assessed(
        dataset_ref=dataset_ref,
        profile_hash=(profile.dataset_profile_hash if profile is not None
                      else _absent_profile_hash(dataset_ref)),
        has_profile=profile is not None,
        has_binding=False)
    if profile is None:
        assessed.reasons.add("profile_absent")
    else:
        role_field = profile.authority_role
        if role_field.load_bearing is not None:
            assessed.load_bearing_authority = str(role_field.load_bearing.value or "")
        if role_field.display is not None:
            assessed.proposed_authority = str(role_field.display.value or "")

    try:
        # A REVOKED grant raises rather than reading as "unconfigured" — that distinction is the
        # whole reason `resolve_table` raises instead of returning None for it. Here it still means
        # this copy cannot serve, so it is recorded as an unaddressable candidate rather than
        # propagated: one dataset whose grant was withdrawn must not take the whole decision down.
        assessed.has_binding = resolve_table(
            conn, catalog_source=source, table=table) is not None
    except ConnectionError_:
        assessed.has_binding = False
    if not assessed.has_binding:
        assessed.reasons.add("no_binding")
    return assessed


def _pin_binding(conn: DbConn, dataset_ref: str, *, recorded_by: str | None) -> str | None:
    """PERSIST the winner's binding and return its immutable ``pbr_`` revision id.

    ``None`` when the dataset cannot be addressed at all, or when the catalog cannot say WHICH table
    the name means (``_assert_one_addressable_table``) — both are
    :data:`SELECTION_BINDING_MISSING`, which is the code that seam already raises."""
    from featuregen.data_agent.binding_store import select_table_binding
    from featuregen.data_agent.connection import ConnectionError_

    source, table = _split(dataset_ref)
    try:
        selected = select_table_binding(conn, catalog_source=source, table=table,
                                        recorded_by=recorded_by)
    except ConnectionError_:
        return None
    return None if selected is None else selected[2]


def _current_policy(conn: DbConn, need: DatasetNeedV1) -> DatasetServingPolicyRevisionV1 | None:
    from featuregen.overlay.upload.serving_policy_store import current_serving_policy

    entity_id, need_role, serving_purpose = need.policy_key()
    found = current_serving_policy(conn, entity_id=entity_id, need_role=need_role,
                                   serving_purpose=serving_purpose)
    return None if found is None else found[0]


def _policy_choice(policy: DatasetServingPolicyRevisionV1) -> tuple[str | None, tuple[str, ...]]:
    """``(chosen_ref | None, the refs that are tied)`` for a policy on its own.

    One preferred ref decides. Two-plus equally preferred is an EXPLICIT ambiguity the policy
    contract already names (:attr:`DatasetServingPolicyRevisionV1.ambiguous`); zero preferred with
    one eligible decides; zero preferred with several is the same ambiguity. Nothing here breaks a
    tie."""
    if len(policy.preferred_dataset_refs) == 1:
        return policy.preferred_dataset_refs[0], ()
    if policy.preferred_dataset_refs:
        return None, policy.preferred_dataset_refs
    if len(policy.eligible_dataset_refs) == 1:
        return policy.eligible_dataset_refs[0], ()
    return None, policy.eligible_dataset_refs


def select_dataset_source(
    conn: DbConn,
    *,
    need: DatasetNeedV1,
    candidate_dataset_refs: Iterable[str] = (),
    roles: Sequence[str] = (),
    recorded_by: str | None = None,
) -> SourceSelectionOutcomeV1:
    """Resolve ONE need to one dataset, or to a typed refusal that names every candidate.

    ``candidate_dataset_refs`` is the bounded set the caller already grounded the question against
    (the retrieval candidates), never a catalog scan: a selector that widened its own candidate set
    could pick a dataset the plan was never grounded against. It is IGNORED for a ``POPULATION``
    need, deliberately (module docstring).
    """
    if need.need_role is DatasetNeedRole.POPULATION:
        return _select_population(conn, need=need, roles=roles, recorded_by=recorded_by)
    return _select_supporting(conn, need=need, roles=roles, recorded_by=recorded_by,
                              candidate_dataset_refs=candidate_dataset_refs)


#: Temporal storage models that keep MORE THAN ONE ROW PER ENTITY. A dataset in one of these is a
#: history, not a population: used as the spine it multiplies every count by however many versions
#: each customer happens to have, and the answer stays plausible while being wrong for exactly the
#: customers who changed the most.
_HISTORY_KEEPING_MODELS: frozenset[str] = frozenset({"scd2", "snapshot", "event_log"})


def _population_history_refusal(conn: DbConn, *, need: DatasetNeedV1,
                                chosen: str) -> SelectionRefusalV1 | None:
    """Refuse a POPULATION whose own temporal model says it keeps several rows per entity.

    THE SPINE HAS NO ROW RULE. ``compile_analysis`` reads the population table RAW — every row is a
    population member — because that is what a population IS. So an SCD2 or snapshot table used as
    the spine does not fail: it silently multiplies every count, and the fixture comments have named
    that hazard since Release 3 without anything enforcing it.

    VOCABULARY ADJUDICATION (the closed eight). This is spelled ``SELECTION_POPULATION_UNDECLARED``
    with a population-specific detail, NOT a new code and not one of the temporal ones:

    * ``TEMPORAL_HISTORICAL_CURRENT_ONLY`` and ``TEMPORAL_MODEL_UNKNOWN`` are statements about a
      dataset's ability to answer a ROW question; here the temporal model is perfectly well known
      and the dataset answers its own question fine. It is the POPULATION role it cannot fill.
    * ``SELECTION_AUTHORITY_INSUFFICIENT`` would say the copy is not authoritative enough, which is
      false — it may be the system of record and still be the wrong SHAPE for a spine.
    * What is genuinely missing is the declaration of WHICH ROWS of it are the population — the
      same decision ``SELECTION_POPULATION_UNDECLARED`` already names, reached from one step
      further in. Its gap (``POPULATION_UNRESOLVED``) and its action (``CONFIRM_POPULATION``) are
      already exactly right, and its clarification asks the one question that resolves this: which
      table holds the population, every member, once.

    A CURRENT-ONLY dataset passes. So does a history-keeping dataset whose current temporal policy
    declares ``current_record`` — that is a declared one-row-per-entity rule rather than an
    inference, and the runtime spine assertion (``analysis.assert_spine_is_unique``) is what stops
    it if the declaration turns out not to hold of the data.
    """
    from featuregen.overlay.upload.temporal_policy import TemporalSelectionKind
    from featuregen.overlay.upload.temporal_policy_store import (
        current_temporal_policy,
        load_bearing_temporal_model,
    )

    source, _table = _split(chosen)
    load_bearing, _displayed = load_bearing_temporal_model(
        conn, source=source, dataset_logical_ref=chosen)
    found = current_temporal_policy(conn, chosen)
    policy = None if found is None else found[0]
    model = (load_bearing or "").strip().lower() or (
        policy.temporal_storage_model.value if policy is not None else "")
    if model not in _HISTORY_KEEPING_MODELS:
        return None
    if policy is not None and policy.current_selection is TemporalSelectionKind.CURRENT_RECORD:
        return None                     # a DECLARED "which row is the current one" rule
    return SelectionRefusalV1(
        code=SELECTION_POPULATION_UNDECLARED,
        subject_refs=(chosen,),
        detail=(f"{chosen!r} keeps history ({model}), so it holds several rows per "
                f"{need.entity_id}, and a population is one row per member. Used as the spine it "
                "does not fail — it multiplies every count by however many versions a customer "
                "happens to have, and the answer stays plausible. Declare a row rule for it (a "
                "temporal policy naming which row is the current one), or name a dataset that is "
                "already one row per member"))


def _select_population(conn: DbConn, *, need: DatasetNeedV1, roles: Sequence[str],
                       recorded_by: str | None) -> SourceSelectionOutcomeV1:
    """Explicit declaration or a serving policy. THERE IS NO THIRD RULE, and that is the feature."""
    policy = _current_policy(conn, need)
    if need.explicit_dataset_ref:
        # Checked BEFORE `_finish`, which persists the winner's binding: a dataset this need is
        # about to refuse must not leave a pinned binding behind.
        refusal = _population_history_refusal(conn, need=need, chosen=need.explicit_dataset_ref)
        if refusal is not None:
            return SourceSelectionOutcomeV1(need=need, refusal=refusal)
        return _finish(conn, need=need, chosen=need.explicit_dataset_ref,
                       basis=SelectionBasis.EXPLICIT_REQUEST, policy=policy, others=(),
                       roles=roles, recorded_by=recorded_by)
    if policy is not None:
        chosen, tied = _policy_choice(policy)
        if chosen is not None:
            refusal = _population_history_refusal(conn, need=need, chosen=chosen)
            if refusal is not None:
                return SourceSelectionOutcomeV1(need=need, refusal=refusal)
            return _finish(conn, need=need, chosen=chosen, basis=SelectionBasis.SERVING_POLICY,
                           policy=policy,
                           others=tuple(r for r in policy.eligible_dataset_refs if r != chosen),
                           roles=roles, recorded_by=recorded_by)
        return _ambiguous(conn, need=need, tied=tied, policy=policy, roles=roles)
    return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
        code=SELECTION_POPULATION_UNDECLARED,
        subject_refs=(need.entity_id,),
        detail=("the population dataset is not declared for "
                f"{need.entity_id!r}: name it in the request, or publish a serving policy for "
                f"({need.entity_id}, {need.need_role.value}, {need.serving_purpose.value}). It is "
                "never inferred from a primary entity, an authority role or a table name — a "
                "population chosen by the system does not fail, it silently shrinks every number "
                "built on it")))


def _select_supporting(conn: DbConn, *, need: DatasetNeedV1, roles: Sequence[str],
                       recorded_by: str | None,
                       candidate_dataset_refs: Iterable[str]) -> SourceSelectionOutcomeV1:
    """Explicit request -> current serving policy -> a single unambiguous eligible candidate."""
    policy = _current_policy(conn, need)
    if need.explicit_dataset_ref:
        others = tuple(normalize_dataset_ref(r, what="candidate_dataset_ref")
                       for r in candidate_dataset_refs)
        return _finish(conn, need=need, chosen=need.explicit_dataset_ref,
                       basis=SelectionBasis.EXPLICIT_REQUEST, policy=policy,
                       others=tuple(r for r in others if r != need.explicit_dataset_ref),
                       roles=roles, recorded_by=recorded_by)
    if policy is not None:
        chosen, tied = _policy_choice(policy)
        if chosen is not None:
            return _finish(conn, need=need, chosen=chosen, basis=SelectionBasis.SERVING_POLICY,
                           policy=policy,
                           others=tuple(r for r in policy.eligible_dataset_refs if r != chosen),
                           roles=roles, recorded_by=recorded_by)
        return _ambiguous(conn, need=need, tied=tied, policy=policy, roles=roles)

    refs = tuple(sorted({normalize_dataset_ref(r, what="candidate_dataset_ref")
                         for r in candidate_dataset_refs}))
    visible = [r for r in refs if _readable(conn, r, roles)]
    assessed = [_assess(conn, r, roles=roles) for r in visible]
    eligible = [a for a in assessed if a.has_binding and a.has_profile]

    if len(eligible) == 1:
        return _finish(conn, need=need, chosen=eligible[0].dataset_ref,
                       basis=SelectionBasis.SINGLE_ELIGIBLE_CANDIDATE, policy=None,
                       others=tuple(a.dataset_ref for a in assessed
                                    if a.dataset_ref != eligible[0].dataset_ref),
                       roles=roles, recorded_by=recorded_by, assessed=assessed)
    if not eligible:
        return _nothing_eligible(need=need, assessed=assessed)
    return _rank(conn, need=need, eligible=eligible, assessed=assessed, roles=roles,
                 recorded_by=recorded_by)


def _rank(conn: DbConn, *, need: DatasetNeedV1, eligible: list[_Assessed],
          assessed: list[_Assessed], roles: Sequence[str],
          recorded_by: str | None) -> SourceSelectionOutcomeV1:
    """Break a multi-candidate contest on AUTHORITY, or refuse. Never on order, name or time."""
    from featuregen.overlay.upload.bridge_realization import ExecutionTier

    governed_top = max(a.rank(allow_proposed=False) for a in eligible)
    governed_winners = [a for a in eligible if a.rank(allow_proposed=False) == governed_top]
    if governed_top > 0 and len(governed_winners) == 1:
        winner = governed_winners[0]
        winner.reasons.add("load_bearing_authority")
        return _finish(conn, need=need, chosen=winner.dataset_ref,
                       basis=SelectionBasis.SINGLE_ELIGIBLE_CANDIDATE, policy=None,
                       others=tuple(a.dataset_ref for a in assessed
                                    if a.dataset_ref != winner.dataset_ref),
                       roles=roles, recorded_by=recorded_by, assessed=assessed,
                       authority_basis=AuthorityBasis.LOAD_BEARING_PROFILE)

    # A governed authority could not separate them. Would a PROPOSED one have?
    proposed_top = max(a.rank(allow_proposed=True) for a in eligible)
    proposed_winners = [a for a in eligible if a.rank(allow_proposed=True) == proposed_top]
    proposed_would_decide = proposed_top > 0 and len(proposed_winners) == 1

    if proposed_would_decide and need.execution_tier is ExecutionTier.SANDBOX:
        winner = proposed_winners[0]
        winner.reasons.add("proposed_authority_sandbox")
        return _finish(conn, need=need, chosen=winner.dataset_ref,
                       basis=SelectionBasis.SINGLE_ELIGIBLE_CANDIDATE, policy=None,
                       others=tuple(a.dataset_ref for a in assessed
                                    if a.dataset_ref != winner.dataset_ref),
                       roles=roles, recorded_by=recorded_by, assessed=assessed,
                       authority_basis=AuthorityBasis.PROPOSED_AUTHORITY_SANDBOX,
                       warnings=(PROPOSED_AUTHORITY_USED,))
    if proposed_would_decide:
        # PRODUCTION. The distinction matters: this is not "nobody can tell these apart", it is
        # "the only thing that tells them apart is a proposal nobody confirmed" — and the action is
        # to confirm one, not to pick a dataset.
        for candidate in eligible:
            candidate.reasons.add("authority_insufficient")
        return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
            code=SELECTION_AUTHORITY_INSUFFICIENT,
            subject_refs=tuple(sorted(a.dataset_ref for a in eligible)),
            detail=(f"{len(eligible)} datasets can serve this need in production and the only "
                    f"thing separating them is a PROPOSED authority_role on "
                    f"{proposed_winners[0].dataset_ref!r}. A proposal may rank in sandbox; "
                    "production needs the classification confirmed through the four-eyes profile "
                    "flow, or a serving policy that names the copy"),
            considered_candidates=_candidates(assessed, tied={a.dataset_ref for a in eligible})))

    for candidate in eligible:
        candidate.reasons.add("equally_preferred")
    return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
        code=SELECTION_SOURCE_AMBIGUOUS,
        subject_refs=tuple(sorted(a.dataset_ref for a in eligible)),
        detail=(f"{len(eligible)} datasets are equally eligible to serve this need and no governed "
                "authority separates them. Declare a serving policy or name the dataset in the "
                "request — a tie broken by upload time, lexical order or catalog recency is an "
                "ordering accident wearing a decision's clothes"),
        considered_candidates=_candidates(assessed, tied={a.dataset_ref for a in eligible})))


def _nothing_eligible(*, need: DatasetNeedV1, assessed: list[_Assessed]) -> SourceSelectionOutcomeV1:
    """No candidate can serve. Which of the eight closed codes is honest depends on WHY."""
    subjects = tuple(sorted(a.dataset_ref for a in assessed)) or (need.entity_id,)
    if assessed and all("no_binding" in a.reasons for a in assessed):
        return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
            code=SELECTION_BINDING_MISSING, subject_refs=subjects,
            detail=("every candidate for this need is explainable metadata with no current "
                    "authorized physical binding: it can be described, not read"),
            considered_candidates=_candidates(assessed, tied=set())))
    # Nothing to choose from, or nothing with an assembled profile. The vocabulary is CLOSED at
    # eight and this is "the source is not settled" — the same judgement `temporal_policy_agreement`
    # records for TEMPORAL_MODEL_UNKNOWN covering both absence and contradiction. Inventing a ninth
    # spelling here would fork a vocabulary that `analysis.intent`, `analysis.clarify` and
    # `data_agent.learning` all derive from, and the GAP and ACTION are already exactly right:
    # DATASET_SOURCE_UNRESOLVED / DECLARE_SERVING_POLICY.
    return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
        code=SELECTION_SOURCE_AMBIGUOUS, subject_refs=subjects,
        detail=(f"no dataset is eligible to serve ({need.entity_id}, {need.need_role.value}): "
                f"{len(assessed)} candidate(s) were considered and none has both an assembled "
                "profile and a current physical binding. Declare a serving policy naming the copy"),
        considered_candidates=_candidates(assessed, tied=set())))


def _ambiguous(conn: DbConn, *, need: DatasetNeedV1, tied: tuple[str, ...],
               policy: DatasetServingPolicyRevisionV1,
               roles: Sequence[str]) -> SourceSelectionOutcomeV1:
    """The POLICY itself cannot pick — zero preferred with several eligible, or several preferred."""
    assessed = [_assess(conn, ref, roles=roles) for ref in policy.eligible_dataset_refs]
    for candidate in assessed:
        candidate.reasons.add("policy_eligible")
        if candidate.dataset_ref in tied:
            candidate.reasons.add("equally_preferred")
    return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
        code=SELECTION_SOURCE_AMBIGUOUS, subject_refs=tuple(sorted(tied)),
        detail=(f"serving policy {policy.revision_id} leaves {len(tied)} equally eligible "
                "datasets and nothing declares which serves. Name one preferred dataset, or name "
                "the dataset in the request"),
        considered_candidates=_candidates(assessed, tied=set(tied))))


def _candidates(assessed: Iterable[_Assessed], *,
                tied: set[str]) -> tuple[CandidateDecisionV1, ...]:
    """EVERY candidate, with a disposition — the rule-9 workings of a refusal."""
    return tuple(
        a.decision(CandidateDisposition.TIED if a.dataset_ref in tied
                   else CandidateDisposition.REJECTED)
        for a in assessed)


def _finish(conn: DbConn, *, need: DatasetNeedV1, chosen: str, basis: SelectionBasis,
            policy: DatasetServingPolicyRevisionV1 | None, others: tuple[str, ...],
            roles: Sequence[str], recorded_by: str | None,
            assessed: list[_Assessed] | None = None,
            authority_basis: AuthorityBasis | None = None,
            warnings: tuple[str, ...] = ()) -> SourceSelectionOutcomeV1:
    """Assess the winner, PIN its binding, and build the immutable decision."""
    chosen = normalize_dataset_ref(chosen, what="selected_dataset_ref")
    if not _readable(conn, chosen, roles):
        # Hidden and missing read the same (D11). An explicit request for a dataset the caller
        # cannot see is answered as one that cannot be addressed, never as "it exists but not for
        # you".
        return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
            code=SELECTION_BINDING_MISSING, subject_refs=(chosen,),
            detail=f"{chosen!r} is not a readable dataset in this catalog"))

    by_ref = {a.dataset_ref: a for a in (assessed or ())}
    winner = by_ref.get(chosen) or _assess(conn, chosen, roles=roles)
    if basis is SelectionBasis.EXPLICIT_REQUEST:
        winner.reasons.add("explicit_request")
    elif basis is SelectionBasis.SERVING_POLICY and policy is not None:
        winner.reasons.add("policy_preferred" if chosen in policy.preferred_dataset_refs
                           else "policy_eligible")

    if not winner.has_binding:
        return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
            code=SELECTION_BINDING_MISSING, subject_refs=(chosen,),
            detail=(f"{chosen!r} has no current authorized physical binding: a semantic profile "
                    "without an address is explainable metadata, not an executable source"),
            considered_candidates=(winner.decision(CandidateDisposition.REJECTED),)))

    revision_id = _pin_binding(conn, chosen, recorded_by=recorded_by)
    if revision_id is None:
        return SourceSelectionOutcomeV1(need=need, refusal=SelectionRefusalV1(
            code=SELECTION_BINDING_MISSING, subject_refs=(chosen,),
            detail=(f"{chosen!r} could not be pinned to one immutable binding revision; the "
                    "catalog cannot say which physical table this name means"),
            considered_candidates=(winner.decision(CandidateDisposition.REJECTED),)))

    decisions = [winner.decision(CandidateDisposition.SELECTED, binding_revision_id=revision_id)]
    for ref in dict.fromkeys(others):
        if ref == chosen:
            continue
        other = by_ref.get(ref) or _assess(conn, ref, roles=roles)
        if policy is not None and ref not in policy.eligible_dataset_refs:
            other.reasons.add("not_in_policy")
        elif policy is not None:
            other.reasons.add("policy_eligible")
        decisions.append(other.decision(
            CandidateDisposition.ELIGIBLE if other.has_binding and other.has_profile
            else CandidateDisposition.REJECTED))

    resolved_authority = winner.load_bearing_authority or (
        winner.proposed_authority
        if authority_basis is AuthorityBasis.PROPOSED_AUTHORITY_SANDBOX else "")
    if authority_basis is None:
        authority_basis = (AuthorityBasis.POLICY_DECLARATION
                           if basis is SelectionBasis.SERVING_POLICY else AuthorityBasis.UNKNOWN)
    return SourceSelectionOutcomeV1(
        need=need, warnings=warnings,
        selection=DatasetSourceSelectionV1(
            need=need,
            selected_dataset_ref=chosen,
            selected_dataset_profile_hash=winner.profile_hash,
            selected_binding_revision_id=revision_id,
            serving_policy_revision_id=(policy.revision_id if policy is not None
                                        and basis is SelectionBasis.SERVING_POLICY else None),
            authority_role=resolved_authority or AuthorityRole.UNKNOWN.value,
            authority_basis=authority_basis,
            selection_basis=basis,
            considered_candidates=tuple(decisions)))
