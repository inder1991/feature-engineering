-- src/featuregen/db/migrations/1143_target_authoring_provenance.sql
--
-- What the tool proposed, what the person said about changing it, and what it was adapted from.
--
-- RESERVATION. 1142 was allocated 2026-09-01 and applied live 2026-09-02 08:29; 1143 is allocated
-- here. Migration files apply lexically and are checksummed — immutable once applied anywhere.
--
-- WHY COLUMNS AND NOT TABLES: all three are 1:1 with the definition, immutable once written, and
-- never queried independently. Side tables would add joins to every read for no property columns
-- do not already have.
--
-- The DIFF between `proposed_draft` and the registered rule is the provenance — "proposed 90 days,
-- human changed it to 180" — which is why the proposal is stored rather than discarded once it has
-- been edited. `author_comment` carries the why behind that diff, and is the one thing the diff
-- cannot say for itself.

ALTER TABLE target_definition
    ADD COLUMN IF NOT EXISTS proposed_draft jsonb;

ALTER TABLE target_definition
    ADD COLUMN IF NOT EXISTS author_comment text NOT NULL DEFAULT '';

-- ADAPT (spec §7.5 Step 4). Nullable because most labels descend from nothing; a self-reference is
-- refused because a self-referential lineage renders as an infinite chain to anything walking it.
ALTER TABLE target_definition
    ADD COLUMN IF NOT EXISTS adapted_from text REFERENCES target_definition(definition_id);

ALTER TABLE target_definition
    DROP CONSTRAINT IF EXISTS target_definition_adapted_from_not_self;
ALTER TABLE target_definition
    ADD CONSTRAINT target_definition_adapted_from_not_self
    CHECK (adapted_from IS NULL OR adapted_from <> definition_id);
