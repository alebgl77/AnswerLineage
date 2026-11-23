"""Route des listes de rappel : lire la liste de rappel d'un incident.

La liste se lit sous le délai maximal des lectures du graphe (``reading``), page par
page, les destinataires les plus exposés d'abord. Comme toute route de l'API, elle
exige un jeton OIDC et un rôle que la matrice des droits lui accorde
(``auth/matrix.py``).
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse

from answerlineage_api.lineage.reading import TIMED_OUT, reading
from answerlineage_api.recall import store
from answerlineage_api.recall.models import RecallList
from answerlineage_api.responses import RequestRefused, RequestRejected, refused

router = APIRouter(prefix="/api/v1", tags=["rappel"])

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_INVALID: dict[int | str, dict[str, Any]] = {
    422: {"model": RequestRejected, "description": "Un paramètre est invalide."}
}

IncidentId = Annotated[
    str, Path(pattern=_UUID, description="Identifiant de l'incident, tel que l'API le donne.")
]
EntryLimit = Annotated[int, Query(ge=1, le=200, description="Nombre maximal d'entrées renvoyées.")]
EntryCursor = Annotated[
    str | None,
    Query(
        alias="cursor",
        pattern=store.CURSOR_PATTERN,
        description=(
            "Curseur opaque de la page suivante, tel que l'API le donne dans « next_cursor »."
        ),
    ),
]


@router.get(
    "/incidents/{incident_id}/recall-list",
    response_model=RecallList,
    summary=(
        "Donne la liste de rappel d'un incident : un message par destinataire, les plus"
        " exposés d'abord"
    ),
    responses={
        404: {
            "model": RequestRefused,
            "description": (
                "Aucun incident ne porte cet identifiant (« unknown_incident »), ou l'incident"
                " n'a pas de liste de rappel (« no_recall_list ») : son analyse attend, a"
                " échoué, ou est tronquée."
            ),
        },
        **_INVALID,
        **TIMED_OUT,
    },
)
async def recall_list(
    request: Request, incident_id: IncidentId, limit: EntryLimit = 100, cursor: EntryCursor = None
) -> RecallList | JSONResponse:
    """Une entrée par destinataire pseudonymisé, avec ses réponses touchées, rangée par
    exposition ; les décomptes de toute la liste."""
    after = 0 if cursor is None else int(cursor)
    async with reading(request) as reader:
        found = await store.recall_list(reader, UUID(incident_id), limit=limit, after=after)
    return found if isinstance(found, RecallList) else refused(404, found.code)
