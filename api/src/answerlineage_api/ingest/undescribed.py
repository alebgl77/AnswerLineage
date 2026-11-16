"""Les outils à décrire : ceux dont les appels ont des lectures non décrites.

Un point de capture qui voit l'appel d'un outil sans règle de lecture ni SQL
déclaré le marque : la lecture de l'appel est « non résolue », motif
``undescribed_tool``. Cette lecture rassemble les outils ainsi marqués, par
émetteur, à partir des événements reçus : elle ne touche pas au graphe, et un
exploitant sait quelles règles écrire sans ouvrir les journaux des proxys.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from answerlineage_api.identifiers import normalize_identifier
from answerlineage_api.ingest.models import TOOL_CALL_FACET
from answerlineage_api.lineage.reading import TIMED_OUT, reading
from answerlineage_api.responses import RequestRejected
from answerlineage_api.store import schema

# Motif que porte la lecture d'un appel dont l'outil n'est pas décrit.
UNDESCRIBED_TOOL: Final = "undescribed_tool"
_MARKED: Final = {"run": {"facets": {TOOL_CALL_FACET: {"unresolvedReason": UNDESCRIBED_TOOL}}}}


class UndescribedTool(BaseModel):
    """Un outil appelé sans règle de lecture, vu par un émetteur."""

    emitter: str = Field(description="Émetteur : l'espace de noms des jobs, « mcp://<serveur> ».")
    tool: str = Field(description="Nom de l'outil, le nom du job de ses appels.")
    calls: int = Field(description="Événements reçus pendant la période qui le marquent.")
    last_seen: datetime = Field(description="Réception du plus récent de ces événements.")


class UndescribedToolList(BaseModel):
    tools: list[UndescribedTool]


router = APIRouter(prefix="/api/v1", tags=["ingestion"])

Days = Annotated[
    int, Query(ge=1, le=31, description="Période lue, en jours de réception jusqu'à maintenant.")
]
Limit = Annotated[
    int, Query(ge=1, le=1000, description="Nombre maximal d'outils (émetteur et outil) renvoyés.")
]


@router.get(
    "/ingestion/undescribed-tools",
    response_model=UndescribedToolList,
    summary="Liste les outils appelés sans règle de lecture, par émetteur",
    responses={
        422: {"model": RequestRejected, "description": "Un paramètre est invalide."},
        **TIMED_OUT,
    },
)
async def undescribed_tools(
    request: Request, days: Days = 7, limit: Limit = 100
) -> UndescribedToolList:
    """Du plus récemment vu au plus ancien, sur les événements reçus pendant ``days`` jours.

    Un outil décrit depuis disparaît de la liste quand ses derniers appels non
    décrits sortent de la période. Comme toute lecture, elle s'exécute sous le
    délai maximal des lectures.
    """
    table = schema.event_raw
    tool = table.c.payload["job"]["name"].astext
    last_seen = func.max(table.c.recorded_at).label("last_seen")
    statement = (
        select(table.c.emitter, tool.label("tool"), func.count().label("calls"), last_seen)
        .where(
            table.c.tool_call,
            table.c.recorded_at >= func.now() - timedelta(days=days),
            table.c.payload.contains(_MARKED),
        )
        .group_by(table.c.emitter, tool)
        .order_by(last_seen.desc(), table.c.emitter, tool)
        .limit(limit)
    )
    async with reading(request) as reader:
        found = (await reader.execute(statement, {})).all()
    return UndescribedToolList(
        tools=[
            UndescribedTool(
                emitter=row.emitter,
                tool=normalize_identifier(row.tool),
                calls=row.calls,
                last_seen=row.last_seen,
            )
            for row in found
        ]
    )
