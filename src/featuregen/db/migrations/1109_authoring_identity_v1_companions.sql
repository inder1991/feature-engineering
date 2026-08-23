-- src/featuregen/db/migrations/1109_authoring_identity_v1_companions.sql
-- IDENTITY V1, RECORDED AS WHAT IT WAS — a record of the defect, never a claim about history.
--
-- WHY. Identity V2 (the corrected composition: provider contract + strategy facts) activates with
-- the strategy wiring, and §11.1.1's order requires every EXISTING draft to carry an explicit V1
-- companion FIRST — otherwise "which composition minted this draft's identity?" has no answer, and
-- a V2 deploy would be indistinguishable from a world where V1 never existed. The V1 config hash is
-- the CONSTANT the getattr-on-a-dict defect produced; recording it with the payload that produced
-- it is honest — pretending to know the historical model configuration would be fabrication.
--
-- ▲ THE SCOPE KEY IS COMPUTED IN SQL, and one caveat is stated rather than hidden. JCS for a flat
-- object of strings is '{"k":"v",...}' with sorted keys, no spaces, RFC 8259 string escaping —
-- `to_json(text)::text` provides the escaping. For the identifier/hash-shaped values these columns
-- hold, no escaping fires and the SQL result equals Python's jcs_sha256 exactly. A pathological
-- `definition_revision` containing characters PG escapes differently from JCS's shortest-form rule
-- COULD diverge; the companion's scope key is informational (every ENFORCING scope-key computation
-- runs in Python from the draft's own columns), so divergence cannot mis-route a retirement.
-- Measured before writing: live holds 7 drafts, all with plain hash/id values.

INSERT INTO formula_draft_authoring_identity
    (formula_draft_id, identity_version, retirement_scope_key, config_payload_json, config_hash)
SELECT
    d.formula_draft_id,
    1,
    encode(sha256(convert_to(
        '{"catalog_snapshot_hash":' || to_json(d.catalog_snapshot_hash)::text
        || ',"considered_revision_id":' || to_json(d.considered_revision_id)::text
        || ',"definition_revision":' || to_json(d.definition_revision)::text
        || ',"option_id":' || to_json(d.option_id)::text
        || ',"planning_request_hash":' || to_json(d.planning_request_hash)::text
        || '}', 'UTF8')), 'hex'),
    -- The payload that PRODUCED the constant, recorded as the defect it is. For any draft whose
    -- stored hash is NOT the known constant (none exist today), the honest payload is "unrecorded".
    CASE WHEN d.authoring_config_hash =
              'f5c34b84d694062755f4b88605f9fc8d67e2f4ac1699054f99f6ccd09bfdc3c8'
         THEN '{"model": "", "max_tokens": 0, "prompt_id": "", "defect": "getattr-on-dict constant"}'::jsonb
         ELSE '{"unrecorded": true}'::jsonb
    END,
    d.authoring_config_hash
FROM formula_draft d
WHERE NOT EXISTS (SELECT 1 FROM formula_draft_authoring_identity i
                  WHERE i.formula_draft_id = d.formula_draft_id);
