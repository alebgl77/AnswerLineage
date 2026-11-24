"""Ce que l'explorateur lit sans jeton pour se connecter : le mode et l'émetteur.

L'explorateur se connecte par le flux d'autorisation OIDC avec PKCE : il lui
faut l'émetteur, son client public et la portée à demander. Rien de cela n'est
secret, ni ne dit quoi que ce soit du lignage. En démonstration, la route dit
que l'authentification est désactivée, et l'explorateur l'affiche.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from answerlineage_api.auth.access import Authentication

router = APIRouter(prefix="/api/v1", tags=["installation"])


class AuthenticationSettings(BaseModel):
    """Le mode d'authentification de l'API, et ce qu'un client public doit savoir."""

    mode: Literal["oidc", "disabled"] = Field(
        description=(
            "« oidc » : chaque route exige un jeton de l'émetteur. « disabled » :"
            " démonstration sur la boucle locale, sans jeton."
        )
    )
    issuer: str | None = Field(
        description="L'émetteur OIDC ; nul quand l'authentification est désactivée."
    )
    client_id: str | None = Field(
        description=(
            "Le client public de l'explorateur chez l'émetteur ; nul quand l'explorateur ne"
            " se connecte pas."
        )
    )
    scope: str | None = Field(
        description="La portée que l'explorateur demande ; nulle sans client."
    )


@router.get(
    "/authentication",
    response_model=AuthenticationSettings,
    summary="Dit comment se connecter : le mode d'authentification et l'émetteur",
)
async def authentication_settings(request: Request) -> AuthenticationSettings:
    """Ouverte sans jeton : l'explorateur la lit avant de se connecter."""
    authentication: Authentication = request.app.state.authentication
    if authentication.verifier is None:
        return AuthenticationSettings(mode="disabled", issuer=None, client_id=None, scope=None)
    settings = authentication.verifier.settings
    client = settings.explorer_client_id
    return AuthenticationSettings(
        mode="oidc",
        issuer=settings.issuer,
        client_id=client,
        scope=None if client is None else settings.explorer_scope,
    )
