-- src/featuregen/db/migrations/1082_generation_authorization.sql
-- S11 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the GENERATION
-- AUTHORIZATION — the thing S9 and S10 both reference by id and nothing anywhere mints.
--
-- THE GAP THIS CLOSES, and it is a real one rather than a tidy-up. `verification_attempt` (1080) is
-- keyed on `generation_authorization_revision_id`; `evaluate_generate` takes one and refuses a
-- blank; the plan's invariant 17 says "a generation is authorized FOR a target". Every one of those
-- treats the id as given, and until this table no row anywhere produced one. A verification could
-- therefore name an authorization that never existed, and nothing could tell.
--
-- INVARIANT 17 IS THE SHAPE OF THE TABLE. "The approved target travels with the selection" — so the
-- target ref and its MODE are columns here, not something a later stage re-derives. A generation
-- authorized for a prediction target and one authorized for an exploration build are different
-- authorizations even over the same group, because the first has a column that must not leak into
-- its own features and the second has none.
--
-- `target_ref` IS NULL EXACTLY WHEN THE MODE IS `exploration`, enforced. An exploration build has no
-- target — that is what the mode means — and a NULL under `prediction` would be a generation
-- authorized to predict something nobody named. The CHECK makes the two fields one statement rather
-- than two that can disagree.
--
-- ENVIRONMENT-SCOPED, per F3. Deployment placement in every key, so an authorization minted for the
-- sandbox cannot be spent in production.
--
-- WHY THE REVISION IS CONTENT-ADDRESSED. Re-authorizing the same generation over the same target in
-- the same environment is ONE authorization, so a double-click does not mint two and a verification
-- naming "the" authorization is unambiguous. Who authorized it and when are PROVENANCE and sit
-- outside the hash, the same split 1074 made for inventory observations.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS generation_authorization (
    revision_id        text PRIMARY KEY CHECK (btrim(revision_id) <> ''),

    environment_id     text NOT NULL CHECK (btrim(environment_id) <> ''),
    logical_group_name text NOT NULL CHECK (btrim(logical_group_name) <> ''),

    -- The build set this generation covers. C-D10's root: generation authorization covers ALL
    -- member selections, so it names the set rather than a feature.
    build_set_revision_id text NOT NULL CHECK (btrim(build_set_revision_id) <> ''),

    -- INVARIANT 17. The approved target travels with the selection.
    target_mode        text NOT NULL CHECK (target_mode IN ('prediction', 'exploration')),
    target_ref         text CHECK (target_ref IS NULL OR btrim(target_ref) <> ''),

    -- Provenance — outside the content hash, so re-authorizing the same thing is one authorization.
    authorized_by      text NOT NULL CHECK (btrim(authorized_by) <> ''),
    authorized_at      text NOT NULL CHECK (btrim(authorized_at) <> ''),
    recorded_at        timestamptz NOT NULL DEFAULT now(),

    -- One statement, not two that can disagree: an exploration build has no target, and a
    -- prediction without one would be authorized to predict something nobody named.
    CONSTRAINT generation_authorization_target_matches_mode
        CHECK ((target_mode = 'exploration') = (target_ref IS NULL))
);

CREATE INDEX IF NOT EXISTS generation_authorization_by_group
    ON generation_authorization (environment_id, logical_group_name, recorded_at);

-- ── append-only ──────────────────────────────────────────────────────────────────────────────────
-- An authorization is what a verification and a publication both stand on. Editing one would move
-- the target a sealed artifact was generated for, after the fact.
CREATE OR REPLACE FUNCTION s11_generation_authorization_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'generation_authorization is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS generation_authorization_no_update ON generation_authorization;
CREATE TRIGGER generation_authorization_no_update
    BEFORE UPDATE OR DELETE ON generation_authorization
    FOR EACH ROW EXECUTE FUNCTION s11_generation_authorization_write_once();
