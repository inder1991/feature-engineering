-- src/featuregen/db/migrations/1100_action_authorization_revision.sql
-- ONE AUTHORIZATION MODEL: an act, a resource, an actor, and the policy that said yes.
--
-- WHY THIS EXISTS. `generation_authorization` (1041/1095) authorizes exactly one act — generating a
-- build set — and the platform now has SIX (parent plan §3). Five of them have no authorization
-- record at all, so five acts are governed by whichever permission happens to gate their route.
-- A permission answers "may this person call this endpoint"; an authorization answers "was THIS act
-- on THIS resource decided, by whom, under which policy" — and only the second is re-checkable by a
-- worker minutes later.
--
-- ▲ ADDITIVE ONLY. This migration DROPS NOTHING. `generation_authorization` and 1095's referential
-- chain are untouched, so the running image keeps working and a rollback to the previous image is
-- safe — which is exactly the expand/contract property §20.1 says these migrations otherwise lack.
-- The contract half (re-point 1095, drop the old table) is a LATER migration, once every one of the
-- six acts has a caller here. Doing both halves at once is what makes a rollback mean "restore the
-- database too".
--
-- ▲ THE DEVELOPMENT POLICY IS RECORDED, NOT ASSUMED (owner ruling, 2026-08-22). While the tool is
-- under development any authenticated user may trigger any implemented non-production stage, and
-- the server records who. That is deliberately permissive AND deliberately explicit: every row
-- stamps `policy_version = 'development-v1'`, so the day production governance lands, "which
-- authorizations were issued under the permissive rules" is ONE QUERY rather than an archaeology
-- exercise, and none of them can be mistaken for a governed approval.
--
-- ▲ NO APPROVER, GRANTEE, DELEGATION OR ENTITLEMENT COLUMNS. Segregation of duties is premature
-- complexity for a tool still being built; it becomes a release-readiness requirement (§21.0). What
-- is NOT deferred is that permission is SERVER-OWNED: `actor_subject` is derived from the
-- authenticated identity and never from a request body, which is the half of this that survives
-- into production unchanged.

CREATE TABLE IF NOT EXISTS action_authorization_revision (
    -- Content-addressed over the decision below, so the same act decided twice under the same
    -- policy is ONE row rather than two that could disagree.
    authorization_id       text PRIMARY KEY CHECK (btrim(authorization_id) <> ''),

    -- WHICH of the six acts. An authorization authorizes ONE; a generic one authorizes the
    -- strongest, which is how a preview approval comes to permit a production write.
    action                 text NOT NULL CHECK (action IN (
                               'AUTHOR_FORMULA', 'GENERATE_PREVIEW',
                               'EXECUTE_SANDBOX', 'PUBLISH_SANDBOX',
                               'MATERIALIZE_PRODUCTION', 'PUBLISH_PRODUCTION')),

    -- WHAT it acts on, hashed. ▲ A hash cannot carry a foreign key, so where an act names a typed
    -- resource the TYPED CHILD TABLE holds the real reference and this is derived from it. Storing
    -- an opaque payload here and calling it a binding would be a promise, not a constraint.
    resource_identity_hash text NOT NULL CHECK (btrim(resource_identity_hash) <> ''),

    -- WHO. Derived from the authenticated identity, NEVER from a request body — that is the whole
    -- point of 0.1's deletion of client-supplied roles, and the reason this column exists at all.
    actor_subject          text NOT NULL CHECK (btrim(actor_subject) <> ''),

    -- WHERE. Part of the grant rather than ambient context: an approval for one environment must
    -- not read as an approval for another.
    environment_id         text NOT NULL CHECK (btrim(environment_id) <> ''),

    -- WHICH RULES said yes. See the header: this is what makes the deferral of real governance
    -- auditable instead of invisible.
    policy_version         text NOT NULL CHECK (btrim(policy_version) <> ''),

    -- The governed decision, recorded as a value rather than re-asked later under different rules.
    permission_result      text NOT NULL CHECK (permission_result IN ('allowed', 'refused')),

    -- So the answer is RE-DERIVABLE rather than asserted. A worker compares against this; it does
    -- not recompute a fresh verdict and proceed on whatever it gets (§7.1's drift rule).
    evidence_hash          text NOT NULL CHECK (btrim(evidence_hash) <> ''),

    -- ▲ PROVENANCE, and therefore OUTSIDE the identity — 1099's rule. Two authorizations differing
    -- only in when they were recorded are the same authorization.
    decided_at             timestamptz NOT NULL DEFAULT now()
);

-- ▲ THE SHAPE COMPOSITE FOREIGN KEYS NEED. A durable job referencing only `authorization_id` can
-- name an authorization issued for a DIFFERENT action or a DIFFERENT resource, and every key would
-- still pass — 1095 solved precisely this for generation by keying on the relationship rather than
-- the id, and the replacement must be at least as strong as the thing it replaces. A superset of
-- the primary key, so it is satisfied by construction and costs only the index.
CREATE UNIQUE INDEX IF NOT EXISTS action_authorization_revision_act_key
    ON action_authorization_revision (action, resource_identity_hash, authorization_id);

CREATE INDEX IF NOT EXISTS action_authorization_revision_by_actor
    ON action_authorization_revision (actor_subject, decided_at DESC);

-- ▲ APPEND-ONLY, under the same guard shape as 1096/1097/1099. An authorization that can be edited
-- after the fact is a record of what somebody currently wishes had been approved.
CREATE OR REPLACE FUNCTION action_authorization_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'action_authorization_revision is append-only: an authorization records a '
                    'decision that was taken, and a decision that can be rewritten was not one';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS action_authorization_revision_no_change ON action_authorization_revision;
CREATE TRIGGER action_authorization_revision_no_change
    BEFORE UPDATE OR DELETE ON action_authorization_revision
    FOR EACH ROW EXECUTE FUNCTION action_authorization_revision_write_once();
