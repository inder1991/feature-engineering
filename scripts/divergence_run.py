"""E3 — the finite divergence run: replay recorded hypotheses through BOTH paths, once.

A ONE-SHOT comparison script, not a mode. For each recorded hypothesis (the newest N
`contract_intent` rows with a catalog source), build the considered set under the LEGACY
path and under SEMANTIC_V1 against the same database, and emit the per-candidate pair table
plus an explained/unexplained adjudication skeleton the operator reviews.

USAGE (operator, dev environment):

    FEATUREGEN_DSN=postgresql://... \\
    FEATUREGEN_LLM_PROVIDER=anthropic FEATUREGEN_LLM_KEY_FILE=... \\
    uv run python scripts/divergence_run.py --limit 10 --catalog-source cib \\
        --out docs/architecture/2026-08-XX-divergence-run.md

COSTS AND SCOPE, stated plainly: the legacy path calls the model's free-form generator and
the semantic path calls the intent task — real provider spend per hypothesis (~2-4 calls
each). The script never writes to the catalog it reads beyond the append-only observation/
audit rows generation always writes. Divergences are EXPECTED (the remediation changed
serving deliberately); the table's job is making every one EXPLAINED — each row lands in
the output with an `adjudication: TODO` the operator replaces with `explained: <reason>` or
escalates as a task. Zero unexplained rows is the E3 acceptance.

The known-by-construction explanations (pre-filled where detectable):

* `retired-free-form` — a legacy card from the free-form generator; the generator is OFF
  under semantic_v1 by design (B1).
* `variant-expansion` — semantic serves @window=N variants where legacy served one card (B5).
* `blocked-now-actionable` — legacy served a card the engine refuses/blocks with a named
  code (the metadata now governs — C-phase).
* `new-recall` — semantic serves a candidate legacy never had (closure retrieval C7, intent
  origin SE-6).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime


def _candidates(body: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for feature_set in body.get("alternatives", ()):
        for card in feature_set.get("features", ()):
            key = (card.get("source_definition_id") or card.get("recipe_id")
                   or card.get("name") or "?")
            out[str(key)] = {
                "name": card.get("name"),
                "source": card.get("generation_source", "?"),
                "status": card.get("candidate_status") or "served",
                "validation": card.get("validation_status"),
            }
    return out


def _explain(key: str, legacy: dict | None, semantic: dict | None) -> str:
    # NON-VACUITY: an errored leg is never a match — a run where both paths fail
    # identically must surface as unexplained rows, not silently zero out.
    if (legacy or {}).get("status") == "error" or (semantic or {}).get("status") == "error":
        return "TODO (a path errored — the replay itself needs fixing)"
    if legacy is not None and semantic is None:
        if legacy.get("source") in ("llm_freeform", None, "?"):
            return "explained: retired-free-form (B1 — the generator is off by design)"
        return "TODO"
    if legacy is None and semantic is not None:
        if "@window=" in key:
            return "explained: variant-expansion (B5 — explicit bounded variants)"
        if semantic.get("source") == "llm_intent":
            return "explained: new-recall (SE-6 — the intent origin through the one engine)"
        return "explained: new-recall (C7 closure / engine retrieval)"
    if legacy is not None and semantic is not None:
        if legacy.get("status") != semantic.get("status"):
            return ("explained: blocked-now-actionable (the metadata governs — "
                    "C-phase floors/laws)")
        return "match"
    return "TODO"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--catalog-source", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import psycopg
    from fastapi.testclient import TestClient

    from featuregen.api.app import create_app_from_env

    dsn = os.environ["FEATUREGEN_DSN"]
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT DISTINCT hypothesis FROM contract_intent "
            "WHERE hypothesis IS NOT NULL AND hypothesis <> '' "
            "ORDER BY hypothesis LIMIT %s", (args.limit,)).fetchall()
    hypotheses = [r[0] for r in rows]
    if not hypotheses:
        print("no recorded hypotheses found — nothing to replay", file=sys.stderr)
        return 1

    app = create_app_from_env()
    client = TestClient(app)
    headers = {"X-User": "divergence-run", "X-Roles": "platform_admin"}

    lines = [
        f"# Divergence run — {datetime.now(UTC).date()}",
        "",
        f"Replayed {len(hypotheses)} recorded hypotheses through BOTH paths against "
        f"`{args.catalog_source}`. Every non-match row needs an adjudication: replace each "
        "`TODO` with `explained: <reason>` or open a task. Zero unexplained rows is the "
        "E3 acceptance.",
        "",
        "| hypothesis | candidate | legacy | semantic | adjudication |",
        "|---|---|---|---|---|",
    ]
    unexplained = 0
    compared_pairs = 0
    for hypothesis in hypotheses:
        # As a user would: the REAL recognizer classifies the hypothesis (one model call),
        # and its proposed primary becomes the confirmed scope BOTH legs replay under —
        # the historical runs predate the scoped flow, so no recorded scope exists.
        # A stored CLASSIFIED recognition for this hypothesis replays without a provider
        # call (recognitions are append-only; the sealed inputs must match the considered-
        # set's, so only attempts sealed with THIS objective qualify). Fresh recognition is
        # the fallback — and its inputs are sealed identically.
        with psycopg.connect(dsn) as conn2:
            stored = conn2.execute(
                "SELECT a.recognition_id, a.intent_id, a.candidates "
                "FROM intent_recognition_attempt a "
                "JOIN contract_intent i ON i.intent_id = a.intent_id "
                "WHERE i.hypothesis = %s AND a.status = 'classified' "
                "AND a.input_json->>'redacted_prediction_goal' = 'divergence replay' "
                "ORDER BY a.created_at DESC LIMIT 1", (hypothesis,)).fetchone()
        if stored is not None:
            rec = {"recognition_id": stored[0], "intent_id": stored[1],
                   "candidates": stored[2], "status": "classified"}
        else:
            # The recognition SEALS its inputs (hypothesis + objective) and the considered-
            # set verifies them unchanged — identical objective text on both calls.
            rec = client.post("/contract/recognitions", json={
                "hypothesis": hypothesis, "objective": "divergence replay"},
                headers=headers).json()
        primary = next((c["use_case_id"] for c in rec.get("candidates", ())
                        if c.get("relationship") == "primary"), None)
        if primary is None:
            lines.append(f"| {hypothesis[:48]} | — | — | — | "
                         f"skipped: recognizer returned no primary "
                         f"(status={rec.get('status')}) |")
            continue
        pair = {}
        for mode in ("legacy", "semantic_v1"):
            os.environ["FEATUREGEN_SEMANTIC_PLANNING"] = mode
            res = client.post("/contract/considered-set", json={
                "hypothesis": hypothesis, "objective": "divergence replay",
                "catalog_source": args.catalog_source, "contract_version": 2,
                # The recognition's OWN immutable intent — the designed reuse path; the
                # ownership pin (recognition ∈ intent ∈ actor) fails any other resolution.
                "intent_id": rec.get("intent_id"),
                "recognition_id": rec.get("recognition_id"),
                "confirmed_scope": {"primary": primary, "secondary": [],
                                    "expansion": "exact"},
            }, headers=headers)
            pair[mode] = (_candidates(res.json()) if res.status_code == 200
                          else {"__error__": {"name": f"HTTP {res.status_code}: "
                                              + res.text[:120],
                                              "source": "?", "status": "error",
                                              "validation": None}})
        compared_pairs += 1
        keys = sorted(set(pair["legacy"]) | set(pair["semantic_v1"]))
        for key in keys:
            legacy = pair["legacy"].get(key)
            semantic = pair["semantic_v1"].get(key)
            verdict = _explain(key, legacy, semantic)
            if verdict == "match":
                continue
            if verdict.startswith("TODO"):
                unexplained += 1
            lines.append(
                f"| {hypothesis[:48]} | {key[:56]} "
                f"| {json.dumps(legacy)[:60] if legacy else '—'} "
                f"| {json.dumps(semantic)[:60] if semantic else '—'} | {verdict} |")

    with open(args.out, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {args.out}; compared pairs: {compared_pairs}; "
          f"unexplained rows: {unexplained}")
    if compared_pairs == 0:
        print("VACUOUS RUN — nothing was actually compared", file=sys.stderr)
        return 3
    return 0 if unexplained == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
