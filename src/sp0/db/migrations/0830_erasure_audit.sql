-- erasure_audit — audited crypto-shred trail (§9). Phase-08-owned NET-NEW table (not part of the
-- overview shared DDL). Records erasures AND retentions.
CREATE TABLE erasure_audit (
    erasure_id     text        PRIMARY KEY,                       -- 'era_...'
    blob_id        text        NOT NULL,
    classification text        NULL,
    kms_key_id     text        NULL,
    reason         text        NOT NULL,
    requested_by   jsonb       NOT NULL,                          -- IdentityEnvelope
    outcome        text        NOT NULL
                       CHECK (outcome IN ('shredded','retained_governance','retained_legal_hold','not_found')),
    performed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX erasure_audit_blob_idx ON erasure_audit (blob_id);
