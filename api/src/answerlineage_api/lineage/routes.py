"""Routes de lecture du lignage.

Chaque route lit le graphe sous un délai maximal (``reading``) : au-delà, elle
répond ``503`` et ``query_timeout``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from answerlineage_api.ingest.models import FieldError
from answerlineage_api.lineage import coverage, impact, overview, trace
from answerlineage_api.lineage.models import (
    AnswerList,
    CoverageList,
    DatasetList,
    GraphView,
    ImpactResult,
    Sources,
    Window,
)
from answerlineage_api.lineage.paging import CURSOR_PATTERN, Cursor, CursorError
from answerlineage_api.lineage.reading import TIMED_OUT, reading
from answerlineage_api.responses import RequestRefused, RequestRejected, refused

router = APIRouter(prefix="/api/v1", tags=["lignage"])

# PostgreSQL ne range pas le caractère nul dans un texte : il ne peut désigner aucune ligne.
_NO_NUL = r"^[^\x00]*$"
_INVALID: dict[int | str, dict[str, Any]] = {
    422: {"model": RequestRejected, "description": "Un paramètre est invalide."}
}


def _rejected(path: str, kind: str, message: str) -> JSONResponse:
    """Un paramètre bien formé mais inutilisable : son chemin et le motif, jamais sa valeur."""
    rejected = RequestRejected(errors=[FieldError(path=path, type=kind, message=message)])
    return JSONResponse(status_code=422, content=rejected.model_dump())


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
    responses={**_INVALID, **TIMED_OUT},
)
async def find_answers(
    request: Request, channel: Channel, message_ref: MessageRef, limit: Limit = 20
) -> AnswerList:
    """Le destinataire d'un message retrouve la réponse d'agent qu'il porte."""
    async with reading(request) as reader:
        return AnswerList(answers=await trace.find_by_message(reader, channel, message_ref, limit))


@router.get(
    "/answers/{answer_id}/sources",
    response_model=Sources,
    summary="Remonte d'une réponse à ses sources",
    responses={
        404: {"model": RequestRefused, "description": "Aucune réponse ne porte cet identifiant."},
        **_INVALID,
        **TIMED_OUT,
    },
)
async def answer_sources(request: Request, answer_id: AnswerId) -> Sources | JSONResponse:
    """Jeux de données et versions lus, outils appelés et agents intervenus, sans contenu."""
    async with reading(request) as reader:
        answer = await trace.find_answer(reader, answer_id)
        if answer is None:
            return refused(404, "unknown_answer")
        return await trace.sources(reader, answer)


GraphLimit = Annotated[
    int, Query(ge=1, le=200, description="Nombre maximal de réponses dans la vue.")
]
CursorParam = Annotated[
    str | None,
    Query(
        alias="cursor",
        pattern=CURSOR_PATTERN,
        description=(
            "Curseur opaque de la page suivante, tel que l'API le donne dans « next_cursor »."
        ),
    ),
]


def _cursor(value: str | None) -> Cursor | JSONResponse | None:
    if value is None:
        return None
    try:
        return Cursor.decode(value)
    except CursorError:
        return _rejected("/query/cursor", "cursor_invalid", "Ce curseur ne désigne aucune page.")


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
        **TIMED_OUT,
    },
)
async def graph(
    request: Request,
    limit: GraphLimit = 60,
    cursor_value: CursorParam = None,
    answer: OneAnswer = None,
) -> GraphView | JSONResponse:
    """Données, outils, agents, réponses et destinataires, avec la chaîne de chaque réponse."""
    cursor = _cursor(cursor_value)
    if isinstance(cursor, JSONResponse):
        return cursor
    async with reading(request) as reader:
        if answer is None:
            return await overview.overview(reader, limit=limit, cursor=cursor)
        found = await trace.find_answer(reader, answer)
        if found is None:
            return refused(404, "unknown_answer")
        return await overview.view_of_answer(reader, found)


DatasetLimit = Annotated[
    int, Query(ge=1, le=500, description="Nombre maximal de jeux de données renvoyés.")
]


@router.get(
    "/datasets",
    response_model=DatasetList,
    summary="Liste les jeux de données connus du graphe",
    responses={**_INVALID, **TIMED_OUT},
)
async def datasets(request: Request, limit: DatasetLimit = 200) -> DatasetList:
    """Les jeux de données lus ou écrits par un run, par espace de noms puis par nom."""
    async with reading(request) as reader:
        return await overview.datasets(reader, limit=limit)


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
        **TIMED_OUT,
    },
)
async def propagate(
    request: Request,
    namespace: Namespace,
    name: DatasetNameParam,
    start: WindowStart,
    end: WindowEnd,
    limit: GraphLimit = 200,
    cursor_value: CursorParam = None,
) -> ImpactResult | JSONResponse:
    """Réponses dont un run a lu le jeu de données pendant la fenêtre, et leurs destinataires."""
    if end <= start:
        return _rejected("/query/to", "window_empty", "La fin de la fenêtre doit suivre son début.")
    cursor = _cursor(cursor_value)
    if isinstance(cursor, JSONResponse):
        return cursor
    async with reading(request) as reader:
        result = await impact.propagate(
            reader, namespace=namespace, name=name, start=start, end=end, limit=limit, cursor=cursor
        )
    return result if result is not None else refused(404, "unknown_dataset")


EmitterLimit = Annotated[
    int, Query(ge=1, le=500, description="Nombre maximal d'émetteurs renvoyés.")
]
PeriodStart = Annotated[
    AwareDatetime,
    Query(alias="from", description="Début de la période, inclus (RFC 3339, avec fuseau)."),
]
PeriodEnd = Annotated[
    AwareDatetime,
    Query(alias="to", description="Fin de la période, exclue (RFC 3339, avec fuseau)."),
]


@router.get(
    "/coverage",
    response_model=CoverageList,
    summary="Donne la couverture du lignage de chaque émetteur sur une période",
    responses={**_INVALID, **TIMED_OUT},
)
async def coverage_by_emitter(
    request: Request, start: PeriodStart, end: PeriodEnd, limit: EmitterLimit = 100
) -> CoverageList | JSONResponse:
    """Part des appels vus présents dans le graphe, trous connus, émetteurs muets."""
    if end <= start:
        return _rejected("/query/to", "window_empty", "La fin de la période doit suivre son début.")
    async with reading(request) as reader:
        emitters, more = await coverage.by_emitter(
            reader, start=start, end=end, now=datetime.now(UTC), limit=limit
        )
    return CoverageList(window=Window(start=start, end=end), emitters=emitters, more=more)
