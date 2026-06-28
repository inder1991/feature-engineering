from __future__ import annotations

# Phase 03: versioned declarative state-machine tables (§4.1/§4.2).
# Each row is one transition; aggregates pin a table_version. Registered into
# Phase 01's MIGRATIONS list by editing src/sp0/db/migrations.py (see below).
STATE_MACHINE_DDL = """
CREATE TABLE run_transition_table (
    table_version integer     NOT NULL,
    from_state    text        NOT NULL,
    to_state      text        NOT NULL,
    trigger       text        NOT NULL,
    guard_expr    text        NULL,
    guard_inputs  jsonb       NOT NULL DEFAULT '{}',              -- predicate -> declared input ref
    precedence    integer     NOT NULL,
    on_success    jsonb       NOT NULL,                           -- {"to":..., "emits":...}
    on_guard_fail jsonb       NULL,                               -- {"to":..., "emits":"GUARD_FAILED"}
    PRIMARY KEY (table_version, from_state, trigger, precedence)
);
CREATE TABLE feature_lifecycle_table (LIKE run_transition_table INCLUDING ALL);
"""
