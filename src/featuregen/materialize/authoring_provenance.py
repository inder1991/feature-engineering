"""HOW a formula was authored — DERIVED from evidence, never taken from what a candidate called itself.

▲ **THE MISTAKE THIS MODULE EXISTS TO PREVENT.** A candidate's `generation_source` says where the
IDEA came from — `recipe`, `llm_intent`, `user_defined`. It does NOT say how the FORMULA was
written, and the two come apart immediately: `formula_draft_worker` ALWAYS drives the LLM author and
critic, so a feature a user picked from a RECIPE recommendation is nonetheless LLM-authored. Choosing
a production certificate from the origin would hand such a feature a recipe-compiler certificate that
never covered how it was actually written — a governance hole wearing the costume of a sensible
default.

So the method is derived from what the RUN LEFT BEHIND:

* ``LLM_AUTHORED`` — the run's author and critic provider calls are present and strictly reconciled.
  Reconciled, not merely present: an unreconciled dispatch cannot say what was sent to the provider,
  so it cannot establish that a provider authored anything.
* ``REVIEWED_RECIPE_BLUEPRINT`` — the trace carries ``REVIEW_BYPASSED`` (the stage a reviewed
  blueprint takes INSTEAD of a critic turn), naming the reviewed expectation it was validated
  against.

Anything else — both kinds of evidence, or neither — is **refused**. A published number whose
authoring method is ambiguous cannot be certified, and guessing between two methods is precisely the
guess that produces the wrong certificate.

The second half of this module is the WRITER: :func:`derive_member_provenance` turns one artifact's
members into derived rows and :func:`record_member_provenance` stores them, both called from
:func:`~featuregen.materialize.seal_v2.seal_v2` so that how a sealed feature was authored is decided
by the same act that seals it. See those functions for why derivation happens BEFORE anything is
written and why a member that cannot be derived stops the whole seal.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from featuregen.canonical import jcs_sha256

#: Reconciled author AND critic provider calls.
LLM_AUTHORED = "LLM_AUTHORED"
#: A REVIEW_BYPASSED trace naming the reviewed blueprint's expectation.
REVIEWED_RECIPE_BLUEPRINT = "REVIEWED_RECIPE_BLUEPRINT"


class AuthoringMethodUndecidable(RuntimeError):
    """The evidence does not establish ONE authoring method.

    Raised rather than returned as an "unknown" method, because every caller of this module is about
    to decide whether something may be published in production. An unknown that can be passed along
    becomes an unknown that is later treated as benign; a refusal cannot.
    """


@dataclass(frozen=True, slots=True)
class AuthoringProvenanceV1:
    """One member's derived method, and the evidence it was derived from.

    `evidence_hash` is what makes the method CHECKABLE rather than asserted: a reader re-derives it
    from the same run and compares. A method with no re-derivable basis is a claim.
    """

    authoring_method: str
    authoring_run_id: str
    evidence_hash: str
    #: What the derivation actually observed, for the refusal message and for the hash.
    evidence: dict[str, object]


def derive_authoring_method(conn, authoring_run_id: str) -> AuthoringProvenanceV1:
    """Decide how this run authored its formula, from the run's own durable evidence.

    Refuses when the evidence names both methods or neither. Never falls back to a default: the
    default would be applied exactly when the truth is unclear, which is when it matters most.
    """
    from featuregen.overlay.upload.dispatch_audit import formula_dispatches_reconciled
    from featuregen.overlay.upload.recipe_formula_eval import _dispatch_identity

    author_refs, critic_refs, _llm_refs = _dispatch_identity(conn, authoring_run_id)
    reconciled = formula_dispatches_reconciled(conn, authoring_run_id)
    llm_evidence = bool(author_refs) and bool(critic_refs) and reconciled

    bypass = conn.execute(
        "SELECT count(*) FROM formula_authoring_trace_event "
        " WHERE authoring_run_id = %s AND stage = 'REVIEW_BYPASSED'",
        (authoring_run_id,)).fetchone()[0]
    blueprint_evidence = bypass > 0

    evidence: dict[str, object] = {
        "authoring_run_id": authoring_run_id,
        "author_dispatch_count": len(author_refs),
        "critic_dispatch_count": len(critic_refs),
        "dispatches_reconciled": reconciled,
        "review_bypassed_events": bypass,
    }

    # ▲ RAW DISPATCH PRESENCE, not the reconciled-gated boolean, and the difference is a
    # mis-certification. A run whose dispatches did not RECONCILE still ATTEMPTED a provider;
    # pairing that with a reviewed bypass is the clearest case there is of a trace disagreeing with
    # itself. Gating this check on `llm_evidence` — which requires reconciliation — let exactly that
    # combination fall THROUGH to `REVIEWED_RECIPE_BLUEPRINT` below: the STRONGEST method claim,
    # minted from the WEAKEST evidence, into an append-only table nothing can correct afterwards.
    #
    # Unreachable while no production caller supplies `reviewed_blueprint` — which is why it was
    # never seen. The deterministic lane is what makes both paths live at once, so the guard is
    # widened BEFORE that lane exists rather than after it seals something.
    provider_attempted = bool(author_refs) or bool(critic_refs)
    if provider_attempted and blueprint_evidence:
        # BOTH is not "probably the LLM one". Picking either method would certify against evidence
        # that contradicts the choice.
        raise AuthoringMethodUndecidable(
            f"authoring run {authoring_run_id} carries evidence of BOTH provider authoring "
            f"({'reconciled' if reconciled else 'UNRECONCILED'}) and a reviewed-blueprint bypass; "
            f"its trace disagrees with itself and no certificate can be chosen from it: "
            f"{evidence}")
    if llm_evidence:
        method = LLM_AUTHORED
    elif blueprint_evidence:
        method = REVIEWED_RECIPE_BLUEPRINT
    else:
        raise AuthoringMethodUndecidable(
            f"authoring run {authoring_run_id} establishes no authoring method — no reconciled "
            f"author+critic provider calls and no REVIEW_BYPASSED stage. An unreconciled dispatch "
            f"cannot say what was sent to the provider, so it cannot evidence that one authored "
            f"anything: {evidence}")

    return AuthoringProvenanceV1(
        authoring_method=method, authoring_run_id=authoring_run_id,
        evidence_hash=jcs_sha256({**evidence, "authoring_method": method}), evidence=evidence)


# ══ the WRITER: one honest row per member, decided by the act that seals ═════════════════════════


class MemberProvenanceRefused(RuntimeError):
    """A member's authoring provenance could not be established, so nothing may be sealed.

    Distinct from :class:`AuthoringMethodUndecidable`, which is about ONE run's evidence. This one
    is about an ARTIFACT: it names the member, and it is what a caller catches to refuse the build.
    A seal that continued past it would produce an artifact carrying a published number whose
    authoring method nobody can name — and the production gate reads a missing row as "nothing to
    check", which is the one reading that must never be reachable by accident.
    """


@dataclass(frozen=True, slots=True)
class MemberAuthoringInputV1:
    """What the SEALING CALLER knows about one member, and nothing it would have to guess.

    Deliberately carries no ``authoring_method``. The method is DERIVED here from the run's own
    evidence (see the module docstring); a field for it would be a place for a caller to assert one,
    and an asserted method is exactly what this table exists to make impossible.

    ``formula_draft_id`` is optional because the column is: a run may be the anchor without a draft
    row in some lanes. ``selection_revision_id``, ``authoring_run_id`` and ``formula_content_hash``
    are not — they are what makes the row tie back to a person's choice, to the act that authored
    it, and to the bytes this artifact actually carries.
    """

    #: The published feature name within the artifact. Must be one of the columns the sealed graphs
    #: publish — `seal_v2` checks that rather than trusting it, because a row filed under a name the
    #: artifact does not publish describes nothing.
    member_name: str
    selection_revision_id: str
    authoring_run_id: str
    formula_content_hash: str
    formula_draft_id: str | None = None


@dataclass(frozen=True, slots=True)
class DerivedMemberProvenanceV1:
    """One member's facts, paired with the method DERIVED from its run's evidence."""

    member: MemberAuthoringInputV1
    provenance: AuthoringProvenanceV1


def derive_member_provenance(
    conn, members: Iterable[MemberAuthoringInputV1],
) -> tuple[DerivedMemberProvenanceV1, ...]:
    """Derive every member's authoring method, or refuse — reading only, writing nothing.

    **Called BEFORE anything about the artifact is stored.** A refusal here must leave no manifest,
    no artifact row and no provenance row behind, and the only way to guarantee that is to ask the
    question before the first write rather than to undo the writes afterwards. It is the same rule
    ``store_manifest`` already follows for bytes: the one point where the mistake is still
    recoverable is before anything is recorded.

    **Every member, or none.** A member whose run cannot say how it was authored refuses the WHOLE
    derivation. Sealing the others and skipping it would produce an artifact that publishes a column
    with no provenance row — and a later reader cannot tell that from an artifact whose provenance
    was never written at all.

    Raises:
        MemberProvenanceRefused: a member names no selection, no run or no formula hash; two members
            share a name; or a member's evidence does not establish one authoring method.
    """
    derived: list[DerivedMemberProvenanceV1] = []
    seen: set[str] = set()
    for member in members:
        for field, value in (("member_name", member.member_name),
                             ("selection_revision_id", member.selection_revision_id),
                             ("authoring_run_id", member.authoring_run_id),
                             ("formula_content_hash", member.formula_content_hash)):
            # BLANK IS NOT MISSING-AND-FINE. Every one of these answers a different question — which
            # column, whose choice, which authoring act, which bytes — and a blank makes its question
            # unanswerable while leaving a row that looks recorded.
            if not str(value or "").strip():
                raise MemberProvenanceRefused(
                    f"member {member.member_name!r} supplies a blank {field}: the provenance row "
                    f"would record a fact nobody can follow back, which reads as evidence while "
                    f"being none")
        if member.member_name in seen:
            raise MemberProvenanceRefused(
                f"member {member.member_name!r} is supplied twice: 'how was this authored' would "
                f"be a question with two answers, and the table's key forbids the second")
        seen.add(member.member_name)

        try:
            provenance = derive_authoring_method(conn, member.authoring_run_id)
        except AuthoringMethodUndecidable as exc:
            # ▲ THE REFUSAL NAMES THE MEMBER. `derive_authoring_method` knows only a run id; an
            # operator reading this needs to know which published column is affected, because that
            # is what they have to go and look at.
            raise MemberProvenanceRefused(
                f"member {member.member_name!r} (selection {member.selection_revision_id}) cannot "
                f"establish how its formula was authored, so no production certificate could be "
                f"chosen for it and nothing may be sealed: {exc}") from exc
        derived.append(DerivedMemberProvenanceV1(member=member, provenance=provenance))
    return tuple(derived)


def record_member_provenance(
    conn, artifact_id: str, derived: Sequence[DerivedMemberProvenanceV1],
) -> None:
    """Write one provenance row per member, then PROVE the stored rows are the ones described.

    ``ON CONFLICT DO NOTHING`` makes re-sealing an artifact safe — the seal itself is idempotent and
    a redelivered job must not fail on the second insert. It also makes MIS-sealing silent: a second
    call naming a different run for the same member would be ignored, and this function would return
    as though it had recorded that run. So the insert is followed by a read-back, exactly as
    ``seal_v2._require_identity_unchanged`` does for the artifact's own identity, and the first
    stored row that disagrees is named on both sides.

    The table is append-only by trigger, so a disagreement cannot be resolved by overwriting: it is
    reported, and the two callers who disagree about how a published feature was authored find out
    rather than one of them silently winning.
    """
    for item in derived:
        conn.execute(
            "INSERT INTO sealed_artifact_member_provenance ("
            "artifact_id, member_name, selection_revision_id, formula_draft_id, authoring_run_id, "
            "formula_content_hash, authoring_method, authoring_evidence_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (artifact_id, member_name) DO NOTHING",
            (artifact_id, item.member.member_name, item.member.selection_revision_id,
             item.member.formula_draft_id,
             # ALWAYS the run the method was derived FROM, for both methods. The column is nullable
             # only because a future lane may have none; leaving it null for a reviewed blueprint
             # would discard the one thing that makes the derivation repeatable.
             item.provenance.authoring_run_id,
             item.member.formula_content_hash,
             item.provenance.authoring_method, item.provenance.evidence_hash))

    stored = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT member_name, selection_revision_id, formula_draft_id, authoring_run_id, "
            "formula_content_hash, authoring_method, authoring_evidence_hash "
            "FROM sealed_artifact_member_provenance WHERE artifact_id = %s",
            (artifact_id,)).fetchall()}
    for item in derived:
        expected = (item.member.selection_revision_id, item.member.formula_draft_id,
                    item.provenance.authoring_run_id, item.member.formula_content_hash,
                    item.provenance.authoring_method, item.provenance.evidence_hash)
        actual = stored.get(item.member.member_name)
        if actual is None:                    # pragma: no cover — the insert above guarantees it
            raise MemberProvenanceRefused(
                f"the provenance row for {item.member.member_name!r} of artifact {artifact_id!r} "
                f"vanished between its insert and its read-back")
        if tuple(actual) != expected:
            raise MemberProvenanceRefused(
                f"artifact {artifact_id!r} already records member {item.member.member_name!r} as "
                f"{tuple(actual)!r} and this seal describes {expected!r}. How a published feature "
                f"was authored is a fact about a past act: two answers means the two callers "
                f"disagree about which act produced it, and the stored one cannot be edited")


@dataclass(frozen=True, slots=True)
class SealedMemberProvenanceV1:
    """One stored row, for a reader asking how a member of a sealed artifact was authored."""

    member_name: str
    selection_revision_id: str
    formula_draft_id: str | None
    authoring_run_id: str | None
    formula_content_hash: str
    authoring_method: str
    authoring_evidence_hash: str


def member_provenance_of(conn, artifact_id: str) -> tuple[SealedMemberProvenanceV1, ...]:
    """How every member of an artifact was authored, from the store alone.

    The question the production gate asks, answerable from an artifact id and a database — which is
    all a restarted worker holds. An artifact with NO rows answers ``()``, and that is not "nothing
    was wrong": it is "nothing to check", which a gate must read as a refusal rather than as a pass.
    """
    return tuple(
        SealedMemberProvenanceV1(
            member_name=row[0], selection_revision_id=row[1], formula_draft_id=row[2],
            authoring_run_id=row[3], formula_content_hash=row[4], authoring_method=row[5],
            authoring_evidence_hash=row[6])
        for row in conn.execute(
            "SELECT member_name, selection_revision_id, formula_draft_id, authoring_run_id, "
            "formula_content_hash, authoring_method, authoring_evidence_hash "
            "FROM sealed_artifact_member_provenance WHERE artifact_id = %s ORDER BY member_name",
            (artifact_id,)).fetchall())
