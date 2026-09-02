-- src/featuregen/db/migrations/1142_target_label_registry.sql
--
-- Derived target labels (spec 2026-09-01). A training label is normally CONSTRUCTED, not stored:
-- the deployed catalogs carry 237 columns, 100% concept coverage, and ZERO in the outcome family,
-- because the labels these users need were never going to be sitting there.
--
-- RESERVATION. 1130-1139 are the cross-catalog serving program's block and 1140/1141 were
-- allocated on 2026-08-29; 1142 is allocated here, on 2026-09-01, for this registry. Migration
-- files apply lexically and are checksummed by the ledger — immutable once applied anywhere.
--
-- Mirrors the feature registry (`feature` / `feature_definition` / `feature_derives_from` /
-- `feature_consumer`) because the owner asked for reuse across models "similar to the feature
-- registry", and that pattern already carries every property a label needs.
--
-- DEVIATION, stated: the spec lists four tables; this creates three, folding `target` into
-- `target_definition`. The feature registry separates them because a feature keeps one name across
-- many revisions; here a different window is a DIFFERENT label with a different name (spec §5), so
-- name and definition are 1:1 and a second table would hold only a join. This forecloses the
-- active-revision pointer spec §12.2 anticipates — that question is still open, and splitting is
-- one migration away, which is cheaper than building revision semantics before the governance rule
-- that gives them meaning is decided.

CREATE TABLE IF NOT EXISTS target_definition (
    definition_id   text PRIMARY KEY,
    name            text NOT NULL,
    entity          text NOT NULL,
    shape           text NOT NULL,
    window_days     integer NOT NULL,
    label_type      text NOT NULL,
    rule            jsonb NOT NULL,
    content_hash    text NOT NULL UNIQUE,
    description     text NOT NULL DEFAULT '',
    -- DELIBERATELY NARROWER than the feature ladder, which also has DATA-CHECKED and
    -- USEFULNESS-CHECKED. Under specify-not-execute those rungs are unreachable: the platform
    -- never sees the labels the rule produces, so it cannot know the class balance or whether the
    -- rule matched nothing at all. A CHECK is a stronger guarantee than a convention nobody
    -- re-reads. CONSEQUENCE, accepted: admitting them later costs a migration — the right price
    -- for making an unearnable claim impossible to write by accident in the meantime.
    verification    text NOT NULL DEFAULT 'DESIGN-CHECKED',
    registered_by   text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT target_definition_name_chk  CHECK (name ~ '^tgt_[a-z0-9_]{1,123}$'),
    CONSTRAINT target_definition_shape_chk CHECK (shape IN ('state_change', 'event_window')),
    CONSTRAINT target_definition_type_chk  CHECK (label_type IN ('binary', 'count', 'amount')),
    CONSTRAINT target_definition_window_chk CHECK (window_days > 0),
    CONSTRAINT target_definition_verification_chk
        CHECK (verification IN ('UNVERIFIED', 'DESIGN-CHECKED'))
);

-- One name per entity, exactly as `feature_definition_name_per_entity`.
CREATE UNIQUE INDEX IF NOT EXISTS target_definition_name_per_entity
    ON target_definition (entity, name);

-- Lineage, for IMPACT ("this column is retired — which labels break?"), never a leakage blocklist:
-- a feature reading the same columns BACKWARD of the as-of date is the method, not a leak.
CREATE TABLE IF NOT EXISTS target_derives_from (
    definition_id  text NOT NULL REFERENCES target_definition(definition_id) ON DELETE CASCADE,
    -- The catalog is part of the identity. `object_ref` is only `public.{table}.{column}`, so a
    -- bare ref does not name a column (M3) — the same reason `_column_meta` scopes to the pair.
    catalog_source text NOT NULL,
    object_ref     text NOT NULL,
    PRIMARY KEY (definition_id, catalog_source, object_ref)
);

CREATE TABLE IF NOT EXISTS target_consumer (
    definition_id text NOT NULL REFERENCES target_definition(definition_id) ON DELETE CASCADE,
    consumer_ref  text NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (definition_id, consumer_ref)
);

CREATE INDEX IF NOT EXISTS target_definition_entity_idx ON target_definition (entity);
