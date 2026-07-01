"""SP-2 production wiring — the additive authz surface + candidate-promotion wiring (design §2.1 #5).
P1 introduces `seed_sp2_authz(conn)`, which owns ALL DB-backed SP-2 setup (X2): the authz rows, the
`register_primary_selected(conn)` wiring, and the projection checkpoints — and which P9 EXTENDS to
also register the DocumentSchemaRegistry contract schemas (`documents/registry.py:20`, requires a
conn). P9's `register_sp2(handler_registry)` is CONN-LESS and registers ONLY in-memory things — the
SP-2 event-type schemas + the command catalog — so it never touches the DB; production bootstrap calls
BOTH (`register_sp2(...)` then `seed_sp2_authz(conn)`). Same authz-row shape as
authz.policy._POLICY_ROWS — coarse command capability only; fine-grained authority (the SP-2-built
request-owner guard, confirmer_is_requester_human, delegation_allowed=False) lives in the command
handlers + intake/mcv.py, NOT in these rows (mirrors SP-1)."""

from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.documents.primary import register_primary_selected

# §2.1 #5 + the SP-2 command-capability rows. The additive rejection authority `reject_intent`
# admits the platform/service principal to issue OUT_OF_SCOPE / PROHIBITED_DATA_CLASS terminal
# outcomes (→ SP-0 RUN_REJECTED) — SP-0's `reject` (authz/policy.py:42) STAYS validator-only, and
# requester abandonment reuses SP-0's data-scientist-owned `withdraw`. NO onboarding-answer row is
# added (deferred, §14): the USE_CASE_ONBOARDING task uses SP-0's existing
# ("open_task","","workflow","service",None) row.
_SP2_POLICY_ROWS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("submit_intent", "", "data_scientist", "human", None),
    ("submit_intent", "", "intake-agent", "service", None),
    ("answer_clarification", "", "data_scientist", "human", None),
    ("select_candidate_doc", "", "data_scientist", "human", None),
    ("open_gate1_task", "", "intake-agent", "service", None),
    ("confirm_contract", "", "data_scientist", "human", None),
    ("request_edit", "", "data_scientist", "human", None),
    ("reject_intent", "", "intake-agent", "service", None),  # additive rejection authority (§2.1 #5)
)


def seed_sp2_authz(conn: DbConn) -> None:
    """Idempotently seed SP-2's DB-backed setup (X2): the authz rows, the PRIMARY_SELECTED wiring for
    hypothesis-mode candidate promotion (document-level primitive, §7.1), and the (optional, P8)
    fail-closed FC-status read-model checkpoint. Every step is ON CONFLICT DO NOTHING / an idempotent
    registration. P9 EXTENDS this function to also register the DocumentSchemaRegistry contract schemas
    (which likewise require a conn); the conn-less `register_sp2(handler_registry)` stays event-type
    schemas + command catalog only and never calls this."""
    for action, gate, role, kind, scope in _SP2_POLICY_ROWS:
        conn.execute(
            """
            INSERT INTO authz_policy (action, gate, permitted_role, actor_kind, scope)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (action, gate, permitted_role, actor_kind) DO NOTHING
            """,
            (action, gate, role, kind, scope),
        )
    # PRIMARY_SELECTED (SP-0 primitive) — registers the schema durably + in the in-memory singleton
    # and seeds the stage_primary checkpoint, so the P6 select_candidate_doc promotion appends validate.
    register_primary_selected(conn)
    # Optional fail-closed FC-status read-model checkpoint (P8) — also seeded by 0510; idempotent.
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name) VALUES ('feature_contract') "
        "ON CONFLICT DO NOTHING"
    )
