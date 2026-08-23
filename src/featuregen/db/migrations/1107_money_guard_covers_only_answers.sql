-- src/featuregen/db/migrations/1107_money_guard_covers_only_answers.sql
-- THE MONEY GUARD COVERS ANSWERS, not failures — because a failed draft bought nothing.
--
-- WHY. 1090's unique index over formula_identity_hash covers EVERY row, so a terminal FAILED or
-- CANCELLED draft occupies its identity slot for ever: `formula_draft` is append-only (no DELETE)
-- and terminal states have empty transition sets (no resurrection). The regeneration exception
-- (1103) binds the EXACT identity being re-requested — and the INSERT it authorizes then loses to
-- the failed row, unconditionally. The adversarial review put it precisely: the remedy the refusal
-- names is structurally unreachable, and the operator who presents the approved exception has one
-- use burned per click while the platform tells them to go obtain the thing they are holding.
--
-- THE RULE (§11.1.2): the guard asks "have we already BOUGHT this answer?". READY bought one;
-- BLOCKED is a verdict about the candidate, which re-buying cannot change; a live draft is a
-- purchase in flight. FAILED and CANCELLED bought NOTHING — so they stay as history and stop
-- holding the slot.
--
-- ▲ A NEW FILE, not an edit: 1090 is applied to the live cluster and applied migrations are
-- immutable — the ledger's checksum guard would refuse the drift. Measured before writing: live
-- holds 7 drafts (4 FAILED, 3 BLOCKED), all with DISTINCT identity hashes, so the narrower index
-- cannot fail to build. ▲ Re-measure in this transaction all the same — the rule governs, not the
-- reading:
--
--     SELECT formula_identity_hash FROM formula_draft
--     WHERE state NOT IN ('FAILED','CANCELLED')
--     GROUP BY 1 HAVING count(*) > 1;   -- must return zero rows
--
-- ▲ ONE MORE CONSEQUENCE, stated so nobody discovers it as a mystery: `request_draft`'s INSERT must
-- name this index's predicate in its ON CONFLICT clause, or Postgres cannot infer the arbiter.

DROP INDEX IF EXISTS formula_draft_identity;

CREATE UNIQUE INDEX IF NOT EXISTS formula_draft_identity
    ON formula_draft (formula_identity_hash)
    WHERE state NOT IN ('FAILED', 'CANCELLED');
