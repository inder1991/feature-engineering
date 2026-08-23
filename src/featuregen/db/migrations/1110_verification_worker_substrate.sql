-- src/featuregen/db/migrations/1110_verification_worker_substrate.sql
-- THE LEASE, THE FENCE, THE ATTEMPT COUNT AND THE OUTPUT IDENTITY — what makes 1094's lifecycle
-- DRIVABLE by a durable worker instead of a promise in a docstring.
--
-- THE GAP THIS CLOSES (§9.0, and it was the most serious finding of the architect review). The
-- verification route said "a worker executes it" and no worker existed: the durable worker's
-- handler set held exactly one handler, and it was compile. Worse, 1094's lifecycle had NO lease,
-- fence or attempts column — so even with a worker, a crash between CLAIMED and terminal would
-- have wedged the artifact's verification PERMANENTLY under `verification_request_one_live`
-- (§9.0.1's wedge shape: a state only a live worker can leave, plus a uniqueness guard on the
-- live states). The columns below are 1092's own discipline, applied to the table whose absence of
-- them 1092's comment already pointed at.
--
-- ▲ EXPAND-ONLY. Nullable columns on a table the running image writes (the route INSERTs
-- verification_request? — it does NOT yet; the route writes only verification_attempt, which is
-- the defect. Still: nullable, because the 1095 lesson is cheap to honour and expensive to skip).

ALTER TABLE verification_request
    ADD COLUMN IF NOT EXISTS lease_owner       text,
    ADD COLUMN IF NOT EXISTS lease_expires_at  timestamptz,
    ADD COLUMN IF NOT EXISTS lease_fence       bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS attempts          integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS action_decision_revision_id text
        REFERENCES action_decision_revision(decision_id);

-- ── the SANDBOX OUTPUT REVISION: what a verification actually produced, as an identity ──────────
-- Publication binds to a THING, not to a name: "the rows this run wrote, content-addressed", so
-- PUBLISH_SANDBOX can require the exact output a passing verification measured — and a re-run that
-- produced different rows is visibly a DIFFERENT output, not a silent replacement.
CREATE TABLE IF NOT EXISTS sandbox_output_revision (
    output_revision_id   text PRIMARY KEY CHECK (btrim(output_revision_id) <> ''),
    request_id           text NOT NULL REFERENCES verification_request(request_id),
    sealed_artifact_id   text NOT NULL CHECK (btrim(sealed_artifact_id) <> ''),
    environment_id       text NOT NULL CHECK (btrim(environment_id) <> ''),
    -- The measured shape, content-addressed. Row hashes stay OUT of this table (they are data,
    -- and this is governance metadata); the manifest hash covers them at rest.
    output_manifest_hash text NOT NULL CHECK (btrim(output_manifest_hash) <> ''),
    row_count            bigint NOT NULL CHECK (row_count >= 0),
    produced_at          timestamptz NOT NULL DEFAULT now(),

    -- One output per request: a request that measured twice is two requests.
    CONSTRAINT sandbox_output_revision_one_per_request UNIQUE (request_id)
);

CREATE OR REPLACE FUNCTION sandbox_output_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'sandbox_output_revision is append-only: it is what a verification MEASURED, '
                    'and a measurement that can be rewritten is not evidence';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sandbox_output_revision_no_change ON sandbox_output_revision;
CREATE TRIGGER sandbox_output_revision_no_change
    BEFORE UPDATE OR DELETE ON sandbox_output_revision
    FOR EACH ROW EXECUTE FUNCTION sandbox_output_revision_write_once();
