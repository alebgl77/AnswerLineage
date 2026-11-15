"""Une lecture du graphe : bornée en durée par PostgreSQL, et en taille.

Chaque route de lecture a un délai maximal. C'est PostgreSQL qui l'impose :
avant chaque requête, ``statement_timeout`` reçoit, pour la transaction
seulement, le temps qui reste au délai de la route. Une requête qui le dépasse
est annulée dans la base, pas abandonnée par l'application pendant qu'elle y
tourne encore ; la route répond alors ``503`` et ``query_timeout``.

Chaque requête porte aussi une borne sur les lignes qu'elle rend : une ligne de
plus que la borne est demandée, pour savoir si elle est dépassée et le dire.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg.errors import QueryCanceled
from sqlalchemy import CursorResult, Executable, Row, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.responses import RequestRefused, refused

ENV_READ_TIMEOUT = "ANSWERLINEAGE_READ_TIMEOUT_MS"
# Une lecture bornée rend son résultat en quelques dizaines de millisecondes ;
# cinq secondes laissent la marge d'un cache froid, rendent vite la connexion
# au pool, et restent sous le délai des relais HTTP usuels (30 à 60 s), pour
# que l'appelant reçoive le motif plutôt qu'une coupure.
DEFAULT_READ_TIMEOUT_MS = 5_000
# Au-delà de dix minutes, le délai ne protège plus le service.
MAX_READ_TIMEOUT_MS = 600_000

# Le refus d'une lecture interrompue, tel que le contrat de chaque route de lecture le décrit.
TIMED_OUT: dict[int | str, dict[str, Any]] = {
    503: {
        "model": RequestRefused,
        "description": (
            "La lecture a dépassé son délai maximal : PostgreSQL l'a interrompue"
            " (« query_timeout »)."
        ),
    }
}

_STATEMENT_TIMEOUT = text("SELECT set_config('statement_timeout', :timeout, true)")


class ReadTimedOut(Exception):  # noqa: N818 — le nom dit le motif, comme les erreurs de la base.
    """La lecture a dépassé son délai maximal : PostgreSQL l'a annulée."""


def read_timeout_from_settings(value: int | None, environment: Mapping[str, str]) -> int:
    """Délai maximal d'une lecture, en millisecondes : de l'appelant, sinon de l'environnement."""
    if value is None:
        configured = environment.get(ENV_READ_TIMEOUT, "").strip()
        if not configured:
            return DEFAULT_READ_TIMEOUT_MS
        if not configured.isdigit():
            raise ValueError(f"{ENV_READ_TIMEOUT} doit être un nombre entier de millisecondes.")
        value = int(configured)
    if not 1 <= value <= MAX_READ_TIMEOUT_MS:
        raise ValueError(
            f"Le délai maximal d'une lecture va de 1 à {MAX_READ_TIMEOUT_MS} millisecondes"
            f" ({ENV_READ_TIMEOUT})."
        )
    return value


@dataclass(frozen=True)
class Bounded:
    """Les lignes d'une requête, au plus sa borne, et si la borne a été dépassée."""

    rows: Sequence[Row[Any]]
    truncated: bool


class Reader:
    """Une connexion de lecture dont chaque requête part sous le temps qui reste au délai."""

    def __init__(self, conn: AsyncConnection, *, deadline: float) -> None:
        self._conn = conn
        self._deadline = deadline

    async def execute(
        self, statement: Executable, parameters: Mapping[str, Any]
    ) -> CursorResult[Any]:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ReadTimedOut
        timeout = str(math.ceil(remaining * 1000))
        try:
            await self._conn.execute(_STATEMENT_TIMEOUT, {"timeout": timeout})
            return await self._conn.execute(statement, dict(parameters))
        except DBAPIError as error:
            if isinstance(error.orig, QueryCanceled):
                raise ReadTimedOut from None
            raise

    async def fetch(
        self, statement: Executable, parameters: Mapping[str, Any], *, limit: int
    ) -> Bounded:
        """Au plus ``limit`` lignes ; la requête reçoit ``:limit``, une de plus que la borne."""
        rows = (await self.execute(statement, {**parameters, "limit": limit + 1})).all()
        return Bounded(rows=rows[:limit], truncated=len(rows) > limit)


@asynccontextmanager
async def reading(request: Request) -> AsyncIterator[Reader]:
    """Une lecture sous le délai de l'application, qui court dès l'arrivée de la requête.

    À la sortie, la transaction est annulée : le délai posé pour elle disparaît
    avec elle, et la connexion revient au pool telle qu'elle en est sortie.
    """
    deadline = time.monotonic() + request.app.state.read_timeout_ms / 1000
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        yield Reader(conn, deadline=deadline)


async def refuse_timed_out(_: Request, error: Exception) -> JSONResponse:
    """Le motif du refus, sans rien de la requête ni des valeurs reçues."""
    if not isinstance(error, ReadTimedOut):  # pragma: no cover — enregistré pour ce type.
        raise error
    return refused(503, "query_timeout")
