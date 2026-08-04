-- src/featuregen/db/migrations/1050_crosswalk_definition.sql
-- Release C Task 10 (verified-interfaces doc D7 — 1050 is the crosswalk store, the ONLY number
-- this task allocates).
--
-- WHAT a crosswalk is: two identifier endpoints related THROUGH a mapping dataset. What it is NOT:
-- a bridge. Existing bridge fact keys and directional realization identities are load-bearing
-- (§4-correction-8), so nothing here touches `entity_bridge_candidate_evidence`,
-- `bridge_join_realization_revision` or their pointers. A crosswalk is a SEPARATE definition that
-- references the same endpoint types.
--
-- IMMUTABLE REVISIONS + CAS POINTER — the 1047/1048/1049 discipline, and behind it the strictest
-- template on the tree (`bridge_store.py:610-630`): every write is verified by reading the row back
-- and re-deriving its identity, so a store that silently dropped or mangled a revision is a raised
-- error, never a missing crosswalk somebody discovers months later.
--
-- 'cwd_' + the RFC-8785 JCS content hash (D1) names BOTH identity levels; the COLUMN says which
-- level a value is, exactly as 'pbr_' names a family rather than one table's key:
--   * definition_id — the two endpoint identities + the mapping dataset. What the crosswalk IS.
--   * revision_id   — additionally the ordered leg pairs, temporal policy and evidence. What this
--                     VERSION of it says.
-- Both are environment-independent: a definition holds LOGICAL, UNBOUND endpoints only, so no
-- re-upload and no binding move can re-key one. Physical identities belong to the execution
-- revision, which THIS TASK DELIBERATELY HAS NO PRODUCER FOR — there is no execution table here,
-- and its absence is the point rather than an omission.
--
-- NO EXECUTION COLUMN, NO SAFETY COLUMN, NO AVAILABILITY COLUMN. Discovery and execution safety are
-- separate axes (§5.8). A row here means "this crosswalk is proposed and visible", never "this
-- crosswalk may run". Safety is MEASURED by Task 11 against observations; adding a nullable
-- safety column now would invite a reader to treat NULL as an answer.
--
-- REVIEW STATUS IS ON THE POINTER, and its vocabulary is `bridge_assessment.LinkReviewStatus`
-- verbatim — no third review vocabulary. A human confirmation moves this column and NOTHING else:
-- it does not make a crosswalk visible (it already is), and it cannot make one executable (nothing
-- can, on this tree). Supersession is the pointer moving to a newer revision; a rejection/retirement
-- surface belongs to Task 13's governance work and is deliberately not invented here.
--
-- BOUNDED BY CHECK AS WELL AS BY CODE. The store bounds every text and list before it writes; these
-- CHECKs are the backstop for a hand-written row or a future caller that forgets. A leg with no
-- pairs is not a leg, and a 400-pair "leg" is a discovery bug arriving as a storage problem.

CREATE TABLE IF NOT EXISTS crosswalk_definition_revision (
    revision_id           text        PRIMARY KEY
        CHECK (revision_id ~ '^cwd_[0-9a-f]{64}$'),
    definition_id         text        NOT NULL
        CHECK (definition_id ~ '^cwd_[0-9a-f]{64}$'),
    -- Canonically ORDERED sides (the application canonicalizes at construction, migration 1036's
    -- lesson): 'source'/'target' name the two sides, never a traversal direction. Direction is
    -- measured by Task 11 and compiled by Task 12.
    source_endpoint_ref   text        NOT NULL CHECK (length(source_endpoint_ref) BETWEEN 1 AND 512),
    target_endpoint_ref   text        NOT NULL CHECK (length(target_endpoint_ref) BETWEEN 1 AND 512),
    mapping_dataset_ref   text        NOT NULL CHECK (length(mapping_dataset_ref) BETWEEN 1 AND 512),
    -- A crosswalk relates two DIFFERENT endpoints through a dataset that is neither of them.
    CHECK (source_endpoint_ref <> target_endpoint_ref),
    CHECK (mapping_dataset_ref <> source_endpoint_ref
           AND mapping_dataset_ref <> target_endpoint_ref),
    mapping_temporal_policy_revision_id text NULL
        CHECK (mapping_temporal_policy_revision_id IS NULL
               OR length(mapping_temporal_policy_revision_id) BETWEEN 1 AND 128),
    definition_json       jsonb       NOT NULL
        CHECK (jsonb_typeof(definition_json) = 'object'),
    CHECK (jsonb_array_length(definition_json -> 'source_to_mapping_pairs') BETWEEN 1 AND 16),
    CHECK (jsonb_array_length(definition_json -> 'mapping_to_target_pairs') BETWEEN 1 AND 16),
    CHECK (jsonb_array_length(definition_json -> 'evidence_refs') BETWEEN 0 AND 64),
    content_hash          text        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    authored_by           text        NOT NULL CHECK (length(authored_by) BETWEEN 1 AND 256),
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS crosswalk_definition_revision_definition_idx
    ON crosswalk_definition_revision (definition_id);
-- Discovery reads by dataset: "what crosswalks does this table take part in" is asked of all three
-- refs, so all three are indexed.
CREATE INDEX IF NOT EXISTS crosswalk_definition_revision_source_idx
    ON crosswalk_definition_revision (source_endpoint_ref);
CREATE INDEX IF NOT EXISTS crosswalk_definition_revision_target_idx
    ON crosswalk_definition_revision (target_endpoint_ref);
CREATE INDEX IF NOT EXISTS crosswalk_definition_revision_mapping_idx
    ON crosswalk_definition_revision (mapping_dataset_ref);

CREATE TABLE IF NOT EXISTS crosswalk_definition_current (
    definition_id   text        PRIMARY KEY
        CHECK (definition_id ~ '^cwd_[0-9a-f]{64}$'),
    revision_id     text        NOT NULL
        REFERENCES crosswalk_definition_revision(revision_id),
    -- `bridge_assessment.LinkReviewStatus`, verbatim. Confirmation is accountability, not
    -- permission and not availability.
    review_status   text        NOT NULL DEFAULT 'unreviewed'
        CHECK (review_status IN ('unreviewed', 'human_verified')),
    pointer_version integer     NOT NULL CHECK (pointer_version >= 1),
    declared_by     text        NOT NULL CHECK (length(declared_by) BETWEEN 1 AND 256),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
