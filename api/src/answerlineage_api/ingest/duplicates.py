"""Compteur des événements reçus en double, et sa lecture.

Les services sont sans état : le compteur vit dans PostgreSQL, dans la
transaction qui reçoit les événements. Il se tient par émetteur et par jour
(date UTC de réception), la maille à laquelle un exploitant cherche d'où vient
une tempête de rejeux.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import Date, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.identifiers import normalize_identifier
from answerlineage_api.ingest.models import RunEvent
from answerlineage_api.lineage.reading import TIMED_OUT, reading
from answerlineage_api.responses import RequestRejected
from answerlineage_api.store import schema


def emitter_of(event: RunEvent) -> str:
    """Émetteur d'un événement : l'espace de noms de son job, sous sa forme normalisée.

    L'ingestion n'accepte qu'un espace de noms qui a la forme d'un nom, borné en
    longueur : c'est celui que le graphe retient.
    """
    return normalize_identifier(event.job.namespace)


async def count_duplicates(conn: AsyncConnection, emitters: list[str]) -> None:
    """Ajoute un doublon par émetteur listé, au jour UTC courant de la base.

    Les lignes sont écrites dans l'ordre de leur clé : deux envois simultanés
    prennent les verrous dans le même ordre et ne s'interbloquent pas.
    """
    if not emitters:
        return
    table = schema.ingest_duplicate
    today = func.timezone("UTC", func.now()).cast(Date)
    statement = insert(table).values(
        [
            {"emitter": emitter, "day": today, "duplicates": count}
            for emitter, count in sorted(Counter(emitters).items())
        ]
    )
    await conn.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.emitter, table.c.day],
            set_={"duplicates": table.c.duplicates + statement.excluded.duplicates},
        )
    )


class DuplicateCount(BaseModel):
    """Doublons reçus d'un émetteur, un jour."""

    emitter: str = Field(description="Espace de noms du job des événements.")
    day: date = Field(description="Jour UTC de réception.")
    count: int = Field(description="Événements reçus alors qu'ils étaient déjà connus.")


class DuplicateList(BaseModel):
    duplicates: list[DuplicateCount]


router = APIRouter(prefix="/api/v1", tags=["ingestion"])

Limit = Annotated[
    int, Query(ge=1, le=1000, description="Nombre maximal de lignes (émetteur et jour) renvoyées.")
]


@router.get(
    "/ingestion/duplicates",
    response_model=DuplicateList,
    summary="Compte les événements reçus en double, par émetteur et par jour",
    responses={
        422: {"model": RequestRejected, "description": "Un paramètre est invalide."},
        **TIMED_OUT,
    },
)
async def duplicates(request: Request, limit: Limit = 100) -> DuplicateList:
    """Du jour le plus récent au plus ancien. Sans doublon, un émetteur n'a pas de ligne.

    Comme toute lecture, elle s'exécute sous le délai maximal des lectures.
    """
    table = schema.ingest_duplicate
    statement = (
        select(table.c.emitter, table.c.day, table.c.duplicates)
        .order_by(table.c.day.desc(), table.c.emitter)
        .limit(limit)
    )
    async with reading(request) as reader:
        found = (await reader.execute(statement, {})).all()
    return DuplicateList(
        duplicates=[
            DuplicateCount(emitter=row.emitter, day=row.day, count=row.duplicates) for row in found
        ]
    )
