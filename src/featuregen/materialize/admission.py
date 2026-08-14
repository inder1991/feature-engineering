"""Spec §1.2 — Gate 1: admit an AUTHORED artifact, or refuse it.

THE ATTACK THIS GATE EXISTS TO STOP. :class:`~featuregen.formula.result.AuthoringResult` is a
publicly constructible frozen dataclass. A caller can fabricate one that claims
``authoring_disposition="RESOLVED"``, attach any :class:`~featuregen.formula.schema.TypedFormulaV1`
it likes, set ``candidate_formula_hash`` to whatever makes that formula look authored, and cite a
LEGITIMATE ``authoring_run_id``. Every field of such an object agrees with every other field, so an
in-memory consistency check proves exactly nothing.

So nothing here trusts the supplied object. Every check is against the run's **immutable terminal
trace event** (``materialize.authoring_trace.read_terminal_event``) and its **write-once manifest**
(``read_run_intent_hash``) — rows migration **1022** physically forbids updating, deleting or
truncating, carrying a canonical payload and the sha256 ``payload_hash`` that makes it
tamper-evident.

⚠️ **WHAT THE SUPPLIED RESULT CONTRIBUTES CHANGED WITH THE LANE.** Against 1020 it contributed
exactly one thing the trace did not hold — the formula OBJECT itself — and every other field of it
was checked against a record written from somewhere else. **1022's terminal payload holds the
result material too**: ``_terminal_payload`` writes ``"result": _plain(result)`` alongside the
dispositions and axes (``replay_authoring.py:151-162``). Two consequences, and neither is a
loophole, but both must be stated rather than implied:

* against a FORGING CALLER — the attack in the paragraph above — nothing weakens. Such a caller
  supplies an object from thin air, and checks 3/4/5 compare it field by field against a WORM row
  it did not write.
* against ``materialize.resolve``, which rebuilds its object FROM that same payload, check 5 is an
  intra-payload consistency check: the axes it compares were written from one object, in one row, by
  one statement, so for a genuine row they cannot disagree. What is load-bearing on that path is
  **check 2** (the payload is authentic against its own ``payload_hash``, on a row 1022 forbids
  updating) and **check 6** (the intent comes from migration 1023's work item — a DIFFERENT store —
  and must re-hash to the manifest). Check 4 also stays a genuine cross-EVENT derivation: the
  restorer rebuilds the formula from the ``AUTHOR_PROPOSAL_PARSED`` and ``OUTPUT_POLICY_RESOLVED``
  events, so its digest is re-derived from material the TERMINAL event does not contain.

This is inherent to the lane: 1022's terminal payload is the only durable source of the result
material there is, so any resolution path must read it. It is bounded by check 2's threat model —
altering it requires the same out-of-band write that would defeat 1020.

⚠️ **THE EVIDENCE LANE MOVED (Phase G).** These checks read migration 1022's
``formula_authoring_run`` / ``formula_authoring_trace_event``, not migration 1020's
``authoring_run`` / ``authoring_trace_event``. 1020 is the store this gate was built against and
**nothing in ``src/`` has ever written it**: the live authoring worker calls
``formula.replay_authoring.run_authoring`` (``overlay/upload/recipe_formula_worker.py:35``), which
writes 1022. A gate reading 1020 therefore refused every real governed feature at check 1. The
reasoning, the borrowed-hasher discipline and the deliberate refusal to accept EITHER store are all
in ``materialize.authoring_trace``; the orphaned lane is recorded in ``docs/DEFERRED-WORK.md``
A.33. The move STRENGTHENS check 6 — see ``_verify_intent_hash``.

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

⚠️ **CHECK 3 READS THE PAYLOAD, NOT THE EVENT KIND.** The 1022 orchestrator's normal terminal
append writes ``completed`` for every disposition that reaches the §F fold — ``RESOLVED``,
``NEEDS_REVIEW``, ``UNSUPPORTED`` and an ``invalid_output`` ``REJECTED`` alike
(``replay_authoring.py:674-683``); only its technical/parse/validator arms write ``failed``.
Admitting on "a completed event exists" would admit a feature whose human review is still
outstanding — the single highest-consequence mistake available at this boundary.

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.contracts.db import DbConn

# Imported helpers are bound PRIVATELY (``_name`` / a module alias) on purpose: they are this
# module's implementation, not part of its API, and ``test_admission.py``'s
# ``test_no_function_accepts_a_bare_formula`` introspects every PUBLIC function of this module. A
# re-exported helper would be swept into that check, so an unrelated rename upstream could turn the
# gate's bypass guard red — or, worse, a helper that really did take a bare formula could be
# re-exported here and quietly become a public entry point.
from featuregen.formula.canonical import formula_content_hash as _formula_content_hash
from featuregen.formula.result import AuthoringResult
from featuregen.formula.schema import SchemaError, TypedFormulaV1
from featuregen.formula.schema import body_expressions as _body_expressions
from featuregen.formula.turns import AuthoringIntent
from featuregen.materialize import authoring_trace as _trace
from featuregen.materialize.authoring_trace import authoring_intent_hash as _authoring_intent_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

__all__ = [
    "AdmittedFeature",
    "FeatureNamePlanError",
    "ResolvedFeatureInput",
    "admit_artifacts",
    "hive_identifier",
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
#: (Hive's column-name bound). Anything outside is folded to ``_`` by :func:`hive_identifier`.
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
    #: B3 — the FROZEN PLAN ENVELOPE the served option was built from, or ``None`` for an input
    #: whose work item predates migration 1068. It is PROVENANCE the gate validates against, never
    #: an input to the intent hash: check 6 re-derives ``authoring_intent_hash`` from the five
    #: fields of ``intent`` and this field is not one of them, so an envelope's presence, absence or
    #: content cannot move that digest.
    plan_envelope: Mapping[str, Any] | None = None


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
    #: The envelope this artifact was ADMITTED against, carried forward so ``compile_ir`` validates
    #: the same object check 7 did rather than re-reading it from somewhere else.
    plan_envelope: Mapping[str, Any] | None = None


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
    _verify_schema_version(formula, run_id)                              # 4b (BR-6)
    _verify_axes(event, result, run_id)                                  # 5
    _verify_intent_hash(conn, item.intent, run_id)                       # 6
    _verify_plan_envelope(formula, item.plan_envelope, run_id)           # 7 (B3)

    return AdmittedFeature(
        feature_name=hive_identifier(item.intent.name),
        formula=formula,
        formula_content_hash=content_hash,
        intent=item.intent,
        authoring_run_id=run_id,
        plan_envelope=item.plan_envelope,
    )


def _verify_schema_version(formula: TypedFormulaV1, run_id: str) -> None:
    """BR-6 (4b): this compiler consumes exactly Formula-v1. A v2 (or any-other-version) formula
    reaching admission is refused loudly — compiling it under v1 semantics would silently
    misread operations v1 never defined. The v2 path arrives with an engine that ADVERTISES it."""
    if formula.formula_schema_version != 1:
        raise MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"authoring run {run_id}: formula schema version "
            f"{formula.formula_schema_version} is not compilable by this engine (v1 only)",
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
    """Recompute the payload's sha256 and compare it (``TERMINAL_PAYLOAD_TAMPERED``).

    ``authoring_trace.payload_digest`` is the writer's OWN hasher, borrowed rather than
    re-implemented, so the two sides of this check cannot drift; ``test_admission.py`` pins the
    equality against a real, unaltered run. Note it is NOT ``materialize.canonical.materialize_hash``
    (RFC 8785), because 1022 hashes with ``json.dumps`` — a second copy that guessed wrong would
    make this gate refuse every genuine feature, which is as broken as admitting a forged one.

    Migration 1022 makes the row physically immutable, so a mismatch means the stored bytes were
    altered out of band (a direct write as a superuser, a restore from doctored bytes). Refusing is
    the only safe reading: every later check reads this same payload.

    A NULL ``payload_hash`` FAILS CLOSED. 1026 added the column to an existing table, so a row can
    carry no tamper evidence at all; reading that as "nothing to compare against, therefore fine"
    would make check 2 opt-out, and an unverifiable payload is exactly what a tamper produces."""
    if event.payload_hash is None:
        raise MaterializationRefused(
            CompilationRefusalCode.TERMINAL_PAYLOAD_TAMPERED,
            f"the terminal {event.kind} event of authoring run {run_id} records no payload_hash, "
            "so its payload cannot be authenticated",
        )
    if _trace.payload_digest(event.payload) != event.payload_hash:
        raise MaterializationRefused(
            CompilationRefusalCode.TERMINAL_PAYLOAD_TAMPERED,
            f"the terminal {event.kind} event of authoring run {run_id} does not match its "
            "recorded payload_hash",
        )


def _payload_field(event: _trace.TerminalEvent, key: str) -> object:
    """One field of the terminal payload, or ``None`` when the payload is not an object at all.

    1022's ``payload`` column carries no ``jsonb_typeof = 'object'`` CHECK (1020's did), so a
    directly-written row may hold an array or a scalar. Every caller of this helper treats ``None``
    as a refusal, so an unreadable record fails CLOSED rather than raising ``AttributeError`` out of
    a governed gate — the same reading ``_require_resolved`` gives a missing disposition."""
    payload = event.payload
    return payload.get(key) if isinstance(payload, Mapping) else None


# ── 3. the PAYLOAD's disposition is RESOLVED ─────────────────────────────────────────────────────

def _require_resolved(event: _trace.TerminalEvent, run_id: str) -> None:
    """``NOT_RESOLVED`` unless the payload says ``RESOLVED``.

    ⚠️ The event KIND is not consulted, and must not be: 1022's normal terminal append writes
    ``completed`` for every disposition that reaches the §F fold, so a ``NEEDS_REVIEW``,
    ``UNSUPPORTED`` or ``invalid_output`` ``REJECTED`` run closes with ``completed`` too. A missing
    or non-string disposition is refused for the same reason — an unreadable verdict is not a
    permissive one."""
    disposition = _payload_field(event, "authoring_disposition")
    if disposition != _RESOLVED:
        raise MaterializationRefused(
            CompilationRefusalCode.NOT_RESOLVED,
            f"the terminal {event.kind} event of authoring run {run_id} records "
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
    recorded = _payload_field(event, "candidate_formula_hash")
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
    supplied object, so the two records must be the same record.

    ⚠️ STRENGTH DEPENDS ON WHO SUPPLIED THE OBJECT, and the module docstring says why: against a
    forging caller this is a real comparison against a WORM row; against ``materialize.resolve``,
    whose object is rebuilt from this very payload's nested ``result``, it is an intra-payload
    consistency check and checks 2 and 6 are what carry that path."""
    differing = tuple(
        field for field in _AXIS_FIELDS
        if getattr(result, field) != _payload_field(event, field)
    )
    if differing:
        detail = ", ".join(
            f"{field}: supplied {getattr(result, field)!r} != recorded "
            f"{_payload_field(event, field)!r}"
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

    ⚠️ SCOPE, VERIFIED — and WIDER than it was. ``authoring_trace.authoring_intent_hash`` re-derives
    by the recipe the 1022 manifest was stamped with (``replay_authoring.py:82-89``, ``:307``):
    ``name``, ``hypothesis``, ``target_entity``, ``target_grain_keys`` **and
    ``recipe_authoring_context``** — all FIVE fields of ``AuthoringIntent``. Against migration 1020
    the fifth was outside the digest and this docstring recorded that as a standing limit; moving
    lanes closed it. That field carries the authoring prompt's whole catalog context
    (``formula/author.py:98-99``), so an intent claiming a context the author never saw is a
    different authoring request, and is now refused rather than admitted."""
    recorded = _trace.read_run_intent_hash(conn, run_id)
    supplied = _authoring_intent_hash(intent)
    if recorded != supplied:
        raise MaterializationRefused(
            CompilationRefusalCode.INTENT_HASH_MISMATCH,
            f"the intent supplied for authoring run {run_id} hashes to {supplied}, but the run's "
            f"manifest records intent_hash={recorded!r}",
        )


# ── 7. the artifact is the one the human's governed plan described (D-4) ─────────────────────────


def _verify_plan_envelope(
    formula: TypedFormulaV1, envelope: Mapping[str, Any] | None, run_id: str
) -> None:
    """``PLAN_ENVELOPE_DIVERGENCE`` unless the artifact matches the plan the option was served with.

    THE ARTIFACT-SHAPED HALF of D-4, and it is deliberately small: this gate holds a formula and an
    intent, not a compilation, so it can answer exactly two of the envelope's questions — the grain
    entity the feature is computed at, and the relation its expressions read. The rest of the
    envelope (the read set, the population, the window) is only knowable once physical resolution
    has run, and is checked in :func:`~featuregen.materialize.ir.compile_ir`, after compiling.

    ⚠️ **A missing envelope is not a failure, and must not become one.** Every work item written
    before migration 1068 carries none, and the whole materialization path predates the envelope;
    refusing them here would make B3 a breaking change to a governed lane rather than a check added
    to it. Likewise a BLANK field inside an envelope is not an assertion — ``output_grain`` is ``""``
    for a planning request that declared none — so only what the envelope actually states is
    compared.

    Case is folded on both sides for the same reason ``fold_frozen_binding_plan`` folds it on the
    UOA: the catalog says ``Customer``, the planning grain says ``customer``, and a capitalization
    is not a different unit of analysis.
    """
    if not envelope:
        return
    declared_grain = str(envelope.get("output_grain") or "").strip().lower()
    if declared_grain and formula.grain.entity.strip().lower() != declared_grain:
        raise MaterializationRefused(
            CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE,
            f"authoring run {run_id}: the artifact is computed at "
            f"{formula.grain.entity!r} grain, but the frozen plan the option was served with says "
            f"output_grain={envelope.get('output_grain')!r}. The governed plan and the artifact "
            f"are not the same feature, and compilation never substitutes one for the other",
        )
    declared_table = str(envelope.get("source_table") or "").strip().lower()
    declared_catalog = str(envelope.get("catalog_source") or "").strip().lower()
    if not declared_table:
        return
    for path, expression in _body_expressions(formula.body):
        source, schema, table = _split_table_ref(expression.source_relation.table_ref)
        if table != declared_table or (declared_catalog and source != declared_catalog):
            raise MaterializationRefused(
                CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE,
                f"authoring run {run_id}: {path} reads "
                f"{expression.source_relation.table_ref!r} ({source}::{schema}.{table}), but the "
                f"frozen plan the option was served with names source_table="
                f"{envelope.get('source_table')!r} in catalog "
                f"{envelope.get('catalog_source')!r}",
            )


def _split_table_ref(table_ref: str) -> tuple[str, str, str]:
    """``(source, schema, table)`` from a ``source::schema.table`` logical ref, case-folded.

    Parsed here rather than through ``object_ref.parse_ref`` because a malformed ref must not raise
    a bare ``ValueError`` out of a governed gate (§14): an unparseable relation simply compares
    unequal to whatever the envelope declared, and is refused as the divergence it is.
    """
    source, _, path = table_ref.partition("::")
    parts = path.split(".")
    schema = parts[0] if len(parts) > 1 else ""
    table = parts[-1] if parts else ""
    return source.strip().lower(), schema.strip().lower(), table.strip().lower()


# ── the feature name (spec §1.2, final paragraph) ────────────────────────────────────────────────

def hive_identifier(name: str) -> str:
    """``intent.name`` folded to a Hive identifier — the physical column the feature will occupy.

    Deterministic and conservative: NFKC-normalize, strip, lower-case, and map every character Hive
    does not accept in an unquoted identifier to ``_``. Nothing is collapsed or truncated, because
    both would map two distinct names onto one column — the very thing the collision check exists to
    prevent. A name that cannot be expressed at all (empty, not starting with a letter, longer than
    Hive's 128-character bound) is a plan error, not a name to invent a mangling for.

    PUBLIC because the group plan (§9/§10.2) names the same columns and must reach the SAME answer:
    a second normalizer would be a second chance to disagree about which column a feature occupies,
    and the disagreement would surface as a schema gate failing on a name nobody chose. It is
    idempotent, so re-applying it to an already-admitted ``feature_name`` is a validation."""
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
