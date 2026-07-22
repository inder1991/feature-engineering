-- src/featuregen/db/migrations/1005_llm_dispatch_provenance.sql
-- Delivery C5 (LLM dispatch provenance) Task 1: per-provider-request audit tables. BEFORE each
-- physical LLM request during ingestion enrichment the writer records an immutable dispatch
-- header (llm_dispatch) plus subject attribution (llm_dispatch_subject); AFTER egress it appends
-- the transport outcome (llm_dispatch_outcome) and associates the logical llm_call back to its
-- run (ingestion_run_llm_call) and to its physical dispatches (llm_call_dispatch).
--   CLASSIFICATION: llm_dispatch INHERITS llm_call's (0510) SENSITIVE / read-controlled /
--   governance-retention classification — it stores ONLY the egress-approved REDACTED request
--   (redacted_input), never raw upload text; the write-once triggers (mirroring
--   llm_call_write_once) make the audit records physically immutable.
--   ingestion_run_id is NULLABLE on llm_dispatch: not every dispatch belongs to an ingestion run
--   (feature-generation dispatches record NULL honestly).
-- All statements idempotent: CREATE ... IF NOT EXISTS / CREATE OR REPLACE.

-- 1) The immutable dispatch header, written BEFORE egress (write-once, SENSITIVE).
--    UNIQUE (logical_call_ref, attempt_no) is the retry/replay idempotency key: one physical
--    dispatch record per attempt of a logical call.
CREATE TABLE IF NOT EXISTS llm_dispatch (
    dispatch_ref      text        PRIMARY KEY,
    logical_call_ref  text        NOT NULL,
    attempt_no        integer     NOT NULL,
    ingestion_run_id  text        NULL REFERENCES ingestion_run(id),
    stage             text        NOT NULL,
    task              text        NOT NULL,
    input_hash        text        NOT NULL,                -- sha256 of the exact redacted input
    redacted_input    jsonb       NOT NULL,                -- the egress-approved LLM-safe request
    redaction_version text        NULL,
    provider          text        NULL,
    model             text        NULL,
    prompt_version    integer     NULL,
    schema_version    integer     NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT llm_dispatch_logical_attempt_unique UNIQUE (logical_call_ref, attempt_no)
);

CREATE OR REPLACE FUNCTION llm_dispatch_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'llm_dispatch records are write-once: % not allowed on dispatch_ref=%',
        TG_OP, COALESCE(OLD.dispatch_ref, NEW.dispatch_ref);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER llm_dispatch_no_mutation
    BEFORE UPDATE OR DELETE ON llm_dispatch
    FOR EACH ROW EXECUTE FUNCTION llm_dispatch_write_once();

-- 2) Subject attribution: WHICH catalog objects/fields a dispatch was about (write-once).
CREATE TABLE IF NOT EXISTS llm_dispatch_subject (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dispatch_ref   text   NOT NULL REFERENCES llm_dispatch(dispatch_ref),
    catalog_source text   NULL,
    object_ref     text   NULL,
    logical_ref    text   NULL,
    field_names    jsonb  NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS llm_dispatch_subject_dispatch_idx
    ON llm_dispatch_subject (dispatch_ref);

CREATE OR REPLACE FUNCTION llm_dispatch_subject_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'llm_dispatch_subject records are write-once: % not allowed on id=%',
        TG_OP, COALESCE(OLD.id, NEW.id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER llm_dispatch_subject_no_mutation
    BEFORE UPDATE OR DELETE ON llm_dispatch_subject
    FOR EACH ROW EXECUTE FUNCTION llm_dispatch_subject_write_once();

-- 3) Transport outcome, appended AFTER egress. Append-only + write-once (INSERT allowed, no
--    UPDATE/DELETE): a retry attempt-boundary APPENDS one row per attempt (DELIBERATELY no
--    UNIQUE(dispatch_ref)), but a recorded outcome is tamper-evident — a bank-grade trail must not
--    let a 'transport_failed' be silently flipped to 'response_received' (or a row erased).
CREATE TABLE IF NOT EXISTS llm_dispatch_outcome (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dispatch_ref text        NOT NULL REFERENCES llm_dispatch(dispatch_ref),
    outcome      text        NOT NULL CHECK (outcome IN ('response_received', 'transport_failed')),
    recorded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_dispatch_outcome_dispatch_idx
    ON llm_dispatch_outcome (dispatch_ref);

CREATE OR REPLACE FUNCTION llm_dispatch_outcome_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'llm_dispatch_outcome records are write-once: % not allowed on id=%',
        TG_OP, COALESCE(OLD.id, NEW.id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER llm_dispatch_outcome_no_mutation
    BEFORE UPDATE OR DELETE ON llm_dispatch_outcome
    FOR EACH ROW EXECUTE FUNCTION llm_dispatch_outcome_write_once();

-- 4) Run <-> logical-call association (mirrors 0998 ingestion_run_object): "which LLM calls did
--    this run make" (UNIQUE leads on ingestion_run_id) and "which runs used this call" (index).
CREATE TABLE IF NOT EXISTS ingestion_run_llm_call (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id text        NOT NULL REFERENCES ingestion_run(id),
    llm_call_ref     text        NOT NULL REFERENCES llm_call(llm_call_ref),
    stage            text        NOT NULL,
    at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ingestion_run_llm_call_unique UNIQUE (ingestion_run_id, llm_call_ref, stage)
);
CREATE INDEX IF NOT EXISTS ingestion_run_llm_call_ref_idx
    ON ingestion_run_llm_call (llm_call_ref);

-- 5) Logical-call <-> physical-dispatch association: AFTER egress the logical llm_call is
--    associated back to the dispatch header(s) that physically carried it.
CREATE TABLE IF NOT EXISTS llm_call_dispatch (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    llm_call_ref text   NOT NULL REFERENCES llm_call(llm_call_ref),
    dispatch_ref text   NOT NULL REFERENCES llm_dispatch(dispatch_ref),
    CONSTRAINT llm_call_dispatch_unique UNIQUE (llm_call_ref, dispatch_ref)
);
CREATE INDEX IF NOT EXISTS llm_call_dispatch_ref_idx
    ON llm_call_dispatch (dispatch_ref);
