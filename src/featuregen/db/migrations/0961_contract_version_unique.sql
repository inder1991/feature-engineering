-- src/featuregen/db/migrations/0961_contract_version_unique.sql
-- Review fix B4: a governed contract's (feature_name, version) must be unique, so the MAX+1 read-then-
-- insert race cannot persist two rows claiming the same version. A concurrent double-confirm now fails
-- the second INSERT (fail-closed) instead of silently duplicating a version.
ALTER TABLE contract ADD CONSTRAINT contract_name_version_unique UNIQUE (feature_name, version);
