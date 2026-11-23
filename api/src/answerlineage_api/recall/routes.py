"""Routes des listes de rappel : lire la liste d'un incident, lister les listes, décider.

Les lectures s'exécutent sous le délai maximal des lectures du graphe (``reading``),
page par page. Décider est une action journalisée, réservée au responsable d'agent (et à
l'administrateur, qui a les rôles des personnes) par la matrice des droits : avant le
corps, l'auteur doit être identifié par le sujet de son jeton OIDC, sinon rien n'est
journalisé. Le corps est lu borné, et validé champ par champ, de listes fermées : aucun
texte libre. Aucune route n'envoie de rappel : la base refuse tout envoi sans validation.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.auth.access import caller_of
from answerlineage_api.ingest.models import FieldError
from answerlineage_api.ingest.routes import REFUSED_400, REFUSED_413, REFUSED_415, read_json
from answerlineage_api.lineage.paging import CursorError
from answerlineage_api.lineage.reading import TIMED_OUT, reading
from answerlineage_api.recall import store
from answerlineage_api.recall.models import (
    MAX_DECISION_BYTES,
    RecallDecisionChange,
    RecallList,
    RecallLists,
    RecallState,
    validate_decision,
)
from answerlineage_api.responses import RequestRefused, RequestRejected, refused

router = APIRouter(prefix="/api/v1", tags=["rappel"])

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_INVALID: dict[int | str, dict[str, Any]] = {
    422: {"model": RequestRejected, "description": "Un paramètre est invalide."}
}
_MISSING = (
    "Aucun incident ne porte cet identifiant (« unknown_incident »), ou l'incident n'a pas de"
    " liste de rappel (« no_recall_list ») : son analyse attend, a échoué, ou est tronquée."
)

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
ListLimit = Annotated[int, Query(ge=1, le=100, description="Nombre maximal de listes renvoyées.")]
ListCursor = Annotated[
    str | None,
    Query(
        alias="cursor",
        pattern=store.LISTS_CURSOR_PATTERN,
        description=(
            "Curseur opaque de la page suivante, tel que l'API le donne dans « next_cursor »."
        ),
    ),
]
StateParam = Annotated[
    RecallState | None, Query(description="Seulement les listes dans cet état ; toutes par défaut.")
]
EscalatedParam = Annotated[
    bool | None,
    Query(
        description=(
            "« true » : seulement les listes remontées au responsable de données faute de"
            " décision ; « false » : seulement les autres. Toutes par défaut."
        )
    ),
]


@router.get(
    "/incidents/{incident_id}/recall-list",
    response_model=RecallList,
    summary=(
        "Donne la liste de rappel d'un incident : un message par destinataire, les plus"
        " exposés d'abord, et la décision prise sur elle"
    ),
    responses={404: {"model": RequestRefused, "description": _MISSING}, **_INVALID, **TIMED_OUT},
)
async def recall_list(
    request: Request, incident_id: IncidentId, limit: EntryLimit = 100, cursor: EntryCursor = None
) -> RecallList | JSONResponse:
    """Une entrée par destinataire pseudonymisé, avec ses réponses touchées, rangée par
    exposition ; les décomptes de toute la liste ; son état, sa décision, sa remontée, et
    les messages mis à l'envoi."""
    after = 0 if cursor is None else int(cursor)
    async with reading(request) as reader:
        found = await store.recall_list(reader, UUID(incident_id), limit=limit, after=after)
    return found if isinstance(found, RecallList) else refused(404, found.code)


@router.get(
    "/recall-lists",
    response_model=RecallLists,
    summary=(
        "Liste les listes de rappel, de l'incident le plus grave au moins grave, la plus"
        " ancienne d'abord"
    ),
    responses={**_INVALID, **TIMED_OUT},
)
async def recall_lists(
    request: Request,
    state: StateParam = None,
    escalated: EscalatedParam = None,
    limit: ListLimit = 20,
    cursor_value: ListCursor = None,
) -> RecallLists | JSONResponse:
    """Chaque liste avec son état, sa décision, sa remontée et ses décomptes, sans ses
    entrées. Les listes en attente remontées au responsable de données se lisent avec
    « state=pending&escalated=true »."""
    cursor = None
    if cursor_value is not None:
        try:
            cursor = store.ListsCursor.decode(cursor_value)
        except CursorError:
            rejected = RequestRejected(
                errors=[
                    FieldError(
                        path="/query/cursor",
                        type="cursor_invalid",
                        message="Ce curseur ne désigne aucune page.",
                    )
                ]
            )
            return JSONResponse(status_code=422, content=rejected.model_dump())
    async with reading(request) as reader:
        return await store.recall_lists(
            reader, state=state, escalated=escalated, limit=limit, cursor=cursor
        )


def _author(request: Request) -> store.Author | None:
    """L'auteur identifié de la requête : l'émetteur OIDC et le sujet de son jeton."""
    caller = caller_of(request)
    if caller is None or caller.issuer is None or caller.subject is None:
        return None
    return store.Author(caller.issuer, caller.subject)


@router.post(
    "/incidents/{incident_id}/recall-list/decision",
    response_model=RecallDecisionChange,
    summary=(
        "Valide, modifie ou refuse la liste de rappel d'un incident, et journalise la décision"
    ),
    responses={
        400: {"model": RequestRefused, "description": REFUSED_400},
        403: {
            "model": RequestRefused,
            "description": (
                "Le jeton ne porte pas de sujet admis (« author_unidentified ») : une décision"
                " se journalise sous le nom de son auteur."
            ),
        },
        404: {"model": RequestRefused, "description": _MISSING},
        409: {
            "model": RequestRefused,
            "description": (
                "La liste porte déjà une autre décision (« recall_list_decided ») : une"
                " décision est définitive."
            ),
        },
        413: {"model": RequestRefused, "description": REFUSED_413},
        415: {"model": RequestRefused, "description": REFUSED_415},
        422: {"model": RequestRejected, "description": "La décision est invalide."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/RecallDecisionRequest"}
                }
            },
        }
    },
)
async def decide(request: Request, incident_id: IncidentId) -> JSONResponse:
    """La décision, son auteur (l'émetteur et le sujet de son jeton) et l'heure de la base
    sont journalisés, en ajout seul ; une liste validée ou modifiée peut partir, une liste
    refusée ne partira pas. Idempotent : la même décision redemandée rend la liste telle
    quelle (``changed`` faux), sans rien journaliser de plus."""
    author = _author(request)
    if author is None:
        return refused(403, "author_unidentified")
    body, refusal = await read_json(request, max_bytes=MAX_DECISION_BYTES)
    if refusal is not None:
        return refusal
    asked, errors = validate_decision(body)
    if asked is None:
        return JSONResponse(status_code=422, content=RequestRejected(errors=errors).model_dump())
    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            change = await store.decide(conn, UUID(incident_id), author, asked)
    except store.RecallListDecidedError:
        return refused(409, "recall_list_decided")
    if isinstance(change, store.Missing):
        return refused(404, change.code)
    return JSONResponse(status_code=200, content=change.model_dump(mode="json"))
