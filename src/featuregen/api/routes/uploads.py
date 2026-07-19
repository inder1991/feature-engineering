from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile

from featuregen.api.deps import get_conn, get_identity, get_llm_optional, require_catalog_write
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.contracts.errors import ConcurrencyError
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.csv_reader import read_csv_rows
from featuregen.overlay.upload.excel_reader import read_excel_rows
from featuregen.overlay.upload.ftr_adapter import (
    PreparedFtrUpload,
    ftr_fingerprint_error,
    is_ftr_glossary,
    read_ftr_glossary,
    to_glossary_upload,
)
from featuregen.overlay.upload.glossary_reader import (
    GlossaryUpload,
    is_glossary_csv,
    read_glossary,
)
from featuregen.overlay.upload.ingest import IngestResult, ingest_upload
from featuregen.overlay.upload.ingestion_run import (
    RUN_ID_HEADER,
    _effective_config_snapshot,
    open_run,
    source_fingerprint,
    terminalize_run,
    terminalize_run_durable,
)
from featuregen.overlay.upload.source_profile import (
    FTR_GLOSSARY_PROFILE,
    SOURCE_CAPABILITY_PROFILE_VERSION,
    TECHNICAL_CSV_PROFILE,
    SourceCapabilityProfile,
)
from featuregen.overlay.upload.stage_report import StageRecorder, record_stage

router = APIRouter()
logger = logging.getLogger(__name__)

# Design #3: the run id rides a RESPONSE HEADER — on success and on every post-open error — so a
# caller whose request failed can still fetch GET /ingestion-runs/{id}. A header, deliberately:
# it does not change the JSON body, so the flag-off POST /uploads response stays byte-for-byte.
# The name itself lives in ingestion_run.py (shared with the connector import route).
_RUN_ID_HEADER = RUN_ID_HEADER

# A catalog upload is a SCHEMA export (column names/types/grain), not a data extract, so a modest cap
# bounds the whole-file in-memory read + parse against an accidental or malicious oversized upload.
_MAX_UPLOAD_BYTES = int(os.environ.get("FEATUREGEN_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))


def _read_capped(file: UploadFile) -> bytes:
    data = file.file.read(_MAX_UPLOAD_BYTES + 1)   # read one past the cap to detect an over-limit file
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"upload exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit")
    return data


def _peek_headers(text: str) -> list[str]:
    """The first (header) row of a CSV, without consuming the reader used to parse the rows."""
    return next(csv.reader(io.StringIO(text)), [])


def _read_rows(
    filename: str, data: bytes, source: str
) -> tuple[list[CanonicalRow], SourceCapabilityProfile | None, GlossaryUpload | None,
           PreparedFtrUpload | None]:
    """Read an upload into rows + the source profile that governs validation + (for a glossary) its
    semantic sidecar (spec §U). An exact-fingerprint FTR export takes the FTR adapter path FIRST
    (A1 Task 3b): its ``GlossaryUpload`` rides the same glossary spine, and the 4th element carries
    the ``PreparedFtrUpload`` envelope whose sanitize provenance the route records in the PARSE
    stage detail (resolution #6). A NEAR-FTR file (the distinctive ``schema.table.column`` header
    present but not the exact 17-column multiset) is REJECTED with a fingerprint diagnostic
    (resolution #10) — never silently mangled by another reader. A glossary-shaped CSV takes the
    glossary path and carries ``FTR_GLOSSARY_PROFILE`` (so its ``type="unknown"`` rows validate)
    AND the ``GlossaryUpload`` whose records drive per-field evidence wiring in ``ingest_upload``;
    every other upload keeps its existing, byte-for-byte-unchanged path with no profile and no
    glossary (4th element ``None``)."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        return read_excel_rows(data, source=source), None, None, None
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig")
        headers = _peek_headers(text)
        if is_ftr_glossary(headers):
            prepared = read_ftr_glossary(text, source=source)
            g = to_glossary_upload(prepared)
            return g.rows, FTR_GLOSSARY_PROFILE, g, prepared
        err = ftr_fingerprint_error(headers)
        if err is not None:
            raise HTTPException(status_code=400, detail=f"FTR glossary format error: {err}")
        if is_glossary_csv(headers):
            upload = read_glossary(text, source=source)
            return upload.rows, FTR_GLOSSARY_PROFILE, upload, None
        return read_csv_rows(text, source=source), None, None, None
    raise HTTPException(status_code=400,
                        detail="unsupported file type (expected .csv, .xlsx, or .xlsm)")


@router.post("/uploads", dependencies=[Depends(require_catalog_write)])
def create_upload(
    file: Annotated[UploadFile, File(...)],
    source: Annotated[str, Form(...)],
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient | None, Depends(get_llm_optional)],
) -> IngestResult:
    # The source id IS the catalog identity (fact keys, snapshots, the brake all key on it raw), so
    # normalize it the way every other identity component is normalized — strip+LOWER, matching
    # object_ref._norm — before anything downstream sees it: 'sales', 'sales ' and 'Sales' must be
    # ONE catalog (#16). A merely-stripped 'Sales' would miss the prior 'sales' refs and bypass the
    # large-change brake as a "first upload" while its facts still keyed on the lowered stream.
    source = source.strip().lower()
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    # Design #3: open the durable run manifest BEFORE parse, on an independent committing
    # connection, so a parse/oversize/unsupported failure still has a queryable run row. The
    # effective_config flag snapshot is pinned HERE, once — never re-read from env mid-run.
    # authorization_decision records the gate outcome (review FIX 4): this line is reached only
    # after the route's require_catalog_write dependency passed.
    run_id = open_run(conn, origin_type="upload", catalog_source=source,
                      filename=file.filename, actor=identity,
                      effective_config=_effective_config_snapshot(), now=datetime.now(UTC),
                      authorization_decision="granted:catalog_write")
    response.headers[_RUN_ID_HEADER] = run_id   # the success response; error paths set it below
    # FIX #4: the id ALSO rides request.state — get_conn's commit runs in dependency teardown,
    # AFTER this route returns, and a commit failure discards the built response (header included)
    # while raising a bare psycopg error with no exc.headers. The app-level Exception handler
    # shares this request scope, so state is the one channel that survives every failure mode.
    request.state.ingestion_run_id = run_id
    # Design #22: buffer an honest per-stage account (parse here; every ingest stage inside
    # ingest_upload) and flush it ALONGSIDE terminalize — never mid-request, never into the body.
    recorder = StageRecorder()
    parse_started = datetime.now(UTC)           # #13 gap A: parse's start instant, kept for the
    file_sha256: str | None = None              # error paths too. Stays before the capped read.
    selected_profile: SourceCapabilityProfile | None = None   # None until parse selects one
    failure_status = "rejected"                 # pre-ingest failure = the FILE was rejected...
    try:
        data = _read_capped(file)
        file_sha256 = hashlib.sha256(data).hexdigest()
        try:
            rows, profile, glossary, prepared = _read_rows(file.filename or "", data, source)
        except HTTPException:
            raise
        except Exception as exc:   # a malformed file is a client error, not a 500
            raise HTTPException(status_code=400, detail=f"could not parse upload: {exc}") from exc
        # Delivery B item 9 (source-profile provenance): record WHICH capability profile governs
        # this run. `_read_rows` returns profile=None for the technical/xlsx path, where ingest
        # writes its source evidence at TECHNICAL_CSV_PROFILE strengths — record that same
        # effective profile, never a fabricated one. The run opened BEFORE parse (design #3), so
        # the pair lands at terminalize; a pre-parse failure honestly leaves both NULL.
        selected_profile = profile or TECHNICAL_CSV_PROFILE
        # A1 resolution #6: an FTR upload's sanitize provenance rides the PARSE stage detail —
        # ingest_upload takes no prepared metadata, so this is the one honest place to record how
        # many sample clauses/PII spans parse removed and under which sanitizer/redactor versions.
        # R5-8: the legacy `sanitized_clauses` aggregate conflated stripped clauses, blanked
        # fields and PII spans — the honest breakdown rides beside it so the run manifest states
        # exactly what sanitization did (and to which field class).
        parse_detail: dict = {"rows": len(rows)}
        if prepared is not None:
            parse_detail.update({"sanitized_clauses": prepared.sanitized_count,
                                 "definitions_stripped": prepared.definitions_stripped,
                                 "definitions_suppressed": prepared.definitions_suppressed,
                                 "pii_spans_redacted": prepared.pii_spans_redacted,
                                 "fields_redacted": prepared.fields_redacted,
                                 "sanitizer_version": prepared.sanitizer_version,
                                 "redaction_version": prepared.redaction_version})
        record_stage(recorder, "parse", "succeeded", detail=parse_detail,
                     started_at=parse_started)
        pre_fingerprint, fingerprint_algo = source_fingerprint(conn, source)
        failure_status = "failed"               # ...an ingest-stage fault = the ATTEMPT failed
        # client=None (no provider configured) -> enrichment is skipped; a configured client runs
        # the governed, audited enrichment path (M2/M4). Either way the upload itself succeeds or
        # brakes. `profile` carries the glossary-vs-technical decision so validation is
        # profile-aware (spec §U); `glossary` (a glossary upload only) carries the sidecar that
        # drives per-field evidence wiring.
        #
        # Typed fault mapping (#27): parse errors became a 400 above, but every ingest fault used
        # to collapse into an opaque 500. Map the KNOWN fault classes to a status + a stage
        # diagnostic; an unknown fault still surfaces as a 500 (logged with its traceback) but
        # names the failed stage.
        try:
            result = ingest_upload(conn, source, rows, actor=identity,
                                   now=datetime.now(UTC), client=client, profile=profile,
                                   glossary=glossary, stage_recorder=recorder,
                                   # design #3 provenance: the run this route opened above —
                                   # ingest records its observed/changed objects + asserted/
                                   # changed facts against it (fail-soft, body untouched).
                                   ingestion_run_id=run_id)
        except ConcurrencyError as exc:
            # OCC: a concurrent upload/confirm bumped one of this upload's fact streams mid-write.
            # The request's transaction rolls back cleanly, so a retry is the correct client
            # response.
            raise HTTPException(
                status_code=409,
                detail="ingest conflict: a concurrent change touched this catalog while the "
                       f"upload was being persisted — retry the upload ({exc})") from exc
        except psycopg.Error as exc:
            # A graph-constraint / persist / validation DB fault. Name the stage and the fault
            # CLASS (+ SQLSTATE) — never the raw driver message, which can embed row values
            # (redaction).
            sqlstate = getattr(exc, "sqlstate", None)
            raise HTTPException(
                status_code=422,
                detail=f"ingest failed at the persist/graph stage: {type(exc).__name__}"
                       f"{f' (SQLSTATE {sqlstate})' if sqlstate else ''} — "
                       "the upload was not applied") from exc
        except Exception as exc:
            logger.exception("upload of %r failed at the ingest stage", source)
            raise HTTPException(
                status_code=500,
                detail=f"ingest stage failed: {type(exc).__name__} — the upload was not "
                       "applied") from exc
        # Terminalize ON THE REQUEST CONNECTION: the terminal status (IngestResult.status maps
        # 1:1 onto the run vocabulary) commits atomically with the ingest it describes —
        # 'ingested' can never be recorded for a transaction that then fails to commit.
        post_fingerprint, _ = source_fingerprint(conn, source)
        # R5-9: an FTR upload's run manifest records the INPUT data-row count (accepted columns +
        # the table term + adapter-quarantined rows) — `len(rows)` silently drops the latter two.
        # Every non-FTR path (technical/generic/xlsx) keeps `len(rows)` exactly as before.
        terminalize_run(conn, run_id, status=result.status, now=datetime.now(UTC),
                        row_count=(prepared.input_row_count if prepared is not None
                                   else len(rows)),
                        quarantined_count=result.quarantined,
                        file_sha256=file_sha256, pre_fingerprint=pre_fingerprint,
                        post_fingerprint=post_fingerprint,
                        fingerprint_algo_version=fingerprint_algo,
                        source_type=selected_profile.source_type,
                        profile_version=SOURCE_CAPABILITY_PROFILE_VERSION)
        # #22: the stage reports commit WITH the terminal state on the request connection
        # (flush is savepointed + fail-contained, so it can neither 500 the upload nor change
        # the response body — which stays exactly the IngestResult serialization).
        recorder.flush(conn, run_id, now=datetime.now(UTC))
        return result
    except HTTPException as exc:
        # The request transaction is rolling back — terminalize on an independent connection so
        # the failed attempt's manifest survives. Redaction: record the exception CLASS (of the
        # underlying cause when the HTTPException merely wraps one), never its message.
        # #22: a pre-ingest failure (oversize / unsupported / unparseable) never recorded parse,
        # so the run's stage account states honestly where it stopped.
        if not recorder.has("parse"):
            record_stage(recorder, "parse", "failed", reason_code=f"http_{exc.status_code}",
                         started_at=parse_started)
        terminalize_run_durable(
            run_id, status=failure_status, now=datetime.now(UTC), file_sha256=file_sha256,
            redacted_failure_code=type(exc.__cause__ or exc).__name__,
            reason_code=f"http_{exc.status_code}",
            source_type=selected_profile.source_type if selected_profile else None,
            profile_version=(SOURCE_CAPABILITY_PROFILE_VERSION if selected_profile else None),
            fallback_conn=conn)
        recorder.flush_durable(run_id, now=datetime.now(UTC), fallback_conn=conn)
        exc.headers = {**(exc.headers or {}), _RUN_ID_HEADER: run_id}
        raise
    except Exception as exc:
        # Review FIX 2: a raw (non-HTTPException) fault — e.g. a psycopg.Error from the
        # source_fingerprint calls or the success-path terminalize — used to escape with NO
        # run-id header and leave the run stuck in_progress. The request transaction is likely
        # ABORTED (a DB fault poisons it), so the terminal state MUST go on a fresh connection;
        # then the run id rides the raised exception's headers, which the app-level Exception
        # handler lifts onto the default 500 response (body untouched). Re-raised unchanged.
        terminalize_run_durable(
            run_id, status="failed", now=datetime.now(UTC), file_sha256=file_sha256,
            redacted_failure_code=type(exc).__name__,
            reason_code="unhandled_exception",
            source_type=selected_profile.source_type if selected_profile else None,
            profile_version=(SOURCE_CAPABILITY_PROFILE_VERSION if selected_profile else None),
            fallback_conn=conn)
        # #22: flush the stages REACHED before the fault, durably — a failed run still explains
        # how far it got. Best-effort; a lost flush never masks the real failure.
        recorder.flush_durable(run_id, now=datetime.now(UTC), fallback_conn=conn)
        exc.headers = {**(getattr(exc, "headers", None) or {}), _RUN_ID_HEADER: run_id}
        raise
