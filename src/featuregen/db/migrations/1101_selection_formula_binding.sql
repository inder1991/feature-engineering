-- src/featuregen/db/migrations/1101_selection_formula_binding.sql
-- A BUILD SET PINS ITS FORMULA — and the pin PROVES the formula belongs to the selection.
--
-- WHY THIS EXISTS. `build_set_member` was `(revision_id, position, selection_revision_id)` and
-- nothing else, and 1092's own comment argued that was right: "the formula it resolves to is looked
-- up through it rather than copied, so a set cannot drift from the choice it records". ▲ THAT
-- REASONING IS INVERTED. The selection is stable; the DRAFT is not. `restore_formula_v3` resolves it
-- with `ORDER BY d.updated_at DESC LIMIT 1` — the newest draft for this selection, whenever the
-- worker happens to look — so a new draft landing between the decision and the build changes what
-- the build MEANS while `build_set_revision.content_hash` stays put. Same identity, different
-- feature, and a retry of a failed build silently builds something else.
--
-- ▲ AND A LOOSE PAIR OF COLUMNS WOULD NOT HAVE BEEN ENOUGH. Adding `formula_draft_id` and
-- `formula_content_hash` to the member stops latest-draft-wins and still permits this:
--
--     member selects                     Feature A
--     pinned formula is a valid formula for   Feature B
--     the id exists - the hash matches - the build proceeds
--
-- Nothing forces the two to agree, because they are independent values on one row. Worker-side
-- validation is the kind of check that is correct on the day it is written and bypassed by the next
-- caller. So the agreement is made UNREPRESENTABLE instead: composite foreign keys into BOTH source
-- identities, and the row can only exist when the draft and the selection already agree on
-- candidate, considered revision, option, planning request AND the formula's own content.
--
-- ▲ WHY `binding_plan_hash` AND `planning_request_hash` ARE IN THE SELECTION KEY. Both are immutable
-- columns of `feature_selection_revision` (1072). Without them a binding could pair a selection with
-- a formula authored under a DIFFERENT question — the draft would satisfy its own key and the
-- selection would satisfy its own, and the two would describe different work.
--
-- ▲ WHY `formula_content_hash` IS `NOT NULL` HERE THOUGH IT IS NULLABLE ON THE DRAFT. Under the
-- default MATCH SIMPLE, a composite foreign key with ANY null column is NOT ENFORCED AT ALL — so a
-- binding carrying a null content hash would silently skip the check it exists for. Requiring it is
-- also correct independently: a binding may only be created for a draft that produced a formula.
--
-- MEASURED BEFORE WRITING (live kind, 2026-08-22): `feature_selection_revision` 0,
-- `build_set_member` 0, `build_set_revision` 0. So the member column lands NOT NULL with no
-- nullable phase and no backfill — there is nothing to grandfather, and a backfill that chose
-- "the latest draft" would fabricate a choice nobody made. ▲ RE-MEASURE IN THE TRANSACTION THAT
-- APPLIES THIS: the rule governs, not the reading.

-- ── the parents need keys the composite references can point at ──────────────────────────────────
-- Both are supersets of an existing primary key, so they are satisfied by construction and cost
-- only the index. Neither can fail on existing data.
CREATE UNIQUE INDEX IF NOT EXISTS feature_selection_revision_binding_key
    ON feature_selection_revision (revision_id, considered_revision_id, option_id,
                                   planning_request_hash, binding_plan_hash);

CREATE UNIQUE INDEX IF NOT EXISTS formula_draft_binding_key
    ON formula_draft (formula_draft_id, considered_revision_id, option_id,
                      planning_request_hash, formula_content_hash);

-- ── the binding ─────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS selection_formula_binding (
    binding_id             text PRIMARY KEY CHECK (btrim(binding_id) <> ''),

    selection_revision_id  text NOT NULL,
    formula_draft_id       text NOT NULL,
    formula_content_hash   text NOT NULL CHECK (btrim(formula_content_hash) <> ''),

    -- Shared by both composite keys below, which is what forces the draft and the selection to
    -- agree: one column, two parents, no way to satisfy them with different values.
    considered_revision_id text NOT NULL,
    option_id              text NOT NULL,
    planning_request_hash  text NOT NULL,
    binding_plan_hash      text NOT NULL,

    recorded_at            timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT selection_formula_binding_selection_agrees
        FOREIGN KEY (selection_revision_id, considered_revision_id, option_id,
                     planning_request_hash, binding_plan_hash)
        REFERENCES feature_selection_revision (revision_id, considered_revision_id, option_id,
                                               planning_request_hash, binding_plan_hash),

    CONSTRAINT selection_formula_binding_formula_agrees
        FOREIGN KEY (formula_draft_id, considered_revision_id, option_id,
                     planning_request_hash, formula_content_hash)
        REFERENCES formula_draft (formula_draft_id, considered_revision_id, option_id,
                                  planning_request_hash, formula_content_hash),

    -- One binding per (selection, draft) pair: asking twice is the same binding, not a second one.
    CONSTRAINT selection_formula_binding_once
        UNIQUE (selection_revision_id, formula_draft_id)
);

-- ▲ THE KEY `build_set_member` NEEDS. The member keeps `selection_revision_id` — every reader uses
-- it — so the member now holds the selection TWICE: once directly, once through the binding. That is
-- the very shape this migration exists to forbid, one level up. This unique key lets the member
-- reference BOTH together, so the two can never name different selections.
CREATE UNIQUE INDEX IF NOT EXISTS selection_formula_binding_member_key
    ON selection_formula_binding (binding_id, selection_revision_id);

CREATE INDEX IF NOT EXISTS selection_formula_binding_by_draft
    ON selection_formula_binding (formula_draft_id);

-- ▲ APPEND-ONLY. A binding that can be edited is a pin that moves, which is the defect.
CREATE OR REPLACE FUNCTION selection_formula_binding_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'selection_formula_binding is append-only: a pin that can be edited after the '
                    'build was decided is not a pin';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS selection_formula_binding_no_change ON selection_formula_binding;
CREATE TRIGGER selection_formula_binding_no_change
    BEFORE UPDATE OR DELETE ON selection_formula_binding
    FOR EACH ROW EXECUTE FUNCTION selection_formula_binding_write_once();

-- ── the member references the binding, not two loose values ─────────────────────────────────────
ALTER TABLE build_set_member
    ADD COLUMN IF NOT EXISTS selection_formula_binding_id text;

-- Zero rows measured, so this is immediate. See the header for why no backfill exists.
ALTER TABLE build_set_member
    ALTER COLUMN selection_formula_binding_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'build_set_member_formula_pinned_v1') THEN
        -- ▲ THE VERSION IS IN THE NAME ON PURPOSE. A later rule — pinning the admission identity
        -- too — is `..._pinned_v2`, added beside this one, so "which pinning rule did this row
        -- satisfy" keeps one answer per constraint rather than a redefinition nobody can date.
        ALTER TABLE build_set_member
            ADD CONSTRAINT build_set_member_formula_pinned_v1
            FOREIGN KEY (selection_formula_binding_id, selection_revision_id)
            REFERENCES selection_formula_binding (binding_id, selection_revision_id);
    END IF;
END $$;
