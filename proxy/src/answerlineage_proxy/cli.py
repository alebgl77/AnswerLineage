"""Ligne de commande du proxy MCP : ``answerlineage-proxy --upstream <URL>``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import timedelta

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
    ENV_PORT,
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
)
from answerlineage_proxy.coverage import DEFAULT_WINDOW, MAX_WINDOW, MIN_WINDOW
from answerlineage_proxy.guard import LOOPBACK_HOSTS
from answerlineage_proxy.sink import NullSink

logger = logging.getLogger("answerlineage_proxy")

ENV_COVERAGE_WINDOW = "ANSWERLINEAGE_PROXY_COVERAGE_WINDOW_SECONDS"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="answerlineage-proxy",
        description="Proxy transparent placé devant un serveur MCP en HTTP.",
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
        "--coverage-window-seconds",
        type=int,
        help=(
            "durée d'une fenêtre des relevés de couverture, de 1 à 86400 secondes, "
            f"{int(DEFAULT_WINDOW.total_seconds())} par défaut (ou {ENV_COVERAGE_WINDOW})"
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
        sql_tools=dict(SqlTool.parse(value) for value in args.sql_tool),
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
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = settings_from_args(argv, os.environ)
        coverage_window = coverage_window_from_args(argv, os.environ)
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
    logger.warning(
        "Aucune destination d'événements : les appels d'outils et les relevés de couverture "
        "sont comptés, l'envoi vers l'API d'ingestion n'est pas encore disponible."
    )
    uvicorn.run(
        McpProxy(settings, NullSink(), coverage_window=coverage_window),
        host=settings.host,
        port=settings.port,
        # Le journal d'accès contiendrait chemins et paramètres de requête ; les
        # en-têtes Server et Date du serveur MCP sont relayés tels quels.
        access_log=False,
        server_header=False,
        date_header=False,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
