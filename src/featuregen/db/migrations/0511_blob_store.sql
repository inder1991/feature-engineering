-- src/featuregen/db/migrations/0511_blob_store.sql
-- SP-2 fix F1 (P1-b / P2-c): a minimal WRITE-ONCE blob store so refs that are minted at intake but
-- whose payloads were never persisted become durably resolvable. Two classes of ref land here:
--   * candidate/draft document `body_ref`s — candidate bodies are NOT event-inlined, so without a
--     durable store they cannot be loaded later (binding the chosen candidate, audit/replay);
--   * `raw_input_ref` — the raw intent is held BY REFERENCE (§9.4, never sent to the LLM); it is the
--     audit-of-record for raw-intent replay + the confirm-time raw re-screen.
-- This is a TABLE, not an event aggregate (no aggregate-CHECK change). Writes go through
-- intake/blobs.py::write_blob (INSERT, idempotent). All CREATE ... IF NOT EXISTS / CREATE OR REPLACE
-- — fully idempotent.

CREATE TABLE IF NOT EXISTS blob (
    blob_ref     text        PRIMARY KEY,
    content      jsonb       NOT NULL,               -- the exact stored payload (body / raw intent)
    content_hash text        NOT NULL,               -- 'sha256:<hex>' of the canonical-JSON content
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Physical immutability (no UPDATE/DELETE) — the write-once backstop, mirroring SP-0's Phase-02
-- documents_no_mutation and SP-2's llm_call_no_mutation triggers. INSERT (write_blob) is unaffected;
-- a governance-retained body / raw-intent audit copy can never be silently altered or deleted.
CREATE OR REPLACE FUNCTION blob_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'blob store is write-once: % not allowed on blob_ref=%',
        TG_OP, COALESCE(OLD.blob_ref, NEW.blob_ref);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER blob_no_mutation
    BEFORE UPDATE OR DELETE ON blob
    FOR EACH ROW EXECUTE FUNCTION blob_write_once();
