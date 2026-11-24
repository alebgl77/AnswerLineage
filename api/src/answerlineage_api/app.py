"""Application HTTP d'AnswerLineage.

Lancement : ``uvicorn --factory answerlineage_api.app:create_app``, avec
``ANSWERLINEAGE_DATABASE_URL`` pour la base.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp

from answerlineage_api.auth import matrix
from answerlineage_api.auth.access import (
    AccessRefusedError,
    Authentication,
    authentication_from_settings,
    authorize,
    refuse_access,
)
from answerlineage_api.auth.routes import router as authentication_router
from answerlineage_api.explorer import explorer_files
from answerlineage_api.guard import HostOriginMiddleware, guard_from_settings
from answerlineage_api.headers import SecurityHeadersMiddleware
from answerlineage_api.health import router as health_router
from answerlineage_api.incidents.models import (
    confirmation_json_schema,
    declaration_json_schema,
    quarantine_request_json_schema,
)
from answerlineage_api.incidents.routes import router as incidents_router
from answerlineage_api.incidents.signature import QuarantineSigning, signing_from_settings
from answerlineage_api.ingest.body import max_body_bytes_from_settings
from answerlineage_api.ingest.coverage import router as coverage_router
from answerlineage_api.ingest.dead_letters import replay_json_schema
from answerlineage_api.ingest.dead_letters import router as dead_letters_router
from answerlineage_api.ingest.duplicates import router as duplicates_router
from answerlineage_api.ingest.models import run_event_json_schema
from answerlineage_api.ingest.routes import router as ingestion_router
from answerlineage_api.ingest.undescribed import router as undescribed_router
from answerlineage_api.lineage.reading import (
    ReadTimedOut,
    read_timeout_from_settings,
    refuse_timed_out,
)
from answerlineage_api.lineage.routes import router as lineage_router
from answerlineage_api.pseudonyms.models import revelation_request_json_schema
from answerlineage_api.pseudonyms.routes import router as recipients_router
from answerlineage_api.pseudonyms.vault import PseudonymVault, vault_from_settings
from answerlineage_api.recall.models import decision_request_json_schema
from answerlineage_api.recall.routes import router as recall_router
from answerlineage_api.responses import reject_invalid_request
from answerlineage_api.store.engine import create_engine, database_url_from_environment

# L'API a son propre numéro de version, distinct de celui du produit.
API_VERSION = "1.0.0"
DEFAULT_MAX_BATCH_EVENTS = 1000


class _Application(FastAPI):
    """L'application, dont toute réponse porte les en-têtes de sécurité.

    Ils sont posés autour de toute la pile, au-delà du middleware qui répond 500
    à une panne : un middleware ajouté par ``add_middleware`` resterait en deçà.
    """

    def build_middleware_stack(self) -> ASGIApp:
        authentication: Authentication = self.state.authentication
        return SecurityHeadersMiddleware(
            super().build_middleware_stack(), authentication.connect_sources()
        )


def create_app(
    engine: AsyncEngine | None = None,
    *,
    max_batch_events: int = DEFAULT_MAX_BATCH_EVENTS,
    max_body_bytes: int | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    ui_directory: str | os.PathLike[str] | None = None,
    read_timeout_ms: int | None = None,
    authentication: Authentication | None = None,
    quarantine_signing: QuarantineSigning | None = None,
    pseudonym_vault: PseudonymVault | None = None,
) -> FastAPI:
    """Construit l'application. Sans moteur fourni, la base vient de l'environnement.

    ``allowed_hosts`` et ``allowed_origins`` nomment les hôtes et les origines
    attendus en plus de la boucle locale ; sans eux, ils viennent de
    ``ANSWERLINEAGE_ALLOWED_HOSTS`` et ``ANSWERLINEAGE_ALLOWED_ORIGINS``.
    ``ui_directory`` est le dossier construit de l'explorateur, servi à la
    racine ; sans lui, il vient de ``ANSWERLINEAGE_UI_DIR``, et l'API ne sert
    aucune page quand il n'est pas donné.
    ``read_timeout_ms`` est le délai maximal d'une lecture du graphe, en
    millisecondes ; sans lui, il vient de ``ANSWERLINEAGE_READ_TIMEOUT_MS``,
    et vaut 5 secondes quand la variable n'est pas donnée.
    ``max_body_bytes`` borne en octets le corps d'une requête d'ingestion, reçu
    comme déployé ; sans lui, il vient de ``ANSWERLINEAGE_MAX_BODY_BYTES``.
    ``authentication`` dit comment l'API reconnaît ses appelants ; sans elle,
    elle vient de l'environnement (``ANSWERLINEAGE_OIDC_*``), et l'API ne démarre
    pas sans émetteur, à moins d'être désactivée pour la démonstration
    (``ANSWERLINEAGE_AUTH=disabled``), sur la boucle locale seule.
    ``quarantine_signing`` signe la diffusion des périmètres en quarantaine aux
    proxys ; sans elle, ses secrets viennent de ``ANSWERLINEAGE_QUARANTINE_SECRET``
    et ``ANSWERLINEAGE_QUARANTINE_PREVIOUS_SECRET``, et la diffusion n'est pas
    signée quand ils ne sont pas donnés.
    ``pseudonym_vault`` est le coffre à clés qui calcule les pseudonymes des
    destinataires, pour les révéler ; sans lui, il vient de l'environnement
    (``ANSWERLINEAGE_PSEUDONYM_KEYS_FILE`` ou ``ANSWERLINEAGE_PSEUDONYM_KEYS``), et
    aucune révélation n'est possible quand aucune clé n'est réglée.
    """
    guard = guard_from_settings(allowed_hosts, allowed_origins, os.environ)
    body_limit = max_body_bytes_from_settings(max_body_bytes, os.environ)
    explorer = explorer_files(ui_directory, os.environ)
    read_timeout = read_timeout_from_settings(read_timeout_ms, os.environ)
    access = authentication_from_settings(authentication, os.environ)
    access.check_guard(guard)
    signing = signing_from_settings(quarantine_signing, os.environ)
    vault = pseudonym_vault if pseudonym_vault is not None else vault_from_settings(os.environ)

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

    app = _Application(
        title="AnswerLineage",
        version=API_VERSION,
        summary="Lignage et rappel des réponses d'agents",
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        # Les pages de documentation chargeraient des scripts depuis un CDN.
        docs_url=None,
        redoc_url=None,
        # Avant toute route, et avant ses paramètres : le jeton, puis la matrice des droits.
        dependencies=[Depends(authorize)],
    )
    app.state.engine = engine
    app.state.max_batch_events = max_batch_events
    app.state.read_timeout_ms = read_timeout
    app.state.max_body_bytes = body_limit
    app.state.authentication = access
    app.state.quarantine_signing = signing
    app.state.pseudonym_vault = vault
    app.include_router(ingestion_router)
    app.include_router(duplicates_router)
    app.include_router(dead_letters_router)
    app.include_router(undescribed_router)
    app.include_router(coverage_router)
    app.include_router(lineage_router)
    app.include_router(incidents_router)
    app.include_router(recall_router)
    app.include_router(recipients_router)
    app.include_router(health_router)
    app.include_router(authentication_router)
    # Une route que la matrice ne déclare pas ne s'ouvre pas par oubli : l'application
    # ne se construit pas.
    undeclared = matrix.undeclared(app.routes)
    if undeclared:
        raise RuntimeError("Matrice des droits incomplète : " + " ; ".join(undeclared) + ".")
    app.add_exception_handler(AccessRefusedError, refuse_access)
    app.add_exception_handler(RequestValidationError, reject_invalid_request)
    app.add_exception_handler(ReadTimedOut, refuse_timed_out)
    app.add_middleware(HostOriginMiddleware, guard=guard)
    if explorer is not None:
        # Monté en dernier : les routes de l'API passent avant les fichiers.
        app.mount("/", explorer, name="explorer")

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            document = get_openapi(
                title=app.title, version=app.version, summary=app.summary, routes=app.routes
            )
            schemas = document.setdefault("components", {}).setdefault("schemas", {})
            run_event = run_event_json_schema()
            schemas.update(run_event.pop("$defs", {}))
            schemas["RunEvent"] = run_event
            declaration = declaration_json_schema()
            schemas.update(declaration.pop("$defs", {}))
            schemas["IncidentDeclaration"] = declaration
            schemas["QuarantineRequest"] = quarantine_request_json_schema()
            confirmation = confirmation_json_schema()
            schemas.update(confirmation.pop("$defs", {}))
            schemas["ProposalConfirmation"] = confirmation
            schemas["DeadLetterReplay"] = replay_json_schema()
            revelation = revelation_request_json_schema()
            schemas.update(revelation.pop("$defs", {}))
            schemas["RevelationRequest"] = revelation
            schemas["RecallDecisionRequest"] = decision_request_json_schema()
            matrix.publish(document)
            app.openapi_schema = document
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
