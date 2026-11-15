"""Routes d'ingestion : un événement, ou un lot d'événements."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.ingest.models import FieldError, validate_event
from answerlineage_api.ingest.store import (
    AcceptedEvent,
    UnstorableEventError,
    event_identifier,
    store_events,
)

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


class RequestRefused(BaseModel):
    error: str = Field(description="Code stable du motif de refus.")


def _refused(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


def _not_a_number(_: str) -> Any:
    raise ValueError("NaN et Infinity ne sont pas du JSON")


async def _read_json(request: Request) -> tuple[Any, JSONResponse | None]:
    try:
        return json.loads(await request.body(), parse_constant=_not_a_number), None
    except (ValueError, RecursionError):
        return None, _refused(400, "invalid_json")


def _accept(raw: object) -> tuple[AcceptedEvent | None, list[FieldError]]:
    event, errors = validate_event(raw)
    if event is None or not isinstance(raw, dict):
        return None, errors
    try:
        return AcceptedEvent(event_identifier(raw), event, raw), []
    except UnstorableEventError as error:
        return None, [FieldError(path="", type="unstorable_text", message=str(error))]


@router.post(
    "/lineage/batch",
    status_code=202,
    response_model=BatchResult,
    summary="Envoie un lot de RunEvents OpenLineage",
    responses={
        400: {"model": RequestRefused, "description": "Le corps n'est pas un tableau JSON."},
        413: {"model": RequestRefused, "description": "Le lot dépasse la taille maximale."},
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
    avec le chemin de ses champs fautifs, le reste du lot est accepté."""
    body, refusal = await _read_json(request)
    if refusal is not None:
        return refusal
    if not isinstance(body, list):
        return _refused(400, "batch_must_be_an_array")
    if len(body) > request.app.state.max_batch_events:
        return _refused(413, "batch_too_large")

    accepted: dict[str, AcceptedEvent] = {}
    valid = 0
    rejected: list[Rejection] = []
    for index, raw in enumerate(body):
        event, errors = _accept(raw)
        if event is None:
            rejected.append(Rejection(index=index, errors=errors))
        else:
            valid += 1
            accepted.setdefault(event.event_id, event)

    engine: AsyncEngine = request.app.state.engine
    async with engine.begin() as conn:
        stored = await store_events(conn, list(accepted.values()))

    result = BatchResult(accepted=valid, duplicates=valid - stored, rejected=rejected)
    status = 422 if body and valid == 0 else 202
    return JSONResponse(status_code=status, content=result.model_dump())


@router.post(
    "/lineage",
    status_code=201,
    response_model=EventResult,
    summary="Envoie un RunEvent OpenLineage",
    responses={
        400: {"model": RequestRefused, "description": "Le corps n'est pas du JSON."},
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
    """Point d'entrée standard d'OpenLineage : un émetteur conforme s'y branche tel quel."""
    body, refusal = await _read_json(request)
    if refusal is not None:
        return refusal
    event, errors = _accept(body)
    if event is None:
        return JSONResponse(status_code=422, content=EventRejected(errors=errors).model_dump())

    engine: AsyncEngine = request.app.state.engine
    async with engine.begin() as conn:
        stored = await store_events(conn, [event])
    result = EventResult(event_id=event.event_id, duplicate=stored == 0)
    return JSONResponse(status_code=201, content=result.model_dump())
