"""S4 — the policy resolver: occurrences persisted, realizations published, references resolved.

**This is the first thing in the platform that resolves a governed policy reference.** Nothing did
before, and that is a checkable claim rather than a framing: ``parse_policy_ref`` validates a ref's
SHAPE and KIND against an in-process registry and never touches a connection, and
``semantic_eligibility`` emits ``STATUS_POLICY_UNRESOLVED`` on the mere PRESENCE of a ref — its own
resolution text says "no resolver serves yet". So until now every governed reference was unresolved
by definition, and "this one cannot be resolved" was not a distinguishable state. It is now, and it
has a name: :data:`~featuregen.overlay.upload.semantic_eligibility_reasons.
POLICY_REFERENCE_UNRESOLVABLE`, in the ``needs_setup`` family, because the remedy is governance work
— publish a realization — and not a data check or a rebinding.

**Resolution keys on the FAMILY, not on the occurrence.** C-C8's family key is defined as what a
"current" realization is current FOR, so family membership IS the claim that a realization applies at
an occurrence. Requiring the ``realizes_occurrences`` link at resolution time would refuse a second
expression that reads the same policy over the same column for no reason anyone could defend. The
link is still recorded and still load-bearing — it is what makes
:func:`~featuregen.formula.policy_realization.unrealized_occurrences` able to detect an occurrence
nothing answers — so :class:`ResolvedPolicyV1` reports ``claims_occurrence`` rather than hiding the
difference between "this realization was built for this occurrence" and "this realization covers the
family this occurrence falls in".

**Two refusals, both named, and deliberately different.** Publication refuses with C-C9's own codes
(``TWO_GOVERNED_DECLARATIONS``, ``NO_DETERMINISTIC_WINNER``, ``NO_CANDIDATE``) — those say the
candidates could not decide. Resolution refuses with ``POLICY_REFERENCE_UNRESOLVABLE`` — that says
nobody has published for this family at all. Collapsing them would tell an operator "unresolvable"
when the truth is "two teams both declared it".

**The conflict is retained by being WRITTEN.** C-C9 computes ``SOURCE_OVERRODE_LLM`` as a resolved
finding on the verdict, not on the winner — the winner is a candidate the caller built and knows
nothing about the decision it went on to win. :func:`publish_policy_realization` is where the two
meet: the verdict's findings are persisted against the winner's revision alongside the winner's own,
which is what makes "the source won and the model disagreed" survive past the moment of deciding.

**C-A3c's deferred gate lands here: provenance pinned on the occurrence.** ``MeasureFact`` already
names itself "the provenance an occurrence must pin", and :func:`record_occurrence_set` requires the
reads rather than accepting them — an optional argument would make the unpinned call the easy one to
write. What it closes is specific: a per-row-currency monetary operand used to arrive looking
NON-MONETARY with nothing recorded, so the FX requirement could not fire and a mixed-currency
population was summed in silence. The reads are keyed by DERIVATION, so two derivations over a
catalog that moved each pin what they saw and neither restates the other.

**No V2 path writes through the mutable upsert.** ``data_agent.eligibility_store.record_eligibility``
overwrites a policy in place through ``ON CONFLICT (catalog_source, table_name) DO UPDATE SET`` with
no version guard and no history, so the previous meaning is simply gone. Nothing here can reach it:
revisions are append-only by trigger, and the pointer moves only through
:func:`set_current_realization`, which names the exact version it read and raises on any drift — the
strictest CAS template in this tree (``bridge_store``'s ``pointer_version``, by way of
``serving_policy_store``).
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.formula.measure_facts import MeasureFacts, MeasureReadDisposition
from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.policy_admissibility import (
    AdmissibilityOutcomeV1,
    AdmissibilityVerdictV1,
    decide_admissibility,
)
from featuregen.formula.policy_occurrences import (
    PolicyOccurrenceSetV1,
    PolicyOccurrenceV1,
    occurrence_set_hash,
    required_policy_kinds_v2,
)
from featuregen.formula.policy_realization import (
    ConflictFindingV1,
    PolicyRealizationRevisionV1,
    RealizationFamilyKeyV1,
    RealizationProvenanceV1,
    family_key_for,
    family_key_hash,
)
from featuregen.formula.schema_v3 import AggregateExpressionV3
from featuregen.overlay.upload.semantic_eligibility_reasons import POLICY_REFERENCE_UNRESOLVABLE

__all__ = [
    "MeasureReadRowV1",
    "OccurrenceProvenanceMissing",
    "PolicyPointerConflict",
    "PolicyRealizationRefused",
    "PolicyReferenceUnresolvable",
    "PolicyStoreCorrupt",
    "ResolvedPolicyV1",
    "current_realization_pointer",
    "load_realization_revision",
    "measure_reads_for",
    "operand_facts_from_measure",
    "publish_policy_realization",
    "record_occurrence_set",
    "record_realization_revision",
    "resolve_policy_occurrence",
    "set_current_realization",
    "unmet_policy_kinds",
    "unresolved_references",
]


class PolicyReferenceUnresolvable(Exception):
    """No realization is current for the family this reference falls in.

    Carries :attr:`refusal_code` so a caller reports the platform's named reason rather than
    inventing a message — the difference between a refusal a product surface can group and explain
    and one that reads as an internal error.
    """

    refusal_code = POLICY_REFERENCE_UNRESOLVABLE

    def __init__(self, message: str, *, occurrence: PolicyOccurrenceV1 | None = None) -> None:
        super().__init__(message)
        self.occurrence = occurrence


class PolicyRealizationRefused(Exception):
    """Admissibility refused: the candidates could not decide. Distinct from unresolvable.

    Carries the whole verdict, conflicts included, because the refusal's REASON is the useful part —
    "two governed declarations" is fixed upstream in the source and "no deterministic winner" is
    fixed by getting evidence, and an operator told only "refused" cannot tell which.
    """

    def __init__(self, verdict: AdmissibilityVerdictV1) -> None:
        super().__init__(
            f"{verdict.refusal_code}: "
            + "; ".join(finding.detail for finding in verdict.conflicts))
        self.verdict = verdict
        self.refusal_code = verdict.refusal_code


class PolicyPointerConflict(Exception):
    """The current pointer moved between the read and the write. Nothing was overwritten."""


class PolicyStoreCorrupt(Exception):
    """A stored row cannot reproduce the identity it is filed under — surfaced, never served."""


class OccurrenceProvenanceMissing(ValueError):
    """An occurrence was offered without the measure read C-A3c requires it to pin."""


@dataclass(frozen=True, slots=True)
class MeasureReadRowV1:
    """One pinned measure read, as stored. The read-back shape of C-A3c's provenance."""

    occurrence_hash: str
    field: str
    value: str
    disposition: MeasureReadDisposition
    producer: str | None
    strength: str | None
    decision_event_id: str | None
    selected_evidence_ids: tuple[str, ...]
    policy_version: str
    resolver_version: str | None


@dataclass(frozen=True, slots=True)
class ResolvedPolicyV1:
    """What resolving a reference yields: the current realization, and how it relates to the ask."""

    occurrence: PolicyOccurrenceV1
    revision: PolicyRealizationRevisionV1
    pointer_version: int
    #: Whether the current revision explicitly names THIS occurrence in ``realizes_occurrences``.
    #: False is ordinary — a realization covers a family — and is reported rather than hidden so a
    #: reader can tell a purpose-built answer from a family-wide one.
    claims_occurrence: bool


# ── occurrences ──────────────────────────────────────────────────────────────────────────────────
def record_occurrence_set(
    conn: DbConn,
    occurrences: PolicyOccurrenceSetV1,
    *,
    set_id: str,
    bound_input_set_revision_id: str,
    measure_reads: Mapping[str, MeasureFacts],
) -> str:
    """Append an occurrence set, the binding it was derived over, and what each column WAS KNOWN TO
    BE at the time.

    The binding is required by the database (a foreign key to S3's table), because an occurrence
    names a physical dataset and one derived over no binding would name a dataset nobody bound.
    Idempotent: the set and its occurrences are content-identified, so re-deriving writes nothing.

    ``measure_reads`` maps occurrence hash → the verified measure read for its bound column, and it
    is REQUIRED — this is C-A3c's deferred gate, and an optional argument would make the unpinned
    call the easy one to write. :class:`~featuregen.formula.measure_facts.MeasureFact` names itself
    "the provenance an occurrence must pin", and the defect it closes is specific: a per-row-currency
    monetary operand used to arrive looking NON-MONETARY with nothing recorded, so the FX
    requirement could not fire and a mixed-currency population was summed in silence. An ``absent``
    read is a positive statement — "nobody has decided this column's unit", the ordinary case since
    most columns are not measures — and is stored rather than skipped, because a missing row and a
    recorded absence are the two things worth keeping apart.

    Raises:
        OccurrenceProvenanceMissing: an occurrence has no reading. Named, because the fix is to
            READ the column through the verified-decision seam, not to relax the write.
    """
    missing = [occurrence.occurrence_hash for occurrence in occurrences.occurrences
               if occurrence.occurrence_hash not in measure_reads]
    if missing:
        raise OccurrenceProvenanceMissing(
            f"{len(missing)} occurrence(s) offered with no measure read: what the platform knew "
            f"about the bound column is what decides whether a currency conversion is needed at "
            f"all, and an occurrence recorded without it cannot later be told apart from one over "
            f"a column nobody had decided ({missing[0]})")

    conn.execute(
        "INSERT INTO policy_occurrence_set (set_id, bound_input_set_revision_id, content_hash) "
        "VALUES (%s, %s, %s) ON CONFLICT (set_id) DO NOTHING",
        (set_id, bound_input_set_revision_id, occurrence_set_hash(occurrences)))
    for occurrence in occurrences.occurrences:
        conn.execute(
            "INSERT INTO policy_occurrence (occurrence_hash, set_id, expr_path, policy_ref_field, "
            "policy_kind, policy_ref, semantic_role, bound_dataset, bound_column, environment_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (occurrence_hash) DO NOTHING",
            (occurrence.occurrence_hash, set_id, occurrence.expr_path,
             occurrence.policy_ref_field, occurrence.policy_kind, occurrence.policy_ref,
             occurrence.semantic_role, occurrence.bound_dataset, occurrence.bound_column,
             occurrence.environment_id))

        facts = measure_reads[occurrence.occurrence_hash]
        for fact in (facts.unit, facts.currency):
            conn.execute(
                "INSERT INTO policy_occurrence_measure_read (set_id, occurrence_hash, field, "
                "value, disposition, producer, strength, decision_event_id, "
                "selected_evidence_ids, policy_version, resolver_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (set_id, occurrence_hash, field) DO NOTHING",
                (set_id, occurrence.occurrence_hash, fact.field, fact.value,
                 fact.disposition.value, fact.producer, fact.strength, fact.decision_event_id,
                 json.dumps(list(fact.selected_evidence_ids)), fact.policy_version,
                 fact.resolver_version))
    return set_id


def measure_reads_for(conn: DbConn, set_id: str) -> dict[tuple[str, str], MeasureReadRowV1]:
    """Every measure read one derivation pinned, keyed by ``(occurrence_hash, field)``."""
    return {
        (row[0], row[1]): MeasureReadRowV1(
            occurrence_hash=row[0], field=row[1], value=row[2],
            disposition=MeasureReadDisposition(row[3]), producer=row[4], strength=row[5],
            decision_event_id=row[6], selected_evidence_ids=tuple(row[7]),
            policy_version=row[8], resolver_version=row[9])
        for row in conn.execute(
            "SELECT occurrence_hash, field, value, disposition, producer, strength, "
            "decision_event_id, selected_evidence_ids, policy_version, resolver_version "
            "FROM policy_occurrence_measure_read WHERE set_id = %s "
            "ORDER BY occurrence_hash, field", (set_id,)).fetchall()}


def operand_facts_from_measure(facts: MeasureFacts) -> OperandFactsV2:
    """The verified measure read as the facts the need calculation consumes.

    Written so the currency need is decided from a VERIFIED read rather than from an
    :class:`OperandFactsV2` assembled somewhere that may have degraded silently — which is the exact
    C-A3c failure. An ``absent`` read yields ``""``, and that is honest: nobody decided, so nothing
    claims the operand is monetary. The difference from before is that the absence is now RECORDED
    next to the occurrence instead of being indistinguishable from a non-measure.
    """
    return OperandFactsV2(unit=facts.unit.value, currency=facts.currency.value)


# ── realizations ─────────────────────────────────────────────────────────────────────────────────
def record_realization_revision(
    conn: DbConn,
    revision: PolicyRealizationRevisionV1,
    *,
    extra_conflicts: Sequence[ConflictFindingV1] = (),
) -> str:
    """Append a realization revision, what it realizes, and every conflict observed reaching it.

    ``extra_conflicts`` are the DECIDING findings — C-C9 computes them on the verdict rather than on
    the winner, because the winner is a candidate that knows nothing about the decision it went on
    to win. They are written first, so a code collision keeps the finding that describes the
    decision being persisted rather than one carried in from an earlier one.
    """
    key = revision.family_key
    conn.execute(
        "INSERT INTO policy_realization_revision (revision_id, family_key_hash, policy_kind, "
        "policy_ref, bound_dataset, environment_id, semantic_role, executable_content_hash, "
        "cas_pointer, provenance) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, family_key_hash(key), key.policy_kind, key.policy_ref,
         key.bound_dataset, key.environment_id, key.semantic_role,
         revision.executable_content_hash, revision.cas_pointer, revision.provenance.value))

    for occurrence_hash in revision.realizes_occurrences:
        conn.execute(
            "INSERT INTO policy_realization_occurrence (revision_id, occurrence_hash) "
            "VALUES (%s, %s) ON CONFLICT (revision_id, occurrence_hash) DO NOTHING",
            (revision.revision_id, occurrence_hash))

    for finding in tuple(extra_conflicts) + revision.conflict_findings:
        conn.execute(
            "INSERT INTO policy_realization_conflict (revision_id, code, detail, resolved) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (revision_id, code) DO NOTHING",
            (revision.revision_id, finding.code, finding.detail, finding.resolved))
    return revision.revision_id


def set_current_realization(
    conn: DbConn,
    *,
    family_key: RealizationFamilyKeyV1,
    revision_id: str,
    expected_pointer_version: int,
    declared_by: str,
) -> int:
    """Advance the family's current pointer by compare-and-swap; return the NEW version.

    ``expected_pointer_version=0`` claims first-write; ``>=1`` names the exact version the caller
    read. Anything else raises :class:`PolicyPointerConflict` and writes nothing — which is the
    whole difference between this and the mutable upsert the acceptance clause forbids.
    """
    if expected_pointer_version < 0:
        raise PolicyPointerConflict("expected_pointer_version cannot be negative")
    if not declared_by.strip():
        raise PolicyPointerConflict(
            "advancing a policy pointer must record who declared it: a byte-identical "
            "re-derivation REUSES the revision, so without this the act of making it current "
            "would leave no trace of who did it")

    key_hash = family_key_hash(family_key)
    if expected_pointer_version == 0:
        changed = conn.execute(
            "INSERT INTO policy_realization_current "
            "(family_key_hash, revision_id, pointer_version, declared_by) VALUES (%s, %s, 1, %s) "
            "ON CONFLICT (family_key_hash) DO NOTHING",
            (key_hash, revision_id, declared_by.strip())).rowcount
    else:
        changed = conn.execute(
            "UPDATE policy_realization_current SET revision_id = %s, "
            "pointer_version = pointer_version + 1, declared_by = %s, updated_at = now() "
            "WHERE family_key_hash = %s AND pointer_version = %s",
            (revision_id, declared_by.strip(), key_hash, expected_pointer_version)).rowcount
    if changed != 1:
        raise PolicyPointerConflict(
            f"the realization pointer for {key_hash} moved past version "
            f"{expected_pointer_version}; nothing was overwritten")
    return expected_pointer_version + 1


def publish_policy_realization(
    conn: DbConn,
    candidates: Sequence[PolicyRealizationRevisionV1],
    *,
    expected_pointer_version: int,
    declared_by: str,
) -> tuple[PolicyRealizationRevisionV1, AdmissibilityVerdictV1, int]:
    """Decide between candidates for ONE family, append them all, and point at the winner.

    **Every candidate is recorded, not only the winner.** C-C9's retained ``SOURCE_OVERRODE_LLM``
    finding names the losing proposal by revision id, and a finding whose reference resolves to
    nothing is a record of a disagreement nobody can inspect — which is most of what retaining it
    was for. Losers are revisions and nothing more: they are not current, and only an explicit
    :func:`set_current_realization` could ever make one so.

    Raises:
        PolicyRealizationRefused: admissibility refused — including with no candidates at all,
            which is ``NO_CANDIDATE`` rather than a second vocabulary for the same fact. Nothing is
            written: a refusal produced no artifact, so its findings have no revision to hang on
            and ride on the exception's verdict instead. Inventing a family-decision table to hold
            them would be a table nothing reads.
        ValueError: the candidates span more than one family. A winner chosen across families would
            be current for more than one thing, which is exactly what C-C8's key exists to prevent.
    """
    families = {family_key_hash(candidate.family_key) for candidate in candidates}
    if len(families) > 1:
        raise ValueError(
            f"{len(families)} families among the candidates: admissibility decides WITHIN a family, "
            f"and a winner chosen across families would be current for more than one thing")
    # No candidates is not a special case here — C-C9 already has a name for it (`NO_CANDIDATE`),
    # and inventing a second way to say "nothing was offered" would put two vocabularies in front
    # of the same operator.

    verdict = decide_admissibility(candidates)
    if verdict.outcome is AdmissibilityOutcomeV1.REFUSED:
        raise PolicyRealizationRefused(verdict)

    winner = verdict.winner
    assert winner is not None  # AdmissibilityVerdictV1 refuses this combination at construction
    for candidate in candidates:
        record_realization_revision(
            conn, candidate,
            extra_conflicts=(verdict.conflicts
                             if candidate.revision_id == winner.revision_id else ()))
    version = set_current_realization(
        conn, family_key=winner.family_key, revision_id=winner.revision_id,
        expected_pointer_version=expected_pointer_version, declared_by=declared_by)
    return winner, verdict, version


# ── resolution ───────────────────────────────────────────────────────────────────────────────────
def current_realization_pointer(
    conn: DbConn, family_key: RealizationFamilyKeyV1,
) -> tuple[str, int, str] | None:
    """``(revision_id, pointer_version, declared_by)`` for a family, or ``None`` if none is current."""
    row = conn.execute(
        "SELECT revision_id, pointer_version, declared_by FROM policy_realization_current "
        "WHERE family_key_hash = %s", (family_key_hash(family_key),)).fetchone()
    return None if row is None else (row[0], row[1], row[2])


def load_realization_revision(
    conn: DbConn, revision_id: str,
) -> PolicyRealizationRevisionV1 | None:
    """One revision, reconstructed and identity-verified.

    The family key is re-derived from its stored parts and checked against the stored
    ``family_key_hash``. A row that cannot reproduce the key it is filed under is store corruption:
    it would be served as the current answer for a family it no longer belongs to.
    """
    row = conn.execute(
        "SELECT family_key_hash, policy_kind, policy_ref, bound_dataset, environment_id, "
        "semantic_role, executable_content_hash, cas_pointer, provenance "
        "FROM policy_realization_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None

    key = RealizationFamilyKeyV1(
        policy_kind=row[1], policy_ref=row[2], bound_dataset=row[3], environment_id=row[4],
        semantic_role=row[5])
    if family_key_hash(key) != row[0]:
        raise PolicyStoreCorrupt(
            f"realization revision {revision_id} is filed under family {row[0]} but its stored "
            f"parts derive {family_key_hash(key)}: it would be served as the current answer for a "
            f"family it no longer belongs to")

    occurrences = tuple(item[0] for item in conn.execute(
        "SELECT occurrence_hash FROM policy_realization_occurrence WHERE revision_id = %s "
        "ORDER BY occurrence_hash", (revision_id,)).fetchall())
    conflicts = tuple(
        ConflictFindingV1(code=item[0], detail=item[1], resolved=item[2])
        for item in conn.execute(
            "SELECT code, detail, resolved FROM policy_realization_conflict "
            "WHERE revision_id = %s ORDER BY code", (revision_id,)).fetchall())
    return PolicyRealizationRevisionV1(
        revision_id=revision_id, family_key=key, executable_content_hash=row[6],
        cas_pointer=row[7], provenance=RealizationProvenanceV1(row[8]),
        realizes_occurrences=occurrences, conflict_findings=conflicts)


def resolve_policy_occurrence(conn: DbConn, occurrence: PolicyOccurrenceV1) -> ResolvedPolicyV1:
    """Resolve one governed reference to the realization currently serving it.

    Raises:
        PolicyReferenceUnresolvable: nothing is current for this family. Named rather than returned
            as ``None`` because a caller that ignored a ``None`` would execute a formula whose
            governed policy has no executable answer — the failure this whole path exists to make
            impossible.
        PolicyStoreCorrupt: the pointer names a revision that is missing or mis-filed.
    """
    family = family_key_for(occurrence)
    pointer = current_realization_pointer(conn, family)
    if pointer is None:
        raise PolicyReferenceUnresolvable(
            f"{POLICY_REFERENCE_UNRESOLVABLE}: no realization is current for "
            f"{occurrence.policy_ref!r} as {occurrence.semantic_role!r} over "
            f"{occurrence.bound_dataset!r} in {occurrence.environment_id!r}. The reference is "
            f"well-formed and nothing answers it — publishing a realization is the remedy",
            occurrence=occurrence)

    revision_id, pointer_version, _declared_by = pointer
    revision = load_realization_revision(conn, revision_id)
    if revision is None:
        raise PolicyStoreCorrupt(
            f"the current pointer for {family_key_hash(family)} names missing revision "
            f"{revision_id}")
    return ResolvedPolicyV1(
        occurrence=occurrence, revision=revision, pointer_version=pointer_version,
        claims_occurrence=occurrence.occurrence_hash in revision.realizes_occurrences)


def unresolved_references(
    conn: DbConn, occurrences: PolicyOccurrenceSetV1,
) -> tuple[PolicyOccurrenceV1, ...]:
    """Every occurrence in the set that nothing currently realizes.

    The coverage question asked in bulk, in occurrence-hash order — the same order the set carries,
    so a report over it does not depend on how the formula was walked.
    """
    return tuple(
        occurrence for occurrence in occurrences.occurrences
        if current_realization_pointer(conn, family_key_for(occurrence)) is None)


def unmet_policy_kinds(
    conn: DbConn,
    *,
    expression: AggregateExpressionV3,
    operand_facts: OperandFactsV2,
    occurrences: PolicyOccurrenceSetV1,
) -> frozenset[str]:
    """Policy kinds this expression NEEDS that no resolved occurrence supplies.

    The comparison C-C7 deliberately left to a caller, made here because S4 is that caller. The two
    halves stay apart: the need comes from :func:`~featuregen.formula.policy_occurrences.
    required_policy_kinds_v2` over bound facts, the supply comes from occurrences that actually
    resolve, and only the difference is returned. Folding them would make an unmet need
    indistinguishable from an undeclared one.

    Both directions of C-C7's currency clause fall straight out of this. An operand whose facts are
    not ``monetary``/``per_row`` needs no currency conversion, so no currency occurrence is missing
    for it — not because a currency occurrence was excused, but because it was never needed.
    """
    needed = required_policy_kinds_v2(expression, operand_facts)
    supplied = {
        occurrence.policy_kind for occurrence in occurrences.occurrences
        if current_realization_pointer(conn, family_key_for(occurrence)) is not None}
    return frozenset(needed - supplied)
