-- Delivery R5: relational integrity for generation authority records and server-derived
-- use-case provenance. Refuse inconsistent history before adding constraints; never repair it.

CREATE OR REPLACE FUNCTION featuregen_assert_generation_lineage_integrity() RETURNS void AS $$
DECLARE
    bad_count bigint;
BEGIN
    SELECT count(*) INTO bad_count
      FROM confirmed_generation_scope s
      LEFT JOIN contract_intent i ON i.intent_id = s.intent_id
      LEFT JOIN feature_generation_run g ON g.generation_run_id = s.generation_run_id
     WHERE i.intent_id IS NULL OR g.generation_run_id IS NULL
        OR g.intent_id IS DISTINCT FROM s.intent_id;
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'generation lineage audit failed: % confirmed scopes have an orphan or mismatched intent/run',
            bad_count;
    END IF;

    SELECT count(*) INTO bad_count
      FROM confirmed_generation_scope s
      JOIN intent_recognition_attempt a ON a.recognition_id = s.recognition_id
     WHERE a.intent_id IS DISTINCT FROM s.intent_id;
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'generation lineage audit failed: % confirmed scopes reference a cross-intent recognition',
            bad_count;
    END IF;

    SELECT count(*) INTO bad_count
      FROM confirmed_generation_scope s
      JOIN confirmed_generation_scope prior ON prior.scope_id = s.supersedes_scope_id
     WHERE prior.intent_id IS DISTINCT FROM s.intent_id;
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'generation lineage audit failed: % confirmed scopes supersede a cross-intent scope',
            bad_count;
    END IF;

    SELECT count(*) INTO bad_count
      FROM contract_considered_revision r
      LEFT JOIN feature_generation_run g ON g.generation_run_id = r.generation_run_id
      LEFT JOIN catalog_metadata_snapshot m ON m.snapshot_id = r.metadata_snapshot_id
     WHERE g.generation_run_id IS NULL
        OR g.intent_id IS DISTINCT FROM r.intent_id
        OR (r.metadata_snapshot_id IS NOT NULL AND m.snapshot_id IS NULL)
        OR (m.snapshot_id IS NOT NULL AND m.generation_run_id IS DISTINCT FROM r.generation_run_id);
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'generation lineage audit failed: % considered revisions have inconsistent run/intent/snapshot lineage',
            bad_count;
    END IF;

    SELECT count(*) INTO bad_count
      FROM recipe_formula_shadow_expected_run e
      LEFT JOIN feature_generation_run g ON g.generation_run_id = e.generation_run_id
      LEFT JOIN confirmed_generation_scope s ON s.scope_id = e.confirmed_scope_id
      LEFT JOIN contract_considered_revision r
        ON r.considered_revision_id = e.considered_revision_id
     WHERE g.generation_run_id IS NULL
        OR g.intent_id IS DISTINCT FROM e.intent_id
        OR s.scope_id IS NULL
        OR s.generation_run_id IS DISTINCT FROM e.generation_run_id
        OR s.intent_id IS DISTINCT FROM e.intent_id
        OR r.considered_revision_id IS NULL
        OR r.generation_run_id IS DISTINCT FROM e.generation_run_id
        OR r.intent_id IS DISTINCT FROM e.intent_id
        OR r.considered_content_hash IS DISTINCT FROM e.considered_content_hash;
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'generation lineage audit failed: % formula expected runs have inconsistent duplicated lineage',
            bad_count;
    END IF;

    SELECT count(*) INTO bad_count
      FROM recipe_formula_shadow_run_manifest m
      LEFT JOIN recipe_formula_shadow_expected_run e
        ON e.generation_run_id = m.generation_run_id
     WHERE e.generation_run_id IS NULL
        OR e.intent_id IS DISTINCT FROM m.intent_id
        OR e.considered_revision_id IS DISTINCT FROM m.considered_revision_id
        OR e.considered_content_hash IS DISTINCT FROM m.considered_content_hash;
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'generation lineage audit failed: % formula manifests have inconsistent duplicated lineage',
            bad_count;
    END IF;

    SELECT count(*) INTO bad_count
      FROM recipe_formula_shadow_observation o
      LEFT JOIN recipe_formula_shadow_expected_run e
        ON e.generation_run_id = o.generation_run_id
      LEFT JOIN contract_considered_revision r
        ON r.considered_revision_id = o.considered_revision_id
      LEFT JOIN catalog_metadata_snapshot m ON m.snapshot_id = o.metadata_snapshot_id
      LEFT JOIN formula_authoring_run a ON a.authoring_run_id = o.authoring_run_id
     WHERE e.generation_run_id IS NULL
        OR e.intent_id IS DISTINCT FROM o.intent_id
        OR e.considered_revision_id IS DISTINCT FROM o.considered_revision_id
        OR e.considered_content_hash IS DISTINCT FROM o.considered_content_hash
        OR r.generation_run_id IS DISTINCT FROM o.generation_run_id
        OR r.intent_id IS DISTINCT FROM o.intent_id
        OR (o.metadata_snapshot_id IS NOT NULL AND m.snapshot_id IS NULL)
        OR (m.snapshot_id IS NOT NULL
            AND m.generation_run_id IS DISTINCT FROM o.generation_run_id)
        OR (r.metadata_snapshot_id IS DISTINCT FROM o.metadata_snapshot_id)
        OR (o.authoring_run_id IS NOT NULL AND a.authoring_run_id IS NULL);
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'generation lineage audit failed: % formula observations have inconsistent duplicated lineage',
            bad_count;
    END IF;

    SELECT count(*) INTO bad_count
      FROM recipe_formula_shadow_work_item w
      LEFT JOIN recipe_formula_shadow_expected_run e
        ON e.generation_run_id = w.generation_run_id
      LEFT JOIN contract_considered_revision r
        ON r.considered_revision_id = w.considered_revision_id
      LEFT JOIN catalog_metadata_snapshot m ON m.snapshot_id = w.metadata_snapshot_id
     WHERE e.generation_run_id IS NULL
        OR e.intent_id IS DISTINCT FROM w.intent_id
        OR e.considered_revision_id IS DISTINCT FROM w.considered_revision_id
        OR e.considered_content_hash IS DISTINCT FROM w.considered_content_hash
        OR r.generation_run_id IS DISTINCT FROM w.generation_run_id
        OR r.intent_id IS DISTINCT FROM w.intent_id
        OR (w.metadata_snapshot_id IS NOT NULL AND m.snapshot_id IS NULL)
        OR (m.snapshot_id IS NOT NULL
            AND m.generation_run_id IS DISTINCT FROM w.generation_run_id)
        OR r.metadata_snapshot_id IS DISTINCT FROM w.metadata_snapshot_id;
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'generation lineage audit failed: % formula work items have inconsistent duplicated lineage',
            bad_count;
    END IF;
END;
$$ LANGUAGE plpgsql;

SELECT featuregen_assert_generation_lineage_integrity();

ALTER TABLE confirmed_generation_scope
    ADD CONSTRAINT confirmed_generation_scope_intent_fk
    FOREIGN KEY (intent_id) REFERENCES contract_intent(intent_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE confirmed_generation_scope
    ADD CONSTRAINT confirmed_generation_scope_run_fk
    FOREIGN KEY (generation_run_id) REFERENCES feature_generation_run(generation_run_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE contract_considered_revision
    ADD CONSTRAINT contract_considered_revision_run_fk
    FOREIGN KEY (generation_run_id) REFERENCES feature_generation_run(generation_run_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE contract_considered_revision
    ADD CONSTRAINT contract_considered_revision_snapshot_fk
    FOREIGN KEY (metadata_snapshot_id) REFERENCES catalog_metadata_snapshot(snapshot_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_run_manifest
    ADD CONSTRAINT recipe_formula_shadow_manifest_intent_fk
    FOREIGN KEY (intent_id) REFERENCES contract_intent(intent_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_run_manifest
    ADD CONSTRAINT recipe_formula_shadow_manifest_revision_fk
    FOREIGN KEY (considered_revision_id)
    REFERENCES contract_considered_revision(considered_revision_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_observation
    ADD CONSTRAINT recipe_formula_shadow_observation_intent_fk
    FOREIGN KEY (intent_id) REFERENCES contract_intent(intent_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_observation
    ADD CONSTRAINT recipe_formula_shadow_observation_revision_fk
    FOREIGN KEY (considered_revision_id)
    REFERENCES contract_considered_revision(considered_revision_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_observation
    ADD CONSTRAINT recipe_formula_shadow_observation_snapshot_fk
    FOREIGN KEY (metadata_snapshot_id) REFERENCES catalog_metadata_snapshot(snapshot_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_observation
    ADD CONSTRAINT recipe_formula_shadow_observation_authoring_run_fk
    FOREIGN KEY (authoring_run_id) REFERENCES formula_authoring_run(authoring_run_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_work_item
    ADD CONSTRAINT recipe_formula_shadow_work_item_intent_fk
    FOREIGN KEY (intent_id) REFERENCES contract_intent(intent_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_work_item
    ADD CONSTRAINT recipe_formula_shadow_work_item_revision_fk
    FOREIGN KEY (considered_revision_id)
    REFERENCES contract_considered_revision(considered_revision_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE recipe_formula_shadow_work_item
    ADD CONSTRAINT recipe_formula_shadow_work_item_snapshot_fk
    FOREIGN KEY (metadata_snapshot_id) REFERENCES catalog_metadata_snapshot(snapshot_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION featuregen_check_confirmed_scope_lineage() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM feature_generation_run g
         WHERE g.generation_run_id = NEW.generation_run_id
           AND g.intent_id = NEW.intent_id
    ) OR (
        NEW.recognition_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM intent_recognition_attempt a
             WHERE a.recognition_id = NEW.recognition_id AND a.intent_id = NEW.intent_id
        )
    ) OR (
        NEW.supersedes_scope_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM confirmed_generation_scope prior
             WHERE prior.scope_id = NEW.supersedes_scope_id AND prior.intent_id = NEW.intent_id
        )
    ) THEN
        RAISE EXCEPTION 'confirmed scope % has inconsistent generation lineage', NEW.scope_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION featuregen_check_considered_revision_lineage() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM feature_generation_run g
          LEFT JOIN catalog_metadata_snapshot m ON m.snapshot_id = NEW.metadata_snapshot_id
         WHERE g.generation_run_id = NEW.generation_run_id
           AND g.intent_id = NEW.intent_id
           AND (
               NEW.metadata_snapshot_id IS NULL
               OR m.generation_run_id = NEW.generation_run_id
           )
    ) THEN
        RAISE EXCEPTION 'considered revision % has inconsistent generation lineage',
            NEW.considered_revision_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION featuregen_check_formula_expected_lineage() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM feature_generation_run g
          JOIN confirmed_generation_scope s ON s.scope_id = NEW.confirmed_scope_id
          JOIN contract_considered_revision r
            ON r.considered_revision_id = NEW.considered_revision_id
         WHERE g.generation_run_id = NEW.generation_run_id
           AND g.intent_id = NEW.intent_id
           AND s.generation_run_id = NEW.generation_run_id
           AND s.intent_id = NEW.intent_id
           AND r.generation_run_id = NEW.generation_run_id
           AND r.intent_id = NEW.intent_id
           AND r.considered_content_hash = NEW.considered_content_hash
    ) THEN
        RAISE EXCEPTION 'formula expected run % has inconsistent generation lineage',
            NEW.generation_run_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION featuregen_check_formula_manifest_lineage() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM recipe_formula_shadow_expected_run e
         WHERE e.generation_run_id = NEW.generation_run_id
           AND e.intent_id = NEW.intent_id
           AND e.considered_revision_id = NEW.considered_revision_id
           AND e.considered_content_hash = NEW.considered_content_hash
    ) THEN
        RAISE EXCEPTION 'formula manifest % has inconsistent generation lineage', NEW.manifest_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION featuregen_check_formula_observation_lineage() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM recipe_formula_shadow_expected_run e
          JOIN contract_considered_revision r
            ON r.considered_revision_id = NEW.considered_revision_id
          LEFT JOIN catalog_metadata_snapshot m ON m.snapshot_id = NEW.metadata_snapshot_id
         WHERE e.generation_run_id = NEW.generation_run_id
           AND e.intent_id = NEW.intent_id
           AND e.considered_revision_id = NEW.considered_revision_id
           AND e.considered_content_hash = NEW.considered_content_hash
           AND r.generation_run_id = NEW.generation_run_id
           AND r.intent_id = NEW.intent_id
           AND r.metadata_snapshot_id IS NOT DISTINCT FROM NEW.metadata_snapshot_id
           AND (
               NEW.metadata_snapshot_id IS NULL
               OR m.generation_run_id = NEW.generation_run_id
           )
    ) THEN
        RAISE EXCEPTION 'formula observation % has inconsistent generation lineage',
            NEW.observation_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION featuregen_check_formula_work_item_lineage() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM recipe_formula_shadow_expected_run e
          JOIN contract_considered_revision r
            ON r.considered_revision_id = NEW.considered_revision_id
          LEFT JOIN catalog_metadata_snapshot m ON m.snapshot_id = NEW.metadata_snapshot_id
         WHERE e.generation_run_id = NEW.generation_run_id
           AND e.intent_id = NEW.intent_id
           AND e.considered_revision_id = NEW.considered_revision_id
           AND e.considered_content_hash = NEW.considered_content_hash
           AND r.generation_run_id = NEW.generation_run_id
           AND r.intent_id = NEW.intent_id
           AND r.metadata_snapshot_id IS NOT DISTINCT FROM NEW.metadata_snapshot_id
           AND (
               NEW.metadata_snapshot_id IS NULL
               OR m.generation_run_id = NEW.generation_run_id
           )
    ) THEN
        RAISE EXCEPTION 'formula work item % has inconsistent generation lineage', NEW.work_item_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER confirmed_generation_scope_lineage_deferred
    AFTER INSERT ON confirmed_generation_scope
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION featuregen_check_confirmed_scope_lineage();
CREATE CONSTRAINT TRIGGER contract_considered_revision_lineage_deferred
    AFTER INSERT ON contract_considered_revision
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION featuregen_check_considered_revision_lineage();
CREATE CONSTRAINT TRIGGER recipe_formula_shadow_expected_lineage_deferred
    AFTER INSERT ON recipe_formula_shadow_expected_run
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION featuregen_check_formula_expected_lineage();
CREATE CONSTRAINT TRIGGER recipe_formula_shadow_manifest_lineage_deferred
    AFTER INSERT OR UPDATE ON recipe_formula_shadow_run_manifest
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION featuregen_check_formula_manifest_lineage();
CREATE CONSTRAINT TRIGGER recipe_formula_shadow_observation_lineage_deferred
    AFTER INSERT ON recipe_formula_shadow_observation
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION featuregen_check_formula_observation_lineage();
CREATE CONSTRAINT TRIGGER recipe_formula_shadow_work_item_lineage_deferred
    AFTER INSERT ON recipe_formula_shadow_work_item
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION featuregen_check_formula_work_item_lineage();

ALTER TABLE confirmed_scope_use_case
    DROP CONSTRAINT confirmed_scope_use_case_origin_check;
ALTER TABLE confirmed_scope_use_case
    ADD CONSTRAINT confirmed_scope_use_case_origin_check CHECK (
        origin IN ('llm_proposed', 'accepted_llm_proposal', 'user_added', 'user_overridden')
    );
ALTER TABLE confirmed_scope_use_case
    ADD COLUMN proposed_relationship text NULL CHECK (
        proposed_relationship IS NULL OR proposed_relationship IN ('primary', 'secondary')
    );
ALTER TABLE confirmed_scope_use_case
    ADD COLUMN replaces_use_case_id text NULL;
