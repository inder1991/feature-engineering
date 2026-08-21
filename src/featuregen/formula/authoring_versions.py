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


def qualifies_as_v3_evidence(stored: Mapping[str, object] | None) -> tuple[bool, tuple[str, ...]]:
    """Does this run's manifest carry the complete V3 identity? Returns ``(ok, missing_axes)``.

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
