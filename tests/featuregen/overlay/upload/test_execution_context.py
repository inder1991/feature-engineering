"""A3 — the server-owned execution-context revision store (migration 1130).

What these tests pin, in the order the task states them: the migration applies (the session
fixture applies all migrations, so the table simply exists); append-only physics
(UPDATE/DELETE/TRUNCATE all refuse); content-addressed idempotency (the same semantic triple
ensures ONCE — same ``revision_id``, one row); closed vocabularies refused BEFORE any SQL (a
probe "connection" proves the conn is never touched); the load round-trip and the load-of-missing
None; R2's pin at the persistence layer (a logical plan's digest is IDENTICAL across two
different execution-context revisions — the context appears nowhere in logical material); and
content-hash uniqueness under a hostile double-insert (the DB backstop behind the store's
ON CONFLICT idiom).
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.execution_context import (
    EXECUTION_CONTEXT_ID_PREFIX,
    EXECUTION_CONTEXT_PURPOSES,
    ExecutionContextDefect,
    ExecutionContextRevisionV1,
    ensure_execution_context_revision,
    load_execution_context_revision,
)
from featuregen.overlay.upload.bridge_realization_proposal import FEATURE_GENERATION_PURPOSE
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    DrivingTimeRoleV1,
    IntervalBoundaryPolicyV1,
    KnowledgeTimeBasisV2,
    LogicalFeaturePlanV2,
    LogicalOperandBindingV1,
    LogicalPlanProvenanceV1,
    LogicalRelationshipSegmentV1,
    LogicalTemporalJoinSemanticsV1,
    StaticLinkMeaningV1,
    UnmatchedRowMeaningV1,
    logical_digest,
)

CUST = "cib::public.bo_cib_customer"
TXN = "ftr::public.comp_financial_tran_repos_dly"


class _NeverTouched:
    """A probe standing in for a connection: ANY attribute access fails the test. Handing this to
    the store proves a refusal happened BEFORE SQL, not merely before commit."""

    def __getattr__(self, name):  # pragma: no cover - reaching here IS the failure
        raise AssertionError(f"the store touched the connection ({name!r}) before validating")


def _ensure(conn, *, environment_id="env-uat", execution_tier=ExecutionTier.SANDBOX,
            purpose=FEATURE_GENERATION_PURPOSE) -> str:
    return ensure_execution_context_revision(
        conn, environment_id=environment_id, execution_tier=execution_tier, purpose=purpose)


def _count(conn) -> int:
    return conn.execute("SELECT count(*) FROM execution_context_revision").fetchone()[0]


# ── the migration applies (session fixture applies ALL migrations, 1130 included) ──────────────


def test_the_1130_table_exists_and_starts_empty_shaped(db) -> None:
    row = db.execute(
        "SELECT revision_id, environment_id, execution_tier, purpose, content_hash, recorded_at "
        "FROM execution_context_revision LIMIT 0").fetchone()
    assert row is None


# ── content-addressed idempotency ──────────────────────────────────────────────────────────────


def test_ensure_twice_is_one_row_same_id(db) -> None:
    first = _ensure(db)
    second = _ensure(db)
    assert first == second
    assert first.startswith(EXECUTION_CONTEXT_ID_PREFIX)
    assert db.execute(
        "SELECT count(*) FROM execution_context_revision WHERE revision_id = %s",
        (first,)).fetchone()[0] == 1


def test_the_id_is_content_derived_never_random(db) -> None:
    """The revision_id is a pure function of the semantic triple: prefix + canonical hash."""
    revision = ExecutionContextRevisionV1(
        environment_id="env-uat", execution_tier=ExecutionTier.SANDBOX,
        purpose=FEATURE_GENERATION_PURPOSE)
    assert _ensure(db) == revision.revision_id
    assert revision.revision_id == EXECUTION_CONTEXT_ID_PREFIX + revision.content_hash


def test_distinct_triples_mint_distinct_revisions(db) -> None:
    sandbox_uat = _ensure(db)
    production_uat = _ensure(db, execution_tier=ExecutionTier.PRODUCTION)
    sandbox_prod = _ensure(db, environment_id="env-prod")
    assert len({sandbox_uat, production_uat, sandbox_prod}) == 3


def test_the_tier_persists_as_the_enum_value_spelling(db) -> None:
    """The DB stores the StrEnum VALUE ('sandbox'/'production') — the spelling every jsonb
    applicability-scope payload already persists — never the Python member NAME."""
    revision_id = _ensure(db, execution_tier=ExecutionTier.PRODUCTION)
    stored = db.execute(
        "SELECT execution_tier FROM execution_context_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()[0]
    assert stored == ExecutionTier.PRODUCTION.value == "production"


# ── closed vocabularies, refused BEFORE SQL ────────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["STAGING", "Sandbox", "SANDBOX", "", None, 3])
def test_unknown_tier_refused_before_any_sql(tier) -> None:
    with pytest.raises(ExecutionContextDefect):
        ensure_execution_context_revision(
            _NeverTouched(), environment_id="env-uat", execution_tier=tier,
            purpose=FEATURE_GENERATION_PURPOSE)


@pytest.mark.parametrize("purpose", ["serving", "feature_generation ", "", None, 3])
def test_unknown_purpose_refused_before_any_sql(purpose) -> None:
    with pytest.raises(ExecutionContextDefect):
        ensure_execution_context_revision(
            _NeverTouched(), environment_id="env-uat",
            execution_tier=ExecutionTier.SANDBOX, purpose=purpose)


@pytest.mark.parametrize("environment_id", ["", "   ", None, 3])
def test_blank_environment_refused_before_any_sql(environment_id) -> None:
    with pytest.raises(ExecutionContextDefect):
        ensure_execution_context_revision(
            _NeverTouched(), environment_id=environment_id,
            execution_tier=ExecutionTier.SANDBOX, purpose=FEATURE_GENERATION_PURPOSE)


def test_the_purpose_vocabulary_is_the_platform_constant() -> None:
    assert EXECUTION_CONTEXT_PURPOSES == (FEATURE_GENERATION_PURPOSE,)


# ── load ───────────────────────────────────────────────────────────────────────────────────────


def test_load_round_trip(db) -> None:
    revision_id = _ensure(db, environment_id="env-prod",
                          execution_tier=ExecutionTier.PRODUCTION)
    loaded = load_execution_context_revision(db, revision_id)
    assert loaded is not None
    assert loaded.revision_id == revision_id
    assert loaded.environment_id == "env-prod"
    assert loaded.execution_tier is ExecutionTier.PRODUCTION
    assert loaded.purpose == FEATURE_GENERATION_PURPOSE
    assert loaded.recorded_at is not None
    assert loaded.revision_id == EXECUTION_CONTEXT_ID_PREFIX + loaded.content_hash


def test_load_of_missing_id_returns_none(db) -> None:
    assert load_execution_context_revision(db, "ecx_" + "0" * 64) is None


# ── append-only physics ────────────────────────────────────────────────────────────────────────


def test_update_and_delete_refuse(db) -> None:
    _ensure(db)
    for statement in ("UPDATE execution_context_revision SET recorded_at = now()",
                      "DELETE FROM execution_context_revision"):
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            with db.transaction():
                db.execute(statement)
        assert "append-only" in str(excinfo.value)
        assert "execution_context_revision" in str(excinfo.value)


def test_truncate_refuses(db) -> None:
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        with db.transaction():
            db.execute("TRUNCATE execution_context_revision")
    assert "append-only" in str(excinfo.value)


# ── the DB's named CHECKs are a real backstop behind the store's validation ────────────────────


@pytest.mark.parametrize("column,value", [
    ("execution_tier", "staging"),
    ("purpose", "serving"),
    ("environment_id", "   "),
])
def test_the_named_checks_refuse_off_vocabulary_rows(db, column, value) -> None:
    row = {"revision_id": "ecx_" + "e" * 64, "environment_id": "env-uat",
           "execution_tier": "sandbox", "purpose": "feature_generation",
           "content_hash": "e" * 64}
    row[column] = value
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO execution_context_revision (revision_id, environment_id, "
                "execution_tier, purpose, content_hash) VALUES (%s, %s, %s, %s, %s)",
                (row["revision_id"], row["environment_id"], row["execution_tier"],
                 row["purpose"], row["content_hash"]))


def test_content_hash_uniqueness_survives_a_hostile_double_insert(db) -> None:
    """The store's ON CONFLICT rides the revision_id PK; the content_hash UNIQUE is the backstop
    that refuses the same content smuggled in under a DIFFERENT id (concurrent-ish writers can
    only converge on one row per triple)."""
    revision_id = _ensure(db)
    content_hash = db.execute(
        "SELECT content_hash FROM execution_context_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()[0]
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO execution_context_revision (revision_id, environment_id, "
                "execution_tier, purpose, content_hash) VALUES (%s, 'env-uat', 'sandbox', "
                "'feature_generation', %s)", ("ecx_" + "f" * 64, content_hash))
    assert _count(db) == 1


def test_store_level_double_ensure_converges_not_errors(db) -> None:
    """Two writers ensuring the same triple both succeed and observe the SAME revision — the
    ON CONFLICT DO NOTHING + read-back idiom, not an error surfaced to the second writer."""
    ids = {_ensure(db) for _ in range(3)}
    assert len(ids) == 1
    assert _count(db) == 1


# ── R2's pin at the persistence layer ──────────────────────────────────────────────────────────


def _semantics() -> LogicalTemporalJoinSemanticsV1:
    return LogicalTemporalJoinSemanticsV1(
        effective_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        knowledge_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        driving_time_role=DrivingTimeRoleV1.CUTOFF_PARAMETER,
        interval_boundary_policy=IntervalBoundaryPolicyV1.CLOSED_OPEN,
        unmatched_row_meaning=UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE,
        static_link_meaning=StaticLinkMeaningV1.REFUSE,
    )


def _logical_plan() -> LogicalFeaturePlanV2:
    return LogicalFeaturePlanV2(
        canonical_definition_content_hash="c" * 64,
        canonical_definition_revision_id="cdr_1",
        operation="sum_window_delta",
        operand_bindings=(
            LogicalOperandBindingV1(
                role="amount", logical_column_ref=f"{TXN}.actual_tran_amt_aed",
                governed_semantic_revision_id="sem_amount_1"),
        ),
        output_grain_key_refs=(f"{CUST}.cust_num",),
        selected_parameters=(("window_days", 30),),
        relationship_path=(
            LogicalRelationshipSegmentV1(
                left_endpoint_refs=(f"{CUST}.cust_num",),
                right_endpoint_refs=(f"{TXN}.cif_id",),
                temporal_semantics=_semantics()),
        ),
        formula_policy_identities=(("direction_value_map", "pol_dir_1"),),
        provenance=LogicalPlanProvenanceV1(),
    )


def _payload_mentions_context(payload) -> bool:
    if isinstance(payload, dict):
        return any("execution_context" in key or _payload_mentions_context(value)
                   for key, value in payload.items())
    if isinstance(payload, (list, tuple)):
        return any(_payload_mentions_context(item) for item in payload)
    return isinstance(payload, str) and payload.startswith(EXECUTION_CONTEXT_ID_PREFIX)


def test_r2_logical_digest_identical_across_two_context_revisions(db) -> None:
    """R2 at the persistence layer: minting DIFFERENT execution-context revisions changes
    nothing about a logical plan's digest, and the context appears nowhere in logical material."""
    plan = _logical_plan()
    digest_before = logical_digest(plan)
    sandbox = _ensure(db, environment_id="env-uat", execution_tier=ExecutionTier.SANDBOX)
    production = _ensure(db, environment_id="env-prod", execution_tier=ExecutionTier.PRODUCTION)
    assert sandbox != production
    assert logical_digest(plan) == digest_before
    assert logical_digest(_logical_plan()) == digest_before
    assert not _payload_mentions_context(plan.content_payload())
