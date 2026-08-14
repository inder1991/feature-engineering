-- src/featuregen/db/migrations/1067_materialization_request_option_link.sql
-- Task B4 of the formula/execution seam (reservation 1067 per that plan's D-8): a materialization
-- run acquires GOVERNED PROVENANCE.
--
-- THE HOLE THIS CLOSES. `materialization_request` (1053) carries a logical group name, an actor, a
-- roles snapshot, an idempotency key and an opaque `activation_state` jsonb — and **nothing links a
-- run to the governed option a human approved**. So a compile could be triggered for a group whose
-- option decision blocked `execute_materialization`, and no record would connect the two.
--
--   * NULLABLE, and that is the whole design, not a concession. The existing work-item-driven path
--     predates this link and must keep working: `POST /materialization-runs` takes its members as
--     `recipe_formula_shadow_work_item` ids because nothing durable maps a logical group to its
--     members, and requiring an option key would break every caller of a shipped surface. A row
--     with both columns NULL is the legacy path, exactly as it is today.
--   * COMPOSITE FOREIGN KEY to `semantic_option_decision (considered_revision_id, option_id)` —
--     1063's own UNIQUE constraint `semantic_option_decision_option_uq` is the target. A pair that
--     names no decision row is refused by the DATABASE rather than by a writer's good intentions,
--     which is what stops a request from citing an approval that does not exist. MATCH SIMPLE (the
--     default) is deliberate: with it, a row where either column is NULL satisfies the constraint,
--     so the legacy path is unconstrained while a *stated* pair must be real. The half-stated case
--     is closed by a CHECK below rather than by MATCH FULL, so the error a caller sees names the
--     problem instead of quoting a foreign key.
--   * NO ON DELETE / ON UPDATE clause: `semantic_option_decision` is append-only (1063's triggers
--     refuse UPDATE, DELETE and TRUNCATE), so a cascade would describe an event that cannot happen.
--   * An index on the pair, because the operator question this link exists to answer — "what did
--     this approved option actually run?" — is a lookup BY the option, not by the request.
ALTER TABLE materialization_request
    ADD COLUMN IF NOT EXISTS considered_revision_id text NULL,
    ADD COLUMN IF NOT EXISTS option_id              text NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'materialization_request_option_fk'
          AND conrelid = 'materialization_request'::regclass
    ) THEN
        ALTER TABLE materialization_request
            ADD CONSTRAINT materialization_request_option_fk
            FOREIGN KEY (considered_revision_id, option_id)
            REFERENCES semantic_option_decision (considered_revision_id, option_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'materialization_request_option_is_whole'
          AND conrelid = 'materialization_request'::regclass
    ) THEN
        -- Half a key is not provenance. Stated as its own CHECK so the refusal says "an option is
        -- named by BOTH halves" rather than surfacing as a foreign-key violation on a pair the
        -- caller never meant to state.
        ALTER TABLE materialization_request
            ADD CONSTRAINT materialization_request_option_is_whole
            CHECK ((considered_revision_id IS NULL) = (option_id IS NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS materialization_request_option_idx
    ON materialization_request (considered_revision_id, option_id)
    WHERE considered_revision_id IS NOT NULL;
