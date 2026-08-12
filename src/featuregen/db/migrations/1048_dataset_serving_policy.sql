-- src/featuregen/db/migrations/1048_dataset_serving_policy.sql
-- Release-B profile Task 7 (reservation: verified-interfaces doc D7 — 1048 is the serving policy
-- store; 1049 is the temporal one; this task allocates NOTHING else).
--
-- WHICH COPY of a dataset may serve a need, declared explicitly. Immutable revisions + a CAS
-- current pointer, the same shape as 1047's catalog narrative and the bridge_store pointer_version
-- discipline. The revision id is DETERMINISTIC from canonical content ('dsp_' + the RFC-8785 JCS
-- hash of the key, both ref sets and the D2 authority axes), so a byte-identical re-declaration
-- resolves to the SAME revision and never churns a source selection that references it.
--
-- WHY A POLICY IS ITS OWN OPERATIONAL DECLARATION (plan §6.4 / D12.7): an upload-only catalog has
-- no source-attested authority_role at all. Without this table the only way to make such a catalog
-- selectable would be to promote an LLM proposal to load-bearing. The policy is the alternative:
-- a person declares it through the four-eyes admin route, and `authored_by`/`declared_by` record
-- who — which is provenance, deliberately OUTSIDE content identity (§6.2's split).
--
-- THE KEY IS (entity_id, need_role, serving_purpose). Not the dataset: the policy answers "which
-- dataset serves THIS need", so keying it by dataset would invert the question and make "no policy"
-- indistinguishable from "no dataset".
--
-- NO FRESHNESS COLUMN, deliberately (§6.4): no delivery-SLA or observation contract exists yet, and
-- a freshness value inside a decision identity would re-key every replay when data merely arrived
-- later. `source_selection.reject_freshness_keys` enforces the same rule in code.

CREATE TABLE IF NOT EXISTS dataset_serving_policy_revision (
    revision_id            text        PRIMARY KEY
        CHECK (revision_id ~ '^dsp_[0-9a-f]{64}$'),
    entity_id              text        NOT NULL,
    need_role              text        NOT NULL
        CHECK (need_role IN ('population', 'event_source', 'dimension_source',
                             'reference', 'mapping')),
    serving_purpose        text        NOT NULL
        CHECK (serving_purpose IN ('feature_serving', 'analytical')),
    -- jsonb arrays of normalized TABLE logical refs; sorted, deduped and re-validated in code
    -- before every insert (preferred is a subset of eligible — the contract enforces it).
    eligible_dataset_refs  jsonb       NOT NULL
        CHECK (jsonb_typeof(eligible_dataset_refs) = 'array'
               AND jsonb_array_length(eligible_dataset_refs) > 0),
    preferred_dataset_refs jsonb       NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(preferred_dataset_refs) = 'array'),
    -- The FULL PolicyProvenanceV1 (D2 triples + governed decision refs). Only the axes are inside
    -- content_hash; this column carries the rest.
    provenance             jsonb       NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(provenance) = 'object'),
    content_hash           text        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- Persistence PROVENANCE, outside identity.
    authored_by            text        NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dataset_serving_policy_revision_key_idx
    ON dataset_serving_policy_revision (entity_id, need_role, serving_purpose);

CREATE TABLE IF NOT EXISTS dataset_serving_policy_current (
    entity_id        text        NOT NULL,
    need_role        text        NOT NULL,
    serving_purpose  text        NOT NULL,
    revision_id      text        NOT NULL REFERENCES dataset_serving_policy_revision(revision_id),
    pointer_version  integer     NOT NULL CHECK (pointer_version >= 1),
    -- WHO made this revision current. A byte-identical re-declaration by a second admin REUSES the
    -- revision (identity is content), so without this column the act of making it current would
    -- leave no trace of who performed it.
    declared_by      text        NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, need_role, serving_purpose)
);
