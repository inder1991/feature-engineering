-- src/featuregen/db/migrations/1041_catalog_engine.sql
--
-- ONE CATALOG IS ONE ENGINE. Where a catalog's data physically lives, declared once per catalog
-- rather than once per table.
--
-- Migration 1037 gave every table its own binding row, which is correct and unmaintainable: 126 FTR
-- columns across one table is fine, a real EDP is thousands. The routing key was always available
-- more cheaply — a catalog knows its engine (hive | oracle | postgres) and its tier (edp | ods), and
-- every row already carries its own schema. So the address has two halves that come from different
-- places and neither needs maintaining:
--
--     the INSTANCE  comes from the connection  — host, port, principal, service
--     the NAMESPACE comes from the catalog     — DPL_EIB_COMPLIANCE, already on graph_node
--
-- and the DATABASE never appears in a query at all. Verified against a real HiveServer2: HiveQL has
-- no third namespace level (`CREATE SCHEMA` is an alias for `CREATE DATABASE`) and refuses a
-- three-part name; Oracle is `SCHEMA.TABLE` with the service selected by the connection. Both
-- dialects render `schema.table`, so `database` on the physical identity is a DISAMBIGUATOR — which
-- cluster — and nothing more.
--
-- The per-table bindings from 1037 survive as an EXCEPTION mechanism: the one table pointed at a
-- snapshot, or mid-migration. Rare by design, so the existence of a row is itself informative.

-- The engine + tier a whole catalog lives on. `engine` is checked against what a dialect exists for;
-- `tier` is deliberately OPEN — edp/ods is the bank's taxonomy, not this system's, and a closed list
-- would reject a correct value on the day someone adds one.
CREATE TABLE IF NOT EXISTS catalog_engine (
    catalog_source text PRIMARY KEY,
    engine         text        NOT NULL CHECK (engine IN ('hive', 'oracle', 'postgres')),
    tier           text        NOT NULL,
    declared_by    text        NOT NULL,
    declared_at    timestamptz NOT NULL DEFAULT now()
);

-- A connection is now reached by (engine, tier, environment) rather than by name.
ALTER TABLE data_source_connection ADD COLUMN IF NOT EXISTS tier text NOT NULL DEFAULT '';
-- The instance the connection selects: an Oracle service, or a Hive cluster label. Identity-bearing
-- and never rendered into SQL — see above.
ALTER TABLE data_source_connection ADD COLUMN IF NOT EXISTS database_name text NOT NULL DEFAULT '';

-- One ACTIVE connection per (engine, tier, environment). Two would mean the same catalog resolves to
-- different clusters depending on read order — and in a bank the two candidates are UAT and
-- production, so the wrong one returns a plausible answer from the wrong data.
CREATE UNIQUE INDEX IF NOT EXISTS data_source_connection_route_idx
    ON data_source_connection (kind, tier, environment_id)
    WHERE active;
