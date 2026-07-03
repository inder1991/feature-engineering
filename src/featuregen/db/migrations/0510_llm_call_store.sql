-- src/featuregen/db/migrations/0510_llm_call_store.sql
-- SP-2 Phase 1 (design §2.1 #4, §9.3, Decision D9/D15): the SP-2-owned append-only `llm_call`
-- record store — an SP-0-style write-once artifact (like SP-1's overlay_evidence), referenced by
-- llm_call_ref, classified SENSITIVE / governance-retained / read-controlled. It stores the STORED
-- REDACTED (LLM-safe) input itself (redacted_input) — never the raw intent (that stays in SP-0's
-- encrypted raw_input_ref) — so a regulator can REPLAY the exact prompt (MRM/adverse-action). This
-- is a TABLE, not an event aggregate (no aggregate-CHECK change). P3 writes it via record_llm_call.
-- All CREATE ... IF NOT EXISTS; the checkpoint insert is ON CONFLICT DO NOTHING — fully idempotent.

CREATE TABLE IF NOT EXISTS llm_call (
    llm_call_ref          text        PRIMARY KEY,
    feature_contract_id   text        NULL,
    run_id                text        NOT NULL,
    task                  text        NOT NULL,
    provider              text        NOT NULL,
    model                 text        NOT NULL,
    prompt_id             text        NOT NULL,
    prompt_version        integer     NOT NULL,
    output_schema_id      text        NOT NULL,
    output_schema_version integer     NOT NULL,
    generation_settings   jsonb       NOT NULL DEFAULT '{}',   -- pinned; part of the idempotency key
    redaction_version     text        NOT NULL,                -- which IntentRedactor policy (§9.4)
    input_hash            text        NOT NULL,                -- sha256 of the exact redacted input
    redacted_input        jsonb       NOT NULL,                -- the STORED LLM-safe input (replayable)
    input_redaction       jsonb       NOT NULL DEFAULT '{}',   -- what was scrubbed (audit boundary)
    raw_output            jsonb       NULL,                    -- the model's structured output
    validation_result     jsonb       NOT NULL DEFAULT '{}',   -- ok|invalid|refusal|truncated|... (§9.2)
    repair_attempts       jsonb       NOT NULL DEFAULT '[]',   -- LIST of {attempt,class,reason} records (§9.2)
    latency_ms            integer     NULL,
    cost_metadata         jsonb       NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            jsonb       NOT NULL                 -- identity_to_jsonb(service:intake-agent)
);

-- Idempotency probe for the P3 full-identity dedup key (§9.3, §12); the full-identity uniqueness is
-- enforced in the P3 record writer, this index accelerates the (run_id, task, input_hash) lookup.
CREATE INDEX IF NOT EXISTS llm_call_idem_idx ON llm_call (run_id, task, input_hash);

-- Physical immutability (no UPDATE/DELETE) — the write-once backstop for the SENSITIVE /
-- governance-retained record store, mirroring SP-0's Phase-02 documents_write_once and Phase-06
-- feature_versions_write_once triggers. INSERT (record_llm_call) is unaffected; a regulator's
-- replay copy of the exact redacted prompt can never be silently altered or deleted.
CREATE OR REPLACE FUNCTION llm_call_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'llm_call records are write-once: % not allowed on llm_call_ref=%',
        TG_OP, COALESCE(OLD.llm_call_ref, NEW.llm_call_ref);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER llm_call_no_mutation
    BEFORE UPDATE OR DELETE ON llm_call
    FOR EACH ROW EXECUTE FUNCTION llm_call_write_once();

-- Checkpoint for the OPTIONAL fail-closed FC-status read-model projection (built in P8, secondary to
-- the fold, §11/§12). Seeded here so it exists after migrations, mirroring SP-1's 0507 overlay seed.
INSERT INTO projection_checkpoints (projection_name) VALUES ('feature_contract')
ON CONFLICT DO NOTHING;
