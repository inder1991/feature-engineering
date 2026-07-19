-- src/featuregen/db/migrations/1008_contract_metadata_snapshot_binding.sql
-- MF-3 — durably bind the CONFIRMED contract to ITS immutable metadata snapshot (Delivery C0).
-- The confirmed contract's only durable snapshot link WAS contract_considered.snapshot_id, set via
-- ON CONFLICT (intent_id) DO UPDATE (gate1.py) — a MUTABLE upsert pointer. A confirm-then-broaden on the
-- same owned intent silently repoints it S1->S2 AFTER the contract was governed against S1, so a
-- regulator reconstructing "what catalog state was this contract authored against" via
-- contract.intent_id -> contract_considered.snapshot_id can get the WRONG snapshot. Recording the snapshot
-- the contract was CONFIRMED against directly on the contract row (which is never mutated by any code path
-- — confirm always INSERTs a NEW version) makes the binding immutable: a later broaden cannot repoint it.
-- ADDITIVE + NULLABLE (no CHECK): a pre-C0 or non-REPEATABLE-READ contract that took no snapshot simply
-- leaves these NULL. Plain text refs (mirrors 1007's considered-set lineage), NOT foreign keys.
-- Idempotent (ADD COLUMN IF NOT EXISTS).
ALTER TABLE contract ADD COLUMN IF NOT EXISTS metadata_snapshot_id  text;
ALTER TABLE contract ADD COLUMN IF NOT EXISTS metadata_content_hash text;
