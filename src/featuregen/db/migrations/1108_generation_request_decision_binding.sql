-- src/featuregen/db/migrations/1108_generation_request_decision_binding.sql
-- THE DECISION, ON THE AUTHORITATIVE ROW — not only inside a queue payload.
--
-- WHY. The request-time decision (1106) was frozen into the queue payload, and the worker recheck
-- read it from there. That works, and it is not enough: the queue row is the WORK ITEM and the
-- generation_request row is the RECORD — a redelivered, dead-lettered or reaped message takes the
-- payload's copy with it, and "which decision did this attempt run under" becomes unanswerable from
-- the durable tables alone. Owner ruling 2026-08-23 item 5: the decision persists on the
-- request/attempt rows themselves.
--
-- ▲ NULLABLE, DELIBERATELY, and this is the 1095 lesson applied in advance. The RUNNING image
-- writes generation_request; a NOT NULL column with no default breaks its INSERT the moment this
-- migration applies — exactly how sealing broke on the live cluster (image at 1093, DB at 1095).
-- Expand now (nullable, new code writes it), contract later (NOT NULL once no older image can run)
-- — the same discipline as 1100/1100b. The WORKER refuses a NULL at act time regardless
-- (ACTION_DECISION_MISSING), so a nullable column is not a nullable gate.

ALTER TABLE generation_request
    ADD COLUMN IF NOT EXISTS action_decision_revision_id text
        REFERENCES action_decision_revision(decision_id);

CREATE INDEX IF NOT EXISTS generation_request_by_decision
    ON generation_request (action_decision_revision_id)
    WHERE action_decision_revision_id IS NOT NULL;
