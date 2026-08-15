-- src/featuregen/db/migrations/1070_recognition_request_identity.sql
-- Task 0 of the recognition repair seam (docs/superpowers/plans/2026-08-15-recognition-repair-seam.md,
-- blocker B2): a recognition attempt records WHICH REQUEST it answered, and which audited provider
-- call produced the answer.
--
-- THE DEFECT THIS CLOSES. 0974 made recognition idempotent on `(intent_id, input_hash)`, and 1024
-- pinned `input_hash` to the redacted USER INPUT and its redaction policy — nothing about the prompt,
-- the output schema, the taxonomy, the semantic validator, or the model. So a re-run of the same
-- objective under a CHANGED build called the provider again, got a different answer, and the
-- `ON CONFLICT DO NOTHING` insert returned the FIRST row's id. The HTTP response was built from the
-- new in-memory result while confirmation and provenance later read the old row: on the live
-- incident, a screen could show "Churn" over a `recognition_id` whose stored status is
-- `technical_failure`. Two answers, one id, and no way to tell which one anything downstream meant.
--
-- WHAT `input_hash` IS, AND WHAT IT NOW CARRIES. `input_hash` was always the IDEMPOTENCY KEY — 0974's
-- own comment calls it that — and the sealed content hash got its OWN column (`input_content_hash`)
-- in 1024 precisely because the two are different questions. This migration WIDENS the key's
-- material without touching the content column: `input_content_hash` keeps meaning "the redacted user
-- input, and nothing else" (`load_recognition_input` re-derives it from the input alone, and 1024's
-- `contract_generation_input` lineage trigger joins on it), while `input_hash` now carries the full
-- REQUEST identity. Neither hash absorbs the other.
--
-- THE COEXISTENCE RULE, and why the old unique key STAYS.
--   * `UNIQUE (intent_id, input_hash)` from 0974 is NOT dropped and NOT replaced. Migrations are
--     applied BEFORE the new image (backend-first), so for a window the OLD code runs against THIS
--     schema — and its insert names that constraint in an `ON CONFLICT (intent_id, input_hash)`
--     clause. Postgres raises "no unique or exclusion constraint matching the ON CONFLICT
--     specification" when the inference finds nothing, so dropping the constraint would turn every
--     recognition into a 500 for the whole deploy window. A correctness fix that breaks the running
--     system on the way in is not a fix.
--   * The widened key therefore rides IN that constraint: a request-identity row writes the same
--     value into `input_hash` and `recognition_request_hash`, so one constraint enforces one rule and
--     a re-run under a changed contract simply lands on a different key — a NEW append-only row.
--   * `recognition_request_hash` is what makes the widening READABLE rather than implied. A row that
--     carries it is keyed by request identity; a row whose value is NULL is keyed by input content
--     alone. That is the ONLY thing the NULL means, and it is the truth about how the row was
--     written — not a gap waiting for a backfill.
--   * NO BACKFILL, and none is possible: 1024 installed `intent_recognition_attempt_no_mutation`,
--     which refuses UPDATE and DELETE on this table outright (the 1060/1061 append-only discipline).
--     A legacy row's request identity was never observed, and inventing one — hashing today's prompt
--     and today's model onto an answer produced by neither — would be a fabricated provenance that
--     then looked exactly like a real one. Legacy rows keep a permanent, truthful NULL.
--   * Readers FAIL CLOSED on that NULL: `find_recognition_attempt` matches on
--     `recognition_request_hash` only, so a legacy row is never served as the answer to a request
--     whose identity nobody recorded. The consequence is bounded and honest — the first re-run of a
--     legacy objective under the new code pays one provider call and writes one properly-keyed row.
--
-- WHY THE CHECK. `recognition_request_hash IS NULL OR input_hash = recognition_request_hash` states
-- the coexistence rule where it cannot drift from the code that relies on it. It passes on every
-- existing row by construction (they are all NULL), so it VALIDATES against a populated legacy table
-- rather than aborting on one — the failure mode CI cannot see.
--
-- `llm_call_ref` — NULLABLE, and deliberately NOT a foreign key. `llm_call` is written by
-- `_record_llm_call_durable` (enrich_llm finding #20) on a SEPARATE connection that commits on its
-- own, precisely so audit evidence survives a rollback of the request transaction; it also degrades
-- to a best-effort write on the request connection when that connection cannot be opened. An FK
-- would couple this INSERT to another transaction's visibility and could turn a degraded audit into
-- a failed recognition — and recognition is FAIL-OPEN by contract: it never blocks generation and
-- never 5xxs. NULL therefore means "no audited call is linked" (a pre-1070 row, or a dispatch that
-- never reached the provider), which is exactly what the recognizer already reports.
ALTER TABLE intent_recognition_attempt
    ADD COLUMN IF NOT EXISTS recognition_request_hash text NULL,
    ADD COLUMN IF NOT EXISTS llm_call_ref             text NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'intent_recognition_attempt_request_is_the_key'
          AND conrelid = 'intent_recognition_attempt'::regclass
    ) THEN
        ALTER TABLE intent_recognition_attempt
            ADD CONSTRAINT intent_recognition_attempt_request_is_the_key
            CHECK (recognition_request_hash IS NULL
                   OR input_hash = recognition_request_hash);
    END IF;
END $$;

-- The lookup this column exists to serve — "has THIS build already answered THIS request for this
-- intent?" — runs before every dispatch, so it is the one that must not scan. PARTIAL: legacy rows
-- are not answers to any recorded request and are never a hit. UNIQUE states the rule directly
-- (one attempt per (intent, request identity)); it cannot fail on existing data, because the
-- predicate excludes every legacy row, and it cannot fail on new data either — the CHECK above
-- makes it exactly the `UNIQUE (intent_id, input_hash)` constraint 0974 already enforces.
CREATE UNIQUE INDEX IF NOT EXISTS intent_recognition_attempt_request_idx
    ON intent_recognition_attempt (intent_id, recognition_request_hash)
    WHERE recognition_request_hash IS NOT NULL;
