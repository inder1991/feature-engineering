# Verified Interfaces — Semantic Context + Catalog Profiles Release Train (2026-08-01)

**Status: CONTROLLING.** This document freezes the shared interfaces for the three-plan program
(semantic enrichment, catalog profiles, codegen remediation). Where this document and a plan
disagree, this document wins; the plan is amended, not reinterpreted. Every decision below was
verified against `origin/main @ fa9a20b0` — citations refer to that tree. The adversarial review
that motivated each decision is `docs/architecture/2026-08-01-plan-review-semantic-context-and-catalog-profiles.md`.

**Merge note (2026-08-05):** a parallel session independently authored a shorter "binding addendum"
at this same path (visible in main's history at `a3f0123c`). This document supersedes it at this
path: its migration-reservation table (§5 there) predated the executed allocations and disagrees
with reality (1050 = crosswalk, 1051/1052 taken — the D7 table below is the executed record), and
its unique §12 Task-0F companion freeze lives standalone as
`docs/architecture/2026-08-03-verified-interfaces-suggestion-discovery.md`.

**Program sequence (supersedes the execution-order diagrams inside both 2026-08-01 plans, which
show codegen as a later sequential step — codegen remediation is an active PARALLEL predecessor):**

```text
Track 1:  Task 0 (baseline) -> Task 0.5 (this doc) -> Task 0.6 (seam repairs)
          -> Release A:  Sem 1-2 ∥ Prof 1-3  ->  Sem 3-4 + Prof 4  ->  Sem 5-6
                         ->  Sem 7-9 + Prof 5  ->  Sem 10 + Prof 6
          -> deploy gate (approval) -> live-LLM/re-upload gate (separate approval)
          -> Release B:  Prof 7-9 incl. wiring the API to existing run_analysis
          -> fixture validation -> bounded Hive/ODS profiling (approval)
Track 2:  codegen remediation Tasks 1-26 (parallel, own worktree)
Sync:     Release B + remediated codegen -> Phase-G wiring plan (design + approval)
          -> Release C Tasks 10-13 (crosswalk execution)
```

Generated-project acceptance (semantic Task 8's end-to-end item) waits for Track 2. Everything
else in Releases A/B does not.

---

## D1. Canonical hash scheme

All NEW content hashes in this program — `SemanticContextBundleV1.content_hash`,
`dataset_profile_hash`, `CatalogProfileRevisionV1.content_hash`, policy revision hashes,
`DatasetSourceSelectionV1`/`DatasetRowSelectionV1.content_hash`, analysis-plan identity v2 —
use **RFC 8785 JCS via `materialize/canonical.py:33 materialize_hash`** (the scheme behind the
CHECK-pinned `pbr_` binding revision ids, `physical.py:238-244`).

- The `field_evidence.py:38-46` `json.dumps(sort_keys=True)` scheme remains for the stores that
  already use it; no stored hash is rewritten.
- No new inline hash implementation may be written; import `materialize_hash`.
- Excluded from every content hash: wall-clock, job state, environment, physical bindings (which
  live in `DatasetSourceSelectionV1`), projection timestamps.
- The two bundle builders must byte-match on shared fields; the property test serializes both
  through the same canonicalizer.

## D2. Evidence authority — typed triple, not a flat vocabulary

`SemanticValueV1` carries the REAL authority model verbatim:

```python
producer: str      # EvidenceProducer value (overlay/evidence.py:15-30) — 8 members
strength: str      # AssertionStrength value (:33-41)
lifecycle: str     # EvidenceLifecycle value (:44-48)
```

The plans' flat 5-value `authority` becomes a DERIVED display label with this fixed projection
(display only, never persisted, never hashed as the authority):

| producer × strength | display label |
| --- | --- |
| source × attested/confirmed | `source_attested` |
| source × proposed/supported | `source_proposed` |
| human × confirmed | `human` |
| llm × any | `llm_proposed` |
| profiler/structural_connector × attested | `deterministic` |
| parser/taxonomy/legacy × any | `system` |
| (from specialized governed facts / `OperationalColumnFacts.authority="governed"`) | `governed` |

`CatalogProfileRevisionV1.authority` and `PolicyProvenanceV1.authority` reference the same triple
(profile plan §6.2/§6.4 amended accordingly). No consumer branches on the display label.

## D3. Relationship context — mirror the real shape

`RelationshipContextV1` is REPLACED by a two-level shape matching `entity_map.py:96-122` and the
two real readers (`available_identifier_links`, `bridge_assessment.py:648`;
`load_current_bridge_realizations`, `bridge_store.py:633`):

```python
@dataclass(frozen=True, slots=True)
class DirectionalRealizationContextV1:
    realization_revision_id: str
    from_ref: str
    to_ref: str
    lifecycle: str
    safety_status: str
    cardinality: str | None
    scope_id: str | None            # RealizationApplicabilityScopeV1.scope_id — no invented hash
    sandbox_eligible: bool
    production_eligible: bool       # pure predicate; see display rule below

@dataclass(frozen=True, slots=True)
class RelationshipContextV1:
    relationship_ref: str           # bridge fact_key today; crosswalk definition_id in Release C
    kind: str                       # RelationshipKind — owned per D5
    left_ref: str
    right_ref: str
    availability: str               # LinkAvailability: available | unavailable — nothing else
    review_status: str | None
    assessment_revision_id: str | None   # = candidate_revision_id (bridge_assessment.py:424)
    realizations: tuple[DirectionalRealizationContextV1, ...]
    producer: str; strength: str; lifecycle: str
    current: bool
    evidence_ids: tuple[str, ...]
```

- **Dropped until Release C defines them:** `definition_revision_id`, `execution_revision_id`,
  `mapping_dataset_ref`, `leg_plan_hashes`, `leg_realization_revision_ids`,
  `applicability_scope_hash` (none exists on main). Release C extends this contract additively
  with `crosswalk: CrosswalkContextV1 | None` carrying `JoinLegPinV1` tuples (D5).
- **Availability never encodes safety.** The four-way `discoverable|sandbox_only|executable|
  unavailable` is deleted. UI derives "executable" ONLY from a reader that revalidates live
  dependencies (`bridge_store.executable_bridge_realizations`, `:886-909`); the pure predicate
  (`eligible_for_production`) may label history, never a live capability.
- Builders compose the two existing readers exactly as `entity_map._link_view` does; that
  composition moves into ONE shared function both entity_map and the bundle call (no duplication,
  no third reader).

## D4. Observation context — faithful projection of the V2 store

`ObservationContextV1` mirrors `RelationshipObservationV2` (`relationship_observation.py:313-341`,
migration 1038) field-for-field. Binding decisions:

- Keep BOTH directional maxima (`max_right_matches_per_left_row`, `max_left_matches_per_right_row`)
  — no single `direction` field.
- Keep `method ∈ {exact, approximate}` × `row_coverage ∈ {full, sampled, partial}` as two axes.
  The plans' `evidence_basis` merger is deleted; `governed_key`/`source_constraint` are not
  observation values (they are realization-evidence kinds and stay there).
- Identity fields: `scope_id`, `left_binding_revision_id`, `right_binding_revision_id`,
  `realization_revision_id` — exact store names, sides preserved.
- No `lifecycle_status`/`expires_at`/`observation_kind`/`supports` inventions. Currentness comes
  from the `relationship_observation_current` pointer; strength/conflict from `strength`/
  `conflict_observed`.
- The asymmetry rule ("a sample may disprove uniqueness but never establish it") is already
  enforced (`:417-430`, `store.py:251-253`); consumers read it, they do not re-derive it.
- Semantic Task 12 is now a one-paragraph pointer to this section; it freezes nothing new.

## D5. Contract ownership

| Contract | Canonical owner | Consumers import |
| --- | --- | --- |
| `SemanticValueV1`, `SemanticContextBundleV1`, `IdentifierNamespaceV1`, `NeighbourColumnV1`, `RelationshipContextV1`, `ObservationContextV1`, `RelationshipKind` | semantic Task 1, `overlay/upload/semantic_context.py` | profile plan, Context Graph, feature/agent adapters |
| `MISSING_CONTEXT_CODES`, `REASON_CODES`, `UNRESOLVED_REASONS` (closed vocabularies) | semantic Task 1, same module — defined BEFORE first emission, hash-load-bearing from day one | both plans |
| `DataRole`, `AuthorityRole`, `TemporalStorageModel`, `EffectiveProfileFieldV1`, `DatasetSemanticProfileV1`, `CatalogProfileRevisionV1` | profile Task 1/2 | semantic Task 7/8 |
| `DatasetNeedV1`, `DatasetSourceSelectionV1`, `DatasetTemporalPolicyRevisionV1`, `DatasetRowSelectionV1`, `PolicyProvenanceV1` | profile Task 7 | semantic Task 8 (Release-B acceptance) |
| `JoinLegPinV1` (five-field form, profile §6.6), `CrosswalkDefinitionRevisionV1`, `CrosswalkExecutionRevisionV1` | Release C Task 10 | semantic context via the additive `crosswalk` extension (D3) |

- **Naming:** the dataset profile hash is `dataset_profile_hash` in EVERY contract (the
  `profile_hash`/`selected_profile_hash` variants in the profile plan are renamed).
- `unresolved_reason` is a closed enum honoring the product rule — every member maps to exactly
  one of `{undecided, needs_data_check, structurally_unsuitable}` and the UI renders that family,
  never a failure-shaped free string. "No evidence at all" is `undecided:no_evidence`, distinct
  from `influence_not_operational` (which display fields report as their NORMAL state, not as
  unresolved — profile §6.3 amended).
- `DatasetNeedV1.execution_tier` reuses `bridge_realization.ExecutionTier`; semantic Task 8's
  feature path constructs needs with `SANDBOX` until a production feature flow exists.
- HOME DECISION (2026-08-03, Task 7 review-ratified): the §6.4/§6.5 selection/temporal contracts
  live in `overlay/upload` (both execution stacks already import it; neither imports the other for
  a source decision; `overlay/upload/__init__.py` stays empty). Release C's crosswalk contracts
  use the SAME home — do not re-litigate.
- WIRE-ENUM SPLIT (2026-08-03, ratified): `MODEL_UNRESOLVED_CODES` (the intent wire schema enum)
  is deliberately DISJOINT from `SELECTION_REFUSAL_CODES` — the model must never be able to assert
  an overlap/binding/population refusal (functional rule 1), and the request body stays
  byte-identical (D10 no-silent-widening). Selection refusals surface via clarify/learning only.

## D6. Snapshot pins — six kinds, compat-safe hashing

- Item kinds: `column_field` (existing) + `dataset_profile`, `serving_policy`, `source_selection`,
  `physical_binding`, `temporal_policy`, `row_selection` — the six-pin list wins.
- `build_metadata_snapshot` RAISES on an unknown requested field (today it silently drops,
  `feature_metadata_snapshot.py:426`).
- **Hash compat rule (no migration needed):** `item_hash` for `item_kind="column_field"` keeps the
  legacy computation (kind excluded) so existing stored snapshots stay valid; every NEW kind
  includes `item_kind` in its hash. Cross-kind collision is thereby impossible without touching
  legacy rows.
- `compare_snapshot_to_current` dispatches by `item_kind`; an unknown stored kind returns a typed
  `SNAPSHOT_KIND_UNSUPPORTED` refusal, never generic drift.

## D7. Migration reservations

Allocation rule: uniqueness is the FULL FILENAME; ledger state is recorded as the applied
name-set, never a head number (duplicate prefixes exist at 0973/0974/1034/1036/1037/1038/1040
and the runner is lexical + name-ledgered, `db/migrations.py:260-317`).

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

| 1056 | PII allow-policy surface (user-directed 2026-08-04): `pii_use_policy` immutable revisions + CAS current pointer |
| 1057 | Release C Task 11 — crosswalk observation store (`1057_crosswalk_observation.sql`), CONSUMED. The database-derivation unification shipped in the same task needs NO DDL: the inventory-derived writer (`bridge_assessment.resolve_and_record_endpoint_binding`) has zero `src/` callers, and the live writer (`binding_store.record_binding`) already derived `database` from the connection — so converging onto the connection's declaration re-addresses no stored row. Proven by `test_binding_derivation_unification`. |

| 1058 | Readiness wave (2026-08-09) — `graph_node.fibo_path` display projection (`1058_graph_node_fibo_path.sql`), CONSUMED. The third FTR sidecar taxonomy path, joining `bian_path`/`process_path` from 1051; it was captured as SOURCE evidence exactly like both siblings and then had no policy and no flat column, so it resolved nowhere — invisible precisely BECAUSE its siblings worked. Closes the last `UNCARRIED_GAPS` entry. Same rebuildable, never-authoritative contract as 1051/1052. |

| 1059 | Intake build increment 2 (router-quality plan 2026-08-10) — `contract_intent` target-reading extension (`1059_contract_intent_target_reading.sql`), CONSUMED. The storage decision executed: the model's ticket draft lives in `structured_result` (increment 1, no DDL); the HUMAN confirmation extends `contract_intent` with `target_window_days`/`target_type`/`business_domain` + provenance (`target_provenance` ∈ human_confirmed / user_typed / exploring, `target_confirmed_by/_at`). All additive-NULL; legacy client-supplied target_ref writes are untouched (provenance NULL). |

New needs append 1060+ to this table FIRST (edit this doc in the same commit as the migration; note Phase G's future G-2/G-3 iterations also draw from this pool — coordinate here).

## D8. Flag matrix

| Flag | Enabled by | Depends on |
| --- | --- | --- |
| `FEATUREGEN_FEATURE_CONTEXT` | Release-A deploy gate | — |
| `FEATUREGEN_DATASET_PROFILES` | Release-A deploy gate (same approval, both flags presented) | — |
| `FEATUREGEN_SOURCE_TEMPORAL_SELECTION` | Release-B gate | `FEATUREGEN_DATASET_PROFILES=1` |
| `FEATUREGEN_CROSSWALK_EXECUTION` | Release-C gate | `FEATUREGEN_SOURCE_TEMPORAL_SELECTION=1` — enforced fail-closed at startup, not by convention. NOTE (2026-08-03): Release B's flag dependency ships as fail-closed-at-every-call-site + loud boot log, which honors its row; Release C's row REQUIRES a true boot refusal and must NOT inherit the log-only precedent |

- `FEATUREGEN_MATERIALIZE_ENABLED` (Phase G, recorded post-unification 2026-08-04): the
  materialization-lane kill switch + its four companion settings (PROJECT_ROOT / INVENTORY /
  L0_PYTHON / L0_TIMEOUT_SECONDS) — default off, conformant truthy set, in both manifests;
  enabling it is part of a later deploy gate, never implicit.
- All four use the widened truthy set `{"1","true","yes","on"}` (`feature_assist.py:193` pattern)
  and are added to `deploy/kind/k8s/20-backend.yaml` + `.env.example` (defaults off).
- Feature-context versions: v4 ships REGISTERED alongside v2/v3 (D10). Rollback ladder:
  flag off → v1 menu (unchanged); flag on + `FEATUREGEN_FEATURE_CONTEXT_VERSION=3` → today's
  shipped behavior; flag on (default) → v4. The env override exists precisely so v3 remains
  reachable after Task 8.
- Profile-in-feature-context is governed by `FEATUREGEN_DATASET_PROFILES` AND
  `FEATUREGEN_FEATURE_CONTEXT`; with either off, feature payloads are byte-identical to that
  flag's off-state today. Profile Task 6's flag-off check must assert the COMBINATION states.

## D9. Gates between the plans

- Profile Tasks 1–3 may run in parallel with semantic Tasks 1–2 (the program sequence above)
  BECAUSE this document, not task completion, freezes their shared contracts. The shared-file
  rule stands: `concepts.py`, `party_vocab.py`, `bridge_candidates.py`, `entity_map.py` belong to
  semantic Tasks 1–2; `field_policies.py`, `field_resolution.py`, `field_correction.py` belong to
  profile Task 1 — no cross-edits.
- `table_synth.py`/`enrich_llm.py` are single-owner during the joint step (semantic 3–4 +
  profile 4): one implementation stream, both plans' checkboxes.
- Semantic Task 10's live-LLM comparison is EXPLICITLY part of Gate B (separate approval), not a
  pre-gate deliverable. The pre-gate deliverable is the replay/fixture half only. (Amends
  semantic Task 10/Execution Order; matches profile Task 6's rule.)
- Test-count gates are scoped to NAMED focused suites (the Task-0 lists), never the whole repo
  (DEFERRED-WORK §C contamination). The mutation harness is a NEW deliverable of the joint
  eval step (Sem 10 + Prof 6) — it does not exist on main; nobody may cite it as existing.

## D10. Egress and schema-registry rules (repaired in Task 0.6)

- `schema_for(id, version)` returning None for a REQUESTED version is a raised error at dispatch,
  never a silent unenforced call (fixes the trap documented at `enrich_llm.py:769-773`).
- Unknown TOP-LEVEL metadata keys in the single-call path fail closed (today they egress
  unscanned — `enrich_llm.py:168` intersection bug).
- Every new context key ships with an explicit classification in the relevant allowlist
  (`_FEATURE_COLUMN_*`, `_ITEM_META_ALLOWED`, `_COLUMN_PROFILE_KEYS`, `_ROSTER_ENTRY_KEYS`) plus
  a golden egress test. Unclassified = blocked stays the law; the change is that blocked is LOUD.
- The fact wrapper accepts the D2 typed triple; `llm_proposed` display never appears on the wire —
  the wire carries `(producer, strength)`.
- Pass-B extension (profile Task 4) requires a REAL schema v3 body + prompt version bump
  (`overlay_table_synth_batch` v2 is a byte-alias of v1 with `additionalProperties: false`).
- Feature-context v4 requires registration in `_SCHEMAS` before `_feature_schema_version()` may
  return 4.

## D11. Read scope

- Table anchors get DERIVED visibility: a table node is visible iff the caller can see ≥1 of its
  columns (the `overlay/upload/catalogs.py:50-59` shape). Search predicates, profile-text
  matching, catalog narrative reads, and the Context Graph all use it. (Repaired for search in
  Task 0.6; new surfaces adopt it from birth.)
- Both bundle builders take `roles` and filter every neighbour/link/table read through
  `visible_requires <@ allowed` — the bundle inherits NOTHING from the un-scoped
  `read_operational_value`/`read_column_facts` paths without a scope check at the boundary
  (closes DEFERRED-WORK B-1 #8 for this surface instead of widening it).
- Entity Map's documented un-scoped links (`entity_map.py:20-27`) remain as-is; the semantic
  Task 6 "same value everywhere" claim is amended to name that exception.

## D12. Bound amendments to the plans

1. **Counterparty (semantic Task 2) — REVISED 2026-08-01 after review probes proved the original
   instruction self-contradictory** (`graph_node.entity` is itself a fact-key input via
   `bridge_grounding.advisory_entity_id`, so "backfill entity but don't re-key" was
   unsatisfiable; a re-key resurrects human-REJECTED decoy links and orphans VERIFIED ones):
   the entity stays `counterparty` EVERYWHERE it is persisted or feeds derivation —
   `Concept.entity_link`, `graph_node.entity`, `axis_projection`, grounding (`concept_entity` =
   raw `entity_link`), candidate enumeration and fact keys. NO `graph_node` entity backfill, NO
   search-doc reproject hooks. The `customer` correction is READ-TIME ONLY via the alias seam
   (`display_entity`) in Entity Map, asset detail, bundles and any UI render — the exact
   `sensitivity` vs `sensitivity_display` precedent (migration 1042: never rewrite the value
   that generates derived state). Migration 1045 carries ONLY the catalog semantic-scope table.
   `known_entities()` keeps `counterparty` as a readable legacy member; new classification
   vocabulary excludes `counterparty_id`; the concept critic's revise pass canonicalizes through
   `canonical_concept_name` like Pass A. The three pinned pairing tests KEEP their original
   entity literals; alias-seam tests assert display values only. Issuer folding into
   `assess_grounded_identifier_link` unchanged. Gate-B approval text must state the
   vocabulary-fingerprint change forces a full concept re-classification at next live ingest
   (paid LLM spend).
2. **Retrieval legs (semantic Task 9):** grain/time is leg 1 (as shipped, by design); lexical is
   leg 2; semantic expansion is leg 3; link neighbourhood leg 4. Plan renumbered.
3. **Stage outcomes (semantic Task 5):** `selected/unchanged/...` live in the stage `detail`
   payload; stage state stays within the 0996 CHECK vocabulary; `stage_report.py` added to the
   task's file list.
4. **Supersession (semantic Task 6) — implemented, wording corrected:** `_write_llm_field_evidence`
   reuses on `proposed_value_hash` (oldest matching row wins — that stability IS the point);
   `_write_concept_evidence` reuses on `input_hash`. Two reuse keys, two paths, DELIBERATE — the
   earlier parenthetical "like the concept writer" was wrong. Known trade: a same-value reuse
   keeps the original `input_hash`/`source_snapshot_id`, so re-derivation recency is not recorded;
   a compensating decision-log record is DEFERRED (recorded in the writer docstring).
5. **Concept cache identity (semantic Task 4):** sibling-roster context enters the PROMPT but not
   the per-column `input_hash`/cache key; roster changes re-enrich only on the vocabulary/pipeline
   fingerprint, not per-sibling-edit. (Prevents per-table identity cascades; accepted trade-off:
   a sibling change does not auto-invalidate neighbours until the next fingerprint bump.)
6. **`AttributionPolicyV1` → `DimensionAttributionPolicyV1`** everywhere in the profile plan; and
   Release B Task 8 explicitly budgets NEW work for `CURRENT_RECORD` (basis today declared and
   refused) and `LATEST_SNAPSHOT_AS_OF` (no analysis-path implementation) — only
   `VALID_AT_REPORT_CUTOFF` is reuse.
7. **Load-bearing path (profile Tasks 1/3):** `authority_role` and `temporal_storage_model` get
   `human_editable=True` with the existing four-eyes propose/confirm flow (`propose_override` →
   `confirm_override`, distinct subjects) — platform-admin confirmer, `data_owner` may propose via
   a new scoped route. `set_advisory` is whitelisted to `business_context` ONLY. The Release-B
   serving/temporal policy remains the alternative operational declaration.
8. **Release C predecessor:** the codegen-remediation plan + Phase-G wiring plan are named hard
   predecessors of Release C Task 12 and of semantic Task 8's generated-project acceptance.
9. **Superseded-plan drops** (OPERATIONAL/AUDIT serving purposes, owner/steward, OpenMetadata
   description import, `table_role` facet, typed `dependency_kind`, sandbox observation
   persistence, `open_gaps` demand surface) are recorded in `docs/DEFERRED-WORK.md` with triggers
   as part of Release-A Task 6 exit — reinstated only by explicit decision.
10. **`catalog_profile_revision_id` semantics:** authoring-context provenance on evidence;
    effective reads resolve the CURRENT pointer; `dataset_profile_hash` includes the current
    narrative revision id — so narrative edits DO re-key dataset profiles (accepted: narrative is
    meaning-bearing) but do NOT invalidate feature snapshots, which pin the decision refs of D6,
    revalidated at execution.

## D13. Product decisions 2026-08-01 — fine-grained classification axes

Background: both live source files declare `data_domain` uniformly (FTR = Compliance × 127 rows,
CIB = Customer × 111 rows), so `domain` is a two-value coarse axis by uploader authority — which
source-over-LLM precedence rightly preserves. The fine-grained taxonomy already in the files is
the per-column BIAN levels and business-process paths, captured today as source evidence
(`bian_path`, `process_path`, `fibo_path`). User decision: adopt BOTH of the following; neither
changes domain precedence.

**D13.1 — BIAN/process as a first-class searchable axis (owner: profile Task 5 step).**
- Migration 1051 adds rebuildable `graph_node` display-projection columns `bian_path`,
  `process_path` (the facet mechanism requires literal columns; projections follow the existing
  table display-column pattern and ride the `table_display_reprojection`/resolution path — never
  authoritative, always rebuildable from evidence).
- Search: facet + filter on both (facet values are the stored " > "-joined paths; a hierarchical
  L1/L2 facet UI may segment on the delimiter client-side — no new server vocabulary). Read scope
  identical to other column facets.
- Data-agent retrieval (semantic Task 9): BIAN/process terms join the leg-2/leg-3 controlled
  semantic expansion inputs.
- Display: already shipped (dossier source-glossary section); no new UI surface required beyond
  the facet controls.

**D13.2 — LLM sub-domain proposals beside the source domain (owner: joint Task 4 step).**
- NEW field `sub_domain`, recommendation/display tier exactly like `domain` (LLM visible,
  human-editable via existing four-eyes, never load-bearing, never overwrites `domain`). Source
  `domain` stays the coarse governed axis; `sub_domain` is a finer LLM-proposed axis rendered
  with its `llm_proposed` authority label per the no-blocked rule.
- Produced by the existing Pass-A domain task extended per D10 discipline: a REAL new schema
  version body (per-column `sub_domain` beside the table-first `domain`/`column_domains` shape),
  prompt version bump, registration before request, golden payload tests. No second LLM call.
- `graph_node.sub_domain` display projection rides migration 1051; facet added in the same
  profile-Task-5 step as D13.1 once populated.
- Population requires the Gate-B re-enrichment run (live LLM spend — already governed by that
  approval; witnesses: `counter_party_bic`, `pstd_date`-family temporal columns, and at least one
  CIB flag column should receive sub-domains finer than Customer/Compliance).
- Closed-vocabulary option deliberately deferred: sub_domain v1 is free-text-constrained-by-prompt
  like `domain`; a curated sub-domain list becomes a `_KNOWN_VOCAB_VALIDATORS` entry later if the
  bank supplies one (record as a deferred item at the joint step, not silently).

## D14. PII allow-policy surface (2026-08-04, user-directed)

Closes the `PERSONAL_DATA_POLICY_REQUIRED` door the use-gate names. Bindings:
- **SINGLE-PERSON approval, by explicit user decision** — one authorized approver (the
  platform-admin claim) declares concept + bounded purpose text; ACTIVE immediately; revocation
  is likewise one action. This deliberately deviates from the four-eyes convention for this
  surface; the immutable who/when/purpose record is the control. Do not re-add a confirmer step
  without a new user decision.
- House store pattern: immutable revisions + CAS current pointer (migration 1056); revocation is
  a new revision, never a delete.
- The gate clears a PII operand iff EVERY pii-classed concept it uses has an ACTIVE policy;
  protected characteristics NEVER clear (structurally_unsuitable is not policy-addressable).
- Purpose is bounded free text in v1; a closed purpose taxonomy is a later refinement.
- Acceptance: the five A.34 recipes light when their anchors are approved; revocation refuses
  them again; the feature's provenance names the covering policy.
