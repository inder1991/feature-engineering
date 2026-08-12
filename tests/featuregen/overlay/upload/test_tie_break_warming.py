"""TASK 2b increment 3 — warming: adjudicate every genuine tie at the ingest tail, once.

The owner's precompute goal, delivered at the right layer: requests never call the model, because
the deliberations were made when the catalog changed — at ingest, where the LLM budget, ledger and
audited dispatch already operate. A fixture with a REAL tie (two `event_timestamp` columns) drives
the whole path: ground → find AMBIGUOUS bindings → adjudicate through the governed seam → store
content-addressed. A re-run replays every verdict without one model call; no client degrades to
counted skips, never an error — the worst case for warming is exactly today's behaviour.
"""
from __future__ import annotations

from tests.featuregen.overlay.upload.test_templates import SOURCE

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.tie_break import (
    TIE_BREAK_TASK,
    TieBreakCandidate,
    find_tie_break_verdict,
    tie_break_input_hash,
    warm_tie_break_verdicts,
)

_TS_A = "public.transactions.aaa_load_ts"      # sorts FIRST — today's silent winner
_TS_B = "public.transactions.zzz_event_ts"     # the semantically right event clock

_ROWS = [
    (CanonicalRow(SOURCE, "transactions", "cust_ref", "integer", is_grain=True,
                  entity="Customer", definition="The customer this transaction belongs to."),
     "customer_id"),
    (CanonicalRow(SOURCE, "transactions", "aaa_load_ts", "timestamp",
                  definition="The warehouse batch load timestamp — a pipeline artifact."),
     "event_timestamp"),
    (CanonicalRow(SOURCE, "transactions", "zzz_event_ts", "timestamp",
                  definition="When the customer's transaction actually occurred."),
     "event_timestamp"),
]


def _tied_catalog(db):
    build_graph(db, SOURCE, [r for r, _ in _ROWS],
                concepts={content_hash(r): c for r, c in _ROWS if c})


def _client() -> FakeLLM:
    # One scripted answer serves every tie in this fixture: they all tie the same two columns.
    return FakeLLM(script={TIE_BREAK_TASK: FakeResponse(
        output={"ranking": [_TS_B, _TS_A],
                "rationale": "the recipe wants the business event time, not a load stamp"})})


class _MustNotBeCalled:
    def call(self, *a, **k):  # pragma: no cover
        raise AssertionError("warming a warmed catalog must replay, never re-dispatch")


def test_warming_adjudicates_every_genuine_tie_and_a_rerun_replays(db):
    _tied_catalog(db)
    stats = warm_tie_break_verdicts(db, catalog_source=SOURCE, client=_client(),
                                    roles=("data_owner",))
    assert stats["ambiguous"] >= 1, "the fixture exists to produce at least one tie"
    assert stats["adjudicated"] == stats["ambiguous"]
    # the second pass over the unchanged catalog costs ZERO model calls
    again = warm_tie_break_verdicts(db, catalog_source=SOURCE, client=_MustNotBeCalled(),
                                    roles=("data_owner",))
    assert again["ambiguous"] == stats["ambiguous"]
    assert again["replayed"] == again["ambiguous"]
    assert again.get("adjudicated", 0) == 0


def test_the_stored_verdict_is_findable_by_the_recomputed_key(db):
    """The warming write and the future request-time read must meet at the SAME key — recompute it
    here exactly as a consumer would, from the catalog's own enrichment text."""
    _tied_catalog(db)
    warm_tie_break_verdicts(db, catalog_source=SOURCE, client=_client(), roles=("data_owner",))
    from featuregen.overlay.upload.templates import ALL_TEMPLATES

    dormancy = next(t for t in ALL_TEMPLATES if t.id == "dormancy_days")
    need = next(n for n in dormancy.needs if n.concept == "event_timestamp")
    rows = {r[0]: r for r in db.execute(
        "SELECT object_ref, definition, ai_summary, semantic_terms FROM graph_node "
        "WHERE catalog_source=%s AND object_ref = ANY(%s)",
        (SOURCE, [_TS_A, _TS_B])).fetchall()}
    tied = tuple(TieBreakCandidate(ref=ref, definition=row[1] or "", ai_summary=row[2] or "",
                                   semantic_terms=row[3] or "")
                 for ref, row in sorted(rows.items()))
    key = tie_break_input_hash(template_id=dormancy.id, need_role=need.role,
                               need_concept=need.concept, intent=dormancy.intent, tied=tied)
    verdict = find_tie_break_verdict(db, input_hash=key, tied_refs=(_TS_A, _TS_B))
    assert verdict is not None
    assert verdict.ranking[0] == _TS_B, "the deliberation, not the alphabet, picked the clock"


def test_no_client_degrades_to_counted_skips(db):
    _tied_catalog(db)
    stats = warm_tie_break_verdicts(db, catalog_source=SOURCE, client=None,
                                    roles=("data_owner",))
    assert stats["ambiguous"] >= 1
    assert stats["unavailable"] == stats["ambiguous"]
    assert stats.get("adjudicated", 0) == 0


def test_the_call_ceiling_bounds_the_warming_spend(db):
    _tied_catalog(db)
    stats = warm_tie_break_verdicts(db, catalog_source=SOURCE, client=_client(),
                                    roles=("data_owner",), max_provider_calls=1)
    assert stats["adjudicated"] == 1
    assert stats["call_ceiling"] == stats["ambiguous"] - 1
