"""L'accès à chaque route : le jeton lu et vérifié, puis la matrice des droits appliquée.

Une dépendance, posée sur toute l'application, s'exécute avant la validation des
paramètres et avant toute lecture du corps : un appel sans jeton reçoit ``401``,
un rôle qui n'ouvre pas la route ``403``, sans que la route ne lise rien.

- ``401 missing_token`` : aucun en-tête ``Authorization`` ;
- ``401 invalid_token`` : un en-tête répété, mal formé, d'un autre schéma que
  ``Bearer``, un jeton de plus de 16 Kio, ou que l'émetteur n'a pas signé pour
  l'API ; ``401 expired_token`` : un jeton juste, mais expiré ;
- ``403 insufficient_role`` : aucun des rôles du jeton n'ouvre la route ;
- ``503 authentication_unavailable`` : l'émetteur ne donne pas la clé qui
  vérifierait le jeton, et aucune n'est encore connue.

Aucune réponse ne recopie l'en-tête ni le jeton, et rien n'en est journalisé.

L'authentification se règle par l'environnement (``authentication_from_settings``).
Elle ne se désactive que par un réglage nommé, ``ANSWERLINEAGE_AUTH=disabled``,
pour la démonstration : chaque appel a alors tous les rôles, et l'API refuse de
démarrer si elle accepte un autre nom que la boucle locale (``Host`` ou
``Origin``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from answerlineage_api.auth.fetch import Fetch, IssuerUnavailableError, fetch_document
from answerlineage_api.auth.matrix import access_of
from answerlineage_api.auth.oidc import OidcSettings, OidcVerifier, TokenRefusedError
from answerlineage_api.auth.roles import Role
from answerlineage_api.guard import LOOPBACK_HOSTS, RequestGuard

ENV_MODE: Final = "ANSWERLINEAGE_AUTH"
ENV_ISSUER: Final = "ANSWERLINEAGE_OIDC_ISSUER"
ENV_AUDIENCE: Final = "ANSWERLINEAGE_OIDC_AUDIENCE"
ENV_ROLES_CLAIM: Final = "ANSWERLINEAGE_OIDC_ROLES_CLAIM"
ENV_ROLE_MAP: Final = "ANSWERLINEAGE_OIDC_ROLE_MAP"
ENV_EMITTER_CLIENTS: Final = "ANSWERLINEAGE_OIDC_EMITTER_CLIENTS"
ENV_EXPLORER_CLIENT_ID: Final = "ANSWERLINEAGE_OIDC_EXPLORER_CLIENT_ID"
ENV_EXPLORER_SCOPE: Final = "ANSWERLINEAGE_OIDC_EXPLORER_SCOPE"
DISABLED: Final = "disabled"

# Un jeton porteur, au plus : assez pour les revendications de groupes d'un annuaire,
# sous la borne d'un en-tête des serveurs HTTP usuels.
MAX_TOKEN_LENGTH: Final = 16 * 1024
MAX_ROLE_MAP_BYTES: Final = 64 * 1024
# Un jeton porteur (RFC 6750, 2.1) : token68, après « Bearer » et des espaces.
_BEARER: Final = re.compile(r"[Bb][Ee][Aa][Rr][Ee][Rr] +([A-Za-z0-9\-._~+/]+=*)")
_LOOPBACK_NAMES: Final = frozenset(name.strip("[]") for name in LOOPBACK_HOSTS)

# Le défi de chaque refus (RFC 6750, 3).
_CHALLENGES: Final = {
    "missing_token": "Bearer",
    "invalid_token": 'Bearer error="invalid_token"',
    "expired_token": 'Bearer error="invalid_token"',
    "insufficient_role": 'Bearer error="insufficient_scope"',
}


class AccessRefusedError(Exception):
    """L'appel est refusé avant la route : un statut et un code stable."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


async def refuse_access(_: Request, error: Exception) -> JSONResponse:
    """La réponse d'un refus d'accès : son code, et le défi porteur qui va avec."""
    if not isinstance(error, AccessRefusedError):  # pragma: no cover — enregistré pour ce type.
        raise error
    challenge = _CHALLENGES.get(error.code)
    headers = {"www-authenticate": challenge} if challenge is not None else None
    return JSONResponse(status_code=error.status, content={"error": error.code}, headers=headers)


def bearer_token(values: Sequence[str]) -> str:
    """Le jeton de l'en-tête ``Authorization`` ; ``TokenRefusedError`` sinon."""
    if not values:
        raise TokenRefusedError("missing_token")
    if len(values) > 1 or len(values[0]) > MAX_TOKEN_LENGTH + len("Bearer "):
        raise TokenRefusedError("invalid_token")
    found = _BEARER.fullmatch(values[0])
    if found is None or len(found[1]) > MAX_TOKEN_LENGTH:
        raise TokenRefusedError("invalid_token")
    return found[1]


@dataclass(frozen=True)
class Authentication:
    """Comment l'API reconnaît ses appelants : un vérificateur OIDC, ou rien du tout."""

    verifier: OidcVerifier | None

    @classmethod
    def oidc(cls, settings: OidcSettings, *, fetch: Fetch = fetch_document) -> Authentication:
        return cls(OidcVerifier(settings, fetch=fetch))

    @classmethod
    def disabled(cls) -> Authentication:
        """La démonstration : chaque appel a tous les rôles. Pour la boucle locale seule."""
        return cls(None)

    @property
    def mode(self) -> str:
        return DISABLED if self.verifier is None else "oidc"

    async def roles(self, authorization: Sequence[str]) -> frozenset[Role]:
        """Les rôles de l'appelant, d'après ses en-têtes ``Authorization``."""
        if self.verifier is None:
            return frozenset(Role)
        try:
            return await self.verifier.roles(bearer_token(authorization))
        except TokenRefusedError as refused:
            raise AccessRefusedError(401, refused.code) from None
        except IssuerUnavailableError:
            raise AccessRefusedError(503, "authentication_unavailable") from None

    def connect_sources(self) -> tuple[str, ...]:
        """Les origines que l'explorateur joint, en plus de l'API : celle de l'émetteur."""
        if self.verifier is None or self.verifier.settings.explorer_client_id is None:
            return ()
        parts = urlsplit(self.verifier.settings.issuer)
        return (f"{parts.scheme}://{parts.netloc}",)

    def check_guard(self, guard: RequestGuard) -> None:
        """Désactivée, l'authentification n'admet que la boucle locale : sinon, l'API ne
        démarre pas."""
        if self.verifier is not None:
            return
        origins = {urlsplit(origin).hostname or "" for origin in guard.origins}
        if not guard.hosts <= {name.lower() for name in LOOPBACK_HOSTS} or not (
            origins <= _LOOPBACK_NAMES
        ):
            raise ValueError(
                f"L'authentification est désactivée ({ENV_MODE}={DISABLED}) : l'API n'accepte "
                "alors que la boucle locale. Retirer les hôtes et les origines ajoutés "
                "(ANSWERLINEAGE_ALLOWED_HOSTS, ANSWERLINEAGE_ALLOWED_ORIGINS), ou régler un "
                "émetteur OIDC."
            )


async def authorize(request: Request) -> None:
    """Avant toute route : le jeton de l'appelant, puis la matrice des droits."""
    route = request.scope.get("route")
    access = access_of(request.method, getattr(route, "path", ""))
    if access is None:  # pragma: no cover — une route hors matrice empêche l'application.
        raise AccessRefusedError(403, "insufficient_role")
    if access.public:
        return
    authentication: Authentication = request.app.state.authentication
    roles = await authentication.roles(request.headers.getlist("authorization"))
    if not roles & access.roles:
        raise AccessRefusedError(403, "insufficient_role")


def _listed(environment: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in environment.get(name, "").split(",") if item.strip())


def _role_map(environment: Mapping[str, str]) -> dict[str, list[str]] | None:
    text = environment.get(ENV_ROLE_MAP, "").strip()
    if not text:
        return None
    try:
        if len(text.encode()) > MAX_ROLE_MAP_BYTES:
            raise ValueError
        raw = json.loads(text)
    except ValueError:
        raw = None
    if not isinstance(raw, dict) or not all(
        isinstance(roles, str | list) for roles in raw.values()
    ):
        raise ValueError(
            f"{ENV_ROLE_MAP} est un objet JSON : valeur de l'émetteur, puis un rôle ou une liste"
            " de rôles."
        )
    return {value: [roles] if isinstance(roles, str) else roles for value, roles in raw.items()}


def authentication_from_settings(
    authentication: Authentication | None, environment: Mapping[str, str]
) -> Authentication:
    """L'authentification de l'appelant, sinon celle de l'environnement.

    Sans émetteur réglé, l'API ne démarre pas : elle ne s'ouvre jamais par défaut."""
    if authentication is not None:
        return authentication
    mode = environment.get(ENV_MODE, "oidc").strip()
    if mode == DISABLED:
        return Authentication.disabled()
    if mode != "oidc":
        raise ValueError(f"{ENV_MODE} vaut « oidc », par défaut, ou « {DISABLED} ».")
    issuer = environment.get(ENV_ISSUER, "").strip()
    audience = environment.get(ENV_AUDIENCE, "").strip()
    if not issuer or not audience:
        raise ValueError(
            f"Chaque route de l'API exige un jeton OIDC : régler l'émetteur ({ENV_ISSUER}) et "
            f"l'audience de l'API ({ENV_AUDIENCE}). Pour la démonstration seule, sur la boucle "
            f"locale : {ENV_MODE}={DISABLED}."
        )
    try:
        settings = OidcSettings.create(
            issuer,
            audience,
            roles_claim=environment.get(ENV_ROLES_CLAIM, "").strip() or "roles",
            role_map=_role_map(environment),
            emitter_clients=_listed(environment, ENV_EMITTER_CLIENTS),
            explorer_client_id=environment.get(ENV_EXPLORER_CLIENT_ID, "").strip() or None,
            explorer_scope=environment.get(ENV_EXPLORER_SCOPE, "").strip() or "openid",
        )
    except ValueError as error:
        raise ValueError(f"{error} Réglages : variables ANSWERLINEAGE_OIDC_*.") from error
    return Authentication.oidc(settings)
