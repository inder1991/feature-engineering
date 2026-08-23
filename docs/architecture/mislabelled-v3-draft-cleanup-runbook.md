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

## 2. RETIRE the drafts — they cannot be deleted

▲ **An earlier revision of this runbook said `DELETE FROM formula_draft`. That statement cannot
execute.** `formula_draft_guard` (migration 1090) raises `formula_draft is append-only` on every
DELETE, and `test_formula_draft_store.py` already pins that refusal. Do not disable the trigger: a
draft is what a person was shown and what an authoring run was spent on, and the failures recorded
on BLOCKED and FAILED drafts are the only evidence those defects were ever real.

Retirement is an APPEND beside the draft (migration 1096):

```python
from featuregen.overlay.upload.formula_draft_store import retire_formula_draft

retire_formula_draft(
    conn, draft_id,
    reason="SCHEMA_CONTRACT_MISMATCH",
    detail="manifest declared 3; stored formula is 2 (pre-027cc923 author contract)",
    retired_by="ops@bank")
```

The draft stays exactly as it was. Readers exclude or label retired drafts via
`retired_draft_ids(conn)` rather than finding them absent — which keeps *"why is this draft gone?"*
an answerable question instead of a gap.

## 3. Regenerate under NEW ids

A re-drafted candidate gets a **new** `formula_draft_id` and therefore a new authoring run.

`_deterministic_run_id(draft_id)` derives the run id from the draft id, so reusing a draft id would
collide with the immutable run row and refuse. That refusal is correct and is the reason new ids are
required rather than preferred.

**Regeneration calls the provider and costs money.** It is a separate operator decision, taken with
a computed cost in front of you — see `candidate-regeneration-runbook.md`, whose rule applies
identically here: *a key becoming usable is not an instruction to spend.*

Once the replacement exists, name it — a separate, later act, which is why the retirement row
allows a null replacement rather than forcing a placeholder that reads as a draft nobody made:

```python
from featuregen.overlay.upload.formula_draft_store import record_draft_replacement

record_draft_replacement(conn, retired_draft_id, replacement_draft_id=new_draft_id)
```

It may be set once. Changing it would make *"what replaced this draft"* a question with two
answers.

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

**And qualify by the DATABASE, not the manifest alone.** `qualifies_as_v3_evidence(versions)` reads
only the version bundle, and there is an interval it cannot see: between `b5249e80` (the version
constants moved to 3) and `027cc923` (the author contract became v3), a run carried a fully current
manifest while being physically driven under `formula_author_turn_v2`. The authority is
`qualifies_as_v3_evidence_for_run(conn, run_id)`, which additionally checks the output schema every
author call was actually REQUESTED under — the one axis no pre-fix run can fake, because the audited
seam records it at the moment of the call rather than declaring it afterwards.

**Live status, audited 2026-08-21 (read-only):** the step-1 query returned **0 rows**. All 7 drafts
are BLOCKED (3) or FAILED (4) and none carries a formula or a hash. Nothing needs retiring or
regenerating today; this runbook is the procedure for when something does.
