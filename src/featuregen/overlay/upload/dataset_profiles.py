"""The effective dataset semantic profile (profile plan §6.3, interface doc D1/D2/D5/D11/D12.10).

NO new truth store: a dataset profile is a typed READ MODEL assembled from what already exists —
the per-field ``field_evidence`` resolution at the TABLE logical ref, the specialized governed
grain/availability fact stamps, and the current catalog narrative. One repeatable-read transaction,
one explainable object, one canonical hash.

**Fact-ref identity (one, documented):** the grain/availability heads key on
``graph_node.grain_fact_event_id`` / ``graph_node.availability_fact_event_id`` on the TABLE node —
the projection the resolver already maintains (``table_fact_projection.py``; the same location the
stamp audit reads). ``overlay_fact_state`` is consulted only to ENRICH the head with its
``fact_key``/folded status; the stamp is the identity that enters ``dataset_profile_hash``. A stamp
whose state row cannot be found is store drift (owned by ``scripts/profile_reconcile.py``) and is
carried honestly with ``folded_status="unknown"`` rather than silently dropped from the hash.

**Isolation:** callers run the builder inside ONE ``REPEATABLE READ`` transaction so every field,
fact head and narrative pointer describe a single torn-free snapshot — the API reads use the
existing ``get_feature_gen_conn`` dependency (the asset-detail precedent); write surfaces assemble
under their own serialized transaction (source + table advisory locks).

**Hash (D1):** ``dataset_profile_hash`` = ``materialize_hash`` (RFC 8785 JCS) over both
resolutions, the D2 authority triples, states/unresolved reasons, evidence ids, the two fact heads
and the CURRENT catalog narrative revision id (D12.10 — narrative edits re-key dataset profiles;
byte-identical re-authoring resolves to the same deterministic revision id and does NOT). It
excludes physical bindings, environment, wall-clock, job state and projection timestamps.

TODO(semantic-merge): ``SemanticValueV1`` is owned by the parallel semantic stream's
``overlay/upload/semantic_context.py`` (D5). The module-local dataclass below is shaped to the D2
contract (value + producer/strength/lifecycle triple + evidence ids); when that module lands,
replace this definition with the import — this is the single adapter point.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from featuregen.contracts import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.evidence import AssertionStrength
from featuregen.overlay.field_authority import (
    FieldPolicy,
    FieldResolution,
    InfluenceTier,
    resolve_field_authority,
)
from featuregen.overlay.field_evidence import (
    FieldEvidence,
    read_active_field_evidence,
    to_view,
)
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.field_revalidation import active_disqualifiers_for
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.profile_vocab import (
    UnresolvedReason,
    data_role_from_table_role,
    unresolved_family,
)

#: The contract token inside every ``dataset_profile_hash`` payload — bump ONLY on a meaning-bearing
#: contract change (a new hashed field, a state-vocabulary change), never for provenance.
_PROFILE_CONTRACT = "dataset_semantic_profile_v1"

#: profile field name -> the ``field_evidence`` field_name it reads (all at the TABLE logical ref).
#: ``data_role`` is DERIVED from ``table_role`` (correction 4) and is handled specially.
_PROFILE_FIELD_SOURCES: dict[str, str] = {
    "description": "definition",
    "business_context": "business_context",
    "domains": "domain",
    "primary_entity": "primary_entity",
    "authority_role": "authority_role",
    "temporal_storage_model": "temporal_storage_model",
    "event_or_snapshot": "event_or_snapshot",
}

# EffectiveProfileFieldV1.state vocabulary (§6.3, closed).
STATE_DISPLAY_ONLY = "display_only"
STATE_LOAD_BEARING = "load_bearing"
STATE_NO_EVIDENCE = "no_evidence"
STATE_NEEDS_DATA_OBSERVATION = "needs_data_observation"
STATE_STRUCTURALLY_UNSUITABLE = "structurally_unsuitable"
STATE_CONFLICT = "conflict"
STATE_PROJECTION_UNAVAILABLE = "projection_unavailable"


@dataclass(frozen=True, slots=True)
class SemanticValueV1:
    """One resolved value + the D2 authority triple VERBATIM (producer × strength × lifecycle —
    never the flat 5-label projection) + the evidence ids backing exactly this value.

    Module-local stand-in shaped to the D2 contract; see the module TODO(semantic-merge)."""

    value: str
    producer: str
    strength: str
    lifecycle: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EffectiveProfileFieldV1:
    """One profile field's effective view — an explicit typed RE-WRAPPER of the authority engine's
    :class:`overlay.field_authority.FieldResolution` (there is NO second resolver: the same pure
    ``resolve_field_authority`` under the same registered policy produced ``display``/
    ``load_bearing``; this class only types the values with their D2 triples and re-expresses the
    resolver's reason in the closed §6.3 state + D5 three-family vocabulary).

    CRITICAL display semantics (§6.3 as amended): for a RECOMMENDATION-ceiling field,
    ``state=display_only`` is the NORMAL state — ``unresolved_reason`` is ``None`` there, and is
    populated ONLY from the three-family :class:`profile_vocab.UnresolvedReason` vocabulary.
    "No evidence at all" is ``no_evidence`` + ``undecided:no_evidence`` — honestly undecided,
    never failure-styled."""

    display: SemanticValueV1 | None
    load_bearing: SemanticValueV1 | None
    state: str
    unresolved_reason: str | None      # an UnresolvedReason value, or None
    unresolved_family: str | None      # its family (undecided | needs_data_check | structurally_unsuitable)
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GovernedFactHeadV1:
    """The head of one specialized governed table fact, keyed by the graph stamp (module note)."""

    fact_key: str
    folded_status: str
    confirmed_event_id: str | None


@dataclass(frozen=True, slots=True)
class DatasetSemanticProfileV1:
    """The assembled effective semantic profile for ONE table (§6.3, D5 naming)."""

    dataset_logical_ref: str
    catalog_profile_revision_id: str | None
    description: EffectiveProfileFieldV1
    business_context: EffectiveProfileFieldV1
    domains: EffectiveProfileFieldV1
    data_role: EffectiveProfileFieldV1
    primary_entity: EffectiveProfileFieldV1
    authority_role: EffectiveProfileFieldV1
    temporal_storage_model: EffectiveProfileFieldV1
    event_or_snapshot: EffectiveProfileFieldV1
    grain_fact: GovernedFactHeadV1 | None
    availability_fact: GovernedFactHeadV1 | None
    missing_context: tuple[str, ...]
    dataset_profile_hash: str


_STRENGTH_RANK: dict[str, int] = {
    AssertionStrength.PROPOSED.value: 0,
    AssertionStrength.SUPPORTED.value: 1,
    AssertionStrength.ATTESTED.value: 2,
    AssertionStrength.CONFIRMED.value: 3,
}


def _semantic_value(
    evidence: list[FieldEvidence], value: str | None, *, mapped: str | None = None
) -> SemanticValueV1 | None:
    """Type ``value`` with the D2 triple of its strongest backing evidence + ALL backing evidence
    ids (sorted). ``mapped`` substitutes a display-mapped value (data_role) while the backing is
    still matched on the resolver's raw ``value``. ``None`` value -> ``None``."""
    if value is None:
        return None
    backing = [e for e in evidence if to_view(e).value == value]
    if not backing:   # defensive: a resolver value always has backing among its inputs
        return None
    best = max(backing, key=lambda e: _STRENGTH_RANK.get(e.strength, -1))
    return SemanticValueV1(
        value=mapped if mapped is not None else value,
        producer=best.producer,
        strength=best.strength,
        lifecycle=best.lifecycle,
        evidence_ids=tuple(sorted(e.evidence_id for e in backing)),
    )


def _wrap_resolution(
    resolution: FieldResolution,
    evidence: list[FieldEvidence],
    policy: FieldPolicy,
    *,
    map_value=None,
) -> EffectiveProfileFieldV1:
    """Re-wrap one :class:`FieldResolution` into the §6.3 shape (see the class docstring).

    ``map_value`` (data_role) maps a resolver value into its DISPLAY vocabulary member without
    touching the underlying evidence."""
    mapper = map_value if map_value is not None else (lambda v: v)
    display = _semantic_value(evidence, resolution.display_value,
                              mapped=mapper(resolution.display_value)
                              if resolution.display_value is not None else None)
    load_bearing = _semantic_value(evidence, resolution.load_bearing_value,
                                   mapped=mapper(resolution.load_bearing_value)
                                   if resolution.load_bearing_value is not None else None)
    reason_codes = (resolution.unresolved_reason,) if resolution.unresolved_reason else ()

    if not evidence:
        return EffectiveProfileFieldV1(
            display=None, load_bearing=None, state=STATE_NO_EVIDENCE,
            unresolved_reason=UnresolvedReason.NO_EVIDENCE.value,
            unresolved_family=unresolved_family(UnresolvedReason.NO_EVIDENCE).value,
            reason_codes=reason_codes)
    if load_bearing is not None:
        return EffectiveProfileFieldV1(
            display=display, load_bearing=load_bearing, state=STATE_LOAD_BEARING,
            unresolved_reason=None, unresolved_family=None, reason_codes=())

    # A RECOMMENDATION field short-circuits at the influence ceiling BEFORE the resolver's own
    # conflict detection (resolution order step 4 < step 7), so a display-tie surfaces here only
    # as display=None. Detect it the way `_select`'s PREFER_CONFIRMED does — distinct values tied
    # at the top strength — so disagreeing evidence reports the honest CONFLICT/needs_data_check,
    # not a shapeless "undecided".
    if display is None:
        top = max(_STRENGTH_RANK.get(e.strength, -1) for e in evidence)
        top_values = {to_view(e).value for e in evidence
                      if _STRENGTH_RANK.get(e.strength, -1) == top}
        if len(top_values) > 1:
            return EffectiveProfileFieldV1(
                display=None, load_bearing=None, state=STATE_CONFLICT,
                unresolved_reason=UnresolvedReason.CONFLICT.value,
                unresolved_family=unresolved_family(UnresolvedReason.CONFLICT).value,
                reason_codes=reason_codes)

    raw = resolution.unresolved_reason or ""
    if raw == "influence_not_operational":
        # The NORMAL state of a RECOMMENDATION-ceiling field: influence, not operation — NOT
        # unresolved and NOT a failure (§6.3 amended). Display may still be None while the only
        # evidence is a pending four-eyes proposal below the display bar; that pendingness is
        # honestly "undecided", not an error.
        if policy.influence_max is InfluenceTier.RECOMMENDATION and display is not None:
            return EffectiveProfileFieldV1(
                display=display, load_bearing=None, state=STATE_DISPLAY_ONLY,
                unresolved_reason=None, unresolved_family=None, reason_codes=reason_codes)
        reason = UnresolvedReason.AUTHORITY_INSUFFICIENT
        state = STATE_DISPLAY_ONLY
    elif raw == "authority_insufficient":
        reason = UnresolvedReason.AUTHORITY_INSUFFICIENT
        state = STATE_DISPLAY_ONLY
    elif raw == "conflict":
        reason = UnresolvedReason.CONFLICT
        state = STATE_CONFLICT
    elif raw.startswith("disqualified:"):
        # The only honoured disqualifier today is CONFIRMATION_PENDING_REVALIDATION — a material
        # change awaiting a human re-check: squarely the "needs a data check" family.
        reason = UnresolvedReason.PENDING_REVALIDATION
        state = STATE_NEEDS_DATA_OBSERVATION
    else:
        # "specialized_fact" and any future resolver reason: the generic projection cannot serve
        # this field — visible as projection_unavailable, family undecided (nobody decided HERE).
        reason = UnresolvedReason.AUTHORITY_INSUFFICIENT
        state = STATE_PROJECTION_UNAVAILABLE
    return EffectiveProfileFieldV1(
        display=display, load_bearing=None, state=state,
        unresolved_reason=reason.value, unresolved_family=unresolved_family(reason).value,
        reason_codes=reason_codes)


def _resolve_field(conn: DbConn, logical_ref: str, field_name: str):
    """(resolution, active evidence, policy) for one field — the SAME pure resolver + registered
    policy + disqualifier seam the decision path uses (no second resolver)."""
    policy = policy_for(field_name)
    if policy is None:   # a profile field always has a registered policy; fail loudly if not
        raise KeyError(f"no registered policy for profile field {field_name!r}")
    evidence = read_active_field_evidence(conn, logical_ref, field_name)
    resolution = resolve_field_authority(
        [to_view(e) for e in evidence], policy,
        active_disqualifiers=active_disqualifiers_for(conn, logical_ref, field_name))
    return resolution, evidence, policy


def _fact_head(conn: DbConn, stamp_event_id: str | None) -> GovernedFactHeadV1 | None:
    """The governed fact head for one TABLE-node stamp (the module's ONE fact-ref identity).
    ``None`` stamp -> no head. A stamp whose ``overlay_fact_state`` row is missing is drift —
    carried with ``folded_status="unknown"`` so the stamp still keys the hash (never silently
    dropped); ``profile_reconcile`` owns surfacing it."""
    if stamp_event_id is None:
        return None
    row = conn.execute(
        "SELECT fact_key, status FROM overlay_fact_state WHERE confirmed_event_id = %s",
        (stamp_event_id,)).fetchone()
    if row is None:
        return GovernedFactHeadV1(fact_key="", folded_status="unknown",
                                  confirmed_event_id=stamp_event_id)
    return GovernedFactHeadV1(fact_key=row[0], folded_status=row[1],
                              confirmed_event_id=stamp_event_id)


def _field_hash_payload(field: EffectiveProfileFieldV1) -> dict:
    """The hash projection of one field: both resolutions, D2 triples, state, reasons, evidence
    ids — no timestamps, no provenance."""
    return {
        "display": asdict(field.display) if field.display is not None else None,
        "load_bearing": asdict(field.load_bearing) if field.load_bearing is not None else None,
        "state": field.state,
        "unresolved_reason": field.unresolved_reason,
        "unresolved_family": field.unresolved_family,
        "reason_codes": list(field.reason_codes),
    }


def build_dataset_profile(
    conn: DbConn,
    *,
    source: str,
    dataset_logical_ref: str,
    catalog_profile_revision_id: str | None = None,
) -> DatasetSemanticProfileV1 | None:
    """Assemble the effective semantic profile for ONE table (§6.3). Returns ``None`` when the
    table has no graph node (never uploaded / dropped) — the route maps that to 404 under its own
    visibility check.

    ``dataset_logical_ref`` is the SCHEMA-PRESERVING table logical ref (``normalize_ref(source,
    schema, table)``) — the key every evidence/decision store uses. ``catalog_profile_revision_id``
    is the CURRENT narrative pointer resolved by the caller (D12.10: authoring provenance lives on
    evidence; effective reads resolve the current pointer via ``profile_store``) — ``None`` when no
    narrative exists, which the profile reports as missing context.

    Run inside one REPEATABLE READ transaction (module docstring)."""
    _src, _schema, table, column = parse_ref(dataset_logical_ref)
    if column is not None:
        raise ValueError(f"dataset_logical_ref must be a TABLE ref, got {dataset_logical_ref!r}")
    # The graph stores table nodes PUBLIC-FLATTENED (field_resolution._graph_key convention).
    node = conn.execute(
        "SELECT grain_fact_event_id, availability_fact_event_id FROM graph_node "
        "WHERE catalog_source = %s AND lower(object_ref) = %s AND kind = 'table'",
        (source.strip().lower(), f"public.{table}".lower())).fetchone()
    if node is None:
        return None

    fields: dict[str, EffectiveProfileFieldV1] = {}
    for name, evidence_field in _PROFILE_FIELD_SOURCES.items():
        resolution, evidence, policy = _resolve_field(conn, dataset_logical_ref, evidence_field)
        fields[name] = _wrap_resolution(resolution, evidence, policy)

    # data_role — DERIVED from the existing canonical table_role resolution (correction 4): the
    # SAME resolver output, values mapped through the ONE adapter (legacy `bridge` displays as
    # `crosswalk`; the split of legacy `fact` follows the resolved event_or_snapshot display).
    tr_resolution, tr_evidence, tr_policy = _resolve_field(conn, dataset_logical_ref, "table_role")
    eos_display = (fields["event_or_snapshot"].display.value
                   if fields["event_or_snapshot"].display is not None else None)
    fields["data_role"] = _wrap_resolution(
        tr_resolution, tr_evidence, tr_policy,
        map_value=lambda v: data_role_from_table_role(v, event_or_snapshot=eos_display).value)

    grain_fact = _fact_head(conn, node[0])
    availability_fact = _fact_head(conn, node[1])

    missing: list[str] = []
    if catalog_profile_revision_id is None:
        missing.append("catalog_narrative:absent")
    field_order = ("description", "business_context", "domains", "data_role", "primary_entity",
                   "authority_role", "temporal_storage_model", "event_or_snapshot")
    for name in field_order:
        f = fields[name]
        if f.unresolved_reason is not None:
            missing.append(f"{name}:{f.unresolved_reason}")
    if grain_fact is None:
        missing.append("grain_fact:absent")
    if availability_fact is None:
        missing.append("availability_fact:absent")

    payload = {
        "contract": _PROFILE_CONTRACT,
        "dataset_logical_ref": dataset_logical_ref,
        "catalog_profile_revision_id": catalog_profile_revision_id,
        "fields": {name: _field_hash_payload(fields[name]) for name in field_order},
        "grain_fact": asdict(grain_fact) if grain_fact is not None else None,
        "availability_fact": asdict(availability_fact) if availability_fact is not None else None,
        "missing_context": missing,
    }
    return DatasetSemanticProfileV1(
        dataset_logical_ref=dataset_logical_ref,
        catalog_profile_revision_id=catalog_profile_revision_id,
        description=fields["description"],
        business_context=fields["business_context"],
        domains=fields["domains"],
        data_role=fields["data_role"],
        primary_entity=fields["primary_entity"],
        authority_role=fields["authority_role"],
        temporal_storage_model=fields["temporal_storage_model"],
        event_or_snapshot=fields["event_or_snapshot"],
        grain_fact=grain_fact,
        availability_fact=availability_fact,
        missing_context=tuple(missing),
        dataset_profile_hash=materialize_hash(payload),
    )
