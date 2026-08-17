"""S8 — the development gold proof, the mutations it survived, and the two capability facts.

**"Supported" is two facts, never one word** (invariant 11, §0.4). ``renderer_dispatchable`` is a
property of THIS BUILD — the renderer has a branch for the operator or it does not, and
``engine_capability`` derives that from the renderer's own self-description rather than from a table
somebody maintains. ``execution_proved`` is a property of a RUN: generated code executed against
reviewed gold. :func:`capability_of` returns both and never their conjunction, because collapsing
them loses which half is missing — and the halves have completely different remedies.

**An absent proof is not a failed one.** ``execution_proof_hash`` is nullable and the distinction is
load-bearing: ``NULL`` means nobody has taken a proof, ``false`` on a mutation means a proof ran and
something did not behave. A caller that could not tell those apart would report an unproved operator
as a broken one, and an operator nobody has tested as one that works.

**Changed bytes invalidate a proof with no version bump.** ``generated_project_hash`` is inside the
proof's own content hash, so a proof is looked up BY the bytes it was taken over: different bytes
simply do not find it. That is the whole mechanism — no staleness flag, no version field to
increment, because a version bump is a claim someone remembered to make.

**The mutation set is recorded per mutation, not only as a version.** The integer says which set
ran; the rows say what it caught. A proof carrying only the version could not answer "did the
missing-status-filter mutation actually fail?" afterwards, and a mutation silently passing is
exactly how a gold proof becomes ceremonial — so :func:`record_execution_proof` REFUSES a proof
whose mutation set is incomplete or whose mutations did not all fail.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.materialize.operator_graph_v2 import OperatorKindV2
from featuregen.overlay.upload.publication_revisions import OperatorExecutionProofV1

__all__ = [
    "PILOT_MUTATIONS",
    "CapabilityV1",
    "MutationOutcomeV1",
    "ProofIncomplete",
    "capability_of",
    "proof_for_bytes",
    "record_execution_proof",
    "record_renderer_dispatch",
    "set_execution_proof",
]

#: The seven mutations S8 names, each one a wrong NUMBER rather than an error — which is why a gold
#: corpus is the only thing that catches them. Frozen here so "the mutation set" is a value the code
#: can check completeness against rather than a list in a document.
PILOT_MUTATIONS: tuple[str, ...] = (
    "wrong_debit_mapping",
    "missing_status_filter",
    "reversal_neutralization_removed",
    "post_cutoff_fx_accepted",
    "quote_inversion_reversed",
    "conversion_after_aggregation",
    "duplicate_rate_gate_deleted",
)


class ProofIncomplete(ValueError):
    """A proof was offered whose mutation set does not establish what a proof claims.

    Named rather than a bare ``ValueError`` because the remedy is specific: run the missing
    mutations, or fix the one that passed. Both are work on the proof, not on the caller.
    """


@dataclass(frozen=True, slots=True)
class MutationOutcomeV1:
    """One mutation and whether the gold corpus caught it.

    ``detected=False`` is a genuine outcome, not an error state: it is the corpus failing to notice
    a wrong number, which is the single most important thing a gold proof can tell you. It is
    recorded, and it refuses the proof.
    """

    mutation_name: str
    detected: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CapabilityV1:
    """The two facts, side by side. Never their conjunction.

    :attr:`supported` exists so a caller that genuinely wants the conjunction says so, rather than
    reaching for whichever field it happened to have — but the fields stay separate on the type,
    because "no renderer branch" and "no proof yet" send you to different places.
    """

    engine_id: str
    operator_kind: str
    renderer_dispatchable: bool
    execution_proof_hash: str | None

    @property
    def execution_proved(self) -> bool:
        """Whether a proof exists. ``None`` means nobody took one — never "it failed"."""
        return self.execution_proof_hash is not None

    @property
    def supported(self) -> bool:
        """Invariant 11's conjunction, stated once so no caller re-derives it differently."""
        return self.renderer_dispatchable and self.execution_proved


def record_execution_proof(
    conn: DbConn,
    proof: OperatorExecutionProofV1,
    mutations: Sequence[MutationOutcomeV1],
    *,
    expected_mutations: tuple[str, ...] = PILOT_MUTATIONS,
) -> str:
    """Append a proof and the mutation outcomes that establish it. Returns the proof hash.

    Raises:
        ProofIncomplete: the mutation set is missing a member, names one nobody expected, or carries
            a mutation the corpus did NOT catch. All three mean the same thing — this proof does not
            establish what a proof claims — and recording it anyway would put a capability behind
            evidence that never existed.
    """
    outcomes = {mutation.mutation_name: mutation for mutation in mutations}
    if len(outcomes) != len(mutations):
        raise ProofIncomplete(
            "a mutation is reported twice: which outcome counts would be a list-order accident")

    missing = sorted(set(expected_mutations) - set(outcomes))
    if missing:
        raise ProofIncomplete(
            f"the proof does not run {missing}: a mutation set with a hole proves nothing about the "
            f"behaviour that hole covers, and the proof would claim a corpus that catches wrong "
            f"numbers it was never shown")
    unexpected = sorted(set(outcomes) - set(expected_mutations))
    if unexpected:
        raise ProofIncomplete(
            f"the proof reports {unexpected}, which are not in the mutation set version it pins: a "
            f"mutation outside the pinned set is one nobody can reproduce from the version alone")
    undetected = sorted(name for name, outcome in outcomes.items() if not outcome.detected)
    if undetected:
        raise ProofIncomplete(
            f"the gold corpus did NOT catch {undetected}: each of these is a wrong number rather "
            f"than an error, so a corpus that misses one would let that exact defect ship. The "
            f"outcome is real and the proof is refused — the corpus is what needs fixing")

    conn.execute(
        "INSERT INTO operator_execution_proof (proof_hash, signature, signature_version, "
        "compiler_version, renderer_version, physical_type_policy, topology_version, "
        "gold_corpus_hash, generated_project_hash, mutation_set_version, engine_versions) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "ON CONFLICT (proof_hash) DO NOTHING",
        (proof.content_hash, proof.signature, proof.signature_version, proof.compiler_version,
         proof.renderer_version, proof.physical_type_policy, proof.topology_version,
         proof.gold_corpus_hash, proof.generated_project_hash, proof.mutation_set_version,
         json.dumps([list(pair) for pair in sorted(proof.engine_versions)])))
    for mutation in mutations:
        conn.execute(
            "INSERT INTO operator_proof_mutation (proof_hash, mutation_name, detected, detail) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (proof_hash, mutation_name) DO NOTHING",
            (proof.content_hash, mutation.mutation_name, mutation.detected, mutation.detail))
    return proof.content_hash


def proof_for_bytes(conn: DbConn, generated_project_hash: str) -> str | None:
    """The proof taken over EXACTLY these bytes, or ``None``.

    This is the whole of "changed bytes invalidate a proof with no version bump": a proof is found
    by the bytes it was taken over, so different bytes simply do not find one. There is no staleness
    flag to set and no version to increment, because both would be claims someone had to remember.
    """
    row = conn.execute(
        "SELECT proof_hash FROM operator_execution_proof WHERE generated_project_hash = %s "
        "ORDER BY recorded_at DESC, proof_hash LIMIT 1", (generated_project_hash,)).fetchone()
    return None if row is None else row[0]


def record_renderer_dispatch(
    conn: DbConn, *, engine_id: str, dispatchable: Mapping[str, bool],
) -> None:
    """Record fact ONE for every operator kind: whether this build's renderer can emit it.

    ``dispatchable`` must cover the whole vocabulary. A kind left out would read as "not
    dispatchable" to any caller that defaults, and as "unknown" to one that does not — and the
    renderer either has a branch or it does not, so there is no third answer to record.
    """
    vocabulary = {kind.value for kind in OperatorKindV2}
    missing = sorted(vocabulary - set(dispatchable))
    if missing:
        raise ValueError(
            f"the dispatch record omits {missing}: the renderer either has a branch for an operator "
            f"or it does not, so an omitted kind is not a third answer — it is a claim nobody made "
            f"that a caller will read as one")
    unknown = sorted(set(dispatchable) - vocabulary)
    if unknown:
        raise ValueError(
            f"the dispatch record names {unknown}, which are not operator kinds: advertising a "
            f"capability for something the graph vocabulary cannot express advertises nothing")

    for kind, can_render in sorted(dispatchable.items()):
        conn.execute(
            "INSERT INTO engine_operator_capability (engine_id, operator_kind, "
            "renderer_dispatchable) VALUES (%s, %s, %s) "
            "ON CONFLICT (engine_id, operator_kind) DO UPDATE SET "
            "renderer_dispatchable = EXCLUDED.renderer_dispatchable",
            (engine_id, kind, can_render))


def set_execution_proof(
    conn: DbConn, *, engine_id: str, operator_kind: str, proof_hash: str,
) -> None:
    """Attach fact TWO — a proof — to an operator's capability row.

    Separate from :func:`record_renderer_dispatch` because the two facts are established by
    different things at different times: dispatch by building the renderer, proof by running the
    gold corpus. One function writing both would make it possible to record a proof as a side effect
    of a build.

    Raises:
        ValueError: no capability row for this operator, so there is nothing to attach a proof to —
            an operator with a proof and no dispatch record would be "proved" for a renderer that
            cannot emit it.
    """
    changed = conn.execute(
        "UPDATE engine_operator_capability SET execution_proof_hash = %s "
        "WHERE engine_id = %s AND operator_kind = %s",
        (proof_hash, engine_id, operator_kind)).rowcount
    if changed != 1:
        raise ValueError(
            f"no capability row for ({engine_id!r}, {operator_kind!r}): a proof attached to an "
            f"operator with no dispatch record would claim an operator is proved for a renderer "
            f"that may not be able to emit it at all")


def capability_of(
    conn: DbConn, *, engine_id: str, operator_kind: str,
) -> CapabilityV1 | None:
    """Both facts for one operator, or ``None`` if this build has never recorded it.

    ``None`` rather than a default: an engine or operator this build has never heard of must be
    treated as unsupported by the caller, and returning a fabricated row would make "never recorded"
    indistinguishable from "recorded as unsupported".
    """
    row = conn.execute(
        "SELECT renderer_dispatchable, execution_proof_hash FROM engine_operator_capability "
        "WHERE engine_id = %s AND operator_kind = %s", (engine_id, operator_kind)).fetchone()
    if row is None:
        return None
    return CapabilityV1(engine_id=engine_id, operator_kind=operator_kind,
                        renderer_dispatchable=row[0], execution_proof_hash=row[1])
