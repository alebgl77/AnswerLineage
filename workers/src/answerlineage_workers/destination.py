"""Destination de la réémission : un consommateur OpenLineage externe, joint en HTTP.

Marquez, DataHub, OpenMetadata, Dataplex lisent le transport HTTP d'OpenLineage :
un événement par requête ``POST``, en JSON. La destination est l'adresse de ce
point d'entrée, réglée par l'exploitant au démarrage du worker, jamais tirée d'un
événement ni d'une réponse :

- **une adresse ``https``**, ou ``http`` sur la boucle locale seulement, pour les
  tests et la démonstration ; sans identifiant, paramètre ni fragment : elle peut
  apparaître dans la configuration, et aucun message ne la cite ;
- **ni métadonnées des clouds ni lien local.** Les adresses de lien local
  (``169.254.0.0/16``, ``fe80::/10``), où les clouds servent leurs métadonnées et
  les identifiants d'une machine, celles de métadonnées hors du lien local
  (``fd00:ec2::254``, ``100.100.100.200``, ``168.63.129.16``), et les adresses
  nulles, de diffusion ou de groupe sont refusées, écrites telles quelles ou
  enveloppées dans une adresse IPv6 (``::ffff:…``, ``64:ff9b::…``). Le contrôle
  vaut au démarrage, pour une adresse écrite et pour ce qu'un nom désigne alors,
  puis à chaque connexion : le nom est résolu, chaque adresse contrôlée, et la
  connexion se fait à l'adresse contrôlée, pour qu'une seconde résolution ne mène
  pas ailleurs ;
- **les autres adresses internes sont admises** (``10.0.0.0/8``,
  ``192.168.0.0/16``, ``fc00::/7``…) : le catalogue d'une entreprise est souvent
  sur son réseau privé, et l'adresse vient de l'exploitant, pas d'un tiers ;
- **TLS vérifié**, certificat et nom, TLS 1.2 au moins ; **aucune redirection
  suivie** ; aucun mandataire lu de l'environnement ;
- **des délais bornés** : la connexion, chaque lecture et écriture, et l'envoi
  entier, qu'une réponse servie goutte à goutte ne prolonge pas ;
- **un en-tête ``Authorization`` facultatif**, lu de l'environnement, envoyé tel
  quel, jamais journalisé ni affiché.

Le client parle HTTP/1.1 et garde sa connexion d'un événement à l'autre. Il ne
lit de la réponse que son statut, et au plus 64 Kio de corps, qu'il ne garde
pas. Il n'emploie que la bibliothèque standard ; ses entrées-sorties se font
dans un fil à lui, hors de la boucle des workers.
"""

from __future__ import annotations

import asyncio
import http.client
import re
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any, Final, Literal
from urllib.parse import urlsplit

URL_VARIABLE: Final = "ANSWERLINEAGE_REEMIT_URL"
AUTHORIZATION_VARIABLE: Final = "ANSWERLINEAGE_REEMIT_AUTHORIZATION"
MAX_URL_LENGTH: Final = 2048
MAX_AUTHORIZATION_LENGTH: Final = 8192
# Corps de la réponse lu au plus, sans être gardé ; au-delà, la connexion est fermée.
MAX_RESPONSE_BYTES: Final = 64 * 1024
# Une destination refuse l'événement lui-même : le renvoyer n'y changerait rien.
REJECTED_STATUSES: Final = frozenset({400, 413, 422})

Address = IPv4Address | IPv6Address
# Les adresses d'un nom, pour un port : ``socket.getaddrinfo`` hors des tests.
Resolver = Callable[[str, int], Sequence[Address]]

# Métadonnées des clouds hors du lien local : AWS en IPv6, Alibaba Cloud, Azure.
_METADATA: Final = (
    ip_network("fd00:ec2::254/128"),
    ip_network("100.100.100.200/32"),
    ip_network("168.63.129.16/32"),
)
_BROADCAST: Final = IPv4Address("255.255.255.255")
# Préfixe NAT64 bien connu : une adresse IPv4 y voyage dans les 32 derniers bits.
_NAT64: Final = ip_network("64:ff9b::/96")
_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME: Final = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}\.?")
_PRINTABLE: Final = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")


class DestinationError(ValueError):
    """La destination réglée est refusée. Le message est un gabarit fixe : il ne la cite pas."""


class ForbiddenAddressError(OSError):
    """Le nom de la destination désigne une adresse interdite : rien n'est envoyé."""


def _unwrapped(address: Address) -> Address:
    """L'adresse IPv4 qu'une adresse IPv6 enveloppe, ou l'adresse elle-même."""
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _NAT64:
            return IPv4Address(int(address) & 0xFFFF_FFFF)
    return address


def forbidden(address: Address) -> bool:
    """Vrai pour une adresse où aucun catalogue n'a sa place : métadonnées, lien local, nulle."""
    address = _unwrapped(address)
    return (
        address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address == _BROADCAST
        or any(address in network for network in _METADATA)
    )


def admitted(scheme: str, address: Address) -> bool:
    """Vrai quand la destination peut être jointe à cette adresse.

    En ``http``, la boucle locale seulement : ailleurs, l'événement et l'en-tête
    d'authentification voyageraient en clair.
    """
    if forbidden(address):
        return False
    return scheme == "https" or _unwrapped(address).is_loopback


def _literal(host: str) -> Address | None:
    try:
        return ip_address(host)
    except ValueError:
        return None


@dataclass(frozen=True)
class Destination:
    """Le point d'entrée OpenLineage d'un consommateur externe, contrôlé."""

    scheme: Literal["http", "https"]
    host: str
    port: int
    # Le chemin de la requête, tel que l'adresse le donne.
    target: str
    # Jamais affiché : ni par ``repr``, ni dans un message.
    authorization: str | None = field(default=None, repr=False)

    @property
    def secure(self) -> bool:
        return self.scheme == "https"


def parse_destination(url: str, authorization: str | None = None) -> Destination:
    """Contrôle l'adresse de la destination et l'en-tête d'authentification.

    Lève ``DestinationError``, dont le message ne cite ni l'une ni l'autre.
    """
    if len(url) > MAX_URL_LENGTH or not url.isascii() or not url.isprintable() or " " in url:
        raise DestinationError("L'adresse de la destination est illisible.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise DestinationError("L'adresse de la destination est illisible.") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise DestinationError(
            "La destination doit être une adresse https, ou http sur la boucle locale."
        )
    if "@" in parts.netloc:
        raise DestinationError("L'adresse de la destination ne doit contenir aucun identifiant.")
    if "?" in url or "#" in url:
        raise DestinationError(
            "L'adresse de la destination ne doit porter ni paramètres ni fragment."
        )
    host = (parts.hostname or "").rstrip(".")
    literal = _literal(host)
    if literal is None and (not host or len(host) > 253 or not _HOSTNAME.fullmatch(host)):
        raise DestinationError("L'adresse de la destination ne désigne aucun hôte.")
    if literal is not None and forbidden(literal):
        raise DestinationError(
            "La destination désigne une adresse de métadonnées, de lien local ou nulle : refusée."
        )
    if scheme == "http" and not (
        host == "localhost" or (literal is not None and admitted("http", literal))
    ):
        raise DestinationError(
            "Une destination en http doit être sur la boucle locale ; ailleurs, en https."
        )
    if authorization is not None and (
        len(authorization) > MAX_AUTHORIZATION_LENGTH or not _PRINTABLE.fullmatch(authorization)
    ):
        raise DestinationError(
            "L'en-tête d'authentification de la destination doit être un texte ASCII imprimable"
            f" d'au plus {MAX_AUTHORIZATION_LENGTH} caractères."
        )
    return Destination(
        "https" if scheme == "https" else "http",
        host,
        port or (443 if scheme == "https" else 80),
        parts.path or "/",
        authorization,
    )


def resolve(host: str, port: int) -> list[Address]:
    """Les adresses d'un nom, dans l'ordre du résolveur, sans doublon."""
    found: list[Address] = []
    for *_, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = ip_address(str(sockaddr[0]).partition("%")[0])
        if address not in found:
            found.append(address)
    return found


def addresses_of(destination: Destination, resolver: Resolver = resolve) -> list[Address]:
    """Les adresses où joindre la destination ; ``ForbiddenAddressError`` si l'une est interdite.

    Un nom qui mène, ne serait-ce que par une de ses adresses, à une adresse
    interdite est refusé en entier : ce n'est pas un catalogue.
    """
    literal = _literal(destination.host)
    found = [literal] if literal is not None else list(resolver(destination.host, destination.port))
    if not found:
        raise ForbiddenAddressError("aucune adresse")
    if not all(admitted(destination.scheme, address) for address in found):
        raise ForbiddenAddressError("adresse interdite")
    return found


def check_resolution(destination: Destination, resolver: Resolver = resolve) -> None:
    """Au démarrage : refuse un nom qui mène à une adresse interdite.

    Un nom qui ne se résout pas encore est admis : il sera contrôlé à chaque
    connexion, et une destination injoignable ne doit pas empêcher le worker de
    projeter.
    """
    try:
        addresses_of(destination, resolver)
    except ForbiddenAddressError:
        raise DestinationError(
            "Le nom de la destination mène à une adresse de métadonnées, de lien local ou nulle,"
            " ou hors de la boucle locale en http : refusé."
        ) from None
    except OSError:
        return


def tls_context() -> ssl.SSLContext:
    """Vérifie le certificat et le nom de la destination, TLS 1.2 au moins.

    Les autorités sont celles du système ; ``SSL_CERT_FILE`` ou ``SSL_CERT_DIR``
    en désignent d'autres, pour un catalogue servi sous une autorité interne.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


@dataclass(frozen=True)
class Timeouts:
    """Délais d'un envoi, en secondes : connexion, chaque lecture ou écriture, envoi entier."""

    connect: float = 5.0
    io: float = 10.0
    request: float = 30.0


class _Connection(http.client.HTTPConnection):
    """Une connexion déjà ouverte vers une adresse contrôlée, et qui ne s'ouvre pas seule.

    ``http.client`` rouvre d'elle-même une connexion fermée, vers le nom de
    l'hôte : ce serait une résolution que rien ne contrôle.
    """

    def connect(self) -> None:
        raise http.client.NotConnected


class _SecureConnection(_Connection):
    default_port = 443


class _Client:
    """Envoie à la destination, une requête à la fois, dans le fil de l'expéditeur."""

    def __init__(
        self,
        destination: Destination,
        resolver: Resolver,
        timeouts: Timeouts,
        context: ssl.SSLContext,
    ) -> None:
        self._destination = destination
        self._resolver = resolver
        self._timeouts = timeouts
        self._context = context
        self._connection: _Connection | None = None
        self._socket: socket.socket | None = None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if destination.authorization is not None:
            headers["Authorization"] = destination.authorization
        self._headers = headers

    def _left(self, deadline: float, bound: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return min(bound, left)

    def _open(self, deadline: float) -> _Connection:
        destination = self._destination
        last: OSError = ForbiddenAddressError("aucune adresse")
        for address in addresses_of(destination, self._resolver):
            try:
                raw = socket.create_connection(
                    (str(address), destination.port),
                    timeout=self._left(deadline, self._timeouts.connect),
                )
                break
            except OSError as error:
                last = error
        else:
            raise last
        self._socket = raw
        try:
            if destination.secure:
                raw.settimeout(self._left(deadline, self._timeouts.io))
                self._socket = self._context.wrap_socket(raw, server_hostname=destination.host)
            kind = _SecureConnection if destination.secure else _Connection
            connection = kind(destination.host, destination.port)
            connection.sock = self._socket
        except BaseException:
            self.close()
            raise
        return connection

    def post(self, body: bytes, deadline: float) -> int:
        """Envoie le corps et rend le statut de la réponse.

        Une connexion gardée que la destination a fermée entre deux envois est
        rouverte une fois : l'événement n'était pas parti.
        """
        reused = self._connection is not None and self._connection.sock is not None
        try:
            return self._exchange(body, deadline)
        except (ConnectionResetError, BrokenPipeError):
            if not reused:
                raise
            return self._exchange(body, deadline)

    def _exchange(self, body: bytes, deadline: float) -> int:
        try:
            if self._connection is None or self._connection.sock is None:
                self.close()
                self._connection = self._open(deadline)
            connection = self._connection
            assert self._socket is not None  # noqa: S101 — ouvert par _open.
            self._socket.settimeout(self._left(deadline, self._timeouts.io))
            connection.request("POST", self._destination.target, body=body, headers=self._headers)
            self._socket.settimeout(self._left(deadline, self._timeouts.io))
            response = connection.getresponse()
            response.read(MAX_RESPONSE_BYTES)
            if not response.isclosed() or connection.sock is None:
                # Une réponse plus longue que lue, ou une connexion que la destination ferme.
                self.close()
            return response.status
        except BaseException:
            self.close()
            raise

    def abort(self) -> None:
        """Coupe la connexion en cours, depuis un autre fil : l'envoi bloqué échoue aussitôt."""
        current = self._socket
        if current is not None:
            with suppress(OSError):
                socket.socket.shutdown(current, socket.SHUT_RDWR)

    def close(self) -> None:
        connection, self._connection = self._connection, None
        current, self._socket = self._socket, None
        if connection is not None:
            with suppress(OSError):
                connection.close()
        if current is not None:
            with suppress(OSError):
                current.close()


Verdict = Literal["sent", "rejected", "failed", "interrupted"]


@dataclass(frozen=True)
class Delivery:
    """L'issue d'un envoi : le verdict et sa raison, un statut ou une classe d'erreur."""

    verdict: Verdict
    # « HTTP 201 », « HTTP 503 », « ConnectionRefusedError » : jamais un message.
    reason: str

    @classmethod
    def of(cls, status: int) -> Delivery:
        if 200 <= status < 300:
            return cls("sent", f"HTTP {status}")
        if status in REJECTED_STATUSES:
            return cls("rejected", f"HTTP {status}")
        # Une redirection n'est pas suivie : la destination est mal réglée, ou détournée.
        return cls("failed", f"HTTP {status}")


class Sender:
    """Envoie les événements à la destination, un à la fois, hors de la boucle des workers.

    Chaque envoi a un délai total : au-delà, la connexion est coupée et l'envoi
    compte comme un échec, dont l'issue n'est pas sûre. Un arrêt demandé coupe
    l'envoi en cours de même.
    """

    def __init__(
        self,
        destination: Destination,
        *,
        resolver: Resolver = resolve,
        timeouts: Timeouts | None = None,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self.timeouts = timeouts or Timeouts()
        self._client = _Client(destination, resolver, self.timeouts, context or tls_context())
        # Un seul fil : un envoi abandonné à son délai finit avant le suivant.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reemission")

    async def send(self, body: bytes, stop: asyncio.Event | None = None) -> Delivery:
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + self.timeouts.request
        sending = loop.run_in_executor(self._executor, self._client.post, body, deadline)
        waits: set[asyncio.Future[Any]] = {sending}
        stopped = None if stop is None else asyncio.ensure_future(stop.wait())
        if stopped is not None:
            waits.add(stopped)
        try:
            done, _ = await asyncio.wait(
                waits, timeout=self.timeouts.request, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if stopped is not None:
                stopped.cancel()
        if sending not in done:
            self._client.abort()
            sending.cancel()
            if stopped is not None and stopped in done:
                return Delivery("interrupted", "stop")
            return Delivery("failed", "TimeoutError")
        try:
            status = sending.result()
        except (OSError, http.client.HTTPException, ValueError) as error:
            return Delivery("failed", type(error).__name__)
        return Delivery.of(status)

    def close(self) -> None:
        self._client.abort()
        self._executor.submit(self._client.close)
        self._executor.shutdown(wait=False, cancel_futures=False)
