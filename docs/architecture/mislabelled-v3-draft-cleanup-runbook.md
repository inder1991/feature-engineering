# Runbook — retiring development drafts that were labelled V3 and authored as V2

**This is an operator action on development data. Nothing automates it.**

## What happened

Until `027cc923`, `formula_draft_worker` declared `formula_schema: 3` on every run it opened and
drove the provider under `AUTHOR_TURN_CONTRACT_V2`, whose instruction reads *"The proposal MUST
declare formula_schema_version 2."* So the manifest said V3, the stored formula was V2, and the
provider contract recorded was `formula_author_turn_v2`. Those drafts may still read **READY**.

They are not servable — admission refuses them now (`FORMULA_SCHEMA_UNSUPPORTED`, naming the
disagreement) — but a READY row that can never be built is confusing development data, and the
evaluator must never consume it as V3 evidence.

## What must NOT happen

**Do not rewrite or relabel the authoring evidence.** `formula_authoring_run` is write-once by
trigger and `formula_authoring_trace_event` is append-only by design. Those rows are the record of
what the software actually did, and editing them to say V3 would manufacture exactly the evidence
this whole fix exists to make impossible. The trace stays. Only the *draft* — a derived, re-creatable
row — is reset.

## 1. Identify

Reads the manifest and the stored proposal separately and compares them. A query keyed on the
manifest alone returns every V3 draft; one keyed on the proposal alone returns every V2 draft.

```sql
SELECT d.formula_draft_id,
       d.state,
       d.authoring_run_id,
       r.versions->>'formula_schema'                                          AS declared,
       e.payload->'result'->'candidate_proposal'->>'formula_schema_version'   AS produced
  FROM formula_draft d
  JOIN formula_authoring_run r          ON r.authoring_run_id = d.authoring_run_id
  JOIN formula_authoring_trace_event e  ON e.authoring_run_id = d.authoring_run_id
 WHERE e.kind = 'completed'
   AND e.payload->'result'->'candidate_proposal' IS NOT NULL
   AND r.versions->>'formula_schema' IS DISTINCT FROM
       (e.payload->'result'->'candidate_proposal'->>'formula_schema_version')
 ORDER BY d.formula_draft_id;
```

The same disagreement, pinned as a test:
`test_author_contract_v3.py::test_a_MISLABELLED_RUN_IS_FINDABLE_by_the_cleanup_query`.

**Audited on 2026-08-21 (read-only, from the plan's earlier live audit):** `formula_draft` held 7
rows, 3 BLOCKED and 4 FAILED, **none carrying a formula**. If that still holds, this runbook has
nothing to reset and step 3 is a no-op — run the query and find out rather than assuming.

## 2. Reset the DRAFTS, never the traces

For each row the query returns:

```sql
DELETE FROM formula_draft WHERE formula_draft_id = :draft_id;
```

The draft is derived from the candidate and the authoring run; deleting it removes a claim
("READY") that is no longer true. The run and its trace are untouched and remain auditable as the
record of a v2 formula authored under a v2 contract — which is what they honestly are.

## 3. Regenerate under NEW ids

A re-drafted candidate gets a **new** `formula_draft_id` and therefore a new authoring run.

`_deterministic_run_id(draft_id)` derives the run id from the draft id, so reusing a draft id would
collide with the immutable run row and refuse. That refusal is correct and is the reason new ids are
required rather than preferred.

**Regeneration calls the provider and costs money.** It is a separate operator decision, taken with
a computed cost in front of you — see `candidate-regeneration-runbook.md`, whose rule applies
identically here: *a key becoming usable is not an instruction to spend.*

## 4. Confirm nothing mislabelled can be consumed

The evaluator qualifies runs by IDENTITY, never by commit date — a date would be a guess about which
build wrote a row that nothing in the row supports:

```python
from featuregen.formula.authoring_versions import qualifies_as_v3_evidence

ok, disagreeing = qualifies_as_v3_evidence(stored_versions)
```

A pre-fix run declares `formula_schema: 3` and is indistinguishable from real V3 evidence on that
axis alone. What it cannot fake is the rest of the tuple — `orchestrator` and `disposition` both
moved to 3 when the V3 state was added, and the author output schema is `formula_author_turn_v3`,
which no pre-fix run was ever requested under.

Re-run the step-1 query after regeneration. It must return zero rows.
