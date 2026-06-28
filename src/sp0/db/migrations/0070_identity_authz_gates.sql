-- Phase 07 — identity / authz / human gates.
-- Owned tables (authz_policy, security_audit, human_tasks, human_task_responses) are
-- transcribed VERBATIM from the shared SP-0 contract; task_delegations is a Phase-07
-- SUPPORTING table (not in the core DDL; phase-internal). PostgreSQL 15+.
--
-- File-based migration: applied (in lexical order, after the core Python DDL) by
-- sp0.db.migrations.apply_migrations. The 0070_ prefix sorts it after Phase-06's
-- 0060_aggregates_lifecycle. All statements are idempotent (IF NOT EXISTS) so the
-- whole migration set can be re-applied against an existing schema.
--
-- NOTE: authz_policy PK columns are NOT NULL by definition; the contract marks
-- `gate` NULL but the PK forces it NOT NULL, so non-gate rows use the '' sentinel.
CREATE TABLE IF NOT EXISTS authz_policy (
    action        text        NOT NULL,
    gate          text        NULL,
    permitted_role text       NOT NULL,
    actor_kind    text        NOT NULL CHECK (actor_kind IN ('human','service','any')),
    scope         text        NULL,
    PRIMARY KEY (action, gate, permitted_role, actor_kind)
);

CREATE TABLE IF NOT EXISTS security_audit (
    security_event_id text        PRIMARY KEY,
    seq               bigint      NOT NULL DEFAULT nextval('global_seq_seq'),
    event_type        text        NOT NULL,
    actor             jsonb       NOT NULL,
    attempted_action  text        NOT NULL,
    aggregate         text        NULL,
    aggregate_id      text        NULL,
    decision          text        NOT NULL
                          CHECK (decision IN ('denied','allowed_break_glass','flagged')),
    reason            text        NULL,
    prev_hash         text        NULL,
    entry_hash        text        NOT NULL,
    retention_class   text        NOT NULL DEFAULT 'regulator',
    occurred_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS security_audit_seq_idx   ON security_audit (seq);
CREATE INDEX IF NOT EXISTS security_audit_actor_idx ON security_audit ((actor->>'subject'));

CREATE TABLE IF NOT EXISTS human_tasks (
    task_id            text        PRIMARY KEY,
    task_version       integer     NOT NULL DEFAULT 1,
    run_id             text        NULL,
    feature_id         text        NULL,
    gate               text        NOT NULL
                           CHECK (gate IN ('CLARIFICATION','DATA_STEWARD','COMPLIANCE',
                                           'INDEPENDENT_VALIDATION','FINAL_APPROVAL')),
    required_inputs    text[]      NOT NULL DEFAULT '{}',
    eligible_assignees jsonb       NOT NULL,
    allowed_responses  text[]      NOT NULL,
    quorum_required    integer     NOT NULL DEFAULT 1,
    quorum_of_role     text        NULL,
    delegation_allowed boolean     NOT NULL DEFAULT true,
    sla                text        NULL,
    status             text        NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','answered','conflict','expired','cancelled','superseded')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS human_tasks_open_idx ON human_tasks (gate) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS human_task_responses (
    task_id      text        NOT NULL REFERENCES human_tasks(task_id),
    subject      text        NOT NULL,
    response     text        NOT NULL,
    on_behalf_of text        NULL,
    answered_seq bigint      NOT NULL,
    answered_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, subject)
);

-- task_delegations — Phase-07 SUPPORTING table (NOT part of the shared core DDL; phase-internal,
-- referenced only by this phase). The core contract gives us `human_tasks.delegation_allowed`
-- and `human_task_responses.on_behalf_of` but no place to record WHO may answer on whose behalf.
-- This table records validated delegation grants so submit_human_signal can verify a REAL
-- delegation relationship exists and that the PRINCIPAL is itself an eligible assignee (§7).
CREATE TABLE IF NOT EXISTS task_delegations (
    task_id    text        NOT NULL REFERENCES human_tasks(task_id),
    principal  text        NOT NULL,                              -- eligible assignee granting authority
    delegate   text        NOT NULL,                              -- subject acting on the principal's behalf
    granted_by text        NOT NULL,                              -- who recorded the grant
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, principal, delegate)
);
