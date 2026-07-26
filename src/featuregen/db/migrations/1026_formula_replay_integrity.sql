-- Delivery R4: replay identity and authoritative queue-fence lineage.
ALTER TABLE formula_authoring_run
    ADD COLUMN IF NOT EXISTS queue_id bigint NULL REFERENCES queue(id),
    ADD COLUMN IF NOT EXISTS lease_owner text NULL,
    ADD COLUMN IF NOT EXISTS lease_fence bigint NULL;

ALTER TABLE formula_authoring_run DROP CONSTRAINT IF EXISTS formula_authoring_run_fence_shape;
ALTER TABLE formula_authoring_run ADD CONSTRAINT formula_authoring_run_fence_shape CHECK (
    (queue_id IS NULL AND lease_owner IS NULL AND lease_fence IS NULL)
    OR
    (queue_id IS NOT NULL AND lease_owner IS NOT NULL AND lease_fence IS NOT NULL)
);

ALTER TABLE formula_authoring_trace_event
    ADD COLUMN IF NOT EXISTS stage text NULL,
    ADD COLUMN IF NOT EXISTS logical_turn_index integer NULL,
    ADD COLUMN IF NOT EXISTS canonical_input_hash text NULL,
    ADD COLUMN IF NOT EXISTS provider_contract_hash text NULL,
    ADD COLUMN IF NOT EXISTS tool_context_hash text NULL,
    ADD COLUMN IF NOT EXISTS canonical_output_hash text NULL,
    ADD COLUMN IF NOT EXISTS payload_hash text NULL,
    ADD COLUMN IF NOT EXISTS queue_id bigint NULL REFERENCES queue(id),
    ADD COLUMN IF NOT EXISTS lease_owner text NULL,
    ADD COLUMN IF NOT EXISTS lease_fence bigint NULL;

ALTER TABLE formula_authoring_trace_event
    DROP CONSTRAINT IF EXISTS formula_authoring_event_fence_shape;
ALTER TABLE formula_authoring_trace_event ADD CONSTRAINT formula_authoring_event_fence_shape CHECK (
    (queue_id IS NULL AND lease_owner IS NULL AND lease_fence IS NULL)
    OR
    (queue_id IS NOT NULL AND lease_owner IS NOT NULL AND lease_fence IS NOT NULL)
);

ALTER TABLE llm_dispatch
    ADD COLUMN IF NOT EXISTS queue_id bigint NULL REFERENCES queue(id),
    ADD COLUMN IF NOT EXISTS lease_owner text NULL,
    ADD COLUMN IF NOT EXISTS lease_fence bigint NULL;

ALTER TABLE llm_dispatch DROP CONSTRAINT IF EXISTS llm_dispatch_fence_shape;
ALTER TABLE llm_dispatch ADD CONSTRAINT llm_dispatch_fence_shape CHECK (
    (queue_id IS NULL AND lease_owner IS NULL AND lease_fence IS NULL)
    OR
    (queue_id IS NOT NULL AND lease_owner IS NOT NULL AND lease_fence IS NOT NULL)
);

-- Let an exact terminal retry reach the unique/idempotency conflict handler. Any different insert
-- after a terminal event remains forbidden.
CREATE OR REPLACE FUNCTION formula_authoring_reject_after_terminal() RETURNS trigger AS $$
DECLARE
    terminal formula_authoring_trace_event%ROWTYPE;
BEGIN
    SELECT * INTO terminal
      FROM formula_authoring_trace_event
     WHERE authoring_run_id = NEW.authoring_run_id
       AND kind IN ('completed', 'failed');
    IF FOUND THEN
        IF terminal.idempotency_key = NEW.idempotency_key
           AND terminal.seq = NEW.seq
           AND terminal.kind = NEW.kind
           AND terminal.llm_call_ref IS NOT DISTINCT FROM NEW.llm_call_ref
           AND terminal.payload = NEW.payload
           AND terminal.stage IS NOT DISTINCT FROM NEW.stage
           AND terminal.logical_turn_index IS NOT DISTINCT FROM NEW.logical_turn_index
           AND terminal.canonical_input_hash IS NOT DISTINCT FROM NEW.canonical_input_hash
           AND terminal.provider_contract_hash IS NOT DISTINCT FROM NEW.provider_contract_hash
           AND terminal.tool_context_hash IS NOT DISTINCT FROM NEW.tool_context_hash
           AND terminal.canonical_output_hash IS NOT DISTINCT FROM NEW.canonical_output_hash
           AND terminal.payload_hash IS NOT DISTINCT FROM NEW.payload_hash
           AND terminal.queue_id IS NOT DISTINCT FROM NEW.queue_id
           AND terminal.lease_owner IS NOT DISTINCT FROM NEW.lease_owner
           AND terminal.lease_fence IS NOT DISTINCT FROM NEW.lease_fence
        THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'formula authoring run % is already terminal', NEW.authoring_run_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
