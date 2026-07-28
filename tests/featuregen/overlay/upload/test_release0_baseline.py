"""Release 0 — regenerate the verified-interfaces baseline against ONE commit.

The reference document's measurements were taken against a deployment whose code no longer matches
any branch. This module re-derives everything that can be derived from **this** commit: migrations
apply from scratch, the real operator FTR file ingests, and each measured claim is recomputed.

Run it:

    FTR_CSV=/path/to/FTR_Column_Mapping_final.csv \\
      .venv/bin/python -m pytest tests/featuregen/overlay/upload/test_release0_baseline.py -s -q

It skips when `FTR_CSV` is absent, because the file is customer metadata and is deliberately not in
the repository — so CI stays green while an operator can reproduce the numbers on demand.

**What this module can and cannot measure.** Concept assignment is LLM enrichment; without a
provider the ingest assigns none. So the concept-dependent numbers (M1, M6, and M3a's eligible-pair
count, which needs identifier CONCEPTS) cannot be honestly re-derived here — they are properties of
an enriched catalog, not of the code. Everything that is a property of the code is re-derived, and
the split is reported rather than papered over.
"""
from __future__ import annotations

import os
import pathlib
from datetime import UTC, datetime

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import validate_rows
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE

SOURCE = "ftr"
_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _csv() -> str:
    path = os.environ.get("FTR_CSV", "")
    if not path or not pathlib.Path(path).is_file():
        pytest.skip("set FTR_CSV to the operator FTR export to regenerate the baseline")
    return pathlib.Path(path).read_text(encoding="utf-8-sig")


def _actor():
    return IdentityEnvelope(subject="release0", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


@pytest.fixture
def ingested(overlay_conn):
    """The real FTR export, ingested on THIS commit. No LLM: concepts are therefore absent."""
    db = overlay_conn
    upload = to_glossary_upload(read_ftr_glossary(_csv(), source=SOURCE))
    good = validate_rows(upload.rows, SOURCE, profile=FTR_GLOSSARY_PROFILE).good
    result = ingest_upload(db, SOURCE, good, actor=_actor(), now=_NOW,
                           profile=FTR_GLOSSARY_PROFILE, glossary=upload)
    assert result.status == "ingested", result.status
    return db


def _one(conn, sql, *params):
    return conn.execute(sql, params or None).fetchone()[0]


def test_migrations_apply_from_scratch_on_this_commit(db):
    """Release 0's first question: does the whole migration set, including 1032, apply clean to an
    empty database? The `db` fixture applies them, so reaching here answers it — but assert the two
    newest artifacts exist rather than trusting that."""
    assert _one(db, "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='graph_node' AND column_name=%s", "visible_requires") == 1
    assert _one(db, "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='graph_node' AND column_name=%s", "declared_type") == 1


def test_regenerate_the_measured_baseline(ingested, capsys):
    """Recompute every measured claim and print it as a paste-ready block."""
    db = ingested
    cols = _one(db, "SELECT count(*) FROM graph_node WHERE kind='column'")
    tables = _one(db, "SELECT count(*) FROM graph_node WHERE kind='table'")
    schema_attested = _one(db, "SELECT count(*) FROM graph_node WHERE schema_name IS NOT NULL")
    declared = db.execute(
        "SELECT coalesce(declared_type,'(null)'), count(*) FROM graph_node WHERE kind='column' "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    attested_types = _one(db, "SELECT count(*) FROM graph_node "
                              "WHERE kind='column' AND data_type <> 'unknown'")
    taxonomy = db.execute(
        "SELECT field_name, count(*) FROM field_evidence "
        "WHERE field_name IN ('bian_path','fibo_path','business_term','definition','domain') "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    concepts = _one(db, "SELECT count(*) FROM graph_node WHERE kind='column' AND concept IS NOT NULL")
    requires = db.execute(
        "SELECT coalesce(array_to_string(visible_requires,'+'),'(null)'), count(*) "
        "FROM graph_node WHERE kind='column' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    sources = _one(db, "SELECT count(DISTINCT catalog_source) FROM graph_node")
    bridges = _one(db, "SELECT count(*) FROM entity_bridge_edge")

    with capsys.disabled():
        print("\n" + "=" * 72)
        print("RELEASE 0 — regenerated baseline (integration/ontology-data-agent)")
        print("=" * 72)
        print(f"  columns ingested          : {cols}")
        print(f"  tables                    : {tables}")
        print(f"  schema-attested nodes     : {schema_attested}  (M4)")
        print(f"  declared_type distribution: {declared}")
        print(f"  attested data_type <> unknown: {attested_types}")
        print(f"  field_evidence by field   : {taxonomy}")
        print(f"  visible_requires buckets  : {requires}")
        print(f"  catalog sources           : {sources}  (M7)")
        print(f"  entity_bridge_edge rows   : {bridges}  (M3)")
        print(f"  columns with a concept    : {concepts}  "
              f"(M1 — 0 expected: no LLM in this harness)")
        print("=" * 72)

    # Properties of the CODE, re-derived on this commit.
    assert cols > 0 and tables > 0
    assert schema_attested == cols + tables, "the FTR adapter attests a schema for every node"
    assert attested_types == 0, "a glossary attests no physical type"
    assert dict(taxonomy).get("bian_path"), "BIAN evidence must be persisted (corrected claim)"
    assert bridges == 0, "M3: no route exists to create one"
    assert sources == 1, "M7: the binding constraint"


def test_the_read_scope_fix_computes_on_real_data(ingested):
    """`visible_requires` is GENERATED, so it cannot drift from its inputs. With no governed floor
    resolved yet (no enrichment in this harness), every column requires nothing — and the moment a
    floor is set, the requirement appears without any writer touching the row."""
    db = ingested
    assert _one(db, "SELECT count(*) FROM graph_node WHERE visible_requires <> '{}'") == 0
    db.execute("UPDATE graph_node SET effective_restriction='restricted' "
               "WHERE kind='column' AND column_name='cust_name'")
    assert _one(db, "SELECT count(*) FROM graph_node WHERE visible_requires = '{restricted}'") == 1


def test_the_bridge_fix_makes_identifier_columns_classifiable(ingested):
    """The declared_type fallback, on the real file: before it, every column resolved to the
    unclassifiable `other` family and NO bridge candidate was possible for a glossary catalog."""
    from featuregen.overlay.upload.bridge_candidates import _resolve_family
    rows = ingested.execute(
        "SELECT data_type, declared_type FROM graph_node WHERE kind='column'").fetchall()
    families = [_resolve_family(dt, decl) for dt, decl in rows]
    classifiable = [f for f, _ in families if f != "other"]
    assert len(classifiable) >= 100, f"only {len(classifiable)} of {len(rows)} classifiable"
    assert all(basis == "declared" for _, basis in families if _ != "other")
