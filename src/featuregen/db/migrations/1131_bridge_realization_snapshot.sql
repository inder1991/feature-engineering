-- src/featuregen/db/migrations/1131_bridge_realization_snapshot.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task A4: the dependency-snapshot store —
-- one IMMUTABLE, content-addressed record of the realization state a planning request saw for
-- its COMPLETE considered set of cross-catalog links. This is what R11's
-- `assess_realization_for_preview(..., pinned_dependency_snapshot_id, ...)` will name once the
-- assessment consumes snapshots (the B2b seam): the assessment can later re-read EXACTLY what
-- planning saw, instead of re-running the reviewer-proven per-realization multi-query pattern
-- (`executable_bridge_realizations` / `revalidate_bridge_realization`'s per-row reads).
--
-- RESERVATION. 1130-1139 were assigned to this plan by T0 at live head 1121 (1122-1129 belong to
-- the remediation program's registry). The §T0 mapping row for 1131 is "dependency-snapshot
-- store" (task A4). Migration files apply lexically and are checksummed by the ledger —
-- immutable once merged or applied anywhere. 1130 (execution_context_revision) precedes this
-- file lexically, so the context revisions this table's rows name already have their store.
--
-- Shape decisions:
--  * APPEND-ONLY (the 1034/1060/1062/1120/1130 guard idiom: a row-level BEFORE UPDATE OR DELETE
--    trigger and a statement-level BEFORE TRUNCATE trigger sharing one plpgsql raiser). A
--    snapshot is the frozen read moment other records pin by id; editing it would silently
--    change what an assessment believes planning saw.
--  * CONTENT-ADDRESSED: snapshot_id = 'brsnap_' || sha256(canonical captured payload), minted by
--    the store (the ecx_/brds_/dtp_/jvp_ family). The identity covers the captured entries, the
--    scope scalars and the truncation VERDICT (cause + truncated keys + cap value) — never
--    recorded_at and never the truncation's wall-clock elapsed_note, which is disclosure, not
--    identity (rounds 9/14: a wall-clock deadline does not produce deterministic truncation, so
--    the clock never enters a hash). content_hash is UNIQUE so the same captured state can never
--    be smuggled in under a second id.
--  * TRUNCATION IS PERSISTED AND DISCLOSED (rounds 9/14): `truncation` records the cause
--    ('none' | 'cap' | 'deadline' | 'cap_and_deadline'), the truncated bridge keys in the
--    builder's pinned stable order, the cap value, and the elapsed note. A snapshot whose cause
--    is not 'none' is INCOMPLETE and loads with complete=false — nothing downstream may treat it
--    as the whole considered set.
--  * SCOPE SCALARS beside the jsonb: execution_tier / purpose reuse the same closed spellings as
--    1130 (ExecutionTier's persisted lowercase values; the step-3/4 purpose vocabulary);
--    execution_context_revision_id optionally pins A3's server-owned context revision — nullable
--    because context adoption is wired at B2b (honest absence, never a fabricated ref). It is
--    DELIBERATELY not a FOREIGN KEY: execution_context_revision is append-only (its rows never
--    vanish, so referential decay is impossible), the store verifies existence AND tier/purpose
--    agreement before writing, and an FK would make Postgres refuse TRUNCATE on the REFERENCED
--    table with FeatureNotSupported BEFORE that table's own append-only raiser fires — silently
--    re-contracting 1130's guard. The 1130 pattern (store validation over a fabricated FK) is
--    reused.

CREATE TABLE IF NOT EXISTS bridge_realization_snapshot (
    snapshot_id                   text        PRIMARY KEY,
    execution_context_revision_id text        NULL,
    execution_tier                text        NOT NULL,
    purpose                       text        NOT NULL,
    captured                      jsonb       NOT NULL,
    truncation                    jsonb       NOT NULL,
    content_hash                  text        NOT NULL,
    recorded_at                   timestamptz NOT NULL DEFAULT now(),

    -- ExecutionTier's persisted value spellings, closed (the 1130 convention).
    CONSTRAINT bridge_realization_snapshot_tier_chk CHECK (
        execution_tier IN ('sandbox', 'production')),
    -- The step-3/4 purpose vocabulary, closed.
    CONSTRAINT bridge_realization_snapshot_purpose_chk CHECK (
        purpose IN ('feature_generation')),
    -- The captured state is an ORDERED ARRAY of per-bridge entries; truncation is one object.
    CONSTRAINT bridge_realization_snapshot_captured_chk CHECK (
        jsonb_typeof(captured) = 'array'),
    CONSTRAINT bridge_realization_snapshot_truncation_chk CHECK (
        jsonb_typeof(truncation) = 'object'),
    CONSTRAINT bridge_realization_snapshot_content_hash_key UNIQUE (content_hash)
);

-- ── append-only guards (one raiser; it names the table and the reason) ─────────────────────────
-- TG_OP is assigned in statement-level TRUNCATE triggers; OLD/NEW are NOT, which is why the
-- raiser touches neither.
CREATE OR REPLACE FUNCTION bridge_realization_snapshot_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'bridge_realization_snapshot is append-only: % is not allowed. A snapshot is the frozen '
        'read moment a planning request pinned — rewriting it would change what an assessment '
        'believes planning saw. Record a NEW snapshot instead.', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER bridge_realization_snapshot_no_mutation
    BEFORE UPDATE OR DELETE ON bridge_realization_snapshot
    FOR EACH ROW EXECUTE FUNCTION bridge_realization_snapshot_append_only();
-- A FOR EACH ROW trigger does not fire on TRUNCATE; this is the only guard that does.
CREATE OR REPLACE TRIGGER bridge_realization_snapshot_no_truncate
    BEFORE TRUNCATE ON bridge_realization_snapshot
    FOR EACH STATEMENT EXECUTE FUNCTION bridge_realization_snapshot_append_only();
