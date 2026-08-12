-- The subject/current pointer over the immutable structured-result store (semantic Task 5, D7).
--
-- `structured_result` (migration 1039) is content-addressed and IMMUTABLE: one validated answer per
-- versioned input, forever. That is exactly right for replay and exactly useless for the question a
-- UI asks — "what is the CURRENT semantic adjudication / ontology-gap suggestion for THIS column?"
-- Answering it from 1039 alone means scanning `output_json` across every historical revision.
--
-- So this table adds ONE mutable row per (subject_kind, subject_ref, result_type) naming which
-- immutable revision is current. The revisions stay authoritative; the pointer is derived and
-- rebuildable. It copies the 1033/1038 immutable-revision + CAS-pointer pattern verbatim
-- (`governed_candidate_current`, `bridge_store.record_candidate_assessment`):
--
--   * `pointer_version` is the compare-and-set token. An advance is
--     `UPDATE ... WHERE pointer_version = <expected>` — never SELECT-then-INSERT, so two concurrent
--     re-enrichments cannot both "win" and leave the loser's revision silently dropped.
--   * `lifecycle` withdraws a pointer without deleting history: a re-derivation that no longer
--     suggests an ontology gap must retire the previous suggestion, and a `withdrawn` row keeps the
--     audit trail of what was retired and when. Discovery reads filter `lifecycle = 'current'`.
--
-- SUBJECT KINDS in use at this migration: `column` (the current adjudication for a logical column
-- ref) and `ontology_gap` (the same revision, listed for vocabulary-gap review). Two pointer rows
-- can name ONE revision on purpose — the adjudication and the gap are two questions answered by one
-- immutable output, and each has its own supersession lifetime.
--
-- The pointer NEVER carries a verdict, a value or a payload. Everything readable is in the
-- referenced revision; a reader that wants the content follows the FK.

CREATE TABLE IF NOT EXISTS structured_result_current (
    subject_kind         text        NOT NULL,
    subject_ref          text        NOT NULL,
    result_type          text        NOT NULL,
    structured_result_id text        NOT NULL
        REFERENCES structured_result(structured_result_id),
    pointer_version      integer     NOT NULL DEFAULT 1 CHECK (pointer_version > 0),
    lifecycle            text        NOT NULL DEFAULT 'current'
        CHECK (lifecycle IN ('current', 'withdrawn')),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subject_kind, subject_ref, result_type)
);

-- Discovery: "every subject of kind K with a current result of type T" — the ontology-gap review
-- queue's only query, and it never touches `output_json`.
CREATE INDEX IF NOT EXISTS structured_result_current_discovery_idx
    ON structured_result_current(subject_kind, result_type, lifecycle, subject_ref);

-- Reverse lookup: which subjects point at one revision (audit: "what did this answer become
-- current for?").
CREATE INDEX IF NOT EXISTS structured_result_current_revision_idx
    ON structured_result_current(structured_result_id);
