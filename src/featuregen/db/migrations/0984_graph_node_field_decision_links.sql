-- src/featuregen/db/migrations/0984_graph_node_field_decision_links.sql
-- Spec §4/§6: link the flat display columns on a graph_node back to the field_decision_event that
-- produced them (the resolve-and-project payoff). The flat column is the DISPLAY value (what a
-- reviewer sees); the DECISION is authority (is a load-bearing value present?). Operational/feature
-- code reads the decision via the *_decision_id link, NEVER the flat column — this is the
-- display ≠ authority boundary (must-prove #4/#5). One nullable link column per projected field.
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS concept_decision_id     text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS definition_decision_id  text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS domain_decision_id      text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS additivity_decision_id  text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS logical_type_decision_id text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS sensitivity_decision_id text NULL;

-- Sensitivity is special (spec §7, review #8): the taxonomy floor sets a most-restrictive
-- `effective_restriction` (in safety_floor.SENSITIVITY_ORDER — public<internal<confidential<
-- restricted<prohibited), DISTINCT from the existing read-scope `sensitivity` tag column
-- (pii|restricted) which stays untouched so search read-scope is unaffected. `classification_status`
-- stays 'proposed' until a source/human sensitivity CONFIRMS — a proposed floor RESTRICTS but does
-- not CERTIFY.
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS effective_restriction   text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS classification_status   text NULL;
