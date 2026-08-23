"""The money at the PHYSICAL seam — every attempt reserved before egress, settled after.

▲ The case worth reading first is `test_A_MIXED_RETRY_AND_REPAIR_SEQUENCE_RESERVES_EVERY_PHYSICAL_CALL`
— the owner-requested envelope proof: retry and repair budgets are INDEPENDENT, so one structured
call is up to 1 + 2 retries + 2 repairs = FIVE physical calls, and a ceiling enforced anywhere above
`AuditingClient.call` under-counts by exactly the difference.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.intake.llm import (
    PROVIDER_AUTH_ERROR,
    PROVIDER_OK,
    PROVIDER_TRANSIENT,
    STATUS_FAILED,
    LLMRequest,
    LLMResult,
    drive_structured_call,
)
from featuregen.overlay.upload.dispatch_audit import AuditingClient, DispatchAuditContext
from featuregen.overlay.upload.llm_spend import (
    authorize_spend,
    reconcile_expired_spend,
)


@pytest.fixture
def durable_dsn(monkeypatch, _dsn):
    """FEATUREGEN_DSN at the test cluster (the dispatch-audit pattern) + spend-table cleanup —
    these commit for real and are write-once, so the fixture drops the guards to delete."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    yield _dsn
    with psycopg.connect(_dsn, autocommit=True) as c:
        for table, trigger in (
            ("llm_spend_settlement", "llm_spend_settlement_no_change"),
            ("llm_spend_reservation", "llm_spend_reservation_no_change"),
            ("llm_spend_authorization_revision", "llm_spend_authorization_revision_no_change"),
        ):
            c.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        c.execute("DELETE FROM llm_spend_settlement")
        c.execute("DELETE FROM llm_spend_reservation")
        c.execute("DELETE FROM llm_spend_authorization_revision")
        for table, trigger in (
            ("llm_spend_settlement", "llm_spend_settlement_no_change"),
            ("llm_spend_reservation", "llm_spend_reservation_no_change"),
            ("llm_spend_authorization_revision", "llm_spend_authorization_revision_no_change"),
        ):
            c.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
        c.execute("ALTER TABLE llm_dispatch_subject DISABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_outcome DISABLE TRIGGER llm_dispatch_outcome_no_mutation")
        c.execute("ALTER TABLE llm_dispatch DISABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("DELETE FROM llm_dispatch_outcome WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE logical_call_ref LIKE 'lc-spend%')")
        c.execute("DELETE FROM llm_dispatch_subject WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE logical_call_ref LIKE 'lc-spend%')")
        c.execute("DELETE FROM llm_dispatch WHERE logical_call_ref LIKE 'lc-spend%'")
        c.execute("ALTER TABLE llm_dispatch ENABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_subject ENABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_outcome ENABLE TRIGGER llm_dispatch_outcome_no_mutation")


class _ScriptedClient:
    """A provider that answers from a script — the driver's retries and repairs made visible."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def call(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        kind = self.script.pop(0)
        if kind == "transient":
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_TRANSIENT)
        if kind == "invalid":
            return LLMResult(output={"wrong": True}, self_reported_scores={}, call_ref=f"c{self.calls}",
                             status=PROVIDER_OK,
                             cost_metadata={"input_tokens": 100, "output_tokens": 50})
        return LLMResult(output={"answer": 42}, self_reported_scores={}, call_ref=f"c{self.calls}",
                         status=PROVIDER_OK,
                         cost_metadata={"input_tokens": 100, "output_tokens": 50})


def _validate(output):
    from featuregen.intake.llm import SchemaValidationError

    if "answer" not in output:
        raise SchemaValidationError("answer is required")


def _request():
    return LLMRequest(
        task="spend-seam-test", inputs={"instruction": "compute"},
        prompt_id="p-spend", prompt_version=1,
        output_schema_id="s-spend", output_schema_version=1, output_schema={"type": "object"},
        generation_settings={"provider": "fake", "model": "test", "max_tokens": 512})


def _authorize(dsn, *, calls, ref):
    with psycopg.connect(dsn) as conn:
        auth = authorize_spend(
            conn, action="AUTHOR_FORMULA", actor_subject="user:sam", job_identity=ref,
            member_identities=["m1"], provider_contract_hash="pc-seam", max_calls=calls,
            max_tokens=calls * 1_000, currency="USD", max_cost=str(calls), pricing_version="dev-v1",
            expires_at="2099-01-01T00:00:00Z")
        conn.commit()
        return auth


def _client(dsn, script, *, ref, calls):
    auth = _authorize(dsn, calls=calls, ref=ref)
    ctx = DispatchAuditContext(
        ingestion_run_id=None, stage="formula", subjects=[],
        spend_authorization_id=auth, spend_call_tokens=1_000, spend_call_cost="1")
    inner = _ScriptedClient(script)
    return inner, AuditingClient(inner, ctx, logical_call_ref=ref), auth


# ══ THE ENVELOPE ═══════════════════════════════════════════════════════════════════════════════
def test_A_MIXED_RETRY_AND_REPAIR_SEQUENCE_RESERVES_EVERY_PHYSICAL_CALL(durable_dsn):
    """▲ transient → retry, invalid → repair ×2, then valid: FIVE physical calls from ONE logical
    call, mixed classes — legal because retry and repair budgets are independent. Every one is
    reserved before egress and settled after, bound to its own dispatch_ref."""
    inner, client, auth = _client(
        durable_dsn, ["transient", "invalid", "invalid", "ok"], ref="lc-spend-mixed", calls=5)

    outcome = drive_structured_call(client, _request(), _validate, sleep=lambda _s: None)

    assert outcome.status in ("repaired", "retried")
    assert outcome.provider_calls == 4
    assert inner.calls == 4
    with psycopg.connect(durable_dsn) as conn:
        rows = conn.execute(
            "SELECT r.dispatch_ref, s.reservation_id IS NOT NULL "
            "  FROM llm_spend_reservation r "
            "  LEFT JOIN llm_spend_settlement s ON s.reservation_id = r.reservation_id "
            " WHERE r.spend_authorization_id = %s", (auth,)).fetchall()
    assert len(rows) == 4, "one reservation per PHYSICAL call, not per logical call"
    assert all(ref is not None for ref, _ in rows), "each bound durably to its dispatch"
    assert all(settled for _, settled in rows), "each settled from the provider's own usage"


def test_EXHAUSTION_FAILS_CLOSED_MID_SEQUENCE_with_no_egress(durable_dsn):
    """▲ The ceiling bites BETWEEN physical attempts of one logical call. The refused attempt
    leaves NO dispatch row — the reservation and the audit row rolled back together, so an attempt
    the budget refused has no audited egress, because none happened. And the driver fails closed
    immediately (auth arm) rather than burning retries against a ceiling that cannot move."""
    inner, client, auth = _client(
        durable_dsn, ["invalid", "invalid", "ok"], ref="lc-spend-exhausted", calls=2)

    outcome = drive_structured_call(client, _request(), _validate, sleep=lambda _s: None)

    assert outcome.status == STATUS_FAILED
    assert inner.calls == 2, "the third attempt never reached the provider"
    with psycopg.connect(durable_dsn) as conn:
        dispatches = conn.execute(
            "SELECT count(*) FROM llm_dispatch WHERE logical_call_ref = 'lc-spend-exhausted'"
        ).fetchone()[0]
        reservations = conn.execute(
            "SELECT count(*) FROM llm_spend_reservation WHERE spend_authorization_id = %s",
            (auth,)).fetchone()[0]
    assert dispatches == 2, "no audited egress for the refused attempt"
    assert reservations == 2


# ══ THE RECONCILER ═════════════════════════════════════════════════════════════════════════════
def test_THE_RECONCILER_SETTLES_FROM_THE_OUTCOME_never_from_hope(db):
    """response_received (or no outcome at all) → re-charge worst case: assuming an unaccounted
    call was free buys its tokens twice. transport_failed → zero, explicitly, so the ledger says
    "checked and unspent" rather than merely "expired"."""
    auth = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:sam", job_identity="j-rec",
        member_identities=[], provider_contract_hash="pc-rec", max_calls=10, max_tokens=10_000,
        currency="USD", max_cost="10", pricing_version="dev-v1",
        expires_at="2099-01-01T00:00:00Z")
    for ref, outcome in (("d-answered", "response_received"), ("d-transport", "transport_failed"),
                         ("d-silent", None)):
        db.execute(
            "INSERT INTO llm_dispatch (dispatch_ref, logical_call_ref, attempt_no, stage, task, "
            "input_hash, redacted_input) VALUES (%s, %s, 1, 'formula', 't', 'ih', '{}'::jsonb)",
            (ref, f"lc-{ref}"))
        if outcome:
            db.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) VALUES (%s, %s)",
                       (ref, outcome))
        db.execute(
            "INSERT INTO llm_spend_reservation (reservation_id, spend_authorization_id, "
            "dispatch_ref, reserved_calls, reserved_tokens, reserved_cost, expires_at) "
            "VALUES (%s, %s, %s, 1, 500, '0.50', '2026-01-01T00:00:00Z')",
            (f"rsv-{ref}", auth, ref))

    tallies = reconcile_expired_spend(db, now="2026-08-23T00:00:00Z")

    assert tallies == {"recharged_worst_case": 2, "released_transport_failed": 1}
    settled = dict(db.execute(
        "SELECT reservation_id, actual_tokens FROM llm_spend_settlement").fetchall())
    assert settled["rsv-d-answered"] == 500
    assert settled["rsv-d-silent"] == 500
    assert settled["rsv-d-transport"] == 0
