"""Routes de lecture du lignage."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.ingest.models import FieldError
from answerlineage_api.lineage import impact, overview, trace
from answerlineage_api.lineage.models import (
    AnswerList,
    DatasetList,
    GraphView,
    ImpactResult,
    Sources,
)
from answerlineage_api.responses import RequestRefused, RequestRejected, refused

router = APIRouter(prefix="/api/v1", tags=["lignage"])

# PostgreSQL ne range pas le caractère nul dans un texte : il ne peut désigner aucune ligne.
_NO_NUL = r"^[^\x00]*$"
_INVALID: dict[int | str, dict[str, Any]] = {
    422: {"model": RequestRejected, "description": "Un paramètre est invalide."}
}

Channel = Annotated[
    str,
    Query(
        min_length=1,
        max_length=200,
        pattern=_NO_NUL,
        description="Canal de diffusion du message, tel que l'émetteur le nomme.",
    ),
]
MessageRef = Annotated[
    str,
    Query(
        min_length=1,
        max_length=500,
        pattern=_NO_NUL,
        description="Référence du message dans ce canal.",
    ),
]
Limit = Annotated[int, Query(ge=1, le=100, description="Nombre maximal de réponses renvoyées.")]
AnswerId = Annotated[
    str,
    Path(
        pattern=r"^[0-9a-f]{48}$", description="Identifiant de la réponse, tel que l'API le donne."
    ),
]


@router.get(
    "/answers",
    response_model=AnswerList,
    summary="Retrouve les réponses diffusées par un message",
    responses={**_INVALID},
)
async def find_answers(
    request: Request, channel: Channel, message_ref: MessageRef, limit: Limit = 20
) -> AnswerList:
    """Le destinataire d'un message retrouve la réponse d'agent qu'il porte."""
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        return AnswerList(answers=await trace.find_by_message(conn, channel, message_ref, limit))


@router.get(
    "/answers/{answer_id}/sources",
    response_model=Sources,
    summary="Remonte d'une réponse à ses sources",
    responses={
        404: {"model": RequestRefused, "description": "Aucune réponse ne porte cet identifiant."},
        **_INVALID,
    },
)
async def answer_sources(request: Request, answer_id: AnswerId) -> Sources | JSONResponse:
    """Jeux de données et versions lus, outils appelés et agents intervenus, sans contenu."""
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        answer = await trace.find_answer(conn, answer_id)
        if answer is None:
            return refused(404, "unknown_answer")
        return await trace.sources(conn, answer)


GraphLimit = Annotated[
    int, Query(ge=1, le=200, description="Nombre maximal de réponses dans la vue.")
]
Before = Annotated[
    AwareDatetime | None,
    Query(description="Ne garder que les réponses produites avant cet instant (RFC 3339)."),
]


OneAnswer = Annotated[
    str | None,
    Query(
        pattern=r"^[0-9a-f]{48}$",
        description="Identifiant d'une réponse : la vue se réduit alors à sa chaîne.",
    ),
]


@router.get(
    "/graph",
    response_model=GraphView,
    summary="Donne le graphe en cinq colonnes des réponses les plus récentes, ou d'une réponse",
    responses={
        404: {"model": RequestRefused, "description": "Aucune réponse ne porte cet identifiant."},
        **_INVALID,
    },
)
async def graph(
    request: Request, limit: GraphLimit = 60, before: Before = None, answer: OneAnswer = None
) -> GraphView | JSONResponse:
    """Données, outils, agents, réponses et destinataires, avec la chaîne de chaque réponse."""
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        if answer is None:
            return await overview.overview(conn, limit=limit, before=before)
        found = await trace.find_answer(conn, answer)
        if found is None:
            return refused(404, "unknown_answer")
        return await overview.view_of_answer(conn, found)


DatasetLimit = Annotated[
    int, Query(ge=1, le=500, description="Nombre maximal de jeux de données renvoyés.")
]


@router.get(
    "/datasets",
    response_model=DatasetList,
    summary="Liste les jeux de données connus du graphe",
    responses={**_INVALID},
)
async def datasets(request: Request, limit: DatasetLimit = 200) -> DatasetList:
    """Les jeux de données lus ou écrits par un run, par espace de noms puis par nom."""
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        return await overview.datasets(conn, limit=limit)


Namespace = Annotated[
    str,
    Query(
        min_length=1,
        max_length=500,
        pattern=_NO_NUL,
        description="Espace de noms du jeu de données, par exemple « bigquery ».",
    ),
]
DatasetNameParam = Annotated[
    str,
    Query(min_length=1, max_length=500, pattern=_NO_NUL, description="Nom du jeu de données."),
]
WindowStart = Annotated[
    AwareDatetime,
    Query(alias="from", description="Début de la fenêtre, inclus (RFC 3339, avec fuseau)."),
]
WindowEnd = Annotated[
    AwareDatetime,
    Query(alias="to", description="Fin de la fenêtre, exclue (RFC 3339, avec fuseau)."),
]


@router.get(
    "/impact",
    response_model=ImpactResult,
    summary="Propage un incident de données jusqu'aux réponses et aux destinataires",
    responses={
        404: {"model": RequestRefused, "description": "Ce jeu de données n'est pas connu."},
        **_INVALID,
    },
)
async def propagate(
    request: Request,
    namespace: Namespace,
    name: DatasetNameParam,
    start: WindowStart,
    end: WindowEnd,
    limit: GraphLimit = 200,
    before: Before = None,
) -> ImpactResult | JSONResponse:
    """Réponses dont un run a lu le jeu de données pendant la fenêtre, et leurs destinataires."""
    if end <= start:
        rejected = RequestRejected(
            errors=[
                FieldError(
                    path="/query/to",
                    type="window_empty",
                    message="La fin de la fenêtre doit suivre son début.",
                )
            ]
        )
        return JSONResponse(status_code=422, content=rejected.model_dump())
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        result = await impact.propagate(
            conn, namespace=namespace, name=name, start=start, end=end, limit=limit, before=before
        )
    return result if result is not None else refused(404, "unknown_dataset")
