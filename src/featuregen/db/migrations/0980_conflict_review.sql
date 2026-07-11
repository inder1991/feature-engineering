-- src/featuregen/db/migrations/0980_conflict_review.sql
-- Phase 0 Authority Kernel (spec §10): the conflict-review lifecycle + audit history. When
-- competing evidence disagrees on a governed field (e.g. two sources assert different sensitivity),
-- a conflict is OPENed for human review. Identity is a STABLE `fingerprint` (sha256 over the logical
-- ref, field, sorted competing value-hashes, and the field policy version) so a re-upload UPDATES /
-- REOPENs the existing conflict rather than duplicating it — distinct from ingest quarantine
-- (validation rows) and the fact STALE/REVERIFY flow (per-fact re-verify). `competing_value_hashes`
-- is stored alongside `competing_evidence_ids` because it is a fingerprint input and must round-trip.
-- Every state change appends one immutable `conflict_review_event` (from_state -> to_state, actor,
-- reason) so the full review history is auditable. Additive and idempotent (CREATE ... IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS conflict_review (
    conflict_id             text        PRIMARY KEY,
    fingerprint             text        NOT NULL UNIQUE,      -- stable identity; a re-upload reopens
    logical_ref             text        NOT NULL,
    field_name              text        NOT NULL,             -- the governed field in conflict
    severity                text        NOT NULL,
    competing_evidence_ids  jsonb       NOT NULL DEFAULT '[]',
    competing_value_hashes  jsonb       NOT NULL DEFAULT '[]',
    state                   text        NOT NULL DEFAULT 'open',
    owner                   text        NULL,                 -- assigned reviewer (nullable)
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conflict_review_state_idx ON conflict_review (state);

-- Append-only audit history: one row per state transition (including the initial OPEN and any
-- REOPEN). `from_state` is NULL for the initial OPEN; `actor` records WHO drove the transition.
CREATE TABLE IF NOT EXISTS conflict_review_event (
    event_id     text        PRIMARY KEY,
    conflict_id  text        NOT NULL REFERENCES conflict_review(conflict_id),
    from_state   text        NULL,
    to_state     text        NOT NULL,
    actor        text        NOT NULL,
    reason       text        NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conflict_review_event_conflict_idx
    ON conflict_review_event (conflict_id, created_at);
