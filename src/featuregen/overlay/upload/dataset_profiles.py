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

**[F4] Projection sensitivity of that identity (documented, not re-keyed):** those stamps are a
PROJECTION. ``build_graph`` wipes ``graph_node`` on every ingest and the end-of-ingest
``project_table_facts`` that restores the stamps is SKIPPED under overlay projection lag
(``ingest.py``, the ``table_fact_projection`` "lagged" branch). So an ingest that lands while the
projection trails leaves a genuinely CONFIRMED grain/availability fact temporarily unstamped —
the profile then reports ``grain_fact=None`` and ``dataset_profile_hash`` MOVES, purely from lag.
The identity stays the stamp (re-keying onto ``overlay_fact_state`` would trade this for a
divergence between the profile and every other stamp reader); instead the builder reads the SAME
readiness gate ingest uses and emits ``fact_heads:projection_lagged`` into ``missing_context``, so
the movement is explained at the point a consumer sees it. The code clears itself when the
projection catches up and the next ingest re-stamps.

**Isolation:** callers run the builder inside ONE ``REPEATABLE READ`` transaction so every field,
fact head and narrative pointer describe a single torn-free snapshot — the API reads use the
existing ``get_feature_gen_conn`` dependency (the asset-detail precedent); write surfaces assemble
under their own serialized transaction (source + table advisory locks).

**Hash (D1):** ``dataset_profile_hash`` = ``materialize_hash`` (RFC 8785 JCS) over both
resolutions, the D2 authority triples, states/unresolved reasons, evidence ids, the two fact heads
and the CURRENT catalog narrative revision id (D12.10 — narrative edits re-key dataset profiles;
byte-identical re-authoring resolves to the same deterministic revision id and does NOT). It
excludes physical bindings, environment, wall-clock, job state and projection timestamps.

**Values (D2/D5):** display/load_bearing values are the canonical
``semantic_context.SemanticValueV1`` — the value plus the FULL authority triple of every backing
evidence record, leading with the strongest. The wire and the hash publish the FLAT projection of
that (value + the leading triple + every backing evidence id) through the one
:func:`_value_payload` boundary; ``profile_payload`` — not ``dataclasses.asdict`` — is what the
routes serialize, because the canonical value nests its authority per record and this surface's
payload shape (and therefore ``dataset_profile_hash``) is defined on the flat one.
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
from featuregen.overlay.upload.profile_vocab import data_role_from_table_role
from featuregen.overlay.upload.semantic_context import (
    UNRESOLVED_AUTHORITY_INSUFFICIENT,
    UNRESOLVED_CONFLICT,
    UNRESOLVED_NO_EVIDENCE,
    UNRESOLVED_PENDING_REVALIDATION,
    UNRESOLVED_PENDING_REVIEW,
    EvidenceAuthorityV1,
    SemanticValueV1,
    unresolved_family,
    unresolved_label,
)
from featuregen.projections.runner import projection_lag

#: The contract token inside every ``dataset_profile_hash`` payload — bump ONLY on a meaning-bearing
#: contract change (a new hashed field, a state-vocabulary change), never for provenance.
_PROFILE_CONTRACT = "dataset_semantic_profile_v1"

#: [F4] The missing-context code that EXPLAINS a fact-head movement caused by projection lag rather
#: than by a governance decision (module docstring). It rides ``missing_context`` — and therefore the
#: hash — deliberately: a consumer that CAS-anchored on a stamped profile must SEE that the head it
#: is now missing is a lagged read, not a retraction.
MISSING_FACT_HEADS_LAGGED = "fact_heads:projection_lagged"

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
# No Release-A producer emits this yet; it lands with the deterministic-contradiction path (Task 4:
# e.g. SCD2 claimed with no candidate boundary columns). Its REASON comes from the canonical
# vocabulary's `structurally_unsuitable:*` members — this module holds no reason of its own.
STATE_STRUCTURALLY_UNSUITABLE = "structurally_unsuitable"
STATE_CONFLICT = "conflict"
STATE_PROJECTION_UNAVAILABLE = "projection_unavailable"
# The honest "nobody decided yet" state (F2/F9): evidence exists but nothing clears the display
# bar — competing unreviewed proposals, or evidence below every display rule. NEVER paired with a
# display value; `display_only` is reserved for fields that actually display something.
STATE_UNDECIDED = "undecided"


@dataclass(frozen=True, slots=True)
class EffectiveProfileFieldV1:
    """One profile field's effective view — an explicit typed RE-WRAPPER of the authority engine's
    :class:`overlay.field_authority.FieldResolution` (there is NO second resolver: the same pure
    ``resolve_field_authority`` under the same registered policy produced ``display``/
    ``load_bearing``; this class only types the values with their D2 triples and re-expresses the
    resolver's reason in the closed §6.3 state + D5 three-family vocabulary).

    CRITICAL display semantics (§6.3 as amended): for a RECOMMENDATION-ceiling field,
    ``state=display_only`` is the NORMAL state — ``unresolved_reason`` is ``None`` there, and is
    populated ONLY from the three-family ``semantic_context.UNRESOLVED_REASONS`` vocabulary.
    "No evidence at all" is ``no_evidence`` + ``undecided`` — honestly undecided, never
    failure-styled.

    ``unresolved_reason``/``unresolved_family`` are the canonical member SPLIT IN TWO (the
    ``unresolved_label``/``unresolved_family`` pair of one ``undecided:no_evidence``-style member),
    because this surface has always published the family in its own column."""

    display: SemanticValueV1 | None
    load_bearing: SemanticValueV1 | None
    state: str
    unresolved_reason: str | None      # a canonical member's family-free label, or None
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
    """The assembled effective semantic profile for ONE table (§6.3, D5 naming).

    **[F4] ``grain_fact``/``availability_fact`` are PROJECTION-SENSITIVE.** They read the TABLE
    node's graph stamps, which an ingest wipes and only re-writes when the overlay projection is
    caught up — so both heads (and therefore ``dataset_profile_hash``) can move under projection
    lag with no governance decision behind it. When that gate reports lag, ``missing_context``
    carries :data:`MISSING_FACT_HEADS_LAGGED` and the absence must be read as "not yet re-stamped",
    never as "the fact was retracted". See the module docstring for why the identity is not
    re-keyed."""

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
    evidence: list[FieldEvidence], value: str | None, *, field_name: str,
    mapped: str | None = None,
) -> SemanticValueV1 | None:
    """Type ``value`` as a canonical :class:`SemanticValueV1` carrying the D2 authority triple of
    EVERY record backing exactly this value, LEADING with the strongest (the record whose triple
    the flat wire projection publishes, and the one ``display_label()`` derives from). ``mapped``
    substitutes a display-mapped value (data_role) while the backing is still matched on the
    resolver's raw ``value``. ``None`` value -> ``None``.

    ``resolution_status="current"``: a profile field is always the CURRENTLY resolved value, never
    a source-declared one. ``operational_influence`` stays ``None`` — that axis is the operational
    facts surface, which this read model does not consult and must not guess at."""
    if value is None:
        return None
    backing = [e for e in evidence if to_view(e).value == value]
    if not backing:   # defensive: a resolver value always has backing among its inputs
        return None
    # Lead with the strongest exactly as before (`max` keeps the first of a tie in read order);
    # the rest follow by evidence id so the value is deterministic for the hash.
    best = max(backing, key=lambda e: _STRENGTH_RANK.get(e.strength, -1))
    rest = sorted((e for e in backing if e.evidence_id != best.evidence_id),
                  key=lambda e: e.evidence_id)
    return SemanticValueV1(
        field_name=field_name,
        value=mapped if mapped is not None else value,
        evidence=tuple(
            EvidenceAuthorityV1(producer=e.producer, strength=e.strength, lifecycle=e.lifecycle,
                                producer_ref=e.producer_ref, evidence_id=e.evidence_id)
            for e in (best, *rest)),
        resolution_status="current",
    )


def _wrap_resolution(
    resolution: FieldResolution,
    evidence: list[FieldEvidence],
    policy: FieldPolicy,
    *,
    field_name: str,
    map_value=None,
) -> EffectiveProfileFieldV1:
    """Re-wrap one :class:`FieldResolution` into the §6.3 shape (see the class docstring).

    ``field_name`` is the PROFILE field name (``data_role``, not the ``table_role`` evidence it
    derives from) — it names the value on the canonical :class:`SemanticValueV1`. ``map_value``
    (data_role) maps a resolver value into its DISPLAY vocabulary member without touching the
    underlying evidence."""
    mapper = map_value if map_value is not None else (lambda v: v)
    display = _semantic_value(evidence, resolution.display_value, field_name=field_name,
                              mapped=mapper(resolution.display_value)
                              if resolution.display_value is not None else None)
    load_bearing = _semantic_value(evidence, resolution.load_bearing_value, field_name=field_name,
                                   mapped=mapper(resolution.load_bearing_value)
                                   if resolution.load_bearing_value is not None else None)
    reason_codes = (resolution.unresolved_reason,) if resolution.unresolved_reason else ()

    if not evidence:
        return EffectiveProfileFieldV1(
            display=None, load_bearing=None, state=STATE_NO_EVIDENCE,
            unresolved_reason=unresolved_label(UNRESOLVED_NO_EVIDENCE),
            unresolved_family=unresolved_family(UNRESOLVED_NO_EVIDENCE),
            reason_codes=reason_codes)
    if load_bearing is not None:
        return EffectiveProfileFieldV1(
            display=display, load_bearing=load_bearing, state=STATE_LOAD_BEARING,
            unresolved_reason=None, unresolved_family=None, reason_codes=())

    # A RECOMMENDATION field short-circuits at the influence ceiling BEFORE the resolver's own
    # conflict detection (resolution order step 4 < step 7), so a display-tie surfaces here only
    # as display=None. Detect it the way `_select`'s PREFER_CONFIRMED does — distinct values tied
    # at the top strength.
    #
    # [F2] HONESTY NOTE on what this wrapper can distinguish: the resolver reports only that the
    # tie exists; THIS wrapper sees the evidence and can distinguish ties by the TIED STRENGTH
    # (it does not adjudicate producer pairs). A tie at PROPOSED is a set of UNREVIEWED
    # proposals — nobody with authority has decided anything, so the product family is
    # `undecided` (undecided:pending_review), never a failure-shaped conflict. A tie at
    # supported/attested/confirmed is a genuine contradiction between load-bearing-capable
    # assertions (e.g. two source attestations disagreeing) — THAT is conflict/needs_data_check.
    if display is None:
        top = max(_STRENGTH_RANK.get(e.strength, -1) for e in evidence)
        top_values = {to_view(e).value for e in evidence
                      if _STRENGTH_RANK.get(e.strength, -1) == top}
        if len(top_values) > 1:
            if top <= _STRENGTH_RANK[AssertionStrength.PROPOSED.value]:
                return EffectiveProfileFieldV1(
                    display=None, load_bearing=None, state=STATE_UNDECIDED,
                    unresolved_reason=unresolved_label(UNRESOLVED_PENDING_REVIEW),
                    unresolved_family=unresolved_family(UNRESOLVED_PENDING_REVIEW),
                    reason_codes=reason_codes)
            return EffectiveProfileFieldV1(
                display=None, load_bearing=None, state=STATE_CONFLICT,
                unresolved_reason=unresolved_label(UNRESOLVED_CONFLICT),
                unresolved_family=unresolved_family(UNRESOLVED_CONFLICT),
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
        reason = UNRESOLVED_AUTHORITY_INSUFFICIENT
        state = STATE_DISPLAY_ONLY
    elif raw == "authority_insufficient":
        reason = UNRESOLVED_AUTHORITY_INSUFFICIENT
        state = STATE_DISPLAY_ONLY
    elif raw == "conflict":
        reason = UNRESOLVED_CONFLICT
        state = STATE_CONFLICT
    elif raw.startswith("disqualified:"):
        # The only honoured disqualifier today is CONFIRMATION_PENDING_REVALIDATION — a material
        # change awaiting a human re-check: squarely the "needs a data check" family.
        reason = UNRESOLVED_PENDING_REVALIDATION
        state = STATE_NEEDS_DATA_OBSERVATION
    else:
        # "specialized_fact" and any future resolver reason: the generic projection cannot serve
        # this field — visible as projection_unavailable, family undecided (nobody decided HERE).
        # Deliberately NOT the canonical `needs_data_check:projection_unavailable` member: that one
        # says "go check the data", and nothing here is wrong with the data — the STATE names the
        # degraded read, while the reason stays honestly "nobody decided at this surface".
        reason = UNRESOLVED_AUTHORITY_INSUFFICIENT
        state = STATE_PROJECTION_UNAVAILABLE
    if state == STATE_DISPLAY_ONLY and display is None:
        # [F9] `display_only` with nothing to display is a lying state. When no active evidence
        # clears the display bar, report the honest family-shaped state the reason maps to
        # (`undecided` for authority_insufficient) — `display_only` is reserved for fields that
        # actually display something.
        state = unresolved_family(reason)
    return EffectiveProfileFieldV1(
        display=display, load_bearing=None, state=state,
        unresolved_reason=unresolved_label(reason), unresolved_family=unresolved_family(reason),
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


def _value_payload(value: SemanticValueV1 | None) -> dict | None:
    """The FLAT projection of one canonical semantic value — the value, the D2 triple of its
    LEADING (strongest) evidence, and every backing evidence id.

    This is the single adapter between the canonical value (which nests authority per evidence
    record) and this surface's published shape. Both the API payload and ``dataset_profile_hash``
    are defined on it, so a change here moves the wire AND re-keys every profile."""
    if value is None:
        return None
    lead = value.evidence[0]
    return {
        "value": value.value,
        "producer": lead.producer,
        "strength": lead.strength,
        "lifecycle": lead.lifecycle,
        "evidence_ids": tuple(sorted(e.evidence_id for e in value.evidence)),
    }


def _field_payload(field: EffectiveProfileFieldV1) -> dict:
    """The projection of one field: both resolutions, D2 triples, state, reasons, evidence ids —
    no timestamps, no provenance. The hash and the API response share it BY DESIGN: what a
    consumer CAS-anchors on is exactly what it was shown."""
    return {
        "display": _value_payload(field.display),
        "load_bearing": _value_payload(field.load_bearing),
        "state": field.state,
        "unresolved_reason": field.unresolved_reason,
        "unresolved_family": field.unresolved_family,
        "reason_codes": list(field.reason_codes),
    }


#: The §6.3 field order — the hash payload's key order and the response's field order.
_EFFECTIVE_FIELDS = ("description", "business_context", "domains", "data_role", "primary_entity",
                     "authority_role", "temporal_storage_model", "event_or_snapshot")


def profile_payload(profile: DatasetSemanticProfileV1) -> dict:
    """The API projection of an assembled profile — what the routes serialize.

    NOT ``dataclasses.asdict``: the canonical :class:`SemanticValueV1` nests its authority per
    evidence record, and this surface publishes the flat triple (:func:`_value_payload`). Key order
    follows the dataclass, so the rendered body is byte-identical to the ``asdict`` it replaced."""
    return {
        "dataset_logical_ref": profile.dataset_logical_ref,
        "catalog_profile_revision_id": profile.catalog_profile_revision_id,
        **{name: _field_payload(getattr(profile, name)) for name in _EFFECTIVE_FIELDS},
        "grain_fact": asdict(profile.grain_fact) if profile.grain_fact is not None else None,
        "availability_fact": (asdict(profile.availability_fact)
                              if profile.availability_fact is not None else None),
        "missing_context": list(profile.missing_context),
        "dataset_profile_hash": profile.dataset_profile_hash,
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
        fields[name] = _wrap_resolution(resolution, evidence, policy, field_name=name)

    # data_role — DERIVED from the existing canonical table_role resolution (correction 4): the
    # SAME resolver output, values mapped through the ONE adapter (legacy `bridge` displays as
    # `crosswalk`; the split of legacy `fact` follows the resolved event_or_snapshot display).
    tr_resolution, tr_evidence, tr_policy = _resolve_field(conn, dataset_logical_ref, "table_role")
    eos_display = (fields["event_or_snapshot"].display.value
                   if fields["event_or_snapshot"].display is not None else None)
    fields["data_role"] = _wrap_resolution(
        tr_resolution, tr_evidence, tr_policy, field_name="data_role",
        map_value=lambda v: data_role_from_table_role(v, event_or_snapshot=eos_display).value)

    grain_fact = _fact_head(conn, node[0])
    availability_fact = _fact_head(conn, node[1])

    missing: list[str] = []
    if catalog_profile_revision_id is None:
        missing.append("catalog_narrative:absent")
    field_order = _EFFECTIVE_FIELDS
    for name in field_order:
        f = fields[name]
        if f.unresolved_reason is not None:
            missing.append(f"{name}:{f.unresolved_reason}")
    if grain_fact is None:
        missing.append("grain_fact:absent")
    if availability_fact is None:
        missing.append("availability_fact:absent")
    # [F4] The SAME readiness gate ingest consults before it re-stamps the fact heads. Under lag the
    # two heads above are a lagged read of a projection, not a statement about governance — say so.
    if projection_lag(conn, "overlay") > 0:
        missing.append(MISSING_FACT_HEADS_LAGGED)

    payload = {
        "contract": _PROFILE_CONTRACT,
        "dataset_logical_ref": dataset_logical_ref,
        "catalog_profile_revision_id": catalog_profile_revision_id,
        "fields": {name: _field_payload(fields[name]) for name in field_order},
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
