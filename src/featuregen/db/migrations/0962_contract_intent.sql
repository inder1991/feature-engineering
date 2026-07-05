-- src/featuregen/db/migrations/0962_contract_intent.sql
-- Review fix M6: durably record the intent. The mandatory hypothesis is the feature's premise — without
-- this table the DB could not answer "what hypothesis motivated this contract?" (only an intent_id FK
-- survived into contract_gate1_choice). Relational, per the M6 persistence decision (not event-sourced).
CREATE TABLE IF NOT EXISTS contract_intent (
    intent_id           text        PRIMARY KEY,
    hypothesis          text        NOT NULL,
    definition          text        NOT NULL DEFAULT '',
    intake_mode         text        NOT NULL,           -- 'definition' | 'hypothesis'
    redacted_hypothesis text        NOT NULL DEFAULT '',
    redacted_definition text        NOT NULL DEFAULT '',
    actor               jsonb       NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
