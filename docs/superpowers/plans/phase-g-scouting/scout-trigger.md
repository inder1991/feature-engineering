# Phase-G trigger-surface scout report

Worktree: `/Users/ascoe/Projects/ai/feature-engineering/.claude/worktrees/phase-g` (branch `feature/phase-g`).
All paths below are absolute-relative to that root. Read-only pass; nothing modified.

---

## 0. Headline findings (read this first)

1. **`src/featuregen/materialize/` is entirely unwired.** No API route, no `__main__` subcommand, no
   queue handler, no worker stage imports anything from `featuregen.materialize` except three leaf
   helpers (`canonical.materialize_hash`, `codes.MaterializationRefused`, `inventory.ClusterInventoryV1`)
   pulled in by `data_agent/` and `overlay/upload/bridge_assessment.py`. Verified by grep across `src/`.
   Phase-G is genuinely the first caller.
2. **No existing API route enqueues.** Every governed write in `src/featuregen/api/routes/` executes
   synchronously inside the request transaction (`get_conn`). The only durable-queue producers in the
   whole codebase are `runtime/step.py:149` (commit_step → outbox) and
   `overlay/upload/recipe_formula_shadow.py:793` (the formula-shadow lane → outbox).
3. **The generic `HandlerRegistry` path cannot carry a materialization job as-is.** `process_one` →
   `_build_context` (`runtime/dispatch.py:111-133`) requires `payload["event_id"]` and a `run`-aggregate
   event stream. A job with no run stream is DLQ'd as an unresolvable triggering event.
   **The existing precedent for exactly that problem is the dedicated fenced lane**
   (`recipe_formula_shadow.author.v1`), documented in §1.4 below. That is the pattern to copy.
4. **A gap the trigger surface must close: `AuthoringResult` is not persisted on the live authoring path.**
   See §5.3 — this is the single biggest unknown in "feature id → admission inputs".

---

## 1. How long-running / governed work is triggered today

### 1.1 The handler registry (name → step handler)

`src/featuregen/runtime/handlers.py:6-22` — the whole registry:

```python
class HandlerRegistry:
    """Name -> step Handler. Re-registering a name is a load-time error (§10)."""

    def __init__(self) -> None:
        self._by_name: dict[str, Handler] = {}

    def register(self, handler: Handler) -> None:
        name = handler.name
        if name in self._by_name:
            raise ValueError(f"handler {name!r} already registered")
        self._by_name[name] = handler

    def get(self, name: str) -> Handler:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no handler registered: {name!r}") from None
```

The `Handler` protocol — `src/featuregen/contracts/protocols.py:31-40`:

```python
@runtime_checkable
class Handler(Protocol):
    name: str
    version: int
    timeout_seconds: float

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        """IDEMPOTENT (§5.3). MUST NOT emit feature-/request-stream events, write outside its
        run_id, or read mutable projections. Returns events (validated against the registry) and
        optionally one document. Signals retryable/permanent via HandlerResult.disposition."""
```

`HandlerContext` / `HandlerResult` — `src/featuregen/contracts/envelopes.py:147-176`. Note the key
constraint: **handlers are PURE.** All effects are DECLARED on `HandlerResult`
(`new_events`, `document`, `external_commands`, `timers`, `activations`) and applied atomically by
`commit_step`. `ctx.read_conn` is a *separate, read-only, autocommit* connection
(`runtime/dispatch.py:65-80`) — a handler that tries to write through it fails fast with
`psycopg ReadOnlySqlTransaction`.

### 1.2 Verbatim shape of registering a NEW handler

The only production example — `src/featuregen/aggregates/activation.py:376-419`:

```python
class ActivateVersionHandler:
    """§5.8 saga step 2 — the feature-side activation step the Phase-04 worker dispatches
    (keyed on `queue.handler == name`). ..."""

    name = "activate_version"
    version = 1
    timeout_seconds = 30.0

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        p = ctx.triggering_event.payload
        expires_at = p.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        return HandlerResult(
            disposition=Disposition.OK,
            activations=(
                NewActivation(
                    feature_id=p["feature_id"],
                    ...
                ),
            ),
        )


ACTIVATE_VERSION_HANDLER: Handler = ActivateVersionHandler()


def register_phase06_handlers(registry) -> None:
    """Register Phase-06 saga handlers into Phase-04's HandlerRegistry (production wiring)."""
    registry.register(ACTIVATE_VERSION_HANDLER)
```

Wired into the composition root at `src/featuregen/runtime/worker.py:555-607` (`compose()`), which is
called once by `run_forever` (`worker.py:658`).

### 1.3 Enqueue API and idempotency

`src/featuregen/runtime/queue.py:79-101` (`enqueue`) and `queue.py:104-150` (`enqueue_checked`):

```python
def enqueue(
    conn: psycopg.Connection,
    *,
    message_id: str,
    partition_key: str,
    handler: str,
    payload: Mapping[str, Any],
    available_at: datetime | None = None,
    priority: int = 100,
) -> int:
    """Insert a 'ready' work item; idempotent on message_id. Returns the row id."""
```

`enqueue_checked` adds a `payload_hash` and raises `QueueIdempotencyConflict` when one `message_id`
is reused for materially different work (`queue.py:141-150`). **Use `enqueue_checked` for Phase-G** —
that is what the formula-shadow lane does (`outbox.py:284-292` routes formula handlers to it).

Claiming — `queue.py:153-199` (`claim_one`): `FOR UPDATE SKIP LOCKED`, **excludes** partitions that
already have an in-flight lease (per-aggregate serialization), and **excludes** `_DEDICATED_HANDLERS`.
A `UniqueViolation` on `queue_one_inflight_per_partition` is translated to "nothing claimed".

Failure semantics — `queue.py:309-348`:

```python
def complete(conn, queue_id) -> None:
    """Mark a claimed item done and release its lease."""

def fail_retryable(conn, queue_id, *, error: str) -> None:
    """Transient failure: reschedule with backoff, or DLQ once attempts hit the budget (§5.6)."""
    # attempts >= max_attempts -> status='dead' + _record_dead_letter(reason="retry_exhausted")
    # else                     -> status='ready', available_at = now() + compute_backoff(attempts)

def fail_permanent(conn, queue_id, *, error: str) -> None:
    """Deterministic failure: skip delivery retry, route to DLQ (§5.6)."""
    # status='dead' + _record_dead_letter(reason="poison_permanent")
```

DLQ observability: `queue.py:18-23` `_record_dead_letter` bumps `queue.dlq` and logs
`queue.dead_letter` at error level.

Dispatcher classification — `runtime/dispatch.py:136-270` (`process_one`):
* unknown handler name → `fail_permanent` (BLOCKER #2 poison guard), `dispatch.py:200-206`
* `HandlerTimeout` → `fail_retryable` + `counters.incr("dispatch.leaked_connections")`, `dispatch.py:215-224`
* `Disposition.OK` → `commit_step` inside a savepoint; an OCC `ConcurrencyError` → `fail_retryable("OCC: …")`, `dispatch.py:226-243`
* `Disposition.RETRYABLE` / `PERMANENT` → the matching `fail_*`, `dispatch.py:245-254`
* any raised exception → `fail_retryable(f"handler fault: {exc!r}")` backstop, `dispatch.py:255-264`

Idempotency ledger: `runtime/ledger.py` `is_processed(conn, message_id)` — a redelivered message that
already committed a step is `complete`d as `"duplicate"` (`dispatch.py:158-162`).

The outbox producer path (transactional outbox → relay → queue):
* `runtime/outbox.py:111-148` `insert_outbox_message_checked` (idempotent on `message_id`, refuses
  same-id/different-content with `OutboxIdempotencyConflict`)
* `runtime/outbox.py:151-233` `relay_publish_batch` — three-step leased relay owning its own txs
* `runtime/outbox.py:253-294` `make_queue_publisher(route, *, max_partition_depth, route_required)` —
  topic → handler routing, `BackpressureError` for durable waiting, `UnroutedOutboxTopic` for a
  route-required topic with no route

### 1.4 THE PATTERN TO COPY — the dedicated fenced lane

`recipe_formula_shadow` is a long-running (LLM-authoring), non-run-stream job. Its whole wiring:

| Concern | File:line |
|---|---|
| Topic + handler name constants | `src/featuregen/overlay/upload/recipe_formula_shadow.py:46-47` — `RECIPE_FORMULA_SHADOW_TOPIC = "recipe_formula_shadow.requested.v1"`, `RECIPE_FORMULA_SHADOW_HANDLER = "recipe_formula_shadow.author.v1"` |
| Handler set excluded from `claim_one` | `src/featuregen/runtime/queue.py:33-34` — `FORMULA_SHADOW_QUEUE_HANDLERS = frozenset({"recipe_formula_shadow.author.v1"})`; `_DEDICATED_HANDLERS = CONTROL_SIGNAL_HANDLERS \| FORMULA_SHADOW_QUEUE_HANDLERS` |
| Dedicated fenced claim (300s lease, monotonic `lease_fence`) | `queue.py:202-244` `claim_recipe_formula_shadow` |
| Lease renewal / complete / fail, all fence-guarded | `queue.py:247-306` `renew_…`, `complete_…`, `fail_recipe_formula_shadow(…, permanent: bool)` |
| Relay route reserved + un-overridable | `runtime/worker.py:66-68` `_DEFAULT_RELAY_ROUTE`; `worker.py:96-105` raises `"the reserved recipe-formula shadow route cannot be overridden"` and force-adds it to `route_required` |
| Worker stage | `runtime/worker.py:436-446` `_drain_formula` → `process_recipe_formula_shadow_once(conn, owner=f"{owner}:formula", now=now)`; counted as `tick.formula_processed` |
| The consumer | `src/featuregen/overlay/upload/recipe_formula_worker.py:160-…` `process_recipe_formula_shadow_once` — claim → reauthorize → drift-check → author → terminalize under a fence-guarded `SELECT … FOR UPDATE` (`recipe_formula_worker.py:107-150`) |
| Producer (immutable work item + outbox row) | `recipe_formula_shadow.py:793-800`:<br>`insert_outbox_message_checked(conn, OutboxMessage(message_id=f"formula-shadow:{work_item_id}", partition_key=f"formula-run:{generation_run_id}", topic=RECIPE_FORMULA_SHADOW_TOPIC, payload={"work_item_id": work_item_id}))` |
| Feature flag for the lane | `recipe_formula_shadow.py:82-83` — `os.environ.get("FEATUREGEN_RECIPE_FORMULA_SHADOW", "0") == "1"` |

Note the shape: **the queue payload carries only an opaque `work_item_id`**; every input is frozen in
a durable, content-hashed work-item row and re-verified by the worker. That is exactly the shape a
materialization run wants (the run's inputs must be frozen at trigger time, not re-read at claim time).

### 1.5 How the worker is run

`src/featuregen/__main__.py:31-101`. Three subcommands, `main(argv) -> int` (never `sys.exit`s, so it
is directly testable — `tests/featuregen/test_main_pointer_repair.py` is the template):

```python
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="featuregen", description="FeatureGen platform runtime.")
    sub = parser.add_subparsers(dest="command", required=True)

    worker = sub.add_parser("worker", help="run the durable-runtime worker daemon")
    worker.add_argument("--dsn", default=os.environ.get("FEATUREGEN_DSN"))
    worker.add_argument("--interval", type=float, default=1.0, help="seconds between ticks")

    migrate = sub.add_parser("migrate", help="apply schema migrations (idempotent)")
    migrate.add_argument("--dsn", default=os.environ.get("FEATUREGEN_DSN"))

    repair = sub.add_parser(
        "pointer-repair",
        help="rebuild feature->current-contract pointers (H2d): legacy backfill, or --feature-id "
             "to deterministically repair one feature's pointer")
    repair.add_argument("--dsn", default=os.environ.get("FEATUREGEN_DSN"))
    repair.add_argument("--feature-id", default=None, ...)
    return parser
```

`run_forever` — `worker.py:629-…`: ONE **autocommit** connection, `compose(conn)` builds the registry
+ projections, `register_overlay_config(overlay_config_from_env())`, `_relay_publisher_from_env(...)`,
`current_cost_ceilings()` fail-fast, then loops `run_worker_once` with signal-based graceful shutdown.
`run_worker_once` (`worker.py:374-541`) is ONE bounded non-blocking pass; every stage is wrapped by
`_stage(name)` (`worker.py:346-361`) so one failing stage counts+logs and never stalls the tick.

**A `pointer-repair`-shaped CLI subcommand is the cheapest possible Phase-G trigger** (one committing
transaction, DSN from `--dsn`/`FEATUREGEN_DSN`, `log(...)` on completion) — `__main__.py:68-82` is the
verbatim template.

---

## 2. API route conventions

### 2.1 The router / dependency idiom (identical in every route module)

`src/featuregen/api/routes/gate.py:28-30`, `features.py:30-32`, `governance.py:88-90`,
`ingestion_runs.py:23-24`:

```python
router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
```

`scope="function"` is load-bearing: `get_conn` (`api/deps.py:100-116`) commits on success / rolls back
on error, and the function scope makes the commit run **before** the response is sent, so a failed
commit is a 500 rather than a silent 200 over pre-commit state.

Routers are included in `src/featuregen/api/app.py:139-167` (`create_app`). A new `materialize` router
goes in the `from featuregen.api.routes import (...)` block at `app.py:17-43` and an
`app.include_router(materialize.router)` line.

### 2.2 Permission check — the exact call shape

Route-level guard (the dominant idiom, ~every route):

```python
@router.post("/features", dependencies=[Depends(require_feature_generate)])
def create_feature(body: FeatureSpecIn, conn: _Conn, identity: _Identity) -> dict[str, str]:
```
— `src/featuregen/api/routes/features.py:55-60`.

Prebuilt guards — `src/featuregen/api/deps.py:75-78`:

```python
require_catalog_read = require_permission(CATALOG_READ)
require_catalog_write = require_permission(CATALOG_WRITE)
require_feature_read = require_permission(FEATURE_READ)
require_feature_generate = require_permission(FEATURE_GENERATE)
```

The factory — `api/deps.py:57-70`:

```python
def require_permission(permission: str):
    """A route dependency that 403s unless the caller's roles grant `permission`. ..."""

    def _dep(request: Request,
             identity: Annotated[IdentityEnvelope, Depends(get_identity)]) -> IdentityEnvelope:
        if not has_permission(identity.role_claims, permission):
            audit_access_denied(identity, f"{permission} on {request.method} {request.url.path}")
            raise HTTPException(status_code=403, detail=f"missing permission: {permission}")
        return identity

    return _dep
```

The **governance/admin** gate is a different, deliberately non-permission check — `api/deps.py:81-91`:

```python
def require_confirmer(request, identity) -> IdentityEnvelope:
    """Governance confirmer gate: the caller must carry the raw `platform-admin` role CLAIM — the exact
    claim the overlay's dual-owner confirm authorizes on (join_confirmation.py:68). Deliberately NOT the
    `platform_admin` permission bundle, to avoid a route-passes-but-overlay-denies mismatch."""
    if "platform-admin" not in identity.role_claims:
        audit_access_denied(identity, f"platform-admin claim on {request.method} {request.url.path}")
        raise HTTPException(status_code=403, detail="requires the platform-admin role")
    return identity
```

`audit_access_denied` (`deps.py:38-54`) writes an `ACCESS_DENIED` row to the tamper-evident
`security_audit` chain **on a separate committing connection**, because the 403 rolls the request
transaction back. Skipped when the auth stub is on.

### 2.3 Identity threading

`get_identity` — `api/deps.py:146-172`. Real auth is `Authorization: Bearer <token>` →
`resolve_session(conn, token, now)` → an `authenticated=True` principal whose roles come from the
user's GROUPS. Fallback header stub `X-User` / `X-Roles` is gated on `FEATUREGEN_AUTH_STUB=1`
(`deps.py:94-97`, OFF by default = production-safe). The identity is threaded into domain calls as
`identity.subject` (e.g. `gate.py:117` `decided_by=identity.subject`) or as the whole envelope on a
`Command` (`governance.py:225-230` `actor=identity`).

### 2.4 Request/response model conventions

Pydantic `BaseModel` inputs declared at module top; plain `dict` / dataclass returns.
`src/featuregen/api/routes/gate.py:33-49`:

```python
class EvaluateIn(BaseModel):
    cohort: str
    since: datetime
    until: datetime


class ActivationDecisionIn(BaseModel):
    evaluation_id: str
    decision: str
    reason: str = ""
    supersedes_decision_id: str | None = None
```

`Field(min_length=1)` for required non-empty strings (`features.py:36`, `46`).
Domain `ValueError` → 422 (`gate.py:119-120`), not-found → 404 (`features.py:80`),
governed denial → **`return JSONResponse(status_code=409, …)` and never `raise`** so `get_conn` still
COMMITS the security-audit row the domain wrote — the rationale is spelled out verbatim at
`governance.py:232-243`.

### 2.5 Does any route enqueue instead of executing? — **NO.**

Grep of `src/featuregen/api/routes/` for `enqueue|outbox|BackgroundTask`: zero producer hits.
The heaviest existing route, `POST /uploads` (`api/routes/uploads.py`), runs the **entire** ingest
(parse + Pass A/B/C + LLM enrichment + graph build) inline in the request transaction.

But it also carries **the durable-run pattern Phase-G should study** — a route-started, worker-reconciled
long job:

* `uploads.py:153` `run_id = open_run(conn, origin_type="upload", catalog_source=source, ...)`
* `uploads.py:157` `response.headers[_RUN_ID_HEADER] = run_id` (`X-Ingestion-Run-Id`, `ingestion_run.py:54`)
* `uploads.py:162` `request.state.ingestion_run_id = run_id` (so a raw 500 still surfaces the id via
  the app-level exception handler at `app.py:112-137`)
* `uploads.py:253` / `277` / `294` `terminalize_run(...)` / `terminalize_run_durable(...)` on every exit path
* worker crash-recovery stage: `worker.py:324-343` `_sweep_ingestion_runs` →
  `reconcile_ingestion_runs(conn, now=..., lease_timeout=...)` (`overlay/upload/ingestion_run.py:264`),
  lease seconds from `FEATUREGEN_INGESTION_RUN_LEASE_SECONDS` (`worker.py:338`)
* read surface: `src/featuregen/api/routes/ingestion_runs.py:27-33`
  `GET /ingestion-runs/{run_id}` gated on `require_catalog_read`, 404 on unknown

---

## 3. RBAC

### 3.1 Two separate authorization systems — do not confuse them

**(A) API functional RBAC** — `src/featuregen/identity/permissions.py`. Permissions are the primitive;
roles are bundles; routes check permissions. This is what `require_permission` consults.

**(B) Command-level authz policy** — `src/featuregen/authz/policy.py` (`authorize_command`,
`_POLICY_ROWS` at `policy.py:17-59`, DB table `authz_policy`) plus SoD (`authz/sod.py`). Its role
vocabulary is different and hyphenated/underscored inconsistently: `data_scientist`, `data_owner`,
`compliance`, `validator`, `approver`, `workflow`, `release`, `owner`, `monitoring`, `overlay`,
`platform-admin`, `auditor`, `intake-agent`. Actions include `activate`, `retier`, `break_glass`,
`admin_correct`, `resolve_degraded`, `migrate_workflow_version`. **No materialization action exists.**
This system only engages if Phase-G routes work through `execute_command` — none of the newer
overlay/upload routes do.

### 3.2 The primitives, verbatim

`src/featuregen/identity/permissions.py:23-41`:

```python
# ---- Capabilities (the stable primitive routes depend on) ---------------------------------------
CATALOG_READ = "catalog:read"          # browse the data catalogue: search, join edges, join paths
CATALOG_WRITE = "catalog:write"        # publish/curate the data catalogue: upload, quarantine, entity tags
FEATURE_READ = "feature:read"          # browse the feature + hypothesis catalogue (registry, Feature 360)
FEATURE_GENERATE = "feature:generate"  # run the feature-generation workflow + govern contracts
IAM_MANAGE = "iam:manage"              # administer users / groups / roles
# Confirm/reject discovered joins on the governance queue. NOTE: the route gate (require_confirmer)
# does NOT rely on this yet — it checks the raw `platform-admin` role claim directly, matching the
# overlay's dual-owner confirm. This constant exists for future reconciliation of that gate into the
# permission model.
GOVERNANCE_CONFIRM = "governance:confirm"
# Read SAFE LLM-call audit summaries (which task/dispatch touched a ref, versions, outcome, times).
# RESTRICTED: raw/redacted LLM inputs, raw outputs and repair bodies are NEVER exposed by this — they
# stay in the audit store. Granted only to platform_admin + an explicitly provisioned audit role, so a
# catalog_viewer / data_owner / feature_engineer cannot read the LLM audit trail.
AUDIT_READ = "audit:read"

ALL_PERMISSIONS = frozenset({CATALOG_READ, CATALOG_WRITE, FEATURE_READ, FEATURE_GENERATE, IAM_MANAGE,
                             GOVERNANCE_CONFIRM, AUDIT_READ})
```

### 3.3 The roles, verbatim

`permissions.py:43-58` (six entries — "5 functional roles" in project memory predates `audit_reader`):

```python
# ---- Roles (bundles) ----------------------------------------------------------------------------
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # read-only: browse the data catalogue AND the feature/hypothesis catalogue
    "catalog_viewer": frozenset({CATALOG_READ, FEATURE_READ}),
    # publishes/curates the data catalogue (upload + quarantine + entity tags); does NOT build features
    "data_owner": frozenset({CATALOG_READ, CATALOG_WRITE}),
    # builds features: runs the generation workflow + governs contracts; cannot upload
    "feature_engineer": frozenset({CATALOG_READ, FEATURE_READ, FEATURE_GENERATE}),
    # identity administrator: manages access only (separation of duties from data + feature work)
    "access_admin": frozenset({IAM_MANAGE}),
    # explicitly provisioned audit reader: may read SAFE LLM-call audit summaries and nothing else.
    # Deliberately NOT bundled into catalog_viewer / data_owner / feature_engineer.
    "audit_reader": frozenset({AUDIT_READ}),
    # superuser
    "platform_admin": ALL_PERMISSIONS,
}
```

Helpers: `permissions_for(roles)` `:61`, `has_permission(roles, permission)` `:69`,
`roles_granting(permission)` `:73`.

### 3.4 Closest fit for "may trigger a materialization run"

**`FEATURE_GENERATE = "feature:generate"`** — its own comment is *"run the feature-generation workflow
+ govern contracts"*, and its role bundle is `feature_engineer` (+ `platform_admin`). It is the only
primitive whose semantics already cover "start the workflow that produces a feature". The prebuilt
dependency `require_feature_generate` (`api/deps.py:78`) exists and is already used for the analogous
"registration is the explicit-confirm step" route (`features.py:55`).

**Recommended layering** (mirrors how this platform already stacks a permission with an interlock):
`dependencies=[Depends(require_feature_generate)]` for *who may ask*, plus the durable enablement
decision + flag (§4) for *whether this deployment may run at all*. If Phase-G wants a stricter first
release, `require_confirmer` (raw `platform-admin` claim) is the existing "authority-only, off the
customer path" gate — `gate.py:1-3` describes exactly that posture, and `gate.py:65/90/112/124`
apply it. A brand-new primitive (`materialize:run`) is available but would be the only permission with
no role bundle unless `ROLE_PERMISSIONS` is edited, and `permissions.py`'s own docstring argues against
inventing route-specific role strings.

---

## 4. Flag / activation-interlock conventions ("D8 style")

### 4.1 The three flag idioms in use

**(a) Bare env kill-switch, default OFF, read only at the boundary.** The dominant idiom:

```python
def _live_cross_catalog_flag_on() -> bool:
    """3C.2a — the LIVE governed cross-catalog kill switch, read ONLY in the route (the builder is handed
    the resolved boolean, never the env). OFF by default → no readiness query, no governed lens, byte-
    identical to today. On its own it is necessary-but-not-sufficient: activation approval is still
    required (see :func:`require_live_ready`), so a flag-on-but-unapproved deployment fails closed 503."""
    return os.environ.get("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "0") == "1"
```
— `src/featuregen/api/routes/contract.py:313-319`. Same shape at `contract.py:307-311`
(`FEATUREGEN_INTENT_RANKING`), `recipe_formula_shadow.py:82-83`
(`FEATUREGEN_RECIPE_FORMULA_SHADOW`), `overlay/upload/graph.py:34-35` (`OVERLAY_GOVERNED_JOINS`,
`OVERLAY_PASS_C`), `overlay/upload/ingest.py:140` (`OVERLAY_TABLE_SYNTH`), `:148`, `:163`, `:171`, `:180`.

**(b) Typed mode with fail-closed parsing + a health-endpoint report.**
`src/featuregen/overlay/upload/contract/scope_mode.py:15-53` — a `StrEnum` mode, `_ENV` constant, a
`ScopeModeStatus(mode, configured_value, configuration_valid)` dataclass; an unparseable value returns
the SAFE mode with `configuration_valid=False` rather than raising, and `/health`
(`api/app.py:169-194`) reports `scope_execution_mode` + `scope_mode_configuration_valid` and flips the
whole app to `"degraded"` when invalid.

**(c) Sealed, validated deployment config object.** `src/featuregen/overlay/config.py` —
`overlay_config_from_env()` `:85`, `register_overlay_config(config)` `:141`,
`current_overlay_config()` `:147` which **raises** if nothing is registered ("fail closed: … so a
command/stage that needs config never resolves against a silent default"), `_clear_overlay_config()`
`:158` for tests. Sealed at both entry points: `app.py:96` (lifespan) and `worker.py:665`.
Typed settings live in `src/featuregen/config.py:14-53` (`Settings.from_env()` / `get_settings()`).

### 4.2 "Flag + durable enablement decision" end to end (the charter's D8 style)

This is the fully worked example — `src/featuregen/overlay/upload/contract/live_activation.py`:

| Step | Where |
|---|---|
| Flag | `live_activation.py:55-56` `_flag_on()` → `FEATUREGEN_INTENT_LIVE_CROSS_CATALOG` |
| Deployment identity | `live_activation.py:51-52` `deployment_id()` → `FEATUREGEN_DEPLOYMENT_ID`, default `"unset"` |
| Code version vector (what invalidates an approval) | `live_activation.py:59-70` `current_version_vector()` — a dict of ~13 `*_VERSION` constants, CODE only, deliberately no catalog/graph fingerprint |
| Machine gate harness (read-only) | `src/featuregen/overlay/upload/planner/gate_operate.py` — `select_window` `:46`, `run_gold_suite`, `run_double_compile`, `run_drift_checks`; verdict from `planner/shadow_report.evaluate_machine_gate` |
| Persist an evaluation (content-hashed, PASS/FAIL, stamped with the version vector) | `live_activation.py:77-94` `record_evaluation(...) -> evaluation_id` |
| Persist a decision (APPROVE only over a PASS evaluation) | `live_activation.py:97-114` `record_decision(...) -> decision_id` |
| The runtime interlock | `live_activation.py:117-141` `is_live_cross_catalog_enabled(conn)` — flag ∧ latest non-superseded decision for THIS deployment is APPROVE ∧ its evaluation is PASS ∧ stored version vector == current. Ties resolve REVOKE-first; an `"unset"` deployment never honours an approval |
| The raise-if-not-ready helper | `live_activation.py:144-148` `require_live_ready(conn)` → `LiveActivationNotReady` |
| Optional third prong (signed artifact) | `live_activation.py:195-224` `signed_gate_artifact_valid()` — inert when no `FEATUREGEN_INTENT_GATE_PUBLIC_KEY` (posture logged every call), fully enforced when a key IS configured; content enforcement (`gate_passed`, version match, expiry) at `:159-192` |
| The full combined gate | `live_activation.py:254-261` `cross_catalog_grounding_enabled(conn)` = interlock ∧ signed artifact |
| Boot-time diagnostic (log-only, never crashes startup) | `live_activation.py:226-252` `startup_artifact_check()`; called from `api/app.py:101-104` inside the lifespan, wrapped in a bare `except` |

The **HTTP surface** for the interlock — `src/featuregen/api/routes/gate.py`, all four routes
`dependencies=[Depends(require_confirmer)]`:
* `POST /gate/evaluate` `:65` — read-only verdict
* `POST /gate/enablement-evaluation` `:90` — persists the evaluation, returns `evaluation_id`
* `POST /gate/activation-decision` `:112` — `record_decision(..., decided_by=identity.subject)`,
  `ValueError` → 422
* `GET /gate/cohorts` `:124`

The **consumption** shape at a route — `api/routes/contract.py:565-568` and `:814-816`:

```python
    if body.catalog_source is None and _live_cross_catalog_flag_on():
        try:
            require_live_ready(conn)
        ...
    is_live = is_live_cross_catalog_enabled(conn)   # contract.py:637, :819
```
and the refusal at the governing write — `contract.py:1115`
`if cross_catalog and not cross_catalog_grounding_enabled(conn): …`

**Note the flag-off short-circuit discipline**: when the flag is off there is *no DB query at all*, so
the off path stays byte-identical. Phase-G's switch should preserve that property.

### 4.3 Full inventory of existing env flags (grep `os.environ.get("FEATUREGEN|OVERLAY`)

`FEATUREGEN_AUDIT_HMAC_KEY` (`config.py:45`) · `FEATUREGEN_AUTH_STUB` (`api/deps.py:97`) ·
`FEATUREGEN_AUTO_MIGRATE` (`api/app.py:74`) · `FEATUREGEN_DEPLOYMENT_ID` (`live_activation.py:52`) ·
`FEATUREGEN_DSN` (`config.py:43`, `__main__.py:36/40/46`) · `FEATUREGEN_ENVIRONMENT` (`config.py:48`) ·
`FEATUREGEN_EXTERNAL_MAX_ATTEMPTS` (`runtime/external_commands.py:333`) ·
`FEATUREGEN_EXTERNAL_STALE_SECONDS` (`worker.py:402`) ·
`FEATUREGEN_INGESTION_RUN_LEASE_SECONDS` (`worker.py:338`) ·
`FEATUREGEN_INTENT_GATE_ARTIFACT` (`live_activation.py:215`) ·
`FEATUREGEN_INTENT_GATE_PUBLIC_KEY` (`config.py:46`) ·
`FEATUREGEN_INTENT_LIVE_CROSS_CATALOG` (`contract.py:319`, `live_activation.py:56`) ·
`FEATUREGEN_INTENT_RANKING` (`contract.py:311`) ·
`FEATUREGEN_INTENT_SCOPED_APPLICABILITY` (`contract/gate1.py:332`) ·
`FEATUREGEN_LEAKED_CONN_CAP` (`worker.py:400`) · `FEATUREGEN_LLM_*` (`intake/llm_claude.py:49-54`) ·
`FEATUREGEN_LOG_LEVEL` (`runtime/logging_setup.py:34`) ·
`FEATUREGEN_MAX_CANDIDATES` (`runtime/cost_budget.py:44`) ·
`FEATUREGEN_MAX_UPLOAD_BYTES` (`uploads.py:63`) ·
`FEATUREGEN_OM_ALLOWED_HOSTS` (`integrations.py:121`) ·
`FEATUREGEN_PRODUCER_COMMIT` (`config.py:47`) ·
`FEATUREGEN_RECIPE_FORMULA_SHADOW` (`recipe_formula_shadow.py:83`) ·
`FEATUREGEN_RELAY_ROUTES` / `FEATUREGEN_RELAY_REQUIRED` (`worker.py:85`, `:100`) ·
`FEATUREGEN_SCOPE_EXECUTION_MODE` (`scope_mode.py:20`) ·
`OVERLAY_ENRICH_*`, `OVERLAY_ENTITY_BRIDGES`, `OVERLAY_GOVERNED_JOINS`, `OVERLAY_PASS_C`,
`OVERLAY_SEMANTIC_BINDING_*`, `OVERLAY_SEMBIND_DEADLINE_S`, `OVERLAY_TABLE_SYNTH`.

Naming convention: platform-wide switches use `FEATUREGEN_*`; overlay/ingest-local ones use `OVERLAY_*`.
Phase-G should use `FEATUREGEN_MATERIALIZE_*`. Documented in `.env.example` (5.0 KB) and `.env.demo`.

---

## 5. What makes a feature triggerable

### 5.1 `admit_artifacts` — the Gate-1 preconditions

Signature — `src/featuregen/materialize/admission.py:140-157`:

```python
def admit_artifacts(conn: DbConn, inputs: Iterable[ResolvedFeatureInput]) -> tuple[AdmittedFeature, ...]:
    """Admit every input, in order, or refuse the WHOLE batch (spec §1.2)."""
```

Input type — `admission.py:111-121`:

```python
@dataclass(frozen=True, slots=True)
class ResolvedFeatureInput:
    intent: AuthoringIntent      # featuregen.formula.turns.AuthoringIntent
    result: AuthoringResult      # featuregen.formula.result.AuthoringResult
```

The six checks, `admission.py:160-178`, each with its `CompilationRefusalCode`:

| # | Check | Storage read | Refusal code | Impl |
|---|---|---|---|---|
| 1 | a terminal trace event exists for `result.authoring_run_id` | `authoring_trace_event` (migration 1020), kind ∈ {`COMPLETED`,`FAILED`} | `AUTHORING_RUN_INCOMPLETE` | `admission.py:183-195` via `formula.trace.read_terminal_event` |
| 2 | `materialize_hash(payload) == payload_hash` | same row | `TERMINAL_PAYLOAD_TAMPERED` | `admission.py:200-216` |
| 3 | `payload["authoring_disposition"] == "RESOLVED"` (**payload, not event kind** — REJECTED/UNSUPPORTED also write `COMPLETED`) | same row | `NOT_RESOLVED` | `admission.py:221-234` |
| 4 | `formula_content_hash(result.candidate_formula) == payload["candidate_formula_hash"]` (the supplied `.candidate_formula_hash` field is **never read**) | same row + the in-memory formula | `FORMULA_HASH_MISMATCH` | `admission.py:239-274` |
| 5 | the six §F axes on `result` equal the payload's | same row | `AXES_MISMATCH` | `admission.py:279-301`, axis list at `:83-90` |
| 6 | `authoring_intent_hash(intent) == authoring_run.intent_hash` | `authoring_run` (migration 1020, write-once manifest) | `INTENT_HASH_MISMATCH` | `admission.py:306-325` |

Plus the plan-error guards (not governed refusals): `hive_identifier(intent.name)` must normalize
(`admission.py:330-350`) and no two features may fold to one column
(`_reject_name_collisions`, `admission.py:353-363`) → `FeatureNamePlanError`.

There is **no partial admission**: the first refusal raises and the batch yields nothing.

### 5.2 Everything downstream of admission (the rest of the run's preconditions)

The compile chain, in order, with what each needs:

1. **`compile_ir(conn, admitted, *, roles, spine_decl, inventory, bridge_realizations=None)`** —
   `src/featuregen/materialize/ir.py:232-315`. Returns `FormulaExecutionIRV1 | MaterializationRefused`.
   * **spine declaration** (`SpineSourceDeclarationV1`, `spine.py:284`) is REQUIRED — a `None`
     declaration returns `SPINE_SOURCE_NOT_DECLARED` (`spine.py:774-780`). *There is no store for
     this today; the trigger surface must supply or persist it.*
   * `validate_spine_declaration(conn, declaration, roles=…)` (`spine.py:745-800`) checks, in order:
     declaration self-consistency → column existence + read scope (`READ_SCOPE_INSUFFICIENT`) →
     governed `entity_assignment` agrees → governed `GRAIN` makes the keys unique →
     governed `availability_time` backs the availability column (`AVAILABILITY_TIME_NOT_GOVERNED`).
     **These are governed overlay facts**, i.e. VERIFIED `grain` / `entity_assignment` /
     `availability_time` rows must already exist for the spine table.
   * grain entity must equal the spine's entity, else `GRAIN_PATH_NOT_GOVERNED` (`ir.py:279-286`).
   * every body expression compiles via `compile_expression` (join plans, bridges, windows).
   * **cluster inventory** (`ClusterInventoryV1`, `inventory.py:302-347`) is REQUIRED. It is loaded
     from a **YAML file a human wrote**: `load_inventory(path)` `inventory.py:593`. No env var, no
     DB table, no caller — grep shows only tests call it. **Phase-G must decide how the inventory
     path reaches the trigger.**
   * cross-catalog formulas additionally need `executable_bridge_realizations(conn,
     purpose="feature_generation", environment=inventory.environment_id)` to return a current
     realization (`ir.py:270-276`).
2. **`authorize_compilation(conn, irs, spine, *, roles)`** — Gate 2, `ir.py:565-637`. Group-wide.
   Returns `AuthorizedCompilation | MaterializationRefused`. Refuses `COLUMN_NOT_GOVERNED` (existence
   first) then `READ_SCOPE_INSUFFICIENT`. Raises `ValueError` on an empty group or an IR compiled
   against a different spine.
3. **`derive_contract` / `group_by_contract` / `derive_group_contract`** —
   `materialize/contract.py:569`, `:686`, `:733` → a `ContractGroup` (`contract.py:561`).
4. **`build_group_plan(group, features, *, logical_group_name)`** — `group_plan.py:287-345` →
   `FeatureGroupPlanV1`; `expected_schema` `:349`, `expected_schema_hash` `:372`.
5. **`bind_group` / `plan_revision` / `physical_target_for`** — `binding.py`. The published target is
   always `sandbox_feature.<hive name>` (`binding.py:56` `SANDBOX_NAMESPACE = "sandbox_feature"`;
   `binding.py:59-66`) — **there is no production namespace in this slice.**
6. **Render** — `materialize/render/` + `identity.seal_project` / `generated_project_hash` /
   `sandbox_execution_hash` (`identity.py:62-73`, `GENERATED_LOCK_FILENAME = "GENERATED.lock"` `:78`).
7. **`prepare_run(rendered, inventory, metastore, *, generation_id, run_id, business_dt, requests,
   staging_base, capability_attestation_id, bridge_authorization=None, …)`** —
   `runprep.py:831-…`. Needs:
   * a live **`MetastorePartitions`** probe (Protocol, `runprep.py:153`) — partition existence
   * a **`capability_attestation_id`** — from `publish.record_attestation` (`publish.py:408`), which
     "accepts nothing else" than a real `ProbeResult` (`publish.py:235`, `assess_probe_observations`
     `:315`). *So a publication-capability probe must have been run against the environment first.*
   * `business_dt` (ONE per run; multi-partition/backfill is deferred — `docs/DEFERRED-WORK.md` §A.1)
   * `REQUIRED_RUN_PARAMETERS` exactly (`render/project.py`), enforced twice: `check_run_parameters`
     (`submit.py:101-123`) and the rendered `RunParametersHook`.
8. **`LocalClusterSubmitter(python_executable, env, timeout_seconds)`** — `submit.py:137-210`.
   `python_executable` has **no default** ("nothing may silently run the artifact in the control
   plane's own interpreter, which does not have pyspark"); `PYSPARK_PYTHON` **and**
   `PYSPARK_DRIVER_PYTHON` must both be in the merged env or `submit` raises before a process exists.
9. **Control plane** — `control_plane.py`. All INSERT-only (migration `1034_materialization_control_plane.sql`,
   ordering trigger in `1044_run_event_ordering.sql`). `record_generation` `:376`,
   `append_run_event` `:387` (caller supplies `seq`; duplicates/out-of-order are `UniqueViolation` /
   `RaiseException`), `record_run_manifest`, `run_status(conn, run_id)` `:422` folds status from events.
   Event vocabulary `RunEventKind` (`control_plane.py:139-155`): `RUN_PREPARED`, `RUN_SUBMITTED`,
   `COMPUTATION_COMPLETED`, `GATES_PASSED`, `GATES_FAILED`†, `PUBLISHED`†, `PUBLICATION_REFUSED`†,
   `RUN_FAILED`† († terminal). `RunStatus` at `:158-176`.
   **Note: no `REQUESTED` / `ACCEPTED` / `RUNNING` kinds exist** — the `REQUESTED→ACCEPTED→RUNNING→…`
   state machine is explicitly deferred (`docs/DEFERRED-WORK.md` §A, Child-5 row). A queue-triggered
   design therefore has *no control-plane event to record "a run was asked for"* until it reaches
   `RUN_PREPARED`; Phase-G either accepts that gap or reserves a new kind (which means touching the
   migration's CHECK constraint).
   **Nothing here mints ids or timestamps** — `generation_id`, `run_id`, `seq`, `occurred_at` are all
   supplied by the caller (`control_plane.py` module docstring, lines 22-30). Use
   `featuregen.aggregates.ids.mint_id(prefix)` (`aggregates/ids.py:24`, ULID-style, time-ordered).

### 5.3 ⚠️ THE GAP: feature id → `(AuthoringIntent, AuthoringResult)`

There is **no path today** from a user-facing `feature_id` to the two objects `admit_artifacts` needs.

* `AuthoringResult` is explicitly **"Pure in-memory: no DB, no execution, no durable artifact"**
  (`src/featuregen/formula/result.py:26-28`). It is returned by
  `formula.authoring.run_authoring(conn, intent, author_client, critic_client, roles=…, actor=…)`
  (`authoring.py:266`) and never stored.
* The live authoring trace's terminal payload does **NOT** carry the formula —
  `formula/authoring.py:394-409` writes only `authoring_disposition`, the six axes,
  `candidate_formula_hash`, `critic_findings_hash`, `output_requirements`, `authority_failures`.
  So `TypedFormulaV1` is **not recoverable** from the store admission reads.
* **There are TWO authoring trace stores and they are not the same one:**
  * `authoring_run` + `authoring_trace_event` — migration `1020_formula_authoring_trace.sql`, written
    by `src/featuregen/formula/trace.py` (`_INSERT_RUN` `:104`, `_INSERT_EVENT` `:108`). **This is what
    `admission.py` reads** (`admission.py:60`, `:189`, `:318`).
  * `formula_authoring_run` + `formula_authoring_trace_event` — migration `1022_formula_replay_trace.sql`,
    written by `src/featuregen/formula/replay_trace.py`, driven by the shadow queue lane
    (`recipe_formula_worker.py:365` → `replay_authoring.run_authoring`). **This one DOES persist the
    formula**: `replay_authoring.py:151-162` `_terminal_payload` includes `"result": _plain(result)`
    (which contains `candidate_formula`), and `_restore_terminal_result` (`replay_authoring.py:179-249`)
    rebuilds a full `AuthoringResult` from it, re-verifying `candidate_formula_hash`.
  Admission would refuse a run authored via the replay/shadow lane (no row in `authoring_trace_event`),
  and cannot rebuild the artifact for a run authored via the live lane.
* `AuthoringIntent` (`formula/turns.py:147-157`: `name`, `hypothesis`, `target_entity`,
  `target_grain_keys`, `recipe_authoring_context`) is likewise not persisted whole — only its
  `intent_hash` is (`authoring_run.intent_hash`), and check 6 hashes **only** `name` / `hypothesis` /
  `target_entity` / `target_grain_keys` (explicitly noted at `admission.py:313-317`).
* `formula.authoring.run_authoring` has **zero callers in `src/`** — only `tests/` and the materialize
  fixtures. The API never authors a formula on the live path.
* The API-visible feature registry (`POST /features`, `features.py:55`; `feature` table, migration 0970)
  has no column linking a feature to an `authoring_run_id`. Grep confirms `authoring_run_id` appears
  only in `formula/*` and `materialize/*`.

**Consequence for the trigger surface.** Phase-G must pick one of:
(a) trigger takes an explicit `authoring_run_id` **and** the caller re-supplies intent+result
    (matches today's `ResolvedFeatureInput` contract, but the API cannot construct one);
(b) persist the intent + result at authoring time (a new write on the live path, mirroring
    `replay_authoring._terminal_payload`) so a `feature_id → authoring_run_id → (intent, result)`
    resolver can exist;
(c) route through the replay lane's richer store and teach admission (or a resolver) to read it.
This is a **design decision Phase-G cannot avoid**, and the trigger surface's pre-flight validation
depends on which one is taken.

### 5.4 Concrete pre-flight checklist the trigger surface can run BEFORE starting a run

Cheap, all read-only, all fail-fast:

1. `formula.trace.run_status(conn, authoring_run_id)` is complete, and
   `read_terminal_event(...).payload["authoring_disposition"] == "RESOLVED"` — the two conditions
   admission checks 1 and 3 (`formula/trace.py:286`).
2. A resolvable `(intent, result)` pair exists (see §5.3 — currently unresolved).
3. A `SpineSourceDeclarationV1` exists for the group, and
   `validate_spine_declaration(conn, decl, roles=identity.role_claims)` does not refuse — this alone
   proves the governed `entity_assignment` / `grain` / `availability_time` facts are VERIFIED.
4. The cluster inventory YAML resolves (`load_inventory(path)`) and its `environment_id` matches the
   deployment (`Settings.environment`, `config.py:38`).
5. A `PublicationCapabilityAttestation` exists for the environment
   (`publish.read_attestations(conn, …)`, `publish.py:468`) — else `prepare_run` cannot be called.
6. No name collision: every `hive_identifier(intent.name)` in the batch is distinct
   (`admission.hive_identifier`, `:330`).
7. The submitter environment is real: `python_executable` set, `PYSPARK_PYTHON` +
   `PYSPARK_DRIVER_PYTHON` present (`submit.py:170-175`).
8. Read scope: `authorize_compilation` is group-wide — refusing one element refuses the whole group,
   so per-feature triggering does **not** reduce risk; the group is the natural unit.

**Per-feature vs per-group, decided by the code**: Gate 2 (`ir.py:565-637`) authorizes the group's
complete physical read set and *raises* on an empty group; `build_group_plan` requires the planned
features to be **exactly** the group's members (`group_plan.py:317-325`); publication is atomic per
group (`docs/DEFERRED-WORK.md`: "atomic group publication (a partially-published table is a
correctness failure)"). **The trigger must be per-GROUP.** A per-feature endpoint would either be a
one-member group or a lie.

---

## 6. Existing E2E test patterns — the Phase-G acceptance-test template

### 6.1 The fullest HTTP-driven governed flow

**`tests/featuregen/api/test_full_ingestion_e2e.py`** (293 lines). Docstring, lines 1-8:

> FULL-STACK end-to-end: CSV upload -> Pass C discovery -> dual-admin confirm -> traversal ->
> dashboard — every state change driven through the REAL HTTP routes (FastAPI TestClient), never a
> direct `ingest_upload`/`propose_fact`/`confirm_fact` call.

Shape to copy:

* Module docstring states THE CHAIN as numbered stages and, crucially, **why each stage is the seam
  no per-stage suite covers**.
* Module-level identity constants, one per persona, with a comment naming the exact claim
  (`test_full_ingestion_e2e.py:68-71`):
  ```python
  UPLOADER = {"X-User": "tester", "X-Roles": "platform_admin"}   # catalog:write (functional bundle)
  ADMIN1   = {"X-User": "priya",  "X-Roles": "platform-admin"}   # raw confirmer claim (hyphen)
  ADMIN2   = {"X-User": "rahman", "X-Roles": "platform-admin"}   # DISTINCT second confirmer
  VIEWER   = {"X-User": "v",      "X-Roles": "catalog_viewer"}   # catalog:read (dashboards)
  ```
* `@pytest.fixture(autouse=True) def _clean_process_globals()` (`:74-82`) clearing process globals
  (`_clear_catalog_adapter()`, `_clear_overlay_config()`) so nothing leaks into fail-closed suites.
* A `sealed_config` fixture (`:85-93`) that **must depend on `client`** because the app lifespan seals
  its own env config at TestClient startup and `register_overlay_config` is last-writer-wins.
* Flags set with `monkeypatch.setenv` **before** the POST, with a comment that ingest reads env per
  call (`:110`).
* Assertions alternate HTTP responses with **direct DB reads on the same `conn`** to prove the
  durable side effect (`:120-124`, `:175-180`).
* Four tests in the file: the happy chain, a **regression test that documents a real bug this e2e
  found**, a **negative probe proving the gate is armed** ("prove the happy path's green is not a
  silently-skipped gate", `:245`), and a **flag-OFF byte-identical test** (`:274-293`).

### 6.2 Fixtures / harness

* `tests/featuregen/api/conftest.py:17-46` — `make_client` / `client`. Overrides **both** `get_conn`
  and `get_feature_gen_conn` to the shared rolled-back `conn`; `_auth_stub` autouse fixture sets
  `FEATUREGEN_AUTH_STUB=1` (`:10-14`); `admin_headers` / `non_admin_headers` (`:49-59`).
* `tests/conftest.py:45-72` — session `_dsn` (env `FEATUREGEN_TEST_DSN` or an ephemeral
  `pytest-postgresql` cluster, migrations applied once) and the function-scoped `conn` that rolls back.
* `tests/featuregen/conftest.py:25-31` and `tests/featuregen/runtime/conftest.py:14-18` — the `db`
  alias for `conn`.
* **For a worker-driven test**: `tests/featuregen/runtime/conftest.py:210-249` —
  `_throwaway_autocommit_db` (creates/drops an isolated migrated DB) and `autocommit_worker_conn`
  ("the exact connection mode `run_forever` opens"). Use these if the Phase-G acceptance test drives
  `run_worker_once`. Existing consumers: `tests/featuregen/runtime/test_worker.py`,
  `test_dispatch.py`, `test_queue.py`.
* **For a CLI-driven test**: `tests/featuregen/test_main_pointer_repair.py` — drives
  `featuregen.__main__.main(argv)` directly and asserts the int exit code.
* **Materialize fixtures already exist**: `tests/featuregen/materialize/fixtures.py` —
  `authored_formula(name)`, `raw_proposal(name)`, `intent_for(name, **kw)`,
  `seed_materialize_catalog(db)` (seeds the governed catalog through `build_graph` +
  `record_field_evidence` + `resolve_and_project`, **not** flat inserts), plus
  `test_fixtures.py::test_every_fixture_is_what_child1_really_resolves` pinning the two halves together.
  `tests/featuregen/materialize/test_admission.py:66-100` shows how to seed a REAL resolved run:
  ```python
  @pytest.fixture
  def resolved_run(catalog):
      """A REAL run that really resolved: real manifest, real terminal event, real formula."""
      return run_authoring(catalog, intent_for(_FEATURE), _author(raw_proposal(_FEATURE)),
                           _critic(), roles=(), actor=_ACTOR)
  ```
  with a module-scoped `no_dsn` fixture (`test_admission.py:45-53`) deleting `FEATUREGEN_DSN` because
  write-once trace rows a durable connection commits can never be cleaned up between suite runs —
  **Phase-G must do the same or use `_throwaway_autocommit_db`.**
* Other e2e-ish HTTP suites worth skimming: `tests/featuregen/api/test_gate_routes.py` (the closest
  analogue: an authority-only flag+interlock surface), `test_delivery0_scope_enforcement.py`,
  `test_require_confirmer.py`, `tests/featuregen/overlay/upload/contract/test_no_permissive_path_when_live.py`.

---

## 7. Recommendation and the deciding evidence

**Route-vs-queue.** A materialization run is minutes-to-hours of Spark under a
`LocalClusterSubmitter` with a default `timeout_seconds = 3600.0` (`submit.py:162`) and a
`subprocess.Popen` that must own its own process group. That cannot live in a FastAPI request:
`get_conn` holds one transaction open for the request's whole life (`api/deps.py:100-116`), and a
disconnect/restart would orphan a Spark process writing into `staging_root` (the exact hazard
`submit.py:150-157` documents). **Queue, not route** — specifically the **dedicated fenced lane**
(§1.4), not the generic `HandlerRegistry` path, because `_build_context` (`dispatch.py:111-133`)
requires a run-stream `event_id` a materialization job does not have, and the formula-shadow lane
already solved exactly this (300s renewable lease + monotonic `lease_fence` +
`renew_recipe_formula_shadow`, `queue.py:247-260`) versus `claim_one`'s 30s default.

**The route still exists** — but as a thin, fast **producer**: validate the pre-flight checklist
(§5.4) synchronously, mint `generation_id`/`run_id`, freeze the inputs into a durable work-item row,
`insert_outbox_message_checked`, return `202` + the run id in a header (the `X-Ingestion-Run-Id`
precedent, `uploads.py:157`), and expose a `GET /materialization-runs/{run_id}` read surface folding
`control_plane.run_status` (the `ingestion_runs.py:27-33` precedent).

**Unit = group** (§5.2 closing paragraph). **Permission = `feature:generate` / `require_feature_generate`**,
optionally hardened to `require_confirmer` for the first release. **Flag = a `FEATUREGEN_MATERIALIZE_*`
env kill-switch default `"0"`, read only at the trigger boundary, layered over a durable
evaluation+decision interlock in the `live_activation.py` shape** (§4.2), with the flag-off path
short-circuiting before any DB read.

**Two things Phase-G cannot design around and must decide explicitly:**
1. The `feature_id → (AuthoringIntent, AuthoringResult)` gap (§5.3) — including which of the two
   authoring trace stores is authoritative.
2. Where the `ClusterInventoryV1` YAML path comes from (`load_inventory` has no caller and no env var)
   and where the `capability_attestation_id` is produced.

**Housekeeping note:** migration numbers 1034, 1036, 1037, 1038 and 1040 are each double-allocated in
`src/featuregen/db/migrations/`; the highest is `1044_run_event_ordering.sql`, and project memory
records 1044-1050 as reserved by the concurrent release train. Coordinate before allocating.
