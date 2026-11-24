"""Le jeton d'un émetteur envers l'API d'AnswerLineage : octroi ``client_credentials``.

L'API exige un jeton porteur sur chacune de ses routes. Un émetteur, le proxy
par exemple, obtient le sien auprès de l'émetteur OIDC que règle l'exploitant,
par l'octroi ``client_credentials`` (RFC 6749, 4.4), avec son identifiant de
client et son secret. Ce jeton n'a rien à voir avec ceux des agents : c'est
l'identité de l'émetteur d'événements lui-même auprès de l'API.

- **le point de jeton est découvert** (``/.well-known/openid-configuration``) :
  le document doit nommer l'émetteur réglé, et le point de jeton est en
  ``https``, ou en ``http`` sur la boucle locale seulement ;
- **le jeton reste en mémoire**, jamais écrit ni journalisé, et se renouvelle
  avant son expiration ; un refus ``401`` de l'API le fait oublier ;
- **le secret vient d'une variable d'environnement ou d'un fichier**, que
  chaque émetteur nomme, jamais de la ligne de commande, que la liste des
  processus montre ; il n'est ni journalisé ni affiché ;
- **rien n'attend le jeton sur le chemin d'un appel** : seules des tâches de
  fond le demandent, celle de la boîte d'envoi par exemple. Une panne de
  l'émetteur OIDC se lit comme une panne de l'API : les événements attendent
  dans la boîte d'envoi.

Le journal, celui de l'émetteur qui emploie le jeton (``logger``), ne dit que
la classe d'un échec ou un statut : ni l'adresse de l'émetteur OIDC, ni sa
réponse, ni l'identifiant du client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

import httpx

from answerlineage_emitter.config import ConfigurationError

# Journal par défaut ; chaque émetteur donne le sien.
DEFAULT_LOGGER: Final = logging.getLogger("answerlineage_emitter")

DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
# Délais d'une demande à l'émetteur : connexion, chaque lecture, puis la demande entière.
CONNECT_TIMEOUT: Final = 5.0
READ_TIMEOUT: Final = 5.0
REQUEST_TIMEOUT: Final = 10.0
# Réponse de l'émetteur lue au plus : un document de découverte ou un jeton y tiennent.
MAX_RESPONSE_BYTES: Final = 64 * 1024
# Un jeton porteur, au plus : celui que l'API admet.
MAX_TOKEN_LENGTH: Final = 16 * 1024
MAX_SECRET_BYTES: Final = 4096
MAX_SETTING_LENGTH: Final = 1024
# Durée de vie admise d'un jeton ; sans durée annoncée, cinq minutes.
DEFAULT_LIFETIME: Final = 300
MAX_LIFETIME: Final = 24 * 3600
# Le jeton se renouvelle avant d'expirer : une minute avant, ou au cinquième de sa vie.
RENEW_MARGIN: Final = 60.0
# Après un échec, l'émetteur OIDC n'est pas redemandé avant ce délai : la boîte d'envoi,
# et les autres tâches de fond qui portent le jeton, ne le sollicitent pas à chaque reprise.
COOL_DOWN: Final = 5.0

# Un jeton porteur (RFC 6750, 2.1) : token68.
_TOKEN: Final = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
# Identifiant de client, portée, audience : du texte imprimable, sans espace de contrôle.
_PRINTABLE: Final = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")
_CLIENT_LOGGERS: Final = ("httpx", "httpcore")
# La boucle locale, telle que httpx écrit l'hôte d'une adresse (IPv6 sans crochets).
_LOOPBACK_NAMES: Final = ("localhost", "::1")


class TokenUnavailableError(Exception):
    """Aucun jeton n'est disponible. ``reason`` est un texte fixe ou un statut, jamais une
    réponse de l'émetteur."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Tokens(Protocol):
    """Ce que la boîte d'envoi, et toute tâche de fond qui joint l'API, attendent d'un jeton."""

    async def token(self) -> str: ...

    def invalidate(self) -> None: ...


def secure_url(url: httpx.URL) -> bool:
    """Vrai quand un jeton peut partir vers cette adresse : https, ou http sur la boucle locale."""
    host = url.host.lower()
    loopback = host in _LOOPBACK_NAMES or host.startswith("127.")
    return url.scheme == "https" or (url.scheme == "http" and loopback)


@dataclass(frozen=True)
class ClientCredentials:
    """L'émetteur OIDC, l'identifiant du client et son secret, qu'un émetteur d'événements lui
    présente."""

    issuer: str
    client_id: str
    secret: str = field(repr=False)
    scope: str | None = None
    audience: str | None = None

    @classmethod
    def create(
        cls,
        issuer: str,
        client_id: str,
        secret: str,
        *,
        scope: str | None = None,
        audience: str | None = None,
    ) -> ClientCredentials:
        """Contrôle les réglages. Aucun message ne cite une valeur : un secret pourrait être
        donné par erreur à la place d'un autre réglage."""
        try:
            url = httpx.URL(issuer)
        except (httpx.InvalidURL, TypeError):
            raise ConfigurationError("L'adresse de l'émetteur OIDC est illisible.") from None
        if len(issuer) > MAX_SETTING_LENGTH or not url.host or url.userinfo:
            raise ConfigurationError("L'adresse de l'émetteur OIDC est une URL sans identifiant.")
        if url.query or url.fragment:
            raise ConfigurationError(
                "L'adresse de l'émetteur OIDC ne porte ni paramètres ni fragment."
            )
        if not secure_url(url):
            raise ConfigurationError(
                "L'émetteur OIDC se joint en https, ou en http sur la boucle locale seulement."
            )
        for name, value in (("identifiant du client", client_id), ("portée", scope)):
            if value is not None and (
                len(value) > MAX_SETTING_LENGTH or not _PRINTABLE.fullmatch(value)
            ):
                raise ConfigurationError(f"Réglage OIDC illisible : {name}.")
        if audience is not None and (
            len(audience) > MAX_SETTING_LENGTH or not _PRINTABLE.fullmatch(audience)
        ):
            raise ConfigurationError("Réglage OIDC illisible : audience.")
        if not secret or len(secret.encode()) > MAX_SECRET_BYTES or not secret.isprintable():
            raise ConfigurationError("Le secret du client OIDC est illisible.")
        return cls(issuer.rstrip("/"), client_id, secret, scope, audience)


def client_secret(
    environment: Mapping[str, str],
    secret_file: str | None = None,
    *,
    variable: str,
    file_variable: str,
) -> str | None:
    """Le secret du client : dans un fichier (``secret_file``, sinon celui que nomme la
    variable ``file_variable``) ou dans la variable ``variable``. ``None`` sans secret.

    Aucun message ne cite le secret, ni le chemin du fichier.
    """
    path = secret_file or environment.get(file_variable) or None
    text = environment.get(variable) or None
    if path is not None and text is not None:
        raise ConfigurationError(
            f"Le secret du client OIDC vient d'un fichier ou de {variable}, pas des deux."
        )
    if path is None:
        return text
    try:
        with Path(path).open("rb") as source:
            content = source.read(MAX_SECRET_BYTES + 1)
    except OSError:
        raise ConfigurationError("Le fichier du secret du client OIDC est illisible.") from None
    if len(content) > MAX_SECRET_BYTES:
        raise ConfigurationError("Le fichier du secret du client OIDC est trop grand.")
    try:
        return content.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ConfigurationError(
            "Le fichier du secret du client OIDC n'est pas du texte UTF-8."
        ) from None


def _quiet_client_logs() -> None:
    for name in _CLIENT_LOGGERS:
        client_logger = logging.getLogger(name)
        if client_logger.level == logging.NOTSET or client_logger.level < logging.WARNING:
            client_logger.setLevel(logging.WARNING)


def _document(body: bytes) -> Mapping[str, Any]:
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        raise TokenUnavailableError("réponse illisible") from None
    if not isinstance(document, dict):
        raise TokenUnavailableError("réponse illisible")
    return document


def _lifetime(value: object) -> int:
    if value is None:
        return DEFAULT_LIFETIME
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TokenUnavailableError("durée de vie illisible")
    return min(value, MAX_LIFETIME)


class ClientCredentialsTokens:
    """Le jeton d'un émetteur : obtenu de l'émetteur OIDC, gardé en mémoire, renouvelé avant
    d'expirer.

    Une seule demande à la fois : les tâches de fond d'un émetteur, comme la boîte
    d'envoi et la synchronisation des quarantaines du proxy, se partagent le jeton.
    """

    def __init__(
        self,
        credentials: ClientCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger = DEFAULT_LOGGER,
    ) -> None:
        _quiet_client_logs()
        self._logger = logger
        self._credentials = credentials
        self._transport = transport
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._renew_at = 0.0
        self._token_endpoint: httpx.URL | None = None
        self._retry_at = 0.0
        self._failing = False
        # Jetons obtenus de l'émetteur, et demandes en échec : des nombres seulement.
        self.obtained = 0
        self.failures = 0

    def __repr__(self) -> str:
        return f"ClientCredentialsTokens(obtained={self.obtained}, failures={self.failures})"

    def invalidate(self) -> None:
        """L'API a refusé le jeton : le prochain envoi en demande un nouveau."""
        self._token = None
        self._renew_at = 0.0

    async def token(self) -> str:
        """Le jeton en cours, ou un nouveau ; ``TokenUnavailableError`` si l'émetteur n'en
        donne pas."""
        async with self._lock:
            now = self._clock()
            if self._token is not None and now < self._renew_at:
                return self._token
            if now < self._retry_at:
                raise TokenUnavailableError("émetteur en échec, nouvel essai différé")
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    token, lifetime = await self._obtain()
            except TokenUnavailableError as error:
                self._failed(error.reason)
                raise
            except (httpx.HTTPError, TimeoutError) as error:
                self._failed(type(error).__name__)
                raise TokenUnavailableError(type(error).__name__) from None
            obtained = self._clock()
            self._token = token
            self._renew_at = obtained + lifetime - min(RENEW_MARGIN, lifetime / 5)
            self._retry_at = 0.0
            self.obtained += 1
            if self._failing:
                self._failing = False
                self._logger.info("Jeton d'accès à l'API de nouveau obtenu de l'émetteur OIDC.")
            return token

    def _failed(self, reason: str) -> None:
        self.failures += 1
        self._token = None
        self._token_endpoint = None
        self._retry_at = self._clock() + COOL_DOWN
        if not self._failing:
            self._failing = True
            self._logger.warning(
                "Jeton d'accès à l'API indisponible (%s) : les événements attendent dans la "
                "boîte d'envoi.",
                reason,
            )

    def _client(self) -> httpx.AsyncClient:
        # Certificat vérifié, aucune redirection suivie, rien de l'environnement.
        client = httpx.AsyncClient(
            verify=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            transport=self._transport,
        )
        client.headers.clear()
        return client

    async def _read(self, reply: httpx.Response) -> bytes:
        body = bytearray()
        async for chunk in reply.aiter_bytes():
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise TokenUnavailableError("réponse trop grande")
        return bytes(body)

    async def _obtain(self) -> tuple[str, int]:
        async with self._client() as client:
            endpoint = self._token_endpoint or await self._discover(client)
            self._token_endpoint = endpoint
            form = {"grant_type": "client_credentials"}
            if self._credentials.scope is not None:
                form["scope"] = self._credentials.scope
            if self._credentials.audience is not None:
                form["audience"] = self._credentials.audience
            async with client.stream(
                "POST",
                endpoint,
                data=form,
                # client_secret_basic : identifiant et secret encodés (RFC 6749, 2.3.1).
                auth=httpx.BasicAuth(
                    _form_encoded(self._credentials.client_id),
                    _form_encoded(self._credentials.secret),
                ),
                headers={"accept": "application/json"},
            ) as reply:
                if reply.status_code != 200:
                    raise TokenUnavailableError(f"statut {reply.status_code}")
                answer = _document(await self._read(reply))
        token = answer.get("access_token")
        if (
            not isinstance(token, str)
            or len(token) > MAX_TOKEN_LENGTH
            or not _TOKEN.fullmatch(token)
        ):
            raise TokenUnavailableError("jeton illisible")
        kind = answer.get("token_type")
        if not isinstance(kind, str) or kind.lower() != "bearer":
            raise TokenUnavailableError("jeton d'un autre type que porteur")
        return token, _lifetime(answer.get("expires_in"))

    async def _discover(self, client: httpx.AsyncClient) -> httpx.URL:
        url = httpx.URL(self._credentials.issuer + DISCOVERY_PATH)
        async with client.stream("GET", url, headers={"accept": "application/json"}) as reply:
            if reply.status_code != 200:
                raise TokenUnavailableError(f"découverte, statut {reply.status_code}")
            document = _document(await self._read(reply))
        if document.get("issuer") != self._credentials.issuer:
            raise TokenUnavailableError("découverte d'un autre émetteur")
        endpoint = document.get("token_endpoint")
        try:
            found = httpx.URL(endpoint) if isinstance(endpoint, str) else None
        except httpx.InvalidURL:
            found = None
        if found is None or not found.host or found.userinfo or not secure_url(found):
            raise TokenUnavailableError("point de jeton refusé")
        return found


def _form_encoded(value: str) -> str:
    """L'encodage que RFC 6749 (2.3.1) demande avant l'authentification HTTP de base."""
    return str(httpx.QueryParams({"v": value})).removeprefix("v=")
