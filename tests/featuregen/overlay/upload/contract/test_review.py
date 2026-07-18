"""Phase 4 — critique→refine loop + MCV."""
from datetime import UTC, datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.review import author_contract, validate_minimum
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=UTC)


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "churned", "boolean")])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (NOW, NOW))


def _draft(derives, defn="vague"):
    return ContractDraft(feature_name="avg_balance_90d", definition=defn, grain_table="accounts",
                         aggregation="avg_90d", as_of_column="posted_at", derives_from=derives,
                         derives_pairs=tuple(("bank", d) for d in derives))


class _SeqLLM:
    """Returns responses in CALL order regardless of inputs."""
    def __init__(self, responses):
        self._r, self._i = responses, 0

    def call(self, request):
        from featuregen.intake.llm import LLMResult
        out = self._r[min(self._i, len(self._r) - 1)]
        self._i += 1
        return LLMResult(output=out, self_reported_scores={}, call_ref="", status="ok")


def test_mcv_is_the_deterministic_gauntlet(db):
    _bank(db)
    check = validate_minimum(db, _draft(["public.accounts.balance"]),
                             target_ref="public.accounts.churned", now=NOW)
    assert check.ok and check.reasons == []
    # a draft that derives from the target label fails MCV — deterministically
    bad = validate_minimum(db, _draft(["public.accounts.churned"]),
                           target_ref="public.accounts.churned", now=NOW)
    assert not bad.ok and "leaks target" in bad.reasons[0]


def test_critique_refine_loop_converges(db):
    _bank(db)
    draft = _draft(["public.accounts.balance"], defn="vague")
    client = _SeqLLM([
        {"findings": ["definition is vague — state the window and grain"]},  # critique round 1
        {"definition": "Average end-of-day ledger balance per account over 90 days."},  # refine round 1
        {"findings": []},                                                    # critique round 2 → clean
    ])
    final, unresolved = author_contract(db, draft, client, target_ref="public.accounts.churned",
                                        now=NOW, budget=3)
    assert unresolved == []                                                  # MCV clean, critique clean
    assert final.definition == "Average end-of-day ledger balance per account over 90 days."  # refined


def test_structural_defect_surfaces_and_stops(db):
    _bank(db)
    # a leaky draft: MCV fails, critique is silent -> the loop must surface, not spin
    draft = _draft(["public.accounts.churned"])
    client = _SeqLLM([{"findings": []}])
    final, unresolved = author_contract(db, draft, client, target_ref="public.accounts.churned",
                                        now=NOW, budget=3)
    assert unresolved and "leaks target" in unresolved[0]


def test_mcv_grounding_uses_live_graph_not_the_draft(db):
    # B2: a draft claiming a column that no longer exists in the graph must fail grounding.
    _bank(db)
    ghost = _draft(["public.accounts.vanished"])
    check = validate_minimum(db, ghost, now=NOW)
    assert not check.ok and "ungrounded" in check.reasons[0]


def test_critique_is_audited(db):
    # M5: the critique call is recorded in llm_call (routed through the audited seam)
    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.upload.contract.review import critique_contract
    _bank(db)
    client = FakeLLM(script={"overlay.contract.critique": FakeResponse(
        output={"findings": ["state the window"]})})
    before = db.execute("SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    findings = critique_contract(db, _draft(["public.accounts.balance"]), client)
    after = db.execute("SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert findings == ["state the window"]
    assert after == before + 1


def test_mcv_rejects_fabricated_catalog_pair(db):
    # M4: a client-supplied derives_pairs catalog that the column doesn't live in must fail closed
    _bank(db)   # 'bank' catalog has public.accounts.balance
    draft = ContractDraft("f", "def", "accounts", "avg_90d", "posted_at",
                          ["public.accounts.balance"],
                          derives_pairs=(("fabricated_catalog", "public.accounts.balance"),))
    check = validate_minimum(db, draft, now=NOW)
    assert not check.ok and "unknown column" in check.reasons[0]
