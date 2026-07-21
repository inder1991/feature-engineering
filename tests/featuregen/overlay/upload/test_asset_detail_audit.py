"""Delivery F / Task F2-audit — the subject-linked LLM-audit-summaries section + the ``audit:read``
permission that gates it.

SAFE-ONLY: the section returns which dispatch/task/versions/outcome touched a ref — NEVER the
``redacted_input`` (or any raw output / repair body). ``audit:read`` is restricted to platform_admin +
an explicitly provisioned ``audit_reader`` role; a catalog_viewer gets the section named in
``unavailable_sections`` (no 403, no hidden count) — the same gating contract F0 uses for feature:read.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb
from tests.featuregen._helpers import mint_test_identity

from featuregen.identity.permissions import (
    ALL_PERMISSIONS,
    AUDIT_READ,
    ROLE_PERMISSIONS,
    permissions_for,
    roles_granting,
)
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

AUDITOR = mint_test_identity(subject="user:auditor", role_claims=("audit_reader",))
ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform_admin",))
VIEWER = mint_test_identity(subject="user:viewer", role_claims=("catalog_viewer",))


def _seed_dispatch(conn, source, object_ref, logical_ref, *, dref="d1", task="overlay.semantic_bindings"):
    conn.execute(
        "INSERT INTO llm_dispatch (dispatch_ref, logical_call_ref, attempt_no, stage, task, "
        "input_hash, redacted_input, provider, model, prompt_version, schema_version) "
        "VALUES (%s, %s, 1, 'enrich', %s, 'ih', %s, 'anthropic', 'claude', 3, 2)",
        (dref, f"lcr-{dref}", task, Jsonb({"SECRET_PROMPT": "do not leak this redacted body"})),
    )
    conn.execute(
        "INSERT INTO llm_dispatch_subject (dispatch_ref, catalog_source, object_ref, logical_ref, "
        "field_names) VALUES (%s, %s, %s, %s, %s)",
        (dref, source, object_ref, logical_ref, Jsonb(["definition"])),
    )
    conn.execute(
        "INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) VALUES (%s, 'response_received')",
        (dref,),
    )


def _audit(conn, source, object_ref, *, identity):
    return build_asset_detail(conn, source=source, object_ref=object_ref,
                              roles=identity.role_claims, identity=identity, include=["audit"])


# ── (1) the permission itself ───────────────────────────────────────────────────────────────────────

def test_audit_read_permission_is_restricted():
    assert AUDIT_READ in ALL_PERMISSIONS
    # granted ONLY to the provisioned audit role + the superuser
    assert roles_granting(AUDIT_READ) == ["audit_reader", "platform_admin"]
    # NOT in any of the ordinary bundles
    for role in ("catalog_viewer", "data_owner", "feature_engineer", "access_admin"):
        assert AUDIT_READ not in ROLE_PERMISSIONS[role], role
    assert AUDIT_READ in permissions_for(["audit_reader"])
    assert AUDIT_READ not in permissions_for(["catalog_viewer"])


# ── (2) with audit:read → safe summaries, NO raw body ────────────────────────────────────────────────

def test_audit_section_returns_safe_summaries_never_raw(overlay_conn):
    conn = overlay_conn
    build_graph(conn, "t1", [CanonicalRow("t1", "trades", "notional", "numeric")])
    _seed_dispatch(conn, "t1", "public.trades.notional", "t1.public.trades.notional")

    body = _audit(conn, "t1", "public.trades.notional", identity=AUDITOR)
    assert "audit" in body["included_sections"]
    assert "audit" not in body["unavailable_sections"]
    section = body["audit"]
    assert section["status"] == "available"
    assert len(section["summaries"]) == 1
    s = section["summaries"][0]
    # SAFE fields present
    assert s["task"] == "overlay.semantic_bindings"
    assert s["outcome"] == "response_received"
    assert s["provider"] == "anthropic" and s["schema_version"] == 2
    assert s["field_names"] == ["definition"]
    # the RESTRICTED redacted_input / raw body is NEVER present anywhere in the response
    import json
    blob = json.dumps(body, default=str)
    assert "SECRET_PROMPT" not in blob and "do not leak" not in blob
    assert "redacted_input" not in s and "input_hash" not in s

    # platform_admin also sees it
    admin_body = _audit(conn, "t1", "public.trades.notional", identity=ADMIN)
    assert "audit" in admin_body["included_sections"]


# ── (3) without audit:read → absent, in unavailable_sections, NO 403, NO hidden count ────────────────

def test_audit_section_gated_absent_for_viewer(overlay_conn):
    conn = overlay_conn
    build_graph(conn, "t1", [CanonicalRow("t1", "trades", "notional", "numeric")])
    _seed_dispatch(conn, "t1", "public.trades.notional", "t1.public.trades.notional")

    body = _audit(conn, "t1", "public.trades.notional", identity=VIEWER)
    # the anchor itself is visible (catalog_viewer has catalog:read); only the audit section is withheld
    assert body is not None
    assert "audit" not in body
    assert "audit" not in body["included_sections"]
    assert "audit" in body["unavailable_sections"]
    # no hidden count / no summaries leaked anywhere
    import json
    assert "SECRET_PROMPT" not in json.dumps(body, default=str)
