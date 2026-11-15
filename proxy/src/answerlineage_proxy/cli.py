"""Ligne de commande du proxy MCP : ``answerlineage-proxy --upstream <URL>``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence

import uvicorn

from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import (
    DEFAULT_HOST,
    DEFAULT_MAX_INSPECTED_BYTES,
    DEFAULT_PORT,
    ENV_HOST,
    ENV_PORT,
    ENV_SERVER_NAME,
    ENV_UPSTREAM,
    ProxyConfigurationError,
    ProxySettings,
)
from answerlineage_proxy.sink import NullSink

logger = logging.getLogger("answerlineage_proxy")


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
    return parser


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
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = settings_from_args(argv, os.environ)
    except ProxyConfigurationError as error:
        print(f"Configuration invalide : {error}", file=sys.stderr)
        return 2

    logger.info(
        "Proxy MCP du serveur « %s » : écoute sur %s:%d.",
        settings.server_name,
        settings.host,
        settings.port,
    )
    logger.warning(
        "Aucune destination d'événements : les appels d'outils sont comptés, "
        "l'envoi vers l'API d'ingestion arrive avec la boîte d'envoi (US-0406)."
    )
    uvicorn.run(
        McpProxy(settings, NullSink()),
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
