# Join-Confirmation Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the API + React screen that lets two distinct platform-admins approve or reject the governed `approved_join` proposals Pass C discovers, driving the existing dual-admin confirmation loop and projecting the approved join to an operational graph edge.

**Architecture:** Three new units — a domain read/write helper module (`overlay/upload/join_governance.py`), a thin FastAPI router (`api/routes/governance.py`), and a React screen (`GovernanceReviewScreen.tsx`) — over the *unchanged* overlay confirmation state machine (`confirm_fact`/`reject_fact`/`_confirm_approved_join`). The route layer is transport-only; four-eyes and the two-distinct-admins rule stay in the overlay. Confirm/reject are keyed by `fact_key`, not `task_id`.

**Tech Stack:** Python 3.11 · FastAPI · psycopg3 · pytest (+ pytest-postgresql ephemeral PG) · React/TypeScript · uv.

**Spec:** `docs/superpowers/specs/2026-07-13-join-confirmation-surface-design.md` (v2). Read it — every task's requirements implicitly include it.

## Global Constraints

- Confirm/reject routes are **fact-key-based**: `POST /governance/joins/{fact_key}/confirm` and `.../reject`. The `GET` is `GET /sources/{source}/governance/joins`.
- RBAC gate `require_confirmer` = the **raw `"platform-admin"` role claim** in `identity.role_claims` (NOT the `platform_admin` permission bundle). This exactly matches what `overlay/join_confirmation.py:68` authorizes on.
- The **two distinct admins** rule is enforced inside the overlay (`_confirm_approved_join` denies a repeat subject). The route surfaces the denial; it does not re-implement distinctness.
- **Only** `PROPOSED` / `PARTIALLY_CONFIRMED` proposals are listed. VERIFIED/REJECTED are excluded. **No** verified-history view, **no** revoke endpoint/UI in this build.
- Confirm/reject **must validate `fact_type == "approved_join"`** on the loaded fact → **404** otherwise (no event written).
- On the **second (distinct-admin) confirmation → VERIFIED**, the route **synchronously projects** the operational edge (guarded by `projection_lag == 0`, fail-soft). The confirm response distinguishes `governance_status` from `operational_projection` (`projected` / `pending` / `not_applicable`).
- Request bodies are **backend-validated** (Pydantic): note `max_length=1000`, category a fixed enum; whitespace trimmed, empty → `None`.
- **No customer values** are rendered or egressed — only metadata evidence (score / signals / namespace / grain / explanation).
- `source` is compared **normalized** (lowercased, per `overlay/upload/object_ref.normalize_ref`).
- The list endpoint is **bounded**: `limit` default 100, max 500; `next_cursor` reserved (always `null` this build).
- Tests run with `uv run pytest <path> -q`. API tests set `FEATUREGEN_AUTH_STUB=1` and send `X-User` / `X-Roles` headers (see `api/deps.py:106-132`); a caller with `X-Roles: platform-admin` carries the claim.

## File Structure

- **Create** `src/featuregen/overlay/upload/join_governance.py` — the read model (`list_open_approved_join_proposals`), the approval-stream reader, the confirm/reject context bridge (`load_join_confirmation_context`), and the projection helper (`project_verified_join`). Pure domain; no FastAPI.
- **Create** `src/featuregen/api/routes/governance.py` — the three endpoints + Pydantic request models + overlay-error→HTTP mapping.
- **Create** `frontend/src/screens/GovernanceReviewScreen.tsx` — the review screen (v2 mockup).
- **Modify** `src/featuregen/overlay/join_confirmation.py` — thread an optional `note` into the PARTIALLY_CONFIRMED + CONFIRMED event payloads (dual path). (And `overlay/confirmation_commands.py` single path for symmetry.)
- **Modify** `src/featuregen/api/deps.py` — add `require_confirmer`.
- **Modify** `src/featuregen/identity/permissions.py` — add a `governance:confirm` permission to the `platform_admin` bundle (future reconciliation; not relied on).
- **Modify** `src/featuregen/api/app.py` — register the governance router.
- **Modify** `frontend/src/api.ts`, `frontend/src/nav.ts`, `frontend/src/App.tsx` — client functions + route registration.
- **Tests:** `tests/featuregen/api/test_governance_routes.py`, `tests/featuregen/overlay/upload/test_join_governance.py`, plus a small frontend test.

---

### Task 1: Approver-note threading into the confirm events

**Files:**
- Modify: `src/featuregen/overlay/join_confirmation.py` (the `OVERLAY_FACT_PARTIALLY_CONFIRMED` and `OVERLAY_FACT_CONFIRMED` `append_overlay_event` payloads inside `_confirm_approved_join`).
- Modify: `src/featuregen/overlay/confirmation_commands.py:165-170` (single-path `OVERLAY_FACT_CONFIRMED` payload — symmetry).
- Test: `tests/featuregen/overlay/upload/passc/test_confirm_note.py`

**Interfaces:**
- Consumes: the Phase 3A passc conftest (`tests/featuregen/overlay/upload/passc/conftest.py`): `passc_conn`, `_propose_join`, `build_join_ref`, `human_admin_1`, `human_admin_2`, and `_confirm_join`. `confirm_fact(conn, cmd)` reads `cmd.args`.
- Produces: `OVERLAY_FACT_PARTIALLY_CONFIRMED.payload["note"]` and `OVERLAY_FACT_CONFIRMED.payload["note"]` carry `cmd.args.get("note")` (or `None`). Task 3's approval reader depends on this key.

- [ ] **Step 1: Write the failing test.** A dual join, first admin confirms with a note; assert the PARTIALLY_CONFIRMED event payload carries it.

```python
# test_confirm_note.py — build a proposed dual join, confirm-with-note as admin1, read the raw stream.
from featuregen.contracts.envelopes import Command
from featuregen.overlay.confirmation_commands import confirm_fact
from featuregen.overlay.store import load_fact
from featuregen.overlay.identity import fact_key

def _confirm_cmd(ref, target_event_id, actor, note=None):
    return Command(action="confirm_fact", aggregate="overlay_fact", aggregate_id=None,
                   args={"ref": ref, "fact_type": "approved_join", "use_case": None,
                         "target_event_id": target_event_id, "note": note},
                   actor=actor, idempotency_key="ik-1", expected_version=None)

def test_partial_confirm_persists_note(passc_conn, _propose_join, build_join_ref,
                                       human_admin_1):
    ref, evidence = _propose_join(passc_conn)                      # proposes a dual approved_join DRAFT
    key = fact_key(ref, "approved_join", None)
    stream = load_fact(passc_conn, key)
    target = stream[-1].event_id                                   # the DRAFT head
    confirm_fact(passc_conn, _confirm_cmd(ref, target, human_admin_1, note="check the CIF namespace"))
    stream = load_fact(passc_conn, key)
    partial = [e for e in stream if e.type == "OVERLAY_FACT_PARTIALLY_CONFIRMED"][-1]
    assert partial.payload["note"] == "check the CIF namespace"
```

Adjust the `_propose_join` / target-event access to the actual conftest helper shapes (read the conftest first; it already exposes a proposed dual join + the head event id).

- [ ] **Step 2: Run it — FAIL** (`KeyError: 'note'`). `uv run pytest tests/featuregen/overlay/upload/passc/test_confirm_note.py -q`
- [ ] **Step 3: Implement.** In `join_confirmation.py`, in `_confirm_approved_join`, add `"note": cmd.args.get("note")` to **both** the `OVERLAY_FACT_PARTIALLY_CONFIRMED` payload and the `OVERLAY_FACT_CONFIRMED` payload dicts. In `confirmation_commands.py:165-170`, add `"note": args.get("note")` to the single-path `OVERLAY_FACT_CONFIRMED` payload (symmetry). Read the exact `append_overlay_event(... payload={...})` calls and add the one key; change nothing else.
- [ ] **Step 4: Run — PASS.** Add a second test: distinct admin2 confirms with a different note → the CONFIRMED payload carries admin2's note. `uv run pytest tests/featuregen/overlay/upload/passc/test_confirm_note.py -q`
- [ ] **Step 5: Regression + commit.** `uv run pytest tests/featuregen/overlay/ -q` (green). `git add -A && git commit -m "feat(overlay): thread an optional approver note into confirm events"`

---

### Task 2: `require_confirmer` RBAC dependency + `governance:confirm` permission

**Files:**
- Modify: `src/featuregen/api/deps.py` (add `require_confirmer` after `require_feature_generate` at :78).
- Modify: `src/featuregen/identity/permissions.py` (add `GOVERNANCE_CONFIRM` + map into the `platform_admin` bundle).
- Test: `tests/featuregen/api/test_require_confirmer.py`

**Interfaces:**
- Consumes: `api/deps.py:get_identity`, `IdentityEnvelope.role_claims: tuple[str,...]`, `audit_access_denied`.
- Produces: `from featuregen.api.deps import require_confirmer` — a FastAPI dependency usable as `dependencies=[Depends(require_confirmer)]` that 403s unless `"platform-admin"` is in `identity.role_claims`.

- [ ] **Step 1: Failing test** — mount a trivial route guarded by `require_confirmer`; assert 200 with the claim, 403 without.

```python
# test_require_confirmer.py
import os
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from featuregen.api.deps import require_confirmer

def _app():
    app = FastAPI()
    @app.get("/x", dependencies=[Depends(require_confirmer)])
    def _x(): return {"ok": True}
    return app

def test_confirmer_allows_platform_admin(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")
    c = TestClient(_app())
    r = c.get("/x", headers={"X-User": "priya", "X-Roles": "platform-admin"})
    assert r.status_code == 200

def test_confirmer_denies_non_admin(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")
    c = TestClient(_app())
    r = c.get("/x", headers={"X-User": "joe", "X-Roles": "catalog_viewer"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run — FAIL** (`ImportError: require_confirmer`). `uv run pytest tests/featuregen/api/test_require_confirmer.py -q`
- [ ] **Step 3: Implement.** In `deps.py`:

```python
def require_confirmer(
    request: Request,
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> IdentityEnvelope:
    """Governance confirmer gate: the caller must carry the raw `platform-admin` role CLAIM — the exact
    claim the overlay's dual-owner confirm authorizes on (join_confirmation.py:68). Deliberately NOT the
    `platform_admin` permission bundle, to avoid a route-passes-but-overlay-denies mismatch."""
    if "platform-admin" not in identity.role_claims:
        audit_access_denied(identity, f"platform-admin claim on {request.method} {request.url.path}")
        raise HTTPException(status_code=403, detail="requires the platform-admin role")
    return identity
```

In `permissions.py`, add `GOVERNANCE_CONFIRM = "governance:confirm"` and include it in the `platform_admin` bundle's permission set (find `ROLE_PERMISSIONS[... "platform_admin" ...]` and add the constant). Note in a comment that the route does NOT rely on this yet (future reconciliation).

- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/api/test_require_confirmer.py -q`
- [ ] **Step 5: Commit.** `uv run pytest tests/featuregen/ -q -k "permissions or deps or require_confirmer"` then `git add -A && git commit -m "feat(api): require_confirmer platform-admin gate + governance:confirm permission"`

---

### Task 3: `join_governance.py` read model + approval-stream reader

**Files:**
- Create: `src/featuregen/overlay/upload/join_governance.py`
- Test: `tests/featuregen/overlay/upload/test_join_governance.py`

**Interfaces:**
- Consumes: `overlay/upload/table_fact_projection.py`'s reader pattern (`:105-125`) + `_WORKLIST_READER`; `overlay/task_read.get_task_proposal`; `overlay/store.load_fact`; `overlay/identity._ref_from_payload` (`identity.py:32`), `fact_key`; `overlay/state.fold_overlay_state`; `overlay/upload/object_ref.normalize_ref`; `overlay/upload/passc/types.JoinCandidateEvidenceV1`; the Phase 3A passc conftest to seed proposals + confirm.
- Produces:
  - `list_open_approved_join_proposals(conn, source: str, *, limit: int = 100) -> list[dict]` — each dict has keys: `fact_key`, `tasks` (list of `{task_id, side, status}`), `from` `{table, column}`, `to` `{table, column}`, `cardinality`, `proposed_direction`, `status` (`"PROPOSED"|"PARTIALLY_CONFIRMED"`), `approvals` (list of `{subject, display_name, role, note, confirmed_at}`), `evidence`, `evidence_version`, `evidence_parse_status`.
  - `read_join_approvals(conn, fact_key) -> list[dict]` — the approval-stream reader (used by the list + by the routes for the confirm response). Returns `[{subject, display_name, role, note, confirmed_at}]` from the stream's PARTIALLY_CONFIRMED/CONFIRMED events.

- [ ] **Step 1: Failing tests.** Seed a proposed dual join (passc conftest), then:

```python
# test_join_governance.py
from featuregen.overlay.upload.join_governance import (
    list_open_approved_join_proposals, read_join_approvals)
from featuregen.overlay.identity import fact_key

def test_lists_a_proposed_join_with_evidence(passc_conn, _propose_join):
    ref, _ = _propose_join(passc_conn)                    # source = e.g. "src" (read conftest for the value)
    out = list_open_approved_join_proposals(passc_conn, ref.from_ref.catalog_source)
    assert len(out) == 1
    p = out[0]
    assert p["status"] == "PROPOSED"
    assert p["from"]["column"] and p["to"]["column"]
    assert p["cardinality"] in ("1:1", "1:N", "N:1")
    assert p["evidence_parse_status"] == "parsed"
    assert isinstance(p["evidence"]["score"], int)
    assert len(p["tasks"]) == 2                            # a dual join opens two side-tasks
    assert p["fact_key"] == fact_key(ref, "approved_join", None)

def test_excludes_other_sources(passc_conn, _propose_join):
    ref, _ = _propose_join(passc_conn)
    assert list_open_approved_join_proposals(passc_conn, "some-other-source") == []

def test_partial_confirm_shows_approver_and_note(passc_conn, _propose_join, human_admin_1):
    ref, _ = _propose_join(passc_conn)
    # confirm once with a note (reuse the Task-1 confirm-with-note helper or the conftest partial path)
    ...  # drive one confirm as human_admin_1 with note="looks right"
    out = list_open_approved_join_proposals(passc_conn, ref.from_ref.catalog_source)
    assert out[0]["status"] == "PARTIALLY_CONFIRMED"
    assert out[0]["approvals"][0]["subject"] == human_admin_1.subject
    assert out[0]["approvals"][0]["note"] == "looks right"

def test_evidence_missing_does_not_crash(passc_conn):
    # a proposal whose evidence metric_values are absent -> parse_status != "parsed", still listed
    ...  # seed a bare approved_join proposal without join evidence
    out = list_open_approved_join_proposals(passc_conn, "src")
    assert out and out[0]["evidence_parse_status"] in ("missing", "invalid", "partial")
```

Read the passc conftest first to fill the `_propose_join` return + the confirm-once path exactly.

- [ ] **Step 2: Run — FAIL** (module missing). `uv run pytest tests/featuregen/overlay/upload/test_join_governance.py -q`
- [ ] **Step 3: Implement `join_governance.py`.** Key logic:
  - `_READER = IdentityEnvelope(subject="system:join-governance", actor_kind="service", authenticated=True, auth_method="internal", role_claims=("platform-admin",))` (mirror `_WORKLIST_READER`).
  - `list_open_approved_join_proposals`: `SELECT task_id FROM human_tasks WHERE status='open' ORDER BY created_at DESC`; for each, `get_task_proposal(conn, task_id, _READER)` in a `try/except (skip on error)`; keep `p["fact_type"] == "approved_join"`. For each kept task, `load_fact(conn, p['fact_key'])`, decode the ref via `_ref_from_payload(stream[0].payload["catalog_object_ref"])`; skip (log) if decode fails or it isn't an `ApprovedJoinRef`; recover `catalog_source = ref.from_ref.catalog_source`; `if normalize_source(catalog_source) != normalize_source(source): continue`. **Group by `fact_key`**: accumulate the per-side task rows (`{task_id, side, status}` — `side` from the task's `eligible_assignees` entry if present, else `None`), and build ONE view per fact_key. `status` + `approvals` from `read_join_approvals`. Shape the evidence tolerantly (below). Enforce `limit` (`max(1, min(limit, 500))`; slice the grouped result). **Bad data on one task never aborts the loop.**
  - `read_join_approvals(conn, fact_key)`: `load_fact`; for each event of type `OVERLAY_FACT_PARTIALLY_CONFIRMED` or `OVERLAY_FACT_CONFIRMED`, emit `{subject: <event.actor.subject or payload confirmer>, display_name: None, role: <from payload confirmers/side>, note: event.payload.get("note"), confirmed_at: event.created_at.isoformat()}`. (Read the event/actor shape to get subject + created_at exactly.)
  - Evidence shaping helper `_shape_evidence(proposal) -> (evidence_dict, version, parse_status)`: if `proposal["evidence"] is None` or has no `metric_values` → `({}, None, "missing")`. Else read `metric_values` (a dict = `asdict(JoinCandidateEvidenceV1)`): pull `score`, `positive_signals`, `negative_signals`, `namespace_compatibility`, `namespace_reason_codes`, `grain_status` (from `cardinality_status`), `grain_evidence`, `explanation`, defaulting missing keys to `[]`/`None`/`""` and appending a `warnings` note for each defaulted required field; `parse_status = "parsed"` when all present, `"partial"` when some defaulted, `"invalid"` on a type error. Never raise.
  - `from`/`to`/`cardinality`/`proposed_direction`: from the decoded `ApprovedJoinRef` (`ref.from_ref.table/column`, `ref.to_ref...`, `ref.cardinality`) — the authoritative structural source, not the display string.
  - `normalize_source(s)`: `s.strip().lower()` (matches `normalize_ref` lowercasing).
- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/overlay/upload/test_join_governance.py -q`
- [ ] **Step 5: Regression + commit.** `uv run pytest tests/featuregen/overlay/ -q`; `git add -A && git commit -m "feat(overlay): join-governance read model + approval-stream reader"`

---

### Task 4: Confirm/reject context bridge + synchronous projection helper

**Files:**
- Modify: `src/featuregen/overlay/upload/join_governance.py`
- Test: `tests/featuregen/overlay/upload/test_join_governance.py` (extend)

**Interfaces:**
- Consumes: `overlay/store.load_fact`; `overlay/identity._ref_from_payload`, `fact_key`; `overlay/state.fold_overlay_state`; `overlay/upload/passc/projection.project_confirmed_joins`; `overlay/projections/runner.projection_lag`.
- Produces:
  - `load_join_confirmation_context(conn, fact_key: str) -> dict` with keys `{ref, fact_type, use_case, target_event_id}`. **Raises `JoinGovernanceNotFound` (new)** if the fact stream is empty, the ref won't decode, or `fact_type != "approved_join"`.
  - `project_verified_join(conn, source: str, ref, *, now) -> str` returning `"projected"` (ran) or `"pending"` (projection lag > 0). Fail-soft: any projection exception → returns `"pending"` + logs (the fact stays VERIFIED).

- [ ] **Step 1: Failing tests.**

```python
import pytest
from featuregen.overlay.upload.join_governance import (
    load_join_confirmation_context, project_verified_join, JoinGovernanceNotFound)
from featuregen.overlay.identity import fact_key

def test_context_returns_typed_ref_for_a_join(passc_conn, _propose_join):
    ref, _ = _propose_join(passc_conn)
    ctx = load_join_confirmation_context(passc_conn, fact_key(ref, "approved_join", None))
    assert ctx["fact_type"] == "approved_join"
    assert ctx["ref"].from_ref.column == ref.from_ref.column
    assert ctx["target_event_id"]                       # the current head

def test_context_rejects_non_join_fact(passc_conn, seed_grain_fact):
    # a grain fact_key -> not approved_join -> raises
    with pytest.raises(JoinGovernanceNotFound):
        load_join_confirmation_context(passc_conn, seed_grain_fact)   # seed via table-fact propose

def test_project_verified_join_creates_operational_edge(passc_conn, _propose_join, _confirm_join, human_admin_1, human_admin_2):
    ref, _ = _propose_join(passc_conn)
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)   # -> VERIFIED (no projection)
    status = project_verified_join(passc_conn, ref.from_ref.catalog_source, ref, now=None)
    assert status == "projected"
    # find_join_path now traverses (reuse the Phase 3A traversal assertion)
```

Note: `_confirm_join` in the passc conftest already runs a projection at the end; for this test, either use a variant that stops at VERIFIED without projecting, or assert `project_verified_join` is idempotent (returns `projected`, edge present). Read the conftest and pick; if `_confirm_join` always projects, assert idempotency instead.

- [ ] **Step 2: Run — FAIL.** `uv run pytest tests/featuregen/overlay/upload/test_join_governance.py -q -k "context or project_verified"`
- [ ] **Step 3: Implement.**
  - `class JoinGovernanceNotFound(Exception): ...`
  - `load_join_confirmation_context`: `stream = load_fact(conn, fact_key)`; if not stream → raise. Decode `ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])` (catch → raise `JoinGovernanceNotFound`). Read `fact_type` from `stream[0].payload` (the DRAFT records it — confirm the key name by reading a proposed stream; likely `stream[0].payload["fact_type"]` or derivable). If `fact_type != "approved_join"` → raise. `state = fold_overlay_state(stream)`; `target_event_id = _cas_target(state)` — reuse the overlay's CAS-target helper (import from `confirmation_commands` or `state`; read how `confirm_fact` computes `_cas_target(state)` at `confirmation_commands.py:77`) so the route passes the exact `target_event_id` the handler expects. Return `{ref, fact_type, use_case: None, target_event_id}`.
  - `project_verified_join`: `if projection_lag(conn, "overlay") != 0: return "pending"`. Else `with conn.transaction():` call `project_confirmed_joins(conn, source=source, pairs=[ref], now=now)`; return `"projected"`. Wrap in `try/except Exception: log; return "pending"`.
- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/overlay/upload/test_join_governance.py -q`
- [ ] **Step 5: Commit.** `uv run pytest tests/featuregen/overlay/ -q`; `git add -A && git commit -m "feat(overlay): join confirm/reject context bridge + synchronous verified-join projection"`

---

### Task 5: `governance.py` API router (list + confirm + reject) + registration

**Files:**
- Create: `src/featuregen/api/routes/governance.py`
- Modify: `src/featuregen/api/app.py` (register the router next to the others at :88-99)
- Test: `tests/featuregen/api/test_governance_routes.py`

**Interfaces:**
- Consumes: `api/deps.py` (`get_conn`, `get_identity`, `require_confirmer`); `join_governance` (`list_open_approved_join_proposals`, `load_join_confirmation_context`, `project_verified_join`, `read_join_approvals`, `JoinGovernanceNotFound`); `overlay/confirmation_commands.confirm_fact`, `overlay/confirmation_commands.reject_fact`; `contracts/envelopes.Command`; `overlay/state.fold_overlay_state` + `overlay/store.load_fact` (to read post-confirm status).
- Produces: three routes (see Global Constraints). Registered in `app.py`.

- [ ] **Step 1: Failing test — the dual-admin happy path through HTTP.**

```python
# test_governance_routes.py — uses the app TestClient + ephemeral PG + auth stub (mirror an existing
# api route test's fixtures, e.g. tests/featuregen/api/test_quarantine_routes.py or conftest).
def _h(user): return {"X-User": user, "X-Roles": "platform-admin"}

def test_list_confirm_dual_verifies_and_projects(client, seed_pass_c_join):
    source, fact_key = seed_pass_c_join            # seeds an OVERLAY_PASS_C proposal + its human_tasks
    # GET lists it
    r = client.get(f"/sources/{source}/governance/joins", headers=_h("priya"))
    assert r.status_code == 200
    props = r.json()["proposals"]
    assert len(props) == 1 and props[0]["fact_key"] == fact_key
    assert props[0]["status"] == "PROPOSED"
    # admin1 confirm
    r = client.post(f"/governance/joins/{fact_key}/confirm", json={"note": "cif ok"}, headers=_h("priya"))
    assert r.json()["governance_status"] == "PARTIALLY_CONFIRMED"
    # admin1 repeat -> 409
    assert client.post(f"/governance/joins/{fact_key}/confirm", json={}, headers=_h("priya")).status_code == 409
    # note visible to the second approver
    r = client.get(f"/sources/{source}/governance/joins", headers=_h("rahman"))
    assert r.json()["proposals"][0]["approvals"][0]["note"] == "cif ok"
    # distinct admin2 confirm -> VERIFIED + projected
    r = client.post(f"/governance/joins/{fact_key}/confirm", json={}, headers=_h("rahman"))
    body = r.json()
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] in ("projected", "pending")
```

Plus tests: reject with `{category, note}` → REJECTED (and category on the payload); `fact_type != approved_join` → confirm/reject 404; non-admin (`X-Roles: catalog_viewer`) → 403; bad category enum → 422; over-length note → 422; unknown fact_key → 404. Reuse the Phase 3A passc helpers to seed proposals into the test DB the TestClient reads (set `FEATUREGEN_DSN` to the ephemeral PG; seed on a direct conn; the app's `get_conn` reads the same DSN).

- [ ] **Step 2: Run — FAIL** (routes missing). `uv run pytest tests/featuregen/api/test_governance_routes.py -q`
- [ ] **Step 3: Implement `governance.py`.**

```python
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
# ... imports: get_conn, get_identity, require_confirmer, join_governance fns, Command,
#     confirm_fact, reject_fact, load_fact, fold_overlay_state

router = APIRouter()

class ConfirmJoinRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)

class RejectJoinRequest(BaseModel):
    category: Literal["wrong_direction", "wrong_cardinality", "different_entity",
                      "not_a_real_key", "needs_data_check"]
    note: str | None = Field(default=None, max_length=1000)

def _clean(s): 
    s = (s or "").strip()
    return s or None

@router.get("/sources/{source}/governance/joins", dependencies=[Depends(require_confirmer)])
def list_joins(source: str,
               conn=Depends(get_conn),
               limit: int = Query(default=100, ge=1, le=500)):
    proposals = list_open_approved_join_proposals(conn, source, limit=limit)
    return {"source": source.strip().lower(), "proposals": proposals, "next_cursor": None}

@router.post("/governance/joins/{fact_key}/confirm", dependencies=[Depends(require_confirmer)])
def confirm_join(fact_key: str, body: ConfirmJoinRequest,
                 conn=Depends(get_conn),
                 identity: Annotated[..., Depends(get_identity)] = ...):
    try:
        ctx = load_join_confirmation_context(conn, fact_key)
    except JoinGovernanceNotFound:
        raise HTTPException(status_code=404, detail="No such join proposal.")
    cmd = Command(action="confirm_fact", aggregate="overlay_fact", aggregate_id=fact_key,
                  args={"ref": ctx["ref"], "fact_type": "approved_join", "use_case": ctx["use_case"],
                        "target_event_id": ctx["target_event_id"], "note": _clean(body.note)},
                  actor=identity, idempotency_key=f"confirm:{fact_key}:{identity.subject}",
                  expected_version=None)
    result = confirm_fact(conn, cmd)
    if not result.accepted:
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
    status = fold_overlay_state(load_fact(conn, fact_key)).status
    projection = "not_applicable"
    if status == "VERIFIED":
        projection = project_verified_join(conn, ctx["ref"].from_ref.catalog_source, ctx["ref"], now=None)
    return {"governance_status": status, "operational_projection": projection,
            "approvals": read_join_approvals(conn, fact_key)}

@router.post("/governance/joins/{fact_key}/reject", dependencies=[Depends(require_confirmer)])
def reject_join(fact_key: str, body: RejectJoinRequest,
                conn=Depends(get_conn),
                identity: Annotated[..., Depends(get_identity)] = ...):
    try:
        ctx = load_join_confirmation_context(conn, fact_key)
    except JoinGovernanceNotFound:
        raise HTTPException(status_code=404, detail="No such join proposal.")
    cmd = Command(action="reject_fact", aggregate="overlay_fact", aggregate_id=fact_key,
                  args={"ref": ctx["ref"], "fact_type": "approved_join", "use_case": ctx["use_case"],
                        "target_event_id": ctx["target_event_id"],
                        "reason": {"category": body.category, "note": _clean(body.note)}},
                  actor=identity, idempotency_key=f"reject:{fact_key}:{identity.subject}",
                  expected_version=None)
    result = reject_fact(conn, cmd)
    if not result.accepted:
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
    return {"governance_status": "REJECTED", "category": body.category}
```

`_deny_to_detail(reason)`: map the overlay deny strings to friendly text — "already confirmed" → "You already approved this — a different admin must confirm."; "stale"/"superseded" → "Changed since you loaded it — refresh."; else the reason. (Read the exact deny strings in `join_confirmation.py:118` + `confirmation_commands.py:81` and match on a stable substring.) Register in `app.py`: `from featuregen.api.routes import governance` and `app.include_router(governance.router)`.

- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/api/test_governance_routes.py -q`
- [ ] **Step 5: Regression + commit.** `uv run pytest tests/featuregen/api/ -q`; `git add -A && git commit -m "feat(api): governance router — list/confirm/reject discovered joins"`

---

### Task 6: React `GovernanceReviewScreen` + api.ts + route registration

**Files:**
- Create: `frontend/src/screens/GovernanceReviewScreen.tsx`
- Modify: `frontend/src/api.ts` (client fns), `frontend/src/nav.ts` (route literal), `frontend/src/App.tsx` (import + PAGES entry + render branch).
- Test: `frontend/src/screens/GovernanceReviewScreen.test.tsx` (or the project's frontend test convention — check `frontend/` for the test runner).

**Interfaces:**
- Consumes: `api.ts`'s `request`/`post` (`:17-67`); the endpoint shapes from Task 5. The **visual + interaction design is the approved v2 mockup** (artifact `e1f2c6df-038f-40ac-88ac-09465f7c6a2b`): evidence-forward cards, the baseline+derived checklist gating Approve, structured reject chips, the first-approver note, the consequence + "not value-verified" lines. `ReviewQueueScreen.tsx` is the structural React template (source input → load → per-row action handlers → session-local resolved map).
- Produces: a `governance` route rendering the screen.

- [ ] **Step 1: api.ts client functions.** Add, mirroring `listQuarantine`/`resolveQuarantineRow`:

```ts
export interface JoinApproval { subject: string; display_name: string | null; role: string | null; note: string | null; confirmed_at: string | null }
export interface JoinProposal {
  fact_key: string; from: {table:string;column:string}; to:{table:string;column:string};
  cardinality: string; proposed_direction: string; status: 'PROPOSED'|'PARTIALLY_CONFIRMED';
  approvals: JoinApproval[]; evidence: any; evidence_parse_status: string;
}
export const listJoinProposals = (source: string) =>
  request<{source:string; proposals: JoinProposal[]; next_cursor: string|null}>(`/sources/${encodeURIComponent(source)}/governance/joins`);
export const confirmJoin = (factKey: string, body: {note?: string}) =>
  post(`/governance/joins/${encodeURIComponent(factKey)}/confirm`, body);
export const rejectJoin = (factKey: string, body: {category: string; note?: string}) =>
  post(`/governance/joins/${encodeURIComponent(factKey)}/reject`, body);
```

- [ ] **Step 2: The screen.** Build `GovernanceReviewScreen.tsx` from the v2 mockup markup translated to React state: `source` input → `listJoinProposals` → cards. Per card: render the from→to join, the evidence (score demoted, signals), the **consequence line** and **"matched on metadata, not value-verified"** caution (templated from the fields), the **checklist** = the 4 baseline items + one per evidence signal, **Approve disabled until every checklist box is ticked**, a structured **Reject** with the 5 category chips + optional note, and the **first approver's note** when `status === 'PARTIALLY_CONFIRMED'`. On Approve → `confirmJoin(fact_key, {note})`; on Reject → `rejectJoin(fact_key, {category, note})`; keep a session-local resolved map like `ReviewQueueScreen`. Concurrency UX: disable the button on submit; on `ApiError` 409 show the detail and reload the list; never blind-retry.
- [ ] **Step 3: Register the route.** In `nav.ts` add `'governance'` to `Route`/`ROUTES`; in `App.tsx` import `GovernanceReviewScreen`, add a `PAGES` entry (`{route:'governance', label:'Governance', eyebrow:'Review', title:'Discovered joins', description:'Approve or reject joins Pass C found.'}`) and a `{route === 'governance' && <GovernanceReviewScreen/>}` branch.
- [ ] **Step 4: Test.** One render+interaction test: mock `listJoinProposals` to return one PROPOSED proposal → the card renders with its evidence; **Approve is disabled until all checklist boxes are checked**; checking them enables it; clicking Approve calls `confirmJoin` with the fact_key. Run with the frontend test command (check `frontend/package.json` scripts — e.g. `npm test` / `vitest`).
- [ ] **Step 5: Commit.** Run the frontend test + `uv run pytest tests/featuregen/api/test_governance_routes.py -q` (unaffected). `git add -A && git commit -m "feat(frontend): GovernanceReviewScreen — approve/reject discovered joins"`

---

## Self-Review

**Spec coverage:** §1 endpoints → Task 5; §2 read model/bridge/projection/confirm-note → Tasks 1,3,4; §3 checklist/consequence (frontend, deterministic) → Task 6; §4 frontend + concurrency UX → Task 6; §5 RBAC → Task 2; §6 error mapping → Task 5 (`_deny_to_detail` + 404/403/409/400/422); §7 tests → each task's tests + Task 5's suite; §8 security → Tasks 2,5 (claim gate; metadata-only). Acceptance criteria 1–11 all map to a task's test. No gap.

**Placeholder scan:** the two `...` in Task 3/4/5 tests mark "read the conftest and fill the exact seed/confirm-once helper" — the surrounding assertions are concrete; the implementer completes the seed line from the named conftest. The frontend markup references the approved v2 mockup artifact as its concrete source (not a vague "build a screen"). No logic is hand-waved.

**Type consistency:** `fact_key` (str) keys confirm/reject throughout; `list_open_approved_join_proposals(conn, source, *, limit)`, `read_join_approvals(conn, fact_key)`, `load_join_confirmation_context(conn, fact_key) -> {ref, fact_type, use_case, target_event_id}`, `project_verified_join(conn, source, ref, *, now) -> "projected"|"pending"`, `JoinGovernanceNotFound` — used identically in Tasks 3/4/5. Request models `ConfirmJoinRequest`/`RejectJoinRequest` and the `{governance_status, operational_projection, approvals}` / `{governance_status, category}` responses match §1.

## Execution Handoff

Build **subagent-driven** (fresh implementer per task, two-stage review, whole-branch review before merge) on branch `confirmation-surface`, model split per project convention (Fable implementers, Opus reviewers).
