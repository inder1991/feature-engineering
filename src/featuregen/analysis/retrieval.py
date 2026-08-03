"""Choose which catalog objects a question's prompt may see.

The roadmap's rule is "never put the whole catalog in a prompt", and the reason is not only cost: a
model offered 150,000 columns picks plausible wrong ones, and the wrongness is invisible in the
answer. So a bounded set is retrieved per question, and :mod:`featuregen.analysis.intent` rejects
anything outside it.

**The property that makes a question answerable at all.** A question names CONCEPTS, not columns —
"which customers had fewer transactions this month" mentions neither ``cif_id`` nor ``tran_month``,
and lexical search over definitions will not surface either. But an :class:`AnalysisPlanV1` requires
both: one as ``entity_ref``, one as a window's ``anchor_ref``. Since intent extraction now REJECTS a
ref it was not offered, retrieving by relevance alone would make every question unanswerable — the
model would have nothing legitimate to name.

So the governed **grain** and **as-of** columns of every candidate table are always offered, whatever
they scored, and they are offered FIRST so a budget can never drop them in favour of a better-matching
descriptive column. Getting that ordering backwards produces a set that looks richer and cannot
express a single period-over-period question.

**Truncation is reported, never silent.** A bound that quietly discards half the catalog reads as
"this is everything relevant" — the same defect class as a stage that reports success and produces
nothing. :class:`Retrieval` carries what was dropped.

**Read-scoped throughout.** Every leg goes through ``allowed_sensitivities(roles)``, so a column the
caller may not see is never offered to a model on their behalf. That is the same seam as the governed
read-scope fix, and it matters more here: this set becomes prompt text.

**Four legs, in this order (interface doc D12.2).** The plan proposed inverting them; the ordering
below is the shipped expressibility invariant and the inversion is the DEFECT documented above:

1. the governed **grain/as-of** columns of every candidate table — unchanged, and first;
2. **lexical** relevance;
3. **controlled semantic expansion** — concept names and their registry ancestors, domains,
   sub-domains, entities, glossary terms and the D13.1 BIAN/process paths of what legs 1-2 already
   found, re-queried through the SAME read-scoped full-text search. Exact vocabulary, never
   embeddings: an approximate neighbour that nothing sanctioned is how a model gets offered a
   column from a table nobody said was related;
4. a bounded **one-hop available-link neighbourhood** over the shipped
   ``available_identifier_links`` reader, with both endpoints re-scoped.

**The offered set stays CLOSED.** Legs 3 and 4 add read-scoped refs to ``IntentCandidates`` BEFORE
the model is called; they never let an output ref bypass ``validate_intent``. A ref this module did
not offer is still rejected, which is the property that makes a hallucinated column a bounded
repair rather than a plan that grounds against nothing.

**Truncation is reported PER LEG.** One aggregate count could not say whether the answer was
narrowed by relevance or by a link budget, and those call for different actions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from featuregen.analysis.intent import IntentCandidates
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.search import MATCH_ANY, search

#: The four legs, in execution order. Named so a report cannot invent a fifth.
LEG_GRAIN_TIME = "grain_time"
LEG_LEXICAL = "lexical"
LEG_SEMANTIC_EXPANSION = "semantic_expansion"
LEG_LINK_NEIGHBOURHOOD = "link_neighbourhood"
LEGS: tuple[str, ...] = (LEG_GRAIN_TIME, LEG_LEXICAL, LEG_SEMANTIC_EXPANSION,
                         LEG_LINK_NEIGHBOURHOOD)

#: Expansion vocabulary comes from these flat ``graph_node`` columns. Every one is a CONTROLLED
#: platform value — a registry concept, a governed domain, an entity token, the source's own
#: BIAN/process path (D13.1), the glossary-projected term text, the D13.2 LLM sub-domain. None of
#: them is user text, which is why they can be fed back into the query without a second redaction.
_EXPANSION_COLUMNS = ("concept", "domain", "sub_domain", "entity", "semantic_terms",
                      "bian_path", "process_path")

#: The TABLE-grain profile vocabulary (profile Task 5), harvested only while
#: `FEATUREGEN_DATASET_PROFILES` is on. A question is usually asked in the words of the DATASET
#: ("settlement ledger", "system of record") rather than in a column's, and these five are where
#: those words live. All are rebuildable display projections; none is consulted as authority.
_TABLE_PROFILE_COLUMNS = ("definition", "business_context", "domain", "data_role",
                          "primary_entity")

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class RetrievalBudget:
    """How much of the catalog may reach one prompt.

    Deliberately small. The defaults are a judgement, not a measurement — they are the point at which
    a prompt stays reviewable by a human, and they are exposed so a deployment can tune them with
    evidence rather than by editing this module.
    """

    max_columns: int = 60
    max_tables: int = 6
    #: Lexical hits to consider before the budget is applied. Wider than `max_columns` on purpose:
    #: the grain/as-of columns claim their places first, and the remainder is filled from these.
    lexical_limit: int = 80
    #: Leg 3: how many CONTROLLED vocabulary tokens may be re-queried, and how many extra tables
    #: the expansion may bring in. Both deliberately small — expansion is meant to reach the table
    #: a question implies, not to widen the catalog until something matches.
    max_expansion_terms: int = 24
    max_expansion_tables: int = 3
    #: Leg 4: how many one-hop link partners may be offered. A bank catalog's customer hub reaches
    #: almost everything, so this is a hard stop, not a heuristic.
    max_link_neighbours: int = 10
    #: How many SEMANTIC BUNDLES ride to intent extraction. Far below `max_columns`: a bundle per
    #: offered ref would be both a large prompt and a per-column read fan-out, and the columns that
    #: need explaining are the structural ones plus the best lexical matches.
    max_context_bundles: int = 12


@dataclass(frozen=True, slots=True)
class LegReportV1:
    """What ONE retrieval leg found and what it could not fit.

    Per leg, because one aggregate number cannot distinguish "relevance narrowed this" from "the
    link budget stopped this", and the two call for different actions from a user."""

    leg: str
    considered: int = 0
    offered: int = 0
    dropped: int = 0

    @property
    def truncated(self) -> bool:
        return self.dropped > 0

    def as_dict(self) -> dict:
        return {"leg": self.leg, "considered": self.considered, "offered": self.offered,
                "dropped": self.dropped, "truncated": self.truncated}


@dataclass(frozen=True, slots=True)
class Retrieval:
    """The bounded set, plus what it cost to bound it."""

    candidates: IntentCandidates
    tables_considered: tuple[str, ...] = ()
    #: Columns that matched but did not fit the budget. Non-zero means the answer may rest on a
    #: narrower view of the catalog than exists — worth telling a user, not swallowing. Kept as the
    #: aggregate it always was; `legs` is what says WHICH bound bit.
    dropped_columns: int = 0
    #: Set when NO table could be identified: the question matched nothing the caller may read.
    empty_reason: str = ""
    #: Per-leg accounting (D12.2). Always all four legs, in order, even when a leg contributed
    #: nothing — an absent entry cannot distinguish "found nothing" from "never ran".
    legs: tuple[LegReportV1, ...] = ()
    #: The CONTROLLED vocabulary leg 3 expanded on, for explanation. Never user text.
    expansion_terms: tuple[str, ...] = ()
    #: Bounded `SemanticContextBundleV1.for_analysis_planning` projections for the most
    #: load-bearing offered refs, each carrying its D2 authority axes and missing-context codes.
    context_bundles: tuple[dict, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.candidates.column_refs


def _column_ref(source: str, table: str, column: str) -> str:
    """``source::table.column`` — the form `grounding._parse` resolves.

    It reads the LAST two dotted segments and looks up the flattened ``public.<table>.<column>``, so
    the schema is deliberately omitted: including it would still parse, but it would be a second
    spelling of one identity and the two would drift.
    """
    return f"{source}::{table}.{column}"


def _table_ref(source: str, table: str) -> str:
    return f"{source}::{table}"


def _structural_columns(conn, pairs: Iterable[tuple[str, str]], *,
                        roles: Iterable[str]) -> list[tuple[str, str, str, bool, bool]]:
    """The governed grain and as-of columns of the given (source, table) pairs.

    One query for all of them rather than one per table: a per-item query inside a loop is the defect
    that cost this codebase 157 catalog scans per enrichment pass, and the shape is easy to repeat.
    """
    pairs = sorted(set(pairs))
    if not pairs:
        return []
    sources = sorted({source for source, _ in pairs})
    tables = sorted({table for _, table in pairs})
    # `visible_requires`, NOT the raw `sensitivity` tag. The tag is what the file declared; the
    # governed floor the concept cascade derived lives in the generated column, and filtering on the
    # tag is the leak fixed in 9766c415 — 28 FTR columns including an Emirates ID number were
    # readable by anyone holding `catalog:read`. It matters more here than anywhere: this set becomes
    # prompt text, so the raw predicate would put a national ID in front of a model.
    rows = conn.execute(
        "SELECT catalog_source, table_name, column_name, is_grain, is_as_of FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = ANY(%s) AND table_name = ANY(%s) "
        "  AND (is_grain OR is_as_of) "
        "  AND COALESCE(visible_requires, '{}') <@ %s "
        "ORDER BY catalog_source, table_name, column_name",
        (sources, tables, allowed_sensitivities(roles))).fetchall()
    # The ANY/ANY cross-product can match a table name that exists under a different source, so the
    # pairs are re-checked rather than trusted.
    wanted = set(pairs)
    return [(s, t, c, g, a) for s, t, c, g, a in rows if (s, t) in wanted]


#: Bound on the terms a refusal records as the thing nobody could resolve.
_MAX_GAP_TERMS = 8


def record_retrieval_gap(conn, question: str, *, roles: Iterable[str] = (),
                         analysis_request_id: str, now: datetime) -> str | None:
    """Record a retrieval refusal as the ONE actionable ontology gap it is: nobody has a term for
    what was asked (``SEMANTIC_TERM_UNRESOLVED`` / ``DEFINE_SEMANTIC_TERM``).

    This is what finally gives the learning store a PRODUCTION producer. `record_gap` was reachable
    only through `run_analysis`, which has no caller, so `GET /learning/gaps` read a table nothing
    populated — metadata rot, and the reason the plan gated this behind "do not write unreachable
    metadata". The retrieval path is reached on every planning request, so writing here is the
    opposite: a real, recurring signal about a real gap in the vocabulary.

    THE SUBJECT IS REDACTED. A question is free text from a person ("transactions for Ahmed
    Al-Mansouri"), and `subject_refs` is durable. The terms are taken from the REDACTED text, so a
    customer name can never become an ontology candidate; they are bounded, lower-cased and sorted
    so the same unanswered question dedupes to one gap however it was typed.

    Returns the event id, or ``None`` when the question yielded no recordable term (a refusal with
    no subject is not actionable, and inventing one would be worse than recording nothing)."""
    from featuregen.data_agent.learning import (
        AnalysisLearningEventV1,
        LearningStage,
        RequiredAction,
        record_gap,
    )
    from featuregen.intake.redaction import redact_free_text

    redaction = redact_free_text(question)
    if redaction.text is None:      # the redactor failed closed — nothing safe to persist
        return None
    terms = sorted({t for t in _TOKEN_RE.findall(redaction.text.lower()) if len(t) >= 3})
    if not terms:
        return None
    return record_gap(conn, AnalysisLearningEventV1(
        analysis_request_id=analysis_request_id,
        stage=LearningStage.PLANNING,
        code="SEMANTIC_TERM_UNRESOLVED",
        subject_refs=tuple(terms[:_MAX_GAP_TERMS]),
        required_action=RequiredAction.DEFINE_SEMANTIC_TERM,
        dependency_snapshot_id=_catalog_snapshot_id(conn, roles=roles),
    ), now=now)


def _catalog_snapshot_id(conn, *, roles: Iterable[str]) -> str:
    """A deterministic id for the catalog state this refusal was reached under.

    Re-asking an unanswerable question against the SAME catalog is not new information (the gap
    dedupes); re-asking after an upload IS, because the gap may have been resolved. The drift
    watermarks are exactly that boundary, so they are what the id is derived from."""
    import hashlib

    rows = conn.execute(
        "SELECT catalog_source, last_completed_at FROM overlay_drift_watermark "
        "ORDER BY catalog_source").fetchall()
    material = "|".join(f"{source}@{stamp.isoformat() if stamp else ''}" for source, stamp in rows)
    return "cat-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _expansion_terms(conn, refs: Sequence[tuple[str, str, str]], *, roles: Iterable[str],
                     limit: int) -> tuple[str, ...]:
    """Leg 3's CONTROLLED vocabulary, read from the flat display columns of what legs 1-2 found.

    One query, not one per column — the 157-scan defect class is easy to repeat here. Every source
    column is a platform-controlled value (registry concept, governed domain, entity token, the
    source's own BIAN/process path, the glossary term projection, the D13.2 sub-domain), so the
    tokens can be fed back into the SAME full-text query without a second redaction pass: nothing
    here is user text, and nothing here is an embedding.

    Read-scoped like every other leg — a term is only harvested from a column the caller can see,
    or the expansion would leak the vocabulary of a restricted column."""
    if not refs:
        return ()
    sources = sorted({s for s, _t, _c in refs})
    tables = sorted({t for _s, t, _c in refs})
    rows = conn.execute(
        f"SELECT catalog_source, table_name, column_name, {', '.join(_EXPANSION_COLUMNS)} "
        "FROM graph_node WHERE kind = 'column' AND catalog_source = ANY(%s) "
        "  AND table_name = ANY(%s) AND COALESCE(visible_requires, '{}') <@ %s",
        (sources, tables, allowed_sensitivities(roles))).fetchall()
    wanted = set(refs)
    terms: list[str] = []
    seen: set[str] = set()

    def _harvest(value) -> None:
        if not value:
            return
        for token in _TOKEN_RE.findall(str(value).lower()):
            # One-character tokens carry no meaning and two-character ones are mostly noise
            # ("of", "l1"); a bounded vocabulary is the point of a CONTROLLED expansion.
            if len(token) < 3 or token in seen:
                continue
            seen.add(token)
            terms.append(token)

    for row in rows:
        if (row[0], row[1], row[2]) not in wanted:
            continue
        for value in row[3:]:
            _harvest(value)
    # The TABLE profile (profile Task 5): description, business context, domain, data role and
    # primary entity are meaning-bearing vocabulary about the dataset a column lives in, and the
    # question is usually asked in those words rather than in a column's. Flag-gated, so retrieval
    # with `FEATUREGEN_DATASET_PROFILES` off expands on exactly what it expanded on before.
    from featuregen.overlay.upload.profile_vocab import dataset_profiles_enabled

    if dataset_profiles_enabled():
        for row in conn.execute(
            f"SELECT {', '.join(_TABLE_PROFILE_COLUMNS)} FROM graph_node "
            "WHERE kind = 'table' AND catalog_source = ANY(%s) AND table_name = ANY(%s)",
            (sources, tables)).fetchall():
            # Table nodes carry `visible_requires = {}`, so their scope is DERIVED: this harvest is
            # reached only for tables whose COLUMNS the caller already passed scope on above.
            for value in row:
                _harvest(value)
    # Deterministic order so the same catalog state produces the same prompt.
    return tuple(sorted(terms)[:limit])


def _link_neighbours(conn, refs: Sequence[str], *, roles: Iterable[str],
                     limit: int) -> tuple[list[tuple[str, str, str]], int]:
    """Leg 4: the ONE-HOP available-link partners of the offered columns, both endpoints re-scoped.

    Uses the shipped ``available_identifier_links`` reader — no second lifecycle fold, no second
    notion of what "available" means. Returns ``(partners, considered)`` where a partner is a
    ``(source, table, column)`` the caller can PROVABLY see: a partner absent from the graph is not
    provably visible, so it is dropped rather than offered (fail closed — a link must never become
    an existence oracle for a column the caller may not read)."""
    from featuregen.overlay.upload.bridge_assessment import available_identifier_links
    from featuregen.overlay.upload.object_ref import parse_ref

    allowed = allowed_sensitivities(roles)
    found: list[tuple[str, str, str]] = []
    considered = 0
    seen: set[tuple[str, str, str]] = set()
    mine = set(refs)
    for ref in refs:
        for link in available_identifier_links(conn, object_ref=ref):
            for endpoint in (link.assessment.left_endpoint, link.assessment.right_endpoint):
                for member in endpoint.members:
                    m_source, m_schema, m_table, m_column = parse_ref(member.logical_column_ref)
                    if m_column is None:
                        continue
                    flat = f"{m_schema}.{m_table}.{m_column}"
                    if flat in mine or (m_source, m_table, m_column) in seen:
                        continue
                    considered += 1
                    seen.add((m_source, m_table, m_column))
                    visible = conn.execute(
                        "SELECT 1 FROM graph_node WHERE kind = 'column' "
                        "AND catalog_source = %s AND lower(object_ref) = %s "
                        "AND COALESCE(visible_requires, '{}') <@ %s",
                        (m_source, flat.lower(), allowed)).fetchone()
                    if visible is None:
                        continue
                    found.append((m_source, m_table, m_column))
    found.sort()
    return found[:limit], considered


def _context_bundles(conn, refs: Sequence[tuple[str, str, str]], *, roles: Iterable[str],
                     limit: int) -> tuple[dict, ...]:
    """Bounded `for_analysis_planning` projections for the most load-bearing offered refs, each
    carrying the D2 authority axes and the closed missing-context codes.

    The SAME bundle contract feature generation and the Context tab consume — so the three surfaces
    cannot disagree about what a column means. Bounded hard: a bundle per offered ref would be both
    a large prompt and a per-column read fan-out, and the refs that need explaining are the
    structural ones plus the best lexical matches.

    A ref whose bundle cannot be assembled (it vanished, or read scope narrowed) is SKIPPED — the
    offered set is unaffected, because context is explanation and its absence must never silently
    change what may be named."""
    from featuregen.overlay.upload.semantic_context import bundle_from_store, for_analysis_planning

    out: list[dict] = []
    for source, table, column in refs[:limit]:
        try:
            bundle = bundle_from_store(conn, source, f"public.{table}.{column}", roles=roles)
        except Exception:   # noqa: BLE001 — context is explanation; never a retrieval failure
            continue
        payload = for_analysis_planning(bundle)
        # The offered ref, in the SAME spelling `validate_intent` matches on. The bundle re-keys by
        # the schema-preserving logical ref, and two spellings of one identity in one prompt is how
        # a model names the wrong one.
        payload["ref"] = _column_ref(source, table, column)
        payload["authority"] = {
            v.field_name: f"{v.evidence[0].producer}/{v.evidence[0].strength}"
            for v in bundle.resolved_semantics if v.evidence
        }
        out.append({k: v for k, v in payload.items() if v not in (None, "", [], {})})
    return tuple(out)


def retrieve_candidates(conn, question: str, *, now: datetime,
                        roles: Iterable[str] = (),
                        budget: RetrievalBudget | None = None,
                        fresh_within: timedelta = timedelta(hours=24)) -> Retrieval:
    """The catalog objects one question may be planned against.

    Two legs, in priority order:

    1. the governed grain and as-of columns of every table the question touched — always offered,
       because the plan cannot be expressed without them and the question never names them;
    2. the lexical matches themselves, filling whatever budget remains.

    Legs 3 (controlled semantic expansion) and 4 (a bounded one-hop available-link neighbourhood)
    ADD to the offered set before the model is called; they never let an output ref bypass
    ``validate_intent``. Neither uses embeddings: an approximate neighbour that nothing sanctioned
    is precisely the join-identity mistake the planning layer exists to catch.
    """
    budget = budget or RetrievalBudget()

    # MATCH_ANY, not the default. `plainto_tsquery` ANDs its terms, which is right for a keyword
    # search box and useless for a whole question: "transaction value by customer" would require all
    # three words in ONE column's document, so retrieval returned nothing for every question asked.
    hits = search(conn, question, now=now, roles=roles, fresh_within=fresh_within,
                  limit=budget.lexical_limit, match=MATCH_ANY).hits
    columns = [h for h in hits if h.kind == "column" and h.column]
    if not columns:
        return Retrieval(
            candidates=IntentCandidates(column_refs=frozenset(), table_refs=frozenset()),
            empty_reason="no readable catalog column matched this question",
            legs=tuple(LegReportV1(leg=leg) for leg in LEGS))

    # Tables ranked by their best-scoring column, so the budget keeps the most relevant TABLES whole
    # rather than a scattering of columns from many.
    table_order: list[tuple[str, str]] = []
    for hit in columns:                                   # `search` returns score-ordered hits
        pair = (hit.catalog_source, hit.table)
        if pair not in table_order:
            table_order.append(pair)
    kept_tables = table_order[:budget.max_tables]
    kept = set(kept_tables)

    refs: list[str] = []
    parts: dict[str, tuple[str, str, str]] = {}   # ref -> (source, table, column)
    labels: dict[str, str] = {}

    def _add(source: str, table: str, column: str, label: str = "", *, force: bool = False) -> bool:
        """Offer one column. ``force`` bypasses the column budget and is used by LEG 1 ONLY.

        That is the module's founding invariant, not a convenience: the governed grain and as-of
        columns are always offered whatever they scored, because an `AnalysisPlanV1` requires both
        and the question never names them. A budget that could drop them produces a candidate set
        that looks richer and cannot express a single period-over-period question."""
        ref = _column_ref(source, table, column)
        if ref in parts:
            return False
        if not force and len(refs) >= budget.max_columns:
            return False
        refs.append(ref)
        parts[ref] = (source, table, column)
        if label:
            labels.setdefault(ref, label)
        return True

    # ── Leg 1 FIRST — see the module docstring. These are what make a plan expressible. ──────────
    grain_refs: set[str] = set()
    as_of_refs: set[str] = set()
    structural_refs: list[tuple[str, str, str]] = []
    for source, table, column, is_grain, is_as_of in _structural_columns(conn, kept_tables,
                                                                        roles=roles):
        ref = _column_ref(source, table, column)
        _add(source, table, column, force=True)
        structural_refs.append((source, table, column))
        # The role travels with the ref so a clarification can ask "which column identifies the
        # customer?" and list the two identifiers rather than all sixty columns.
        (grain_refs if is_grain else as_of_refs).add(ref)
    structural = len(refs)
    leg1 = LegReportV1(leg=LEG_GRAIN_TIME, considered=len(structural_refs), offered=structural)

    # ── Leg 2 — lexical relevance, filling what is left. ────────────────────────────────────────
    considered = 0
    lexical_refs: list[tuple[str, str, str]] = []
    before = len(refs)
    for hit in columns:
        if (hit.catalog_source, hit.table) not in kept:
            continue
        considered += 1
        if _add(hit.catalog_source, hit.table, hit.column, hit.concept or hit.column or ""):
            lexical_refs.append((hit.catalog_source, hit.table, hit.column))
    for hit in columns:
        ref = _column_ref(hit.catalog_source, hit.table, hit.column)
        if ref in parts:
            labels.setdefault(ref, hit.concept or hit.column or "")
    leg2 = LegReportV1(leg=LEG_LEXICAL, considered=considered, offered=len(refs) - before,
                       # Only the relevance leg can overflow at this point; the structural columns
                       # are never dropped, so a negative difference would mean leg 1 alone
                       # exceeded the budget — reported as zero, not as a nonsensical count.
                       dropped=max(0, structural + considered - len(refs)))

    # ── Leg 3 — CONTROLLED semantic expansion over what legs 1-2 found. ─────────────────────────
    terms = _expansion_terms(conn, list(parts.values()), roles=roles,
                             limit=budget.max_expansion_terms)
    expansion_considered = expansion_offered = 0
    expansion_tables: list[tuple[str, str]] = []
    if terms:
        expansion_hits = [
            h for h in search(conn, " ".join(terms), now=now, roles=roles,
                              fresh_within=fresh_within, limit=budget.lexical_limit,
                              match=MATCH_ANY).hits
            if h.kind == "column" and h.column
        ]
        for hit in expansion_hits:
            pair = (hit.catalog_source, hit.table)
            if pair not in kept and pair not in expansion_tables:
                if len(expansion_tables) >= budget.max_expansion_tables:
                    continue
                expansion_tables.append(pair)
            if _column_ref(*[hit.catalog_source, hit.table, hit.column]) in parts:
                continue
            expansion_considered += 1
            if _add(hit.catalog_source, hit.table, hit.column, hit.concept or hit.column or ""):
                expansion_offered += 1
    leg3 = LegReportV1(leg=LEG_SEMANTIC_EXPANSION, considered=expansion_considered,
                       offered=expansion_offered,
                       dropped=expansion_considered - expansion_offered)

    # ── Leg 4 — a bounded ONE-HOP available-link neighbourhood. ─────────────────────────────────
    flat_refs = [f"public.{table}.{column}" for _s, table, column in parts.values()]
    partners, link_considered = _link_neighbours(conn, flat_refs, roles=roles,
                                                 limit=budget.max_link_neighbours)
    link_offered = 0
    link_tables: list[tuple[str, str]] = []
    for source, table, column in partners:
        if (source, table) not in kept and (source, table) not in expansion_tables \
                and (source, table) not in link_tables:
            link_tables.append((source, table))
        if _add(source, table, column):
            link_offered += 1
    leg4 = LegReportV1(leg=LEG_LINK_NEIGHBOURHOOD, considered=link_considered,
                       offered=link_offered, dropped=max(0, link_considered - link_offered))

    all_tables = [*kept_tables, *expansion_tables, *link_tables]
    # The bundles explain the STRUCTURAL columns first (they are what a plan cannot omit), then the
    # best lexical matches — the same priority the offered set itself is built on.
    bundle_refs = structural_refs + [r for r in lexical_refs if r not in set(structural_refs)]
    return Retrieval(
        candidates=IntentCandidates(
            column_refs=frozenset(refs),
            table_refs=frozenset(_table_ref(s, t) for s, t in all_tables),
            labels={ref: label for ref, label in sorted(labels.items()) if ref in parts},
            grain_refs=frozenset(grain_refs), as_of_refs=frozenset(as_of_refs)),
        tables_considered=tuple(_table_ref(s, t) for s, t in all_tables),
        dropped_columns=leg2.dropped + leg3.dropped + leg4.dropped,
        legs=(leg1, leg2, leg3, leg4),
        expansion_terms=terms,
        context_bundles=_context_bundles(conn, bundle_refs, roles=roles,
                                         limit=budget.max_context_bundles),
    )
