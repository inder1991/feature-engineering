"""Task 4c — the synonyms stage asks for a COUNT and is allowed to read the DEFINITION.

Synonyms are the only semantic handle an UNCLASSIFIED column has: with no concept it cannot be
found by meaning, so its aliases are the entire basis on which anyone can search for it. The prior
instruction undermined that twice — it named no count (three or four terms came back) and it said
"use only that item's table/column/type/concept", which forbade the business definition the payload
was already carrying.

What this file pins, and why each is a REVERT-CATCHER rather than a restatement:

  * the instruction asks for 15-20 and names the definition (the change itself);
  * it still asks for ONE COMMA-SEPARATED LINE, because `ingest._project_semantic_terms` splits on
    COMMAS ONLY — asking for more terms must never become asking for a term per line, which is the
    exact shape `_accept_single_line` refuses (Task 4b) and which would project 20 terms as ONE;
  * the payload really does carry the definition the instruction now tells the model to read — an
    instruction must never cite evidence the item does not send;
  * M4 survives it: a TECHNICAL column's uploader free text still does not egress, so the widened
    instruction widened which stage READS the definition, not which columns may send one;
  * 20 terms fit inside `_MAX_SYNONYMS_LEN`, inside the `_MAX_META_LEN` window that SILENTLY
    TRUNCATES them into the same run's summary payload, and inside the per-value egress cap they
    meet again on the NEXT run as `semantic_terms`;
  * a run that drafted every column and STORED NOTHING no longer reports a clean `succeeded` — the
    review follow-up. A column with no glossary record is a designed SKIP, not a failure, so the
    failure count stayed 0 and the stage looked identical to one that stored everything.
"""
from __future__ import annotations

import re

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import enrich_llm as llm
from featuregen.overlay.upload import semantic_context as sc
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import UNCLASSIFIED
from featuregen.overlay.upload.enrich import _SYN_INSTRUCTION, content_hash, draft_synonyms
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.ingest import _enrichment_outcome

_SOURCE = "ftr"
_TABLE = "comp_fin_tran"
_SCHEMA = "dpl_core"
_ACTOR = IdentityEnvelope(subject="user:uploader", actor_kind="human", authenticated=True,
                          auth_method="oidc", role_claims=("data_owner",))

#: A real bank alias set for `create_user_nm` at the count the instruction now asks for. Used to
#: measure the bound against realistic text rather than against padding.
_TWENTY_REAL_TERMS = (
    "audit user, created by, record creator, entry user, data steward, creating user name, "
    "originating user, maker id, input user, source user, capture user, data entry operator, "
    "record originator, creator name, created by user id, initiating user, transaction maker, "
    "onboarding user, system user name, user who created the record")


def _row(column: str, *, definition: str = "", type_: str = "unknown") -> CanonicalRow:
    return CanonicalRow(_SOURCE, _TABLE, column, type_, definition=definition)


def _record(column: str, **over) -> GlossaryRecord:
    base = dict(
        logical_ref=f"{_SOURCE}::{_SCHEMA}.{_TABLE}.{column}",
        term_name="Record Creating User",
        definition="The user id of the operator who keyed the record into the origination "
                   "channel; used for maker-checker audit and for four-eyes attribution.",
        domain="Operations",
        synonyms=("Maker", "Input User"),
        bian_path="Party Reference Data Directory > Party Routing Profile",
        fibo_path="fibo-fbc:Agent",
        term_type="Attribute",
        process_path="Onboarding > Account Origination > Data Capture",
        related_terms=("Checker", "Approving User"),
        schema=_SCHEMA,
        physical_fqn=f"{_SCHEMA}.{_TABLE}.{column}",
        declared_type="varchar(100)",
    )
    base.update(over)
    return GlossaryRecord(**base)


def _bundle(column: str) -> sc.SemanticContextBundleV1:
    rows = [_row(column)]
    return sc.bundle_from_upload(rows[0], glossary_record=_record(column), cohort=rows,
                                 roles=enrich._ENRICHMENT_ROLES)


def _technical_bundle(row: CanonicalRow) -> sc.SemanticContextBundleV1:
    """The bundle a column with NO glossary sidecar really gets: `TECHNICAL_CSV_PROFILE`, whose
    source `definition` is the uploader's own free text."""
    return sc.bundle_from_upload(row, glossary_record=None, cohort=[row],
                                 roles=enrich._ENRICHMENT_ROLES)


# ── the instruction ──────────────────────────────────────────────────────────────────────────────


def test_the_synonym_instruction_asks_for_a_count_and_permits_the_definition() -> None:
    """The bare `"15" in ...` / `"20" in ...` this started as passed on "Give 1520 terms" — a
    substring check cannot tell a RANGE from a typo. Matched as a range of two whole numbers."""
    assert re.search(r"\b15\b.{0,12}\b20\b", _SYN_INSTRUCTION), _SYN_INSTRUCTION
    assert "use only that item's table/column/type/concept" not in _SYN_INSTRUCTION
    assert "definition" in _SYN_INSTRUCTION.lower()


def test_the_instruction_still_asks_for_ONE_COMMA_SEPARATED_LINE() -> None:
    """The half of the contract asking for MORE terms must not break.

    `ingest._project_semantic_terms` splits the stored value on COMMAS and nothing else, so a
    newline-separated answer becomes ONE term with the whole blob as its dedupe key — and
    `_accept_single_line` (Task 4b) refuses it outright, so the item resolves to nothing at all.
    Asking for 20 terms is exactly the wording most likely to tempt a rewrite into "one per line".
    """
    assert "ONE comma-separated line per item" in _SYN_INSTRUCTION
    # …and the gate the instruction must agree with still refuses the per-line shape, at the count
    # the instruction now asks for.
    accept = enrich._accept_single_line(enrich._MAX_SYNONYMS_LEN)
    assert accept(_TWENTY_REAL_TERMS)[1] == "valid"
    assert accept(_TWENTY_REAL_TERMS.replace(", ", "\n")) == (None, "invalid_value")


# ── the instruction may only cite evidence the ITEM actually sends ───────────────────────────────


def test_the_dispatched_item_really_carries_the_definition_the_instruction_names() -> None:
    """Step 1's finding, pinned. `_drafting_payload` -> `for_summary` renders the curated definition
    as `business_definition`, and `_definition_egress_guard` restores the sanitized, word-bounded
    form for a curated column. If that ever stopped, the instruction would be citing evidence the
    model was never shown — the failure mode this whole task exists to remove, inverted."""
    payload = enrich._drafting_payload(
        _bundle("create_user_nm"),
        {"table": _TABLE, "column": "create_user_nm", "type": "varchar(100)",
         "concept": UNCLASSIFIED},
        row=_row("create_user_nm"), rec=_record("create_user_nm"))
    assert payload["business_definition"].startswith("The user id of the operator")


def test_a_TECHNICAL_column_still_sends_no_uploader_free_text() -> None:
    """M4 is untouched by this task. Widening the instruction widened which stage READS a definition,
    not which columns may SEND one: a column with no glossary sidecar has arbitrary uploader prose,
    and `_definition_egress_guard` still removes it. The instruction names the definition; it does
    not require one, so such a column simply drafts from its name, type and table."""
    row = _row("create_user_nm", definition="whatever the uploader typed here")
    bundle = _technical_bundle(row)
    # Non-vacuous: the bundle DOES carry it — the guard is what removes it, not its absence.
    assert sc._value_of(bundle, "definition") == "whatever the uploader typed here"
    payload = enrich._drafting_payload(
        bundle, {"table": _TABLE, "column": "create_user_nm", "type": "varchar(100)"},
        row=row, rec=None)
    assert "business_definition" not in payload


# ── the column that has nothing else ─────────────────────────────────────────────────────────────


def test_an_unclassified_column_still_gets_synonyms(db) -> None:
    """Synonyms are the ONLY search handle an unclassified column has — never skip it."""
    row = _row("create_user_nm", type_="varchar(100)")
    h = content_hash(row)
    client = FakeLLM(script={"overlay.enrich.synonyms": FakeResponse(
        output={"results": [{"ref": h, "synonyms": _TWENTY_REAL_TERMS}]})})
    out = draft_synonyms(db, [row], client, concepts={h: UNCLASSIFIED})
    assert out[h] == _TWENTY_REAL_TERMS
    assert out[h].count(",") >= 4


# ── the bound: 20 terms must FIT, on both the inbound gate and the return trip ───────────────────


def test_twenty_terms_fit_the_accept_bound_with_room() -> None:
    """The arithmetic behind asking for 15-20 rather than reporting it as unachievable.

    `_MAX_SYNONYMS_LEN` is 1000. Measured on the 20-term set below: a realistic bank alias averages
    14.3 chars, so 20 of them with ", " separators is 325 chars — under a THIRD of the bound. Even a
    deliberately verbose answer of 20 terms at 45 chars each lands at 938 and still fits. So no
    headroom change is needed, and the schema `maxLength` that `test_enrich_output_bounds` pins
    EQUAL to this bound needs none either.
    """
    assert enrich._MAX_SYNONYMS_LEN == 1000
    assert len(_TWENTY_REAL_TERMS) < enrich._MAX_SYNONYMS_LEN // 2, len(_TWENTY_REAL_TERMS)
    verbose = ", ".join(["a" * 45] * 20)          # 20 terms at 45 chars each — the pessimistic case
    assert len(verbose) <= enrich._MAX_SYNONYMS_LEN, len(verbose)


def test_the_MERGED_projection_still_fits_egress_after_twenty_terms_are_added() -> None:
    """The return trip, measured on the real FTR glossary rather than assumed.

    The drafted terms do not travel alone. `ingest._project_semantic_terms` MERGES them into
    `graph_node.semantic_terms` alongside the record's own term name, synonyms, BIAN/FIBO and
    process paths and related terms; the NEXT run's payloads carry that COMBINED string back out
    under the `semantic_terms` key, graded per value by `_max_len_for`. An over-cap value has its
    whole ITEM excluded + audited (`_item_len_ok`), so this stage can starve every OTHER stage of
    that column's context — the inversion documented at `_MAX_PROFILE_PROSE`.

    Measured on `ftr_sample_synthetic.csv` (126 column records): the glossary side is 31 chars min,
    60 median, 174 max. Twenty realistic aliases are 325 chars, so the widest real column lands at
    500 of 1000 — half the cap. The guard asserted is the SUM, because that is what egress grades.
    """
    import pathlib

    from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
    from featuregen.overlay.upload.glossary_reader import join_path

    csv_path = pathlib.Path(__file__).parent / "fixtures" / "ftr_sample_synthetic.csv"
    upload = to_glossary_upload(read_ftr_glossary(csv_path.read_text(encoding="utf-8"),
                                                  source="ftr"))
    records = [r for r in upload.records if not r.is_table]
    assert len(records) > 100                       # the fixture really is the wide one

    widest = max(
        len(join_path([rec.term_name, *rec.synonyms, rec.bian_path, rec.fibo_path,
                       rec.process_path, *rec.related_terms], sep=" "))
        for rec in records)
    merged = widest + 1 + len(_TWENTY_REAL_TERMS)
    assert llm._item_len_ok({"table": _TABLE, "column": "create_user_nm",
                             "semantic_terms": "x" * merged}), merged
    # …and the raw accept bound itself does not sit ABOVE the egress cap, which would let a
    # near-maximal answer alone push the merge over on a column with any glossary text at all.
    assert enrich._MAX_SYNONYMS_LEN <= llm._max_len_for("semantic_terms")


# ── the stage may not claim a success it did not have ───────────────────────────────────────────


def test_a_column_with_no_glossary_record_is_COUNTED_as_skipped_not_silently_dropped(db) -> None:
    """`_write_synonym_evidence`'s `ref_of` returns None for a column with no glossary record — "no
    schema-preserving identity to key evidence on". That is a designed SKIP, so it is deliberately
    not a failure, and the writer's return value (the failure count) stays 0.

    Which is exactly how a run that stored nothing used to look identical to one that stored
    everything. `_write_llm_field_evidence` has always accepted a `counts` out-param that increments
    `skipped` on a None ref; this writer never passed it, so the count was not merely unreported —
    it was never COMPUTED. It is passed now.
    """
    # TRANSACTION FIRST, deliberately. psycopg3's `conn.transaction()` COMMITS when it is the
    # OUTERMOST block, and the writer opens one per item — so calling it as this test's first DB
    # operation commits real `field_evidence` rows that survive the fixture's teardown rollback and
    # leak into every later test in the session (`test_pass_a_evidence` asserts a GLOBAL
    # `count(*) WHERE producer = 'llm'` of zero, and fails). One statement here puts the connection
    # in a transaction, so the writer's blocks are SAVEPOINTS and the rollback reaches them. The
    # sibling writer tests get this incidentally by calling `build_graph` first; stating it is the
    # difference between a hermetic test and one that happens to run early.
    db.execute("SELECT 1")

    curated = _row("create_user_nm", type_="varchar(100)")
    orphan = _row("no_sidecar_col", type_="varchar(100)")     # in the file, absent from the glossary
    glossary = GlossaryUpload(rows=[curated], records=[_record("create_user_nm")])

    counts: dict[str, int] = {}
    failures = enrich._write_synonym_evidence(
        db, source=_SOURCE, rows=[curated, orphan],
        synonyms={content_hash(curated): _TWENTY_REAL_TERMS,
                  content_hash(orphan): _TWENTY_REAL_TERMS},
        glossary=glossary, bindings=None, source_snapshot_id="snap", counts=counts)

    assert failures == 0                      # a skip is NOT a failure — that is the whole trap
    assert counts.get("skipped") == 1         # …and it is now visible instead of inferred from zero
    assert counts.get("written") == 1         # non-vacuous: the curated sibling really did store


def test_drafted_everything_stored_nothing_is_PARTIAL_never_a_clean_succeeded() -> None:
    """The outcome the count exists to correct.

    A glossary-less upload drafts synonyms for every column (`draft_synonyms` is gated only on
    `client`) and stores NONE of them: the writer is gated on `glossary is not None and snapshot_id
    is not None`, `_project_semantic_terms` is unreachable (its only call sites are inside
    `_ingest_glossary_evidence`), and the same-run summary ride-along does not run either
    (`enrich_summary` is `not_applicable` when `glossary is None`). So the drafted terms have ZERO
    consumers on that path.

    Every item still RESOLVED, so `resolved == expected`, `unresolved == 0` and `internal_failures
    == 0` — the exact shape that returned `succeeded`. It reports `partial` now.
    """
    resolved = {"h1": "a, b", "h2": "c, d", "h3": "e, f"}
    state, reason, detail = _enrichment_outcome(resolved, 3, evidence_skipped=3)
    assert state == "partial"
    assert reason == "evidence_not_stored"
    assert detail["evidence_skipped"] == 3
    assert detail["resolved"] == 3 and "unresolved" not in detail    # nothing failed; nothing landed
    # The pre-fix behaviour, stated as the thing this must never return again.
    assert _enrichment_outcome(resolved, 3)[0] == "succeeded"


def test_a_GLOSSARY_LESS_ingest_reports_partial_not_succeeded_end_to_end(db) -> None:
    """The wiring, not just the two halves. This is the run the reviewer found: a TECHNICAL upload
    with a client drafts synonyms for every column and persists none of them, because
    `_write_synonym_evidence` is never CALLED (gated on `glossary is not None and snapshot_id is not
    None`) rather than called-and-skipping. So the writer's `counts` stay empty and the branch that
    counts the whole draft as skipped is in `ingest_upload` itself — untested by the two unit tests
    above, and the thing most likely to be wrong.
    """
    from datetime import timedelta

    from featuregen.overlay.config import OverlayConfig, register_overlay_config
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.stage_report import StageRecorder

    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))
    rows = [CanonicalRow("tech_csv", "comp_fin_tran", "create_user_nm", "varchar"),
            CanonicalRow("tech_csv", "comp_fin_tran", "txn_amt", "numeric")]
    results = [{"ref": content_hash(r), "synonyms": _TWENTY_REAL_TERMS} for r in rows]
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": []}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": []}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": results}),
    })
    rec = StageRecorder()
    res = ingest_upload(db, "tech_csv", rows, actor=_ACTOR, client=client, stage_recorder=rec)
    assert res.status == "ingested", (res.status, res.reason, res.flagged)

    report = next(r for r in rec.reports if r.stage == "enrich_synonyms")
    assert report.detail["resolved"] == 2            # every column really was drafted…
    assert report.detail["evidence_skipped"] == 2    # …and every one of them stored nowhere
    assert report.state == "partial"                 # NOT the clean `succeeded` this used to be
    assert report.reason_code == "evidence_not_stored"
    # The money actually was spent: this is a labelling fix, not a claim that nothing happened.
    assert report.detail["expected"] == 2


def test_a_skip_is_ranked_below_a_real_failure_and_below_truncation() -> None:
    """A designed non-write must never be laundered into `items_failed` — the same lie `truncated`
    was introduced to stop — and must never MASK a louder one. Precedence: a dispatched failure
    outranks truncation, which outranks a skip; the counts all still ride the detail."""
    _s, reason, detail = _enrichment_outcome({"h1": "a"}, 2, evidence_skipped=1)
    assert reason == "items_failed" and detail["evidence_skipped"] == 1     # real failure wins
    _s, reason, detail = _enrichment_outcome({"h1": "a"}, 2, not_attempted=1, evidence_skipped=1)
    assert reason == "truncated" and detail["evidence_skipped"] == 1        # truncation outranks it
    # A stage that stored everything is unchanged, byte-for-byte — no key, no state change.
    state, reason, detail = _enrichment_outcome({"h1": "a"}, 1)
    assert (state, reason) == ("succeeded", None) and "evidence_skipped" not in detail
    # Clamped: a writer cannot decline more items than the stage resolved.
    assert _enrichment_outcome({"h1": "a"}, 1, evidence_skipped=9)[2]["evidence_skipped"] == 1


def test_the_SAME_RUN_ride_along_to_the_summary_stage_neither_truncates_nor_excludes() -> None:
    """The IMMEDIATE consumer, and the one that would have silently cut a 20-term answer in half.

    The drafted synonyms do not wait for the next upload. `ingest._summary_dossier_extras` puts this
    run's value straight into the summary drafter's payload as `ai_synonyms`, and
    `enrich.summary_payload` writes it as `str(val)[:_MAX_META_LEN]` — a SILENT TRUNCATION, not a
    rejection, so an over-long value would reach the model cut mid-term with no signal anywhere.
    Then `_item_len_ok` grades the same key at `_max_len_for`, where over-cap EXCLUDES the whole
    item.

    Three constants therefore have to agree, and they are pinned together because each fails
    DIFFERENTLY: `_MAX_SYNONYMS_LEN` is what may be written, `_MAX_META_LEN` truncates, and
    `_MAX_LEN_DEFAULT` excludes. This is precisely why Task 4c depends on Task 4b: at the old
    `_MAX_META_LEN` of 200 a 20-term answer (325 chars) would have been chopped to 200 on the way
    into the summary stage, losing its last seven aliases with nothing in the codebase saying so.
    """
    assert enrich._MAX_SYNONYMS_LEN <= enrich._MAX_META_LEN        # never silently truncated
    assert enrich._MAX_SYNONYMS_LEN <= llm._max_len_for("ai_synonyms")   # never excluded
    # The realistic answer this task asks for survives the truncating step byte-for-byte.
    assert _TWENTY_REAL_TERMS[:enrich._MAX_META_LEN] == _TWENTY_REAL_TERMS
    payload = enrich.summary_payload(
        _row("create_user_nm", type_="varchar(100)"), _record("create_user_nm"),
        {"ai_synonyms": _TWENTY_REAL_TERMS}, _bundle("create_user_nm"))
    assert payload["ai_synonyms"] == _TWENTY_REAL_TERMS
    assert llm._item_len_ok(payload)
