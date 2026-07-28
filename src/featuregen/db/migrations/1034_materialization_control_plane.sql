-- src/featuregen/db/migrations/1034_materialization_control_plane.sql
-- Spec A §12 — the APPEND-ONLY materialization control plane (plan Task 14).
--
-- This plane is the record of WHAT WAS PUBLISHED. A record that can be rewritten proves nothing, so
-- all seven tables reject UPDATE, DELETE and TRUNCATE. `RunManifestV1.status` implies mutation, and
-- an append-only row cannot hold a field that moves — so the moving part lives in
-- `materialization_run_event` rows that are folded to derive the current status, and a single
-- TERMINAL manifest may be inserted at the end. Nothing is ever updated in place.
--
-- NUMBERING. The plan said `1031_materialization_control_plane.sql`; 1031 was taken by
-- `1031_graph_node_measure_annotation_links.sql` from a parallel stream. 1032 and 1033 are ALSO
-- taken — by `1032_graph_node_visibility_requires.sql` and `1033_data_observation.sql`, which are
-- not yet on origin/main but exist on `fix/join-neighbourhood-cap` and
-- `integration/ontology-data-agent` and will collide on merge. 1034 was free across every ref
-- (`git log --all --diff-filter=A -- 'src/featuregen/db/migrations/1034*'` is empty).
--
-- THE THREE GUARDS, and why TRUNCATE needs its own.
--   * UPDATE / DELETE — a `BEFORE UPDATE OR DELETE ... FOR EACH ROW` trigger that RAISEs. An ORIGIN
--     (default) row trigger fires for EVERY role including the owner/superuser under the normal
--     `session_replication_role = origin`, so ordinary row DML is physically blocked.
--   * TRUNCATE — a SEPARATE `BEFORE TRUNCATE ... FOR EACH STATEMENT` trigger. A FOR EACH ROW trigger
--     does NOT fire on TRUNCATE at all: measured on this repo's test server (PostgreSQL 18), a table
--     carrying a row-level INSERT/UPDATE/DELETE trigger was silently emptied by `TRUNCATE`. 1012
--     used only the REVOKE below as its TRUNCATE control, which is a DEPLOYMENT control; this plane
--     carries the statement trigger as well, so a superuser session cannot empty it either.
--   * A guarded `REVOKE UPDATE, DELETE, TRUNCATE ... FROM featuregen_app` — defence in depth for the
--     non-superuser production role, guarded by a role-exists check so it is a clean no-op in the
--     superuser test cluster where the role is absent.
--
-- Note for anyone writing a guard test: a bare `TRUNCATE <parent>` on an FK-referenced table fails
-- with `FeatureNotSupported` ("cannot truncate a table referenced in a foreign key constraint")
-- BEFORE any BEFORE-TRUNCATE trigger fires. A test asserting only "TRUNCATE raises" on such a table
-- passes with no trigger installed. Use `TRUNCATE ... CASCADE` (which does reach the trigger) and
-- assert the guard's own message.
--
-- Idempotent / re-runnable in the repo style (`apply_migrations` ledgers by filename, and the test
-- suite re-executes this exact SQL against a POPULATED plane): CREATE TABLE IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS + CREATE OR REPLACE FUNCTION/TRIGGER (PostgreSQL 14+) + a guarded
-- REVOKE. Nothing here drops, alters or deletes, so re-application cannot destroy a record.

-- ── generations: ONE compilation of ONE group ────────────────────────────────────────────────────
-- Every other record points here. §7's group-level identity: the contract the group publishes
-- under, the packing list it packs, and the bytes that were rendered.
CREATE TABLE IF NOT EXISTS materialization_generation (
    generation_id                 text PRIMARY KEY,
    logical_group_name            text NOT NULL CHECK (btrim(logical_group_name) <> ''),
    materialization_contract_hash text NOT NULL CHECK (btrim(materialization_contract_hash) <> ''),
    group_plan_hash               text NOT NULL CHECK (btrim(group_plan_hash) <> ''),
    generated_project_hash        text NOT NULL CHECK (btrim(generated_project_hash) <> ''),
    -- Offset-aware ISO 8601, supplied by the caller and validated in Python (mirrors
    -- materialize/binding.py): the plane records when a thing HAPPENED, not when it was told.
    created_at                    text NOT NULL CHECK (btrim(created_at) <> ''),
    recorded_at                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS materialization_generation_group_idx
    ON materialization_generation (logical_group_name);

-- ── §10.3 capability attestations — ingested ONLY from a probe result ────────────────────────────
-- `evidence_hash` is NOT NULL on purpose, and is the one field §10.3's dataclass sketch omits:
-- without it a stored attestation carries no probe evidence at all, and "an attestation with no
-- probe evidence cannot exist" would be unprovable from the record. `record_attestation` (Task 16)
-- owns the ingestion path; the table lands here with the rest of the plane.
CREATE TABLE IF NOT EXISTS publication_capability_attestation (
    attestation_id          text PRIMARY KEY,
    environment_id          text NOT NULL CHECK (btrim(environment_id) <> ''),
    hive_version            text NOT NULL CHECK (btrim(hive_version) <> ''),
    spark_version           text NOT NULL CHECK (btrim(spark_version) <> ''),
    metastore_version       text NOT NULL CHECK (btrim(metastore_version) <> ''),
    -- The mechanism vocabulary (`PublishMechanism`) is Task 16's and is deliberately NOT closed
    -- here: a CHECK written before the probe exists would either invent members or refuse the one
    -- the probe proves.
    mechanism               text NOT NULL CHECK (btrim(mechanism) <> ''),
    passed                  boolean NOT NULL,
    covers_schema_evolution boolean NOT NULL,
    evidence_hash           text NOT NULL CHECK (btrim(evidence_hash) <> ''),
    attested_at             text NOT NULL CHECK (btrim(attested_at) <> ''),
    recorded_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS publication_capability_attestation_env_idx
    ON publication_capability_attestation (environment_id, mechanism);

-- ── §10.1 the physical target is bound to its contract, in TWO records ───────────────────────────
-- The binding is written ONCE per logical name — UNIQUE(logical_group_name) is what makes that a
-- database fact rather than a convention. There is deliberately no current_group_plan_hash, no
-- status and no updated_at: the table is append-only, so a field that moves could not be honoured.
CREATE TABLE IF NOT EXISTS group_binding (
    binding_id                    text PRIMARY KEY,
    logical_group_name            text NOT NULL UNIQUE CHECK (btrim(logical_group_name) <> ''),
    materialization_contract_hash text NOT NULL CHECK (btrim(materialization_contract_hash) <> ''),
    physical_target               text NOT NULL CHECK (btrim(physical_target) <> ''),
    recorded_at                   timestamptz NOT NULL DEFAULT now()
);

-- Appended whenever the packing list changes. ONE generation compiles ONE plan, so
-- (binding_id, generation_id) is the key: a second revision for the same generation would be a
-- second answer to a question that has one. "Current plan" is DERIVED (the latest revision whose
-- generation published), never stored here.
CREATE TABLE IF NOT EXISTS group_plan_revision (
    binding_id      text NOT NULL REFERENCES group_binding(binding_id),
    generation_id   text NOT NULL REFERENCES materialization_generation(generation_id),
    group_plan_hash text NOT NULL CHECK (btrim(group_plan_hash) <> ''),
    created_at      text NOT NULL CHECK (btrim(created_at) <> ''),
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (binding_id, generation_id)
);

-- ── §11 validation reports (L0/L1/L2) ────────────────────────────────────────────────────────────
-- `ValidationReportV1` and its ingestion path are Task 15's; the table lands with the plane.
-- L3 is deliberately NOT a level here: §11.2's L3 is "the real run", which this plane records as
-- run EVENTS. A report claiming L3 would be a validation verdict about an execution.
CREATE TABLE IF NOT EXISTS pipeline_validation_report (
    report_id              text PRIMARY KEY,
    generation_id          text NOT NULL REFERENCES materialization_generation(generation_id),
    -- NULL for L0: the project is validated before any run exists.
    run_id                 text NULL,
    generated_project_hash text NOT NULL CHECK (btrim(generated_project_hash) <> ''),
    group_plan_hash        text NOT NULL CHECK (btrim(group_plan_hash) <> ''),
    level                  text NOT NULL CHECK (level IN ('L0', 'L1', 'L2')),
    environment_id         text NOT NULL CHECK (btrim(environment_id) <> ''),
    status                 text NOT NULL CHECK (status IN ('passed', 'failed', 'error')),
    started_at             text NOT NULL CHECK (btrim(started_at) <> ''),
    finished_at            text NOT NULL CHECK (btrim(finished_at) <> ''),
    findings               jsonb NOT NULL DEFAULT '[]'::jsonb
                               CHECK (jsonb_typeof(findings) = 'array'),
    recorded_at            timestamptz NOT NULL DEFAULT now(),
    -- §11.2, made physical: an unreachable cluster yields status="error" with ZERO findings —
    -- never invented ones. A findings list under 'error' is a fabricated observation.
    CONSTRAINT pipeline_validation_report_error_has_no_findings
        CHECK (status <> 'error' OR jsonb_array_length(findings) = 0)
);
CREATE INDEX IF NOT EXISTS pipeline_validation_report_generation_idx
    ON pipeline_validation_report (generation_id, level);

-- ── §12 run events — the moving part of a run's status ───────────────────────────────────────────
-- `event_kind` is CLOSED and mirrors `featuregen.materialize.control_plane.RunEventKind` exactly
-- (the test suite compares the two sets with ==). `seq` is the run's total order and is supplied by
-- the caller: computing max(seq)+1 in the writer is a read-modify-write that two appenders can both
-- win, and the unique key turns that race into a refusal instead of a silent reordering.
CREATE TABLE IF NOT EXISTS materialization_run_event (
    run_id        text NOT NULL CHECK (btrim(run_id) <> ''),
    seq           integer NOT NULL CHECK (seq >= 0),
    generation_id text NOT NULL REFERENCES materialization_generation(generation_id),
    event_kind    text NOT NULL CONSTRAINT materialization_run_event_kind_is_closed CHECK (
                      event_kind IN (
                          'RUN_PREPARED',
                          'RUN_SUBMITTED',
                          'COMPUTATION_COMPLETED',
                          'GATES_PASSED',
                          'GATES_FAILED',
                          'PUBLISHED',
                          'PUBLICATION_REFUSED',
                          'RUN_FAILED')),
    occurred_at   text NOT NULL CHECK (btrim(occurred_at) <> ''),
    -- Operator-facing text under §14's rule: counts, types, hashes and locations only, never a data
    -- value. The control plane never reads feature data.
    detail        text NOT NULL DEFAULT '',
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS materialization_run_event_generation_idx
    ON materialization_run_event (generation_id, event_kind);

-- At most ONE terminal event per run. The fold derives the current status by reading the last
-- event; two terminal events would make that a choice rather than a reading.
CREATE UNIQUE INDEX IF NOT EXISTS materialization_run_event_one_terminal
    ON materialization_run_event (run_id)
    WHERE event_kind IN ('GATES_FAILED', 'PUBLISHED', 'PUBLICATION_REFUSED', 'RUN_FAILED');

-- ── §12 the terminal run manifest — exactly one per run ──────────────────────────────────────────
-- `run_id` is the PRIMARY KEY, which is what "a single terminal manifest row may be inserted at the
-- end" means physically. Every column is an identity, a count, a location or a verdict: there is no
-- column here a data value could occupy.
CREATE TABLE IF NOT EXISTS materialization_run_manifest (
    run_id                        text PRIMARY KEY,
    generation_id                 text NOT NULL REFERENCES materialization_generation(generation_id),
    group_plan_hash               text NOT NULL CHECK (btrim(group_plan_hash) <> ''),
    materialization_contract_hash text NOT NULL CHECK (btrim(materialization_contract_hash) <> ''),
    generated_project_hash        text NOT NULL CHECK (btrim(generated_project_hash) <> ''),
    sandbox_execution_hash        text NOT NULL CHECK (btrim(sandbox_execution_hash) <> ''),
    business_dt                   text NOT NULL CHECK (business_dt ~ '^\d{4}-\d{2}-\d{2}$'),
    publication_mechanism         text NOT NULL CHECK (btrim(publication_mechanism) <> ''),
    -- §10.3: no publication without a passing attestation for the exact environment. The FK is the
    -- database's half of that rule — a manifest cannot name an attestation that does not exist.
    capability_attestation_id     text NOT NULL
                                      REFERENCES publication_capability_attestation(attestation_id),
    expected_feature_columns      text[] NOT NULL
                                      CHECK (cardinality(expected_feature_columns) > 0),
    staged_row_count              bigint NULL CHECK (staged_row_count >= 0),
    published_row_count           bigint NULL CHECK (published_row_count >= 0),
    schema_hash                   text NULL,
    key_uniqueness_result         text NULL,
    required_column_result        text NULL,
    orphan_grain_key_count        bigint NULL CHECK (orphan_grain_key_count >= 0),
    publication_location          text NULL,
    started_at                    text NULL,
    published_at                  text NULL,
    -- The manifest is written ONCE, at the END, so its status is a TERMINAL one. Mirrors the
    -- terminal members of `RunStatus` exactly (compared with == by the test suite).
    status                        text NOT NULL
                                      CONSTRAINT materialization_run_manifest_status_is_terminal
                                      CHECK (status IN ('rejected', 'published', 'refused', 'failed')),
    recorded_at                   timestamptz NOT NULL DEFAULT now(),
    -- A manifest claiming 'published' must state when, where and how many rows — otherwise it
    -- records that something was published but nothing a reader could check the table against.
    CONSTRAINT materialization_run_manifest_published_is_complete
        CHECK (status <> 'published'
               OR (published_at IS NOT NULL
                   AND publication_location IS NOT NULL
                   AND published_row_count IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS materialization_run_manifest_generation_idx
    ON materialization_run_manifest (generation_id);

-- ── the append-only guards ───────────────────────────────────────────────────────────────────────
-- One function for every table: it references neither OLD nor NEW, because a STATEMENT-level
-- trigger leaves both unassigned and touching them would turn the TRUNCATE guard into a different
-- error — one that says nothing about append-only.
CREATE OR REPLACE FUNCTION materialization_control_plane_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'the materialization control plane is append-only: % is not allowed on %. It is the record '
        'of what was published, and a record that can be rewritten proves nothing — a run''s status '
        'is folded from appended events and its terminal manifest is inserted once.',
        TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

-- Installed from a loop over the table list so a table cannot be given one guard and not the other,
-- and cannot be missed entirely — the failure mode a hand-written block of 14 statements has.
DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'materialization_generation',
        'pipeline_validation_report',
        'materialization_run_event',
        'materialization_run_manifest',
        'publication_capability_attestation',
        'group_binding',
        'group_plan_revision'
    ] LOOP
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION materialization_control_plane_append_only()',
            target || '_no_mutation', target);
        -- A FOR EACH ROW trigger does NOT fire on TRUNCATE. This is the only guard that does.
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER %I BEFORE TRUNCATE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION materialization_control_plane_append_only()',
            target || '_no_truncate', target);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM featuregen_app', target);
        END IF;
    END LOOP;
END $$;
