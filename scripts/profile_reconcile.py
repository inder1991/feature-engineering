#!/usr/bin/env python3
"""Profile reconciliation report (Release-A profile Task 2; precedent: stamp_reconcile.py).

READ-ONLY. Compares, for every catalog/table, the four places profile truth is represented:

  1. per-field ``field_evidence`` (the truth) re-resolved through the SAME pure resolver;
  2. the CURRENT ``field_decision_event`` head (what the decision log says was resolved);
  3. the profile read model (``dataset_profiles.build_dataset_profile`` display values);
  4. the flat ``graph_node`` display projections (``authority_role`` / ``temporal_storage_model``)

plus the catalog narrative store: every ``catalog_profile_current`` pointer must resolve to a
revision that reproduces its own deterministic identity, and every narrative'd catalog must still
exist in the graph.

Prints one line per divergence and exits NONZERO when any is found (0 = fully consistent).
It never writes: repair is the resolver's job (re-resolve / re-upload), not this report's.

Usage:  uv run python scripts/profile_reconcile.py [--dsn postgresql://...]
        (defaults to FEATUREGEN_DSN, then FEATUREGEN_TEST_DSN)
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

from featuregen.overlay.field_authority import resolve_field_authority
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.field_evidence import (
    canonical_hash,
    read_active_field_evidence,
    to_view,
)
from featuregen.overlay.upload.dataset_profiles import build_dataset_profile
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.field_revalidation import active_disqualifiers_for
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_store import current_catalog_profile

# The two Task-2 table-node display projections this report audits (closed internal map — the
# stamp_reconcile._STAMP_COLUMNS convention).
_PROJECTED_FIELDS: dict[str, str] = {
    "authority_role": "authority_role",
    "temporal_storage_model": "temporal_storage_model",
}


def _table_nodes(conn):
    """Every (catalog_source, schema_name, table_name, object_ref, projected columns...)."""
    cols = ", ".join(_PROJECTED_FIELDS.values())
    return conn.execute(
        f"SELECT catalog_source, schema_name, table_name, object_ref, {cols} "
        "FROM graph_node WHERE kind = 'table' ORDER BY catalog_source, object_ref").fetchall()


def reconcile(conn) -> list[str]:
    """All divergences, as human-readable lines. Empty list == consistent."""
    problems: list[str] = []

    for source, schema, table, object_ref, *projected in _table_nodes(conn):
        logical_ref = normalize_ref(source, schema or None, table)
        for (field, _col), graph_value in zip(_PROJECTED_FIELDS.items(), projected,
                                              strict=True):
            evidence = read_active_field_evidence(conn, logical_ref, field)
            policy = policy_for(field)
            assert policy is not None   # registered in Task 1
            resolution = resolve_field_authority(
                [to_view(e) for e in evidence], policy,
                active_disqualifiers=active_disqualifiers_for(conn, logical_ref, field))
            expected = resolution.display_value
            decisions = read_field_decisions(conn, logical_ref, field)
            head = decisions[-1] if decisions else None
            if not evidence and graph_value is None and head is None:
                continue   # nothing anywhere: consistent silence
            # evidence vs graph projection
            if graph_value != expected:
                problems.append(
                    f"{source} {object_ref} {field}: graph displays {graph_value!r} but the "
                    f"active evidence resolves to {expected!r}")
            # evidence vs decision head
            head_hash = head.display_value_hash if head is not None else None
            expected_hash = canonical_hash(expected) if expected is not None else None
            if evidence and head is None:
                problems.append(
                    f"{source} {object_ref} {field}: active evidence exists but no decision was "
                    "ever recorded (resolve_and_project has not run)")
            elif head is not None and head_hash != expected_hash:
                problems.append(
                    f"{source} {object_ref} {field}: decision head {head.decision_event_id} "
                    f"display hash disagrees with the re-resolved evidence")

        # profile read model vs graph projection (the read model re-resolves evidence itself, so
        # this closes the triangle: evidence == profile == graph).
        profile = build_dataset_profile(conn, source=source, dataset_logical_ref=logical_ref)
        if profile is None:
            continue
        for field, attr in (("authority_role", profile.authority_role),
                            ("temporal_storage_model", profile.temporal_storage_model)):
            profile_value = attr.display.value if attr.display is not None else None
            graph_value = dict(zip(_PROJECTED_FIELDS, projected, strict=True))[field]
            if profile_value != graph_value:
                problems.append(
                    f"{source} {object_ref} {field}: profile read model says {profile_value!r} "
                    f"but the graph projection says {graph_value!r}")

    # Catalog narrative store integrity + orphaned pointers.
    for (source,) in conn.execute(
            "SELECT catalog_source FROM catalog_profile_current ORDER BY catalog_source"):
        try:
            current = current_catalog_profile(conn, source)   # raises on corruption
        except Exception as exc:  # noqa: BLE001 — the report must list every failure, not stop
            problems.append(f"{source} catalog narrative: {exc}")
            continue
        assert current is not None   # the pointer row exists by construction of this loop
        in_graph = conn.execute(
            "SELECT 1 FROM graph_node WHERE catalog_source = %s LIMIT 1", (source,)).fetchone()
        if in_graph is None:
            problems.append(
                f"{source} catalog narrative: current pointer exists but the catalog has no "
                "graph nodes (orphaned narrative)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("FEATUREGEN_DSN")
                        or os.environ.get("FEATUREGEN_TEST_DSN"),
                        help="PostgreSQL DSN (default: FEATUREGEN_DSN / FEATUREGEN_TEST_DSN)")
    args = parser.parse_args(argv)
    if not args.dsn:
        print("profile_reconcile: no DSN (pass --dsn or set FEATUREGEN_DSN)", file=sys.stderr)
        return 2
    with psycopg.connect(args.dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")   # belt-and-braces: this report never writes
        problems = reconcile(conn)
        conn.rollback()
    for line in problems:
        print(f"DIVERGENT: {line}")
    print(f"profile_reconcile: {len(problems)} divergence(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
