-- attempt_memory — cross-aggregate dedup/exploration memory (§3.9). Owned by Phase 08.
-- Non-PII by construction; exempt from routine crypto-shred.
CREATE TABLE attempt_memory (
    definition_hash     text        PRIMARY KEY,                  -- content hash; never PII
    score               numeric     NULL,
    disposition         text        NOT NULL
                            CHECK (disposition IN ('explored','discarded','rejected','selected','promoted')),
    reason              text        NULL,
    request_id          text        NULL,
    feature_id          text        NULL,
    crypto_shred_exempt boolean     NOT NULL DEFAULT true,        -- survives erasure of source bodies
    first_seen          timestamptz NOT NULL DEFAULT now(),
    last_seen           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX attempt_memory_feature_idx ON attempt_memory (feature_id) WHERE feature_id IS NOT NULL;
