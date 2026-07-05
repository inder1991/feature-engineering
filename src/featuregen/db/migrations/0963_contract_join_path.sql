-- src/featuregen/db/migrations/0963_contract_join_path.sql
-- B3 follow-on: persist the deterministic join path on the governed contract (grain -> derived tables).
-- The no-DB-honesty piece — the joins a contract assumes, recorded for the human/regulator. Enabled by
-- the catalog-qualified derives_pairs (B3): the join path is resolved in the correct catalog.
ALTER TABLE contract ADD COLUMN IF NOT EXISTS join_path jsonb NOT NULL DEFAULT '[]'::jsonb;
