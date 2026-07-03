-- src/featuregen/db/migrations/0900_events_write_once.sql
-- Physical write-once enforcement for the append-only event stream (review BLOCKER #4).
--
-- The `events` table is the immutable event stream, but until now that immutability was
-- producer-convention only (append_event INSERTs, nobody UPDATE/DELETEs) — there was no
-- DB-level guard, so a privileged actor could still tamper with or truncate the stream.
-- This trigger makes any row-level mutation physically impossible, mirroring Phase-02's
-- documents_no_mutation and the security_audit / feature_versions / blob write-once triggers.
-- INSERT (append_event) is unaffected; UPDATE and DELETE are rejected. Statement-level
-- TRUNCATE is covered separately by the WORM grant revoke (Task 6).
--
-- Sorts after the core Python DDL (0002_events) and all 05xx overlay/feature-contract
-- migrations that extend the events table. Idempotent: CREATE OR REPLACE FUNCTION +
-- CREATE OR REPLACE TRIGGER (PostgreSQL 14+), so apply_migrations stays safely re-runnable.
CREATE OR REPLACE FUNCTION events_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'events are append-only: % not allowed on event_id=%',
        TG_OP, COALESCE(OLD.event_id, NEW.event_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER events_no_mutation
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_write_once();
