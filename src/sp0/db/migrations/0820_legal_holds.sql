-- legal_holds — §9 legal-hold / open-audit erasure exemption. Phase-08-owned NET-NEW table
-- (not part of the overview shared DDL; nothing else references it).
CREATE TABLE legal_holds (
    hold_id      text        PRIMARY KEY,                         -- 'hold_...'
    scope_kind   text        NOT NULL
                     CHECK (scope_kind IN ('blob','feature','feature_version','request','run','subject')),
    scope_ref    text        NOT NULL,
    reason       text        NOT NULL,
    placed_by    jsonb       NOT NULL,                            -- IdentityEnvelope
    placed_at    timestamptz NOT NULL DEFAULT now(),
    released_at  timestamptz NULL
);
CREATE INDEX legal_holds_active_idx ON legal_holds (scope_kind, scope_ref) WHERE released_at IS NULL;
