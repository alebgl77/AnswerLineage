"""Contrôle des en-têtes Host et Origin, avant toute route.

Un service qui écoute sur la boucle locale reste atteignable par une page web
ouverte sur le même poste. Par réassociation DNS, un nom d'hôte de l'attaquant
finit par désigner 127.0.0.1 : le navigateur envoie alors ce nom dans l'en-tête
Host, et l'adresse de la page dans Origin. Une requête dont l'un des deux n'est
pas attendu est refusée.

Une autre page peut aussi viser le service par une balise ``<img>`` ou
``<script>``, sans en-tête Origin : elle ne lit pas la réponse, mais en apprend
le statut. Le navigateur dit d'où vient une requête (``Sec-Fetch-Site``) et ce
qu'elle charge (``Sec-Fetch-Mode``, ``Sec-Fetch-Dest``) : sans origine acceptée,
une requête venue d'une autre page est refusée, sauf la navigation d'un lien
suivi, qui ouvre la page sans rien apprendre à celle d'où il part.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_HOSTS = "ANSWERLINEAGE_ALLOWED_HOSTS"
ENV_ORIGINS = "ANSWERLINEAGE_ALLOWED_ORIGINS"

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
# Accepte tout hôte et toute origine : choix explicite de l'opérateur.
ANY_HOST = "*"

_HOST = r"\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9_-][A-Za-z0-9._-]{0,252}"
_HOST_NAME = re.compile(_HOST)
_AUTHORITY = re.compile(rf"({_HOST})(?::[0-9]{{1,5}})?")
_ORIGIN = re.compile(rf"https?://({_HOST})(?::[0-9]{{1,5}})?", re.IGNORECASE)


# Ce que le navigateur dit d'une requête de la page elle-même, ou de son
# utilisateur qui saisit l'adresse. Toute autre valeur désigne une autre page.
_OWN_FETCH_SITES = frozenset({"same-origin", "none"})


def _name(host: str) -> str:
    return host.lower().removesuffix(".")


def _from_another_page(fetch: dict[bytes, list[str]]) -> bool:
    """Vrai pour ce qu'une autre page charge sans que son utilisateur y navigue."""
    if all(site in _OWN_FETCH_SITES for site in fetch[b"sec-fetch-site"]):
        return False
    modes, destinations = fetch[b"sec-fetch-mode"], fetch[b"sec-fetch-dest"]
    return not (modes == ["navigate"] and destinations == ["document"])


@dataclass(frozen=True)
class RequestGuard:
    """Hôtes et origines attendus par un service."""

    # Noms d'hôte acceptés dans Host, sans port. Une origine dont l'hôte en fait
    # partie est acceptée.
    hosts: frozenset[str]
    # Origines acceptées en plus, sous la forme ``schéma://hôte[:port]``.
    origins: frozenset[str] = frozenset()

    @classmethod
    def create(cls, hosts: Iterable[str], origins: Iterable[str] = ()) -> RequestGuard:
        accepted_hosts = set()
        for host in hosts:
            if host != ANY_HOST and not _HOST_NAME.fullmatch(host):
                raise ValueError("Un hôte accepté est un nom d'hôte seul, sans port ni schéma.")
            accepted_hosts.add(_name(host))
        accepted_origins = set()
        for origin in origins:
            if not _ORIGIN.fullmatch(origin):
                raise ValueError(
                    "Une origine acceptée s'écrit schéma://hôte[:port], en http ou https."
                )
            accepted_origins.add(origin.lower())
        return cls(frozenset(accepted_hosts), frozenset(accepted_origins))

    def refusal(self, headers: Iterable[tuple[bytes, bytes]]) -> str | None:
        """Code du refus, ou ``None`` quand la requête vient d'où elle est attendue."""
        if ANY_HOST in self.hosts:
            return None
        hosts: list[str] = []
        origins: list[str] = []
        fetch: dict[bytes, list[str]] = {
            b"sec-fetch-site": [],
            b"sec-fetch-mode": [],
            b"sec-fetch-dest": [],
        }
        for name, value in headers:
            lowered = name.lower()
            if lowered == b"host":
                hosts.append(value.decode("latin-1"))
            elif lowered == b"origin":
                origins.append(value.decode("latin-1"))
            elif lowered in fetch:
                fetch[lowered].append(value.decode("latin-1").strip().lower())

        authority = _AUTHORITY.fullmatch(hosts[0]) if len(hosts) == 1 else None
        if authority is None or _name(authority[1]) not in self.hosts:
            return "host_not_allowed"
        if not origins:
            # Une balise <img> ou <script> n'envoie pas Origin ; le navigateur dit
            # tout de même d'où vient la requête.
            return "cross_site_not_allowed" if _from_another_page(fetch) else None
        origin = _ORIGIN.fullmatch(origins[0]) if len(origins) == 1 else None
        if origin is None:
            return "origin_not_allowed"
        if origins[0].lower() in self.origins or _name(origin[1]) in self.hosts:
            return None
        return "origin_not_allowed"


class HostOriginMiddleware:
    """Refuse, avant toute route, une requête qui ne vient pas d'où elle est attendue."""

    def __init__(self, app: ASGIApp, guard: RequestGuard) -> None:
        self._app = app
        self._guard = guard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            refusal = self._guard.refusal(scope["headers"])
            if refusal is not None:
                response = JSONResponse(status_code=403, content={"error": refusal})
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def guard_from_settings(
    allowed_hosts: Sequence[str] | None,
    allowed_origins: Sequence[str] | None,
    environment: Mapping[str, str],
) -> RequestGuard:
    """Hôtes et origines attendus : ceux de l'appelant, sinon ceux de l'environnement.

    La boucle locale est toujours acceptée.
    """
    try:
        hosts = allowed_hosts if allowed_hosts is not None else _listed(environment, ENV_HOSTS)
        origins = (
            allowed_origins if allowed_origins is not None else _listed(environment, ENV_ORIGINS)
        )
        return RequestGuard.create([*LOOPBACK_HOSTS, *hosts], origins)
    except ValueError as error:
        raise ValueError(f"{error} Réglages : {ENV_HOSTS}, {ENV_ORIGINS}.") from error


def _listed(environment: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in environment.get(name, "").split(",") if item.strip())
