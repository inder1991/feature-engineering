"""FastAPI app factory. Run: uvicorn --factory featuregen.api.app:create_app"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI

from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.api.deps import get_conn, get_identity
from featuregen.api.routes import (
    admin,
    assist,
    auth,
    contract,
    entity,
    features,
    governance,
    governance_dashboard,
    graph,
    ingestion_runs,
    integrations,
    lineage,
    quarantine,
    readiness,
    search,
    uploads,
)
from featuregen.config import get_settings
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.db.migrations import apply_migrations, pending_migrations
from featuregen.events.registry import event_registry
from featuregen.intake.llm import LLMClient
from featuregen.overlay.config import overlay_config_from_env, register_overlay_config
from featuregen.overlay.facts import register_overlay_event_types

logger = logging.getLogger(__name__)


def _startup_migration_check(app: FastAPI) -> None:
    """Guard against the long-lived-DB-drift footgun: a schema behind the code produces a confusing
    runtime 500 (a missing column), not an obvious error. On startup, detect pending migrations against
    the runtime DSN and EITHER auto-apply (FEATUREGEN_AUTO_MIGRATE=1, handy for dev/demo) or log a loud,
    actionable warning — and record the outcome on app.state so /health can report a degraded schema.
    Read-only + fail-open: no DSN (e.g. tests override get_conn) or an unreachable DB never blocks
    startup (a bounded connect_timeout keeps a black-holed DB from hanging boot)."""
    app.state.schema_pending = []
    dsn = get_settings().dsn
    if not dsn:
        return
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            pending = pending_migrations(conn)
            if not pending:
                return
            if os.environ.get("FEATUREGEN_AUTO_MIGRATE") == "1":
                apply_migrations(conn)
                logger.warning("auto-applied %d pending migration(s): %s",
                               len(pending), ", ".join(pending))
            else:
                app.state.schema_pending = pending
                logger.warning(
                    "DATABASE SCHEMA IS BEHIND THE CODE: %d migration(s) pending (%s). Run "
                    "`python -m featuregen migrate` (or set FEATUREGEN_AUTO_MIGRATE=1) — endpoints "
                    "touching the new schema will otherwise 500.", len(pending), ", ".join(pending[:8]))
    except Exception:  # noqa: BLE001 — a startup DB check must never prevent the app from booting
        logger.warning("could not check pending migrations at startup", exc_info=True)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The same process bootstrap the worker and the test suite use: event schemas (idempotent)
    # + the sealed overlay config (fail-closed accessor needs it registered before any ingest).
    # The overlay OVERLAY_FACT_* schemas are what an upload's append_event validation needs
    # (production wires them via register_overlay in runtime.worker); register them here too.
    register_phase06_event_schemas()
    register_overlay_event_types(event_registry())
    register_overlay_config(overlay_config_from_env())
    _startup_migration_check(app)
    yield


def create_app(llm_client: LLMClient | None = None) -> FastAPI:
    app = FastAPI(title="FeatureGen API", lifespan=_lifespan)
    app.state.llm_client = llm_client

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(uploads.router)
    app.include_router(ingestion_runs.router)
    app.include_router(integrations.router)
    app.include_router(search.router)
    app.include_router(quarantine.router)
    app.include_router(readiness.router)
    app.include_router(graph.router)
    app.include_router(lineage.router)
    app.include_router(features.router)
    app.include_router(governance.router)
    app.include_router(governance_dashboard.router)
    app.include_router(assist.router)
    app.include_router(contract.router)
    app.include_router(entity.router)

    @app.get("/health")
    def health() -> dict:
        # Reports the schema status captured at startup (no per-call DB churn): a schema behind the
        # code is 'degraded', not a false 'ok' — so a readiness probe catches the broken-deploy /
        # unpackaged-migrations footgun instead of letting endpoints 500 later.
        pending = getattr(app.state, "schema_pending", [])
        if pending:
            return {"status": "degraded", "schema": "behind", "pending_migrations": len(pending)}
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics(
        conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
        identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    ) -> dict:
        """Operational snapshot for dashboards/alerts: in-process counters + projection lag + degraded
        / skipped markers + pending-migration count. Identity-gated (no unauthenticated DB load).
        Liveness/readiness stays on /health."""
        from featuregen.projections.runner import projection_lag
        from featuregen.runtime.observability import counters
        lag = {n: projection_lag(conn, n) for n in ("overlay", "stage_primary")}
        degraded = conn.execute("SELECT count(*) FROM projection_degraded").fetchone()[0]
        skipped = conn.execute("SELECT count(*) FROM projection_skips").fetchone()[0]
        return {
            "counters": counters.snapshot().get("counters", {}),
            "projection_lag": lag,
            "degraded_markers": int(degraded),
            "skipped_events": int(skipped),
            "pending_migrations": len(getattr(app.state, "schema_pending", [])),
        }

    return app


def create_app_from_env() -> FastAPI:
    """uvicorn --factory entrypoint. Wires the real (config-gated) Claude adapter when
    FEATUREGEN_LLM_PROVIDER=anthropic; otherwise the app runs without an LLM client
    (ingest un-enriched, assist endpoints 503). Never falls back to FakeLLM (D5)."""
    from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm

    cfg = ClaudeConfig.from_env()
    return create_app(llm_client=build_claude_llm(cfg) if cfg.enabled else None)
