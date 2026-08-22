# Incident — migration checksum drift, 2026-08-21

**Deploy-blocking. Found before any deploy attempted it. No data loss, no schema damage.**

## The rule this incident establishes

> **Once a migration has been applied anywhere persistent, its file is immutable. Correct it with a
> NEW migration.**

Reconciling `schema_migrations.checksum` to a changed file is **not** an accepted alternative. It was
done once here, as an incident repair, only because schema equivalence had first been proved against
a faithful restore. It must not become practice: the ledger is the evidence that a specific source
was applied, and moving it to match an edited file destroys exactly that evidence.

Corollary: **deployment notes and backup paths do not belong in migration source.** They change after
the file is applied, which is the failure below. They belong in a record like this one.

## What happened

`db/migrations.apply_migrations` records a sha256 of each migration's source and raises
`RuntimeError` on any ledger row whose checksum no longer matches its file — deliberately, because
silently skipping an already-applied migration whose source changed would leave the schema wrong.

Migrations 1094 and 1095 were applied to the live kind cluster, and their files were then edited by
`537e2431` ("the build-set FK must be NOT VALID"). Two ledger rows no longer matched their sources.

The kind deployment's init container runs `python -m featuregen migrate`, so the effect is not a
degraded service but a deploy that cannot start — repeatedly, until reconciled. Nothing was deployed
during the window, so nothing broke. (The general API startup check is fail-open and does not inspect
checksum drift when no migrations are pending, so it would not have surfaced this either.)

Then, while applying 1096, an "APPLIED" note was added to *its* header after applying it —
reproducing the same drift on the new migration within minutes. That is the reason the rule above is
stated as absolutely as it is.

## Why the repair was safe here

Schema equivalence was established rather than assumed: the current 1094 and 1095 files were
re-applied to a scratch restore of the live database. Zero errors, and **no schema difference** —
only pg_dump's random `\restrict` tokens moved. Live already matched the files, so the drift was
purely the ledger's recorded bytes and reconciling it changed no schema.

## Detection

`scripts/verify_migration_ledger.py` compares every SQL migration on disk against a database's
ledger and exits non-zero on drift. Run it before any deploy:

```bash
kubectl exec -n featuregen <pg-pod> -- psql -U postgres -d featuregen -tAc \
  "SELECT name||'|'||checksum FROM schema_migrations" > /tmp/led.txt
python scripts/verify_migration_ledger.py --ledger-file /tmp/led.txt
```

A file with no ledger row is reported and is **not** a failure — that is simply a migration not yet
applied to that database. Drift is the deploy-stopping condition.

## State after the repair

| | |
|---|---|
| ledger rows | 192 (177 SQL files + 15 Python-registered in the `MIGRATIONS` tuple) |
| checksum drift | none |
| latest migration | `1096_formula_draft_retirement` |
| backup taken first | `~/featuregen-backups/featuregen-pre-1096-20260821-161831.sql` (134M) |

## Deployment log

This section, not the migration files, is where "when was this applied" lives. That is the whole
corollary above: a note added to a migration header after it was applied is what caused this
incident twice.

### 1097 + 1098 — applied 2026-08-22

| | |
|---|---|
| migrations | `1097_recipe_formula_evaluation_contract`, `1098_recipe_formula_eval_v2_cases` |
| ledger rows after | 194 |
| backup taken first | `~/featuregen-backups/featuregen-pre-1097-1098-20260822-115244.sql` (134M, completion marker verified) |
| drift after | none; 0 migrations pending |
| existing data | untouched — `formula_draft` 7, `formula_authoring_run` 3, `recipe_formula_eval_run` 0, all unchanged; the three new tables are empty |

Sequence: dump → scratch restore (`featuregen_scratch_1097`) → dry run → **probe** → live → verify →
scratch dropped. The probe is worth keeping as a step: it inserted a contract row into the scratch
restore and confirmed the real database refused both `UPDATE` and `DELETE` with the intended
message, and that the `clean` ↔ `expectation_ref` pairing check fires. Guards that exist only in a
test suite are not guards on the cluster.

▲ **THE DEPLOYED IMAGE COULD NOT HAVE DONE THIS, and that is worth knowing before the next deploy.**
The backend init container runs `python -m featuregen migrate`, but the running image only carries
migrations through **1093** — so it applied nothing, silently and successfully, against a database
at 1096. Migrations 1094–1098 have all been applied by running the migrator from a local checkout
through a port-forward. That is fine and it is what happened; what must not happen is anyone reading
a green init container as evidence that the schema is current. **The image is the floor, not the
authority — check the ledger.**

## ▲ Open: the live database is ahead of `origin/main`

`main` ends at **1089**; this branch reaches **1098** and the live kind database is now at **1098**
too. Deploying anything
from `main` against that cluster would meet a schema it does not know, and its migrate step would
not reconcile the difference.

**Before reusing the shared cluster for unrelated `main` work: merge this lineage, or give the branch
an isolated database.**
