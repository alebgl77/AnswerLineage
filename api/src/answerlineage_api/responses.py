"""Réponses d'erreur communes à toutes les routes.

Une erreur ne renvoie jamais la valeur reçue : un code stable, ou le chemin du
champ fautif avec son motif.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from answerlineage_api.ingest.models import FieldError, json_pointer


class RequestRefused(BaseModel):
    error: str = Field(description="Code stable du motif de refus.")


class RequestRejected(BaseModel):
    """Requête dont un paramètre est invalide."""

    errors: list[FieldError]


def refused(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


async def reject_invalid_request(_: Request, error: Exception) -> JSONResponse:
    """Remplace la réponse de validation par défaut, qui recopie la valeur reçue."""
    if not isinstance(error, RequestValidationError):  # pragma: no cover — enregistré pour ce type.
        raise error
    rejected = RequestRejected(
        errors=[
            FieldError(path=json_pointer(item["loc"]), type=item["type"], message=item["msg"])
            for item in error.errors()
        ]
    )
    return JSONResponse(status_code=422, content=rejected.model_dump())
