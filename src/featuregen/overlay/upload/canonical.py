from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from featuregen.overlay.upload.object_ref import _norm
from featuregen.overlay.upload.read_scope import SENSITIVITY_ROLES

if TYPE_CHECKING:
    from featuregen.overlay.upload.source_profile import SourceCapabilityProfile

_REQUIRED = ("source", "table", "column", "type")
_REQUIRED_NO_TYPE = ("source", "table", "column")   # `type` dropped for a profile that doesn't attest it
_VALID_SENSITIVITY = frozenset({"", *SENSITIVITY_ROLES})   # "" (none) + the recognized tags

# Closed vocabularies for the enumerable canonical fields (a blank means "not declared" and passes).
# A typo would otherwise silently degrade a fan-hint (cardinality), an aggregation-safety signal
# (additivity), or be coerced to the posted_at default (as_of_basis) — quarantine it so the reviewer
# sees it, mirroring the sensitivity check. `as_of_basis` matches ingest's own {posted_at, ingested_at}
# coercion vocab (the fact-level `event_time_plus_lag` is not expressible via the CSV basis column).
_VALID_CARDINALITY = frozenset({"1:1", "1:N", "N:1"})
_VALID_ADDITIVITY = frozenset({"additive", "semi_additive", "non_additive"})
_VALID_AS_OF_BASIS = frozenset({"posted_at", "ingested_at"})

# Per-table column-count bound (#29). The ALWAYS-ON structural assumption is LINEAGE's: it installs
# a whole ANCHOR table even past its MAX_NODES=200 cap ("acceptable under upload governance, where
# table widths are bounded" — lineage.py), so validation upholds that bound here — the single seam
# every ingestion entry (CSV/Excel ingest, glossary, OpenMetadata connector) already flows through.
# Keep the value in LOCKSTEP with lineage.MAX_NODES (test-enforced); raising one requires the other.
# NOT the 64-profile LLM synthesis egress cap (enrich_llm._MAX_COLUMN_PROFILES): that is a flags-on
# concern table_synth enforces itself — a 65..200-column table ingests fine and simply gets no LLM
# table synthesis, whereas quarantining at 64 rejected legitimate wide bank tables (denormalized /
# regulatory extracts) even with no LLM configured.
MAX_COLUMNS_PER_TABLE = 200

# The glossary sentinel for a physical type the source declares but does NOT attest (spec §U). A
# glossary carries meaning, not structure, so its rows are emitted with `type=UNKNOWN_TYPE` — never
# `""` (which quarantines). Under a type-attesting profile (technical, or the no-profile default) this
# sentinel is treated as a missing type; under a glossary profile it passes (a readiness gap, Task 9).
UNKNOWN_TYPE = "unknown"


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    source: str
    table: str
    column: str
    type: str
    is_grain: bool = False
    as_of: bool = False
    as_of_basis: str = ""     # posted_at | ingested_at (how availability is derived)
    definition: str = ""
    sensitivity: str = ""
    joins_to: str = ""        # target "table.column" (single-column join)
    cardinality: str = ""     # N:1 | 1:1 | 1:N
    additivity: str = ""      # additive | semi_additive | non_additive (safe-aggregation)
    unit: str = ""            # e.g. dollars, cents
    currency: str = ""        # e.g. USD
    entity: str = ""          # the business entity this column denotes (Customer, Account)
    # NOT catalog metadata: a reader-recorded cell-parse fault (e.g. an invalid boolean token).
    # Non-empty ⟹ validate_rows quarantines the row with this reason (#18 follow-up) — a per-row
    # cell fault must neither raise in the reader (that 400s the WHOLE upload, unlike an enum typo,
    # which only quarantines its row) nor silently coerce (the original #18 finding).
    parse_error: str = ""


@dataclass(frozen=True, slots=True)
class RowError:
    row_index: int
    message: str
    row: CanonicalRow | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    good: list[CanonicalRow] = field(default_factory=list)
    quarantined: list[RowError] = field(default_factory=list)
    structural_error: str | None = None


def _material(r: CanonicalRow) -> tuple:
    """The load-bearing fields of a row (everything but the advisory `definition`). Two rows for the
    same column that agree here are true duplicates; disagreement is a conflict worth quarantining."""
    return (r.type, r.is_grain, r.as_of, r.as_of_basis, r.sensitivity, r.joins_to,
            r.cardinality, r.additivity, r.unit, r.currency, r.entity)


def validate_rows(rows: list[CanonicalRow],
                  catalog_source: str | None = None,
                  *, profile: SourceCapabilityProfile | None = None) -> ValidationResult:
    """Validate rows for one upload. When `catalog_source` is given (the upload's source), any row
    declaring a DIFFERENT source is quarantined — the upload is single-source (T3), and downstream
    (facts, graph) key object identity on this one source, so a foreign-source row would collide.

    `profile` (spec §U) makes the ONE validator profile-aware — it is NOT a second, forked validator.
    A physical `type` is required only when the profile ATTESTS it: a technical CSV (or the default
    `profile=None` = today's behaviour) requires a NON-EMPTY type; a glossary profile does not attest
    `type`, so an absent/`unknown` type passes as a readiness gap (Task 9), never a quarantine. The
    `UNKNOWN_TYPE` sentinel is interpreted as "no type attested" ONLY under a glossary profile; under a
    type-attesting profile or `profile=None`, a literal `"unknown"` is just a present type value
    (MINOR-6 technical-path parity). EVERY other check — identity present, source-mismatch, sensitivity
    validity, dedup/conflict — is identical across profiles.

    Accepted rows are returned with their identity components (source/table/column, and the joins_to
    target) NORMALIZED by the same strip+lower `_norm` the dedup key uses (#1): the returned row IS
    the operational identity every downstream stage keys on. Quarantined rows keep the raw values."""
    if not rows:
        return ValidationResult(structural_error="empty upload: no rows")
    if all(not r.source for r in rows):
        return ValidationResult(structural_error="no row has a source")

    # `type` is a hard requirement unless the profile explicitly does not attest it (a glossary).
    type_required = profile is None or profile.attests("type")
    required = _REQUIRED if type_required else _REQUIRED_NO_TYPE

    good: list[CanonicalRow] = []
    quarantined: list[RowError] = []
    seen: dict[tuple[str, str, str], tuple[CanonicalRow, int]] = {}  # key -> (first row, its index)
    conflicted: set[tuple[str, str, str]] = set()

    for i, r in enumerate(rows):
        # A physical `type` is missing when the profile requires it AND the cell is empty. The
        # `UNKNOWN_TYPE` sentinel is meaningful ONLY under a non-type-attesting (glossary) profile —
        # where `type` is not required at all and the sentinel passes as a readiness gap. Under a
        # type-attesting profile (technical) OR the default `profile=None`, a literal `"unknown"` is a
        # PRESENT type value like any other (MINOR-6: do not quarantine it — pre-branch behaviour).
        missing = [f for f in required if not str(getattr(r, f)).strip()]   # whitespace-only == missing
        if missing:
            quarantined.append(RowError(i, f"missing required field(s): {', '.join(missing)}", r))
            continue
        if r.parse_error:
            # A reader-detected cell fault (an invalid boolean token, #18 follow-up). Quarantine
            # THIS row with the reader's reason — consistent with the enum-vocabulary checks below;
            # raising in the reader rejected the whole file for one typo'd flag.
            quarantined.append(RowError(i, r.parse_error, r))
            continue
        if catalog_source is not None and _norm(r.source) != _norm(catalog_source):
            quarantined.append(RowError(
                i, f"row source '{r.source}' does not match upload source '{catalog_source}'", r))
            continue
        if r.sensitivity not in _VALID_SENSITIVITY:
            # An unrecognized sensitivity would make the node invisible to EVERY role (fail-closed
            # but silent). Quarantine it instead so the reviewer fixes/normalizes the value.
            quarantined.append(RowError(
                i, f"unrecognized sensitivity '{r.sensitivity}' "
                f"(expected one of: {', '.join(sorted(_VALID_SENSITIVITY - {''}))})", r))
            continue
        if "." in r.table or "." in r.column:
            # The graph/lineage object ref is "public.<table>.<column>" (dot-joined, unescaped). A '.'
            # inside a table or column name mis-parses: two distinct rows can collide on the graph PK
            # (catalog_source, object_ref), or join-path/lineage split on the wrong segment and bind
            # the wrong table. Fail closed here so a dotted name never reaches normalize_ref/build_graph.
            quarantined.append(RowError(
                i, f"table/column name contains the '.' path separator "
                f"({r.table!r}, {r.column!r}); it would corrupt the object reference", r))
            continue
        # Closed-vocabulary check for the enumerable fields (empty = not declared, always allowed). A
        # value outside its set is quarantined for review rather than silently degraded/coerced (#18).
        enum_bad = None
        if r.cardinality and r.cardinality not in _VALID_CARDINALITY:
            enum_bad = (f"unrecognized cardinality '{r.cardinality}' "
                        f"(expected one of: {', '.join(sorted(_VALID_CARDINALITY))})")
        elif r.additivity and r.additivity not in _VALID_ADDITIVITY:
            enum_bad = (f"unrecognized additivity '{r.additivity}' "
                        f"(expected one of: {', '.join(sorted(_VALID_ADDITIVITY))})")
        elif r.as_of_basis and r.as_of_basis not in _VALID_AS_OF_BASIS:
            enum_bad = (f"unrecognized as_of_basis '{r.as_of_basis}' "
                        f"(expected one of: {', '.join(sorted(_VALID_AS_OF_BASIS))})")
        if enum_bad:
            quarantined.append(RowError(i, enum_bad, r))
            continue
        # Key on the SAME strip+lower normalizer as object identity (object_ref._norm): a raw key
        # would let two case-variant rows for ONE physical column (e.g. a pii-tagged 'SSN' + an
        # untagged 'ssn') slip past the fail-closed conflict path below and graph an untagged,
        # world-visible twin of the PII column.
        key = (_norm(r.source), _norm(r.table), _norm(r.column))
        # The ACCEPTED row carries the normalized identity itself (#1): dedup keyed on _norm but rows
        # returned raw let two uploads differing only in case/padding split one physical column into
        # twin graph nodes, false mass drift (0% snapshot overlap -> brake-held as a "wrong source"),
        # and enrichment-cache misses. Every downstream stage (build_graph refs, the drift snapshot,
        # fact payloads, content_hash) inherits THIS one identifier. `joins_to` names a table.column
        # target, so it is the same class of identity and gets the same _norm — otherwise a declared
        # join to 'Accounts.ID' would dangle beside the normalized 'public.accounts.id' node. The
        # RAW row is preserved where the code needs display: quarantined RowErrors (above) keep it.
        canonical = replace(r, source=_norm(r.source), table=_norm(r.table),
                            column=_norm(r.column), joins_to=_norm(r.joins_to))
        if key in seen:
            first_row, first_i = seen[key]
            if _material(first_row) == _material(canonical):
                continue  # same load-bearing metadata (advisory `definition` may differ) -> dedup
            # Same column, differing metadata. Dedup used to keep the FIRST row and silently drop the
            # later one's fields — including `sensitivity`, so a later `pii` tag could be lost and the
            # column left world-readable (fail-OPEN). Fail CLOSED instead: quarantine ALL rows for the
            # column (pull the already-accepted first one back out) so it is NOT graphed until resolved.
            msg = f"conflicting metadata for {key} (rows for the same column disagree)"
            if key not in conflicted:
                conflicted.add(key)
                good.remove(first_row)
                quarantined.append(RowError(first_i, msg, first_row))
            quarantined.append(RowError(i, msg, r))
            continue
        seen[key] = (canonical, i)
        good.append(canonical)

    # Per-table width bound (#29), applied AFTER dedup so it counts the unique ACCEPTED columns per
    # (source, table) — exactly the set that would be graphed. Over the bound, the WHOLE table is
    # quarantined (fail-closed): graphing an arbitrary first-N prefix would misrepresent the table
    # while lineage's whole-table anchor install and the LLM synthesis egress cap still assume the
    # bound holds. Other tables in the same upload are untouched.
    width: dict[tuple[str, str], int] = {}
    for key in seen:
        if key not in conflicted:
            width[key[:2]] = width.get(key[:2], 0) + 1
    too_wide = {tk for tk, n in width.items() if n > MAX_COLUMNS_PER_TABLE}
    if too_wide:
        kept: list[CanonicalRow] = []
        for row in good:
            tk = (row.source, row.table)
            if tk not in too_wide:
                kept.append(row)
                continue
            quarantined.append(RowError(
                seen[(row.source, row.table, row.column)][1],
                f"table '{row.table}' declares {width[tk]} columns, over the "
                f"{MAX_COLUMNS_PER_TABLE}-column per-table bound (lineage assumes bounded "
                f"table widths); the whole table is quarantined — split it or "
                f"reduce its width", row))
        good = kept

    return ValidationResult(good=good, quarantined=quarantined, structural_error=None)
