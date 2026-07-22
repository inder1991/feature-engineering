from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.search import search


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _ingest(db, now):
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("deposits", "accounts", "balance", "numeric",
                     definition="customer ledger balance"),
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"


def test_search_finds_by_name_and_definition(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    _ingest(db, now)

    # 'balance' matches the column name.
    hits = search(db, "balance", now=now).hits
    assert any(h.object_ref == "public.accounts.balance" for h in hits)

    # 'customer' matches only the definition of balance.
    hits2 = search(db, "customer", now=now).hits
    assert any(h.column == "balance" for h in hits2)


def test_grain_column_outranks_plain_on_name(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    _ingest(db, now)
    hits = search(db, "id", now=now).hits
    assert hits and hits[0].object_ref == "public.accounts.id"
    assert hits[0].is_grain is True


def test_stale_source_excluded(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    _ingest(db, now)
    # Query far in the future -> the source's watermark is older than the 24h SLA -> excluded.
    later = now + timedelta(days=3)
    assert search(db, "balance", now=later).hits == []


def test_resolved_row_freshness_is_its_own_not_the_source_watermark(db):
    # Round-3 #5: search freshness is SOURCE-level (the drift watermark), but a quarantine-RESOLVED
    # row is added incrementally — it was never part of any scan/snapshot, so it must not inherit
    # the source watermark (a later scan of the OTHER rows would keep re-blessing it as fresh
    # forever). Its freshness is its OWN resolution instant: honest right after resolving, stale
    # once that instant ages past the SLA — regardless of the watermark advancing without it.
    from featuregen.overlay.upload.ingest import resolve_quarantine_row
    from featuregen.overlay.upload.review_queue import list_quarantine
    _seal()
    t0 = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "", "numeric"),   # blank column -> quarantined
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=t0).status == "ingested"
    idx = list_quarantine(db, "deposits")[0].row_index
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": "balance"},
                                              actor=_actor(), now=t0)
    assert resolved, reason

    # Right after resolution the row IS searchable — its own attestation (t0) is fresh.
    assert any(h.column == "balance" for h in search(db, "balance", now=t0).hits)

    # A later watermark advance (a scan re-blessing the SCANNED objects, without a graph rebuild)
    # must NOT re-bless the resolved row it never saw.
    t1 = t0 + timedelta(days=3)
    db.execute("UPDATE overlay_drift_watermark SET last_completed_at = %s "
               "WHERE catalog_source = 'deposits'", (t1,))
    hits = search(db, "", now=t1).hits                        # empty query = browse all fresh rows
    assert any(h.column == "id" for h in hits)                # scanned column: fresh under the scan
    assert not any(h.column == "balance" for h in hits)       # resolved column: honestly stale


def test_search_uses_llm_concept(db):
    from featuregen.intake.llm import FakeLLM, FakeResponse
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]  # cryptic name, no definition
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "ledger balance"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
    })
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now,
                         client=client).status == "ingested"
    # 'monetary' finds the cryptic 'bal' column only via its LLM-assigned concept.
    hits = search(db, "monetary", now=now).hits
    assert any(h.column == "bal" for h in hits)
    assert next(h for h in hits if h.column == "bal").concept == "monetary_amount"


def test_search_uses_llm_domain_and_drafted_definition(db):
    from featuregen.intake.llm import FakeLLM, FakeResponse
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]  # cryptic, no definition
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "the account ledger balance"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
    })
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now,
                         client=client).status == "ingested"

    # domain is searchable + surfaced on the hit
    dom_hits = search(db, "deposits", now=now).hits
    bal = next((h for h in dom_hits if h.column == "bal"), None)
    assert bal is not None and bal.domain == "Deposits"

    # the drafted definition made 'ledger' find the otherwise-cryptic column
    assert any(h.column == "bal" for h in search(db, "ledger", now=now).hits)


def test_field_resolution_projection_rebuilds_search_doc(db):
    # Round-3 #20: build_graph writes search_doc ONCE at insert; resolve_and_project later changes
    # the node's concept/definition display values. Full-text search must follow the CURRENT values:
    # the new terms match, the replaced ones stop matching.
    from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
    from featuregen.overlay.field_evidence import (
        field_input_hash,
        record_field_evidence,
        stale_source_evidence,
    )
    from featuregen.overlay.upload.field_resolution import resolve_and_project
    from featuregen.overlay.upload.object_ref import normalize_ref

    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric",
                         definition="obsolete ledger wording")]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"
    # Freshly built: the initial definition term matches (build_graph behaviour unchanged).
    assert any(h.column == "balance" for h in search(db, "obsolete", now=now).hits)

    ref = normalize_ref("deposits", None, "accounts", "balance")

    def seed(field, value, producer, strength):
        # SUPERSEDE like a real producer (`_write_producer_field`): the technical ingest above now
        # writes its own source/attested definition evidence (Delivery B item 8), so a bare second
        # attested row would be a same-strength CONFLICT resolving to no display value at all.
        input_hash = field_input_hash(logical_ref=ref, field_name=field, material=value)
        stale_source_evidence(db, logical_ref=ref, field_name=field, producer=producer,
                              keep_input_hash=input_hash)
        record_field_evidence(
            db, logical_ref=ref, field_name=field, proposed_value=value, producer=producer,
            strength=strength, producer_ref="test-producer", source_snapshot_id="snap-1",
            input_hash=input_hash)

    seed("definition", "authoritative settlement narrative",
         EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    seed("concept", "monetary_stock", EvidenceProducer.LLM, AssertionStrength.PROPOSED)
    resolve_and_project(db, source="deposits", logical_refs=[ref])
    # Sanity: the display projection landed on the flat node.
    assert db.execute(
        "SELECT definition, concept FROM graph_node WHERE catalog_source = 'deposits' "
        "AND object_ref = 'public.accounts.balance'").fetchone() == (
        "authoritative settlement narrative", "monetary_stock")

    # The NEW definition and (humanized) concept terms match the node...
    assert any(h.column == "balance" for h in search(db, "settlement", now=now).hits)
    assert any(h.column == "balance" for h in search(db, "monetary", now=now).hits)
    # ...and the REPLACED definition term no longer does.
    assert not any(h.column == "balance" for h in search(db, "obsolete", now=now).hits)


def test_legacy_applied_reapply_rebuilds_search_doc(db):
    # Round-3 #20 (entity path) + E4: a LEGACY 'applied' entity tag (legacy_file_declared) is
    # re-applied by build_graph's re-apply-on-rebuild AFTER it wrote search_doc — the reapply must
    # re-derive the doc, or the legacy tag term is unfindable. (The new governed apply writes nothing
    # to the graph until a human confirms; that path is covered in test_entity_e4.)
    from featuregen.overlay.upload.graph import build_graph

    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [CanonicalRow("deposits", "accounts", "cust_id", "integer")]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"
    assert not any(h.column == "cust_id" for h in search(db, "customer", now=now).hits)

    # A pre-existing LEGACY applied tag (kept readable, non-governed) survives re-upload.
    db.execute(
        "INSERT INTO entity_suggestion (catalog_source, object_ref, table_name, column_name, "
        "suggested_entity, status) VALUES ('deposits', 'public.accounts.cust_id', 'accounts', "
        "'cust_id', 'Customer', 'applied')")
    build_graph(db, "deposits", rows)   # legacy reapply re-writes entity + rebuilds search_doc
    assert any(h.column == "cust_id" for h in search(db, "customer", now=now).hits)
