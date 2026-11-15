"""Application HTTP d'AnswerLineage.

Lancement : ``uvicorn --factory answerlineage_api.app:create_app``, avec
``ANSWERLINEAGE_DATABASE_URL`` pour la base.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.guard import HostOriginMiddleware, guard_from_settings
from answerlineage_api.ingest.models import run_event_json_schema
from answerlineage_api.ingest.routes import router as ingestion_router
from answerlineage_api.lineage.routes import router as lineage_router
from answerlineage_api.responses import reject_invalid_request
from answerlineage_api.store.engine import create_engine, database_url_from_environment

# L'API a son propre numéro de version, distinct de celui du produit.
API_VERSION = "1.0.0"
DEFAULT_MAX_BATCH_EVENTS = 1000


def create_app(
    engine: AsyncEngine | None = None,
    *,
    max_batch_events: int = DEFAULT_MAX_BATCH_EVENTS,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
) -> FastAPI:
    """Construit l'application. Sans moteur fourni, la base vient de l'environnement.

    ``allowed_hosts`` et ``allowed_origins`` nomment les hôtes et les origines
    attendus en plus de la boucle locale ; sans eux, ils viennent de
    ``ANSWERLINEAGE_ALLOWED_HOSTS`` et ``ANSWERLINEAGE_ALLOWED_ORIGINS``.
    """
    guard = guard_from_settings(allowed_hosts, allowed_origins, os.environ)

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
    app.include_router(lineage_router)
    app.add_exception_handler(RequestValidationError, reject_invalid_request)
    app.add_middleware(HostOriginMiddleware, guard=guard)

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
