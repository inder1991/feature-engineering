-- Immutable candidate/realization revision substrate for cross-catalog identifier links.
-- Human review, deterministic safety and lifecycle remain separate axes.

CREATE TABLE IF NOT EXISTS governed_candidate_identity (
    candidate_id          text        PRIMARY KEY,
    candidate_family      text        NOT NULL,
    logical_identity_json jsonb       NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS governed_candidate_revision (
    candidate_revision_id text        PRIMARY KEY,
    candidate_id          text        NOT NULL
        REFERENCES governed_candidate_identity(candidate_id),
    assessment_json       jsonb       NOT NULL,
    assessment_version    text        NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS governed_candidate_revision_candidate_idx
    ON governed_candidate_revision(candidate_id, created_at);

CREATE TABLE IF NOT EXISTS governed_candidate_current (
    candidate_id          text        PRIMARY KEY
        REFERENCES governed_candidate_identity(candidate_id),
    candidate_revision_id text        NOT NULL
        REFERENCES governed_candidate_revision(candidate_revision_id),
    pointer_version       bigint      NOT NULL CHECK (pointer_version > 0),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bridge_join_realization_revision (
    realization_revision_id text        PRIMARY KEY,
    realization_id          text        NOT NULL,
    bridge_fact_key         text        NOT NULL,
    realization_json        jsonb       NOT NULL,
    dependency_snapshot_id  text        NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (realization_id, realization_revision_id)
);
CREATE INDEX IF NOT EXISTS bridge_join_realization_revision_identity_idx
    ON bridge_join_realization_revision(realization_id, created_at);
CREATE INDEX IF NOT EXISTS bridge_join_realization_revision_bridge_idx
    ON bridge_join_realization_revision(bridge_fact_key);

CREATE TABLE IF NOT EXISTS bridge_join_realization_current (
    realization_id          text        PRIMARY KEY,
    realization_revision_id text        NOT NULL
        REFERENCES bridge_join_realization_revision(realization_revision_id),
    safety_status            text        NOT NULL
        CHECK (safety_status IN ('unassessed', 'deterministically_validated', 'unsafe')),
    review_status            text        NOT NULL
        CHECK (review_status IN ('unreviewed', 'human_verified')),
    lifecycle                text        NOT NULL
        CHECK (lifecycle IN ('active', 'stale', 'rejected', 'superseded')),
    pointer_version          bigint      NOT NULL CHECK (pointer_version > 0),
    updated_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bridge_realization_dependency (
    realization_revision_id text        NOT NULL
        REFERENCES bridge_join_realization_revision(realization_revision_id),
    dependency_kind         text        NOT NULL,
    dependency_key          text        NOT NULL,
    dependency_revision     text        NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        realization_revision_id, dependency_kind, dependency_key, dependency_revision
    )
);
CREATE INDEX IF NOT EXISTS bridge_realization_dependency_reverse_idx
    ON bridge_realization_dependency(dependency_kind, dependency_key, dependency_revision);

CREATE TABLE IF NOT EXISTS bridge_realization_decision_event (
    decision_event_id       text        PRIMARY KEY,
    realization_revision_id text        NOT NULL
        REFERENCES bridge_join_realization_revision(realization_revision_id),
    decision_axis           text        NOT NULL
        CHECK (decision_axis IN ('deterministic_safety', 'human_review')),
    decision_value          text        NOT NULL,
    actor_json              jsonb       NOT NULL,
    evidence_json           jsonb       NOT NULL DEFAULT '{}',
    occurred_at             timestamptz NOT NULL DEFAULT now()
);

-- Preserve the legacy ledger as a compatibility read model and seed immutable history for rows that
-- predate this store. Modern assessments replace these synthetic current revisions on first
-- re-evaluation; the legacy table remains readable during migration.
INSERT INTO governed_candidate_identity (
    candidate_id, candidate_family, logical_identity_json, created_at
)
SELECT DISTINCT ON (candidate_id)
       candidate_id,
       'identifier_link',
       jsonb_build_object(
           'entity_id', entity_id,
           'left_catalog_source', left_catalog_source,
           'left_object_ref', left_object_ref,
           'right_catalog_source', right_catalog_source,
           'right_object_ref', right_object_ref
       ),
       updated_at
FROM entity_bridge_candidate_evidence
ORDER BY candidate_id, updated_at DESC
ON CONFLICT (candidate_id) DO NOTHING;

INSERT INTO governed_candidate_revision (
    candidate_revision_id, candidate_id, assessment_json, assessment_version, created_at
)
SELECT 'legacy:' || md5(
           candidate_id || ':' || derivation_version || ':' || evidence_json::text
       ),
       candidate_id,
       jsonb_build_object(
           'legacy', true,
           'entity_id', entity_id,
           'left_catalog_source', left_catalog_source,
           'left_object_ref', left_object_ref,
           'right_catalog_source', right_catalog_source,
           'right_object_ref', right_object_ref,
           'fact_key', fact_key,
           'data_type_family', data_type_family,
           'evidence', evidence_json
       ),
       derivation_version,
       updated_at
FROM entity_bridge_candidate_evidence
ON CONFLICT (candidate_revision_id) DO NOTHING;

INSERT INTO governed_candidate_current (
    candidate_id, candidate_revision_id, pointer_version, updated_at
)
SELECT DISTINCT ON (candidate_id)
       candidate_id,
       'legacy:' || md5(
           candidate_id || ':' || derivation_version || ':' || evidence_json::text
       ),
       1,
       updated_at
FROM entity_bridge_candidate_evidence
ORDER BY candidate_id, updated_at DESC
ON CONFLICT (candidate_id) DO NOTHING;
