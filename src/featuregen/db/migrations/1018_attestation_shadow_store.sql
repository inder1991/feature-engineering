-- src/featuregen/db/migrations/1018_attestation_shadow_store.sql
-- P0 shadow-measurement harness, Task 1 — the durable, append-only (WORM) telemetry store the rest
-- of the harness writes to. P0 is MEASURE-ONLY (design §Goal, docs/superpowers/specs/
-- 2026-07-22-p0-shadow-measurement-design.md): it never writes to any authority store, only the
-- three tables below. Mirrors the `planner_shadow_*` idiom (migration 0999 / shadow_store.py):
-- enum CHECKs, `jsonb_typeof(...) = 'object'` CHECKs on JSON payloads, a stored `payload_hash`, and
-- WORM enforcement.
--
--   * attestation_gold_label       — human ground truth ingested from the labelling worksheet.
--                                     PK (logical_ref, field_name) — `logical_ref` already embeds the
--                                     catalog source (object_ref.py), so no separate catalog_source key
--                                     component is needed. Append-only: a re-submission of the same key
--                                     is a no-op (ON CONFLICT DO NOTHING in the writer), never an update.
--   * attestation_shadow_run       — one row per shadow run (the dispatch manifest): which catalog,
--                                     which gold-set version, which model/signal versions, and the
--                                     declared `sampled_keys` — the durable, explicit EXPECTED SET
--                                     (an array of {logical_ref, field_name} objects) reconcile compares
--                                     captured observations against by set membership, not just count
--                                     (a scalar count alone cannot detect key-substitution capture loss:
--                                     an observation written for a wrong/extra key can coincidentally
--                                     make the counts agree while a genuinely-sampled key is missing).
--                                     `column_count` mirrors `jsonb_array_length(sampled_keys)` and is
--                                     kept as a redundant, CHECK-enforced cross-field guard.
--   * attestation_shadow_observation — one row per (shadow_run, logical_ref, field_name). Stores NO
--                                     gold value — correctness is a READ-TIME JOIN to
--                                     attestation_gold_label, so an observation is never contaminated by
--                                     the label and can be re-scored against a corrected gold set.
--
-- WORM enforcement is TWO layers (mirrors 1012_contract_worm.sql, the current best-practice evolution
-- of the 0971/0999 REVOKE-only idiom — a REVOKE alone is a no-op against a superuser, so it cannot be
-- verified by an in-process UPDATE attempt under the superuser test cluster):
--   1. a BEFORE UPDATE OR DELETE FOR EACH ROW trigger per table that RAISEs — blocks row DML for EVERY
--      role, including a superuser, under the normal session_replication_role = origin;
--   2. a guarded REVOKE UPDATE, DELETE, TRUNCATE ON ... FROM featuregen_app when that role exists — a
--      row trigger does not fire on a statement-level TRUNCATE, so this is the TRUNCATE control (a
--      deployment-role control: a superuser always bypasses grants regardless).
-- INSERT (append) and SELECT are unaffected by either layer.

-- ── attestation_gold_label ──
CREATE TABLE IF NOT EXISTS attestation_gold_label (
    catalog_source  text        NOT NULL,
    logical_ref     text        NOT NULL,
    field_name      text        NOT NULL,
    gold_value      text        NOT NULL,
    labeller_ids    jsonb       NOT NULL CHECK (jsonb_typeof(labeller_ids) = 'array'),
    adjudicated_by  text        NOT NULL,
    labelled_at     timestamptz NOT NULL DEFAULT now(),
    notes           text        NULL,
    payload_hash    text        NOT NULL,
    PRIMARY KEY (logical_ref, field_name)
);
CREATE INDEX IF NOT EXISTS attestation_gold_label_source_idx
    ON attestation_gold_label (catalog_source);

CREATE OR REPLACE FUNCTION attestation_gold_label_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'attestation_gold_label is append-only (WORM): % not allowed on (logical_ref=%, '
        'field_name=%)', TG_OP, COALESCE(OLD.logical_ref, NEW.logical_ref),
        COALESCE(OLD.field_name, NEW.field_name);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER attestation_gold_label_no_mutation
    BEFORE UPDATE OR DELETE ON attestation_gold_label
    FOR EACH ROW EXECUTE FUNCTION attestation_gold_label_write_once();

-- ── attestation_shadow_run — the dispatch manifest ──
CREATE TABLE IF NOT EXISTS attestation_shadow_run (
    shadow_run_id      text        PRIMARY KEY,
    catalog_source     text        NOT NULL,
    gold_version_hash  text        NOT NULL,
    model_ids          jsonb       NOT NULL CHECK (jsonb_typeof(model_ids) = 'object'),
    signal_versions    jsonb       NOT NULL CHECK (jsonb_typeof(signal_versions) = 'object'),
    started_at         timestamptz NOT NULL DEFAULT now(),
    sampled_keys       jsonb       NOT NULL CHECK (jsonb_typeof(sampled_keys) = 'array'),
    sampled_keys_hash  text        NOT NULL,
    column_count       int         NOT NULL CHECK (column_count >= 0),
    payload_hash       text        NOT NULL,
    CONSTRAINT attestation_shadow_run_column_count_matches_keys
        CHECK (column_count = jsonb_array_length(sampled_keys))
);
CREATE INDEX IF NOT EXISTS attestation_shadow_run_source_idx
    ON attestation_shadow_run (catalog_source);

CREATE OR REPLACE FUNCTION attestation_shadow_run_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'attestation_shadow_run is append-only (WORM): % not allowed on shadow_run_id=%',
        TG_OP, COALESCE(OLD.shadow_run_id, NEW.shadow_run_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER attestation_shadow_run_no_mutation
    BEFORE UPDATE OR DELETE ON attestation_shadow_run
    FOR EACH ROW EXECUTE FUNCTION attestation_shadow_run_write_once();

-- ── attestation_shadow_observation — one row per (run, column, field); NO gold value stored ──
CREATE TABLE IF NOT EXISTS attestation_shadow_observation (
    shadow_run_id       text        NOT NULL REFERENCES attestation_shadow_run (shadow_run_id) ON DELETE CASCADE,
    logical_ref         text        NOT NULL,
    field_name          text        NOT NULL,
    proposer_value      text        NULL,
    proposer_producer   text        NULL,
    reclassify_value    text        NULL,
    reclassify_agrees   boolean     NULL,
    grounding_checks    jsonb       NOT NULL CHECK (jsonb_typeof(grounding_checks) = 'object'),
    grounding_coverage  numeric     NOT NULL CHECK (grounding_coverage >= 0 AND grounding_coverage <= 1),
    grounding_conflict  boolean     NOT NULL,
    confidence          numeric     NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    risk_tier           text        NOT NULL,
    payload_hash        text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shadow_run_id, logical_ref, field_name),
    CONSTRAINT attestation_obs_reclassify_agrees_scope
        CHECK ((reclassify_agrees IS NULL) = (reclassify_value IS NULL))
);
CREATE INDEX IF NOT EXISTS attestation_shadow_observation_run_idx
    ON attestation_shadow_observation (shadow_run_id);
CREATE INDEX IF NOT EXISTS attestation_shadow_observation_ref_idx
    ON attestation_shadow_observation (logical_ref, field_name);

CREATE OR REPLACE FUNCTION attestation_shadow_observation_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'attestation_shadow_observation is append-only (WORM): % not allowed on '
        '(shadow_run_id=%, logical_ref=%, field_name=%)', TG_OP,
        COALESCE(OLD.shadow_run_id, NEW.shadow_run_id), COALESCE(OLD.logical_ref, NEW.logical_ref),
        COALESCE(OLD.field_name, NEW.field_name);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER attestation_shadow_observation_no_mutation
    BEFORE UPDATE OR DELETE ON attestation_shadow_observation
    FOR EACH ROW EXECUTE FUNCTION attestation_shadow_observation_write_once();

-- WORM TRUNCATE guard (layer 2): revoke destructive DML from the production app role when it exists.
-- No-op in the superuser test cluster (a superuser bypasses grants), so this control relies on
-- production running under the NON-superuser featuregen_app role (mirrors 0971/0999/1012).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON attestation_gold_label         FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON attestation_shadow_run         FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON attestation_shadow_observation FROM featuregen_app;
    END IF;
END $$;
