from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import (
    dismiss_quarantine_row,
    ingest_upload,
    resolve_quarantine_row,
)
from featuregen.overlay.upload.review_queue import list_quarantine


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_quarantine_persisted_and_cleared_on_reupload(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)

    # Upload 1: one good row + one bad (blank column) -> ingested, 1 quarantined + persisted.
    rows1 = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "", "text"),  # blank column -> quarantined
    ]
    res1 = ingest_upload(db, "deposits", rows1, actor=_actor(), now=now)
    assert res1.status == "ingested" and res1.quarantined == 1

    q = list_quarantine(db, "deposits")
    assert len(q) == 1
    assert q[0].row_index == 1
    assert "missing" in q[0].reason
    assert q[0].raw["table"] == "accounts"      # raw row is captured for the reviewer

    # Upload 2: the row is fixed -> quarantine for the source is cleared.
    rows2 = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "name", "text"),
    ]
    assert ingest_upload(db, "deposits", rows2, actor=_actor(), now=now).status == "ingested"
    assert list_quarantine(db, "deposits") == []


def _quarantine_one_bad(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "", "text"),   # blank column -> quarantined
    ]
    ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    q = list_quarantine(db, "deposits")
    assert len(q) == 1
    return q[0].row_index


def test_resolve_quarantine_row_adds_to_catalog_and_clears(db):
    idx = _quarantine_one_bad(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": "name"}, actor=_actor())
    assert resolved and reason == ""
    assert list_quarantine(db, "deposits") == []                    # left the queue
    assert db.execute(                                              # entered the catalog
        "SELECT 1 FROM graph_node WHERE catalog_source = 'deposits' AND object_ref = %s",
        ("public.accounts.name",)).fetchone() is not None


def test_resolve_still_invalid_keeps_the_row(db):
    idx = _quarantine_one_bad(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": ""}, actor=_actor())
    assert not resolved and reason                                  # surfaced why
    assert len(list_quarantine(db, "deposits")) == 1               # still quarantined


def test_resolve_rejects_a_column_already_in_the_catalog(db):
    idx = _quarantine_one_bad(db)
    # 'id' already exists in the catalog -> resolving the blank row to 'id' is a conflict
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": "id"}, actor=_actor())
    assert not resolved and "already" in reason


def test_resolve_recognizes_case_space_variant_of_existing_column(db):
    # #7: the resolution path's "already in the catalog?" check must use the SAME normalized ref as
    # the main ingest path — a case/trailing-space variant of an existing column IS that column, not
    # a new one to add as a twin node.
    idx = _quarantine_one_bad(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": "ID "}, actor=_actor())
    assert not resolved and "already" in reason
    twins = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'deposits' AND kind = 'column' "
        "AND lower(btrim(column_name)) = 'id'").fetchone()[0]
    assert twins == 1                                   # no case-variant twin was added


def test_resolve_cannot_bypass_the_large_change_brake(db):
    # #4: an all-quarantined WRONG-SOURCE upload is held by the brake — but resolving it row-by-row
    # used to bypass the brake entirely, letting a reviewer contaminate the catalog with an unrelated
    # schema one object at a time. The cumulative resolved additions must re-trip the source brake.
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "balance", "numeric"),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("deposits", "customers", "cust_id", "integer", is_grain=True),
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"

    foreign = [CanonicalRow("crm", t, c, "text") for t, c in [
        ("leads", "lead_id"), ("leads", "stage"),
        ("tickets", "ticket_id"), ("tickets", "priority")]]
    held = ingest_upload(db, "deposits", foreign, actor=_actor(), now=now)
    assert held.status == "held"                      # the upload path is braked...
    q = list_quarantine(db, "deposits")
    assert len(q) == 4

    outcomes = [resolve_quarantine_row(db, "deposits", item.row_index,
                                       {"source": "deposits"}, actor=_actor())
                for item in q]
    refused = [reason for resolved, reason in outcomes if not resolved]
    assert refused and "brake" in refused[0]          # ...and row-by-row resolution is too
    # The brake fired BEFORE the whole foreign schema landed in the graph.
    foreign_cols = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'deposits' AND kind = 'column' "
        "AND table_name IN ('leads', 'tickets')").fetchone()[0]
    assert foreign_cols < 4
    assert list_quarantine(db, "deposits")            # refused rows stay queued for review


def _quarantine_sensitivity_conflict_pair(db):
    """Ingest one good row + a conflicting duplicate pair for accounts.ssn (one 'pii', one untagged).
    validate_rows quarantines BOTH members fail-closed. Returns (untagged_idx, pii_idx)."""
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "ssn", "text", sensitivity="pii"),
        CanonicalRow("deposits", "accounts", "ssn", "text"),   # untagged twin -> conflict pair
    ]
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    assert res.status == "ingested" and res.quarantined == 2
    q = list_quarantine(db, "deposits")
    assert len(q) == 2 and all("conflicting" in item.reason for item in q)
    untagged = next(i for i in q if not (i.raw.get("sensitivity") or "").strip())
    pii = next(i for i in q if (i.raw.get("sensitivity") or "").strip() == "pii")
    return untagged.row_index, pii.row_index


def test_resolve_refuses_untagged_member_while_pii_sibling_is_quarantined(db):
    # Round-3 #4: validate_rows quarantined BOTH members of the pii/untagged conflict pair so the
    # untagged copy can't make a PII column world-readable — but resolving the untagged member ALONE
    # re-validated it in isolation and graphed exactly that world-readable node. Fail closed: a
    # resolution whose sensitivity sits below a still-quarantined sibling's tag must be refused.
    untagged_idx, _pii_idx = _quarantine_sensitivity_conflict_pair(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", untagged_idx, {}, actor=_actor())
    assert not resolved and "sensitivity" in reason
    assert db.execute(   # no world-readable node was added for the PII column
        "SELECT 1 FROM graph_node WHERE catalog_source = 'deposits' AND object_ref = %s",
        ("public.accounts.ssn",)).fetchone() is None
    assert len(list_quarantine(db, "deposits")) == 2       # both members stay queued


def test_resolve_untagged_member_edited_to_the_sibling_tag_succeeds(db):
    # Matching the quarantined sibling's tag is consistent (nothing is weakened) -> allowed.
    untagged_idx, _pii_idx = _quarantine_sensitivity_conflict_pair(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", untagged_idx,
                                              {"sensitivity": "pii"}, actor=_actor())
    assert resolved and reason == ""
    sens = db.execute(
        "SELECT sensitivity FROM graph_node WHERE catalog_source = 'deposits' AND object_ref = %s",
        ("public.accounts.ssn",)).fetchone()[0]
    assert sens == "pii"                                    # the node carries the restrictive tag


def test_resolve_pii_member_succeeds_and_sibling_then_hits_the_catalog_check(db):
    # Resolving the MOST restrictive member first is safe (at-or-above every sibling); the leftover
    # untagged twin is then refused by the existing already-in-the-catalog check, so it can never
    # overwrite/weaken the pii node either.
    untagged_idx, pii_idx = _quarantine_sensitivity_conflict_pair(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", pii_idx, {}, actor=_actor())
    assert resolved and reason == ""
    sens = db.execute(
        "SELECT sensitivity FROM graph_node WHERE catalog_source = 'deposits' AND object_ref = %s",
        ("public.accounts.ssn",)).fetchone()[0]
    assert sens == "pii"
    resolved2, reason2 = resolve_quarantine_row(db, "deposits", untagged_idx, {}, actor=_actor())
    assert not resolved2 and "already" in reason2


def _ingest_prior_queue(db, now):
    """Ingest a 3-good + 1-bad upload for 'deposits': queue holds the one bad row."""
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "balance", "numeric"),
        CanonicalRow("deposits", "customers", "cust_id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "", "text"),   # blank column -> quarantined
    ]
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    assert res.status == "ingested" and res.quarantined == 1
    assert len(list_quarantine(db, "deposits")) == 1


def test_held_upload_with_no_quarantine_keeps_the_prior_queue(db):
    # #33: a held upload did NOT ingest — the catalog still reflects the prior upload — so a held
    # upload that produced NO quarantine must not wipe the queue a reviewer is working through
    # (pre-fix, persist_quarantine's whole-source refresh with an empty list deleted every entry).
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    _ingest_prior_queue(db, now)

    # a truncated (but individually valid) re-upload trips the large-change brake: held, 0 quarantined
    held = ingest_upload(
        db, "deposits", [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True)],
        actor=_actor(), now=now)
    assert held.status == "held" and held.quarantined == 0
    assert len(list_quarantine(db, "deposits")) == 1   # the reviewer's queue survives


def test_held_upload_with_quarantine_still_replaces_the_queue(db):
    # The held path's existing intent stands (#33 contract): when the held upload DID produce
    # quarantine, the reviewer sees why ITS rows failed — replacing the prior queue.
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    _ingest_prior_queue(db, now)

    foreign = [CanonicalRow("crm", t, c, "text") for t, c in [
        ("leads", "lead_id"), ("leads", "stage"),
        ("tickets", "ticket_id"), ("tickets", "priority")]]   # wrong source -> all quarantined
    held = ingest_upload(db, "deposits", foreign, actor=_actor(), now=now)
    assert held.status == "held" and held.quarantined == 4
    q = list_quarantine(db, "deposits")
    assert len(q) == 4                                        # the held upload's rows, not the old one
    assert all("crm" in item.reason for item in q)


def test_structural_rejection_without_quarantine_keeps_the_prior_queue(db):
    # #33 contract: a structural rejection produced nothing to review, so (like the held-clean case)
    # the prior queue is left untouched.
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    _ingest_prior_queue(db, now)
    res = ingest_upload(db, "deposits", [], actor=_actor(), now=now)
    assert res.status == "rejected" and res.quarantined == 0
    assert len(list_quarantine(db, "deposits")) == 1


def test_structural_rejection_with_reader_quarantine_replaces_the_queue(db):
    # #33 consistency: a structurally-rejected glossary whose READER quarantined rows (multi-schema
    # fold collisions merged in by ingest) surfaces them like the held/all-quarantined paths do,
    # instead of leaving the prior upload's queue silently stale beside a rejection.
    from featuregen.overlay.upload.canonical import RowError
    from featuregen.overlay.upload.glossary_reader import GlossaryUpload
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    _ingest_prior_queue(db, now)

    rows = [CanonicalRow("", "t", "c", "unknown")]            # sourceless -> structural rejection
    glossary = GlossaryUpload(rows=rows, records=[], quarantined=[
        RowError(len(rows), "multi-schema fold collision: s1.t.c vs s2.t.c", None)])
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=now, glossary=glossary)
    assert res.status == "rejected" and res.quarantined == 1
    q = list_quarantine(db, "deposits")
    assert len(q) == 1 and "collision" in q[0].reason         # the rejection's evidence, not the old row


def test_resolve_second_as_of_column_is_refused(db):
    # #17 resolve-path: after the as_of ambiguity quarantined both rows, resolving ONE picks the
    # table's availability basis explicitly; resolving the OTHER must be refused loudly — a second
    # resolve would silently flip the basis (the same last-writer-wins the validation fix removed).
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("deposits", "accounts", "ingested_at", "timestamp", as_of=True),
    ]
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    assert res.status == "ingested" and res.quarantined == 2
    q = list_quarantine(db, "deposits")
    first = next(i for i in q if i.raw["column"] == "posted_at")
    second = next(i for i in q if i.raw["column"] == "ingested_at")

    resolved, reason = resolve_quarantine_row(db, "deposits", first.row_index, {}, actor=_actor())
    assert resolved and reason == ""
    resolved2, reason2 = resolve_quarantine_row(db, "deposits", second.row_index, {}, actor=_actor())
    assert not resolved2 and "as_of" in reason2

    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    from featuregen.overlay.upload.upload_catalog import table_ref
    state = fold_overlay_state(load_fact(db, fact_key(table_ref("deposits", "accounts"),
                                                      "availability_time")))
    assert state.value["column"] == "posted_at"     # the human-chosen basis stands


def test_dismiss_quarantine_row(db):
    idx = _quarantine_one_bad(db)
    assert dismiss_quarantine_row(db, "deposits", idx) is True
    assert list_quarantine(db, "deposits") == []
    assert dismiss_quarantine_row(db, "deposits", 999) is False    # unknown row


def test_resolve_grain_column_reconciles_the_grain_fact(db):
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    from featuregen.overlay.upload.upload_catalog import table_ref
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "cust_id", "", is_grain=True),   # blank type -> quarantined
    ]
    ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    idx = list_quarantine(db, "deposits")[0].row_index
    resolved, _ = resolve_quarantine_row(db, "deposits", idx, {"type": "integer"}, actor=_actor())
    assert resolved
    # the table's grain fact now covers BOTH grain columns, not just the one that ingested cleanly
    state = fold_overlay_state(load_fact(db, fact_key(table_ref("deposits", "accounts"), "grain")))
    assert set(state.value["columns"]) == {"id", "cust_id"}
