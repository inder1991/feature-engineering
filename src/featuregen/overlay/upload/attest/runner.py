"""P0 shadow-measurement harness — Task 5: the RUNNER that composes Tasks 1-4 over the
gold-labelled columns, plus the stratified gold-worksheet **emit** (draw the blind sample) and
**ingest** (adjudicated labels -> ``attestation_gold_label``). Design:
docs/superpowers/specs/2026-07-22-p0-shadow-measurement-design.md §5; sampling protocol:
docs/superpowers/specs/2026-07-22-p0-gold-set-labelling-protocol.md.

``run_shadow`` is MEASURE-ONLY (design §Goal): it reads existing evidence (:mod:`field_evidence`)
and the physical graph (:mod:`graph_node`, via the schema-aware :func:`logical_ref_of`), and calls
the four Task 1-4 signals — :func:`~featuregen.overlay.upload.attest.grounding.ground_concept`
(pure, no write), :func:`~featuregen.overlay.upload.attest.reclassify.reclassify_concept` (writes
only the audited seam's own telemetry, never an authority row — see that module's docstring), and
:func:`~featuregen.overlay.upload.attest.fusion.fuse` (pure) — then writes ONLY to the
``attestation_*`` WORM tables via :mod:`shadow_store`. It never writes ``field_evidence`` /
``field_decision_event`` / ``graph_node`` or any other authority-tier row.

**Concept-only scope.** Tasks 1-4 (grounding / re-classification / fusion) are ALL defined over the
``concept`` field — there is no equivalent triangulation signal yet for ``sensitivity`` (the second
field the labelling protocol collects). ``run_shadow`` therefore samples and scores only the
``concept`` gold labels; a ``sensitivity`` gold label sits in ``attestation_gold_label`` unused by
this runner until a sensitivity-specific signal exists (a later task, not P0's scope per the design
doc's non-goals). The worksheet emit still asks for BOTH fields (the protocol's two-field grain),
so the human labelling pass need not be re-run when that signal lands.

**No raw data values, ever (task invariant).** A :class:`WorksheetRow` carries the column's name,
its file-declared definition, its attested BIAN/FIBO path, and a DERIVED sample-value SHAPE
(``semantic_type``/``logical_representation`` — the same safe facets
:func:`~featuregen.overlay.upload.attest.reclassify._value_shape` derives for the LLM egress path)
— never the AI's proposed concept and never a raw value. The definition text is additionally run
through :func:`~featuregen.overlay.upload.sanitize.sanitize_definition` — the SAME data-leak
backstop (clause-strip + fail-closed data-marker scan + PII redaction) the LLM enrichment egress
path applies to a glossary description before ANY consumer sees it — so an embedded "representative
values such as ..." clause AND a bare raw value sitting in ordinary prose (e.g. an account number
mentioned outside any such clause) are both handled exactly as the LLM path would handle them: a
recognized clause is excised, PII is redacted, and a definition the sanitizer cannot prove safe
(``suspected_unhandled`` / a redactor that fails closed) is dropped from the row entirely (``None``)
rather than emitted verbatim.
"""
from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from featuregen.contracts import DbConn
from featuregen.intake.llm import DEFAULT_LLM_MODEL, LLMClient
from featuregen.overlay.evidence import EvidenceProducer
from featuregen.overlay.field_evidence import FieldEvidence, read_active_field_evidence
from featuregen.overlay.upload.attest.fusion import fuse
from featuregen.overlay.upload.attest.grounding import (
    _MEASURE_NAME_TOKENS,
    _name_tokens,
    _parser_type_family,
    ground_concept,
)
from featuregen.overlay.upload.attest.reclassify import ColumnContext, reclassify_concept
from featuregen.overlay.upload.attest.shadow_store import (
    ObservationV1,
    ReconcileV1,
    ShadowRunV1,
    reconcile,
    write_gold_label,
    write_observation,
    write_shadow_run,
)
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.sanitize import sanitize_definition

# The only field run_shadow scores (see module docstring: "Concept-only scope").
_CONCEPT_FIELD = "concept"

# The two fields the labelling protocol collects per column (protocol doc §"What a gold label is").
_WORKSHEET_FIELDS: tuple[str, ...] = ("concept", "sensitivity")

# Signal versions stamped on every run manifest — bumped whenever a signal's logic changes, so a
# report can split observations by which code produced them.
_SIGNAL_VERSIONS: Mapping[str, str] = {
    "grounding": "1.0.0", "reclassify": "1.0.0", "fusion": "1.0.0",
}

# sensitivity_floor values that make a column INTRINSICALLY high risk (design doc §3.6: "risk tier =
# high (intrinsic PII/leakage ...)"). "proxy" (a fair-lending signal, not raw PII — e.g. geographic,
# country_code) is deliberately excluded: it is a softer signal than genuine PII/protected/special
# category, and the design's own phrase is "intrinsic PII/leakage", not "any sensitivity tag".
_HIGH_RISK_SENSITIVITY = frozenset({"pii", "protected_attribute", "special_category"})

# Column-name tokens for the worksheet's RISK stratum (protocol doc: "looks-like-PII, money/amount,
# identifier, descriptive, technical/ETL"). Structural-name heuristics only — never the AI concept.
_PII_NAME_TOKENS = frozenset({
    "ssn", "sin", "tin", "dob", "birth", "name", "email", "phone", "mobile", "address", "passport",
    "nationalid",
})
_ETL_NAME_TOKENS = frozenset({"etl", "batch", "dummy", "load", "tech", "sys", "internal"})


@dataclass(frozen=True, slots=True)
class WorksheetRow:
    """One blind labelling row (protocol doc worksheet columns 1-6, plus the team-filled 7-9).

    ``catalog_source``/``logical_ref``/``field_name`` identify what is being labelled;
    ``column_name``/``definition``/``bian_path``/``fibo_path``/``sample_shape`` are the BLIND
    context shown to the labeller — see the module docstring for what is deliberately excluded
    (the AI concept, any raw value). ``gold_value``/``labeller_ids``/``adjudicated_by``/``notes``
    are ``None``/empty as emitted; the labelling team fills them in before the row is handed to
    :func:`ingest_gold_worksheet`."""

    catalog_source: str
    logical_ref: str
    field_name: str
    column_name: str
    definition: str | None = None
    bian_path: str | None = None
    fibo_path: str | None = None
    sample_shape: Mapping[str, str] = field(default_factory=dict)
    # Team-filled (protocol columns 7-9) — None/empty until adjudicated.
    gold_value: str | None = None
    labeller_ids: tuple[str, ...] = ()
    adjudicated_by: str | None = None
    notes: str | None = None


def _latest_active_evidence(
    conn: DbConn, logical_ref: str, field_name: str, *, producer: str | None = None
) -> FieldEvidence | None:
    """The most recent ACTIVE evidence row for ``(logical_ref, field_name)``, optionally scoped to
    one ``producer`` — or ``None`` when nothing matches. ``read_active_field_evidence`` orders by
    ``created_at``/``evidence_id``, so the last element is the most recent."""
    rows = read_active_field_evidence(conn, logical_ref, field_name)
    if producer is not None:
        rows = [r for r in rows if r.producer == producer]
    return rows[-1] if rows else None


def _str_value(ev: FieldEvidence | None) -> str | None:
    if ev is None or not isinstance(ev.proposed_value, str) or not ev.proposed_value:
        return None
    return ev.proposed_value


# ── stratified sampling (worksheet emit) ────────────────────────────────────────────────────────
def _catalog_columns(conn: DbConn, catalog_source: str) -> list[tuple[str, str, str, str | None]]:
    """Every ``kind='column'`` node for ``catalog_source``: ``(object_ref, table_name, column_name,
    domain)``. ``domain`` is the flat display column ``build_graph`` already stamps per column."""
    rows = conn.execute(
        "SELECT object_ref, table_name, column_name, domain FROM graph_node "
        "WHERE catalog_source = %s AND kind = 'column' ORDER BY object_ref",
        (catalog_source,)).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _risk_bucket(column_name: str, type_family: str) -> str:
    """The RISK stratum for one column — structural name-token + parser-type heuristics only,
    NEVER the AI concept (anchoring the sample on the AI's own guess would defeat the measurement).
    """
    tokens = _name_tokens(column_name)
    if tokens & _PII_NAME_TOKENS:
        return "looks_like_pii"
    if tokens & _MEASURE_NAME_TOKENS:
        return "money_amount"
    if type_family == "identifier" or "id" in tokens:
        return "identifier"
    if tokens & _ETL_NAME_TOKENS:
        return "technical_etl"
    return "descriptive"


def _stratum_key(conn: DbConn, logical_ref: str, column_name: str, domain: str | None) -> str:
    """``domain|risk|type-family`` (protocol doc: "cross domain x risk x type-family")."""
    type_family = _parser_type_family(conn, logical_ref) or "unknown"
    risk = _risk_bucket(column_name, type_family)
    return f"{domain or 'undetermined'}|{risk}|{type_family}"


def _select_columns(
    conn: DbConn, catalog_source: str, *, size: int, seed: object
) -> list[tuple[str, str, str]]:
    """Draw up to ``size`` distinct columns, stratified by ``_stratum_key`` and deterministic given
    ``seed`` (mirrors ``planner/contract_eval.stratified_sample``'s ``random.Random(f"{seed}:{key}")``
    convention — each stratum's draw is seeded independently of every other stratum, so adding or
    removing a stratum never perturbs another stratum's chosen order).

    Fewer than ``size`` columns in the catalog -> every column is returned (protocol doc: "120
    columns for the FTR source [of 126] — so nearly a census"). Otherwise: shuffle each stratum
    (seeded), then round-robin one column at a time across strata (in a fixed sorted-key order) until
    ``size`` is reached — spreading the sample across every observed stratum rather than exhausting
    one before touching the next."""
    all_columns = _catalog_columns(conn, catalog_source)
    if len(all_columns) <= size:
        return [(object_ref, table, column) for object_ref, table, column, _domain in all_columns]

    by_stratum: dict[str, list[tuple[str, str, str]]] = {}
    for object_ref, table, column, domain in all_columns:
        logical_ref = logical_ref_of(conn, catalog_source, object_ref)
        key = _stratum_key(conn, logical_ref, column, domain)
        by_stratum.setdefault(key, []).append((object_ref, table, column))
    for key, bucket in by_stratum.items():
        bucket.sort()   # deterministic base order before the seeded shuffle
        random.Random(f"{seed}:{key}").shuffle(bucket)

    ordered_keys = sorted(by_stratum)
    picked: list[tuple[str, str, str]] = []
    round_idx = 0
    while len(picked) < size:
        progressed = False
        for key in ordered_keys:
            bucket = by_stratum[key]
            if round_idx < len(bucket):
                picked.append(bucket[round_idx])
                progressed = True
                if len(picked) >= size:
                    break
        if not progressed:
            break   # every stratum exhausted before reaching size (all_columns <= size already
                    # handled above, so this is unreachable in practice — kept as a safety exit)
        round_idx += 1
    return picked


def _sample_shape(conn: DbConn, logical_ref: str) -> dict[str, str]:
    """The DERIVED, safe sample-value shape for a column — the same
    ``semantic_type``/``logical_representation`` PARSER facets
    :func:`~featuregen.overlay.upload.attest.grounding._parser_type_family` reasons over — never a
    raw value (see the module docstring)."""
    shape: dict[str, str] = {}
    for field_name in ("semantic_type", "logical_representation"):
        value = _str_value(
            _latest_active_evidence(conn, logical_ref, field_name,
                                    producer=EvidenceProducer.PARSER.value))
        if value:
            shape[field_name] = value
    return shape


def emit_gold_worksheet(
    conn: DbConn, catalog_source: str, *, size: int = 120, seed: object
) -> list[WorksheetRow]:
    """Emit the stratified, blind gold-labelling worksheet (protocol doc): up to ``size`` distinct
    columns (all of them, if the catalog has fewer), each contributing one row per labelled field
    (``concept``, ``sensitivity``). Deterministic given ``seed``. See the module docstring for what
    a row does and does not carry."""
    selected = _select_columns(conn, catalog_source, size=size, seed=seed)
    rows: list[WorksheetRow] = []
    for object_ref, _table, column in selected:
        logical_ref = logical_ref_of(conn, catalog_source, object_ref)
        definition = _str_value(_latest_active_evidence(conn, logical_ref, "definition"))
        if definition:
            # I-1: route through the SAME sanitizer the LLM egress path uses (strip + fail-closed
            # data-marker scan + PII redaction) — see module docstring. A blanked field (an
            # unhandled sample-values marker, or a redactor that failed closed) is never emitted
            # verbatim; it is dropped to None rather than leaking the raw text.
            definition = sanitize_definition(definition).clean or None
        bian_path = _str_value(_latest_active_evidence(conn, logical_ref, "bian_path"))
        fibo_path = _str_value(_latest_active_evidence(conn, logical_ref, "fibo_path"))
        shape = _sample_shape(conn, logical_ref)
        for field_name in _WORKSHEET_FIELDS:
            rows.append(WorksheetRow(
                catalog_source=catalog_source, logical_ref=logical_ref, field_name=field_name,
                column_name=column, definition=definition, bian_path=bian_path,
                fibo_path=fibo_path, sample_shape=shape))
    return rows


def ingest_gold_worksheet(conn: DbConn, rows: Sequence[WorksheetRow]) -> int:
    """Write every ADJUDICATED row (``gold_value`` and ``adjudicated_by`` both set) to
    ``attestation_gold_label`` via :func:`~featuregen.overlay.upload.attest.shadow_store.write_gold_label`
    (task 1's idempotent WORM writer). A row the team has not yet labelled is skipped, not an error
    — a partially-filled-in worksheet may be ingested incrementally. Returns the count written."""
    written = 0
    for row in rows:
        if row.gold_value is None or row.adjudicated_by is None:
            continue
        write_gold_label(
            conn, catalog_source=row.catalog_source, logical_ref=row.logical_ref,
            field_name=row.field_name, gold_value=row.gold_value,
            labeller_ids=list(row.labeller_ids), adjudicated_by=row.adjudicated_by,
            notes=row.notes)
        written += 1
    return written


# ── the shadow runner ───────────────────────────────────────────────────────────────────────────
def _risk_tier(conn: DbConn, logical_ref: str) -> str:
    """"high" iff the column's TAXONOMY-derived ``sensitivity_floor``/``leakage_anchor`` evidence
    says it is intrinsically PII/protected/special-category or a leakage anchor (design doc §3.6);
    else "low". No signal at all (concept never classified / no taxonomy derivation yet) -> "low",
    mirroring grounding's own "absent is not a conflict" convention: a missing signal is never
    invented as a risk."""
    sensitivity = _latest_active_evidence(conn, logical_ref, "sensitivity_floor",
                                          producer=EvidenceProducer.TAXONOMY.value)
    leakage = _latest_active_evidence(conn, logical_ref, "leakage_anchor",
                                      producer=EvidenceProducer.TAXONOMY.value)
    if sensitivity is not None and sensitivity.proposed_value in _HIGH_RISK_SENSITIVITY:
        return "high"
    if leakage is not None and leakage.proposed_value is True:
        return "high"
    return "low"


def _sampled_gold_keys(conn: DbConn, catalog_source: str) -> list[tuple[str, str]]:
    """The concept-field gold labels for ``catalog_source`` — see module docstring "Concept-only
    scope" for why ``sensitivity`` gold labels are not sampled here."""
    rows = conn.execute(
        "SELECT logical_ref FROM attestation_gold_label "
        "WHERE catalog_source = %s AND field_name = %s ORDER BY logical_ref",
        (catalog_source, _CONCEPT_FIELD)).fetchall()
    return [(r[0], _CONCEPT_FIELD) for r in rows]


def run_shadow(
    conn: DbConn, catalog_source: str, *, client: LLMClient, shadow_run_id: str, gold_version: str
) -> ReconcileV1:
    """Run the P0 shadow measurement over every ``concept`` gold-labelled column of
    ``catalog_source`` (design §5): for each, read the proposer's existing ``concept`` evidence,
    run :func:`ground_concept` + :func:`reclassify_concept`, fuse the two into one confidence,
    assign an intrinsic risk tier, and append one ``attestation_shadow_observation`` — then write
    the run manifest (``sampled_keys`` = the EXACT gold set scored) and return the reconcile result.

    MEASURE-ONLY: writes nothing but the ``attestation_*`` WORM tables — see the module docstring.
    """
    now = datetime.now(UTC)
    sampled_keys = _sampled_gold_keys(conn, catalog_source)
    write_shadow_run(conn, ShadowRunV1(
        shadow_run_id=shadow_run_id, catalog_source=catalog_source, gold_version_hash=gold_version,
        model_ids={"proposer": DEFAULT_LLM_MODEL, "reclassifier": DEFAULT_LLM_MODEL},
        signal_versions=dict(_SIGNAL_VERSIONS), started_at=now, sampled_keys=sampled_keys))

    for logical_ref, field_name in sampled_keys:
        proposer_ev = _latest_active_evidence(conn, logical_ref, field_name,
                                              producer=EvidenceProducer.LLM.value)
        proposer_value = _str_value(proposer_ev)
        proposer_producer = proposer_ev.producer if proposer_ev is not None else None

        grounding = ground_concept(conn, logical_ref, proposer_value or "")

        _source, _schema, _table, column = parse_ref(logical_ref)
        definition = _str_value(_latest_active_evidence(conn, logical_ref, "definition"))
        ctx = ColumnContext(name=column or "", definition=definition)
        reclassify_value = reclassify_concept(conn, client, logical_ref, column_ctx=ctx).value

        fusion = fuse(proposer_value=proposer_value, reclassify_value=reclassify_value,
                      grounding=grounding)
        # CHECK attestation_obs_reclassify_agrees_scope: reclassify_agrees IS NULL iff
        # reclassify_value IS NULL — fuse()'s agree flag is always a bool, so translate here.
        reclassify_agrees = (None if reclassify_value is None
                            else bool(fusion.agreement["proposer_reclassify_agree"]))

        write_observation(conn, ObservationV1(
            shadow_run_id=shadow_run_id, logical_ref=logical_ref, field_name=field_name,
            proposer_value=proposer_value, proposer_producer=proposer_producer,
            reclassify_value=reclassify_value, reclassify_agrees=reclassify_agrees,
            grounding_checks=dict(grounding.checks), grounding_coverage=grounding.coverage,
            grounding_conflict=grounding.conflict, confidence=fusion.confidence,
            risk_tier=_risk_tier(conn, logical_ref), created_at=now))

    return reconcile(conn, shadow_run_id)
