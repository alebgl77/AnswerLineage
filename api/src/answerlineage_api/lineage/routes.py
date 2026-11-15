"""Routes de lecture du lignage."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.lineage import overview, trace
from answerlineage_api.lineage.models import AnswerList, GraphView, Sources
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


@router.get(
    "/graph",
    response_model=GraphView,
    summary="Donne le graphe en cinq colonnes des réponses les plus récentes",
    responses={**_INVALID},
)
async def graph(request: Request, limit: GraphLimit = 60, before: Before = None) -> GraphView:
    """Données, outils, agents, réponses et destinataires, avec la chaîne de chaque réponse."""
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        return await overview.overview(conn, limit=limit, before=before)
