-- src/featuregen/db/migrations/1089_review_bypassed_trace_kind.sql
-- S2 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): admit the DETERMINISTIC
-- path's trace event kind.
--
-- WHY A KIND OF ITS OWN, and not a `critic_result` with empty findings. "The critic ran and found
-- nothing" and "no critic ran" are different facts. Recording the second as the first would make a
-- reader infer which happened from an empty list — and an empty list is also what a clean critic
-- produces, so the inference is not available. A run that stood on a reviewed blueprint says so.
--
-- WHY THE DATABASE HAS AN OPINION AT ALL. 1022 declares the kind vocabulary as a CHECK, which is
-- what stops a typo becoming a new event class nobody reads. Extending it is therefore a migration
-- rather than a constant edit — the Python vocabulary (`replay_trace._KINDS`) and this CHECK are
-- two expressions of one closed set, and letting them drift would mean a kind the code writes and
-- the database refuses, discovered at the first live run.
--
-- WIDENING ONLY. No existing kind is removed and no row is touched. Every trace written before
-- this migration remains valid under the new constraint, which is why the swap is safe on a table
-- that is append-only and already holds live runs.
--
-- NOT APPLIED. This file is written, not run.

ALTER TABLE formula_authoring_trace_event
    DROP CONSTRAINT IF EXISTS formula_authoring_trace_event_kind_check;

ALTER TABLE formula_authoring_trace_event
    ADD CONSTRAINT formula_authoring_trace_event_kind_check CHECK (kind IN (
        'author_turn',
        'critic_result',
        'validation_result',
        -- S2: the deterministic path stood on a reviewed blueprint, and no critic ran.
        'review_bypassed',
        'completed',
        'failed'
    ));
