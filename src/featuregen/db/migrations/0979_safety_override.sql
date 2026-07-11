-- src/featuregen/db/migrations/0979_safety_override.sql
-- Phase 0 Authority Kernel (spec §7): the governed audit record for a below-floor sensitivity
-- DOWNGRADE. Sensitivity is a most-restrictive FLOOR that evidence may only RAISE; taking a fact
-- BELOW its floor is a privileged act that requires a specific authority (PRIVACY or SECURITY), a
-- rationale, a policy reference, and a bounded effective window — NOT a generic confirmation. This
-- is distinct from the compliance-gated free-text `policy_tag` basis. Each downgrade decision lands
-- one immutable row here (WRITE-ONCE), mirroring the blob/llm_call/security_audit stores: INSERT
-- (record_safety_override) is unaffected; a governance downgrade approval can never be silently
-- altered or deleted. Fully idempotent (CREATE ... IF NOT EXISTS / CREATE OR REPLACE).
CREATE TABLE IF NOT EXISTS safety_override (
    override_id           text        PRIMARY KEY,
    fact_key              text        NOT NULL,
    field                 text        NOT NULL,             -- the governed field, e.g. 'sensitivity'
    previous_floor        text        NOT NULL,             -- the floor being taken below
    override_value        text        NOT NULL,             -- the below-floor value approved
    approved_by_authority text        NOT NULL,             -- GovernanceAuthority (privacy|security)
    rationale             text        NOT NULL,
    policy_reference      text        NOT NULL,
    effective_from        timestamptz NULL,                 -- open-ended when NULL
    effective_until       timestamptz NULL,                 -- open-ended when NULL
    created_by            jsonb       NOT NULL,             -- identity_to_jsonb(actor)
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS safety_override_fact_key_idx ON safety_override (fact_key);

-- Physical immutability (no UPDATE/DELETE): the write-once backstop for a governance approval.
CREATE OR REPLACE FUNCTION safety_override_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'safety_override is write-once: % not allowed on override_id=%',
        TG_OP, COALESCE(OLD.override_id, NEW.override_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER safety_override_no_mutation
    BEFORE UPDATE OR DELETE ON safety_override
    FOR EACH ROW EXECUTE FUNCTION safety_override_write_once();
