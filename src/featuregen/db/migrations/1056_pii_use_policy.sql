-- src/featuregen/db/migrations/1056_pii_use_policy.sql
-- The PII allow-policy surface (verified-interfaces doc D14; DEFERRED-WORK A.34).
--
-- WHAT THIS OPENS. `feature_assist._use_gate` refuses a personal-data operand with
-- PERSONAL_DATA_POLICY_REQUIRED — a `needs_setup` refusal that names an artifact which did not
-- exist. Five shipped recipes (screening_exposure, device_sharing_velocity, new_device_flag,
-- geo_velocity_impossible, external_own_transfer_trend) are permanently refused because of it.
-- This is that artifact: a governed declaration that a NAMED pii-classed concept may be used as a
-- model input, for a NAMED purpose, by a NAMED approver.
--
-- 1056 IS THE ONLY NUMBER THIS SLICE ALLOCATES (D7). 1053-1055 belong to a parallel session; this
-- store depends on nothing they create, so the neighbour pins tolerate them either way.
--
-- SINGLE-APPROVER, BY EXPLICIT USER DECISION (D14). Every other governed declaration on this branch
-- is four-eyes. This one is not: ONE platform-admin declares concept + purpose and the policy is
-- ACTIVE immediately, and one action revokes it. The control is the immutable who/when/purpose
-- record, not a second pair of eyes. There is deliberately no `confirmed_by` column, because a
-- nullable one would read as a step somebody forgot rather than a step nobody is asked for.
--
-- IMMUTABLE REVISIONS + CAS POINTER, the 1047/1048/1049 discipline. `pup_` + the RFC-8785 JCS
-- content hash is the deterministic revision id, so a feature's provenance can name a policy
-- revision that can never be mutated underneath it.
--
-- REVOCATION IS A NEW REVISION, NEVER A DELETE OR AN UPDATE. `status` is REVISION CONTENT, so
-- revoking mints a second, content-distinct revision (same concept, same purpose, status
-- 'revoked') and advances the pointer to it. The approval it replaces stays readable forever.
-- Re-approving with the identical purpose therefore resolves back to the FIRST revision id — which
-- is correct and is why WHO made it current lives on the pointer, not on identity.
--
-- WHY `concept_name` CARRIES NO FOREIGN KEY AND NO CHECK. The set of pii-classed concepts lives in
-- the python registry (`concepts.CONCEPT_REGISTRY`, `is_personal_data`), not in a table. A CHECK
-- here could only be a hand-typed copy that drifts, and the copy would then decide what is
-- licensable. `pii_policy.validate_policy_concept` is the single gate, enforced at authoring, and
-- it refuses a protected characteristic with wording no policy can lift.
--
-- WHY THE APPROVER IS NOT IN THE CONTENT HASH. The §6.2 house split: identity is "what was
-- declared, under what class of authority"; `approved_by` / `approved_at` are "who filed it". A
-- second admin declaring identical content reuses the revision, and the pointer records who made
-- it current.

CREATE TABLE IF NOT EXISTS pii_use_policy_revision (
    revision_id   text        PRIMARY KEY
        CHECK (revision_id ~ '^pup_[0-9a-f]{64}$'),
    -- A concept NAME, never a column ref: a policy licenses a MEANING for the whole platform, so
    -- it cannot be worked around by pointing at another column carrying the same concept.
    concept_name  text        NOT NULL,
    -- Bounded free text in v1 (D14). A closed purpose taxonomy is the named later refinement; the
    -- length bound is the part that has to exist now, so a purpose can never become a payload.
    purpose       text        NOT NULL CHECK (char_length(purpose) BETWEEN 8 AND 300),
    status        text        NOT NULL CHECK (status IN ('active', 'revoked')),
    provenance    jsonb       NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(provenance) = 'object'),
    content_hash  text        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- Provenance, OUTSIDE content identity (module header).
    approved_by   text        NOT NULL,
    approved_at   timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pii_use_policy_revision_concept_idx
    ON pii_use_policy_revision (concept_name);

CREATE TABLE IF NOT EXISTS pii_use_policy_current (
    concept_name    text        PRIMARY KEY,
    revision_id     text        NOT NULL REFERENCES pii_use_policy_revision(revision_id),
    pointer_version integer     NOT NULL CHECK (pointer_version >= 1),
    declared_by     text        NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
