"""Routes des incidents : déclarer, lire un incident, lister les incidents, mettre un
incident en quarantaine et la lever, diffuser les périmètres en quarantaine.

La déclaration lit son corps borné, comme l'ingestion, et le valide champ par
champ ; l'incident et sa demande d'analyse s'enregistrent dans une seule
transaction. Les lectures s'exécutent sous le délai maximal des lectures du
graphe (``reading``).

La quarantaine s'active et se lève par deux actions explicites, idempotentes
(``quarantine.py``). La levée est une action, ``POST …/quarantine/lift``, et non
un ``DELETE`` : la quarantaine levée reste, pour l'histoire de l'incident, et
une levée pourra porter un corps (son motif, d'une liste fermée), ce que
``DELETE`` ne promet pas. Les proxys lisent les périmètres en quarantaine
active par ``GET /api/v1/quarantine``, sous un ETag qui leur évite de
retélécharger une liste qui n'a pas changé.

Ces routes n'authentifient pas encore leurs appelants : comme le reste de
l'API, elles sont faites pour la boucle locale ou un accès déjà contrôlé.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.incidents import quarantine, store
from answerlineage_api.incidents.models import (
    MAX_DECLARATION_BYTES,
    MAX_QUARANTINE_BYTES,
    ActiveQuarantines,
    IncidentDetail,
    IncidentList,
    QuarantineChange,
    validate_declaration,
    validate_quarantine,
)
from answerlineage_api.incidents.models import Incident as IncidentModel
from answerlineage_api.incidents.store import INCIDENT_CURSOR_PATTERN, IncidentCursor
from answerlineage_api.ingest.models import FieldError
from answerlineage_api.ingest.routes import REFUSED_400, REFUSED_413, REFUSED_415, read_json
from answerlineage_api.lineage.paging import CURSOR_PATTERN, Cursor, CursorError
from answerlineage_api.lineage.reading import TIMED_OUT, reading
from answerlineage_api.responses import RequestRefused, RequestRejected, refused

router = APIRouter(prefix="/api/v1", tags=["incidents"])

_INVALID: dict[int | str, dict[str, Any]] = {
    422: {"model": RequestRejected, "description": "Un paramètre est invalide."}
}
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _rejected(path: str, kind: str, message: str) -> JSONResponse:
    """Un paramètre bien formé mais inutilisable : son chemin et le motif, jamais sa valeur."""
    rejected = RequestRejected(errors=[FieldError(path=path, type=kind, message=message)])
    return JSONResponse(status_code=422, content=rejected.model_dump())


def _cursor_refused() -> JSONResponse:
    return _rejected("/query/cursor", "cursor_invalid", "Ce curseur ne désigne aucune page.")


@router.post(
    "/incidents",
    status_code=201,
    response_model=IncidentModel,
    summary="Déclare un incident de données et lance son analyse d'impact",
    responses={
        400: {"model": RequestRefused, "description": REFUSED_400},
        413: {"model": RequestRefused, "description": REFUSED_413},
        415: {"model": RequestRefused, "description": REFUSED_415},
        422: {"model": RequestRejected, "description": "La déclaration est invalide."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/IncidentDeclaration"}}
            },
        }
    },
)
async def declare(request: Request) -> JSONResponse:
    """L'incident est créé à l'état « qualified », avec une demande d'analyse que les workers
    réclament. Le corps est lu borné, compressé ou non (``gzip``)."""
    body, refusal = await read_json(request, max_bytes=MAX_DECLARATION_BYTES)
    if refusal is not None:
        return refusal
    declaration, errors = validate_declaration(body)
    if declaration is None:
        return JSONResponse(status_code=422, content=RequestRejected(errors=errors).model_dump())
    engine: AsyncEngine = request.app.state.engine
    async with engine.begin() as conn:
        incident = await store.declare(conn, declaration)
    return JSONResponse(status_code=201, content=incident.model_dump(mode="json", by_alias=True))


IncidentId = Annotated[
    str, Path(pattern=_UUID, description="Identifiant de l'incident, tel que l'API le donne.")
]
AnswerLimit = Annotated[
    int, Query(ge=1, le=200, description="Nombre maximal de réponses touchées renvoyées.")
]
AnswerCursor = Annotated[
    str | None,
    Query(
        alias="cursor",
        pattern=CURSOR_PATTERN,
        description=(
            "Curseur opaque de la page suivante des réponses touchées, tel que l'API le donne"
            " dans « result.next_cursor »."
        ),
    ),
]


@router.get(
    "/incidents/{incident_id}",
    response_model=IncidentDetail,
    summary="Donne un incident, l'état de son analyse et les réponses touchées",
    responses={
        404: {"model": RequestRefused, "description": "Aucun incident ne porte cet identifiant."},
        **_INVALID,
        **TIMED_OUT,
    },
)
async def incident(
    request: Request,
    incident_id: IncidentId,
    limit: AnswerLimit = 200,
    cursor_value: AnswerCursor = None,
) -> IncidentDetail | JSONResponse:
    """Le périmètre, la fenêtre, la gravité, l'état de l'analyse et, quand elle est faite, une
    page des réponses touchées avec leurs destinataires, la couverture et ses trous."""
    cursor = None
    if cursor_value is not None:
        try:
            cursor = Cursor.decode(cursor_value)
        except CursorError:
            return _cursor_refused()
    async with reading(request) as reader:
        found = await store.incident(reader, UUID(incident_id), limit=limit, cursor=cursor)
    return found if found is not None else refused(404, "unknown_incident")


IncidentLimit = Annotated[
    int, Query(ge=1, le=100, description="Nombre maximal d'incidents renvoyés.")
]
IncidentCursorParam = Annotated[
    str | None,
    Query(
        alias="cursor",
        pattern=INCIDENT_CURSOR_PATTERN,
        description=(
            "Curseur opaque de la page suivante, tel que l'API le donne dans « next_cursor »."
        ),
    ),
]


@router.get(
    "/incidents",
    response_model=IncidentList,
    summary="Liste les incidents déclarés, les plus récents d'abord",
    responses={**_INVALID, **TIMED_OUT},
)
async def incidents(
    request: Request, limit: IncidentLimit = 20, cursor_value: IncidentCursorParam = None
) -> IncidentList | JSONResponse:
    """Chaque incident avec son périmètre, sa fenêtre, sa gravité et l'état de son analyse."""
    cursor = None
    if cursor_value is not None:
        try:
            cursor = IncidentCursor.decode(cursor_value)
        except CursorError:
            return _cursor_refused()
    async with reading(request) as reader:
        return await store.incidents(reader, limit=limit, cursor=cursor)


_UNKNOWN_INCIDENT: dict[int | str, dict[str, Any]] = {
    404: {"model": RequestRefused, "description": "Aucun incident ne porte cet identifiant."}
}


def _changed(change: QuarantineChange) -> JSONResponse:
    return JSONResponse(status_code=200, content=change.model_dump(mode="json"))


@router.post(
    "/incidents/{incident_id}/quarantine",
    response_model=QuarantineChange,
    summary="Met un incident en quarantaine : les proxys reçoivent son périmètre",
    responses={
        400: {"model": RequestRefused, "description": REFUSED_400},
        409: {
            "model": RequestRefused,
            "description": (
                "La borne des quarantaines actives est atteinte (« too_many_quarantines ») :"
                " en lever une d'abord."
            ),
        },
        413: {"model": RequestRefused, "description": REFUSED_413},
        415: {"model": RequestRefused, "description": REFUSED_415},
        422: {"model": RequestRejected, "description": "La demande est invalide."},
        **_UNKNOWN_INCIDENT,
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/QuarantineRequest"}}
            },
        }
    },
)
async def activate_quarantine(request: Request, incident_id: IncidentId) -> JSONResponse:
    """Le périmètre de la quarantaine est celui de l'incident. Idempotent : un incident déjà
    en quarantaine la garde telle quelle (``changed`` faux), sans nouvelle annonce. Les
    heures sont posées par la base ; l'activation s'annonce par webhook dans sa transaction."""
    body, refusal = await read_json(request, max_bytes=MAX_QUARANTINE_BYTES)
    if refusal is not None:
        return refusal
    asked, errors = validate_quarantine(body)
    if asked is None:
        return JSONResponse(status_code=422, content=RequestRejected(errors=errors).model_dump())
    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            change = await quarantine.activate(conn, UUID(incident_id), asked.mode)
    except quarantine.QuarantineLimitError:
        return refused(409, "too_many_quarantines")
    return _changed(change) if change is not None else refused(404, "unknown_incident")


@router.post(
    "/incidents/{incident_id}/quarantine/lift",
    response_model=QuarantineChange,
    summary="Lève la quarantaine d'un incident : les proxys ne reçoivent plus son périmètre",
    responses={
        409: {
            "model": RequestRefused,
            "description": (
                "L'incident n'a jamais été mis en quarantaine (« not_quarantined ») : il n'y a"
                " rien à lever."
            ),
        },
        **_INVALID,
        **_UNKNOWN_INCIDENT,
    },
)
async def lift_quarantine(request: Request, incident_id: IncidentId) -> JSONResponse:
    """Idempotent : une quarantaine déjà levée est rendue telle quelle (``changed`` faux),
    sans nouvelle annonce. La levée est définitive ; activer de nouveau ouvre une nouvelle
    quarantaine. Elle s'annonce par webhook dans sa transaction."""
    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            change = await quarantine.lift(conn, UUID(incident_id))
    except quarantine.NotQuarantinedError:
        return refused(409, "not_quarantined")
    return _changed(change) if change is not None else refused(404, "unknown_incident")


IfNoneMatch = Annotated[
    str | None,
    Header(
        description=(
            "La version déjà reçue, entre guillemets, telle que l'ETag la donne. Une valeur de"
            f" plus de {quarantine.MAX_IF_NONE_MATCH} caractères n'est pas lue."
        )
    ),
]


@router.get(
    "/quarantine",
    response_model=ActiveQuarantines,
    summary="Donne les périmètres en quarantaine active, tels que les proxys les reçoivent",
    responses={
        304: {
            "description": (
                "La version donnée dans If-None-Match est la version courante : rien n'a changé."
            )
        },
        **TIMED_OUT,
    },
)
async def active_quarantines(request: Request, if_none_match: IfNoneMatch = None) -> Response:
    """Chaque quarantaine active, avec son mode et le périmètre de son incident, et la version
    de la liste, qui est aussi l'ETag de la réponse. Une lecture bornée : l'activation tient
    les quarantaines actives sous leur borne."""
    async with reading(request) as reader:
        diffusion = await quarantine.active_quarantines(reader)
    headers = {"etag": diffusion.etag, "cache-control": "no-cache"}
    if diffusion.matches(if_none_match):
        return Response(status_code=304, headers=headers)
    return JSONResponse(
        status_code=200, content=diffusion.quarantines.model_dump(mode="json"), headers=headers
    )
