-- src/featuregen/db/migrations/1140_binding_chain_totality_closure.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task B2, FIX ROUND: the two holes 1135's
-- totality law left open, and one deferral 1135 took that it did not need.
--
-- RESERVATION. 1130-1139 were assigned to this plan by T0 at live head 1121 and are fully
-- allocated; 1140 was allocated to THIS closure by the reviewing coordinator on 2026-08-29 and is
-- recorded in the plan's §T0 mapping table in the same commit as this file. 1122-1129 remain the
-- remediation program's block and 1117 stays reserved-unused. Migration files apply lexically and
-- are checksummed by the ledger — immutable once merged or applied anywhere, INCLUDING the
-- documented persistent-FEATUREGEN_TEST_DSN mode. 1135 (the binding chain) and 1136 (the adoption
-- store) both precede this file lexically, so every table and function it re-points already exists.
--
-- ── WHAT 1135 GOT WRONG ─────────────────────────────────────────────────────────────────────
-- 1135 stated the member law as "a build member of a PLANNED selection must name a combined
-- binding", and derived "planned" from the presence of a `selection_formula_plan_binding`. That is
-- a marker-free rule, which was the appeal — and it is defeated twice, both times by ORDER rather
-- than by any missing check:
--
--   (A) WRITE THE MEMBER FIRST. `build_set_store` inserts a member with no combined binding; the
--       selection is not planned yet, so the law is satisfied. THEN the selection is bound to its
--       plan. The member is now an orphan of a planned selection, and both tables are append-only,
--       so it can never be repaired. 1135's trigger fired only AFTER INSERT ON build_set_member and
--       nothing ever re-checked members already written.
--
--   (B) SKIP THE LINK. Seed a planned option, bind it, seed and bind a draft, record 1101's
--       (selection, formula) pin — and simply never create the `selection_formula_plan_binding`.
--       Every commit check passes, because the member law's precondition IS the link that was
--       skipped. There was NO totality law at the selection link at all: the requirement propagated
--       option -> draft and stopped. Worse, the store then reported that governed selection as
--       "PRE-PLAN ... refused for cross-catalog generation" — a governed selection silently
--       misclassified as legacy, which is the failure mode this whole chain exists to prevent.
--
-- This file closes both, in 1135's own vocabulary — inherit-from-parent, checked at COMMIT where
-- ordering demands it, checked immediately where it does not.
--
-- ── 1. THE SELECTION LINK IS NOW TOTAL (closes B) ───────────────────────────────────────────
-- A `selection_formula_binding` (1101's pin) whose DRAFT carries a `formula_draft_plan_binding` must
-- have a `selection_formula_plan_binding` of its own by COMMIT. The requirement is INHERITED from
-- the draft, exactly as the draft's is inherited from its option — one declaration per lane, and no
-- second marker that could disagree with the first.
--
-- ▲ DEFERRED, AND HERE THE DEFERRAL IS REQUIRED, not merely convenient. `selection_formula_plan_
-- binding` carries a FOREIGN KEY onto `selection_formula_binding`, so the pin must exist BEFORE the
-- plan binding can be written. A non-deferred trigger would therefore fire at a moment when the row
-- it demands COULD NOT YET EXIST, and would refuse every legitimate flow. The cost is real and is
-- stated plainly: this trigger has no WHEN clause (its condition needs a subquery, which WHEN
-- forbids), so EVERY insert into `selection_formula_binding` queues a pending trigger event, and
-- Postgres refuses ALTER TABLE / TRUNCATE on a table with pending events. A transaction that
-- inserts a pin and then alters that table must run `SET CONSTRAINTS ALL IMMEDIATE` first to drain
-- the queue — which is valid precisely when the queued rows are pre-plan, and is exactly what a
-- legacy-shape migration audit seeds.
--
-- ▲ AND IT MAKES ONE DEMAND ON B2b: the 1101 pin and its plan binding must be recorded in ONE
-- transaction. That is not an inconvenience of the mechanism, it is the law — a pin that is planned
-- but not yet bound is the window construction (B) walked through.
CREATE OR REPLACE FUNCTION selection_formula_plan_binding_is_total() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM formula_draft_plan_binding d
               WHERE d.formula_draft_id = NEW.formula_draft_id)
       AND NOT EXISTS (SELECT 1 FROM selection_formula_plan_binding b
                       WHERE b.selection_revision_id = NEW.selection_revision_id
                         AND b.formula_draft_id = NEW.formula_draft_id) THEN
        RAISE EXCEPTION
            'selection % is pinned to formula draft %, which is bound to a logical plan, and the '
            'pair has no plan binding at COMMIT: a governed selection without one is reported as '
            'PRE-PLAN and refused for cross-catalog generation — a governed choice silently '
            'misclassified as legacy. Record the plan binding in the SAME transaction as the pin',
            NEW.selection_revision_id, NEW.formula_draft_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS selection_formula_binding_plan_binding_total ON selection_formula_binding;
CREATE CONSTRAINT TRIGGER selection_formula_binding_plan_binding_total
    AFTER INSERT ON selection_formula_binding
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION selection_formula_plan_binding_is_total();

-- ── 2. A SELECTION MAY NOT ENTER THE PLANNED LANE OVER AN ORPHANED MEMBER (closes A) ────────
-- The other direction of the member law, and the one 1135 never wrote: if a selection already has
-- build members carrying NO combined binding, it may not now be bound to a plan. Both tables are
-- append-only, so the orphan could never be repaired afterwards — refusing the binding is the only
-- moment at which anything can still be done about it, and the refusal says what to do.
--
-- ▲ NOT DEFERRED, deliberately. This trigger only ever looks BACKWARDS, at members that already
-- exist. A member written LATER in the same transaction is caught immediately by the member trigger
-- re-pointed below, so nothing is missed by checking now — and checking now queues no pending event
-- on `selection_formula_plan_binding` at all.
CREATE OR REPLACE FUNCTION selection_formula_plan_binding_has_no_orphan_members()
RETURNS trigger AS $$
DECLARE
    orphans integer;
BEGIN
    SELECT count(*) INTO orphans FROM build_set_member m
    WHERE m.selection_revision_id = NEW.selection_revision_id
      AND m.combined_binding_id IS NULL;
    IF orphans > 0 THEN
        RAISE EXCEPTION
            'selection % already has % build member(s) with no combined binding, so it cannot now '
            'be bound to a logical plan: those members are append-only and could never be given '
            'the pins they would then be required to carry. Declare a NEW build set whose members '
            'name their combined bindings',
            NEW.selection_revision_id, orphans;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS selection_formula_plan_binding_no_orphan_members
    ON selection_formula_plan_binding;
CREATE TRIGGER selection_formula_plan_binding_no_orphan_members
    AFTER INSERT ON selection_formula_plan_binding
    FOR EACH ROW EXECUTE FUNCTION selection_formula_plan_binding_has_no_orphan_members();

-- ── 3. THE MEMBER LAW NEEDS NO DEFERRAL (measured, not assumed) ─────────────────────────────
-- 1135 made the member check a DEFERRED constraint trigger on the assumption that a member and its
-- binding might be written in either order. They cannot be: `build_set_member_combined_pinned_v1`
-- (1135) is a plain, NON-DEFERRABLE foreign key, so the combined binding must already exist when
-- the member is inserted; and 1092 forbids UPDATE on `build_set_member`, so the column can never be
-- filled in afterwards. The deferral bought nothing and cost the queued pending events that made
-- `ALTER TABLE build_set_member` refuse inside a transaction that had inserted into it.
--
-- Re-pointed to a plain AFTER INSERT trigger, running the SAME function 1135 defined — the law is
-- unchanged, only the moment it is enforced moves earlier, which is strictly better: the refusal
-- now names the statement that caused it.
DROP TRIGGER IF EXISTS build_set_member_combined_binding_total ON build_set_member;
CREATE TRIGGER build_set_member_combined_binding_total
    AFTER INSERT ON build_set_member
    FOR EACH ROW EXECUTE FUNCTION build_member_combined_binding_is_total();
