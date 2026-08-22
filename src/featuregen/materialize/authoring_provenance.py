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
"""
from __future__ import annotations

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

    if llm_evidence and blueprint_evidence:
        # BOTH is not "probably the LLM one". A run that drove a provider AND recorded a reviewed
        # bypass is a run whose own trace disagrees with itself, and picking either method would
        # certify against evidence that contradicts the choice.
        raise AuthoringMethodUndecidable(
            f"authoring run {authoring_run_id} carries evidence of BOTH reconciled provider "
            f"authoring and a reviewed-blueprint bypass; its trace disagrees with itself and no "
            f"certificate can be chosen from it: {evidence}")
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
