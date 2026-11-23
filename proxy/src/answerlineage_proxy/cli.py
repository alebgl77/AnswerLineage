"""Ligne de commande du proxy MCP : ``answerlineage-proxy --upstream <URL>``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta

from answerlineage_proxy import lifecycle
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import (
    ADMIN_HOST,
    DEFAULT_HOST,
    DEFAULT_MAX_INSPECTED_BYTES,
    DEFAULT_PORT,
    DEFAULT_SHUTDOWN_DELAY,
    DEFAULT_SHUTDOWN_GRACE,
    DEFAULT_SQL_BUDGET_MS,
    DEFAULT_SQL_CACHE_SIZE,
    DEFAULT_SQL_QUEUE_SIZE,
    ENV_ADMIN_PORT,
    ENV_ALLOWED_HOSTS,
    ENV_ALLOWED_ORIGINS,
    ENV_FINGERPRINT_KEY,
    ENV_FINGERPRINT_KEY_FILE,
    ENV_HOST,
    ENV_INSTANCE_ID,
    ENV_PORT,
    ENV_READ_RULES_FILE,
    ENV_SERVER_NAME,
    ENV_SHUTDOWN_DELAY,
    ENV_SHUTDOWN_GRACE,
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
    optional_port,
    read_rules,
    seconds,
)
from answerlineage_proxy.coverage import DEFAULT_WINDOW, MAX_WINDOW, MIN_WINDOW
from answerlineage_proxy.credentials import (
    ENV_AUDIENCE,
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    ENV_CLIENT_SECRET_FILE,
    ENV_ISSUER,
    ENV_SCOPE,
    ClientCredentials,
    ClientCredentialsTokens,
    Tokens,
    client_secret,
    secure_url,
)
from answerlineage_proxy.guard import LOOPBACK_HOSTS
from answerlineage_proxy.lifecycle import SERVER_OPTIONS as SERVER_OPTIONS
from answerlineage_proxy.outbox import (
    DEFAULT_FLUSH_SECONDS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_EVENTS,
    Outbox,
    OutboxSettings,
)
from answerlineage_proxy.quarantine import (
    DEFAULT_INTERVAL,
    INTERVAL_LIMITS,
    QuarantineSettings,
    QuarantineSync,
)
from answerlineage_proxy.quarantine_signature import ENV_SECRET as ENV_QUARANTINE_SECRET
from answerlineage_proxy.quarantine_signature import (
    ENV_SECRET_FILE as ENV_QUARANTINE_SECRET_FILE,
)
from answerlineage_proxy.quarantine_signature import read_secret
from answerlineage_proxy.resource import check_gateway
from answerlineage_proxy.sink import EventSink, NullSink

logger = logging.getLogger("answerlineage_proxy")

_MODE_NAMES = {"transparent": "transparent", "gateway": "passerelle"}

ENV_COVERAGE_WINDOW = "ANSWERLINEAGE_PROXY_COVERAGE_WINDOW_SECONDS"
# Boîte d'envoi : l'adresse de l'API d'AnswerLineage, ses bornes, son délai de vidage.
ENV_API_URL = "ANSWERLINEAGE_PROXY_API_URL"
ENV_OUTBOX_MAX_EVENTS = "ANSWERLINEAGE_PROXY_OUTBOX_MAX_EVENTS"
ENV_OUTBOX_MAX_BYTES = "ANSWERLINEAGE_PROXY_OUTBOX_MAX_BYTES"
ENV_OUTBOX_FLUSH_SECONDS = "ANSWERLINEAGE_PROXY_OUTBOX_FLUSH_SECONDS"
# Synchronisation des périmètres en quarantaine, depuis la même API.
ENV_QUARANTINE_INTERVAL = "ANSWERLINEAGE_PROXY_QUARANTINE_INTERVAL_SECONDS"


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
        help=(
            "mode passerelle : les clients joignent le proxy à sa propre URL, et l'en-tête Host "
            "devient celui du serveur MCP ; refusé pour un serveur dont les jetons sont liés à "
            "son URL"
        ),
    )
    parser.add_argument(
        "--audience-bound-tokens",
        action="store_true",
        help=(
            "déclare les jetons du serveur MCP liés à son URL (audience) : le mode passerelle "
            "est alors refusé, sans lire les métadonnées OAuth du serveur"
        ),
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
    parser.add_argument(
        "--admin-port",
        type=int,
        metavar="PORT",
        help=(
            f"port d'administration, sur {ADMIN_HOST} seulement, où se lisent la vie et la "
            f"disponibilité du proxy (/livez, /readyz) ; aucun par défaut (ou {ENV_ADMIN_PORT})"
        ),
    )
    parser.add_argument(
        "--shutdown-delay-seconds",
        type=float,
        help=(
            "délai de retrait : au signal SIGTERM, le proxy se déclare non prêt et sert encore "
            "ce temps, pour qu'un répartiteur le retire de sa rotation, "
            f"{DEFAULT_SHUTDOWN_DELAY:g} s par défaut (ou {ENV_SHUTDOWN_DELAY})"
        ),
    )
    parser.add_argument(
        "--shutdown-grace-seconds",
        type=int,
        help=(
            "délai de grâce : une fois l'écoute fermée, temps laissé aux appels en cours pour "
            f"se terminer, au-delà ils sont coupés, {DEFAULT_SHUTDOWN_GRACE} s par défaut "
            f"(ou {ENV_SHUTDOWN_GRACE})"
        ),
    )
    parser.add_argument(
        "--quarantine-interval-seconds",
        type=int,
        help=(
            "intervalle entre deux interrogations de l'API sur les périmètres en quarantaine, "
            f"de {INTERVAL_LIMITS[0]} à {INTERVAL_LIMITS[1]} secondes, {DEFAULT_INTERVAL} par "
            f"défaut (ou {ENV_QUARANTINE_INTERVAL})"
        ),
    )
    parser.add_argument(
        "--quarantine-secret-file",
        metavar="FICHIER",
        help=(
            "fichier qui contient le secret partagé avec l'API, qui vérifie la signature de la "
            f"diffusion des quarantaines (ou {ENV_QUARANTINE_SECRET_FILE}, ou le secret lui-même "
            f"dans {ENV_QUARANTINE_SECRET}) ; jamais sur la ligne de commande. Sans lui, les "
            "périmètres sont appris sans être vérifiés"
        ),
    )
    parser.add_argument(
        "--oidc-issuer",
        metavar="URL",
        help=(
            "émetteur OIDC auprès duquel le proxy obtient son jeton envers l'API, par l'octroi "
            f"client_credentials (ou {ENV_ISSUER}) ; sans lui, l'API est appelée sans jeton"
        ),
    )
    parser.add_argument(
        "--oidc-client-id",
        metavar="CLIENT",
        help=f"identifiant du client OIDC du proxy (ou {ENV_CLIENT_ID})",
    )
    parser.add_argument(
        "--oidc-client-secret-file",
        metavar="FICHIER",
        help=(
            f"fichier qui contient le secret du client OIDC (ou {ENV_CLIENT_SECRET_FILE}, ou le "
            f"secret lui-même dans {ENV_CLIENT_SECRET}) ; jamais sur la ligne de commande"
        ),
    )
    parser.add_argument(
        "--oidc-scope",
        metavar="PORTÉE",
        help=f"portée demandée avec le jeton, si l'émetteur en exige une (ou {ENV_SCOPE})",
    )
    parser.add_argument(
        "--oidc-audience",
        metavar="AUDIENCE",
        help=f"audience demandée avec le jeton, si l'émetteur en exige une (ou {ENV_AUDIENCE})",
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
            else seconds(environment, ENV_OUTBOX_FLUSH_SECONDS, DEFAULT_FLUSH_SECONDS)
        ),
    )


def quarantine_settings_from_args(
    argv: Sequence[str] | None, environment: Mapping[str, str], outbox: OutboxSettings | None
) -> QuarantineSettings | None:
    """Réglages de la synchronisation des quarantaines : l'API de la boîte d'envoi, et
    l'intervalle de l'option, sinon de l'environnement ; le secret de la diffusion, d'un
    fichier ou d'une variable. Aucune sans adresse d'API, et un secret sans elle est refusé."""
    args = build_parser().parse_args(argv)
    secret = read_secret(environment, args.quarantine_secret_file)
    if outbox is None:
        if secret is not None:
            raise ProxyConfigurationError(
                "Un secret de diffusion des quarantaines est réglé sans adresse d'API (--api-url) :"
                " il ne servirait à rien."
            )
        return None
    interval = args.quarantine_interval_seconds
    if interval is None:
        interval = integer(environment, ENV_QUARANTINE_INTERVAL, DEFAULT_INTERVAL)
    return QuarantineSettings.create(outbox.api_url, interval, secret=secret)


def credentials_from_args(
    argv: Sequence[str] | None, environment: Mapping[str, str], outbox: OutboxSettings | None
) -> ClientCredentials | None:
    """L'identité du proxy envers l'API : l'émetteur, le client et son secret, des options,
    sinon de l'environnement. Aucune sans émetteur ; un réglage partiel est refusé."""
    args = build_parser().parse_args(argv)
    issuer = args.oidc_issuer or environment.get(ENV_ISSUER) or None
    client_id = args.oidc_client_id or environment.get(ENV_CLIENT_ID) or None
    secret = client_secret(environment, args.oidc_client_secret_file)
    if issuer is None and client_id is None and secret is None:
        return None
    if issuer is None or client_id is None or secret is None:
        raise ProxyConfigurationError(
            f"Le jeton du proxy demande l'émetteur ({ENV_ISSUER}), l'identifiant du client "
            f"({ENV_CLIENT_ID}) et son secret ({ENV_CLIENT_SECRET_FILE} ou {ENV_CLIENT_SECRET})."
        )
    if outbox is None:
        raise ProxyConfigurationError(
            "Un jeton OIDC est réglé sans adresse d'API (--api-url) : il ne servirait à rien."
        )
    if not secure_url(outbox.api_url):
        raise ProxyConfigurationError(
            "Le jeton du proxy ne part qu'en https, ou en http vers la boucle locale : "
            "l'adresse de l'API doit être en https."
        )
    return ClientCredentials.create(
        issuer,
        client_id,
        secret,
        scope=args.oidc_scope or environment.get(ENV_SCOPE) or None,
        audience=args.oidc_audience or environment.get(ENV_AUDIENCE) or None,
    )


def token_source(credentials: ClientCredentials | None) -> Tokens | None:
    """Le jeton du proxy, partagé par la boîte d'envoi et la synchronisation des quarantaines."""
    if credentials is None:
        return None
    logger.info("Envois à l'API authentifiés par un jeton OIDC du proxy (client_credentials).")
    return ClientCredentialsTokens(credentials, logger=logger)


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
        audience_bound_tokens=args.audience_bound_tokens,
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
        admin_port=(
            args.admin_port
            if args.admin_port is not None
            else optional_port(environment, ENV_ADMIN_PORT)
        ),
        shutdown_delay=(
            args.shutdown_delay_seconds
            if args.shutdown_delay_seconds is not None
            else seconds(environment, ENV_SHUTDOWN_DELAY, DEFAULT_SHUTDOWN_DELAY)
        ),
        shutdown_grace=(
            args.shutdown_grace_seconds
            if args.shutdown_grace_seconds is not None
            else integer(environment, ENV_SHUTDOWN_GRACE, DEFAULT_SHUTDOWN_GRACE)
        ),
    )


def event_sink(outbox: OutboxSettings | None, tokens: Tokens | None = None) -> EventSink:
    """La destination des événements, la même pour les deux transports : la boîte d'envoi
    vers l'API quand son adresse est réglée, sinon une destination qui compte sans envoyer."""
    if outbox is None:
        logger.warning(
            "Aucune adresse d'API (--api-url ou %s) : les appels d'outils et les relevés de "
            "couverture sont comptés sans être envoyés, et aucun périmètre en quarantaine "
            "n'est reçu (mode observer).",
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
    return Outbox(outbox, tokens=tokens, logger=logger)


def quarantine_sync(
    settings: QuarantineSettings | None, tokens: Tokens | None = None
) -> QuarantineSync | None:
    """La synchronisation des quarantaines, la même pour les deux transports : aucune sans
    adresse d'API, le proxy reste alors en mode observer."""
    return None if settings is None else QuarantineSync(settings, tokens=tokens)


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
        quarantine = quarantine_settings_from_args(argv, os.environ, outbox)
        credentials = credentials_from_args(argv, os.environ, outbox)
        if settings.mode == "gateway":
            # Les métadonnées OAuth du serveur disent si ses jetons sont liés à son URL :
            # elles se lisent avant de servir, jamais sur le chemin d'un appel.
            asyncio.run(check_gateway(settings))
    except ProxyConfigurationError as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2

    logger.info(
        "Proxy MCP du serveur « %s » : écoute sur %s:%d, mode %s.",
        settings.server_name,
        settings.host,
        settings.port,
        _MODE_NAMES[settings.mode],
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
    tokens = token_source(credentials)
    proxy = McpProxy(
        settings,
        event_sink(outbox, tokens),
        coverage_window=coverage_window,
        quarantine=quarantine_sync(quarantine, tokens),
    )
    log_instance(proxy.instance)
    return lifecycle.run(proxy, settings)


if __name__ == "__main__":
    sys.exit(main())
