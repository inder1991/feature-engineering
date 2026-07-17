"""R5-8/R5-9 — the ingestion-run manifest is HONEST about an FTR upload.

Contracts under test:
- ``ingestion_run.row_count`` records the INPUT data-row count — EVERY CSV data row the adapter
  read: accepted columns + the table term + adapter-quarantined rows — never ``len(rows)``, which
  silently drops the table term and the quarantines (R5-9). The non-FTR path is untouched
  (``test_uploads.py`` / ``test_ingestion_runs.py`` stay green on ``len(rows)``).
- The parse stage's ``detail`` carries the honest sanitizer breakdown — definitions stripped
  (canonical clause excised) vs suppressed (blanked fail-closed) vs definition PII spans redacted
  vs NON-definition field values redacted — alongside the sanitizer/redactor versions (R5-8).

Fixture rows mirror tests/featuregen/overlay/upload/test_ftr_adapter.py (inline, never read from
~/Downloads).
"""
from __future__ import annotations

from tests.featuregen.api._helpers import upload_csv
from tests.featuregen.overlay.upload.test_ftr_adapter import _FTR_CSV, _row

from featuregen.overlay.upload.ingestion_run import RUN_ID_HEADER

# Three extra data rows on top of the 3-row base fixture (2 columns + the table term), each
# exercising ONE counter of the breakdown:
# - row 21: canonical sample clause -> STRIPPED; its synonyms carry one email -> 1 FIELD redaction.
# - row 22: a surviving data marker -> SUPPRESSED (blanked fail-closed).
# - row 23: unresolvable FQN -> adapter-QUARANTINED; its definition's email -> 1 definition SPAN.
_CSV = (
    _FTR_CSV
    + _row(source_row="21", fqn="DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.ACCT_NUM",
           term_name="Account Number",
           definition=('"Customer account number. The sample profile is NUMERIC, with '
                       'representative values such as 3708484836801; 3708446902413, which '
                       'supports interpretation."'),
           synonyms="Acct No|ops.desk@example.com")
    + _row(source_row="22", fqn="DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.CPTY_NAME",
           term_name="Counterparty Name",
           definition='"Counterparty name; observed entries include ARTKOM FZE and NORDIC AS."')
    + _row(source_row="23", fqn="no_dots_here", term_name="Broken Row",
           definition='"Contact ops.desk@example.com for the source extract mapping."')
)


def test_ftr_run_manifest_honest_row_count_and_parse_breakdown(client, conn):
    res = upload_csv(client, "ftr", _CSV)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ingested"
    assert body["quarantined"] == 1
    run_id = res.headers[RUN_ID_HEADER]

    # R5-9: row_count = the 6 INPUT data rows (4 accepted columns + the table term + the
    # quarantined row), NOT len(rows) == 4.
    row_count, quarantined_count = conn.execute(
        "SELECT row_count, quarantined_count FROM ingestion_run WHERE id = %s",
        (run_id,)).fetchone()
    assert row_count == 6
    assert quarantined_count == 1

    # R5-8: the parse-stage detail explains what sanitization did, honestly split.
    state, detail = conn.execute(
        "SELECT state, detail FROM ingestion_run_stage "
        "WHERE ingestion_run_id = %s AND stage = 'parse'", (run_id,)).fetchone()
    assert state == "succeeded"
    assert detail["definitions_stripped"] == 1
    assert detail["definitions_suppressed"] == 1
    assert detail["pii_spans_redacted"] == 1
    assert detail["fields_redacted"] == 1
    assert detail["sanitizer_version"]              # versions ride alongside the breakdown
    assert "redaction_version" in detail
