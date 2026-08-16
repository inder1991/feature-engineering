-- src/featuregen/db/migrations/1086_generated_artifact_manifest.sql
-- C-D4 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the EXTERNAL control-plane
-- manifest for a sealed artifact's files, and the Postgres content store it points at.
--
-- WHY EXTERNAL, AND WHY GENERATED.lock IS UNTOUCHED. `read_lock` enforces a strict TWO-KEY top
-- level — `set(parsed) != {"compilation", "generated_project_hash"}` raises (identity.py:446) — so
-- adding a manifest key to the lock makes every existing lock unreadable. And `run_l0` calls
-- `_lock_of` as its FIRST statement inside the chain's transaction without catching
-- (validation.py:1003-1004, chain.py:784-786), so an extended lock would abort the compile rather
-- than produce a finding.
--
-- WHY POSTGRES AND NOT AN OBJECT STORE. Every file this slice renders is text and the whole project
-- is kilobytes. Standing up an object store would add an availability dependency to the compile
-- path for no gain. The manifest names files by `content_reference` rather than by "row in this
-- table", so the day a real object store is warranted only the reference scheme changes.
--
-- THE MANIFEST MUST NEVER BE WRITTEN INTO THE PROJECT TREE. The cluster-side integrity gate hashes
-- every file in the tree except `GENERATED.lock` and `*.pyc` (render/nodes_gate.py:443-449), so a
-- manifest sidecar, restore marker or store receipt placed beside the source fails
-- `PROJECT_INTEGRITY` on the cluster. `materialize_to` also refuses a non-empty target
-- (render/project.py:1358-1362), so there is nowhere in the tree such a file could legally appear.
-- These tables are the only home.
--
-- ON THE "SECOND CYCLE". The plan justifies the external manifest partly by a cycle: a blob pointer
-- known only AFTER storage. That is only half right, and worth recording so nobody re-derives a
-- constraint that does not exist. `generated_project_hash` skips the lock UNCONDITIONALLY
-- (identity.py:385), so bytes added to the lock never re-enter the hash — files can be stored first
-- and a manifest minted afterwards with no circularity. The only genuine self-reference is a
-- pointer to the LOCK'S OWN bytes, and the manifest refuses to cover the lock for exactly that
-- reason. The external manifest is still the right cut; the reason is `read_lock`, not a cycle.
--
-- MIGRATION NUMBER. The plan reserves 1078 for "artifact file manifest + the content store it
-- points at" — two tables under one filename, and `apply_migrations` ledgers by filename stem AND
-- byte checksum (db/migrations.py:310-318). Per the product owner's direction (2026-08-16) each
-- deliverable takes its own filename; this is 1086, above the reserved band.
--
-- NOT APPLIED. This file is written, not run. Applying it is an operator action.

-- ── the content store ────────────────────────────────────────────────────────────────────────────
-- CONTENT-ADDRESSED: `content_reference` IS the digest, so two artifacts that render byte-identical
-- files share one row and a re-render of unchanged bytes stores nothing new. It is also write-once
-- in the only way that matters — a given reference always names the same bytes, because the
-- reference is computed from them.
CREATE TABLE IF NOT EXISTS generated_artifact_blob (
    content_reference text PRIMARY KEY CHECK (content_reference LIKE 'sha256:%'),
    content           text        NOT NULL,
    byte_length       integer     NOT NULL CHECK (byte_length >= 0),
    recorded_at       timestamptz NOT NULL DEFAULT now()
);

-- ── the manifest ─────────────────────────────────────────────────────────────────────────────────
-- One row per FILE of one artifact. `sha256` and `byte_length` are carried here as well as being
-- derivable from the blob, because the manifest is what a retrieval is checked AGAINST: a digest
-- read from the same row as the bytes could not disprove them.
CREATE TABLE IF NOT EXISTS generated_artifact_file (
    artifact_id       text NOT NULL CHECK (btrim(artifact_id) <> ''),
    path              text NOT NULL CHECK (btrim(path) <> ''),

    -- `GENERATED.lock` is excluded by the writer and forbidden here, so the rule survives a caller
    -- that bypasses the writer. The lock's content is decided by this manifest's own existence.
    CONSTRAINT generated_artifact_file_excludes_lock CHECK (path <> 'GENERATED.lock'),

    sha256            text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_length       integer NOT NULL CHECK (byte_length >= 0),
    media_type        text NOT NULL CHECK (btrim(media_type) <> ''),
    content_reference text NOT NULL REFERENCES generated_artifact_blob(content_reference),
    recorded_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (artifact_id, path)
);

CREATE INDEX IF NOT EXISTS generated_artifact_file_by_reference
    ON generated_artifact_file (content_reference);

-- Append-only. A manifest entry is what a retrieval is verified against; a row that can be updated
-- is one that can be brought into agreement with substituted bytes after the fact, which would
-- make the verification ceremonial.
CREATE OR REPLACE FUNCTION generated_artifact_file_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'generated_artifact_file records are write-once (% / %)',
        OLD.artifact_id, OLD.path;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS generated_artifact_file_no_update ON generated_artifact_file;
CREATE TRIGGER generated_artifact_file_no_update
    BEFORE UPDATE OR DELETE ON generated_artifact_file
    FOR EACH ROW EXECUTE FUNCTION generated_artifact_file_write_once();

-- The blob is content-addressed, so an UPDATE would make the reference a lie about its own bytes.
CREATE OR REPLACE FUNCTION generated_artifact_blob_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'generated_artifact_blob is content-addressed and write-once (%)',
        OLD.content_reference;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS generated_artifact_blob_no_update ON generated_artifact_blob;
CREATE TRIGGER generated_artifact_blob_no_update
    BEFORE UPDATE ON generated_artifact_blob
    FOR EACH ROW EXECUTE FUNCTION generated_artifact_blob_write_once();
