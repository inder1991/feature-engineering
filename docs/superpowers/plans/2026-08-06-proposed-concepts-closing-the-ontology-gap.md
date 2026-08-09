# Proposed Concepts — Closing the Ontology-Gap Loop

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When no registry concept fits a column, the LLM's proposed concept is stored, assigned to the column and usable — for display, search and feature generation — while remaining unable to gate a join until a human promotes it.

**Architecture:** A second, durable vocabulary tier. Tier 1 is the governed `CONCEPT_REGISTRY` in `concepts.py` — unchanged, source-controlled, join-eligible. Tier 2 is a `proposed_concept` table written by adjudication, carrying `llm/proposed` authority. Resolution reads both; join candidacy reads only tier 1. Promotion is a governed human action that moves a term from tier 2 to tier 1.

**Tech Stack:** Python 3.12 / FastAPI / psycopg3 / PostgreSQL; React + TypeScript + Vitest; pytest.

## The invariant this plan deliberately reverses

`semantic_gap.py` states three rules for an ontology-gap suggestion. **Two are preserved by this plan. One is reversed on purpose, and the reversal must be argued in the commit message, not slipped past.**

| Current rule | This plan |
|---|---|
| *"NEVER edits `CONCEPT_REGISTRY` — an automatically growing ontology is how a vocabulary stops meaning anything"* | **PRESERVED.** Tier 2 is a separate store. The governed registry grows only by human promotion. |
| *"NEVER becomes field evidence — a gap is the absence of a classification, so writing a `concept` proposal from one would assert exactly the thing the model just said it could not say"* | **REVERSED.** See the argument below. |
| *"NEVER becomes a join key, an operand or an executable fact (functional rule 13)"* | **PRESERVED**, and enforced structurally — see Task 4. |

**The argument for reversing rule 2.** The adjudicator returns two claims in one answer: `selected_concept: "unclassified"` (none of *your 324* fit) and `ontology_gap.proposed_label` (here is one that would). Those are not contradictory — the first is about the existing vocabulary, the second proposes an addition to it. Writing `concept = record_audit_user` **at tier 2** asserts "a concept fits this column and it is not yet in your registry," which is exactly what the model said. The original rule is sound only while the two tiers are indistinguishable; once the tier is explicit and carried in the authority, the objection dissolves.

What the current behaviour costs: the column is left `unclassified`, which is strictly less information than the model produced, and the suggestion is displayed once and never actionable — so the identical gap is re-derived, at full LLM cost, on every upload forever.

## Global Constraints

- **Tier 2 concepts can never gate join candidacy.** `Concept.namespace` is the only join-candidacy axis and a proposed concept has none. Task 4 proves this with a test rather than assuming it.
- **The governed registry stays source code.** No migration and no route may insert into `CONCEPT_REGISTRY`. Promotion emits a reviewed code change; it does not mutate the tuple at runtime.
- **Authority is always visible.** A tier-2 concept carries `producer='llm', strength='proposed'` and surfaces through the existing `semantic_authority` map. A consumer can always tell the tiers apart.
- **Reuse the existing suggestion machinery.** `OntologyGapSuggestionV1` already validates shape (`MAX_LABEL_LEN=64`, `MAX_DEFINITION_LEN=400`, `MAX_ALIASES=8`), rides the immutable `structured_result`, and has a migration-1046 subject/current pointer that supersedes rather than duplicates. This plan adds a durable store and a promotion path; it does not re-invent validation.
- **Migration 1058** is this plan's only reservation.
- Backend tests `uv run pytest <path> -v`; frontend `npm test -- <path>` from `frontend/`.

---

### Task 1: The `proposed_concept` store

**Files:**
- Create: `src/featuregen/db/migrations/1058_proposed_concept.sql`
- Create: `src/featuregen/overlay/upload/proposed_concepts.py`
- Test: `tests/featuregen/overlay/upload/test_proposed_concepts.py`

**Interfaces:**
- Produces: `record_proposed_concept(conn, *, name, definition, parent_concept, aliases, catalog_source, ingestion_run_id, structured_result_id) -> str` (returns the row id); `resolve_proposed_concept(conn, name) -> ProposedConceptV1 | None`; `list_proposed_concepts(conn, *, status) -> list[ProposedConceptV1]`. Tasks 2, 3, 5 consume these exact names.

- [ ] **Step 1: Write the migration**

```sql
-- src/featuregen/db/migrations/1058_proposed_concept.sql
-- Tier 2 of the vocabulary: an LLM-proposed concept the governed registry does not yet contain.
-- Deliberately NOT a mirror of concepts.CONCEPT_REGISTRY — it carries no `namespace`, which is the
-- join-candidacy axis, so a proposed concept is structurally incapable of gating a join. Promotion
-- to tier 1 is a reviewed source change; this table never becomes the registry.
CREATE TABLE IF NOT EXISTS proposed_concept (
    id                   text        PRIMARY KEY,
    name                 text        NOT NULL,
    definition           text        NOT NULL,
    parent_concept       text        NULL,          -- a tier-1 registry member, or NULL
    aliases              text[]      NOT NULL DEFAULT '{}',
    status               text        NOT NULL DEFAULT 'proposed'
                             CHECK (status IN ('proposed','promoted','rejected','merged')),
    merged_into          text        NULL,          -- a tier-1 name, set when status = 'merged'
    first_seen_source    text        NOT NULL,
    first_seen_run_id    text        NULL,
    structured_result_id text        NULL,
    occurrences          integer     NOT NULL DEFAULT 1,
    created_at           timestamptz NOT NULL DEFAULT now(),
    decided_at           timestamptz NULL,
    decided_by           text        NULL,
    CONSTRAINT proposed_concept_name_unique UNIQUE (name)
);
CREATE INDEX IF NOT EXISTS proposed_concept_status_idx ON proposed_concept (status);
```

`UNIQUE (name)` is what stops vocabulary sprawl: the same label proposed by a second catalog increments `occurrences` rather than creating a duplicate row — and a high `occurrences` is the strongest signal a reviewer has that a term is real.

- [ ] **Step 2: Write the failing test**

```python
def test_a_second_proposal_of_the_same_name_increments_rather_than_duplicates(db):
    first = record_proposed_concept(
        db, name="record_audit_user", definition="The user account that created the record.",
        parent_concept=None, aliases=("created_by",), catalog_source="cib",
        ingestion_run_id="ingrun_1", structured_result_id=None)
    second = record_proposed_concept(
        db, name="record_audit_user", definition="Who created the row.",
        parent_concept=None, aliases=(), catalog_source="payments",
        ingestion_run_id="ingrun_2", structured_result_id=None)
    assert first == second
    got = resolve_proposed_concept(db, "record_audit_user")
    assert got.occurrences == 2
    assert got.first_seen_source == "cib"      # first writer keeps provenance


def test_a_rejected_name_is_not_resolvable_as_a_concept(db):
    record_proposed_concept(db, name="junk", definition="d", parent_concept=None, aliases=(),
                            catalog_source="cib", ingestion_run_id=None,
                            structured_result_id=None)
    set_proposed_concept_status(db, "junk", status="rejected", actor="alice")
    assert resolve_proposed_concept(db, "junk") is None
```

- [ ] **Step 3: Run it — expect FAIL** (`ModuleNotFoundError: proposed_concepts`)

Run: `uv run pytest tests/featuregen/overlay/upload/test_proposed_concepts.py -v`

- [ ] **Step 4: Implement the module**

`proposed_concepts.py` holds `ProposedConceptV1` (a frozen dataclass mirroring the table), the three functions named in **Interfaces**, plus `set_proposed_concept_status(conn, name, *, status, actor, merged_into=None)`. Reuse `semantic_gap`'s existing bounds (`MAX_LABEL_LEN`, `MAX_DEFINITION_LEN`, `MAX_ALIASES`) rather than declaring new ones — import them. `resolve_proposed_concept` returns `None` for any status other than `'proposed'` and `'promoted'`, so a rejected term stops resolving the moment it is rejected.

- [ ] **Step 5: Run the tests — expect PASS**, then run the migration suite:

`uv run pytest tests/featuregen/db -v`

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/db/migrations/1058_proposed_concept.sql \
        src/featuregen/overlay/upload/proposed_concepts.py \
        tests/featuregen/overlay/upload/test_proposed_concepts.py
git commit -m "feat(ontology): add the tier-2 proposed_concept store (migration 1058)"
```

---

### Task 2: Adjudication assigns the proposed concept

**Files:**
- Modify: `src/featuregen/overlay/upload/semantic_adjudication.py`
- Modify: `src/featuregen/overlay/upload/semantic_gap.py` (docstring only — the reversed rule)
- Test: `tests/featuregen/overlay/upload/test_semantic_adjudication.py`

**Interfaces:**
- Consumes: `record_proposed_concept` from Task 1.
- Produces: an adjudication whose `ontology_gap` is present now also writes `concept` field evidence with `producer='llm', strength='proposed'` and the proposed label as the value.

- [ ] **Step 1: Write the failing test**

```python
def test_a_gap_suggestion_assigns_the_proposed_concept_as_llm_evidence(db, adjudicated_column):
    """The column stops being `unclassified` and carries the proposed label at tier 2."""
    outcome = adjudicate_semantics(db, _client_returning_gap(
        label="record_audit_user", definition="The user account that created the record."),
        [adjudicated_column.row], concepts={adjudicated_column.hash: UNCLASSIFIED},
        glossary=adjudicated_column.glossary, bindings=adjudicated_column.bindings,
        source_snapshot_id=adjudicated_column.snapshot, actor=_ACTOR,
        critic_outcomes=None, ingestion_run_id="ingrun_1", bundles=None)

    assert outcome["gap_suggested"] == 1
    ev = _active_evidence(db, adjudicated_column.logical_ref, field_name="concept")
    assert ev.proposed_value == "record_audit_user"
    assert (ev.producer, ev.strength) == ("llm", "proposed")
    assert resolve_proposed_concept(db, "record_audit_user") is not None


def test_a_second_catalog_reuses_an_existing_proposal_rather_than_coining_a_new_one(
        db, adjudicated_column):
    """Without this, two catalogs coin two names for one idea and the vocabulary fragments.

    `UNIQUE (name)` in Task 1 only dedups IDENTICAL spellings — `record_audit_user` and
    `creation_user_ref` both survive it. The defence has to be upstream: the model must SEE the
    live tier-2 terms and be told to reuse one when it fits.
    """
    record_proposed_concept(db, name="record_audit_user", definition="Who created the record.",
                            parent_concept=None, aliases=(), catalog_source="cib",
                            ingestion_run_id=None, structured_result_id=None)
    payload = _adjudication_payload(db, adjudicated_column)
    assert "record_audit_user" in payload["catalog_metadata"]["proposed_vocabulary"]
    assert "reuse it exactly" in payload["redacted_intent"]
```

- [ ] **Step 2: Run both — expect FAIL** (no evidence row, and no `proposed_vocabulary` key)

- [ ] **Step 3: Show the model the live tier-2 terms, and tell it to reuse them**

Adjudication already sends the model the 324-term registry via `classification_vocabulary`. Add a second, clearly separate section carrying the live proposals, and amend the instruction so the model prefers reuse over coinage:

```python
    # Anti-fragmentation: without this, catalog A coins `record_audit_user` and catalog B coins
    # `creation_user_ref` for the same idea. Task 1's UNIQUE(name) cannot catch that — two spellings
    # of one meaning both survive it — so the defence has to be here, before the coinage happens.
    # Kept SEPARATE from `classification_vocabulary`: a proposed term is not a registry member, and
    # collapsing the two sections would let the model return one as a `selected_concept`.
    metadata["proposed_vocabulary"] = [
        p.name for p in list_proposed_concepts(conn, status="proposed")
    ][:_MAX_PROPOSED_VOCABULARY]
```

and extend `_ADJUDICATION_INSTRUCTION` with one sentence:

```
"A `proposed_vocabulary` section lists terms an earlier catalog already proposed for gaps like "
"this one. If one of them fits, reuse it exactly rather than coining a new name — a second name "
"for the same idea fragments the vocabulary. Describe a gap only when neither the registry nor "
"the proposed terms cover the column."
```

Bound the section with `_MAX_PROPOSED_VOCABULARY = 200`, ordered by `occurrences DESC` so the most-corroborated terms survive the cut. A reused name increments `occurrences` rather than creating a row — so reuse also strengthens the promotion signal.

- [ ] **Step 4: Write the proposed concept and assign it**

In the branch of `adjudicate_semantics` that currently records the gap suggestion, add — **after** the existing suggestion write, so a failure to assign never loses the suggestion:

```python
        # Tier 2 (2026-08-06): the gap is no longer display-only. Record the proposed term in the
        # `proposed_concept` store and ASSIGN it to this column as `llm/proposed` evidence. This
        # reverses semantic_gap's "never becomes field evidence" rule deliberately: the adjudicator
        # said no REGISTRY concept fits AND named one that would, and those are different claims.
        # The value is tier 2 — it carries no namespace, so it cannot gate a join (see Task 4).
        record_proposed_concept(
            conn, name=gap.proposed_label, definition=gap.definition,
            parent_concept=gap.parent_concept, aliases=tuple(gap.aliases),
            catalog_source=catalog_source, ingestion_run_id=ingestion_run_id,
            structured_result_id=result_id)
        _write_llm_field_evidence(
            conn, logical_ref=evidence_ref, field_name="concept",
            value=gap.proposed_label, source_snapshot_id=source_snapshot_id)
```

- [ ] **Step 5: Correct the module docstring it contradicts**

In `semantic_gap.py`, replace the second bullet. Leaving a docstring asserting a rule the code no longer follows is worse than the behaviour change itself:

```
* it becomes `llm/proposed` field evidence at TIER 2 (`proposed_concept`, migration 1058) — the
  adjudicator said no REGISTRY concept fits and named one that would, which are different claims.
  It still NEVER edits `CONCEPT_REGISTRY`, and a tier-2 concept carries no `namespace`, so it
  cannot gate join candidacy;
```

- [ ] **Step 6: Run the tests — expect PASS**

Run: `uv run pytest tests/featuregen/overlay/upload/test_semantic_adjudication.py tests/featuregen/overlay/upload/test_semantic_gap.py -v`

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/overlay/upload/semantic_adjudication.py \
        src/featuregen/overlay/upload/semantic_gap.py \
        tests/featuregen/overlay/upload/test_semantic_adjudication.py
git commit -m "feat(ontology): assign an LLM-proposed concept at tier 2 instead of leaving unclassified

Deliberately reverses semantic_gap's 'never becomes field evidence' rule. The adjudicator returns
two distinct claims — no registry concept fits, AND here is one that would — and tier 2 records the
second without asserting the first. Tier-2 concepts carry no namespace and cannot gate a join."
```

---

### Task 3: Resolution reads both tiers

**Files:**
- Modify: `src/featuregen/overlay/upload/concepts.py` (`concept_record`, `is_known_concept`)
- Test: `tests/featuregen/overlay/upload/test_concepts.py`

**Interfaces:**
- Consumes: `resolve_proposed_concept`.
- Produces: `concept_record(name, conn=None)` returns a tier-1 `Concept` unchanged, or a tier-2 shim when `conn` is supplied and the name is a live proposal. `conn=None` (every pure caller) is byte-for-byte unchanged.

- [ ] **Step 1: Write the failing test**

```python
def test_a_proposed_concept_resolves_for_display_but_carries_no_namespace(db):
    record_proposed_concept(db, name="record_audit_user", definition="d", parent_concept=None,
                            aliases=(), catalog_source="cib", ingestion_run_id=None,
                            structured_result_id=None)
    got = concept_record("record_audit_user", conn=db)
    assert got is not None
    assert got.definition == "d"
    assert got.namespace is None, "a tier-2 concept must never carry a join-candidacy namespace"


def test_tier_one_resolution_is_unchanged_without_a_connection():
    assert concept_record("customer_id") is not None
    assert concept_record("record_audit_user") is None      # pure path sees tier 1 only
```

- [ ] **Step 2: Run it — expect FAIL** (`concept_record` takes no `conn`)

- [ ] **Step 3: Add the optional second tier**

```python
def concept_record(name: str, *, conn=None) -> Concept | None:
    """The registry entry for `name`, or None.

    With `conn`, ALSO resolves tier-2 proposed concepts (migration 1058) — returned as a `Concept`
    with `namespace=None` and `group="proposed"`, so every existing namespace-keyed gate (join
    candidacy, bridge discovery) excludes it without a new check. `conn=None` is the pure tier-1
    path every existing caller uses and is unchanged.
    """
    hit = CONCEPT_REGISTRY.get(canonical_concept_name(name))
    if hit is not None or conn is None:
        return hit
    from featuregen.overlay.upload.proposed_concepts import resolve_proposed_concept
    proposed = resolve_proposed_concept(conn, name)
    if proposed is None:
        return None
    return Concept(name=proposed.name, group="proposed", definition=proposed.definition,
                   is_a=proposed.parent_concept, namespace=None)
```

- [ ] **Step 4: Run the tests — expect PASS**, plus the full concepts suite:

`uv run pytest tests/featuregen/overlay/upload/test_concepts.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/concepts.py tests/featuregen/overlay/upload/test_concepts.py
git commit -m "feat(ontology): resolve tier-2 proposed concepts for display, never for joins"
```

---

### Task 4: Prove a proposed concept can never gate a join

**Files:**
- Test only: `tests/featuregen/overlay/upload/test_proposed_concept_join_safety.py` (create)

**Interfaces:** none. This task adds no production code — it pins the safety property Task 3 relies on.

The property is meant to fall out of the existing design: identifier concepts must declare a `namespace`, bridge candidacy gates on shared namespace, and a tier-2 concept has `namespace=None`. **A safety property nobody tested is a safety property nobody has.**

- [ ] **Step 1: Write the test**

```python
"""A tier-2 concept must be structurally incapable of making two columns join candidates.

`Concept.namespace` is the ONLY join-candidacy axis ("two columns are join-candidates iff their
concepts share a namespace"). A proposed concept carries None. This pins that — if a future change
gives tier 2 a namespace, or moves candidacy off namespace, this test is the alarm.
"""

def test_two_columns_sharing_a_proposed_concept_are_not_bridge_candidates(db):
    record_proposed_concept(db, name="record_audit_user", definition="d", parent_concept=None,
                            aliases=(), catalog_source="cib", ingestion_run_id=None,
                            structured_result_id=None)
    left = _column(catalog="cib", table="customer", column="create_user_nm",
                   concept="record_audit_user")
    right = _column(catalog="payments", table="txn", column="created_by",
                    concept="record_audit_user")
    assert bridge_candidates([left, right]) == []


def test_a_promoted_concept_with_a_namespace_IS_a_candidate(db):
    """The negative test alone would pass on a broken builder that returns [] for everything."""
    left = _column(catalog="cib", table="customer", column="cust_id", concept="customer_id")
    right = _column(catalog="payments", table="txn", column="party_ref", concept="customer_id")
    assert bridge_candidates([left, right]) != []
```

> Reuse this repo's existing candidate-builder helper rather than adding one — `tests/featuregen/overlay/upload/test_bridge_candidates.py` already constructs column views for exactly this shape.

- [ ] **Step 2: Run both — expect PASS**

Run: `uv run pytest tests/featuregen/overlay/upload/test_proposed_concept_join_safety.py -v`

If the first test FAILS, stop: tier 2 is reaching join candidacy and Task 3 must be corrected before anything else ships. If the second fails, the helper is wrong and the first test proves nothing.

- [ ] **Step 3: Commit**

```bash
git add tests/featuregen/overlay/upload/test_proposed_concept_join_safety.py
git commit -m "test(ontology): pin that a tier-2 concept cannot gate join candidacy"
```

---

### Task 5: The review and promotion route

**Files:**
- Create: `src/featuregen/api/routes/proposed_concepts.py`
- Modify: `src/featuregen/api/app.py` (register the router)
- Test: `tests/featuregen/api/test_proposed_concepts_route.py`

**Interfaces:**
- Consumes: `list_proposed_concepts`, `set_proposed_concept_status`.
- Produces: `GET /ontology/proposed-concepts`, `POST /ontology/proposed-concepts/{name}/reject`, `POST /ontology/proposed-concepts/{name}/merge`.

**Promotion is deliberately not an API action.** Accepting a term into tier 1 means adding a `Concept(...)` line with a group, a namespace and a sensitivity — decisions with join and access-control consequences. The route surfaces the queue and lets a reviewer **reject** or **merge into an existing tier-1 concept**; promotion emits a reviewed code change. Anything else would make the registry runtime-mutable, which is the one rule this plan preserves.

- [ ] **Step 1: Write the failing test**

```python
def test_the_queue_lists_proposals_newest_and_most_frequent_first(db, client):
    ...
    body = client.get("/ontology/proposed-concepts").json()
    assert [c["name"] for c in body["items"]] == ["record_audit_user", "settlement_leg_ref"]
    assert body["items"][0]["occurrences"] == 7


def test_merging_retires_the_proposal_and_names_its_target(db, client):
    resp = client.post("/ontology/proposed-concepts/record_audit_user/merge",
                       json={"into": "audit_user_id"})
    assert resp.status_code == 200
    assert resolve_proposed_concept(db, "record_audit_user") is None


def test_merging_into_a_non_registry_name_is_refused(db, client):
    resp = client.post("/ontology/proposed-concepts/record_audit_user/merge",
                       json={"into": "not_a_real_concept"})
    assert resp.status_code == 422
```

- [ ] **Step 2: Run — expect FAIL** (404, no route)

- [ ] **Step 3: Implement the router**

Gate every endpoint with `require_confirmer` — the same `platform-admin` claim the other governance listings use. Order the queue by `occurrences DESC, created_at DESC`: a term three catalogs independently proposed is the strongest candidate for promotion. `merge` validates `into` is a tier-1 `CONCEPT_REGISTRY` member and refuses with 422 otherwise.

- [ ] **Step 4: Register it** in `create_app` beside `app.include_router(semantics.router)`.

- [ ] **Step 5: Run the tests — expect PASS**

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/api/routes/proposed_concepts.py src/featuregen/api/app.py \
        tests/featuregen/api/test_proposed_concepts_route.py
git commit -m "feat(ontology): add the proposed-concept review queue and merge/reject actions"
```

---

### Task 6: The review screen

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/screens/ProposedConceptsScreen.tsx`
- Modify: `frontend/src/nav.ts`, `frontend/src/App.tsx`
- Test: `frontend/src/screens/ProposedConceptsScreen.test.tsx`

**Interfaces:**
- Consumes: the three routes from Task 5.

Every other governance surface in this codebase landed backend-first and the screen never followed — semantic bindings and bridge realizations both. **This task exists so this one does not join them.**

- [ ] **Step 1: Write the failing test**

```tsx
it('lists proposals with their occurrence count and the catalog that first saw them', async () => {
  renderProposedConcepts([
    { name: 'record_audit_user', definition: 'The user account that created the record.',
      occurrences: 7, first_seen_source: 'cib', parent_concept: null, aliases: ['created_by'] },
  ])
  expect(await screen.findByTestId('proposal-record_audit_user')).toHaveTextContent('7 catalogs')
  expect(screen.getByTestId('proposal-record_audit_user')).toHaveTextContent('cib')
})

it('offers merge and reject, and never offers promote', async () => {
  renderProposedConcepts([{ name: 'record_audit_user', definition: 'd', occurrences: 1,
                            first_seen_source: 'cib', parent_concept: null, aliases: [] }])
  expect(await screen.findByRole('button', { name: /merge/i })).toBeEnabled()
  expect(screen.getByRole('button', { name: /reject/i })).toBeEnabled()
  expect(screen.queryByRole('button', { name: /promote/i })).toBeNull()
})
```

The second test is the load-bearing one: promotion is a code change, and a button implying otherwise would be a lie about what the product can do.

- [ ] **Step 2: Run — expect FAIL**

Run: `cd frontend && npm test -- ProposedConceptsScreen`

- [ ] **Step 3: Build the screen**

One row per proposal: the name, the AI's definition, `occurrences` rendered as "seen in N catalogs", the first-seeing catalog, the suggested parent, aliases. Actions: **Merge into…** (a picker over tier-1 concepts) and **Reject**. Follow `GovernanceReviewScreen.tsx`'s existing list-and-act layout rather than inventing one.

For promotion, render a copyable snippet the reviewer pastes into `concepts.py`:

```python
Concept("record_audit_user", "identifier", namespace="TODO — the issuer's value space",
        description="The user account that created the record.")
```

The `TODO` is deliberate and must stay: `namespace` is the join-candidacy axis and only a human knows the issuer. A generated default would silently make two unrelated columns joinable.

- [ ] **Step 4: Wire nav and route**, following how `GovernanceReviewScreen` is registered.

- [ ] **Step 5: Run the tests — expect PASS**

Run: `cd frontend && npm test`

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.ts frontend/src/screens/ProposedConceptsScreen.tsx \
        frontend/src/screens/ProposedConceptsScreen.test.tsx frontend/src/nav.ts frontend/src/App.tsx
git commit -m "feat(frontend): add the proposed-concept review queue screen"
```

---

### Task 7: End-to-end verification

- [ ] **Step 1: Full suites**

`uv run pytest tests/featuregen -q` and `cd frontend && npm test` — no new failures against baseline.

- [ ] **Step 2: Upload CIB and confirm a column that was `unclassified` now carries a proposal**

```sql
SELECT logical_ref, proposed_value, producer, strength
FROM field_evidence
WHERE field_name = 'concept' AND producer = 'llm' AND lifecycle = 'active'
  AND proposed_value IN (SELECT name FROM proposed_concept);

SELECT name, occurrences, first_seen_source, status FROM proposed_concept ORDER BY occurrences DESC;
```

- [ ] **Step 3: Confirm the join gate held**

```sql
-- must return zero rows: no bridge candidate may rest on a tier-2 concept
SELECT * FROM entity_bridge_candidate
WHERE concept IN (SELECT name FROM proposed_concept WHERE status = 'proposed');
```

A non-empty result means Task 4's property broke in production and the flagged bridges must be withdrawn before anything else proceeds.

- [ ] **Step 4: Re-upload the same file and confirm no duplicate proposals**

`occurrences` should increment; the row count must not grow. This is the anti-sprawl property, and a re-upload is the cheapest way to test it.

---

## Out of scope

- **Automatic promotion.** No path may add to `CONCEPT_REGISTRY` at runtime, however strong the evidence. That rule survives this plan intact.
- **Tier-2 concepts in bridge/crosswalk discovery.** Deliberately excluded via the missing namespace; revisit only after promotion volume shows the review step is a real bottleneck.
- **Retrofitting existing `unclassified` columns.** They pick up proposals on their next ingest. A backfill would re-run adjudication across every catalog at full LLM cost for a benefit the next upload delivers free.
