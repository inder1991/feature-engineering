"""Crosswalk CANDIDATE discovery (Release C Task 10).

Two sources, one output shape, and no way to run anything.

**Source (a) — a dataset profiled as crosswalk/bridge.** §4-correction-10 is the whole rule: a
table role is a DISCOVERY HINT, never an executable crosswalk. So a `crosswalk`/`bridge` role ALONE
proposes nothing. It proposes a CANDIDATE only when the table also carries the structure a mapping
table must have — at least two identifier-group concepts drawing on DIFFERENT identifier NAMESPACES
(the three-axis model: `Concept.namespace` is the only join-candidacy gate, so two columns in the
same namespace are a duplicate key, not a mapping). Both halves are required, and
``test_crosswalk_discovery`` fails if the role alone ever produces a definition.

  *A consequence worth stating, found while building this.* The plan's headline example — "customer
  number maps to CIF through this table" — does NOT discover as a crosswalk under the SHIPPED
  concept registry, because both sides are the `customer_id` concept in the ONE `cif` namespace
  (``concepts.py``), which makes that pair a DIRECT-equality bridge and the existing bridge family's
  job. That is the namespace rule working, not failing: a crosswalk exists exactly where the
  identifiers are not directly equal. The shapes that DO discover are genuine two-scheme pairs
  (internal account -> correspondent account reference, core serial -> SWIFT UETR, and so on). If
  the bank's CIB customer number and FTR CIF are in truth two different schemes, the fix is a
  registry change — two concepts with two namespaces — not a weaker discovery rule.

**Source (b) — an LLM structured suggestion.** THIS TASK ADDS NO LLM CALL. What it adds is the typed
INGESTION seam: a suggestion that arrives through the EXISTING structured-result path
(``structured_results``, migration 1046 — the same generic subject/current-pointer store the
semantic adjudicator writes through) is validated here against the concept registry, the identifier
namespaces and the caller's read scope, then becomes a candidate on exactly the same footing as (a),
carrying LLM-recommendation evidence rather than structural evidence. Issuing the call belongs to
the joint enrichment surface, where the prompt/schema/egress discipline (D10) already lives; adding
a second egress path here would duplicate it. Deliberate, recorded, deferred.

**Bounded BEFORE pairwise enumeration.** Mapping tables, identifier columns per mapping table and
endpoint columns per namespace are all capped before any pair is formed, and every cap that bites is
COUNTED in the report. An unbounded cross-product over a bank's column space is the failure mode the
bridge candidate deriver already bounds against (``bridge_candidates.py:42-44``).

**Nothing here is executable, structurally.** No import from ``materialize`` except the D1 hasher, no
plan production, no execution revision, no safety verdict. Every candidate lands as a PROPOSED
definition revision (review status ``unreviewed``, which is a usable discovery — never a failure
state) and the only thing a later human confirmation changes is accountability.

**Transformed / semantic-only suggestions** are returned by discovery — they are discoverable — but
they produce NO crosswalk definition, because a crosswalk definition asserts equality through a
mapping table and this repo has no transformation contract (§4-correction-9). They are therefore
structurally non-executable in the strongest sense: there is nothing to execute. Persisting them
needs a relationship-kind-general store, which Task 13 owns.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from featuregen.contracts import DbConn
from featuregen.overlay.upload.bridge_assessment import (
    BridgeContractError,
    EvidenceKind,
    EvidenceRefV1,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    KeyMemberRole,
    LinkReviewStatus,
    TypeBasis,
)
from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY
from featuregen.overlay.upload.concepts import concept as registry_concept
from featuregen.overlay.upload.crosswalk import (
    CrosswalkContractError,
    CrosswalkDefinitionRevisionV1,
    LogicalMappingPairV1,
)
from featuregen.overlay.upload.crosswalk_store import publish_crosswalk_definition
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_vocab import DataRole, data_role_from_table_role
from featuregen.overlay.upload.read_scope import allowed_classes, anchor_visibility_predicate
from featuregen.overlay.upload.semantic_context import RelationshipKind
from featuregen.overlay.upload.structured_results import (
    StructuredResultCorruption,
    load_current_structured_result,
    load_structured_result,
)

#: The result type a crosswalk suggestion arrives under on the EXISTING structured-result path.
#: Its subject is the MAPPING dataset, because that is what the suggestion is about.
CROSSWALK_SUGGESTION_RESULT_TYPE = "crosswalk_suggestion"
SUBJECT_DATASET = "dataset"

# ── bounds, applied BEFORE any pairwise enumeration ─────────────────────────────────────────────

MAX_MAPPING_DATASETS = 50
MAX_IDENTIFIER_COLUMNS_PER_MAPPING = 24
MAX_ENDPOINT_COLUMNS_PER_NAMESPACE = 20
MAX_CANDIDATES_PER_MAPPING = 40
MAX_SUGGESTIONS_PER_MAPPING = 20
#: The identifier pool is read ONCE per pass, not once per mapping table per namespace — the
#: "hoist the load out of the loop" lesson the suggestions path already learned (157 scans -> 1).
MAX_IDENTIFIER_COLUMNS_PER_PASS = 2000

# ── why a shape did NOT become a candidate (closed, counted, never a silent drop) ───────────────

#: The role said "crosswalk" but the table carries fewer than two identifier concepts.
REASON_TOO_FEW_IDENTIFIERS = "too_few_identifier_columns"
#: Two-or-more identifier columns, but all in ONE namespace — a duplicate key, not a mapping.
REASON_SINGLE_NAMESPACE = "single_identifier_namespace"
#: No endpoint anywhere draws on one of the mapping table's namespaces, so no crosswalk to propose.
REASON_NO_ENDPOINT_FOR_NAMESPACE = "no_endpoint_for_namespace"
#: The pair could not be expressed as a definition (a non-public mapping dataset, a malformed leg).
REASON_UNREPRESENTABLE = "unrepresentable_definition"
#: A suggestion named a ref this caller cannot see, or that does not exist.
REASON_SUGGESTION_UNREADABLE_REF = "suggestion_unreadable_ref"
#: A suggestion named a column whose concept is absent from the registry or is not an identifier.
REASON_SUGGESTION_UNGROUNDED = "suggestion_not_grounded_in_identifiers"
#: A suggestion's two sides draw on the SAME namespace, so it is not a crosswalk.
REASON_SUGGESTION_SAME_NAMESPACE = "suggestion_single_namespace"
#: A suggestion's endpoint side and its mapping side disagree on namespace.
REASON_SUGGESTION_NAMESPACE_MISMATCH = "suggestion_namespace_mismatch"
#: A suggestion was malformed (missing keys, wrong types, unknown relationship kind).
REASON_SUGGESTION_MALFORMED = "suggestion_malformed"

DISCOVERY_REASON_CODES: frozenset[str] = frozenset({
    REASON_TOO_FEW_IDENTIFIERS, REASON_SINGLE_NAMESPACE, REASON_NO_ENDPOINT_FOR_NAMESPACE,
    REASON_UNREPRESENTABLE, REASON_SUGGESTION_UNREADABLE_REF, REASON_SUGGESTION_UNGROUNDED,
    REASON_SUGGESTION_SAME_NAMESPACE, REASON_SUGGESTION_NAMESPACE_MISMATCH,
    REASON_SUGGESTION_MALFORMED,
})

#: Which cap bit, when one did. Reported, never silent.
TRUNCATION_MAPPING_DATASETS = "mapping_dataset_limit"
TRUNCATION_IDENTIFIER_COLUMNS = "identifier_column_limit"
TRUNCATION_ENDPOINT_COLUMNS = "endpoint_column_limit"
TRUNCATION_CANDIDATES = "candidate_limit"
TRUNCATION_SUGGESTIONS = "suggestion_limit"


@dataclass(frozen=True, slots=True)
class _IdentifierColumn:
    """One visible identifier column, reduced to what candidacy needs."""

    logical_ref: str
    table_ref: str
    catalog_source: str
    column_name: str
    concept: str
    namespace: str
    entity: str | None


@dataclass(frozen=True, slots=True)
class CrosswalkCandidateV1:
    """One proposed relationship discovered through a mapping dataset.

    ``definition`` is present for :attr:`RelationshipKind.CROSSWALK` only. A transformed or
    semantic-only suggestion is DISCOVERABLE (it is returned here) and structurally non-executable
    (there is no definition to execute, and no transformation contract exists to build one)."""

    relationship_kind: RelationshipKind
    mapping_dataset_ref: str
    source_ref: str
    target_ref: str
    source_namespace: str
    target_namespace: str
    origin: str                     # dataset_role | llm_suggestion
    evidence_refs: tuple[EvidenceRefV1, ...]
    definition: CrosswalkDefinitionRevisionV1 | None = None

    @property
    def executable(self) -> bool:
        """Always False. There is no execution path on this tree; the property exists so a reader
        asking the question gets the honest answer rather than inferring one from a missing key."""
        return False


@dataclass(frozen=True, slots=True)
class CrosswalkDiscoveryReportV1:
    """What discovery found, what it declined to propose, and what a cap cut off."""

    candidates: tuple[CrosswalkCandidateV1, ...]
    mapping_datasets_considered: int
    reason_counts: Mapping[str, int] = field(default_factory=dict)
    truncation_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return any(self.truncation_counts.values())

    @property
    def definitions(self) -> tuple[CrosswalkDefinitionRevisionV1, ...]:
        return tuple(c.definition for c in self.candidates if c.definition is not None)


# ── reads ───────────────────────────────────────────────────────────────────────────────────────

#: The two raw ``table_role`` spellings the ONE adapter maps onto :attr:`DataRole.CROSSWALK` — the
#: canonical ``bridge`` and the input alias ``crosswalk`` (``table_vocab._ROLE_ALIASES``). They are
#: named here so the DISCRIMINATING filter can live in the SQL; the adapter stays the post-read
#: authority, so a vocabulary change is still caught rather than silently widening this list.
_CROSSWALK_TABLE_ROLES: tuple[str, ...] = ("bridge", "crosswalk")

#: The mapping-dataset population, as a WHERE clause. Everything that DISCRIMINATES is in it, which
#: is what makes ``LIMIT`` a cap on the right set (see :func:`_mapping_datasets`).
_MAPPING_DATASET_WHERE = (
    "gn.kind = 'table' "
    "AND (lower(btrim(gn.table_role)) = ANY(%s) OR gn.data_role = %s) "
    f"AND {anchor_visibility_predicate('gn')}")

#: The identifier-column population, likewise.
_IDENTIFIER_COLUMN_WHERE = (
    "kind = 'column' AND concept = ANY(%s) AND COALESCE(visible_requires, '{}') <@ %s")


def _identifier_namespace_concepts() -> list[str]:
    """The registry's identifier-group concepts that declare a namespace — the pool's SQL filter.

    A bounded list (~50 names today) built ONCE per pass, deliberately FROM the live registry
    rather than hand-typed: a new identifier concept joins discovery by being registered, and one
    that loses its namespace leaves, with no second list to forget."""
    return sorted(
        name for name, registered in CONCEPT_REGISTRY.items()
        if registered.group == "identifier" and registered.namespace)


def _true_count(conn: DbConn, *, table: str, where: str, params: Sequence[object]) -> int:
    """How many rows the capped read WOULD have returned. Issued only when a cap actually bit —
    honesty about a truncation is worth one extra aggregate; a second scan on every clean pass is
    not."""
    row = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", list(params)).fetchone()
    return int(row[0]) if row else 0


def _mapping_datasets(conn: DbConn, *, roles: Iterable[str], source: str | None,
                      truncation: Counter[str]) -> list[tuple[str, str]]:
    """The visible tables whose role reads crosswalk/bridge — bounded, ordered, truncation counted.

    **The filter is in the SQL, so the LIMIT caps the crosswalk-family population** and not "every
    table carrying any role at all". Capping the wider set and filtering afterwards in Python is a
    cap on the WRONG population: sixty ordinary dimension tables sorting first is a small catalog,
    not an exotic one, and under that shape the one mapping table never survives the read — a pass
    that reports zero candidates, truncates nothing, and is wrong.

    The role is still re-derived through the ONE adapter (``data_role_from_table_role``) after the
    read, as the authority check: the SQL names the two spellings that adapter maps, and the adapter
    remains the thing that decides. A `data_role` disjunct is included because that projection is
    the only surviving signal for a row whose raw ``table_role`` was never written."""
    allowed = allowed_classes(roles)
    where = _MAPPING_DATASET_WHERE
    params: list[object] = [
        list(_CROSSWALK_TABLE_ROLES), DataRole.CROSSWALK.value, allowed, allowed]
    if source is not None:
        where += " AND gn.catalog_source = %s"
        params.append(source)
    rows = conn.execute(
        "SELECT gn.catalog_source, gn.object_ref, gn.table_role, gn.data_role, "
        "       gn.event_or_snapshot FROM graph_node gn "
        f"WHERE {where} ORDER BY gn.catalog_source, gn.object_ref LIMIT %s",
        [*params, MAX_MAPPING_DATASETS + 1]).fetchall()
    out: list[tuple[str, str]] = []
    for catalog_source, object_ref, table_role, data_role, event_or_snapshot in rows:
        derived = data_role_from_table_role(table_role, event_or_snapshot=event_or_snapshot)
        if derived is not DataRole.CROSSWALK and data_role != DataRole.CROSSWALK.value:
            continue
        schema, table = object_ref.split(".", 1)
        out.append((catalog_source, normalize_ref(catalog_source, schema, table, None)))
    if len(rows) > MAX_MAPPING_DATASETS:
        # The TRUE omitted count, not the "at least one more row existed" that a LIMIT n+1 read
        # can tell you: a truncation report whose number is always 1 is a flag wearing a count's
        # name, and the operator reading it cannot tell 1 missing mapping table from 400.
        total = _true_count(conn, table="graph_node gn", where=where, params=params)
        truncation[TRUNCATION_MAPPING_DATASETS] += total - MAX_MAPPING_DATASETS
        out = out[:MAX_MAPPING_DATASETS]
    return out


def _identifier_columns(conn: DbConn, *, roles: Iterable[str],
                        truncation: Counter[str]) -> list[_IdentifierColumn]:
    """EVERY visible identifier column, read ONCE per pass and resolved through the registry.

    A column counts only when its concept is registered, is in the ``identifier`` group and declares
    a namespace — the same three conditions ``bridge_candidates._identifier_columns`` applies, for
    the same reason: an unregistered concept grounds nothing.

    Those three conditions are answered by the REGISTRY, so the concept names they admit are pushed
    into the query as a bounded ``= ANY`` list. The cap then bounds the identifier pool rather than
    "every column carrying any concept" — a limit spent on two thousand amount columns leaves an
    empty identifier pool and a silently candidate-free pass. The registry re-check below stays as
    the post-read authority."""
    concepts = _identifier_namespace_concepts()
    where = _IDENTIFIER_COLUMN_WHERE
    params: list[object] = [concepts, allowed_classes(roles)]
    rows = conn.execute(
        "SELECT catalog_source, object_ref, column_name, concept, entity FROM graph_node "
        f"WHERE {where} ORDER BY catalog_source, object_ref LIMIT %s",
        [*params, MAX_IDENTIFIER_COLUMNS_PER_PASS + 1]).fetchall()
    if len(rows) > MAX_IDENTIFIER_COLUMNS_PER_PASS:
        total = _true_count(conn, table="graph_node", where=where, params=params)
        truncation[TRUNCATION_IDENTIFIER_COLUMNS] += total - MAX_IDENTIFIER_COLUMNS_PER_PASS
        rows = rows[:MAX_IDENTIFIER_COLUMNS_PER_PASS]
    out: list[_IdentifierColumn] = []
    for catalog_source, object_ref, column_name, concept_name, entity in rows:
        registered = registry_concept(concept_name)
        if registered is None or registered.group != "identifier" or not registered.namespace:
            continue
        schema, table, column = object_ref.split(".", 2)
        out.append(_IdentifierColumn(
            logical_ref=normalize_ref(catalog_source, schema, table, column),
            table_ref=normalize_ref(catalog_source, schema, table, None),
            catalog_source=catalog_source, column_name=column_name, concept=concept_name,
            namespace=registered.namespace, entity=entity or registered.entity_link))
    return out


def _endpoint(column: _IdentifierColumn) -> IdentifierEndpointV1:
    """A LOGICAL, UNBOUND single-member endpoint. Physical identity belongs to execution, and
    tuple-key/type conclusions belong to the bridge ASSESSMENT — discovery asserts neither."""
    return IdentifierEndpointV1(
        logical_table_ref=column.table_ref,
        members=(IdentifierColumnMemberV1(
            column.logical_ref, "unknown", TypeBasis.UNKNOWN, KeyMemberRole.UNKNOWN),),
        entity_id=column.entity, concept=column.concept)


def _structural_evidence(mapping_dataset_ref: str, source: _IdentifierColumn,
                         target: _IdentifierColumn) -> tuple[EvidenceRefV1, ...]:
    """Structural metadata evidence — the role AND the two namespaces that made this a candidate.

    Both halves are recorded because both are required (§4-correction-10): the evidence has to be
    able to answer "why is this here" with more than "a table was labelled bridge"."""
    return (
        EvidenceRefV1(evidence_id=f"crosswalk_role:{mapping_dataset_ref}",
                      kind=EvidenceKind.STRUCTURAL_METADATA, producer="taxonomy"),
        EvidenceRefV1(evidence_id=f"crosswalk_namespace:{source.namespace}:{source.logical_ref}",
                      kind=EvidenceKind.STRUCTURAL_METADATA, producer="taxonomy"),
        EvidenceRefV1(evidence_id=f"crosswalk_namespace:{target.namespace}:{target.logical_ref}",
                      kind=EvidenceKind.STRUCTURAL_METADATA, producer="taxonomy"),
    )


def _build_candidate(
    *, mapping_dataset_ref: str, source: _IdentifierColumn, target: _IdentifierColumn,
    mapping_source: _IdentifierColumn, mapping_target: _IdentifierColumn,
    evidence: tuple[EvidenceRefV1, ...], origin: str, reasons: Counter[str],
) -> CrosswalkCandidateV1 | None:
    """Assemble ONE candidate, turning an unrepresentable shape into a counted reason.

    A refusal here is information (the catalog can mean it, the platform cannot yet express it), so
    it is counted rather than raised — one non-public mapping dataset must not end a whole pass."""
    try:
        definition = CrosswalkDefinitionRevisionV1(
            source_endpoint=_endpoint(source),
            mapping_dataset_ref=mapping_dataset_ref,
            source_to_mapping_pairs=(
                LogicalMappingPairV1(source.logical_ref, mapping_source.logical_ref),),
            mapping_to_target_pairs=(
                LogicalMappingPairV1(target.logical_ref, mapping_target.logical_ref),),
            target_endpoint=_endpoint(target),
            evidence_refs=evidence,
        )
    except (CrosswalkContractError, BridgeContractError):
        # BridgeContractError too: the bridge endpoint grammar is flat-public, so a
        # schema-preserving catalog reaches the same "representable as meaning, not yet as an
        # execution address" wall the mapping-dataset refusal names.
        reasons[REASON_UNREPRESENTABLE] += 1
        return None
    return CrosswalkCandidateV1(
        relationship_kind=RelationshipKind.CROSSWALK,
        mapping_dataset_ref=mapping_dataset_ref,
        source_ref=definition.source_ref, target_ref=definition.target_ref,
        source_namespace=source.namespace, target_namespace=target.namespace,
        origin=origin, evidence_refs=definition.evidence_refs, definition=definition)


# ── source (a): a dataset profiled as crosswalk/bridge ──────────────────────────────────────────

def _candidates_from_role(
    *, mapping_dataset_ref: str, pool: Sequence[_IdentifierColumn],
    reasons: Counter[str], truncation: Counter[str],
) -> list[CrosswalkCandidateV1]:
    mapping_columns = [column for column in pool if column.table_ref == mapping_dataset_ref]
    if len(mapping_columns) > MAX_IDENTIFIER_COLUMNS_PER_MAPPING:
        truncation[TRUNCATION_IDENTIFIER_COLUMNS] += (
            len(mapping_columns) - MAX_IDENTIFIER_COLUMNS_PER_MAPPING)
        mapping_columns = mapping_columns[:MAX_IDENTIFIER_COLUMNS_PER_MAPPING]
    if len(mapping_columns) < 2:
        # THE RULE (§4-correction-10): the role alone proposes NOTHING.
        reasons[REASON_TOO_FEW_IDENTIFIERS] += 1
        return []
    by_namespace: dict[str, _IdentifierColumn] = {}
    for column in mapping_columns:
        by_namespace.setdefault(column.namespace, column)
    if len(by_namespace) < 2:
        reasons[REASON_SINGLE_NAMESPACE] += 1
        return []

    # BOUND THE ENDPOINT POOL BEFORE ENUMERATING PAIRS. Each namespace gets at most
    # MAX_ENDPOINT_COLUMNS_PER_NAMESPACE endpoints, so the pair count is capped by construction
    # rather than by a filter applied after a cross-product has already been built.
    endpoints: dict[str, list[_IdentifierColumn]] = {}
    for namespace in sorted(by_namespace):
        candidates_for_ns = [
            column for column in pool
            if column.namespace == namespace and column.table_ref != mapping_dataset_ref
        ]
        if not candidates_for_ns:
            reasons[REASON_NO_ENDPOINT_FOR_NAMESPACE] += 1
            continue
        if len(candidates_for_ns) > MAX_ENDPOINT_COLUMNS_PER_NAMESPACE:
            truncation[TRUNCATION_ENDPOINT_COLUMNS] += (
                len(candidates_for_ns) - MAX_ENDPOINT_COLUMNS_PER_NAMESPACE)
            candidates_for_ns = candidates_for_ns[:MAX_ENDPOINT_COLUMNS_PER_NAMESPACE]
        endpoints[namespace] = candidates_for_ns

    out: list[CrosswalkCandidateV1] = []
    namespaces = sorted(endpoints)
    truncated_here = 0
    for i, left_ns in enumerate(namespaces):
        for right_ns in namespaces[i + 1:]:
            for source in endpoints[left_ns]:
                for target in endpoints[right_ns]:
                    if source.table_ref == target.table_ref:
                        continue
                    if len(out) >= MAX_CANDIDATES_PER_MAPPING:
                        truncated_here += 1
                        continue
                    candidate = _build_candidate(
                        mapping_dataset_ref=mapping_dataset_ref, source=source, target=target,
                        mapping_source=by_namespace[left_ns], mapping_target=by_namespace[right_ns],
                        evidence=_structural_evidence(mapping_dataset_ref, source, target),
                        origin="dataset_role", reasons=reasons)
                    if candidate is not None:
                        out.append(candidate)
    if truncated_here:
        truncation[TRUNCATION_CANDIDATES] += truncated_here
    return out


# ── source (b): an LLM structured suggestion, via the EXISTING structured-result path ───────────

_SUGGESTION_KEYS = ("source_column_ref", "mapping_source_column_ref",
                    "mapping_target_column_ref", "target_column_ref")


def _suggestion_kind(raw: object) -> RelationshipKind | None:
    if raw is None:
        return RelationshipKind.CROSSWALK
    if not isinstance(raw, str):
        return None
    try:
        return RelationshipKind(raw.strip().lower())
    except ValueError:
        return None


def _candidates_from_suggestions(
    conn: DbConn, *, mapping_dataset_ref: str, pool: Sequence[_IdentifierColumn],
    reasons: Counter[str], truncation: Counter[str],
) -> list[CrosswalkCandidateV1]:
    """Ingest crosswalk suggestions already recorded for this mapping dataset. NO LLM CALL.

    The payload is validated against the same registry + namespace rules source (a) obeys, so an
    LLM cannot propose a relationship the deterministic path would have refused. Every rejection is
    a counted reason — a suggestion that quietly vanished would be worse than one that was wrong."""
    pointer = load_current_structured_result(
        conn, subject_kind=SUBJECT_DATASET, subject_ref=mapping_dataset_ref,
        result_type=CROSSWALK_SUGGESTION_RESULT_TYPE)
    if pointer is None or pointer.lifecycle != "current":
        return []
    try:
        result = load_structured_result(conn, pointer.structured_result_id)
    except StructuredResultCorruption:
        reasons[REASON_SUGGESTION_MALFORMED] += 1
        return []
    if result is None or not isinstance(result.output, Mapping):
        reasons[REASON_SUGGESTION_MALFORMED] += 1
        return []
    raw_suggestions = result.output.get("crosswalks")
    if not isinstance(raw_suggestions, Sequence) or isinstance(raw_suggestions, (str, bytes)):
        reasons[REASON_SUGGESTION_MALFORMED] += 1
        return []
    if len(raw_suggestions) > MAX_SUGGESTIONS_PER_MAPPING:
        truncation[TRUNCATION_SUGGESTIONS] += len(raw_suggestions) - MAX_SUGGESTIONS_PER_MAPPING
        raw_suggestions = raw_suggestions[:MAX_SUGGESTIONS_PER_MAPPING]

    named_refs: set[str] = set()
    parsed: list[tuple[dict[str, str], RelationshipKind]] = []
    for raw in raw_suggestions:
        if not isinstance(raw, Mapping):
            reasons[REASON_SUGGESTION_MALFORMED] += 1
            continue
        kind = _suggestion_kind(raw.get("relationship_kind"))
        if kind is None or any(not isinstance(raw.get(key), str) or not raw[key].strip()
                               for key in _SUGGESTION_KEYS):
            reasons[REASON_SUGGESTION_MALFORMED] += 1
            continue
        refs = {key: raw[key].strip().lower() for key in _SUGGESTION_KEYS}
        named_refs.update(refs.values())
        parsed.append((refs, kind))
    if not parsed:
        return []

    visible = {column.logical_ref: column for column in pool
               if column.logical_ref in named_refs}

    evidence = (EvidenceRefV1(
        evidence_id=pointer.structured_result_id, kind=EvidenceKind.LLM_RECOMMENDATION,
        producer="llm"),)
    out: list[CrosswalkCandidateV1] = []
    for refs, kind in parsed:
        columns = {key: visible.get(ref) for key, ref in refs.items()}
        if any(column is None for column in columns.values()):
            # Unreadable OR unregistered-as-identifier are BOTH answered here, so a suggestion that
            # named a restricted column is never distinguishable from one that named a nonexistent
            # one — the read-scope answer must not become an existence oracle.
            reasons[REASON_SUGGESTION_UNREADABLE_REF] += 1
            continue
        source = columns["source_column_ref"]
        target = columns["target_column_ref"]
        mapping_source = columns["mapping_source_column_ref"]
        mapping_target = columns["mapping_target_column_ref"]
        if any(column.table_ref != mapping_dataset_ref
               for column in (mapping_source, mapping_target)):
            reasons[REASON_SUGGESTION_UNGROUNDED] += 1
            continue
        if source.namespace == target.namespace:
            reasons[REASON_SUGGESTION_SAME_NAMESPACE] += 1
            continue
        if (source.namespace != mapping_source.namespace
                or target.namespace != mapping_target.namespace):
            reasons[REASON_SUGGESTION_NAMESPACE_MISMATCH] += 1
            continue
        if kind is not RelationshipKind.CROSSWALK:
            # DISCOVERABLE, and structurally non-executable: no definition is built, because a
            # crosswalk definition asserts equality through a mapping table and this repo has no
            # transformation contract (§4-correction-9).
            out.append(CrosswalkCandidateV1(
                relationship_kind=kind, mapping_dataset_ref=mapping_dataset_ref,
                source_ref=source.logical_ref, target_ref=target.logical_ref,
                source_namespace=source.namespace, target_namespace=target.namespace,
                origin="llm_suggestion", evidence_refs=evidence, definition=None))
            continue
        candidate = _build_candidate(
            mapping_dataset_ref=mapping_dataset_ref, source=source, target=target,
            mapping_source=mapping_source, mapping_target=mapping_target,
            evidence=evidence, origin="llm_suggestion", reasons=reasons)
        if candidate is not None:
            out.append(candidate)
    return out


# ── the pass ────────────────────────────────────────────────────────────────────────────────────

def discover_crosswalk_candidates(
    conn: DbConn, *, roles: Iterable[str] = (), source: str | None = None,
) -> CrosswalkDiscoveryReportV1:
    """One bounded discovery pass over the visible catalog. Writes nothing.

    Persisting is a separate, explicit act (:func:`persist_crosswalk_candidates`) so a read-only
    review of what WOULD be proposed never mutates the catalog."""
    reasons: Counter[str] = Counter()
    truncation: Counter[str] = Counter()
    mapping_datasets = _mapping_datasets(
        conn, roles=roles, source=source, truncation=truncation)
    pool = _identifier_columns(conn, roles=roles, truncation=truncation)
    candidates: list[CrosswalkCandidateV1] = []
    seen: set[tuple[str, str]] = set()
    for _catalog_source, mapping_dataset_ref in mapping_datasets:
        found = [
            *_candidates_from_role(
                mapping_dataset_ref=mapping_dataset_ref, pool=pool,
                reasons=reasons, truncation=truncation),
            *_candidates_from_suggestions(
                conn, mapping_dataset_ref=mapping_dataset_ref, pool=pool,
                reasons=reasons, truncation=truncation),
        ]
        for candidate in found:
            key = (candidate.relationship_kind.value,
                   candidate.definition.revision_id if candidate.definition is not None
                   else f"{candidate.source_ref}|{candidate.target_ref}")
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    candidates.sort(key=lambda c: (c.mapping_dataset_ref, c.source_ref, c.target_ref,
                                   c.relationship_kind.value))
    return CrosswalkDiscoveryReportV1(
        candidates=tuple(candidates),
        mapping_datasets_considered=len(mapping_datasets),
        reason_counts=dict(reasons), truncation_counts=dict(truncation))


def _merge_evidence(
    definitions: Sequence[CrosswalkDefinitionRevisionV1],
) -> CrosswalkDefinitionRevisionV1:
    """One definition carrying the DEDUPED evidence of every origin that proposed it.

    The merge is over ``identity_payload``, not object identity, so the same evidence arriving from
    two readers collapses to one ref. Order is not fixed here on purpose: the revision constructor
    canonically sorts evidence, which is what makes the merged revision re-derive byte-identically
    on the next pass."""
    base = definitions[0]
    merged: dict[tuple, EvidenceRefV1] = {}
    for definition in definitions:
        for ref in definition.evidence_refs:
            merged.setdefault(tuple(sorted(ref.identity_payload().items())), ref)
    evidence = tuple(merged.values())
    if len(evidence) == len(base.evidence_refs):
        return base
    return replace(base, evidence_refs=evidence)


def persist_crosswalk_candidates(
    conn: DbConn, report: CrosswalkDiscoveryReportV1, *, actor: str,
    roles: Iterable[str] = (),
) -> tuple[str, ...]:
    """Publish every discovered crosswalk as a PROPOSED definition revision; return their ids.

    ``unreviewed`` is the honest state and a USABLE one — an unreviewed crosswalk is a discovery
    somebody can act on, never a failure. Nothing published here is executable: there is no
    execution revision, no safety verdict and no plan, and confirming one later changes only who
    is accountable for it.

    **ONE PASS, ONE REVISION PER CROSSWALK.** Candidates are grouped by ``definition_id`` BEFORE
    anything is published and their evidence is merged into a single revision. Publishing them
    separately is publishing two revisions of the same crosswalk from one pass: the pointer walked
    twice per pass (2, 4, 6...), and — because the revision that landed LAST decided what the
    surface displayed — the authority chip flipped taxonomy/llm every run with nothing in the
    catalog having changed. It also lost half the story: a crosswalk both the structural rule and
    an LLM found is better evidenced than either alone, and the merged revision says so.

    Idempotent: re-running a pass over an unchanged catalog re-derives the same merged revision id
    and leaves the pointers exactly where they are."""
    from featuregen.overlay.upload.crosswalk_store import current_crosswalk_definition

    grouped: dict[str, list[CrosswalkDefinitionRevisionV1]] = {}
    for candidate in report.candidates:
        if candidate.definition is None:
            continue        # transformed / semantic-only: discoverable, nothing to define
        grouped.setdefault(candidate.definition.definition_id, []).append(candidate.definition)

    published: list[str] = []
    for definition_id in sorted(grouped):
        definition = _merge_evidence(grouped[definition_id])
        current = current_crosswalk_definition(conn, definition_id)
        if current is not None and current.revision.revision_id == definition.revision_id:
            published.append(definition.revision_id)
            continue
        revision_id, _version = publish_crosswalk_definition(
            conn, definition,
            expected_pointer_version=0 if current is None else current.current.pointer_version,
            actor=actor, roles=roles,
            review_status=(current.review_status if current is not None
                           else LinkReviewStatus.UNREVIEWED))
        published.append(revision_id)
    return tuple(published)
