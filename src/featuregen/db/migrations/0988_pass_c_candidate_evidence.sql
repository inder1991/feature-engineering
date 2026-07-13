-- 0988: Pass C candidate ledger (Phase 3A Task 6).
--
-- The durable home for Pass-C join-candidate evidence: reviewer-facing evidence for proposed
-- candidates, persistence for weak (not-proposed) candidates, and the re-ingest dedupe /
-- conflict-adjudication record `decide_action` reads (lifecycle.py). One row per UNORDERED
-- column-ref pair per catalog source — `from_ref`/`to_ref` are stored SORTED so the same pair
-- always lands on the same row regardless of proposed direction. Task 10 writes rows; Task 6
-- only reads them (prior bucket / namespace_compatibility / fingerprint / fact_key).
CREATE TABLE IF NOT EXISTS pass_c_candidate_evidence (
    catalog_source        text NOT NULL,
    candidate_id          text NOT NULL,
    candidate_fingerprint text NOT NULL,
    from_ref              text NOT NULL,      -- unordered column-ref pair (store sorted)
    to_ref                text NOT NULL,
    fact_key              text,               -- set once proposed
    proposed_event_id     text,
    bucket                text NOT NULL,      -- strong | weak
    namespace_compatibility text NOT NULL,
    lifecycle             text NOT NULL,      -- proposed | weak | superseded | rejected
    evidence_json         jsonb NOT NULL,     -- asdict(JoinCandidateEvidenceV1)
    source_snapshot_id    text NOT NULL,
    config_version        text NOT NULL,
    candidate_algorithm_version text NOT NULL,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_source, from_ref, to_ref)   -- one row per unordered column pair
);
