"""S5 — persisting the executable output policy, the bound formula revision, and its compilations.

**A compiler bump leaves the bound-formula hash unchanged, and this store is where that stops being
a property of a dataclass and becomes a property of the database.** ``compiler_version`` is not a
column of ``bound_formula_revision``; it lives on ``bound_formula_compilation``, one row per compile.
Recompiling the same formula against the same inputs under a new toolchain therefore finds the
revision already present and records a second compilation — the identity holds, and the fact that a
different compiler produced it is not lost. The same split 1074 made for inventory observations,
where several observations legitimately share one content identity.

**An unresolved binding cannot be recorded.** :func:`record_bound_formula` takes the
:class:`~featuregen.materialize.bound_formula_v2.ExecutableOutputPolicyV2` and writes it first, so a
revision can never name an output policy that does not exist. The foreign key enforces the same rule
against a caller that bypasses the writer.

**Mismatches are recorded WITH the fields that were in scope.** S5's acceptance is a claim about
which fields a refusal may compare, so a stored mismatch that did not carry the comparison scope
could not be checked against the rule afterwards — and that rule is exactly the thing most likely to
be quietly widened later.
"""
from __future__ import annotations

import json

from featuregen.contracts import DbConn
from featuregen.formula.output_intent_v2 import AuthoredOutputIntentV2
from featuregen.formula.schema import AdditivityClass
from featuregen.materialize.bound_formula_v2 import (
    BoundFormulaRevisionV2,
    ExecutableOutputPolicyV2,
    bound_formula_hash_v2,
    executable_output_hash_v2,
)
from featuregen.materialize.output_resolution_v2 import IntentMismatchV1, compared_fields

__all__ = [
    "BoundFormulaStoreCorrupt",
    "compilations_of",
    "load_executable_output",
    "mismatches_for",
    "record_bound_formula",
    "record_executable_output",
    "record_intent_mismatch",
    "same_identity_compilations",
]


class BoundFormulaStoreCorrupt(Exception):
    """A stored row cannot reproduce the identity it is filed under — surfaced, never served."""


def record_executable_output(conn: DbConn, policy: ExecutableOutputPolicyV2) -> str:
    """Append an executable output policy under its content hash and return it. Idempotent."""
    output_hash = executable_output_hash_v2(policy)
    conn.execute(
        "INSERT INTO executable_output_policy (output_hash, physical_type, unit, currency_code, "
        "conversion_policy_ref, output_additivity, nullable, physical_type_policy) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (output_hash) DO NOTHING",
        (output_hash, policy.physical_type, policy.unit, policy.currency_code,
         policy.conversion_policy_ref, policy.output_additivity.value, policy.nullable,
         policy.physical_type_policy))
    return output_hash


def load_executable_output(conn: DbConn, output_hash: str) -> ExecutableOutputPolicyV2 | None:
    """One policy, reconstructed and identity-verified.

    A row that cannot reproduce the hash it is filed under is store corruption: it would be served
    as the description of a column it no longer describes.
    """
    row = conn.execute(
        "SELECT physical_type, unit, currency_code, conversion_policy_ref, output_additivity, "
        "nullable, physical_type_policy FROM executable_output_policy WHERE output_hash = %s",
        (output_hash,)).fetchone()
    if row is None:
        return None
    policy = ExecutableOutputPolicyV2(
        physical_type=row[0], unit=row[1], currency_code=row[2], conversion_policy_ref=row[3],
        output_additivity=AdditivityClass(row[4]), nullable=row[5], physical_type_policy=row[6])
    if executable_output_hash_v2(policy) != output_hash:
        raise BoundFormulaStoreCorrupt(
            f"executable output policy {output_hash} does not reproduce its own hash: it would be "
            f"served as the description of a column it no longer describes")
    return policy


def record_bound_formula(
    conn: DbConn,
    revision: BoundFormulaRevisionV2,
    policy: ExecutableOutputPolicyV2,
    *,
    compilation_id: str,
    compiled_at: str,
) -> str:
    """Append the output policy, the bound revision, and THIS compilation. Returns the revision id.

    The revision is idempotent on its content hash and the compilation is not: a second compile
    under a different compiler writes a second compilation row against the same revision, which is
    how "a compiler bump leaves the hash unchanged" stays visible instead of merely being true.

    Raises:
        ValueError: ``revision.executable_output_hash`` does not name ``policy``. Two arguments that
            could disagree would let a revision be filed against a policy it does not describe.
    """
    output_hash = executable_output_hash_v2(policy)
    if revision.executable_output_hash != output_hash:
        raise ValueError(
            f"the revision names executable output {revision.executable_output_hash} but the policy "
            f"offered hashes to {output_hash}: a revision filed against a policy it does not "
            f"describe would publish a column whose type nobody agreed to")

    record_executable_output(conn, policy)
    revision_id = bound_formula_hash_v2(revision)
    conn.execute(
        "INSERT INTO bound_formula_revision (revision_id, formula_content_hash, "
        "bound_input_set_hash, environment_id, executable_output_hash) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (revision_id) DO NOTHING",
        (revision_id, revision.formula_content_hash, revision.bound_input_set_hash,
         revision.environment_id, revision.executable_output_hash))
    conn.execute(
        "INSERT INTO bound_formula_compilation (compilation_id, revision_id, compiler_version, "
        "compiled_at) VALUES (%s, %s, %s, %s) ON CONFLICT (compilation_id) DO NOTHING",
        (compilation_id, revision_id, revision.compiler_version, compiled_at))
    return revision_id


def compilations_of(conn: DbConn, revision_id: str) -> tuple[tuple[str, str, str], ...]:
    """Every compilation of one revision as ``(compilation_id, compiler_version, compiled_at)``.

    More than one is the ORDINARY case, not a defect: it is what a compiler bump looks like when the
    computation did not change.
    """
    return tuple((row[0], row[1], row[2]) for row in conn.execute(
        "SELECT compilation_id, compiler_version, compiled_at FROM bound_formula_compilation "
        "WHERE revision_id = %s ORDER BY recorded_at, compilation_id", (revision_id,)).fetchall())


def same_identity_compilations(conn: DbConn, revision: BoundFormulaRevisionV2) -> tuple[str, ...]:
    """The compiler versions that produced this revision's identity, in order.

    Answers "did a compiler bump change anything" without re-deriving the compilation: if this
    returns two versions for one revision, the answer is no.
    """
    return tuple(item[1] for item in compilations_of(conn, bound_formula_hash_v2(revision)))


def record_intent_mismatch(
    conn: DbConn,
    mismatch: IntentMismatchV1,
    intent: AuthoredOutputIntentV2,
    *,
    mismatch_id: str,
    formula_content_hash: str,
    environment_id: str,
) -> str:
    """Record a refusal together with the fields that were eligible for comparison.

    The scope is derived from the intent here rather than taken as an argument, so a stored
    mismatch cannot claim a comparison scope the intent did not have.
    """
    conn.execute(
        "INSERT INTO output_intent_mismatch (mismatch_id, formula_content_hash, environment_id, "
        "code, field, intended, resolved, detail, compared_fields) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "ON CONFLICT (mismatch_id) DO NOTHING",
        (mismatch_id, formula_content_hash, environment_id, mismatch.code, mismatch.field,
         mismatch.intended, mismatch.resolved, mismatch.detail,
         json.dumps(list(compared_fields(intent)))))
    return mismatch_id


def mismatches_for(
    conn: DbConn, formula_content_hash: str,
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Recorded mismatches for one formula as ``(code, field, compared_fields)``."""
    return tuple((row[0], row[1], tuple(row[2])) for row in conn.execute(
        "SELECT code, field, compared_fields FROM output_intent_mismatch "
        "WHERE formula_content_hash = %s ORDER BY recorded_at, mismatch_id",
        (formula_content_hash,)).fetchall())
