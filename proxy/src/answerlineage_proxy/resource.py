"""Mode passerelle et jetons liés à l'URL du serveur MCP (RFC 9728, RFC 8707).

Le proxy se place devant un serveur MCP de deux façons :

- **transparent**, par défaut : les clients joignent le proxy à l'URL du serveur MCP,
  celle qu'ils connaissaient, et le proxy relaie l'en-tête ``Host`` qu'ils envoient.
  Pour le serveur, rien ne change : ni son nom, ni le chemin, ni le jeton ;
- **passerelle** (``--no-preserve-host``) : les clients joignent le proxy à sa propre
  URL, et le proxy remplace ``Host`` par celui du serveur MCP.

Un serveur MCP protégé par OAuth publie ses métadonnées de ressource protégée
(RFC 9728). Un client MCP demande alors un jeton pour l'URL qu'il joint (indicateur
de ressource, RFC 8707), et le serveur n'accepte qu'un jeton dont l'audience est la
sienne. Derrière une passerelle, le client joint l'URL du proxy : les métadonnées
relayées désignent une autre ressource que celle qu'il joint, qu'il doit écarter
(RFC 9728, section 3.3), et un jeton obtenu pour l'URL du proxy est refusé par le
serveur. Seul le proxy pourrait l'échanger contre un autre, en terminant OAuth : il
ne le fait jamais. Le mode passerelle est donc refusé pour un serveur dont les
jetons sont liés à son URL, que l'exploitant le déclare (``--audience-bound-tokens``)
ou que le proxy le lise dans les métadonnées du serveur, au démarrage, avant de
servir.

La lecture des métadonnées est une entrée hostile : deux adresses bien connues au
plus, celles de la RFC 9728 ; aucune redirection suivie, rien de l'environnement,
aucun en-tête d'autorisation ; une réponse compressée n'est pas déployée, un corps
est lu sur 64 Kio au plus, en JSON strict ; 10 s pour l'ensemble. Une réponse qui
s'en écarte ne dit rien : le proxy le journalise par un motif d'une liste fermée,
sans rien recopier de la réponse, et démarre, puisque la capture reste ouverte en
cas de panne. L'exploitant qui sait ses jetons liés le déclare.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

from answerlineage_proxy.config import (
    GATEWAY_REFUSED_BY_METADATA,
    ProxyConfigurationError,
    ProxySettings,
)

logger = logging.getLogger("answerlineage_proxy")

# Préfixe bien connu des métadonnées de ressource protégée (RFC 9728, section 3).
WELL_KNOWN: Final = b"/.well-known/oauth-protected-resource"
# Bornes de la lecture : la taille d'un document, la longueur de l'identifiant de
# ressource, et les délais (connexion, chaque lecture, puis la lecture entière).
MAX_METADATA_BYTES: Final = 64 * 1024
MAX_RESOURCE_LENGTH: Final = 2048
# Profondeur d'imbrication admise : un document de métadonnées n'en a que deux. Bornée
# ici, elle ne dépend pas de la pile que l'interpréteur accorde à l'analyseur JSON.
MAX_NESTING: Final = 16
CONNECT_TIMEOUT: Final = 5.0
READ_TIMEOUT: Final = 5.0
READ_DEADLINE: Final = 10.0
_NOT_PUBLISHED: Final = frozenset({404, 410})

Verdict = Literal["bound", "absent", "unknown"]


@dataclass(frozen=True)
class MetadataReading:
    """Ce que disent les métadonnées du serveur MCP : ses jetons sont liés à son URL
    (``bound``), il n'en publie pas (``absent``), ou rien de sûr (``unknown``)."""

    verdict: Verdict
    # Motif d'une liste fermée, jamais un extrait de la réponse.
    reason: str


class _RefusedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def metadata_urls(upstream: httpx.URL) -> tuple[httpx.URL, ...]:
    """Les adresses des métadonnées du serveur MCP (RFC 9728, section 3.1) : le préfixe
    bien connu suivi du chemin du serveur, puis le préfixe seul, à la racine."""
    path = upstream.raw_path.rstrip(b"/")
    root = upstream.copy_with(raw_path=WELL_KNOWN)
    if not path:
        return (root,)
    return (upstream.copy_with(raw_path=WELL_KNOWN + path), root)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError
        found[key] = value
    return found


def _refuse_constant(_: str) -> Any:
    raise ValueError


def _nesting_within(body: bytes, limit: int) -> bool:
    """Vrai quand tableaux et objets s'imbriquent sur ``limit`` niveaux au plus, chaînes exclues."""
    depth = 0
    in_string = escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # \
                escaped = True
            elif byte == 0x22:  # "
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > limit:
                return False
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1
    return True


def check_document(body: bytes) -> None:
    """Contrôle un document de métadonnées : JSON strict, un objet qui déclare sa ressource
    par une URL absolue sans fragment (RFC 9728, section 2). ``_RefusedError`` sinon."""
    if not _nesting_within(body, MAX_NESTING):
        raise _RefusedError("not_json")
    try:
        document = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_refuse_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _RefusedError("not_json") from None
    if not isinstance(document, dict):
        raise _RefusedError("invalid_document")
    resource = document.get("resource")
    if not isinstance(resource, str) or not 0 < len(resource) <= MAX_RESOURCE_LENGTH:
        raise _RefusedError("invalid_document")
    try:
        url = httpx.URL(resource)
    except (httpx.InvalidURL, TypeError):
        raise _RefusedError("invalid_document") from None
    if url.scheme not in ("http", "https") or not url.host or url.fragment:
        raise _RefusedError("invalid_document")


def _client() -> httpx.AsyncClient:
    # Certificat vérifié, aucune redirection suivie, rien de l'environnement : seule
    # l'adresse du serveur MCP est jointe, et rien n'est annoncé de plus.
    client = httpx.AsyncClient(
        verify=True,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
    )
    client.headers.clear()
    return client


async def _read_one(client: httpx.AsyncClient, url: httpx.URL) -> MetadataReading | None:
    """Lit une adresse ; ``None`` quand le serveur n'y publie rien."""
    headers = {"accept": "application/json", "accept-encoding": "identity"}
    async with client.stream("GET", url, headers=headers) as reply:
        status = reply.status_code
        if status in _NOT_PUBLISHED:
            return None
        if 300 <= status < 400:
            return MetadataReading("unknown", "redirect")
        if status != 200:
            return MetadataReading("unknown", "status")
        if reply.headers.get("content-encoding", "identity").strip().lower() != "identity":
            # Une réponse compressée n'est jamais déployée.
            return MetadataReading("unknown", "encoded")
        media = reply.headers.get("content-type", "").split(";")[0].strip().lower()
        if media != "application/json":
            return MetadataReading("unknown", "not_json")
        body = bytearray()
        async for chunk in reply.aiter_raw():
            body += chunk
            if len(body) > MAX_METADATA_BYTES:
                return MetadataReading("unknown", "too_large")
    try:
        check_document(bytes(body))
    except _RefusedError as refused:
        return MetadataReading("unknown", refused.code)
    return MetadataReading("bound", "resource_declared")


async def read_metadata(
    upstream: httpx.URL,
    *,
    client: httpx.AsyncClient | None = None,
    deadline: float = READ_DEADLINE,
) -> MetadataReading:
    """Lit les métadonnées de ressource protégée du serveur MCP, aux adresses de la
    RFC 9728, dans le délai ; ne lève jamais."""
    owned = client is None
    reader = client if client is not None else _client()
    try:
        async with asyncio.timeout(deadline):
            for url in metadata_urls(upstream):
                reading = await _read_one(reader, url)
                if reading is not None:
                    return reading
        return MetadataReading("absent", "not_published")
    except (TimeoutError, httpx.TimeoutException):
        return MetadataReading("unknown", "timeout")
    except httpx.HTTPError:
        return MetadataReading("unknown", "unreachable")
    finally:
        if owned:
            await reader.aclose()


async def check_gateway(
    settings: ProxySettings, *, client: httpx.AsyncClient | None = None
) -> MetadataReading | None:
    """En mode passerelle, refuse un serveur MCP dont les métadonnées déclarent la
    ressource : ses jetons sont liés à son URL. Rien n'est lu en mode transparent."""
    if settings.preserve_host:
        return None
    reading = await read_metadata(settings.upstream, client=client)
    if reading.verdict == "bound":
        raise ProxyConfigurationError(GATEWAY_REFUSED_BY_METADATA)
    if reading.verdict == "unknown":
        logger.warning(
            "Mode passerelle : métadonnées OAuth du serveur MCP illisibles (%s) ; le proxy ne "
            "peut pas dire si ses jetons sont liés à son URL. S'ils le sont, le serveur "
            "refusera les appels : le déclarer avec --audience-bound-tokens, ou placer le "
            "proxy en mode transparent.",
            reading.reason,
        )
    else:
        logger.info(
            "Mode passerelle : le serveur MCP ne publie pas de métadonnées de ressource "
            "protégée OAuth."
        )
    return reading
