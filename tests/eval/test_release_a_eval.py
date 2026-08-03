"""Release-A evaluation — the REPLAY half (semantic Task 10 + profile Task 6, one run).

Runs the six measurements over the gold sets, writes a versioned report under `tests/eval/reports/`,
and prints it. The RELEASE BARS are asserted next door in `test_release_a_bars.py`; this module's
job is to produce the numbers those bars and the evaluation record cite.

Run it:

    uv run --extra dev pytest -q -m eval tests/eval/test_release_a_eval.py -s

No provider key is needed or accepted: the whole module runs inside `no_network()`, which refuses
every non-loopback socket. The live same-model comparison, token/call cost and live retrieval lift
are Gate B (interface doc D9) and are deliberately absent.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.eval import gold_catalog_profiles as pg
from tests.eval import gold_semantic_context as sg
from tests.eval import release_a_harness as h

pytestmark = pytest.mark.eval

_CONTEXT_REVISION = "release_a_eval_v1"
REPORT_DIR = Path(__file__).parent / "reports"


@pytest.fixture()
def hermetic(monkeypatch):
    """Every eval run: no key in the environment, no non-loopback socket, no live build recorded."""
    for var in ("ANTHROPIC_API_KEY", "FEATUREGEN_LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    h.LIVE_PROVIDER_BUILDS.clear()
    with h.no_network():
        yield
    assert h.LIVE_PROVIDER_BUILDS == []


@pytest.fixture()
def report(hermetic, db, monkeypatch) -> dict:
    """THE run. One fixture so every bar and the record cite the same numbers from one execution."""
    now = datetime.now(UTC)

    # ── 1/2: concept accuracy + unclassified precision, thin vs rich ─────────────────────────────
    thin = h.run_concepts(db, context=h.THIN)
    rich = h.run_concepts(db, context=h.RICH)
    families = h.family_accuracy(thin, rich)

    # ── 3: false cross-namespace candidates over the REAL derivation ─────────────────────────────
    h.seed_gold_catalog(db)
    namespaces = h.cross_namespace_candidates(db)

    # ── 4: grounded retrieval, profiles off then on ──────────────────────────────────────────────
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    retrieval_thin = h.retrieval_hits(db, now=now, profiles_on=False)
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    retrieval_rich = h.retrieval_hits(db, now=now, profiles_on=True)
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)

    # ── 5: Pass-B replay through the real structured_results store ───────────────────────────────
    written = h.record_passb_gold(db, context_revision=_CONTEXT_REVISION)
    passb = h.replay_passb(db, context_revision=_CONTEXT_REVISION)
    passb["recorded"] = sorted(written)

    # ── 6: profile table-selection quality, asked of the real flag-sensitive consumer ────────────
    source = sg.load()["catalog_source"]
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    selection_thin = h.table_selection_quality(
        profiles_on=False, visible=h.visible_table_fields(db, source=source))
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    selection_rich = h.table_selection_quality(
        profiles_on=True, visible=h.visible_table_fields(db, source=source))
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)

    # ── 7: what the REAL generator accepts when every gold feature is proposed to it ─────────────
    featuregen = h.run_feature_generation(db, now=now)

    out = {
        "generated_at": now.isoformat(),
        "mode": "replay",
        "provider_mode": h.FIXTURE,
        "gold_sets": {"semantic": sg.load()["gold_set_id"], "profile": pg.load()["gold_set_id"]},
        "deferred_to_gate_b": [
            "live same-provider/model thin-vs-rich comparison",
            "real token and provider-call cost",
            "live retrieval lift against a real index",
            "ontology-gap usefulness judgement",
        ],
        "concept_accuracy_by_family": [f.as_dict() for f in families],
        "concept_accuracy_overall": {
            "cases": sum(f.cases for f in families),
            "thin_hits": sum(f.thin_hits for f in families),
            "rich_hits": sum(f.rich_hits for f in families),
            "thin_forbidden_selections": sum(f.thin_forbidden for f in families),
            "rich_forbidden_selections": sum(f.rich_forbidden for f in families),
        },
        "unclassified_precision": {"thin": h.unclassified_precision(thin),
                                   "rich": h.unclassified_precision(rich)},
        "cross_namespace": namespaces,
        "grounded_retrieval": {"thin": retrieval_thin, "rich": retrieval_rich},
        "passb_replay": passb,
        "table_selection": {"thin": selection_thin, "rich": selection_rich},
        "feature_generation": featuregen,
        "unsafe_features": h.unsafe_feature_acceptance(set(featuregen["accepted"])),
    }

    REPORT_DIR.mkdir(exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"release_a_eval_{stamp}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    out["report_path"] = str(path)
    print("\n" + json.dumps(out, indent=2))       # visible with -s
    return out


def test_the_replay_ran_with_no_client_and_no_network(report: dict) -> None:
    assert report["provider_mode"] == h.FIXTURE
    assert h.LIVE_PROVIDER_BUILDS == []
    assert not os.environ.get("ANTHROPIC_API_KEY")
    assert Path(report["report_path"]).exists()


def test_every_semantic_family_was_measured(report: dict) -> None:
    measured = {f["family"] for f in report["concept_accuracy_by_family"]}
    assert measured == set(sg.families())
    assert all(f["cases"] > 0 for f in report["concept_accuracy_by_family"])


def test_every_profile_family_has_a_dataset_in_the_selection_world(report: dict) -> None:
    measured = {q["prefer"] for q in report["table_selection"]["rich"]["per_question"]}
    assert measured <= {d.table for d in pg.datasets()}
    assert report["table_selection"]["rich"]["questions"] == len(pg.selection_questions())


def test_the_passb_replay_is_a_full_no_client_round_trip(report: dict) -> None:
    """Pass B writes to `structured_results` but never sets a `structured_result_current` pointer,
    so the replay must find each synthesis by RECOMPUTED input hash. That it does is the proof the
    plan's 'replayed structured results' item was asking for."""
    passb = report["passb_replay"]
    assert passb["absent"] == [], f"not replayable: {passb['absent']}"
    assert passb["mismatched"] == [], f"replayed bytes differ: {passb['mismatched']}"
    assert passb["replayed"] == passb["expected"]
    assert passb["replayed"], "a replay proof over zero datasets proves nothing"
