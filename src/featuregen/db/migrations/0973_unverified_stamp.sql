-- src/featuregen/db/migrations/0973_unverified_stamp.sql
-- Honest verification lifecycle: introduce the UNVERIFIED rung and re-stamp contract-less features.
-- Direct registration (POST /features) is UNVERIFIED; DESIGN-CHECKED is earned only via the governed
-- contract flow (confirm_contract re-runs the MCV). Closes the false-stamp (review finding #4).

ALTER TABLE feature ALTER COLUMN verification SET DEFAULT 'UNVERIFIED';

DO $$
DECLARE n_restamped int;
BEGIN
  UPDATE feature SET verification = 'UNVERIFIED'
   WHERE verification = 'DESIGN-CHECKED'
     AND feature_id NOT IN (SELECT feature_id FROM contract);
  GET DIAGNOSTICS n_restamped = ROW_COUNT;
  RAISE NOTICE 'restamped % contract-less feature(s) to UNVERIFIED', n_restamped;
  RAISE NOTICE '% consumer link(s) now reference an UNVERIFIED feature',
    (SELECT count(*) FROM feature_consumer fc
       JOIN feature f ON f.feature_id = fc.feature_id
      WHERE f.verification = 'UNVERIFIED');
END $$;

ALTER TABLE feature  ADD CONSTRAINT feature_verification_ck
  CHECK (verification IN ('UNVERIFIED', 'DESIGN-CHECKED', 'DATA-CHECKED', 'USEFULNESS-CHECKED'));
ALTER TABLE contract ADD CONSTRAINT contract_verification_ck
  CHECK (verification IN ('UNVERIFIED', 'DESIGN-CHECKED', 'DATA-CHECKED', 'USEFULNESS-CHECKED'));
