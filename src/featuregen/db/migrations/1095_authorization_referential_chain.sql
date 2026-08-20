-- src/featuregen/db/migrations/1095_authorization_referential_chain.sql
-- The chain: build set -> authorization -> generation request -> sealed artifact.
--
-- THE GAP. Every link existed as a VALUE and none as a REFERENCE. `generation_request` recorded a
-- build set and an environment but never which approval permitted the work; `sealed_artifact_v2`
-- recorded an environment and a group but never what authorized producing it. So "which approval
-- produced this artifact" — the question an auditor asks about any published number — was
-- answerable only by matching loose fields and hoping the match meant something.
--
-- COMPOSITE FOREIGN KEYS, NOT COLUMNS PLUS CHECKS. The obvious fix adds an id and validates
-- agreement in code, which leaves the disagreement REPRESENTABLE: a request could name an
-- authorization for another environment and only a caller that remembered to look would notice.
-- Instead the referenced columns travel INSIDE the key, so a mismatch is not caught — it cannot be
-- written.
--
-- ▲ A FIRST VERSION OF THIS FILE STOPPED HALFWAY and was described as a chain. It was two BRANCHES
-- sharing an authorization:
--
--     request ---> authorization <--- artifact
--        |
--        +-- sealed_artifact_id : plain text, unenforced
--
-- so a SUCCEEDED request could point at a nonexistent artifact, one produced under a different
-- approval, or one from another environment. The request -> artifact edge below is what makes the
-- picture an actual chain.
--
-- ▲ AND IT MADE THE AUTHORIZATION NULLABLE, reasoning that NULL meant "predates the chain". That
-- reasoning does not survive contact with the writers: both producers defaulted the argument to
-- None, so NULL equally meant "a new caller forgot". A column whose absence has two meanings cannot
-- be used to tell them apart. The product is pre-live, the V1 chain writes NEITHER table, and both
-- are empty on every environment — so the honest form is NOT NULL now, before more unauthorized
-- development rows accumulate. The defaults are removed from the writers in the same change.
--
-- APPLIED to live 2026-08-20 (189 -> 191), after a scratch-restore dry run caught the orphan
-- authorization documented above. Backup: ~/featuregen-backups/featuregen-pre-1094-1095-*.dump

-- ── an approval names a build set that EXISTS ────────────────────────────────────────────────────
-- Missing entirely before. An approval for a build set nobody declared is a permission to generate
-- something that does not exist, and a downstream request rejecting it later is a worse place to
-- learn that than the moment it is granted.
--
-- ▲ NOT VALID, AND THE REASON IS STRUCTURAL RATHER THAN CONVENIENT. A validating FK aborts this
-- migration on the live database: one `generation_authorization` row exists (user:ops, 2026-08-17)
-- naming build set `bs-1`, and `build_set_revision` is EMPTY — an approval for a set that has never
-- existed, which is exactly the hole this constraint closes. Found by the scratch-restore dry run,
-- not on live.
--
-- It cannot be cleaned up first: `generation_authorization` is APPEND-ONLY by trigger (1082), so
-- the row cannot be deleted, and dropping that trigger to tidy one dev row would trade a durable
-- write-once guarantee for a cosmetic fix.
--
-- NOT VALID enforces every INSERT and UPDATE from here on and does not re-examine what is already
-- stored. That is the honest statement: the existing row predates the rule, cannot be removed, and
-- no new approval may repeat it. When the pre-live data is reset, run
--
--     ALTER TABLE generation_authorization
--         VALIDATE CONSTRAINT generation_authorization_covers_a_real_build_set;
--
-- which scans the table and promotes it to fully enforced. That command is deliberately NOT here:
-- it would fail today, and a migration that fails is worse than one that says what it is waiting on.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'generation_authorization_covers_a_real_build_set') THEN
        ALTER TABLE generation_authorization
            ADD CONSTRAINT generation_authorization_covers_a_real_build_set
            FOREIGN KEY (build_set_revision_id) REFERENCES build_set_revision (revision_id)
            NOT VALID;
    END IF;
END $$;

-- ── the composite targets ────────────────────────────────────────────────────────────────────────
-- UNIQUE CONSTRAINTS rather than unique indexes: Postgres will not accept a bare index as the
-- target of a composite foreign key ("there is no unique constraint matching given keys"). All
-- three are unique by construction, since the leading column is already a primary key — they exist
-- to make the composite targets legal, not to add a guarantee.
--
-- Added conditionally rather than DROP-then-ADD: a drop would briefly remove the target of a
-- foreign key already depending on it, and this file is applied twice by the idempotency check.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'generation_authorization_build_set_key') THEN
        ALTER TABLE generation_authorization
            ADD CONSTRAINT generation_authorization_build_set_key
            UNIQUE (revision_id, build_set_revision_id, environment_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'generation_authorization_group_key') THEN
        ALTER TABLE generation_authorization
            ADD CONSTRAINT generation_authorization_group_key
            UNIQUE (revision_id, environment_id, logical_group_name);
    END IF;
END $$;

-- ── artifact -> authorization ────────────────────────────────────────────────────────────────────
ALTER TABLE sealed_artifact_v2
    ADD COLUMN IF NOT EXISTS generation_authorization_revision_id text;
UPDATE sealed_artifact_v2 SET generation_authorization_revision_id = NULL WHERE FALSE;
ALTER TABLE sealed_artifact_v2
    ALTER COLUMN generation_authorization_revision_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'sealed_artifact_v2_authorized_for_this_group') THEN
        ALTER TABLE sealed_artifact_v2
            ADD CONSTRAINT sealed_artifact_v2_authorized_for_this_group
            FOREIGN KEY (generation_authorization_revision_id, environment_id,
                         logical_group_name)
            REFERENCES generation_authorization (revision_id, environment_id,
                                                 logical_group_name);
    END IF;
    -- The target of the request -> artifact edge below.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'sealed_artifact_v2_authorization_key') THEN
        ALTER TABLE sealed_artifact_v2
            ADD CONSTRAINT sealed_artifact_v2_authorization_key
            UNIQUE (artifact_id, generation_authorization_revision_id, environment_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS sealed_artifact_v2_by_authorization
    ON sealed_artifact_v2 (generation_authorization_revision_id);

-- ── request -> authorization, AND request -> artifact ────────────────────────────────────────────
ALTER TABLE generation_request
    ADD COLUMN IF NOT EXISTS generation_authorization_revision_id text;
ALTER TABLE generation_request
    ALTER COLUMN generation_authorization_revision_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'generation_request_authorized_for_this_work') THEN
        ALTER TABLE generation_request
            ADD CONSTRAINT generation_request_authorized_for_this_work
            FOREIGN KEY (generation_authorization_revision_id, build_set_revision_id,
                         environment_id)
            REFERENCES generation_authorization (revision_id, build_set_revision_id,
                                                 environment_id);
    END IF;
    -- THE EDGE THAT MAKES IT A CHAIN. A request may only point at an artifact produced under the
    -- SAME approval in the SAME environment. Without it `sealed_artifact_id` was plain text: a
    -- SUCCEEDED request could name an artifact that does not exist, or one somebody else's approval
    -- produced, and nothing in the schema would object.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'generation_request_produced_this_artifact') THEN
        ALTER TABLE generation_request
            ADD CONSTRAINT generation_request_produced_this_artifact
            FOREIGN KEY (sealed_artifact_id, generation_authorization_revision_id,
                         environment_id)
            REFERENCES sealed_artifact_v2 (artifact_id,
                                           generation_authorization_revision_id,
                                           environment_id);
    END IF;
END $$;
