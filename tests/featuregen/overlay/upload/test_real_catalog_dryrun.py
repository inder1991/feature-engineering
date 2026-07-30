"""Dry-run the REAL bank catalogs through the deterministic half of ingest.

Read-only with respect to anything that matters: an ephemeral PostgreSQL from the test harness, no
LLM client (so every enrichment stage reports `skipped_no_client` and no token is spent), and the
demo cluster is not touched. The real CSVs are gitignored and never committed — this skips when they
are absent, so CI is unaffected.

**Why this exists.** Claims like "the eight bogus `branch_id` bridges collapse to two" were derived
by reading code, not by running it against the 126 + 111 real columns. That is the same position the
Hive dialect was in before a real engine refused its three-part table name.

**What it can and cannot settle.** It proves both real files parse, route to the glossary path and
ingest with nothing quarantined — a genuine regression check that the committed synthetic fixture
cannot give, because the synthetic file was built to match FTR's shape and CIB's differs.

It CANNOT settle the vocabulary questions. Concept assignment has no deterministic path: with
`client is None`, `ingest_upload` records `enrich_concept: skipped_no_client` and every column keeps
`concept = NULL` (`ingest.py:2019-2027`). Bridges derive from concepts, so they are empty too.
Whether `cust_prim_branch_nm` stops being read as an identifier, and whether nine bridge candidates
become two, are answers only a model call produces — so those claims stay unverified here rather
than being asserted from a run that could not have tested them.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload

#: Both are gitignored bank exports. FTR is conventionally dropped at the repo root; CIB has no
#: conventional home, so it is env-only. Absent either, every test here skips.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_FTR = Path(os.environ.get("FTR_CSV", _REPO_ROOT / "FTR_Column_Mapping_final.csv"))
_CIB = Path(os.environ.get("CIB_CSV", "/nonexistent/CIB_Customer_Column_Mapping_final.csv"))


def _load(db, path: Path, source: str):
    from datetime import UTC, datetime

    upload = to_glossary_upload(read_ftr_glossary(path.read_text(encoding="utf-8-sig"), source=source))
    actor = IdentityEnvelope(subject="dryrun", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))
    # client=None: deterministic stages only. Nothing is sent anywhere.
    result = ingest_upload(db, source, upload.rows, actor=actor,
                           now=datetime(2026, 7, 29, tzinfo=UTC), client=None, glossary=upload)
    return upload, result


@pytest.fixture
def real_catalogs(db):
    if not _FTR.exists() or not _CIB.exists():
        pytest.skip(f"real catalogs not present ({_FTR}, {_CIB})")
    from datetime import timedelta
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))
    ftr = _load(db, _FTR, "ftr")
    cib = _load(db, _CIB, "cib")
    return {"ftr": ftr, "cib": cib, "db": db}


def test_both_real_catalogs_ingest_with_nothing_quarantined(real_catalogs):
    """The check the synthetic fixture cannot make. `ftr_sample_synthetic.csv` was built to mirror
    FTR's exact 17-header shape; CIB's layout differs (no `source_row`, a different flag block), so
    only the real file exercises the second layout end to end."""
    db = real_catalogs["db"]
    for source, expected in (("ftr", 126), ("cib", 111)):
        upload, result = real_catalogs[source]
        assert len(upload.rows) == expected, f"{source} parsed {len(upload.rows)} rows"
        assert result.quarantined == 0, f"{source} quarantined {result.quarantined}"
        got = db.execute(
            "SELECT count(*) FROM graph_node WHERE catalog_source=%s AND kind='column'",
            (source,)).fetchone()[0]
        assert got == expected


def test_a_BOM_must_not_decide_whether_a_file_is_a_glossary(real_catalogs):
    """Found by this dry-run. `is_glossary_mapping` matches raw header strings, so a UTF-8 BOM on
    the FIRST header hides it. FTR is immune by luck — its matched header `schema.table.column` sits
    second, after `source_row` — while CIB's is first, so a plain `utf-8` decode makes the whole
    file 'not a glossary mapping' and quarantines all 111 rows.

    Not a live defect: the upload route decodes `utf-8-sig` (`api/routes/uploads.py:98`) and strips
    it. But detection depending on the caller's decoding is a trap set for the next caller, and the
    dry-run walked straight into it.
    """
    import csv
    import io

    from featuregen.overlay.upload.ftr_adapter import is_glossary_mapping

    def headers(path, encoding):
        return next(csv.reader(io.StringIO(path.read_text(encoding=encoding))))

    # The contract every caller must honour: decode the BOM away, as the route does.
    for path in (_FTR, _CIB):
        assert is_glossary_mapping(headers(path, "utf-8-sig")), path.name

    # And the trap, pinned so it is visible rather than latent. A BOM before the opening quote also
    # stops csv seeing the field as quoted, so the header arrives as '﻿"schema.table.column"'.
    # FTR still passes here — its matched header is second, untouched by the BOM — which is exactly
    # why one real file was never enough to reveal this.
    assert is_glossary_mapping(headers(_FTR, "utf-8"))
    assert not is_glossary_mapping(headers(_CIB, "utf-8")), (
        "CIB now survives a raw utf-8 decode — if this was fixed deliberately in the adapter, "
        "delete this assertion; the fix is to strip U+FEFF before parsing headers")


def test_report(real_catalogs):
    db = real_catalogs["db"]
    print("\n" + "=" * 78)
    for source in ("ftr", "cib"):
        upload, result = real_catalogs[source]
        cols = db.execute(
            "SELECT count(*) FROM graph_node WHERE catalog_source=%s AND kind='column'",
            (source,)).fetchone()[0]
        print(f"{source.upper():<5} rows={len(upload.rows):<5} columns_ingested={cols:<5} "
              f"result={getattr(result, 'quarantined', 'n/a')}")

    print("\n--- concept distribution (both catalogs) ---")
    for src, concept, n in db.execute(
            "SELECT catalog_source, coalesce(concept,'(none)'), count(*) FROM graph_node "
            "WHERE kind='column' GROUP BY 1,2 ORDER BY 1, 3 DESC").fetchall():
        if n >= 2 or concept != "(none)":
            print(f"  {src:<4} {concept:<28} {n}")

    print("\n--- THE branch-name question ---")
    for ref, concept in db.execute(
            "SELECT object_ref, concept FROM graph_node WHERE kind='column' "
            "AND (object_ref ILIKE '%%branch%%') ORDER BY object_ref").fetchall():
        print(f"  {ref:<55} -> {concept}")

    print("\n--- bridge candidates ---")
    cands = derive_bridge_candidates(db, roles=("platform_admin",))
    print(f"  total: {len(cands)}")
    for c in cands:
        print(f"    {c}")
    print("=" * 78)
