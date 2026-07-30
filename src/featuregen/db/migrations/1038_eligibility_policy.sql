-- src/featuregen/db/migrations/1038_eligibility_policy.sql
--
-- Which rows COUNT — the one business decision the analysis IR refuses to proceed without.
--
-- `TransactionEligibilityPolicyV1` existed with nowhere to live: every test invented one, and no
-- deployment could supply it, so `ExecutionInputs` was unassemblable even once a table was bound
-- (migration 1037). This is where a source's definition of "a transaction that counts" is recorded.
--
-- NOT CONFIGURATION. A binding is an address — one right answer, an operator task. This is a
-- JUDGEMENT about the bank's data: whether a PENDING transaction is activity, whether a reversal
-- that arrived after the cutoff was known at the cutoff. Two reasonable people can disagree, and the
-- answer changes the number rather than merely widening it. So the row records WHO decided and WHEN,
-- and an unconfirmed policy is usable but disclosed — the same rule the planning layer applies
-- everywhere else: a doubt travels with the answer, a refusal blocks it.
--
-- `reversal_mode` carries the parked pilot decision (`eligibility.REVERSAL_AS_OF_UNRESOLVED`). Only
-- `boolean_or_code_column` is implemented; the other four are refused at construction rather than
-- approximated, because treating a compensating row as a flag silently counts both a transaction and
-- its reversal.

CREATE TABLE IF NOT EXISTS eligibility_policy (
    catalog_source        text        NOT NULL,
    table_name            text        NOT NULL,
    status_column         text        NOT NULL,
    included_status_values text[]     NOT NULL,
    reversal_mode         text        NOT NULL,
    reversal_column       text        NOT NULL,
    non_reversed_values   text[]      NOT NULL,
    null_behavior         text        NOT NULL DEFAULT 'exclude',
    -- Provenance. `proposed_by` is whoever (or whatever) suggested it; `confirmed_by` is NULL until a
    -- human has agreed. Both are kept: a policy an LLM proposed and a human confirmed is a different
    -- thing from one a human authored, and collapsing them loses the distinction the moment it
    -- matters.
    proposed_by           text        NOT NULL,
    confirmed_by          text,
    confirmed_at          timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_source, table_name)
);

-- One policy per table. A second would mean two definitions of "counts" for one table, with read
-- order deciding which answer a user got.
CREATE UNIQUE INDEX IF NOT EXISTS eligibility_policy_identity_idx
    ON eligibility_policy (catalog_source, lower(table_name));
