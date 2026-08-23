-- src/featuregen/db/migrations/1114_production_publication_and_certificates.sql
-- §9.1 publication (written FIRST in the spec — the act whose partial failure is visible to the
-- bank) + §10.3's certificate parent that did not exist. Same posture as 1113: engineering
-- behind an unavailable action.
--
-- ▲ Publication has NO UNKNOWN_OUTCOME state, and that is a DESIGN FACT, not an omission: the
-- active pointer is a database row (`production_active_revision`), so the swap and the attempt's
-- terminal state commit in ONE transaction — the window that produces unknown outcomes was
-- removed, not survived. A future change that moves the swap outside the database re-opens
-- §9.1's full list (the spec says so, in those words).

CREATE TABLE IF NOT EXISTS production_publication_attempt (
    attempt_id                     text PRIMARY KEY CHECK (btrim(attempt_id) <> ''),
    -- ▲ THE COMPOSITE FK — §9.1's forgery rule made schema: publication names the
    -- MATERIALIZATION ATTEMPT and the exact output THAT attempt produced. A publication row
    -- naming an output some other act produced is unrepresentable.
    materialization_attempt_id     text NOT NULL
        REFERENCES production_materialization_attempt(attempt_id),
    output_revision_id             text NOT NULL,
    environment_id                 text NOT NULL CHECK (btrim(environment_id) <> ''),
    logical_group_name             text NOT NULL CHECK (btrim(logical_group_name) <> ''),
    status                         text NOT NULL DEFAULT 'REQUESTED'
        CONSTRAINT production_publication_status_v1 CHECK (status IN (
            'REQUESTED', 'CLAIMED', 'PUBLISHED', 'REFUSED', 'FAILED', 'CANCELLED')),
    action_decision_revision_id    text NOT NULL REFERENCES action_decision_revision(decision_id),
    requested_by                   text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at                   timestamptz NOT NULL,
    terminal_detail_json           jsonb,
    lease_owner                    text,
    lease_expires_at               timestamptz,
    lease_fence                    bigint NOT NULL DEFAULT 0,
    attempts                       integer NOT NULL DEFAULT 0,

    CONSTRAINT production_publication_publishes_that_output
        FOREIGN KEY (materialization_attempt_id, output_revision_id)
        REFERENCES materialized_output_revision (attempt_id, output_revision_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS production_publication_one_live
    ON production_publication_attempt (environment_id, logical_group_name)
    WHERE status IN ('REQUESTED', 'CLAIMED');

-- ── the ACTIVE POINTER: what is actually out there right now, answerable by one read ────────────
CREATE TABLE IF NOT EXISTS production_active_revision (
    environment_id         text NOT NULL CHECK (btrim(environment_id) <> ''),
    logical_group_name     text NOT NULL CHECK (btrim(logical_group_name) <> ''),
    output_revision_id     text NOT NULL REFERENCES materialized_output_revision(output_revision_id),
    publication_attempt_id text NOT NULL REFERENCES production_publication_attempt(attempt_id),
    -- The CAS's fence: the swap is `… WHERE fence < EXCLUDED.fence`, so a zombie's stale claim
    -- loses INSIDE the statement, not by convention.
    fence                  bigint NOT NULL,
    published_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (environment_id, logical_group_name)
);

-- ── §10.3: the certificate PARENT that did not exist ────────────────────────────────────────────
-- Issued by step 9's evaluation programme; until then EMPTY, the reader answers None, and the
-- gate surfaces METHOD_CERTIFICATE_MISSING — the honest hard-block §9 demands from day one
-- (absence must never act as permission).
CREATE TABLE IF NOT EXISTS method_certificate_revision (
    certificate_revision_id text PRIMARY KEY CHECK (btrim(certificate_revision_id) <> ''),
    certificate_kind        text NOT NULL
        CONSTRAINT method_certificate_kind_v1 CHECK (certificate_kind IN (
            'AUTHORING_METHOD', 'EXECUTION_STACK')),
    subject_identity_kind   text NOT NULL CHECK (subject_identity_kind IN (
            'AUTHORING_METHOD', 'EXECUTION_STACK')),
    subject_identity_hash   text NOT NULL CHECK (btrim(subject_identity_hash) <> ''),
    contract_hash           text NOT NULL CHECK (btrim(contract_hash) <> ''),
    corpus_hash             text NOT NULL CHECK (btrim(corpus_hash) <> ''),
    outcome                 text NOT NULL CHECK (outcome IN ('CERTIFIED', 'FAILED')),
    evidence_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    issued_at               timestamptz NOT NULL DEFAULT now(),

    -- C5: the kind and its subject must AGREE, or an authoring certificate can carry an
    -- execution-stack subject and the typing buys nothing.
    CONSTRAINT method_certificate_kind_agrees CHECK (certificate_kind = subject_identity_kind)
);

CREATE INDEX IF NOT EXISTS method_certificate_by_subject
    ON method_certificate_revision (certificate_kind, subject_identity_hash, issued_at);

CREATE OR REPLACE FUNCTION method_certificate_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'method_certificate_revision is append-only: a certificate that can be edited '
                    'after issuance certifies nothing';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS method_certificate_revision_no_change ON method_certificate_revision;
CREATE TRIGGER method_certificate_revision_no_change
    BEFORE UPDATE OR DELETE ON method_certificate_revision
    FOR EACH ROW EXECUTE FUNCTION method_certificate_revision_write_once();

-- The parent key 1114's AUTHORING_METHOD binding FKs against (1102's PK is only
-- (artifact_id, member_name); the binding must agree on the HASH too, or a re-derivation could
-- bind to the row while disagreeing with it).
CREATE UNIQUE INDEX IF NOT EXISTS sealed_artifact_member_method_identity_hash_key
    ON sealed_artifact_member_method_identity (artifact_id, member_name, method_identity_hash);

-- ── the PER-MEMBER bindings, recorded on the MATERIALIZATION attempt ────────────────────────────
-- §10.3 consequence 2: a derived verdict can change between the two acts, so the attempt STORES
-- the certificate revision and subject, and publication COMPARES — it never re-derives and
-- proceeds on the new answer (§7.1's drift rule, one level down).
CREATE TABLE IF NOT EXISTS production_attempt_member_certificate (
    attempt_id              text NOT NULL
        REFERENCES production_materialization_attempt(attempt_id),
    member_name             text NOT NULL CHECK (btrim(member_name) <> ''),
    certificate_kind        text NOT NULL CHECK (certificate_kind IN (
            'AUTHORING_METHOD', 'EXECUTION_STACK')),
    certificate_revision_id text NOT NULL
        REFERENCES method_certificate_revision(certificate_revision_id),
    subject_identity_kind   text NOT NULL,
    subject_identity_hash   text NOT NULL CHECK (btrim(subject_identity_hash) <> ''),
    -- ▲ Where the subject has a parent row, FK TO IT: a hash with a parent is a binding, without
    -- one an assertion. AUTHORING_METHOD rows name the sealed member identity; MATCH SIMPLE
    -- leaves the FK unenforced for EXECUTION_STACK rows (method_artifact_id NULL) — deliberate,
    -- and the CHECK below is what makes the NULL pattern a contract instead of a loophole.
    method_artifact_id      text,
    PRIMARY KEY (attempt_id, member_name, certificate_kind),
    CONSTRAINT production_member_certificate_kind_agrees CHECK (
        certificate_kind = subject_identity_kind),
    CONSTRAINT production_member_certificate_authoring_names_its_member CHECK (
        (certificate_kind = 'AUTHORING_METHOD') = (method_artifact_id IS NOT NULL)),
    CONSTRAINT production_member_certificate_binds_the_sealed_identity
        FOREIGN KEY (method_artifact_id, member_name, subject_identity_hash)
        REFERENCES sealed_artifact_member_method_identity
            (artifact_id, member_name, method_identity_hash)
);
