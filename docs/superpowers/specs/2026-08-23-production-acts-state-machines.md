# The two production acts — exact state machines, before any implementation

Parent plan §9.1 (revision five) rules that step 7 cannot begin from a list of requirements:
leases, fencing, CAS and reconciliation are properties, not a machine. This document writes the
machine — **publication first**, because it is the act whose partial failure is visible to the
bank — and makes the eight demanded decisions explicitly. Everything here is engineering **behind
unavailable actions**: §0.1.0 keeps `MATERIALIZE_PRODUCTION` and `PUBLISH_PRODUCTION` refusing at
the decision service, so no row in any table below can be created until the owner opens the
policy. Building the machine now is what makes opening the policy a decision rather than a
project.

---

## 1. PRODUCTION PUBLICATION — written first

### 1.0 The design move that removes the worst window

Publication is a POINTER SWAP, not a data write: the values were already materialized and
identity-hashed (`materialized_output_revision`). The classic crash window — "the external system
did something and the database does not know" — exists only if the active pointer lives outside
the database. **It does not.** `production_active_revision` is a database row; the swap and the
attempt's terminal state commit in ONE transaction. What remains external (serving caches,
downstream readers) READS the pointer and read-repairs; nothing external is written during the
act. This is a deliberate narrowing: the machine below has no UNKNOWN_OUTCOME state **because the
design removed the window that produces one**, and a future change that moves the swap outside
the database re-opens §9.1's full list.

### 1.1 States and legal transitions

```
REQUESTED ──claim──▶ CLAIMED ──checks pass──▶ PUBLISHED     (terminal)
   │                    │
   │                    ├── recheck refuses / certificates drift /
   │                    │   output mismatch ─▶ REFUSED       (terminal, product)
   │                    └── crash, lease expires ─▶ (re-claimable REQUESTED-equivalent:
   │                                                CLAIMED with expired lease)
   └── cancel before claim ─▶ CANCELLED                      (terminal)
FAILED (terminal, platform) reachable from CLAIMED on any non-product error.
```

* **Claim** = `FOR UPDATE SKIP LOCKED` over due rows, fence +1 — 1092's discipline, identical to
  the four existing lanes.
* **One live attempt per (environment, logical group)** — partial unique index over non-terminal
  states, the money-guard shape: FAILED/REFUSED/CANCELLED release the slot.

### 1.2 The eight decisions

| Decision | Answer |
|---|---|
| **External operation identity** | None exists: the swap is internal. The attempt id IS the operation identity; `production_active_revision` stores which attempt last swapped it. |
| **Fence propagation** | The pointer row carries `fence`; the swap is `UPDATE … WHERE fence < %s` — a zombie's stale fence loses INSIDE the CAS, not by convention. |
| **Retry attachment** | A redelivered/re-claimed attempt re-attaches iff non-terminal and lease-expired. A new user request while a live attempt exists returns the live attempt (`created: false`). A retry after REFUSED/FAILED is a NEW attempt. |
| **UNKNOWN-OUTCOME state** | Deliberately absent — see 1.0. The reconciler asserts the invariant that makes this legal: no PUBLISHED attempt without the pointer naming it, and vice versa; a violation is a loud gauge, never a guess. |
| **Reconciliation algorithm** | Sweep non-terminal attempts with expired leases → re-claimable (the claim scan already does this); sweep the pointer↔attempt invariant; report, never mutate data. |
| **Quarantine / cleanup** | Nothing to quarantine: publication writes one row. |
| **Partial-write policy** | Structural: ONE pointer per group. There is no partial publication. |
| **CAS** | The swap statement itself (`WHERE fence < %s`), plus the attempt's compare-and-set status moves. |

### 1.3 What CLAIMED checks, in order (all inside one transaction with the swap)

1. **Decision recheck** (§8.2) — `recheck()` against the attempt's stored decision id; drift or
   absence → REFUSED by name.
2. **The output is server-resolved** (§9.1's forgery rule): the request named the
   MATERIALIZATION ATTEMPT; the worker resolves `materialized_output_revision` through the
   composite FK. A client-supplied output id does not exist in the schema.
3. **Certificate comparison, not re-derivation** (§10.3): re-derive each member's method identity
   from live evidence; compare to the sealed identity AND to the subject on the certificate
   binding STORED at materialization. Publication never proceeds on a fresh answer.
4. **The swap**: `INSERT … ON CONFLICT (environment_id, logical_group_name) DO UPDATE … WHERE
   production_active_revision.fence < EXCLUDED.fence`, then attempt → PUBLISHED, same commit.

---

## 2. PRODUCTION MATERIALIZATION

### 2.1 States and legal transitions

```
REQUESTED ─claim─▶ CLAIMED ─submit─▶ RUNNING ─outcome read─▶ STAGED ─promote+record─▶ SUCCEEDED
   │                  │                 │                        │
   │                  │                 └─ crash between submit and outcome read
   │                  │                    ─▶ UNKNOWN_OUTCOME (the state crash recovery FINDS)
   │                  ├─ recheck refuses ─▶ REFUSED (terminal, product)
   │                  └─ platform error ─▶ FAILED  (terminal; staging quarantined)
   └─ cancel before claim ─▶ CANCELLED
UNKNOWN_OUTCOME ─reconciler─▶ RUNNING (still going) | STAGED (finished; manifest verified)
                              | FAILED (cluster says failed / job absent)
                              | UNKNOWN_OUTCOME (cluster unreachable — held, gauged, NEVER guessed)
```

### 2.2 The eight decisions

| Decision | Answer |
|---|---|
| **External operation identity** | `external_operation_id = "fgm:{attempt_id}:{fence}"`, set as the Spark application name/tag at submit and STORED on the attempt before the submit call — the reconciler's question to the cluster is keyed on it. |
| **Fence propagation** | Into the operation id (above), into the staging path (`…/staging/{attempt_id}/{fence}/`), and checked at PROMOTE: a staging dir whose fence is not the attempt's current fence is a zombie's work — quarantined, never promoted. The fence travels in the NAMES the external system already preserves, because Spark carries no first-class fence. |
| **Retry attachment** | Same rule as publication. A redelivery while RUNNING/UNKNOWN re-attaches to the SAME external_operation_id — it asks about the job; it never submits a second one (idempotent retry = re-attach, not re-run). |
| **UNKNOWN-OUTCOME state** | First-class (`UNKNOWN_OUTCOME`), entered whenever the worker cannot prove what the cluster did — crash after submit, timeout reading the outcome. |
| **Reconciliation algorithm** | Per UNKNOWN attempt: query the cluster by external_operation_id. Running → leave (extend lease). Succeeded → verify the staged manifest exists under the fenced path, hash it → STAGED, resume. Failed/absent → FAILED with the cluster's reason. Unreachable → stays UNKNOWN with a gauge (the released-message discipline: held is not judged). |
| **Quarantine / cleanup** | The worker records `quarantine_path` when it fails an attempt with staging present; deletion is an OPERATOR sweep over recorded quarantine paths — the platform never auto-deletes data it is not sure about. |
| **Partial-write policy** | A build's members are COLUMNS of one output: the compute either produces the whole manifest or the attempt fails. There is no per-member partial write to police at this layer; §9's all-must-pass lives at the gate, and the write is all-or-nothing by construction. A future per-member physical layout must amend THIS row before it ships. |
| **CAS** | Status moves compare-and-set (the house `advance` shape); PROMOTE is rename-into-place keyed by the fenced path, and RECORD (output revision + SUCCEEDED) is one transaction. |

### 2.3 Output identity

`materialized_output_revision`: content-addressed over the staged manifest
(`output_revision_id = jcs_sha256(manifest)`), UNIQUE per attempt, append-only. Publication's
composite FK `(materialization_attempt_id, output_revision_id)` means "publish what THAT attempt
produced" is enforced by the schema, not by the reader.

---

## 3. Certificates at the boundary (§10.3, executed)

* `method_certificate_revision` — the parent that did not exist: append-only, typed subject
  (`AUTHORING_METHOD | EXECUTION_STACK`), contract + corpus hashes, outcome, issued_at. Its
  ISSUER is step 9's evaluation programme; until then the table is empty and the reader answers
  `None`, which surfaces as `METHOD_CERTIFICATE_MISSING` — the honest hard-block §9 demands from
  day one.
* `production_attempt_member_certificate` — bindings recorded ON THE MATERIALIZATION ATTEMPT at
  decision time; publication COMPARES against these stored rows (never re-derives-and-proceeds).
  Kind and subject must agree by CHECK; `AUTHORING_METHOD` rows FK to
  `sealed_artifact_member_method_identity` — a hash with a parent is a binding, without one an
  assertion.

## 4. What ships now vs. what waits

| Ships now (step 7 engineering) | Waits |
|---|---|
| Both attempt schemas, the pointer, the output revision, certificates (migrations 1113/1114) | opening the two actions (§0.1.0 — owner) |
| The certificate reader (honest `None`) | certificate ISSUANCE (step 9's programme) |
| Both routes: ask → refuse `ACTION_UNAVAILABLE` today, decide + enqueue when the policy opens | the Spark submit/promote adapters (step 0b substrate) |
| Both reconcilers + worker stage (§15.1: a lifecycle table ships WITH its reconciler) | operator quarantine sweep tooling |
