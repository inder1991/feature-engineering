-- src/featuregen/db/migrations/1095_authorization_referential_chain.sql
-- The chain: generation authorization -> generation request -> sealed artifact.
--
-- THE GAP. Every link existed as a VALUE and none as a REFERENCE. `generation_request` recorded a
-- build set and an environment but never which authorization permitted the work.
-- `sealed_artifact_v2` recorded an environment and a group but never what authorized producing it.
-- So "which approval produced this artifact" — the question an auditor asks about any published
-- number — was answerable only by matching loose fields and hoping the match meant something.
--
-- COMPOSITE FOREIGN KEYS, NOT COLUMNS PLUS CHECKS. The obvious fix is to add an id and validate
-- agreement in code. That leaves the disagreement REPRESENTABLE: a request could name an
-- authorization for another environment, and only a caller that remembered to look would notice.
-- Instead the referenced columns travel INSIDE the key:
--
--     generation_request (authorization, build_set, environment)
--         -> generation_authorization (revision_id, build_set_revision_id, environment_id)
--
--     sealed_artifact_v2 (authorization, environment, logical_group)
--         -> generation_authorization (revision_id, environment_id, logical_group_name)
--
-- A request naming an authorization issued for a different build set or environment is not caught;
-- it cannot be written. Same for an artifact sealed under an authorization for another group. The
-- two supporting UNIQUE indexes are what make those composite targets legal, and they are unique
-- by construction because `revision_id` is already the primary key.
--
-- NULLABLE, DELIBERATELY, AND THAT IS THE HONEST PART. The V1 chain writes neither table, and
-- backfilling an authorization for rows that never had one would invent the very evidence this
-- migration exists to make trustworthy. NULL means "this predates the chain" and is distinguishable
-- from any authorization; the V2 producers pass it and the readers refuse a NULL where an
-- authorization is required. When the V1 chain is deleted these become NOT NULL, in a migration
-- that can say so truthfully because nothing will be able to produce a NULL.
--
-- NOT APPLIED. This file is written, not run.

-- ── the composite targets ────────────────────────────────────────────────────────────────────────
-- UNIQUE CONSTRAINTS rather than unique indexes: Postgres will not accept a bare index as the
-- target of a composite foreign key ("there is no unique constraint matching given keys"), which
-- the first version of this migration discovered. Both are unique by construction anyway, since
-- `revision_id` is already the primary key — they exist to make the composite target legal, not to
-- add a guarantee.
--
-- Added conditionally rather than DROP-then-ADD. A drop-and-recreate rebuilds the backing index on
-- every re-run, and this file is applied twice by the idempotency check; more importantly a DROP
-- would briefly remove the target of a foreign key that already depends on it.
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

-- ── request -> authorization ─────────────────────────────────────────────────────────────────────
ALTER TABLE generation_request
    ADD COLUMN IF NOT EXISTS generation_authorization_revision_id text;

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
END $$;

-- ── artifact -> authorization ────────────────────────────────────────────────────────────────────
ALTER TABLE sealed_artifact_v2
    ADD COLUMN IF NOT EXISTS generation_authorization_revision_id text;

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
END $$;

-- The auditor's index: every artifact one approval produced.
CREATE INDEX IF NOT EXISTS sealed_artifact_v2_by_authorization
    ON sealed_artifact_v2 (generation_authorization_revision_id);
