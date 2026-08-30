-- src/featuregen/db/migrations/1141_draft_plan_binding_rechecks_its_pins.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task B2, FIX ROUND 2: the one construction that
-- survived 1140 — construction (B)'s end state, reached ACROSS TWO TRANSACTIONS.
--
-- RESERVATION. 1130-1139 were assigned by T0 at live head 1121; 1140 was allocated on 2026-08-29 by
-- the reviewing coordinator for the first totality closure; **1141 was allocated on 2026-08-29 by
-- the same ruling for this one**, and is recorded in the plan's §T0 mapping table in the same commit
-- as this file. 1122-1129 remain the remediation program's block and 1117 stays reserved-unused.
-- Migration files apply lexically and are checksummed by the ledger — immutable once merged or
-- applied anywhere, INCLUDING the documented persistent-FEATUREGEN_TEST_DSN mode. 1135, 1136 and
-- 1140 all precede this file lexically, so every table and trigger it completes already exists.
--
-- ── THE CONSTRUCTION 1140 DID NOT CLOSE ─────────────────────────────────────────────────────
-- 1140 gave the selection link two guards, and they are not symmetric:
--
--   trigger 1 (DEFERRED, on `selection_formula_binding`)  — when a PIN is written, its draft must
--       already be plan-bound OR not plan-bound at all. It looks at the world as it stands at that
--       transaction's COMMIT, and it never runs again.
--   trigger 2 (immediate, on `selection_formula_plan_binding`) — the BACKWARD-LOOKING re-check for
--       construction (A): a selection may not enter the planned lane over members already written.
--
-- The gap: 1140 added a backward-looking re-check for (A) and no symmetric one for (B). So:
--
--   TXN1  an UNMARKED option, a draft, a selection, a legal pre-plan 1101 pin, and a build member
--         with a NULL combined binding. Every check passes — correctly, because nothing here is
--         planned — and trigger 1 is DISCHARGED at this commit.
--   TXN2  write `considered_option_plan_binding` by raw SQL (skipping the store's arming check),
--         then call `bind_formula_draft_plan`, which is an ORDINARY store call that reads no
--         marker. The draft is now plan-bound. Nothing re-checks TXN1's pin, because trigger 1
--         fires on INSERT into the pin table and that insert is in the past.
--
-- Measured end state: plan bindings 0, orphan members 1, and `require_planned_selection` reporting
-- a governed selection as PRE-PLAN — construction (B) exactly, reached the long way round. Across
-- transactions the closure rested on ONE store check (`bind_considered_option_plan`'s arming
-- refusal), which is precisely the "correct on the day it is written, bypassed by the next caller"
-- posture this task exists to replace. A law that holds only while one function stays the sole
-- writer is a convention, not a law.
--
-- ── THE CLOSURE ─────────────────────────────────────────────────────────────────────────────
-- One trigger, mirroring 1140's trigger 2 in DIRECTION: when a draft becomes plan-bound, re-check
-- the pins that already name it. After this file, the moment a draft enters the planned lane is a
-- moment at which every 1101 pin for that draft must acquire its `selection_formula_plan_binding` —
-- no matter which transaction wrote the pin, and no matter which caller wrote the draft binding.
--
-- ▲ DEFERRED, AND THE DEFERRAL IS REQUIRED — for the same structural reason as 1140's trigger 1,
-- not by preference. `selection_formula_plan_binding` carries a FOREIGN KEY onto
-- `formula_draft_plan_binding (formula_draft_id, logical_digest)`, so the draft binding must exist
-- BEFORE the selection plan binding can be written. At the instant this trigger's row is inserted,
-- the row it demands COULD NOT YET EXIST. An immediate check would refuse the legitimate in-order
-- flow — pin first, then bind the draft, then bind the selection — which is the ordering the
-- journey actually takes. Checked at COMMIT, that flow passes and the two-transaction construction
-- above does not.
--
-- ▲ THE PENDING-EVENT COST IS NEGLIGIBLE HERE, unlike 1140's trigger 1. That one sits on
-- `selection_formula_binding`, which every legacy pin in the platform passes through, so it queues
-- an event for rows that will never be planned. This one sits on `formula_draft_plan_binding`, a
-- table that ONLY ever receives planned-lane rows — so every event it queues is one we want
-- checked, and no pre-plan transaction is affected at all. The same escape hatch applies if a
-- planned-lane transaction ever needs to ALTER the table: `SET CONSTRAINTS ALL IMMEDIATE` drains
-- the queue by RUNNING the check, which is the point rather than a way around it.

CREATE OR REPLACE FUNCTION formula_draft_plan_binding_rechecks_its_pins() RETURNS trigger AS $$
DECLARE
    unbound integer;
BEGIN
    SELECT count(*) INTO unbound
    FROM selection_formula_binding p
    WHERE p.formula_draft_id = NEW.formula_draft_id
      AND NOT EXISTS (SELECT 1 FROM selection_formula_plan_binding b
                      WHERE b.selection_revision_id = p.selection_revision_id
                        AND b.formula_draft_id = p.formula_draft_id);
    IF unbound > 0 THEN
        RAISE EXCEPTION
            'formula draft % became bound to a logical plan while % selection pin(s) for it still '
            'have no plan binding at COMMIT: those selections would be governed and yet reported '
            'PRE-PLAN and refused for cross-catalog generation. Bind every pinned selection in the '
            'SAME transaction that binds the draft, or bind the draft before the pins are made',
            NEW.formula_draft_id, unbound;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS formula_draft_plan_binding_pins_total ON formula_draft_plan_binding;
CREATE CONSTRAINT TRIGGER formula_draft_plan_binding_pins_total
    AFTER INSERT ON formula_draft_plan_binding
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION formula_draft_plan_binding_rechecks_its_pins();
