-- src/featuregen/db/migrations/0978_evidence_axes.sql
-- Phase 0 Authority Kernel (spec §3.1): extend the immutable overlay_evidence record with a
-- producer / strength / lifecycle axis plus item-linkage. This says WHO produced the evidence
-- (profiler vs LLM vs source vs human vs legacy), HOW strongly it is asserted, where it sits in its
-- lifecycle, and which upstream item it came from. Additive and idempotent: existing rows predate
-- these axes, so they backfill to producer='profiler', strength='supported', lifecycle='active'
-- (all pre-existing callers write profiling evidence — see the Step-0 caller audit). The two hashes
-- are nullable (only producer-specific evidence carries them); evidence_spans defaults to an empty
-- JSON array. Backward-compatible; every statement is IF NOT EXISTS.
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS producer                   text  NOT NULL DEFAULT 'profiler';
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS strength                   text  NOT NULL DEFAULT 'supported';
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS lifecycle                  text  NOT NULL DEFAULT 'active';
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS producer_configuration_hash text NULL;
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS producer_item_ref          text  NULL;
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS evidence_spans             jsonb NOT NULL DEFAULT '[]';
