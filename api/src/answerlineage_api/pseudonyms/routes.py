"""Routes des destinataires : révéler un destinataire, lire le journal des révélations.

Révéler est une action tracée, réservée au gestionnaire d'incident (matrice des
droits) : la personne apporte des identités candidates, l'API dit laquelle a le
pseudonyme visé, et journalise l'action, son auteur et son motif. Avant le corps,
l'auteur doit être identifié (le sujet de son jeton OIDC) et le coffre à clés réglé ;
sinon rien n'est calculé ni journalisé.

Le journal se lit, page par page, la révélation la plus récente d'abord, par
l'auditeur et l'administrateur.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.auth.access import caller_of
from answerlineage_api.ingest.routes import REFUSED_400, REFUSED_413, REFUSED_415, read_json
from answerlineage_api.lineage.reading import TIMED_OUT, reading
from answerlineage_api.pseudonyms import store
from answerlineage_api.pseudonyms.models import (
    MAX_REVELATION_BYTES,
    Revelation,
    RevelationJournal,
    RevelationRequest,
    validate_revelation,
)
from answerlineage_api.pseudonyms.vault import PseudonymVault, canonical_identity
from answerlineage_api.responses import RequestRefused, RequestRejected, refused

router = APIRouter(prefix="/api/v1", tags=["destinataires"])

_INVALID: dict[int | str, dict[str, Any]] = {
    422: {"model": RequestRejected, "description": "Un paramètre est invalide."}
}


def _author(request: Request) -> store.Author | None:
    """L'auteur identifié de la requête : l'émetteur OIDC et le sujet de son jeton."""
    caller = caller_of(request)
    if caller is None or caller.issuer is None or caller.subject is None:
        return None
    # L'émetteur est celui du réglage, borné comme le journal le borne (AUTHOR_ISSUER_MAX).
    return store.Author(caller.issuer, caller.subject)


async def _matched(vault: PseudonymVault, asked: RevelationRequest) -> int | None:
    """Le rang de la première candidate qui a le pseudonyme visé, sous l'une des clés."""
    for rank, candidate in enumerate(asked.candidates):
        canonical = canonical_identity(candidate)
        if canonical is not None and asked.pseudonym in await vault.pseudonyms(canonical):
            return rank
    return None


@router.post(
    "/recipients/reveal",
    response_model=Revelation,
    summary="Révèle un destinataire parmi des identités candidates, et journalise l'action",
    responses={
        400: {"model": RequestRefused, "description": REFUSED_400},
        403: {
            "model": RequestRefused,
            "description": (
                "Le jeton ne porte pas de sujet admis (« author_unidentified ») : une"
                " révélation se journalise sous le nom de son auteur."
            ),
        },
        404: {
            "model": RequestRefused,
            "description": (
                "Aucun destinataire connu ne porte ce pseudonyme (« unknown_recipient ») ; la"
                " tentative est journalisée."
            ),
        },
        413: {"model": RequestRefused, "description": REFUSED_413},
        415: {"model": RequestRefused, "description": REFUSED_415},
        422: {"model": RequestRejected, "description": "La demande est invalide."},
        429: {
            "model": RequestRefused,
            "description": (
                f"L'auteur a fait {store.REVELATIONS_PER_HOUR} révélations dans l'heure"
                " (« revelation_rate_limited ») ; « Retry-After » dit dans combien de secondes"
                " une place se libère. Rien n'est journalisé."
            ),
        },
        503: {
            "model": RequestRefused,
            "description": (
                "Le coffre à clés n'est pas réglé (« pseudonym_keys_unavailable ») : rien n'est"
                " calculé ni journalisé."
            ),
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/RevelationRequest"}}
            },
        }
    },
)
async def reveal(request: Request) -> JSONResponse:
    """Dit laquelle des identités candidates a le pseudonyme visé, en recalculant le HMAC avec
    les clés du coffre, et journalise l'action : auteur, heure, motif, pseudonyme, nombre de
    candidates, résultat. Aucune identité n'est renvoyée, gardée ni journalisée : la réponse
    donne le rang de la candidate."""
    author = _author(request)
    if author is None:
        return refused(403, "author_unidentified")
    vault: PseudonymVault | None = request.app.state.pseudonym_vault
    if vault is None:
        return refused(503, "pseudonym_keys_unavailable")
    body, refusal = await read_json(request, max_bytes=MAX_REVELATION_BYTES)
    if refusal is not None:
        return refusal
    asked, errors = validate_revelation(body)
    if asked is None:
        return JSONResponse(status_code=422, content=RequestRejected(errors=errors).model_dump())
    matched = await _matched(vault, asked)
    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            outcome = await store.reveal(conn, author, asked, matched)
    except store.RevelationRateLimitedError as limited:
        return JSONResponse(
            status_code=429,
            content={"error": "revelation_rate_limited"},
            headers={"retry-after": str(limited.retry_after)},
        )
    if outcome.result == "unknown_recipient":
        return refused(404, "unknown_recipient")
    revealed = Revelation(
        revelation=outcome.revelation,
        result=outcome.result,
        candidate=matched if outcome.result == "matched" else None,
    )
    return JSONResponse(status_code=200, content=revealed.model_dump())


JournalLimit = Annotated[
    int, Query(ge=1, le=100, description="Nombre maximal de révélations renvoyées.")
]
JournalCursor = Annotated[
    str | None,
    Query(
        alias="cursor",
        pattern=store.JOURNAL_CURSOR_PATTERN,
        description=(
            "Curseur opaque de la page suivante, tel que l'API le donne dans « next_cursor »."
        ),
    ),
]


@router.get(
    "/recipients/revelations",
    response_model=RevelationJournal,
    summary="Lit le journal des révélations de destinataires, la plus récente d'abord",
    responses={**_INVALID, **TIMED_OUT},
)
async def revelations(
    request: Request, limit: JournalLimit = 20, cursor: JournalCursor = None
) -> RevelationJournal:
    """Chaque révélation : son heure, son auteur, son motif, le pseudonyme visé, le nombre de
    candidates et le résultat. Jamais une identité : le journal n'en porte aucune."""
    async with reading(request) as reader:
        return await store.journal(
            reader, limit=limit, before=None if cursor is None else int(cursor)
        )
