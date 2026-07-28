# Verified Interfaces — Cross-Catalog Identity, Bridges, and Ontology

Date: 2026-07-28 · Verified against `origin/main@ef213a83` plus the on-branch fix `e6e5f31d`.

**This is a reference, not a plan.** It records what the code actually does, with a citation for
every claim, so that the cross-catalog plan and the specs built on it can cite rather than restate.
It changes when the code changes.

**Why it exists.** Three separate defects this week were "the capability is already there" claims
that dissolved on contact with the code: the bridge matcher never reading `declared_type`, BIAN/FIBO
taxonomy never being persisted, and confirm-time CAS not existing in the fact spine. Each survived
multiple adversarial reviews because the reviews checked reasoning, not the interfaces underneath
it. **A claim here without a file citation is a defect in this document.**

Companion documents:

- `../superpowers/plans/2026-07-27-e3-e5-cross-catalog-ontology-program.md` — the plan. Owns
  contracts, phases, gates.
- `2026-07-27-verified-interfaces-materialization.md` — the equivalent reference for the
  materialization path.

---

## 1. How to use this document

When a plan or spec asserts that something is "already earned", it cites a line here. When an
implementer is about to build on an interface, they read the relevant entry first and re-check it —
main moves, and an entry can go stale between the writing and the building.

When an entry turns out to be wrong, fix it **here** and let the dependent documents inherit the
correction, rather than patching the claim wherever it was repeated.

---

## 2. What is already earned

The program reuses these shipped capabilities; it does not recreate them. **Every entry carries a
source citation.** Rev 4 asserted several of these without one and three turned out to be inert —
an uncited claim is now a defect in this document, not a shortcut.

- **Bounded join-neighbourhood traversal is SHIPPED — adopt it, do not respecify it.**
  `join_path.py` provides `MAX_HOPS_DEFAULT = 1`, `MAX_NEIGHBOUR_TABLES = 20`,
  `MAX_COLUMNS_CONSIDERED = 300`, `MAX_HOPS_CEILING = 3`, deterministic pre-truncation ordering by
  `(hop distance, table name)` with a stop-at-first-breach rule, typed `limit_reason`, and
  `JoinNeighbourhood.as_metadata()` returning
  `tables_considered / tables_available / truncated / max_hops / limit_reason`
  (`src/featuregen/overlay/upload/join_path.py:219-265, 283-343`). It is already consumed by the P4
  suggestions surface and its numbers were measured against a hub fixture (12,710 → 6,284 statements
  at 40 neighbours; bounded, not merely smaller). `OntologyServerLimitsV1` **extends** this with the
  collections `join_path` does not bound; it does not restate it.
- **Per-field property authority is SHIPPED — adopt it, do not respecify it.** `field_evidence`
  stores per-`(logical_ref, field_name)` proposals with producer, strength, lifecycle, evidence
  spans, confidence band and source snapshot (`db/migrations/0983_field_evidence.sql`);
  `field_policies._POLICIES` + `field_authority.resolve_field_authority` resolve each field
  independently with influence tiers, disqualifiers and conflict strategies
  (`overlay/upload/field_policies.py:203-228`, `overlay/field_authority.py:262-310`); and
  `build_asset_detail` already assembles identity + per-field effective value with
  authority/provenance + per-field evidence, read-scope applied to the anchor first, one
  `REPEATABLE READ` snapshot, and a content-hash `consistency_token`/ETag
  (`overlay/upload/asset_detail.py:71-81, 126-141, 562`). That is the shape `OntologyPropertyV1`
  specifies. E3 maps onto it and fills its two real gaps (below), rather than building a parallel
  assembler.
- `graph_node` and `graph_edge` provide source-qualified table/column metadata and
  intra-catalog joins at the **public-flattened graph identity**. `schema_name` (migration
  `1000_graph_node_schema_declared.sql`) is a nullable non-key column populated **only by the
  FTR/glossary adapter** (`overlay/upload/graph.py:156-178`). It is evidence on one ingest path, not
  a catalog-wide attestation, and graph identity alone is not sufficient physical identity.
- The concept registry is global across catalogs and currently contains 283 concepts,
  19 groups, 52 `is_a` declarations, and 38 entity identifiers. These counts are an
  audit snapshot, **not an API contract**.
- `EntityRelationshipDefinitionV1` and `ENTITY_RELATIONSHIPS_V1` provide five curated
  grain-rollup relationships used by the planner.
- `derive_catalog_realizations` binds governed/declared joins to those five
  relationships and detects cardinality conflicts.
- `entity_bridge_edge` is the VERIFIED cross-catalog identity-key projection —
  **cross-catalog only, by code and by constraint.** `_bridge_write_error` refuses
  `left.catalog_source == right.catalog_source` (`overlay/identity.py:131-133`),
  `derive_bridge_candidates` skips same-source pairs (`bridge_candidates.py:98`), and migration
  `0989` enforces `CHECK (left_catalog_source <> right_catalog_source)`. Two same-named schemas
  under **one** source therefore cannot be bridged by the shipped fact at all — see the
  intra-source namespace gap in "What is not earned".
- `derive_bridge_candidates` already considers identifier columns across all loaded
  catalogs. E5 must not build a duplicate candidate lifecycle, but E3 reuses only this
  algorithmic seam after replacing its unbounded pair enumeration, public-schema identity loss,
  and scalar-only key model. It must not treat every bridged foreign key as an object primary key.
  **Its matching rule is weak and E3 must strengthen it**: concept group `identifier` + identical
  `entity_link` + identical coarse type family (`_TYPE_FAMILY` collapses `int4` and `bigserial`)
  + distinct source (`bridge_candidates.py:19-29, 85-102`). There is no format, prefix, range,
  key-shape or uniqueness evidence.
- E1a has landed governed definition, domain, synonym, and concept-cascade evidence.
  **Caveat that E3 inherits:** synonyms are written as `llm/proposed` `semantic_terms` field
  evidence (`enrich.py:856-899`) but `semantic_terms` has **no entry in `_POLICIES`**, so
  `policy_for('semantic_terms')` returns `None` and there is no resolvable value or authority for
  it today. `OntologyPropertyV1` lists synonyms as a field envelope; that envelope needs a policy
  first (E3.Foundation).
- Governed `entity_assignment`, `currency_binding`, `grain`, `availability_time`,
  `approved_join`, and `entity_bridge` facts already exist. **The `grain` fact value carries
  `is_unique: boolean`** (`overlay/facts.py:70-83`) and the profiler deliberately proposes
  `is_unique=False` for sampled uniqueness in `[0.99, 1.0)` so a human adjudicates
  (`src/featuregen/overlay/profiler_heuristics.py:54-66`). `is_unique` asserts STRICT uniqueness —
  `True` only when the sampled ratio is exactly `1.0` — while a near-unique candidate is still
  proposed with `is_unique=False` and the ratio carried in evidence for the reviewer. A VERIFIED
  grain is therefore **not automatically a key**.
- The semantic-binding deliveries already provide immutable candidate sets, a CAS
  current-set projection, candidate-to-governed-fact links, schema-preserving column
  references, and the closed `entity_assignment`/`currency_binding` candidate kinds.
  They do **not** yet provide stable semantic candidate ids separate from revisions,
  candidate-family-scoped currentness, or extensible candidate kinds. E3 migrates and adapts this
  substrate; it does not create a parallel column-link candidate lifecycle. **Both candidate tables
  are physically immutable (WORM):** `BEFORE UPDATE OR DELETE ... RAISE EXCEPTION` row triggers plus
  `REVOKE UPDATE, DELETE, TRUNCATE ... FROM featuregen_app`
  (`db/migrations/1014_semantic_binding_candidate.sql`). Adding a nullable column is fine;
  **populating it on existing rows is an UPDATE the store refuses.** See the backfill rule below.
- **Two further candidate lifecycles already exist** and must be reconciled, not ignored:
  `entity_bridge_candidate_evidence` (`0989_entity_bridge_governance.sql:6-22`) already persists
  candidate id, canonical unordered endpoint pair, fact_key, proposed_event_id, derivation_version
  and evidence_json — i.e. the identity `IdentifierNamespaceBridgeCandidateV1` re-invents; and
  `entity_suggestion` (`0967_entity_suggestion.sql:6-16`) is a pending/applied/dismissed column-
  entity advisory lifecycle that `build_graph` re-applies. Without an explicit decision, a column
  entity proposal would live in **four** places (`entity_suggestion`,
  `semantic_binding_candidate(kind=entity_assignment)`, `field_evidence('entity')`, and the new
  family). E3.0 must pick one home per proposal class and state the migration for the rest.
- `/search`, `/graph/lineage`, asset detail, read-scope filtering, and the committed
  `LineageView` provide useful implementation seams. **`search()` is already cross-catalog,
  fresh-only and read-scoped, and already returns `n.concept` in its hit projection**
  (`overlay/upload/search.py:33-38`), which the UI already renders
  (`frontend/src/screens/SearchScreen.tsx:435`). `concept` is simply absent from `_COLUMN_FACETS`
  (`search.py:15-22`). This is the basis of the E5.0a slice below.

## 3. What is not earned

- There is no E3 ontology-link contract or advisory edge store.
- The five planner roll-ups are not a general link-type vocabulary. They cannot
  represent labels such as `owned_by`, `denominated_in`, `converted_by`, or `as_of`.
- No entity title/display-property binding exists. The E2 design explicitly deferred
  it to E3.
- There is no source-agnostic object-type read model or ontology API.
- There is no object-resolution projection or conflict model.
- There is no schema-safe cross-catalog identifier-namespace contract.
- Existing catalog realization derivation maps a missing cardinality to `N:1`; that
  default is planner compatibility behavior, not ontology evidence.
- Existing object-grain derivation reads the first flat `is_grain` column and does not
  prove compatibility with the complete ordered governed `grain` fact.
- Current authority values cannot be truthfully compressed into one ordered tier:
  producer/derivation, verification status, and operational eligibility are distinct.
- `entity_bridge_edge` is a rebuildable projection whose active reader does not itself
  prove that the backing event stream is current. E3 needs an explicit projection
  readiness and authoritative-state revalidation protocol.
- The generic fact spine has no cross-fact uniqueness rule capable of enforcing one
  operational title when each candidate property receives a different fact key.
- **The fact spine has no confirmation-time CAS, and Rev 4's three CAS acceptance tests described
  behaviour it cannot exhibit.** `propose_fact` denies **at propose time** whenever a non-terminal
  fact already exists for the `fact_key`, and sticky-denies a previously rejected proposal
  fingerprint (`overlay/proposal_commands.py:34-40, 71-80`). Because `fact_key` hashes
  `(ref, fact_type, use_case)` only (`identity.py:79-111`) and this plan puts the selected
  entity/property in the fact **value**, two competing object types — or two competing title
  properties — collide on **one** fact key and the second is refused before any confirmation. The
  real semantics are "first proposal blocks all rivals until explicitly rejected", which is a
  different product behaviour (it needs a reject path in the UI, not a race winner) and must be
  specified as such.
- **A new fact type is not registered by declaring its schema.** Every authority-side seam is
  opt-in and fails open or crashes by default: `enter_fact` — the audited single-party four-eyes
  exception — is blocked only by an explicit denylist
  `if fact_type in ("entity_assignment", "currency_binding")`
  (`confirmation_commands.py:325-333`), so a new owner-known `object_type_binding` would be
  **single-party self-assertable**; `resolve_authority` has no branch for the new types and falls
  through to `if not isinstance(ref, CatalogObjectRef): raise TypeError`
  (`overlay/authority.py:167-170`) for a tuple ref; `Authority.dual`/`task_assignees` per-side
  planning is `approved_join`-only (`authority.py:62-95`); and the `FactType` Literal
  (`overlay/_types.py:45-54`), reverify, expiry and drift paths all need entries.
- **The refs this plan needs are not expressible.** Every governed fact ref is a
  `CatalogObjectRef(catalog_source, object_kind, schema, table, column)`
  (`identity.py:9-15`), `_CATALOG_OBJECT_REF_SCHEMA` sets `additionalProperties: False`
  (`overlay/facts.py:39-50`), and `_ref_from_payload` decodes exactly three shapes
  (`identity.py:42-57`). A realization-scoped title ref (which must carry `entity_id`) and a
  tuple-scoped bridge ref (ordered column tuples) are therefore **new ref types**, not new value
  schemas — each needing `_ref_from_payload` extension, `fact_key` canonicalization, and updates to
  the freshness/expiry pollers that decode payload refs.
- **Cardinality is part of `approved_join` fact IDENTITY, so it cannot be corrected.**
  `identity.fact_key` hashes cardinality into the canonical tuple (`identity.py:93-103`, line 99).
  Correcting a wrong cardinality therefore does not demote the wrong fact — it mints a **second,
  distinct fact on the same column pair, and both can be VERIFIED**. `passc/projection.py:109-118,
  134-137` arbitrates by `min(verified, key=fact_key)` — the lexicographically smallest sha256 —
  while its own comment calls the situation "impossible under the Pass-C ledger's one-row-per-
  unordered-pair invariant", an invariant the ingest propose path (`ingest.py:383-398`) does not
  share. `join_drift._declared_join_map`/`detect_governed_join_divergences` compare only
  `from_ref → to_ref` (`join_drift.py:50-64, 92-110`), so a re-upload flipping `N:1` to `1:N` raises
  **no divergence at all**. A property this plan itself calls silently-aggregation-corrupting is
  currently arbitrated by a hash.
- **Honest `unknown` cardinality is unimplementable on the shipped path without a schema change.**
  `graph.py:93` fabricates the default **at propose time** (`cardinality=row.cardinality or "N:1"`),
  so the governed fact VALUE asserts `N:1` for a blank upload; the `approved_join` value schema
  **requires** cardinality and admits only `1:1|1:N|N:1` with no `unknown` and no basis field
  (`overlay/facts.py:100, 118`); and after dual confirmation the projection overwrites the honest
  `NULL` on `graph_edge.cardinality` with the fabricated value (`passc/projection.py:139-149`). By
  the time E3.1 tries to "revalidate the raw backing cardinality" the raw missing-ness has been
  erased from **both** the fact and the edge.
- **There is no product surface that can create an `entity_bridge`.** `api/routes/governance.py`
  exposes list/confirm/reject for joins, table facts and semantic bindings only
  (route decorators at `governance.py:155-442`); there is no bridge route anywhere in
  `api/routes/`. Every VERIFIED bridge in existence was made by a fixture or a script. E5.3, E5.6,
  E5.7 and E5.8 are all built on VERIFIED bridges.
- **Bridge confirmation is single-party today, and E5 is exactly what invalidates that.**
  `overlay/authority.py:132-145` returns `Authority(role='platform-admin', subjects=(),
  governance_queue=True)` with `dual` left at its default `False`, and every shipped
  `CatalogAdapter.owner_of` returns `None` (`upload_catalog.py:65-66, 101-102`), so the same-owner
  collapse never fires and **every bridge takes the single-admin path**. The in-code deferral says
  "Two-owner dual sign-off is deferred to 3C (when a bridge becomes live-traversable)"
  (`authority.py:135-144`) — bridges are already traversed by the planner
  (`planner/assembly.py:156`, `planner/plan.py:76`, `planner/multisource_assembly.py:241`), and E5
  widens that reach further. Because `owner_of` is `None` everywhere, **every four-eyes claim in
  this plan degenerates to "two platform admins" at best, and "one platform admin" for bridges.**
- **Durable LLM result reuse does not exist.** `llm_dispatch`
  (`db/migrations/1005_llm_dispatch_provenance.sql:19-34`) stores `redacted_input` and **no
  response**; `llm_dispatch_outcome` stores only `response_received|transport_failed`. Its
  `UNIQUE(logical_call_ref, attempt_no)` is not content-addressed because `logical_call_ref` is a
  fresh `mint_id("lc")` per invocation (`enrich_llm.py:882, 1258`), and it is documented as "one
  physical dispatch record per attempt" — a retry deliberately creates a **new** key, the opposite
  of what replay requires. The only store holding `raw_output` is `llm_call` (migration `0510`),
  keyed `(run_id, task, input_hash)` with `run_id NOT NULL` — a key a run-less system-principal
  generation cannot form. The one full-identity probe, `find_llm_call` (`intake/llm.py:409-453`),
  has **zero production callers** and includes `run_id` in its identity.
- **Read scope filters the raw tag, not the governed one.** Every read-scope predicate filters
  `graph_node.sensitivity` — the raw file-declared tag (`read_scope.allowed_sensitivities`
  consumed at `join_path.py:163,176,276`, `bridge_candidates.py:57`, `graph.py:376-377`,
  `search.py:103`, `semantics.py:90,142`, `column_readiness.py:342`). The governed, floor-clamped
  value is written to a **different column**, `graph_node.effective_restriction`
  (`field_resolution.py:326, 353-356`), and `sensitivity` is deliberately absent from
  `_DISPLAY_COLUMN` so the resolver never updates it. `effective_restriction`'s only reader is
  `materialize/classify.py:204`. Net effect: **the concept-derived sensitivity floor never restricts
  catalog visibility.** The two vocabularies also disagree — `read_scope.SENSITIVITY_ROLES` knows
  only `{pii, restricted}` while `safety_floor.SENSITIVITY_ORDER` is
  `{public, internal, confidential, restricted, prohibited}`, so `effective_restriction`
  of `confidential` or `prohibited` has **no grantable role at all**.
- **Tables have no sensitivity, so "a fully hidden object is absent" is not achievable today.**
  `graph.py:239-245` inserts `kind='table'` rows with no `sensitivity` in the INSERT list, so the
  value is NULL and every `(sensitivity IS NULL OR sensitivity = ANY(...))` predicate admits them
  unconditionally. No shipped rule derives a table's visibility from its columns'.
- **Read scope is not durable state.** `build_graph` executes
  `DELETE FROM graph_node WHERE catalog_source = %s` (`graph.py:234`) and re-inserts each column
  with `r.sensitivity or None` (`graph.py:256-266`). A re-upload omitting the sensitivity column
  silently blanks the tag and the column becomes visible everywhere. The only guards are an
  object-COUNT brake (`brake.py:22-38`) and an intra-upload duplicate-conflict check
  (`ingest.py:2753-2793`), neither of which sees a tag that simply vanished between uploads.
- **No registry content fingerprint exists.** `entity_registry.GRAPH_VERSION = "1.0.0"` carries an
  explicit NOTE that a content fingerprint "would catch a definition change made without bumping the
  version. Omitted" (`taxonomy/entity_registry.py:15-17, 29`), and
  `catalog_realizations.CONCEPT_REGISTRY_FOR_REALIZATION = "concepts@1"` is a hand-maintained
  literal folded into `realization_fingerprint` (`catalog_realizations.py:120, 147-152`). **Editing
  a concept definition changes no fingerprint today**, so every cursor/candidate-revision binding
  this plan makes to "registry content fingerprint" binds to something that does not exist.
- **The concept registry has no cycle check.** `concepts._validate_registry` checks only duplicate
  names and `is_a` resolvability (`concepts.py:856-867`) — no self-parent, no cycle. Descendant
  traversal is unsafe until E5.1's validation lands, so E5.1's validation is a **prerequisite for
  its own traversal**, not a nice-to-have.
- **`AuthorityEnvelopeV1`'s enums are not a superset of the shipped ones.** `derivation ∈ {source,
  registry, rulebook, llm, human, legacy}` has no slot for `EvidenceProducer.PROFILER`, `PARSER`,
  `STRUCTURAL_CONNECTOR` or `TAXONOMY` (`overlay/evidence.py:15-30`) — and `STRUCTURAL_CONNECTOR` is
  what proposes **every** bridge and Pass-C join (`bridge_propose.py:42`). Folding a sampled
  statistical inference (profiler) into `source` misattributes an observation as an attestation.
  `verification ∈ {proposed, verified, rejected, expired, unknown_legacy}` has no value for the
  shipped `PARTIALLY_CONFIRMED` or `REVERIFY` fold states nor fact-lifecycle `STALE`
  (`overlay/state.py:67-124`, `_types.py:33-42`): mapping a drift-STALEd fact to
  `verification=verified, freshness=stale` renders a **demoted** fact as verified, and mapping
  `PARTIALLY_CONFIRMED` to `verified` is an outright fail-open.
- **A global metadata revision would convert per-source ingest serialization into global
  serialization.** Ingest is one transaction holding a **per-source advisory lock** across the
  Pass-A LLM enrichment stages, which cannot be released mid-transaction
  (`ingest.py:1670-1686`). Bumping one global counter row inside that transaction makes every
  concurrent upload of every catalog block on one row for the duration of another source's LLM
  enrichment — minutes on a 126-column file.
- **Schema does not reach ingest at all on the mainstream path — this is the program's largest
  single gap and Rev 4 mis-stated it.** `CanonicalRow` has **no schema field**
  (`overlay/upload/canonical.py:44-69`) and `_headers._ALIASES` has **no schema alias**
  (`overlay/upload/_headers.py:13-28`), so a CSV/Excel/OpenMetadata upload cannot express a schema
  — there is no collision to detect because the concept is inexpressible. Schema exists only on the
  FTR/glossary path, and there the reader **quarantines** same-`(table, column)` cross-schema terms
  fail-closed in its own pass 2, emitting no `CanonicalRow` and no sidecar
  (`glossary_reader.py:183-205`; `ftr_adapter.py:259`), precisely because "the schema is dropped
  from the `CanonicalRow`, so no later stage can see the collision".
  Consequences the plan must own rather than assume away:
  1. There is **no single "public-flattening collision step"** to persist a roster in front of.
     The loss happens in the readers, before any `CanonicalRow` exists.
  2. `validate_rows` keys dedup on `(source, table, column)` with no schema
     (`canonical.py:208`); identical load-bearing metadata silently dedups, differing metadata
     quarantines all rows fail-closed (`canonical.py:226-229`).
  3. `_cross_schema_conflicts` (`ingest.py:836-902`) is a **cross-upload** re-attribution fence
     against already-persisted nodes only, and within one file `incoming_tables.setdefault(table,
     rec.schema)` means first schema wins (`ingest.py:870`).
  4. Therefore, under this plan's own gate that unknown schema cannot be operational, **a
     CSV-ingested catalog would produce zero operational realizations, zero identifier bindings and
     zero namespace bridges.** E3.1's read contract would be structurally empty for the primary
     ingest path.
  E3 cannot recover a physical object that ingestion did not retain, and it cannot retain one
  without a reader/`CanonicalRow` contract change. That change is now E3.Foundation.0 and is a
  funding gate, not a bullet.
- **There is no intra-source identifier namespace.** Two same-named schemas under one source are
  the motivating example for physical identity, yet the shipped bridge is cross-catalog by code and
  by DB `CHECK` (see above). Bridging `sales.orders.id ↔ hr.orders.id` is a **new capability**, not
  an adaptation of `entity_bridge`, and Rev 4 budgeted nothing for it.
- **Existing `entity_bridge` facts have `public` fabricated into their immutable identity.**
  `bridge_candidates._col_ref` hard-codes `schema="public"` (`bridge_candidates.py:70-72`), that ref
  is hashed into `fact_key` (`identity.py:64-71, 87-92`), and `entity_bridge_edge` stores only
  public-flattened endpoint refs with no schema column (`0989:24-34`). This directly contradicts
  this plan's rule "E3 never silently substitutes `public`". Under E3's own gate **every existing
  bridge is non-operational** — the opposite of what Rev 4's "a legacy bridge adapts as a one-element
  tuple" acceptance asserted. E3.0 must choose explicitly between re-attesting legacy bridges and
  admitting them under a named, audited legacy exception.
- `entity_assignment` governs a **column's** entity. It does not assert which canonical object type
  a table models. The advisory `primary_entity` field and a complete `grain` fact do not replace a
  table-level governed object-type binding.
- The v1 `entity_bridge` fact binds one column to one column. Independent member bridges do not
  prove equivalence of two ordered composite keys.
- There is no cross-request metadata revision covering physical metadata, governed facts, and E3
  projections. PostgreSQL `REPEATABLE READ` protects one request only; it is not a continuation
  snapshot for the next page.
- Current catalog authorization provides deployment-wide `catalog:read` plus sensitivity roles.
  It has no source-entitlement policy. `IdentityEnvelope.tenant` exists, but current catalog keys do
  not carry tenant.
- `derive_bridge_candidates` is unbounded pairwise enumeration. It is not a bounded seam.
- ~~There is no ontology property contract capable of retaining different authority/freshness/
  provenance for concept, definition, domain, type, sensitivity, and synonyms.~~
  **Corrected in Rev 5: this was wrong.** A per-field policy, resolver and assembled reader all
  ship (see "already earned"), covering concept, definition, domain, sensitivity, data_type, unit,
  currency, entity, additivity and temporal_role, with `asset_detail` returning per-field display
  value + authority + provenance for 8 of this plan's fields. The **two real gaps** are narrower:
  (a) `semantic_terms` (synonyms) has no `_POLICIES` entry, so it has no resolvable
  value/authority; and (b) the resolver is single-catalog and anchor-scoped — E3 needs the same
  envelopes assembled across a paged, source-entitled, cross-catalog result set. Scope
  `OntologyPropertyV1` to those two gaps.
- The shipped semantic-binding current-set primary key is one row per table, and the candidate kind
  constraint admits only `entity_assignment` and `currency_binding`. Adding title/link generation
  without a migration would either fail the constraint or replace unrelated currentness.
- The current formula grammar supports aggregate, ratio, and difference bodies only.
  It cannot execute a currency conversion/product expression. E3 must not claim that
  `converted_by` produces an executable feature until that grammar is extended.
- `AssetDetailSampleScreen.tsx` is a local, hard-coded, uncommitted prototype on the
  inspected worktree. It is design input, not a reusable mainline dependency. The
  committed `LineageView` is the implementation baseline unless the prototype is
  separately reviewed and landed.


---

## 4. The identifier-link (bridge) substrate

| Fact | Citation |
| --- | --- |
| A bridge links ONE column to ONE column | `overlay/identity.py` `EntityBridgeRef` |
| Cross-catalog ONLY — same-source refused in code and by DB `CHECK` | `identity.py:131-133`, `bridge_candidates.py:98`, `0989_entity_bridge_governance.sql:21` |
| Candidate evidence table already exists | `0989_entity_bridge_governance.sql:6-22` |
| Proposal writes a PROPOSED fact + candidate evidence row | `bridge_propose.py:28-71` |
| **Projection drops anything not VERIFIED** | `bridge_projection.py:36` |
| **`active_bridges` filters `WHERE status = 'VERIFIED'`** | `bridge_projection.py:62` |
| The planner already consumes bridges | `planner/plan.py:19,76`; 12 call sites across `assembly`, `multisource_*`, `declarations`, `fingerprint` |
| Bridge `fact_key`s are hashed into **plan identity** | `plan.py:76`, `declarations.py:826`, `fingerprint.py:75` |
| No HTTP route exists to propose/confirm/reject a bridge | `api/routes/governance.py:155-442` covers joins, table-facts, semantic-bindings only |
| Confirmation is single platform-admin; `owner_of` returns `None` everywhere | `overlay/authority.py:132-145`, `upload_catalog.py:65-66,101-102` |
| Candidate matching: identifier concept + identical `entity_link` + compatible type family + distinct source | `bridge_candidates.py:85-102` |
| Candidate enumeration is an **unbounded** nested loop | `bridge_candidates.py:129-130` |

**Plan identity is the trap here.** Because bridge `fact_key`s are hashed into the plan fingerprint,
any change to *which* bridges are visible changes plan identity. A bridge's `status` must therefore
stay out of every hash, or confirming a link would invalidate every plan resting on it — the same
trap `operand_roles` hit, where the fix was keeping the new field off anything hashed.

### 4.1 Fixed on branch (`e6e5f31d`)

`_identifier_columns` classified every column by `graph_node.data_type` alone. A glossary upload
attests no physical type — the FTR adapter emits `CanonicalRow.type='unknown'` and carries the
file's answer in `graph_node.declared_type` — so every glossary column resolved to the `other`
family and was dropped before pairing. **A bridge candidate was structurally impossible for any
glossary-sourced catalog.** Measured on the deployed FTR catalog: 126/126 columns
`data_type='unknown'`, 113 of them `declared_type='string'`, **0 of 28** identifier columns
eligible — including the single `customer_id` (`cif_id`).

Fixed by falling back to `declared_type` only when nothing was attested, with
`BridgeCandidateV1.type_basis` (`attested|declared|mixed`) recording the basis. **0 → 28** eligible.

**The general lesson:** the platform stores *declared* and *attested* metadata in separate columns
on purpose. A reader consulting only one silently excludes an entire class of source. Any adapter
reading a physical attribute — type, schema, grain, nullability — must state which column it reads.

---

## 5. Evidence available about a column

Verified by querying the deployed catalog (126 columns, source `ftr`):

| Field | Persisted? | Coverage |
| --- | --- | --- |
| `definition` (business definition) | yes, `graph_node` | 126/126 |
| `semantic_terms` (synonyms) | yes | 126/126 |
| `domain` | yes | 126/126 |
| `declared_type` | yes | 126/126 |
| `concept` → `entity_link` | yes (registry-derived) | 120/126 |
| `schema_name`, `table_name`, `column_name` | yes | 126/126 |
| **`bian_path`, `fibo_path`** | **NO** | file has 114/127 |
| **`term_type`, `process_path`, `related_terms`** | **NO** | file has 127/127 |
| `entity` (free-text tag) | yes but empty | 0/126 |

**The taxonomy is discarded.** The FTR file carries banking-standard alignment — `CIF_ID` is BIAN
`Customer Management / Customer Profile`, FIBO `Business Entities` — and it is the strongest
semantic signal for deciding whether two identifier columns denote the same namespace. It is read
(`glossary_reader.py:73-74`), used once to help classify the concept (`enrich.py:503`), and then
dropped. No table stores it; `information_schema` has no column matching `%bian%` or `%fibo%`, and
no evidence row contains it.

**Value-shape evidence is unavailable for glossary catalogs.** The profiler computes
`distinct_count` and `uniqueness_ratio` (`overlay/profiler_metrics.py`) but samples real data. FTR
is a glossary upload with no dataset behind it. Any judgement about FTR identifiers is therefore
about **meaning**, not values.

---

## 6. The `attest/` harness — the reusable second-opinion pattern

`overlay/upload/attest/` implements proposer + independent critic + deterministic grounding +
fusion + shadow measurement, for concepts. It must be reused rather than reinvented.

| Component | Contract |
| --- | --- |
| `grounding.py::ground_concept` | → `GroundingV1(checks, coverage, conflict)` — deterministic corroboration |
| `reclassify.py::ColumnContext` | the **blind** input: name/definition/sample-shape only, **never the proposer's prior answer** |
| `reclassify.py::ReclassifyV1` | one independent opinion; `value=None` on failure or off-vocabulary output |
| `fusion.py::fuse(...)` | → `FusionV1(confidence, agreement)`; pure; a hard deterministic conflict **caps** confidence regardless of agreement (`_CONFLICT_CAP`); `coverage` scales how much LLM agreement is trusted (`_AGREE_COVERAGE_WEIGHT`) |
| `shadow_store.py`, `runner.py`, `report.py` | shadow observation and measurement |

Blindness is the load-bearing property: a critic shown the proposed answer agrees with it.

---

## 7. The gauntlet and the LLM seam

`REQUIREMENT_CODES` is a **closed** vocabulary (`feature_assist.py:110-114`): `TYPE_IS_NUMERIC`,
`GRAIN_IS_UNIQUE`, `TEMPORAL_IS_POPULATED`, `TEMPORAL_LAG_BOUNDED`, `JOIN_CONNECTIVITY`,
`UNIT_CONSISTENT`, `CURRENCY_CONSISTENT`, `ADDITIVITY_SUPPORTS_OPERATION`. `Requirement` is an
immutable value object validated against the versioned registry (`validation_requirements.py`).
`VALIDATION_STATES` is `DESIGN_CHECKED | NEEDS_EXTERNAL_VALIDATION | REJECTED`.

Requirements serialize onto the governed contract through
`contract/_serial.py::requirements_to_json` / `requirements_from_json`.

**Precedent for provenance travelling with output:** `materialize/classify.py:40` — "the requirement
travels with the artifact" — for `access_requirements`.

`intake/llm.py::drive_structured_call(client, request, validate_output, *, repair_budget,
retry_budget) -> StructuredCallOutcome` is the provider-agnostic, fail-closed call seam.
`DEFAULT_LLM_MODEL = "claude-sonnet-5"` (`llm.py:88`).

**No durable LLM result reuse exists.** `llm_dispatch` persists `redacted_input` and no response
(`1005_llm_dispatch_provenance.sql:19-34`); its key is per-attempt by design; `find_llm_call`
(`intake/llm.py:409-453`) has zero production callers and is `run_id`-scoped.

---

## 8. Measured baselines

Measured 2026-07-28 against the deployed demo cluster. These are facts about the current catalog,
not targets; the plan owns the targets.

| # | Measure | Value |
| --- | --- | --- |
| M1 | Columns carrying a concept | **120 / 126**, across 33 concepts |
| M2 | Tables with a VERIFIED grain and `is_unique = true` | **1 of 1** (`tran_id`) — 100%, but n=1 proves nothing about coverage |
| M3 | VERIFIED bridges creatable **through the product** | **0** — no route exists |
| M3a | Identifier columns eligible for bridge candidacy | **0 → 28** after `e6e5f31d` |
| M4 | Sources with attested schema | **1 of 1**, via the FTR/glossary path only; zero for CSV/Excel |
| M5 | Columns whose governed floor is stricter than the raw tag read-scope consults | **28** (16 `restricted`, 12 `confidential`) |
| M6 | Distinct `(concept, source)` pairs reachable in one cross-catalog query | **33** |
| M7 | Catalog sources loaded with a concept-bearing catalog | **1** (`ftr`) |

**M7 is the binding constraint.** With one source there is nothing to cross, and every
bridge-consuming phase is fixture-only regardless of how much is built.

**M5 is a live finding outside the cross-catalog programme.** Every read-scope predicate filters
`graph_node.sensitivity` — the raw file-declared tag, NULL for all 126 columns — while the governed
floor lands in `graph_node.effective_restriction`, whose only reader is `materialize/classify.py:204`.
Untagged is always visible, so **28 columns the system itself classified as sensitive are visible to
any caller with `catalog:read`**, including customer names, addresses, phone numbers and an Emirates
ID number. `read_scope.SENSITIVITY_ROLES` also knows only `{pii, restricted}`, so the 12
`confidential` columns have no grantable role even if the filter were switched over.
