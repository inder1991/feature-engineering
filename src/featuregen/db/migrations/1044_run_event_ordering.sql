-- 1044: a run's event stream is append-ONLY AND ORDERED. fold_run_status raises
-- forever if an event follows a terminal one, and the append-only triggers from
-- 1034 make that state unrepairable — so the database must refuse the write, not
-- merely the read. Races between concurrent INSERTs for one run are closed by the
-- (run_id, seq) PK plus this trigger's max-seq check running BEFORE INSERT.
CREATE OR REPLACE FUNCTION materialization_run_event_ordered()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM materialization_run_event
        WHERE run_id = NEW.run_id
          AND event_kind IN ('GATES_FAILED', 'PUBLISHED', 'PUBLICATION_REFUSED', 'RUN_FAILED')
    ) THEN
        RAISE EXCEPTION 'materialization_run_event: run % already recorded a terminal event',
            NEW.run_id;
    END IF;
    IF EXISTS (
        SELECT 1 FROM materialization_run_event
        WHERE run_id = NEW.run_id AND seq >= NEW.seq
    ) THEN
        RAISE EXCEPTION 'materialization_run_event: seq % does not extend run %',
            NEW.seq, NEW.run_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS materialization_run_event_ordered ON materialization_run_event;
CREATE TRIGGER materialization_run_event_ordered
    BEFORE INSERT ON materialization_run_event
    FOR EACH ROW EXECUTE FUNCTION materialization_run_event_ordered();
