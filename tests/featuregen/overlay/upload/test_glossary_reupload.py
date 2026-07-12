"""Task 10 — glossary ingest wiring: re-upload staleness, human-confirmation revalidation, conflicts,
and the existing-path guard (must-prove #7/#8; review fixes #4/#5/#6/#16).

Drives the real ``ingest_upload`` end-to-end (the integration surface) with a glossary sidecar, and
proves:
  * a CHANGED definition STALEs the old ``definition@source`` evidence and writes a new ACTIVE one,
    while an UNCHANGED column's evidence is REUSED (same input_hash, not re-written) though the
    ``source_snapshot_id`` advanced (producer-scoped staleness, review #7);
  * a human-confirmed ``sensitivity`` SURVIVES a re-upload, and when the column's material changed a
    ``field_revalidation`` row is written + ``active_disqualifiers_for`` returns
    ``{CONFIRMATION_PENDING_REVALIDATION}`` so resolution BLOCKS the load-bearing value — the human
    evidence is NOT staled (must-prove #8);
  * a same-FQN conflicting-definition upload opens a ``conflict_review`` item (review #12/#16);
  * a non-glossary upload writes NO field_evidence / revalidation / conflict rows (guarded — the
    existing path is byte-for-byte unchanged).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import Disqualifier
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import is_feature_eligible, resolve_and_project
from featuregen.overlay.upload.field_revalidation import (
    active_disqualifiers_for,
    flag_pending_revalidation,
)
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref

_SOURCE = "gloss"
_BAL_REF = normalize_ref(_SOURCE, "public", "accounts", "balance")
_STATUS_REF = normalize_ref(_SOURCE, "public", "accounts", "status")
_HEADER = ("physical_name,business_term,description_business_definition,data_domain,"
           "bian_path,fibo_path\n")


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _glossary_csv(balance_def: str, status_def: str = "The account status.") -> str:
    return (_HEADER
            + f"public.accounts.balance,Account Balance,{balance_def},Deposits,,\n"
            + f"public.accounts.status,Account Status,{status_def},Deposits,,\n")


def _ingest(db, csv_text: str, now: datetime) -> None:
    upload = read_glossary(csv_text, source=_SOURCE)
    # client=None isolates SOURCE/PARSER evidence (no LLM concepts / taxonomy) for the staleness proofs.
    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=now,
                        client=None, glossary=upload)
    assert res.status == "ingested"


def _all_definition_rows(db, ref: str) -> list[tuple[str, str]]:
    """(proposed_value, lifecycle) for EVERY definition@source row (active or staled), oldest first."""
    return db.execute(
        "SELECT proposed_value, lifecycle FROM field_evidence "
        "WHERE logical_ref = %s AND field_name = 'definition' AND producer = 'source' "
        "ORDER BY created_at, evidence_id", (ref,)).fetchall()


NOW = datetime(2026, 7, 12, tzinfo=UTC)


def test_changed_definition_stales_old_source_evidence_unchanged_is_reused(db):
    _seal()
    _ingest(db, _glossary_csv("The ledger balance."), NOW)

    # Upload 1 wrote exactly one ACTIVE definition@source per column.
    bal1 = read_active_field_evidence(db, _BAL_REF, "definition")
    status1 = read_active_field_evidence(db, _STATUS_REF, "definition")
    assert len(bal1) == 1 and bal1[0].proposed_value == "The ledger balance."
    assert len(status1) == 1
    status_id_1, status_hash_1 = status1[0].evidence_id, status1[0].input_hash
    snap1 = bal1[0].source_snapshot_id

    # Re-upload: balance definition CHANGED, status definition UNCHANGED. New ingestion run.
    _ingest(db, _glossary_csv("The AVAILABLE balance (revised)."), NOW)

    # balance: the OLD definition@source is STALE, exactly one NEW ACTIVE row carries the new value.
    bal_rows = _all_definition_rows(db, _BAL_REF)
    assert ("The ledger balance.", "stale") in bal_rows
    bal_active = read_active_field_evidence(db, _BAL_REF, "definition")
    assert len(bal_active) == 1
    assert bal_active[0].proposed_value == "The AVAILABLE balance (revised)."
    assert bal_active[0].source_snapshot_id != snap1        # a NEW ingestion-run snapshot id

    # status: UNCHANGED input -> the SAME row is REUSED (not re-written, not staled), even though the
    # upload's source_snapshot_id advanced.
    status2 = read_active_field_evidence(db, _STATUS_REF, "definition")
    assert len(status2) == 1
    assert status2[0].evidence_id == status_id_1            # the identical row (reused)
    assert status2[0].input_hash == status_hash_1
    assert status2[0].source_snapshot_id == snap1           # still the first run's snapshot (not rewritten)


def test_human_confirmed_sensitivity_survives_and_blocks_on_material_change(db):
    _seal()
    _ingest(db, _glossary_csv("The ledger balance."), NOW)

    # A human confirms this column's sensitivity, then it resolves as CERTIFIED (load-bearing).
    record_field_evidence(
        db, logical_ref=_BAL_REF, field_name="sensitivity", proposed_value="restricted",
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="human-review", source_snapshot_id="human-1",
        input_hash=field_input_hash(logical_ref=_BAL_REF, field_name="sensitivity",
                                    material="restricted"))
    human_id = read_active_field_evidence(db, _BAL_REF, "sensitivity")[0].evidence_id
    resolve_and_project(db, source=_SOURCE, logical_refs=[_BAL_REF], now=NOW)
    (status_before,) = db.execute(
        "SELECT classification_status FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = 'public.accounts.balance'", (_SOURCE,)).fetchone()
    assert status_before == "confirmed"                    # certified before the material change

    # Re-upload with a CHANGED definition -> material change flags sensitivity PENDING revalidation.
    _ingest(db, _glossary_csv("The AVAILABLE balance (revised)."), NOW)

    # A field_revalidation row was written and the disqualifier is now active for sensitivity.
    pending = db.execute(
        "SELECT count(*) FROM field_revalidation WHERE logical_ref = %s AND field_name = 'sensitivity' "
        "AND status = 'pending'", (_BAL_REF,)).fetchone()[0]
    assert pending == 1
    assert active_disqualifiers_for(db, _BAL_REF, "sensitivity") == frozenset(
        {Disqualifier.CONFIRMATION_PENDING_REVALIDATION})

    # The human evidence SURVIVED — NOT staled by the source re-upload.
    surviving = read_active_field_evidence(db, _BAL_REF, "sensitivity")
    assert [e.evidence_id for e in surviving] == [human_id]

    # ...and resolution now BLOCKS the load-bearing value pending re-confirmation.
    (status_after,) = db.execute(
        "SELECT classification_status FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = 'public.accounts.balance'", (_SOURCE,)).fetchone()
    assert status_after == "proposed"                      # blocked (was 'confirmed')
    assert is_feature_eligible(db, _BAL_REF, "sensitivity") is False


def test_same_fqn_conflicting_definition_opens_conflict_review(db):
    _seal()
    # Two rows name the SAME column with a DIFFERENT definition — validate_rows dedups them to one
    # attachable row, but the disagreement is a metadata conflict (review #12) opened for review.
    csv_text = (_HEADER
                + "public.accounts.balance,Account Balance,The ledger balance.,Deposits,,\n"
                + "public.accounts.balance,Account Balance,A DIFFERENT definition.,Deposits,,\n")
    upload = read_glossary(csv_text, source=_SOURCE)
    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=NOW, client=None,
                        glossary=upload)
    assert res.status == "ingested"

    conflict = db.execute(
        "SELECT field_name, state FROM conflict_review WHERE logical_ref = %s", (_BAL_REF,)).fetchone()
    assert conflict is not None
    assert conflict[0] == "definition" and conflict[1] == "open"


def test_non_glossary_upload_writes_no_evidence_revalidation_or_conflict(db):
    """Guard (existing path unchanged): a technical upload (glossary=None) takes NONE of the glossary
    wiring — no field_evidence, no field_revalidation, no conflict_review rows."""
    _seal()
    rows = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
            CanonicalRow("deposits", "accounts", "balance", "numeric")]
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=NOW)   # no profile, no glossary
    assert res.status == "ingested" and res.asserted >= 1

    assert db.execute("SELECT count(*) FROM field_evidence").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM field_revalidation").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM conflict_review").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM field_decision_event").fetchone()[0] == 0


# ── Task-10 Important-1: the readiness diagnostic is ADVISORY — a DB-level error inside it must not
# abort the upload (was: it poisoned the request tx and the next persist_quarantine DELETE 500'd,
# rolling back the already-asserted facts + graph). ──
def test_readiness_db_error_does_not_abort_glossary_upload(db, monkeypatch):
    _seal()
    from featuregen.overlay.upload import ingest as ingest_mod

    def _boom(conn, **kw):
        # Simulate a DB-level failure INSIDE compute_readiness: this aborts the request transaction.
        conn.execute("SELECT 1 FROM a_table_that_does_not_exist_xyz")

    monkeypatch.setattr(ingest_mod, "compute_readiness", _boom)

    upload = read_glossary(_glossary_csv("The ledger balance."), source=_SOURCE)
    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=NOW, client=None,
                        glossary=upload)

    # The advisory diagnostic's DB error was contained by the savepoint: the upload STILL succeeded
    # (no exception propagated out of ingest_upload) and nothing was rolled back.
    assert res.status == "ingested"
    assert read_active_field_evidence(db, _BAL_REF, "definition")    # the glossary evidence survived
    assert db.execute(                                              # the built graph survived too
        "SELECT count(*) FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (_SOURCE, "public.accounts.balance")).fetchone()[0] == 1


# ── Task-10 Important-3(a): a re-upload that drops the sample-profile phrase must STALE the prior
# PARSER logical_representation/semantic_type — a present->absent field can't stay load-bearing. ──
_PROFILED_DEF = ("The ledger balance. The sample profile is NUMERIC, with representative values such "
                 "as 1250.00; 9.99; 42.50, which supports interpretation.")


def _parser_lifecycles(db, ref: str, field: str) -> set[str]:
    return {lc for (lc,) in db.execute(
        "SELECT lifecycle FROM field_evidence WHERE logical_ref = %s AND field_name = %s "
        "AND producer = 'parser'", (ref, field)).fetchall()}


def test_reupload_dropping_sample_profile_stales_prior_parser_evidence(db):
    _seal()
    _ingest(db, _glossary_csv(_PROFILED_DEF), NOW)

    active1 = read_active_field_evidence(db, _BAL_REF, "logical_representation")
    assert len(active1) == 1 and active1[0].producer == "parser"    # parser certified a shape

    # Re-upload: the edited definition NO LONGER carries a sample-profile phrase -> parser asserts nothing.
    _ingest(db, _glossary_csv("The ledger balance (revised, no sample profile)."), NOW)

    # The prior parser evidence is STALE (present->absent), nothing ACTIVE remains to stay load-bearing.
    assert read_active_field_evidence(db, _BAL_REF, "logical_representation") == []
    assert read_active_field_evidence(db, _BAL_REF, "semantic_type") == []
    assert _parser_lifecycles(db, _BAL_REF, "logical_representation") == {"stale"}


# ── Task-10 Important-3(b): CLEARING a definition on a human-confirmed column is a material change even
# though no present value staled — the human confirmation must be flagged pending-revalidation (blocked),
# NOT staled. ──
def test_clearing_definition_flags_human_confirmed_column_pending_revalidation(db):
    _seal()
    _ingest(db, _glossary_csv("The ledger balance."), NOW)

    record_field_evidence(
        db, logical_ref=_BAL_REF, field_name="sensitivity", proposed_value="restricted",
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="human-review", source_snapshot_id="human-1",
        input_hash=field_input_hash(logical_ref=_BAL_REF, field_name="sensitivity",
                                    material="restricted"))
    human_id = read_active_field_evidence(db, _BAL_REF, "sensitivity")[0].evidence_id

    # Re-upload CLEARING the balance definition (empty cell) — a material field present->absent.
    _ingest(db, _glossary_csv(""), NOW)

    # The prior source definition itself was staled (present->absent), leaving no ACTIVE definition.
    assert read_active_field_evidence(db, _BAL_REF, "definition") == []

    # The human-confirmed sensitivity is flagged PENDING revalidation and the disqualifier is active.
    pending = db.execute(
        "SELECT count(*) FROM field_revalidation WHERE logical_ref = %s AND field_name = 'sensitivity' "
        "AND status = 'pending'", (_BAL_REF,)).fetchone()[0]
    assert pending == 1
    assert active_disqualifiers_for(db, _BAL_REF, "sensitivity") == frozenset(
        {Disqualifier.CONFIRMATION_PENDING_REVALIDATION})

    # The human evidence SURVIVED — a source re-upload never stales human evidence.
    assert [e.evidence_id for e in read_active_field_evidence(db, _BAL_REF, "sensitivity")] == [human_id]
    assert is_feature_eligible(db, _BAL_REF, "sensitivity") is False


# ── Task-10 Minor-4: an unresolvable (identity-less) glossary row must not manufacture a bogus conflict
# against the empty ref `source::public.`. ──
def test_identity_less_rows_do_not_open_bogus_conflicts(db):
    _seal()
    # Two unresolvable 1-part FQNs with DIFFERENT definitions -> glossary_reader emits identity-less
    # rows (table=""/column=""); they must NOT collapse to `source::public.` and open a definition conflict.
    csv_text = (_HEADER
                + "no_dots_here,Term A,Definition ONE.,Deposits,,\n"
                + "also_no_dots,Term B,Definition TWO.,Deposits,,\n")
    upload = read_glossary(csv_text, source=_SOURCE)
    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=NOW, client=None,
                        glossary=upload)

    assert res.status == "ingested"                                 # the rows quarantine; upload still ok
    assert db.execute("SELECT count(*) FROM conflict_review").fetchone()[0] == 0


# ── Task-10 Minor-5: a glossary conflict must open under the SAME schema-preserving logical_ref the
# object's evidence/decisions use — not the schema-forced-public row key. ──
def test_conflict_opens_under_schema_preserving_ref(db):
    _seal()
    schema_ref = normalize_ref(_SOURCE, "dpl_eib_compliance", "accounts", "balance")
    public_ref = normalize_ref(_SOURCE, None, "accounts", "balance")
    assert schema_ref != public_ref                                 # they diverge for a non-public schema
    csv_text = (
        _HEADER
        + "dpl_eib_compliance.accounts.balance,Account Balance,The ledger balance.,Deposits,,\n"
        + "dpl_eib_compliance.accounts.balance,Account Balance,A DIFFERENT definition.,Deposits,,\n")
    upload = read_glossary(csv_text, source=_SOURCE)
    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=NOW, client=None,
                        glossary=upload)
    assert res.status == "ingested"

    conflict = db.execute(
        "SELECT logical_ref, field_name FROM conflict_review").fetchone()
    assert conflict is not None
    assert conflict[0] == schema_ref                                # schema-preserving, NOT flattened
    assert conflict[1] == "definition"


# ── Task-10 Minor-6: flag_pending_revalidation is idempotent per (logical_ref, field_name, pending). ──
def test_flag_pending_revalidation_is_idempotent(db):
    id1 = flag_pending_revalidation(db, logical_ref=_BAL_REF, field_name="sensitivity",
                                    reason="first flag", source_snapshot_id="snap-1", now=NOW)
    id2 = flag_pending_revalidation(db, logical_ref=_BAL_REF, field_name="sensitivity",
                                    reason="second flag", source_snapshot_id="snap-2", now=NOW)

    assert id1 == id2                                               # the same pending flag, not a dup
    n = db.execute(
        "SELECT count(*) FROM field_revalidation WHERE logical_ref = %s AND field_name = 'sensitivity' "
        "AND status = 'pending'", (_BAL_REF,)).fetchone()[0]
    assert n == 1
