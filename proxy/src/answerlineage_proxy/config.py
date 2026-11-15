"""Réglages du proxy MCP."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

ENV_UPSTREAM = "ANSWERLINEAGE_PROXY_UPSTREAM"
ENV_SERVER_NAME = "ANSWERLINEAGE_PROXY_SERVER_NAME"
ENV_HOST = "ANSWERLINEAGE_PROXY_HOST"
ENV_PORT = "ANSWERLINEAGE_PROXY_PORT"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8717
DEFAULT_MAX_INSPECTED_BYTES = 1024 * 1024

_SERVER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


class ProxyConfigurationError(ValueError):
    """Un réglage du proxy est absent ou invalide."""


@dataclass(frozen=True)
class ProxySettings:
    """Réglages d'un proxy placé devant un serveur MCP."""

    # Adresse du serveur MCP, derrière le proxy.
    upstream: httpx.URL
    # Nom logique du serveur : il nomme les jobs, sous ``mcp://<nom>``.
    server_name: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Mode transparent : le serveur reçoit l'en-tête Host envoyé par le client.
    preserve_host: bool = True
    # Au-delà, le corps est relayé sans être lu.
    max_inspected_bytes: int = DEFAULT_MAX_INSPECTED_BYTES
    connect_timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.upstream.scheme not in ("http", "https") or not self.upstream.host:
            raise ProxyConfigurationError(
                "L'adresse du serveur MCP doit être une URL http ou https complète."
            )
        if self.upstream.userinfo:
            raise ProxyConfigurationError(
                "L'adresse du serveur MCP ne doit contenir aucun identifiant."
            )
        if self.upstream.query or self.upstream.fragment:
            raise ProxyConfigurationError(
                "L'adresse du serveur MCP ne doit porter ni paramètres ni fragment."
            )
        if not _SERVER_NAME.fullmatch(self.server_name):
            raise ProxyConfigurationError(
                "Le nom du serveur accepte 1 à 128 lettres, chiffres, points, tirets et soulignés."
            )
        if not 0 <= self.port <= 65535:
            raise ProxyConfigurationError("Le port doit être compris entre 0 et 65535.")
        if self.max_inspected_bytes < 0:
            raise ProxyConfigurationError("La taille maximale lue ne peut pas être négative.")

    @classmethod
    def create(
        cls,
        upstream: str,
        server_name: str | None = None,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        preserve_host: bool = True,
        max_inspected_bytes: int = DEFAULT_MAX_INSPECTED_BYTES,
    ) -> ProxySettings:
        try:
            url = httpx.URL(upstream)
        except (httpx.InvalidURL, TypeError) as error:
            raise ProxyConfigurationError("L'adresse du serveur MCP est illisible.") from error
        return cls(
            upstream=url,
            server_name=server_name if server_name is not None else url.host,
            host=host,
            port=port,
            preserve_host=preserve_host,
            max_inspected_bytes=max_inspected_bytes,
        )

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ProxySettings:
        upstream = environment.get(ENV_UPSTREAM)
        if not upstream:
            raise ProxyConfigurationError(
                f"La variable {ENV_UPSTREAM} doit contenir l'adresse du serveur MCP."
            )
        try:
            port = int(environment.get(ENV_PORT, DEFAULT_PORT))
        except ValueError as error:
            raise ProxyConfigurationError(f"{ENV_PORT} doit être un entier.") from error
        return cls.create(
            upstream,
            environment.get(ENV_SERVER_NAME) or None,
            host=environment.get(ENV_HOST, DEFAULT_HOST),
            port=port,
        )
