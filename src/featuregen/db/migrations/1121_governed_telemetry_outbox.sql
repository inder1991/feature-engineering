-- src/featuregen/db/migrations/1121_governed_telemetry_outbox.sql
-- Stage 1B task S1B-1, second half: the durable work queue that lets telemetry planning run OUT of
-- the request path — a run enqueues its frozen inputs, a worker leases the item, replans under
-- them, and writes the (immutable) observations 1120 holds.
--
-- ▲ THIS TABLE IS MUTABLE BY DESIGN — the ONE exception in this pair, which is why it lives in its
-- own file rather than inside 1120. The justification is narrow and worth stating precisely:
-- status, lease_owner, lease_expires_at, lease_fence, attempt_count, last_error and completed_at
-- are WORKER COORDINATION STATE, not evidence. Coordination state whose whole purpose is to change
-- (claim, expire, reclaim, finish) cannot be append-only without inventing a second table to hold
-- the changes. The evidence is elsewhere and IS append-only: the observations the worker writes.
-- frozen_inputs is the one identity-bearing column here, and nothing in this schema needs to
-- rewrite it — a different input set is a different work item.
--
-- LEASE VOCABULARY PARITY. The columns mirror the durable `queue` table that recipe_formula_worker
-- leases through (featuregen/runtime/queue.py: claim/renew/complete_recipe_formula_shadow):
-- lease_owner / lease_expires_at / lease_fence are spelled EXACTLY as there, and the claim is the
-- same `FOR UPDATE SKIP LOCKED` CTE that flips status, stamps the owner, extends the lease and
-- bumps both the attempt count and a monotonically increasing fence. Two deliberate divergences,
-- recorded so a reader does not mistake them for drift:
--   * `attempt_count` (queue spells it `attempts`) — this table's name is fixed by the Stage-1B
--     store contract, and the semantics are identical: incremented on every claim, including a
--     reclaim after expiry, so a poison item is visible as a rising count rather than a silence.
--   * status 'queued' (queue spells the same state 'ready') — likewise contract-fixed. The other
--     three states line up: leased / done / failed.
-- lease_fence is load-bearing: complete_telemetry_work refuses a completion whose fence is not the
-- one the caller leased under (fenced since the S1B-1 fix round), and the worker writes a whole
-- item inside one savepoint that rolls back on that refusal — a zombie worker that lost its lease
-- can neither finish an item the live worker now owns nor land observations beside its rows.

CREATE TABLE IF NOT EXISTS governed_telemetry_outbox (
    work_item_id      text        PRIMARY KEY,
    generation_run_id text        NOT NULL
        REFERENCES feature_generation_run (generation_run_id),
    intent_id         text        NULL REFERENCES contract_intent (intent_id),
    -- request refs (canonical ids + hashes), scope material, snapshot id, roles, target_entity and
    -- the anchor catalog: everything the worker needs to replan WITHOUT re-reading a moved catalog.
    frozen_inputs     jsonb       NOT NULL,
    status            text        NOT NULL DEFAULT 'queued',
    lease_owner       text        NOT NULL DEFAULT '',
    lease_expires_at  timestamptz NULL,
    lease_fence       bigint      NOT NULL DEFAULT 0,
    attempt_count     integer     NOT NULL DEFAULT 0,
    last_error        text        NOT NULL DEFAULT '',
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz NULL,

    CONSTRAINT governed_telemetry_outbox_status_chk CHECK (
        status IN ('queued', 'leased', 'done', 'failed'))
);

-- The claim's own access path: oldest claimable first, so a starved item cannot stay starved.
CREATE INDEX IF NOT EXISTS governed_telemetry_outbox_claim_idx
    ON governed_telemetry_outbox (status, lease_expires_at, recorded_at);
CREATE INDEX IF NOT EXISTS governed_telemetry_outbox_run_idx
    ON governed_telemetry_outbox (generation_run_id, recorded_at);

-- The SAME lineage rule 1120 installs on governed_planning_observation, from the SAME function: a
-- work item's denormalized intent must be the run's own intent, and NULL only when the run has none.
CREATE OR REPLACE TRIGGER governed_telemetry_outbox_intent_lineage
    BEFORE INSERT ON governed_telemetry_outbox
    FOR EACH ROW EXECUTE FUNCTION governed_observation_intent_lineage();
