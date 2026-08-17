"""S9 — on-demand sandbox verification (1080).

*"Verification executes with no publication capability present; two attempts do not share a staging
path; staleness is three-way — a comparable ``OBSERVED`` input that changed ⇒ stale, an identical
observation ⇒ current, ``UNPINNED`` ⇒ neither, remaining labelled unverifiable and never claimed
current or stale on content; observation strength is never ``PINNED`` without enforced reads."*

The third clause is the one with a trap in it. A two-way answer forces ``UNPINNED`` into "current"
or "stale", and BOTH are lies: "current" claims a check nobody could run, "stale" claims a change
nobody observed. So the tests check the third value directly, and check that drifted policies do
**not** move it.
"""
from __future__ import annotations

import inspect

import psycopg
import pytest

from featuregen.overlay.upload import verification_store
from featuregen.overlay.upload.verification_revisions import (
    RetentionStateV1,
    VerificationExecutionIdentityV1,
    VerifiedOutputRevisionV1,
)
from featuregen.overlay.upload.verification_store import (
    ObservationStrengthV1,
    StagingPathCollision,
    StalenessV1,
    UnenforcedPinnedReads,
    label_for,
    record_verification_attempt,
    record_verified_output,
    set_retention_state,
    staleness_of,
)

GAR = "gar-0001"
ARTIFACT = "art-1"
ROOT = "hdfs://nn/staging/featuregen"
POLICY_A = "sha256:policy-direction"
POLICY_B = "sha256:policy-status"


def _identity(*, attempt: int = 1, check_set_hash: str = "sha256:check-set",
              observation: str = "obs-1") -> VerificationExecutionIdentityV1:
    return VerificationExecutionIdentityV1(
        generation_authorization_revision_id=GAR, check_set_hash=check_set_hash,
        inventory_observation_id=observation, attempt=attempt)


def _attempt(db, *, attempt: int = 1, **overrides) -> str:
    return record_verification_attempt(
        db, _identity(attempt=attempt, **overrides), sealed_artifact_id=ARTIFACT,
        staging_root=ROOT, started_at="2026-08-17T00:00:00Z")


def _verified(
    execution_hash: str, *, revision_id: str = "vo-1",
    strength: ObservationStrengthV1 = ObservationStrengthV1.OBSERVED,
    policies: tuple[str, ...] = (POLICY_A, POLICY_B),
) -> VerifiedOutputRevisionV1:
    return VerifiedOutputRevisionV1(
        revision_id=revision_id, execution_hash=execution_hash,
        check_set_hash="sha256:check-set",
        validator_versions=(("schema", "1"), ("nulls", "2")),
        pinned_policy_hashes=policies, input_observation_strength=strength.value)


# ══ ACCEPTANCE 1 — verification executes with NO PUBLICATION CAPABILITY present ═════════════════
def test_THE_WRITER_TAKES_NO_PUBLICATION_CAPABILITY():
    """Enforced by ABSENCE. A parameter would eventually be passed, and a stored one eventually
    read as "may publish" — the exact attestation verification is defined to run without."""
    parameters = set(inspect.signature(record_verification_attempt).parameters)
    assert parameters == {"conn", "identity", "sealed_artifact_id", "staging_root", "started_at"}
    assert not any("publi" in name or "capab" in name for name in parameters), parameters


def test_THE_SCHEMA_CARRIES_NO_PUBLICATION_COLUMN(db):
    """Not a nullable one, not a boolean defaulting to false — none."""
    for table in ("verification_attempt", "verified_output_revision"):
        columns = {row[0] for row in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()}
        assert not any("publi" in column or "capab" in column for column in columns), (
            table, sorted(columns))


def test_the_MODULE_never_mentions_publication_in_its_code():
    """The code, not the prose: this module explains at length that it takes no capability, and a
    whole-file grep would read that explanation as the thing it disclaims."""
    code = _code_only(verification_store)
    for word in ("publish", "publication", "capability"):
        assert word not in code, word


def test_a_verification_RUNS_and_records_without_one(db):
    execution_hash = _attempt(db)
    row = db.execute(
        "SELECT sealed_artifact_id, attempt, staging_path FROM verification_attempt "
        "WHERE execution_hash = %s", (execution_hash,)).fetchone()
    assert row[0] == ARTIFACT
    assert row[1] == 1
    assert row[2].endswith("/attempt=1")


def test_a_verification_must_name_the_EXACT_sealed_artifact(db):
    """§0.3 asks for THE EXACT artifact; one naming none verifies whatever happened to be
    rendered."""
    with pytest.raises(ValueError, match="whatever happened to be rendered"):
        record_verification_attempt(db, _identity(), sealed_artifact_id="  ",
                                    staging_root=ROOT, started_at="t")


# ══ ACCEPTANCE 2 — TWO ATTEMPTS DO NOT SHARE A STAGING PATH ════════════════════════════════════
def test_TWO_ATTEMPTS_GET_DIFFERENT_PATHS(db):
    """The existing staging root is GENERATION-scoped, so without `attempt` a second verification
    writes over the first and "the exact staging output" names two things."""
    first = _attempt(db, attempt=1)
    second = _attempt(db, attempt=2)
    assert first != second

    paths = {row[0] for row in db.execute(
        "SELECT staging_path FROM verification_attempt").fetchall()}
    assert len(paths) == 2
    assert paths == {f"{ROOT}/{GAR}/attempt=1", f"{ROOT}/{GAR}/attempt=2"}


def test_ATTEMPT_IS_PART_OF_THE_EXECUTION_IDENTITY():
    """Not merely of the path: two attempts that hashed the same would be one execution with two
    outputs."""
    assert _identity(attempt=1).execution_hash != _identity(attempt=2).execution_hash


def test_a_PATH_COLLISION_IS_REFUSED_not_overwritten(db):
    """The first attempt's output may already be being read."""
    _attempt(db, attempt=1)
    # A second execution that differs only in something outside the path — same GAR, same attempt.
    with pytest.raises(StagingPathCollision, match="already holds"):
        record_verification_attempt(
            db, _identity(attempt=1, check_set_hash="sha256:a-different-check-set"),
            sealed_artifact_id=ARTIFACT, staging_root=ROOT, started_at="t")


def test_THE_DATABASE_ENFORCES_PATH_UNIQUENESS_TOO(db):
    """Against a caller that bypasses the writer."""
    _attempt(db, attempt=1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO verification_attempt (execution_hash, "
            "generation_authorization_revision_id, check_set_hash, inventory_observation_id, "
            "attempt, run_parameters, staging_path, sealed_artifact_id, started_at) "
            "VALUES (%s, %s, %s, %s, %s, '[]'::jsonb, %s, %s, %s)",
            ("sha256:other", "another-gar", "sha256:cs", "obs-1", 7,
             f"{ROOT}/{GAR}/attempt=1", ARTIFACT, "t"))


def test_ATTEMPT_ZERO_IS_REFUSED():
    """Zero would collide with the generation-scoped root the field exists to replace."""
    with pytest.raises(ValueError, match="counted from 1"):
        _identity(attempt=0)


def test_recording_the_same_attempt_twice_is_idempotent(db):
    _attempt(db, attempt=1)
    _attempt(db, attempt=1)
    assert db.execute("SELECT count(*) FROM verification_attempt").fetchone()[0] == 1


# ══ ACCEPTANCE 3 — STALENESS IS THREE-WAY ══════════════════════════════════════════════════════
def test_AN_OBSERVED_INPUT_THAT_CHANGED_IS_STALE(db):
    verified = _verified(_attempt(db))
    staleness, drifted = staleness_of(verified, current_policy_hashes=(POLICY_A,))
    assert staleness is StalenessV1.STALE
    assert drifted == (POLICY_B,)


def test_AN_IDENTICAL_OBSERVATION_IS_CURRENT(db):
    verified = _verified(_attempt(db))
    staleness, drifted = staleness_of(verified, current_policy_hashes=(POLICY_A, POLICY_B))
    assert staleness is StalenessV1.CURRENT
    assert drifted == ()


def test_UNPINNED_IS_NEITHER_current_NOR_stale(db):
    """The trap. A two-way answer forces this into one of the two, and BOTH are lies: "current"
    claims a check nobody could run, "stale" claims a change nobody observed."""
    verified = _verified(_attempt(db), strength=ObservationStrengthV1.UNPINNED)
    staleness, drifted = staleness_of(verified, current_policy_hashes=(POLICY_A, POLICY_B))
    assert staleness is StalenessV1.NEITHER
    assert drifted == ()


def test_UNPINNED_STAYS_NEITHER_EVEN_WHEN_POLICIES_DRIFTED(db):
    """Not because nothing moved — because nothing was PINNED, so no content comparison can
    attribute the movement to this output. Reporting it as stale would claim an observation nobody
    made."""
    verified = _verified(_attempt(db), strength=ObservationStrengthV1.UNPINNED)
    staleness, drifted = staleness_of(verified, current_policy_hashes=())
    assert staleness is StalenessV1.NEITHER
    assert drifted == ()

    # The discriminator: the SAME drift, observed, is stale.
    observed = _verified(_attempt(db), strength=ObservationStrengthV1.OBSERVED)
    assert staleness_of(observed, current_policy_hashes=())[0] is StalenessV1.STALE


def test_THE_REMAINING_CASE_IS_LABELLED_UNVERIFIABLE(db):
    """Never current, never stale, in one place — so two surfaces cannot describe one output
    differently."""
    unpinned = label_for(_verified(_attempt(db), strength=ObservationStrengthV1.UNPINNED),
                         current_policy_hashes=())
    assert unpinned.label == "unverifiable"
    assert unpinned.staleness.is_unverifiable is True

    observed = label_for(_verified(_attempt(db)), current_policy_hashes=(POLICY_A, POLICY_B))
    assert observed.label == "current"
    assert observed.staleness.is_unverifiable is False


def test_the_three_values_are_EXACTLY_three():
    """A fourth would be a case nobody decided; two would be the boolean this clause rejects."""
    assert {member.value for member in StalenessV1} == {"stale", "current", "neither"}
    assert {member.value for member in ObservationStrengthV1} == {"pinned", "observed", "unpinned"}


def test_the_drifted_policies_are_NAMED_not_counted(db):
    """A currency conversion changing is a different conversation from a status policy changing."""
    verified = _verified(_attempt(db))
    _staleness, drifted = staleness_of(verified, current_policy_hashes=())
    assert set(drifted) == {POLICY_A, POLICY_B}


def test_an_output_pinning_NO_policies_is_refused(db):
    """It could never go stale: a policy changed afterwards would leave it vouching for an artifact
    whose meaning moved, with nothing able to notice."""
    with pytest.raises(ValueError, match="cannot go stale"):
        _verified(_attempt(db), policies=())


# ══ ACCEPTANCE 4 — PINNED is never claimed without ENFORCED READS ══════════════════════════════
def test_PINNED_WITHOUT_ENFORCED_READS_IS_REFUSED(db):
    """Pinned means the run COULD ONLY have read what it pinned. Without enforcement that is a
    description of intent, and a staleness answer computed from it is about a promise."""
    verified = _verified(_attempt(db), strength=ObservationStrengthV1.PINNED)
    with pytest.raises(UnenforcedPinnedReads, match="description of intent"):
        record_verified_output(db, verified, reads_enforced=False)
    assert db.execute("SELECT count(*) FROM verified_output_revision").fetchone()[0] == 0


def test_PINNED_WITH_ENFORCED_READS_records(db):
    verified = _verified(_attempt(db), strength=ObservationStrengthV1.PINNED)
    record_verified_output(db, verified, reads_enforced=True)
    row = db.execute(
        "SELECT input_observation_strength, reads_enforced FROM verified_output_revision "
        "WHERE revision_id = %s", ("vo-1",)).fetchone()
    assert row == ("pinned", True)


def test_the_DATABASE_refuses_the_combination_too(db):
    """One of the two checks being absent is how the other gets removed."""
    execution_hash = _attempt(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO verified_output_revision (revision_id, execution_hash, check_set_hash, "
            "validator_versions, pinned_policy_hashes, input_observation_strength, "
            "reads_enforced, retention_state) "
            "VALUES (%s, %s, %s, '[]'::jsonb, '[\"sha256:p\"]'::jsonb, 'pinned', false, 'live')",
            ("vo-bad", execution_hash, "sha256:cs"))


def test_OBSERVED_and_UNPINNED_do_not_require_enforced_reads(db):
    """The rule is about the PINNED claim specifically — an observed run that did not enforce its
    reads is honest, and refusing it would push authors to over-claim."""
    for strength, revision in ((ObservationStrengthV1.OBSERVED, "vo-observed"),
                               (ObservationStrengthV1.UNPINNED, "vo-unpinned")):
        record_verified_output(
            db, _verified(_attempt(db), revision_id=revision, strength=strength),
            reads_enforced=False)
    assert db.execute("SELECT count(*) FROM verified_output_revision").fetchone()[0] == 2


# ══ retention is the ONE thing that moves ══════════════════════════════════════════════════════
def test_RETENTION_MOVES_and_nothing_else_does(db):
    """`live → marked_orphan → quarantined → swept`, reused from `runtime/blob_gc` verbatim rather
    than invented — one lifecycle, one meaning."""
    verified = _verified(_attempt(db))
    record_verified_output(db, verified, reads_enforced=False)

    set_retention_state(db, "vo-1", RetentionStateV1.MARKED_ORPHAN)
    set_retention_state(db, "vo-1", RetentionStateV1.QUARANTINED)
    assert db.execute(
        "SELECT retention_state FROM verified_output_revision WHERE revision_id = %s",
        ("vo-1",)).fetchone()[0] == "quarantined"

    with pytest.raises(psycopg.errors.RaiseException, match="except retention_state"):
        db.execute(
            "UPDATE verified_output_revision SET check_set_hash = %s WHERE revision_id = %s",
            ("sha256:rewritten", "vo-1"))


def test_a_verified_output_cannot_be_DELETED(db):
    record_verified_output(db, _verified(_attempt(db)), reads_enforced=False)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("DELETE FROM verified_output_revision WHERE revision_id = %s", ("vo-1",))


def test_a_QUARANTINED_output_is_not_servable(db):
    """A swept output's bytes are gone; a quarantined one's are pending deletion. Neither may be
    served, and the distinction matters to an operator deciding whether recovery is possible."""
    assert RetentionStateV1.LIVE.is_servable is True
    assert RetentionStateV1.MARKED_ORPHAN.is_servable is True
    assert RetentionStateV1.QUARANTINED.is_servable is False
    assert RetentionStateV1.SWEPT.is_servable is False


def test_an_attempt_is_APPEND_ONLY(db):
    execution_hash = _attempt(db)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(
            "UPDATE verification_attempt SET staging_path = %s WHERE execution_hash = %s",
            ("hdfs://nn/elsewhere", execution_hash))


def test_a_verified_output_must_name_an_attempt_that_EXISTS(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        record_verified_output(db, _verified("sha256:no-such-execution"), reads_enforced=False)


def _code_only(module) -> str:
    """A module's source with every docstring and comment removed."""
    lines, inside = [], False
    for raw in inspect.getsource(module).splitlines():
        line = raw.split("#", 1)[0] if not inside else raw
        fences = line.count('"""')
        if inside:
            if fences:
                inside = False
                line = line.split('"""', 1)[1]
            else:
                continue
        elif fences == 1:
            inside = True
            line = line.split('"""', 1)[0]
        elif fences >= 2:
            line = line.split('"""')[0] + line.rsplit('"""', 1)[1]
        lines.append(line)
    return "\n".join(lines).lower()
