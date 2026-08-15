-- src/featuregen/db/migrations/1071_recognition_quality.sql
-- Task 5 of the recognition repair seam (docs/superpowers/plans/2026-08-15-recognition-repair-seam.md):
-- a recognition attempt records WHICH OF FIVE THINGS happened to it, so the API and the UI can stop
-- reporting four different outcomes as the same one.
--
-- THE DEFECT THIS CLOSES. Task 4 gave the recognizer a strict partial partition: when repair fails,
-- the candidates that ARE valid survive and the ones that are not are dropped with a closed reason
-- code. That record lived only on the in-memory `RecognitionResult.dropped_candidates`, and Task 0
-- made the endpoint serve the STORED row rather than the in-memory result — deliberately, because
-- the returned `recognition_id` must name the row the payload was built from. The two are correct
-- separately and lossy together: a partial recovery was served with no trace of the loss, and the
-- screen said "No use-case was recognised" for a technical failure, a genuine unscoped answer, an
-- ambiguous answer with real alternatives, AND a partial recovery that had kept a real scope. Four
-- different facts, one sentence, three of them false.
--
-- WHAT IS STORED, AND WHAT IS DERIVED.
--   * `recognition_disposition` — the FIVE-valued outcome (clean / repaired / partially_recovered /
--     unscoped / technical_failure), stored rather than re-derived at read time. The derivation rule
--     lives in code (`recognition.recognition_quality`) and code changes; a stored row must keep
--     saying what the platform actually served, not what today's rule would make of it. The CHECK
--     below is the closed vocabulary, so a sixth value cannot arrive without a migration that says
--     so.
--   * `repair_attempt_count` — how many turns the MODEL was asked to fix its own answer. NOT
--     provider calls: a truncation retry is not a correction, and counting it as one would tell a
--     user their answer had been questioned when it had not.
--   * `dropped_candidates` — the drop records themselves, `[{"index": int|null, "reason_code": …}]`.
--     The COUNT and the CODE LIST the API serves are projections of this, so they cannot drift from
--     it. `index` is the position in the body the model returned; `null` means the whole RESULT was
--     refused (an aggregate rule) rather than a candidate being at fault. Reason codes are the
--     closed, value-free `RECOGNITION_FAILURE_CODES` — never a model-chosen string — which is what
--     makes this column safe to serve to a browser without further scrubbing.
--
-- ALL THREE ARE NULLABLE, AND THAT NULL IS A FACT. A row written before this migration has no
-- quality, and there is no backfill: 1024's `intent_recognition_attempt_no_mutation` trigger refuses
-- UPDATE and DELETE on this table outright (the append-only discipline 1070 also had to respect).
-- Nor would a backfill be honest — `clean` is not derivable from a legacy row, because "the model
-- answered first time" and "nobody recorded whether it did" are different facts and only one of them
-- is knowable. So `recognition_quality` is served as NULL for those rows and the UI falls back to
-- exactly today's behaviour. `dropped_candidates` is NULL rather than `'[]'` for the same reason:
-- an empty list would claim "nothing was dropped", which nobody observed.
--
-- WHY THE QUALITY IS WRITTEN AT INSERT AND NEVER PATCHED IN. Same trigger. There is no
-- "record the attempt now, add its quality when we know it" path on this table, and designing one
-- would mean either a second table or a mutable row — so `record_recognition_attempt` takes the
-- quality as an argument and writes one complete row. That is also why the recognizer computes it:
-- the repair count exists only on the seam result and is gone by the time a caller holds a
-- `RecognitionResult`.
--
-- ADDITIVE AND IDEMPOTENT: three nullable columns and one CHECK, all `IF NOT EXISTS`-guarded. The
-- CHECK passes on every existing row by construction (they are all NULL), so it VALIDATES against a
-- populated legacy table rather than aborting on one — the failure mode CI, which only ever migrates
-- an empty database, cannot see.
ALTER TABLE intent_recognition_attempt
    ADD COLUMN IF NOT EXISTS recognition_disposition text    NULL,
    ADD COLUMN IF NOT EXISTS repair_attempt_count    integer NULL,
    ADD COLUMN IF NOT EXISTS dropped_candidates      jsonb   NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'intent_recognition_attempt_disposition_is_closed'
          AND conrelid = 'intent_recognition_attempt'::regclass
    ) THEN
        ALTER TABLE intent_recognition_attempt
            ADD CONSTRAINT intent_recognition_attempt_disposition_is_closed
            CHECK (recognition_disposition IS NULL
                   OR recognition_disposition IN ('clean', 'repaired', 'partially_recovered',
                                                  'unscoped', 'technical_failure'));
    END IF;
END $$;

-- THE QUALITY IS WRITTEN WHOLE OR NOT AT ALL. The reader decides "does this row have a quality?"
-- from ONE column, so three columns that could disagree would let it serve half a record as a whole
-- one — a `clean` disposition beside an unrecorded drop set is precisely the lie this migration
-- exists to stop. A repair count is arithmetic the platform did, so it is never negative.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'intent_recognition_attempt_quality_is_coherent'
          AND conrelid = 'intent_recognition_attempt'::regclass
    ) THEN
        ALTER TABLE intent_recognition_attempt
            ADD CONSTRAINT intent_recognition_attempt_quality_is_coherent
            CHECK ((recognition_disposition IS NULL) = (repair_attempt_count IS NULL)
                   AND (recognition_disposition IS NULL) = (dropped_candidates IS NULL)
                   AND (repair_attempt_count IS NULL OR repair_attempt_count >= 0));
    END IF;
END $$;
