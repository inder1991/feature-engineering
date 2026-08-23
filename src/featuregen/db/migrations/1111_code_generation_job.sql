-- src/featuregen/db/migrations/1111_code_generation_job.sql
-- THE DURABLE JOURNEY AGGREGATE — child §3.5 / parent §0.1.3. The coordinator that turns one
-- explicit user act ("prepare formulas and code for THESE selections") into the composed chain:
-- resolve strategies → request drafts → wait on durable formula states → bind → declare the build
-- set → decide GENERATE_PREVIEW → enqueue generation — surviving browser closure, reload and
-- worker restarts, idempotent on its exact request content.
--
-- ▲ THIS IS A PREVIEW COORDINATOR (child §3.5's settlement): terminal at PREVIEW_READY, with
-- immutable LINKS to the sandbox and production attempts that follow. Owning the whole journey
-- would make it a second authority over acts it does not gate (parent §3).
--
-- ▲ A LIFECYCLE TABLE SHIPS WITH ITS LEASE AND ITS RECONCILER (parent §15.1, §9.0.1's wedge —
-- the fourth instance found this programme, so the fifth table does not get to repeat it): lease,
-- fence and attempts from day one, and the live-scope idempotency index below mirrors 1107's
-- money-guard lesson — a FAILED or CANCELLED job bought nothing and must not hold the identity
-- slot, so recovery can re-request while double-clicks land on the live row.

CREATE TABLE IF NOT EXISTS code_generation_job (
    job_id                    text PRIMARY KEY CHECK (btrim(job_id) <> ''),
    -- Idempotency identity: jcs_sha256 over the EXACT request content (considered revision,
    -- ordered selections, declaration identity, environment, target). NOT the primary key,
    -- because a retry after FAILED/CANCELLED is a NEW job over the same content — the partial
    -- unique index below scopes the guard to live jobs, exactly like the money guard.
    content_identity_hash     text NOT NULL CHECK (btrim(content_identity_hash) <> ''),
    considered_revision_id    text NOT NULL
        REFERENCES contract_considered_revision(considered_revision_id),
    target_reading_revision_id text NOT NULL CHECK (btrim(target_reading_revision_id) <> ''),
    environment_id            text NOT NULL CHECK (btrim(environment_id) <> ''),
    logical_group_name        text NOT NULL CHECK (btrim(logical_group_name) <> ''),
    -- The five declarations a generation cannot derive, verbatim as submitted; the STORED payload
    -- is complete while the identity hash above folds only `declaration_identity` (the 55f7235a
    -- lesson: hashing provenance forks the identity per clock read).
    declaration_json          jsonb NOT NULL,
    -- The execution parameters the generation request needs beyond the declaration (engine,
    -- physical type policy, per-feature empty-value semantics, roles, target mode/ref) — part of
    -- the exact request content, so part of the identity hash.
    execution_parameters_json jsonb NOT NULL,
    status                    text NOT NULL DEFAULT 'REQUESTED'
        CONSTRAINT code_generation_job_status_v1 CHECK (status IN (
            'REQUESTED', 'PLANNING_FORMULAS', 'AUTHORING', 'READY_TO_BUILD',
            'GENERATING_PREVIEW', 'PREVIEW_READY', 'BLOCKED', 'FAILED', 'CANCELLED')),
    requested_by              text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at              timestamptz NOT NULL,
    -- BLOCKED is a product outcome (its members' blockers are the answer); FAILED is a platform
    -- failure (its detail names the posture or the crash). One column serves both terminals.
    terminal_detail_json      jsonb,
    -- Links filled as the chain progresses — nullable EXPAND (the 1095 lesson), each a real FK so
    -- a job cannot claim a build set or generation that is not there.
    build_set_revision_id     text REFERENCES build_set_revision(revision_id),
    generation_request_id     text REFERENCES generation_request(request_id),
    -- The lease (1092's discipline): claims are `FOR UPDATE SKIP LOCKED` over due jobs, the fence
    -- rises per claim, and an expired lease is re-claimable by the same scan — the lane IS its
    -- own lease reconciler, and the wedge shape cannot form.
    lease_owner               text,
    lease_expires_at          timestamptz,
    lease_fence               bigint NOT NULL DEFAULT 0,
    attempts                  integer NOT NULL DEFAULT 0
);

-- ▲ THE LIVE-SCOPE GUARD: one live job per exact request content. Terminal states release the
-- slot — a failed job is not an answer (§11.1.2), and holding the identity hostage to it would
-- make recovery impossible without a content change nobody actually wants.
CREATE UNIQUE INDEX IF NOT EXISTS code_generation_job_one_live
    ON code_generation_job (content_identity_hash)
    WHERE status NOT IN ('FAILED', 'CANCELLED');

CREATE INDEX IF NOT EXISTS code_generation_job_due
    ON code_generation_job (lease_expires_at)
    WHERE status IN ('REQUESTED', 'PLANNING_FORMULAS', 'AUTHORING', 'READY_TO_BUILD',
                     'GENERATING_PREVIEW');

-- ── MEMBERS: ordered, each carrying its own strategy, draft, binding and blockers ───────────────
-- Position is identity: the order a person picked features in decides the published table's
-- column order, so it is a fact about the build and never a set.
CREATE TABLE IF NOT EXISTS code_generation_job_member (
    job_id                       text NOT NULL REFERENCES code_generation_job(job_id),
    position                     integer NOT NULL CHECK (position >= 0),
    selection_revision_id        text NOT NULL REFERENCES feature_selection_revision(revision_id),
    considered_revision_id       text NOT NULL CHECK (btrim(considered_revision_id) <> ''),
    option_id                    text NOT NULL CHECK (btrim(option_id) <> ''),
    formula_strategy             text NOT NULL CHECK (btrim(formula_strategy) <> ''),
    strategy_warnings_json       jsonb NOT NULL DEFAULT '[]'::jsonb,
    member_state                 text NOT NULL DEFAULT 'SELECTED'
        CONSTRAINT code_generation_job_member_state_v1 CHECK (member_state IN (
            'SELECTED', 'AUTHORING', 'FORMULA_READY', 'BOUND', 'BLOCKED', 'FAILED')),
    -- Filled as the member progresses; real FKs so a member cannot claim work that is not there.
    formula_draft_id             text REFERENCES formula_draft(formula_draft_id),
    -- ▲ The BINDING, not a loose (draft_id, hash) pair — parent §11.0.1/P0-6: a loose pair still
    -- permits a valid READY formula belonging to a DIFFERENT selection. The coordinator hands the
    -- parent's binding through; it does not re-resolve.
    selection_formula_binding_id text REFERENCES selection_formula_binding(binding_id),
    blockers_json                jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (job_id, position),
    -- One selection appears once per job: naming a feature twice is a caller error the store
    -- refuses BY NAME before this constraint ever fires.
    CONSTRAINT code_generation_job_member_selection_once UNIQUE (job_id, selection_revision_id),
    -- A member that reached BLOCKED must say why; a member that has not must not carry stale
    -- blockers from an earlier claim.
    CONSTRAINT code_generation_job_member_blocked_names_blockers CHECK (
        (member_state = 'BLOCKED') = (jsonb_array_length(blockers_json) > 0))
);

-- ── EVENTS: append-only stage history — what a progress projection reads ────────────────────────
CREATE TABLE IF NOT EXISTS code_generation_job_event (
    event_seq   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      text NOT NULL REFERENCES code_generation_job(job_id),
    stage       text NOT NULL CHECK (btrim(stage) <> ''),
    detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS code_generation_job_event_by_job
    ON code_generation_job_event (job_id, event_seq);

CREATE OR REPLACE FUNCTION code_generation_job_event_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'code_generation_job_event is append-only: it is what HAPPENED, and a history '
                    'that can be rewritten is not a history';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS code_generation_job_event_no_change ON code_generation_job_event;
CREATE TRIGGER code_generation_job_event_no_change
    BEFORE UPDATE OR DELETE ON code_generation_job_event
    FOR EACH ROW EXECUTE FUNCTION code_generation_job_event_write_once();

-- ── ACTIONS: one job performs SEVERAL actions, so it records several — parent §0.1.3 (R7) ───────
-- One row per action the journey performs, each carrying that action's OWN authorization and
-- request-time decision once the stage runs. A single `requested_action` with a single
-- authorization could not truthfully cover AUTHOR_FORMULA and GENERATE_PREVIEW, let alone the
-- sandbox acts the workspace then shows.
--
-- ▲ The composite FK is MATCH SIMPLE and both authorization columns are nullable until the
-- stage decides — DELIBERATE: a PENDING stage has no authorization yet, and MATCH SIMPLE means
-- the constraint enforces exactly when the row claims one (the known trap, used on purpose and
-- documented here so nobody "fixes" it into a day-one NOT NULL that no stage could satisfy).
CREATE TABLE IF NOT EXISTS code_generation_job_action (
    job_id                    text NOT NULL REFERENCES code_generation_job(job_id),
    action                    text NOT NULL CHECK (btrim(action) <> ''),
    resource_identity_hash    text,
    authorization_revision_id text,
    decision_revision_id      text REFERENCES action_decision_revision(decision_id),
    state                     text NOT NULL DEFAULT 'PENDING'
        CONSTRAINT code_generation_job_action_state_v1 CHECK (state IN (
            'PENDING', 'DECIDED', 'PERFORMED', 'REFUSED', 'SKIPPED')),
    PRIMARY KEY (job_id, action),
    FOREIGN KEY (action, resource_identity_hash, authorization_revision_id)
        REFERENCES action_authorization_revision (action, resource_identity_hash, authorization_id)
);
