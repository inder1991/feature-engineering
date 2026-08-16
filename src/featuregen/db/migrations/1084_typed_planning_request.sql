-- src/featuregen/db/migrations/1084_typed_planning_request.sql
-- C-D11 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the typed planning
-- request, stored as a SECOND, INDEPENDENT source so `DECISION_RECORD_TAMPERED` can actually fire.
--
-- THE DEFECT THIS CLOSES. `api/routes/contract.py:374` compares `record["planning_request_hash"]`
-- against `record["decision_manifest"]["planning_request_hash"]` and raises 409 on a mismatch. Both
-- values are written from ONE in-memory object in one statement, so they cannot disagree unless
-- someone edits the database by hand: the branch is unreachable through the production write path.
-- `grep -rn DECISION_RECORD_TAMPERED src/ tests/ frontend/src/` returns exactly one line — the
-- raise itself. No test, no handler. Nothing would have failed if the gate stopped working.
--
-- WHY TWO COLUMNS AND NOT ONE. `request_payload` holds the request's canonical bytes;
-- `planning_request_hash` holds the identity claimed for those bytes, stored SEPARATELY. The reader
-- recomputes the hash from the payload rather than trusting the stored one, so corrupting either
-- column — or the decision record's reference to it — is detectable. A hash stored inside the
-- payload it describes cannot disprove that payload, which is precisely the shape the inert gate
-- already has.
--
-- MIGRATION NUMBER. The plan reserves 1073 for S2 and lists THREE deliverables against that one
-- filename (feature definition · authoring work item + compatibility reader · typed planning
-- request). `apply_migrations` ledgers by filename stem AND byte checksum and raises on drift
-- (db/migrations.py:310-318), so whichever deliverable writes 1073 first owns it and the other two
-- cannot edit the file once it has been applied anywhere. Per the product owner's direction
-- (2026-08-16) each deliverable takes its own filename; this one is 1084, above the reserved band.
--
-- NOT APPLIED. This file is written, not run. Applying it is an operator action.

CREATE TABLE IF NOT EXISTS typed_planning_request (
    considered_revision_id  text        NOT NULL,
    option_id               text        NOT NULL,

    -- The canonical serialization of `FeaturePlanningRequestV1` — the same form its hash is
    -- computed over. Not a summary: the reader reconstructs the typed object from exactly these
    -- bytes, so anything omitted here is a field the recomputed hash would miss.
    request_payload         jsonb       NOT NULL,

    -- The identity claimed for `request_payload`, stored independently of it. This is the value the
    -- reader checks the payload AGAINST; it is never the source of truth for what the request is.
    planning_request_hash   text        NOT NULL,

    recorded_at             timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (considered_revision_id, option_id)
);

-- Append-only in spirit and enforced: a stored request is the evidence a decision was verified
-- against, and a row that can be updated is a row that can be brought into agreement with a
-- tampered decision after the fact — which would defeat the entire point of the second source.
CREATE OR REPLACE FUNCTION typed_planning_request_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'typed_planning_request records are write-once (% / %)',
        OLD.considered_revision_id, OLD.option_id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS typed_planning_request_no_update ON typed_planning_request;
CREATE TRIGGER typed_planning_request_no_update
    BEFORE UPDATE OR DELETE ON typed_planning_request
    FOR EACH ROW EXECUTE FUNCTION typed_planning_request_write_once();

-- Reading back by hash answers "which decisions were made about THIS request", which is the
-- question an auditor asks after a policy or definition changes.
CREATE INDEX IF NOT EXISTS typed_planning_request_by_hash
    ON typed_planning_request (planning_request_hash);
