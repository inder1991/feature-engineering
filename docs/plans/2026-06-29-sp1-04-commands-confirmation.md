# SP-1 — Phase 4 — Commands & confirmation (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Global Constraints + Shared Contract:** see [sp1-00-overview.md](2026-06-29-sp1-00-overview.md) (authoritative).

---

This phase builds the write side of the overlay: per-fact **authority resolution** (`authority.py`), the
five-handler command catalog (`commands.py`: `propose_fact`, `confirm_fact`, `reject_fact`, `enter_fact`,
plus the Phase-6 `run_profiler` slot), the task-scoped proposal read (`get_task_proposal`), and the
production wiring (`bootstrap.py`: `register_overlay` + `seed_overlay_authz`). Implements design §3.4, §6,
§6.3, §6.4, §6.5, §7.2.

**Cross-phase Consumes (built in earlier phases; used verbatim here):**
- Phase 1: `events`/`human_tasks` overlay columns; `GateTaskSpec.fact_key`/`.draft_event_id`/`.target_event_id`/`.evidence_ref`; `_task_aggregate` overlay arm; `open_task` threading those columns; `0083` `projection_checkpoints('overlay')` seed.
- Phase 2: `overlay/identity.py` (`CatalogObjectRef`, `ApprovedJoinRef`, `ColumnPair`, `fact_key`, `display_object_ref`, `proposal_fingerprint`); `overlay/facts.py` (`validate_fact_value`, `FactValidationError`, `register_overlay_event_types`, the `OVERLAY_FACT_*` event schemas); `overlay/state.py` (`fold_overlay_state(stream) -> OverlayState` with `.status`, `.draft_event_id`, `.confirmed_event_id`); `overlay/store.py` (`append_overlay_event(...)`, `load_fact(conn, fact_key)`); `overlay/evidence.py` (`read_evidence`).
- Phase 3: `overlay/catalog.py` (`CatalogAdapter` Protocol, `.owner_of`).
- SP-0: `Command`/`CommandResult`/`DbConn`/`IdentityEnvelope`; `commands/api.py::execute_command`; `commands/registry.py::register_command`; `gates/tasks.py::open_task`/`cancel_task`; `authz/policy.py::seed_authz_policy`; `authz/authorizer.py::PolicyAuthorizer`; `commands/authz_seam.py::register_command_authorizer`; `identity/build.py::build_human_identity`/`build_service_identity`; `events/registry.py::event_registry`.

---

### Task 4.1: `authority.py` — per-fact authority resolver + SoD helper

**Files:**
- Create: `src/featuregen/overlay/authority.py`
- Test: `tests/featuregen/overlay/test_authority.py`

**Interfaces:**
- Consumes: `featuregen.overlay.catalog.CatalogAdapter` (`.owner_of(ref) -> str | None`); `featuregen.overlay.identity.{CatalogObjectRef, ApprovedJoinRef, display_object_ref}`; `featuregen.contracts.{DbConn, IdentityEnvelope}`.
- Produces:
  - `Authority` (frozen/slots dataclass): `role: str`, `gate: str`, `subjects: tuple[str | None, ...]`, `governance_queue: bool`, `dual: bool = False`; property `eligible_assignees -> dict[str, str]`.
  - `resolve_authority(conn, adapter, ref, fact_type) -> Authority` — data facts → data owner via `adapter.owner_of`; `policy_tag` → Compliance; unknown owner → governance-queue marker (`platform-admin`, never the submitter); `approved_join` → both owners.
  - `proposer_ne_confirmer(stream, actor) -> bool` — four-eyes SoD predicate (True ⇒ confirmer differs from the recorded proposer).

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/overlay/test_authority.py
from dataclasses import dataclass
from typing import Any

from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.overlay.authority import (
    Authority,
    proposer_ne_confirmer,
    resolve_authority,
)
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    display_object_ref,
)


class _Cat:
    """Minimal CatalogAdapter test double keyed on the display object_ref string."""

    def __init__(self, owners: dict[str, str] | None = None) -> None:
        self._owners = owners or {}

    def owner_of(self, ref: CatalogObjectRef) -> str | None:
        return self._owners.get(display_object_ref(ref))

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def list_objects(self):
        return []

    def fingerprint(self):
        return {}


@dataclass(frozen=True)
class _Evt:
    type: str
    payload: dict[str, Any]


def _orders() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def test_data_fact_resolves_to_data_owner(db):
    cat = _Cat({display_object_ref(_orders()): "user:alice"})
    auth = resolve_authority(db, cat, _orders(), "grain")
    assert auth.role == "data_owner"
    assert auth.gate == "OVERLAY_DATA_OWNER"
    assert auth.subjects == ("user:alice",)
    assert auth.governance_queue is False
    assert auth.eligible_assignees == {"role": "data_owner", "subject": "user:alice"}


def test_policy_tag_resolves_to_compliance(db):
    cat = _Cat({display_object_ref(_orders()): "user:alice"})
    auth = resolve_authority(db, cat, _orders(), "policy_tag")
    assert auth.role == "compliance"
    assert auth.gate == "OVERLAY_COMPLIANCE"
    assert auth.subjects == ()
    assert auth.eligible_assignees == {"role": "compliance"}


def test_unknown_owner_routes_to_governance_not_submitter(db):
    cat = _Cat({})  # ownership not recorded
    auth = resolve_authority(db, cat, _orders(), "availability_time")
    assert auth.governance_queue is True
    assert auth.role == "platform-admin"
    assert auth.eligible_assignees == {"role": "platform-admin"}
    assert "user:" not in str(auth.subjects)  # never the request submitter


def test_approved_join_two_distinct_owners_is_dual(db):
    a = _orders()
    b = CatalogObjectRef("pg:core", "table", "sales", "customers")
    cat = _Cat({display_object_ref(a): "user:alice", display_object_ref(b): "user:bob"})
    ref = ApprovedJoinRef(a, b, (ColumnPair("customer_id", "id"),), "N:1")
    auth = resolve_authority(db, cat, ref, "approved_join")
    assert auth.dual is True
    assert auth.subjects == ("user:alice", "user:bob")


def test_approved_join_same_owner_both_sides_is_not_dual(db):
    a = _orders()
    b = CatalogObjectRef("pg:core", "table", "sales", "customers")
    cat = _Cat({display_object_ref(a): "user:alice", display_object_ref(b): "user:alice"})
    ref = ApprovedJoinRef(a, b, (ColumnPair("customer_id", "id"),), "N:1")
    auth = resolve_authority(db, cat, ref, "approved_join")
    assert auth.dual is False
    assert auth.subjects == ("user:alice", "user:alice")


def test_proposer_ne_confirmer(db):
    alice = build_human_identity(subject="user:alice", role_claims=("data_owner",))
    bob = build_human_identity(subject="user:bob", role_claims=("data_owner",))
    svc = build_service_identity(
        subject="service:profiler", role_claims=("overlay",), attestation="sig"
    )
    human_proposed = [_Evt("OVERLAY_FACT_PROPOSED", {"proposed_by": {"subject": "user:alice"}})]
    svc_proposed = [_Evt("OVERLAY_FACT_PROPOSED", {"proposed_by": {"subject": "service:profiler"}})]
    assert proposer_ne_confirmer(human_proposed, alice) is False  # self-confirm blocked
    assert proposer_ne_confirmer(human_proposed, bob) is True
    assert proposer_ne_confirmer(svc_proposed, alice) is True  # service proposal, human confirm
    assert isinstance(Authority(role="x", gate="g", subjects=(), governance_queue=False), Authority)
    _ = svc  # service identity used only to anchor the four-eyes scenario above
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/overlay/test_authority.py -v`
  - Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.authority'`.

- [ ] **Step 3 — minimal implementation**

```python
# src/featuregen/overlay/authority.py
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef


@dataclass(frozen=True, slots=True)
class Authority:
    """Resolved authority for a fact. `subjects` holds the resolved owner subject(s)
    (two ordered entries for an `approved_join`); `governance_queue` is True when a required
    owner is unknown — the task then routes to the platform-admin/data-governance queue, NEVER
    to whoever submitted the request (§6 step 1). `dual` is True only for an `approved_join`
    with two DISTINCT known owners (two-task confirmation, §6.4)."""

    role: str
    gate: str
    subjects: tuple[str | None, ...]
    governance_queue: bool
    dual: bool = False

    @property
    def eligible_assignees(self) -> dict[str, str]:
        a: dict[str, str] = {"role": self.role}
        known = [s for s in self.subjects if s]
        if known:
            a["subject"] = known[0]
        return a


def resolve_authority(
    conn: DbConn,
    adapter: CatalogAdapter,
    ref: CatalogObjectRef | ApprovedJoinRef,
    fact_type: str,
) -> Authority:
    # conn is part of the stable contract (owner overrides / governance config may be stored
    # in future); the reference resolver derives authority purely from the catalog adapter.
    del conn
    if fact_type == "policy_tag":
        return Authority(
            role="compliance", gate="OVERLAY_COMPLIANCE", subjects=(), governance_queue=False
        )
    if fact_type == "approved_join":
        assert isinstance(ref, ApprovedJoinRef)
        from_owner = adapter.owner_of(ref.from_ref)
        to_owner = adapter.owner_of(ref.to_ref)
        unknown = from_owner is None or to_owner is None
        dual = (not unknown) and (from_owner != to_owner)
        return Authority(
            role=("platform-admin" if unknown else "data_owner"),
            gate="OVERLAY_DATA_OWNER",
            subjects=(from_owner, to_owner),
            governance_queue=unknown,
            dual=dual,
        )
    assert isinstance(ref, CatalogObjectRef)
    owner = adapter.owner_of(ref)
    if owner is None:
        return Authority(
            role="platform-admin", gate="OVERLAY_DATA_OWNER", subjects=(), governance_queue=True
        )
    return Authority(
        role="data_owner", gate="OVERLAY_DATA_OWNER", subjects=(owner,), governance_queue=False
    )


def proposer_ne_confirmer(stream: Sequence, actor: IdentityEnvelope) -> bool:
    """Four-eyes SoD predicate (§6.5): True when the confirmer differs from the recorded
    proposer. A service/profiler proposal is trivially distinct from a human confirmer."""
    for e in reversed(list(stream)):
        if e.type == "OVERLAY_FACT_PROPOSED":
            proposed_by = e.payload.get("proposed_by") or {}
            return proposed_by.get("subject") != actor.subject
    return True
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/overlay/test_authority.py -v`
  - Expected: PASS (6 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/overlay/authority.py tests/featuregen/overlay/test_authority.py && git commit -m "feat(overlay): per-fact authority resolver + four-eyes SoD helper"`

---

### Task 4.2: `commands.py` — `propose_fact` (+ human-gate task)

**Files:**
- Create: `src/featuregen/overlay/commands.py`
- Create: `tests/featuregen/overlay/conftest.py`
- Test: `tests/featuregen/overlay/test_propose_fact.py`

**Interfaces:**
- Consumes: `append_overlay_event`/`load_fact` (store.py); `fold_overlay_state` (state.py); `fact_key`/`display_object_ref`/`proposal_fingerprint` (identity.py); `validate_fact_value`/`FactValidationError` (facts.py); `resolve_authority` (authority.py); `GateTaskSpec` + `open_task` (SP-0 gates); `register_command` (SP-0).
- Produces: `propose_fact(conn, cmd) -> CommandResult`; the catalog-adapter seam `register_overlay_catalog_adapter(adapter)` / `current_catalog_adapter()`; `OverlayCommandError`; module helpers `_latest_proposed`, `_cas_target`, `_actor_is_authority`, `_close_fact_tasks`. Appends `OVERLAY_FACT_PROPOSED` (`expected_version=0` for a new fact) and opens an `OVERLAY_DATA_OWNER`/`OVERLAY_COMPLIANCE` task per resolved owner.

- [ ] **Step 1 — create the shared overlay test conftest**

```python
# tests/featuregen/overlay/conftest.py
import pytest

from featuregen.events.registry import event_registry
from featuregen.overlay.commands import (
    register_overlay_catalog_adapter,
    register_overlay_commands,
)
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.identity import display_object_ref


class StubCatalog:
    """In-memory CatalogAdapter test double (stands in for Phase 3's FixtureCatalog so Phase 4
    is independent of its constructor). Owners are keyed on the display object_ref string."""

    def __init__(self) -> None:
        self.owners: dict[str, str] = {}

    def set_owner(self, ref, subject: str) -> None:
        self.owners[display_object_ref(ref)] = subject

    def owner_of(self, ref):
        return self.owners.get(display_object_ref(ref))

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def list_objects(self):
        return []

    def fingerprint(self):
        return {}


@pytest.fixture(autouse=True)
def _register_overlay_runtime():
    # Runs AFTER the autouse registry reset in tests/conftest.py, so overlay event schemas and
    # the overlay command catalog are present for every overlay test.
    register_overlay_event_types(event_registry())
    register_overlay_commands()


@pytest.fixture
def catalog():
    cat = StubCatalog()
    register_overlay_catalog_adapter(cat)
    return cat
```

- [ ] **Step 2 — write the failing test**

```python
# tests/featuregen/overlay/test_propose_fact.py
from psycopg.rows import dict_row

from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.identity import CatalogObjectRef, fact_key


def _orders() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _propose_cmd(*, ref, fact_type, value, use_case=None, actor=None, key="k1"):
    actor = actor or build_human_identity(subject="user:alice", role_claims=("data_owner",))
    args = {"ref": ref, "fact_type": fact_type, "proposed_value": value}
    if use_case is not None:
        args["use_case"] = use_case
    return Command(
        action="propose_fact",
        aggregate="overlay_fact",
        aggregate_id=None,
        args=args,
        actor=actor,
        idempotency_key=key,
    )


def test_propose_creates_draft_and_data_owner_task(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    cmd = _propose_cmd(ref=_orders(), fact_type="grain", value={"columns": ["order_id"], "is_unique": True})
    res = propose_fact(db, cmd)
    assert res.accepted is True
    key = fact_key(_orders(), "grain")
    assert res.aggregate_id == key
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT type FROM events WHERE overlay_fact_id=%s ORDER BY stream_version", (key,)
        )
        assert [r["type"] for r in cur.fetchall()] == ["OVERLAY_FACT_PROPOSED"]
        cur.execute(
            "SELECT gate, eligible_assignees FROM human_tasks WHERE fact_key=%s AND status='open'",
            (key,),
        )
        row = cur.fetchone()
        assert row["gate"] == "OVERLAY_DATA_OWNER"
        assert row["eligible_assignees"]["subject"] == "user:alice"


def test_duplicate_fingerprint_is_denied(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    value = {"columns": ["order_id"], "is_unique": True}
    assert propose_fact(db, _propose_cmd(ref=_orders(), fact_type="grain", value=value, key="k1")).accepted
    dup = propose_fact(db, _propose_cmd(ref=_orders(), fact_type="grain", value=value, key="k2"))
    assert dup.accepted is False
    assert "duplicate" in dup.denied_reason


def test_policy_tag_opens_compliance_task(db, catalog):
    cmd = _propose_cmd(
        ref=_orders(),
        fact_type="policy_tag",
        value={"decision": "restricted", "basis": "PII review 2026-06"},
        use_case="marketing",
    )
    res = propose_fact(db, cmd)
    assert res.accepted is True
    key = fact_key(_orders(), "policy_tag", "marketing")
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT gate, eligible_assignees FROM human_tasks WHERE fact_key=%s", (key,)
        )
        row = cur.fetchone()
        assert row["gate"] == "OVERLAY_COMPLIANCE"
        assert row["eligible_assignees"] == {"role": "compliance"}
```

- [ ] **Step 3 — run it (fails)**
  - `uv run pytest tests/featuregen/overlay/test_propose_fact.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'propose_fact' from 'featuregen.overlay.commands'`.

- [ ] **Step 4 — minimal implementation**

```python
# src/featuregen/overlay/commands.py
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from featuregen.commands.registry import register_command
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import cancel_task, open_task
from featuregen.overlay.authority import Authority, proposer_ne_confirmer, resolve_authority
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import (
    display_object_ref,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact

# Default re-verification horizon stamped on FACT_CONFIRMED; the SP-0 timer that fires at
# `expires_at` is scheduled in Phase 7 (`freshness.schedule_expiry`) — Phase 4 only records it.
_DEFAULT_TTL = timedelta(days=180)


class OverlayCommandError(Exception):
    """Raised on overlay command misconfiguration (e.g. no catalog adapter registered)."""


_CATALOG_ADAPTER: CatalogAdapter | None = None


def register_overlay_catalog_adapter(adapter: CatalogAdapter) -> None:
    """Inject the process-wide catalog adapter the handlers use to resolve authority (mirrors the
    `commands.authz_seam` injection pattern)."""
    global _CATALOG_ADAPTER
    _CATALOG_ADAPTER = adapter


def current_catalog_adapter() -> CatalogAdapter:
    if _CATALOG_ADAPTER is None:
        raise OverlayCommandError("no overlay catalog adapter registered")
    return _CATALOG_ADAPTER


def _latest_proposed(stream):
    for e in reversed(stream):
        if e.type == "OVERLAY_FACT_PROPOSED":
            return e
    return None


def _cas_target(state) -> str | None:
    """The event id a confirm/reject must target (SP-0 OCC + §6.3 CAS): the DRAFT id while the
    fact is awaiting first/second confirmation, else the prior confirmed_event_id (re-verify)."""
    if state.status in ("DRAFT", "PARTIALLY_CONFIRMED"):
        return state.draft_event_id
    return state.confirmed_event_id


def _actor_is_authority(authority: Authority, actor) -> bool:
    if authority.role == "compliance":
        return "compliance" in actor.role_claims
    return actor.subject in {s for s in authority.subjects if s}


def _close_fact_tasks(conn: DbConn, key: str, *, subject: str | None = None, reason: str) -> None:
    rows = conn.execute(
        "SELECT task_id, eligible_assignees FROM human_tasks WHERE fact_key=%s AND status='open'",
        (key,),
    ).fetchall()
    for task_id, eligible in rows:
        if subject is not None and (eligible or {}).get("subject") != subject:
            continue
        cancel_task(conn, task_id, reason=reason)


def propose_fact(conn: DbConn, cmd: Command) -> CommandResult:
    adapter = current_catalog_adapter()
    args = cmd.args
    ref = args["ref"]
    fact_type = args["fact_type"]
    use_case = args.get("use_case")
    proposed_value = args["proposed_value"]
    evidence_ref = args.get("evidence_ref")
    try:
        validate_fact_value(fact_type, proposed_value, use_case=use_case)
    except FactValidationError as exc:
        return CommandResult(accepted=False, aggregate_id="", denied_reason=f"invalid fact value: {exc}")
    key = fact_key(ref, fact_type, use_case)
    fp = proposal_fingerprint(
        proposed_value,
        profile_version=args.get("profile_version"),
        thresholds=args.get("thresholds"),
    )
    existing = load_fact(conn, key)
    state = fold_overlay_state(existing)
    if state.status in ("DRAFT", "PARTIALLY_CONFIRMED"):
        latest = _latest_proposed(existing)
        if latest is not None and latest.payload.get("proposal_fingerprint") == fp:
            return CommandResult(
                accepted=False,
                aggregate_id=key,
                denied_reason="duplicate of a pending proposal (same fingerprint)",
            )
    rejected_fps = {
        e.payload.get("retired_fingerprint")
        for e in existing
        if e.type == "OVERLAY_FACT_REJECTED"
    }
    if fp in rejected_fps:
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="fingerprint previously rejected (sticky)"
        )
    authority = resolve_authority(conn, adapter, ref, fact_type)
    draft = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_PROPOSED",
        payload={
            "catalog_object_ref": asdict(ref),
            "object_ref": display_object_ref(ref),
            "fact_type": fact_type,
            "use_case": use_case,
            "proposed_value": proposed_value,
            "proposal_fingerprint": fp,
            "evidence_ref": evidence_ref,
            "proposed_by": {"subject": cmd.actor.subject, "actor_kind": cmd.actor.actor_kind},
        },
        actor=cmd.actor,
        expected_version=0 if not existing else None,
    )
    targets = [s for s in authority.subjects if s] or [None]
    for subj in targets:
        eligible: dict[str, str] = {"role": authority.role}
        if subj:
            eligible["subject"] = subj
        open_task(
            conn,
            GateTaskSpec(
                gate=authority.gate,
                required_inputs=("proposed_value",),
                eligible_assignees=eligible,
                allowed_responses=("confirm", "reject"),
                fact_key=key,
                draft_event_id=draft.event_id,
                evidence_ref=evidence_ref,
            ),
            cmd.actor,
        )
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(draft.event_id,))


_OVERLAY_CATALOG = {
    "propose_fact": propose_fact,
}


def register_overlay_commands() -> None:
    for action, handler in _OVERLAY_CATALOG.items():
        register_command(action, handler)
```

- [ ] **Step 5 — run it (passes)**
  - `uv run pytest tests/featuregen/overlay/test_propose_fact.py -v`
  - Expected: PASS (3 tests).

- [ ] **Step 6 — commit**
  - `git add src/featuregen/overlay/commands.py tests/featuregen/overlay/conftest.py tests/featuregen/overlay/test_propose_fact.py && git commit -m "feat(overlay): propose_fact handler opens a human-gate task with dedup"`

---

### Task 4.3: `commands.py` — `confirm_fact` / `reject_fact` (CAS, authority, SoD, close task)

**Files:**
- Modify: `src/featuregen/overlay/commands.py` (append two handlers; extend `_OVERLAY_CATALOG`)
- Test: `tests/featuregen/overlay/test_confirm_reject.py`

**Interfaces:**
- Consumes: everything from 4.2 plus `_cas_target`, `_actor_is_authority`, `_close_fact_tasks`, `proposer_ne_confirmer`.
- Produces: `confirm_fact(conn, cmd) -> CommandResult` (appends `OVERLAY_FACT_CONFIRMED`: `value`, `confirmers`, `expires_at`, `confirms_event_id`); `reject_fact(conn, cmd) -> CommandResult` (appends `OVERLAY_FACT_REJECTED`: `rejected_by`, `reason`, `target_event_id`, `retired_fingerprint`). Both CAS on `cmd.args["target_event_id"]`, enforce resolved authority, and `cancel_task` the open task.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/overlay/test_confirm_reject.py
from psycopg.rows import dict_row

from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.overlay.commands import confirm_fact, propose_fact, reject_fact
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
BOB = build_human_identity(subject="user:bob", role_claims=("data_owner",))
COMPLIANCE = build_human_identity(subject="user:carol", role_claims=("compliance",))


def _orders() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _propose(db, *, fact_type="grain", value=None, use_case=None, actor=BOB, key="p"):
    value = value or {"columns": ["order_id"], "is_unique": True}
    args = {"ref": _orders(), "fact_type": fact_type, "proposed_value": value}
    if use_case is not None:
        args["use_case"] = use_case
    res = propose_fact(
        db,
        Command("propose_fact", "overlay_fact", None, args, actor, key),
    )
    assert res.accepted, res.denied_reason
    return res.produced_event_ids[0]  # the DRAFT (target) event id


def _confirm_cmd(*, fact_type="grain", use_case=None, target, actor=ALICE, key="c"):
    args = {"ref": _orders(), "fact_type": fact_type, "target_event_id": target}
    if use_case is not None:
        args["use_case"] = use_case
    return Command("confirm_fact", "overlay_fact", None, args, actor, key)


def test_owner_confirms_draft_to_verified(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)  # proposed by BOB (four-eyes ok)
    res = confirm_fact(db, _confirm_cmd(target=draft))
    assert res.accepted is True
    key = fact_key(_orders(), "grain")
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE fact_key=%s", (key,))
        assert cur.fetchone()["status"] == "cancelled"


def test_wrong_role_is_denied(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)
    res = confirm_fact(db, _confirm_cmd(target=draft, actor=COMPLIANCE))
    assert res.accepted is False
    assert "authority" in res.denied_reason


def test_stale_target_event_id_is_denied(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    _propose(db)
    res = confirm_fact(db, _confirm_cmd(target="evt_does_not_exist"))
    assert res.accepted is False
    assert "stale" in res.denied_reason


def test_reject_marks_rejected_and_records_fingerprint(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)
    res = reject_fact(
        db,
        Command(
            "reject_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "target_event_id": draft, "reason": "wrong key"},
            ALICE,
            "r",
        ),
    )
    assert res.accepted is True
    key = fact_key(_orders(), "grain")
    stream = load_fact(db, key)
    assert fold_overlay_state(stream).status == "REJECTED"
    rej = next(e for e in stream if e.type == "OVERLAY_FACT_REJECTED")
    assert rej.payload["retired_fingerprint"] is not None


def test_data_owner_cannot_confirm_policy_tag(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(
        db,
        fact_type="policy_tag",
        value={"decision": "deny", "basis": "PII"},
        use_case="ads",
        actor=BOB,
    )
    # ALICE is a data_owner, not Compliance → denied by the fine authority check (SP-0 SoD posture)
    res = confirm_fact(db, _confirm_cmd(fact_type="policy_tag", use_case="ads", target=draft, actor=ALICE))
    assert res.accepted is False
    assert "authority" in res.denied_reason
    # Compliance can confirm it
    ok = confirm_fact(
        db, _confirm_cmd(fact_type="policy_tag", use_case="ads", target=draft, actor=COMPLIANCE, key="c2")
    )
    assert ok.accepted is True
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/overlay/test_confirm_reject.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'confirm_fact'`.

- [ ] **Step 3 — minimal implementation** (append to `commands.py`, then extend `_OVERLAY_CATALOG`)

```python
def confirm_fact(conn: DbConn, cmd: Command) -> CommandResult:
    adapter = current_catalog_adapter()
    args = cmd.args
    ref = args["ref"]
    fact_type = args["fact_type"]
    use_case = args.get("use_case")
    key = fact_key(ref, fact_type, use_case)
    stream = load_fact(conn, key)
    if not stream:
        return CommandResult(accepted=False, aggregate_id=key, denied_reason="fact does not exist")
    state = fold_overlay_state(stream)
    if state.status not in ("DRAFT", "PARTIALLY_CONFIRMED", "REVERIFY", "STALE"):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason=f"fact not awaiting confirmation (status={state.status})",
        )
    if args.get("target_event_id") != _cas_target(state):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="stale confirmation: target_event_id has been superseded",
        )
    authority = resolve_authority(conn, adapter, ref, fact_type)
    if not _actor_is_authority(authority, cmd.actor):
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="actor is not the resolved authority for this fact"
        )
    if not proposer_ne_confirmer(stream, cmd.actor):
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="four-eyes: a proposer may not confirm the same fact"
        )
    proposed = _latest_proposed(stream)
    value = args.get("value", proposed.payload["proposed_value"])
    role = "compliance" if fact_type == "policy_tag" else "data_owner"
    expires_at = (datetime.now(UTC) + _DEFAULT_TTL).isoformat()
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": value,
            "confirmers": [{"subject": cmd.actor.subject, "role": role}],
            "expires_at": expires_at,
            "confirms_event_id": args["target_event_id"],
        },
        actor=cmd.actor,
        caused_by=args["target_event_id"],
    )
    _close_fact_tasks(conn, key, reason="fact confirmed")
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(confirmed.event_id,))


def reject_fact(conn: DbConn, cmd: Command) -> CommandResult:
    adapter = current_catalog_adapter()
    args = cmd.args
    ref = args["ref"]
    fact_type = args["fact_type"]
    use_case = args.get("use_case")
    key = fact_key(ref, fact_type, use_case)
    stream = load_fact(conn, key)
    if not stream:
        return CommandResult(accepted=False, aggregate_id=key, denied_reason="fact does not exist")
    state = fold_overlay_state(stream)
    if state.status not in ("DRAFT", "PARTIALLY_CONFIRMED", "REVERIFY", "STALE"):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason=f"fact not awaiting confirmation (status={state.status})",
        )
    if args.get("target_event_id") != _cas_target(state):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="stale rejection: target_event_id has been superseded",
        )
    authority = resolve_authority(conn, adapter, ref, fact_type)
    if not _actor_is_authority(authority, cmd.actor):
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="actor is not the resolved authority for this fact"
        )
    proposed = _latest_proposed(stream)
    retired_fp = proposed.payload.get("proposal_fingerprint") if proposed else None
    rejected = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_REJECTED",
        payload={
            "rejected_by": cmd.actor.subject,
            "reason": args.get("reason"),
            "target_event_id": args["target_event_id"],
            "retired_fingerprint": retired_fp,
        },
        actor=cmd.actor,
        caused_by=args["target_event_id"],
    )
    _close_fact_tasks(conn, key, reason="fact rejected")
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(rejected.event_id,))
```

Then extend the catalog:

```python
_OVERLAY_CATALOG = {
    "propose_fact": propose_fact,
    "confirm_fact": confirm_fact,
    "reject_fact": reject_fact,
}
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/overlay/test_confirm_reject.py -v`
  - Expected: PASS (5 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/overlay/commands.py tests/featuregen/overlay/test_confirm_reject.py && git commit -m "feat(overlay): confirm_fact/reject_fact with CAS, fine authority and SoD"`

---

### Task 4.4: `commands.py` — `enter_fact` (direct/proactive self-confirm)

**Files:**
- Modify: `src/featuregen/overlay/commands.py` (append `enter_fact`; extend `_OVERLAY_CATALOG`)
- Test: `tests/featuregen/overlay/test_enter_fact.py`

**Interfaces:**
- Consumes: `validate_fact_value`, `resolve_authority`, `_actor_is_authority`, `append_overlay_event`, `load_fact`.
- Produces: `enter_fact(conn, cmd) -> CommandResult` — when the human actor IS the resolved authority, atomically appends `OVERLAY_FACT_PROPOSED` (`expected_version=0`) + `OVERLAY_FACT_CONFIRMED` (self-confirm, audited). Denied for service principals and for a dual-owner `approved_join`.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/overlay/test_enter_fact.py
from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.overlay.commands import enter_fact
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
SVC = build_service_identity(subject="service:profiler", role_claims=("overlay",), attestation="sig")


def _orders() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _customers() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "customers")


def _enter(*, ref, fact_type, value, use_case=None, actor=ALICE, key="e"):
    args = {"ref": ref, "fact_type": fact_type, "proposed_value": value}
    if use_case is not None:
        args["use_case"] = use_case
    return Command("enter_fact", "overlay_fact", None, args, actor, key)


def test_owner_direct_enters_grain_to_verified(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    res = enter_fact(
        db, _enter(ref=_orders(), fact_type="grain", value={"columns": ["order_id"], "is_unique": True})
    )
    assert res.accepted is True
    assert len(res.produced_event_ids) == 2  # PROPOSED + CONFIRMED
    stream = load_fact(db, fact_key(_orders(), "grain"))
    assert fold_overlay_state(stream).status == "VERIFIED"
    confirmed = next(e for e in stream if e.type == "OVERLAY_FACT_CONFIRMED")
    assert confirmed.payload["confirmers"][0]["subject"] == "user:alice"


def test_service_cannot_self_confirm(db, catalog):
    catalog.set_owner(_orders(), "service:profiler")
    res = enter_fact(
        db,
        _enter(
            ref=_orders(),
            fact_type="grain",
            value={"columns": ["order_id"], "is_unique": True},
            actor=SVC,
        ),
    )
    assert res.accepted is False
    assert "human" in res.denied_reason


def test_dual_owner_join_direct_entry_rejected(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:bob")  # two distinct owners → dual
    ref = ApprovedJoinRef(_orders(), _customers(), (ColumnPair("customer_id", "id"),), "N:1")
    value = {
        "from_columns": ["customer_id"],
        "to_ref": {
            "catalog_source": "pg:core",
            "object_kind": "table",
            "schema": "sales",
            "table": "customers",
            "column": None,
        },
        "to_columns": ["id"],
        "cardinality": "N:1",
    }
    res = enter_fact(db, _enter(ref=ref, fact_type="approved_join", value=value))
    assert res.accepted is False
    assert "dual-owner" in res.denied_reason
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/overlay/test_enter_fact.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'enter_fact'`.

- [ ] **Step 3 — minimal implementation** (append to `commands.py`, then extend `_OVERLAY_CATALOG`)

```python
def enter_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Direct/proactive entry (§3.4): a HUMAN resolved authority self-confirms an owner-known fact.
    An audited exception to four-eyes — never available to a service/profiler proposal, and never to
    a dual-owner approved_join (which must use the two-task flow, §6.4)."""
    adapter = current_catalog_adapter()
    args = cmd.args
    if cmd.actor.actor_kind != "human":
        return CommandResult(
            accepted=False, aggregate_id="", denied_reason="self-confirm (enter_fact) requires a human authority"
        )
    ref = args["ref"]
    fact_type = args["fact_type"]
    use_case = args.get("use_case")
    proposed_value = args["proposed_value"]
    try:
        validate_fact_value(fact_type, proposed_value, use_case=use_case)
    except FactValidationError as exc:
        return CommandResult(accepted=False, aggregate_id="", denied_reason=f"invalid fact value: {exc}")
    key = fact_key(ref, fact_type, use_case)
    authority = resolve_authority(conn, adapter, ref, fact_type)
    if authority.dual:
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="dual-owner approved_join cannot be self-confirmed; use the two-task flow",
        )
    if not _actor_is_authority(authority, cmd.actor):
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="actor is not the resolved authority for this fact"
        )
    if load_fact(conn, key):
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="fact already exists; use propose/confirm"
        )
    fp = proposal_fingerprint(proposed_value)
    draft = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_PROPOSED",
        payload={
            "catalog_object_ref": asdict(ref),
            "object_ref": display_object_ref(ref),
            "fact_type": fact_type,
            "use_case": use_case,
            "proposed_value": proposed_value,
            "proposal_fingerprint": fp,
            "evidence_ref": None,
            "proposed_by": {"subject": cmd.actor.subject, "actor_kind": "human"},
        },
        actor=cmd.actor,
        expected_version=0,
    )
    if fact_type == "approved_join":
        confirmers = [
            {"subject": cmd.actor.subject, "role": "data_owner_from"},
            {"subject": cmd.actor.subject, "role": "data_owner_to"},
        ]
    else:
        role = "compliance" if fact_type == "policy_tag" else "data_owner"
        confirmers = [{"subject": cmd.actor.subject, "role": role}]
    expires_at = (datetime.now(UTC) + _DEFAULT_TTL).isoformat()
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": proposed_value,
            "confirmers": confirmers,
            "expires_at": expires_at,
            "confirms_event_id": draft.event_id,
        },
        actor=cmd.actor,
        caused_by=draft.event_id,
    )
    return CommandResult(
        accepted=True, aggregate_id=key, produced_event_ids=(draft.event_id, confirmed.event_id)
    )
```

Then extend the catalog:

```python
_OVERLAY_CATALOG = {
    "propose_fact": propose_fact,
    "confirm_fact": confirm_fact,
    "reject_fact": reject_fact,
    "enter_fact": enter_fact,
}
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/overlay/test_enter_fact.py -v`
  - Expected: PASS (3 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/overlay/commands.py tests/featuregen/overlay/test_enter_fact.py && git commit -m "feat(overlay): enter_fact direct self-confirm for human authority"`

---

### Task 4.5: `commands.py` — `approved_join` dual-confirmation

**Files:**
- Modify: `src/featuregen/overlay/commands.py` (insert a dual branch into `confirm_fact`; add `_confirm_approved_join`)
- Test: `tests/featuregen/overlay/test_approved_join.py`

**Interfaces:**
- Consumes: `confirm_fact` (extended), `_close_fact_tasks`, `_latest_proposed`, `append_overlay_event`.
- Produces: dual-confirmation flow — first owner → `OVERLAY_FACT_PARTIALLY_CONFIRMED` (`by_owner`, `role`, `draft_event_id`); second (distinct) owner → `OVERLAY_FACT_CONFIRMED` recording BOTH confirmers; same-owner-both-sides verifies in a single confirm (handled by the existing single path, since `Authority.dual` is False); either owner's reject → REJECTED (existing `reject_fact`).

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/overlay/test_approved_join.py
from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.overlay.commands import confirm_fact, propose_fact, reject_fact
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
BOB = build_human_identity(subject="user:bob", role_claims=("data_owner",))
EVE = build_human_identity(subject="user:eve", role_claims=("data_owner",))


def _orders():
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _customers():
    return CatalogObjectRef("pg:core", "table", "sales", "customers")


def _ref():
    return ApprovedJoinRef(_orders(), _customers(), (ColumnPair("customer_id", "id"),), "N:1")


def _value():
    return {
        "from_columns": ["customer_id"],
        "to_ref": {
            "catalog_source": "pg:core",
            "object_kind": "table",
            "schema": "sales",
            "table": "customers",
            "column": None,
        },
        "to_columns": ["id"],
        "cardinality": "N:1",
    }


def _propose(db):
    res = propose_fact(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _ref(), "fact_type": "approved_join", "proposed_value": _value()},
            EVE,  # proposer distinct from both owners
            "p",
        ),
    )
    assert res.accepted, res.denied_reason
    return res.produced_event_ids[0]


def _confirm(db, *, target, actor, key):
    return confirm_fact(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _ref(), "fact_type": "approved_join", "target_event_id": target},
            actor,
            key,
        ),
    )


def test_two_step_verify_records_both_approvers(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:bob")
    draft = _propose(db)
    key = fact_key(_ref(), "approved_join")

    first = _confirm(db, target=draft, actor=ALICE, key="c1")
    assert first.accepted is True
    assert fold_overlay_state(load_fact(db, key)).status == "PARTIALLY_CONFIRMED"

    second = _confirm(db, target=draft, actor=BOB, key="c2")
    assert second.accepted is True
    stream = load_fact(db, key)
    assert fold_overlay_state(stream).status == "VERIFIED"
    confirmed = next(e for e in stream if e.type == "OVERLAY_FACT_CONFIRMED")
    subjects = {c["subject"] for c in confirmed.payload["confirmers"]}
    assert subjects == {"user:alice", "user:bob"}


def test_one_confirm_is_insufficient(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:bob")
    draft = _propose(db)
    _confirm(db, target=draft, actor=ALICE, key="c1")
    # same owner trying to also satisfy the second side
    again = _confirm(db, target=draft, actor=ALICE, key="c2")
    assert again.accepted is False
    assert "other owner" in again.denied_reason


def test_same_owner_both_sides_single_confirm_verifies(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:alice")
    draft = _propose(db)
    res = _confirm(db, target=draft, actor=ALICE, key="c1")
    assert res.accepted is True
    assert fold_overlay_state(load_fact(db, fact_key(_ref(), "approved_join"))).status == "VERIFIED"


def test_either_owner_reject_marks_rejected(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:bob")
    draft = _propose(db)
    res = reject_fact(
        db,
        Command(
            "reject_fact",
            "overlay_fact",
            None,
            {"ref": _ref(), "fact_type": "approved_join", "target_event_id": draft, "reason": "no"},
            BOB,
            "r",
        ),
    )
    assert res.accepted is True
    assert fold_overlay_state(load_fact(db, fact_key(_ref(), "approved_join"))).status == "REJECTED"
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/overlay/test_approved_join.py -v`
  - Expected: FAIL — `test_two_step_verify_records_both_approvers` errors/fails: the single-path `confirm_fact` appends `OVERLAY_FACT_CONFIRMED` on the first owner's confirm, so status is `VERIFIED` (not `PARTIALLY_CONFIRMED`) and only one confirmer is recorded.

- [ ] **Step 3 — minimal implementation** — insert the dual branch into `confirm_fact` immediately after `authority = resolve_authority(...)` and before the `_actor_is_authority` single check:

```python
    authority = resolve_authority(conn, adapter, ref, fact_type)
    if fact_type == "approved_join" and authority.dual:
        return _confirm_approved_join(conn, cmd, key, stream, state, authority)
    if not _actor_is_authority(authority, cmd.actor):
        ...  # (existing single-path code continues unchanged)
```

and add the helper at module scope:

```python
def _confirm_approved_join(conn, cmd, key, stream, state, authority):
    actor = cmd.actor
    owners = {s for s in authority.subjects if s}
    if actor.subject not in owners:
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="actor is not an owner of either side of the join"
        )
    partial = [e for e in stream if e.type == "OVERLAY_FACT_PARTIALLY_CONFIRMED"]
    proposed = _latest_proposed(stream)
    if not partial:
        evt = append_overlay_event(
            conn,
            fact_key=key,
            type="OVERLAY_FACT_PARTIALLY_CONFIRMED",
            payload={
                "by_owner": actor.subject,
                "role": "data_owner",
                "draft_event_id": state.draft_event_id,
            },
            actor=actor,
            caused_by=state.draft_event_id,
        )
        _close_fact_tasks(conn, key, subject=actor.subject, reason="first owner confirmed (partial)")
        return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(evt.event_id,))
    first = partial[-1].payload["by_owner"]
    if actor.subject == first:
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="this owner already confirmed; awaiting the other owner",
        )
    expires_at = (datetime.now(UTC) + _DEFAULT_TTL).isoformat()
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": proposed.payload["proposed_value"],
            "confirmers": [
                {"subject": first, "role": "data_owner_from"},
                {"subject": actor.subject, "role": "data_owner_to"},
            ],
            "expires_at": expires_at,
            "confirms_event_id": state.draft_event_id,
        },
        actor=actor,
        caused_by=state.draft_event_id,
    )
    _close_fact_tasks(conn, key, reason="join fully confirmed")
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(confirmed.event_id,))
```

(The same-owner-both-sides case never reaches `_confirm_approved_join`: `Authority.dual` is False, so the
existing single path verifies in one confirm. `reject_fact` already accepts `approved_join` from either
owner because `_actor_is_authority` for a `data_owner` authority admits any subject in `authority.subjects`.)

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/overlay/test_approved_join.py -v`
  - Expected: PASS (4 tests).

- [ ] **Step 5 — re-run the prior command tests (no regression)**
  - `uv run pytest tests/featuregen/overlay/test_confirm_reject.py tests/featuregen/overlay/test_enter_fact.py -v`
  - Expected: PASS (8 tests).

- [ ] **Step 6 — commit**
  - `git add src/featuregen/overlay/commands.py tests/featuregen/overlay/test_approved_join.py && git commit -m "feat(overlay): approved_join dual-confirmation (PARTIALLY_CONFIRMED then VERIFIED)"`

---

### Task 4.6: `commands.py` — `get_task_proposal` (task-scoped read)

**Files:**
- Modify: `src/featuregen/overlay/commands.py` (append `get_task_proposal`)
- Test: `tests/featuregen/overlay/test_get_task_proposal.py`

**Interfaces:**
- Consumes: `human_tasks` (`fact_key`, `eligible_assignees`, `evidence_ref`); `load_fact`; `_latest_proposed`; `read_evidence` (evidence.py).
- Produces: `get_task_proposal(conn, task_id, actor) -> dict` with keys `object_ref, fact_type, use_case, proposed_value, evidence`. Authorized to the task assignee (matching `eligible_assignees.subject`/`role`) or the governance role (`platform-admin`); raises `OverlayCommandError` otherwise.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/overlay/test_get_task_proposal.py
import pytest

from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.overlay.commands import (
    OverlayCommandError,
    get_task_proposal,
    propose_fact,
)
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref, fact_key

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
MALLORY = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))


def _orders():
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _propose_and_task(db):
    res = propose_fact(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {
                "ref": _orders(),
                "fact_type": "grain",
                "proposed_value": {"columns": ["order_id"], "is_unique": True},
            },
            build_human_identity(subject="user:bob", role_claims=("data_owner",)),
            "p",
        ),
    )
    assert res.accepted
    key = fact_key(_orders(), "grain")
    row = db.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open'", (key,)
    ).fetchone()
    return row[0]


def test_assignee_can_read_proposal(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    task_id = _propose_and_task(db)
    out = get_task_proposal(db, task_id, ALICE)
    assert out["fact_type"] == "grain"
    assert out["object_ref"] == display_object_ref(_orders())
    assert out["proposed_value"] == {"columns": ["order_id"], "is_unique": True}
    assert out["use_case"] is None
    assert out["evidence"] is None


def test_non_assignee_is_denied(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    task_id = _propose_and_task(db)
    with pytest.raises(OverlayCommandError):
        get_task_proposal(db, task_id, MALLORY)
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/overlay/test_get_task_proposal.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'get_task_proposal'`.

- [ ] **Step 3 — minimal implementation** (append to `commands.py`; add `from featuregen.overlay.evidence import read_evidence` to the imports)

```python
def get_task_proposal(conn: DbConn, task_id: str, actor) -> dict:
    """Task-scoped proposal read (§7.2): returns what the assignee must see to confirm. Authorized
    to the task's assignee (eligible subject/role) or the governance role; denied to anyone else —
    distinct from the deferred end-user `resolve_fact` authz."""
    row = conn.execute(
        "SELECT fact_key, eligible_assignees, evidence_ref FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone()
    if row is None:
        raise OverlayCommandError(f"unknown task {task_id}")
    key, eligible, evidence_ref = row
    eligible = eligible or {}
    role = eligible.get("role")
    subject = eligible.get("subject")
    authorized = (
        (subject is not None and actor.subject == subject)
        or (role is not None and role in actor.role_claims)
        or ("platform-admin" in actor.role_claims)
    )
    if not authorized:
        raise OverlayCommandError("actor is not authorized to read this task proposal")
    proposed = _latest_proposed(load_fact(conn, key))
    if proposed is None:
        raise OverlayCommandError(f"task {task_id} has no proposal on its fact stream")
    p = proposed.payload
    return {
        "object_ref": p["object_ref"],
        "fact_type": p["fact_type"],
        "use_case": p.get("use_case"),
        "proposed_value": p["proposed_value"],
        "evidence": read_evidence(conn, evidence_ref) if evidence_ref else None,
    }
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/overlay/test_get_task_proposal.py -v`
  - Expected: PASS (2 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/overlay/commands.py tests/featuregen/overlay/test_get_task_proposal.py && git commit -m "feat(overlay): get_task_proposal task-scoped authorized read"`

---

### Task 4.7: `bootstrap.py` — `register_overlay` + `seed_overlay_authz` (end-to-end under PolicyAuthorizer)

**Files:**
- Create: `src/featuregen/overlay/bootstrap.py`
- Test: `tests/featuregen/overlay/test_bootstrap.py`

**Interfaces:**
- Consumes: `register_overlay_event_types` (facts.py); `register_overlay_commands` (commands.py); `event_registry` (SP-0); `authz_policy` table; `projection_checkpoints` table.
- Produces:
  - `register_overlay(handler_registry) -> None` — registers overlay event schemas + the overlay command catalog. (The `timer.overlay_expiry` `OverlayExpiryHandler` is registered into `handler_registry` in Phase 7 Task 7.1, where `freshness.py` is built; this function is extended there.)
  - `seed_overlay_authz(conn) -> None` — idempotent INSERT of the 9 overlay authz rows (overview) + `projection_checkpoints('overlay')` init.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/overlay/test_bootstrap.py
from psycopg.rows import dict_row

from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
from featuregen.overlay.commands import register_overlay_catalog_adapter
from featuregen.overlay.identity import CatalogObjectRef, fact_key


class _Registry:
    """Stand-in HandlerRegistry; Phase 4 registers no runtime handlers."""

    def __init__(self):
        self.handlers = {}

    def register(self, handler):
        self.handlers[handler.name] = handler


def _orders():
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _wire(db, catalog):
    register_overlay(_Registry())
    seed_authz_policy(db)
    seed_overlay_authz(db)
    register_command_authorizer(PolicyAuthorizer())
    from tests.featuregen.overlay.conftest import StubCatalog  # type: ignore

    cat = StubCatalog()
    cat.set_owner(_orders(), "user:alice")
    register_overlay_catalog_adapter(cat)
    return cat


def test_data_owner_can_propose_and_confirm_via_execute_command(db, catalog):
    _wire(db, catalog)
    svc = build_service_identity(subject="service:profiler", role_claims=("overlay",), attestation="sig")
    owner = build_human_identity(subject="user:alice", role_claims=("data_owner",))

    proposed = execute_command(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "proposed_value": {"columns": ["order_id"], "is_unique": True}},
            svc,
            "ik-propose",
        ),
    )
    assert proposed.accepted is True, proposed.denied_reason
    draft = proposed.produced_event_ids[0]

    confirmed = execute_command(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "target_event_id": draft},
            owner,
            "ik-confirm",
        ),
    )
    assert confirmed.accepted is True, confirmed.denied_reason
    key = fact_key(_orders(), "grain")
    n = db.execute(
        "SELECT count(*) FROM events WHERE overlay_fact_id=%s AND type='OVERLAY_FACT_CONFIRMED'", (key,)
    ).fetchone()[0]
    assert n == 1


def test_wrong_role_is_denied_and_audited(db, catalog):
    _wire(db, catalog)
    mallory = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))
    res = execute_command(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "proposed_value": {"columns": ["order_id"], "is_unique": True}},
            mallory,
            "ik-deny",
        ),
    )
    assert res.accepted is False
    assert res.denied_reason == "no matching authz policy"
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit "
            "WHERE event_type='COMMAND_DENIED' AND attempted_action='propose_fact'"
        )
        assert cur.fetchone()["n"] == 1
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/overlay/test_bootstrap.py -v`
  - Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.bootstrap'`.

- [ ] **Step 3 — minimal implementation**

```python
# src/featuregen/overlay/bootstrap.py
from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.events.registry import event_registry
from featuregen.overlay.commands import register_overlay_commands
from featuregen.overlay.facts import register_overlay_event_types

# §6.5 overlay command authz rows (coarse capability only; fine authority/SoD lives in the
# handlers + authority.py). Same shape as authz.policy._POLICY_ROWS.
_OVERLAY_POLICY_ROWS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("propose_fact", "", "data_owner", "human", None),
    ("propose_fact", "", "overlay", "service", None),
    ("run_profiler", "", "overlay", "service", None),
    ("confirm_fact", "", "data_owner", "human", None),
    ("confirm_fact", "", "compliance", "human", None),
    ("reject_fact", "", "data_owner", "human", None),
    ("reject_fact", "", "compliance", "human", None),
    ("enter_fact", "", "data_owner", "human", None),
    ("enter_fact", "", "compliance", "human", None),
)


def register_overlay(handler_registry) -> None:
    """Production wiring for the overlay write side: event schemas (so `append_event` validation
    passes) + the overlay command catalog. The `timer.overlay_expiry` handler is added to
    `handler_registry` in Phase 7 (Task 7.1) once `freshness.OverlayExpiryHandler` exists; this
    function is extended there. `handler_registry` is accepted now so callers can wire once."""
    register_overlay_event_types(event_registry())
    register_overlay_commands()


def seed_overlay_authz(conn: DbConn) -> None:
    """Idempotently seed the overlay authz rows and the overlay projection checkpoint."""
    for action, gate, role, kind, scope in _OVERLAY_POLICY_ROWS:
        conn.execute(
            """
            INSERT INTO authz_policy (action, gate, permitted_role, actor_kind, scope)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (action, gate, permitted_role, actor_kind) DO NOTHING
            """,
            (action, gate, role, kind, scope),
        )
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name) VALUES ('overlay') "
        "ON CONFLICT DO NOTHING"
    )
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/overlay/test_bootstrap.py -v`
  - Expected: PASS (2 tests).

- [ ] **Step 5 — run the whole Phase-4 suite (no regression)**
  - `uv run pytest tests/featuregen/overlay/ -v`
  - Expected: PASS (all Phase-4 tests green).

- [ ] **Step 6 — lint**
  - `uv run ruff check src/featuregen/overlay/authority.py src/featuregen/overlay/commands.py src/featuregen/overlay/bootstrap.py`
  - Expected: no findings.

- [ ] **Step 7 — commit**
  - `git add src/featuregen/overlay/bootstrap.py tests/featuregen/overlay/test_bootstrap.py && git commit -m "feat(overlay): register_overlay + seed_overlay_authz wiring under PolicyAuthorizer"`
