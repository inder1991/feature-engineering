"""Delivery F0 Task 2 — the ASSET READ MODEL: one catalog asset, bounded sections, read-scoped.

:func:`build_asset_detail` assembles the sections the prototype ``AssetDetailSampleScreen`` (5 tabs)
consumes for ONE catalog asset ``(source, object_ref)``. It is a pure READ over already-shipped
authority — it never re-derives value, authority, or readiness, and it writes nothing:

* ``identity`` — the physical/logical identity from the anchor ``graph_node`` row.
* ``effective_metadata`` — the display value per field paired with its C1 authority/provenance
  (:func:`operational_facts.read_operational_value` — governed vs hint vs missing).
* ``evidence`` — the anchor's per-field proposals (active/stale/rejected) + its latest decision.
* ``relationships`` — containment (the parent table + sibling columns) + VERIFIED approved_joins +
  the SEMANTIC subsection (F2b, after Delivery E): the column's VERIFIED entity + currency edges,
  the current-set semantic candidate history + divergences, and the server-calculated governance
  actions the caller may run per binding — all read-scoped. A caller who lacks the catalog:read
  permission gets it named in ``unavailable_sections`` (never an empty-success that reads as "no
  semantic links"); an anchor with no semantic data gets an explicit empty-but-available subsection.
* ``readiness`` — the F0-T1 per-column capability MATRIX + the parent-table blocker diagnostic.
* ``history`` — the reverse ``ingestion_run_object`` lookup (which runs observed/changed this ref,
  newest-first) + each run's per-stage outcomes.
* ``actions`` — server-calculated commands the caller may run. F0 keeps this empty: the real
  correction command is Delivery F0-T4.

READ-SCOPE (the security invariant): the anchor is loaded UNDER the caller's sensitivity scope BEFORE
any section is built. A hidden anchor (a sensitivity the caller's roles can't see) is indistinguishable
from a missing one — both yield ``None`` (the route 404s), so the catalog never leaks that a restricted
object EXISTS. Related nodes/edges (sibling columns, join endpoints) are filtered by the SAME scope IN
SQL, so a hidden sibling is simply absent — no count, no id, nothing that reveals it. Sections that
cannot be served for THIS caller are named in ``unavailable_sections`` without a hidden count.

CONSISTENCY: the caller assembles every section under ONE ``REPEATABLE READ`` transaction (the route's
``get_feature_gen_conn`` dep), so all sections describe ONE torn-free catalog snapshot; a
content-hash ``consistency_token`` (also the HTTP ``ETag``) fingerprints that snapshot.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.identity.permissions import AUDIT_READ, CATALOG_READ, has_permission
from featuregen.overlay.identity import _norm
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.column_readiness import column_readiness
from featuregen.overlay.upload.column_usability import column_usability, table_rollup
from featuregen.overlay.upload.cross_catalog_links import cross_catalog_links
from featuregen.overlay.upload.operational_facts import OperationalValue, read_operational_value
from featuregen.overlay.upload.read_scope import allowed_sensitivities, anchor_visibility_predicate
from featuregen.overlay.upload.readiness import ReadinessScopeType, compute_readiness
from featuregen.overlay.upload.semantic_binding_governance import caller_binding_actions

# The response contract version — bump on a breaking shape change so a client can negotiate.
ASSET_DETAIL_VERSION = "asset-detail/v1"

# F5: the history section returns at most this many newest run associations (a daily-ingested ref
# would otherwise return an unbounded run list + a per-run stage round trip inside the RR tx). When
# more exist the section flags ``truncated: true``.
_HISTORY_RUN_LIMIT = 20

# The F0 sections, in render order. ``identity`` is ALWAYS built (it is the anchor's core); the rest
# are selectable via the route's ``include`` param. ``relationships.semantic`` is F1 (unavailable).
# ``source_glossary`` (richness Task 3C) is the dossier's "what the source file itself said" section.
_F0_SECTIONS: tuple[str, ...] = (
    "identity", "effective_metadata", "evidence", "relationships", "readiness", "history", "actions",
    "audit", "source_glossary",
    # semantic Task 5: the adjudicator's reviewable second opinion for a column Pass A could not
    # settle — alternatives, closed reason/missing-context codes and any ontology-gap suggestion.
    "semantic_adjudication",
)

# The subject-linked LLM-audit-summaries section returns at most this many newest dispatches.
_AUDIT_SUMMARY_LIMIT = 50

# effective_metadata fields: (label, graph_node flat column, C1 field_name). The DISPLAY value comes
# from the flat column (the decision log stores only a hash); the AUTHORITY/provenance comes from C1.
_METADATA_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("concept", "concept", "concept"),
    ("definition", "definition", "definition"),
    # The API key must match the EVIDENCE field_name: the client looks up
    # `proposals_by_field[key]` / `latest_decision_by_field[key]`, so a display-shaped "ai summary"
    # key rendered the value while silently detaching its proposal and decision history. The screen
    # humanises underscores for display already.
    ("ai_summary", "ai_summary", "ai_summary"),
    ("domain", "domain", "domain"),
    # D13.2 — the FINER LLM-proposed axis beside the coarse source `domain`. Rendered exactly like
    # every other recommendation field, which is the point: it arrives with its `llm_proposed`
    # authority label from `_authority_label`, never as a governed value and never in place of
    # `domain`. A column with no proposal simply has no entry (no fabricated empty).
    ("sub_domain", "sub_domain", "sub_domain"),
    ("additivity", "additivity", "additivity"),
    ("unit", "unit", "unit"),
    ("currency", "currency", "currency"),
    ("entity", "entity", "entity"),
    ("type", "data_type", "logical_representation"),
    # Richness Task 3C: the two display axes the axis projection fills (migrations 1042 / 1040).
    # ``sensitivity_display`` is the DISPLAY label, never the enforcement tag (``sensitivity`` /
    # ``visible_requires`` stay the only read-scope authority); ``party_role`` is advisory.
    ("sensitivity_display", "sensitivity_display", "sensitivity_display"),
    ("party_role", "party_role", "party_role"),
)

# The display axes whose value, when present without any evidence row, was written by the
# DETERMINISTIC axis projection (fill-only-NULL, provenance in the run manifest) — labelled so a
# projected value never reads as "unattested" (which would frame a normal state as failure).
_PROJECTED_AXES = frozenset({"sensitivity_display", "party_role"})

# The anchor graph_node columns this read model surfaces (identity + display metadata + fact links).
# entity_status / entity_fact_key / entity_fact_event_id back the F12 entity-authority gate below.
_ANCHOR_COLUMNS = (
    "catalog_source, object_ref, kind, table_name, column_name, schema_name, data_type, "
    "declared_type, definition, ai_summary, is_grain, is_as_of, concept, domain, sub_domain, "
    "sensitivity, "
    "sensitivity_display, party_role, additivity, "
    "unit, currency, entity, entity_status, entity_fact_key, entity_fact_event_id, "
    "grain_fact_event_id, availability_fact_event_id"
)


def _load_anchor(conn: DbConn, source: str, object_ref: str, allowed: list[str]) -> dict | None:
    """The anchor ``graph_node`` row, loaded UNDER read-scope. Returns ``None`` when the ref does
    not exist OR carries a sensitivity the caller's roles can't see — the two are deliberately
    indistinguishable so a hidden object never leaks its existence via a different status/shape.
    A TABLE anchor's scope is DERIVED from its columns (D11, the shared predicate): table nodes
    carry ``visible_requires = {}``, so the raw clause alone would hand out the identity of a
    fully-restricted table — an existence oracle."""
    row = conn.execute(
        f"SELECT {_ANCHOR_COLUMNS} FROM graph_node gn "
        # F7: object_ref is canonical lowercase, so equality against lower(%s) uses the (source,
        # object_ref) PK directly instead of forcing a functional scan over lower(object_ref).
        "WHERE gn.catalog_source = %s AND gn.object_ref = lower(%s) "
        f"AND {anchor_visibility_predicate()}",
        (source, object_ref, allowed, allowed),
    ).fetchone()
    if row is None:
        return None
    cols = _ANCHOR_COLUMNS.replace(" ", "").split(",")
    return dict(zip(cols, row, strict=True))


def _identity_section(anchor: dict, logical_ref: str) -> dict:
    """Physical + logical identity from the anchor row (never a hidden field)."""
    return {
        "graph_ref": anchor["object_ref"],
        "object_ref": anchor["object_ref"],
        "logical_ref": logical_ref,
        "source": anchor["catalog_source"],
        "kind": anchor["kind"],
        "schema_name": anchor["schema_name"],   # the physical (pre-flatten) schema, or None
        "table": anchor["table_name"],
        "column": anchor["column_name"],
        "operational_type": anchor["data_type"],   # the numeric-usable operational type
        "declared_type": anchor["declared_type"],   # the FTR-declared SQL type (non-operational)
        "is_grain": anchor["is_grain"],
        "is_as_of": anchor["is_as_of"],
    }


def _authority_label(ov: OperationalValue, display_value: object | None) -> str:
    """The authority a display value carries: ``governed`` for a C1-verified projection; else
    ``hint`` when a value is shown; else ``missing`` (no value, no decision)."""
    if ov.status == "resolved":
        return "governed"
    if display_value is not None or ov.value is not None:
        return "hint"
    return "missing"


# Honest author of an UNCONFIRMED value: who asserted it and at what strength, from the active
# evidence layer. Surfaced as effective_metadata's fallback so a value with a known author never reads
# as "unattested" just because no governed DECISION exists yet.
_EVIDENCE_PROVENANCE_LABELS: dict[tuple[str, str], str] = {
    ("source", "attested"): "source attested",
    ("source", "proposed"): "source proposed",
    ("llm", "proposed"): "AI proposed",
    ("llm", "corroborated"): "AI corroborated",
    ("taxonomy", "confirmed"): "rulebook confirmed",
    ("taxonomy", "proposed"): "rulebook proposed",
    ("parser", "supported"): "parser detected",
}


def _evidence_provenance_label(producer: str, strength: str) -> str:
    return _EVIDENCE_PROVENANCE_LABELS.get((producer, strength), f"{producer} {strength}")


def _effective_metadata_section(conn: DbConn, logical_ref: str, anchor: dict) -> dict:
    """The display value + C1 authority/provenance for each metadata field (columns only). Every
    field's authority/provenance is SOURCED from C1 :func:`read_operational_value` — never
    re-derived here."""
    fields: dict[str, dict] = {}
    # Newest ACTIVE evidence per field — the honest author of a value that has no governed decision,
    # AND (Task 3C) the value it proposed: a NULL display axis with a live AI proposal renders that
    # proposal (labelled), never a blank — "nobody decided yet" must be distinguishable from
    # "nothing was ever proposed".
    active_ev = {
        r[0]: (r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT DISTINCT ON (field_name) field_name, producer, strength, proposed_value "
            "FROM field_evidence WHERE logical_ref = %s AND lifecycle = 'active' "
            "ORDER BY field_name, created_at DESC, evidence_id DESC",
            (logical_ref,),
        ).fetchall()
    }
    for label, flat_col, c1_field in _METADATA_FIELDS:
        display_value = anchor.get(flat_col)
        ov = read_operational_value(conn, logical_ref, c1_field)
        entry = {
            "value": display_value,
            "authority": _authority_label(ov, display_value),
            "c1_status": ov.status,
            "provenance": ov.decision_event_id or ov.fact_event_id,
            "selected_evidence_ids": list(ov.selected_evidence_ids),
        }
        ev = active_ev.get(c1_field)
        entry["evidence_provenance"] = (
            _evidence_provenance_label(ev[0], ev[1]) if ev else None
        )
        # The newest ACTIVE proposal's value — what the screen renders (with the author chip) when
        # the display column is NULL. Carried whether or not a display value exists; the display
        # value always wins on screen.
        entry["proposed_value"] = ev[2] if ev else None
        # A projected display axis (sensitivity_display / party_role) has no evidence rows — its
        # value came from the deterministic axis projection. Say so, rather than "unattested".
        if label in _PROJECTED_AXES and display_value is not None \
                and entry["evidence_provenance"] is None:
            entry["evidence_provenance"] = "system projected"
        # Type display policy (Task 3C): the flat data_type is the OPERATIONAL type; when it is
        # unknown but the source DECLARED a SQL type, the declared type is the honest display —
        # with its basis named — and bare "unknown" is reserved for genuinely holding nothing.
        if label == "type":
            operational = anchor.get("data_type")
            known = operational is not None and str(operational).strip().lower() \
                not in ("", "unknown")
            if known:
                entry["basis"] = "operational"
            elif anchor.get("declared_type"):
                entry["value"] = anchor["declared_type"]
                entry["basis"] = "declared"
                # A value IS displayed now, so the authority label must follow it (hint, not
                # missing) — authority is about attestation, and the file did declare this.
                entry["authority"] = _authority_label(ov, entry["value"])
                if entry["evidence_provenance"] is None:
                    entry["evidence_provenance"] = "source declared"
            else:
                entry["basis"] = None
        # [E1a T3] domain is TWO-LEVEL: a table's domain is the DEFAULT context its columns inherit,
        # and a column asserts its own only as an explicit OVERRIDE. So the column's own active
        # evidence IS the discriminator — with it the value is `direct` (authored at this column);
        # without it the value on the flat column came from the table, and we say so rather than
        # fabricating column-level evidence for what is really inheritance.
        if label == "domain":
            entry["origin"] = "direct" if ev else "inherited"
            if ev is None:
                entry["inherited_from"] = anchor["table_name"]
        # [F12] composition-audit — ``entity`` is governed by the E3 SECOND gate
        # (``entity_status='VERIFIED'``, the same gate ``verified_entity_of`` and this payload's OWN
        # relationships.semantic subsection apply), which C1 cannot model (entity has no fact/decision
        # wiring in read_operational_value — it reads as an advisory hint). A bank-confirmed entity
        # must render ``governed`` WITH its fact provenance, and a just-withdrawn (demoted) one falls
        # back to the plain hint label — so one payload's two sections can never disagree.
        if label == "entity" and anchor.get("entity_status") == "VERIFIED" \
                and display_value is not None:
            entry["authority"] = "governed"
            entry["provenance"] = (anchor.get("entity_fact_event_id")
                                   or anchor.get("entity_fact_key"))
        fields[label] = entry
    return {"fields": fields}


# The dossier's "From the source glossary" fields (richness Task 3C) — everything the SOURCE FILE
# itself asserted about this term, persisted as source field-evidence (Task 3 Step 6b added the four
# previously-lost ones). Read back per-field from the ACTIVE source evidence; a field the file never
# declared is simply absent (no fabricated empties), and every present value carries the source
# producer/strength label ("source attested" / "source proposed") as its provenance chip.
_SOURCE_GLOSSARY_FIELDS: tuple[str, ...] = (
    "business_term", "term_type", "process_path", "related_terms",
    "bian_path", "fibo_path", "physical_fqn",
)


def _source_glossary_section(conn: DbConn, logical_ref: str) -> dict:
    """The anchor's ACTIVE source-glossary field evidence, one entry per declared field. Pure read
    over ``field_evidence`` (producer='source'); the anchor already passed read-scope. ``fields``
    is empty — not invented — for an asset whose upload declared none of them."""
    rows = conn.execute(
        "SELECT DISTINCT ON (field_name) field_name, proposed_value, strength "
        "FROM field_evidence WHERE logical_ref = %s AND producer = 'source' "
        "AND lifecycle = 'active' AND field_name = ANY(%s) "
        "ORDER BY field_name, created_at DESC, evidence_id DESC",
        (logical_ref, list(_SOURCE_GLOSSARY_FIELDS)),
    ).fetchall()
    fields = {
        name: {
            "value": value,
            "provenance": _evidence_provenance_label("source", strength),
        }
        for name, value, strength in rows
        if value is not None and str(value).strip() != ""
    }
    return {"fields": fields}


def _evidence_section(conn: DbConn, logical_ref: str) -> dict:
    """The anchor's per-field proposals bucketed by lifecycle (active/stale/rejected/superseded) +
    each field's LATEST decision — a set query over the field stores, not a per-field round trip.

    The anchor already passed read-scope (a hidden anchor 404s before this runs), so its own
    evidence is permitted; nothing here reaches beyond the anchor's ``logical_ref``."""
    proposals = conn.execute(
        "SELECT field_name, lifecycle, evidence_id, producer, strength, proposed_value, "
        "confidence_band FROM field_evidence WHERE logical_ref = %s "
        "ORDER BY field_name, created_at, evidence_id",
        (logical_ref,),
    ).fetchall()
    by_field: dict[str, dict] = {}
    for field_name, lifecycle, eid, producer, strength, value, band in proposals:
        entry = by_field.setdefault(
            field_name, {"active": [], "stale": [], "rejected": [], "superseded": []}
        )
        bucket = entry.get(lifecycle)
        if bucket is None:   # a lifecycle outside the shown buckets is not surfaced (still counted)
            bucket = entry.setdefault(lifecycle, [])
        bucket.append({
            "evidence_id": eid, "producer": producer, "strength": strength,
            "proposed_value": value, "confidence_band": band,
        })

    # F8: ONE DISTINCT ON query for the latest decision per field, replacing the prior per-field 1+N
    # (a DISTINCT field_name query + a LIMIT-1 round trip per field). The ORDER BY makes the
    # DISTINCT-ON row the newest decision for each field (matching the per-field ORDER BY it replaced).
    latest: dict[str, dict] = {}
    for field_name, deid, event_type, conflict_status, lbv_hash, created_at in conn.execute(
        "SELECT DISTINCT ON (field_name) field_name, decision_event_id, event_type, "
        "conflict_status, load_bearing_value_hash, created_at FROM field_decision_event "
        "WHERE logical_ref = %s ORDER BY field_name, created_at DESC, decision_event_id DESC",
        (logical_ref,),
    ).fetchall():
        latest[field_name] = {
            "decision_event_id": deid, "event_type": event_type, "conflict_status": conflict_status,
            "load_bearing": lbv_hash is not None, "decided_at": created_at,
        }

    return {"proposals_by_field": by_field, "latest_decision_by_field": latest}


def _relationships_section(
    conn: DbConn, source: str, anchor: dict, allowed: list[str], roles: Iterable[str],
    identity: IdentityEnvelope | None = None,
) -> tuple[dict, list[str]]:
    """Containment (parent table + read-scoped sibling columns) + VERIFIED approved_joins touching
    the ref (read-scoped on BOTH endpoints) + the SEMANTIC subsection (F2b — verified entity/currency
    edges, candidate history, divergences, caller-gated governance actions). Withheld from a caller
    lacking catalog:read (named in the returned list); otherwise real, read-scoped, never a stub.

    Every related read is sensitivity-filtered IN SQL, so a hidden sibling / hidden join endpoint /
    hidden semantic endpoint is simply absent — no count, no id, no leak."""
    is_column = anchor["kind"] == "column"
    table = anchor["table_name"]

    # Containment: the sibling/child COLUMN nodes of the anchor's table, read-scoped. For a column
    # anchor the anchor itself is excluded; for a table anchor these are its children.
    sibling_rows = conn.execute(
        "SELECT object_ref, column_name, data_type, sensitivity FROM graph_node "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
        "AND lower(object_ref) <> lower(%s) "
        "AND visible_requires <@ %s "
        "ORDER BY object_ref",
        (source, table, anchor["object_ref"], allowed),
    ).fetchall()
    siblings = [
        {"object_ref": r[0], "column": r[1], "data_type": r[2], "sensitivity": r[3]}
        for r in sibling_rows
    ]
    table_ref = f"public.{table}"
    containment = {
        "table": {"object_ref": table_ref, "table": table},
        "columns": siblings,   # sibling columns (column anchor) or child columns (table anchor)
    }

    # VERIFIED approved_joins touching the ref. For a column anchor the ref itself is the endpoint;
    # for a table anchor the endpoints are its (read-scoped) column refs.
    if is_column:
        endpoint_refs = [anchor["object_ref"]]
    else:
        endpoint_refs = [s["object_ref"] for s in siblings]
    approved_joins: list[dict] = []
    if endpoint_refs:
        join_rows = conn.execute(
            "SELECT e.from_ref, e.to_ref, e.cardinality, e.approved_join_status, "
            "e.approved_join_fact_key FROM graph_edge e "
            "LEFT JOIN graph_node fn ON fn.object_ref = e.from_ref "
            "  AND fn.catalog_source = e.catalog_source "
            "LEFT JOIN graph_node tn ON tn.object_ref = e.to_ref "
            "  AND tn.catalog_source = e.catalog_source "
            "WHERE e.catalog_source = %s AND e.kind = 'joins' "
            "AND e.approved_join_status = 'VERIFIED' "
            "AND (e.from_ref = ANY(%s) OR e.to_ref = ANY(%s)) "
            # M-5 fail-closed (same leak as the F2b endpoint filters): require BOTH join endpoints to
            # EXIST and pass scope, so a join to an absent/hidden node is OMITTED, never NULL-admitted.
            "AND fn.object_ref IS NOT NULL AND COALESCE(fn.visible_requires, '{}') <@ %s "
            "AND tn.object_ref IS NOT NULL AND COALESCE(tn.visible_requires, '{}') <@ %s "
            "ORDER BY e.from_ref, e.to_ref",
            (source, endpoint_refs, endpoint_refs, allowed, allowed),
        ).fetchall()
        approved_joins = [
            {"from_ref": r[0], "to_ref": r[1], "cardinality": r[2], "status": r[3],
             "approved_join_fact_key": r[4]}
            for r in join_rows
        ]

    semantic, semantic_unavailable = _semantic_subsection(
        conn, source, anchor, allowed, roles, identity)
    section = {
        "containment": containment,
        "approved_joins": approved_joins,
        "semantic": semantic,
        # Cross-catalog links — the SAME business entity in another catalog. Included whether or not
        # a human has confirmed them: confirmation ANNOTATES, it does not gate visibility. Before
        # this the candidate ledger had no readers at all, so a derived link
        # (`cib.cust_num <-> ftr.cif_id`) existed in the database and appeared nowhere.
        "cross_catalog": [
            {**asdict(link), "status": str(link.status), "why": link.why}
            for link in cross_catalog_links(conn, object_ref=anchor["object_ref"])
        ],
    }
    return section, semantic_unavailable


def _semantic_subsection(
    conn: DbConn, source: str, anchor: dict, allowed: list[str], roles: Iterable[str],
    identity: IdentityEnvelope | None,
) -> tuple[dict, list[str]]:
    """The F2b SEMANTIC relationship subsection for the anchor (Delivery E data): VERIFIED entity +
    currency edges, the current-set candidate history, the declared≠governed divergence signal, and
    the server-calculated governance actions the CALLER may run per binding.

    READ-SCOPED, fail-closed, no leak — mirrors the peer sections:
    * PERMISSION gate: the subsection needs catalog:read (the SAME permission the route + the other
      sections use). A caller who lacks it gets ``{"status": "unavailable"}`` and the subsection named
      in ``unavailable_sections`` — explicit, never an empty-success that reads as "no semantic links",
      and never a hidden count.
    * SENSITIVITY: every related endpoint (a currency edge's other column, a candidate's other column)
      is filtered IN SQL, so a hidden node makes its edge/candidate simply ABSENT — no count, no id.
    * VERIFIED is rendered DISTINCTLY from proposed: ``verified_edges`` carry ``status="VERIFIED"`` +
      their fact_key / confirmed_event_id provenance; a DRAFT candidate carries ``fact_status`` folded
      via E2 (DRAFT → ``PROPOSED``). Semantic bindings are per-column, so a table anchor gets an
      explicit empty-but-available subsection (its columns carry their own bindings).
    * ACTIONS reuse E2's owner-or-admin available-actions authz (``caller_binding_actions``) — the
      asset UI may NOT advertise an edge as editable unless the server returns the command here."""
    if not has_permission(roles, CATALOG_READ):
        # Fail closed: withheld, explicit, no hidden count (mirrors the F0 unavailable contract).
        return {"status": "unavailable"}, ["relationships.semantic"]
    if anchor["kind"] != "column":
        # Semantic bindings attach to columns; a table asset has none of its own. Explicit empty.
        return {"status": "available", "verified_edges": [], "candidates": [], "divergences": []}, []

    object_ref = anchor["object_ref"]
    verified_edges: list[dict] = []
    divergences: list[dict] = []

    # VERIFIED entity edge + the divergence signal, from the anchor's OWN governed graph_node row
    # (the anchor already passed read-scope). A governed entity_assignment sets entity + entity_status
    # 'VERIFIED' + provenance links; a conflicting re-upload leaves the file value in declared_entity.
    ent = conn.execute(
        "SELECT entity, declared_entity, entity_status, entity_fact_key, entity_fact_event_id "
        "FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (source, object_ref),
    ).fetchone()
    if ent is not None:
        entity, declared_entity, entity_status, e_fact_key, e_event_id = ent
        if entity_status == "VERIFIED" and entity is not None:
            actions = (caller_binding_actions(conn, fact_key=e_fact_key, actor=identity)["actions"]
                       if e_fact_key else [])
            verified_edges.append({
                "kind": "entity_assignment", "status": "VERIFIED", "object_ref": object_ref,
                "entity": entity, "fact_key": e_fact_key, "confirmed_event_id": e_event_id,
                "available_actions": actions,
            })
            if declared_entity is not None and _norm(declared_entity) != _norm(entity):
                # DIVERGENCE: a re-upload declared a DIFFERENT entity; the governed value wins and the
                # file value is preserved as declared_entity — the two differing IS the signal.
                divergences.append({
                    "kind": "entity_divergence", "object_ref": object_ref,
                    "declared_entity": declared_entity, "governed_entity": entity,
                    "fact_key": e_fact_key,
                })

    # VERIFIED currency edges touching the anchor (measure→currency), read-scoped on BOTH endpoints —
    # a hidden currency/measure column omits the whole edge (mirrors approved_joins). A
    # FIXED-CURRENCY literal edge (Task 4) has NO target column (to_ref NULL, currency_code set):
    # only its measure endpoint is scoped — there is no second column to hide.
    for fk, from_ref, to_ref, ccode, kind, status, conf_event in conn.execute(
        "SELECT e.fact_key, e.from_ref, e.to_ref, e.currency_code, e.kind, e.status, "
        "e.confirmed_event_id "
        "FROM semantic_binding_edge e "
        "LEFT JOIN graph_node fn ON fn.object_ref = e.from_ref "
        "  AND fn.catalog_source = e.catalog_source "
        "LEFT JOIN graph_node tn ON tn.object_ref = e.to_ref "
        "  AND tn.catalog_source = e.catalog_source "
        "WHERE e.catalog_source = %s AND e.status = 'VERIFIED' "
        "AND (e.from_ref = %s OR e.to_ref = %s) "
        # M-5 fail-closed: a MISSING endpoint row must NOT read as visible (a LEFT JOIN leaves its
        # sensitivity NULL, which `IS NULL` would wrongly admit) — require BOTH endpoints to EXIST
        # and pass scope, so an edge to an absent/hidden node is OMITTED (no leak). A literal edge
        # has no target endpoint to require.
        "AND fn.object_ref IS NOT NULL AND COALESCE(fn.visible_requires, '{}') <@ %s "
        "AND (e.to_ref IS NULL OR "
        "     (tn.object_ref IS NOT NULL AND COALESCE(tn.visible_requires, '{}') <@ %s)) "
        "ORDER BY e.from_ref, e.to_ref",
        (source, object_ref, object_ref, allowed, allowed),
    ).fetchall():
        verified_edges.append({
            "kind": kind, "status": status, "from_ref": from_ref, "to_ref": to_ref,
            "currency_code": ccode,
            "fact_key": fk, "confirmed_event_id": conf_event,
            "available_actions": caller_binding_actions(conn, fact_key=fk, actor=identity)["actions"],
        })

    # Current-set semantic candidates touching the anchor (disposition + reason codes), read-scoped on
    # both endpoints. The proposal LINK (if any) folds to a fact_status (DRAFT → PROPOSED via E2) so a
    # candidate is shown as proposed, distinct from VERIFIED; a linked binding also carries its actions.
    candidates: list[dict] = []
    for cid, kind, disposition, reason_codes, subj, tgt, proposed_value, fk in conn.execute(
        "SELECT c.candidate_id, c.binding_kind, c.disposition, c.reason_codes, "
        "c.subject_graph_ref, c.target_graph_ref, c.proposed_value, p.fact_key "
        "FROM current_semantic_binding_candidate_set cur "
        "JOIN semantic_binding_candidate c ON c.candidate_set_id = cur.candidate_set_id "
        "LEFT JOIN semantic_binding_candidate_proposal p ON p.candidate_id = c.candidate_id "
        "LEFT JOIN graph_node sn ON sn.object_ref = c.subject_graph_ref "
        "  AND sn.catalog_source = c.catalog_source "
        "LEFT JOIN graph_node tn ON tn.object_ref = c.target_graph_ref "
        "  AND tn.catalog_source = c.catalog_source "
        "WHERE cur.catalog_source = %s AND cur.status = 'current' "
        "AND (c.subject_graph_ref = %s OR c.target_graph_ref = %s) "
        # M-5 fail-closed: require BOTH endpoints to EXIST and pass scope — a candidate whose other
        # endpoint has no graph_node row (or is hidden) is OMITTED, never admitted via a NULL join.
        "AND sn.object_ref IS NOT NULL AND COALESCE(sn.visible_requires, '{}') <@ %s "
        "AND tn.object_ref IS NOT NULL AND COALESCE(tn.visible_requires, '{}') <@ %s "
        "ORDER BY c.subject_graph_ref, c.binding_kind, c.candidate_id",
        (source, object_ref, object_ref, allowed, allowed),
    ).fetchall():
        if fk:
            gov = caller_binding_actions(conn, fact_key=fk, actor=identity)
            fact_status, actions = gov["status"], gov["actions"]
        else:
            fact_status, actions = None, []
        candidates.append({
            "candidate_id": cid, "binding_kind": kind, "disposition": disposition,
            "reason_codes": reason_codes if isinstance(reason_codes, list) else [],
            "subject_graph_ref": subj, "target_graph_ref": tgt, "proposed_value": proposed_value,
            "fact_key": fk, "fact_status": fact_status, "available_actions": actions,
        })

    return {"status": "available", "verified_edges": verified_edges,
            "candidates": candidates, "divergences": divergences}, []


def _readiness_section(
    conn: DbConn, source: str, anchor: dict, roles: Iterable[str]
) -> dict:
    """The F0-T1 per-column capability MATRIX (columns only) + the parent-table blocker diagnostic
    (:func:`compute_readiness` TABLE scope). Both are pure reads; ``roles`` read-scopes BOTH the
    matrix's join-connectivity read AND the table diagnostic (F1): the table diagnostic is computed
    under the caller's read-scope so a hidden sensitivity-restricted sibling column never leaks its
    name/id/count via a ``field:...`` requirement, an ``advisory_gaps`` ref, or a ``summary_scores``
    tally — the same no-leak guarantee the anchor load and relationships section already carry."""
    section: dict = {}
    caps = (column_readiness(conn, source=source, object_ref=anchor["object_ref"], roles=roles)
            if anchor["kind"] == "column" else None)
    # A table asset has no per-column matrix.
    section["column_capabilities"] = asdict(caps) if caps is not None else None
    diagnostic = compute_readiness(
        conn, source=source, scope=ReadinessScopeType.TABLE, subset=anchor["table_name"],
        roles=roles,
    )
    # The parent table as a ROLL-UP, not as rows. Shipping the diagnostic whole meant 341 blocking
    # plus 445 review rows on EVERY column page, to say one thing — all 341 shared the single cause
    # `unresolved_authority`. The full lists keep their dedicated home at
    # `GET /sources/{source}/readiness?subset={table}`, which the "show all" disclosure fetches, so
    # nothing is lost and the common page stops carrying 786 rows it never displays usefully.
    section["table_rollup"] = asdict(table_rollup(diagnostic, table=anchor["table_name"]))
    # The product view of this column: per-role usability in plain English. The raw capability
    # matrix stays above it — this is a translation, not a replacement, so anything reading
    # `column_capabilities` is untouched.
    # Computed from the SAME matrix above — one read, two views.
    section["usability"] = asdict(column_usability(caps)) if caps is not None else None
    return section


def _history_section(conn: DbConn, source: str, object_ref: str) -> dict:
    """The reverse ``ingestion_run_object`` lookup (which runs observed/changed this ref, newest
    first) + each run's per-stage outcomes.

    BOUNDED + BATCHED (F5): the run list is capped at :data:`_HISTORY_RUN_LIMIT` newest associations
    (``truncated`` is ``True`` when more exist), and every listed run's stages are fetched in ONE
    ``ingestion_run_id = ANY(...)`` query grouped in Python — so a daily-ingested ref costs a constant
    2 queries and a bounded payload inside the RR transaction, never the prior 1 + N-runs round trips
    and unbounded body. ``lower(o.object_ref)`` is kept (NOT F7'd to the PK) so the case-folded
    predicate uses the purpose-built ``ingestion_run_object_source_ref_at_idx`` functional index."""
    run_rows = conn.execute(
        "SELECT o.ingestion_run_id, o.relation, o.at, r.status, r.origin_type, r.started_at, "
        "r.completed_at FROM ingestion_run_object o "
        "JOIN ingestion_run r ON r.id = o.ingestion_run_id "
        "WHERE o.catalog_source = %s AND lower(o.object_ref) = lower(%s) "
        "ORDER BY o.at DESC, o.ingestion_run_id "
        "LIMIT %s",
        (source, object_ref, _HISTORY_RUN_LIMIT + 1),   # +1 sentinel row detects truncation
    ).fetchall()
    truncated = len(run_rows) > _HISTORY_RUN_LIMIT
    run_rows = run_rows[:_HISTORY_RUN_LIMIT]

    run_ids = [row[0] for row in run_rows]
    stages_by_run: dict[str, list[dict]] = {}
    if run_ids:
        for run_id, stage, attempt, state, reason_code in conn.execute(
            "SELECT ingestion_run_id, stage, attempt, state, reason_code FROM ingestion_run_stage "
            "WHERE ingestion_run_id = ANY(%s) ORDER BY ingestion_run_id, id",
            (run_ids,),
        ).fetchall():
            stages_by_run.setdefault(run_id, []).append(
                {"stage": stage, "attempt": attempt, "state": state, "reason_code": reason_code}
            )

    runs = [
        {"ingestion_run_id": run_id, "relation": relation, "at": at, "status": status,
         "origin_type": origin_type, "started_at": started_at, "completed_at": completed_at,
         "stages": stages_by_run.get(run_id, [])}
        for run_id, relation, at, status, origin_type, started_at, completed_at in run_rows
    ]
    return {"runs": runs, "truncated": truncated}


def _audit_section(conn: DbConn, source: str, object_ref: str, logical_ref: str) -> dict:
    """F2-audit: subject-linked LLM-call audit SUMMARIES for this ref — which dispatches touched it,
    their task/stage/provider/model/versions, transport outcome and timestamps.

    SAFE-ONLY: this returns NO ``redacted_input`` (the egress-approved request body), NO raw model
    output, and NO repair body — those stay in the restricted audit store and are never surfaced here.
    The caller reaches this function ONLY after the ``audit:read`` gate in :func:`build_asset_detail`
    (a caller without it gets the section listed in ``unavailable_sections`` instead — no hidden
    count). Bounded to the newest :data:`_AUDIT_SUMMARY_LIMIT` dispatches; the reverse lookup rides the
    1016 ``llm_dispatch_subject (catalog_source, object_ref | logical_ref)`` indexes."""
    rows = conn.execute(
        "SELECT d.dispatch_ref, d.task, d.stage, d.provider, d.model, d.prompt_version, "
        "       d.schema_version, d.created_at, s.field_names, o.outcome, o.recorded_at "
        "FROM llm_dispatch_subject s "
        "JOIN llm_dispatch d ON d.dispatch_ref = s.dispatch_ref "
        "LEFT JOIN LATERAL ("
        "    SELECT outcome, recorded_at FROM llm_dispatch_outcome oo "
        "    WHERE oo.dispatch_ref = s.dispatch_ref ORDER BY oo.recorded_at DESC LIMIT 1"
        ") o ON true "
        "WHERE s.catalog_source = %s AND (s.object_ref = %s OR s.logical_ref = %s) "
        "ORDER BY d.created_at DESC, d.dispatch_ref "
        "LIMIT %s",
        (source, object_ref, logical_ref, _AUDIT_SUMMARY_LIMIT + 1),
    ).fetchall()
    truncated = len(rows) > _AUDIT_SUMMARY_LIMIT
    summaries = [
        {"dispatch_ref": dref, "task": task, "stage": stage, "provider": provider, "model": model,
         "prompt_version": prompt_v, "schema_version": schema_v, "created_at": created_at,
         "field_names": field_names, "outcome": outcome, "outcome_at": recorded_at}
        for (dref, task, stage, provider, model, prompt_v, schema_v, created_at, field_names,
             outcome, recorded_at) in rows[:_AUDIT_SUMMARY_LIMIT]
    ]
    return {"status": "available", "summaries": summaries, "truncated": truncated}


def _semantic_adjudication_section(conn: DbConn, logical_ref: str) -> dict:
    """The column's CURRENT semantic adjudication, read through the migration-1046 subject/current
    pointer — one pointer row plus one revision row, never a scan over ``output_json``.

    ``status`` is ``available`` or ``absent``; ABSENT is the NORMAL state (adjudication is the
    exception path — most columns are clear and were never referred), so it is rendered as a plain
    empty section, never as a gap or a failure. ``confidence_band`` rides here as EXPLANATION for
    the reader: nothing on this surface, or behind it, may treat it as authority — the adjudicated
    concept is `llm/proposed` evidence like any other, and its authority is shown by the
    ``evidence``/``effective_metadata`` sections that already carry the producer/strength triple.

    The anchor passed read-scope before this runs, and nothing here reaches beyond its own
    ``logical_ref``."""
    from featuregen.overlay.upload.semantic_adjudication import load_current_adjudication

    adjudication, result_id = load_current_adjudication(conn, logical_ref)
    if adjudication is None:
        return {"status": "absent"}
    gap = adjudication.ontology_gap
    return {
        "status": "available",
        "structured_result_id": result_id,
        "selected_concept": adjudication.selected_concept,
        "alternatives": list(adjudication.alternatives),
        # Explanatory only — never authority (semantic Task 5 contract).
        "confidence_band": adjudication.confidence_band,
        "reason_codes": list(adjudication.reason_codes),
        "missing_context": list(adjudication.missing_context),
        "ontology_gap": None if gap is None else gap.as_dict(),
    }


def _consistency_token(body: dict) -> str:
    """A content-hash fingerprint of the assembled snapshot — the ``consistency_token`` and HTTP
    ``ETag``. Canonical JSON (sorted keys, ``default=str`` for datetimes) so the same snapshot
    always yields the same token."""
    blob = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_asset_detail(
    conn: DbConn, *, source: str, object_ref: str, roles: Iterable[str],
    include: Iterable[str] | None = None, identity: IdentityEnvelope | None = None,
) -> dict | None:
    """Assemble the bounded F0 sections for ONE catalog asset ``(source, object_ref)``.

    READ-ONLY, and read-scoped: the anchor is loaded under the caller's sensitivity scope FIRST.
    Returns ``None`` when the anchor does not exist OR is hidden from these ``roles`` (the route
    maps ``None`` to 404 — a hidden object is indistinguishable from a missing one, no existence
    leak). ``include`` selects which sections to build (default: every F0 section); ``identity`` is
    always built. Every section is assembled from already-shipped authority (C0/C1/F0-T1/readiness/
    the run manifest) — nothing here re-derives value, authority, or readiness.

    Sections that cannot be served for THIS caller are listed in ``unavailable_sections`` without a
    hidden count. The SEMANTIC relationship subsection (F2b) is real, read-scoped Delivery-E data;
    ``identity`` (the authenticated caller — the route passes the session principal) is used ONLY to
    compute the per-binding governance actions the caller may run (owner-or-admin, E2 authz). With no
    ``identity`` the subsection still renders read-only (no actions). Assemble under ONE
    ``REPEATABLE READ`` transaction (the caller's ``conn``) so every section describes one torn-free
    snapshot; ``consistency_token`` fingerprints it.
    """
    norm_source = source.strip().lower()
    allowed = allowed_sensitivities(roles)
    anchor = _load_anchor(conn, norm_source, object_ref, allowed)
    if anchor is None:
        return None

    requested = set(include) if include is not None else set(_F0_SECTIONS)
    logical_ref = logical_ref_of(conn, norm_source, anchor["object_ref"])
    is_column = anchor["kind"] == "column"

    unavailable: list[str] = []
    body: dict = {
        "version": ASSET_DETAIL_VERSION,
        "source": norm_source,
        "object_ref": anchor["object_ref"],
        "kind": anchor["kind"],
    }

    # identity is always present (the anchor's core).
    body["identity"] = _identity_section(anchor, logical_ref)
    built = ["identity"]

    if "effective_metadata" in requested:
        if is_column:
            body["effective_metadata"] = _effective_metadata_section(conn, logical_ref, anchor)
        else:
            body["effective_metadata"] = {"fields": {}, "note": "table asset — no per-field metadata"}
        built.append("effective_metadata")

    if "evidence" in requested:
        body["evidence"] = _evidence_section(conn, logical_ref)
        built.append("evidence")

    if "source_glossary" in requested:
        body["source_glossary"] = _source_glossary_section(conn, logical_ref)
        built.append("source_glossary")

    if "semantic_adjudication" in requested:
        # Columns only: adjudication answers "which concept is this column?", a question a TABLE
        # anchor does not have. An honest empty section beats a fabricated one.
        body["semantic_adjudication"] = (
            _semantic_adjudication_section(conn, logical_ref) if is_column
            else {"status": "absent", "note": "table asset — no column adjudication"})
        built.append("semantic_adjudication")

    if "relationships" in requested:
        relationships, rel_unavailable = _relationships_section(
            conn, norm_source, anchor, allowed, roles, identity)
        body["relationships"] = relationships
        unavailable.extend(rel_unavailable)
        built.append("relationships")

    if "readiness" in requested:
        body["readiness"] = _readiness_section(conn, norm_source, anchor, roles)
        built.append("readiness")

    if "history" in requested:
        body["history"] = _history_section(conn, norm_source, anchor["object_ref"])
        built.append("history")

    if "actions" in requested:
        # F0: no server-calculated commands yet — the correction command is Delivery F0-T4.
        body["actions"] = []
        built.append("actions")

    if "audit" in requested:
        # Separately authorized (F2-audit): the LLM-audit-summaries section needs audit:read. A caller
        # without it gets the section named in unavailable_sections — explicit, NO 403, NO hidden count
        # (exactly the feature:read gating contract). SAFE summaries only; raw inputs/outputs restricted.
        if has_permission(roles, AUDIT_READ):
            body["audit"] = _audit_section(conn, norm_source, anchor["object_ref"], logical_ref)
            built.append("audit")
        else:
            unavailable.append("audit")

    body["included_sections"] = built
    body["unavailable_sections"] = unavailable
    body["consistency_token"] = _consistency_token(body)
    return body
