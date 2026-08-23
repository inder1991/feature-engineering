-- src/featuregen/db/migrations/1093_executable_policy_payload.sql
-- The CONTENT a policy realization points at — the thing the renderer actually needs.
--
-- THE GAP THIS CLOSES. `policy_realization_revision` (1075) records `executable_content_hash` and
-- `cas_pointer`: an address, and a promise that content lives there. Nothing stored the content.
-- So a realization could say
--
--     eligible_status_policy_hash = abc123
--
-- and no renderer could turn that into
--
--     WHERE transaction_status IN ('POSTED', 'SETTLED')
--
-- A hash NAMES a decision. It is not the decision. Every governed policy in this platform was
-- therefore decidable and un-renderable at the same time.
--
-- CONTENT-ADDRESSED, so the address IS the integrity check. A payload's id is the hash of its own
-- canonical bytes, which means a realization pointing at `abc123` either finds bytes that hash to
-- `abc123` or finds nothing — there is no third case where it finds different bytes wearing the
-- right name. That is why the primary key is the hash rather than a minted id.
--
-- IMMUTABLE, by trigger. A policy payload that could be edited would silently change the meaning of
-- every artifact already sealed against it: a feature verified under "POSTED, SETTLED" would quietly
-- become a feature about "POSTED, SETTLED, PENDING" with its verification still green. Changing a
-- policy mints a new payload and a new realization; the old one stays because things were built on
-- it.
--
-- TYPED BY KIND, and the kinds are closed. Each shape has different required fields — a status
-- policy needs values and a column, an FX policy needs a rate relation, keys, a time column, a quote
-- convention and a missing-rate behaviour. Storing them as untyped JSON with no kind would make
-- "which fields must be present" unanswerable, and a payload missing the field the renderer reaches
-- for is a silent default in waiting.
--
-- NO DEFAULTS ANYWHERE. Every declared policy resolves to content or causes a named refusal. A
-- defaulted policy is a wrong number wearing a governed costume: the artifact says a decision was
-- applied, and the decision was invented at render time by whoever wrote the fallback.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS executable_policy_payload (
    -- The hash of the canonical payload bytes. PK because the address IS the integrity check.
    content_hash   text PRIMARY KEY CHECK (btrim(content_hash) <> ''),

    -- WHICH SHAPE. Closed, because each kind has different required fields and a reader that cannot
    -- tell which kind it is holding cannot know which fields to demand.
    policy_kind    text NOT NULL CHECK (policy_kind IN (
                       'eligible_status',      -- which status values count
                       'direction',            -- debit/credit mapping
                       'reversal',             -- linkage column and survivor rule
                       'currency_conversion'   -- rate relation, keys, convention, missing-rate rule
                   )),

    -- The payload's own schema version, separate from the content. A shape that gains a field is a
    -- new version; the same decision re-expressed under a new shape is a DIFFERENT payload with a
    -- different hash, and both may legitimately exist while artifacts sealed under each survive.
    payload_version integer NOT NULL CHECK (payload_version >= 1),

    payload_json   jsonb NOT NULL,

    -- WHO put this content here. A payload is a governed decision; one with no author is a decision
    -- nobody can be asked about.
    recorded_by    text NOT NULL CHECK (btrim(recorded_by) <> ''),
    recorded_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS executable_policy_payload_by_kind
    ON executable_policy_payload (policy_kind, payload_version);

-- ── immutable, and that is the whole point ───────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION executable_policy_payload_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'executable_policy_payload is immutable: changing a policy mints a NEW payload, '
                    'because artifacts already sealed against this one were verified under exactly '
                    'these bytes';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS executable_policy_payload_no_change ON executable_policy_payload;
CREATE TRIGGER executable_policy_payload_no_change
    BEFORE UPDATE OR DELETE ON executable_policy_payload
    FOR EACH ROW EXECUTE FUNCTION executable_policy_payload_immutable();
