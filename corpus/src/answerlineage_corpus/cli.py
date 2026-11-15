"""Ligne de commande du corpus : ``answerlineage-corpus``.

``events`` écrit les événements OpenLineage de l'entreprise synthétique,
``truth`` la vérité de ses incidents, et ``load`` envoie les événements à une
API d'ingestion d'AnswerLineage.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from answerlineage_corpus.enterprise import (
    DEFAULT_DAYS,
    DEFAULT_INCIDENTS,
    DEFAULT_REQUESTS,
    Enterprise,
    generate,
)
from answerlineage_corpus.events import render
from answerlineage_corpus.loader import DEFAULT_BATCH_SIZE, LoadError, send
from answerlineage_corpus.truth import truth

DEFAULT_SEED = 1


def _parser() -> argparse.ArgumentParser:
    size = argparse.ArgumentParser(add_help=False)
    size.add_argument("--seed", type=int, default=DEFAULT_SEED, help="graine de l'entreprise")
    size.add_argument("--requests", type=int, default=DEFAULT_REQUESTS, help="nombre de demandes")
    size.add_argument("--days", type=int, default=DEFAULT_DAYS, help="jours ouvrés couverts")
    size.add_argument("--incidents", type=int, default=DEFAULT_INCIDENTS, help="incidents")

    parser = argparse.ArgumentParser(
        prog="answerlineage-corpus",
        description="Entreprise synthétique à vérité connue, déterminée par sa graine.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("events", parents=[size], help="écrit les événements OpenLineage (JSON)")
    commands.add_parser("truth", parents=[size], help="écrit la vérité des incidents (JSON)")
    load = commands.add_parser("load", parents=[size], help="envoie les événements à une API")
    load.add_argument(
        "--url", required=True, help="adresse de l'API, par exemple http://127.0.0.1:8000"
    )
    load.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    return parser


def _enterprise(args: argparse.Namespace) -> Enterprise:
    return generate(args.seed, requests=args.requests, days=args.days, incidents=args.incidents)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        enterprise = _enterprise(args)
    except ValueError as error:
        print(f"Paramètres invalides : {error}", file=sys.stderr)
        return 2
    if args.command == "events":
        json.dump(render(enterprise), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if args.command == "truth":
        json.dump(truth(enterprise), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    try:
        result = send(render(enterprise), args.url, batch_size=args.batch_size)
    except LoadError as error:
        print(f"Envoi interrompu : {error}", file=sys.stderr)
        return 1
    print(
        f"{result.accepted} événements acceptés, dont {result.duplicates} déjà connus ; "
        f"{result.rejected} rejetés."
    )
    return 0 if result.rejected == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
