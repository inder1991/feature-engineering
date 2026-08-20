-- src/featuregen/db/migrations/1094_verification_request.sql
-- Step 13 — the ON-DEMAND verification lifecycle. What `verification_attempt` (1080) has no room for.
--
-- THE GAP. 1080 records what an attempt DID: its execution hash, its staging path, the sealed
-- artifact it verified. It has no status, so there is no state in which a verification has been
-- ASKED FOR and not yet run, and no way to tell "still running" from "the worker died" from
-- "nothing consumes this table". 1092's `generation_request` says so in its own comment; this is
-- the same lifecycle for the same reason, over verification instead of generation.
--
-- The two are deliberately separate tables rather than one with a `kind` column. They carry
-- different evidence — a generation produces an artifact, a verification produces a verdict ABOUT
-- one — and a shared table would need every constraint below to be conditional on the kind, which
-- is how a constraint stops being enforced.
--
-- ON DEMAND, and that is a design statement rather than a scheduling one. Verification costs a
-- cluster run. It happens because somebody asked, and asking is recorded here with who asked and
-- when, so an operator reading a green feature can see which request produced its evidence.
--
-- VERIFICATION DOES NOT IMPLY PERMISSION. There is no publication column here, exactly as 1080 has
-- none: a column in this table would eventually be read as consent, and verification is defined to
-- run without it. Step 14 asks THIS table whether a passing verification exists; it does not ask
-- this table whether to publish.
--
-- APPLIED to live 2026-08-20 (189 -> 191), after a scratch-restore dry run caught the orphan
-- authorization documented above. Backup: ~/featuregen-backups/featuregen-pre-1094-1095-*.dump

CREATE TABLE IF NOT EXISTS verification_request (
    request_id           text PRIMARY KEY CHECK (btrim(request_id) <> ''),

    -- WHAT is being verified: one sealed artifact, named exactly. A verification not tied to a
    -- specific artifact verifies whatever happened to be rendered — 1080's own words, and the
    -- reason the request carries it too rather than only the attempt.
    sealed_artifact_id   text NOT NULL CHECK (btrim(sealed_artifact_id) <> ''),
    environment_id       text NOT NULL CHECK (btrim(environment_id) <> ''),

    status               text NOT NULL CHECK (status IN (
                             'REQUESTED', 'CLAIMED', 'RUNNING',
                             'PASSED', 'FAILED', 'REFUSED', 'CANCELLED')),

    -- The attempt this request produced, once it has produced one. NULL until then: an honest
    -- absence rather than a placeholder that reads as an attempt with no findings.
    execution_hash       text,

    -- WHY it refused, per check. REFUSED is a PRODUCT result — the artifact does not verify, and
    -- this names which check and why. FAILED is an outage and carries a reason instead. Keeping
    -- them apart is the whole point: one is the feature's problem and one is the platform's.
    findings             jsonb NOT NULL DEFAULT '[]'::jsonb,
    failure_reason       text,

    requested_by         text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at         text NOT NULL CHECK (btrim(requested_at) <> ''),
    updated_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT verification_request_refused_names_findings
        CHECK (status <> 'REFUSED' OR jsonb_array_length(findings) > 0),
    CONSTRAINT verification_request_failure_reason_belongs_to_failed
        CHECK ((failure_reason IS NULL) = (status <> 'FAILED')),
    -- A PASSED request must name the attempt that passed. The attempt IS the evidence; passing
    -- without one is a green status with nothing behind it, which is the state step 14 must never
    -- be able to read as permission.
    CONSTRAINT verification_request_pass_carries_an_attempt
        CHECK (status <> 'PASSED' OR execution_hash IS NOT NULL)
);

-- ONE LIVE REQUEST PER ARTIFACT PER ENVIRONMENT. Two workers verifying one artifact at once would
-- write two attempts against the same authorization, and 1080's UNIQUE(authorization, attempt)
-- would fail the second one deep inside a cluster run rather than at the door. Partial, so the
-- history of finished requests is unbounded — an artifact verified, changed and re-verified keeps
-- every verdict it ever earned.
CREATE UNIQUE INDEX IF NOT EXISTS verification_request_one_live
    ON verification_request (sealed_artifact_id, environment_id)
    WHERE status IN ('REQUESTED', 'CLAIMED', 'RUNNING');

CREATE INDEX IF NOT EXISTS verification_request_by_artifact
    ON verification_request (sealed_artifact_id, environment_id, updated_at DESC);
