"""Delivery C5 Task 2 — the standalone PRE-DISPATCH audit writer (``record_dispatch``).

Durability model under test: the writer commits an immutable ``llm_dispatch`` header +
``llm_dispatch_subject`` rows (migration 1005) on a FRESH independent connection resolved from
``get_settings().dsn`` (the ``test_ingestion_run`` durable pattern), BEFORE any physical provider
request — so the egress authorization evidence survives the surrounding upload transaction rolling
back. Unlike the post-egress ``_record_llm_call_durable`` (best-effort degrade), this writer is
FAIL-CLOSED: a write that cannot be durably committed raises ``AuditUnavailable``, and the caller
must then NOT dispatch (wired in C5-T3).
"""
from __future__ import annotations

import hashlib
import json

import psycopg
import pytest

from featuregen.formula.control import LeaseFence, LeaseFenceLost
from featuregen.intake.llm import LLMRequest, compute_input_hash
from featuregen.overlay.upload.dispatch_audit import (
    AuditIntegrityError,
    AuditUnavailable,
    compute_physical_request_hash,
    formula_dispatches_reconciled,
    record_dispatch,
)


@pytest.fixture
def durable_dsn(monkeypatch, _dsn):
    """Point FEATUREGEN_DSN at the test cluster so the fresh-connection path really commits — and
    clean the committed rows up afterwards (they outlive any request rollback BY DESIGN, and both
    tables are write-once: as table owner, drop the trigger guards just long enough to delete,
    mirroring test_enrich_llm's llm_call cleanup)."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    created: list[str] = []   # logical_call_refs this test committed
    yield created
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "DISABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_outcome "
                  "DISABLE TRIGGER llm_dispatch_outcome_no_mutation")
        c.execute("ALTER TABLE llm_dispatch DISABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("DELETE FROM llm_dispatch_subject WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE logical_call_ref = ANY(%s))",
                  (created,))
        c.execute("DELETE FROM llm_dispatch WHERE logical_call_ref = ANY(%s)", (created,))
        c.execute("ALTER TABLE llm_dispatch ENABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "ENABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_outcome "
                  "ENABLE TRIGGER llm_dispatch_outcome_no_mutation")
        c.execute("DELETE FROM queue WHERE message_id='dispatch-fence-test'")


_SUBJECTS = [
    {"catalog_source": "deposits", "object_ref": "public.accounts", "logical_ref": "accounts",
     "field_names": ["balance", "opened_at"]},
    {"catalog_source": "deposits", "object_ref": "public.customers", "logical_ref": "customers",
     "field_names": ["customer_id"]},
]

# Already egress-approved REDACTED inputs (the writer stores them verbatim, never raw upload text).
_REDACTED_INPUT = {"intent": "classify", "columns": ["balance", "opened_at"]}


def _record(**overrides) -> str:
    kwargs = dict(logical_call_ref="log_c5t2", attempt_no=1, ingestion_run_id=None,
                  stage="enrichment", task="overlay.enrich.concept",
                  redacted_input=_REDACTED_INPUT, input_hash="sha256:deadbeef",
                  subjects=_SUBJECTS, redaction_version="pii-v3", provider="anthropic",
                  model="claude-sonnet-5", prompt_version=1, schema_version=1)
    kwargs.update(overrides)
    return record_dispatch(**kwargs)


# ── the independent-commit write ──────────────────────────────────────────────────────────────────


def test_record_dispatch_commits_header_and_subjects_independently(durable_dsn, _dsn) -> None:
    """One immutable header + one subject row per subject, committed on the writer's OWN
    connection — a FRESH connection sees them without any caller-side commit."""
    durable_dsn.append("log_c5t2")
    ref = _record()
    assert ref.startswith("disp_")
    with psycopg.connect(_dsn) as fresh:   # fresh conn: the write must ALREADY be committed
        header = fresh.execute(
            "SELECT logical_call_ref, attempt_no, ingestion_run_id, stage, task, input_hash, "
            "redacted_input, redaction_version, provider, model, prompt_version, schema_version "
            "FROM llm_dispatch WHERE dispatch_ref = %s", (ref,)).fetchone()
        subjects = fresh.execute(
            "SELECT catalog_source, object_ref, logical_ref, field_names "
            "FROM llm_dispatch_subject WHERE dispatch_ref = %s ORDER BY id", (ref,)).fetchall()
    assert header == ("log_c5t2", 1, None, "enrichment", "overlay.enrich.concept",
                      "sha256:deadbeef", _REDACTED_INPUT, "pii-v3", "anthropic",
                      "claude-sonnet-5", 1, 1)
    assert subjects == [("deposits", "public.accounts", "accounts", ["balance", "opened_at"]),
                        ("deposits", "public.customers", "customers", ["customer_id"])]


# ── idempotent replay (UNIQUE(logical_call_ref, attempt_no)) ──────────────────────────────────────


def test_replay_of_same_logical_call_and_attempt_returns_existing_ref(durable_dsn, _dsn) -> None:
    """An idempotent replay of an already-audited attempt returns the EXISTING dispatch_ref —
    no raise, no double insert (the migration's UNIQUE key holds)."""
    durable_dsn.append("log_c5t2_replay")
    first = _record(logical_call_ref="log_c5t2_replay")
    second = _record(logical_call_ref="log_c5t2_replay")
    assert second == first
    with psycopg.connect(_dsn) as fresh:
        headers = fresh.execute(
            "SELECT count(*) FROM llm_dispatch WHERE logical_call_ref = %s",
            ("log_c5t2_replay",)).fetchone()[0]
        subjects = fresh.execute(
            "SELECT count(*) FROM llm_dispatch_subject WHERE dispatch_ref = %s",
            (first,)).fetchone()[0]
    assert headers == 1     # one physical dispatch record per attempt — never duplicated
    assert subjects == 2    # the replay appended no duplicate subject rows


def test_replay_with_changed_physical_request_fails_integrity(durable_dsn) -> None:
    durable_dsn.append("log_c5t2_changed")
    _record(
        logical_call_ref="log_c5t2_changed",
        physical_request_hash="physical-a",
    )
    with pytest.raises(AuditIntegrityError):
        _record(
            logical_call_ref="log_c5t2_changed",
            physical_request_hash="physical-b",
        )


def test_replay_with_changed_formula_turn_identity_fails_integrity(
    durable_dsn,
) -> None:
    durable_dsn.append("log_c5t2_formula_identity")
    _record(
        logical_call_ref="log_c5t2_formula_identity",
        physical_request_hash="physical-a",
        call_role="formula.author",
        turn_index=0,
        canonical_turn_input_hash="logical-a",
        provider_contract_hash="contract-a",
        prompt_content_hash="prompt-a",
        schema_content_hash="schema-a",
    )
    with pytest.raises(AuditIntegrityError):
        _record(
            logical_call_ref="log_c5t2_formula_identity",
            physical_request_hash="physical-a",
            call_role="formula.author",
            turn_index=1,
            canonical_turn_input_hash="logical-a",
            provider_contract_hash="contract-a",
            prompt_content_hash="prompt-a",
            schema_content_hash="schema-a",
        )


def test_physical_request_hash_distinguishes_repair_payloads() -> None:
    common = {
        "task": "formula.author",
        "prompt_id": "formula-author-v1",
        "prompt_version": 1,
        "output_schema_id": "typed-formula-v1",
        "output_schema_version": 1,
        "generation_settings": {"provider": "anthropic", "model": "test"},
    }
    first = LLMRequest(
        **common,
        inputs={"catalog_metadata": {}, "_repair_errors": ["first error"]},
    )
    second = LLMRequest(
        **common,
        inputs={"catalog_metadata": {}, "_repair_errors": ["second error"]},
    )
    assert compute_input_hash(first.inputs) == compute_input_hash(second.inputs)
    assert compute_physical_request_hash(first) != compute_physical_request_hash(second)


def test_formula_dispatch_reconciliation_requires_every_audit_dimension() -> None:
    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    def digest(value) -> str:
        return hashlib.sha256(json.dumps(
            value, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    schema = {"type": "object", "additionalProperties": False}
    inputs = {"instruction": "author safely", "catalog_metadata": {}}
    settings = {"provider": "anthropic", "model": "test"}
    request = LLMRequest(
        task="formula.author",
        prompt_id="formula-author-v1",
        prompt_version=1,
        inputs=inputs,
        output_schema_id="typed-formula-v1",
        output_schema_version=1,
        generation_settings=settings,
        output_schema=schema,
    )
    input_hash = compute_input_hash(inputs)
    prompt_hash = digest(inputs["instruction"])
    schema_hash = digest(schema)
    contract_hash = digest({
        "call_role": "formula.author",
        "provider": "anthropic",
        "model": "test",
        "generation_settings": settings,
        "prompt_id": "formula-author-v1",
        "prompt_version": 1,
        "prompt_content_hash": prompt_hash,
        "output_schema_id": "typed-formula-v1",
        "output_schema_version": 1,
        "schema_content_hash": schema_hash,
    })
    dispatch = (
        "dispatch-1", "formula.author", input_hash, inputs, "anthropic", "test", 1, 1,
        compute_physical_request_hash(request), "formula.author", input_hash,
        contract_hash, prompt_hash, schema_hash,
    )
    link = (
        "call-1", "formula.author", "anthropic", "test", "formula-author-v1", 1,
        "typed-formula-v1", 1, settings, input_hash,
    )

    class _Conn:
        def __init__(self, *, dispatches=(dispatch,), links=(link,),
                     outcomes=(("response_received",),), trace=((1,),)):
            self.dispatches = dispatches
            self.links = links
            self.outcomes = outcomes
            self.trace = trace

        def execute(self, sql, *args):
            if "FROM llm_dispatch WHERE authoring_run_id" in sql:
                return _Result(self.dispatches)
            if "FROM llm_call_dispatch" in sql:
                return _Result(self.links)
            if "FROM document_type_registry" in sql:
                return _Result(((schema,),))
            if "FROM llm_dispatch_outcome" in sql:
                return _Result(self.outcomes)
            if "FROM formula_authoring_trace_event" in sql:
                return _Result(self.trace)
            raise AssertionError(sql)

    assert formula_dispatches_reconciled(_Conn(), "run")
    assert not formula_dispatches_reconciled(_Conn(dispatches=()), "run")
    broken_dispatch = list(dispatch)
    broken_dispatch[8] = "0" * 64
    assert not formula_dispatches_reconciled(
        _Conn(dispatches=(tuple(broken_dispatch),)), "run")
    assert not formula_dispatches_reconciled(_Conn(links=()), "run")
    assert not formula_dispatches_reconciled(_Conn(outcomes=()), "run")
    assert not formula_dispatches_reconciled(_Conn(trace=((0,),)), "run")


def test_formula_dispatch_reconciliation_query_runs_against_postgres(db) -> None:
    assert not formula_dispatches_reconciled(db, "missing-authoring-run")


def test_a_new_attempt_is_a_new_dispatch_record(durable_dsn) -> None:
    """A RETRY is a new attempt_no — a distinct dispatch record, not a replay."""
    durable_dsn.append("log_c5t2_retry")
    first = _record(logical_call_ref="log_c5t2_retry", attempt_no=1)
    second = _record(logical_call_ref="log_c5t2_retry", attempt_no=2)
    assert second != first


def test_formula_dispatch_insert_is_conditioned_on_the_live_queue_fence(
    durable_dsn, _dsn
) -> None:
    logical_ref = "log_c5t2_fenced"
    durable_dsn.append(logical_ref)
    with psycopg.connect(_dsn, autocommit=True) as setup:
        queue_id = setup.execute(
            "INSERT INTO queue "
            "(message_id,partition_key,handler,payload,status,lease_owner,"
            "lease_expires_at,lease_fence) "
            "VALUES ('dispatch-fence-test','dispatch-fence-test',"
            "'recipe_formula_shadow.author.v1','{}'::jsonb,'leased','worker-a',"
            "now() + interval '5 minutes',11) RETURNING id"
        ).fetchone()[0]
    fence = LeaseFence(queue_id, "worker-a", 11)
    dispatch_ref = _record(
        logical_call_ref=logical_ref,
        lease_fence=fence,
    )
    with psycopg.connect(_dsn, autocommit=True) as check:
        assert check.execute(
            "SELECT queue_id,lease_owner,lease_fence FROM llm_dispatch "
            "WHERE dispatch_ref=%s",
            (dispatch_ref,),
        ).fetchone() == (queue_id, "worker-a", 11)
        check.execute(
            "UPDATE queue SET lease_expires_at=now() - interval '1 second' "
            "WHERE id=%s",
            (queue_id,),
        )
    with pytest.raises(LeaseFenceLost):
        _record(logical_call_ref=logical_ref, lease_fence=fence)
    with pytest.raises(LeaseFenceLost):
        _record(
            logical_call_ref="log_c5t2_fenced_expired",
            lease_fence=fence,
        )
    with psycopg.connect(_dsn) as check:
        assert check.execute(
            "SELECT count(*) FROM llm_dispatch "
            "WHERE logical_call_ref='log_c5t2_fenced_expired'"
        ).fetchone()[0] == 0


# ── fail-closed durability (AuditUnavailable — the caller must NOT dispatch) ──────────────────────


def test_unreachable_dsn_raises_audit_unavailable(monkeypatch) -> None:
    """A configured-but-unreachable DSN must raise — never silently succeed or degrade to a
    request connection (a missing PRE-dispatch audit means the provider request must not go)."""
    monkeypatch.setenv("FEATUREGEN_DSN", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    with pytest.raises(AuditUnavailable):
        _record(logical_call_ref="log_c5t2_down")


def test_missing_dsn_raises_audit_unavailable(monkeypatch) -> None:
    """No DSN at all ⟹ no durable commit is possible ⟹ fail closed (no best-effort fallback
    connection exists for this writer — that is the point)."""
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    with pytest.raises(AuditUnavailable):
        _record(logical_call_ref="log_c5t2_nodsn")
