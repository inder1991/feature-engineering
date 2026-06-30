-- src/featuregen/db/migrations/0506_overlay_timers.sql
-- SP-1 Phase 1 (design §2.3): admit the overlay expiry timer kind. Additive + idempotent.
ALTER TABLE timers DROP CONSTRAINT IF EXISTS timers_kind_check;
ALTER TABLE timers ADD CONSTRAINT timers_kind_check CHECK (
    kind IN ('sla','reminder','escalation','auto_park',
             'experiment_expiry','business_repair','cost_breaker','overlay_expiry')
);
