-- src/featuregen/db/migrations/1074_inventory_and_bound_input_set.sql
-- S3 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the generation inventory
-- observation and the bound input set revision.
--
-- ACCEPTANCE, MADE STRUCTURAL. "Binding is unreachable without an inventory" is a foreign key here,
-- not a check somebody remembers to run: `bound_input_set_revision.inventory_observation_id` is
-- NOT NULL and REFERENCES the observation. `compile_feature_group` and `compile_ir` both already
-- require an inventory, so a binding recorded without one would describe a resolution that could
-- not have happened — and would be indistinguishable from one that did.
--
-- "The bound set is addressable independently of any policy" is the second clause, and it is
-- satisfied by ABSENCE: there is no policy column, no policy table reference and no policy-shaped
-- join anywhere below. C-C7's occurrence derivation CONSUMES a bound set; the reverse dependency
-- would make the two mutually recursive and neither constructible first.
--
-- STORE THE WHOLE OBSERVATION, HASH A SUBSET (C-B7). `inventory_json` keeps the complete snapshot
-- for audit; `content_hash` covers only environment + engine versions + the schema mappings
-- actually used + the layouts for the exact read set. Without that split, adding an unrelated table
-- to a cluster would invalidate every compiled feature, and so would re-capturing the same
-- environment tomorrow. `observation_id` and the inventory's own `captured_at` are provenance and
-- are deliberately outside the hash — that is what makes an identical re-capture identical.
--
-- ONE FILE, ONE OWNER. F5's hazard is several deliverables written INDEPENDENTLY against one
-- reserved filename, since `apply_migrations` ledgers by stem AND byte checksum and whichever lands
-- first owns it. Both S3 deliverables are written here in one commit by one author, so the file is
-- whole on arrival — the same reason 1072 carries both of S1's. Where deliverables were authored
-- separately (S2's three) each took its own number.
--
-- NOT APPLIED. This file is written, not run.

-- ── the generation inventory observation ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS generation_inventory_observation (
    observation_id   text PRIMARY KEY CHECK (btrim(observation_id) <> ''),
    environment_id   text NOT NULL CHECK (btrim(environment_id) <> ''),

    -- The COMPLETE snapshot, for audit. Never the thing identity is computed over.
    inventory_json   jsonb NOT NULL,

    -- What the compilation actually consulted — the two lists that narrow the identity. Stored so a
    -- reader can see WHY the hash covers what it covers without re-deriving the compilation.
    used_schema_refs jsonb NOT NULL,
    read_set         jsonb NOT NULL,

    -- Over environment + engine versions + used mappings + the layouts for the read set. NOT over
    -- `inventory_json`, and the difference is the whole point of the type.
    content_hash     text NOT NULL CHECK (btrim(content_hash) <> ''),

    captured_at      text NOT NULL CHECK (btrim(captured_at) <> ''),
    recorded_at      timestamptz NOT NULL DEFAULT now()
);

-- "Which observations are the same environment as far as a compilation is concerned" — the question
-- an operator asks when deciding whether a re-capture invalidated anything. It did not if the hash
-- matches, and this index is how that is answered without a scan.
CREATE INDEX IF NOT EXISTS generation_inventory_observation_by_content
    ON generation_inventory_observation (content_hash);

-- ── the bound input set ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bound_input_set_revision (
    revision_id              text PRIMARY KEY CHECK (btrim(revision_id) <> ''),

    -- THE ACCEPTANCE. Binding is unreachable without an inventory, enforced by the database rather
    -- than by whoever calls the writer.
    inventory_observation_id text NOT NULL
                                  REFERENCES generation_inventory_observation(observation_id),

    -- Carried as well as reachable through the observation, because a bound set is read on its own
    -- and "which environment did this resolve in" must not require a join to answer.
    environment_id           text NOT NULL CHECK (btrim(environment_id) <> ''),
    content_hash             text NOT NULL CHECK (btrim(content_hash) <> ''),
    recorded_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bound_input (
    revision_id      text NOT NULL REFERENCES bound_input_set_revision(revision_id),
    logical_ref      text NOT NULL CHECK (btrim(logical_ref) <> ''),
    physical_dataset text NOT NULL CHECK (btrim(physical_dataset) <> ''),
    physical_column  text NOT NULL,

    -- One ref resolves to one place. Two bindings would make which one applies a row-order
    -- accident, which is exactly what the type refuses in memory.
    PRIMARY KEY (revision_id, logical_ref)
);

CREATE INDEX IF NOT EXISTS bound_input_by_dataset
    ON bound_input (physical_dataset);

-- ── append-only ──────────────────────────────────────────────────────────────────────────────────
-- An observation is what a compilation's identity was computed FROM, and a bound set is what it
-- resolved TO. Either being editable afterwards would let a sealed artifact's inputs be restated
-- without the artifact changing.
CREATE OR REPLACE FUNCTION s3_inventory_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS generation_inventory_observation_no_update
    ON generation_inventory_observation;
CREATE TRIGGER generation_inventory_observation_no_update
    BEFORE UPDATE OR DELETE ON generation_inventory_observation
    FOR EACH ROW EXECUTE FUNCTION s3_inventory_write_once();

DROP TRIGGER IF EXISTS bound_input_set_revision_no_update ON bound_input_set_revision;
CREATE TRIGGER bound_input_set_revision_no_update
    BEFORE UPDATE OR DELETE ON bound_input_set_revision
    FOR EACH ROW EXECUTE FUNCTION s3_inventory_write_once();

DROP TRIGGER IF EXISTS bound_input_no_update ON bound_input;
CREATE TRIGGER bound_input_no_update
    BEFORE UPDATE OR DELETE ON bound_input
    FOR EACH ROW EXECUTE FUNCTION s3_inventory_write_once();
