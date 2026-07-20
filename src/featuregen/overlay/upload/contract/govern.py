"""Phase 5 — confirm + govern (versioned, drift-linked).

`confirm_contract` is the HUMAN GATE — the only write that makes a contract governing. It registers the
draft as a versioned feature contract and wires its derives-from into the feature layer, so freshness
lineage and drift impact apply for free: a governed contract KNOWS when its inputs drifted. A re-confirm
of the same feature is a new version; history stays.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload import feature_validation_projection
from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
from featuregen.overlay.upload.contract._serial import requirements_to_json
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.invalidation import (
    _MISSING,
    _catalog_state_signature,
    confirm_dependency_hash,
    dependencies_drifted,
    has_dependency_rows,
    join_edge_marker,
)
from featuregen.overlay.upload.contract.review import validate_minimum
from featuregen.overlay.upload.features import (
    FeatureFreshness,
    FeatureSpec,
    consumers_of_feature,
    feature_freshness,
    features_affected_by,
    get_feature,
    register_feature,
)
from featuregen.overlay.upload.validation_requirements import DEFAULT_SCHEMA_VERSION, schema_for

# Delivery C4-T3 / C2-C3 review (I-1a): the immutable-requirement schema version stamped on each
# persisted `feature_validation_requirement` row is the REGISTRY's OWN version. Each row now stamps the
# requirement's OWN `schema_version` (the registry "v1"), so a downstream `schema_for(code, version)`
# RESOLVES — the previous separate "req-schema-v1" namespace could not be resolved by the registry. This
# constant is the unified registry default, kept as the shared reference (e.g. the pre-C0 fingerprint).
# A re-assessment against a NEW schema version yields NEW rows (the 1009 UNIQUE key includes it).
REQUIREMENT_SCHEMA_VERSION = DEFAULT_SCHEMA_VERSION


class ContractValidationError(Exception):
    """The draft failed the deterministic MCV — it must not be governed."""


class ContractPointerConflict(Exception):
    """H2b fail-closed: the ``feature_current_contract`` compare-and-swap matched 0 rows — a concurrent
    confirm advanced the pointer between this confirm's pointer READ and its CAS. The per-feature advisory
    lock (taken at the top of ``confirm_contract``) makes this UNREACHABLE in practice; it is kept as
    defense-in-depth so the pointer can NEVER be lost-updated. Raising it aborts the whole confirm
    transaction (no torn write). (H2d should map it at the API like ``UniqueViolation`` -> 409.)"""


def feature_contract_lock_key(feature_name: str) -> int:
    """H2b — stable 64-bit advisory-lock key serializing confirms of ONE feature identity.

    sha256 over a dedicated ``contract_confirm:`` namespace of the NORMALIZED feature identity
    (``.strip().lower()`` — the repo's catalog/feature key convention), first 8 bytes big-endian signed
    — the exact derivation ``ingest_source_lock_key`` uses, under a DISTINCT prefix so this key space
    cannot collide with the ingest / drift / renewal namespaces nor the fixed constants (security-chain
    7_000_007, migrations 6157423001, global-seq 4_201_873_355_201_001). Identical feature identities
    ALWAYS derive the same key (the serialization invariant); a superset of DB-distinct case/whitespace
    variants merely over-serializes (safe). MUST stay stable across releases: two versions deriving
    different keys for the same feature would stop excluding each other during a rolling deploy,
    re-opening the pointer-CAS interleave this lock exists to close."""
    normalized = feature_name.strip().lower()
    return int.from_bytes(
        hashlib.sha256(f"contract_confirm:{normalized}".encode()).digest()[:8],
        "big", signed=True)


def _contract_input_items(conn, draft: ContractDraft):
    """H2b — expand the RECONCILED draft into role-labelled input items (the immutable lineage a contract
    version was built from). Yields ``(source, graph_ref, logical_ref, physical_ref, role, decision_id,
    fact_id)``. Reflects the POST-reconciliation (Slice-3 server-authoritative grain/derives) values,
    EXACTLY like the feature/feature_derives_from compat writes — the draft handed to ``confirm_contract``
    is already reconciled upstream (the route overwrites grain_table/derives_from/join_path from the
    server-reconstructed chosen feature before calling confirm).

    Completeness: derives + grain + as_of + governed join-path columns all become rows — derives-pairs-
    only lineage is INCOMPLETE. decision_id/fact_id are NULL here: the draft carries no field-decision /
    governed-fact id yet (H2c reverse-dependency + H1b governed-support wire those)."""
    catalogs = {cs for cs, _ in draft.derives_pairs}
    # grain_table / as_of_column are bare names on the draft; attribute them to the catalog that ACTUALLY
    # holds the grain-table node (single-catalog: the one catalog; cross-catalog: the source whose
    # graph_node carries public.<grain_table>, else sorted[0]) — the SAME resolution the dependency-item
    # build uses, so the input-lineage catalog_source matches the dependency row instead of a
    # sorted(catalogs)[0] that mis-attributes a cross-catalog grain to the wrong source.
    grain_source = _grain_catalog(conn, draft.grain_table, catalogs)
    # derives — every measure/source column the feature reads (B3 carried pairs).
    for cs, ref in draft.derives_pairs:
        yield (cs, ref, ref, ref, "derives", None, None)
    # grain — the server-authoritative grain table (Slice-3 reconciled).
    if draft.grain_table and grain_source is not None:
        yield (grain_source, None, draft.grain_table, draft.grain_table, "grain", None, None)
    # as_of — the grain table's as-of column.
    if draft.as_of_column and grain_source is not None:
        yield (grain_source, None, draft.as_of_column, draft.as_of_column, "as_of", None, None)
    # governed join-path columns — each join step's target ref, so the lineage is COMPLETE.
    # # H1b: ungoverned support-column requirement/rejection is wired there; this task records the
    # roles the reconciled draft already carries and drops NONE silently.
    for step in draft.join_path:
        ref = step.get("ref") or step.get("to")
        if not ref:
            continue
        yield (step.get("catalog_source") or grain_source or "", ref, ref, ref, "join", None, None)


def _insert_contract_input_columns(conn, contract_id: str, draft: ContractDraft) -> None:
    """H2b — persist the write-once ``contract_input_column`` lineage for THIS contract version. The
    ``item_hash`` is a deterministic content hash of (contract_id, role, refs, decision/fact ids), so the
    same input under a different contract version hashes differently and two distinct inputs of one
    contract never collide. ON CONFLICT DO NOTHING makes the insert idempotent (a re-run is a no-op — the
    1011 no-mutation trigger blocks UPDATE/DELETE, NOT an INSERT that DO-NOTHINGs)."""
    for source, graph_ref, logical_ref, physical_ref, role, decision_id, fact_id in \
            _contract_input_items(conn, draft):
        item_hash = canonical_hash({
            "contract_id": contract_id, "source": source, "graph_ref": graph_ref,
            "logical_ref": logical_ref, "physical_ref": physical_ref, "role": role,
            "decision_id": decision_id, "fact_id": fact_id})
        conn.execute(
            "INSERT INTO contract_input_column (contract_id, source, graph_ref, logical_ref, "
            "physical_ref, role, decision_id, fact_id, item_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (contract_id, item_hash) DO NOTHING",
            (contract_id, source, graph_ref, logical_ref, physical_ref, role, decision_id, fact_id,
             item_hash))


def _grain_catalog(conn, grain_table: str | None, catalogs: set[str]) -> str | None:
    """C-2 — the catalog that ACTUALLY holds the grain TABLE node, resolved among the derives catalogs.
    Single-catalog: the one catalog. Cross-catalog: the catalog whose ``graph_node`` has
    ``public.<grain_table>`` (never ``sorted(catalogs)[0]``, which mis-attributes a cross-catalog grain
    to the wrong source → a MISSING, self-matching dependency baseline). Returns None when there is no
    grain; falls back to ``sorted(catalogs)[0]`` when no catalog holds the node (the confirm-hash poison
    then fails that unresolvable grain closed)."""
    if not grain_table or not catalogs:
        return None
    if len(catalogs) == 1:
        return next(iter(catalogs))
    t_ref = f"public.{grain_table}".lower()
    row = conn.execute(
        "SELECT catalog_source FROM graph_node WHERE catalog_source = ANY(%s) "
        "AND lower(object_ref) = %s AND kind = 'table' ORDER BY catalog_source LIMIT 1",
        (sorted(catalogs), t_ref)).fetchone()
    return row[0] if row is not None else sorted(catalogs)[0]


def _grain_key_column_ref(conn, catalog_source: str, grain_table: str) -> str | None:
    """C-1 — the grain-KEY column node (``is_grain = true``) GRAIN_IS_UNIQUE consulted, deterministic on
    ``object_ref``. Recording THIS node (its state signature carries ``is_grain``) is what lets the read
    gate catch a flipped ``is_grain`` / a dropped grain fact — ``project_table_facts_for_ref`` clears the
    column's ``is_grain`` when the governed fact staled/rejected. None when the grain has no is_grain
    column (GRAIN_IS_UNIQUE had no grain operand to clear)."""
    row = conn.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND is_grain = true AND kind = 'column' ORDER BY object_ref LIMIT 1",
        (catalog_source, grain_table)).fetchone()
    return row[0] if row is not None else None


def _contract_dependency_items(conn, draft: ContractDraft):
    """H2c — the reverse-dependency items a contract version depends on for drift fan-out AND the
    read-time fail-closed gate. Records the SAME catalog items that back a ``contract_input_column`` row
    (derives + grain + as_of + join) AND, per H2 review C-1, the items that actually CLEAR the blocking
    checks: the grain-KEY column (``is_grain`` clears GRAIN_IS_UNIQUE), and each join step's FROM-side
    column + the clearing join EDGE (``graph_edge`` + approved-join status clears JOIN_CONNECTIVITY).
    Yields ``(catalog_source, graph_ref, object_ref, decision_id, fact_id, event_id)``; decision_id /
    fact_id / event_id are NULL (the reconciled draft carries no ids yet — # H1b wires those). C-2: the
    grain catalog is resolved to the source that holds the grain-table node (not ``sorted[0]``), so a
    cross-catalog grain's dep row is RESOLVABLE rather than a self-matching MISSING baseline."""
    catalogs = {cs for cs, _ in draft.derives_pairs}
    grain_source = _grain_catalog(conn, draft.grain_table, catalogs)
    # derives — every measure/source column the feature reads (already a public-flattened object_ref).
    for cs, ref in draft.derives_pairs:
        yield (cs, ref, ref, None, None, None)
    if draft.grain_table and grain_source is not None:
        # grain — the server-authoritative grain TABLE node (public.<table>).
        t_ref = f"public.{draft.grain_table}".lower()
        yield (grain_source, t_ref, t_ref, None, None, None)
        # C-1 grain-KEY column — the is_grain column GRAIN_IS_UNIQUE cleared. Flipping is_grain or
        # dropping the grain fact changes/removes this node's signature → the read gate downgrades.
        gref = _grain_key_column_ref(conn, grain_source, draft.grain_table)
        if gref is not None:
            yield (grain_source, gref, gref, None, None, None)
    # as_of — the grain table's as-of COLUMN node (public.<table>.<column>).
    if draft.as_of_column and draft.grain_table and grain_source is not None:
        c_ref = f"public.{draft.grain_table}.{draft.as_of_column}".lower()
        yield (grain_source, c_ref, c_ref, None, None, None)
    # join-path — each step's TARGET ref plus (C-1) its FROM-side column and the clearing join EDGE.
    for step in draft.join_path:
        step_catalog = step.get("catalog_source") or grain_source or ""
        to_ref = step.get("ref") or step.get("to")
        if to_ref:
            yield (step_catalog, to_ref, to_ref, None, None, None)
        from_ref = step.get("from")
        if from_ref:
            yield (step_catalog, from_ref, from_ref, None, None, None)
        # C-1 — the clearing join EDGE (single-catalog column-level 'join' step). Recorded under a
        # marker so the read gate hashes the graph_edge's existence + approved-join state: dropping the
        # edge or losing the VERIFIED approval downgrades. (Cross-catalog governed_segment steps carry
        # no from/to edge; their ref rides the target-ref item above, poison-failed if unresolvable.)
        if step.get("kind") == "join" and from_ref and to_ref:
            marker = join_edge_marker(from_ref, to_ref)
            yield (step_catalog, marker, marker, None, None, None)


def _insert_contract_metadata_dependencies(conn, contract_id: str, draft: ContractDraft) -> None:
    """H2c — persist the write-once ``contract_metadata_dependency`` reverse-dep rows for THIS contract
    version (one per check-clearing / input-binding catalog item), in the SAME transaction as the input
    rows. ``item_hash`` is the item's content hash AT CONFIRM — its identity refs + its CURRENT
    ``graph_node`` state signature (the value/type that made it load-bearing) — so the read-time gate can
    recompute the current hash and HARD-downgrade the contract on any mismatch. ON CONFLICT DO NOTHING
    keeps it idempotent (the 1011 no-mutation trigger blocks UPDATE/DELETE, not a DO-NOTHING INSERT)."""
    for catalog_source, graph_ref, object_ref, decision_id, fact_id, event_id in \
            _contract_dependency_items(conn, draft):
        item_hash = confirm_dependency_hash(
            conn, contract_id=contract_id, catalog_source=catalog_source, graph_ref=graph_ref,
            logical_ref=object_ref, decision_id=decision_id, fact_id=fact_id, event_id=event_id)
        conn.execute(
            "INSERT INTO contract_metadata_dependency (contract_id, catalog_source, graph_ref, "
            "logical_ref, decision_id, fact_id, event_id, item_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (contract_id, item_hash) DO NOTHING",
            (contract_id, catalog_source, graph_ref, object_ref, decision_id, fact_id, event_id,
             item_hash))


# ── H1b — Gate-1 role-binding confirmation hash ─────────────────────────────────────────────────────
# The draft exposes the exact role bindings + a deterministic ``binding_hash``; the confirm requires that
# hash and 409s if the server-authoritative bindings DRIFTED since draft (a column retyped, a fact
# retired/expired, an authority changed). It is the ROLE-BINDING analog of the plan-staleness 409, over
# the SAME reconciled inputs H2b persists as ``contract_input_column`` rows. Reuses the ONE hash scheme
# (``canonical_hash``, H2b/H2c) + H2c's ``_catalog_state_signature`` — NO second hash, NO new machinery.
def _binding_state_ref(role: str, logical_ref: str | None, grain_table: str | None) -> str | None:
    """The PUBLIC-FLATTENED graph object_ref whose CURRENT state signature backs a binding's drift check.
    derives / join refs are already public-flattened; the BARE grain-TABLE and as_of-COLUMN names the
    input rows carry are flattened here (mirroring ``_contract_dependency_items``' ref rules) so a
    retype / retire / expire of the underlying node changes the signature — a bare name would resolve
    MISSING and never move."""
    if role == "grain":
        return f"public.{grain_table}".lower() if grain_table else None
    if role == "as_of":
        return (f"public.{grain_table}.{logical_ref}".lower()
                if grain_table and logical_ref else None)
    return logical_ref


def _binding_authority(state) -> tuple[str, list[str]]:
    """A human-facing authority label + warnings DERIVED from a binding's CURRENT catalog-state signature
    (the same signature the hash folds, so exposure and gate can never disagree). ``MISSING`` ⟹ the ref
    left the catalog; a governed fact flag (``is_grain`` / ``is_as_of``) or a classification status ⟹
    ``governed``; a sensitivity tag surfaces a read-scope-restricted binding."""
    if state == _MISSING:
        return "missing", ["ref_not_in_catalog"]
    warnings: list[str] = []
    if state.get("sensitivity"):
        warnings.append(f"sensitivity:{state['sensitivity']}")
    governed = bool(state.get("is_grain") or state.get("is_as_of")
                    or state.get("classification_status"))
    return ("governed" if governed else "declared"), warnings


def confirmed_role_bindings(conn, draft: ContractDraft) -> list[dict]:
    """The ordered, server-authoritative role bindings the confirm PERSISTS as ``contract_input_column``
    rows — reused for BOTH the ``/contract/draft`` exposure and the confirm-time binding-hash gate. Reuses
    ``_contract_input_items`` (the exact reconciled inputs: derives + grain + as_of + governed join) and
    H2c's ``_catalog_state_signature`` (the current load-bearing state — retype/retire/expire/unauthorize
    all move it). READ-ONLY: it never writes, so computing bindings at draft/confirm mutates no global
    field/fact authority. The internal ``_state`` signature is folded into the hash but omitted from the
    exposure."""
    bindings: list[dict] = []
    for source, _graph_ref, logical_ref, physical_ref, role, decision_id, fact_id in \
            _contract_input_items(conn, draft):
        state = _catalog_state_signature(
            conn, source, _binding_state_ref(role, logical_ref, draft.grain_table))
        authority, warnings = _binding_authority(state)
        bindings.append({
            "role": role, "source": source, "ref": physical_ref or logical_ref,
            "decision_id": decision_id, "fact_id": fact_id,
            "authority": authority, "warnings": warnings, "_state": state})
    return bindings


def binding_hash(bindings: list[dict]) -> str | None:
    """A stable content hash over the SORTED per-binding hashes of the confirmed role bindings — reuses
    ``canonical_hash`` (H2b/H2c's ONE scheme; no second hash). Contract-INDEPENDENT (no ``contract_id``),
    so a draft and its confirm derive the SAME value for the SAME reconciled bindings; ANY drift
    (retype / retire / expire / authority change) moves a binding's ``_state`` → a different hash → the
    confirm 409s. ``None`` when there are no bindings (nothing to gate)."""
    if not bindings:
        return None
    per = sorted(
        canonical_hash({"role": b["role"], "source": b["source"], "ref": b["ref"],
                        "decision_id": b["decision_id"], "fact_id": b["fact_id"],
                        "authority": b["authority"], "state": b["_state"]})
        for b in bindings)
    return canonical_hash({"bindings": per})


def binding_exposure(bindings: list[dict]) -> list[dict]:
    """The ``/contract/draft``-facing projection of the confirmed bindings: role / column-ref / source /
    authority / warnings per binding (the internal state signature is dropped). Shows the human EXACTLY
    what they are confirming, alongside the overall ``binding_hash``."""
    return [{"role": b["role"], "ref": b["ref"], "source": b["source"],
             "authority": b["authority"], "warnings": b["warnings"]} for b in bindings]


def _cancel_undelivered_external_submissions(conn, prior_contract_id: str | None) -> None:
    """H2b — Delivery I seam (NO-OP stub). On a pointer advance the plan requires cancelling any
    UNDELIVERED external submissions for the now-superseded contract. Delivery I (external submissions)
    does not exist yet, so there is nothing to cancel and NO external-submission table to touch. The
    SUPERSEDED path already calls this so Delivery I wires it in one place. # Delivery I wires this."""
    return


def _confirm_snapshot_binding(conn, intent_id: str | None) -> tuple[str | None, str | None]:
    """MF-3 — the SERVER C0 metadata-snapshot lineage recorded on the considered set for this intent,
    read AT CONFIRM. Returns ``(snapshot_id, content_hash)`` to bind IMMUTABLY onto the write-once-in-
    practice contract row: ``contract_considered.snapshot_id`` is a MUTABLE upsert pointer (a later
    broaden repoints it S1->S2), so recording the value AT CONFIRM on the contract row is what makes
    "what catalog state was this contract authored against" reconstructable and un-repointable. Returns
    ``(None, None)`` when the intent has no considered-set row, or it recorded no snapshot (a pre-C0 /
    READ COMMITTED considered set) — additive, the columns stay NULL."""
    if intent_id is None:
        return None, None
    row = conn.execute(
        "SELECT snapshot_id, snapshot_content_hash FROM contract_considered WHERE intent_id = %s",
        (intent_id,)).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


@dataclass(frozen=True, slots=True)
class Contract:
    contract_id: str
    feature_id: str
    feature_name: str
    version: int



def confirm_contract(conn, draft: ContractDraft, *, actor, roles: Iterable[str] = (),
                     target_ref: str | None = None,
                     now: datetime | None = None, intent_id: str | None = None,
                     confirmed_binding_hash: str | None = None) -> Contract:
    """The human gate. RE-RUNS the deterministic MCV (B1) and refuses to govern an invalid draft, then
    registers a versioned governed contract + wires its derives-from into the feature layer. Re-confirming
    the same feature bumps the version. A non-empty definition is required (no empty-narrative contract).
    `roles` is the CONFIRMING actor's read-scope, threaded into the re-run so the cross-table
    join-authority disposition judges the confirmer's real authority — without it a sensitivity-tagged
    hop would read DENIED and over-reject a legitimately authorized feature."""
    # H2b STEP 1 — ADVISORY LOCK FIRST, before ANY feature lookup or validate_minimum. Serializes
    # concurrent confirms of the SAME feature identity so their pointer CAS (STEP 5) cannot interleave:
    # two first-confirms can't both register the feature (B4 proliferation), and a re-confirm can't lose
    # the pointer advance. pg_advisory_xact_lock binds to the CALLER's transaction (confirm never opens
    # its own) and releases on its COMMIT/ROLLBACK. Taken BEFORE the per-feature reads so the whole
    # read-decide-write of the pointer is serialized. (Ordered before the global validation-projection
    # checkpoint lock taken later in _seed_validation_lifecycle — a consistent lock order, no deadlock.)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (feature_contract_lock_key(draft.feature_name),))
    tref = target_ref if target_ref is not None else draft.target_ref   # M3: fall back to the draft's
    check = validate_minimum(conn, draft, target_ref=tref, now=now, roles=roles)
    if not check.ok:
        raise ContractValidationError(f"contract failed MCV, not governed: {check.reasons}")
    if not (draft.definition or "").strip():
        raise ContractValidationError("contract has an empty definition, not governed")
    pairs = draft.derives_pairs   # B3: resolved (catalog_source, object_ref) carried on the draft
    # B4: ONE feature per feature_name — re-confirm reuses + refreshes the feature (no proliferation),
    # so drift impact/freshness point at a single live feature, not N duplicates.
    prev = conn.execute("SELECT contract_id, feature_id, version FROM contract "
                        "WHERE feature_name = %s ORDER BY version DESC LIMIT 1",
                        (draft.feature_name,)).fetchone()
    if prev is not None:
        # MF-4: legacy latest contract by feature_name — the superseded FALLBACK for a pre-H2b feature
        # with no pointer. The compat feature/derives writes are DEFERRED to after the CAS (STEP 7).
        legacy_prior_contract_id, feature_id, version = prev[0], prev[1], prev[2] + 1
    else:
        legacy_prior_contract_id = None   # first version — nothing to retire
        feature_id = register_feature(conn, FeatureSpec(
            name=draft.feature_name, description=draft.definition, grain_table=draft.grain_table,
            aggregation=draft.aggregation, as_of_column=draft.as_of_column, derives_from=pairs,
            verification="DESIGN-CHECKED"))   # governed => EARNS DESIGN-CHECKED (default is UNVERIFIED)
        version = 1                            # the feature row must exist before the contract FK below
    # H2b STEP 2 — read the CURRENT pointer (the AUTHORITATIVE superseded target). Prefer the pointer's
    # prior contract; fall back to the legacy latest-by-feature_name contract when no pointer exists yet
    # (a feature governed before H2b). Read under the advisory lock, so a re-confirm sees the truly-latest
    # committed pointer and its CAS below is uncontended.
    pointer = conn.execute(
        "SELECT contract_id, pointer_version FROM feature_current_contract WHERE feature_id = %s",
        (feature_id,)).fetchone()
    prior_pointer_contract_id = pointer[0] if pointer is not None else None
    prior_pointer_version = pointer[1] if pointer is not None else None
    superseded_contract_id = (prior_pointer_contract_id if prior_pointer_contract_id is not None
                              else legacy_prior_contract_id)
    contract_id = mint_id("contract")
    # MF-3: bind THIS contract to the immutable metadata snapshot the considered set was authored against,
    # read AT CONFIRM from the server row. Persisted onto the never-repointed contract row so a later
    # broaden (which repoints the mutable contract_considered.snapshot_id) cannot change what catalog state
    # this governed contract was authored against. NULL on a pre-C0 / READ COMMITTED set (additive).
    metadata_snapshot_id, metadata_content_hash = _confirm_snapshot_binding(conn, intent_id)
    # H1b — FOLD the confirmed role-binding hash into the 1011 ``metadata_input_fingerprint`` (the
    # feature-contract metadata fingerprint now includes the confirmed role-binding hash; no migration —
    # reuses the existing column). ``metadata_content_hash`` (the MF-3 immutable snapshot binding) stays
    # PURE, so "what catalog state this contract was authored against" is unchanged; only the additive
    # fingerprint composes the two. NULL binding_hash (a direct/pre-H1b confirm) ⟹ the pre-H1b value
    # (the pure content_hash), byte-identical.
    input_fingerprint = (
        canonical_hash({"snapshot": metadata_content_hash, "binding_hash": confirmed_binding_hash})
        if confirmed_binding_hash is not None else metadata_content_hash)
    # H2b STEP 3 — insert the immutable contract version + the new 1011 columns. generation_source /
    # recipe_id / physical_plan_id / planner_declaration_id are NULL: the draft does not carry them yet.
    # # H1a/H3 will supply them (recipe / physical-plan / planner-declaration provenance).
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, definition, version, actor, "
        "join_path, intent_id, verification, validation_status, requirements, "
        "metadata_snapshot_id, metadata_content_hash, metadata_input_fingerprint, "
        "initial_validation_status, initial_verification) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)",
        (contract_id, feature_id, draft.feature_name, draft.definition, version, _actor_json(actor),
         json.dumps(list(draft.join_path)), intent_id,   # intent_id: audit link to the hypothesis (M5)
         "DESIGN-CHECKED",   # §14.5 stamp — gauntlet-passed; predictive value unverified (0968).
         #                     A SEPARATE (hyphenated, 0973-constrained) axis from validation_status.
         check.validation_status,   # RF-C1: the CONFIRM-TIME re-run's honest tri-state — NOT the
         #                            draft's carried value (an upgrade/downgrade since Gate #1 is
         #                            a real change and must be recorded, never silently kept stale)
         json.dumps(requirements_to_json(check.requirements)),
         metadata_snapshot_id, metadata_content_hash,   # MF-3: immutable contract -> snapshot binding
         input_fingerprint,          # 1011 metadata_input_fingerprint — the C0 snapshot input hash the
         #                             contract was authored against, FOLDED with the H1b binding_hash
         #                             (pure content_hash when no binding_hash; NULL on a pre-C0 confirm)
         check.validation_status,    # 1011 initial_validation_status — the at-confirm INITIAL axis, same
         #                             value the ASSESSED event stamps (SEPARATE from the mutable 1003 col)
         "DESIGN-CHECKED"))          # 1011 initial_verification — the at-confirm INITIAL verification
    # H2b STEP 4 — insert the immutable, write-once contract_input_column lineage: one role-labelled row
    # per reconciled input (derives + grain + as_of + governed join). This + the pointer (STEP 5) are the
    # AUTHORITATIVE write; feature/feature_derives_from (STEP 7) are the current-pointer compat projection.
    _insert_contract_input_columns(conn, contract_id, draft)
    # H2c STEP 4b — write the write-once contract_metadata_dependency reverse-dep rows (one per
    # check-clearing / input-binding catalog item), same transaction. These back BOTH the eager
    # invalidate_contracts_for fan-out AND the read-time second fail-closed gate.
    _insert_contract_metadata_dependencies(conn, contract_id, draft)
    # H2b STEP 5 — CAS the feature_current_contract pointer to this new version (the AUTHORITATIVE write).
    if prior_pointer_version is None:
        conn.execute(
            "INSERT INTO feature_current_contract (feature_id, contract_id, pointer_version, set_at) "
            "VALUES (%s, %s, 1, now())", (feature_id, contract_id))
    else:
        swapped = conn.execute(
            "UPDATE feature_current_contract SET contract_id = %s, pointer_version = %s, set_at = now() "
            "WHERE feature_id = %s AND pointer_version = %s",
            (contract_id, prior_pointer_version + 1, feature_id, prior_pointer_version)).rowcount
        if swapped != 1:   # someone advanced the pointer concurrently — fail closed (never lost-update)
            raise ContractPointerConflict(
                f"feature_current_contract CAS lost for feature {feature_id} at pointer_version "
                f"{prior_pointer_version}; a concurrent confirm advanced it")
        # H2b STEP 8 — on a pointer ADVANCE, cancel undelivered external submissions for the superseded
        # contract (Delivery I seam — no-op today; the SUPERSEDED path already calls it).
        _cancel_undelivered_external_submissions(conn, superseded_contract_id)
    # H2b STEP 7 — COMPAT PROJECTION. feature/feature_derives_from now reflect the CURRENT pointer (they
    # are the current-pointer compatibility projection, NOT historical truth — the pointer + input rows
    # above are). Sequenced AFTER the CAS. A first-confirm already wrote them via register_feature (the
    # feature row must exist before the contract FK); a re-confirm refreshes them here to mirror the
    # now-current version — same values as before, just explicitly downstream of the pointer.
    if prev is not None:
        conn.execute(
            "UPDATE feature SET description = %s, grain_table = %s, aggregation = %s, "
            "as_of_column = %s, verification = %s WHERE feature_id = %s",   # refresh the stamp too
            (draft.definition, draft.grain_table, draft.aggregation, draft.as_of_column,
             "DESIGN-CHECKED", feature_id))
        conn.execute("DELETE FROM feature_derives_from WHERE feature_id = %s", (feature_id,))
        for catalog_source, object_ref in pairs:
            conn.execute("INSERT INTO feature_derives_from (feature_id, catalog_source, object_ref) "
                         "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (feature_id, catalog_source, object_ref))
    # H2b STEP 6 / Delivery C4-T3: ADDITIVELY seed the event-sourced validation lifecycle from the SAME
    # confirm-time MCV re-run. The 1003 columns above stay the INITIAL stamp (unchanged); this emits the
    # ASSESSED event + persists the immutable requirement rows + projects the current-state row — all on
    # THIS transaction, so the lifecycle seed is atomic with confirm. superseded_contract_id is now the
    # POINTER's prior contract (STEP 2), so the existing SUPERSEDED emit demotes the RIGHT prior version.
    _seed_validation_lifecycle(conn, contract_id, check, pairs, metadata_content_hash,
                               superseded_contract_id=superseded_contract_id)
    return Contract(contract_id, feature_id, draft.feature_name, version)


def _seed_validation_lifecycle(conn, contract_id, check, pairs, snapshot_content_hash,
                               *, superseded_contract_id=None) -> None:
    """C4-T3: from the confirm-time ``MinimumCheck``, persist the immutable requirement rows, emit the
    ASSESSED event, and fold it into ``feature_contract_validation_state`` — all on ``conn`` (atomic
    with the contract insert). Idempotent: requirement rows use ``ON CONFLICT DO NOTHING`` on the 1009
    identity key, and the projection's sequence guard makes the fold a replay-safe no-op.

    The requirement fingerprint is the IMMUTABLE metadata-snapshot content_hash (MF-3 binding — what
    catalog state the contract was authored against) when present, else a canonical hash of the draft's
    resolved (catalog, ref) pairs + the confirm-time requirements (a pre-C0 / snapshot-less confirm).

    MF-1: takes the projection checkpoint row FOR UPDATE (``lock_checkpoint``) BEFORE inserting any
    event, so concurrent confirms serialize their seq-assignment WITH the fold (no skip/regress).
    MF-4: when ``superseded_contract_id`` is given (a re-confirm), emits a SUPERSEDED event for that
    retired version BEFORE this version's ASSESSED — folded in the same transaction, under the same
    lock, so the retired version's live stamp is demoted terminally.
    """
    # MF-1: serialize emit+fold across concurrent confirms — lock BEFORE any event is inserted.
    feature_validation_projection.lock_checkpoint(conn)
    fingerprint = snapshot_content_hash or canonical_hash(
        {"derives_pairs": [[cs, ref] for cs, ref in pairs],
         "requirements": requirements_to_json(check.requirements)})
    for req in check.requirements:
        operand = [req.operand[0], req.operand[1]]
        content_hash = canonical_hash({"code": req.code, "operand": operand, "detail": req.detail})
        # C2-C3 review (I-1): persist the requirement's REGISTRY-typed shape, not a lossy stand-in. The
        # requirements come from the confirm-time MCV re-mint (build_requirement), so they are always
        # registry-valid; schema_for resolves.
        #   (a) requirement_schema_version = the requirement's OWN schema_version (the registry "v1"),
        #       so a downstream schema_for(code, version) RESOLVES (the old "req-schema-v1" could not);
        #   (b) params_json carries the TYPED params (e.g. ADDITIVITY's {"operation": ...}) the external
        #       check (Delivery I) reads — kept alongside `detail`, which used to be all this column held;
        #   (c) blocking is the REGISTRY schema's blocking flag, not a hardcoded True.
        # Write-once + identity-keyed (1009).
        schema = schema_for(req.code, req.schema_version)
        conn.execute(
            "INSERT INTO feature_validation_requirement (requirement_id, contract_id, "
            "requirement_schema_version, metadata_input_fingerprint, code, subject_json, "
            "params_json, blocking, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (contract_id, requirement_schema_version, metadata_input_fingerprint, "
            "content_hash) DO NOTHING",
            (mint_id("req"), contract_id, req.schema_version, fingerprint, req.code,
             Jsonb({"operand": operand}),
             Jsonb({"params": dict(req.params), "detail": req.detail}),
             schema.blocking, content_hash))
    # MF-4: a re-confirm mints a NEW contract_id for this feature; the PRIOR latest version is now
    # dead. Emit SUPERSEDED for it in THIS transaction, BEFORE the new version's ASSESSED, so the
    # fold demotes the retired version's live stamp AND marks it terminally superseded — a late
    # EXTERNAL_PASSED can never resurrect it, and a consumer filtering on effective_verification
    # never picks the dead version. Same lock + seq space -> folded in order by catch_up below.
    if superseded_contract_id is not None:
        conn.execute(
            "INSERT INTO feature_contract_validation_event "
            "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'SUPERSEDED', %s)",
            (mint_id("fcve"), superseded_contract_id, Jsonb({"superseded_by": contract_id})))
    # The ASSESSED payload is MINIMAL + honest: the C4 lowercase status vocabulary (mirrors the 1009
    # CHECK — a DISTINCT axis from the 1003 UPPERCASE column), plus counts. The fold reads the
    # requirement rows above for the authoritative blocking detail, so the requirement rows MUST be
    # persisted before this event is folded.
    conn.execute(
        "INSERT INTO feature_contract_validation_event "
        "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'ASSESSED', %s)",
        (mint_id("fcve"), contract_id, Jsonb({
            "validation_status": check.validation_status.lower(),
            "requirement_count": len(check.requirements),
            "has_blocking": bool(check.requirements)})))
    feature_validation_projection.catch_up(conn)   # fold SUPERSEDED (if any) + ASSESSED in seq order


def contract_freshness(conn, contract_id: str, *, now: datetime) -> FeatureFreshness:
    """A contract is only as fresh as its feature's stalest source — catalog drift stales the contract."""
    row = conn.execute("SELECT feature_id FROM contract WHERE contract_id = %s",
                       (contract_id,)).fetchone()
    if row is None:
        raise KeyError(contract_id)
    return feature_freshness(conn, row[0], now=now)


def contracts_affected_by(conn, catalog_source: str, object_ref: str) -> list[str]:
    """Drift impact: the CURRENT contract (max version) per feature that derives from a drifted column
    — not every historical version (B4)."""
    feature_ids = features_affected_by(conn, catalog_source, object_ref)
    if not feature_ids:
        return []
    rows = conn.execute(
        "SELECT DISTINCT ON (feature_name) contract_id FROM contract "
        "WHERE feature_id = ANY(%s) ORDER BY feature_name, version DESC",
        (feature_ids,)).fetchall()
    return sorted(r[0] for r in rows)


# Delivery C4-T4: the effective-fields sentinels the fail-closed read returns instead of the
# projection's real stamp. UNVERIFIED is always the safe effective verification when we cannot trust
# the read model — NEVER the legacy 1003 stamp.
_EFFECTIVE_UNVERIFIED = "UNVERIFIED"
_EFFECTIVE_UNAVAILABLE = "unavailable"        # projection DEGRADED/LAGGED — fail closed
_EFFECTIVE_LEGACY_UNASSESSED = "legacy_unassessed"  # a pre-C4 contract with no projected state row


def _effective_validation(ready: bool, state_status, state_verification) -> tuple[str, str]:
    """Map the projection health + a contract's ``feature_contract_validation_state`` row (its
    ``validation_status``/``effective_verification``, or ``None`` when there is no state row) to the
    authoritative ``(effective_validation_status, effective_verification)`` the read APIs expose.

    FAIL CLOSED (C4-T4): the effective stamp is PROJECTION-sourced only. A DEGRADED/LAGGED projection
    (``ready is False``) → ``('unavailable', 'UNVERIFIED')`` — never the legacy 1003 column. A
    contract with NO state row (historical / pre-C4) → ``('legacy_unassessed', 'UNVERIFIED')``, not
    fabricated as design_checked. Otherwise the real projected effective stamp is returned verbatim.
    """
    if not ready:
        return _EFFECTIVE_UNAVAILABLE, _EFFECTIVE_UNVERIFIED
    if state_status is None:
        return _EFFECTIVE_LEGACY_UNASSESSED, _EFFECTIVE_UNVERIFIED
    return state_status, state_verification


# H2c: the downgrade a drifted dependency forces — needs (re)validation, stamp UNVERIFIED. NEVER the
# promoted DATA-CHECKED/design_checked once any input drifted (fail closed, not latest-wins).
_EFFECTIVE_NEEDS_REVALIDATION = "needs_external_validation"


def _apply_dependency_read_gate(conn, contract_id: str, eff_status: str,
                                eff_verif: str) -> tuple[str, str]:
    """H2c READ-TIME SECOND fail-closed gate. A PROMOTED stamp (``design_checked`` — the status that
    carries DESIGN-CHECKED or DATA-CHECKED) is HARD-downgraded to needs_external_validation/UNVERIFIED
    when ANY of the contract's ``contract_metadata_dependency`` items has drifted since confirm
    (recomputed current hash != stored hash: item missing / retyped / cleared / retired). This runs on
    top of the projection's already-fail-closed effective stamp, so a stale DATA-CHECKED can NEVER be
    served even if NO ``INVALIDATED`` was folded yet (projection lag, a missed eager wire, a seamed
    drift source). Non-promoted stamps (needs_external_validation/rejected/unavailable/legacy) pass
    through untouched — the gate only ever DOWNGRADES."""
    if eff_status != "design_checked":
        return eff_status, eff_verif
    # I-1fc fail-closed: a PROMOTED (design_checked) contract with ZERO dependency rows is gate-blind
    # (dependencies_drifted returns False on no rows) — the pre-H2c cohort (promoted C4 state, no dep
    # rows). A promoted stamp with NO recorded lineage cannot be trusted drift-free, so downgrade.
    # legacy_unassessed is non-promoted (eff_status != design_checked) and never reaches here.
    if not has_dependency_rows(conn, contract_id):
        return _EFFECTIVE_NEEDS_REVALIDATION, _EFFECTIVE_UNVERIFIED
    if dependencies_drifted(conn, contract_id):
        return _EFFECTIVE_NEEDS_REVALIDATION, _EFFECTIVE_UNVERIFIED
    return eff_status, eff_verif


def contract_read_status(conn, contract_id: str) -> tuple[str, str]:
    """The authoritative, drift-aware effective stamp for ONE contract — what any read surface MUST
    serve. Computes the projection's effective stamp (fail-closed on a degraded/lagged projection),
    THEN applies the H2c dependency read gate on top. Returns ``(effective_validation_status,
    effective_verification)``; a drifted dependency yields ``('needs_external_validation',
    'UNVERIFIED')`` even while the projection state row still reads DATA-CHECKED."""
    ready = feature_validation_projection.is_read_ready(conn)
    state = feature_validation_projection.read_state(conn, contract_id)
    eff_status, eff_verif = _effective_validation(
        ready, None if state is None else state["validation_status"],
        None if state is None else state["effective_verification"])
    return _apply_dependency_read_gate(conn, contract_id, eff_status, eff_verif)


def _contract_requirements(conn, contract_id: str) -> list[dict]:
    """H2d — the IMMUTABLE requirement rows (1009) for a contract version: requirement_id / code /
    params / blocking. Ordered deterministically (created_at, id). Empty for a legacy / pre-C4
    contract (which never fabricated requirement rows)."""
    rows = conn.execute(
        "SELECT requirement_id, code, params_json, blocking FROM feature_validation_requirement "
        "WHERE contract_id = %s ORDER BY created_at, requirement_id", (contract_id,)).fetchall()
    return [{"requirement_id": r[0], "code": r[1], "params": r[2], "blocking": r[3]} for r in rows]


def _invalidation_reasons(conn, contract_id: str) -> list[dict]:
    """H2d — the drift-invalidation reasons for a contract version, read from the APPEND-ONLY INVALIDATED
    validation-event payloads (H2c). Each entry is the event payload (``reason`` + the catalog identity
    that drifted). Ordered by event ``seq``. Empty when the contract was never invalidated."""
    rows = conn.execute(
        "SELECT payload FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'INVALIDATED' ORDER BY seq", (contract_id,)).fetchall()
    return [r[0] for r in rows]


def _contract_history(conn, feature_id: str, contract_id: str) -> dict:
    """H2d — history read STRICTLY from the immutable sources: the ``contract`` VERSIONS for this feature
    (the append-only version log, NEVER the mutable ``feature`` row) + this contract version's
    APPEND-ONLY validation-event stream (1009). Reconstructing history from the compat ``feature`` row
    would be a lie — that row only mirrors the CURRENT pointer."""
    versions = conn.execute(
        "SELECT contract_id, version, verification, initial_validation_status, created_at "
        "FROM contract WHERE feature_id = %s ORDER BY version", (feature_id,)).fetchall()
    events = conn.execute(
        "SELECT event_type, payload, created_at FROM feature_contract_validation_event "
        "WHERE contract_id = %s ORDER BY seq", (contract_id,)).fetchall()
    return {
        "versions": [{"contract_id": v[0], "version": v[1], "verification": v[2],
                      "initial_validation_status": v[3], "created_at": v[4].isoformat()}
                     for v in versions],
        "events": [{"event_type": e[0], "payload": e[1], "created_at": e[2].isoformat()}
                   for e in events],
    }


def list_contracts(conn, *, limit: int = 50) -> list[dict]:
    """The governed-contract inventory (registry READ surface).

    C4-T4: each row carries the EFFECTIVE validation stamp from the ``feature_contract_validation``
    PROJECTION (``effective_validation_status``/``effective_verification`` — the authoritative fields
    for consumers), LEFT JOINed on ``contract_id``, gated FAIL-CLOSED by the projection's read
    readiness. ``verification`` remains the 1003 INITIAL confirm-time stamp (kept, additive) — the
    effective fields, not it, are authoritative, and a lagged/degraded projection serves
    'unavailable'/UNVERIFIED here, never that legacy column.

    H2d (ADDITIVE): each row also exposes, sourced from the pointer + immutable versions/events —
    the feature's CURRENT contract (``current_contract_id``/``pointer_version`` from
    ``feature_current_contract``, + ``is_current``), the at-confirm INITIAL stamp columns
    (``initial_validation_status``/``initial_verification``, 1011), the immutable snapshot
    ``metadata_input_fingerprint`` (1008/MF-3), the planner ids (``physical_plan_id``/
    ``planner_declaration_id`` — NULL until H1a/H3), the contract's ``requirements`` (1009 rows), and
    its ``invalidation_reasons`` (INVALIDATED event payloads, H2c)."""
    ready = feature_validation_projection.is_read_ready(conn)   # ONE fail-closed gate for the read
    rows = conn.execute(
        "SELECT c.contract_id, c.feature_id, c.feature_name, c.version, c.verification, "
        "c.created_at, s.validation_status, s.effective_verification, "
        "c.initial_validation_status, c.initial_verification, c.metadata_input_fingerprint, "
        "c.physical_plan_id, c.planner_declaration_id, p.contract_id, p.pointer_version "
        "FROM contract c "
        "LEFT JOIN feature_contract_validation_state s ON s.contract_id = c.contract_id "
        "LEFT JOIN feature_current_contract p ON p.feature_id = c.feature_id "
        "ORDER BY c.created_at DESC LIMIT %s", (limit,)).fetchall()
    out = []
    for r in rows:
        eff_status, eff_verif = _effective_validation(ready, r[6], r[7])
        # H2c: second fail-closed gate — a stale DATA-CHECKED/design_checked never survives a drifted
        # dependency, even if the projection has not folded an INVALIDATED yet.
        eff_status, eff_verif = _apply_dependency_read_gate(conn, r[0], eff_status, eff_verif)
        out.append({"contract_id": r[0], "feature_id": r[1], "feature_name": r[2], "version": r[3],
                    "verification": r[4], "created_at": r[5].isoformat(),
                    "effective_validation_status": eff_status,
                    "effective_verification": eff_verif,
                    "current_contract_id": r[13], "pointer_version": r[14],
                    "is_current": r[13] is not None and r[13] == r[0],
                    "initial_validation_status": r[8], "initial_verification": r[9],
                    "metadata_input_fingerprint": r[10],
                    "physical_plan_id": r[11], "planner_declaration_id": r[12],
                    "requirements": _contract_requirements(conn, r[0]),
                    "invalidation_reasons": _invalidation_reasons(conn, r[0])})
    return out


def get_contract_detail(conn, contract_id: str) -> dict | None:
    """C4-T4: the contract detail carries the EFFECTIVE validation stamp read from the
    ``feature_contract_validation`` PROJECTION (``effective_validation_status``/
    ``effective_verification``), gated FAIL-CLOSED. ``verification`` stays the 1003 INITIAL stamp
    (kept, additive); the effective fields are the authoritative ones and never fall back to that
    legacy column when the projection is degraded/lagged.

    H2d (ADDITIVE): also exposes the feature's CURRENT pointer (``current_contract_id``/
    ``pointer_version`` + ``is_current``), the at-confirm INITIAL stamp columns
    (``initial_validation_status``/``initial_verification``), the immutable snapshot
    ``metadata_input_fingerprint``, the planner ids (NULL until H1a/H3), the contract's
    ``requirements`` (1009), its ``invalidation_reasons`` (H2c INVALIDATED payloads), and a ``history``
    section read STRICTLY from the immutable ``contract`` versions + validation-event stream."""
    row = conn.execute(
        "SELECT contract_id, feature_id, feature_name, definition, version, verification, intent_id, "
        "created_at, initial_validation_status, initial_verification, metadata_input_fingerprint, "
        "physical_plan_id, planner_declaration_id FROM contract WHERE contract_id = %s",
        (contract_id,)).fetchone()
    if row is None:
        return None
    ready = feature_validation_projection.is_read_ready(conn)
    state = feature_validation_projection.read_state(conn, contract_id)
    eff_status, eff_verif = _effective_validation(
        ready, None if state is None else state["validation_status"],
        None if state is None else state["effective_verification"])
    # H2c: second fail-closed gate on the single-contract detail read too.
    eff_status, eff_verif = _apply_dependency_read_gate(conn, contract_id, eff_status, eff_verif)
    pointer = conn.execute(
        "SELECT contract_id, pointer_version FROM feature_current_contract WHERE feature_id = %s",
        (row[1],)).fetchone()
    current_contract_id = pointer[0] if pointer is not None else None
    return {"contract_id": row[0], "feature_id": row[1], "feature_name": row[2], "definition": row[3],
            "version": row[4], "verification": row[5], "intent_id": row[6],
            "created_at": row[7].isoformat(),
            "effective_validation_status": eff_status,
            "effective_verification": eff_verif,
            "current_contract_id": current_contract_id,
            "pointer_version": pointer[1] if pointer is not None else None,
            "is_current": current_contract_id is not None and current_contract_id == row[0],
            "initial_validation_status": row[8], "initial_verification": row[9],
            "metadata_input_fingerprint": row[10],
            "physical_plan_id": row[11], "planner_declaration_id": row[12],
            "requirements": _contract_requirements(conn, contract_id),
            "invalidation_reasons": _invalidation_reasons(conn, contract_id),
            "history": _contract_history(conn, row[1], contract_id)}


def feature_detail(conn, feature_id: str, *, roles=()) -> dict | None:
    """Feature 360: everything about one feature in a single view — its definition + verification stamp
    + lineage (from get_feature, READ-SCOPED by roles), the governed contract's narrative + join path,
    the HYPOTHESIS it was born from (feature -> current contract -> intent), and its consumers (which
    models use it). The hypothesis is present only for features born through the hypothesis-driven flow.

    I-2fc (fail-closed / double-authority): the feature-level EFFECTIVE verification is routed through
    the ``feature_current_contract`` POINTER + ``contract_read_status`` (the gated truth) — NEVER the
    mutable ``feature.verification`` stamp (never demoted by drift) nor a latest-BY-VERSION contract
    that isn't the pointer. A drifted current contract therefore downgrades the 360 verification instead
    of showing a stale promoted stamp. A directly-registered feature with no governing contract keeps
    its honest ``feature`` stamp (UNVERIFIED). Other displayed fields are preserved (additive)."""
    feat = get_feature(conn, feature_id, roles=roles)
    if feat is None:
        return None
    # Resolve the CURRENT contract via the pointer (authoritative); fall back to latest-by-version
    # (deterministic tie-break) only for a pre-H2b legacy feature with no pointer.
    pointer = conn.execute(
        "SELECT contract_id FROM feature_current_contract WHERE feature_id = %s",
        (feature_id,)).fetchone()
    if pointer is not None:
        row = conn.execute(
            "SELECT contract_id, definition, version, verification, intent_id, join_path FROM contract "
            "WHERE contract_id = %s", (pointer[0],)).fetchone()
    else:
        row = conn.execute(
            "SELECT contract_id, definition, version, verification, intent_id, join_path FROM contract "
            "WHERE feature_id = %s ORDER BY version DESC, contract_id DESC LIMIT 1",
            (feature_id,)).fetchone()
    contract = None
    hypothesis = None
    eff_status = eff_verif = None
    if row is not None:
        eff_status, eff_verif = contract_read_status(conn, row[0])
        contract = {"contract_id": row[0], "definition": row[1], "version": row[2],
                    "verification": row[3],   # 1003 INITIAL stamp (additive)
                    "effective_validation_status": eff_status,
                    "effective_verification": eff_verif, "join_path": row[5]}
        if row[4]:   # intent_id -> the hypothesis behind the feature
            i = conn.execute(
                "SELECT hypothesis, definition, intake_mode, target_ref FROM contract_intent "
                "WHERE intent_id = %s", (row[4],)).fetchone()
            if i is not None:
                hypothesis = {"hypothesis": i[0], "definition": i[1], "intake_mode": i[2],
                              "target_ref": i[3]}
    out = {**feat, "contract": contract, "hypothesis": hypothesis,
           "consumers": consumers_of_feature(conn, feature_id)}
    # Override the feature-level verification with the GATED effective stamp when the feature is
    # governed; a feature with no contract keeps its honest (non-promoted) `feature` stamp.
    if eff_verif is not None:
        out["verification"] = eff_verif
        out["effective_validation_status"] = eff_status
        out["effective_verification"] = eff_verif
    return out
