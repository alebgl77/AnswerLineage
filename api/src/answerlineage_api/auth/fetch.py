"""Lecture des documents de l'émetteur OIDC : découverte et JWKS, bornée et sans détour.

L'API ne lit que deux documents de l'émetteur qu'elle a réglé : sa découverte
(``/.well-known/openid-configuration``), puis le JWKS qu'elle désigne. Ces
documents disent quelles clés signent les jetons : une lecture détournée
ferait accepter des jetons forgés. Elle se fait donc :

- **en https**, ou en http vers la boucle locale seulement, pour les tests et
  un émetteur posé sur le même poste ;
- **sans redirection suivie**, sans mandataire ni identifiant lus de
  l'environnement, certificat et nom vérifiés ;
- **bornée** : 5 s par document, connexion comprise, et 256 Kio au plus ;
- **dans un fil**, hors de la boucle qui sert les requêtes, avec la
  bibliothèque standard seule.

Une lecture qui échoue lève ``IssuerUnavailableError`` : son motif est un
texte fixe, jamais une réponse de l'émetteur.
"""

from __future__ import annotations

import asyncio
import ssl
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any, Final, NoReturn
from urllib.parse import urlsplit

FETCH_TIMEOUT: Final = 5.0
MAX_DOCUMENT_BYTES: Final = 256 * 1024
_LOOPBACK: Final = ("127.0.0.1", "localhost", "::1")

# Lit un document de l'émetteur, désigné par son adresse : ses octets, bornés.
Fetch = Callable[[str], Awaitable[bytes]]


class IssuerUnavailableError(Exception):
    """L'émetteur ne donne pas un document lisible. ``reason`` est un texte fixe."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def secure_url(url: str) -> bool:
    """Vrai pour une adresse en https, ou en http vers la boucle locale, sans identifiant."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if not host or "@" in parts.netloc or parts.fragment:
        return False
    loopback = host in _LOOPBACK or host.startswith("127.")
    return parts.scheme == "https" or (parts.scheme == "http" and loopback)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Une redirection est un refus : l'émetteur dit lui-même où sont ses documents."""

    def redirect_request(self, *_: Any, **__: Any) -> NoReturn:
        raise IssuerUnavailableError("redirection refusée")


def _opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return urllib.request.build_opener(
        # Aucun mandataire de l'environnement : seule l'adresse réglée est jointe.
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )


def _read(url: str) -> bytes:
    if not secure_url(url):
        raise IssuerUnavailableError("adresse refusée")
    request = urllib.request.Request(url, headers={"accept": "application/json"})  # noqa: S310
    try:
        with _opener().open(request, timeout=FETCH_TIMEOUT) as response:
            if response.status != 200:
                raise IssuerUnavailableError(f"statut {response.status}")
            body: bytes = response.read(MAX_DOCUMENT_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise IssuerUnavailableError(f"statut {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise IssuerUnavailableError(type(error).__name__) from None
    if len(body) > MAX_DOCUMENT_BYTES:
        raise IssuerUnavailableError("document trop grand")
    return body


async def fetch_document(url: str) -> bytes:
    """Les octets d'un document de l'émetteur, lus dans un fil, sous un délai borné."""
    try:
        async with asyncio.timeout(FETCH_TIMEOUT):
            return await asyncio.to_thread(_read, url)
    except TimeoutError:
        raise IssuerUnavailableError("délai dépassé") from None
