-- src/featuregen/db/migrations/0972_integrity_constraints.sql
-- Referential + domain integrity the model was missing (loose text links / unconstrained status).
-- Guarded with pg_constraint checks so a partial re-apply is a no-op. On a consistent DB every value
-- already satisfies these, so validation is clean; a fresh DB has no rows.

-- contract.feature_id must reference a real registered feature (was a bare NOT NULL text column).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contract_feature_id_fk') THEN
        ALTER TABLE contract ADD CONSTRAINT contract_feature_id_fk
            FOREIGN KEY (feature_id) REFERENCES feature (feature_id);
    END IF;
END $$;

-- contract.intent_id (nullable audit link) must reference a real intent when set.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contract_intent_id_fk') THEN
        ALTER TABLE contract ADD CONSTRAINT contract_intent_id_fk
            FOREIGN KEY (intent_id) REFERENCES contract_intent (intent_id);
    END IF;
END $$;

-- contract_considered.intent_id (its PK) must reference a real intent.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contract_considered_intent_id_fk') THEN
        ALTER TABLE contract_considered ADD CONSTRAINT contract_considered_intent_id_fk
            FOREIGN KEY (intent_id) REFERENCES contract_intent (intent_id);
    END IF;
END $$;

-- entity_suggestion.status is a closed vocabulary (pending | applied | dismissed).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'entity_suggestion_status_chk') THEN
        ALTER TABLE entity_suggestion ADD CONSTRAINT entity_suggestion_status_chk
            CHECK (status IN ('pending', 'applied', 'dismissed'));
    END IF;
END $$;
