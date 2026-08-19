# Runbook — regenerating candidate sets after the v3 identity reset

**This is an operator action. Nothing automates it, and nothing retries it.**

Candidate generation calls the Anthropic account for every run. Five runs of ~917 candidates is real
money, so this is a step somebody decides to take, not one that fires when a precondition clears. In
particular: **do not retry this when the provider key becomes valid.** A key becoming usable is not
an instruction to spend.

## Why it is needed

Candidates frozen under `contract-considered-v2` were sealed under an identity that did not include
their typed computation — operation, measures, grain, time column, window, grouping. Those six were
dropped by the serializer, so the identity described how a candidate *read* rather than what it
*computes*.

Those revisions remain **readable and auditable**. They are refused from V3 execution with
`CANDIDATE_REGENERATION_REQUIRED`, which names the remedy rather than the version. Regeneration
produces `contract-considered-v3` candidates whose identity covers what they compute.

## Scope

Regenerating candidate sets **only**. It does not require re-uploading either catalog, and must not:
the catalogs are unchanged and re-ingesting them would be a much larger, riskier operation solving a
problem nobody has.

## Before you start

Audited on 2026-08-19, and worth re-running before you begin because it decides how careful this is:

| Depends on the old revisions | Count |
|---|---|
| formula drafts | 7 (3 BLOCKED, 4 FAILED — **none carries a formula**) |
| Gate-1 choices | 0 |
| feature selection revisions | 0 |
| build sets | 0 |
| generation requests | 0 |
| shadow work items | 0 |
| signed contracts | 0 |

```sql
SELECT 'gate1', count(*) FROM contract_gate1_choice
UNION ALL SELECT 'drafts', count(*) FROM formula_draft
UNION ALL SELECT 'selections', count(*) FROM feature_selection_revision
UNION ALL SELECT 'build sets', count(*) FROM build_set_revision
UNION ALL SELECT 'requests', count(*) FROM generation_request
UNION ALL SELECT 'work items', count(*) FROM recipe_formula_shadow_work_item;
```

**The seven drafts need no migration**, because none contains an executable formula. Their historical
failures stay exactly as they are — they are the record of what happened, and rewriting them would
destroy the only evidence that these defects were real.

**No option id is remapped.** An old draft keeps pointing at the revision it was made against. A
silent remap would make a draft claim to be about a candidate it was never authored for.

## The sequence

### 1. Confirm provider authentication with ONE bounded call

Not the full run. One call, and read the result.

```bash
kubectl exec -n featuregen deploy/backend -c backend -- python -c "
from featuregen.intake.llm import current_llm_client
print(current_llm_client().complete(task='ping', prompt='reply with the single word: ok'))"
```

A `provider auth failure` here stops everything. As of 2026-08-19 the cluster key is well-formed
(`sk-ant-…`, from `deploy/kind/.llm-key`, dated 9 Aug) and **rejected** — expired, revoked or out of
credit. Replace it before step 2, and note that replacing it is not itself permission to regenerate.

### 2. Calculate expected calls, tokens and maximum cost

Before spending, not after. Per run: candidates × calls-per-candidate, times five runs. Read the
configured model and its max_tokens from the shipped settings rather than assuming:

```bash
kubectl exec -n featuregen deploy/backend -c backend -- python -c "
from featuregen.formula.audited import current_formula_generation_settings as s
c = s(); print('model', c.model, '| max_tokens', c.max_tokens)"
kubectl exec -n featuregen postgres-… -- psql -U postgres -d featuregen -tAc \"
SELECT considered_revision_id, jsonb_array_length(
  jsonb_path_query_array(considered_json,'\$.options_by_id.keyvalue()')) AS options
  FROM contract_considered_revision WHERE canonicalization_version='contract-considered-v2';\"
```

Write the number down. A maximum cost nobody computed is a maximum cost nobody agreed to.

### 3. Obtain explicit operator approval

With the number from step 2 in front of them. Approval for *this* regeneration, not standing
permission — a later regeneration is a later decision.

### 4. Regenerate ONE representative revision first

The one with the widest candidate mix, so the verification in step 5 exercises the most shapes. Stop
here and verify before spending on the other four.

### 5. Verify, before continuing

All of these, on the regenerated revision:

- [ ] `canonicalization_version = 'contract-considered-v3'`
- [ ] every option carries all six typed fields: `operation_kind`, `measure_refs`, `grain_refs`,
      `time_ref`, `window`, `grouping_refs`
- [ ] `grain_refs` is non-empty on every option — an empty one is a candidate that cannot be drafted
- [ ] `canonical_candidate_identity_hash` verifies against its payload
- [ ] two candidates that compute different things have different identities
- [ ] a draft requested against one reaches at least AUTHORING (proving draftability, without
      requiring the whole run to succeed)

```sql
SELECT count(*) FILTER (WHERE NOT (f ? 'grain_refs')) AS missing_grain_refs,
       count(*) FILTER (WHERE jsonb_array_length(f->'grain_refs') = 0) AS empty_grain,
       count(*) AS total
  FROM contract_considered_revision r,
       LATERAL jsonb_each(r.considered_json->'options_by_id') o,
       LATERAL (SELECT o.value->'canonical_candidate_identity'->'feature' AS f) x
 WHERE r.considered_revision_id = :regenerated;
```

Any failure stops the runbook. Do not proceed to the remaining four to "see if it is just this one".

### 6. Regenerate the remaining four ONE AT A TIME

Stopping on any failure. Not a loop — four decisions, each with the previous one's result visible.

### 7. Preserve the old revisions and drafts; move only the pointers

Old revisions are immutable and stay. Update only what points at "the current revision" for each
intent. Nothing is deleted, and no draft is rewritten.

### 8. Record the lineage and the reason

Old → new revision id, per run, with the reason: *"v2 candidates were sealed under an identity that
did not include their typed computation."* Without the lineage, a later reader finds two revisions
for one intent and no way to tell which superseded which or why.

### 9. Do NOT re-upload either catalog

Explicitly out of scope. The catalogs are unchanged; regeneration reads them, it does not need them
rebuilt.

## After

The five v2 revisions still exist, still refuse execution by name, and the seven drafts still point
at them. That is the intended end state: the record of what was offered and what happened to it
survives, and only new work uses the new identities.
