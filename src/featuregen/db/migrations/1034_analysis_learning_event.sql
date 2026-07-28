-- 1034 — learning events: what a blocked question taught us about the ontology.
--
-- The first feedback loop. Until now a question that could not be answered simply failed; the
-- evidence that the ontology has a specific, actionable gap was discarded.
--
-- THREE THINGS ARE DELIBERATELY NOT THE SAME, and only one lives here:
--   * a data observation is evidence about DATA           -> data_observation (1033)
--   * a LEARNING GAP is evidence the ontology is incomplete -> this table
--   * a technical failure (Hive unreachable, Spark died)  -> run diagnostics, NOT here
-- A connection timeout must never become "customer relationship missing", or the ontology fills
-- with candidates manufactured by an outage. The code vocabulary is closed in application code and
-- a technical code is refused.
--
-- IMMUTABLE. Resolving a gap writes a `resolution` row referencing the original `gap` row; nothing
-- is updated or deleted. "What did we not know when that decision was made?" must stay answerable.
--
-- GAP IDENTITY vs DEMAND. `gap_key` is (code, subject_refs) — the thing to DECIDE — and excludes
-- the request and the snapshot. Two questions blocked by one gap share a key, so demand is
-- count(DISTINCT analysis_request_id) and a gap is never duplicated by being hit twice. Uniqueness
-- includes the snapshot so a NEW dependency snapshot re-evaluates rather than being swallowed.

CREATE TABLE IF NOT EXISTS analysis_learning_event (
    event_id                  text        PRIMARY KEY,
    kind                      text        NOT NULL,
    analysis_request_id       text        NOT NULL,
    stage                     text        NOT NULL,
    code                      text        NOT NULL,
    gap_key                   text        NOT NULL,
    subject_refs              text[]      NOT NULL,
    required_action           text        NOT NULL,
    dependency_snapshot_id    text        NOT NULL,
    candidate_refs_considered text[]      NOT NULL DEFAULT '{}',
    supporting_evidence_ids   text[]      NOT NULL DEFAULT '{}',
    -- set only on a resolution row; points at the gap it closes
    resolves_event_id         text        NULL REFERENCES analysis_learning_event (event_id),
    decision                  text        NULL,
    decided_by                text        NULL,
    created_at                timestamptz NOT NULL,
    CONSTRAINT analysis_learning_event_kind_ck CHECK (kind IN ('gap', 'resolution')),
    CONSTRAINT analysis_learning_event_stage_ck
        CHECK (stage IN ('grounding', 'planning', 'validation')),
    -- a resolution must say what was decided and reference what it closes; a gap must do neither
    CONSTRAINT analysis_learning_event_resolution_ck CHECK (
        (kind = 'resolution' AND resolves_event_id IS NOT NULL AND decision IS NOT NULL)
        OR (kind = 'gap' AND resolves_event_id IS NULL AND decision IS NULL)),
    CONSTRAINT analysis_learning_event_subjects_ck CHECK (cardinality(subject_refs) > 0)
);

-- One gap row per (request, gap, snapshot): re-running an unchanged blocked question is not new
-- information, but a new snapshot is.
CREATE UNIQUE INDEX IF NOT EXISTS analysis_learning_event_gap_idx
    ON analysis_learning_event (analysis_request_id, gap_key, dependency_snapshot_id)
    WHERE kind = 'gap';

CREATE INDEX IF NOT EXISTS analysis_learning_event_demand_idx
    ON analysis_learning_event (gap_key, kind);
