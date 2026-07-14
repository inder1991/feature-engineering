"""Audit fix I-3 — a governance denial must leave a DURABLE audit trace.

The overlay writes its COMMAND_DENIED row via `_deny_audited` on the REQUEST connection; the
route then raises HTTPException(409) and `get_conn`'s rollback discards that row — a denied
SoD-bypass probe left ZERO durable trace. The routes now mirror `deps.audit_access_denied` (the
403 path): BEFORE the 409, `_audit_governance_denial` re-records the denial on a SEPARATE
committing connection that survives the request rollback.

`_audit_governance_denial` is deliberately a no-op under the dev auth stub (like
`audit_access_denied`: a committing connection would pollute the rolled-back test DB), so these
tests SPY on the call itself: every governed route's not-accepted branch must invoke it with the
denial reason, and an ACCEPTED confirm must not.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.test_join_governance import _seed_join_with_evidence
from tests.featuregen.overlay.upload.test_table_fact_governance import _seed_grain

import featuregen.api.routes.governance as governance
from featuregen.contracts.envelopes import CommandResult
from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.config import _clear_overlay_config
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter


def _h(user: str, roles: str = "platform-admin") -> dict:
    return {"X-User": user, "X-Roles": roles}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    """The routes self-register the upload-context adapter and the app lifespan seals an overlay
    config — both PROCESS globals; clear them after every test (same as test_governance_routes)."""
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


@pytest.fixture
def overlay_env(conn):
    """Seeding preconditions (this module lives outside the overlay conftests' scope): the
    OVERLAY_FACT_* event schemas and the upload-context catalog adapter."""
    register_overlay_event_types(event_registry())
    ensure_upload_catalog_adapter()
    return conn


@pytest.fixture
def audit_spy(monkeypatch):
    """Spy on the durable-audit helper: the stub-mode no-op inside the REAL helper means the
    separate-connection write cannot be observed against the rolled-back test DB — assert the
    CALL instead (the helper's own mechanics mirror the already-tested audit_access_denied)."""
    calls: list[tuple[str, str, str | None]] = []

    def _spy(identity, action, reason):
        calls.append((identity.subject, action, reason))

    monkeypatch.setattr(governance, "_audit_governance_denial", _spy)
    return calls


def test_denied_repeat_confirm_records_durable_audit(client, overlay_env, audit_spy):
    _ref, key = _seed_join_with_evidence(overlay_env)

    r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert audit_spy == []                        # an ACCEPTED confirm records NO denial

    # same-subject repeat confirm -> overlay denies (SoD) -> 409, and the denial is durably audited
    r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 409
    ((subject, action, reason),) = audit_spy
    assert subject == "user:priya"
    assert key in action
    assert "already confirmed" in reason


@pytest.fixture
def denied_commands(monkeypatch):
    """Force the overlay commands to DENY so every route's not-accepted branch is exercised."""

    def _deny(conn, cmd):
        return CommandResult(accepted=False, aggregate_id=cmd.aggregate_id or "",
                             denied_reason="proposer may not confirm (four-eyes, §6.5)")

    monkeypatch.setattr(governance, "confirm_fact", _deny)
    monkeypatch.setattr(governance, "reject_fact", _deny)


def test_all_four_denied_routes_record_durable_audit(
        client, overlay_env, audit_spy, denied_commands):
    _jref, join_key = _seed_join_with_evidence(overlay_env)
    _gref, grain_key = _seed_grain(overlay_env)

    assert client.post(f"/governance/joins/{join_key}/confirm", json={},
                       headers=_h("priya")).status_code == 409
    assert client.post(f"/governance/joins/{join_key}/reject",
                       json={"category": "not_a_real_key"},
                       headers=_h("priya")).status_code == 409
    assert client.post(f"/governance/table-facts/{grain_key}/confirm", json={},
                       headers=_h("priya")).status_code == 409
    assert client.post(f"/governance/table-facts/{grain_key}/reject",
                       json={"category": "not_unique"},
                       headers=_h("priya")).status_code == 409

    assert len(audit_spy) == 4                    # every denied route recorded durably
    assert all("four-eyes" in reason for _s, _a, reason in audit_spy)
    assert {join_key, grain_key} <= {a.split()[-1] for _s, a, _r in audit_spy}
