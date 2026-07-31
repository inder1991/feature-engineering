# Bridge Cardinality and Link Trust — Verified Baseline

Verified on 2026-07-29 against the local Kind context `kind-featuregen`.

## Selected implementation baseline

| Item | Recorded value |
| --- | --- |
| Branch | `feature/bridge-cardinality-link-trust-remediation` |
| Selected commit | `265ce4be18b23c6e9b9da2810066529699f4e07a` |
| Original plan baseline | `1fb8f81fa5283b3a9058cfb341d5d95bfe7158ae` |
| Rebase decision | Deliberately based on current `origin/main`; the original baseline is an ancestor |
| Migration head in code and Kind | `1035_column_summary` |
| Deployed image reference | `featuregen-backend:local` |
| Deployed pod image ID | `sha256:56c702a473c7a292f7fe37eb0889211e7665909bb1b0898370d58733ff33e617` |
| Current local tag image ID | `sha256:018da9b35aaf70be940beff951d8c4bbf1e971423acb76ef464a81127426fa20` |
| Image provenance verdict | **Unverifiable** |

The deliberate rebase retains the data-agent, materialization, bridge-lifecycle, grounding and
contract-invalidation work already merged after `1fb8f81f`. The unrelated dirty files in the
original checkout are outside this worktree.

The running pod exposes neither a source-commit label nor an annotation, and its immutable image ID
does not match the image currently named by the mutable local tag. Therefore the running image
cannot be proved to contain either the original baseline or the selected commit. Task 11 must build
and deploy a replacement image from a clean, recorded commit before implementation acceptance.

## Deployed feature flags

The `featuregen-config` ConfigMap enabled all of the following at verification time:

| Flag | Value |
| --- | --- |
| `OVERLAY_PASS_C` | `1` |
| `OVERLAY_ENTITY_BRIDGES` | `1` |
| `OVERLAY_SEMANTIC_BINDING_CANDIDATES` | `1` |
| `OVERLAY_SEMANTIC_BINDING_PROPOSALS` | `1` |
| `OVERLAY_TABLE_SYNTH` | `1` |

## Read-only Kind result

Run from the repository root:

```bash
kubectl exec -i -n featuregen <postgres-pod> -- \
  psql -U postgres -d featuregen -f - < scripts/verify_bridge_cardinality.sql
```

The transaction is explicitly read-only and rolls back. It reads catalog/governance metadata only,
not bank business rows.

| Named metric | Observed value |
| --- | ---: |
| `pass_c_candidate_rows` | 0 |
| `graph_join_edges` | 0 |
| `cross_catalog_bridge_candidates` | 9 |
| `bridge_candidates_with_cardinality` | 0 |
| `projected_entity_bridge_edges` | 0 |
| `bridge_lifecycle.DRAFT` | 9 |
| `open_bridge_human_tasks` | 9 |
| `catalog_tables.cib` | 1 |
| `catalog_tables.ftr` | 1 |

This confirms the deployed symptom: nine symmetric link proposals exist, none carries physical
cardinality, and no reviewed bridge projection exists. Code that turns those links into
`many_to_one` is inventing a directional execution property.

## Shared structured-result decision

No general content-addressed structured-result store exists on this baseline. `llm_call` is an
immutable call audit, while `enrichment_*` caches and semantic-binding candidate sets are
task-specific stores. None is the roadmap-owned typed result API required by Task 8. The programme
therefore reserves a minimal shared store rather than creating a bridge-only replay table.

## Migration allocation

The duplicate `1034_analysis_learning_event.sql` and
`1034_materialization_control_plane.sql` prefixes are grandfathered. Every migration in this
programme is uniquely allocated above the current maximum. Re-scan the migration directory before
creating each file; a newly occupied prefix requires updating this table before proceeding.

| Prefix | Exact filename | Owner |
| ---: | --- | --- |
| 1036 | `1036_physical_dataset_binding_revision.sql` | Task 2 physical binding identity/revision |
| 1037 | `1037_bridge_candidate_realization_store.sql` | Task 6 candidate and realization revisions/current pointers |
| 1038 | `1038_relationship_observation_v2.sql` | Task 7 tuple-aware scoped observations |
| 1039 | `1039_structured_result_store.sql` | Task 8 shared content-addressed structured results |
| 1040 | `1040_contract_realization_dependency_v2.sql` | Task 9 realization-revision dependency re-baseline |

Migration `1040` is required because `contract_metadata_dependency` is write-once. The programme
must not rewrite existing `item_hash` values in place when contract reuse moves from a symmetric
bridge lifecycle signature to a directional realization revision and dependency snapshot.

## Baseline gate

The focused baseline suite passed before Task 0B changes:

```text
205 passed in 5.79s
```

It covered bridge derivation/projection/demotion/availability, planner declarations,
materialization joins, data-agent relationship evidence, analysis grounding and contract drift
invalidation.

Task 0B may proceed against the selected code commit. Cluster acceptance may not: it remains gated
on a replacement image whose immutable digest is linked to the exact source commit and whose
migration head is re-recorded after deployment.

---

## Ingestion-richness plan — Task 0 record (2026-07-31)

- **Implementation baseline:** `origin/main bfcafafa` (bridge merge `387165a6`/`fc6127da` at
  `b5088f5d` + subsequent main work + the three program plan docs).
- **Migrations:** ALL SEVEN `1036`–`1039` names applied on the kind cluster (both lines' sets;
  filename-keyed ledger; interleave verified empirically by the deployed run — no conflicts).
  Merged tree maximum: `1039_structured_result_store`.
- **Allocation:** ingestion-richness plan reserves **1040–1044** (known need: `graph_node.party_role`;
  spare for Task 7 type-observation storage if required). Catalog-profiles plan allocates **1045+**.
  Re-scan this ledger before creating any file.
- **Store pairing (never unify):** `data_source_binding` (+connection) = CONFIG — reviewed access
  grant + address; `physical_dataset_binding_revision` = immutable content-addressed REVISION cited
  by observations/realizations. Config feeds revision-minting (richness Task 7).
- **Audit script:** `scripts/verify_catalog_richness.sql` (read-only; metric|value rows). First
  run captured post-redeploy drift — recorded in the plan's Task 0 addendum: summaries 237/237
  (file-side payload, quality defect stands), restricted 37, concept cib 110/111, branch_id
  conflation 4 cols (sol_desc still misclassified), 4 stamp-drift rows, candidates 17 / tasks 11,
  bindings 126/0/0.

### Allocation update (2026-07-31, post-Task-3)
The 1040/1041 numeric prefixes were taken by parallel main work DESPITE the reservation
(`1040_bridge_candidate_currentness.sql`, `1041_catalog_engine.sql`) — filename-keyed ledger
coexistence holds, as with the 1036–1038 pairs. Richness plan actually used:
`1040_graph_node_party_role.sql`, `1042_graph_node_sensitivity_display.sql`, and (Task 4)
`1043_semantic_binding_fixed_currency.sql`. Remaining richness reservation: 1044. NOTE for every
session: the numeric-prefix reservation is advisory only;
uniqueness lives in the full FILENAME — re-scan `src/featuregen/db/migrations/` immediately
before creating any file, and never reuse an existing full basename.
