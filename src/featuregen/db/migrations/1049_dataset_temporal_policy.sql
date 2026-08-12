-- src/featuregen/db/migrations/1049_dataset_temporal_policy.sql
-- Release-B profile Task 7 (reservation: verified-interfaces doc D7 — 1049 is the temporal policy
-- store, the second and LAST number this task allocates).
--
-- WHICH ROWS of one dataset answer a current question, and which answer a historical one. Source
-- choice and row choice are separate decisions (§5.7), so this is a separate store from 1048 —
-- keyed by the DATASET, because "how does this table store history" is a property of the table,
-- while "which table serves this need" is a property of the need.
--
-- IMMUTABLE REVISIONS + CAS POINTER, identical discipline to 1047/1048. 'dtp_' + the RFC-8785 JCS
-- content hash is the deterministic revision id, so a row selection can reference a policy revision
-- that can never be mutated underneath it.
--
-- ONLY REFS, NEVER VALUES OR SQL (§6.5). Every *_ref column holds a normalized logical COLUMN ref;
-- the runtime report date is a PARAMETER supplied at execution, never concatenated in. There is no
-- predicate-text column here for exactly that reason: a policy that could carry SQL would be a
-- second, ungoverned query language.
--
-- CURRENT vs HISTORICAL ARE TWO COLUMNS, deliberately (§5.5). One "selection" column would make a
-- historical question fall back to the current row whenever the dataset kept no history — the
-- single most damaging silent wrong answer this release exists to prevent. `explicit_only` is the
-- honest terminal value for that case.
--
-- No CHECK ties `temporal_storage_model` to a profile classification: the agreement rule (a policy
-- must agree with a LOAD-BEARING profile value, or BE the declaration when none exists — D12.7) is
-- a cross-table judgement over a resolver's output, enforced in `temporal_policy_store` at
-- authoring time. A CHECK here could only encode half of it, and the wrong half.

CREATE TABLE IF NOT EXISTS dataset_temporal_policy_revision (
    revision_id            text        PRIMARY KEY
        CHECK (revision_id ~ '^dtp_[0-9a-f]{64}$'),
    dataset_logical_ref    text        NOT NULL,
    temporal_storage_model text        NOT NULL
        CHECK (temporal_storage_model IN ('current_only', 'scd1', 'scd2', 'snapshot',
                                          'event_log', 'unknown')),
    current_selection      text        NOT NULL
        CHECK (current_selection IN ('current_record', 'valid_at_report_cutoff',
                                     'latest_snapshot_as_of', 'explicit_only')),
    historical_selection   text        NOT NULL
        CHECK (historical_selection IN ('current_record', 'valid_at_report_cutoff',
                                        'latest_snapshot_as_of', 'explicit_only')),
    effective_from_ref     text        NULL,
    effective_to_ref       text        NULL,
    snapshot_ref           text        NULL,
    current_flag_ref       text        NULL,
    -- Availability time is kept DISTINCT from effective time: when a fact became true and when it
    -- became readable are different instants, and collapsing them into one "as of" is how a
    -- late-arriving row silently changes a historical answer.
    availability_ref       text        NULL,
    tie_break_refs         jsonb       NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(tie_break_refs) = 'array'),
    provenance             jsonb       NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(provenance) = 'object'),
    content_hash           text        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    authored_by            text        NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dataset_temporal_policy_revision_dataset_idx
    ON dataset_temporal_policy_revision (dataset_logical_ref);

CREATE TABLE IF NOT EXISTS dataset_temporal_policy_current (
    dataset_logical_ref text        PRIMARY KEY,
    revision_id         text        NOT NULL
        REFERENCES dataset_temporal_policy_revision(revision_id),
    pointer_version     integer     NOT NULL CHECK (pointer_version >= 1),
    declared_by         text        NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
