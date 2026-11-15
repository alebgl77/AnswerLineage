"""Application HTTP d'AnswerLineage.

Lancement : ``uvicorn --factory answerlineage_api.app:create_app``, avec
``ANSWERLINEAGE_DATABASE_URL`` pour la base.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.ingest.models import run_event_json_schema
from answerlineage_api.ingest.routes import router as ingestion_router
from answerlineage_api.store.engine import create_engine, database_url_from_environment

# L'API a son propre numéro de version, distinct de celui du produit.
API_VERSION = "1.0.0"
DEFAULT_MAX_BATCH_EVENTS = 1000


def create_app(
    engine: AsyncEngine | None = None, *, max_batch_events: int = DEFAULT_MAX_BATCH_EVENTS
) -> FastAPI:
    """Construit l'application. Sans moteur fourni, la base vient de l'environnement."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = getattr(app.state, "engine", None) is None
        if owned:
            app.state.engine = create_engine(database_url_from_environment())
        try:
            yield
        finally:
            if owned:
                await app.state.engine.dispose()
                app.state.engine = None

    app = FastAPI(
        title="AnswerLineage",
        version=API_VERSION,
        summary="Lignage et rappel des réponses d'agents",
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        # Les pages de documentation chargeraient des scripts depuis un CDN.
        docs_url=None,
        redoc_url=None,
    )
    app.state.engine = engine
    app.state.max_batch_events = max_batch_events
    app.include_router(ingestion_router)

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            document = get_openapi(
                title=app.title, version=app.version, summary=app.summary, routes=app.routes
            )
            schemas = document.setdefault("components", {}).setdefault("schemas", {})
            run_event = run_event_json_schema()
            schemas.update(run_event.pop("$defs", {}))
            schemas["RunEvent"] = run_event
            app.openapi_schema = document
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
