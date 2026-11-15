"""Contrôle des en-têtes Host et Origin, avant tout relais.

Un service qui écoute sur la boucle locale reste atteignable par une page web
ouverte sur le même poste. Par réassociation DNS, un nom d'hôte de l'attaquant
finit par désigner 127.0.0.1 : le navigateur envoie alors ce nom dans l'en-tête
Host, et l'adresse de la page dans Origin. Une requête dont l'un des deux n'est
pas attendu est refusée.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
# Accepte tout hôte et toute origine : choix explicite de l'opérateur.
ANY_HOST = "*"

_HOST = r"\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9_-][A-Za-z0-9._-]{0,252}"
_HOST_NAME = re.compile(_HOST)
_AUTHORITY = re.compile(rf"({_HOST})(?::[0-9]{{1,5}})?")
_ORIGIN = re.compile(rf"https?://({_HOST})(?::[0-9]{{1,5}})?", re.IGNORECASE)


def _name(host: str) -> str:
    return host.lower().removesuffix(".")


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
        """Code du refus, ou ``None`` quand Host et Origin sont attendus."""
        if ANY_HOST in self.hosts:
            return None
        hosts: list[str] = []
        origins: list[str] = []
        for name, value in headers:
            lowered = name.lower()
            if lowered == b"host":
                hosts.append(value.decode("latin-1"))
            elif lowered == b"origin":
                origins.append(value.decode("latin-1"))

        authority = _AUTHORITY.fullmatch(hosts[0]) if len(hosts) == 1 else None
        if authority is None or _name(authority[1]) not in self.hosts:
            return "host_not_allowed"
        if not origins:
            return None
        origin = _ORIGIN.fullmatch(origins[0]) if len(origins) == 1 else None
        if origin is None:
            return "origin_not_allowed"
        if origins[0].lower() in self.origins or _name(origin[1]) in self.hosts:
            return None
        return "origin_not_allowed"
