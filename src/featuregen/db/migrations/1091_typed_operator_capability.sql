-- src/featuregen/db/migrations/1091_typed_operator_capability.sql
-- Capability by TYPED SIGNATURE, bound to the build that produced it.
--
-- WHY 1079's SHAPE COULD NOT SURVIVE. It keyed capability on (engine_id, operator_kind), and
-- `OperatorKindV2.AGGREGATE` is ONE kind covering all 21 aggregate functions. So the table could
-- record "this engine can aggregate" and nothing finer — while the truth today is:
--
--     sum            supported          avg          NOT supported
--     count_rows     supported          median       NOT supported
--     count_distinct supported          percentile   NOT supported
--
-- A row saying AGGREGATE is either a lie about `median` or a slander against `sum`. There is no
-- honest value to write, which is why nothing ever wrote one: the table has zero rows in every
-- environment including production, and no code outside its own store has ever referenced it.
--
-- NOT 21 NEW KINDS. Widening the topology vocabulary to carry every function would put the
-- calculation vocabulary inside the graph's structural one, and they change for different reasons —
-- a new aggregate is a renderer capability, a new kind is a new shape of execution. Instead the
-- signature is qualified:
--
--     (operator_kind, operator_variant)     ("aggregate", "sum")
--                                           ("aggregate", "avg")
--                                           ("final_combine", "ratio")
--                                           ("semantic_selection", "eligible_status")
--                                           ("fx_join", "as_of_rate")
--
-- THE BUILD FINGERPRINT IS THE OTHER HALF, and it is the part 1079 had no answer for at all. An
-- execution proof says "we ran this and the number was right". That claim is ABOUT A BUILD. Change
-- the renderer and the claim silently becomes a statement about code that no longer exists — the
-- proof stays green while the thing it proved is gone. `renderer_build_hash` is in the PRIMARY KEY
-- so a moved renderer does not invalidate anything: it simply has no rows yet, and an operator with
-- no row for the current build is unsupported, which is exactly true. Staleness becomes structural
-- rather than something a sweep has to remember to do.
--
-- SAFE TO REPLACE RATHER THAN ALTER. Verified before writing: zero rows in the live database, zero
-- inbound foreign keys, and the only readers are in `execution_proof_store` and move with it. The
-- new primary key is not a widening of the old one — it adds two columns — so an ALTER would leave
-- a key nobody could have populated correctly anyway.
--
-- NOT APPLIED. This file is written, not run.

DROP TABLE IF EXISTS engine_operator_capability;

CREATE TABLE IF NOT EXISTS engine_operator_capability (
    engine_id             text NOT NULL CHECK (btrim(engine_id) <> ''),

    -- ── the typed signature ─────────────────────────────────────────────────────────────────────
    -- The TOPOLOGY kind: what shape of execution this is. Small, closed, changes rarely.
    operator_kind         text NOT NULL CHECK (btrim(operator_kind) <> ''),
    -- WHICH one, within that kind. `sum` against `avg`; `ratio` against `signed_sum`. This is the
    -- column that lets the table state the truth instead of an average of it.
    operator_variant      text NOT NULL CHECK (btrim(operator_variant) <> ''),

    -- ── the build this row is ABOUT ─────────────────────────────────────────────────────────────
    -- Derived from the renderer's own self-description, never typed out — the same discipline
    -- `engine_capability.py` already applies to its advertised aggregations. A row is a statement
    -- about the build named here and about no other.
    renderer_build_hash   text NOT NULL CHECK (btrim(renderer_build_hash) <> ''),

    -- ── the two facts, which are NOT the same kind of claim ─────────────────────────────────────
    -- Fact one: a property of THIS BUILD — can the renderer emit it at all. Derivable, cheap, and
    -- honest to write at startup.
    renderer_dispatchable boolean NOT NULL,

    -- Fact two: a property of a RUN — did it execute against reviewed gold and produce the right
    -- number. NULL means "no proof has been taken", which is different from a proof that FAILED,
    -- and a caller that cannot tell those apart reports an unproved operator as a broken one.
    --
    -- Writing this without having run anything is the one lie this table exists to prevent. It has
    -- no default for that reason.
    execution_proof_hash  text REFERENCES operator_execution_proof(proof_hash),

    recorded_at           timestamptz NOT NULL DEFAULT now(),

    -- The build hash is IN the key: a row describes one operator under one build, and a new build
    -- starts with none. That is what makes a stale proof unreachable rather than merely wrong.
    PRIMARY KEY (engine_id, operator_kind, operator_variant, renderer_build_hash)
);

-- The advertised-set read: one engine, one build, both facts true.
CREATE INDEX IF NOT EXISTS engine_operator_capability_advertised
    ON engine_operator_capability (engine_id, renderer_build_hash)
    WHERE renderer_dispatchable AND execution_proof_hash IS NOT NULL;

-- Answering "what has this engine ever been able to emit, across builds" — an operator question,
-- deliberately separate from the advertised set, which is always about ONE build.
CREATE INDEX IF NOT EXISTS engine_operator_capability_by_signature
    ON engine_operator_capability (engine_id, operator_kind, operator_variant);
