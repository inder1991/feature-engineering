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

**Read-scoped throughout.** Both legs go through ``allowed_sensitivities(roles)``, so a column the
caller may not see is never offered to a model on their behalf. That is the same seam as the governed
read-scope fix, and it matters more here: this set becomes prompt text.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from featuregen.analysis.intent import IntentCandidates
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.search import MATCH_ANY, search


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


@dataclass(frozen=True, slots=True)
class Retrieval:
    """The bounded set, plus what it cost to bound it."""

    candidates: IntentCandidates
    tables_considered: tuple[str, ...] = ()
    #: Columns that matched but did not fit the budget. Non-zero means the answer may rest on a
    #: narrower view of the catalog than exists — worth telling a user, not swallowing.
    dropped_columns: int = 0
    #: Set when NO table could be identified: the question matched nothing the caller may read.
    empty_reason: str = ""

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


def retrieve_candidates(conn, question: str, *, now: datetime,
                        roles: Iterable[str] = (),
                        budget: RetrievalBudget | None = None,
                        fresh_within: timedelta = timedelta(hours=24)) -> Retrieval:
    """The catalog objects one question may be planned against.

    Two legs, in priority order:

    1. the governed grain and as-of columns of every table the question touched — always offered,
       because the plan cannot be expressed without them and the question never names them;
    2. the lexical matches themselves, filling whatever budget remains.

    A third leg — a bounded VERIFIED graph neighbourhood, so a dimension in a joined table is
    reachable when the question does not name it — is not implemented here. It is called out rather
    than approximated: reaching through unverified edges would offer the model columns from tables
    nothing sanctioned as related, which is the join-identity mistake the planning layer exists to
    catch.
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
            empty_reason="no readable catalog column matched this question")

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

    def _add(ref: str) -> None:
        if ref not in refs:
            refs.append(ref)

    # Leg 1 FIRST — see the module docstring. These are what make a plan expressible.
    grain_refs: set[str] = set()
    as_of_refs: set[str] = set()
    for source, table, column, is_grain, is_as_of in _structural_columns(conn, kept_tables,
                                                                        roles=roles):
        ref = _column_ref(source, table, column)
        _add(ref)
        # The role travels with the ref so a clarification can ask "which column identifies the
        # customer?" and list the two identifiers rather than all sixty columns.
        (grain_refs if is_grain else as_of_refs).add(ref)
    structural = len(refs)

    # Leg 2 — relevance, filling what is left.
    considered = 0
    for hit in columns:
        if (hit.catalog_source, hit.table) not in kept:
            continue
        considered += 1
        if len(refs) < budget.max_columns:
            _add(_column_ref(hit.catalog_source, hit.table, hit.column))

    labels = {
        _column_ref(h.catalog_source, h.table, h.column): (h.concept or h.column or "")
        for h in columns if (h.catalog_source, h.table) in kept
    }
    return Retrieval(
        candidates=IntentCandidates(
            column_refs=frozenset(refs),
            table_refs=frozenset(_table_ref(s, t) for s, t in kept_tables),
            labels={ref: label for ref, label in sorted(labels.items()) if ref in set(refs)},
            grain_refs=frozenset(grain_refs), as_of_refs=frozenset(as_of_refs)),
        tables_considered=tuple(_table_ref(s, t) for s, t in kept_tables),
        # Only the relevance leg can overflow; the structural columns are never dropped, so a
        # negative difference would mean leg 1 alone exceeded the budget — reported as zero rather
        # than as a nonsensical count.
        dropped_columns=max(0, structural + considered - len(refs)),
    )
