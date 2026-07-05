-- src/featuregen/db/migrations/0959_contract_gate1_choice.sql
-- Phase-2 Gate #1 audit: the human's confirmed choice among the considered set (the anchor from the
-- requester's definition + the generated alternatives), with who + why + a snapshot of the full set.
-- This is the governance record — no feature contract is authored without a recorded human choice here.
CREATE TABLE IF NOT EXISTS contract_gate1_choice (
    intent_id        text        PRIMARY KEY,
    chosen_source    text        NOT NULL,    -- 'anchor' | 'alternative'
    chosen_option_id text        NOT NULL,    -- the chosen feature's name
    why              text        NOT NULL DEFAULT '',
    actor            jsonb       NULL,
    considered       jsonb       NOT NULL,     -- snapshot: anchor + alternatives + advisory recommendation
    created_at       timestamptz NOT NULL DEFAULT now()
);
