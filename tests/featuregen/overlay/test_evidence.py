from datetime import UTC, datetime

import pytest

from featuregen.contracts.identity import IdentityEnvelope, identity_to_jsonb
from featuregen.overlay.evidence import Evidence, read_evidence, write_evidence

# `created_by` is a Mapping stored as jsonb (pin 14): callers pass identity_to_jsonb(actor), never a
# raw IdentityEnvelope (which is not JSON-serializable).
_ACTOR = IdentityEnvelope(
    subject="svc_profiler", actor_kind="service", authenticated=True,
    auth_method="workload-identity", role_claims=("overlay",), attestation="att-1",
)


def _write(db, fact_key="fk1"):
    return write_evidence(
        db,
        fact_key=fact_key,
        table_snapshot_at=datetime(2026, 6, 1, tzinfo=UTC),
        row_count=1000,
        sample_size=100,
        profile_version="profiler@1",
        thresholds_used={"uniqueness_min": 0.99},
        metric_values={"null_rate": 0.0, "distinct_count": 1000},
        created_by=identity_to_jsonb(_ACTOR),
    )


def test_write_then_read_round_trips(db):
    evidence_id = _write(db)
    assert evidence_id.startswith("eviu_")
    ev = read_evidence(db, evidence_id)
    assert isinstance(ev, Evidence)
    assert ev.evidence_id == evidence_id
    assert ev.fact_key == "fk1"
    assert ev.row_count == 1000
    assert ev.sample_size == 100
    assert ev.profile_version == "profiler@1"
    assert ev.thresholds_used == {"uniqueness_min": 0.99}
    assert ev.metric_values == {"null_rate": 0.0, "distinct_count": 1000}
    assert ev.created_by == identity_to_jsonb(_ACTOR)
    assert ev.created_by["subject"] == "svc_profiler"
    assert ev.created_at is not None


def test_evidence_is_append_only_each_write_is_a_new_immutable_row(db):
    first = _write(db, fact_key="fk1")
    second = _write(db, fact_key="fk1")
    assert first != second  # a re-profile mints a NEW record; it never updates the old one
    # both rows persist, unchanged
    assert read_evidence(db, first).fact_key == "fk1"
    assert read_evidence(db, second).fact_key == "fk1"
    row = db.execute(
        "SELECT count(*) FROM overlay_evidence WHERE fact_key=%s", ("fk1",)
    ).fetchone()
    assert row[0] == 2


def test_read_unknown_evidence_raises(db):
    with pytest.raises(KeyError):
        read_evidence(db, "eviu_does_not_exist")
