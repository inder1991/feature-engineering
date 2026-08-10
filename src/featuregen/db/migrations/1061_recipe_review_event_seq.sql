-- src/featuregen/db/migrations/1061_recipe_review_event_seq.sql
-- BR-23 schema fix (D7 reservation 1061 appended in this same commit): deterministic event
-- ordering. The 1060 reads ordered by (recorded_at, event_id) — but two events appended in ONE
-- transaction share now() (transaction-start time), and ULID ordering within one millisecond is
-- not guaranteed monotonic, so "current" could resolve to the SUPERSEDED event. A bigserial is
-- assigned at INSERT, monotonic and collision-free: the append ORDER is now a stored fact, not a
-- timestamp coincidence. Additive; the store is a day old with no production rows to backfill
-- meaningfully (existing rows get sequence values in physical order, which is the best available
-- statement about their past).
ALTER TABLE recipe_review_event ADD COLUMN IF NOT EXISTS recorded_seq BIGSERIAL;
CREATE INDEX IF NOT EXISTS recipe_review_event_seq_idx
    ON recipe_review_event (recipe_id, recorded_seq);
