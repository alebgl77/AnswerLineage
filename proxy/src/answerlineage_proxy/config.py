"""Réglages du proxy MCP."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import httpx

from answerlineage_proxy.sql import DIALECTS, Dialect

ENV_UPSTREAM = "ANSWERLINEAGE_PROXY_UPSTREAM"
ENV_SERVER_NAME = "ANSWERLINEAGE_PROXY_SERVER_NAME"
ENV_HOST = "ANSWERLINEAGE_PROXY_HOST"
ENV_PORT = "ANSWERLINEAGE_PROXY_PORT"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8717
DEFAULT_MAX_INSPECTED_BYTES = 1024 * 1024

_SERVER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_ARGUMENT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_NAMESPACE = re.compile(r"[A-Za-z0-9_.:/@-]{1,255}")


class ProxyConfigurationError(ValueError):
    """Un réglage du proxy est absent ou invalide."""


@dataclass(frozen=True)
class SqlTool:
    """Outil MCP dont un argument porte une requête SQL."""

    # Nom de l'argument de l'outil qui contient la requête.
    argument: str
    dialect: Dialect
    # Espace de noms OpenLineage des tables lues, par exemple ``bigquery``.
    namespace: str
    default_catalog: str | None = None
    default_db: str | None = None

    def __post_init__(self) -> None:
        if not _ARGUMENT_NAME.fullmatch(self.argument):
            raise ProxyConfigurationError("Le nom de l'argument SQL est invalide.")
        if self.dialect not in DIALECTS:
            raise ProxyConfigurationError(
                f"Le dialecte SQL doit être l'un de : {', '.join(DIALECTS)}."
            )
        if not _NAMESPACE.fullmatch(self.namespace):
            raise ProxyConfigurationError("L'espace de noms des tables est invalide.")

    @classmethod
    def parse(cls, value: str) -> tuple[str, SqlTool]:
        """Lit ``outil=argument:dialecte[:espace-de-noms]``."""
        tool, separator, rule = value.partition("=")
        parts = rule.split(":", 2)
        if not separator or len(parts) < 2 or not _TOOL_NAME.fullmatch(tool):
            raise ProxyConfigurationError(
                "Un outil SQL se décrit par outil=argument:dialecte[:espace-de-noms]."
            )
        argument, dialect = parts[0], parts[1]
        namespace = parts[2] if len(parts) == 3 else dialect
        return tool, cls(argument=argument, dialect=cast(Dialect, dialect), namespace=namespace)


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
    # Outils dont la requête SQL est analysée, par nom d'outil.
    sql_tools: Mapping[str, SqlTool] = field(default_factory=dict)

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
        if not all(_TOOL_NAME.fullmatch(tool) for tool in self.sql_tools):
            raise ProxyConfigurationError("Un nom d'outil SQL est invalide.")

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
        sql_tools: Mapping[str, SqlTool] | None = None,
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
            sql_tools=dict(sql_tools or {}),
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
