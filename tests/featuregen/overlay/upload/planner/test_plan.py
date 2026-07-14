from datetime import UTC, datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import PlanResolutionStatus, ReplayStrength
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import Need, Template

_NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _catalog(db, source):
    catalog = [
        (CanonicalRow(source, "accounts", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow(source, "accounts", "balance", "numeric", additivity="semi_additive", currency="USD"),
         "monetary_stock")]
    build_graph(db, source, [r for r, _ in catalog], concepts={content_hash(r): c for r, c in catalog})
    db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
               "VALUES (%s, %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
               (source, _NOW, _NOW))


def _tmpl():
    return Template(id="t_bal", family="f", intent="i",
                    needs=(Need(role="stock_col", concept="monetary_stock"),
                           Need(role="entity", concept="customer_id")),
                    params={}, aggregation="avg", additivity="semi_additive", explain="M", use_cases=(),
                    pit="trailing")


def test_plan_bindings_resolves_a_single_catalog_plan(db):
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    assert result.selected_plan_id is not None
    sel = next(p for p in result.candidate_plans if p.plan_id == result.selected_plan_id)
    assert sel.catalog_source == "core"
    assert {b.bound_object_ref for b in sel.ingredient_bindings} == {"public.accounts.balance",
                                                                     "public.accounts.customer_id"}
    assert result.replay_envelope.replay_strength is ReplayStrength.conditional   # watermark stamps, not a snapshot
    assert result.replay_envelope.planner_input_hash


def test_no_authorized_catalog_is_not_applicable(db):
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)   # nothing seeded
    result = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.not_applicable


def test_rejected_alternative_does_not_downgrade_a_resolved_result(db):
    # two catalogs: 'core' binds cleanly; 'bad' has an unsafe stock column (a rejected alternative). The
    # result must still be `resolved` (candidate-local-first).
    _catalog(db, "core")
    bad = [(CanonicalRow("bad", "accounts", "customer_id", "integer", is_grain=True), "customer_id"),
           (CanonicalRow("bad", "accounts", "amt", "numeric"), "monetary_stock"),
           (CanonicalRow("bad", "accounts", "amt2", "numeric"), "outcome_label")]  # noise, not bound
    build_graph(db, "bad", [r for r, _ in bad], concepts={content_hash(r): c for r, c in bad})
    db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
               "VALUES ('bad', %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
               (_NOW, _NOW))
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    assert len(result.candidate_plans) >= 2               # alternatives preserved
