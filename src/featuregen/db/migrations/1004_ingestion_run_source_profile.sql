-- src/featuregen/db/migrations/1004_ingestion_run_source_profile.sql
-- feature-ready-ingestion Delivery B item 9: source-profile provenance on the run manifest. Every
-- field_evidence row B-T2 writes carries producer_ref = <ingestion run id>; this records, on that
-- run, WHICH source capability profile produced it — so stored evidence is traceable to a run AND
-- the capability rules that governed it.
--   source_type     — the profile identity (SourceCapabilityProfile.source_type). OPEN vocabulary
--                     ('technical_csv' / 'ftr_glossary' / connector-specific later), so no CHECK:
--                     a new connector profile must not require a migration.
--   profile_version — the capability-profile schema version stamp the run's evidence writers used
--                     (SOURCE_CAPABILITY_PROFILE_VERSION, e.g. 'scp-v1').
-- Both NULLABLE, honestly: the upload route opens its run BEFORE parse (design #3), so a run that
-- failed before profile selection (oversize / unsupported / unparseable) never knew a profile and
-- records NULL — never a fabricated default. Pre-1004 rows likewise stay NULL.
ALTER TABLE ingestion_run ADD COLUMN IF NOT EXISTS source_type     text;
ALTER TABLE ingestion_run ADD COLUMN IF NOT EXISTS profile_version text;
