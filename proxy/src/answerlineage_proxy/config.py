"""Réglages du proxy MCP."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

import httpx

from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.guard import LOOPBACK_HOSTS, RequestGuard
from answerlineage_proxy.names import INSTANCE_MAX, name_fault, normalize_name
from answerlineage_proxy.rules import ReadRule, ReadRulesError, load_read_rules, rules_for
from answerlineage_proxy.sql import DIALECTS, Dialect

ENV_UPSTREAM = "ANSWERLINEAGE_PROXY_UPSTREAM"
ENV_SERVER_NAME = "ANSWERLINEAGE_PROXY_SERVER_NAME"
ENV_HOST = "ANSWERLINEAGE_PROXY_HOST"
ENV_PORT = "ANSWERLINEAGE_PROXY_PORT"
ENV_ALLOWED_HOSTS = "ANSWERLINEAGE_PROXY_ALLOWED_HOSTS"
ENV_ALLOWED_ORIGINS = "ANSWERLINEAGE_PROXY_ALLOWED_ORIGINS"
ENV_SQL_BUDGET_MS = "ANSWERLINEAGE_PROXY_SQL_BUDGET_MS"
ENV_SQL_QUEUE_SIZE = "ANSWERLINEAGE_PROXY_SQL_QUEUE_SIZE"
ENV_SQL_CACHE_SIZE = "ANSWERLINEAGE_PROXY_SQL_CACHE_SIZE"
# Clé d'empreinte : jamais sur la ligne de commande, que la liste des processus montre.
ENV_FINGERPRINT_KEY = "ANSWERLINEAGE_PROXY_FINGERPRINT_KEY"
ENV_FINGERPRINT_KEY_FILE = "ANSWERLINEAGE_PROXY_FINGERPRINT_KEY_FILE"
# Fichier des règles de lecture des outils non SQL.
ENV_READ_RULES_FILE = "ANSWERLINEAGE_PROXY_READ_RULES_FILE"
# Identifiant d'instance des relevés de couverture.
ENV_INSTANCE_ID = "ANSWERLINEAGE_PROXY_INSTANCE_ID"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8717
DEFAULT_MAX_INSPECTED_BYTES = 1024 * 1024
# Analyse du SQL : temps de calcul accordé à une requête, analyses en attente,
# résultats gardés en cache.
DEFAULT_SQL_BUDGET_MS = 50
DEFAULT_SQL_QUEUE_SIZE = 32
DEFAULT_SQL_CACHE_SIZE = 1024
# Bornes des réglages d'analyse : (minimum, maximum).
_SQL_LIMITS = {
    "sql_budget_ms": (1, 60_000),
    "sql_queue_size": (1, 10_000),
    "sql_cache_size": (0, 1_000_000),
}

_SERVER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_ARGUMENT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_NAMESPACE = re.compile(r"[A-Za-z0-9_.:/@-]{1,255}")
# Taille lue d'un fichier de clé : une clé hexadécimale y tient largement.
_KEY_FILE_BYTES = 4096


class ProxyConfigurationError(ValueError):
    """Un réglage du proxy est absent ou invalide."""


class CaptureSettings(Protocol):
    """Réglages de la capture, communs aux transports HTTP et stdio."""

    @property
    def server_name(self) -> str:
        """Nom logique du serveur : il nomme les jobs, sous ``mcp://<nom>``."""

    @property
    def max_inspected_bytes(self) -> int:
        """Taille de message au-delà de laquelle seul son début est lu."""

    @property
    def sql_tools(self) -> Mapping[str, SqlTool]:
        """Outils dont la requête SQL est analysée, par nom d'outil."""

    @property
    def sql_budget_ms(self) -> int:
        """Budget de calcul de l'analyse d'une requête SQL."""

    @property
    def sql_queue_size(self) -> int:
        """Analyses du SQL en attente, au plus."""

    @property
    def sql_cache_size(self) -> int:
        """Résultats d'analyse gardés en cache."""

    @property
    def fingerprint_key(self) -> FingerprintKey | None:
        """Clé d'empreinte du locataire ; sans clé, aucune empreinte."""

    @property
    def read_rules(self) -> Mapping[str, ReadRule]:
        """Règles de lecture qui valent pour ce serveur, par outil."""

    @property
    def instance_id(self) -> str | None:
        """Identifiant d'instance des relevés de couverture ; sans lui, tiré au démarrage."""


def _check_server_name(name: str, hint: str) -> None:
    if not _SERVER_NAME.fullmatch(name):
        raise ProxyConfigurationError(
            "Le nom du serveur accepte 1 à 128 lettres, chiffres, points, tirets et "
            f"soulignés ; {hint}"
        )


def _check_capture(settings: CaptureSettings) -> None:
    """Contrôles communs aux deux transports : taille lue, outils, analyse du SQL."""
    if settings.max_inspected_bytes < 0:
        raise ProxyConfigurationError("La taille maximale lue ne peut pas être négative.")
    if not all(_TOOL_NAME.fullmatch(tool) for tool in [*settings.sql_tools, *settings.read_rules]):
        raise ProxyConfigurationError("Un nom d'outil SQL est invalide.")
    for name, (minimum, maximum) in _SQL_LIMITS.items():
        if not minimum <= getattr(settings, name) <= maximum:
            raise ProxyConfigurationError(f"{name} doit être compris entre {minimum} et {maximum}.")
    if settings.instance_id is not None:
        fault = name_fault(settings.instance_id, max_length=INSTANCE_MAX)
        if fault is not None:
            # Le motif est un texte fixe : il ne cite jamais la valeur reçue.
            raise ProxyConfigurationError(
                f"L'identifiant d'instance est invalide : {fault.message} ({fault.code})."
            )


def instance(value: str | None) -> str | None:
    """L'identifiant d'instance sous sa forme normalisée NFC ; ``None`` sans réglage."""
    return None if value is None else normalize_name(value)


def listed(value: str | None) -> tuple[str, ...]:
    """Lit une liste séparée par des virgules, telle qu'une variable d'environnement la porte."""
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def fingerprint_key(
    environment: Mapping[str, str], key_file: str | None = None
) -> FingerprintKey | None:
    """Lit la clé d'empreinte : dans un fichier (``key_file``, sinon la variable
    ``ANSWERLINEAGE_PROXY_FINGERPRINT_KEY_FILE``) ou dans la variable
    ``ANSWERLINEAGE_PROXY_FINGERPRINT_KEY``, en hexadécimal. ``None`` sans clé.

    Aucun message d'erreur ne cite la clé, ni le chemin du fichier, qui pourrait
    être une clé passée par erreur.
    """
    path = key_file or environment.get(ENV_FINGERPRINT_KEY_FILE) or None
    text = environment.get(ENV_FINGERPRINT_KEY) or None
    if path is not None and text is not None:
        raise ProxyConfigurationError(
            f"La clé d'empreinte vient d'un fichier ou de {ENV_FINGERPRINT_KEY}, pas des deux."
        )
    if path is not None:
        try:
            with Path(path).open("rb") as source:
                content = source.read(_KEY_FILE_BYTES + 1)
        except OSError:
            raise ProxyConfigurationError(
                "Le fichier de la clé d'empreinte est illisible."
            ) from None
        if len(content) > _KEY_FILE_BYTES:
            raise ProxyConfigurationError("Le fichier de la clé d'empreinte est trop grand.")
        text = content.decode("ascii", errors="replace")
    if text is None:
        return None
    try:
        return FingerprintKey.from_hex(text)
    except ValueError as error:
        # Le message de FingerprintKey ne cite jamais la valeur.
        raise ProxyConfigurationError(str(error)) from None


def read_rules(
    environment: Mapping[str, str], rules_file: str | None = None
) -> tuple[ReadRule, ...]:
    """Lit les règles de lecture : dans ``rules_file``, sinon dans le fichier que nomme
    ``ANSWERLINEAGE_PROXY_READ_RULES_FILE``. Aucune règle sans fichier.

    Une règle invalide empêche le démarrage : le message nomme la règle, le champ et
    sa position, jamais une valeur du fichier.
    """
    path = rules_file or environment.get(ENV_READ_RULES_FILE) or None
    if path is None:
        return ()
    try:
        return load_read_rules(path)
    except ReadRulesError as error:
        raise ProxyConfigurationError(f"Règles de lecture invalides : {error}.") from None


def integer(environment: Mapping[str, str], name: str, default: int) -> int:
    """Lit un entier dans une variable d'environnement, ``default`` si elle est absente."""
    try:
        return int(environment.get(name, default))
    except ValueError as error:
        raise ProxyConfigurationError(f"{name} doit être un entier.") from error


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
    # Noms d'hôte acceptés dans l'en-tête Host, en plus de la boucle locale et
    # du serveur MCP. « * » accepte tout hôte et toute origine.
    allowed_hosts: tuple[str, ...] = ()
    # Origines acceptées dans l'en-tête Origin, en plus de celles des hôtes acceptés.
    allowed_origins: tuple[str, ...] = ()
    # Analyse du SQL, hors du chemin de l'appel : budget de calcul par requête, en
    # millisecondes, nombre d'analyses en attente et nombre de résultats en cache.
    sql_budget_ms: int = DEFAULT_SQL_BUDGET_MS
    sql_queue_size: int = DEFAULT_SQL_QUEUE_SIZE
    sql_cache_size: int = DEFAULT_SQL_CACHE_SIZE
    # Clé d'empreinte du locataire. Sans clé, aucune empreinte n'est émise.
    fingerprint_key: FingerprintKey | None = field(default=None, repr=False)
    # Règles de lecture qui valent pour ce serveur, par outil. Un outil SQL garde
    # l'analyse de sa requête.
    read_rules: Mapping[str, ReadRule] = field(default_factory=dict)
    # Identifiant d'instance des relevés de couverture, sous sa forme NFC. Sans lui, le
    # proxy en tire un au démarrage.
    instance_id: str | None = None

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
        _check_server_name(
            self.server_name,
            f"sans --server-name ni {ENV_SERVER_NAME}, il est tiré de l'hôte du serveur MCP.",
        )
        if not 0 <= self.port <= 65535:
            raise ProxyConfigurationError("Le port doit être compris entre 0 et 65535.")
        _check_capture(self)
        try:
            RequestGuard.create([self.upstream_host])
        except ValueError as error:
            raise ProxyConfigurationError(
                "L'hôte du serveur MCP doit être un nom d'hôte ou une adresse IP, "
                "tel qu'un client l'écrit dans l'en-tête Host."
            ) from error
        try:
            RequestGuard.create(self.allowed_hosts, self.allowed_origins)
        except ValueError as error:
            raise ProxyConfigurationError(str(error)) from error

    @property
    def upstream_host(self) -> str:
        """L'hôte du serveur MCP tel qu'un client l'écrit dans l'en-tête Host.

        Une adresse IPv6 s'y écrit entre crochets, un nom accentué en punycode.
        """
        host = self.upstream.raw_host.decode("ascii", errors="replace")
        return f"[{host}]" if ":" in host else host

    def guard(self) -> RequestGuard:
        """Contrôle des requêtes : la boucle locale, le serveur MCP et les hôtes ajoutés."""
        return RequestGuard.create(
            [*LOOPBACK_HOSTS, self.upstream_host, *self.allowed_hosts], self.allowed_origins
        )

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
        allowed_hosts: Sequence[str] = (),
        allowed_origins: Sequence[str] = (),
        sql_budget_ms: int = DEFAULT_SQL_BUDGET_MS,
        sql_queue_size: int = DEFAULT_SQL_QUEUE_SIZE,
        sql_cache_size: int = DEFAULT_SQL_CACHE_SIZE,
        fingerprint_key: FingerprintKey | None = None,
        read_rules: Sequence[ReadRule] = (),
        instance_id: str | None = None,
    ) -> ProxySettings:
        try:
            url = httpx.URL(upstream)
        except (httpx.InvalidURL, TypeError) as error:
            raise ProxyConfigurationError("L'adresse du serveur MCP est illisible.") from error
        name = server_name if server_name is not None else url.host
        return cls(
            upstream=url,
            server_name=name,
            host=host,
            port=port,
            preserve_host=preserve_host,
            max_inspected_bytes=max_inspected_bytes,
            sql_tools=dict(sql_tools or {}),
            allowed_hosts=tuple(allowed_hosts),
            allowed_origins=tuple(allowed_origins),
            sql_budget_ms=sql_budget_ms,
            sql_queue_size=sql_queue_size,
            sql_cache_size=sql_cache_size,
            fingerprint_key=fingerprint_key,
            read_rules=rules_for(tuple(read_rules), name),
            instance_id=instance(instance_id),
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
            allowed_hosts=listed(environment.get(ENV_ALLOWED_HOSTS)),
            allowed_origins=listed(environment.get(ENV_ALLOWED_ORIGINS)),
            sql_budget_ms=integer(environment, ENV_SQL_BUDGET_MS, DEFAULT_SQL_BUDGET_MS),
            sql_queue_size=integer(environment, ENV_SQL_QUEUE_SIZE, DEFAULT_SQL_QUEUE_SIZE),
            sql_cache_size=integer(environment, ENV_SQL_CACHE_SIZE, DEFAULT_SQL_CACHE_SIZE),
            fingerprint_key=fingerprint_key(environment),
            read_rules=read_rules(environment),
            instance_id=environment.get(ENV_INSTANCE_ID) or None,
        )


@dataclass(frozen=True)
class StdioSettings:
    """Réglages d'un proxy qui lance un serveur MCP en stdio et relaie ses flux."""

    # Commande du serveur MCP : le programme, puis ses arguments, lancés sans shell.
    # Elle n'apparaît dans aucun journal, aucun événement ni aucun message d'erreur.
    command: tuple[str, ...] = field(repr=False)
    server_name: str
    max_inspected_bytes: int = DEFAULT_MAX_INSPECTED_BYTES
    sql_tools: Mapping[str, SqlTool] = field(default_factory=dict)
    sql_budget_ms: int = DEFAULT_SQL_BUDGET_MS
    sql_queue_size: int = DEFAULT_SQL_QUEUE_SIZE
    sql_cache_size: int = DEFAULT_SQL_CACHE_SIZE
    fingerprint_key: FingerprintKey | None = field(default=None, repr=False)
    read_rules: Mapping[str, ReadRule] = field(default_factory=dict)
    instance_id: str | None = None

    def __post_init__(self) -> None:
        if not self.command or not self.command[0]:
            raise ProxyConfigurationError("Indiquer la commande du serveur MCP après « -- ».")
        _check_server_name(
            self.server_name,
            f"sans --server-name ni {ENV_SERVER_NAME}, il est tiré du nom du programme lancé.",
        )
        _check_capture(self)

    @classmethod
    def create(
        cls,
        command: Sequence[str],
        server_name: str | None = None,
        *,
        max_inspected_bytes: int = DEFAULT_MAX_INSPECTED_BYTES,
        sql_tools: Mapping[str, SqlTool] | None = None,
        sql_budget_ms: int = DEFAULT_SQL_BUDGET_MS,
        sql_queue_size: int = DEFAULT_SQL_QUEUE_SIZE,
        sql_cache_size: int = DEFAULT_SQL_CACHE_SIZE,
        fingerprint_key: FingerprintKey | None = None,
        read_rules: Sequence[ReadRule] = (),
        instance_id: str | None = None,
    ) -> StdioSettings:
        """Sans nom de serveur, celui du programme lancé, sans son chemin."""
        name = server_name
        if name is None:
            name = PurePosixPath(command[0]).name if command else ""
        return cls(
            command=tuple(command),
            server_name=name,
            max_inspected_bytes=max_inspected_bytes,
            sql_tools=dict(sql_tools or {}),
            sql_budget_ms=sql_budget_ms,
            sql_queue_size=sql_queue_size,
            sql_cache_size=sql_cache_size,
            fingerprint_key=fingerprint_key,
            read_rules=rules_for(tuple(read_rules), name),
            instance_id=instance(instance_id),
        )
