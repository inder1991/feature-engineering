-- Immutable, scope-aware tuple relationship observations.
-- Currentness is per relationship/applicability/principal and never overwrites history.

CREATE TABLE IF NOT EXISTS relationship_observation_revision (
    observation_revision_id     text        PRIMARY KEY,
    current_scope_key           text        NOT NULL,
    realization_revision_id     text        NOT NULL
        REFERENCES bridge_join_realization_revision(realization_revision_id),
    left_binding_revision_id    text        NOT NULL
        REFERENCES physical_dataset_binding_revision(binding_revision_id),
    right_binding_revision_id   text        NOT NULL
        REFERENCES physical_dataset_binding_revision(binding_revision_id),
    left_source_snapshot_id     text        NOT NULL,
    right_source_snapshot_id    text        NOT NULL,
    scope_id                    text        NOT NULL,
    execution_principal         text        NOT NULL,
    method                      text        NOT NULL
        CHECK (method IN ('exact', 'approximate')),
    row_coverage                text        NOT NULL
        CHECK (row_coverage IN ('full', 'sampled', 'partial')),
    complete                    boolean     NOT NULL,
    quality_rank                integer     NOT NULL CHECK (quality_rank BETWEEN 0 AND 30),
    conflict_observed           boolean     NOT NULL,
    producer                    text        NOT NULL CHECK (producer = 'profiler'),
    strength                    text        NOT NULL CHECK (strength = 'supported'),
    observation_json            jsonb       NOT NULL,
    observed_at                 timestamptz NOT NULL,
    recorded_at                 timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS relationship_observation_realization_idx
    ON relationship_observation_revision(realization_revision_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS relationship_observation_scope_history_idx
    ON relationship_observation_revision(current_scope_key, observed_at DESC);
CREATE INDEX IF NOT EXISTS relationship_observation_conflict_idx
    ON relationship_observation_revision(
        realization_revision_id, conflict_observed, observed_at DESC
    );

CREATE TABLE IF NOT EXISTS relationship_observation_current (
    current_scope_key       text        PRIMARY KEY,
    observation_revision_id text        NOT NULL
        REFERENCES relationship_observation_revision(observation_revision_id),
    quality_rank            integer     NOT NULL CHECK (quality_rank BETWEEN 0 AND 30),
    observed_at             timestamptz NOT NULL,
    pointer_version         bigint      NOT NULL CHECK (pointer_version > 0),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
