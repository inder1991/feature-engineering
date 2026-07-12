# Phase 3A — Pass C Deterministic Governed Joins — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Deterministically discover single-column join candidates from the uploaded glossary, file each *strong* candidate as a governed `approved_join` **proposal**, let **two platform-admins** confirm it (governance fallback), and project a confirmed join into an operational `graph_edge` that `find_join_path` traverses. No LLM (Phase 3B).

**Architecture:** ~85% reuse of the `approved_join` propose → **dual-owner** confirm → expiry → feature-gen-reads-only-operational spine. Phase 3A adds a pure deterministic candidate producer (blocker + namespace classifier + scorer), a dedupe lifecycle, propose wiring, a reverse projector (confirmed fact → operational edge, **declared-spare**), an async edge-demotion hook, and a relationship-readiness dimension. Spec: `docs/superpowers/specs/2026-07-12-phase3-passc-governed-joins-design.md` (v2).

**Tech Stack:** Python 3.12, `uv`, psycopg 3, PostgreSQL (ephemeral PG via `postgresql_proc`), pytest.

## Revision log (v2 — adversarial code-review folded in)

v1 was reviewed against the real code; v2 fixes ~13 confirmed defects. Load-bearing changes:
- **Confirmation is DUAL, not single.** `owner_of→None` both sides ⇒ `Authority.dual=True` ⇒ two side-labelled platform-admin tasks, **two distinct** confirmations (`PARTIALLY_CONFIRMED` → `VERIFIED`). `_confirm_join` drives **two distinct** admins. (`authority.py:118-124`, `join_confirmation.py:97-120`.)
- **Flag-off byte-for-byte:** the end-of-ingest projector is **declared-spare** — it only demotes edges whose `approved_join_fact_key IS NOT NULL`, and enumerates pairs from `approved_join` facts (never raw `graph_edge` rows), so a flag-off catalog's declared operational joins are untouched.
- **Reviewer evidence rides `evidence_ref`** (pre-mint `write_evidence(metric_values=…)`, pass `evidence_ref`), which `get_task_proposal` already surfaces — NOT the proposal payload (closed schema) and NOT `proposed_value` (`additionalProperties:False`).
- **Cardinality is in the `fact_key`** ⇒ no confirm-time override (the dual path ignores `args['value']` anyway); any correction is reject → re-propose. A **neither-grain (`MANY_TO_MANY_RISK`) candidate has `cardinality=None`** and is forced to the **weak** bucket, never proposed (schema requires `1:1|1:N|N:1`).
- **Projector is orientation- & scope-safe:** match by the **unordered pair**, render endpoints in **public** graph scope (`public.table.column`), demote **both** orientations, keep **one** operational row.
- **Async demotion hook:** a reject/expiry that takes a join out of `VERIFIED` demotes its linked edge immediately (not just at next ingest).
- **Governed mode pinned:** `OVERLAY_PASS_C` makes declared `joins_to` edges `display_only` **and** routes declared joins through `_propose_governed_joins`.
- **POSSIBLE namespace is reachable** (independent corroborator = synonyms / gated `related_terms`, not term-name equality); AMBIGUOUS pairs are excluded from proposals and surfaced only as weak diagnostics.
- **Weak candidates are recomputed on read** (Pass C is pure) — no new store. **Relationships readiness is a distinct dimension** with its own status enum (not the 4-value `ReadinessRequirement.status`).
- Fail-soft cite corrected to the savepointed Pass A stages (`ingest.py:627-648`); conftest authored in **Task 6** (first DB task); word-boundary negative filters; `_is_id_like` already catches `REF_NUM` (only `FORACID` is missed); `_ENRICH_ACTOR` at `enrich_llm.py:204`; `EXPIRED` is not a real folded status (use `STALE`/`REVERIFY`).

## Global Constraints

- **Deterministic-first, no LLM in 3A.** No client, no egress.
- **No operational join without TWO distinct human confirmations.** Pass C only appends `OVERLAY_FACT_PROPOSED`; two distinct platform-admins reach `VERIFIED`; only a `VERIFIED` fact projects to an operational edge.
- **Default-OFF (`OVERLAY_PASS_C`, default `0`). Flag-off ⇒ `ingest_upload` byte-for-byte.** The projector is declared-spare; governed-mode edge-authority changes fire only under the flag.
- **Fail-soft.** The Pass C discovery+propose block runs in **its own `with conn.transaction():` savepoint + `except`** (mirror the Pass A enrichment stages `ingest.py:627-648`, NOT the bare-except identity block at 596-614 and NOT `_propose_governed_joins` which has no savepoint). `propose_fact` does un-savepointed appends, so the savepoint must be at the Pass C call site.
- **Migration slot `0987`** (last used `0986`). No other migration (weak candidates are recomputed, not stored).
- **Cardinality enum `1:1|1:N|N:1`** (`facts.py:103`); it is part of the `fact_key` (`identity.py:71-81`). No `N:M`; no confirm-time override.
- Reuse types verbatim: `ApprovedJoinRef(from_ref, to_ref, column_pairs: tuple[ColumnPair,...], cardinality: str)`, `ColumnPair(from_col, to_col)`, `CatalogObjectRef(catalog_source, object_kind, schema, table, column=None)`. Join value `{from_ref: asdict, to_ref: asdict, column_pairs: [{from_col,to_col}], cardinality}`.
- Reviewer evidence channel: `write_evidence(...)` (`overlay/evidence.py:75`) with `metric_values` carrying the candidate breakdown, `producer=EvidenceProducer.STRUCTURAL_CONNECTOR`, `strength=AssertionStrength.PROPOSED`; pass the returned `evidence_ref` into the `propose_fact` `args`.
- Tests under `tests/featuregen/overlay/upload/passc/`. Runner `uv run pytest <path> -q`.

## Reuse Map (verified)

| Need | Status | Home |
|---|---|---|
| `approved_join` fact + value schema + `ApprovedJoinRef`/`ColumnPair` | REUSE | `overlay/facts.py:79-106`; `overlay/identity.py:9-29` |
| Propose a governed join (F4/consistency, mints evidence, **one task per owner side**) | REUSE | `overlay/proposal_commands.py:34` `propose_fact`; accepts `evidence=`/`evidence_ref` |
| **Dual-owner** confirmation (both-unknown ⇒ dual; `PARTIALLY_CONFIRMED`→`VERIFIED`; two distinct subjects) | REUSE | `overlay/authority.py:92-137`; `overlay/join_confirmation.py:58-183`; `overlay/confirmation_commands.py:47,87` |
| Reject only pre-VERIFIED (`_AWAITING_CONFIRMATION` excludes VERIFIED) | REUSE | `overlay/confirmation_commands.py:201`; `_lifecycle.py:27` |
| Reviewer evidence store + read | REUSE | `overlay/evidence.py:75` `write_evidence`; `overlay/task_read.py:59-67` `get_task_proposal` (returns `read_evidence(evidence_ref)`) |
| Fact-state read (folded) for dedupe | REUSE | `overlay/store.py` `load_fact` + `overlay/state.py` `fold_overlay_state`; `overlay/identity.py:65` `fact_key` |
| VERIFIED-only fact read for projection | REUSE | `overlay/resolve.py:183` `resolve_fact` (`.provenance['confirmed_event_id']`) |
| Feature-gen traverses only operational join edges | REUSE/EXTEND | `overlay/upload/join_path.py:38,53`; `entity.py:224`; add `(approved_join_fact_key IS NULL OR approved_join_status='VERIFIED')` |
| Governed joins seam (declared → display_only under flag) | EXTEND | `overlay/upload/graph.py:16,24` — make the predicate also fire under `OVERLAY_PASS_C` |
| Id-like detector (name-suffix; catches `REF_NUM`, misses `FORACID`) | REUSE/EXTEND | `overlay/upload/entity.py` `_is_id_like(column_name, data_type)` — combine with `term_type`/concept |
| Service actor + fail-soft savepointed stage pattern | REUSE | `overlay/upload/enrich_llm.py:204` `_ENRICH_ACTOR`; `ingest.py:627-648`, `738-743` |
| `graph_edge` (`catalog_source,kind,from_ref,to_ref` PK; `authority` default `'operational'`; `cardinality`) | EXTEND | `0945/0982/0956`; Phase 3A adds fact-link columns (`0987`) |

## File Structure

**New (`src/featuregen/overlay/upload/passc/`):** `types.py`, `identifiers.py`, `namespace.py`, `candidates.py` (blocker+scorer), `lifecycle.py`, `propose.py`, `projection.py`.
**Modified:** `readiness.py` (relationships dimension), `ingest.py` (wire + governed mode), `graph.py` (governed predicate reads `OVERLAY_PASS_C`), `join_path.py`/`entity.py`/`feature_assist.py` (governed edge filter), `confirmation_commands.py` + `expiry.py` (async edge-demotion hook). Migration `0987`.

---

## Task 1: Types + scoring config

Unchanged from v1 except: `PassCConfig.weights` **drops** `namespace_ambiguous`/`namespace_incompatible` (AMBIGUOUS/INCOMPATIBLE never enter scoring — the blocker gates them out); keep `NamespaceCompatibility`, `CardinalityInferenceStatus`, `SignalEvidence`, `JoinCandidateEvidenceV1`, `DEFAULT_CONFIG` (weights: `same_identifier_concept=40`, `related_terms_key_link=50`, `same_column_name=30`, `same_term_name=25`, `same_entity_tag=25`, `same_bian_leaf=10`, `same_fibo_leaf=10`, `compatible_phase2_entity=15`, `one_side_confirmed_grain=10`, `compatible_domain=10`), `negative_concepts`, `mixed_bian_leaves`, thresholds 80/50, `CONFIG_VERSION`, `ALGORITHM_VERSION`.

- [ ] Tests: enums + config defaults + `JoinCandidateEvidenceV1` `asdict` round-trip. Implement `types.py`. Fail→pass→commit. (See v1 code; delete the two dead namespace weights.)

---

## Task 2: Identifier eligibility + concept normalization

**Files:** Create `.../passc/identifiers.py`; Test `test_identifiers.py`.

**Interfaces:** `ColMeta(object_ref, table, column, data_type, term_name, term_type, concept, synonyms, bian_leaf, fibo_leaf, entity_tag, data_domain, is_grain)`; `is_join_key_eligible(col, cfg=DEFAULT_CONFIG) -> bool`; `normalized_identifier_concept(col) -> str | None` (**folds synonyms**).

- [ ] **Step 1: Failing test**
```python
from featuregen.overlay.upload.passc.identifiers import ColMeta, is_join_key_eligible, normalized_identifier_concept
def _c(**kw):
    b=dict(object_ref="src::public.t.c",table="t",column="c",data_type="text",term_name="",term_type="",
           concept="",synonyms="",bian_leaf="",fibo_leaf="",entity_tag="",data_domain="",is_grain=False); b.update(kw); return ColMeta(**b)
def test_foracid_and_ref_num_eligible():
    assert is_join_key_eligible(_c(column="foracid", term_name="Customer Account Number", term_type="Dimension"))
    assert is_join_key_eligible(_c(column="ref_num", term_name="Reference Number"))     # _is_id_like catches _num
def test_negative_filter_fields_never_eligible():
    assert not is_join_key_eligible(_c(column="cust_name", term_name="Customer Name", concept="name"))
    assert not is_join_key_eligible(_c(column="tran_amt", term_name="Transaction Amount", term_type="Measure"))
def test_word_boundary_negatives_do_not_trip_real_ids():
    # "Mandate Reference" contains substring "date"; "Corporate Account Number" contains "rate" — both are IDs
    assert is_join_key_eligible(_c(column="mandate_ref", term_name="Mandate Reference"))
    assert is_join_key_eligible(_c(column="corp_acct_no", term_name="Corporate Account Number"))
def test_concept_normalization_folds_synonyms():
    a=normalized_identifier_concept(_c(column="cif_id", term_name="Customer Information File Identifier"))
    b=normalized_identifier_concept(_c(column="cif", term_name="Customer Information File Identifier", synonyms="CIF"))
    assert a and a==b
    assert normalized_identifier_concept(_c(column="foracid", term_name="Customer Account Number")) != a
```
- [ ] **Step 3: Implement** — **word-boundary** negative match (`set(re.split(r"[^a-z0-9]+", text.lower())) & cfg.negative_concepts`), not substring; `is_join_key_eligible = term_type≠Measure AND not negative AND (_is_id_like(col.column,col.data_type) OR term_name has an id token)`; narrow the `number` id-token to require an entity context (`account number`, `reference number`) so `Sequence Number` isn't admitted; `normalized_identifier_concept` canonicalizes `term_name` **+ folds `synonyms`** and strips generic id suffixes.
> **Implementer note:** `_is_id_like` already returns True for `REF_NUM` (`_num` suffix) — the term_name path's genuine addition is `FORACID`. Tune against the read-only `~/Downloads/FTR_Column_Mapping.csv` (do NOT copy it into the repo).
- [ ] Fail→pass→commit.

---

## Task 3: Namespace classifier (POSSIBLE reachable)

**Files:** Create `.../passc/namespace.py`; Test `test_namespace.py`.

- [ ] **Step 1: Failing test** — same entity_tag → COMPATIBLE; different entity_tag → INCOMPATIBLE; **same concept + synonyms → COMPATIBLE**; **same concept ALONE (no synonyms/related_terms) → POSSIBLE**; mixed BIAN leaf → AMBIGUOUS; same BIAN leaf only → AMBIGUOUS.
- [ ] **Step 3: Implement** — entity-tag branch first (COMPATIBLE if equal / INCOMPATIBLE if differ); then `same_concept` via `normalized_identifier_concept`; **reinforcement is an INDEPENDENT corroborator** — `synonyms` present on either side, or a gated `related_terms` key-link — **NOT** `term_name`/`column` equality (which are identical whenever concepts match, so they can't distinguish COMPATIBLE from POSSIBLE):
```python
if same_concept:
    reasons.append("same_identifier_concept")
    if a.synonyms or b.synonyms:            # (or a gated related_terms key-link)
        reasons.append("synonym_corroboration"); return N.COMPATIBLE, tuple(reasons)
    return N.POSSIBLE, tuple(reasons)
if mixed:  reasons.append("mixed_bian_leaf"); return N.AMBIGUOUS, tuple(reasons)
if la and la==lb: reasons.append("same_bian_leaf_only"); return N.AMBIGUOUS, tuple(reasons)
return N.AMBIGUOUS, ("generic_reference_without_context",)
```
- [ ] Fail→pass→commit.

---

## Task 4: Candidate blocker

**Files:** Add `block_candidates` to `.../passc/candidates.py`; Test `test_block.py`.

- [ ] `block_candidates(columns, *, allow_self_join=False) -> [CandidatePair]` — distinct-table pairs of `is_join_key_eligible` columns where `classify_namespace ∈ {COMPATIBLE, POSSIBLE}`; deterministic (sort by `object_ref`). **AMBIGUOUS/INCOMPATIBLE are excluded here** (they never reach `score`/`propose`); AMBIGUOUS pairs are surfaced only as weak diagnostics by Task 9 (recomputed), so there is exactly one story: gate=COMPATIBLE|POSSIBLE.
- [ ] Tests: two `cif_id` across tables → paired; `cust_name`/amount never paired; a mixed-leaf/INCOMPATIBLE pair excluded; self-table excluded; stable order. Do NOT assert "same-BIAN-leaf → weak" (it's AMBIGUOUS → excluded here). Fail→pass→commit.

---

## Task 5: Scorer + direction/cardinality (with the MANY_TO_MANY weak-cap)

**Files:** Add `score(...)` to `.../passc/candidates.py`; Test `test_score.py`.

- [ ] `score(pair, *, source_snapshot_id, cfg=DEFAULT_CONFIG) -> JoinCandidateEvidenceV1` — weighted signals → score; **bucket rules (in order):** (1) if `namespace == POSSIBLE` **and** no `related_terms_key_link` signal fired → cap at `weak`; (2) **if `cardinality_status == MANY_TO_MANY_RISK` or `proposed_cardinality is None` → force `weak`** (regardless of score) and put both grains in `missing_requirements`; (3) else `strong` if `≥80`, `weak` if `≥50`, `suppressed` if `<50`. Drop the vacuous `same_entity_tag` cap-exception (a same-entity pair is COMPATIBLE, never POSSIBLE).
- [ ] **Direction/cardinality:** right grain only → `from=a→to=b`, `N:1`, `INFERRED_FROM_CONFIRMED_GRAIN`; left grain only → `from=b→to=a`, `N:1`; both grain → `1:1`, `AMBIGUOUS_BOTH_GRAINS`; neither → `proposed_cardinality=None`, `MANY_TO_MANY_RISK` (→ forced weak by rule 2).
- [ ] Tests: same-concept+synonyms → `strong`, COMPATIBLE; same-concept-only → capped `weak`, POSSIBLE; **neither-grain high-score → `weak` + both grains in `missing_requirements`** (never strong, never None cardinality proposed); right-grain → `N:1` `from=a`; every result has a non-empty `explanation`. Fail→pass→commit.

---

## Task 6: Conftest (Step 0) + fingerprint + dedupe lifecycle

**Why first-DB-task:** Task 6 is the first task that touches the DB, so it authors the shared `passc` conftest that Tasks 7-11 consume.

**Files:** Create `tests/featuregen/overlay/upload/passc/conftest.py`, `.../passc/lifecycle.py`; Test `test_lifecycle.py`.

- [ ] **Step 0: Author `passc/conftest.py`** — `passc_conn(db)` (`ensure_upload_catalog_adapter()` + yield `db`); `service_actor` = reuse `_ENRICH_ACTOR`; **`human_admin_1` and `human_admin_2`** = two DISTINCT `mint_test_identity(subject=…, role_claims=("platform-admin",))`; `_propose_join(conn, ref, evidence, *, actor=service_actor)`; **`_confirm_join(conn, ref, *, admin1, admin2)`** — reads the two open governance-queue gate tasks for the join, `admin1` confirms one side (→ `PARTIALLY_CONFIRMED`, re-read state), `admin2` (distinct) confirms the other (→ `VERIFIED`), then `run_projection(conn, OverlayProjection())`; `_reject_join(conn, ref, *, admin)` (only valid pre-VERIFIED); a `_expire_join(conn, ref)` helper driving `fire_due_overlay_expiries` for the VERIFIED-demotion path.
  > **Implementer note (verify):** confirm the two-task/side model — `authority.task_assignees` opens two side-labelled platform-admin tasks; each confirm targets `args['target_event_id'] = _cas_target(state)` at that moment; re-read `fold_overlay_state` between the two confirms. Confirm `mint_test_identity` path (`tests/featuregen/_helpers.py`).
- [ ] **Step 1: Failing test** — absent → PROPOSE; a DRAFT same-fingerprint → SKIP_ACTIVE; a VERIFIED → SKIP_ACTIVE; an ACTIVE fact for the SAME UNORDERED PAIR with a DIFFERENT `fact_key` (different direction/cardinality) → **CONFLICT**.
- [ ] **Step 3: Implement `lifecycle.py`** — `candidate_fingerprint(evidence)`; `build_join_ref(evidence, source) -> ApprovedJoinRef`; `Action ∈ {PROPOSE, SKIP_ACTIVE, CONFLICT, REPROPOSE}`; `decide_action(conn, ref, evidence)` adjudicates against **ALL `approved_join` facts touching the unordered table pair `{from_table, to_table}`** (enumerate via the source's `approved_join` gate tasks / overlay read-model), not a single `fact_key`: a same-`fact_key` active fact → SKIP_ACTIVE; a different-`fact_key` ACTIVE fact for the pair → CONFLICT; a terminal `REJECTED`/`STALE`/`REVERIFY` same key with a materially-changed bucket/namespace → REPROPOSE; none → PROPOSE. **Do not reference an `"EXPIRED"` folded status** (it folds to `REVERIFY`).
- [ ] Fail→pass→commit.

---

## Task 7: Propose wiring (evidence via `evidence_ref`; grain-gated)

**Files:** Create `.../passc/propose.py`; Test `test_propose.py` (consumes the Task-6 conftest).

- [ ] `propose_join_candidates(conn, source, evidences, *, actor) -> None` — for each **strong** evidence with `cardinality_status == INFERRED_FROM_CONFIRMED_GRAIN` **and** `proposed_cardinality is not None` (a hard gate — never build an `ApprovedJoinRef(cardinality=None)`): `decide_action`; on PROPOSE/REPROPOSE, **pre-mint the reviewer evidence** via `write_evidence(conn, fact_key=<join key>, table_snapshot_at=…, row_count=0, sample_size=0, profile_version=ALGORITHM_VERSION, thresholds_used={}, metric_values=<asdict(evidence): score/reason_codes/explanation/signals/namespace/bucket>, created_by=identity_to_jsonb(actor))` (producer/strength set on the evidence row as STRUCTURAL_CONNECTOR/PROPOSED), then `propose_fact(conn, Command("propose_fact","overlay_fact",None,{"ref":ref,"fact_type":"approved_join","proposed_value":value,"evidence_ref":evidence_ref}, actor, proposal_fingerprint(value)))`. Fail-soft, adapter-gated, counters. A CONFLICT → log + counter (no second governed proposal). A weak/None-cardinality evidence is never sent here (routed to the readiness diagnostic).
- [ ] **Tests** — a strong+grain candidate → `approved_join` folds to `DRAFT`; **`get_task_proposal` returns `evidence` whose `metric_values` surfaces the score + reason codes + explanation** (assert against the reviewer read, not the raw `proposed_value`); a weak candidate is NOT proposed; a propose error is swallowed (fail-soft). Use `_propose_join`/`get_task_proposal` from the conftest.
> **Implementer note:** confirm `write_evidence`'s exact signature + how `evidence_ref` flows onto the DRAFT payload/gate task (`proposal_commands.py:107-131,157`). The proposer `actor` is `_ENRICH_ACTOR` (four-eyes vs the two human confirmers).
- [ ] Fail→pass→commit.

---

## Task 8: Migration 0987 + reverse projector (declared-spare, orientation/scope-safe) + async demotion hook + governed edge filter

**Files:** `db/migrations/0987_graph_edge_join_authority_links.sql`, `.../passc/projection.py`; modify `join_path.py`/`entity.py`/`feature_assist.py` (governed filter), `confirmation_commands.py` + `expiry.py` (async hook); Test `test_projection.py`.

- [ ] **Migration 0987:** `ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS approved_join_fact_key text; … approved_join_event_id text; … approved_join_status text; … authority_updated_at timestamptz;`
- [ ] **Governed edge filter** (spec §14, flag-off-safe): in `find_join_path` (`join_path.py:53`), `entity.py:224`, `feature_assist.py:~612`, add `AND (e.approved_join_fact_key IS NULL OR e.approved_join_status='VERIFIED')` — a flag-off declared edge (NULL link) still traverses byte-for-byte; a governed edge traverses only when VERIFIED.
- [ ] **`project_confirmed_joins(conn, *, source, pairs, now=None)`** — DECLARED-SPARE, orientation/scope-safe, idempotent:
  - Enumerate `pairs` **from the source's `approved_join` gate tasks / facts** (never from raw `graph_edge` rows).
  - For each pair, render endpoints in **public** graph scope: `from_ref/to_ref = f"public.{table}.{column}"` (match `graph_node.object_ref`, NOT the `src::public.…` evidence form).
  - `resolve_fact` (VERIFIED-only, `now=now`). **VERIFIED** → **DELETE any `joins` edge for the unordered pair in either orientation**, then INSERT exactly one operational edge in the confirmed direction with `cardinality`, `approved_join_fact_key`, `approved_join_event_id=.provenance['confirmed_event_id']`, `approved_join_status='VERIFIED'`, `authority_updated_at=now`. **Non-VERIFIED** → set `authority='display_only'` + clear fact links for **both** orientations, but **ONLY for edges whose `approved_join_fact_key IS NOT NULL`** (never demote a file-declared edge).
- [ ] **Async demotion hook:** when an `approved_join` leaves VERIFIED — in `reject_fact` (pre-VERIFIED reject is a no-op on an operational edge; but a REVERIFY/STALE via) and in `fire_due_overlay_expiries`/`_apply_expiry` — `UPDATE graph_edge SET authority='display_only', approved_join_status=<new>, authority_updated_at=now WHERE approved_join_fact_key=<key>`. This closes the ingest-latency window (a rejected/expired join stops traversing immediately, not at next upload).
- [ ] **Tests:** VERIFIED join → operational edge (public-scope, single row, fact links) → `find_join_path` traverses; a **confirmed direction that reverses the declared display edge** → exactly ONE operational row, no stale duplicate; a **flag-off declared operational edge (fact_key NULL) is NEVER demoted** by the projector; a fact taken to STALE via `fire_due_overlay_expiries` → the async hook demotes → `find_join_path` no longer traverses (no re-ingest).
- [ ] Fail→pass→commit.

---

## Task 9: Relationship readiness (distinct dimension; weak recomputed)

**Files:** Modify `overlay/upload/readiness.py`; Test `test_readiness_relationships.py`.

- [ ] Add a **distinct** per-table relationships dimension — its own status enum `RelationshipStatus ∈ {no_candidates, candidate_proposed, weak_candidates_only, confirmed, conflicting}` on a new `RelationshipReadiness` view (do NOT overload the 4-value `ReadinessRequirement.status` Literal). Derivation: fold the table's `approved_join` facts (VERIFIED→`confirmed`; DRAFT/PARTIALLY→`candidate_proposed`; a pair CONFLICT→`conflicting`) **and recompute weak candidates on read** (`block_candidates`→`score` over the table's `ColMeta`; any weak, non-proposed pair → `weak_candidates_only` when there are no stronger). Pass C is pure/deterministic, so recomputation is stable — no store needed. State the scorer + `ColMeta`-assembly dependency.
- [ ] Tests: proposed → `candidate_proposed`; two admins confirm → `confirmed`; only weak → `weak_candidates_only`; none → `no_candidates`. Fail→pass→commit.

---

## Task 10: Ingest wiring + governed mode (own savepoint)

**Files:** Modify `overlay/upload/ingest.py`, `overlay/upload/graph.py`; Test `test_passc_ingest.py`.

- [ ] `pass_c_enabled()` → `os.environ.get("OVERLAY_PASS_C","0")=="1"`.
- [ ] **Governed mode (pin it, don't leave soft):** in `graph.py`, make the governed predicate also fire under Pass C by reading the env directly (avoid an import cycle — `graph.py` must NOT import `pass_c_enabled`): `def governed_joins_enabled(): return os.environ.get("OVERLAY_GOVERNED_JOINS")=="1" or os.environ.get("OVERLAY_PASS_C")=="1"`. This is read inside `build_graph` (before the seam), so under `OVERLAY_PASS_C` declared `joins_to` edges are written `display_only`. Widen the `_propose_governed_joins` gate at `ingest.py:~616` to `if governed_joins_enabled() or pass_c_enabled():` so declared joins are ROUTED to `approved_join` proposals (not stranded display-only).
- [ ] **Pass C block** (behind `pass_c_enabled()`), in **its own `with conn.transaction(): … except Exception: counters+logger`** (NOT the bare-except of `_propose_governed_joins`): assemble `ColMeta` for the upload's columns from `graph_node` + Phase-1/2 evidence (concept/entity/bian/is_grain); `block_candidates`→`score`; **strong+grain → `propose_join_candidates`** (weak/None-cardinality are diagnostics, recomputed by Task 9, not proposed here). Proposer = `_ENRICH_ACTOR`. Mint `mint_id("psc")` for the evidence snapshot.
- [ ] **End-of-ingest projector** (declared-spare, so unconditional is safe): inside a `with conn.transaction(): … except`, gated on `projection_lag(conn,"overlay")==0` (mirror Phase 2), call `project_confirmed_joins(conn, source=catalog_source, pairs=<from approved_join facts/tasks>)`. Because the projector only touches fact-linked edges, a flag-off pure-declared catalog is a no-op → byte-for-byte.
- [ ] Tests: `OVERLAY_PASS_C` unset → ingest byte-for-byte (spy: no candidates, no facts, no edge changes); set → a declared `joins_to` becomes `display_only` **and** routed to an `approved_join` proposal, `find_join_path` returns None pre-confirm; a concept-shared pair → strong candidate proposed. Fail→pass→commit.

---

## Task 11: Integration — the authority proof (dual confirm, production demotion)

**Files:** Test `test_passc_integration.py`.

- [ ] Glossary upload (2 tables sharing a `cif_id` concept, `customer.cif_id` a confirmed grain), `OVERLAY_PASS_C=1`:
  1. strong candidate proposed (`approved_join` `DRAFT`); `find_join_path(txn, customer)` → **None** (fail-closed);
  2. **`_confirm_join(admin1, admin2)`** (two distinct platform-admins) → `PARTIALLY_CONFIRMED` → `VERIFIED`; `project_confirmed_joins` → `find_join_path` **traverses**; relationships readiness = `confirmed`;
  3. **demote via the PRODUCTION path** — `_expire_join` (drive `fire_due_overlay_expiries`) → the async hook demotes the edge → `find_join_path` → None (no re-ingest). Separately, a pre-VERIFIED `_reject_join(admin1)` on a fresh candidate → never operationalized.
- [ ] Run `tests/featuregen/overlay/upload/ -q` + `tests/featuregen/overlay/ -q` — all green; flag-off byte-for-byte holds.
- [ ] Commit.

---

## Self-Review (spec coverage)

| Spec § | Plan |
|---|---|
| §6 gates/negatives/weights/buckets (+ MANY_TO_MANY weak-cap, word-boundary negatives) | Tasks 2/4/5 |
| §7 namespace enum + POSSIBLE reachable + mixed-leaf | Task 3 |
| §8 related_terms gating | Task 5 (`related_terms_key_link`) |
| §9 direction/cardinality + **no confirm-override (reject→re-propose)** | Task 5/6/7; spec §9 corrected |
| §10 evidence payload via `evidence_ref` | Tasks 1 + 7 |
| §11 dedupe/lifecycle + **unordered-pair CONFLICT** | Task 6 |
| §13 governed source of truth + **dual-confirmer** | Task 10 (governed mode) + Task 6/7/11 (two admins) |
| §14 projection (**declared-spare, orientation/scope-safe, async demotion, VERIFIED filter**) | Task 8 |
| §15 weak/suppressed | Task 5 buckets + Task 9 (recomputed) + Task 10 (suppressed→counters) |
| §16 relationship readiness (distinct dimension) | Task 9 |
| §17/§18 composite/self-join deferred | Task 4 |
| §19 versioning | Task 1 |
| §22 fail-closed BOTH directions via production demotion | Task 11 |

**Deferred to Phase 3B:** LLM challenger, exploration mode, `INCOMPATIBLE`-via-challenger.

**Placeholder scan:** the "Implementer note (verify)" callouts (id-word tuning; two-task confirm model; `write_evidence` signature) are grounding directives with a recommended default. **Type consistency:** `ColMeta`, `JoinCandidateEvidenceV1`, `ApprovedJoinRef`, the namespace/cardinality enums, and `RelationshipStatus` are used identically across tasks.

---

## Execution Handoff

**Start Phase 3A from a fresh worktree off the updated `main`** (Phase 2 + 3B.2A) so Pass C sits on everything it references; carry this plan + the v2 spec across. Then **Subagent-Driven** (Fable-5 implementers, Opus task-reviewers, adversarial whole-branch review before merge). Which approach?
