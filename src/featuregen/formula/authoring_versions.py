"""C-A6 — the authoring version bundle, and which adapter a stored run may be read by.

**The policy, decided by the product owner (2026-08-16): backward-READABLE, not cross-version
RESUMABLE.**

A run's version bundle is written once, at open, and never rewritten — ``_insert_run`` is
``ON CONFLICT DO NOTHING``, so the manifest a run was opened under is the manifest it keeps. That
makes the bundle a reliable statement of *which software decided this run*, and this module turns
that statement into a routing decision:

============================  ===========================================================
stored bundle + state         adapter
============================  ===========================================================
current + any state           current replay/resume
legacy + terminal             legacy READ-ONLY replay — no provider call, no new events
legacy + incomplete           typed restart requirement (:data:`LEGACY_RESTART_REQUIRED`)
unknown / partial             reconciliation refusal
============================  ===========================================================

**Why an incomplete legacy run may not resume.** Resuming would append new-orchestrator events —
including ``REVIEW_BYPASSED``, a transition the old stage table never allowed — under a manifest
that says version 1 decided this run. The trace would then be a record of two different orchestrators
with nothing marking where one stopped. Restarting is honest; appending is not.

**"Old readers still read old traces" means NEW software supports HISTORICAL traces.** It does not
mean old software must understand new ``REVIEW_BYPASSED`` traces — it cannot, and pretending
otherwise is what would force a rewrite of stored events.

**Partial matches refuse.** A bundle that matches on four keys and differs on one is not "close
enough": the key it differs on is precisely the one that decided something differently, and picking
an adapter by majority vote would run a trace under software that did not write it.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

__all__ = [
    "LEGACY_RESTART_REQUIRED",
    "VERSION_KEYS",
    "BundleClassV2",
    "classify_version_bundle",
    "legacy_bundle_v1",
]

#: The typed reason an incomplete legacy run refuses. A caller that must tell a human what to do
#: needs to distinguish "restart this run" from "the manifest was tampered with", and a bare
#: reconciliation error collapses the two.
LEGACY_RESTART_REQUIRED = "LEGACY_AUTHORING_RUN_RESTART_REQUIRED"

#: The VERSION-bearing keys of the bundle. Deliberately not every key: ``frozen_configuration_hash``
#: is a per-run pin rather than a version, and including it here would classify every run with a
#: different frozen configuration as a different software version. The full-identity comparison that
#: DOES include it still happens, in ``load_verified_checkpoint`` — this function chooses an adapter,
#: it does not replace the identity check.
VERSION_KEYS: tuple[str, ...] = (
    "orchestrator",
    "formula_schema",
    "operation_grammar",
    "critic",
    "disposition",
    "authoring_v2",
    "frozen_configuration_policy",
    # The two axes the manifest gained: what BYTES a proposal hashes to, and what a governed output
    # MEANS. They belong in the classified set — a key the classifier does not compare is a key a
    # run may differ on while still classifying CURRENT, which is the whole failure this tuple
    # exists to prevent.
    "canonicalization",
    "output_policy",
)

#: What the three C-A6 constants were before the bump, plus the schema version that was hardcoded
#: alongside them. Frozen as literals rather than derived from the current constants: this is a
#: statement about bytes already in the database, and it must not move when the constants next do.
_LEGACY_VALUES: Mapping[str, Any] = {
    "orchestrator": 1,
    "disposition": 1,
    "authoring_v2": 1,
    "formula_schema": 2,
}


class BundleClassV2(StrEnum):
    """Which software wrote this run's manifest, as far as this build can tell."""

    CURRENT = "current"
    LEGACY = "legacy"
    UNKNOWN = "unknown"


def _versions_only(bundle: Mapping[str, Any]) -> dict[str, Any]:
    return {key: bundle.get(key) for key in VERSION_KEYS}


def legacy_bundle_v1(current: Mapping[str, Any]) -> dict[str, Any]:
    """The EXACT version bundle this build wrote before C-A6's bump.

    Built by substituting the pre-bump literals into ``current`` so that keys C-A6 did not touch
    (``operation_grammar``, ``critic``, ``frozen_configuration_policy``) are compared at whatever
    value the current build uses — a run that also differs on one of those is genuinely a different
    software version and classifies UNKNOWN, which is the intended answer.
    """
    return {**_versions_only(current), **_LEGACY_VALUES}


def classify_version_bundle(
    stored: Mapping[str, Any] | None, *, current: Mapping[str, Any],
) -> BundleClassV2:
    """Which adapter may read a run opened under ``stored``.

    Args:
        stored: the manifest read back from ``formula_authoring_run.versions``. ``None`` or empty
            classifies UNKNOWN — a run with no manifest states nothing about what decided it.
        current: the bundle this build would write for a new run.

    Returns:
        :class:`BundleClassV2`. Exact match on every version key, or UNKNOWN — see the module
        docstring on why a partial match is not "close enough".
    """
    if not stored:
        return BundleClassV2.UNKNOWN
    stored_versions = _versions_only(stored)
    if stored_versions == _versions_only(current):
        return BundleClassV2.CURRENT
    if stored_versions == legacy_bundle_v1(current):
        return BundleClassV2.LEGACY
    return BundleClassV2.UNKNOWN


#: The COMPLETE identity a run must carry to count as V3 evidence. Every axis, not a date.
#:
#: ▲ WHY IDENTITY AND NOT A COMMIT DATE. Before the contract fix, this platform declared
#: `formula_schema: 3` on runs it drove under the v2 author contract — an instruction that says
#: "MUST declare formula_schema_version 2". Those runs are indistinguishable from real V3 evidence
#: by their schema field alone, and a date cutoff would be a guess about which build wrote them
#: that nothing in the row supports. The tuple below cannot be satisfied by any of them: the
#: orchestrator and disposition versions moved when the V3 state was added, and the author output
#: schema is `formula_author_turn_v3`, which no pre-fix run was ever requested under.
V3_EVIDENCE_IDENTITY: Mapping[str, object] = {
    "formula_schema": 3,
    "orchestrator": 3,
    "disposition": 3,
    "canonicalization": 1,      # CANONICALIZATION_VERSION_V3
    "output_policy": 1,         # OUTPUT_POLICY_VERSION_V2 — v3 reuses v2's output authority
}

#: The author output schema a genuine V3 run was requested under. Checked SEPARATELY from the
#: version bundle because it does not live there: it is recorded by the audited seam and hashed into
#: a frozen provider contract, and it is the single axis that no pre-fix run can fake — those runs
#: were physically requested under `formula_author_turn_v2`.
V3_AUTHOR_OUTPUT_SCHEMA_ID = "formula_author_turn_v3"
#: Its registered version. Checked alongside the id: a future v3 turn schema v2 would
#: be a different contract requested under the same name.
V3_AUTHOR_OUTPUT_SCHEMA_VERSION = 1

#: The PROMPT identity a genuine V3 run was authored under. Checked beside the schema:
#: the schema is what the answer was validated against, the prompt is what the model was
#: TOLD to produce, and a run can be wrong about either one independently.
V3_AUTHOR_PROMPT_ID = "formula_author_turn_v3"
V3_AUTHOR_PROMPT_VERSION = 1


def qualifies_as_v3_evidence(stored: Mapping[str, object] | None) -> tuple[bool, tuple[str, ...]]:
    """The MANIFEST half of the question. **Not the authority — see
    :func:`qualifies_as_v3_evidence_for_run`, which is.**

    ▲ THIS FUNCTION CANNOT SEE THE PROVIDER CONTRACT, and there is an interval where that matters.
    The version constants moved to 3 in `b5249e80`; the author turn contract only became v3 in
    `027cc923`. A run authored between those two commits carries a manifest that satisfies every
    axis below while having been physically driven under `formula_author_turn_v2` — asked for a v2
    proposal, and given one. The output schema id lives in the dispatch evidence, not in `versions`,
    so no reading of the manifest alone can exclude it.

    Kept as a separate function because it is genuinely useful — it is the cheap filter, and it
    names the disagreeing axes — but a caller that treats it as an activation authority will admit
    exactly the runs this whole correction exists to exclude.

    Returns ``(ok, missing_axes)``.

    The axes that DISAGREE are returned rather than a bare False, because "this run is not V3
    evidence" and "this run is not V3 evidence BECAUSE its orchestrator is 2" send an operator to
    different places — the first to guess, the second to the regeneration list.

    A run with no manifest qualifies for nothing: a run that states nothing about what decided it
    cannot be shown to be anything.
    """
    if not stored:
        return False, ("<no manifest>",)
    disagreeing = tuple(
        f"{axis}={stored.get(axis)!r} (expected {expected!r})"
        for axis, expected in sorted(V3_EVIDENCE_IDENTITY.items())
        if stored.get(axis) != expected)
    return (not disagreeing), disagreeing


def qualifies_as_v3_evidence_for_run(conn, authoring_run_id: str) -> tuple[bool, tuple[str, ...]]:
    """THE authority: does this run carry the complete V3 identity, checked against the DATABASE?

    Returns ``(ok, disagreements)``. The manifest half is necessary and not sufficient — see
    :func:`qualifies_as_v3_evidence` for the interval it cannot see — so this adds the facts that
    live outside ``versions``:

    * every AUTHOR provider call was requested under ``formula_author_turn_v3`` at the expected
      schema version. This is the axis no pre-fix run can fake: those runs were PHYSICALLY
      requested under `formula_author_turn_v2`, and the id is recorded by the audited seam at the
      moment of the call rather than declared afterwards;
    * the run reached a terminal ``completed`` event carrying a proposal;
    * that proposal declares schema 3, and the manifest agrees with it.

    A run with no author call at all does NOT qualify. That is deliberate rather than lenient: the
    deterministic producer authors without a provider, and its evidence is a different thing from a
    provider-authored run — an evaluator that accepted both under one contract would be reporting
    two populations as one.
    """
    row = conn.execute(
        "SELECT versions FROM formula_authoring_run WHERE authoring_run_id = %s",
        (authoring_run_id,)).fetchone()
    ok, disagreements = qualifies_as_v3_evidence(row[0] if row else None)
    problems = list(disagreements)

    # ── the author calls, as they were actually REQUESTED ────────────────────────────────────────
    # The task is matched EXACTLY, never by `LIKE '%author%'`: a pattern also matches whatever task
    # named "reauthor" or "author_review" exists next, and this is the check deciding whether a run
    # may be certified as V3 evidence.
    from featuregen.formula.author import AUTHOR_TASK

    calls = conn.execute(
        "SELECT DISTINCT c.output_schema_id, c.output_schema_version, c.prompt_id, "
        "       c.prompt_version "
        "  FROM formula_authoring_trace_event e "
        "  JOIN llm_call c ON c.llm_call_ref = e.llm_call_ref "
        " WHERE e.authoring_run_id = %s AND e.llm_call_ref IS NOT NULL AND c.task = %s",
        (authoring_run_id, AUTHOR_TASK)).fetchall()
    if not calls:
        problems.append(
            f"<no {AUTHOR_TASK} provider call> (expected one requested under "
            f"{V3_AUTHOR_OUTPUT_SCHEMA_ID!r})")
    for schema_id, schema_version, prompt_id, prompt_version in calls:
        # The SCHEMA is what the answer was validated against; the PROMPT is what the model was
        # told to produce. A run held to the v3 shape under v2's instruction, or the reverse, is
        # not a v3 run — and only checking one of them would miss exactly that.
        if schema_id != V3_AUTHOR_OUTPUT_SCHEMA_ID:
            problems.append(
                f"author output_schema_id={schema_id!r} "
                f"(expected {V3_AUTHOR_OUTPUT_SCHEMA_ID!r})")
        if schema_version != V3_AUTHOR_OUTPUT_SCHEMA_VERSION:
            problems.append(
                f"author output_schema_version={schema_version!r} "
                f"(expected {V3_AUTHOR_OUTPUT_SCHEMA_VERSION!r})")
        if prompt_id != V3_AUTHOR_PROMPT_ID:
            problems.append(f"author prompt_id={prompt_id!r} (expected {V3_AUTHOR_PROMPT_ID!r})")
        if prompt_version != V3_AUTHOR_PROMPT_VERSION:
            problems.append(
                f"author prompt_version={prompt_version!r} "
                f"(expected {V3_AUTHOR_PROMPT_VERSION!r})")

    # ── and the artifact the run actually produced ───────────────────────────────────────────────
    # PARSED, not string-compared. `"formula_schema_version": 3` in the stored JSON is a claim; a
    # payload that says 3 and does not satisfy the v3 grammar is not v3 evidence, and the whole
    # reason this qualifier exists is that a declared version and the thing itself can disagree.
    produced = conn.execute(
        "SELECT payload->'result'->'candidate_proposal' "
        "  FROM formula_authoring_trace_event "
        " WHERE authoring_run_id = %s AND kind = 'completed'",
        (authoring_run_id,)).fetchone()
    if produced is None or produced[0] is None:
        problems.append("<no terminal proposal> (a run that produced nothing evidences nothing)")
    else:
        problems.extend(_v3_parse_problems(produced[0]))

    return (not problems), tuple(problems)


def _v3_parse_problems(raw: Mapping[str, object]) -> tuple[str, ...]:
    """``()`` if ``raw`` genuinely parses as a V3 proposal, else what is wrong with it."""
    from featuregen.formula.parse_v2 import parse_versioned
    from featuregen.formula.schema_leaves import SchemaError
    from featuregen.formula.schema_v3 import TypedFormulaProposalV3

    declared = raw.get("formula_schema_version")
    if declared != 3:
        return (f"produced proposal formula_schema_version={declared!r} (expected 3)",)
    try:
        parsed = parse_versioned(raw)
    except SchemaError as exc:
        return (f"produced proposal declares 3 but does not parse as v3: {exc}",)
    if not isinstance(parsed, TypedFormulaProposalV3):
        return (f"produced proposal declares 3 but parses as {type(parsed).__name__}",)
    return ()
