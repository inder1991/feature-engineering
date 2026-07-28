-- 1032 — read scope must honour the GOVERNED floor, not only the raw file tag.
--
-- THE DEFECT. Every read-scope predicate filtered `graph_node.sensitivity` — the RAW, file-declared
-- tag — under the rule "untagged is always visible". The governed, concept-derived floor is written
-- to a DIFFERENT column, `effective_restriction` (field_resolution.apply_sensitivity_floor), whose
-- only reader was materialize/classify.py. So the floor never restricted catalog visibility.
--
-- Measured on the deployed FTR catalog (2026-07-28): 126/126 columns carry `sensitivity = NULL`,
-- because a business glossary attests no sensitivity, while the concept cascade had ruled 28 of them
-- restricted/confidential — 16 restricted (customer names, addresses, phone numbers, free-text
-- narration, an Emirates ID number) and 12 confidential (sender/beneficiary/correspondent-bank
-- countries). All 28 were readable by any caller holding `catalog:read`. The system correctly
-- identified a national ID as sensitive and then showed it to everyone.
--
-- THE FIX. One generated column carrying the SET of visibility classes a reader must hold, derived
-- from BOTH axes, so the rule lives in exactly one place and every call site is a single clause:
--
--     WHERE ... AND visible_requires <@ %s        -- %s = the caller's allowed classes
--
-- Array containment gives the semantics we actually want:
--   * `{}` (no tag, no floor, or a floor of public/internal) is contained by ANY array, including
--     the empty one -> world-visible, exactly as before. The common case does not change.
--   * `{pii}` requires `pii`; `{restricted}` requires `restricted`.
--   * `{pii,restricted}` requires BOTH. The two axes are independent evidence and the stricter does
--     not absorb the other — holding `restricted_reader` alone must never unlock a pii-tagged
--     column, which a single "strictest class" column could not express without weakening it.
--   * `{prohibited}` can never be satisfied: no role maps to it (read_scope.SENSITIVITY_ROLES), so
--     it is absent from every allowed list. `prohibited` is also the rank an UNRECOGNIZED floor
--     label collapses to (safety_floor), so an unknown value fails closed rather than open.
--
-- GENERATED ... STORED, so it cannot drift from its inputs: there is no writer to forget, no
-- backfill to run, and a re-upload or a re-resolution recomputes it in the same statement that
-- changes the source column.
--
-- `sensitivity` and `effective_restriction` both REMAIN, unchanged and separately readable. They are
-- different facts — one is what the file attested, the other is what governance resolved — and
-- drift detection compares attestations. This migration adds a derived view of them for
-- authorization only; it does not merge them.

-- PRECEDENCE, and why it is not "require both".
--
-- The two columns are NOT independent evidence: `effective_restriction` is DERIVED from the raw tag
-- among other inputs (field_resolution.apply_sensitivity_floor resolves the concept-derived floor
-- together with the declared-sensitivity proposals). Requiring both therefore double-counts the same
-- fact — and it measurably breaks a shipped guarantee: a `pii`-tagged column whose floor resolves to
-- `restricted` would suddenly demand `restricted_reader` on top of `pii_reader`, so a caller who can
-- see PII today would lose access.
--
-- So the floor acts as a FALLBACK, which is exactly the shape of the defect it closes: the leaking
-- columns are the ones the file never tagged. Where a tag exists, behaviour is bit-for-bit what it
-- was. The single exception is `prohibited`, which always wins because nothing may unlock it.
ALTER TABLE graph_node
    ADD COLUMN IF NOT EXISTS visible_requires text[]
    GENERATED ALWAYS AS (
        CASE
            -- Nothing unlocks `prohibited` — including the rank an UNRECOGNIZED floor label
            -- collapses to (safety_floor), so an unknown value fails closed.
            WHEN effective_restriction = 'prohibited' THEN ARRAY['prohibited']
            -- A file-declared tag keeps its own gate, unchanged. Vocabulary {pii, restricted},
            -- pinned by the 0993 CHECK constraint.
            WHEN sensitivity IN ('pii', 'restricted') THEN ARRAY[sensitivity]
            -- Untagged: the governed floor decides. THIS is the leak that was open — a glossary
            -- attests no sensitivity, so every such column fell through to "untagged is visible".
            -- `public`/`internal` impose no requirement; internal means "any authenticated catalog
            -- reader", which is the population this filter already scopes.
            WHEN effective_restriction IN ('confidential', 'restricted') THEN ARRAY[effective_restriction]
            ELSE ARRAY[]::text[]
        END
    ) STORED;

-- GIN supports the `<@` containment operator used by every read-scope predicate.
CREATE INDEX IF NOT EXISTS graph_node_visible_requires_idx
    ON graph_node USING GIN (visible_requires);
