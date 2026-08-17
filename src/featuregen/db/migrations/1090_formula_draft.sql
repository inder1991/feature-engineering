-- src/featuregen/db/migrations/1090_formula_draft.sql
-- Draft formula, ASYNC: the request, its state machine, and its SEPARATELY-IDENTIFIED admission.
--
-- WHY ASYNC AT ALL. A draft is two provider calls plus validation plus admission. Running it inside
-- an HTTP request would hold `get_conn`'s single transaction open across both LLM calls — the exact
-- shape `materialization_runs.py` refuses for compiles, for the same reasons: a client disconnect
-- or an app restart orphans the work, and the database transaction is held for as long as a model
-- takes to answer. It is also what makes the stage progression visible: a caller polls a row that
-- MOVES, rather than waiting on a socket that says nothing until it says everything.
--
-- THE TWO IDENTITIES, AND WHY THEY ARE NOT ONE. This is the reuse rule and it is the whole point of
-- the second table.
--
--   * FORMULA identity answers "would asking the model again produce the same thing?" — the
--     candidate (considered revision + option), the planning request, the frozen catalog snapshot,
--     the authoring configuration and model contract, and the user's own revision of the definition.
--     Every part is something that, if it moved, means the ANSWER could legitimately differ.
--
--   * ADMISSION identity answers "is that formula admissible HERE, NOW?" — the formula's hash, the
--     admission policy version, and the engine's capability-set hash.
--
-- Fold them together and an engine gaining a missing FX operator would invalidate the formula and
-- buy the same answer from the model a second time. Kept apart, the capability-set hash moves, a new
-- admission row is written against the SAME formula, and nothing is spent. That is the difference
-- between a platform whose costs fall as it improves and one whose costs repeat.
--
-- DOUBLE-CLICK MUST NOT BUY TWO ANSWERS. `formula_identity_hash` carries a UNIQUE index, so the
-- second request for an identical candidate/snapshot/config finds the first row and returns it. The
-- idempotency is on the IDENTITY rather than on a caller-supplied key, because a caller that
-- generated a fresh key per click would defeat a key-based one — and the thing being protected is
-- money.
--
-- EACH TRANSITION COMMITS SEPARATELY. The state column moves one step at a time and the worker
-- commits between steps, so a crash mid-draft leaves the row saying exactly how far it got. There is
-- deliberately no "in flight" boolean: `state` IS that information, and a second field would let the
-- two disagree.
--
-- BLOCKED IS A PRODUCT RESULT, NOT A FAILURE. A formula can be perfectly valid and still name an
-- operator this engine does not advertise. Recording that as FAILED would tell an operator to
-- investigate an outage when the honest answer is "this needs an engine capability nobody has
-- proved yet" — a different person, a different remedy.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS formula_draft (
    formula_draft_id      text PRIMARY KEY CHECK (btrim(formula_draft_id) <> ''),

    -- ── the CANDIDATE this draft is for ─────────────────────────────────────────────────────────
    -- Both halves, because an option id is only meaningful inside the revision that froze it.
    considered_revision_id text NOT NULL CHECK (btrim(considered_revision_id) <> ''),
    option_id             text NOT NULL CHECK (btrim(option_id) <> ''),

    -- ── FORMULA IDENTITY: would asking again produce the same thing? ────────────────────────────
    planning_request_hash text NOT NULL CHECK (btrim(planning_request_hash) <> ''),
    catalog_snapshot_hash text NOT NULL CHECK (btrim(catalog_snapshot_hash) <> ''),
    authoring_config_hash text NOT NULL CHECK (btrim(authoring_config_hash) <> ''),
    -- The user's own revision of the feature definition: editing the definition is asking a
    -- DIFFERENT question, and must not be served the previous answer.
    definition_revision   text NOT NULL,
    formula_identity_hash text NOT NULL CHECK (btrim(formula_identity_hash) <> ''),

    -- ── the state machine ───────────────────────────────────────────────────────────────────────
    state                 text NOT NULL CHECK (state IN (
                              'REQUESTED', 'AUTHORING', 'CRITIC_REVIEW', 'VALIDATING',
                              'ADMISSION', 'READY',
                              'BLOCKED', 'FAILED', 'CANCELLED')),

    -- What the run produced, once it has produced anything. All NULL until then — an honest
    -- absence, never a placeholder that reads as an empty formula.
    authoring_run_id      text,
    formula_content_hash  text,
    formula_json          jsonb,

    -- Named blockers for BLOCKED, and a reason for FAILED. Separate columns because they are
    -- different kinds of statement: a blocker is a governed verdict with a code, a failure reason is
    -- an operational sentence.
    blockers              jsonb NOT NULL DEFAULT '[]'::jsonb,
    failure_reason        text,

    requested_by          text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at          text NOT NULL CHECK (btrim(requested_at) <> ''),
    updated_at            timestamptz NOT NULL DEFAULT now(),

    -- A terminal BLOCKED must name what blocked it; anything else is a refusal nobody can act on.
    CONSTRAINT formula_draft_blocked_names_blockers
        CHECK (state <> 'BLOCKED' OR jsonb_array_length(blockers) > 0),
    -- A FAILED must say why, and only a FAILED may.
    CONSTRAINT formula_draft_failure_reason_belongs_to_failed
        CHECK ((failure_reason IS NULL) = (state <> 'FAILED')),
    -- READY must carry the artifact it claims to have produced, and `{}` IS NOT ONE. NOT NULL
    -- alone was not enough: a run that parsed nothing yields an empty object, which satisfies NOT
    -- NULL and reads to every downstream reader as a formula with no body. Found by an end-to-end
    -- test that reached READY carrying `{}` — the constraint below is what makes that state
    -- unreachable rather than merely unlikely.
    CONSTRAINT formula_draft_ready_carries_a_formula
        CHECK (state <> 'READY' OR (formula_content_hash IS NOT NULL
                                    AND formula_json IS NOT NULL
                                    AND formula_json <> '{}'::jsonb))
);

-- THE MONEY GUARD. One paid authoring run per formula identity — a second request for the same
-- candidate, snapshot and configuration finds this row instead of buying the answer again.
CREATE UNIQUE INDEX IF NOT EXISTS formula_draft_identity
    ON formula_draft (formula_identity_hash);

CREATE INDEX IF NOT EXISTS formula_draft_by_candidate
    ON formula_draft (considered_revision_id, option_id);

-- ── admission, identified SEPARATELY ─────────────────────────────────────────────────────────────
-- One row per (formula, admission policy, capability set). When the engine gains an operator the
-- capability hash moves and a NEW row is written against the same formula — no LLM call.
CREATE TABLE IF NOT EXISTS formula_draft_admission (
    admission_identity_hash text PRIMARY KEY CHECK (btrim(admission_identity_hash) <> ''),

    formula_draft_id        text NOT NULL REFERENCES formula_draft(formula_draft_id),
    formula_content_hash    text NOT NULL CHECK (btrim(formula_content_hash) <> ''),
    admission_policy_version integer NOT NULL CHECK (admission_policy_version >= 1),
    engine_id               text NOT NULL CHECK (btrim(engine_id) <> ''),
    -- The hash of the ADVERTISED SET this admission was decided against. Not the engine id: an
    -- engine that gains an operator is the same engine, and it is the SET that moved.
    capability_set_hash     text NOT NULL CHECK (btrim(capability_set_hash) <> ''),

    admitted                boolean NOT NULL,
    blockers                jsonb NOT NULL DEFAULT '[]'::jsonb,
    decided_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT formula_draft_admission_refusal_names_blockers
        CHECK (admitted OR jsonb_array_length(blockers) > 0)
);

CREATE INDEX IF NOT EXISTS formula_draft_admission_by_draft
    ON formula_draft_admission (formula_draft_id, decided_at DESC);

-- ── what may move, and what may not ──────────────────────────────────────────────────────────────
-- The draft's IDENTITY and its candidate are frozen on arrival; the state and its results move as
-- the worker advances. An admission decision never moves at all — a later capability set writes a
-- NEW row, which is the whole point of the second identity.
CREATE OR REPLACE FUNCTION formula_draft_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'formula_draft is append-only';
    END IF;
    IF (NEW.formula_draft_id, NEW.considered_revision_id, NEW.option_id,
        NEW.planning_request_hash, NEW.catalog_snapshot_hash, NEW.authoring_config_hash,
        NEW.definition_revision, NEW.formula_identity_hash, NEW.requested_by, NEW.requested_at)
       IS DISTINCT FROM
       (OLD.formula_draft_id, OLD.considered_revision_id, OLD.option_id,
        OLD.planning_request_hash, OLD.catalog_snapshot_hash, OLD.authoring_config_hash,
        OLD.definition_revision, OLD.formula_identity_hash, OLD.requested_by, OLD.requested_at) THEN
        RAISE EXCEPTION 'formula_draft identity is frozen; only state and results may move';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS formula_draft_no_identity_edit ON formula_draft;
CREATE TRIGGER formula_draft_no_identity_edit
    BEFORE UPDATE OR DELETE ON formula_draft
    FOR EACH ROW EXECUTE FUNCTION formula_draft_guard();

CREATE OR REPLACE FUNCTION formula_draft_admission_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'formula_draft_admission is append-only: a later capability set writes a NEW row';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS formula_draft_admission_no_update ON formula_draft_admission;
CREATE TRIGGER formula_draft_admission_no_update
    BEFORE UPDATE OR DELETE ON formula_draft_admission
    FOR EACH ROW EXECUTE FUNCTION formula_draft_admission_write_once();
