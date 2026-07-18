"""MF-8b — key-gated REAL-PROVIDER batch-size sweep harness (EVIDENCE GENERATOR, not a CI gate).

The batch ceilings (``enrich_config._DEFAULT_MAX_ITEMS``) were lowered to conservative isolation
boundaries (MF-8a) precisely because there was NO evidence that a larger batch holds accuracy: the
hermetic gold gate (``test_enrich_batch_quality.py``) drives a scripted FakeLLM that echoes each
column's expected answer, so it measures the harness, not Anthropic, and compares no batch sizes and
no cross-item contamination. THIS harness generates that missing evidence: for each Pass A/B task it
runs the LIVE provider at several batch sizes against the shared gold corpus and records, per size:
accuracy vs gold, abstention rate, missing refs, duplicate refs, cross-item contamination
(``contamination.py``), wall-clock latency, and token cost (from ``LLMResult.cost_metadata``). It
emits a human-readable report under ``tests/eval/reports/`` and asserts ONLY that it ran end to end —
raising a ceiling later is a HUMAN decision informed by this report, never an automatic pass/fail.

Run it (needs a live key + a throwaway DB):

    ANTHROPIC_API_KEY=... FEATUREGEN_LLM_PROVIDER=anthropic \
        uv run pytest -m eval tests/eval/test_batch_size_sweep.py -q -s

With no ``ANTHROPIC_API_KEY`` it SKIPS cleanly — the only behaviour default CI / this env exercises.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.eval import contamination
from tests.eval.gold_columns import GOLD

from featuregen.intake.llm import LLMRequest, LLMResult
from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm
from featuregen.overlay.upload.enrich import (
    classify_domains,
    content_hash,
    draft_definitions,
    enrich_concepts,
)

pytestmark = pytest.mark.eval

# Batch sizes per task (a single-item baseline, size 1, is always prepended). These mirror the
# briefed sweep — the pre-MF-8a ceiling is the largest size for each Pass A task so the report shows
# what the old ceiling actually bought.
SWEEP_SIZES: dict[str, list[int]] = {
    "concept": [1, 5, 10, 20, 40],
    "definition": [1, 4, 8, 12],
    "domain": [1, 4, 8, 20],
    "table_synth": [1, 2, 4, 8],
}

# The env knob (``OVERLAY_ENRICH_BATCH_<T>_MAX_ITEMS``) and the batch out-key per task.
_OUT_KEY = {"concept": "concept", "definition": "definition", "domain": "domain",
            "table_synth": "synthesis"}
_CACHE_TABLE = {"concept": "enrichment_concept", "definition": "enrichment_definition",
                "domain": "enrichment_domain"}


# --------------------------------------------------------------------------- recording client seam


class _RecordingClient:
    """Wraps the live LLMClient and records, per provider ``call``: the requested item refs, the raw
    returned result entries, the reported token usage, and the wall-clock latency. Everything the
    sweep needs (missing/duplicate refs, contamination answers, cost, latency) comes from here, so
    the metrics never depend on the durable ``llm_call`` audit write surviving the rolled-back test
    transaction."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[dict] = []

    def reset(self) -> None:
        self.calls = []

    def call(self, request: LLMRequest) -> LLMResult:
        cat = request.inputs.get("catalog_metadata", {}) or {}
        requested = [it.get("ref") for it in (cat.get("items") or []) if isinstance(it, dict)]
        t0 = time.perf_counter()
        result = self._inner.call(request)
        dt = time.perf_counter() - t0
        out = result.output if isinstance(result.output, dict) else {}
        results_raw = [e for e in (out.get("results") or []) if isinstance(e, dict)]
        cost = result.cost_metadata or {}
        self.calls.append({
            "requested": requested,
            "results_raw": results_raw,
            "input_tokens": int(cost.get("input_tokens", 0) or 0),
            "output_tokens": int(cost.get("output_tokens", 0) or 0),
            "latency_s": dt,
            "status": result.status,
        })
        return result


# --------------------------------------------------------------------------- per-run metric record


@dataclass
class SizeMetrics:
    task: str
    size: int
    items: int = 0
    provider_calls: int = 0
    accuracy: float | None = None       # concept only (no gold text for definition/domain/synth)
    abstention_rate: float = 0.0
    missing_refs: int = 0
    duplicate_refs: int = 0
    contamination_rate: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_clock_s: float = 0.0
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tokens_per_item(self) -> float:
        return self.total_tokens / self.items if self.items else 0.0


@dataclass
class TaskReport:
    task: str
    sizes: list[SizeMetrics] = field(default_factory=list)


# --------------------------------------------------------------------------- task answer adapters


def _answer_of(task: str, entry: dict) -> str:
    """The generated text an item's answer contributes to the contamination check + abstention."""
    if task == "table_synth":
        return json.dumps(entry.get("synthesis") or
                          {k: v for k, v in entry.items() if k != "ref"}, sort_keys=True)
    return str(entry.get(_OUT_KEY[task], "") or "")


def _is_abstention(task: str, entry: dict) -> bool:
    if task == "concept":
        return str(entry.get("concept", "")).strip().lower() == "unclassified"
    if task == "table_synth":
        syn = entry.get("synthesis") or {}
        grain = syn.get("grain_columns") or []
        return not grain and not syn.get("as_of_column")
    return not str(entry.get(_OUT_KEY[task], "")).strip()   # definition / domain: blank == abstain


def _aggregate(task: str, calls: list[dict], identity_by_ref: dict[str, str]) -> dict:
    """Fold the per-call recordings into the ref-level metrics. Contamination is measured PER CALL
    (a chunk is the only place items actually share a prompt) then pooled — so the size-1 baseline,
    one item per call, is 0 by construction."""
    missing = duplicate = abstentions = 0
    total_items = contaminated = 0
    in_tok = out_tok = 0
    for c in calls:
        in_tok += c["input_tokens"]
        out_tok += c["output_tokens"]
        returned = [e.get("ref") for e in c["results_raw"]]
        seen: set[str] = set()
        for ref in returned:
            if ref in seen:
                duplicate += 1
            seen.add(ref)
        missing += len(set(c["requested"]) - seen)
        # Per-call contamination + abstention over the entries this call actually returned.
        call_items = []
        for e in c["results_raw"]:
            ref = e.get("ref")
            if ref is None:
                continue
            abstentions += _is_abstention(task, e)
            call_items.append(
                contamination.item_from_text(ref, identity_by_ref.get(ref, ""),
                                             _answer_of(task, e)))
        total_items += len(call_items)
        contaminated += len(contamination.contaminated_refs(call_items))
    return {
        "missing": missing, "duplicate": duplicate, "abstentions": abstentions,
        "returned_items": total_items, "contaminated": contaminated,
        "input_tokens": in_tok, "output_tokens": out_tok,
    }


# --------------------------------------------------------------------------- per-task sweep runners


def _clear_cache(db, task: str) -> None:
    table = _CACHE_TABLE.get(task)
    if table:
        db.execute(f"DELETE FROM {table}")   # rolled back with the test tx; forces a live re-classify


def _sweep_concept(db, client: _RecordingClient) -> tuple[dict, dict[str, str]]:
    rows = [r for r, _c, _a in GOLD]
    identity = {content_hash(r): f"{r.table} {r.column} {r.type}" for r in rows}
    def run(_size: int):
        client.reset()
        resolved = enrich_concepts(db, rows, client)
        agg = _aggregate("concept", client.calls, identity)
        hits = sum(1 for r, _e, alts in GOLD if resolved.get(content_hash(r)) in alts)
        agg["accuracy"] = hits / len(GOLD)
        agg["requested_items"] = len(rows)
        return agg
    return {"rows": rows, "run": run}, identity


def _sweep_definition(db, client: _RecordingClient):
    rows = [r for r, _c, _a in GOLD]
    concept_map = {content_hash(r): expected for r, expected, _a in GOLD}
    identity = {content_hash(r): f"{r.table} {r.column} {r.type}" for r in rows}
    def run(_size: int):
        client.reset()
        draft_definitions(db, rows, client, concepts=concept_map)
        agg = _aggregate("definition", client.calls, identity)
        agg["requested_items"] = len(rows)
        return agg
    return {"rows": rows, "run": run}


def _table_inputs():
    """Build per-table (columns) inputs from the gold rows — the same rows, grouped by table."""
    rows = [r for r, _c, _a in GOLD]
    cols: dict[str, set[str]] = {}
    for r in rows:
        cols.setdefault(r.table, set()).add(r.column)
    identity = {t: f"{t} " + " ".join(sorted(cs)) for t, cs in cols.items()}
    return rows, cols, identity


def _sweep_domain(db, client: _RecordingClient):
    rows, _cols, identity = _table_inputs()
    def run(_size: int):
        client.reset()
        classify_domains(db, rows, client)
        agg = _aggregate("domain", client.calls, identity)
        agg["requested_items"] = len({r.table for r in rows})
        return agg
    return {"run": run}


def _sweep_table_synth(db, client: _RecordingClient):
    from featuregen.overlay.upload.column_view import build_table_views
    from featuregen.overlay.upload.table_synth import assemble_table_items, synthesize_tables
    rows, cols, identity = _table_inputs()
    views = build_table_views(rows, glossary=None, bindings=None,
                              concepts=None, definitions=None, domains=None)
    items = assemble_table_items(views)
    def run(_size: int):
        client.reset()
        synthesize_tables(db, client, items, columns_by_table=cols, actor=None)
        agg = _aggregate("table_synth", client.calls, identity)
        agg["requested_items"] = len(items)
        return agg
    return {"run": run}


# --------------------------------------------------------------------------- report rendering


def _render(reports: list[TaskReport]) -> str:
    lines = ["BATCH-SIZE SWEEP — real-provider evidence for the enrichment batch ceilings",
             f"generated: {datetime.now(UTC).isoformat()}",
             f"model: {ClaudeConfig.from_env().model}",
             "metrics: accuracy vs gold (concept only) | abstention | missing/duplicate refs |",
             "         cross-item contamination | wall-clock latency | token cost", ""]
    hdr = (f"  {'size':>5} {'items':>5} {'calls':>5} {'acc':>6} {'abst':>6} {'miss':>5} "
           f"{'dup':>4} {'contam':>7} {'in_tok':>7} {'out_tok':>7} {'tok/it':>7} {'wall_s':>7}")
    for tr in reports:
        lines.append(f"== {tr.task} ==")
        lines.append(hdr)
        for m in tr.sizes:
            if m.error:
                lines.append(f"  {m.size:>5}  ERROR: {m.error}")
                continue
            acc = "  n/a" if m.accuracy is None else f"{m.accuracy:6.1%}"
            lines.append(
                f"  {m.size:>5} {m.items:>5} {m.provider_calls:>5} {acc:>6} "
                f"{m.abstention_rate:6.1%} {m.missing_refs:>5} {m.duplicate_refs:>4} "
                f"{m.contamination_rate:7.1%} {m.input_tokens:>7} {m.output_tokens:>7} "
                f"{m.tokens_per_item:7.1f} {m.wall_clock_s:7.2f}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the harness test


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="live provider required")
def test_batch_size_sweep(db, monkeypatch) -> None:
    # Wire the real adapter + force every task to batch mode. FEATUREGEN_LLM_PROVIDER=anthropic makes
    # _generation_settings() audit the true model (a live call would otherwise request model "test").
    monkeypatch.setenv("FEATUREGEN_LLM_PROVIDER", "anthropic")
    for m in ("CONCEPT", "DEFINITION", "DOMAIN"):
        monkeypatch.setenv(f"OVERLAY_ENRICH_{m}_MODE", "batch")

    client = _RecordingClient(build_claude_llm(ClaudeConfig.from_env()))
    sweeps = {
        "concept": _sweep_concept(db, client)[0],
        "definition": _sweep_definition(db, client),
        "domain": _sweep_domain(db, client),
        "table_synth": _sweep_table_synth(db, client),
    }

    reports: list[TaskReport] = []
    planned = attempted = 0
    for task, sizes in SWEEP_SIZES.items():
        run = sweeps[task]["run"]
        tr = TaskReport(task=task)
        for size in sizes:
            planned += 1
            monkeypatch.setenv(f"OVERLAY_ENRICH_BATCH_{task.upper()}_MAX_ITEMS", str(size))
            m = SizeMetrics(task=task, size=size)
            try:
                _clear_cache(db, task)
                t0 = time.perf_counter()
                agg = run(size)
                m.wall_clock_s = time.perf_counter() - t0
                m.provider_calls = len(client.calls)
                m.items = agg.get("returned_items", 0)
                m.accuracy = agg.get("accuracy")
                req = max(1, agg.get("requested_items", 0))
                m.abstention_rate = agg["abstentions"] / req
                m.missing_refs = agg["missing"]
                m.duplicate_refs = agg["duplicate"]
                m.contamination_rate = (agg["contaminated"] / agg["returned_items"]
                                        if agg["returned_items"] else 0.0)
                m.input_tokens = agg["input_tokens"]
                m.output_tokens = agg["output_tokens"]
                attempted += 1
            except Exception as exc:  # noqa: BLE001 — evidence generator: record + keep sweeping
                m.error = f"{type(exc).__name__}: {exc}"
            tr.sizes.append(m)
        reports.append(tr)

    report_text = _render(reports)
    print("\n" + report_text)   # visible with `-s`
    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = reports_dir / f"batch_size_sweep_{stamp}.txt"
    report_path.write_text(report_text, encoding="utf-8")

    # Evidence generator, NOT a pass/fail gate: assert only that the harness ran end to end and left
    # a report. Ceiling changes are a human call reading this report — never an accuracy assertion.
    assert report_path.exists()
    assert planned > 0
    assert attempted == planned, (
        f"only {attempted}/{planned} sweep points completed — see errors in {report_path}")
