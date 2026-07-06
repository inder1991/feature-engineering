-- src/featuregen/db/migrations/0970_feature_name_unique.sql
-- Enforce the B4 "one feature per name" invariant at the DB level (it was convention-only in govern.py,
-- so a direct POST /features + a later confirm_contract for the same name minted a duplicate feature).
-- Fresh/pre-prod DB has no duplicates; on a populated DB with existing dupes this would need a dedupe
-- first. Registration paths catch the resulting UniqueViolation -> 409.
ALTER TABLE feature ADD CONSTRAINT feature_name_unique UNIQUE (name);
