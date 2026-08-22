-- src/featuregen/db/migrations/1105_llm_spend_authorization.sql
-- MONEY, AS A DURABLE AUTHORIZATION — because a confirmation modal is a UI event.
--
-- WHY THIS EXISTS. Both plans REQUIRE an approved cost ceiling: the action matrix has a blocker row
-- for its absence, the decision service takes one as evidence on AUTHOR_FORMULA, and the child's
-- "selection never spends" promise depends on it. Nothing owned it. A modal authorizes nothing,
-- survives nothing, and cannot be checked by a worker minutes later — which is the only moment that
-- matters, because that is when the provider call happens.
--
-- ▲ IMMUTABLE AUTHORIZATION, APPEND-ONLY EVENTS. An earlier design put mutable counters
-- (`calls_consumed`, `tokens_consumed`) on a table it called append-only, which is a contradiction —
-- and required consumption to be recorded "in the same transaction that records the dispatch",
-- which is IMPOSSIBLE: `record_dispatch` deliberately opens its OWN connection and commits
-- independently (`dispatch_audit.py:133,155`) so the audit survives a caller rollback. An outer
-- worker transaction cannot atomically update spend and audit a dispatch, by design.
--
-- So: RESERVE worst case before the call, SETTLE actuals after it. Both append-only, both on the
-- pre-dispatch connection that is the only seam seeing every PHYSICAL attempt — `drive_structured_call`
-- retries and repairs beneath the logical call, so a reservation taken at the logical layer
-- under-counts by exactly the retries.

CREATE TABLE IF NOT EXISTS llm_spend_authorization_revision (
    spend_authorization_id  text PRIMARY KEY CHECK (btrim(spend_authorization_id) <> ''),

    action                  text NOT NULL CHECK (btrim(action) <> ''),
    actor_subject           text NOT NULL CHECK (btrim(actor_subject) <> ''),

    -- WHAT it covers. A ceiling with no subject authorizes everything.
    job_identity            text NOT NULL CHECK (btrim(job_identity) <> ''),
    member_identities       jsonb NOT NULL,

    -- THE EXACT CONTRACT: a different contract is a different price, so a ceiling priced against one
    -- does not cover the other.
    provider_contract_hash  text NOT NULL CHECK (btrim(provider_contract_hash) <> ''),

    -- ▲ BOTH, because either alone is unbounded: calls without tokens permits one enormous call,
    -- tokens without calls permits an unbounded retry loop of small ones.
    max_calls               integer NOT NULL CHECK (max_calls > 0),
    max_tokens              bigint  NOT NULL CHECK (max_tokens > 0),

    currency                text NOT NULL CHECK (btrim(currency) <> ''),
    max_cost                numeric NOT NULL CHECK (max_cost > 0),

    -- ▲ PINNED. A ceiling in currency is meaningless if the price list under it can move.
    pricing_version         text NOT NULL CHECK (btrim(pricing_version) <> ''),

    -- So a redelivered request cannot re-authorize the same spend under a new id.
    idempotency_identity    text NOT NULL CHECK (btrim(idempotency_identity) <> ''),

    expires_at              timestamptz NOT NULL,
    authorized_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT llm_spend_authorization_revision_idempotent UNIQUE (idempotency_identity)
);

CREATE OR REPLACE FUNCTION llm_spend_authorization_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'llm_spend_authorization_revision is append-only: a ceiling that can be raised '
                    'after the fact is not a ceiling. Record a new authorization instead';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS llm_spend_authorization_revision_no_change
    ON llm_spend_authorization_revision;
CREATE TRIGGER llm_spend_authorization_revision_no_change
    BEFORE UPDATE OR DELETE ON llm_spend_authorization_revision
    FOR EACH ROW EXECUTE FUNCTION llm_spend_authorization_revision_write_once();

-- ── reservation: WORST CASE, taken BEFORE the call ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_spend_reservation (
    reservation_id          text PRIMARY KEY CHECK (btrim(reservation_id) <> ''),
    spend_authorization_id  text NOT NULL REFERENCES llm_spend_authorization_revision,

    -- The dispatch this reservation is for, so "what did this job actually spend" is a join rather
    -- than an estimate. Nullable only until the dispatch ref is minted, in the same transaction.
    dispatch_ref            text,

    reserved_calls          integer NOT NULL CHECK (reserved_calls > 0),
    reserved_tokens         bigint  NOT NULL CHECK (reserved_tokens > 0),
    reserved_cost           numeric NOT NULL CHECK (reserved_cost >= 0),

    -- ▲ A RESERVATION IS A LIFECYCLE STATE ONLY A LIVE WORKER CAN LEAVE, so it expires and is swept
    -- — the same rule §15.1 states for every other lifecycle table. Without this, a crash between
    -- reserve and settle leaves worst-case cost reserved for ever and the budget silently shrinks
    -- until the authorization is exhausted by work that never happened.
    expires_at              timestamptz NOT NULL,
    reserved_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_spend_reservation_by_authorization
    ON llm_spend_reservation (spend_authorization_id);

-- ── settlement: ACTUALS, recorded after the provider answers ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_spend_settlement (
    reservation_id          text PRIMARY KEY REFERENCES llm_spend_reservation(reservation_id),
    actual_calls            integer NOT NULL CHECK (actual_calls >= 0),
    actual_tokens           bigint  NOT NULL CHECK (actual_tokens >= 0),
    actual_cost             numeric NOT NULL CHECK (actual_cost >= 0),
    settled_at              timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION llm_spend_ledger_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'the LLM spend ledger is append-only: reservations and settlements are what was '
                    'reserved and what was spent, and neither becomes untrue later';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS llm_spend_settlement_no_change ON llm_spend_settlement;
CREATE TRIGGER llm_spend_settlement_no_change
    BEFORE UPDATE OR DELETE ON llm_spend_settlement
    FOR EACH ROW EXECUTE FUNCTION llm_spend_ledger_write_once();

-- ── 1103's exception now BINDS its money ────────────────────────────────────────────────────────
-- ▲ THE CONTRACT HALF OF 1103's EXPAND. Migrations apply in lexical order, so 1103 could not
-- reference a table that did not exist yet; it created the column nullable and said so. Here it
-- gains its foreign key and its NOT NULL, which is what makes "regeneration is an approved,
-- cost-confirmed act" true rather than intended.
UPDATE formula_draft_regeneration_exception SET llm_spend_authorization_id = NULL WHERE FALSE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'formula_draft_regeneration_exception_binds_its_spend') THEN
        ALTER TABLE formula_draft_regeneration_exception
            ADD CONSTRAINT formula_draft_regeneration_exception_binds_its_spend
            FOREIGN KEY (llm_spend_authorization_id)
            REFERENCES llm_spend_authorization_revision(spend_authorization_id);
    END IF;
END $$;

-- Measured zero on the live cluster (`formula_draft_retirement` is 0, so no exception can exist),
-- and the test database is fresh — so this is immediate, with no nullable phase to carry forward.
ALTER TABLE formula_draft_regeneration_exception
    ALTER COLUMN llm_spend_authorization_id SET NOT NULL;
