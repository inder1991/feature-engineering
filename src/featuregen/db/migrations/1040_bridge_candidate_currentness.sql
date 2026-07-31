-- A current candidate pointer must say whether the candidate is still in the latest bounded
-- derivation.  Assessment revisions remain immutable history; withdrawal only changes the pointer.
--
-- Without this axis, a later ingestion can correctly suppress a code/name mismatch while the old
-- compatibility ledger row remains discoverable forever.  Human review and generic fact lifecycle
-- are deliberately separate: an automatically withdrawn candidate is not fabricated as a human
-- rejection.

ALTER TABLE governed_candidate_current
    ADD COLUMN IF NOT EXISTS lifecycle text NOT NULL DEFAULT 'active';

ALTER TABLE governed_candidate_current
    DROP CONSTRAINT IF EXISTS governed_candidate_current_lifecycle_check;

ALTER TABLE governed_candidate_current
    ADD CONSTRAINT governed_candidate_current_lifecycle_check
    CHECK (lifecycle IN ('active', 'withdrawn'));

CREATE INDEX IF NOT EXISTS governed_candidate_current_lifecycle_idx
    ON governed_candidate_current(lifecycle, candidate_id);
