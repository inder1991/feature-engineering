"""Spec §1.2 — Gate 1: admit an AUTHORED artifact, or refuse it.

THE ATTACK THIS GATE EXISTS TO STOP. :class:`~featuregen.formula.result.AuthoringResult` is a
publicly constructible frozen dataclass. A caller can fabricate one that claims
``authoring_disposition="RESOLVED"``, attach any :class:`~featuregen.formula.schema.TypedFormulaV1`
it likes, set ``candidate_formula_hash`` to whatever makes that formula look authored, and cite a
LEGITIMATE ``authoring_run_id``. Every field of such an object agrees with every other field, so an
in-memory consistency check proves exactly nothing.

So nothing here trusts the supplied object. Every check is against the run's **immutable terminal
trace event** (``formula.trace.read_terminal_event``) and its **write-once manifest**
(``read_run_intent_hash``) — rows migration 1020 physically forbids updating, deleting or
truncating, carrying a canonical payload and the sha256 ``payload_hash`` that makes it
tamper-evident. The supplied result contributes exactly one thing the trace does not hold: the
formula OBJECT itself, whose content hash must equal the one the run recorded.

THE SIX CHECKS, IN THIS ORDER (spec §1.2). Order is load-bearing — each later check is meaningful
only once the earlier ones have established that there IS an authoritative record and that it says
what it appears to say:

1. a terminal event exists                       → else ``AUTHORING_RUN_INCOMPLETE``
2. ``payload_hash`` validates the payload        → else ``TERMINAL_PAYLOAD_TAMPERED``
3. the payload's disposition is ``RESOLVED``     → else ``NOT_RESOLVED``
4. the payload's ``candidate_formula_hash`` equals ``formula_content_hash`` of the SUPPLIED formula
                                                 → else ``FORMULA_HASH_MISMATCH``
5. the supplied six §F axes equal the payload's  → else ``AXES_MISMATCH``
6. ``authoring_intent_hash(intent)`` equals the run's recorded ``intent_hash``
                                                 → else ``INTENT_HASH_MISMATCH``

⚠️ **CHECK 3 READS THE PAYLOAD, NOT THE EVENT KIND.** ``authoring._TERMINAL_FOR_DISPOSITION`` maps
ONLY ``TECHNICAL_FAILURE`` to ``FAILED``, so ``REJECTED`` and ``UNSUPPORTED`` runs ALSO write a
``COMPLETED`` event. Admitting on "a COMPLETED event exists" would admit rejected formulas — the
single highest-consequence mistake available at this boundary.

⚠️ **CHECK 4 RE-DERIVES THE HASH.** ``result.candidate_formula_hash`` is a field on the forgeable
object and is never read here; the digest is recomputed from ``result.candidate_formula`` with the
same ``formula.canonical.formula_content_hash`` that ``derive_disposition`` used.

WHAT GATE 1 DOES **NOT** DO. It cannot authorize reads: availability columns, join hops, bridge
tables and the spine are only discovered during compilation, so authorization is Gate 2 (§1.3),
group-wide, after the IR is complete. It also does not re-run authoring, re-resolve output authority
or re-validate the formula — those verdicts are the authoring run's, and this gate's only job is to
establish that the artifact in hand IS that run's verdict.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.contracts.db import DbConn

# Imported helpers are bound PRIVATELY (``_name`` / a module alias) on purpose: they are this
# module's implementation, not part of its API, and ``test_admission.py``'s
# ``test_no_function_accepts_a_bare_formula`` introspects every PUBLIC function of this module. A
# re-exported helper would be swept into that check, so an unrelated rename upstream could turn the
# gate's bypass guard red — or, worse, a helper that really did take a bare formula could be
# re-exported here and quietly become a public entry point.
from featuregen.formula import trace as _trace
from featuregen.formula.authoring import authoring_intent_hash as _authoring_intent_hash
from featuregen.formula.canonical import formula_content_hash as _formula_content_hash
from featuregen.formula.result import AuthoringResult
from featuregen.formula.schema import SchemaError, TypedFormulaV1
from featuregen.formula.turns import AuthoringIntent
from featuregen.materialize.canonical import materialize_hash as _materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

__all__ = [
    "AdmittedFeature",
    "FeatureNamePlanError",
    "ResolvedFeatureInput",
    "admit_artifacts",
]

#: The ONE disposition that may be materialized. NEEDS_REVIEW is not a near-miss: it means a human
#: verdict is outstanding, and computing a feature from it would decide that review by default.
_RESOLVED = "RESOLVED"

#: The six §F axes, exactly as ``result.AuthoringAxes`` names them and ``authoring._finish`` writes
#: them into the terminal payload. ONE list, so the two sides of check 5 cannot drift apart.
_AXIS_FIELDS: tuple[str, ...] = (
    "structural_status",
    "capability_status",
    "output_status",
    "expectation_status",
    "critic_status",
    "technical_status",
)

#: A Hive identifier: lower-case ASCII, leading letter, ``_`` and digits thereafter, <= 128 chars
#: (Hive's column-name bound). Anything outside is folded to ``_`` by :func:`_hive_identifier`.
_HIVE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_NON_HIVE_CHARS = re.compile(r"[^a-z0-9_]")


class FeatureNamePlanError(Exception):
    """A feature NAME the plan cannot express as a distinct Hive column — a PLAN error.

    Deliberately NOT a :class:`~featuregen.materialize.codes.MaterializationRefused`, and
    deliberately not one of the §14 codes: the closed vocabulary has no member for it because it is
    not a governed verdict about an artifact. It is the same class of failure as
    ``canonical.materialize_hash``'s ``TypeError`` on a non-mapping — a defect at the call site,
    where the caller assembled the batch. Spec §1.2 says so in as many words: *"a post-normalization
    collision within a group is a plan error, never a silent overwrite."* Raising it loudly is the
    point; the alternative is two features quietly writing one column.
    """


@dataclass(frozen=True, slots=True)
class ResolvedFeatureInput:
    """The ONLY public input to materialization (spec §1.1).

    ``intent`` carries the feature NAME — ``AuthoringResult`` does not (reference §6) — and is the
    object check 6 re-hashes, so it must be THE intent the run was opened for, not a look-alike
    rebuilt from its parts. ``result`` already carries ``authoring_run_id``, so no separate run-id
    field exists to disagree with it."""

    intent: AuthoringIntent
    result: AuthoringResult


@dataclass(frozen=True, slots=True)
class AdmittedFeature:
    """One authored artifact whose provenance has been PROVEN against the immutable trace.

    Downstream compilation consumes only this type, never a ``ResolvedFeatureInput`` and never a
    bare formula: carrying the ``authoring_run_id`` and the verified ``formula_content_hash``
    alongside the formula is what lets any later stage re-derive the same proof instead of trusting
    that someone upstream performed it."""

    feature_name: str
    formula: TypedFormulaV1
    formula_content_hash: str
    intent: AuthoringIntent
    authoring_run_id: str


def admit_artifacts(
    conn: DbConn, inputs: Iterable[ResolvedFeatureInput]
) -> tuple[AdmittedFeature, ...]:
    """Admit every input, in order, or refuse the WHOLE batch (spec §1.2).

    There is no partial admission: the first refusal raises, so a batch either yields an
    ``AdmittedFeature`` per input or yields nothing. A caller that admitted the survivors of a
    refused batch would be compiling a group whose membership nobody decided.

    Raises:
        MaterializationRefused: one of the six §1.2 checks failed, carrying the matching
            ``CompilationRefusalCode``.
        FeatureNamePlanError: two inputs normalize to one Hive identifier, or a name cannot be
            expressed as one at all — a plan error, not a governed refusal.
    """
    admitted = tuple(_admit_one(conn, item) for item in inputs)
    _reject_name_collisions(admitted)
    return admitted


def _admit_one(conn: DbConn, item: ResolvedFeatureInput) -> AdmittedFeature:
    """The six checks, in spec order, for ONE supplied artifact."""
    result = item.result
    run_id = result.authoring_run_id

    event = _terminal_event(conn, run_id)                                # 1
    _verify_payload_hash(event, run_id)                                  # 2
    _require_resolved(event, run_id)                                     # 3
    formula, content_hash = _verify_formula_hash(event, result, run_id)  # 4
    _verify_axes(event, result, run_id)                                  # 5
    _verify_intent_hash(conn, item.intent, run_id)                       # 6

    return AdmittedFeature(
        feature_name=_hive_identifier(item.intent.name),
        formula=formula,
        formula_content_hash=content_hash,
        intent=item.intent,
        authoring_run_id=run_id,
    )


# ── 1. a terminal event exists ───────────────────────────────────────────────────────────────────

def _terminal_event(conn: DbConn, run_id: str) -> _trace.TerminalEvent:
    """The run's terminal event, or ``AUTHORING_RUN_INCOMPLETE``.

    Absence is derived, never asserted: a live run, a process that died mid-authoring, a run whose
    manifest never committed, and a run id that names nothing all read the same way, and all mean
    the same thing here — no authoring verdict exists to admit."""
    event = _trace.read_terminal_event(conn, run_id)
    if event is None:
        raise MaterializationRefused(
            CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE,
            f"authoring run {run_id} has no terminal trace event",
        )
    return event


# ── 2. the payload validates against its hash ────────────────────────────────────────────────────

def _verify_payload_hash(event: _trace.TerminalEvent, run_id: str) -> None:
    """Recompute the sha256 over the payload's RFC 8785 bytes and compare (``TERMINAL_PAYLOAD_TAMPERED``).

    ``materialize_hash`` is byte-identical in construction to what ``trace.append_event`` computed
    (``sha256`` of ``_jcs.dumps`` over the plain payload dict) — the equality is pinned by
    ``tests/featuregen/formula/test_trace_reader.py``. Using the package's ONE hasher here rather
    than a private second copy is what keeps that equality checkable.

    Migration 1020 makes the row physically immutable, so a mismatch means the stored bytes were
    altered out of band (a direct write as a superuser, a restore from doctored bytes). Refusing is
    the only safe reading: every later check reads this same payload."""
    if _materialize_hash(event.payload) != event.payload_hash:
        raise MaterializationRefused(
            CompilationRefusalCode.TERMINAL_PAYLOAD_TAMPERED,
            f"the terminal {event.kind.value} event of authoring run {run_id} does not match its "
            "recorded payload_hash",
        )


# ── 3. the PAYLOAD's disposition is RESOLVED ─────────────────────────────────────────────────────

def _require_resolved(event: _trace.TerminalEvent, run_id: str) -> None:
    """``NOT_RESOLVED`` unless the payload says ``RESOLVED``.

    ⚠️ The event KIND is not consulted, and must not be: only ``TECHNICAL_FAILURE`` writes
    ``FAILED``, so a ``REJECTED`` or ``UNSUPPORTED`` run also closes with ``COMPLETED``. A missing
    or non-string disposition is refused for the same reason — an unreadable verdict is not a
    permissive one."""
    disposition = event.payload.get("authoring_disposition")
    if disposition != _RESOLVED:
        raise MaterializationRefused(
            CompilationRefusalCode.NOT_RESOLVED,
            f"the terminal {event.kind.value} event of authoring run {run_id} records "
            f"authoring_disposition={disposition!r}, not {_RESOLVED}",
        )


# ── 4. the recorded candidate hash equals the supplied formula's ─────────────────────────────────

def _verify_formula_hash(
    event: _trace.TerminalEvent, result: AuthoringResult, run_id: str
) -> tuple[TypedFormulaV1, str]:
    """Return the supplied formula and its content hash, or refuse with ``FORMULA_HASH_MISMATCH``.

    THE forgery check. The digest is recomputed from ``result.candidate_formula``; the
    ``candidate_formula_hash`` field of the supplied object is never read, because a forger sets it
    to whatever makes the pair look consistent.

    Three ways to fail, one code: no formula at all (a result citing a resolved run must carry the
    artifact that run produced), a formula that cannot be canonicalized (``SchemaError`` — an
    uncanonicalizable object has no content identity, so it cannot be the recorded one, and §14
    forbids letting that surface as a bare exception), and a formula whose digest simply differs."""
    recorded = event.payload.get("candidate_formula_hash")
    formula = result.candidate_formula
    if formula is None:
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_HASH_MISMATCH,
            f"the result supplied for authoring run {run_id} carries no candidate_formula, but its "
            f"terminal event records candidate_formula_hash={recorded!r}",
        )
    try:
        supplied = _formula_content_hash(formula)
    except SchemaError as exc:
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_HASH_MISMATCH,
            f"the formula supplied for authoring run {run_id} cannot be canonicalized, so it has "
            f"no content identity to compare with candidate_formula_hash={recorded!r}: {exc}",
        ) from exc
    if supplied != recorded:
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_HASH_MISMATCH,
            f"the formula supplied for authoring run {run_id} hashes to {supplied}, but its "
            f"terminal event records candidate_formula_hash={recorded!r}",
        )
    return formula, supplied


# ── 5. the six axes agree with the payload ───────────────────────────────────────────────────────

def _verify_axes(event: _trace.TerminalEvent, result: AuthoringResult, run_id: str) -> None:
    """``AXES_MISMATCH`` when any of the six §F axes disagrees with the recorded payload.

    Not redundant with check 3, which reads only the FOLDED disposition: the axes are the evidence
    the fold was performed over, and a result whose axes were rewritten under a genuine RESOLVED
    disposition is misreporting WHY the feature was admitted (a ``blocking`` critic quietly relabelled
    ``clean``, say). Everything a later stage records about this feature's provenance comes from the
    supplied object, so the two records must be the same record."""
    differing = tuple(
        field for field in _AXIS_FIELDS
        if getattr(result, field) != event.payload.get(field)
    )
    if differing:
        detail = ", ".join(
            f"{field}: supplied {getattr(result, field)!r} != recorded "
            f"{event.payload.get(field)!r}"
            for field in differing
        )
        raise MaterializationRefused(
            CompilationRefusalCode.AXES_MISMATCH,
            f"the result supplied for authoring run {run_id} disagrees with its terminal event on "
            f"{len(differing)} of {len(_AXIS_FIELDS)} axes ({detail})",
        )


# ── 6. the intent is the one the run was opened for ──────────────────────────────────────────────

def _verify_intent_hash(conn: DbConn, intent: AuthoringIntent, run_id: str) -> None:
    """``INTENT_HASH_MISMATCH`` unless the intent re-hashes to the run manifest's ``intent_hash``.

    The manifest is written BEFORE any provider call and is write-once, so it is the immutable
    record of what was asked. An absent manifest fails CLOSED: a run whose intent nothing recorded
    cannot have its intent proven.

    ⚠️ SCOPE, VERIFIED (``authoring.authoring_intent_hash``, ``authoring.py:253``): the digest covers
    ``name``, ``hypothesis``, ``target_entity`` and ``target_grain_keys`` ONLY. It does NOT cover
    ``AuthoringIntent.recipe_authoring_context``, so this check cannot tell two intents apart when
    they differ only in that field. Recorded rather than worked around — narrowing what a governed
    check proves would be a change to ``authoring``'s identity contract, not to this gate."""
    recorded = _trace.read_run_intent_hash(conn, run_id)
    supplied = _authoring_intent_hash(intent)
    if recorded != supplied:
        raise MaterializationRefused(
            CompilationRefusalCode.INTENT_HASH_MISMATCH,
            f"the intent supplied for authoring run {run_id} hashes to {supplied}, but the run's "
            f"manifest records intent_hash={recorded!r}",
        )


# ── the feature name (spec §1.2, final paragraph) ────────────────────────────────────────────────

def _hive_identifier(name: str) -> str:
    """``intent.name`` folded to a Hive identifier — the physical column the feature will occupy.

    Deterministic and conservative: NFKC-normalize, strip, lower-case, and map every character Hive
    does not accept in an unquoted identifier to ``_``. Nothing is collapsed or truncated, because
    both would map two distinct names onto one column — the very thing the collision check exists to
    prevent. A name that cannot be expressed at all (empty, not starting with a letter, longer than
    Hive's 128-character bound) is a plan error, not a name to invent a mangling for."""
    folded = _NON_HIVE_CHARS.sub("_", unicodedata.normalize("NFKC", name).strip().lower())
    if not _HIVE_IDENTIFIER.fullmatch(folded):
        raise FeatureNamePlanError(
            f"feature name {name!r} does not normalize to a Hive identifier "
            f"(got {folded!r}: it must start with a letter and be at most 128 characters of "
            "[a-z0-9_])"
        )
    return folded


def _reject_name_collisions(admitted: tuple[AdmittedFeature, ...]) -> None:
    """Two features may not share one column (spec §1.2: *never a silent overwrite*)."""
    seen: dict[str, str] = {}
    for feature in admitted:
        clash = seen.get(feature.feature_name)
        if clash is not None:
            raise FeatureNamePlanError(
                f"feature names {clash!r} and {feature.intent.name!r} both normalize to the Hive "
                f"identifier {feature.feature_name!r}; one would silently overwrite the other"
            )
        seen[feature.feature_name] = feature.intent.name
