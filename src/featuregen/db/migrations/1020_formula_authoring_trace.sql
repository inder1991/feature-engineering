-- src/featuregen/db/migrations/1020_formula_authoring_trace.sql
-- Child-1 (TypedFormula authoring) Task 11 — the WRITE-ONCE, CRASH-SAFE authoring trace (design §H).
--
-- Two tables, both physically immutable (no UPDATE / DELETE / TRUNCATE):
--   * authoring_run         — the MANIFEST, inserted FIRST (before any provider call): the run id,
--                             the stamped rule/registry versions, the intent hash, the actor.
--   * authoring_trace_event — the append-only event log over the CLOSED §H vocabulary
--                             STARTED -> LLM_CALL_RECORDED | TOOL_CALLED | TOOL_RESULT_RECORDED |
--                             CRITIC_RECORDED -> COMPLETED | FAILED.
--
-- CRASH-SAFE HONESTY [c12]: there is deliberately NO run status column. The read model derives
-- *incomplete* = "this run has no terminal (COMPLETED/FAILED) event", so a process that dies
-- mid-authoring leaves an honestly incomplete run, and the durable ``llm_call`` rows that outlive a
-- rolled-back request transaction can never make a run read as completed. Nothing ever UPDATEs a
-- row that may not exist — every writer only ever INSERTs.
--
-- WRITE-ONCE: enforced by TRIGGERS, not convention (mirrors 0060_aggregates_lifecycle /
-- 0510_llm_call_store / 0900_events_write_once). Row-level triggers reject UPDATE/DELETE; a
-- FOR EACH ROW trigger does NOT fire on TRUNCATE, so each table ALSO carries its own
-- BEFORE TRUNCATE ... FOR EACH STATEMENT trigger (without it TRUNCATE silently succeeds — the
-- 0900 note about relying on a REVOKE is a grant-level control only, bypassed by a superuser).
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS +
-- CREATE OR REPLACE FUNCTION/TRIGGER (PostgreSQL 14+), so apply_migrations stays re-runnable.

-- ── the manifest, written FIRST ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS authoring_run (
    authoring_run_id text        PRIMARY KEY,                 -- 'arun_...'
    intent_hash      text        NOT NULL,                    -- the authoring intent's content hash
    versions         jsonb       NOT NULL                     -- every rule/registry version stamped
                         CHECK (jsonb_typeof(versions) = 'object'),
    actor            jsonb       NOT NULL,                    -- identity_to_jsonb(actor)
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- ── the append-only trace ────────────────────────────────────────────────────────────────────────
-- payload is CANONICAL REDACTED metadata ONLY (tool result identities/verdicts/hashes, dispositions,
-- reason codes) — never raw catalog data values, matching the metadata-only egress discipline of
-- formula/tools.py. payload_hash is the sha256 over the RFC 8785 (JCS) bytes of that payload, so a
-- stored tool result is tamper-evident and recomputable from the read-back jsonb (§H "canonical
-- redacted result + hash").
CREATE TABLE IF NOT EXISTS authoring_trace_event (
    authoring_trace_event_id text        PRIMARY KEY,         -- 'atev_...'
    authoring_run_id         text        NOT NULL REFERENCES authoring_run (authoring_run_id),
    seq                      integer     NOT NULL CHECK (seq >= 0),
    kind                     text        NOT NULL CHECK (kind IN (
                                 'STARTED', 'LLM_CALL_RECORDED', 'TOOL_CALLED',
                                 'TOOL_RESULT_RECORDED', 'CRITIC_RECORDED',
                                 'COMPLETED', 'FAILED')),
    -- The immutable provider-call record this event evidences. NULLable: not every event has a
    -- call (STARTED / TOOL_CALLED / terminals). llm_call's PK is llm_call_ref (0510).
    llm_call_ref             text        NULL REFERENCES llm_call (llm_call_ref),
    idempotency_key          text        NOT NULL,
    payload                  jsonb       NOT NULL DEFAULT '{}'::jsonb
                                 CHECK (jsonb_typeof(payload) = 'object'),
    payload_hash             text        NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now(),
    -- One event per (run, position): a duplicate seq for the same run is rejected.
    CONSTRAINT authoring_trace_event_run_seq_unique UNIQUE (authoring_run_id, seq),
    -- Retry/replay key: the same logical append can never land twice.
    CONSTRAINT authoring_trace_event_idempotency_unique UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS authoring_trace_event_run_idx
    ON authoring_trace_event (authoring_run_id, seq);
CREATE INDEX IF NOT EXISTS authoring_trace_event_llm_call_idx
    ON authoring_trace_event (llm_call_ref) WHERE llm_call_ref IS NOT NULL;

-- AT MOST ONE terminal event per run. This partial UNIQUE index is the CONCURRENCY-safe half of the
-- rule: two sessions can both pass the trigger's EXISTS probe below, but only one can own this
-- index entry. Together they make "a completed run" a single, unambiguous, unforgeable fact.
CREATE UNIQUE INDEX IF NOT EXISTS authoring_trace_event_one_terminal_idx
    ON authoring_trace_event (authoring_run_id)
    WHERE kind IN ('COMPLETED', 'FAILED');

-- Nothing may be appended AFTER a terminal event — a closed run is closed for good (a late
-- LLM_CALL_RECORDED must never be able to extend or re-open a finished run's story).
CREATE OR REPLACE FUNCTION authoring_trace_event_no_post_terminal() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM authoring_trace_event
                WHERE authoring_run_id = NEW.authoring_run_id
                  AND kind IN ('COMPLETED', 'FAILED')) THEN
        RAISE EXCEPTION
            'authoring run % already has a terminal event: cannot append kind=% seq=%',
            NEW.authoring_run_id, NEW.kind, NEW.seq;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER authoring_trace_event_terminal_guard
    BEFORE INSERT ON authoring_trace_event
    FOR EACH ROW EXECUTE FUNCTION authoring_trace_event_no_post_terminal();

-- ── write-once: UPDATE / DELETE (row level) ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION authoring_run_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'authoring_run is write-once: % not allowed on authoring_run_id=%',
        TG_OP, COALESCE(OLD.authoring_run_id, NEW.authoring_run_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER authoring_run_no_mutation
    BEFORE UPDATE OR DELETE ON authoring_run
    FOR EACH ROW EXECUTE FUNCTION authoring_run_write_once();

CREATE OR REPLACE FUNCTION authoring_trace_event_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'authoring_trace_event is write-once: % not allowed on authoring_trace_event_id=%',
        TG_OP, COALESCE(OLD.authoring_trace_event_id, NEW.authoring_trace_event_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER authoring_trace_event_no_mutation
    BEFORE UPDATE OR DELETE ON authoring_trace_event
    FOR EACH ROW EXECUTE FUNCTION authoring_trace_event_write_once();

-- ── write-once: TRUNCATE (statement level — a row trigger does NOT fire on TRUNCATE) ─────────────
-- A TRUNCATE trigger function may not reference OLD/NEW, hence the separate functions.
CREATE OR REPLACE FUNCTION authoring_run_no_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'authoring_run is write-once: TRUNCATE not allowed';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER authoring_run_no_truncation
    BEFORE TRUNCATE ON authoring_run
    FOR EACH STATEMENT EXECUTE FUNCTION authoring_run_no_truncate();

CREATE OR REPLACE FUNCTION authoring_trace_event_no_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'authoring_trace_event is write-once: TRUNCATE not allowed';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER authoring_trace_event_no_truncation
    BEFORE TRUNCATE ON authoring_trace_event
    FOR EACH STATEMENT EXECUTE FUNCTION authoring_trace_event_no_truncate();

-- Defence in depth for the production (NON-superuser) app role, mirroring 0910 / 0971 / 1019: the
-- triggers above are the real enforcement (they hold even for a superuser); this removes the
-- destructive grants as well so the app role cannot even attempt them.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON authoring_run          FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON authoring_trace_event  FROM featuregen_app;
    END IF;
END $$;
