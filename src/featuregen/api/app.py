"""FastAPI app factory. Run: uvicorn --factory featuregen.api.app:create_app"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.api.routes import (
    assist,
    auth,
    contract,
    features,
    graph,
    quarantine,
    search,
    uploads,
)
from featuregen.events.registry import event_registry
from featuregen.intake.llm import LLMClient
from featuregen.overlay.config import overlay_config_from_env, register_overlay_config
from featuregen.overlay.facts import register_overlay_event_types


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The same process bootstrap the worker and the test suite use: event schemas (idempotent)
    # + the sealed overlay config (fail-closed accessor needs it registered before any ingest).
    # The overlay OVERLAY_FACT_* schemas are what an upload's append_event validation needs
    # (production wires them via register_overlay in runtime.worker); register them here too.
    register_phase06_event_schemas()
    register_overlay_event_types(event_registry())
    register_overlay_config(overlay_config_from_env())
    yield


def create_app(llm_client: LLMClient | None = None) -> FastAPI:
    app = FastAPI(title="FeatureGen API", lifespan=_lifespan)
    app.state.llm_client = llm_client

    app.include_router(auth.router)
    app.include_router(uploads.router)
    app.include_router(search.router)
    app.include_router(quarantine.router)
    app.include_router(graph.router)
    app.include_router(features.router)
    app.include_router(assist.router)
    app.include_router(contract.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def create_app_from_env() -> FastAPI:
    """uvicorn --factory entrypoint. Wires the real (config-gated) Claude adapter when
    FEATUREGEN_LLM_PROVIDER=anthropic; otherwise the app runs without an LLM client
    (ingest un-enriched, assist endpoints 503). Never falls back to FakeLLM (D5)."""
    from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm

    cfg = ClaudeConfig.from_env()
    return create_app(llm_client=build_claude_llm(cfg) if cfg.enabled else None)
