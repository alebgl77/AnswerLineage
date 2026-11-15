"""Ligne de commande du corpus : ``answerlineage-corpus``.

``events`` écrit les événements OpenLineage de l'entreprise synthétique,
``truth`` la vérité de ses incidents, et ``load`` envoie les événements à une
API d'ingestion d'AnswerLineage, puis les relevés de couverture de ses
serveurs MCP. ``check`` contrôle le corpus de vérité.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from answerlineage_corpus.coverage import coverage_reports
from answerlineage_corpus.enterprise import (
    DEFAULT_DAYS,
    DEFAULT_INCIDENTS,
    DEFAULT_REQUESTS,
    Enterprise,
    generate,
)
from answerlineage_corpus.events import render
from answerlineage_corpus.loader import DEFAULT_BATCH_SIZE, LoadError, send, send_reports
from answerlineage_corpus.scenarios import CORPUS_VERSION, check_corpus, load_corpus
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
    load = commands.add_parser(
        "load", parents=[size], help="envoie les événements et les relevés de couverture à une API"
    )
    load.add_argument(
        "--url", required=True, help="adresse de l'API, par exemple http://127.0.0.1:8000"
    )
    load.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    check = commands.add_parser("check", help="contrôle le corpus de vérité")
    check.add_argument(
        "--directory", type=Path, help="dossier de scénarios, ceux du paquet par défaut"
    )
    return parser


def _check(directory: Path | None) -> int:
    problems = check_corpus(directory)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"Corpus de vérité incohérent : {len(problems)} problème(s).", file=sys.stderr)
        return 1
    corpus = load_corpus(directory)
    for scenario in corpus:
        print(
            f"{scenario.name} : niveau {scenario.level}, "
            f"{len(scenario.touched)} touchée(s), {len(scenario.discarded)} écartée(s), "
            f"{len(scenario.untouched)} hors d'atteinte"
        )
    print(
        f"Corpus de vérité cohérent : {len(corpus.scenarios)} scénarios, version {CORPUS_VERSION}."
    )
    return 0


def _enterprise(args: argparse.Namespace) -> Enterprise:
    return generate(args.seed, requests=args.requests, days=args.days, incidents=args.incidents)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "check":
        return _check(args.directory)
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
        reports = send_reports(coverage_reports(enterprise), args.url)
    except LoadError as error:
        print(f"Envoi interrompu : {error}", file=sys.stderr)
        return 1
    print(
        f"{result.accepted} événements acceptés, dont {result.duplicates} déjà connus ; "
        f"{result.rejected} rejetés."
    )
    print(f"{reports.accepted} relevés de couverture acceptés ; {reports.rejected} rejetés.")
    return 0 if result.rejected == 0 and reports.rejected == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
