-- src/featuregen/db/migrations/0965_contract_intent_provenance.sql
-- Review round-2 (contract API governance): make the security-critical state SERVER-side so an HTTP
-- client cannot disable the leakage gate or draft an unvetted feature.
--  * contract_intent.target_ref — the prediction target, persisted at considered-set time; draft/confirm
--    read it server-side instead of trusting a client-supplied (omittable) target_ref (BLOCKER 2).
--  * contract_considered — the validated considered set per intent; /contract/draft reconstructs the
--    chosen feature from HERE, not from an arbitrary client payload (BLOCKER 1).
--  * contract.intent_id — the audit link back to the hypothesis/intent (MAJOR 5).
ALTER TABLE contract_intent ADD COLUMN IF NOT EXISTS target_ref text NULL;
ALTER TABLE contract       ADD COLUMN IF NOT EXISTS intent_id  text NULL;
CREATE INDEX IF NOT EXISTS contract_intent_id_idx ON contract (intent_id);
CREATE TABLE IF NOT EXISTS contract_considered (
    intent_id  text        PRIMARY KEY,
    considered jsonb       NOT NULL,          -- snapshot: anchor + alternatives (with derives_pairs)
    created_at timestamptz NOT NULL DEFAULT now()
);
