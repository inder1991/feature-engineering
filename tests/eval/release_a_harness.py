"""Release-A evaluation harness — REPLAY mode.

Drives the REAL enrichment / adjudication / retrieval / feature-context paths over the checked-in
gold sets, contrasting thin context (v1, flag off) with rich context (v4, flag on + profiles), and
records the six per-family measurements the two plans ask for.

THE MODE SEAM. `build_concept_provider(mode)` returns an `LLMClient`. In `FIXTURE` mode it returns
the deterministic oracle below; in `LIVE` mode it builds the real provider. Both satisfy the same
Protocol and plug into the same `enrich_concepts(..., client=...)` argument, so Gate B can run the
live half by passing a different `mode` — no restructuring, no second driver. NOTHING in this
branch selects `LIVE`: `assert_no_live_dispatch` proves it, and `no_network()` proves the run never
opens a non-loopback socket even if something tried.

THE THIN/RICH SEAM. `enrich_concepts` already takes `bundles` as a caller-supplied dict. Passing
`{}` makes every lookup miss, so `_classifier_payload` degrades to exactly the historical
metadata-only payload (its own docstring guarantees the degradation is byte-identical); passing the
real bundles gives the v4 purpose-adapted payload. No source change was needed for the contrast,
and no flag is toggled inside the enrichment path.

WHAT THIS DOES NOT MEASURE. Provider skill, token cost and live retrieval lift are Gate B (D9).
The fixture provider answers from the bytes it is handed, so every number here is a statement about
what the platform DELIVERED, not about how clever a model was with it.
"""
from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from tests.eval import gold_catalog_profiles as pg
from tests.eval import gold_semantic_context as sg

from featuregen.intake.llm import PROVIDER_OK, LLMClient, LLMRequest, LLMResult
from featuregen.intake.redaction import INPUT_KEY_CATALOG
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import build_enrichment_context, content_hash, enrich_concepts
from featuregen.overlay.upload.glossary_reader import GlossaryUpload

FIXTURE = "fixture"
LIVE = "live"
MODES = (FIXTURE, LIVE)

THIN = "thin"
RICH = "rich"

#: Set by :func:`build_concept_provider` whenever the LIVE branch is taken. The bar test reads it.
LIVE_PROVIDER_BUILDS: list[str] = []


# ── the mode seam ────────────────────────────────────────────────────────────────────────────────


class OracleConceptProvider:
    """The FIXTURE provider.

    An `LLMClient` that answers each batched item from the payload that item was ACTUALLY sent —
    which is the whole point. A scripted `FakeLLM` returns a canned answer regardless of what it was
    shown, so a thin-vs-rich comparison against one measures nothing at all. This provider is a
    function of the request, so the difference between the two runs is exactly the difference in
    what the pipeline put in front of it.
    """

    def __init__(self, cases_by_ref: dict[str, sg.SemanticGoldCase]) -> None:
        self._by_ref = cases_by_ref
        self.requests: list[LLMRequest] = []
        self.seen_payloads: dict[str, dict] = {}

    def call(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        items = (request.inputs.get(INPUT_KEY_CATALOG) or {}).get("items") or []
        results = []
        for item in items:
            ref = item.get("ref")
            case = self._by_ref.get(ref)
            if case is None:
                continue
            payload = {k: v for k, v in item.items() if k != "ref"}
            self.seen_payloads[ref] = payload
            results.append({"ref": ref, "concept": sg.oracle_answer(case, payload)})
        return LLMResult(output={"results": results}, self_reported_scores={},
                         call_ref="", status=PROVIDER_OK, cost_metadata={})


def build_concept_provider(mode: str, *,
                           cases_by_ref: dict[str, sg.SemanticGoldCase] | None = None,
                           model: str | None = None) -> LLMClient:
    """The one place a provider is chosen. Gate B flips `mode`; nothing else moves."""
    if mode == FIXTURE:
        return OracleConceptProvider(cases_by_ref or {})
    if mode == LIVE:
        # Deliberately reachable code so the seam is real rather than aspirational — and deliberately
        # never taken in this branch. Gate B supplies the approval and the key.
        LIVE_PROVIDER_BUILDS.append(model or "default")
        from featuregen.intake.llm import DEFAULT_LLM_MODEL
        from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm
        return build_claude_llm(ClaudeConfig(enabled=True, model=model or DEFAULT_LLM_MODEL))
    raise ValueError(f"unknown provider mode {mode!r}; expected one of {MODES}")


@contextmanager
def no_network() -> Iterator[None]:
    """Refuse every non-loopback connection for the duration.

    Loopback stays open because the test database is a real PostgreSQL on 127.0.0.1 — blocking it
    would prove nothing except that the harness cannot run. Everything else raises, so a stray
    provider call fails loudly instead of quietly costing money.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def _allowed(address: Any) -> bool:
        if not isinstance(address, tuple) or not address:
            return True                      # AF_UNIX and friends: not an egress path
        host = str(address[0])
        return host in {"127.0.0.1", "::1", "localhost", ""} or host.startswith("127.")

    def _guard(name):
        def _inner(self_or_addr, *args, **kwargs):
            raise AssertionError(
                f"the Release-A evaluation attempted a non-loopback {name}() — the replay half "
                f"must never reach a network. Live dispatch is Gate B.")
        return _inner

    def _connect(self, address, *args, **kwargs):
        if _allowed(address):
            return real_connect(self, address, *args, **kwargs)
        return _guard("connect")(self, address)

    def _connect_ex(self, address, *args, **kwargs):
        if _allowed(address):
            return real_connect_ex(self, address, *args, **kwargs)
        return _guard("connect_ex")(self, address)

    def _create_connection(address, *args, **kwargs):
        if _allowed(address):
            return real_create(address, *args, **kwargs)
        return _guard("create_connection")(address)

    socket.socket.connect = _connect            # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex      # type: ignore[method-assign]
    socket.create_connection = _create_connection   # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = real_connect            # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex      # type: ignore[method-assign]
        socket.create_connection = real_create          # type: ignore[assignment]


def assert_no_live_dispatch(provider: LLMClient) -> None:
    assert isinstance(provider, OracleConceptProvider), \
        f"the replay half must run on the fixture provider, got {type(provider).__name__}"
    assert LIVE_PROVIDER_BUILDS == [], \
        f"a live provider was built during the replay half: {LIVE_PROVIDER_BUILDS}"


# ── the gold fixtures, as the enrichment path wants them ─────────────────────────────────────────


def gold_rows() -> list[CanonicalRow]:
    return [sg.row_of(c) for c in sg.cases()]


def gold_glossary() -> GlossaryUpload:
    """A `GlossaryUpload` over the gold columns that HAVE a sidecar. The honestly-unclassified
    columns are absent from `records` on purpose — that absence is what they are testing."""
    rows = gold_rows()
    records = [rec for rec in (sg.record_of(c) for c in sg.cases()) if rec is not None]
    return GlossaryUpload(rows=rows, records=records)


def cases_by_content_hash() -> dict[str, sg.SemanticGoldCase]:
    return {content_hash(sg.row_of(c)): c for c in sg.cases()}


# ── measurement 1: selected-concept accuracy, per family, thin vs rich ───────────────────────────


@dataclass
class ConceptRun:
    """One end-to-end pass of the REAL `enrich_concepts` over the gold set."""

    mode: str                                  # thin | rich
    resolved: dict[str, str]                   # {content_hash: concept}
    provider: OracleConceptProvider
    answers: dict[str, str] = field(default_factory=dict)   # {case_id: concept}

    def answer_for(self, case: sg.SemanticGoldCase) -> str:
        return self.answers.get(case.case_id, sg.UNCLASSIFIED)


def reset_concept_cache(conn) -> None:
    """Clear the concept enrichment cache between the two runs.

    NOT housekeeping — without it the comparison is a lie. The concept cache is keyed by
    `concept_cache_key`, which by DESIGN (D12.5) excludes the bundle: the sibling roster and table
    context enter the prompt but never the per-column identity, precisely so a neighbour's
    reclassification cannot stale every sibling's evidence. The consequence for an evaluation is
    that the second run of the same columns is a 100% cache HIT and replays the FIRST run's answers
    — which made thin and rich come out byte-identical the first time this harness ran. Clearing
    between runs measures classification instead of cache reuse.
    """
    conn.execute("DELETE FROM enrichment_concept")


def run_concepts(conn, *, context: str, mode: str = FIXTURE) -> ConceptRun:
    """Drive the REAL batched concept enrichment.

    `context=THIN` passes `bundles={}` so every per-column lookup misses and the dispatch payload
    degrades to the historical metadata-only shape. `context=RICH` passes the real bundles. Nothing
    else differs between the two runs — same rows, same glossary, same provider object type, same
    acceptance path, same evidence writer.
    """
    reset_concept_cache(conn)
    rows = gold_rows()
    glossary = gold_glossary()
    by_ref = cases_by_content_hash()
    provider = build_concept_provider(mode, cases_by_ref=by_ref)
    assert_no_live_dispatch(provider)

    bundles = ({} if context == THIN
               else build_enrichment_context(conn, rows, glossary))
    resolved = enrich_concepts(conn, rows, provider, glossary=glossary, bundles=bundles)

    answers = {}
    for h, case in by_ref.items():
        answers[case.case_id] = resolved.get(h, sg.UNCLASSIFIED)
    return ConceptRun(mode=context, resolved=resolved, provider=provider, answers=answers)


@dataclass(frozen=True)
class FamilyAccuracy:
    family: str
    cases: int
    thin_hits: int
    rich_hits: int
    thin_forbidden: int
    rich_forbidden: int

    @property
    def thin_accuracy(self) -> float:
        return self.thin_hits / self.cases if self.cases else 0.0

    @property
    def rich_accuracy(self) -> float:
        return self.rich_hits / self.cases if self.cases else 0.0

    def as_dict(self) -> dict:
        return {"family": self.family, "cases": self.cases,
                "thin_accuracy": round(self.thin_accuracy, 4),
                "rich_accuracy": round(self.rich_accuracy, 4),
                "thin_hits": self.thin_hits, "rich_hits": self.rich_hits,
                "thin_forbidden_selections": self.thin_forbidden,
                "rich_forbidden_selections": self.rich_forbidden}


def family_accuracy(thin: ConceptRun, rich: ConceptRun) -> tuple[FamilyAccuracy, ...]:
    out = []
    for family in sg.families():
        cases = sg.cases_in(family)
        out.append(FamilyAccuracy(
            family=family,
            cases=len(cases),
            thin_hits=sum(1 for c in cases if sg.is_hit(c, thin.answer_for(c))),
            rich_hits=sum(1 for c in cases if sg.is_hit(c, rich.answer_for(c))),
            thin_forbidden=sum(1 for c in cases if thin.answer_for(c) in c.forbidden_concepts),
            rich_forbidden=sum(1 for c in cases if rich.answer_for(c) in c.forbidden_concepts),
        ))
    return tuple(out)


# ── measurement 2: unclassified precision ────────────────────────────────────────────────────────


def unclassified_precision(run: ConceptRun) -> dict:
    """Of the columns this run declined to classify, how many SHOULD have been declined.

    The interesting failure is not a low score but a high one reached by declining everything, so
    `declined` is reported beside it.
    """
    declined = [c for c in sg.cases() if run.answer_for(c) == sg.UNCLASSIFIED]
    correct = [c for c in declined if c.expected_concept == sg.UNCLASSIFIED]
    should_decline = [c for c in sg.cases() if c.expected_concept == sg.UNCLASSIFIED]
    return {
        "declined": len(declined),
        "correctly_declined": len(correct),
        "should_decline": len(should_decline),
        "precision": round(len(correct) / len(declined), 4) if declined else 1.0,
        "recall": round(len(correct) / len(should_decline), 4) if should_decline else 1.0,
    }


# ── measurement 3: false cross-namespace candidates ──────────────────────────────────────────────


def seed_gold_catalog(conn, *, now=None) -> None:
    """Put BOTH gold sets into the graph as a real ingest would leave them.

    Three things a bare `build_graph` does not do, each of which silently zeroed a measurement on
    the first run of this harness:

    * `attested_at` is left NULL and search falls back to an ingestion WATERMARK that a direct
      `build_graph` never writes — so every freshness predicate failed and retrieval offered
      nothing at all. Stamping it is what an ingest does.
    * the semantic gold columns and the profile gold tables share one catalog source, and
      `build_graph` DELETEs the source before rebuilding — so they must go in together or the
      second call erases the first.
    * the table-node profile projections (`data_role`, `business_context`, ...) are what leg 3
      harvests when `FEATUREGEN_DATASET_PROFILES` is on. Without them the flag changes nothing and
      the thin-vs-rich retrieval comparison measures a constant.
    """
    from datetime import UTC, datetime

    from featuregen.overlay.upload.graph import build_graph

    stamp = now or datetime.now(UTC)
    source = sg.load()["catalog_source"]

    rows = [sg.catalog_row_of(c) for c in sg.cases()]
    concepts = {content_hash(r): c.expected_concept
                for r, c in zip(rows, sg.cases())
                if c.expected_concept != sg.UNCLASSIFIED}
    seen = {(r.table, r.column) for r in rows}
    for dataset in pg.datasets():
        for row in dataset.rows():
            if (row.table, row.column) in seen:
                continue
            seen.add((row.table, row.column))
            rows.append(row)
    build_graph(conn, source, rows, concepts=concepts)

    peer = sg.load()["peer_catalog"]
    peer_rows = [row for row, _concept, _rec in sg.peer_columns()]
    peer_concepts = {content_hash(row): concept for row, concept, _rec in sg.peer_columns()}
    build_graph(conn, peer["catalog_source"], peer_rows, concepts=peer_concepts)

    conn.execute("UPDATE graph_node SET attested_at = %s WHERE catalog_source = ANY(%s)",
                 (stamp, [source, peer["catalog_source"]]))
    # Search INNER-JOINs `overlay_drift_watermark`, so a source with no watermark row is invisible
    # to every query however fresh its nodes are — the second reason retrieval offered nothing on
    # the first run. An ingest writes this; a direct `build_graph` does not.
    for src in (source, peer["catalog_source"]):
        conn.execute(
            "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id) "
            "VALUES (%s, %s, %s) ON CONFLICT (catalog_source) DO UPDATE "
            "SET last_completed_at = EXCLUDED.last_completed_at",
            (src, stamp, "release_a_eval"))
    project_profile_display(conn, source=source)


def project_profile_display(conn, *, source: str) -> None:
    """Write the profile gold onto the TABLE nodes' display projections.

    These columns are display projections by contract — rebuildable from evidence, never
    authoritative (migrations 1047/1051/1052 and the `_project_display` path). Writing them here is
    therefore restating what the consumption step's projection would restate; it is not smuggling
    authority in, and the load-bearing question is settled by `field_policies`, which this never
    touches.
    """
    for dataset in pg.datasets():
        conn.execute(
            "UPDATE graph_node SET definition = %s, business_context = %s, domain = %s, "
            "data_role = %s, primary_entity = %s, authority_role = %s, "
            "temporal_storage_model = %s "
            "WHERE catalog_source = %s AND kind = 'table' AND table_name = %s",
            (dataset.narrative, dataset.business_context, "Compliance",
             dataset.expected["data_role"], dataset.expected["primary_entity"],
             dataset.expected["authority_role"], dataset.expected["temporal_storage_model"],
             source, dataset.table))


def observed_table_fields(conn, *, source: str) -> dict[str, dict]:
    """What the platform can actually SEE about each gold table, read back from the projections."""
    rows = conn.execute(
        "SELECT table_name, data_role, authority_role, temporal_storage_model "
        "FROM graph_node WHERE catalog_source = %s AND kind = 'table'", (source,)).fetchall()
    return {r[0]: {"data_role": r[1], "authority_role": r[2], "temporal_storage_model": r[3]}
            for r in rows}


def cross_namespace_candidates(conn) -> dict:
    """Run the REAL candidate derivation and grade every gold control against it.

    The graded controls (`must_not_pair`) are all CROSS-source, which is not a detail: the
    derivation enumerates pairs only across distinct catalog sources, so an intra-source control is
    refused by the topology before the namespace gate is consulted and its zero is free. Those six
    intra-source pairs are still carried — as `unreachable_by_topology`, counted separately, so the
    headline number is a statement about the gate and nothing else.
    """
    from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates

    candidates = derive_bridge_candidates(conn, roles=("platform_admin", "pii_reader"))
    pairs: set[frozenset[str]] = set()
    for cand in candidates:
        pairs.add(frozenset({cand.left_ref.column or cand.left_ref.table,
                             cand.right_ref.column or cand.right_ref.table}))

    violations = [f"{a} <-> {b} ({why})" for a, b, why in sg.must_not_pair()
                  if frozenset({a, b}) in pairs]
    unreachable_seen = [f"{a} <-> {b} ({why})" for a, b, why in sg.unreachable_by_topology()
                        if frozenset({a, b}) in pairs]
    permitted_seen = [f"{a} <-> {b}" for a, b, _why in sg.may_pair()
                      if frozenset({a, b}) in pairs]
    return {
        "candidates_derived": len(candidates),
        "must_not_pair_checked": len(sg.must_not_pair()),
        "false_cross_namespace_candidates": len(violations),
        "violations": violations,
        "unreachable_by_topology_checked": len(sg.unreachable_by_topology()),
        "unreachable_by_topology_offered": unreachable_seen,
        "positive_controls_expected": [f"{a} <-> {b}" for a, b, _why in sg.may_pair()],
        "positive_controls_offered": permitted_seen,
    }


@contextmanager
def namespace_gate_disabled() -> Iterator[None]:
    """Collapse every identifier namespace to one value for the duration — the REACHABILITY control.

    This is the review's injection, kept: `Concept.namespace` is the blocking key
    (`_derive_from_identifier_columns` blocks on `(namespace, type_family)`), so rewriting every
    endpoint's namespace to a single literal removes the gate and leaves everything else — grounding,
    type family, source distinctness, hard-conflict suppression — exactly as it was.

    It exists for the same reason the mutation harness exists: a bar that reports zero violations has
    said nothing until someone shows the machine CAN report a violation. Under this injection the
    gold's cross-source controls all become candidates, so the zero above is the gate's doing and not
    the fixture's shape.

    `_identifier_columns` is read from the module's globals at call time by
    `derive_bridge_candidates_with_report`, so patching it here genuinely changes what the real
    derivation does; the original is restored on exit.
    """
    from dataclasses import replace

    from featuregen.overlay.upload import bridge_candidates as bc

    original = bc._identifier_columns

    def _collapsed(conn, *, roles):
        return [replace(col, namespace="ALL_NAMESPACES_COLLAPSED")
                for col in original(conn, roles=roles)]

    bc._identifier_columns = _collapsed          # type: ignore[assignment]
    try:
        yield
    finally:
        bc._identifier_columns = original        # type: ignore[assignment]


def _column_of(ref: str) -> str:
    return ref.rsplit(".", 1)[-1] if "." in ref else ref


# ── measurement 4: grounded retrieval hit rate ───────────────────────────────────────────────────


def retrieval_hits(conn, *, now, profiles_on: bool) -> dict:
    """Run the REAL four-leg retrieval over the gold questions and score grounded hits.

    `profiles_on` is the caller's statement about the flag; the caller sets it (monkeypatch), this
    function only records which world it measured.
    """
    from featuregen.analysis.retrieval import LEG_SEMANTIC_EXPANSION, retrieve_candidates

    per_question = []
    hits = misses = forbidden = 0
    expansion_terms = leg3_offered = leg3_considered = 0
    for q in sg.retrieval_questions():
        result = retrieve_candidates(conn, q["question"], now=now,
                                     roles=("platform_admin", "pii_reader"))
        offered = {_column_of(ref) for ref in _offered_refs(result)}
        got = sorted(offered & set(q["expected_columns"]))
        bad = sorted(offered & set(q["forbidden_columns"]))
        hits += len(got)
        misses += len(set(q["expected_columns"]) - offered)
        forbidden += len(bad)
        expansion_terms += len(result.expansion_terms)
        for leg in result.legs:
            if leg.leg == LEG_SEMANTIC_EXPANSION:
                leg3_considered += leg.considered
                leg3_offered += leg.offered
        per_question.append({"id": q["id"], "expected": q["expected_columns"],
                             "grounded_hits": got, "forbidden_offered": bad,
                             "offered_total": len(offered)})
    total_expected = sum(len(q["expected_columns"]) for q in sg.retrieval_questions())
    return {
        "profiles_on": profiles_on,
        "questions": len(sg.retrieval_questions()),
        "expected_total": total_expected,
        "grounded_hits": hits,
        "missed": misses,
        "forbidden_offered": forbidden,
        "hit_rate": round(hits / total_expected, 4) if total_expected else 0.0,
        # The lift axis. Grounded hits SATURATE on a gold set this small — two tables and ~26
        # columns, so the lexical leg alone already reaches every expected column in both modes and
        # there is no headroom above it (confirmed by re-running at max_columns 60/6/4/3: 8/8 every
        # time). What the profile context demonstrably changes is leg 3's own contribution: the
        # controlled expansion harvests the TABLE profile projections only when the flag is on. So
        # the direction is asserted HERE, where it is real, rather than manufactured on a hit rate
        # that has nowhere to go.
        "expansion_terms": expansion_terms,
        "leg3_considered": leg3_considered,
        "leg3_offered": leg3_offered,
        "per_question": per_question,
    }


def _offered_refs(result) -> list[str]:
    """Everything the retrieval OFFERED, from the closed candidate set the planner may name.

    `candidates.column_refs` is the authority on that (validation turns on it alone); the context
    bundles are added because a ref that was offered AND carried its semantic context is still an
    offer, and dropping it would under-count the rich half against itself.
    """
    refs: list[str] = [str(ref) for ref in result.candidates.column_refs]
    for bundle in result.context_bundles or ():
        ref = bundle.get("ref") if isinstance(bundle, dict) else getattr(bundle, "ref", None)
        if ref:
            refs.append(str(ref))
    return refs


# ── measurement 5: unsafe-feature acceptance ─────────────────────────────────────────────────────


def run_feature_generation(conn, *, now) -> dict:
    """Put every gold feature in front of the REAL generator and record its dispositions.

    The provider here is a plain scripted `FakeLLM`: the question is not what a model proposes but
    what the platform ACCEPTS once a proposal exists, so a scripted proposal is exactly right.
    """
    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.upload.feature_assist import recommend_features_report

    def _flat(ref: str) -> str:
        return "public." + ref.split(".", 1)[1]

    proposals = [{"name": c["name"],
                  "derives_from": [_flat(r) for r in c["derives_from"]],
                  "aggregation": "latest"}
                 for c in sg.unsafe_feature_cases()]
    client = FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": proposals})})
    report = recommend_features_report(
        conn, "predict which transactions will be reported", client,
        catalog_source=sg.load()["catalog_source"],
        target_ref=_flat("ftr.comp_fin_tran.sar_filed_flag"),
        roles=("platform_admin", "pii_reader", "restricted_reader"),
        now=now, budget=3, critic=False)
    return {
        "accepted": sorted(i.name for i in report.ideas),
        "dispositions": {i.name: {"validation_status": i.validation_status,
                                  "requirements": len(i.requirements)}
                         for i in report.ideas},
        "rejections": list(report.rejections),
    }


def unsafe_feature_acceptance(accepted_names: set[str]) -> dict:
    """Grade a set of ACCEPTED feature names against the gold safety labels.

    Kept transport-agnostic (a set of names in, a verdict out) exactly like `feature_eval.py`, so
    the same grader serves the fixture half here and the live half at Gate B.
    """
    cases = sg.unsafe_feature_cases()
    unsafe_accepted = sorted(c["name"] for c in cases if c["unsafe"] and c["name"] in accepted_names)
    safe_accepted = sorted(c["name"] for c in cases
                           if not c["unsafe"] and c["name"] in accepted_names)
    return {
        "unsafe_gold": sum(1 for c in cases if c["unsafe"]),
        "safe_gold": sum(1 for c in cases if not c["unsafe"]),
        "unsafe_accepted": unsafe_accepted,
        "safe_accepted": safe_accepted,
    }


# ── measurement 6: profile table-selection quality + the Pass-B replay ───────────────────────────


def passb_input_hash(*, context_revision: str, table: str) -> str:
    """The exact key `table_synth._record_synthesis_results` content-addresses a synthesis under.
    Recomputed here rather than re-implemented: Pass B never sets a `structured_result_current`
    pointer, so a replay MUST go through `find_structured_result` by input hash."""
    from featuregen.overlay.field_evidence import canonical_hash
    return canonical_hash({"context_revision": context_revision, "table": table})


def record_passb_gold(conn, *, context_revision: str) -> dict[str, str]:
    """Write every gold dataset's reviewer-accepted synthesis into the REAL structured_results store
    under the REAL Pass-B result type. No client, no Pass-B run — this is the recording half of the
    replay proof."""
    from featuregen.overlay.upload.structured_results import record_structured_result
    from featuregen.overlay.upload.table_synth import PASS_B_RESULT_TYPE, PASS_B_RESULT_VERSION

    written: dict[str, str] = {}
    for dataset in pg.datasets():
        if dataset.expected_synthesis is None:
            continue                          # the honestly-unknown table synthesizes nothing
        stored = record_structured_result(
            conn,
            result_type=PASS_B_RESULT_TYPE,
            result_version=PASS_B_RESULT_VERSION,
            input_content_hash=passb_input_hash(context_revision=context_revision,
                                                table=dataset.table),
            output=dict(dataset.expected_synthesis),
            producer_kind="llm_call",
            producer_ref="release_a_eval:replay",
            authority={"authority": "llm_advisory",
                       "logical_ref": f"{dataset.catalog_source}::{dataset.table}"},
        )
        written[dataset.table] = stored.structured_result_id
    return written


def replay_passb(conn, *, context_revision: str) -> dict:
    """The replay half: read every recorded synthesis back by recomputed input hash and confirm it
    matches the gold byte-for-byte. Proves one full no-client Pass-B round trip."""
    from featuregen.overlay.upload.structured_results import find_structured_result
    from featuregen.overlay.upload.table_synth import PASS_B_RESULT_TYPE, PASS_B_RESULT_VERSION

    replayed, mismatched, absent = [], [], []
    for dataset in pg.datasets():
        if dataset.expected_synthesis is None:
            continue
        found = find_structured_result(
            conn,
            result_type=PASS_B_RESULT_TYPE,
            result_version=PASS_B_RESULT_VERSION,
            input_content_hash=passb_input_hash(context_revision=context_revision,
                                                table=dataset.table),
        )
        if found is None:
            absent.append(dataset.table)
        elif found.output != dataset.expected_synthesis:
            mismatched.append(dataset.table)
        else:
            replayed.append(dataset.table)
    return {"replayed": sorted(replayed), "mismatched": sorted(mismatched),
            "absent": sorted(absent),
            "expected": sorted(d.table for d in pg.datasets() if d.expected_synthesis)}


def table_selection_quality(*, profiles_on: bool, visible: dict[str, dict]) -> dict:
    """Score the profile table-selection questions.

    `visible` is `{table: {field: value}}` — what the consumer could SEE about each table in this
    world. With profiles off the classification fields are absent, so a question can only be
    answered lexically; with them on, `data_role`/`authority_role`/`temporal_storage_model` are
    available and the `avoid` table becomes distinguishable. A question scores 1 when its preferred
    table is distinguishable from every table it must avoid.
    """
    fields = ("data_role", "authority_role", "temporal_storage_model")
    per_question = []
    resolved = 0
    for q in pg.selection_questions():
        prefer = q["prefer"][0]
        prefer_view = {f: visible.get(prefer, {}).get(f) for f in fields}
        distinguishable = all(
            any(prefer_view[f] is not None and prefer_view[f] != visible.get(avoid, {}).get(f)
                for f in fields)
            for avoid in q["avoid"]
        ) if q["avoid"] else bool(any(prefer_view.values()))
        resolved += distinguishable
        per_question.append({"id": q["id"], "prefer": prefer, "avoid": q["avoid"],
                             "distinguishable": distinguishable})
    return {
        "profiles_on": profiles_on,
        "questions": len(pg.selection_questions()),
        "resolved": resolved,
        "resolution_rate": round(resolved / len(pg.selection_questions()), 4),
        "per_question": per_question,
    }


def visible_table_fields(conn, *, source: str) -> dict[str, dict]:
    """What a FEATURE-GENERATION consumer can actually see about each gold table right now.

    Asked of the real, flag-sensitive consumer (`feature_assist._profile_advisories`) rather than
    modelled: with `FEATUREGEN_DATASET_PROFILES` off it returns `{}` — no key, not an empty one —
    so the classification is not merely hidden, it is absent from the payload. That is also why
    this function reads the flag through the consumer instead of taking a boolean: a measurement
    that asserted its own answer would prove nothing.
    """
    from featuregen.overlay.upload.feature_assist import _profile_advisories

    observed = observed_table_fields(conn, source=source)
    out: dict[str, dict] = {}
    for table, fields in observed.items():
        member = {
            "table_data_role": fields["data_role"],
            "table_authority_role": fields["authority_role"],
            "table_temporal_storage_model": fields["temporal_storage_model"],
            "table_business_context": None,
            "is_as_of": False,
            "availability_fact_event_id": None,
        }
        block = _profile_advisories([member])
        out[table] = {k: block.get(k) for k in
                      ("data_role", "authority_role", "temporal_storage_model")}
    return out
