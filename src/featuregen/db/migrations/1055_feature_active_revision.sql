-- src/featuregen/db/migrations/1055_feature_active_revision.sql
-- Phase G §3.5 / G-3 — WHICH GENERATION A READER IS CURRENTLY SEEING.
--
-- THE HOLE THIS CLOSES. `group_binding.physical_target` has named `sandbox_feature.<group>` since
-- G-1 and nothing has ever made that name resolve to anything: the chain terminated
-- PUBLICATION_REFUSED, and DEFERRED-WORK A.39 records the consequence in plain words — "every run
-- this programme can currently produce ends in a refusal … what a consumer holds afterwards is a
-- sealed, build-verified Kedro project on disk plus a plane record of what it would read, and no
-- name by which to read a result." G-3's publish step performs the swap. This table is what makes
-- the swap a RECORD rather than a side effect nobody can audit.
--
-- WHAT A ROW MEANS, EXACTLY. "At this instant, the reader-visible state of `physical_target` became
-- this generation's output, on the strength of this capability attestation." It is not a statement
-- that the swap will still hold tomorrow — the newest row for a group is what holds now, and the
-- older rows are the history of what held before. There is deliberately no `is_current` boolean and
-- no `DELETE` of superseded rows: a mutable current-pointer column is a record that can be
-- rewritten, and 1034's rule ("a record that can be rewritten proves nothing") applies with more
-- force here than anywhere else in the plane, because this is the only record that says a feature
-- was ever readable.
--
-- ORDERED, AND THE ORDER IS ENFORCED — 1044's argument, one table over. `seq` is per logical group
-- and strictly increasing, and the BEFORE-INSERT trigger below refuses anything that does not
-- extend the group. Without it, two concurrent publishers could each insert a row and neither the
-- database nor a reader could say which pointer the cluster actually holds — and because the table
-- is append-only, that ambiguity would be permanent. Races are closed by the
-- (logical_group_name, seq) UNIQUE plus the trigger's max-seq check running BEFORE INSERT, exactly
-- as `materialization_run_event` closes them.
--
-- KEYED BY REVISION, SCOPED BY GROUP. `revision_id` is the primary key so a re-entered publish
-- collides loudly rather than appending a second pointer for one swap (the derived-id discipline
-- `chain._derived_id` already applies to generations, runs and reports). `generation_id` is a real
-- FK: a pointer naming a generation the plane never recorded would claim a publication of bytes
-- nobody compiled. `run_id` is carried but NOT constrained — `materialization_run_event` is keyed
-- (run_id, seq) and has no run table to reference, and inventing one here would put a second
-- definition of a run in the schema.
--
-- WHY THE ATTESTATION IS ON THE ROW. §10.3's whole design is that publication capability exists
-- only as ingested probe evidence, and `PublisherSelection` carries the attestation the selection
-- rests on. Recording it HERE is what lets an auditor answer "on what evidence was this swap
-- performed?" from the pointer itself, without reconstructing which attestation was newest at the
-- time — a reconstruction that would move as later probes are ingested.
--
-- NO FEATURE DATA, EVER (§14). `published_object` is a NAME and `row_count` is deliberately absent:
-- a count is a reading of the published table, and the control plane does not read feature data.
-- What was staged is §9's business, and the gates that check it run inside the submitted pipeline.
--
-- NUMBERING. 1055 has been RESERVED for this table since Phase G's T1 (1053's header: "1055 is
-- reserved for G-3's active-revision pointer (§3.5); neither is taken here"; 1054's header and
-- DEFERRED-WORK A.39 say the same). Verified free before writing:
-- `git log --all --diff-filter=A -- 'src/featuregen/db/migrations/1055*'` is empty, and no file
-- named 1055* exists on any ref. 1056 through 1068 are taken by later work, which is why this
-- number could not simply be re-allocated.
--
-- Idempotent / re-runnable in the repo style (`apply_migrations` ledgers by filename, and the test
-- suite re-executes this exact SQL against a POPULATED table): CREATE TABLE IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS + CREATE OR REPLACE FUNCTION + CREATE OR REPLACE TRIGGER + a
-- role-guarded REVOKE. Nothing here drops, alters or deletes, so re-application cannot destroy a
-- record — including on a database whose plane is already populated.

CREATE TABLE IF NOT EXISTS feature_active_revision (
    revision_id            text PRIMARY KEY CHECK (btrim(revision_id) <> ''),
    -- §10.1's publication unit. Publication is atomic per GROUP, so the pointer is per group and
    -- never per feature: a feature-scoped pointer would let two columns of one table be visible at
    -- two different generations, which is the exact non-atomicity §10.3's probe exists to rule out.
    logical_group_name     text NOT NULL CHECK (btrim(logical_group_name) <> ''),
    generation_id          text NOT NULL
                               REFERENCES materialization_generation(generation_id),
    -- The run that performed the swap. Carried, not constrained — see the header.
    run_id                 text NOT NULL CHECK (btrim(run_id) <> ''),
    -- The name a reader queries. `group_binding.physical_target`'s value, copied at swap time
    -- rather than joined, because this row is a claim about what WAS made visible and a later
    -- rebinding must not silently restate history.
    published_object       text NOT NULL CHECK (btrim(published_object) <> ''),
    -- §10.3's evidence. The mechanism is carried beside it because an attestation is scoped to one,
    -- and a pointer that named neither could not be audited without guessing.
    capability_attestation_id text NOT NULL CHECK (btrim(capability_attestation_id) <> ''),
    publication_mechanism  text NOT NULL
                               CONSTRAINT feature_active_revision_mechanism_is_closed
                               CHECK (publication_mechanism IN (
                                   'VERSIONED_POINTER', 'EXCHANGE_PARTITION', 'SET_LOCATION')),
    -- Per-GROUP monotonic. The newest row for a group is the revision a reader is seeing; there is
    -- no `is_current` column, because a mutable pointer is a record that can be rewritten.
    seq                    bigint NOT NULL CHECK (seq >= 0),
    -- The caller's instant, matching `materialization_run_event.occurred_at`: the plane mints no
    -- timestamps, so the swap's own clock is the one recorded.
    activated_at           text NOT NULL CHECK (btrim(activated_at) <> ''),
    -- The DATABASE's stamp, for the same reason `publication_capability_attestation` carries one:
    -- when this plane learned of the swap, which is a different question from when it happened.
    recorded_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT feature_active_revision_ordered UNIQUE (logical_group_name, seq)
);

-- The one question a reader asks: which revision is a group on NOW. The index is on
-- (logical_group_name, seq DESC) so the answer is the first row of a range scan rather than a sort
-- over a group's whole history — which grows with every publication and never shrinks.
CREATE INDEX IF NOT EXISTS feature_active_revision_current
    ON feature_active_revision (logical_group_name, seq DESC);

-- ── the ordering guard ───────────────────────────────────────────────────────────────────────────
-- 1044's rule, applied per GROUP rather than per run. A pointer that did not extend its group is
-- either a stale publisher writing after a newer one, or a second writer for one swap; both leave
-- the table unable to say what a reader sees, and the append-only guards below make that state
-- unrepairable — so the database must refuse the write, not merely the read.
CREATE OR REPLACE FUNCTION feature_active_revision_ordered()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM feature_active_revision
        WHERE logical_group_name = NEW.logical_group_name AND seq >= NEW.seq
    ) THEN
        RAISE EXCEPTION 'feature_active_revision: seq % does not extend group %',
            NEW.seq, NEW.logical_group_name;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS feature_active_revision_ordered ON feature_active_revision;
CREATE TRIGGER feature_active_revision_ordered
    BEFORE INSERT ON feature_active_revision
    FOR EACH ROW EXECUTE FUNCTION feature_active_revision_ordered();

-- ── the append-only guards ───────────────────────────────────────────────────────────────────────
-- 1034's function, NOT a copy of it: one rule, one message, one place to change. 1055 sorts after
-- 1034 in the lexical apply order, so the function exists by the time this runs.
CREATE OR REPLACE TRIGGER feature_active_revision_no_mutation
    BEFORE UPDATE OR DELETE ON feature_active_revision
    FOR EACH ROW EXECUTE FUNCTION materialization_control_plane_append_only();

-- A FOR EACH ROW trigger does not fire on TRUNCATE; this is the only guard that does. Nothing
-- references this table, so a bare TRUNCATE genuinely reaches it rather than being short-circuited
-- by an FK error.
CREATE OR REPLACE TRIGGER feature_active_revision_no_truncate
    BEFORE TRUNCATE ON feature_active_revision
    FOR EACH STATEMENT EXECUTE FUNCTION materialization_control_plane_append_only();

-- Defence in depth for the non-superuser production role, guarded by a role-exists check so it is a
-- clean no-op in the superuser test cluster where the role is absent (1034's pattern).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON feature_active_revision FROM featuregen_app;
    END IF;
END $$;
