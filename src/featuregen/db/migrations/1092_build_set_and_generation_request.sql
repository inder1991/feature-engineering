-- src/featuregen/db/migrations/1092_build_set_and_generation_request.sql
-- "Prepare selected features": the immutable BUILD SET, and the durable REQUEST that acts on it.
--
-- WHAT WAS MISSING. `BuildSetRevisionV1` has existed as a dataclass with no store, and
-- 1082's `generation_authorization` already references a `build_set_revision_id` that no table
-- holds. So the root of the whole generate → compile → seal chain was a foreign key pointing at
-- nothing. Selections and target readings got their tables in 1072; the thing that GROUPS them did
-- not.
--
-- WHY A BUILD SET IS IMMUTABLE. It is the answer to "which features did this person ask to build,
-- together, against which target". Every artifact downstream — the group plan, the contract, the
-- sealed project, the published columns — is derived from that membership. If the set could be
-- edited, a sealed artifact could stop matching the request it came from while both looked current.
-- Changing your mind mints a NEW set; that is what revisions are for.
--
-- ORDER IS PART OF IT. `build_set_member.position` exists because the order a person chose features
-- in is a fact about the build, and a set would discard it. The dataclass already says so; this is
-- where it survives a round trip.
--
-- THE REQUEST IS SEPARATE FROM THE SET, and that separation is load-bearing. A set is a DECLARATION
-- ("build these"); a request is an ATTEMPT to act on it. One set may be attempted more than once —
-- after a provider outage, after an operator gains a capability — and each attempt has its own
-- status, its own refusals and its own artifact. Folding them together would make a retry
-- indistinguishable from a second, different build.
--
-- ALL OR NOTHING, DELIBERATELY. There is no PARTIAL outcome, and its absence is a decision rather
-- than an omission. `admit_artifacts_v2` already refuses to admit the survivors of a batch, for the
-- reason that applies with more force here: a group's identity IS its membership. Building four of
-- five features silently delivers something nobody asked for, under a contract nobody chose, and
-- the person who asked would have to notice the difference to know it happened. A refusal that
-- NAMES the feature that could not be built is actionable; a quiet four-fifths is not.
--
-- IDEMPOTENCY IS ON THE WORK, not on a caller-supplied key. The same build set attempted again
-- while an attempt is already live returns that attempt. A client minting a fresh key per click
-- would defeat a key-based guard, and generation costs real compute — the same reasoning that made
-- `formula_draft` idempotent on formula identity rather than on a request id.
--
-- NOT APPLIED. This file is written, not run.

-- ── the immutable declaration ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS build_set_revision (
    revision_id               text PRIMARY KEY CHECK (btrim(revision_id) <> ''),

    -- WHAT this set is built to predict. The EXACT revision, so a later re-read of the target
    -- cannot silently change what this set was built for.
    target_reading_revision_id text NOT NULL
                               REFERENCES target_reading_revision(revision_id),

    -- The declaration the whole set shares: grain, environment, mode. Stored as its content hash
    -- and its payload, so a reader can both compare and inspect.
    declaration_hash          text NOT NULL CHECK (btrim(declaration_hash) <> ''),
    declaration_json          jsonb NOT NULL,

    -- Content identity over (target reading, ordered members, declaration). Two people asking for
    -- the same build get the same set — which is what makes a re-request cheap rather than a
    -- duplicate.
    content_hash              text NOT NULL CHECK (btrim(content_hash) <> ''),

    declared_by               text NOT NULL CHECK (btrim(declared_by) <> ''),
    declared_at               text NOT NULL CHECK (btrim(declared_at) <> ''),
    recorded_at               timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS build_set_revision_content
    ON build_set_revision (content_hash);

-- ── its ORDERED membership ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS build_set_member (
    revision_id            text NOT NULL REFERENCES build_set_revision(revision_id),
    position               integer NOT NULL CHECK (position >= 0),

    -- WHICH selection. The selection revision is the durable identity of "this person chose this
    -- candidate"; the formula it resolves to is looked up through it rather than copied, so a set
    -- cannot drift from the choice it records.
    selection_revision_id  text NOT NULL REFERENCES feature_selection_revision(revision_id),

    PRIMARY KEY (revision_id, position),
    -- The same selection twice would make "which position is this feature in" unanswerable — the
    -- dataclass refuses it, and so does the table.
    UNIQUE (revision_id, selection_revision_id)
);

-- ── the durable ATTEMPT ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS generation_request (
    request_id             text PRIMARY KEY CHECK (btrim(request_id) <> ''),
    build_set_revision_id  text NOT NULL REFERENCES build_set_revision(revision_id),
    environment_id         text NOT NULL CHECK (btrim(environment_id) <> ''),

    -- The lifecycle. Explicit states rather than a pair of booleans, so "still running" is
    -- distinguishable from "the worker died" and from "nothing consumes this table" — the gap S11's
    -- verification_attempt has and this deliberately does not.
    status                 text NOT NULL CHECK (status IN (
                               'REQUESTED', 'CLAIMED', 'RUNNING',
                               'SUCCEEDED', 'REFUSED', 'FAILED', 'CANCELLED')),

    -- What it produced, once it has produced anything. NULL until then — an honest absence rather
    -- than a placeholder that reads as an empty artifact.
    sealed_artifact_id     text,

    -- WHY it refused, per feature. REFUSED is a product result: the build could not be made from
    -- what was asked for, and this names which member and which code. FAILED is an outage and
    -- carries a reason instead.
    refusals               jsonb NOT NULL DEFAULT '[]'::jsonb,
    failure_reason         text,

    requested_by           text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at           text NOT NULL CHECK (btrim(requested_at) <> ''),
    updated_at             timestamptz NOT NULL DEFAULT now(),

    -- A REFUSED request must name what refused it; a refusal nobody can act on is not one.
    CONSTRAINT generation_request_refused_names_refusals
        CHECK (status <> 'REFUSED' OR jsonb_array_length(refusals) > 0),
    -- A FAILED must say why, and only a FAILED may.
    CONSTRAINT generation_request_failure_reason_belongs_to_failed
        CHECK ((failure_reason IS NULL) = (status <> 'FAILED')),
    -- SUCCEEDED must carry the artifact it claims to have produced. The whole point of the request
    -- is that artifact; succeeding without one would be a status with nothing behind it.
    CONSTRAINT generation_request_success_carries_an_artifact
        CHECK (status <> 'SUCCEEDED' OR sealed_artifact_id IS NOT NULL)
);

-- THE IDEMPOTENCY GUARD. At most ONE live attempt per (build set, environment): asking again while
-- one is in flight finds it rather than starting a second compile of the same thing. Terminal
-- attempts are excluded, so a retry after a failure is allowed — which is the difference between
-- protecting against double-clicks and preventing recovery.
CREATE UNIQUE INDEX IF NOT EXISTS generation_request_one_live_attempt
    ON generation_request (build_set_revision_id, environment_id)
    WHERE status IN ('REQUESTED', 'CLAIMED', 'RUNNING');

CREATE INDEX IF NOT EXISTS generation_request_by_set
    ON generation_request (build_set_revision_id, updated_at DESC);

-- ── what may move, and what may not ──────────────────────────────────────────────────────────────
-- A build set is a DECLARATION and never changes; a request's status moves and its identity does
-- not.
CREATE OR REPLACE FUNCTION build_set_revision_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'build_set_revision is immutable: changing your mind mints a NEW set, because '
                    'every artifact downstream is derived from this membership';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS build_set_revision_no_change ON build_set_revision;
CREATE TRIGGER build_set_revision_no_change
    BEFORE UPDATE OR DELETE ON build_set_revision
    FOR EACH ROW EXECUTE FUNCTION build_set_revision_immutable();

DROP TRIGGER IF EXISTS build_set_member_no_change ON build_set_member;
CREATE TRIGGER build_set_member_no_change
    BEFORE UPDATE OR DELETE ON build_set_member
    FOR EACH ROW EXECUTE FUNCTION build_set_revision_immutable();

CREATE OR REPLACE FUNCTION generation_request_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'generation_request is append-only: an attempt that happened happened';
    END IF;
    IF (NEW.request_id, NEW.build_set_revision_id, NEW.environment_id,
        NEW.requested_by, NEW.requested_at)
       IS DISTINCT FROM
       (OLD.request_id, OLD.build_set_revision_id, OLD.environment_id,
        OLD.requested_by, OLD.requested_at) THEN
        RAISE EXCEPTION 'generation_request identity is frozen; only status and results may move';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS generation_request_no_identity_edit ON generation_request;
CREATE TRIGGER generation_request_no_identity_edit
    BEFORE UPDATE OR DELETE ON generation_request
    FOR EACH ROW EXECUTE FUNCTION generation_request_guard();
