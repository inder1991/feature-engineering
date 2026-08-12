"""Phase-3B.2B — cross-catalog entity-bridge candidate discovery.

A candidate nominates two catalog-local identifier columns drawing from the same identifier
NAMESPACE (the issuer's value space, ``Concept.namespace`` — three-axis model); it does not claim
that their populations are equal. Namespace is the ONLY axis that gates candidacy: entity is
carried on the candidate for corroboration and display, picked deterministically because
``fact_key`` hashes ``entity_id``, and an in-namespace entity mismatch is a display note
(``entity_disagreement``), never a suppression. Bridge-specific grounding keeps the remaining
claims separate and suppresses hard representation/namespace conflicts. Discovery is read-only and
deterministic. A proposal becomes available without human confirmation; confirmation records
review but is not a consumption gate.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from itertools import combinations, product

from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.attest.bridge_grounding import (
    ENTITY_DISAGREEMENT,
    BridgeEndpointGroundingV1,
    EvidencePresence,
    RepresentationRole,
    assess_grounded_identifier_link,
    ground_bridge_endpoint,
    resolve_type_family,
    type_family,
)
from featuregen.overlay.upload.bridge_assessment import (
    CANDIDATE_FAMILY_IDENTIFIER_LINK,
    ConceptAuthority,
    IdentifierLinkAssessmentV1,
)
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.party_vocab import PartyRole, normalize_party_role
from featuregen.overlay.upload.read_scope import allowed_sensitivities

BRIDGE_DERIVATION_VERSION = "3.0.0"
MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR = 20
MAX_PAIR_POOL_PER_ENDPOINT = 80
MAX_BRIDGE_DERIVATION_EXAMPLES = 10
_SMALL_BLOCK_PAIR_LIMIT = MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR**2
_GENERIC_HINT_TOKENS = frozenset({
    "id", "identifier", "key", "ref", "reference", "num", "number", "no", "nbr", "code", "cd",
    "customer", "account", "entity",
})

def _type_family(data_type: str | None) -> str:
    """The family a declared type belongs to, ignoring any length/precision parameter.

    A `varchar(150)` and a `varchar(50)` hold the same KIND of value, so refusing to bridge them
    would be refusing on a formatting detail. An exact lookup matched only the bare `varchar`, which
    made every column of the real second source unclassifiable — the same inert outcome as the
    missing declared type, one layer further down.
    """
    return type_family(data_type)


@dataclass(frozen=True, slots=True)
class BridgeCandidateV1:
    candidate_id: str
    entity_id: str
    left_ref: CatalogObjectRef
    right_ref: CatalogObjectRef
    data_type_family: str
    left_is_grain: bool
    right_is_grain: bool
    #: How the matched family was established: ``attested`` (both sides from a structural source),
    #: ``declared`` (both from a glossary file's own answer), or ``mixed``. Advisory metadata for the
    #: human confirmer — a DECLARED type is someone's spreadsheet entry, not a read of the physical
    #: schema — and deliberately OUTSIDE ``candidate_id``, so re-deriving the same pair after a
    #: structural source attests the type keeps the SAME candidate rather than forking a second one.
    type_basis: str = "attested"
    candidate_family: str = CANDIDATE_FAMILY_IDENTIFIER_LINK
    left_concept_authority: str = ConceptAuthority.UNKNOWN.value
    right_concept_authority: str = ConceptAuthority.UNKNOWN.value
    assessment: IdentifierLinkAssessmentV1 | None = None


@dataclass(frozen=True, slots=True)
class BridgeDerivationExampleV1:
    left_ref: str
    right_ref: str
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "left_ref": self.left_ref,
            "right_ref": self.right_ref,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class BridgeCandidateDerivationV1:
    """Bounded candidate output plus an honest account of everything not retained.

    ``considered_count`` means a pair that reached full bridge grounding/scoring.  A cheap block
    match that was never fully scored is counted only by ``block_matched_count`` and, when omitted,
    ``truncated_pair_count``.
    """

    candidates: tuple[BridgeCandidateV1, ...]
    block_matched_count: int
    considered_count: int
    retained_count: int
    suppressed_count: int
    truncated_pair_count: int
    reason_counts: tuple[tuple[str, int], ...] = ()
    examples: tuple[BridgeDerivationExampleV1, ...] = ()

    @property
    def truncated(self) -> bool:
        return self.truncated_pair_count > 0

    @property
    def truncation_reason(self) -> str | None:
        return "per_endpoint_source_pair_limit" if self.truncated else None

    def stage_detail(
        self,
        *,
        proposed_count: int | None = None,
        proposal_error_count: int = 0,
    ) -> dict[str, object]:
        detail: dict[str, object] = {
            "block_matched_count": self.block_matched_count,
            "considered_count": self.considered_count,
            "retained_count": self.retained_count,
            "suppressed_count": self.suppressed_count,
            "truncated_pair_count": self.truncated_pair_count,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "reason_counts": dict(self.reason_counts),
            "examples": [example.as_dict() for example in self.examples],
        }
        if proposed_count is not None:
            detail["proposed"] = proposed_count
            detail["proposal_errors"] = proposal_error_count
        return detail


@dataclass(frozen=True, slots=True)
class _IdCol:
    catalog_source: str
    table_name: str
    column_name: str
    entity: str
    #: The concept's declared identifier namespace — the GROUPING KEY for candidacy. Entity above
    #: is carried for corroboration/display and the deterministic candidate entity pick only.
    namespace: str
    type_family: str
    is_grain: bool
    type_basis: str
    concept_authority: str
    grounding: BridgeEndpointGroundingV1


def _resolve_family(data_type: str | None, declared_type: str | None) -> tuple[str, str]:
    """``(family, basis)`` for one column, attested first.

    A GLOSSARY upload attests no physical type — the FTR adapter emits ``CanonicalRow.type='unknown'``
    and carries the file's own answer in ``graph_node.declared_type`` — so reading ``data_type`` alone
    classified every glossary column as an unrecognised family and dropped it. That made a bridge
    candidate structurally impossible for a glossary-sourced catalog: measured on the deployed FTR
    catalog, 126/126 columns are ``data_type='unknown'`` while 113 carry ``declared_type='string'``,
    including the single ``customer_id`` the whole cross-catalog story hangs on.

    The attested value stays the stronger authority and decides whenever it classifies. The declared
    value is consulted ONLY when nothing was attested, and the caller records that weaker basis on the
    candidate rather than hiding it. An unclassifiable declared value is NOT a wildcard — it stays
    ``other`` and is excluded, so the family check keeps doing the job it exists for."""
    family, basis = resolve_type_family(data_type, declared_type)
    return family, basis.value


def _identifier_columns(conn, *, roles: Iterable[str]) -> list[_IdCol]:
    rows = conn.execute(
        "SELECT catalog_source, table_name, column_name "
        "FROM graph_node "
        "WHERE kind = 'column' AND concept IS NOT NULL "
        "AND visible_requires <@ %s "
        "ORDER BY catalog_source, object_ref",
        (allowed_sensitivities(roles),)).fetchall()
    out: list[_IdCol] = []
    for catalog_source, table_name, column_name in rows:
        logical_ref = normalize_ref(
            catalog_source, "public", table_name, column_name)
        grounding = ground_bridge_endpoint(conn, logical_ref)
        concept_name = grounding.concept.concept
        c = concept(concept_name) if concept_name is not None else None
        # Identifier concepts all declare a namespace (validated at registry import) and an
        # entity_link; a column whose concept is missing from the registry derives nothing.
        if c is None or c.group != "identifier" or not c.namespace or not c.entity_link:
            continue
        out.append(_IdCol(
            catalog_source=catalog_source,
            table_name=table_name,
            column_name=column_name,
            # The RAW registry entity_link (D12.1-revised): this entity feeds _entity_pick and
            # therefore fact_key — never the display seam. `customer` is read-time display only.
            entity=grounding.entity_id or c.entity_link,
            namespace=c.namespace,
            type_family=grounding.data_type_family,
            is_grain=grounding.is_grain,
            type_basis=grounding.type_basis.value,
            concept_authority=grounding.concept.authority.value,
            grounding=grounding,
        ))
    return out


def _col_ref(col: _IdCol) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=col.catalog_source, object_kind="column", schema="public",
                            table=col.table_name, column=col.column_name)


def _entity_pick(a: _IdCol, b: _IdCol) -> tuple[str, bool]:
    """The candidate's carried entity, DETERMINISTICALLY — ``fact_key`` hashes ``entity_id``, so
    an unstable pick would re-key the same link across re-derivations.

    Agreeing endpoints keep their shared entity (the existing links' identity is preserved).
    Disagreeing endpoints prefer the SUBJECT-role endpoint's entity where the party-role axis
    distinguishes exactly one subject, else the lexicographic minimum. Symmetric in (a, b)."""
    if a.entity == b.entity:
        return a.entity, False
    subjects = [
        col for col in (a, b)
        if normalize_party_role(col.column_name) is PartyRole.SUBJECT
    ]
    if len(subjects) == 1:
        return subjects[0].entity, True
    return min(a.entity, b.entity), True


def _candidate(a: _IdCol, b: _IdCol) -> BridgeCandidateV1:
    left, right = sorted((a, b), key=lambda c: (c.catalog_source, c.table_name, c.column_name))
    entity, entity_disagreement = _entity_pick(left, right)
    grounding = assess_grounded_identifier_link(left.grounding, right.grounding)
    assessment = grounding.to_assessment()
    if entity_disagreement and ENTITY_DISAGREEMENT not in assessment.explanation_codes:
        assessment = replace(
            assessment,
            explanation_codes=(*assessment.explanation_codes, ENTITY_DISAGREEMENT),
        )
    basis = left.type_basis if left.type_basis == right.type_basis else "mixed"
    return BridgeCandidateV1(
        candidate_id=assessment.candidate_id,
        entity_id=entity,
        left_ref=_col_ref(left),
        right_ref=_col_ref(right),
        data_type_family=left.type_family, left_is_grain=left.is_grain, right_is_grain=right.is_grain,
        type_basis=basis,
        left_concept_authority=left.concept_authority,
        right_concept_authority=right.concept_authority,
        assessment=assessment,
    )


def _col_key(col: _IdCol) -> tuple[str, str, str]:
    return (col.catalog_source, col.table_name, col.column_name)


def _specific_tokens(col: _IdCol, field_name: str) -> tuple[str, ...]:
    facet = col.grounding.facet(field_name)
    if facet.presence is EvidencePresence.ABSENT:
        return ()
    return tuple(
        token
        for token in facet.canonical_tokens
        if token not in _GENERIC_HINT_TOKENS
    )


def _blocking_hints(col: _IdCol) -> tuple[str, ...]:
    """Cheap deterministic lookup keys; none is itself a namespace conclusion."""
    hints: list[str] = []
    grounding = col.grounding
    if grounding.explicit_namespace:
        hints.append(f"namespace:{grounding.explicit_namespace}")
    if grounding.concept.concept:
        hints.append(f"concept:{grounding.concept.concept}")
    name_tokens = _specific_tokens(col, "names")
    if name_tokens:
        hints.append(f"name:{' '.join(name_tokens)}")
        hints.extend(f"name-token:{token}" for token in name_tokens)
    for field_name in ("terms", "synonyms", "taxonomy"):
        hints.extend(
            f"{field_name}:{token}"
            for token in _specific_tokens(col, field_name)
        )
    hints.append(f"role:{grounding.representation_role.value}")
    if col.is_grain:
        hints.append("key:declared-grain-member")
    return tuple(dict.fromkeys(hints))


def _counterpart_index(columns: tuple[_IdCol, ...]) -> dict[str, tuple[_IdCol, ...]]:
    index: dict[str, list[_IdCol]] = defaultdict(list)
    for col in columns:
        for hint in _blocking_hints(col):
            index[hint].append(col)
    return {
        hint: tuple(sorted(values, key=_col_key))
        for hint, values in index.items()
    }


def _metadata_overlap(left: _IdCol, right: _IdCol, field_name: str) -> int:
    return len(
        set(left.grounding.facet(field_name).canonical_tokens)
        & set(right.grounding.facet(field_name).canonical_tokens)
    )


def _cheap_pair_score(left: _IdCol, right: _IdCol) -> int:
    """Rank within a block. Scores order discovery only; they never authorize execution."""
    score = 0
    left_grounding = left.grounding
    right_grounding = right.grounding
    if left_grounding.explicit_namespace and right_grounding.explicit_namespace:
        score += (
            1_000
            if left_grounding.explicit_namespace == right_grounding.explicit_namespace
            else -1_000
        )
    if left_grounding.concept.concept == right_grounding.concept.concept:
        score += 80
    if (
        left_grounding.representation_role is RepresentationRole.IDENTIFIER_VALUE
        and right_grounding.representation_role is RepresentationRole.IDENTIFIER_VALUE
    ):
        score += 60
    if left_grounding.representation_role != right_grounding.representation_role:
        score -= 80
    left_name = _specific_tokens(left, "names")
    right_name = _specific_tokens(right, "names")
    if left_name and left_name == right_name:
        score += 120
    score += min(_metadata_overlap(left, right, "terms"), 3) * 35
    score += min(_metadata_overlap(left, right, "synonyms"), 3) * 30
    score += min(_metadata_overlap(left, right, "taxonomy"), 3) * 20
    score += min(_metadata_overlap(left, right, "definition"), 3) * 5
    if left.is_grain and right.is_grain:
        score += 10
    elif left.is_grain or right.is_grain:
        score += 5
    return score


def _bounded_counterparts(
    endpoint: _IdCol,
    *,
    counterparts: tuple[_IdCol, ...],
    index: Mapping[str, tuple[_IdCol, ...]],
) -> tuple[_IdCol, ...]:
    """Use bounded inverted-index reads, then keep the endpoint's best twenty possibilities."""
    pool: dict[tuple[str, str, str], _IdCol] = {}
    for hint in _blocking_hints(endpoint):
        for candidate in index.get(hint, ())[:MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR]:
            pool[_col_key(candidate)] = candidate
            if len(pool) >= MAX_PAIR_POOL_PER_ENDPOINT:
                break
        if len(pool) >= MAX_PAIR_POOL_PER_ENDPOINT:
            break
    # A reachable fallback is required for differently-named but valid identifiers such as
    # cust_num <-> cif_id. It is bounded and deterministically ordered.
    for candidate in counterparts[:MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR]:
        pool.setdefault(_col_key(candidate), candidate)
    ranked = sorted(
        pool.values(),
        key=lambda candidate: (
            -_cheap_pair_score(endpoint, candidate),
            _col_key(candidate),
        ),
    )
    return tuple(ranked[:MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR])


def _pair_keys_for_block(
    left: tuple[_IdCol, ...],
    right: tuple[_IdCol, ...],
) -> set[tuple[tuple[str, str, str], tuple[str, str, str]]]:
    if len(left) * len(right) <= _SMALL_BLOCK_PAIR_LIMIT:
        return {
            (_col_key(left_col), _col_key(right_col))
            for left_col, right_col in product(left, right)
        }
    left_index = _counterpart_index(left)
    right_index = _counterpart_index(right)
    pairs: set[tuple[tuple[str, str, str], tuple[str, str, str]]] = set()
    for left_col in left:
        for right_col in _bounded_counterparts(
            left_col, counterparts=right, index=right_index
        ):
            pairs.add((_col_key(left_col), _col_key(right_col)))
    # Symmetric nomination prevents source ordering from deciding which strong pairs are visible.
    for right_col in right:
        for left_col in _bounded_counterparts(
            right_col, counterparts=left, index=left_index
        ):
            pairs.add((_col_key(left_col), _col_key(right_col)))
    return pairs


def _bound_pairs_before_full_scoring(
    pair_keys: set[tuple[tuple[str, str, str], tuple[str, str, str]]],
    *,
    left_by_key: Mapping[tuple[str, str, str], _IdCol],
    right_by_key: Mapping[tuple[str, str, str], _IdCol],
) -> tuple[tuple[tuple[str, str, str], tuple[str, str, str]], ...]:
    """Apply the symmetric degree cap before constructing full assessments.

    Either side may nominate a pair, but a hub endpoint cannot be fully scored merely because many
    counterparts nominated it. Cheap metadata ranking decides which bounded pairs reach grounding.
    """
    ranked = sorted(
        pair_keys,
        key=lambda pair: (
            -_cheap_pair_score(left_by_key[pair[0]], right_by_key[pair[1]]),
            pair,
        ),
    )
    left_degree: dict[tuple[str, str, str], int] = defaultdict(int)
    right_degree: dict[tuple[str, str, str], int] = defaultdict(int)
    retained: list[tuple[tuple[str, str, str], tuple[str, str, str]]] = []
    for left_key, right_key in ranked:
        if (
            left_degree[left_key] >= MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR
            or right_degree[right_key] >= MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR
        ):
            continue
        left_degree[left_key] += 1
        right_degree[right_key] += 1
        retained.append((left_key, right_key))
    return tuple(retained)


def _bounded_example(
    examples: list[BridgeDerivationExampleV1],
    example: BridgeDerivationExampleV1,
) -> None:
    examples.append(example)
    examples.sort(key=lambda item: (item.left_ref, item.right_ref, item.reason_codes))
    del examples[MAX_BRIDGE_DERIVATION_EXAMPLES:]


def _derive_from_identifier_columns(
    columns: Iterable[_IdCol],
) -> BridgeCandidateDerivationV1:
    """Pure bounded enumeration over endpoints that were grounded once."""
    blocks: dict[tuple[str, str], dict[str, list[_IdCol]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for col in columns:
        if col.type_family == "other":
            continue
        # The BLOCKING KEY is the identifier namespace: cross-namespace pairs are impossible by
        # construction. Entity never gates — it rides on the candidate for corroboration/display.
        blocks[(col.namespace, col.type_family)][col.catalog_source].append(col)

    candidates: dict[str, BridgeCandidateV1] = {}
    reason_counts: dict[str, int] = defaultdict(int)
    examples: list[BridgeDerivationExampleV1] = []
    block_matched_count = 0
    considered_count = 0
    suppressed_count = 0

    for (_namespace, _family), by_source in sorted(blocks.items()):
        normalized = {
            source: tuple(sorted(values, key=_col_key))
            for source, values in by_source.items()
        }
        for left_source, right_source in combinations(sorted(normalized), 2):
            left_columns = normalized[left_source]
            right_columns = normalized[right_source]
            block_matched_count += len(left_columns) * len(right_columns)
            left_by_key = {_col_key(col): col for col in left_columns}
            right_by_key = {_col_key(col): col for col in right_columns}
            pair_keys = _bound_pairs_before_full_scoring(
                _pair_keys_for_block(left_columns, right_columns),
                left_by_key=left_by_key,
                right_by_key=right_by_key,
            )
            considered_count += len(pair_keys)

            scored: list[tuple[int, BridgeCandidateV1]] = []
            for left_key, right_key in sorted(pair_keys):
                candidate = _candidate(
                    left_by_key[left_key], right_by_key[right_key])
                assessment = candidate.assessment
                assert assessment is not None
                if assessment.hard_conflicts:
                    suppressed_count += 1
                    for reason in assessment.hard_conflicts:
                        reason_counts[f"hard_conflict:{reason}"] += 1
                    _bounded_example(
                        examples,
                        BridgeDerivationExampleV1(
                            assessment.left_endpoint.members[0].logical_column_ref,
                            assessment.right_endpoint.members[0].logical_column_ref,
                            assessment.hard_conflicts,
                        ),
                    )
                    continue
                score = _cheap_pair_score(
                    left_by_key[left_key], right_by_key[right_key])
                scored.append((score, candidate))

            for _score, candidate in sorted(
                scored, key=lambda item: (-item[0], item[1].candidate_id)
            ):
                candidates[candidate.candidate_id] = candidate
                assessment = candidate.assessment
                assert assessment is not None
                if assessment.namespace_verdict.value in {"possible", "unknown"}:
                    _bounded_example(
                        examples,
                        BridgeDerivationExampleV1(
                            assessment.left_endpoint.members[0].logical_column_ref,
                            assessment.right_endpoint.members[0].logical_column_ref,
                            ("ambiguous_identifier_namespace",),
                        ),
                    )

    retained_count = len(candidates)
    # Includes both pairs never fully scored and fully-scored valid pairs dropped by the symmetric
    # endpoint cap. Hard-conflict suppressions are reported separately.
    truncated_pair_count = max(
        0, block_matched_count - retained_count - suppressed_count)
    if truncated_pair_count:
        reason_counts["per_endpoint_source_pair_limit"] += truncated_pair_count
    return BridgeCandidateDerivationV1(
        candidates=tuple(candidates[key] for key in sorted(candidates)),
        block_matched_count=block_matched_count,
        considered_count=considered_count,
        retained_count=retained_count,
        suppressed_count=suppressed_count,
        truncated_pair_count=truncated_pair_count,
        reason_counts=tuple(sorted(reason_counts.items())),
        examples=tuple(examples),
    )


def _current_identifier_column(conn, ref: CatalogObjectRef) -> _IdCol | None:
    """Resolve one proposal endpoint against the live graph, never against caller-supplied fields."""
    if (
        ref.object_kind.strip().lower() != "column"
        or ref.schema.strip().lower() != "public"
        or not ref.column
    ):
        return None
    logical_ref = normalize_ref(
        ref.catalog_source, "public", ref.table, ref.column)
    grounding = ground_bridge_endpoint(conn, logical_ref)
    if not grounding.exists:
        return None
    concept_name = grounding.concept.concept
    registered = concept(concept_name) if concept_name is not None else None
    if (
        registered is None
        or registered.group != "identifier"
        or not registered.namespace
        or not registered.entity_link
    ):
        return None
    return _IdCol(
        catalog_source=ref.catalog_source.strip().lower(),
        table_name=ref.table.strip().lower(),
        column_name=ref.column.strip().lower(),
        entity=grounding.entity_id or registered.entity_link,
        namespace=registered.namespace,
        type_family=grounding.data_type_family,
        is_grain=grounding.is_grain,
        type_basis=grounding.type_basis.value,
        concept_authority=grounding.concept.authority.value,
        grounding=grounding,
    )


def bridge_catalog_write_error(conn, ref) -> str | None:
    """State-aware bridge write gate shared by propose, confirm and direct entry.

    The structural identity gate in :mod:`overlay.identity` proves ref/value consistency. This gate
    proves that both claimed endpoints still exist as catalog columns, still draw from ONE
    identifier namespace, and that the claimed entity is the deterministic pick over the two
    endpoints — entity corroborates and displays, it never gates candidacy on its own. Human
    approval is deliberately not required.
    """
    from featuregen.overlay.identity import EntityBridgeRef

    if not isinstance(ref, EntityBridgeRef):
        return "entity_bridge requires an EntityBridgeRef"
    current_endpoints: dict[str, _IdCol] = {}
    for side, endpoint in (("left", ref.left_ref), ("right", ref.right_ref)):
        current = _current_identifier_column(conn, endpoint)
        if current is None:
            return (
                f"entity_bridge {side} endpoint does not exist as a flat logical identifier column: "
                f"{endpoint.catalog_source}::public.{endpoint.table}.{endpoint.column or ''}"
            )
        current_endpoints[side] = current
    left, right = current_endpoints["left"], current_endpoints["right"]
    if left.namespace != right.namespace:
        return (
            f"entity_bridge endpoints draw from different identifier namespaces "
            f"({left.namespace!r} vs {right.namespace!r}); a cross-namespace link is impossible"
        )
    picked_entity, _entity_disagreement = _entity_pick(left, right)
    if picked_entity != ref.entity_id.strip().lower():
        return (
            f"entity_bridge claimed entity {ref.entity_id!r} does not match the deterministic "
            f"entity pick {picked_entity!r} for these endpoints"
        )
    grounding = assess_grounded_identifier_link(
        current_endpoints["left"].grounding,
        current_endpoints["right"].grounding,
    )
    if grounding.hard_conflicts:
        return (
            "entity_bridge endpoints have a hard grounding conflict: "
            + ", ".join(grounding.hard_conflicts)
        )
    return None


def bridge_candidate_write_error(conn, candidate: BridgeCandidateV1) -> str | None:
    """Stronger gate for the automatic candidate path, including live type/basis agreement."""
    from featuregen.overlay.identity import EntityBridgeRef

    ref = EntityBridgeRef(
        entity_id=candidate.entity_id,
        left_ref=candidate.left_ref,
        right_ref=candidate.right_ref,
    )
    error = bridge_catalog_write_error(conn, ref)
    if error is not None:
        return error
    left = _current_identifier_column(conn, candidate.left_ref)
    right = _current_identifier_column(conn, candidate.right_ref)
    assert left is not None and right is not None  # established by bridge_catalog_write_error
    if left.type_family != right.type_family:
        return (
            f"entity_bridge endpoint type families differ ({left.type_family} vs "
            f"{right.type_family}); an explicit normalization realization is required"
        )
    if left.type_family == "other":
        return "entity_bridge endpoint type family is unclassified"
    live_basis = left.type_basis if left.type_basis == right.type_basis else "mixed"
    if candidate.data_type_family != left.type_family or candidate.type_basis != live_basis:
        return (
            "entity_bridge candidate type evidence is stale: "
            f"candidate={candidate.data_type_family}/{candidate.type_basis}, "
            f"current={left.type_family}/{live_basis}"
        )
    claimed_authority = (
        candidate.left_concept_authority,
        candidate.right_concept_authority,
    )
    current_authority = (left.concept_authority, right.concept_authority)
    if claimed_authority != current_authority:
        return (
            "entity_bridge candidate concept authority is stale: "
            f"candidate={claimed_authority}, current={current_authority}"
        )
    return None


def derive_bridge_candidates(conn, *, roles: Iterable[str] = ()) -> tuple[BridgeCandidateV1, ...]:
    """Candidate bridges from declared metadata: identifier concepts in the SAME identifier
    namespace (``Concept.namespace``), in DISTINCT catalog sources, with a COMPATIBLE type family.
    Entity is carried for corroboration/display, never as a gate. Deterministic (canonical
    unordered pair + sorted output + deterministic entity pick). Read-only."""
    return derive_bridge_candidates_with_report(conn, roles=roles).candidates


def derive_bridge_candidate_for_refs(
    conn,
    left_ref: CatalogObjectRef,
    right_ref: CatalogObjectRef,
) -> BridgeCandidateV1 | None:
    """Re-assess one existing logical pair against current endpoint evidence.

    This is deliberately exact rather than a fresh neighbourhood search: a later grain/type/concept
    projection must refresh the candidate that depends on that endpoint even when discovery caps
    would rank a different new candidate into the shortlist.
    """
    left = _current_identifier_column(conn, left_ref)
    right = _current_identifier_column(conn, right_ref)
    if (
        left is None
        or right is None
        or left.catalog_source == right.catalog_source
        or left.namespace != right.namespace
        or left.type_family == "other"
        or left.type_family != right.type_family
    ):
        return None
    return _candidate(left, right)


def derive_bridge_candidates_with_report(
    conn, *, roles: Iterable[str] = ()
) -> BridgeCandidateDerivationV1:
    """Derive candidates with bounded work and visible suppression/truncation accounting."""
    return _derive_from_identifier_columns(_identifier_columns(conn, roles=roles))
