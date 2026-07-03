-- src/featuregen/db/migrations/0905_projection_skips.sql
-- Durable skip ledger for analytics fail-open projections (review MAJOR #20).
--
-- Analytics projections (§3.6) FAIL OPEN: an unappliable ("poison") event is skipped and the
-- checkpoint advances past it so the read model keeps making progress. Until now that skip was
-- SILENT — it produced a wrong number in a regulatory/analytics read model with no signal, a
-- BCBS 239 accuracy gap. run_projection now records each fail-open skip here (projection, event
-- global_seq, reason) so the omission is durable + auditable. This does NOT change fail-open
-- semantics (analytics projections still advance); it only makes the skip observable. The
-- fail-CLOSED path (which halts + marks projection_degraded) is unaffected.
--
-- Independent table with no foreign keys; its 0905_ prefix simply orders it after the core
-- Python DDL and the 05xx overlay migrations. Idempotent (CREATE TABLE IF NOT EXISTS) so
-- apply_migrations stays safely re-runnable. UNIQUE (projection_name, event_global_seq) makes
-- the INSERT ... ON CONFLICT DO NOTHING in run_projection idempotent under re-runs.
CREATE TABLE IF NOT EXISTS projection_skips (
    id             bigserial PRIMARY KEY,
    projection_name text NOT NULL,
    event_global_seq bigint NOT NULL,
    reason         text NOT NULL,
    skipped_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (projection_name, event_global_seq)
);
