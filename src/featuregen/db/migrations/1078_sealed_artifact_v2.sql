-- src/featuregen/db/migrations/1078_sealed_artifact_v2.sql
-- S7 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the SEALED V2 artifact, the
-- subgraph verdict it was sealed under, and the policy realizations it depends on.
--
-- WHAT IS *NOT* HERE, AND WHY. The plan reserved 1078 for "artifact file manifest + the content
-- store it points at" — but C-D4 already shipped both as 1086 (`generated_artifact_blob`,
-- `generated_artifact_file`), and that file's own header records the split. Rewriting them here
-- would give `apply_migrations` two filenames for one pair of tables, and it ledgers by filename
-- stem AND byte checksum. So 1078 holds what is genuinely left: the sealed artifact's identity, the
-- graph verdict, and the realization links.
--
-- `realizes_occurrences` IS THE POINT OF THE THIRD TABLE. C-C8 created the link and S4 persisted it
-- against a REALIZATION; here it is carried onto the SEALED ARTIFACT, because the question an
-- auditor asks about a published number is "which governed policies produced this, and which
-- occurrences did they answer" — and re-deriving that from the compilation would answer it
-- differently once the pointers move. The acceptance clause is explicitly that a REFUSAL keeps
-- these intact: a graph that fails the FX subgraph check still depended on exactly the policies it
-- depended on, and dropping the links on refusal would destroy the evidence at the moment it
-- becomes most interesting.
--
-- THE VERDICT IS STORED, PASS OR FAIL. A sealed artifact that recorded only its successes could not
-- answer "was the FX subgraph inspected at all" — and C-C10's rule is precisely that an untriggered
-- requirement is NOT a pass. `triggered_requirements` distinguishes "checked and satisfied" from
-- "never applied", which a boolean cannot.
--
-- A MISMATCHED DIGEST IS NEITHER SERVED NOR EXECUTED. That is enforced in code by
-- `artifact_manifest.verify_bytes(at=…)` at three points, over the content-addressed store — this
-- table just names the manifest, and `project_digest` is what a serving path compares against.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS sealed_artifact_v2 (
    artifact_id            text PRIMARY KEY CHECK (btrim(artifact_id) <> ''),

    -- Environment first, per F3: deployment placement, in every key.
    environment_id         text NOT NULL CHECK (btrim(environment_id) <> ''),
    logical_group_name     text NOT NULL CHECK (btrim(logical_group_name) <> ''),

    -- What was sealed. The compilation identity's own parts, so an artifact can be matched to a
    -- compilation without re-deriving one.
    compilation_identity_hash text NOT NULL CHECK (btrim(compilation_identity_hash) <> ''),
    group_plan_hash        text NOT NULL CHECK (btrim(group_plan_hash) <> ''),

    -- The digest a serving path compares the retrieved bytes against.
    project_digest         text NOT NULL CHECK (project_digest LIKE 'sha256:%'),

    -- PASS OR FAIL, and which requirements even applied. An untriggered requirement is not a pass
    -- (C-C10), so a boolean here would report "FX subgraph sound" for a feature that has no FX.
    subgraph_satisfied     boolean NOT NULL,
    triggered_requirements jsonb NOT NULL,
    subgraph_findings      jsonb NOT NULL,

    sealed_at              text NOT NULL CHECK (btrim(sealed_at) <> ''),
    recorded_at            timestamptz NOT NULL DEFAULT now(),

    -- A refused seal is RECORDED, not discarded — but it must never look servable.
    CONSTRAINT sealed_artifact_v2_refusal_has_findings
        CHECK (subgraph_satisfied OR jsonb_array_length(subgraph_findings) > 0),
    CONSTRAINT sealed_artifact_v2_pass_has_no_findings
        CHECK (NOT subgraph_satisfied OR jsonb_array_length(subgraph_findings) = 0)
);

CREATE INDEX IF NOT EXISTS sealed_artifact_v2_by_group
    ON sealed_artifact_v2 (environment_id, logical_group_name);

-- WHICH POLICIES PRODUCED THIS NUMBER, and which occurrences they answered. Kept on refusals too.
CREATE TABLE IF NOT EXISTS sealed_artifact_realization (
    artifact_id     text NOT NULL REFERENCES sealed_artifact_v2(artifact_id),
    revision_id     text NOT NULL CHECK (btrim(revision_id) <> ''),
    occurrence_hash text NOT NULL CHECK (btrim(occurrence_hash) <> ''),

    PRIMARY KEY (artifact_id, revision_id, occurrence_hash)
);

CREATE INDEX IF NOT EXISTS sealed_artifact_realization_by_revision
    ON sealed_artifact_realization (revision_id);

-- ── append-only ──────────────────────────────────────────────────────────────────────────────────
-- A sealed artifact is what a published number was produced from, and its realization links are the
-- evidence of which governed policies were applied. Editing either restates history.
CREATE OR REPLACE FUNCTION s7_sealed_artifact_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sealed_artifact_v2_no_update ON sealed_artifact_v2;
CREATE TRIGGER sealed_artifact_v2_no_update
    BEFORE UPDATE OR DELETE ON sealed_artifact_v2
    FOR EACH ROW EXECUTE FUNCTION s7_sealed_artifact_write_once();

DROP TRIGGER IF EXISTS sealed_artifact_realization_no_update ON sealed_artifact_realization;
CREATE TRIGGER sealed_artifact_realization_no_update
    BEFORE UPDATE OR DELETE ON sealed_artifact_realization
    FOR EACH ROW EXECUTE FUNCTION s7_sealed_artifact_write_once();
