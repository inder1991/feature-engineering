-- src/featuregen/db/migrations/1112_formula_method_override.sql
-- §11.3 — "Try AI formula" as a SERVER-AUTHORED OVERRIDE, never a client-chosen method (P0-9).
--
-- Two rules that are each right and, as request fields, contradictory: after a deterministic
-- refusal the user may explicitly ask for an LLM retry (child D2), and the client must never
-- supply a formula method (parent §7). Both survive because the override is THIS durable record:
-- the browser asks; the server VERIFIES that the deterministic refusal it names actually
-- happened — recorded on a draft, by code — and the strategy resolver then consumes the revision
-- as an INPUT FACT. The resolver remains the only component that decides a method: the override
-- changes the EVIDENCE, never the authority.
--
-- ▲ EXPIRY, because a refusal AGES: the blueprint may be fixed in the meantime, and the correct
-- answer then is the deterministic one — which an unexpiring override would quietly override.
-- ▲ The spend authorization is NOT NULL: overriding to the LLM authorizes BUYING an answer, and
-- §11.2 makes spend an authorized act, never a side effect of a retry button.

CREATE TABLE IF NOT EXISTS formula_method_override_revision (
    override_id                 text PRIMARY KEY CHECK (btrim(override_id) <> ''),

    -- WHAT it applies to: the CANDIDATE (§0.1.4's authoring subject), plus the exact refused
    -- draft whose recorded refusal the server verified. A new request for this candidate under
    -- the override mints a NEW draft identity (child D2) — the override never re-labels this one.
    considered_revision_id      text NOT NULL CHECK (btrim(considered_revision_id) <> ''),
    option_id                   text NOT NULL CHECK (btrim(option_id) <> ''),
    refused_formula_draft_id    text NOT NULL REFERENCES formula_draft(formula_draft_id),

    -- The refusal being overridden — CLOSED to the one deterministic refusal that exists. A code
    -- outside the set is a different problem with a different remedy, not an LLM retry.
    original_refusal_code       text NOT NULL
        CONSTRAINT formula_method_override_names_the_refusal CHECK (
            original_refusal_code IN ('REVIEWED_BLUEPRINT_NOT_EXECUTABLE')),
    requested_alternative       text NOT NULL
        CONSTRAINT formula_method_override_alternative_v1 CHECK (
            requested_alternative IN ('LLM_AUTHORED')),

    actor_subject               text NOT NULL CHECK (btrim(actor_subject) <> ''),
    reason                      text NOT NULL CHECK (btrim(reason) <> ''),
    llm_spend_authorization_id  text NOT NULL
        REFERENCES llm_spend_authorization_revision(spend_authorization_id),
    approved_at                 timestamptz NOT NULL DEFAULT now(),
    expires_at                  timestamptz NOT NULL,
    CONSTRAINT formula_method_override_expires_after_approval CHECK (expires_at > approved_at)
);

CREATE INDEX IF NOT EXISTS formula_method_override_by_candidate
    ON formula_method_override_revision (considered_revision_id, option_id, expires_at);

CREATE OR REPLACE FUNCTION formula_method_override_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'formula_method_override_revision is append-only: it is an APPROVAL somebody '
                    'gave, and an approval that can be rewritten is not an approval';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS formula_method_override_no_change ON formula_method_override_revision;
CREATE TRIGGER formula_method_override_no_change
    BEFORE UPDATE OR DELETE ON formula_method_override_revision
    FOR EACH ROW EXECUTE FUNCTION formula_method_override_write_once();
