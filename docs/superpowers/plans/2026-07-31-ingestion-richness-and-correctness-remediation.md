# Ingestion Richness and Correctness Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking. Do not implement relationship/bridge contracts here —
> `2026-07-29-bridge-cardinality-and-link-trust-remediation.md` owns them (its Tasks 0B–8 are
> deployed). Do not implement catalog/dataset profiles or temporal policy here —
> `2026-07-30-catalog-profiles-crosswalk-and-temporal-policy.md` owns them.

**Goal:** Make every uploaded column carry complete, correct, provenance-backed metadata — right
concepts in atomic namespaces, populated sensitivity/entity/currency/additivity axes, working
binding and stamp pipelines — and repair the live CIB/FTR catalogs through a governed
re-derivation, so feature generation, the data agent, and search consume rich information that is
actually true.

**Architecture:** Fix the vocabulary first (registry namespace repair), add a correctness loop to
enrichment (refute-oriented critic + deterministic format corroboration, reusing the deployed
`attest/` critic seam and the `1039` structured-result store), complete the missing per-column
axes as projections with honest provenance, un-stall the two broken pipelines (semantic bindings
126→0→0; table-fact stamps), then run one gated live re-derivation that retires the eight decoy
bridge candidates and brings both catalogs to the coverage targets. Correction is always
re-derivation of LLM-produced values — never mutation of human/uploader decisions.

**Tech Stack:** Python/FastAPI backend, PostgreSQL overlay event store, existing enrichment
(Pass A/B) + `attest/` critic machinery, data-agent observation executor, React frontend, kind
cluster deploy.

> **Revision 2026-07-31:** seventeen findings from an adversarial code-vs-plan review folded in.
> Load-bearing changes: the implementation baseline is the bridge branch merged into main (Task
> 0), Task 2 extracts `bridge_grounding`'s representation machinery instead of duplicating it,
> the new party concepts carry `sensitivity="pii"` so re-derivation can never weaken
> `visible_requires`, and the migration double-allocation between main and the bridge branch is
> a recorded fact to reconcile, not a rule to follow.
>
> **Revision 2026-07-31 (b):** the permanent fix is encoded as the **three-axis semantic model**
> (see “Semantic Axis Model”): `Concept.namespace` becomes the join-candidacy key (Task 1), a
> per-column advisory `party_role` axis is added (Tasks 1/3), and the namespace pairing rule for
> `derive_bridge_candidates` is written as a handoff to the bridge session, to land BEFORE the
> Task 6 re-derivation. Triggered by the live incident of 2026-07-31 09:54 UTC: one mislabeled
> column (`cust_swift_cd` → `counterparty_id`) × 8 mislabeled FTR columns minted 8 decoy
> cross-catalog links overnight.
>
> **Revision 2026-07-31 (c), post-merge deep dive:** the bridge branch is MERGED to
> `origin/main b5088f5d`, so the baseline gate is satisfied and Task 0 shrinks to confirmation.
> Two live code bugs absorbed: the relocated `entity_bridges` ingest stage has NO projection-lag
> gate (Task 5), and the binding-pipeline stall is a CODE bug — both
> `OVERLAY_SEMANTIC_BINDING_*` flags are "1" in `deploy/kind/k8s/20-backend.yaml:42-43` yet the
> proposal store has zero rows (Task 4). Cluster counts moved: 17 candidates / 11 open tasks
> (counterparty decoy family added). New edge rules: namespace issuer-scoping, deterministic
> handoff entity-pick, sticky-rejection non-interference, `bank` entity auto-registration,
> critic dispositions into the field-decision trail.
>
> **Revision 2026-07-31 (d), metadata-visibility review:** the AI summary parrots the definition
> because it drafts from file-side metadata mid-pipeline (fix: tail re-draft from the full
> column view — the cache auto-redrafts on richer payloads); four uploaded glossary fields
> (`term_type`, business processes L1–3, `related_terms`, physical FQN) are prompt-only and
> never durably saved (fix: source field-evidence); and the asset screen hides what we hold
> (declared type behind "unknown", NULL axes rendered blank, suggestions wired only to Search).
> Task 3 gains the persistence/summary steps; **new Task 3C: the column dossier UI**, consuming
> the validated `AssetDetailSampleScreen` prototype.
>
> **Revision 2026-07-31 (e):** **new Task 3D — Entity Map v0**, E0's semantic map pulled forward
> as a read-only screen over `available_identifier_links()` + `known_entities()`; one
> availability truth across map/governance/planner, richer after every task, zero rework. The
> full ontology explorer stays deferred.

## Global Constraints

- **Implementation baseline: `origin/main b5088f5d` — the merge is DONE** (merge commit
  `387165a6` + `fc6127da`, full suite 7260 green). Main now contains BOTH lines:
  `attest/bridge_critic.py`, `attest/bridge_grounding.py`, `structured_results.py`, the bridge
  stores, AND the analysis/data-agent stack. Both sides' independently allocated `1036`–`1038`
  migration sets coexist (filename-keyed ledger). **Verified 2026-07-31:** main's three
  (`1036_bridge_endpoint_canonical_orientation` — a data repair over the 0989 ledger;
  `1037_data_source_binding` — new connection/binding config tables;
  `1038_eligibility_policy` — new store) are content-independent of the bridge's four, safe in
  fresh-DB lexical order and against the cluster's applied set. The cluster still runs the
  PRE-merge build and has applied only the bridge set — **main's three apply for the first time
  at Task 6's deploy**; the pre-flight lists them. The two binding-ish stores are complementary,
  not duplicates: `data_source_binding` is CONFIG (connection = reviewed access grant; binding =
  address naming a connection; `secret_ref` resolved at use), `physical_dataset_binding_revision`
  is the immutable content-addressed REVISION observations/realizations cite — config feeds
  revision-minting (Task 7); never “unify” them. New migrations allocate ABOVE `1039` via the
  shared verified-interface document; never self-allocate.
- Immutable decisions: an LLM re-run may stale only LLM-produced suggestions; human-confirmed and
  uploader/source-declared facts survive (value-diff semantics in `_assert_fact` /
  `proposal_commands`).
- Display never becomes authority: `graph_node.sensitivity` is a display projection;
  `visible_requires` remains the only read-scope enforcement. `entity`/`additivity` projections
  carry provenance and never bypass governed fact gates.
- No cluster mutation (upload, re-enrich, deploy, task closure) without explicit user approval —
  Tasks 6 and 7 are gated.
- No free-text becomes executable; all new vocabulary is closed enums/registry entries.
- Every stage that can produce zero outputs must report a reason (`succeeded {n:0, reason}` |
  `skipped` | `failed`) — silent no-ops are a defect class this plan closes.

---

## Semantic Axis Model (the permanent fix)

The recurring defect class is one `concept` slot doing four jobs (value kind, business referent,
value space, table role). The permanent model splits the join-relevant meanings into three axes:

| Axis | Values (examples) | Lives on | Gates |
| --- | --- | --- | --- |
| **Entity type** | customer, financial_institution, account, transaction, branch | `Concept.entity_link` (exists) | meaning, ontology, display |
| **Identifier namespace** | `cif`, `swift_bic`, scheme-scoped correspondent codes, `iso20022_end_to_end`, `swift_uetr`, `core_serial`, `internal_account` | `Concept.namespace` (**new field, Task 1**) | **join candidacy — the ONLY load-bearing axis for pairing** |
| **Party role** | subject, sender, receiver, intermediary, reimbursement, counterparty | per **column** (`graph_node.party_role`, Task 3) | explanation, feature naming — advisory, never a join gate |

Rules:

1. **A namespace is defined by value equality:** two columns share a namespace iff equal values
   denote the same thing. A US ABA number and a UK sort code both look like "correspondent
   codes" but never share a namespace — scheme-scoped namespaces, not one bucket. The general
   form: **a namespace names ONE ISSUER's value space** — `cif` means THIS bank's CIF registry;
   the moment a second institution's catalog arrives, issuer-scope the namespace
   (`cif@<institution>`) or two banks' customer numbers will pair as joinable. Single-issuer
   names are acceptable only while the platform holds one issuer's catalogs.
2. **"Counterparty" is a role, never an entity type.** A counterparty IS a customer or a
   financial institution; what makes it a counterparty is its position relative to the row's
   subject. Putting it on the entity axis is how `counter_party_cif_id` and `sender_bic` got
   grouped as "the same kind of thing".
3. **Namespace lives on the concept** (every `bank_bic` column shares it); **role lives on the
   column** (`sender_bic` vs `receiver_bic` share a concept and differ only in role). Folding
   role into concepts (`bank_bic_sender`, …) is the combinatorial explosion this model escapes.
4. **Pairing rule** (bridge-session handoff, below): link candidates derive from SAME NAMESPACE
   in distinct catalogs; entity corroborates and explains; role explains. Expected on live data:
   all 8 counterparty decoys become impossible (BIC↔CIF, BIC↔scheme-code cross-namespace); the
   CIF group yields `cust_num ↔ cif_id` AND the previously-missed subset link
   `cust_num ↔ counter_party_cif_id` ("counterparties who are our customers"); the BIC group
   yields honest bank links whose role axis names each one's meaning.
5. Each axis has a named consumer and a must-die mutation (Task 8); an axis without a consumer
   is not added.

## Verified Audit Baseline (kind cluster, 2026-07-31)

All numbers verified read-only this date. Task 0 re-verifies and freezes them in the audit script.

**Inventory:** 2 tables, 237 columns (`ftr` 126, `cib` 111). Real schemas present
(`BO_DPL_CIB`, `DPL_EIB_COMPLIANCE`). **Post-merge:** `origin/main b5088f5d` contains the whole
bridge line (through "execute exact directional realizations" / "expose realization trust
evidence"); Tasks 1–5 implement directly on main. The deployed image is still the PRE-merge
bridge build — its migrations `1036`–`1039` (bridge set) are applied; main's `1036`–`1038`
apply at Task 6's deploy. In the merged ingest, `entity_bridges` runs AFTER
`table_fact_projection` — **but with NO projection-lag gate** (`table_fact_projection` may
record `lagged` and skip while `entity_bridges` still assesses against stale grain; Task 5 owns
the gate).

**Coverage (columns):**

| Axis | ftr | cib | Verdict |
| --- | --- | --- | --- |
| concept / definition / domain / semantic_terms | 126 | 111 | complete |
| entity | 0 | 0 | **empty everywhere** |
| sensitivity (display axis) | 0 | 0 | **empty** (enforcement OK: `visible_requires` = 188 `{}`, 18 `{confidential}`, 33 `{restricted}`; all 20 pii/kyc columns `{restricted}`) |
| attested `data_type` | 0 | 0 | **all `unknown`**; `declared_type` 126 / 109 (missing: `cust_buy_rate`, `cust_sell_rate`) |
| unit / currency | 0 | 0 | **none governed** (6 monetary columns) |
| additivity | 6 | 7 | 13/237 |
| ai_summary | 126 | 0 | **CIB never re-enriched** since stage shipped |
| is_grain flags | 1 | 2 | present |

**Governed facts:** VERIFIED `grain` ×2 (`ftr`: `["tran_id"] unique`; `cib`:
`["business_dt","cust_num"] unique`) and VERIFIED `availability_time` ×2 (`ftr`:
`posted_at/pstd_date`; `cib`: `ingested_at/business_dt` — basis questionable). **But both table
nodes have NULL `grain_fact_event_id` / `availability_fact_event_id`** — stamps not projected.

**Task 0 addendum (2026-07-31 late, script run against the redeployed cluster):** the merged
build was deployed and CIB/FTR re-ingested after this baseline was written. Drift captured by
`scripts/verify_catalog_richness.sql`: **all seven `1036`–`1039` migrations are APPLIED** (the
interleave resolved empirically — Task 6's pre-flight item is satisfied early); `ai_summary` is
now 237/237 but still drafted from the FILE-SIDE payload (the parrot defect stands — Step 6c's
quality target unchanged); `visible_requires` now `{restricted}`×37 / `{confidential}`×18 /
`{}`×182; `coverage_concept` cib 110/111; the `branch_id` conflation shrank to 4 columns (the
registry's `branch_name` fix took effect for CIB name columns on re-ingest — but `sol_desc`
is STILL `branch_id`, the exact desc-as-identifier case Task 2's critic exists to refute);
4 stamp-drift rows (both tables × grain+availability) confirm Task 5 live. Candidates 17,
tasks 11, bindings 126/0/0 — unchanged.

**Pipelines:** semantic bindings **126 candidates → 0 proposals → 0 edges** — and BOTH
`OVERLAY_SEMANTIC_BINDING_CANDIDATES`/`_PROPOSALS` are `"1"` in
`deploy/kind/k8s/20-backend.yaml:42-43`, so this is a CODE defect, not configuration (Task 4).
Bridge candidates as of 2026-07-31 16:00Z: **17** — 8 branch decoys + 8 counterparty decoys
(minted 09:54Z by `cust_swift_cd`→`counterparty_id`) + `cust_num <-> cif_id`; **11 open human
tasks** (the branch's task reconciler consolidated some). Decoy retirement in Task 6 covers BOTH
families at whatever the live count is at run time.

**Concept namespace conflations (live `graph_node` data):**

| Concept | Columns | Defect |
| --- | --- | --- |
| `counterparty_id` | `counter_party_cif_id` (correct) + `counter_party_bic`, `sender_bic`, `receiver_bic`, `corres_bank_intermediary_bic`, `corres_bank_receiver_code`, `corres_bank_sender_code`, `third_reimb_inst_code` | BICs identify **banks**; clearing codes are a third namespace |
| `branch_id` | `sol_desc` (+ correct `tran_branch_sol_id`) | a description classified as an identifier — source of all 8 decoy links |
| `account_id` | `foracid` (internal) + `counter_party_acct_num` (external bank's account) + `va_account_number` (virtual) + `hsmi_customer_acct_nbr` | ≥3 namespaces |
| `transaction_id` | 11 cols incl. `uetr`, `e2e_id`, `sw_srl_num`, `sender_ref_num`, `clearing_system_member_reference_id`, `alt_tran_ref_num`, `part_tran_srl_num` | every payment-reference namespace conflated |
| `pii` | 17 name/address/phone cols incl. `cust_name`, `counter_party_address` | a sensitivity class occupying the semantic slot |
| `category_code` | `module_id`, `reason_for_excluding` | catch-all |

**Registry state:** `concepts.py` already contains the branch fix (`branch_name`, categorical,
"a NAME is not an IDENTIFIER… never a join key", ~lines 952–964) — **but the live data was never
re-derived**, so `sol_desc` still carries `branch_id` and the decoys stand.

**Table-level:** `bo_cib_customer` `table_role=dimension`, `primary_entity` **blank**;
`comp_financial_tran_repos_dly` `event_fact`/`transaction`. No table descriptions (profiles plan
owns the fix).

**Answered en route:** `actual_counter_party_amt` (concept `monetary_flow`, correct) is proposed
as a join key by NO governed store; it appears only as a measure operand in 13
`overlay.feature.recommend` LLM outputs.

## Relationship to Existing Plans

- **Bridge remediation (deployed 0B–8):** owns link/realization contracts, grounding
  (`attest/bridge_grounding.py`, representation roles), bounded enumeration, `bridge_critic.py`,
  observation store `1038`, structured-result store `1039`. This plan REUSES those seams and
  retires the decoy data its grounding already refuses to re-create.
- **Catalog profiles (finalized, not started):** owns catalog/dataset profiles, authority roles,
  temporal policy, profile critic, search/prompt wiring of profile text. This plan delivers the
  **column-level** correctness+completeness its consumers assume, and its coverage targets are
  the profiles plan's data floor. Where both name a critic, both reuse the same `attest/` pattern;
  neither replaces the other's.
- **Governance-review redesign:** owns the queue; Task 6 retires decoy tasks through its existing
  reject path, never by DB deletion.

---

### Task 0: Freeze the Audit Baseline and Allocation

**Files:**
- Create: `scripts/verify_catalog_richness.sql`
- Update: the shared verified-interface document (migration + ownership ledger)
- No product code

**Interfaces:**
- Produces: one psql-executable script whose output reproduces every number in “Verified Audit
  Baseline” as `metric|value` rows; later tasks diff against it.

- [ ] **Step 1: Write the audit script** — read-only SQL producing, by name: per-catalog column
  counts and per-axis coverage (concept, definition, domain, entity, sensitivity, real
  `data_type`, `declared_type`, unit, currency, additivity, ai_summary, is_grain, is_as_of);
  `visible_requires` distribution; overlay fact-type/status counts; table-node stamp presence vs
  overlay VERIFIED facts; binding candidate/proposal/edge counts; bridge candidate list; open
  human-task count; the six-row concept-conflation listing (counterparty_id/branch_id/account_id/
  transaction_id/pii/category_code with their columns).
- [ ] **Step 2: Run it against kind read-only; confirm byte-agreement with the baseline table.**
  Any drift updates THIS plan's baseline section before any other task starts.
- [ ] **Step 3: Confirm the merged baseline (the merge is DONE — `origin/main b5088f5d`).**
  Verify main contains both lines: `attest/bridge_critic.py`, `attest/bridge_grounding.py`,
  `overlay/upload/structured_results.py`, the bridge migrations `1036`–`1039`, AND main's
  analysis stack + its `1036`–`1038`. This is a confirmation, not a wait.
- [ ] **Step 4: Record the migration reconciliation (pre-verified 2026-07-31, re-confirm).**
  The two `1036`–`1038` sets are content-independent — main's three are a 0989-ledger data
  repair, new `data_source_binding`/connection config tables, and the new eligibility store —
  and safe in both fresh-DB lexical order and the cluster's order (bridge set applied; main's
  three pending until Task 6's deploy). Record the verdict + interleaved order in the shared
  verified-interface doc, including the config→revision pairing of `data_source_binding` and
  `physical_dataset_binding_revision` (complementary, never to be unified).
- [ ] **Step 5: Record in the shared verified-interface doc:** merged-baseline SHA, deployed
  image/commit, merged migration maximum, and this plan's reservation (the next block ABOVE that
  maximum; do not self-number).
- [ ] **Step 6: Commit** the script + doc update.

**Acceptance:** one command reproduces the audit; merged baseline named and verified; migration
interleave recorded as safe; allocation recorded; zero product-code changes.

---

### Task 1: Concept Registry Namespace Repair

**Files:**
- Modify: `src/featuregen/overlay/upload/concepts.py`
- Modify: the Pass-A concept prompt vocabulary source (same module/enumeration the prompt renders)
- Test: `tests/featuregen/overlay/upload/test_concepts_registry.py` (extend)

**Interfaces:**
- Produces: new `Concept(...)` registry entries (exact names below) consumed by re-derivation
  (Task 6), the critic (Task 2), and axis projections (Task 3). No schema change.

- [ ] **Step 1: Write failing registry tests** pinning the new vocabulary:

```python
def test_bank_identifiers_are_not_counterparty_ids():
    for name in ("bank_bic", "clearing_member_code"):
        c = concept(name)
        assert c.group == "identifier" and c.entity_link == "bank"

def test_payment_reference_namespaces_are_atomic():
    for name in ("swift_uetr", "end_to_end_reference", "clearing_system_reference",
                 "channel_reference", "internal_transaction_serial"):
        c = concept(name)
        assert c.group == "identifier" and c.entity_link == "transaction"

def test_account_namespaces_are_split():
    assert concept("external_account_ref").entity_link == "account"
    assert concept("virtual_account_id").entity_link == "account"

def test_party_semantics_replace_pii_as_concept():
    # CRITICAL (PII-floor survival): visible_requires derives partly from the concept-driven
    # sensitivity floor (taxonomy_evidence.py sensitivity_floor + migration 1032). The new
    # concepts MUST carry the pii sensitivity class, or re-deriving pii->party_name would
    # weaken the floor on 17 live columns.
    for name in ("party_name", "postal_address", "phone_number", "email_address"):
        c = concept(name)
        assert c.group != "identifier"
        assert c.sensitivity == "pii"          # Concept.sensitivity — the EXISTING field
    # pii remains a valid legacy concept but is documented as sensitivity-class-only

def test_every_identifier_concept_has_entity_link():
    from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY  # no all_concepts() exists
    for c in CONCEPT_REGISTRY.values():
        if c.group == "identifier":
            assert c.entity_link, c.name

def test_namespace_axis_is_identifier_only_and_complete():
    # THE PERMANENT FIX: every identifier concept declares its value space; nothing else may.
    for c in CONCEPT_REGISTRY.values():
        if c.group == "identifier":
            assert c.namespace, c.name          # join candidacy has no meaning without it
        else:
            assert c.namespace is None, c.name  # namespace on a non-identifier is a modeling bug

def test_cif_namespace_spans_customer_and_counterparty_concepts():
    # counter_party_cif_id (concept counterparty_id) and cust_num (customer_id) hold the SAME
    # bank's CIF values — same namespace, different entity/role. This is what surfaces the
    # missed "counterparties who are our customers" link and kills the BIC↔CIF decoy.
    assert concept("customer_id").namespace == concept("counterparty_id").namespace == "cif"
    assert concept("bank_bic").namespace == "swift_bic"
    assert concept("bank_bic").namespace != concept("clearing_member_code").namespace

def test_vocabulary_fingerprint_changes_with_names():
    # enrich.py folds a NAMES-ONLY vocabulary fingerprint into the concept cache key. Adding
    # these concepts must change it (=> full cache re-derivation); pin that here so a future
    # description-only registry fix is KNOWN not to bump the cache by itself.
    from featuregen.overlay.upload.enrich import _vocab_fingerprint  # actual name per enrich.py:79
    assert "bank_bic" in CONCEPTS and _vocab_fingerprint() != KNOWN_PRE_TASK1_FINGERPRINT
```

- [ ] **Step 2: Run; verify FAIL** (unknown concept names, missing `namespace` field).
- [ ] **Step 3: Add `namespace: str | None = None` to the `Concept` dataclass** and declare it on
  EVERY existing identifier concept (`customer_id`/`counterparty_id` → `"cif"`; `account_id` →
  `"internal_account"`; `transaction_id` → `"core_serial"`; `branch_id` → `"branch_sol"`;
  `lei` → `"lei"`; `merchant_id` → `"merchant_scheme"`; …exhaustive — the completeness test
  enforces it). Registry import-time validation: `namespace` set ⟺ `group == "identifier"`.
- [ ] **Step 3b: Add the new registry entries.** Each with its namespace and a disambiguating
  description the prompt renders — descriptions are the LLM's routing signal, so they must
  state the negative:

```python
Concept("bank_bic", "identifier", entity_link="bank", namespace="swift_bic",
        description="SWIFT BIC of a BANK (8/11 alphanumeric). Identifies the institution, "
                    "never the counterparty person/company — a counterparty's CIF is "
                    "counterparty_id; the bank's code is this."),
Concept("clearing_member_code", "identifier", entity_link="bank",
        namespace="correspondent_scheme_code",
        description="A clearing/correspondent scheme member code (national or scheme-local). "
                    "Bank-level, scheme-scoped; not a BIC and not a counterparty. Split into "
                    "per-scheme namespaces the moment a second scheme appears — equal values "
                    "across schemes mean nothing."),
Concept("swift_uetr", "identifier", entity_link="transaction", namespace="swift_uetr",
        description="SWIFT gpi UETR — a UUID tracing one payment end-to-end. Its own "
                    "namespace; never equal to an internal transaction id."),
Concept("end_to_end_reference", "identifier", entity_link="transaction",
        namespace="iso20022_end_to_end",
        description="ISO 20022 EndToEndId assigned by the initiating party. Distinct from "
                    "UETR, scheme references and internal serials."),
Concept("clearing_system_reference", "identifier", entity_link="transaction",
        namespace="clearing_system_ref",
        description="The clearing system's own reference for the instruction."),
Concept("channel_reference", "identifier", entity_link="transaction", namespace="channel_ref",
        description="A channel-assigned reference (branch/mobile/host). Channel-scoped."),
Concept("internal_transaction_serial", "identifier", entity_link="transaction",
        namespace="core_serial",
        description="A core-banking internal serial/partition number. Meaningless outside "
                    "its own system."),
Concept("external_account_ref", "identifier", entity_link="account",
        namespace="external_account",
        description="An account held AT ANOTHER INSTITUTION (e.g. a counterparty's account "
                    "number). Never joinable to internal account_id."),
Concept("virtual_account_id", "identifier", entity_link="account", namespace="virtual_account",
        description="A virtual/shadow account identifier issued for reconciliation."),
Concept("party_name", "sensitive", sensitivity="pii",
        description="A person or organisation NAME. Names display and group; they are "
                    "never identifiers and never join keys."),
Concept("postal_address", "sensitive", sensitivity="pii",
        description="A postal address. Sensitivity-bearing free text; never a key."),
Concept("phone_number", "sensitive", sensitivity="pii",
        description="A telephone/mobile number. Contact detail; not an identifier concept "
                    "for joining."),
Concept("email_address", "sensitive", sensitivity="pii",
        description="An e-mail address. Contact detail; not a join identifier."),
Concept("module_id", "categorical",
        description="A source-system module/product code (which subsystem produced the row). "
                    "System-scoped categorical; not a business category and not a key."),
```

  Groups come from the documented set in `concepts.py:28-33` (`sensitive`, `categorical`,
  `text`, …) — there is no `contact`/`descriptive` group. `sensitivity="pii"` is the existing
  `Concept.sensitivity` field (`"public"|"pii"|"protected_attribute"|"special_category"|"proxy"`),
  already consumed by `templates.py` `_BLOCKED_SENSITIVITIES` — it is the mechanism that keeps
  the `visible_requires` floor intact through re-derivation (see the CRITICAL test above; the
  legacy `pii` concept already carries exactly this shape at `concepts.py:201`).
  `module_id` retires the `category_code` catch-all on `module_id`; `reason_for_excluding` under
  `category_code` is accepted residue, recorded in the audit script.
  **The `bank` entity auto-registers:** `known_entities()` (`taxonomy/dimensions.py:106`) derives
  the valid-entity set from the distinct `Concept.entity_link` values, so `bank_bic`'s
  `entity_link="bank"` registers it — add NO parallel registration. The rollup relationship
  registry deliberately gains no bank relationships (nothing rolls up through a bank).
- [ ] **Step 4: Amend the `pii` concept description** to state it is a sensitivity class kept for
  compatibility, and that names/addresses/phones should carry the party_* concepts; amend
  `counterparty_id`, `account_id`, `transaction_id`, `branch_id` descriptions with the explicit
  negatives ("a BIC is bank_bic, not this", "an external bank's account is
  external_account_ref, not this", "UETR/E2E/clearing refs have their own concepts", "a branch
  NAME/DESCRIPTION is branch_name, not this").
- [ ] **Step 4b: Define the closed `PartyRole` vocabulary + normalizer** (this plan's
  vocabulary module): `subject | sender | receiver | intermediary | reimbursement |
  counterparty` — `None` for off-vocab/ambiguous, mirroring `table_vocab`'s
  strip/lower/exact-token style. Token rules are deterministic (`sender`→sender,
  `receiver`/`recvr`→receiver, `intermediary`→intermediary, `reimb`→reimbursement,
  `counter_party`/`counterparty`→counterparty; the row-subject's own columns → `subject`).
  Advisory by contract: nothing may consume `party_role` as a join or execution gate.
- [ ] **Step 5: Run tests; PASS. Run the full registry validation suite** (aliases disjoint,
  taxonomy intact; namespace ⟺ identifier). Check the Pass-A prompt renders the grown
  vocabulary within its size bounds (~281 → ~295 concepts; if a bound exists in
  `enrich_config`, assert headroom).
- [ ] **Step 6: Commit** `feat(registry): atomic identifier namespaces — banks, payment refs, accounts, party semantics`.

---

### Task 2: Enrichment Concept Critic + Format Corroboration

**Files:**
- Create: `src/featuregen/overlay/upload/attest/concept_critic.py`
- Create: `src/featuregen/overlay/upload/attest/representation.py` — **EXTRACTED from
  `attest/bridge_grounding.py`, not written fresh.** That module (bridge branch, on the merged
  baseline) already implements `RepresentationRole`, `_representation_role()` (exact-token
  name/definition detection — its comment names the substring bug it avoids),
  `_observed_format()`, and `type_family`. Promote those to this shared module; make
  `bridge_grounding` import from it (behavior-identical move, its tests pin that); do NOT leave
  two rulesets to drift.
- Modify: `src/featuregen/overlay/upload/enrich.py` (Pass-A acceptance hook)
- Modify: `src/featuregen/overlay/upload/ingest.py` (stage recording only; post-merge layout —
  the bridge branch moved these seams by ~141 lines vs main)
- Reuse: `attest/bridge_critic.py` — mirror `critique_identifier_link()`'s shape: typed result
  dataclass + closed reason codes; `overlay/upload/structured_results.py` (the `1039` store's
  module) for replay; `feature_assist.py:1015-1157` loop shape
- Test: `tests/featuregen/overlay/upload/attest/test_concept_critic.py`
- Test: `tests/featuregen/overlay/upload/attest/test_representation.py`

**Interfaces:**
- Produces (in `attest/representation.py`): the moved `RepresentationRole`,
  `representation_role(...)` (public now), `observed_format(...)`, `type_family(...)` — plus the
  NEW namespace-shape extension `shape_conflicts(column_name, declared_type, definition,
  concept) -> tuple[str, ...]` returning closed codes
  (`identifier_namespace_mismatch` | `name_or_description_not_identifier` |
  `measure_not_identifier`). The shape checks (BIC 8/11-alphanumeric, UUID/UETR) are the only
  genuinely new logic — everything else is the extraction.
- Produces (in `attest/concept_critic.py`): `critique_concept_batch(client, items, *,
  catalog_revision) -> dict[ref, ConceptCriticResultV1]` mirroring `BridgeCriticResultV1`.
- Consumed by: Pass-A acceptance (this task) and Task 6's re-derivation run.

- [x] **Step 1: Extraction first (behavior-identical move).** Move
  `RepresentationRole`/`_representation_role`/`_observed_format`/`type_family` from
  `bridge_grounding.py` to `attest/representation.py`; `bridge_grounding` re-imports. Run the
  existing bridge-grounding suite unchanged — it is the proof the move changed nothing. Commit
  the move separately.
- [x] **Step 2: Write failing tests for the NEW `shape_conflicts` extension:**

```python
def test_bic_shape_refutes_counterparty_id():
    c = shape_conflicts("counter_party_bic", "string",
                        "SWIFT BIC of the counterparty bank", "counterparty_id")
    assert "identifier_namespace_mismatch" in c    # bic-shaped name+definition vs party concept

def test_desc_suffix_refutes_identifier():
    # delegates to the MOVED representation_role: sol_desc -> DESCRIPTION_TEXT
    c = shape_conflicts("sol_desc", "string", "Branch description", "branch_id")
    assert "name_or_description_not_identifier" in c

def test_amount_refutes_identifier():
    c = shape_conflicts("actual_counter_party_amt", "double",
                        "Actual counterparty amount", "counterparty_id")
    assert "measure_not_identifier" in c

def test_clean_identifier_passes():
    assert shape_conflicts("cif_id", "string", "Customer CIF", "customer_id") == ()
```

  Only the shapes are new (BIC 8/11 alphanumeric token, UUID/UETR, numeric-family-vs-identifier);
  the name/description/label detection is the moved token logic, not re-implemented. No regex
  over free SQL; no network.
- [x] **Step 2b: Run; FAIL on the new codes only. Implement the extension; PASS.**
- [x] **Step 3: Write failing critic tests** (mock LLM via the existing enrich client seam):
  a batch where the critic must (a) refute `branch_id` on a description column citing the
  representation conflict, (b) uphold `customer_id` on `cif_id`, (c) abstain on genuinely
  ambiguous, (d) never emit a concept outside the registry, (e) be replay-deterministic via the
  `1039` store.
- [x] **Step 4: Implement `concept_critic.py`** following `bridge_critic.py`
  (`critique_identifier_link` → typed result + closed reason codes; replay through
  `structured_results.py`): deterministic `shape_conflicts` are computed FIRST and handed to the
  critic as evidence; the critic's question is refute-oriented ("given only this evidence, is
  the assignment supported?"); one revise pass for flagged items (the feature_assist loop
  shape); per-field disposition `accepted | revised | refuted | abstained`.
- [x] **Step 5: Hook Pass-A acceptance — with the re-derivation rule stated exactly.** An
  identifier-group assignment that ends `refuted` never persists; the column's concept resolves,
  in order: the critic's revise-pass result if accepted → a non-identifier abstain
  (`unclassified` disposition recorded) → **never silent retention of a previously-stored wrong
  identifier**. This ordering is what lets Task 6 actually evict `branch_id` from `sol_desc`
  even if the LLM re-proposes it: refuted ⇒ the old value is superseded by the abstain, not
  protected by it. Refuted items record their conflict codes in the stage detail **and in the
  column's field-decision trail** — the replacement decision (via the existing decision-log
  machinery behind `concept_decision_id`) carries the critic's conflict codes as its reason, so
  the audit answers "why did this column's concept change" from the column, not only from one
  run's stage detail. Non-identifier fields pass through unchanged this slice.
- [x] **Step 6: Stage honesty:** record `enrich_concept_critic` with counts
  accepted/revised/refuted/abstained; zero items → `not_applicable`, never silent.
- [x] **Step 7: Run enrichment suite; commit**
  `feat(enrich): refute-oriented concept critic + deterministic representation corroboration`.

---

### Task 3: Complete the Per-Column Axes (sensitivity display, entity, additivity, gaps)

**Files:**
- Modify: `src/featuregen/overlay/upload/ingest.py`
- Modify: `src/featuregen/overlay/upload/enrich.py` / Pass-B seam (additivity defaulting)
- Create: `src/featuregen/overlay/upload/axis_projection.py`
- Modify: `src/featuregen/overlay/upload/search.py` (sensitivity facet now populates — no logic
  change, verify only)
- Test: `tests/featuregen/overlay/upload/test_axis_projection.py`
- Add migration (from the reserved block): `graph_node.party_role text NULL` — the per-COLUMN
  advisory axis of the Semantic Axis Model; no other schema change expected

**Interfaces:**
- Produces: `project_display_axes(conn, catalog_source) -> AxisProjectionReport` — idempotent,
  called at the end of ingest and by the Task 6 repair run.
  `AxisProjectionReport(sensitivity_set, entity_set, additivity_set, skipped)`.

- [x] **Step 1: Write failing tests:**

```python
def test_sensitivity_display_projected_from_enforcement():
    # a column with visible_requires={restricted} and NULL sensitivity gets display 'restricted'
    # a column with {} and a pii/kyc/party concept gets the concept default
    # enforcement (visible_requires) is NEVER written by this projection

def test_entity_projected_from_concept_link_with_provenance():
    # identifier column with concept.entity_link='customer' -> graph_node.entity='customer'
    # provenance recorded as concept_derived; an existing entity_fact/human value is untouched

def test_additivity_defaults_from_concept():
    # monetary_flow column with NULL additivity -> 'additive' (concept default), provenance
    # concept_derived; an uploaded/human additivity is never overwritten

def test_projection_is_idempotent_and_scoped():
    # running twice changes nothing; running for 'ftr' never touches 'cib'
```

- [x] **Step 2: Run; FAIL. Implement `axis_projection.py`:** three UPDATE passes, each guarded
  `WHERE <axis> IS NULL` (display fill-in only), each stamping its provenance via the existing
  decision-id columns' conventions (`sensitivity_decision_id` etc. stay NULL — these are
  projections, not decisions; record provenance in the projection report + stage detail instead).
  Sensitivity precedence: `visible_requires` non-empty → its strongest label; else the
  **existing `Concept.sensitivity` field** (`"pii"`/`"special_category"`/`"protected_attribute"`
  → `restricted`; `"proxy"` → `confidential`) — do NOT build a new mapping table; the registry
  field already exists and `templates.py` `_BLOCKED_SENSITIVITIES` already consumes it; else
  leave NULL (unknown is honest).
  **Built as a NEW `graph_node.sensitivity_display` column (migration 1042), not
  `graph_node.sensitivity`:** the existing column is the read-scope TAG — its 0993 CHECK forbids
  `confidential`, materialize/classify fails closed on any tag outside `SENSITIVITY_ROLES`, and
  1032 GENERATES `visible_requires` from it, so writing display labels there would have changed
  enforcement (exactly what this task's own must-die mutation forbids). Tests pin
  `visible_requires` + the raw tag byte-identical across a projection run; the audit script's
  `coverage_sensitivity_display` now counts the new column (`coverage_sensitivity_tag` keeps the
  raw-tag count).
- [x] **Step 3: Entity pass:** for identifier-group concepts with `entity_link`, fill
  `graph_node.entity` when blank. This is display/planning context, NOT an `entity_assignment`
  fact — the governed fact path stays the Delivery-E command; the projection must skip any column
  with `entity_fact_key` set.
- [x] **Step 3b: Party-role pass (deterministic, advisory):** fill `graph_node.party_role` from
  Task 1's `PartyRole` token normalizer over the column name — `sender_bic → sender`,
  `third_reimb_inst_code → reimbursement`, `counter_party_cif_id → counterparty`,
  `cust_swift_cd → subject` (the row-subject's own attribute) — abstaining (`NULL`) on
  ambiguity rather than guessing. Deterministic-first by design: the tokens carry the answer for
  every live role column; an LLM fallback for ambiguous names is deferred until a real column
  needs it. Tests: the five mappings above; an ambiguous name stays NULL; nothing anywhere
  consumes `party_role` in a join/candidacy/execution predicate (import-gate style assertion).
- [x] **Step 4: Wire into ingest tail** (post-merge layout — the bridge branch moved these
  seams) + record stage `axis_projection` with the report counts. **Search-doc coherence:**
  `entity` is an input to the single `_SEARCH_DOC` expression (`graph.py` — insert-time and
  `rebuild_search_doc` render the same expression, invariant #20). Every node whose `entity`
  this projection changes gets `rebuild_search_doc(conn, catalog_source, object_ref)` called;
  the test asserts an entity-projected node's rebuilt doc equals a fresh-insert doc.
- [x] **Step 5: Declared-type gap honesty:** ingest result lists columns with NULL
  `declared_type` and NULL attested type (`cust_buy_rate`, `cust_sell_rate` today) as
  `type_unknown` items in the existing per-stage detail, so the uploader sees the fix list.
- [x] **Step 6: Pass-B re-synth check:** confirm `primary_entity` proposal for `bo_cib_customer`
  resolves (`customer` is in the entity registry); no code change expected — add the regression
  test that a dimension table with a customer grain proposes `primary_entity=customer`.
- [x] **Step 6b: Persist the four lost glossary fields as source evidence.** The A1 adapter
  captures all 17 CSV headers, but `_SOURCE_FIELDS` (`ingest.py:895`) durably persists only
  definition/domain/business_term/bian_path/fibo_path — `term_type`,
  `process_path` (business processes L1–3), `related_terms`, and the physical FQN survive only
  inside `llm_call` payloads. Extend the source-evidence write (`ingest.py:1165` region) with
  those four as `source_attested` field evidence. Tests: each round-trips from a fixture upload
  to readable field evidence; absent-in-file stays absent (no fabricated empties).
- [x] **Step 6c: Re-draft the AI summary from the FULL enriched view at the ingest tail.**
  `draft_summaries` currently runs mid-pipeline from `_concept_metadata` (file-side fields
  only), so it can only paraphrase the definition. Move (or second-pass) the summary draft to
  the ingest tail, fed by the assembled column dossier — concept, domain, synonyms, term type,
  business processes, related terms, taxonomy paths, grain/as-of role, table role, `party_role`
  — reusing `column_view.py` as the assembler. NO cache surgery needed: `_summary_cache_key`
  hashes the metadata payload, so the richer payload re-drafts every column by construction.
  Tests: the summary prompt payload contains the enriched fields; a definition-only payload and
  the enriched payload produce different cache keys; `definition` is never overwritten.
- [x] **Step 7: Run suites; commit**
  `feat(ingest): sensitivity/entity/additivity display axes projected with provenance`.

---

### Task 3C: The Column Dossier — Asset Detail Shows Everything We Hold

**Purpose:** clicking a column answers everything end-to-end. Today the screen hides information
the platform already stores: the declared SQL type sits behind "unknown", the taxonomy sidecar
never renders, suggested features are wired only to SearchScreen, and NULL axes render as blank
sections instead of provenance-labelled proposals.

**Files:**
- Modify: `src/featuregen/overlay/upload/asset_detail.py` (add the source-glossary evidence
  fields + suggestion refs to the column payload)
- Modify: `frontend/src/screens/AssetDetailScreen.tsx`, `frontend/src/api.ts`
- Reuse: the untracked `frontend/src/screens/AssetDetailSampleScreen.tsx` prototype — the
  already-validated design target for this screen; consume it, do not restart
- Test: backend payload + frontend screen suites

**Boundary:** the profiles plan's Task 5 later adds catalog/dataset-profile fields to this same
screen; this task owns the COLUMN-level dossier only. Same screen, disjoint sections.

**Steps:**

- [x] **"From the source glossary" section**, product names not CSV headers: Business term,
  Term type, Business processes (L1 → L2 → L3 as one path), Related terms, BIAN / FIBO
  classification, Declared type, Physical path. Read from the source field-evidence Task 3
  Step 6b persists; every value carries its `source_attested` provenance chip.
  (New `source_glossary` payload section — newest ACTIVE `producer='source'` evidence per field,
  empty-not-fabricated; the screen's section renders product names + the source chip and is
  absent when nothing was declared.)
- [x] **Type display policy:** when `operational_type` is unknown and `declared_type` exists,
  render `varchar(50) · declared` (basis chip), never a bare "unknown"; an attested type (Task
  7) upgrades the chip. The bare word "unknown" appears only when we genuinely hold nothing.
  (Policy lives in the READ MODEL — the `type` field gains `basis`
  operational/declared/null and the declared value backs it — so every consumer gets it; the
  identity Type section renders the headline + basis chip and the operational slot says
  "— not attested yet", reserving bare "unknown" for a column holding nothing.)
- [x] **AI-proposed values instead of blanks** (standing product direction: AI-proposed is
  usable, never framed as failure): for additivity/unit/currency/entity, render the governed
  value when present, else the LLM-proposed value with an "AI-proposed · unconfirmed" chip
  (E4a unit proposals included), else an explicit "nothing known yet" — a NULL axis must be
  distinguishable from a hidden one. (Every effective_metadata field now carries
  `proposed_value` — the newest ACTIVE evidence's value — and the tri-state axis rows render
  value/proposal/"nothing known yet"; `sensitivity_display` + `party_role` joined the axes,
  labelled "system projected" when the deterministic projection filled them.)
- [x] **Suggested features on the column:** call the existing P4 route
  (`/catalog/{source}/tables/{table}/suggestions`) from asset detail, filtered to suggestions
  that use the opened column; resolve the standing P4 access call (default data-owner session
  403s on `feature:read`) — either grant read on the session role or a column-scoped read
  route; never silently swallow the 403 into an empty section.
  (ACCESS WAS ALREADY RESOLVED on the route: it gates on `catalog:read`, not `feature:read` —
  the narrower change, pinned by `test_suggestions_route` ("data_owner PASSES"). The dossier's
  usage section filters `uses` to the opened column, reuses `SuggestionCard`, and renders a
  403 as the honest access message naming `catalog:read`; a non-403 failure is an error, and
  an empty filter result names how many table suggestions exist that don't use this column.)
- [x] **AI summary placement:** show the (Task 3 Step 6c) enriched summary beside — never in
  place of — the source definition, labelled as AI-drafted. (Meaning section: two slots side by
  side; a missing definition keeps its slot honest — the summary never occupies it.)
- [x] Cross-catalog links, readiness roles, and decision history remain; the dossier orders
  sections: identity → meaning → semantics → governance → usage → history. (Overview =
  identity → Type → Meaning → source glossary → Semantics axes → Governance summary →
  Suggested features; tabs reordered Overview → Relationships → Metadata & evidence →
  Readiness → History so governance follows semantics and usage precedes history.)

**Tests:**

- A column with a declared type never displays bare "unknown".
- The four Step-6b fields render with source provenance; a column without them shows no
  fabricated section.
- An LLM-proposed unit renders with the AI-proposed chip; a governed unit replaces it.
- Suggested features appear for a column used by ≥1 suggestion; a 403 renders an honest
  access message, not an empty list.
- The summary and definition are visibly distinct fields.

---

### Task 3D: Entity Map v0 — the Ontology Becomes Visible

**Purpose:** the ontology has an engine and consumers but no face. This is E0's "semantic map"
pulled forward as a READ-ONLY v0 over data that already exists: entities as nodes, the columns
carrying each entity grouped by catalog, available cross-catalog links as edges. It gets more
interesting after every other task (Task 1 namespaces, Task 3 entity axis, Task 6 re-derivation)
with zero rework, because it renders whatever the governed readers currently say. The full
ontology explorer (traversal, ER editing, multi-hop) stays deferred.

**Files:**
- Create: `src/featuregen/overlay/upload/entity_map.py` (read model)
- Create: route `GET /catalog/entity-map` under `src/featuregen/api/routes/`
- Create: `frontend/src/screens/EntityMapScreen.tsx` (+ nav entry)
- Reuse: `available_identifier_links()` and `known_entities()` — the map NEVER re-reads the
  ledger or re-folds lifecycles; a second availability interpretation is banned (DoD-12's rule)
- Test: `tests/featuregen/overlay/upload/test_entity_map.py` + frontend suite

**Steps:**

- [ ] **Read model:** per entity present in the graph — column count per catalog (read-scoped,
  same `visible_requires` treatment as asset detail), sample column refs; per entity-pair —
  every AVAILABLE link with status (proposed/confirmed), strength, namespace (once Task 1
  lands), and direction-specific eligibility where a realization exists. Population comes from
  `available_identifier_links()` verbatim.
- [ ] **Screen:** entities as nodes sized by column count, catalogs as groupings, links as edges
  with status/strength chips; click a node → search filtered to that entity; click an edge →
  the governance queue item / asset detail relationships. Mermaid-simple rendering is fine for
  v0 — the value is truth, not layout.
- [ ] **Honesty rules:** an empty map states "no governed links yet" (never a blank canvas); a
  proposed link renders as proposed, never dimmed as failure (standing product direction);
  counts the map shows must reconcile with the audit script's numbers.
- [ ] Flag-gate the nav entry with the profile read-model flag family; flag-off is absent, not
  broken.

**Tests:**

- The map's link set is byte-identical to `available_identifier_links()` (same fixture, same
  filters) — governance, planner, and map can never disagree.
- A read-scoped caller sees neither restricted columns nor their counts.
- Empty state renders the honest message; a proposed-only map renders proposed chips.
- After a fixture re-derivation removes a decoy, the map loses the edge without a rebuild.

---

### Task 4: Un-Stall Semantic Bindings (126 candidates → 0 proposals)

Use superpowers:systematic-debugging — root cause before fix.

**Files:**
- Modify (expected, confirm by diagnosis): `src/featuregen/overlay/upload/ingest.py`
  (`semantic_binding_proposals` stage seam) and/or
  `src/featuregen/overlay/upload/semantic_bindings/propose.py`
- Test: `tests/featuregen/overlay/upload/test_semantic_binding_pipeline.py`

**Interfaces:**
- Consumes: existing `semantic_binding_candidate` rows and the existing confirm surface; produces
  no new contracts — this is a wiring repair.

- [x] **Step 1: Reproduce and localize — this is a CODE bug.** Both flags are `"1"` in
  `deploy/kind/k8s/20-backend.yaml:42-43` and the live `semantic_binding_candidate_proposal`
  table has zero rows against 126 candidates (verified 2026-07-31). Verify the running pod's env
  once (configmap drift is conceivable), then go straight to instrumenting the
  candidate→proposal seam on a fixture catalog with monetary+currency columns: candidates
  written vs proposal stage entered vs `sembind_proposable` contents vs proposals emitted vs
  per-candidate denial reasons. Prime suspect: the filter that assembles `sembind_proposable` —
  a stage that enters with 126 candidates and emits nothing without a recorded denial is the
  silent-zero class this plan bans.
  **FOUND (2026-07-31, local fixture repro — no cluster access needed):**
  `shortlist._currency_candidates` marked a pairing STRONG only when the table had EXACTLY ONE
  currency column; live FTR carries several (`tran_crncy` + `actual_tran_crncy`), so every
  measure×currency pairing was WEAK, the `disposition == STRONG` filter starved
  `sembind_proposable` into empty per-set lists, and the stage truthfully-looking recorded a
  vacuous `succeeded {proposed: 0, abstained: 0}` with no per-candidate reason.
- [x] **Step 2: Write the failing test at the located boundary** — e.g. “a monetary column with a
  same-table currency column yields ≥1 currency-binding proposal and stage
  `semantic_binding_proposals=succeeded{n>0}`”, or the config-shaped equivalent.
  (`tests/featuregen/overlay/upload/test_semantic_binding_pipeline.py` — red before the fix.)
- [x] **Step 3: Fix minimally; PASS.** Name-affinity disambiguation
  (`shortlist.preferred_currency_target`, shared by shortlist AND validate so STRONG can never
  drift) + fixed-currency literal candidates (`counter_party_amt_aed` → closed
  `known_currency_codes()` member `AED`); `crncy`/`_crncy` added to the structural currency
  tokens; `DETERMINISTIC_TASK_VERSION` bumped to `d2-shortlist-v2` so the live v1 all-weak
  current sets are superseded on the next ingest instead of replayed.
- [x] **Step 4: Add the silent-no-op guard:** candidates>0 with proposals==0 and no recorded
  per-candidate denial reason forces stage state `failed{reason:"unexplained_zero"}` — this is
  the class-level fix the Global Constraints demand. (The proposal stage now receives ALL
  candidates and returns a `denials` reason→count histogram covering every non-proposed one;
  an explained zero stays `succeeded {proposed: 0, denials: {...}}`.)
- [x] **Step 5: End-to-end fixture:** candidate → proposal → confirm via the **existing
  `semantic_binding_governance` surface** (`api/routes/governance.py:68-77`:
  `list_semantic_binding_proposals`, `load_semantic_binding_confirmation_context`,
  `project_verified_semantic_binding`) → `semantic_binding_edge` row → `graph_node.currency`
  projected. The AED fixed-currency case (`counter_party_amt_aed` → literal `AED`) and the
  column-ref case (`actual_tran_amt` → `actual_tran_crncy`) both covered.
  (Migration `1043_semantic_binding_fixed_currency.sql` — from the richness 1043–1044
  reservation: the 1014 candidate kind-shape CHECK gains the literal variant,
  `semantic_binding_edge.currency_code` + NULL-able `to_ref`, and the `graph_node` currency
  quartet `declared_currency`/`currency_fact_key`/`currency_fact_event_id`/`currency_status`
  mirroring the 1015 entity pattern; replay parity via `SemanticBindingProjection`
  reset/rebuild proven in the e2e test.)
- [x] **Step 6: Commit** `fix(bindings): proposal stage fires; zero-without-reason is a failure`.

---

### Task 5: Table-Fact Stamp Reconciliation

Use superpowers:systematic-debugging.

**Files:**
- Modify (expected): `src/featuregen/overlay/upload/table_fact_projection.py`
- Create: `src/featuregen/overlay/upload/stamp_reconcile.py`
- Modify: `src/featuregen/overlay/upload/ingest.py` (stage recording)
- Test: `tests/featuregen/overlay/upload/test_stamp_reconcile.py`

**Interfaces:**
- Produces: `reconcile_table_fact_stamps(conn, *, source=None) -> tuple[StampDrift, ...]` where
  `StampDrift(object_ref, fact_type, overlay_event_id, stamped_event_id)`; and
  `repair_table_fact_stamps(conn, adapter, *, now)` re-running the existing projection (no
  fabricated event ids — it re-reads `resolve_fact`).

- [x] **Step 1: Reproduce.** Fixture: confirm a grain fact, then re-ingest the same catalog;
  assert the table node's `grain_fact_event_id` — the kind evidence (VERIFIED facts, NULL stamps
  on both tables) says some path wipes or skips the stamp. Locate whether re-ingest node
  recreation drops stamps, or projection-lag skip (`table_fact_projection lagged`) never
  backfills.
- [x] **Step 2: Write the failing test** at the located seam: “after re-ingest + drain,
  VERIFIED grain/availability facts are re-stamped on the table node.”
- [x] **Step 3: Fix minimally** (expected shape: run `project_table_facts` for sources with
  VERIFIED facts even when the upload itself asserted none, after the drain catches up).
- [x] **Step 4: Reconciliation check:** `reconcile_table_fact_stamps` compares overlay VERIFIED
  grain/availability facts against node stamps; wire as an ingest tail stage
  (`stamp_reconcile: {drift: n}`) and expose in the audit script (Task 0 script gains the
  reconcile query — update it).
- [x] **Step 4b: Gate the relocated bridge stage on projection agreement (live bug on main).**
  The merged ingest runs `entity_bridges` AFTER `table_fact_projection` so it assesses against
  governed grain — but when `table_fact_projection` records `lagged` and skips, `entity_bridges`
  still runs (`ingest.py` ~2770 has no lag check), assessing exactly the stale flat grain flags
  the relocation exists to escape. Fix: when the projection stage did not reach `succeeded`,
  the bridge stage records `skipped{reason: projection_lag}` and does not assess; the next
  caught-up ingest (or the Step-3 repair path) assesses. Test: a lagged projection yields a
  skipped bridge stage and zero new candidate revisions; a caught-up rerun assesses normally.
- [x] **Step 5: CIB availability basis review item:** the fact value
  `{basis: ingested_at, column: business_dt}` is questionable (a business date labelled as
  ingestion). Do NOT rewrite. **Verify-or-add:** the re-verify task machinery is currently tied
  to expiry/stale events — check whether an "open a review task WITHOUT staling the fact"
  command exists in `_lifecycle`/task helpers; if not, add the minimal command (opens one
  `human_tasks` row bound to the fact's current confirmed event, reason `basis_review`, no
  status change). Test: the task exists, the fact stays VERIFIED and servable until decided.
- [x] **Step 6: Commit** `fix(projection): table-fact stamps survive re-ingest; drift is reconciled and visible`.

> **Task 5 diagnosis (2026-07-31, executed):** the live 4 stamp-drift rows are NOT a wipe and
> NOT the lagged skip — `project_table_facts_for_ref` (and the confirm-time bridge through it)
> only ever stamped `kind='column'` rows; **no code path had EVER written
> `grain_fact_event_id`/`availability_fact_event_id` on the `kind='table'` node** the audit
> script and baseline hand-query read, so every VERIFIED table fact reported table-node drift
> since migration 0986, healthy ingest or not (reproduced locally: healthy ingest + re-ingest →
> column stamps correct, table stamps NULL, audit query emits drift). Fixed: the projection now
> clear-then-sets the table-node stamp alongside the columns. The lagged-skip-with-no-backfill
> path is REAL as a second defect (build_graph wipes nodes, a `lagged` stage skips, nothing
> backfills until a next caught-up ingest that may never come) — closed by
> `stamp_reconcile.repair_table_fact_stamps` (re-reads `resolve_fact`; an unservable VERIFIED
> fact is left drifted-and-visible, never force-stamped, so the c715a16d declared-flag wipe
> hazard cannot re-enter through the repair). Step 4b's gate also covers a FAILED projection
> (`deferred{table_fact_projection_failed}`), not just `lagged{projection_lag}`. Step 5 verified
> NO existing no-stale review command (all `open_reverify_task` callers are expiry/stale/drift
> flows) → added `reverify_tasks.open_fact_review_task` (reason rides `required_inputs`;
> CAS-bound to the current `confirmed_event_id`; idempotent; appends no overlay event). The
> audit script's `stamp_drift` metric was itself measuring the never-written table stamp — now
> mirrors the reconcile (IS DISTINCT FROM the fact's `confirmed_event_id`, wrong-stamp-aware,
> source-labelled).

---

### Task 6: Governed Live Re-Derivation of CIB and FTR — **GATED: user approval required**

Cluster mutation + LLM spend. Present the pre-flight summary and STOP for approval before
executing (standing rule: never upload/deploy without explicit approval).

**Preconditions (gate inputs, verified before asking for approval):**
- The original source files are on hand: the FTR export CSV is **local-only** (not in the repo)
  and the CIB file's location must be confirmed with the user — re-upload is impossible without
  them; name their paths in the pre-flight summary.
- Cache behavior: Task 1 changed the names-only vocabulary fingerprint, so the concept cache
  re-derives — state the fingerprint pair (old/new) in the pre-flight so a cache no-op is
  detectable before spend.
- Sticky-rejection note: rejecting the 8 decoy facts sticky-denies their fingerprints — safe,
  because the corrected concepts can never re-derive those pairs; record this so nobody treats
  the sticky denial as a bug later.

**Files:**
- Extend: `scripts/verify_catalog_richness.sql` (before/after diff mode)
- Create: `docs/superpowers/verified-interfaces/ingestion-richness-rederivation.md` (run record)
- No new product code — this task RUNS Tasks 1–5's build

**Steps:**

- [ ] **Step 1: Deploy the build** containing Tasks 1–5 (existing deploy.sh; approval covers
  this). This deploy applies main's three pending migrations
  (`1036_bridge_endpoint_canonical_orientation`, `1037_data_source_binding`,
  `1038_eligibility_policy`) to the cluster for the first time — list them in the pre-flight
  summary and confirm the schema_migrations ledger records all seven `1036`–`1039`-range names
  afterward.
- [ ] **Step 2: Pre-flight snapshot:** run the audit script; store output.
- [ ] **Step 3: Re-upload both catalogs** (same source files: FTR export + CIB file). Expected
  mechanics: LLM-produced concept suggestions re-derive under the repaired registry with the
  critic active; human/uploader facts (grain, availability, visible_requires) survive by
  value-diff; axis projection fills sensitivity/entity/additivity; binding proposals fire;
  stamps re-project; `ai_summary` populates for CIB.
- [ ] **Step 4: Decoy retirement — BOTH families, live count at run time.** With corrected
  concepts, `derive_bridge_candidates` yields neither the 8 branch pairs nor the 8 counterparty
  pairs (born 2026-07-31 09:54Z); re-derivation supersedes their assessments. Close every open
  decoy task — whatever the live count is at run time (11 open as of the last audit, task
  consolidation included) — through the governance reject path (recorded actor + reason
  `superseded_by_rederivation`), never SQL deletion. The Customer link and its task remain, and
  honest bank-namespace re-derivations of the same column pairs (e.g. `cust_swift_cd ↔
  sender_bic`) are EXPECTED survivors, not decoys.
- [ ] **Step 5: Post-flight audit + hand-reconciliation.** Targets (the DoD numbers):

| Metric | Target |
| --- | --- |
| sensitivity display | 237/237 non-NULL where enforcement or concept default exists; NULL only where honestly unknown |
| entity (display) | every identifier-group column with `entity_link` |
| additivity | every monetary/numeric column with a concept default |
| ai_summary | 237/237, drafted from the ENRICHED tail payload (Step 6c keys) — spot-check that summaries are syntheses, not definition paraphrases |
| source-glossary evidence (`term_type`, processes, related terms, physical FQN) | present as `source_attested` field evidence for every FTR column that declared them |
| currency bindings | 6/6 monetary columns proposed; confirmable |
| `counter_party_bic`/`sender_bic`/`receiver_bic`/`corres_*` | `bank_bic`/`clearing_member_code`, never `counterparty_id` |
| `sol_desc` | `branch_name` (or non-identifier), never `branch_id` |
| `uetr`/`e2e_id`/`sw_srl_num`… | atomic reference concepts |
| pii-as-concept | 0 columns (party_*/postal/phone/email semantics; sensitivity via axis) |
| bridge candidates | Customer link present; 0 branch decoys; 0 counterparty decoys; open decoy tasks 0 |
| cross-namespace candidates | 0 (requires the namespace pairing handoff landed; else record the gap explicitly) |
| `cust_num ↔ counter_party_cif_id` | present as a `cif`-namespace candidate, population relation unknown (same conditionality) |
| `party_role` | populated on every role-bearing column (`sender_bic`→sender, `third_reimb_inst_code`→reimbursement, `counter_party_*`→counterparty, `cust_swift_cd`→subject); NULL only on genuine ambiguity |
| table stamps | 2/2 tables stamped; reconcile drift 0 |
| VERIFIED human facts | byte-identical to pre-flight (grain ×2, availability ×2) |

- [ ] **Step 6: Record the run** (before/after script outputs, image digest, task closures) in
  the verified-interface doc. Any target miss is a defect to fix before Task 8 signs off — not a
  number to relax.

---

### Task 7: Attested-Type Path (honest basis upgrade) — **GATED: cluster access approval**

**Files:**
- Modify: `src/featuregen/data_agent/observation.py` (type observation shape, if absent)
- Create: `src/featuregen/overlay/upload/type_attestation.py`
- Test: `tests/featuregen/overlay/upload/test_type_attestation.py`
- Test: `tests/featuregen/data_agent/test_type_observation_render.py`

**Interfaces:**
- Produces: `attest_types_from_observation(conn, *, source, table, observation) -> TypeAttestReport`
  — upgrades `graph_node.data_type` from `unknown` ONLY from a real engine read
  (`DESCRIBE`/information_schema via the existing executor+dialects), recording the observation
  ref; `type_basis` derivations everywhere then see `attested`.

- [ ] **Step 1: Failing tests:** an observation carrying engine-reported types upgrades
  `data_type` and records provenance; a declared-only column is never upgraded; re-running with a
  CHANGED engine type does not silently overwrite — it records a conflict for drift handling.
- [ ] **Step 2: Implement + dialect rendering tests** (Hive `DESCRIBE` vs Postgres
  information_schema — rendering and transport tests run without a cluster, per the bridge
  plan's Task 7 precedent). This **extends the `executor.Dialect` Protocol** with one method
  (`render_schema_observation(plan) -> str`) — the Protocol currently has only
  `render_column_profile`/`timeout_statements`/`effective_method`; both dialects implement the
  new method, and the executor path reuses `effective_method` unchanged.
- [ ] **Step 3 (gated):** with approval, run against the kind Spark/Hive sandbox for the two
  tables; targets: `data_type` real for every column physically present; columns absent from the
  physical table (glossary-only) stay `unknown` — that absence is itself recorded.
- [ ] **Step 4: Commit** `feat(types): attested data types from engine observation — declared never silently upgraded`.

---

### Task 8: Adversarial and Mutation Gates

**Files:**
- Extend the suites created in Tasks 1–5, 7
- Extend the repo's must-die/must-survive mutation harness

**Required adversarial cases:**

- a BIC-shaped column proposed as `counterparty_id` → critic refutes with
  `identifier_namespace_mismatch`;
- a `_desc`/`_nm` column proposed as any identifier → refuted;
- a numeric `_amt` column proposed as an identifier → refuted (`measure_not_identifier`);
- a genuinely ambiguous reference column → critic abstains, column keeps a non-identifier
  fallback, disposition recorded;
- sensitivity projection never writes `visible_requires`;
- an existing human `entity_fact` survives the entity projection;
- an uploaded additivity survives the additivity default;
- binding candidates >0 with proposals ==0 and no denial reasons → stage `failed`;
- re-ingest preserves table stamps; reconcile reports drift when a stamp is manually nulled;
- re-derivation preserves VERIFIED grain/availability byte-identically;
- decoy task closure goes through the governed reject path (audit row exists);
- attested-type upgrade only from an engine observation; engine-type change records conflict;
- **re-derivation never weakens `visible_requires` on any column** — the pii→party_name rename
  keeps the floor (pin against the ingest weakening guards, `ingest.py:2904-2938` region);
- a description-only registry edit does NOT bump the vocabulary fingerprint (documented
  behavior, asserted so it is a known property, not a surprise);
- the representation extraction is behavior-identical: the pre-move bridge-grounding suite
  passes unmodified against the moved module;
- a re-ingested column whose stored identifier concept is refuted ends `unclassified`/revised —
  never silently keeps the old identifier;
- a non-identifier concept declaring a `namespace` fails registry validation;
- two identifier columns in the same namespace but different entities still form a candidate
  (entity mismatch is a display note — the cust_num/counter_party_cif_id case), and the entity
  pick is byte-identical across input orders and re-derivations;
- **rejecting a decoy never blocks the honest re-derivation of the same column pair** under a
  different entity/namespace (sticky fingerprints hash the old entity's fact key; the
  bank-namespace `cust_swift_cd ↔ sender_bic` re-derivation gets a new key and proceeds);
- a lagged table-fact projection yields a skipped bridge stage, never an assessment over stale
  flat grain flags;
- an ambiguous column name leaves `party_role` NULL; a wrong `party_role` cannot change any
  candidate set, plan, or execution outcome;
- the summary prompt payload contains the enriched dossier fields, and enriching the payload
  changes the summary cache key (the auto-redraft property);
- the entity map's population is byte-identical to `available_identifier_links()` and its
  counts reconcile with the audit script;
- each of the four source-glossary fields round-trips upload → field evidence → asset payload;
- a declared-type column never renders bare "unknown"; a suggestions 403 renders an access
  message, never an empty section;
- the audit script's coverage assertions hold on the post-run fixture.

**Required mutations (must die):**

- reclassify a name/description column as an identifier without a critic conflict;
- make the critic advisory (persist a refuted identifier assignment);
- write sensitivity into `visible_requires`;
- overwrite a human entity/additivity value from a projection;
- let the binding stage report `succeeded` on unexplained zero;
- fabricate a stamp event id in the repair path;
- upgrade `data_type` from a declared value;
- close a governance task by DELETE;
- drop `sensitivity="pii"` from any party concept (the visible_requires-weakening test must die);
- fork the representation ruleset (a second suffix/token implementation anywhere outside
  `attest/representation.py` — import-gate it like the reader gates);
- let a refuted identifier assignment silently retain the prior stored concept;
- strip `namespace` from one identifier concept (the completeness test must die);
- consume `party_role` in a join-candidacy or execution predicate (the advisory contract must
  die loudly);
- remove the bridge stage's projection-lag gate (assess-under-lag must die);
- make the handoff's entity pick order-dependent (the byte-identity test must die);
- draft the summary from file-only metadata again (the enriched-payload test must die);
- drop one of the four glossary fields from `_SOURCE_FIELDS` (the round-trip test must die);
- render "unknown" over an existing declared type (the display-policy test must die);
- make the entity map read the candidate ledger or fold lifecycles itself instead of
  `available_identifier_links()` (the byte-identity test must die).

**Must-survive no-op + literal focused-test baseline count + proof-of-run**, per the repo's
harness pattern.

---

## Handoff to the Bridge Session — Namespace Pairing Rule

`bridge_candidates.py` belongs to the bridge plan; this section is the specification handed to
its owner, to land **before Task 6's re-derivation** so the live repair derives under the
permanent rule, not the registry patch alone.

**Change:** `derive_bridge_candidates` groups identifier columns by **`concept.namespace`**
instead of `concept.entity_link`. Entity remains carried on the candidate for corroboration and
display; an entity mismatch within one namespace is a display note, not a suppression. **The
entity pick must be deterministic, because `fact_key` hashes `entity_id`:** when the two
endpoints' `entity_link`s agree, use that entity; when they disagree (the
`cust_num`(customer) × `counter_party_cif_id`(counterparty) case), prefer the SUBJECT-role
endpoint's entity where the `party_role` axis distinguishes exactly one subject; otherwise the
lexicographic minimum of the two, plus an `entity_disagreement` explanation code. An unstable
pick would re-key the same link across re-derivations.
The bridge plan's bounded enumeration already names "namespace hints" as a blocking key — this
supplies the data that slot has been waiting for.

**Expected outcomes on the live catalogs (acceptance for the handoff):**

- zero cross-namespace candidates — `swift_bic ↔ cif` and `swift_bic ↔
  correspondent_scheme_code` pairs (all 8 of today's counterparty decoys) become impossible;
- the `cif` namespace group yields `cust_num ↔ cif_id` (existing Customer link, identity
  preserved) AND the new subset candidate `cust_num ↔ counter_party_cif_id`, with
  `governed_population_relation` unknown until evidence;
- the `swift_bic` group yields bank-namespace links whose meaning the `party_role` axis
  explains (e.g. `cust_swift_cd ↔ sender_bic` = "transactions where the sender is the
  customer's own bank");
- existing fact keys never change: the namespace rule changes which candidates DERIVE, not how
  a bridge fact is identified.

**Must-die mutation (owned by whichever suite lands with the change):** pair two identifier
columns whose concepts declare different namespaces.

## Execution Order

```text
PRECONDITION SATISFIED: the bridge branch is merged (origin/main b5088f5d); Task 0 confirms

Task 0  audit script + merged baseline + migration reconciliation   (read-only)
  -> Task 1  registry repair                (code only)
  -> Task 2  representation extraction + concept critic (code only)
  -> Task 3  axis projections + sidecar evidence + tail summary (code only)
  -> Task 3C column dossier UI              (code only; after 3's payload lands)
  -> Task 3D entity map v0                  (code only; readers exist today — may run any time
                                             after Task 0, grows richer as 1/3/6 land)
  -> Task 4  binding pipeline fix           (code only; diagnosis first)
  -> Task 5  stamp reconciliation           (code only; diagnosis first)
  -> Task 6  GATED live re-derivation       (approval + source files; runs 1–5)
  -> Task 7  GATED attested types           (approval for live half; tests before)
  -> Task 8  adversarial + mutation gates
```

Tasks 1–5 are independent enough to parallelize across implementers after Task 0, except Task 3
depends on Task 1's concept defaults and Task 2's critic runs before Pass-A persistence. Task
2's extraction step touches `bridge_grounding.py` on main — if the bridge session resumes work
(its plan's Tasks 11–12 remain), coordinate the move so its rebase is clean. Task 6 is the
single integration point and MUST stop for approval.

## Definition of Done

1. The audit script runs green against the post-re-derivation cluster with every Task 6 target
   met, and its before/after outputs are recorded.
2. No identifier concept spans more than one namespace on the live graph; names, descriptions,
   and amounts carry non-identifier concepts.
3. Every enrichment identifier assignment passed a refute-oriented critic backed by
   deterministic representation checks, with per-field dispositions recorded.
4. Sensitivity, entity, and additivity display axes are populated wherever enforcement, concept
   linkage, or concept defaults exist — with provenance, without ever touching enforcement or
   human decisions.
5. The semantic-binding pipeline produces confirmable currency/unit proposals, and no ingest
   stage can report success on an unexplained zero.
6. Table-fact stamps agree with the overlay on every table, drift is a visible reconcile metric,
   and the questionable CIB availability basis is a recorded human decision, not a silent value.
7. The eight decoy bridge candidates and their tasks are retired through governed paths; the
   Customer link stands.
8. Attested types come only from engine observations; declared remains declared, honestly.
9. All VERIFIED human facts survive the entire programme byte-identically.
10. The three-axis model is in force: every identifier concept declares its namespace, namespace
    is the only axis that gates join candidacy, `party_role` explains and never gates, and
    "counterparty" exists only as a role. The pairing rule itself lands via the bridge-session
    handoff, and Task 6's targets record explicitly if it hasn't.
11. **The column dossier shows everything the platform holds:** every uploaded glossary field is
    durably saved and rendered under a product name with provenance; no screen shows "unknown"
    over a value we hold at weaker basis; AI-proposed values render with chips instead of blank
    sections; the AI summary is a synthesis of the enriched view, visibly distinct from the
    definition; suggested features are reachable from the column that uses them.
12. **The ontology is visible:** the Entity Map v0 renders entities, catalogs, and available
    links from the shared readers — one availability truth across map, governance, and planner —
    and after the re-derivation it shows the honest post-repair graph without rework.
13. Mutation gates prove every one of the above is load-bearing.

## Explicitly Deferred

- Catalog/dataset descriptions, authority roles, temporal storage models, serving policy — the
  profiles plan owns them (its consumers assume THIS plan's data floor).
- Bridge/link admission, realization safety, crosswalks — bridge plan (deployed/ongoing).
- Governed `entity_assignment` fact backfill at scale (the display projection here is
  provenance-marked context; the fact loop stays Delivery-E's command path).
- Fuzzy identity resolution; multi-catalog beyond CIB/FTR; scheduler/tenancy/NFR programme.
