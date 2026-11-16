"""Routes d'ingestion : un événement, ou un lot d'événements.

Le corps est lu borné (``body.py``) : taille, compression et profondeur sont
contrôlées avant l'analyse JSON.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.ingest.body import (
    MAX_DEPTH,
    BodyRefusedError,
    nesting_depth,
    read_body,
)
from answerlineage_api.ingest.models import FieldError, validate_event
from answerlineage_api.ingest.store import (
    AcceptedEvent,
    UnstorableEventError,
    event_identifier,
    store_events,
)
from answerlineage_api.responses import RequestRefused, refused

router = APIRouter(prefix="/api/v1", tags=["ingestion"])

_RUN_EVENT_REF = {"$ref": "#/components/schemas/RunEvent"}


class Rejection(BaseModel):
    """Événement rejeté d'un lot."""

    index: int = Field(description="Rang de l'événement dans le lot, à partir de zéro.")
    errors: list[FieldError]


class BatchResult(BaseModel):
    """Bilan de l'ingestion d'un lot."""

    accepted: int = Field(description="Événements valides, enregistrés ou déjà connus.")
    duplicates: int = Field(description="Événements valides déjà connus, sans effet.")
    rejected: list[Rejection]


class EventResult(BaseModel):
    """Bilan de l'ingestion d'un événement."""

    event_id: str = Field(description="Identifiant déterministe de l'événement.")
    duplicate: bool


class EventRejected(BaseModel):
    errors: list[FieldError]


def _not_a_number(_: str) -> Any:
    raise ValueError("NaN et Infinity ne sont pas du JSON")


async def read_json(
    request: Request, *, max_bytes: int | None = None
) -> tuple[Any, JSONResponse | None]:
    """Le corps JSON d'une requête, lu borné ; ou la réponse qui le refuse.

    La taille maximale est celle de l'ingestion, ou ``max_bytes`` s'il est plus petit.
    """
    limit: int = request.app.state.max_body_bytes
    if max_bytes is not None:
        limit = min(limit, max_bytes)
    try:
        body = await read_body(request.stream(), request.headers, limit)
    except BodyRefusedError as refusal:
        return None, refused(refusal.status, refusal.code)
    try:
        # JSON échangé entre systèmes : UTF-8 seulement (RFC 8259), sans marque d'ordre.
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None, refused(400, "invalid_json")
    # L'analyseur est récursif : il ne s'engage que dans un document peu profond.
    if nesting_depth(body) > MAX_DEPTH:
        return None, refused(400, "json_too_deep")
    try:
        return json.loads(text, parse_constant=_not_a_number), None
    except ValueError:
        return None, refused(400, "invalid_json")


REFUSED_400 = (
    "Le corps n'est pas du JSON en UTF-8 (`invalid_json`), dépasse la profondeur maximale "
    "(`json_too_deep`) ou n'est pas un flux gzip complet (`invalid_gzip`)."
)
REFUSED_413 = "Le corps dépasse la taille maximale, en octets reçus ou déployés (`body_too_large`)."
REFUSED_415 = (
    "Le corps porte un autre encodage que gzip, ou plusieurs (`unsupported_content_encoding`)."
)


def _accept(raw: object, received_at: datetime) -> tuple[AcceptedEvent | None, list[FieldError]]:
    event, errors = validate_event(raw, received_at=received_at)
    if not isinstance(raw, dict):
        return None, errors
    try:
        event_id = event_identifier(raw)
    except UnstorableEventError as error:
        # Un texte que la base ne peut pas conserver passe avant tout autre motif.
        return None, [FieldError(path="", type="unstorable_text", message=str(error)), *errors]
    if event is None:
        return None, errors
    return AcceptedEvent(event_id, event, raw), []


@router.post(
    "/lineage/batch",
    status_code=202,
    response_model=BatchResult,
    summary="Envoie un lot de RunEvents OpenLineage",
    responses={
        400: {
            "model": RequestRefused,
            "description": REFUSED_400 + " Ou bien ce n'est pas un tableau "
            "(`batch_must_be_an_array`).",
        },
        413: {
            "model": RequestRefused,
            "description": REFUSED_413 + " Ou bien le lot compte plus d'événements que "
            "le maximum (`batch_too_large`).",
        },
        415: {"model": RequestRefused, "description": REFUSED_415},
        422: {"model": BatchResult, "description": "Aucun événement du lot n'est valide."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "array", "items": _RUN_EVENT_REF}}},
        }
    },
)
async def ingest_batch(request: Request) -> JSONResponse:
    """Chaque événement est validé séparément : un événement invalide est rejeté
    avec le chemin de ses champs fautifs, le reste du lot est accepté.

    Le corps peut être compressé (``Content-Encoding: gzip``)."""
    body, refusal = await read_json(request)
    if refusal is not None:
        return refusal
    if not isinstance(body, list):
        return refused(400, "batch_must_be_an_array")
    if len(body) > request.app.state.max_batch_events:
        return refused(413, "batch_too_large")

    received_at = datetime.now(UTC)
    accepted: list[AcceptedEvent] = []
    rejected: list[Rejection] = []
    for index, raw in enumerate(body):
        event, errors = _accept(raw, received_at)
        if event is None:
            rejected.append(Rejection(index=index, errors=errors))
        else:
            accepted.append(event)

    # Le compteur de doublons se tient dans cette transaction : il dit ce que dit la réponse.
    engine: AsyncEngine = request.app.state.engine
    async with engine.begin() as conn:
        stored = await store_events(conn, accepted)

    valid = len(accepted)
    result = BatchResult(accepted=valid, duplicates=valid - stored, rejected=rejected)
    status = 422 if body and valid == 0 else 202
    return JSONResponse(status_code=status, content=result.model_dump())


@router.post(
    "/lineage",
    status_code=201,
    response_model=EventResult,
    summary="Envoie un RunEvent OpenLineage",
    responses={
        400: {"model": RequestRefused, "description": REFUSED_400},
        413: {"model": RequestRefused, "description": REFUSED_413},
        415: {"model": RequestRefused, "description": REFUSED_415},
        422: {"model": EventRejected, "description": "L'événement est invalide."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _RUN_EVENT_REF}},
        }
    },
)
async def ingest_event(request: Request) -> JSONResponse:
    """Point d'entrée standard d'OpenLineage : un émetteur conforme s'y branche tel quel.

    Le corps peut être compressé (``Content-Encoding: gzip``)."""
    body, refusal = await read_json(request)
    if refusal is not None:
        return refusal
    event, errors = _accept(body, datetime.now(UTC))
    if event is None:
        return JSONResponse(status_code=422, content=EventRejected(errors=errors).model_dump())

    engine: AsyncEngine = request.app.state.engine
    async with engine.begin() as conn:
        stored = await store_events(conn, [event])
    result = EventResult(event_id=event.event_id, duplicate=stored == 0)
    return JSONResponse(status_code=201, content=result.model_dump())
