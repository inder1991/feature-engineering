-- src/featuregen/db/migrations/1053_materialization_request.sql
-- Phase G §3.2 — a run acquires DURABLE IDENTITY before any work begins.
--
-- THE HOLE THIS CLOSES. A materialization run used to acquire no database identity until someone
-- appended an event, and `fold_run_status` raises on an empty stream (control_plane.py:344). A
-- crash between "we decided to run" and the first append therefore left ZERO trace: nobody could
-- tell a run had ever been requested. This row exists BEFORE any work begins, so that crash leaves
-- a leased, un-advanced request the reconciler can find.
--
-- TWO KINDS OF RECORD, AND WHY THEY MUST NOT BE CONFLATED.
--   * The control plane (1034 + 1044) is IMMUTABLE EVIDENCE — append-only, one terminal event per
--     run, ordering-triggered, UPDATE/DELETE/TRUNCATE all refused. It is the record of what
--     HAPPENED. This migration does not create, alter, drop or re-guard a single object in it: it
--     only takes a foreign key to `materialization_generation`.
--   * `materialization_request` is a MUTABLE COORDINATION RECORD — who asked, for what, under which
--     flag state, is it still being worked, has its lease expired. It is the record of what is
--     BEING ATTEMPTED. It is UPDATEd (accept, lease renewal, terminal link) and therefore carries
--     NO append-only guard: a row that could not be updated could not carry a lease at all.
--
-- So this table makes no append-only claim, holds no fold, and is not evidence. A reader who needs
-- to know what happened reads the plane; this row says only what is being attempted and by whom.
--
-- NUMBERING. 1053 was free across every ref when this landed
-- (`git log --all --diff-filter=A -- 'src/featuregen/db/migrations/1053*'` is empty). 1054 is
-- reserved for the compile-side artifact retention (§3.6) and 1055 for the active-revision pointer
-- (§3.5); neither is taken here.
--
-- Idempotent / re-runnable in the repo style (`apply_migrations` ledgers by filename, and the test
-- suite re-executes this exact SQL against a POPULATED table): CREATE TABLE IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS + CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS/CREATE TRIGGER
-- (1044's style). Nothing here drops, alters or deletes a row, so re-application cannot destroy a
-- record — including on a database whose plane is already populated.

CREATE TABLE IF NOT EXISTS materialization_request (
    request_id            text PRIMARY KEY CHECK (btrim(request_id) <> ''),
    logical_group_name    text NOT NULL CHECK (btrim(logical_group_name) <> ''),
    -- The actor identity. A run spends cluster resources and writes outside the governed catalog;
    -- an unattributed one is not auditable.
    requested_by          text NOT NULL CHECK (btrim(requested_by) <> ''),
    -- The roles SNAPSHOT taken when the request was recorded, not a live lookup: the run must be
    -- judged against the scope its requester actually held, not against whatever they hold by the
    -- time somebody reads the record. An empty array records that no snapshot was taken (nobody
    -- reaches this table without passing the trigger's permission check), and a NULL element is a
    -- role nobody could have held — both are a writer assembled wrongly.
    authorized_roles      text[] NOT NULL
                              CONSTRAINT materialization_request_roles_are_a_snapshot
                              CHECK (cardinality(authorized_roles) > 0
                                     AND array_position(authorized_roles, NULL) IS NULL),
    -- Two identical requests must not become two runs. UNIQUE is what makes that a database fact
    -- rather than a convention in the writer.
    idempotency_key       text NOT NULL UNIQUE CHECK (btrim(idempotency_key) <> ''),
    -- The flag/interlock state observed at accept time. Opaque in CONTENT — interpreting it here
    -- would make the table a policy — but pinned in SHAPE: a bare scalar is a caller who passed a
    -- flag VALUE where the flag STATE belongs.
    activation_state      jsonb NOT NULL CHECK (jsonb_typeof(activation_state) = 'object'),
    lifecycle_state       text NOT NULL
                              CONSTRAINT materialization_request_lifecycle_is_closed
                              CHECK (lifecycle_state IN (
                                  'requested', 'accepted', 'running', 'committed', 'failed')),
    -- NULL until compilation mints one. The FK is real: a request naming a generation that does not
    -- exist would attribute the run to nothing.
    generation_id         text NULL REFERENCES materialization_generation(generation_id),
    -- NULL until a run is prepared, and DELIBERATELY NOT A FOREIGN KEY: run identity lives in the
    -- append-only event stream, and this row must be recordable BEFORE any event exists. An FK here
    -- would make the anchor depend on the very thing it anchors.
    run_id                text NULL CHECK (run_id IS NULL OR btrim(run_id) <> ''),
    resolved_input_digest text NULL
                              CHECK (resolved_input_digest IS NULL
                                     OR btrim(resolved_input_digest) <> ''),
    requested_at          timestamptz NOT NULL DEFAULT now(),
    accepted_at           timestamptz NULL,
    -- The lease is what makes "is anyone still working this?" answerable. It is granted BY
    -- acceptance: a leased row that was never accepted would let the reconciler adopt a request
    -- nobody claimed.
    lease_expires_at      timestamptz NULL,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT materialization_request_lease_follows_acceptance
        CHECK (lease_expires_at IS NULL OR accepted_at IS NOT NULL)
);

-- The reconciler's ONLY real query: an expired lease on a request that has not reached a terminal
-- lifecycle state. PARTIAL on purpose — committed and failed requests accumulate forever and none
-- of them is ever a candidate. `request_id` rides along as the query's tie-break, so the scan is
-- returned already ordered. The predicate cannot mention now(): it is not IMMUTABLE, and a request
-- in 'requested' carries no lease, so the range condition excludes it without help.
CREATE INDEX IF NOT EXISTS materialization_request_expired_lease_idx
    ON materialization_request (lease_expires_at, request_id)
    WHERE lifecycle_state NOT IN ('committed', 'failed');

-- Read-side index for the two operator questions: "what is this group doing?" and "what is still
-- in flight?".
CREATE INDEX IF NOT EXISTS materialization_request_group_idx
    ON materialization_request (logical_group_name, requested_at DESC);

-- `updated_at` answers "was anything done to this row, and when?", so it must not depend on each
-- writer remembering to set it — a store method that forgot would make the lease a lie without
-- failing anything. Stamped by the database instead. now() is the transaction's instant, matching
-- the DEFAULT above and the lease arithmetic (`now() + make_interval(...)`) the store issues, so a
-- request's timestamps are all read from the same clock.
CREATE OR REPLACE FUNCTION materialization_request_touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS materialization_request_touch_updated_at ON materialization_request;
CREATE TRIGGER materialization_request_touch_updated_at
    BEFORE UPDATE ON materialization_request
    FOR EACH ROW EXECUTE FUNCTION materialization_request_touch_updated_at();
