-- src/featuregen/db/migrations/1057_crosswalk_observation.sql
-- Release C Task 11 (verified-interfaces doc D7 — 1057 is this task's ONLY number).
--
-- WHAT THIS IS FOR, and what it deliberately is not. A crosswalk is three tables. Each LEG of it is
-- an ordinary pair and is observed by the SHIPPED two-endpoint V2 store (migration 1038), untouched:
-- no column of `relationship_observation_revision` changes here, no stored row is rewritten, and no
-- three-table observation is forced into its two-endpoint shape. What that store cannot express is
-- the COMPOSITION, and the adversarial review named the three gaps (A5/A8):
--
--   1. composed cross-leg fan-out — how many TARGET rows one SOURCE row reaches through the mapping
--      is not a leg property, and the product of the two leg maxima is an upper BOUND, not a
--      measurement;
--   2. atomic multi-leg as-of — two legs read at two instants describe two different worlds;
--   3. lossy pinning — `partitions_read` keeps values and loses the column, `predicate_ids` keeps
--      ids and loses the content, so neither can be replayed.
--
-- This table carries the composition and the FULL pins. The 1038 rows keep their meaning exactly.
--
-- SCOPE 0 CARRIES NO DDL. The binding-address unification shipped in the same task re-addresses
-- nothing stored (the inventory-derived writer has no `src/` caller; the live writer already derived
-- `database` from the connection), so this file is spent entirely on the observation store — the
-- allocation D7 anticipated with "unused returns to the pool".
--
-- IMMUTABLE REVISIONS + CAS POINTER, the 1038/1050 discipline. `cwo_` + the RFC-8785 JCS content
-- hash (D1) names one measurement; the CURRENT pointer is per (definition revision, scope, pins,
-- principal) — the `current_scope_key`, which deliberately excludes the counts, so re-measuring the
-- same question REPLACES rather than accumulates while every measurement stays in history.
--
-- NO SAFETY COLUMN AND NO ADMISSION COLUMN. A row here says what was SEEN. Turning seeing into a
-- verdict is `crosswalk_admission`'s job, and its output pins these observation ids rather than
-- being stored inside them. A nullable safety column here would be read as an answer.

CREATE TABLE IF NOT EXISTS crosswalk_observation_revision (
    observation_revision_id  text        PRIMARY KEY
        CHECK (observation_revision_id ~ '^cwo_[0-9a-f]{64}$'),
    current_scope_key        text        NOT NULL CHECK (current_scope_key ~ '^[0-9a-f]{64}$'),
    crosswalk_definition_revision_id text NOT NULL
        REFERENCES crosswalk_definition_revision(revision_id),
    -- The two per-LEG rows in the SHIPPED two-endpoint store, BY SIDE — real foreign keys, so a
    -- composition naming a leg row that does not exist is refused at write time rather than
    -- discovered months later at a read.
    --
    -- NULLABLE, and the null is load-bearing rather than lazy. `relationship_observation_revision`
    -- is NOT NULL on `realization_revision_id` with an FK to `bridge_join_realization_revision`
    -- (1038:7-8). A CROSS-catalog leg has a governed realization by construction and its
    -- observation is an ordinary row there. A SAME-catalog leg resolves through `plan_join` and
    -- `JoinLegPinV1` REFUSES a same-catalog pin carrying realization revisions — so storing one
    -- there would mean FABRICATING a bridge realization, i.e. asserting a governed direct link
    -- nobody declared, which `record_relationship_observation` could then demote on a conflict.
    -- Every leg's FULL measurement therefore lives in `observation_json` (and in the identity);
    -- these columns say which legs are ALSO in the bridge family's own history.
    source_leg_observation_revision_id text NULL
        REFERENCES relationship_observation_revision(observation_revision_id),
    target_leg_observation_revision_id text NULL
        REFERENCES relationship_observation_revision(observation_revision_id),
    CHECK (source_leg_observation_revision_id IS NULL
           OR source_leg_observation_revision_id IS DISTINCT FROM target_leg_observation_revision_id),
    mapping_binding_revision_id text     NOT NULL
        REFERENCES physical_dataset_binding_revision(binding_revision_id),
    scope_id                 text        NOT NULL CHECK (length(scope_id) BETWEEN 1 AND 256),
    -- The ONE instant both legs and the mapping were read at (the atomic multi-leg pin). NULL only
    -- when no leg pinned one; the application refuses composition when the two legs disagree.
    as_of                    text        NULL CHECK (as_of IS NULL OR length(as_of) BETWEEN 1 AND 64),
    mapping_temporal_policy_revision_id text NULL
        CHECK (mapping_temporal_policy_revision_id IS NULL
               OR length(mapping_temporal_policy_revision_id) BETWEEN 1 AND 128),
    execution_principal      text        NOT NULL CHECK (length(execution_principal) BETWEEN 1 AND 256),
    -- The V2 axes, verbatim and still two of them (D4 deletes the `evidence_basis` merger).
    method                   text        NOT NULL CHECK (method IN ('exact', 'approximate')),
    row_coverage             text        NOT NULL
        CHECK (row_coverage IN ('full', 'sampled', 'partial')),
    complete                 boolean     NOT NULL,
    -- The two NAMED composed directions. These are SIDES of the canonically-ordered definition,
    -- never a claim about traversal order (the Task-10 caution, migration 1036's lesson).
    source_to_target_max_matches integer NOT NULL CHECK (source_to_target_max_matches >= 0),
    target_to_source_max_matches integer NOT NULL CHECK (target_to_source_max_matches >= 0),
    composed_row_count       bigint      NOT NULL CHECK (composed_row_count >= 0),
    -- A composition that joined nothing cannot have observed a fan-out.
    CHECK (composed_row_count > 0
           OR (source_to_target_max_matches = 0 AND target_to_source_max_matches = 0)),
    quality_rank             integer     NOT NULL CHECK (quality_rank BETWEEN 0 AND 30),
    producer                 text        NOT NULL CHECK (producer = 'profiler'),
    strength                 text        NOT NULL CHECK (strength = 'supported'),
    observation_json         jsonb       NOT NULL
        CHECK (jsonb_typeof(observation_json) = 'object'),
    content_hash             text        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    observed_at              timestamptz NOT NULL,
    recorded_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS crosswalk_observation_definition_idx
    ON crosswalk_observation_revision (crosswalk_definition_revision_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS crosswalk_observation_scope_history_idx
    ON crosswalk_observation_revision (current_scope_key, observed_at DESC);

CREATE TABLE IF NOT EXISTS crosswalk_observation_current (
    current_scope_key       text        PRIMARY KEY CHECK (current_scope_key ~ '^[0-9a-f]{64}$'),
    observation_revision_id text        NOT NULL
        REFERENCES crosswalk_observation_revision(observation_revision_id),
    -- Denormalized so "what is current for this crosswalk" is one index read; the revision remains
    -- the authority and the store verifies the two agree on write.
    crosswalk_definition_revision_id text NOT NULL
        REFERENCES crosswalk_definition_revision(revision_id),
    quality_rank            integer     NOT NULL CHECK (quality_rank BETWEEN 0 AND 30),
    observed_at             timestamptz NOT NULL,
    -- A STORED 0 would make "no pointer existed" indistinguishable from a real version.
    pointer_version         bigint      NOT NULL CHECK (pointer_version > 0),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS crosswalk_observation_current_definition_idx
    ON crosswalk_observation_current (crosswalk_definition_revision_id);
