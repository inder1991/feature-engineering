-- Shared immutable store for validated structured results.
--
-- ``llm_call`` records how a provider call happened.  This store records the typed, validated
-- result that a workflow is allowed to consume.  Result identity is content-addressed and producer
-- provenance is many-to-one so replaying an equivalent result does not fork semantic identity.

CREATE TABLE IF NOT EXISTS structured_result (
    structured_result_id text        PRIMARY KEY,
    result_type          text        NOT NULL,
    result_version       integer     NOT NULL CHECK (result_version > 0),
    input_content_hash   text        NOT NULL,
    output_content_hash  text        NOT NULL,
    output_json          jsonb       NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    -- One canonical validated answer per versioned input. A prompt/schema/policy change must bump
    -- result_version (or change the input hash); replay never silently chooses between two answers.
    UNIQUE (result_type, result_version, input_content_hash)
);
CREATE INDEX IF NOT EXISTS structured_result_input_idx
    ON structured_result(result_type, result_version, input_content_hash);

CREATE TABLE IF NOT EXISTS structured_result_provenance (
    structured_result_id text        NOT NULL
        REFERENCES structured_result(structured_result_id),
    producer_kind        text        NOT NULL,
    producer_ref         text        NOT NULL,
    authority_json       jsonb       NOT NULL DEFAULT '{}',
    recorded_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (structured_result_id, producer_kind, producer_ref)
);
CREATE INDEX IF NOT EXISTS structured_result_provenance_ref_idx
    ON structured_result_provenance(producer_kind, producer_ref);
