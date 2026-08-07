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
    meet again on the NEXT run as `semantic_terms`.
"""
from __future__ import annotations

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import enrich_llm as llm
from featuregen.overlay.upload import semantic_context as sc
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import UNCLASSIFIED
from featuregen.overlay.upload.enrich import _SYN_INSTRUCTION, content_hash, draft_synonyms
from featuregen.overlay.upload.glossary_reader import GlossaryRecord

_SOURCE = "ftr"
_TABLE = "comp_fin_tran"
_SCHEMA = "dpl_core"

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
    assert "15" in _SYN_INSTRUCTION and "20" in _SYN_INSTRUCTION
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
