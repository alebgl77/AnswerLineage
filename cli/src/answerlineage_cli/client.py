"""Client HTTP de la commande : une adresse contrôlée, des délais et des tailles bornés.

La commande parle à l'API comme l'explorateur, par HTTP. Ce qu'elle reçoit
est hostile tant qu'il n'est pas validé :

- la réponse est lue sans compression (``Accept-Encoding: identity``) et
  refusée si elle en porte une : sa taille se borne en octets reçus, et une
  bombe de décompression n'est jamais déployée ;
- la lecture s'arrête dès que la borne de taille est franchie, que la réponse
  annonce sa longueur ou non, et dès que le délai total est passé ;
- une redirection n'est pas suivie ;
- les variables de mandataire (``HTTPS_PROXY``…) et ``.netrc`` sont ignorées :
  la commande joint l'adresse donnée, directement, sans y joindre d'identifiant.

httpx journalise l'adresse complète de chaque requête, paramètres compris :
le niveau de ses journaux est relevé, pour qu'aucun nom de jeu de données ni
aucune fenêtre d'incident n'atteigne un journal.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from answerlineage_cli import __version__

ENV_URL: Final = "ANSWERLINEAGE_API_URL"
DEFAULT_URL: Final = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT: Final = 30.0
MAX_TIMEOUT: Final = 600
MAX_URL_LENGTH: Final = 2048
# Un refus de l'API tient en quelques dizaines d'octets.
MAX_REFUSAL_BYTES: Final = 64 * 1024

_HOST = re.compile(r"\[[0-9a-f:.]{2,45}\]|[a-z0-9_-][a-z0-9._-]{0,252}")
_PATH = re.compile(r"(/[A-Za-z0-9._~!$&'()*+,;=:@%-]*)*")
_TRANSPORT_LOGGERS: Final = ("httpx", "httpcore")


class UsageError(Exception):
    """Un réglage de la commande est inutilisable. Le message est un texte fixe."""


class UnreachableError(Exception):
    """L'API ne répond pas à l'adresse donnée, ou pas dans le délai."""


class InvalidResponseError(Exception):
    """La réponse de l'API n'a pas la forme attendue.

    ``where`` désigne l'endroit de la réponse, par les clés du contrat et les
    rangs, jamais par ce qui s'y trouvait ; ``reason`` est un texte fixe.
    """

    def __init__(self, reason: str, where: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.where = where


@dataclass(frozen=True)
class Received:
    """Une réponse lue en entier, dans ses bornes."""

    status: int
    content_type: str
    body: bytes

    def json(self) -> Any:
        """Le document JSON du corps : UTF-8, sans NaN ni infini, ni profondeur excessive."""
        if self.content_type != "application/json":
            raise InvalidResponseError("elle n'est pas du JSON")
        try:
            return json.loads(self.body.decode("utf-8"), parse_constant=_refuse_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise InvalidResponseError("elle n'est pas du JSON lisible") from None


def _refuse_constant(_: str) -> Any:
    raise ValueError


def quiet_transport_logs() -> None:
    """Relève le niveau des journaux du client HTTP, qui citeraient adresses et en-têtes."""
    for name in _TRANSPORT_LOGGERS:
        transport_logger = logging.getLogger(name)
        if transport_logger.level < logging.WARNING:
            transport_logger.setLevel(logging.WARNING)


def api_base(value: str) -> str:
    """L'adresse de base de l'API, normalisée ; ``UsageError`` si elle n'est pas admise."""
    refusal = UsageError(
        f"L'adresse de l'API (--url ou {ENV_URL}) s'écrit http://hôte[:port][/chemin] ou "
        "https://…, sans identifiant, ni paramètre, ni fragment."
    )
    if len(value) > MAX_URL_LENGTH or not value.isascii() or not value.isprintable():
        raise refusal
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise refusal from None
    scheme = parts.scheme.lower()
    host = (parts.netloc.rpartition("@")[2].rpartition(":")[0] if port else parts.netloc).lower()
    if (
        scheme not in ("http", "https")
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
        or not _HOST.fullmatch(host)
        or not _PATH.fullmatch(parts.path)
    ):
        raise refusal
    authority = host if port is None else f"{host}:{port}"
    return f"{scheme}://{authority}{parts.path.rstrip('/')}"


def configured_base(option: str | None, environment: Mapping[str, str]) -> str:
    """L'adresse de l'option, sinon celle de l'environnement, sinon la boucle locale."""
    if option is not None:
        return api_base(option)
    return api_base(environment.get(ENV_URL) or DEFAULT_URL)


def is_loopback(base: str) -> bool:
    host = urlsplit(base).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


class Api:
    """Les requêtes de la commande vers une API AnswerLineage."""

    def __init__(
        self,
        base: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base = base
        self._timeout = timeout
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "accept-encoding": "identity",
                "user-agent": f"answerlineage-cli/{__version__}",
            },
        )
        quiet_transport_logs()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Api:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(
        self,
        path: str,
        *,
        max_bytes: int,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Received:
        """Une requête GET, sa réponse lue dans ses bornes de taille et de durée."""
        return self._send("GET", path, max_bytes=max_bytes, params=params, headers=headers)

    def post(self, path: str, *, document: Any, max_bytes: int) -> Received:
        """Une requête POST d'un document JSON, sa réponse lue dans ses bornes."""
        return self._send("POST", path, max_bytes=max_bytes, document=document)

    def _send(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        document: Any = None,
    ) -> Received:
        deadline = time.monotonic() + self._timeout
        try:
            with self._client.stream(
                method, self.base + path, params=params, headers=headers, json=document
            ) as response:
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding != "identity":
                    raise InvalidResponseError(
                        "elle est compressée alors qu'aucune compression n'a été demandée"
                    )
                declared = response.headers.get("content-length")
                if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
                    raise InvalidResponseError(_too_large(max_bytes))
                body = bytearray()
                # Sans compression, les octets lus sont les octets reçus.
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise InvalidResponseError(_too_large(max_bytes))
                    if time.monotonic() > deadline:
                        raise UnreachableError
                media = response.headers.get("content-type", "").partition(";")[0]
                return Received(response.status_code, media.strip().lower(), bytes(body))
        except httpx.ProtocolError:
            raise InvalidResponseError("elle n'est pas du HTTP valide") from None
        except (httpx.TransportError, httpx.InvalidURL):
            raise UnreachableError from None


def _too_large(max_bytes: int) -> str:
    return f"elle est trop grande (plus de {max_bytes // 1024} Kio)"
