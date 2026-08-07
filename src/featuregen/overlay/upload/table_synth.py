"""Pass B — per-table input assembler (spec §15.2).

`assemble_table_items` consumes the Task-3 metadata views (`column_view.build_table_views` — one
`TableMetadataView` per table, each column a `ColumnMetadataView` with the sidecar already
bound-and-fenced) and emits one `BatchItem` per table whose metadata carries each column's
egress-safe descriptor plus the table-level `table_definition` when the view has one. The
descriptor keeps `operational_type` and `declared_type` as TWO fields — the declared type is a
HINT from the glossary, never a confirmation of the physical type. Pass B later proposes
grain/availability as human-gated typed-fact proposals and table_role/primary_entity as advisory
field evidence; the assembler does no propose logic.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload import enrich_config, table_vocab
from featuregen.overlay.upload.attest.dataset_profile_critic import CRITIC_BUDGET_EXHAUSTED
from featuregen.overlay.upload.catalog_profiles import CATALOG_NARRATIVE_KEYS
from featuregen.overlay.upload.enrich_batch import BatchItem, CallLedger, run_batched
from featuregen.overlay.upload.enrich_llm import _MAX_COLUMN_PROFILES, ENRICHMENT_RUN_ID
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_vocab import AuthorityRole as _AuthorityRole
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel as _TemporalStorageModel
from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.runtime.observability import counters

if TYPE_CHECKING:
    from datetime import datetime

    from featuregen.overlay.upload.column_view import ColumnMetadataView, TableMetadataView

logger = logging.getLogger(__name__)


def _descriptor(view: ColumnMetadataView) -> dict:
    """Egress-safe per-column descriptor from the Task-3 view: `{column, operational_type,
    declared_type, concept?, business_definition?, term_type?, domain?, process_path?,
    semantic_type?}`. NEVER a conflated `type` key — `operational_type` is the row's physical type
    (stays `unknown` under a glossary upload until confirmed) and `declared_type` is the
    glossary-DECLARED SQL type (a hint; blank for a technical upload). Both always present so the
    synthesizer sees the distinction even when one is blank.

    M4 still holds by construction: the view sources `business_definition` ONLY from the curated
    sidecar meaning or the Pass-A draft (never the uploader's raw `r.definition` cell), bounded by
    `column_view._bounded` to the ONE `enrich_llm.MAX_DEFINITION_LEN` egress window (32_000 since
    2026-08-06 — named rather than restated, so the two cannot drift); the field-aware egress seam
    (`_redact_free_text_meta`) re-sanitizes it (sample-clause strip + PII) at dispatch. Facets are
    bounded structural tokens (200 cap)."""
    desc: dict = {"column": view.column,
                  "operational_type": (view.operational_type or "")[:200],
                  "declared_type": (view.declared_type or "")[:200]}
    if view.concept:
        desc["concept"] = view.concept
    if view.business_definition:
        desc["business_definition"] = view.business_definition
    for key, val in (("term_type", view.term_type), ("domain", view.domain),
                     ("process_path", view.process_path),
                     ("semantic_type", view.semantic_type)):
        if val:
            desc[key] = val[:200]
    return desc


def assemble_table_items(views: dict[str, TableMetadataView]) -> list[BatchItem]:
    """One BatchItem per table view; metadata is `{table, column_profiles, table_definition?}` —
    `table_definition` ONLY when the view carries one (the [F8] schema fence already ran in
    `build_table_views`, so a mismatched table term never reaches this seam). Each profile is the
    dual-type descriptor above and the assembled metadata is admissible under the metadata-only
    egress contract (`enrich_llm._item_egress_ok`). Sidecar attachment/withholding, Pass-A joins,
    and normalization all happened in the view builder — the assembler only projects."""
    items: list[BatchItem] = []
    for table, view in views.items():
        metadata: dict = {"table": table,
                          "column_profiles": [_descriptor(c) for c in view.columns]}
        if view.table_definition:
            metadata["table_definition"] = view.table_definition
        items.append(BatchItem(ref=table, metadata=metadata))
    return items


_VALID_BASIS = {"posted_at", "ingested_at"}  # lag-free bases only (event_time_plus_lag needs lag_hours)

# The per-table disposition fields — the per-run record set is TOTAL over them: every table that
# reaches `make_ref_accept` gets exactly one record per field, and every assembled table that never
# resolves gets the same set as `not_evaluated` ([F12]). ONE constant so the accept, the totalizer,
# and their tests can never drift.
#
# Profile Task 4 adds the four PROFILE suggestions beside the existing structural ones. They are the
# same KIND of thing (advisory table-level proposals) and share the same totality contract.
_STRUCTURAL_DISPOSITION_FIELDS = ("grain", "availability_time", "table_role", "primary_entity",
                                  "event_or_snapshot")
PROFILE_DISPOSITION_FIELDS = ("table_description", "business_context", "authority_role",
                              "temporal_storage_model")
DISPOSITION_FIELDS = (*_STRUCTURAL_DISPOSITION_FIELDS, *PROFILE_DISPOSITION_FIELDS)

#: The closed per-field disposition vocabulary. `not_evaluated` (the table never reached
#: validation) and `not_attempted` (the table WAS evaluated but this field was deliberately never
#: asked — e.g. the proposal-blind critic had no client) are DISTINCT on purpose: one is an
#: infrastructure outcome, the other a decision this run took.
DISPOSITION_STATUSES = frozenset({
    "accepted",         # validated and (where applicable) written as llm/proposed evidence
    "abstained",        # the model was asked and offered nothing
    "dropped_invalid",  # offered, but the code-side gate could not accept it
    "refuted",          # a deterministic contradiction, or the proposal-blind critic, refused it
    "superseded",       # a stronger CURRENT value already governs the field; kept for review only
    "not_attempted",    # in scope, deliberately not asked this run
    "not_evaluated",    # the table never reached per-field validation at all ([F12])
})

#: Critic reason codes that mean the critique NEVER RAN, so the field is `not_attempted` rather
#: than `refuted`. A starved critic disagreed with nothing — it was never asked. (The other
#: `PROFILE_CRITIC_REASON_CODES` all describe a critique that DID run, or a fault while running it,
#: and the critic itself resolves those to UPHELD before this mapping is ever consulted.)
CRITIC_NOT_ATTEMPTED_REASONS = frozenset({CRITIC_BUDGET_EXHAUSTED})


def add_not_evaluated(dispositions: list[dict], table: str) -> None:
    """[F12] totality for a table that NEVER REACHED per-field validation (egress-excluded,
    provider-failed, timed out, whole-rejected raw, or simply missing from the batch result —
    ``run_batched`` returns resolved refs only): append the full five-field record set with status
    ``not_evaluated`` so the per-run disposition shape stays uniform/TOTAL. ``not_evaluated`` is
    DISTINCT from ``abstained`` — abstained means the model was asked and offered nothing for an
    EVALUATED table; not_evaluated means validation never saw the table at all."""
    for field in DISPOSITION_FIELDS:
        dispositions.append({"table": table, "field": field, "status": "not_evaluated",
                             "reason": None, "prior_value_staled": False})


# ── profile Task 4: deterministic contradictions, run BEFORE any model opinion is consulted ─────
#
# Honest signals only. Each rule names the evidence it keys on and ABSTAINS when that evidence is
# absent from the bounded table context — a deterministic refutation no model may overturn must
# never fire on the ordinary case (a glossary catalog whose `operational_type` is uniformly
# "unknown", a wide table presented as a compact roster).

#: Closed vocabulary of profile contradictions.
PROFILE_CONTRADICTION_CODES = frozenset({
    "scd2_without_candidate_boundaries",
    "event_fact_without_time_column",
    "crosswalk_with_fewer_than_two_identifier_sides",
    "authority_claim_without_source_context",
})

#: Word tokens a validity-boundary column carries. Exact tokens, split on non-alphanumerics — never
#: substrings ("mandate" must not fire "date").
_BOUNDARY_TOKENS = frozenset({
    "from", "to", "start", "end", "eff", "effective", "expiry", "expiration", "exp",
    "valid", "since", "until", "open", "close", "closed",
})
#: Word tokens (and declared-type families) that mark a TIME column.
_TIME_TOKENS = frozenset({
    "date", "dt", "time", "ts", "timestamp", "datetime", "day", "month", "year", "asof", "when",
})
_TIME_TYPE_TOKENS = frozenset({"date", "timestamp", "timestamptz", "datetime", "time"})
#: The authority roles that CLAIM this copy is the place the data is decided.
_AUTHORITATIVE_ROLES = frozenset({"system_of_record", "mastered_view", "authoritative_replica"})


def _words(value: object) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", str(value or "").lower()) if w}


def _is_time_column(entry: dict) -> bool:
    if _words(entry.get("column")) & _TIME_TOKENS:
        return True
    for key in ("declared_type", "operational_type"):
        if _words(entry.get(key)) & _TIME_TYPE_TOKENS:
            return True
    return False


def _is_boundary_column(entry: dict) -> bool:
    words = _words(entry.get("column"))
    return bool(words & _BOUNDARY_TOKENS) and (
        bool(words & _TIME_TOKENS) or _is_time_column(entry))


def profile_contradictions(
    synthesis: dict, inventory: Sequence[dict], *,
    source_context: bool = False, catalog_context_available: bool = False,
) -> dict[str, str]:
    """Deterministic contradictions between a Pass-B profile suggestion and the table's own shape.

    Returns ``{field: code}`` — a subset of :data:`PROFILE_CONTRADICTION_CODES` keyed by the
    disposition field it refutes. Computed FIRST, before any critic dispatch: a contradiction
    refutes on its own and survives having no LLM client at all.

    ``inventory`` is the item's own column list — the full ``column_profiles`` on the narrow path,
    the compact ``column_roster`` on the wide one. What each rule can see differs, and each says so:

    * ``scd2_without_candidate_boundaries`` — a claimed SCD2 model needs a validity interval, which
      is a pair of BOUNDARY-shaped time columns (``valid_from``/``eff_dt``/``end_date``…). Keys on
      column NAMES plus declared/operational type families, both of which the compact roster carries
      in full — so this one does NOT abstain on a wide table. It abstains when there is no column
      inventory at all (nothing to look at).
    * ``event_fact_without_time_column`` — an event fact needs a time axis. Same signals, plus the
      synthesis' own ``as_of_column`` proposal, which satisfies it directly.
    * ``crosswalk_with_fewer_than_two_identifier_sides`` — a crosswalk maps at least two identifier
      value spaces. Keys on the resolved CONCEPT GROUP of each column, which only exists where Pass A
      classified one; a table whose inventory carries NO concept at all ABSTAINS (the review's
      explicit caveat: a glossary catalog's roster can be concept-free, and refuting every bridge
      claim there would be the misfire this rule exists to avoid).
    * ``authority_claim_without_source_context`` — an authoritative-copy claim needs something
      authored to rest on. It fires ONLY when the catalog demonstrably HAS source context to check
      against (``catalog_context_available``) and this table carries none; with no narrative
      anywhere it abstains, because "unsupported" and "unknown" are then indistinguishable.
    """
    from featuregen.overlay.upload.concepts import concept as lookup_concept

    out: dict[str, str] = {}
    entries = [e for e in inventory if isinstance(e, dict)]

    temporal = table_vocab_normalize_temporal(synthesis.get("temporal_storage_model"))
    if entries and temporal == "scd2" and not any(_is_boundary_column(e) for e in entries):
        out["temporal_storage_model"] = "scd2_without_candidate_boundaries"

    role = str(synthesis.get("table_role") or "").strip().lower()
    kind = str(synthesis.get("event_or_snapshot") or "").strip().lower()
    if entries and (role == "event_fact" or kind == "event"):
        if not synthesis.get("as_of_column") and not any(_is_time_column(e) for e in entries):
            out["table_role"] = "event_fact_without_time_column"

    if role == "bridge":
        concepts = [str(e.get("concept") or "") for e in entries if e.get("concept")]
        if concepts:      # ABSTAIN when the inventory carries no concept at all
            identifier_sides = sum(
                1 for name in concepts
                if (rec := lookup_concept(name)) is not None and rec.group == "identifier")
            if identifier_sides < 2:
                out["table_role"] = "crosswalk_with_fewer_than_two_identifier_sides"

    authority = str(synthesis.get("authority_role") or "").strip().lower()
    if (authority in _AUTHORITATIVE_ROLES and catalog_context_available and not source_context):
        out["authority_role"] = "authority_claim_without_source_context"

    illegal = set(out.values()) - PROFILE_CONTRADICTION_CODES
    if illegal:   # structurally impossible; guards vocabulary drift
        raise ValueError(f"profile contradiction codes outside the closed set: {sorted(illegal)}")
    return out


def table_vocab_normalize_temporal(raw: object) -> str | None:
    """`profile_vocab.normalize_temporal_storage_model`, imported lazily (profile_vocab imports
    table_vocab, which this module also imports)."""
    from featuregen.overlay.upload.profile_vocab import normalize_temporal_storage_model
    return normalize_temporal_storage_model(raw)


#: The bounded per-value length of a profile PROSE suggestion.
#:
#: It USED to be pinned to `enrich_llm.MAX_DEFINITION_LEN` ("so a description and a definition can
#: never drift apart"). The 2026-08-06 zero-truncation raises took that constant 600 -> 4000 ->
#: 32_000 and this one deliberately did NOT follow either time, because the two answer different
#: questions and the tie was coincidental. This value bounds Pass B's own OUTPUT, and that output
#: can be re-threaded into ITEM metadata as `business_context`/`table_description`, where it meets
#: the per-value egress ACCEPTANCE gate `enrich_llm._MAX_LEN_DEFAULT` (now 1000). So the
#: load-bearing invariant is `_MAX_PROFILE_PROSE < _MAX_LEN_DEFAULT` — 600 against 1000 holds it
#: with margin, and it is UNDISTURBED by the 32_000 raise precisely because that raise moved
#: `MAX_DEFINITION_LEN` and not `_MAX_LEN_DEFAULT`. Raising this to 4000 (let alone 32_000) would
#: make Pass B's own descriptions EXCLUDED-and-audited at the egress gate, which is the opposite of
#: what the raise was for. Pinned by `test_table_synth_assemble.py`.
_MAX_PROFILE_PROSE = 600
#: The non-column citations a suggestion may name.
#:
#: `table_definition` is the table's own curated definition. The CATALOG-NARRATIVE keys (Task 7b)
#: joined it at prompt v5, and without them the narrative was mechanically UNCITABLE: an honest
#: citation of `catalog_description` was filtered out here, `_accept_profile_fields` then dropped the
#: suggestion as `no_evidence_ref`, and the only route to acceptance was attaching a COLUMN citation
#: the suggestion does not actually rest on. That made `table_description`, `business_context`,
#: `authority_role` and `temporal_storage_model` incapable of resting on the prose this task exists
#: to deliver — and it would have taught the model to cite dishonestly to get an answer accepted.
#:
#: (Grain, as-of, `primary_entity`, `table_role` and `event_or_snapshot` were never ref-gated — they
#: are validated against column membership — so the narrative could already influence them. That is
#: the brief's #1 leverage point and it worked from the first commit; this widens the rest.)
#:
#: CITABLE ONLY WHERE THE ITEM CARRIES THEM (review round 2). The first version of this admitted all
#: five UNCONDITIONALLY, which is a forgery surface rather than a capability: `FEATUREGEN_DATASET_
#: PROFILES` is OFF BY DEFAULT, so in the default deployment NO item carries a narrative key at all —
#: yet a model emitting `{"field": "business_context", "refs": ["catalog_description"]}` would clear
#: the `no_evidence_ref` gate and land its prose accepted, citing something that does not exist.
#: That is exactly the "cite anything to get accepted" behaviour the gate exists to prevent, and it
#: is the behaviour widening these refs was supposed to make UNNECESSARY. The presence signal is
#: threaded per table from the items themselves (`_run_synthesis`), so the rule is now what the
#: sentence above always claimed: the refs an item ACTUALLY carries.
_NARRATIVE_CITATION_REFS: frozenset[str] = frozenset(CATALOG_NARRATIVE_KEYS)
_TABLE_DEFINITION_REF = "table_definition"


def narrative_refs_of(metadata: Mapping) -> frozenset[str]:
    """The catalog-narrative keys ONE Pass-B item actually carries — the citable set for that table.
    Empty for every item without a narrative, which is every item in a default deployment."""
    return frozenset(k for k in metadata if k in _NARRATIVE_CITATION_REFS)


def _cited_refs(synthesis: dict, field: str, *, cols: set[str],
                narrative_refs: frozenset[str] = frozenset()) -> list[str]:
    """The refs this suggestion cites, filtered to the BOUNDED TABLE CONTEXT it was given: a real
    column of THIS table, the literal ``table_definition``, or a catalog-narrative key THIS ITEM
    CARRIES. A hallucinated ref is dropped rather than trusted, so "names existing evidence refs" is
    enforced, never assumed.

    ``narrative_refs`` defaults to EMPTY, which is the fail-closed direction: a caller that has not
    said which narrative the item carries has not established that any exists, so none is citable.
    """
    allowed = {c.lower() for c in cols} | {_TABLE_DEFINITION_REF} | narrative_refs
    out: list[str] = []
    for entry in synthesis.get("evidence_refs") or []:
        if not isinstance(entry, dict) or str(entry.get("field") or "").strip() != field:
            continue
        for ref in entry.get("refs") or []:
            if isinstance(ref, str) and ref.strip().lower() in allowed:
                cited = ref.strip().lower()
                if cited not in out:
                    out.append(cited)
    return out


def _accept_profile_fields(synthesis: dict, ref: str, *, cols: set[str],
                           contradictions: dict[str, str], put, source_definition: str | None,
                           critic, narrative_refs: frozenset[str] = frozenset()) -> dict:
    """Validate the four profile suggestions, appending one disposition each (TOTAL over
    :data:`PROFILE_DISPOSITION_FIELDS`). Returns the accepted values, keyed by field.

    The order of authority is fixed and mirrors the concept critic's:

    1. a DETERMINISTIC contradiction refutes outright — no model may overturn it, and the critic is
       never even asked;
    2. the code-side vocabulary/shape gate drops an unusable value (that FIELD only — [F1]);
    3. every surviving suggestion must NAME evidence refs from the bounded table context;
    4. a SOURCE-authored table definition stays current: the LLM's alternative description is
       recorded ``superseded`` (kept for review in the structured result) and never written as
       competing evidence;
    5. for the two OPERATIONAL classifications only, the proposal-blind critic gets a veto.
    """
    from featuregen.overlay.upload.profile_vocab import (
        normalize_authority_role,
        normalize_temporal_storage_model,
    )

    accepted: dict = {}

    def _prose(field: str, *, superseded_by_source: bool) -> None:
        raw = synthesis.get(field)
        if contradictions.get(field):
            put(ref, field, "refuted", contradictions[field])
            return
        if not isinstance(raw, str) or not raw.strip():
            put(ref, field, "abstained")
            return
        value = raw.strip()[:_MAX_PROFILE_PROSE]
        refs = _cited_refs(synthesis, field, cols=cols, narrative_refs=narrative_refs)
        if not refs:
            put(ref, field, "dropped_invalid", "no_evidence_ref")
            return
        if superseded_by_source:
            # The curated text stays CURRENT. The alternative is real review material — it rides
            # the structured result — but it never becomes competing `definition` evidence.
            put(ref, field, "superseded", "source_authored_definition_current")
            return
        accepted[field] = value
        accepted.setdefault("evidence_refs", {})[field] = refs
        put(ref, field, "accepted")

    def _classification(field: str, normalize) -> None:
        raw = synthesis.get(field)
        if contradictions.get(field):
            put(ref, field, "refuted", contradictions[field])
            return
        if raw in (None, ""):
            put(ref, field, "abstained")
            return
        value = normalize(raw)
        if value is None:
            put(ref, field, "dropped_invalid", f"{field}_off_vocab")
            return
        refs = _cited_refs(synthesis, field, cols=cols, narrative_refs=narrative_refs)
        if not refs:
            put(ref, field, "dropped_invalid", "no_evidence_ref")
            return
        if critic is None:
            # In scope but deliberately not asked this run (no client / critic disabled) — an
            # OPERATIONAL classification never lands unreviewed on a single model's say-so.
            put(ref, field, "not_attempted", "critic_unavailable")
            return
        verdict = critic(ref, field, value, refs)
        if verdict is not True:
            # A critique that never RAN is `not_attempted`, not `refuted`: nobody disagreed with
            # this value — nobody was asked. Either way the value does not land.
            status = ("not_attempted" if verdict in CRITIC_NOT_ATTEMPTED_REASONS else "refuted")
            put(ref, field, status,
                verdict if isinstance(verdict, str) else "critic_refuted")
            return
        accepted[field] = value
        accepted.setdefault("evidence_refs", {})[field] = refs
        put(ref, field, "accepted")

    _prose("table_description", superseded_by_source=bool(source_definition))
    _prose("business_context", superseded_by_source=False)
    _classification("authority_role", normalize_authority_role)
    _classification("temporal_storage_model", normalize_temporal_storage_model)
    return accepted


def make_ref_accept(columns_by_table: dict[str, set[str]], *,
                    dispositions: list[dict] | None = None,
                    inventory_by_table: dict[str, Sequence[dict]] | None = None,
                    source_context_by_table: dict[str, bool] | None = None,
                    source_definition_by_table: dict[str, str] | None = None,
                    catalog_context_available: bool = False,
                    narrative_refs_by_table: dict[str, frozenset[str]] | None = None,
                    critic=None):
    """A ref-aware accept for `validate_batch_results(..., ref_aware=True)`. `ref` is the table name;
    validate the serialized `synthesis` against THAT table's real columns and map a valid result onto
    the FACT_VALUE_SCHEMAS shapes (grain `{columns, is_unique}` / availability `{column, basis}`).

    Slice 2: every field is validated INDEPENDENTLY — an invalid field drops THAT FIELD ONLY (the
    table still resolves; only unparseable / non-object raw whole-rejects). The `table_role` vocab
    is enforced HERE, not as a schema enum ([F1] — `reg.validate` would fail the WHOLE synthesis on
    one off-vocab role, destroying this per-field salvage). Every resolved synthesis appends a
    disposition record to `dispositions` for ALL FIVE fields ([F12] — TOTAL):
    ``{"table", "field", "status", "reason", "prior_value_staled": False}`` with
    ``status in {accepted, abstained, dropped_invalid}``; an absent advisory field == abstained.
    ``prior_value_staled`` is set later by the staling seam, never here."""
    disp = dispositions if dispositions is not None else []

    def _put(ref: str, field: str, status: str, reason: str | None = None) -> None:
        if status not in DISPOSITION_STATUSES:   # closed vocabulary; guards drift
            raise ValueError(f"disposition status {status!r} is not in the closed set")
        disp.append({"table": ref, "field": field, "status": status, "reason": reason,
                     "prior_value_staled": False})

    def _replace(ref: str, field: str, status: str, reason: str | None = None) -> None:
        """Upgrade THIS run's record for `{ref, field}` in place (searched newest-first), so a
        deterministic refutation supersedes the accept's own verdict instead of appending a second,
        contradictory record for the same field. Appends when no record exists yet."""
        for rec in reversed(disp):
            if rec.get("table") == ref and rec.get("field") == field:
                if status not in DISPOSITION_STATUSES:
                    raise ValueError(f"disposition status {status!r} is not in the closed set")
                rec["status"], rec["reason"] = status, reason
                return
        _put(ref, field, status, reason)

    def accept(raw: str, ref: str) -> tuple[str | None, str]:
        cols = columns_by_table.get(ref, set())
        back = {c.lower(): c for c in cols}   # normalized -> CANONICAL table spelling
        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        if not isinstance(s, dict):
            return None, "not_object"   # "null"/"[]"/"\"x\"" parse fine but can't .get(...)

        # ── grain: a real list[str], case-folded for duplicates/membership, mapped BACK to the
        # table's canonical spelling. Any violation drops the GRAIN ONLY — the other fields keep
        # their own verdicts. `is_unique=True` is the CLAIM being proposed (these columns are
        # asserted to identify a row), NOT empirical proof — there is no profiling in Phase 2; human
        # confirmation IS the uniqueness attestation. An empty/absent grain_columns == the model
        # ABSTAINING (MF-3), never a reject.
        rg = s.get("grain_columns")
        grain = None
        if rg is None or rg == []:
            _put(ref, "grain", "abstained")
        elif not isinstance(rg, list) or not all(isinstance(c, str) for c in rg):
            _put(ref, "grain", "dropped_invalid", "grain_invalid_shape")
        else:
            fold = [c.strip().lower() for c in rg]
            if len(fold) != len(set(fold)):
                _put(ref, "grain", "dropped_invalid", "grain_duplicate")
            elif len(rg) > table_vocab.MAX_GRAIN_COLS:
                _put(ref, "grain", "dropped_invalid", "grain_over_bound")
            elif any(f not in back for f in fold):
                _put(ref, "grain", "dropped_invalid", "grain_col_not_in_table")
            else:
                grain = {"columns": [back[f] for f in fold], "is_unique": True}
                _put(ref, "grain", "accepted")

        # ── availability: DECOUPLED from grain — a bad as-of (a column the table lacks, or a basis
        # outside the lag-free enum) drops ONLY the availability, never an otherwise-valid grain.
        # [F13]: the column is case-folded and emitted in the CANONICAL table spelling (same map as
        # grain); the basis is strip/lower-matched into `_VALID_BASIS`.
        availability = None
        aoc, aob = s.get("as_of_column"), s.get("as_of_basis")
        if aoc is None:
            _put(ref, "availability_time", "abstained")
        else:
            col = back.get(aoc.strip().lower()) if isinstance(aoc, str) else None
            basis = aob.strip().lower() if isinstance(aob, str) else None
            if col is not None and basis in _VALID_BASIS:
                availability = {"column": col, "basis": basis}
                _put(ref, "availability_time", "accepted")
            else:
                _put(ref, "availability_time", "dropped_invalid",
                     "basis_not_allowed" if col is not None else "as_of_col_not_in_table")
                counters.incr("overlay.table_synth.availability.dropped_bad_as_of")
                logger.info("table_synth dropped a bad as-of for %r (col=%r basis=%r) — keeping grain",
                            ref, aoc, aob)

        # ── advisory fields: strip/lower-normalized, vocab/registry-gated, each with its own
        # disposition. [F13]: a NON-EMPTY event_or_snapshot that normalizes to None is OFF-VOCAB
        # (dropped_invalid), not an abstention.
        reos = s.get("event_or_snapshot")
        eos = table_vocab.normalize_event_or_snapshot(reos)
        if eos is not None:
            _put(ref, "event_or_snapshot", "accepted")
        elif isinstance(reos, str) and reos != "":
            _put(ref, "event_or_snapshot", "dropped_invalid", "event_or_snapshot_off_vocab")
        else:
            _put(ref, "event_or_snapshot", "abstained")

        rr = s.get("table_role")
        role = table_vocab.normalize_table_role(rr, event_or_snapshot=eos)
        if rr and role is None:
            _put(ref, "table_role", "dropped_invalid", "role_off_vocab")
        else:
            _put(ref, "table_role", "accepted" if role else "abstained")

        ent = s.get("primary_entity")
        ent = ent.strip().lower() if isinstance(ent, str) else None
        if ent and ent not in known_entities():
            _put(ref, "primary_entity", "dropped_invalid", "entity_not_registered")
            ent = None
        else:
            _put(ref, "primary_entity", "accepted" if ent else "abstained")

        # ── profile Task 4: the four PROFILE suggestions, deterministic contradictions FIRST.
        inventory = list((inventory_by_table or {}).get(ref, ()))
        contradictions = profile_contradictions(
            {**s, "table_role": role, "event_or_snapshot": eos}, inventory,
            source_context=(source_context_by_table or {}).get(ref, False),
            catalog_context_available=catalog_context_available)
        # A refuted STRUCTURAL field is evicted here too: the disposition `make_ref_accept` already
        # appended for it is upgraded to `refuted`, and the value is dropped so nothing downstream
        # can propose it. A deterministic contradiction outranks every model opinion, including the
        # one that produced this synthesis.
        if contradictions.get("table_role") and role is not None:
            _replace(ref, "table_role", "refuted", contradictions["table_role"])
            role = None

        profile = _accept_profile_fields(
            s, ref, cols=cols, contradictions=contradictions, put=_put,
            source_definition=(source_definition_by_table or {}).get(ref),
            narrative_refs=(narrative_refs_by_table or {}).get(ref) or frozenset(),
            critic=critic)

        # A parseable synthesis with neither grain nor availability is a VALID ABSTENTION (some tables
        # genuinely have no single grain / as-of) — retain the surviving advisory fields and propose
        # zero grain/availability facts. Only unparseable / non-object raw (above) is a failure.
        out = {"grain": grain, "availability_time": availability,
               "table_role": role, "primary_entity": ent, "event_or_snapshot": eos, **profile}
        return json.dumps(out, sort_keys=True), ("valid" if (grain or availability) else "abstained")
    return accept


def make_summary_accept(columns_by_ref: dict[str, set[str]]):
    """A ref-aware accept for the PHASE-1 chunk-summary task (#1). `ref` is a chunk id; validate the
    serialized `summary` and FILTER its candidate columns to those actually in THAT chunk (a summary
    is advisory input to phase 2, not a governed fact — a stray hallucinated column drops silently, it
    must never lose the whole chunk's summary and thereby fail the table). Only unparseable / non-object
    raw is rejected; everything else normalizes to a bounded, egress-safe summary."""
    def accept(raw: str, ref: str) -> tuple[str | None, str]:
        cols = columns_by_ref.get(ref, set())
        back = {c.lower(): c for c in cols}   # normalized -> CANONICAL chunk spelling (Slice 2)

        def _known(names) -> list[str]:
            # Same normalization as make_ref_accept: case-fold, match against the chunk's real
            # columns, emit the CANONICAL spelling; a stray/off-chunk candidate drops silently
            # (a summary is advisory phase-1 input, never a governed fact). Deduped post-fold.
            out: list[str] = []
            for c in names or []:
                if isinstance(c, str):
                    hit = back.get(c.strip().lower())
                    if hit is not None and hit not in out:
                        out.append(hit)
            return out[:32]

        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        if not isinstance(s, dict):
            return None, "not_object"
        grain = _known(s.get("grain_candidates"))
        temporal = _known(s.get("temporal_candidates"))
        entity = [e for e in (s.get("entity_signals") or []) if isinstance(e, str)][:16]
        kind = table_vocab.normalize_event_or_snapshot(s.get("event_or_snapshot"))
        out = {"grain_candidates": grain, "temporal_candidates": temporal,
               "entity_signals": entity, "event_or_snapshot": kind}
        return json.dumps(out, sort_keys=True), "valid"
    return accept


def _table_dispatch_subject(catalog_source: str, schema: str | None, table: str,
                            columns: list[str]) -> dict:
    """One ``llm_dispatch_subject`` mapping (C5-T5) for a Pass B table item: the table's
    schema-aware evidence identity (the SAME ``schema_by_table`` schema its fact proposals key
    under) with ``field_names`` = the column names this physical request carries. Attribution
    strings only — never row data."""
    logical_ref = normalize_ref(catalog_source, schema, table)
    return {"catalog_source": catalog_source, "object_ref": logical_ref.split("::", 1)[1],
            "logical_ref": logical_ref, "field_names": sorted(columns)}


def _dispatch_subjects_for(items: list[BatchItem], *, catalog_source: str | None,
                           schema_by_table: dict[str, str] | None) -> dict[str, dict] | None:
    """The ``{ref: subject}`` mapping for a synthesis batch (C5-T5): one TABLE subject per item,
    ``field_names`` drawn from whichever roster the item carries (full ``column_profiles`` on the
    narrow/summary path, the compact ``column_roster`` on the wide phase-2 path). ``None`` when the
    caller supplied no ``catalog_source`` (a direct/test call) — no subjects to attribute."""
    if catalog_source is None:
        return None
    sbt = schema_by_table or {}
    out: dict[str, dict] = {}
    for it in items:
        descs = it.metadata.get("column_profiles") or it.metadata.get("column_roster") or []
        cols = [d.get("column") for d in descs if isinstance(d, dict) and d.get("column")]
        table = it.metadata.get("table") or it.ref
        out[it.ref] = _table_dispatch_subject(catalog_source, sbt.get(table), table, cols)
    return out


def _profile_critic(conn, client, items: list[BatchItem], *, catalog_source: str | None,
                    schema_by_table: dict[str, str] | None, context_revision: str, actor):
    """The proposal-blind veto for the two OPERATIONAL profile classifications (profile Task 4).

    Returns ``critic(ref, field, value, cited_refs, *, budget=None) -> True | reason_code`` —
    ``True`` upholds; :data:`CRITIC_NOT_ATTEMPTED_REASONS` members mean the critique never ran; any
    other code refutes. The comparison happens INSIDE `attest.dataset_profile_critic`, code-side
    and outside the prompt; this closure only routes the table's own bounded context to it. A run
    with no client gets ``None``, and `_accept_profile_fields` then records `not_attempted` rather
    than landing an operational classification on one model's unreviewed say-so.

    ``budget`` is the enclosing synthesis run's shared ``CallLedger``, bound by `_run_synthesis`:
    each critique is a PHYSICAL provider call and must be spent from the run's own ceiling."""
    if client is None:
        return None
    from featuregen.overlay.upload.attest.dataset_profile_critic import (
        CRITIC_FIELDS,
        ProfileCriticDisposition,
        critique_profile_claim,
    )
    context_by_ref = {it.ref: it.metadata for it in items}
    sbt = schema_by_table or {}

    def _critic(ref: str, field: str, value: str, cited_refs, *, budget=None):
        if field not in CRITIC_FIELDS:
            return True                       # out of scope: the one-model path stands
        context = context_by_ref.get(ref, {})
        table = str(context.get("table") or ref)
        dataset_ref = (normalize_ref(catalog_source, sbt.get(table.strip().lower()), table)
                       if catalog_source else table)
        try:
            outcome = critique_profile_claim(
                conn, client, dataset_ref=dataset_ref, field=field, proposed_value=value,
                context=context, cited_refs=cited_refs, context_revision=context_revision,
                actor=actor, call_budget=budget)
        except Exception:  # noqa: BLE001 — advisory: a critic fault never fails the synthesis
            logger.warning("advisory profile critic failed for %r/%s", ref, field, exc_info=True)
            return True                       # fail-soft UPHELD; never evict on infrastructure
        if outcome.disposition is ProfileCriticDisposition.UPHELD:
            return True
        if outcome.disposition is ProfileCriticDisposition.NOT_ATTEMPTED:
            # The critique never ran (the run's call budget was spent). Report the reason as-is so
            # the disposition is `not_attempted` — a starved critic is not a disagreeing one.
            return outcome.reason_codes[0] if outcome.reason_codes else CRITIC_BUDGET_EXHAUSTED
        return "independent_disagrees"

    return _critic


def synthesize_tables(conn, client, items: list[BatchItem], *, columns_by_table, actor,
                      dispositions: list[dict] | None = None,
                      ingestion_run_id: str | None = None,
                      catalog_source: str | None = None,
                      schema_by_table: dict[str, str] | None = None,
                      stats: dict | None = None,
                      catalog_context_available: bool = False,
                      catalog_narrative: Mapping | None = None) -> dict[str, dict]:
    """Run the governed batch synthesis; return {table: synthesis_dict} for VALID results only.
    Validation is done INSIDE run_batched via the ref-aware accept — this function does no
    post-filtering (an INVALID synthesis never reaches here).

    ``dispositions`` (Slice-2 Task 3) is the caller's per-run collector, threaded into BOTH
    execution paths' ref-aware accepts (`make_ref_accept`), which append the five per-field
    records for every table they validate. Mutated IN PLACE (the caller keeps the same list it
    later hands to `_propose_table_facts` for the [F9] staling flips and totalizes via
    `add_not_evaluated`); ``None`` (a direct caller) collects nothing.

    Wide tables (#1): an item whose ``column_profiles`` exceeds ``_MAX_COLUMN_PROFILES`` cannot egress
    as one giant item, so it is routed through the TWO-PHASE path (phase-1 per-chunk summaries -> a
    single phase-2 synthesis over the summaries + a complete roster). NARROW tables
    (``<=_MAX_COLUMN_PROFILES`` profiles) keep today's single-call fast path byte-for-byte. Since the
    2026-08-06 zero-truncation raise took that cap 64 -> 512, EVERY real bank table on this platform
    (126/144 columns) is narrow and synthesizes in ONE call over its complete profiles; the two-phase
    path is now the backstop for a pathological (>512-column) table rather than the normal route.
    A wide table synthesizes over whatever chunk
    summaries LANDED (the roster is complete regardless); only a table with ZERO chunk summaries,
    or whose synthesis is invalid, simply never appears in the returned dict — the caller then reports
    the honest partial/failed outcome (no phantom "resolved").

    NOTE: the batch-mode config (``OVERLAY_ENRICH_TABLE_SYNTH_MODE`` / ``mode("table_synth")``) is
    intentionally NOT consulted here. Pass B is BATCH-ONLY: a ref_aware task has no single-call
    seam (run_batched skips the single fallback for ref_aware), so there is no "single" execution
    path a mode switch could select. Only the FEATURE switch (``OVERLAY_TABLE_SYNTH``,
    ``ingest.table_synth_enabled``) gates Pass B.

    ``stats`` (optional out-param — return shape unchanged): accumulates ``not_attempted``, the count
    of TABLES the budget/deadline cutoff skipped WITHOUT dispatching their synthesis (narrow path +
    wide phase-2), so the caller labels a truncated Pass B ``truncated`` rather than ``items_failed``.

    C5-T5 — ``ingestion_run_id`` + ``catalog_source`` (+ ``schema_by_table``, the Pass-B fact-key
    schema map): with a run id, every Pass B dispatch (chunk summaries AND syntheses) is pre-audited
    and attributed to the run + its TABLE subjects under stage ``pass_b``. ``ingestion_run_id=None``
    (every direct/test caller) is byte-for-byte today's behavior.

    ``catalog_narrative`` (Task 7b) is the catalog's authored narrative as model context
    (:func:`catalog_profiles.catalog_narrative_block`), joined onto EVERY item here. This is the
    highest-leverage place that prose could go: Pass B decides grain, ``table_role``,
    ``primary_entity`` and ``event_or_snapshot`` from column names and profiles alone, and a
    sentence like "funds-transfer records — all outbound SWIFT/RTGS payments; Compliance-owned"
    answers three of those four outright. Grain is the most expensive thing in this pipeline to get
    wrong. It is CONTEXT and carries its own ``human/proposed`` authority label; it refutes nothing
    and defaults nothing — the deterministic contradictions and the proposal-blind critic remain the
    only things that can overturn a suggestion.

    Joined BEFORE ``context_revision`` deliberately: the narrative is part of the QUESTION, so an
    edit must re-ask it rather than replay an answer given to a different one. The item's OWN keys
    win a collision, so no catalog-grain value can displace a table's identity; ``None`` (every
    direct caller, and any catalog with no authored narrative) leaves every item byte-identical."""
    if catalog_narrative:
        items = [BatchItem(ref=it.ref, metadata={**catalog_narrative, **it.metadata})
                 for it in items]
    narrow = [it for it in items
              if len(it.metadata.get("column_profiles") or []) <= _MAX_COLUMN_PROFILES]
    wide = [it for it in items
            if len(it.metadata.get("column_profiles") or []) > _MAX_COLUMN_PROFILES]
    # The replay identity of THIS run's Pass-B context: a byte-identical re-upload replays every
    # stored critique and synthesis for free, while any content change re-asks the affected
    # questions. Folds in the profile VOCABULARY fingerprint (a changed admissible-answer set is a
    # changed question) and the prompt/schema versions the synthesis stamps.
    context_revision = canonical_hash({
        "prompt_id": _SYNTH_PROMPT_ID, "prompt_version": _SYNTH_PROMPT_VERSION,
        "schema_id": "overlay_table_synth_batch", "schema_version": _SYNTH_SCHEMA_VERSION,
        "profile_vocabulary": _profile_vocabulary_fingerprint(),
        "items": {it.ref: it.metadata for it in sorted(items, key=lambda i: i.ref)},
    })
    critic = _profile_critic(conn, client, items, catalog_source=catalog_source,
                             schema_by_table=schema_by_table,
                             context_revision=context_revision, actor=actor)
    resolved: dict[str, dict] = {}
    if narrow:
        # Today's exact path: one synthesis batch over the full profiles (fast path, byte-for-byte).
        resolved.update(_run_synthesis(conn, client, narrow, columns_by_table=columns_by_table,
                                       actor=actor, instruction=_INSTRUCTION,
                                       dispositions=dispositions,
                                       ingestion_run_id=ingestion_run_id,
                                       catalog_source=catalog_source,
                                       schema_by_table=schema_by_table, stats=stats,
                                       catalog_context_available=catalog_context_available,
                                       critic=critic))
    if wide:
        resolved.update(_synthesize_wide_tables(conn, client, wide,
                                                columns_by_table=columns_by_table, actor=actor,
                                                dispositions=dispositions,
                                                ingestion_run_id=ingestion_run_id,
                                                catalog_source=catalog_source,
                                                schema_by_table=schema_by_table, stats=stats,
                                                catalog_context_available=catalog_context_available,
                                                critic=critic))
    # Profile Task 4: record the accepted synthesis in the shared structured-result store so the
    # Release-A evaluation can REPLAY it without a live LLM. Immutable + content-addressed on the
    # exact context that produced it; a second store would be the duplication the plan forbids.
    _record_synthesis_results(conn, resolved, context_revision=context_revision,
                              catalog_source=catalog_source, schema_by_table=schema_by_table)
    return resolved


#: The Pass-B synthesis contract, named once so the request, the replay identity and the tests read
#: the same values.
#:
#: v5 (Task 7b) — the CATALOG NARRATIVE. Two prompt changes that only make sense together, so they
#: ship as one version: `_TYPE_FIELDS_NOTE` now says what the catalog-narrative keys ARE (whole
#: catalog, not this table; context, never a fact about this table), and `_PROFILE_NOTE` names them
#: as citable refs alongside `table_definition`. Widening `_cited_refs` without telling the model
#: would have left the capability unreachable; telling the model without widening `_cited_refs`
#: would have taught it to cite something the code silently discards. The SCHEMA is untouched — the
#: response shape did not move, only the question.
_SYNTH_PROMPT_ID = "overlay_table_synth_v5"
_SYNTH_PROMPT_VERSION = 5
_SYNTH_SCHEMA_VERSION = 3
#: The phase-1 (wide-table) summary prompt id. Its VERSIONS are `_SYNTH_PROMPT_VERSION` /
#: `_SYNTH_SCHEMA_VERSION` — one Pass B run stamps ONE contract generation across both phases.
_SUMMARY_PROMPT_ID = f"overlay_table_synth_summary_v{_SYNTH_PROMPT_VERSION}"
PASS_B_RESULT_TYPE = "table_profile_synthesis"
PASS_B_RESULT_VERSION = 1


def _profile_vocabulary_fingerprint() -> str:
    from featuregen.overlay.upload.profile_vocab import profile_vocabulary_fingerprint
    return profile_vocabulary_fingerprint()


def _record_synthesis_results(conn, resolved: dict[str, dict], *, context_revision: str,
                              catalog_source: str | None,
                              schema_by_table: dict[str, str] | None) -> None:
    """Persist each accepted synthesis through the EXISTING `structured_result` store, keyed by the
    exact input hash the context produced. Fail-soft per table: a store failure degrades replay,
    never the upload."""
    from featuregen.overlay.upload.structured_results import record_structured_result

    sbt = schema_by_table or {}
    for table, synthesis in resolved.items():
        dataset_ref = (normalize_ref(catalog_source, sbt.get(table.strip().lower()), table)
                       if catalog_source else table)
        input_hash = canonical_hash({"context_revision": context_revision, "table": table})
        try:
            with conn.transaction():   # savepoint: one bad row never poisons the caller's tx
                record_structured_result(
                    conn, result_type=PASS_B_RESULT_TYPE, result_version=PASS_B_RESULT_VERSION,
                    input_content_hash=input_hash, output=dict(synthesis),
                    producer_kind="llm_call", producer_ref=f"{ENRICHMENT_RUN_ID}:pass_b",
                    authority={"authority": "llm_advisory", "logical_ref": dataset_ref})
        except Exception:  # noqa: BLE001 — advisory: replay persistence never fails an upload
            logger.warning("advisory Pass-B structured-result write failed for %r", table,
                           exc_info=True)


def _run_synthesis(conn, client, items: list[BatchItem], *, columns_by_table, actor, instruction,
                   dispositions: list[dict] | None = None,
                   ingestion_run_id: str | None = None,
                   catalog_source: str | None = None,
                   schema_by_table: dict[str, str] | None = None,
                   stats: dict | None = None,
                   catalog_context_available: bool = False,
                   critic=None) -> dict[str, dict]:
    """The governed phase-2 synthesis batch (shared by the narrow fast path and the wide path): SAME
    task/schema/accept/result-shape — only the item metadata (full profiles vs summaries+roster) and
    the instruction differ. Returns {table: synthesis_dict} for VALID results only.

    Ships the Pass B contract via the Task-1 version seam, read from `_SYNTH_PROMPT_VERSION` /
    `_SYNTH_SCHEMA_VERSION` (never re-typed): **prompt v5** (the code-side `table_role` vocab is
    enumerated in the instruction; v5 adds the catalog-narrative context and its citability) over
    the **canonical v3 schema** — a REAL v3 body, because v2 is
    a byte-alias of v1 with `additionalProperties: false` and would reject the profile suggestions.
    [F1]: `table_role` is deliberately NOT a schema enum — `reg.validate` rejects the
    WHOLE synthesis on one schema violation, so a strict role enum would lose a valid grain to one
    off-vocab role; the vocab is enforced per-field in `make_ref_accept` instead.

    ``dispositions`` threads the caller's per-run collector into the ref-aware accept, which
    appends the five per-field records for every table it validates (retries never duplicate: a
    resolved ref is excluded from every retry/split chunk, and only a parseable-dict raw — which
    always resolves — appends records)."""
    inventory = {it.ref: (it.metadata.get("column_profiles")
                          or it.metadata.get("column_roster") or []) for it in items}
    source_definitions = {it.ref: it.metadata.get("table_definition") for it in items
                          if it.metadata.get("table_definition")}
    # Review round 2: WHICH narrative keys each item actually carries, so a citation can only name
    # context that reached the model. Built from the items themselves — the same place `inventory`
    # and `source_definitions` come from — so it cannot disagree with what egressed.
    narrative_refs = {it.ref: narrative_refs_of(it.metadata) for it in items}
    # ONE ledger for this synthesis run, shared by the batching ladder AND the critic it fires from
    # inside `accept`: a critique is a physical provider call, so it spends the SAME ceiling the
    # chunks spend (the finding — critic calls were invisible to `max_provider_calls`). The ledger
    # is per-run_batched-run, exactly the scope `max_provider_calls` has always had.
    ledger = CallLedger(enrich_config.budget("table_synth").max_provider_calls)
    budgeted_critic = None if critic is None else (
        lambda ref, field, value, refs: critic(ref, field, value, refs, budget=ledger))
    accept = make_ref_accept(
        columns_by_table, dispositions=dispositions, inventory_by_table=inventory,
        source_definition_by_table=source_definitions,
        source_context_by_table={ref: True for ref in source_definitions},
        catalog_context_available=catalog_context_available,
        narrative_refs_by_table=narrative_refs, critic=budgeted_critic)
    batch_report: dict = {}   # honest-labeling: run_batched reports budget/deadline not_attempted
    resolved = run_batched(
        conn, client, short="table_synth", task="table_synth",
        prompt_id=_SYNTH_PROMPT_ID, schema_id="overlay_table_synth_batch",
        prompt_version=_SYNTH_PROMPT_VERSION, schema_version=_SYNTH_SCHEMA_VERSION,
        shared_metadata={}, items=items, out_key="synthesis",
        instruction=instruction, accept=accept, actor=actor,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True), ref_aware=True,
        deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
        report=batch_report,
        ingestion_run_id=ingestion_run_id, dispatch_stage="pass_b",
        dispatch_subjects=_dispatch_subjects_for(items, catalog_source=catalog_source,
                                                 schema_by_table=schema_by_table),
        call_ledger=ledger,
    )
    # ACCUMULATE (narrow + wide-phase-2 both funnel here): these table-granular refs are exactly
    # Pass B's expected unit, so a truncated synthesis surfaces as the stage's `not_attempted`.
    if stats is not None and batch_report.get("not_attempted"):
        stats["not_attempted"] = stats.get("not_attempted", 0) + batch_report["not_attempted"]
    return {table: json.loads(raw) for table, raw in resolved.items()}


def _chunk_profiles(profiles: list[dict]) -> list[list[dict]]:
    """Deterministic consecutive chunks of the table's profiles (stable column order preserved), each
    ``<=_MAX_COLUMN_PROFILES`` so every chunk item passes the per-item egress cap."""
    return [profiles[i:i + _MAX_COLUMN_PROFILES]
            for i in range(0, len(profiles), _MAX_COLUMN_PROFILES)]


def _roster_entry(desc: dict) -> dict:
    """One STRUCTURED wide-roster entry `{column, operational_type, declared_type}` from a
    per-column descriptor (the wide path holds assembled items, so the descriptor — which carries
    exactly these keys from the view — is the projection source). Structured, never the old
    `name:type` flat string: a column name may itself contain `:`/`/`, which the flat form
    conflated irrecoverably. Values are bounded to the default per-value egress cap."""
    entry = {"column": (desc.get("column") or "")[:200],
             "operational_type": (desc.get("operational_type") or "")[:200],
             "declared_type": (desc.get("declared_type") or "")[:200]}
    # Profile Task 4: the resolved CONCEPT rides the compact roster too. It is the only signal the
    # crosswalk contradiction can key on ("a crosswalk maps >= 2 identifier value spaces"), so
    # without it that rule would have to abstain on every wide table — which is most of them.
    # `_ROSTER_ENTRY_KEYS` classifies it as a bounded structural token (a registry concept name).
    if desc.get("concept"):
        entry["concept"] = str(desc["concept"])[:200]
    return entry


def _synthesize_wide_tables(conn, client, wide_items: list[BatchItem], *, columns_by_table, actor,
                            dispositions: list[dict] | None = None,
                            ingestion_run_id: str | None = None,
                            catalog_source: str | None = None,
                            schema_by_table: dict[str, str] | None = None,
                            stats: dict | None = None,
                            catalog_context_available: bool = False,
                            critic=None) -> dict[str, dict]:
    """Two-phase synthesis for tables wider than the egress cap (#1). ``dispositions`` threads to
    the PHASE-2 synthesis only (whose refs are the table names); the phase-1 chunk summaries are
    advisory input keyed by chunk ref and record no per-field dispositions.

    Phase 1: split each wide table into consecutive ``<=_MAX_COLUMN_PROFILES``-profile chunks and
    SUMMARIZE each chunk
    (no fact output) — every chunk item is egress-safe. Phase 2: for each table with AT LEAST ONE
    chunk summary, run ONE synthesis over whatever chunk summaries LANDED + a compact complete roster
    of STRUCTURED ``{column, operational_type, declared_type}`` entries + the table's
    ``table_definition`` (when the assembled item carried one). The roster is built from ALL profiles,
    so a budget-skipped chunk summary (advisory phase-2 input, not a governed fact) never starves the
    synthesis — the ``overlay.table_synth.wide.partial_summaries`` counter records the shortfall. Only
    a table with ZERO landed summaries is dropped (``wide.zero_summaries``) so the caller reports it
    honestly as unresolved."""
    chunk_items: list[BatchItem] = []
    chunk_refs_by_table: dict[str, list[str]] = {}
    columns_by_ref: dict[str, set[str]] = {}
    roster_by_table: dict[str, list[dict]] = {}
    table_def_by_table: dict[str, str] = {}
    carried_narrative: dict[str, dict] = {}
    for it in wide_items:
        table = it.ref
        profiles = it.metadata.get("column_profiles") or []
        # Complete roster: STRUCTURED {column, operational_type, declared_type} entries — small,
        # egress-safe, and enough for phase-2 grounding without conflating a `:`-containing name.
        roster_by_table[table] = [_roster_entry(d) for d in profiles]
        # The table-level definition rides the ASSEMBLED item's metadata; the rebuilt phase-2 item
        # must carry it forward explicitly or the wide path silently drops it.
        table_def = it.metadata.get("table_definition")
        if table_def:
            table_def_by_table[table] = table_def
        # Task 7b: the CATALOG narrative rides the assembled item too, and the phase-2 item below is
        # REBUILT from scratch — so it must be carried forward explicitly for exactly the reason
        # `table_definition` is. Without this the wide path silently drops it, and a wide table is
        # precisely the one whose grain the narrative helps most.
        carried_narrative[table] = {k: v for k, v in it.metadata.items()
                                    if k in CATALOG_NARRATIVE_KEYS}
        refs: list[str] = []
        for idx, chunk in enumerate(_chunk_profiles(profiles)):
            ref = f"{table}#chunk{idx}"
            refs.append(ref)
            columns_by_ref[ref] = {d.get("column") for d in chunk if d.get("column")}
            chunk_items.append(BatchItem(ref=ref,
                                         metadata={"table": table, "column_profiles": chunk}))
        chunk_refs_by_table[table] = refs

    summaries = run_batched(
        conn, client, short="table_synth", task="table_synth_summary",
        # The phase-1 summary is stamped with the SAME contract generation as the phase-2 synthesis
        # it feeds (`_SYNTH_PROMPT_VERSION` / `_SYNTH_SCHEMA_VERSION` — prompt v5 over the canonical
        # v3 schema), so one Pass B run never egresses under two generations. Read from the
        # constants rather than re-typed: the previous literal 4/3 pair carried a comment still
        # claiming "prompt v3 / canonical schema v2", which is exactly how a drifted copy hides. The
        # summary TEXT itself is unchanged by the bump (it emits no table_role, so there is no vocab
        # to enumerate) — the version identifies the contract, mirroring the Slice-1 v2-aliases-v1
        # schema precedent.
        prompt_id=_SUMMARY_PROMPT_ID, schema_id="overlay_table_synth_summary_batch",
        prompt_version=_SYNTH_PROMPT_VERSION, schema_version=_SYNTH_SCHEMA_VERSION,
        shared_metadata={}, items=chunk_items, out_key="summary",
        instruction=_SUMMARY_INSTRUCTION, accept=make_summary_accept(columns_by_ref), actor=actor,
        extract=lambda e: json.dumps(e.get("summary"), sort_keys=True), ref_aware=True,
        deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
        # C5-T5: each chunk item attributes to its TABLE (the real catalog object), field_names =
        # the chunk's columns — a wide table's summary dispatches stay subject-attributed.
        ingestion_run_id=ingestion_run_id, dispatch_stage="pass_b",
        dispatch_subjects=_dispatch_subjects_for(chunk_items, catalog_source=catalog_source,
                                                 schema_by_table=schema_by_table),
    )

    phase2_items: list[BatchItem] = []
    for table, refs in chunk_refs_by_table.items():
        present = [r for r in refs if r in summaries]
        if not present:
            # ZERO chunks summarized for a wide table -> no synthesis (never a guessed one): the
            # phase-2 call would have no advisory grounding at all. Only this all-missing case drops
            # the table; the caller then reports it honestly as unresolved.
            counters.incr("overlay.table_synth.wide.zero_summaries")
            logger.info("table_synth wide %r summarized 0/%d chunks — no synthesis (honest miss)",
                        table, len(refs))
            continue
        if len(present) < len(refs):
            # SOME chunks summarized: proceed to phase 2 on the LANDED summaries. This is SAFE
            # because the phase-2 item carries the COMPLETE column_roster built from ALL profiles
            # (above), independent of the summaries — a chunk summary is explicitly advisory phase-2
            # input, not a governed fact. Dropping the whole (e.g. 126-col) table because one chunk's
            # summary call was budget-skipped would stamp all five dispositions not_evaluated and
            # fail Pass B, so we synthesize over whatever summaries arrived.
            counters.incr("overlay.table_synth.wide.partial_summaries")
            logger.info("table_synth wide %r summarized %d/%d chunks — synthesizing on the partial "
                        "summaries (roster is complete)", table, len(present), len(refs))
        chunk_summaries = [json.loads(summaries[r]) for r in present]
        metadata: dict = {**carried_narrative.get(table, {}),
                          "table": table, "chunk_summaries": chunk_summaries,
                          "column_roster": roster_by_table[table]}
        if table in table_def_by_table:
            metadata["table_definition"] = table_def_by_table[table]
        phase2_items.append(BatchItem(ref=table, metadata=metadata))
    if not phase2_items:
        return {}
    # `stats` threads to PHASE-2 only (table-granular refs = Pass B's `not_attempted` unit). Phase-1
    # summary truncation is chunk-granular; a wholly-unsummarized wide table already surfaces as
    # unresolved (`wide.zero_summaries`), so it is deliberately not recounted here.
    return _run_synthesis(conn, client, phase2_items, columns_by_table=columns_by_table,
                          actor=actor, instruction=_SYNTH_WIDE_INSTRUCTION,
                          dispositions=dispositions, ingestion_run_id=ingestion_run_id,
                          catalog_source=catalog_source, schema_by_table=schema_by_table,
                          stats=stats, catalog_context_available=catalog_context_available,
                          critic=critic)


_TYPE_FIELDS_NOTE = (
    "Each column profile carries TWO type fields: operational_type is the observed physical type "
    "(it stays 'unknown' until operationally confirmed — an empty or unknown value means the "
    "physical type is NOT established) and declared_type is the glossary-DECLARED SQL type, a HINT "
    "from documentation, not a confirmation of the physical type. Never treat declared_type as the "
    "operational type. When present, table_definition is the curated business definition of the "
    "whole table. "
    # Prompt v5 (Task 7b): the model was being handed this prose with nothing saying what it is,
    # what GRAIN it describes, or that it must not be treated as settled fact.
    "When present, catalog_description / catalog_business_context / catalog_display_name / "
    "catalog_business_domains describe the WHOLE CATALOG this table belongs to, not this table — "
    "they are prose a person typed to say what the dataset is and why the business keeps it, and "
    "catalog_narrative_authority names who said it. Use them to interpret what a row of this table "
    "represents; they are CONTEXT, never a fact about this table and never a substitute for the "
    "columns. "
)

# Prompt v3 ([F1]): the accepted table_role values are enumerated in the PROMPT (and enforced
# per-field in `make_ref_accept`) — never as an enum on the canonical response schema, which would
# whole-reject a synthesis over one off-vocab role.
_ROLE_VOCAB_NOTE = (
    "table_role MUST be one of: " + ", ".join(table_vocab.TABLE_ROLE_ENUM) +
    " (any other value is discarded); event_or_snapshot MUST be event or snapshot. "
)

# Prompt v4 (profile Task 4): the four PROFILE suggestions, their closed vocabularies, and the two
# rules that make them evidence-bound rather than decorative — cite from the given context, and
# never paraphrase curated text into a competing description. The vocabularies are interpolated
# from `profile_vocab` (never re-typed), so a member added there re-versions the prompt by
# construction instead of silently drifting out of it.
_PROFILE_VOCAB_NOTE = (
    "authority_role MUST be one of: "
    + ", ".join(sorted(m.value for m in _AuthorityRole))
    + "; temporal_storage_model MUST be one of: "
    + ", ".join(sorted(m.value for m in _TemporalStorageModel))
    + " (any other value is discarded). ")

_PROFILE_NOTE = (
    "ALSO suggest, where the evidence supports it: table_description (one plain sentence describing "
    "what one row of this table IS); business_context (why the business keeps this dataset); "
    "authority_role (how authoritative this COPY is); and temporal_storage_model (how it stores "
    "history). "
    "EVERY suggestion must cite its evidence in `evidence_refs` as {field, refs}, naming ONLY "
    "columns from the provided list, the literal 'table_definition', or one of the catalog "
    "narrative keys when the item carries them ('catalog_description', 'catalog_business_context', "
    "'catalog_display_name', 'catalog_business_domains') — a suggestion citing nothing is "
    "discarded. "
    "When a table_definition is already provided it is CURATED and stays current: offer a "
    "table_description only as an ALTERNATIVE for human review, never as a correction, and NEVER "
    "paraphrase the curated text back. Omit any field the evidence does not settle — an omission is "
    "an honest abstention and is always preferred to a guess. "
)

_INSTRUCTION_HEAD = (
    "For each table, identify: the grain (the minimal set of columns whose combination uniquely "
    "identifies one row) — RETURN AN EMPTY grain_columns list if you cannot determine it, do not "
    "guess; the as-of/availability column and its basis (posted_at|ingested_at); "
    "the primary business entity; the table role; and whether it is an event or snapshot table. "
)


_INSTRUCTION = (_TYPE_FIELDS_NOTE + _INSTRUCTION_HEAD + _ROLE_VOCAB_NOTE + _PROFILE_VOCAB_NOTE
                + _PROFILE_NOTE + "Only name columns that appear in the provided column list.")

_SUMMARY_INSTRUCTION = (
    _TYPE_FIELDS_NOTE +
    "For each column CHUNK, SUMMARIZE the columns to support a LATER whole-table synthesis — DO NOT "
    "propose a table grain here. Identify: candidate grain/identifier columns (columns that could help "
    "uniquely identify a row), temporal/as-of columns (event or load timestamps), entity signals "
    "(the business entities these columns describe), and whether the chunk looks like event or "
    "snapshot data. Only name columns that appear in the provided column list."
)

_WIDE_HEAD = (
    "This is a WIDE table presented as per-chunk SUMMARIES (each with candidate grain/id columns, "
    "temporal/as-of columns, entity signals, and an event/snapshot hint) PLUS the table's COMPLETE "
    "column roster (each entry an object {column, operational_type, declared_type, concept?} — the "
    "same two type fields described above, plus the resolved concept where one exists). Using the "
    "summaries and the roster, identify for the WHOLE table: the grain (the minimal set of columns "
    "whose combination uniquely identifies one row) — RETURN AN EMPTY grain_columns list if you "
    "cannot determine it, do not guess; the as-of/availability column and its basis "
    "(posted_at|ingested_at); the primary business entity; the table role; and whether it is an "
    "event or snapshot table. "
)


_SYNTH_WIDE_INSTRUCTION = (_TYPE_FIELDS_NOTE + _WIDE_HEAD + _ROLE_VOCAB_NOTE + _PROFILE_VOCAB_NOTE
                           + _PROFILE_NOTE + "Only name columns that appear in the column roster.")


# The folded fact states in which a Pass B proposal is SKIPPED QUIETLY — a stronger/active claim
# already governs this key: VERIFIED (a declared/structural or human-confirmed fact — Pass B must
# never contest it), or a still-pending proposal/partial (DRAFT / PARTIALLY_CONFIRMED — already in
# the queue; DRAFT is the folded literal for a pending proposal, state.py). All OTHER states
# (REJECTED / REVERIFY / STALE / empty) are handed to propose_fact, which adjudicates: it duplicate-
# denies an identical pending fingerprint, sticky-denies a re-proposed rejected fingerprint, and
# ALLOWS a genuinely new value after a terminal state. We never skip on raw stream existence (that
# would suppress every future proposal once a stream existed, even after rejection/expiry).
_SKIP_QUIET_STATES = frozenset({"VERIFIED", "DRAFT", "PARTIALLY_CONFIRMED"})

# The advisory table-level fields Pass B records as LLM field evidence (never governed facts).
# Profile Task 4 adds the three PERSISTABLE profile suggestions. `table_description` persists under
# the table's `definition` field — the same field a source-authored table term writes — which is
# exactly why `_accept_profile_fields` withholds it (disposition `superseded`) whenever the item
# carried a curated `table_definition`: an accepted description only ever fills an ABSENCE, it never
# becomes competing evidence against curated text.
_ADVISORY_TABLE_FIELDS = ("table_role", "primary_entity", "event_or_snapshot",
                          "table_description", "business_context", "authority_role",
                          "temporal_storage_model")
#: synthesis key -> the `field_evidence` field_name it persists under (identity where absent).
_ADVISORY_FIELD_NAMES = {"table_description": "definition"}


def _mark_staled(dispositions: list[dict] | None, table: str, field: str, *,
                 status_if_missing: str) -> None:
    """Set ``prior_value_staled=True`` on the ``{table, field}`` disposition record ([F9] — driven
    by the staled COUNT in BOTH directions: a present value superseding older LLM rows AND a
    dropped/absent field retiring them). Finds the record ``make_ref_accept`` appended for this run
    (searched newest-first); a caller that never threaded the collector through the accept gets an
    appended record with ``status_if_missing`` so the flag is never silently lost."""
    if dispositions is None:
        return
    for rec in reversed(dispositions):
        if rec.get("table") == table and rec.get("field") == field:
            rec["prior_value_staled"] = True
            return
    dispositions.append({"table": table, "field": field, "status": status_if_missing,
                         "reason": None, "prior_value_staled": True})


def _active_skip_state(conn, ref, fact_type) -> str | None:
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact

    stream = load_fact(conn, fact_key(ref, fact_type))
    if not stream:
        return None
    status = fold_overlay_state(stream).status
    return status if status in _SKIP_QUIET_STATES else None


def _propose_table_facts(conn, source: str, syntheses: dict[str, dict], *, actor,
                         source_snapshot_id: str,
                         schema_by_table: dict[str, str] | None = None,
                         dispositions: list[dict] | None = None,
                         now: datetime | None = None,
                         source_uploader: str | None = None) -> None:
    """Route Pass B grain/availability candidates into governed PROPOSED-only facts and advisory
    table-field evidence. Fail-soft (never aborts the upload). Skips QUIETLY only when a stronger
    active claim governs the key (VERIFIED / a pending proposal); otherwise lets propose_fact
    adjudicate re-proposal after a terminal state, logging any denial as a conflict diagnostic.

    ``actor`` MUST be the service actor (``_ENRICH_ACTOR``) so a human confirmer later satisfies
    four-eyes. ``source_uploader`` is the uploading HUMAN principal's subject (or None): the
    grain/availability operands are shaped by the uploader's own file, so the proposal records that
    principal and ``confirm_fact`` bars them from confirming it single-handedly (program-audit F10
    — the same M-7 SOURCE-provenance rule as the semantic-binding surface). ``source_snapshot_id``
    keys producer-scoped staleness for the advisory evidence (a NOT-NULL column).

    ``schema_by_table`` maps a NORMALIZED table name to the real (non-public) schema its glossary
    column decisions are keyed under. The advisory table-field evidence MUST be keyed under that SAME
    schema so ``readiness`` (schema-aware) sees ONE ``(schema, table)`` pair per physical table — a
    schema-forced-public advisory ref otherwise manufactures a phantom ``(public, table)`` twin that
    double-counts the grain/availability/join requirements and makes a bare TABLE subset ambiguous.
    Empty / absent (a non-glossary technical upload) falls back to ``public``, which is correct —
    technical columns are public and write no glossary column decisions. NOTE: the grain/availability
    FACT stays keyed under the always-public ``table_ref`` (below); only the advisory field evidence
    ref is schema-aligned.

    ``dispositions`` is the SAME per-run collector list ``make_ref_accept`` appended to during
    validation: [F9] flips ``prior_value_staled=True`` on the matching ``{table, field}`` record
    whenever this pass ACTUALLY staled prior LLM rows — a present value superseding an older one
    AND a dropped/absent field retiring them (both driven by the staled COUNT, decoupled from the
    clear-gate below).

    STALE-VALUE LIFECYCLE (Slice-2 Task 2): an advisory field the new synthesis NO LONGER carries
    gets its prior LLM evidence producer-scope staled here; when NO active evidence remains for the
    field (no human/source confirmation keeping it alive), ``stale_and_clear_field`` records a
    STALED decision (supersedes read from the durable decision log, [F2]) and CLEARS the flat
    ``graph_node`` display column. This runs BEFORE the caller's ``resolve_and_project`` in the
    SAME transaction (the ingest Pass B savepoint), which then SKIPS the evidence-less field —
    the clear is never re-projected away.

    ``now`` is the ingest round's threaded decision timestamp, passed through to
    ``stale_and_clear_field`` so the STALED decision carries the SAME ``now`` as the round's
    sibling RESOLVED decisions (the monotonic-ordering contract of ``read_field_decisions``).
    ``None`` (a direct caller) keeps the prior wall-clock behavior."""
    # Imported lazily (mirrors _propose_governed_joins): propose_fact resolves the catalog adapter
    # at import-use time, and the pure assembler/accept tests must import this module without
    # pulling the command stack (or ingest, which imports table_synth lazily in the Pass B block).
    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.catalog import current_catalog_adapter
    from featuregen.overlay.commands import propose_fact
    from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
    from featuregen.overlay.field_evidence import stale_source_evidence
    from featuregen.overlay.identity import proposal_fingerprint
    from featuregen.overlay.upload.enrich_llm import ENRICHMENT_RUN_ID
    from featuregen.overlay.upload.field_resolution import (
        _active_field_names,
        stale_and_clear_field,
    )
    from featuregen.overlay.upload.ingest import _STALE_ALL, _write_producer_field
    from featuregen.overlay.upload.object_ref import normalize_ref
    from featuregen.overlay.upload.upload_catalog import table_ref

    # defense-in-depth: ingest_upload self-ensures the adapter (ensure_upload_catalog_adapter at
    # entry), so this is unreachable in the normal flow — it fail-softs a direct/future caller.
    try:
        current_catalog_adapter()
    except RuntimeError:
        counters.incr("overlay.table_synth.skipped_no_adapter")
        logger.warning("OVERLAY_TABLE_SYNTH on but no catalog adapter registered — skipping.")
        return

    for table, syn in syntheses.items():
        ref = table_ref(source, table)
        for fact_type in ("grain", "availability_time"):
            value = syn.get(fact_type)
            if value is None:
                continue
            skip_state = _active_skip_state(conn, ref, fact_type)
            if skip_state is not None:
                # a stronger/active claim governs this key — Pass B does not contest it
                counters.incr(f"overlay.table_synth.{fact_type}.skipped_{skip_state.lower()}")
                continue
            try:
                # Command needs ALL 6 fields (envelopes.py); mirror _propose_governed_joins exactly.
                # `source_uploader` (when a human uploaded) is the F10 four-eyes provenance.
                args: dict[str, object] = {"ref": ref, "fact_type": fact_type,
                                           "proposed_value": value}
                if source_uploader:
                    args["source_uploader"] = source_uploader
                result = propose_fact(conn, Command(
                    "propose_fact", "overlay_fact", None, args,
                    actor, proposal_fingerprint(value)))
                if result.accepted:
                    counters.incr(f"overlay.table_synth.{fact_type}.proposed")
                else:
                    # propose_fact adjudicated a deny (duplicate fingerprint, sticky-rejected, or a
                    # non-terminal race) — a conflict DIAGNOSTIC, not a silent drop.
                    counters.incr(f"overlay.table_synth.{fact_type}.denied")
                    logger.info("table_synth %s proposal denied for %s.%s: %s",
                                fact_type, source, table, result.denied_reason)
            except Exception:   # noqa: BLE001 — advisory: a proposal error never fails an upload
                counters.incr(f"overlay.table_synth.{fact_type}.error")
                logger.exception("table_synth %s proposal errored for %s.%s",
                                 fact_type, source, table)
        # Advisory table fields -> field evidence via the SAME helper Pass A uses
        # (_write_producer_field: producer-scoped staleness + snapshot reuse + the required
        # source_snapshot_id/input_hash args a bare record_field_evidence would miss).
        # RECOMMENDATION-ceilinged in Task 8. A write error here is contained by the caller's
        # Pass B savepoint+except (ingest wiring).
        schema = (schema_by_table or {}).get(table.strip().lower())
        logical_ref = normalize_ref(source, schema, table)
        for synthesis_key in _ADVISORY_TABLE_FIELDS:
            field_name = _ADVISORY_FIELD_NAMES.get(synthesis_key, synthesis_key)
            v = syn.get(synthesis_key)
            if v:
                staled = _write_producer_field(
                    conn, logical_ref=logical_ref, field_name=field_name, value=v,
                    producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
                    producer_ref=ENRICHMENT_RUN_ID, snapshot_id=source_snapshot_id, material=v)
                if staled > 0:
                    # [F9] present-replaces-older: the accepted value superseded prior LLM rows.
                    _mark_staled(dispositions, table, synthesis_key, status_if_missing="accepted")
                continue
            # Dropped/absent advisory field (the stale-value lifecycle): retire ALL of the LLM's
            # prior ACTIVE rows for this field — _STALE_ALL can never equal a real input_hash, so
            # every LLM row stales; other producers' rows (human/source) are NEVER touched.
            n = stale_source_evidence(
                conn, logical_ref=logical_ref, field_name=field_name,
                producer=EvidenceProducer.LLM, keep_input_hash=_STALE_ALL)
            if n > 0:
                # [F9] DECOUPLED from the clear-gate: the LLM rows WERE staled even when a human
                # confirmation below keeps the field alive and blocks the clear.
                _mark_staled(dispositions, table, synthesis_key, status_if_missing="abstained")
                if field_name not in _active_field_names(conn, logical_ref):
                    # NO producer's evidence remains: resolve_and_project would SKIP this field
                    # (it iterates active field names), leaving the prior display visible. Record
                    # the STALED decision + clear the display NOW — same transaction, BEFORE the
                    # caller's resolve_and_project, so the clear is never re-projected away.
                    stale_and_clear_field(
                        conn, source=source, logical_ref=logical_ref, field_name=field_name,
                        now=now)
