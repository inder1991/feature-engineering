-- src/featuregen/db/migrations/1072_selection_and_target_reading_revisions.sql
-- S1 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the target-reading revision
-- and the feature selection revision, both APPEND-ONLY.
--
-- THE DEFECT THIS CLOSES. `record_target_reading` does `UPDATE contract_intent SET target_ref = …`
-- (contract/intake_ticket.py:226-231). Three consequences, all live:
--   1. the reading a leakage gate ran against is GONE the moment anyone re-reads. A generation
--      authorized for "predict churn" cannot be shown the reading it was authorized under.
--   2. there is NO provenance guard. `contract.py:1380` computes
--      `provenance = "exploring" if body.decision == "exploring" else "human_confirmed"`, so an
--      exploration declaration silently replaces a person's confirmed target — and, because
--      `exploring` is written into the provenance column, it also erases WHO confirmed it.
--   3. `catalog_source` is dropped. Two catalogs that both contain `public.txns.churned` are one
--      row, so a target in the wrong catalog is indistinguishable from the right one.
--
-- MODE AND PROVENANCE ARE SEPARATE AXES (C-D12). `mode` says what kind of build this is —
-- prediction or exploration. `provenance` says who declared it. The shipped vocabulary fused them
-- and the fusion destroyed information, so a person who explicitly declares "no target" now keeps
-- their identity. Legacy rows map with no loss: `exploring` becomes (exploration, NULL provenance),
-- and the NULL is TRUTHFUL — the old schema never recorded a separate declarer for those rows.
--
-- THE REF CARRIES ITS OWN CATALOG. `target_logical_ref` is the canonical `<source>::<object>` form,
-- and `catalog_source` is DERIVED from it in Python rather than stored beside it. One fact, not
-- two: a check can be skipped, a derivation cannot.
--
-- NOT APPLIED. This file is written, not run. Applying it is an operator action.

-- ── the target reading ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS target_reading_revision (
    revision_id   text PRIMARY KEY CHECK (btrim(revision_id) <> ''),
    intent_id     text NOT NULL CHECK (btrim(intent_id) <> ''),

    -- C-D12's axis. Closed, and independent of who declared it.
    mode          text NOT NULL CHECK (mode IN ('prediction', 'exploration')),

    -- NULL only for a legacy `exploring` row, where the old schema destroyed the declarer by
    -- writing `exploring` into the provenance column. A new row always names a person.
    provenance    text CHECK (provenance IN ('human_confirmed', 'user_typed')),

    -- Present exactly when mode = 'prediction'. Not nulled-out prediction fields on an exploration
    -- row: the discriminated union in Python has no such fields to set, and the CHECK below makes
    -- the database agree rather than merely permit.
    target_logical_ref text CHECK (target_logical_ref LIKE '%::%'),
    target_type        text,
    horizon_days       integer CHECK (horizon_days > 0),

    confirmed_by  text,
    supersedes_revision_id text REFERENCES target_reading_revision(revision_id),
    -- Naming who accepted the loss of a confirmed target. Required by the writer when an
    -- exploration declaration replaces a prediction; recorded here so the acceptance is auditable
    -- rather than implied by the absence of a complaint.
    acknowledged_human_loss_by text,

    content_hash  text NOT NULL CHECK (btrim(content_hash) <> ''),
    recorded_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT target_reading_revision_mode_fields CHECK (
        (mode = 'prediction'
             AND target_logical_ref IS NOT NULL
             AND target_type IS NOT NULL
             AND horizon_days IS NOT NULL)
        OR
        (mode = 'exploration'
             AND target_logical_ref IS NULL
             AND target_type IS NULL
             AND horizon_days IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS target_reading_revision_by_intent
    ON target_reading_revision (intent_id, recorded_at DESC);

-- A revision may be superseded at most ONCE. Two successors would fork the chain, and "which
-- reading is current" would then depend on which fork a reader happened to walk.
CREATE UNIQUE INDEX IF NOT EXISTS target_reading_revision_one_successor
    ON target_reading_revision (supersedes_revision_id)
    WHERE supersedes_revision_id IS NOT NULL;

-- ── the feature selection ────────────────────────────────────────────────────────────────────────
-- WHICH served option a person chose, pinned to the exact thing that served it. Migration 1063
-- records every option SERVED, not which was selected, so this is genuinely new — it PINS 1063's
-- identity rather than inventing one.
CREATE TABLE IF NOT EXISTS feature_selection_revision (
    revision_id               text PRIMARY KEY CHECK (btrim(revision_id) <> ''),
    target_reading_revision_id text NOT NULL
                                  REFERENCES target_reading_revision(revision_id),
    considered_revision_id    text NOT NULL CHECK (btrim(considered_revision_id) <> ''),
    option_id                 text NOT NULL CHECK (btrim(option_id) <> ''),
    decision_id               text NOT NULL CHECK (btrim(decision_id) <> ''),
    planning_request_hash     text NOT NULL CHECK (btrim(planning_request_hash) <> ''),
    binding_plan_hash         text NOT NULL CHECK (btrim(binding_plan_hash) <> ''),
    content_hash              text NOT NULL CHECK (btrim(content_hash) <> ''),
    recorded_at               timestamptz NOT NULL DEFAULT now(),

    -- One selection per served option per target reading. Selecting the same option twice under one
    -- reading is one decision recorded twice, not two decisions.
    CONSTRAINT feature_selection_revision_once
        UNIQUE (target_reading_revision_id, considered_revision_id, option_id)
);

CREATE INDEX IF NOT EXISTS feature_selection_revision_by_reading
    ON feature_selection_revision (target_reading_revision_id);

-- ── append-only, enforced ────────────────────────────────────────────────────────────────────────
-- The whole point of both tables. A reading that can be updated is the UPDATE this migration
-- exists to replace; a selection that can be updated is a decision that can be rewritten after the
-- generation it authorized has run.
CREATE OR REPLACE FUNCTION selection_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% records are append-only (%)', TG_TABLE_NAME, OLD.revision_id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS target_reading_revision_no_update ON target_reading_revision;
CREATE TRIGGER target_reading_revision_no_update
    BEFORE UPDATE OR DELETE ON target_reading_revision
    FOR EACH ROW EXECUTE FUNCTION selection_revision_write_once();

DROP TRIGGER IF EXISTS feature_selection_revision_no_update ON feature_selection_revision;
CREATE TRIGGER feature_selection_revision_no_update
    BEFORE UPDATE OR DELETE ON feature_selection_revision
    FOR EACH ROW EXECUTE FUNCTION selection_revision_write_once();
