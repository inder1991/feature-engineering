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
--
-- ...AND WHY THAT SPLIT NEEDS A SECOND HASH (review F3). Keeping the approver OUT of identity is
-- correct — it is what makes approve -> revoke -> re-approve resolve back to the ORIGINAL revision
-- id — but it left the ONE field the single-approver deviation rests on with nothing protecting
-- it: a plain UPDATE could rewrite `approved_by` and the content hash would still verify, so the
-- record that IS the control could be forged in place. `attestation_hash` closes that WITHOUT
-- touching content identity: it covers {revision_id, approved_by, approved_at} only, is written at
-- insert, and is re-derived on EVERY read path. The pointer row gets the same treatment over
-- {concept_name, revision_id, declared_by, pointer_version} because "who made it current" is the
-- other half of the same record. Identity is unchanged; only tamper-evidence is added.
--
-- A POINTER CANNOT LICENSE ACROSS CONCEPTS (review F1). The pointer's FK is COMPOSITE
-- (concept_name, revision_id), not revision_id alone. With the single-column FK a pointer for
-- concept A could legally name concept B's revision, and the gate's bulk reader — which joined on
-- revision_id and keyed off the REVISION's concept — would then license B while A's own policy was
-- revoked. The database is the layer that has to refuse that shape, because the store guard and
-- the reader guard are both code somebody can later "simplify"; a composite FK is not.

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
    -- Provenance, OUTSIDE content identity (module header) — and TAMPER-EVIDENT because of it.
    approved_by   text        NOT NULL,
    approved_at   timestamptz NOT NULL DEFAULT now(),
    -- materialize_hash({revision_id, approved_by, approved_at}) — NOT NULL because a nullable one
    -- would have to be read as "unverifiable", and an unverifiable approver record is exactly the
    -- state this column exists to make impossible.
    attestation_hash text     NOT NULL CHECK (attestation_hash ~ '^[0-9a-f]{64}$'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- The composite the pointer's FK targets. Redundant with the primary key for uniqueness (the
    -- revision id already implies its concept); it exists ONLY so `(concept_name, revision_id)` is
    -- a referenceable key, which is what lets the pointer be constrained to its OWN concept.
    CONSTRAINT pii_use_policy_revision_concept_revision_unique
        UNIQUE (concept_name, revision_id)
);
CREATE INDEX IF NOT EXISTS pii_use_policy_revision_concept_idx
    ON pii_use_policy_revision (concept_name);

CREATE TABLE IF NOT EXISTS pii_use_policy_current (
    concept_name    text        PRIMARY KEY,
    revision_id     text        NOT NULL,
    pointer_version integer     NOT NULL CHECK (pointer_version >= 1),
    declared_by     text        NOT NULL,
    -- materialize_hash({concept_name, revision_id, declared_by, pointer_version}) — the same
    -- tamper-evidence over WHO made this revision current. `updated_at` is deliberately NOT covered:
    -- it is a clock reading, not a decision, and folding it in would make the attestation unstable
    -- across a timestamp round-trip for no gain in what it proves.
    attestation_hash text      NOT NULL CHECK (attestation_hash ~ '^[0-9a-f]{64}$'),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- COMPOSITE, not `REFERENCES pii_use_policy_revision(revision_id)` (header): the pointer may
    -- only name a revision OF ITS OWN CONCEPT.
    CONSTRAINT pii_use_policy_current_concept_revision_fk
        FOREIGN KEY (concept_name, revision_id)
        REFERENCES pii_use_policy_revision (concept_name, revision_id)
);

-- ── the contract-side half of the same surface (review F2) ──────────────────────────────────────
-- The gate resolves WHICH policy revisions licensed a candidate's personal-data operands, but until
-- now that answer died on the in-memory FeatureIdea: the governed artifact recorded no trace of what
-- allowed it. This column carries it, written at CONFIRM from the confirm-time gate re-consult (not
-- from Gate #1), so a contract records the revisions that covered it AT the governing write. Rides
-- on `contract` beside the other at-confirm provenance columns (1011) rather than in a table of its
-- own: it is a property of one immutable contract version, never a relationship with a life of its
-- own. `[]` means "this feature bound no personal data", which is the overwhelming majority and is
-- NOT the same as "we did not check" — a candidate that needed a policy and lacked one was refused
-- and never reached a contract.
--
-- This ALTER is why 1056 is still the only number this stream allocates (D7): the column belongs to
-- the D14 surface, and splitting it into a 1057 would allocate a second number for one half of one
-- decision.
ALTER TABLE contract ADD COLUMN IF NOT EXISTS
    personal_data_policy_revision_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE contract DROP CONSTRAINT IF EXISTS contract_pii_policy_ids_ck;
ALTER TABLE contract ADD CONSTRAINT contract_pii_policy_ids_ck
    CHECK (jsonb_typeof(personal_data_policy_revision_ids) = 'array');
