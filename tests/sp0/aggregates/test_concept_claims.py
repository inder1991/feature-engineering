from sp0.aggregates.concept_claims import claim_concept


def test_first_committed_wins(db):
    assert claim_concept(db, "salary-irregularity", "req_1") is None
    assert claim_concept(db, "salary-irregularity", "req_2") == "req_1"
    row = db.execute(
        "SELECT request_id FROM concept_claims WHERE concept_key = %s",
        ("salary-irregularity",),
    ).fetchone()
    assert row[0] == "req_1"
