# Phase-G scout report — Track-1 collision surface + decision-pin seam

Read-only survey. Two trees:

- Phase-G baseline: `/Users/ascoe/Projects/ai/feature-engineering/.claude/worktrees/phase-g`, branch `feature/phase-g` @ **3b0b7b01**
- Track 1 (live, another session): `/Users/ascoe/Projects/ai/feature-engineering/.claude/worktrees/integration-aug01`, branch `feature/integration-2026-08-01-semantic-profiles` @ **75038690**

Controlling doc read in full:
`/Users/ascoe/Projects/ai/feature-engineering/.claude/worktrees/integration-aug01/docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md` (347 lines, sections D1–D13). **This file does not exist in the Phase-G tree** — Phase G's plan must cite it by the integration-worktree path or copy it in.

---

## 0. HEADLINE CORRECTION TO THE CHARTER'S PREMISE

The charter frames Phase G as "codegen remediation (Track 2) vs integration (Track 1), two live branches." Git says something materially different:

```
git merge-base --is-ancestor feature/phase-g main   -> TRUE
git rev-list --left-right --count main...feature/phase-g   ->  13   0
```

**`feature/phase-g` is fully contained in `main`.** It is `main` minus 13 commits. `main` @ **f3424c36** is literally `Merge branch 'worktree-codegen-review-remediation'` — the codegen remediation is ALREADY MERGED TO MAIN, along with `fa9a20b0` and eleven other commits Phase G's baseline lacks.

The 13 commits `main` has that `feature/phase-g` does not:

```
f3424c36 Merge branch 'worktree-codegen-review-remediation'
fa9a20b0 fix(bridges): decision events get statement-time occurred_at — same-transaction order was a ULID coin flip
17a29f02 docs: check off Task 7's code-half steps (1, 2, 4) with executed records
3b74ac33 feat(types): attested data types from engine observation — declared never silently upgraded
830736f0 docs: mark the namespace-pairing handoff EXECUTED with its acceptance record
b35416f4 feat(bridges): pair link candidates by identifier namespace — entity corroborates, never gates
353da245 docs: check off Task 3D (entity map v0) with the executed record
121fbe18 feat(ui): entity map v0 — the ontology becomes visible
f833f3ee feat(api): GET /catalog/entity-map — the map read route, catalog:read gated
90387f69 feat(read-model): entity map v0 — one availability truth, read-scoped counts
6784cfd6 docs: check off Task 3C (the column dossier) with the executed record
9a234a7d feat(ui): the column dossier — asset detail shows everything the platform holds
58200c41 feat(api): asset detail carries the dossier payload — source glossary, type basis, proposals
```

Consequences for the plan:

1. **Phase G should rebase onto `main`, not treat `feature/phase-g` as an independent track.** Building on a branch that is a strict ancestor of `main` guarantees a rebase later; doing it first is free.
2. The charter's claim "the integration branch lacks Track 2" is TRUE but the reason is not that Track 2 is unmerged — it is that **Track 1 forked from `main` at `fa9a20b0` (before the remediation merge) and has not rebased.** Track 1 is 38 ahead / 78 behind `main`.
3. `fa9a20b0` is exactly the tree the controlling doc says it verified against ("Every decision below was verified against `origin/main @ fa9a20b0`", doc line 6) — consistent.

Topology:

```
1588c890 (common trunk: "docs: check off Task 5 (stamp reconciliation)…")
  ├── codegen-remediation lineage ──> 3b0b7b01  (= feature/phase-g HEAD)
  └── … ──> fa9a20b0 ──> +38 commits ──> 75038690 (= Track 1 HEAD)
                     └──> merged with the codegen lineage ──> f3424c36 (= main)
```

`git merge-base feature/phase-g feature/integration-…` = `1588c890`; phase-g 37 ahead / track1 90 ahead of that point.

---

## 1. THE CONTROLLING DOC

### D1 — Canonical hash scheme (doc lines 29–43)

All NEW content hashes in the program — `SemanticContextBundleV1.content_hash`, `dataset_profile_hash`, `CatalogProfileRevisionV1.content_hash`, policy revision hashes, `DatasetSourceSelectionV1`/`DatasetRowSelectionV1.content_hash`, analysis-plan identity v2 — use **RFC 8785 JCS via `materialize/canonical.py:33 materialize_hash`** (the scheme behind the CHECK-pinned `pbr_` binding revision ids, `physical.py:238-244`).

Rules:
- The `field_evidence.py:38-46` `json.dumps(sort_keys=True)` scheme stays for stores that already use it; **no stored hash is rewritten**.
- **No new inline hash implementation may be written; import `materialize_hash`.**
- Excluded from every content hash: wall-clock, job state, environment, physical bindings (which live in `DatasetSourceSelectionV1`), projection timestamps.
- The two bundle builders must byte-match on shared fields; the property test serializes both through the same canonicalizer.

**Phase-G read:** any new Phase-G identity (run lifecycle id, publish-pointer hash, execution-authorization hash) MUST import `materialize_hash` from `materialize/canonical.py`. Phase G is the OWNER of that module, so this is free — but the exclusion list is binding: no wall-clock, no job state, no environment in a content hash.

### D7 — Migration reservations (doc lines 169–188), FULL TABLE VERBATIM

Allocation rule stated above the table: *"uniqueness is the FULL FILENAME; ledger state is recorded as the applied name-set, never a head number (duplicate prefixes exist at 0973/0974/1034/1036/1037/1038/1040 and the runner is lexical + name-ledgered, `db/migrations.py:260-317`)."*

| Number | Reserved by |
| --- | --- |
| 1044 | codegen remediation (`1044_run_event_ordering.sql` — already claimed by that plan) |
| 1045 | semantic Task 2 — catalog semantic scope table ONLY (entity backfill removed per D12.1-revised) |
| 1046 | semantic Task 5 — `structured_result_current` (GENERIC subject/current pointer: subject_kind × subject_ref × result_type, CAS pointer_version; deliberately not gap-specific) |
| 1047 | profile Task 2 — catalog narrative revision + current, plus co-located `graph_node` `authority_role`/`temporal_storage_model` display+decision-link columns (recorded post-hoc; the stream had only this number) |
| 1048 | profile Task 7 — serving policy store |
| 1049 | profile Task 7 — temporal policy store |
| 1050 | Release C Task 10 — crosswalk store |
| 1051 | D13 — `graph_node` display-projection columns `bian_path`, `process_path`, `sub_domain` (joint Task 4 / profile Task 5) |
| 1052 | consumption step — `graph_node.data_role` display projection (derived from the normalized `table_role` at projection time; the facet mechanism requires a literal column; a rebuildable projection is NOT the duplicate store §4-correction-4 forbids) + table-node search-doc slots for `definition`/`business_context` (insert-time + rebuild parity — the read-time join cannot reach FTS matching) |
| 1053-1055 | RESERVED BLOCK — Phase-G execution wiring (PARALLEL SESSION; run lifecycle / publish pointer / whatever its approved plan needs; unused numbers return to the pool when Phase G's plan finalizes). The 1048-1050 reservations above remain Release B/C's, unchanged. |

Closing rule: *"New needs append 1056+ to this table FIRST (edit this doc in the same commit as the migration)."*

**Verified on disk — actual occupancy of 1044–1059 across every ref:**

| ref | migration files present in 1044–1059 |
| --- | --- |
| `main` | `1044_run_event_ordering.sql` |
| `feature/phase-g` | `1044_run_event_ordering.sql` |
| `feature/integration-…` | `1045_catalog_semantic_scope.sql`, `1046_structured_result_current.sql`, `1047_catalog_profile_revision.sql`, `1051_graph_node_classification_axes.sql`, `1052_graph_node_data_role_and_table_prose.sql` |

Swept every local branch for `105[345]_`: **zero hits. 1053, 1054, 1055 are genuinely free and are Phase G's.**

Also note: **1048, 1049, 1050 are reserved-on-paper but NOT yet created on disk** (Release B Task 7 / Release C Task 10 haven't run). Phase G must not opportunistically take them even though the files are absent — the doc's table is the authority, not the filesystem.

The commit that granted the block is Track 1's HEAD itself: `75038690 docs: reserve migrations 1053-1055 for the Phase-G parallel session`. The reservation is live and committed on Track 1's branch — but it is NOT on `main`, so a Phase-G branch cut from `main` will not see the doc text. Phase G's plan should note that the reservation lands with the Track-1 merge.

### D8 — Flag matrix (doc lines 190–207)

| Flag | Enabled by | Depends on |
| --- | --- | --- |
| `FEATUREGEN_FEATURE_CONTEXT` | Release-A deploy gate | — |
| `FEATUREGEN_DATASET_PROFILES` | Release-A deploy gate (same approval, both flags presented) | — |
| `FEATUREGEN_SOURCE_TEMPORAL_SELECTION` | Release-B gate | `FEATUREGEN_DATASET_PROFILES=1` |
| `FEATUREGEN_CROSSWALK_EXECUTION` | Release-C gate | `FEATUREGEN_SOURCE_TEMPORAL_SELECTION=1` — enforced fail-closed at startup, not by convention |

Exact convention, verbatim from the doc:
- All four use the widened truthy set `{"1","true","yes","on"}` (`feature_assist.py:193` pattern) and are added to `deploy/kind/k8s/20-backend.yaml` + `.env.example` (**defaults off**).
- Feature-context versions: v4 ships REGISTERED alongside v2/v3 (D10). Rollback ladder: flag off → v1 menu (unchanged); flag on + `FEATUREGEN_FEATURE_CONTEXT_VERSION=3` → today's shipped behavior; flag on (default) → v4.
- Profile-in-feature-context is governed by `FEATUREGEN_DATASET_PROFILES` AND `FEATUREGEN_FEATURE_CONTEXT`; with either off, feature payloads are byte-identical to that flag's off-state today. Profile Task 6's flag-off check must assert the COMBINATION states.

**The shipped implementation of the convention** (Phase G copies this shape exactly), `integration-aug01/src/featuregen/overlay/upload/profile_vocab.py:32-40`:

```python
DATASET_PROFILES_FLAG = "FEATUREGEN_DATASET_PROFILES"

def dataset_profiles_enabled() -> bool:
    """... Default OFF ⟹ all new API surfaces 404/hidden, the upload part is ignored with a
    warning, and every existing payload is byte-identical. Reads the widened truthy set
    ``{"1","true","yes","on"}`` (D8; the ``feature_context_enabled`` pattern)."""
    return os.environ.get(DATASET_PROFILES_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}
```

Same pattern at `overlay/upload/feature_assist.py:208` (`FEATURE_CONTEXT_FLAG`) and a frozenset variant at `overlay/upload/planner/multisource_shadow.py:98` (`_TRUTHY`).

Env/deploy wiring precedent: `integration-aug01/.env.example:47,55,59` (commented-out, i.e. off) and `integration-aug01/deploy/kind/k8s/20-backend.yaml:59,69` (`FEATUREGEN_FEATURE_CONTEXT: "0"`, `FEATUREGEN_DATASET_PROFILES: "0"`).

**Phase-G obligations from D8:** a new Phase-G flag (e.g. `FEATUREGEN_EXECUTION_WIRING`) must (a) be a module-level `*_FLAG` constant + a `*_enabled()` helper reading the exact 4-member truthy set, (b) default OFF meaning new routes 404 and every existing payload byte-identical, (c) be added to BOTH `.env.example` and `deploy/kind/k8s/20-backend.yaml` with `"0"`, and (d) if it has a dependency on a Release-B flag, that dependency must be **enforced fail-closed at startup**, not by convention (the `FEATUREGEN_CROSSWALK_EXECUTION` rule).

Note: `.env.example` and `deploy/kind/k8s/20-backend.yaml` are Track-1-modified files. See §3 — they are a soft collision.

### D9 — Gates between the plans (doc lines 209–223), verbatim rules

1. Profile Tasks 1–3 may run in parallel with semantic Tasks 1–2 **BECAUSE this document, not task completion, freezes their shared contracts.**
2. **The shared-file rule** (this is the mechanism Phase G must adopt): `concepts.py`, `party_vocab.py`, `bridge_candidates.py`, `entity_map.py` belong to semantic Tasks 1–2; `field_policies.py`, `field_resolution.py`, `field_correction.py` belong to profile Task 1 — **no cross-edits.**
3. `table_synth.py`/`enrich_llm.py` are **single-owner** during the joint step (semantic 3–4 + profile 4): one implementation stream, both plans' checkboxes.
4. Semantic Task 10's live-LLM comparison is EXPLICITLY part of Gate B (separate approval), not a pre-gate deliverable. The pre-gate deliverable is the replay/fixture half only.
5. **Test-count gates are scoped to NAMED focused suites (the Task-0 lists), never the whole repo** (DEFERRED-WORK §C contamination). The mutation harness is a NEW deliverable of the joint eval step (Sem 10 + Prof 6) — **it does not exist on main; nobody may cite it as existing.**

**Phase-G obligations from D9:** Phase G's plan must (a) declare its own single-owner file list and assert no cross-edits into Track 1's owned files, (b) scope every test-count gate to a NAMED focused suite (e.g. `tests/materialize/…`), never the whole repo, and (c) not cite the mutation harness as existing.

### D10 — Egress and schema-registry rules (doc lines 225–239), verbatim

- `schema_for(id, version)` returning None for a REQUESTED version is a **raised error at dispatch**, never a silent unenforced call (fixes the trap at `enrich_llm.py:769-773`).
- Unknown TOP-LEVEL metadata keys in the single-call path **fail closed** (today they egress unscanned — `enrich_llm.py:168` intersection bug).
- Every new context key ships with an explicit classification in the relevant allowlist (`_FEATURE_COLUMN_*`, `_ITEM_META_ALLOWED`, `_COLUMN_PROFILE_KEYS`, `_ROSTER_ENTRY_KEYS`) plus a **golden egress test**. Unclassified = blocked stays the law; the change is that **blocked is LOUD**.
- The fact wrapper accepts the D2 typed triple; `llm_proposed` display never appears on the wire — the wire carries `(producer, strength)`.
- Pass-B extension (profile Task 4) requires a REAL schema v3 body + prompt version bump (`overlay_table_synth_batch` v2 is a byte-alias of v1 with `additionalProperties: false`).
- Feature-context v4 requires registration in `_SCHEMAS` before `_feature_schema_version()` may return 4.

**Phase-G read:** D10 binds Phase G only if Phase G puts anything on an LLM wire. Execution wiring almost certainly does not — so the practical Phase-G obligation is a **negative assertion in the plan**: "Phase G adds no LLM egress key; the D10 allowlists are untouched." If any Phase-G surface does egress (e.g. a run-failure explanation), the allowlist + golden egress test is mandatory.

### Also load-bearing for Phase G (not asked, but binding)

- **D6 (lines 156–167)** — this is the actual home of the decision pins. See §2.
- **D12.8 (line 298)**: *"Release C predecessor: the codegen-remediation plan + Phase-G wiring plan are named hard predecessors of Release C Task 12 and of semantic Task 8's generated-project acceptance."* Phase G is on Release C's critical path.
- **Program sequence, doc lines 12–22**: `Sync: Release B + remediated codegen -> Phase-G wiring plan (design + approval) -> Release C Tasks 10-13`. Note the doc's own sequence puts Phase G AFTER Release B — the charter's "must run WITHOUT Release B" is a *deliberate relaxation* of the doc, and the seam parameter is what buys it. Phase G's plan should say so explicitly, because it is an amendment to a CONTROLLING document (doc line 3–5: "Where this document and a plan disagree, this document wins; the plan is amended, not reinterpreted"). **Phase G cannot unilaterally reorder the program — it must either land inside the doc's sequence or get D7/D12 amended in the same commit.**
- **D11 (lines 241–252)** — read scope: derived table visibility, `visible_requires <@ allowed` at every boundary. If Phase G adds a read route it inherits this.

---

## 2. RELEASE-B DECISION PINS — what they actually are, and the seam

### What exists TODAY in Track 1

The pins are **snapshot ITEM KINDS**, defined at
`integration-aug01/src/featuregen/overlay/upload/feature_metadata_snapshot.py:72-83`:

```python
# ── Task 0.6 Seam 4 (D6): the six snapshot pin KINDS. ``column_field`` is the only kind with a
# builder/comparator today; the other five are RESERVED vocabulary whose builders arrive with the
# profile/serving/temporal deliveries. No DB change: ``item_kind`` is an existing text column.
ITEM_KIND_COLUMN_FIELD = "column_field"
ITEM_KINDS: frozenset[str] = frozenset({
    ITEM_KIND_COLUMN_FIELD, "dataset_profile", "serving_policy", "source_selection",
    "physical_binding", "temporal_policy", "row_selection",
})

SNAPSHOT_KIND_UNSUPPORTED = "SNAPSHOT_KIND_UNSUPPORTED"
```

**This is the single most important finding for the seam:** Track 1 has ALREADY shipped the reserved vocabulary and the forward-compat mechanism. Phase G does not need to invent it, and must not.

The hash rule (`:86-97`):

```python
def snapshot_item_hash(item_kind: str, material: Mapping[str, object]) -> str:
    if item_kind not in ITEM_KINDS:
        raise ValueError(f"unknown snapshot item kind {item_kind!r}; register it in ITEM_KINDS")
    if item_kind == ITEM_KIND_COLUMN_FIELD:
        return canonical_hash(dict(material))          # legacy: item_kind EXCLUDED
    if "item_kind" in material:
        raise ValueError("item material must not carry its own 'item_kind' key")
    return canonical_hash({**material, "item_kind": item_kind})
```

The forward-compat comparator dispatch (`:486-489`):

```python
# item_kind -> the CURRENT-state item rebuilder ``compare_snapshot_to_current`` dispatches on (D6).
# Only ``column_field`` has one today; the five reserved kinds gain theirs with their builders — a
# stored kind absent here is the typed :data:`SNAPSHOT_KIND_UNSUPPORTED` refusal.
_KIND_COMPARATORS = {ITEM_KIND_COLUMN_FIELD: _build_item}
```

### The SHAPE of a decision pin

A pin is **a `SnapshotItem` row**, not a policy object. `feature_metadata_snapshot.py:254-281`:

```python
@dataclass(frozen=True, slots=True)
class SnapshotItem:
    catalog_source: str
    graph_ref: str
    logical_ref: str | None
    physical_ref: str | None
    item_kind: str            # <- one of the six ITEM_KINDS
    field_or_fact_type: str
    value: str | None
    authority: str
    provenance: str | None
    status: str               # sealed C1 operational status
    decision_event_id: str | None
    fact_event_id: str | None
    item_hash: str
```

Scalars only, frozen, hashable. Persisted to `catalog_metadata_snapshot_item` (`:554-563`) — `item_kind` is an existing text column, **so the pins need NO migration**. Held in memory inside `SnapshotContext` (`:284-303`), which exposes `items() -> tuple[SnapshotItem, ...]` and `facts(catalog_source, object_ref, field)`.

So: **a decision pin is `(item_kind, item_hash, decision_event_id)` carried on a `SnapshotItem` inside a `SnapshotContext`.** Not a dataclass per policy, not a free-standing hash.

### The policy STORES do not exist yet

Swept `integration-aug01/src/featuregen/` for `ServingPolicy|serving_policy|TemporalPolicy|temporal_policy|SourceSelection|RowSelection`. Hits in exactly two files: `overlay/upload/feature_metadata_snapshot.py` (the vocabulary above) and `overlay/upload/templates.py:2364` (a refusal message: *"Future-horizon inclusion remains blocked until TemporalPolicyV1 is compiled."*).

`DatasetSourceSelectionV1` / `DatasetRowSelectionV1` / `ServingPolicyV1` / `TemporalPolicyV1`: **zero definitions anywhere.** They arrive with Release B Task 7 (migrations 1048/1049) and Release C Task 10 (1050) — none of which exist on disk.

### The no-op / absent form TODAY

Because `_KIND_COMPARATORS` has one entry and no builder ever emits the other five kinds:

> **The absent form is a `SnapshotContext` whose `items()` contains zero rows with `item_kind` in `{"serving_policy", "source_selection", "temporal_policy", "row_selection", "dataset_profile", "physical_binding"}` — i.e. an empty tuple after filtering. That is what every snapshot produces today, without exception.**

Phase G therefore gets its no-op for free: filter, get empty, proceed. There is no flag to check and no version to negotiate. When Release B lands and starts emitting those kinds, the same filter starts returning rows — **the seam widens without a Phase-G code change.**

### What the seam parameter should be

The exact precedent already lives in the module Phase G owns —
`phase-g/src/featuregen/materialize/ir.py:206-218`:

```python
class BridgeExecutionAuthorization:
    """Final control-plane check for the exact bridge revisions a run will execute.

    Compilation and rendering may take long enough for a bridge, its exact observation or one of
    its physical bindings to become stale.  This token is therefore minted separately, immediately
    before run preparation.  It is bound to the compiled IR hashes, environment and exact
    ``(realization_revision_id, dependency_snapshot_id)`` pairs.  Human review is deliberately not
    represented: deterministic execution safety, rather than endorsement, is the gate.
    """

    ir_hashes: tuple[str, ...]
    environment_id: str
    realization_dependencies: tuple[tuple[str, str], ...]
```

…threaded into `prepare_run` as an optional keyword with an absent default,
`phase-g/src/featuregen/materialize/runprep.py:831-881`:

```python
def prepare_run(
    rendered: RenderedArtifactIdentity,
    inventory: ClusterInventoryV1,
    metastore: MetastorePartitions,
    *,
    generation_id: str,
    run_id: str,
    business_dt: str,
    requests: tuple[RunInputRequest, ...],
    staging_base: str,
    capability_attestation_id: str,
    bridge_authorization: BridgeExecutionAuthorization | None = None,   # <-- THE PRECEDENT
    additional_parameters: Mapping[str, Any] | None = None,
    required_parameters: Sequence[str] = REQUIRED_RUN_PARAMETERS,
) -> RunPreparation | MaterializationRefused:
```

and its conditional-requirement logic (`:859-880`): if the artifact declares `bridge_realization_dependencies` and the authorization is `None` → typed `MaterializationRefused`; if it declares none and one is supplied non-empty → `ValueError`. **Absent-and-not-needed is silently fine; absent-and-needed is a typed refusal; present-and-not-needed is a caller bug.**

**Recommended seam parameter (one sentence, as requested in the summary):**

> `decision_pins: DecisionPinSet | None = None` — a frozen dataclass wrapping `tuple[SnapshotItem, ...]` filtered to the five non-`column_field` D6 kinds, defaulting to `None` (equivalently, today's always-empty filter of `SnapshotContext.items()`), threaded as a keyword-only argument into `prepare_run` exactly like `bridge_authorization`, refusing typed only when the artifact declares it *requires* a pin kind that is absent.

Design notes to carry into the plan:

- **Do NOT import `feature_metadata_snapshot` into `materialize/`.** That would make Phase G depend on a Track-1-owned file and reverse the layering (materialize is below overlay today — `grep -rn "materialize" src/featuregen/api/` and the gate1 handoff both return nothing). Define `DecisionPinSet` structurally in `materialize/` (a tuple of `(item_kind, item_hash, decision_event_id, graph_ref)` 4-tuples or a small local frozen dataclass) and let the CALLER — the new Phase-G wiring layer above both — do the projection from `SnapshotContext`. That keeps the collision surface at zero.
- **Naming hazard, flagged loudly:** `materialize/runprep.py` already has `snapshot_id` and `PhysicalInputSnapshot` (`:206-273`) — these are *partition/data* snapshots (which Hive partitions a run reads), a completely different concept from the *catalog metadata* snapshot in `feature_metadata_snapshot.py`. `prepare_run` builds `"input_snapshots": [snapshot.parameter_payload() …]` at `:896`. Phase G MUST NOT call its new parameter `snapshot` / `snapshot_items` / `pins` ambiguously. `decision_pins` is unambiguous; keep it.
- **Where it is consumed.** Two candidate stages, and the answer is *both, for different pins*:
  - `runprep.prepare_run` (`runprep.py:831`) — for `source_selection`, `physical_binding`, `row_selection`, `temporal_policy`. These decide *which physical data a run reads*, which is precisely what `resolve_snapshots` (`:531`) and `_resolve_one` (`:471`) do today from `inventory` + `metastore`. This is the primary seam.
  - `contract.derive_contract` (`contract.py:569-576`) — for `serving_policy`. It already takes `overrides: ContractOverrides | None = None` (`contract.py:438-474`), a monotonic-tightening declaration with a `None` default and `__post_init__` validation. A serving-policy pin is the same species: a declared tightening of what the catalog said. **Strong recommendation: do not add a second parameter to `derive_contract`; extend `ContractOverrides` (additively, new field defaulting to `None`) or add `serving_policy_pin` beside `overrides`.** Note `derive_contract`'s existing discipline — a loosening override is a `ValueError` (caller incoherence), a tightening to the most restrictive rank is a typed `PROHIBITED_INPUT` refusal. Phase G's pin handling must match that split.
- **Hashing.** If a pin enters `contract_hash` (`contract.py:555`) or the execution hash, it changes artifact identity. Decide deliberately and state it: pins that select *data* belong in the run/execution identity (they change what was read); pins that select *policy* belong in the contract hash (they change what the artifact promises). Either way, D1 forces `materialize_hash`.
- **Consumers of the snapshot today**, for reference on how the caller side is built: `overlay/upload/contract/gate1.py:43,559` (`build_metadata_snapshot`) and `overlay/upload/recipe_formula_worker.py:45,244` (`compare_snapshot_to_current`). Both are Track-1-adjacent; `gate1.py` is NOT in Track 1's change set, `feature_metadata_snapshot.py` IS.

---

## 3. COLLISION SURFACE (git, not guesswork)

### The charter's three named shared-risk files

Compared at merge-base `1588c890` and branch-to-branch.

| file | Phase-G changed vs merge-base | Track-1 changed vs merge-base | Track-1 changed vs its own fork `fa9a20b0` | verdict |
| --- | --- | --- | --- | --- |
| `src/featuregen/materialize/runprep.py` | **yes**, +124/-19 | **no** | **no** | **NO COLLISION** — Phase-G-only |
| `src/featuregen/materialize/identity.py` | **no** | **no** | **no** | **NO COLLISION** — identical on both sides |
| `src/featuregen/api/app.py` | **no** | **yes**, +7 | **yes**, +7 | **SOFT COLLISION** — see below |

Per-file `git log --oneline -3`:

- `materialize/runprep.py` — Phase-G tip commits `e9e46823`, `de8a09bb`, `0383aa25` (all codegen-remediation work); Track-1 tip commits `c2378f7d`, `5898987d`, `2108e205` (all pre-fork, shared history). Track 1 has not touched it since the fork.
- `materialize/identity.py` — **identical top-3 on both branches** (`c2378f7d`, `46a8b292`, `840d475d`). Byte-identical trees; `git diff` between branches is empty.
- `api/app.py` — Phase-G tip `9c61ef03` (shared history); Track-1 tip `92e65dba feat(profiles): Task 3 …`.

The entire `api/app.py` divergence:

```diff
@@ -35,6 +35,7 @@ from featuregen.api.routes import (
     integrations,
     learning,
     lineage,
+    profiles,
     quarantine,
@@ -146,6 +147,10 @@ def create_app(llm_client: LLMClient | None = None) -> FastAPI:
     app.include_router(assets.router)
+    # Release-A table-asset profiles (flag-gated 404 while FEATUREGEN_DATASET_PROFILES is off).
+    # Distinct `/catalog/asset-profiles` prefix — the assets greedy `{object_ref:path}` route
+    # would swallow a nested literal (see profiles.py module docstring).
+    app.include_router(profiles.router)
     app.include_router(quarantine.router)
```

Two hunks: one alphabetical import insert, one `include_router` insert. **Textually mergeable; the only real risk is Phase G inserting at the same alphabetical position.** Mitigation: Phase G's route module should sort clear of `profiles` (e.g. `runs`, `execution`), and Phase G should insert its `include_router` at the END of the block, not adjacent to `profiles.router`. Also note the routing hazard the comment records: a greedy `{object_ref:path}` route on `assets` swallows nested literals — Phase G's prefix must be distinct at the first segment.

### Track 1's full change set on its branch — the REAL collision map

`git diff --name-only fa9a20b0 feature/integration-2026-08-01-semantic-profiles -- src/featuregen/` → **55 files**:

```
src/featuregen/__main__.py
src/featuregen/analysis/intent.py
src/featuregen/analysis/retrieval.py
src/featuregen/api/app.py
src/featuregen/api/routes/analysis.py
src/featuregen/api/routes/catalogs.py
src/featuregen/api/routes/data_sources.py
src/featuregen/api/routes/profiles.py                     (NEW)
src/featuregen/api/routes/search.py
src/featuregen/api/routes/uploads.py
src/featuregen/data_agent/learning.py
src/featuregen/db/migrations/1045_catalog_semantic_scope.sql          (NEW)
src/featuregen/db/migrations/1046_structured_result_current.sql       (NEW)
src/featuregen/db/migrations/1047_catalog_profile_revision.sql        (NEW)
src/featuregen/db/migrations/1051_graph_node_classification_axes.sql  (NEW)
src/featuregen/db/migrations/1052_graph_node_data_role_and_table_prose.sql (NEW)
src/featuregen/overlay/upload/asset_detail.py
src/featuregen/overlay/upload/attest/bridge_grounding.py
src/featuregen/overlay/upload/attest/concept_critic.py
src/featuregen/overlay/upload/attest/dataset_profile_critic.py
src/featuregen/overlay/upload/attest/representation.py
src/featuregen/overlay/upload/axis_projection.py
src/featuregen/overlay/upload/backfill_projections.py
src/featuregen/overlay/upload/bridge_candidates.py
src/featuregen/overlay/upload/catalog_profiles.py
src/featuregen/overlay/upload/column_authority.py
src/featuregen/overlay/upload/concepts.py
src/featuregen/overlay/upload/context_graph.py
src/featuregen/overlay/upload/dataset_profiles.py
src/featuregen/overlay/upload/enrich_batch.py
src/featuregen/overlay/upload/enrich_config.py
src/featuregen/overlay/upload/enrich_llm.py
src/featuregen/overlay/upload/enrich.py
src/featuregen/overlay/upload/entity_map.py
src/featuregen/overlay/upload/feature_assist.py
src/featuregen/overlay/upload/feature_metadata_snapshot.py   <-- the D6 pin seam
src/featuregen/overlay/upload/field_correction.py
src/featuregen/overlay/upload/field_policies.py
src/featuregen/overlay/upload/field_resolution.py
src/featuregen/overlay/upload/graph.py
src/featuregen/overlay/upload/identifier_scope.py
src/featuregen/overlay/upload/ingest.py
src/featuregen/overlay/upload/lineage.py
src/featuregen/overlay/upload/profile_store.py
src/featuregen/overlay/upload/profile_vocab.py
src/featuregen/overlay/upload/read_scope.py
src/featuregen/overlay/upload/search.py
src/featuregen/overlay/upload/semantic_adjudication.py
src/featuregen/overlay/upload/semantic_bindings/enrich.py
src/featuregen/overlay/upload/semantic_context.py
src/featuregen/overlay/upload/semantic_gap.py
src/featuregen/overlay/upload/stage_report.py
src/featuregen/overlay/upload/structured_results.py
src/featuregen/overlay/upload/table_synth.py
src/featuregen/overlay/upload/table_vocab.py
```

**`git diff --name-only fa9a20b0 <track1> -- src/featuregen/materialize/` returns ZERO files. Track 1 touches nothing under `materialize/` and no test path matching `materializ`.**

Distribution: 40 of 55 under `overlay/upload/`, 7 under `api/`, 5 new migrations, 2 under `analysis/`, 1 `data_agent/`, 1 `__main__.py`.

### Intersection (both branches touched the same file, vs their common merge-base `1588c890`)

```
src/featuregen/api/routes/uploads.py
src/featuregen/data_agent/executor.py
src/featuregen/overlay/upload/graph.py
src/featuregen/overlay/upload/ingest.py
```

Four files. **None of them are Phase-G execution-wiring files** — they are codegen-remediation-era edits (e.g. `3b0b7b01 fix(overlay): sparse-emit dropped-correction surfacing`) that are already merged into `main`. Once Phase G rebases onto `main`, this intersection collapses to Track 1's problem, not Phase G's: Track 1 will resolve these four when it rebases/merges.

### Collision verdict

| surface | status |
| --- | --- |
| `src/featuregen/materialize/**` (all 22 modules + `compile/`, `render/`) | **ZERO collision.** Track 1 has not touched a single file. This is Phase G's exclusive territory. |
| Migrations 1053–1055 | **ZERO collision.** Free on every ref. |
| `src/featuregen/api/app.py` | **SOFT** — 2-hunk insert, textually mergeable, avoid the `profiles` neighbourhood |
| `.env.example`, `deploy/kind/k8s/20-backend.yaml` | **SOFT** — Track 1 adds flag blocks; Phase G appends its own. Append at the end. |
| `src/featuregen/api/routes/*.py` (new file) | **ZERO** if Phase G creates a NEW module; Track 1's 6 touched route files are all pre-existing |
| `overlay/upload/feature_metadata_snapshot.py` | **HARD — DO NOT EDIT.** Track 1 owns it (D6 Seam 4). Phase G consumes it read-only from a layer above, never imports it into `materialize/`. |
| `docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md` | **HARD — Track 1 owns it.** Phase G must not edit D7 itself; if Phase G needs 1056+, it must coordinate (D7's own rule: "edit this doc in the same commit as the migration"). |
| The 4-file intersection above | resolved by rebasing Phase G onto `main` |

---

## 4. TRACK-1 STATE — does the integration tree lack Track 2?

**Yes, confirmed empirically.**

| probe | `feature/integration-…` @ 75038690 | `feature/phase-g` @ 3b0b7b01 | `main` @ f3424c36 |
| --- | --- | --- | --- |
| `COLUMN_NOT_GOVERNED` in `src/` | **0 hits** | **present** (`materialize/codes.py:57`, `materialize/ir.py:34,534,575,617`) | **present** |
| `observed_on` in `src/` | **0 hits (whole tree)** | present | present (`materialize/runprep.py` ×2, `materialize/spine.py` ×7) |
| `db/migrations/1044_run_event_ordering.sql` | **absent** | **present** | **present** |

Track 1's HEAD `75038690` is `docs: reserve migrations 1053-1055 for the Phase-G parallel session` — the reservation grant. Working tree is **clean**. Its `materialize/` directory has the same 22 module names as Phase G's but at the pre-remediation content (and lacks Phase G's `__pycache__`-excluded deltas); `git diff fa9a20b0 <track1> -- src/featuregen/materialize/` = empty, so Track 1's `materialize/` is exactly `fa9a20b0`'s.

**But the charter's framing needs the §0 correction:** the remediation is not "an unmerged parallel track." It is **merged to `main`** at `f3424c36`. Track 1 simply has not rebased since forking at `fa9a20b0`, and is 78 commits behind `main`. Track 1 will pick up all of Track 2 the moment it rebases — and since Track 1 touches zero `materialize/` files, that rebase is conflict-free on the Phase-G-relevant surface.

---

## 5. ACTIONABLE CONSTRAINTS FOR THE PHASE-G PLAN

1. **Rebase onto `main` (f3424c36) before writing code.** `feature/phase-g` is a strict ancestor of `main`; staying on it guarantees rework and hides the 13 commits (notably `3b74ac33 feat(types): attested data types from engine observation` and `fa9a20b0`, the bridge decision-event ordering fix) that a run-lifecycle feature will interact with.
2. **Claim 1053–1055 only.** Do not take 1048/1049/1050 despite the files being absent. Return unused numbers to the pool per D7. If Phase G needs a fourth, append 1056+ to D7's table in the same commit — which requires touching a Track-1-owned doc, so coordinate.
3. **Seam parameter:** `decision_pins: DecisionPinSet | None = None`, keyword-only, on `runprep.prepare_run`; and for serving policy, extend `contract.ContractOverrides` additively rather than adding a parameter to `derive_contract`. Model both on `bridge_authorization` (`runprep.py:842`, refusal logic `:859-880`).
4. **Structural coupling only.** `materialize/` must not import `overlay/upload/feature_metadata_snapshot`. A new wiring layer above both does the `SnapshotContext.items()` → `DecisionPinSet` projection.
5. **No-op is free and needs no flag** — the five non-`column_field` kinds have no builder today, so the filter is empty by construction. State that as the "runs without Release B" proof, and pin it with a test asserting `_KIND_COMPARATORS` has exactly one entry today (a canary that fires when Release B lands).
6. **Flag:** one new `FEATUREGEN_*` env flag, module constant + `*_enabled()` helper, truthy set `{"1","true","yes","on"}`, default OFF ⇒ new routes 404 and existing payloads byte-identical, added to `.env.example` AND `deploy/kind/k8s/20-backend.yaml` as `"0"`. If it depends on a Release-B flag, enforce fail-closed at startup.
7. **Hashes:** import `materialize_hash` from `materialize/canonical.py:33`. No new inline hash. No wall-clock / job state / environment / projection timestamps inside a content hash.
8. **Gates (D9):** declare Phase G's single-owner file list; assert no edits to Track 1's owned files; scope every test-count gate to a NAMED suite, never the repo; do not cite the mutation harness as existing.
9. **Sequence amendment (D12.8 + program sequence lines 12–22):** the controlling doc places the Phase-G wiring plan AFTER Release B, and makes it a hard predecessor of Release C Task 12. Running Phase G *before* Release B is an amendment to a CONTROLLING document. The plan must say so explicitly and route it through the same approval that owns D7/D12, not assume it.
10. **Naming:** avoid `snapshot`/`snapshot_id` for pins — `materialize/runprep.py` already uses those for physical partition snapshots (`PhysicalInputSnapshot`, `:206`).
11. **API surface:** `grep -rn "materialize" src/featuregen/api/` returns **nothing** — materialize is entirely unreachable from the API today, and `gate1.py` has no materialize handoff. That gap IS Phase G's scope, and it is uncontested by Track 1.
