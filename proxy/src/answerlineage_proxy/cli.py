"""Ligne de commande du proxy MCP : ``answerlineage-proxy --upstream <URL>``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any, Final

import uvicorn

from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import (
    DEFAULT_HOST,
    DEFAULT_MAX_INSPECTED_BYTES,
    DEFAULT_PORT,
    DEFAULT_SQL_BUDGET_MS,
    DEFAULT_SQL_CACHE_SIZE,
    DEFAULT_SQL_QUEUE_SIZE,
    ENV_ALLOWED_HOSTS,
    ENV_ALLOWED_ORIGINS,
    ENV_FINGERPRINT_KEY,
    ENV_FINGERPRINT_KEY_FILE,
    ENV_HOST,
    ENV_INSTANCE_ID,
    ENV_PORT,
    ENV_READ_RULES_FILE,
    ENV_SERVER_NAME,
    ENV_SQL_BUDGET_MS,
    ENV_SQL_CACHE_SIZE,
    ENV_SQL_QUEUE_SIZE,
    ENV_UPSTREAM,
    ProxyConfigurationError,
    ProxySettings,
    SqlTool,
    fingerprint_key,
    integer,
    listed,
    read_rules,
)
from answerlineage_proxy.coverage import DEFAULT_WINDOW, MAX_WINDOW, MIN_WINDOW
from answerlineage_proxy.guard import LOOPBACK_HOSTS
from answerlineage_proxy.outbox import (
    DEFAULT_FLUSH_SECONDS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_EVENTS,
    Outbox,
    OutboxSettings,
)
from answerlineage_proxy.sink import EventSink, NullSink

logger = logging.getLogger("answerlineage_proxy")

ENV_COVERAGE_WINDOW = "ANSWERLINEAGE_PROXY_COVERAGE_WINDOW_SECONDS"
# Boîte d'envoi : l'adresse de l'API d'AnswerLineage, ses bornes, son délai de vidage.
ENV_API_URL = "ANSWERLINEAGE_PROXY_API_URL"
ENV_OUTBOX_MAX_EVENTS = "ANSWERLINEAGE_PROXY_OUTBOX_MAX_EVENTS"
ENV_OUTBOX_MAX_BYTES = "ANSWERLINEAGE_PROXY_OUTBOX_MAX_BYTES"
ENV_OUTBOX_FLUSH_SECONDS = "ANSWERLINEAGE_PROXY_OUTBOX_FLUSH_SECONDS"

# Réglages du serveur HTTP qui sert le proxy. Le journal d'accès contiendrait
# chemins et paramètres de requête ; les en-têtes Server et Date du serveur MCP
# sont relayés tels quels, le serveur HTTP n'en ajoute pas.
SERVER_OPTIONS: Final[Mapping[str, Any]] = {
    "access_log": False,
    "server_header": False,
    "date_header": False,
    "log_level": "warning",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="answerlineage-proxy",
        description="Proxy transparent placé devant un serveur MCP en HTTP.",
        epilog=(
            "Mode stdio : answerlineage-proxy [réglages] -- commande [arguments…] lance le "
            "serveur MCP et relaie ses flux."
        ),
    )
    parser.add_argument("--upstream", help=f"adresse du serveur MCP (ou {ENV_UPSTREAM})")
    parser.add_argument(
        "--server-name", help=f"nom logique du serveur dans le lignage (ou {ENV_SERVER_NAME})"
    )
    parser.add_argument(
        "--host", help=f"adresse d'écoute, {DEFAULT_HOST} par défaut (ou {ENV_HOST})"
    )
    parser.add_argument(
        "--port", type=int, help=f"port d'écoute, {DEFAULT_PORT} par défaut (ou {ENV_PORT})"
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HÔTE",
        help=(
            "nom d'hôte accepté dans l'en-tête Host, en plus de la boucle locale et du "
            f"serveur MCP ; « * » les accepte tous (ou {ENV_ALLOWED_HOSTS})"
        ),
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGINE",
        help=(
            "origine acceptée dans l'en-tête Origin, par exemple https://console.exemple.fr "
            f"(ou {ENV_ALLOWED_ORIGINS})"
        ),
    )
    parser.add_argument(
        "--no-preserve-host",
        action="store_true",
        help="remplace l'en-tête Host par celui du serveur MCP (mode passerelle)",
    )
    parser.add_argument(
        "--max-inspected-bytes",
        type=int,
        default=DEFAULT_MAX_INSPECTED_BYTES,
        help="taille de corps au-delà de laquelle une requête est relayée sans être lue",
    )
    parser.add_argument(
        "--sql-tool",
        action="append",
        default=[],
        metavar="OUTIL=ARGUMENT:DIALECTE[:ESPACE]",
        help=(
            "outil dont l'argument porte une requête SQL à analyser, par exemple "
            "execute_sql=query:bigquery (dialectes : bigquery, snowflake, postgres)"
        ),
    )
    parser.add_argument(
        "--sql-run-comment",
        action="append",
        default=[],
        metavar="OUTIL",
        help=(
            "outil déclaré par --sql-tool dont la requête relayée commence par un commentaire "
            "qui porte l'identifiant de run ; désactivé par défaut, le proxy sort alors de la "
            "stricte transparence pour cet outil"
        ),
    )
    parser.add_argument(
        "--sql-budget-ms",
        type=int,
        help=(
            "temps de calcul accordé à l'analyse d'une requête SQL, "
            f"{DEFAULT_SQL_BUDGET_MS} ms par défaut (ou {ENV_SQL_BUDGET_MS})"
        ),
    )
    parser.add_argument(
        "--sql-queue-size",
        type=int,
        help=(
            "analyses du SQL en attente au plus, au-delà la lecture est non résolue, "
            f"{DEFAULT_SQL_QUEUE_SIZE} par défaut (ou {ENV_SQL_QUEUE_SIZE})"
        ),
    )
    parser.add_argument(
        "--sql-cache-size",
        type=int,
        help=(
            "résultats d'analyse du SQL gardés en cache, 0 pour aucun, "
            f"{DEFAULT_SQL_CACHE_SIZE} par défaut (ou {ENV_SQL_CACHE_SIZE})"
        ),
    )
    parser.add_argument(
        "--fingerprint-key-file",
        metavar="FICHIER",
        help=(
            "fichier qui contient la clé d'empreinte du locataire, en hexadécimal "
            f"(ou {ENV_FINGERPRINT_KEY_FILE}, ou la clé elle-même dans {ENV_FINGERPRINT_KEY}) ; "
            "sans clé, aucune empreinte n'est émise"
        ),
    )
    parser.add_argument(
        "--read-rules-file",
        metavar="FICHIER",
        help=(
            "fichier YAML des règles de lecture des outils non SQL : jeux de données lus, "
            f"colonnes, argument de la clé lue (ou {ENV_READ_RULES_FILE})"
        ),
    )
    parser.add_argument(
        "--coverage-window-seconds",
        type=int,
        help=(
            "durée d'une fenêtre des relevés de couverture, de 1 à 86400 secondes, "
            f"{int(DEFAULT_WINDOW.total_seconds())} par défaut (ou {ENV_COVERAGE_WINDOW})"
        ),
    )
    parser.add_argument(
        "--instance-id",
        metavar="INSTANCE",
        help=(
            "identifiant d'instance des relevés de couverture, qui distingue les proxys placés "
            f"devant un même serveur (ou {ENV_INSTANCE_ID}) ; sans lui, tiré au démarrage"
        ),
    )
    parser.add_argument(
        "--api-url",
        metavar="URL",
        help=(
            "adresse de l'API d'AnswerLineage, où la boîte d'envoi remet événements et relevés "
            f"de couverture (ou {ENV_API_URL}) ; sans elle, ils sont comptés sans être envoyés"
        ),
    )
    parser.add_argument(
        "--outbox-max-events",
        type=int,
        help=(
            "événements gardés au plus dans la boîte d'envoi, au-delà le plus ancien est perdu, "
            f"{DEFAULT_MAX_EVENTS} par défaut (ou {ENV_OUTBOX_MAX_EVENTS})"
        ),
    )
    parser.add_argument(
        "--outbox-max-bytes",
        type=int,
        help=(
            "octets gardés au plus dans la boîte d'envoi, "
            f"{DEFAULT_MAX_BYTES} par défaut (ou {ENV_OUTBOX_MAX_BYTES})"
        ),
    )
    parser.add_argument(
        "--outbox-flush-seconds",
        type=float,
        help=(
            "délai de vidage de la boîte d'envoi à l'arrêt, au-delà le reste est perdu, "
            f"{DEFAULT_FLUSH_SECONDS:g} s par défaut (ou {ENV_OUTBOX_FLUSH_SECONDS})"
        ),
    )
    return parser


def coverage_window_from_args(
    argv: Sequence[str] | None, environment: Mapping[str, str]
) -> timedelta:
    """Durée d'une fenêtre des relevés de couverture : de l'option, sinon de l'environnement."""
    args = build_parser().parse_args(argv)
    seconds = args.coverage_window_seconds
    if seconds is None:
        seconds = integer(environment, ENV_COVERAGE_WINDOW, int(DEFAULT_WINDOW.total_seconds()))
    window = timedelta(seconds=seconds)
    if not MIN_WINDOW <= window <= MAX_WINDOW:
        raise ProxyConfigurationError(
            "Une fenêtre des relevés de couverture dure de 1 à 86400 secondes."
        )
    return window


def _seconds(environment: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(environment.get(name, default))
    except ValueError as error:
        raise ProxyConfigurationError(f"{name} doit être un nombre de secondes.") from error


def outbox_settings_from_args(
    argv: Sequence[str] | None, environment: Mapping[str, str]
) -> OutboxSettings | None:
    """Réglages de la boîte d'envoi : des options, sinon de l'environnement. Aucune boîte
    sans adresse d'API."""
    args = build_parser().parse_args(argv)
    api_url = args.api_url or environment.get(ENV_API_URL)
    if not api_url:
        return None
    return OutboxSettings.create(
        api_url,
        max_events=(
            args.outbox_max_events
            if args.outbox_max_events is not None
            else integer(environment, ENV_OUTBOX_MAX_EVENTS, DEFAULT_MAX_EVENTS)
        ),
        max_bytes=(
            args.outbox_max_bytes
            if args.outbox_max_bytes is not None
            else integer(environment, ENV_OUTBOX_MAX_BYTES, DEFAULT_MAX_BYTES)
        ),
        flush_seconds=(
            args.outbox_flush_seconds
            if args.outbox_flush_seconds is not None
            else _seconds(environment, ENV_OUTBOX_FLUSH_SECONDS, DEFAULT_FLUSH_SECONDS)
        ),
    )


def sql_tools_from_args(args: argparse.Namespace) -> dict[str, SqlTool]:
    """Les outils SQL déclarés par ``--sql-tool``, et ceux dont la requête relayée porte
    l'identifiant de run en commentaire (``--sql-run-comment``)."""
    tools = dict(SqlTool.parse(value) for value in args.sql_tool)
    for tool in args.sql_run_comment:
        if tool not in tools:
            # Le nom n'est pas cité : un message d'erreur ne recopie aucune valeur reçue.
            raise ProxyConfigurationError(
                "--sql-run-comment nomme un outil que --sql-tool ne déclare pas."
            )
        tools[tool] = replace(tools[tool], run_comment=True)
    return tools


def log_run_comments(tools: Mapping[str, SqlTool]) -> None:
    """Dit au démarrage pour quels outils le proxy modifie la requête relayée."""
    for tool in sorted(name for name, rule in tools.items() if rule.run_comment):
        # Un nom d'outil validé, jamais une valeur reçue.
        logger.warning(
            "Les requêtes de l'outil « %s » commencent, une fois relayées, par un commentaire "
            "qui porte l'identifiant de run : le proxy n'est plus strictement transparent "
            "pour cet outil.",
            tool,
        )


def settings_from_args(argv: Sequence[str] | None, environment: Mapping[str, str]) -> ProxySettings:
    args = build_parser().parse_args(argv)
    upstream = args.upstream or environment.get(ENV_UPSTREAM)
    if not upstream:
        raise ProxyConfigurationError(
            f"Indiquer l'adresse du serveur MCP avec --upstream ou {ENV_UPSTREAM}."
        )
    port = args.port
    if port is None:
        try:
            port = int(environment.get(ENV_PORT, DEFAULT_PORT))
        except ValueError as error:
            raise ProxyConfigurationError(f"{ENV_PORT} doit être un entier.") from error
    return ProxySettings.create(
        upstream,
        args.server_name or environment.get(ENV_SERVER_NAME) or None,
        host=args.host or environment.get(ENV_HOST, DEFAULT_HOST),
        port=port,
        preserve_host=not args.no_preserve_host,
        max_inspected_bytes=args.max_inspected_bytes,
        sql_tools=sql_tools_from_args(args),
        allowed_hosts=args.allow_host or listed(environment.get(ENV_ALLOWED_HOSTS)),
        allowed_origins=args.allow_origin or listed(environment.get(ENV_ALLOWED_ORIGINS)),
        sql_budget_ms=(
            args.sql_budget_ms
            if args.sql_budget_ms is not None
            else integer(environment, ENV_SQL_BUDGET_MS, DEFAULT_SQL_BUDGET_MS)
        ),
        sql_queue_size=(
            args.sql_queue_size
            if args.sql_queue_size is not None
            else integer(environment, ENV_SQL_QUEUE_SIZE, DEFAULT_SQL_QUEUE_SIZE)
        ),
        sql_cache_size=(
            args.sql_cache_size
            if args.sql_cache_size is not None
            else integer(environment, ENV_SQL_CACHE_SIZE, DEFAULT_SQL_CACHE_SIZE)
        ),
        fingerprint_key=fingerprint_key(environment, args.fingerprint_key_file),
        read_rules=read_rules(environment, args.read_rules_file),
        instance_id=args.instance_id or environment.get(ENV_INSTANCE_ID) or None,
    )


def event_sink(outbox: OutboxSettings | None) -> EventSink:
    """La destination des événements, la même pour les deux transports : la boîte d'envoi
    vers l'API quand son adresse est réglée, sinon une destination qui compte sans envoyer."""
    if outbox is None:
        logger.warning(
            "Aucune adresse d'API (--api-url ou %s) : les appels d'outils et les relevés de "
            "couverture sont comptés sans être envoyés.",
            ENV_API_URL,
        )
        return NullSink()
    logger.info(
        "Événements et relevés de couverture remis à l'API par la boîte d'envoi : "
        "%d événements et %d octets au plus, vidage en %g s à l'arrêt.",
        outbox.max_events,
        outbox.max_bytes,
        outbox.flush_seconds,
    )
    return Outbox(outbox)


def log_instance(instance: str) -> None:
    """Journalise l'instance du proxy : un identifiant réglé ou tiré, qui ne dit rien du trafic.

    Le nom du serveur n'y figure pas : en stdio, il peut venir de la commande lancée."""
    logger.info("Relevés de couverture publiés sous l'instance « %s ».", instance)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--" in arguments:
        # Mode stdio : la commande du serveur MCP suit « -- ».
        from answerlineage_proxy import stdio

        return stdio.main(arguments, os.environ)
    try:
        settings = settings_from_args(argv, os.environ)
        coverage_window = coverage_window_from_args(argv, os.environ)
        outbox = outbox_settings_from_args(argv, os.environ)
    except ProxyConfigurationError as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2

    logger.info(
        "Proxy MCP du serveur « %s » : écoute sur %s:%d.",
        settings.server_name,
        settings.host,
        settings.port,
    )
    if settings.host not in LOOPBACK_HOSTS and not settings.allowed_hosts:
        logger.warning(
            "Le proxy écoute hors de la boucle locale : seuls la boucle locale et le serveur MCP "
            "sont acceptés dans l'en-tête Host. Ajouter les noms du proxy avec --allow-host."
        )
    if settings.fingerprint_key is None:
        logger.warning(
            "Aucune clé d'empreinte (%s ou %s) : les événements ne portent ni empreinte "
            "des arguments ni empreinte du résultat.",
            ENV_FINGERPRINT_KEY,
            ENV_FINGERPRINT_KEY_FILE,
        )
    for tool in sorted(set(settings.read_rules) & set(settings.sql_tools)):
        # Un nom d'outil validé, jamais une valeur reçue.
        logger.warning(
            "La règle de lecture de l'outil « %s » est ignorée : c'est un outil SQL, "
            "sa requête est analysée.",
            tool,
        )
    if settings.read_rules:
        logger.info("Règles de lecture : %d outils décrits.", len(settings.read_rules))
    log_run_comments(settings.sql_tools)
    proxy = McpProxy(settings, event_sink(outbox), coverage_window=coverage_window)
    log_instance(proxy.instance)
    uvicorn.run(
        proxy,
        host=settings.host,
        port=settings.port,
        **SERVER_OPTIONS,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
